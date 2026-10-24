# -*- coding: utf-8 -*-
"""
SubFlow 持久化层（SQLite 单文件）
=================================

设计取舍
--------
1. **任务与历史共用一张 `jobs` 表**，用 `status` 状态机区分，而不是建两张表。
   理由：写一份数据只有一个真相源，不会出现「任务表已成功、历史表没记录」的
   不一致；历史记录页面即 `status IN (终态)` 的查询视图。

2. **幂等靠唯一索引** `ux_jobs_site_torrent_sub(site_id, torrent_id, subid)`。
   同站点、同种子、同字幕包重复提交时 `INSERT ... ON CONFLICT` 直接复用原行，
   配合 `known_subids()` 可避免重复下载与重复落盘。

3. **WAL 模式 + 单连接加锁**。日志写入频繁但单条极小，单连接串行写入足够，
   读操作在 WAL 下不阻塞写。这样避免了连接池复杂度，容器里也稳。

时间统一使用本地时间字符串 `YYYY-MM-DD HH:MM:SS`（可排序、可读、免时区歧义）。
"""

import csv
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from .config import BASE_DIR

DB_PATH = os.getenv("SUBFLOW_DB", os.path.join(BASE_DIR, "data", "subflow.db"))

# 任务状态机
ST_PENDING = "pending"     # 已入队，等待执行
ST_RUNNING = "running"     # 执行中
ST_SUCCESS = "success"     # 下载成功
ST_EXISTS = "exists"       # 目标文件已存在（等价成功，不重复下载）
ST_SKIPPED = "skipped"     # 站点无字幕 / 已处理过
ST_FAILED = "failed"       # 失败

FINAL_STATES = (ST_SUCCESS, ST_EXISTS, ST_SKIPPED, ST_FAILED)

STATUS_LABEL = {
    ST_PENDING: "待处理",
    ST_RUNNING: "进行中",
    ST_SUCCESS: "成功",
    ST_EXISTS: "已存在",
    ST_SKIPPED: "已跳过",
    ST_FAILED: "失败",
}

_lock = threading.RLock()
_conn = None


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 连接与建表

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      TEXT    NOT NULL,
    site_label   TEXT    NOT NULL DEFAULT '',
    torrent_id   TEXT    NOT NULL,
    torrent_name TEXT    NOT NULL DEFAULT '',
    torrent_hash TEXT    NOT NULL DEFAULT '',
    subid        TEXT    NOT NULL DEFAULT '',
    sub_title    TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL,
    message      TEXT    NOT NULL DEFAULT '',
    save_path    TEXT    NOT NULL DEFAULT '',
    file_name    TEXT    NOT NULL DEFAULT '',
    file_size    INTEGER NOT NULL DEFAULT 0,
    extracted    INTEGER NOT NULL DEFAULT 0,
    source       TEXT    NOT NULL DEFAULT 'scan',
    downloader_id TEXT   NOT NULL DEFAULT '',
    category     TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    duration_ms  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_unique
    ON jobs(site_id, torrent_id, subid);
CREATE INDEX IF NOT EXISTS ix_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_site    ON jobs(site_id);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at);
-- 注意：ix_jobs_dl / ix_jobs_cat 建在 _ensure_columns 里。
-- 它们引用的列在老库中需要先 ALTER 补出来，写在 SCHEMA 里会因
-- 「索引引用尚不存在的列」直接报 no such column，导致升级后起不来。

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT 'system',
    job_id  INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_logs_ts    ON logs(ts);
CREATE INDEX IF NOT EXISTS ix_logs_level ON logs(level);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ensure_columns(conn):
    """
    轻量迁移：老库缺列时补上。

    `SCHEMA` 里的 CREATE TABLE IF NOT EXISTS 对已存在的表不会生效，
    所以新增字段必须在这里单独 ALTER 一次，否则升级后写库会报「无此列」。
    依赖这些新列的索引也必须在这里建（不能在 SCHEMA 里），顺序错了直接起不来。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in (("downloader_id", "TEXT NOT NULL DEFAULT ''"),
                      ("category", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, ddl))
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_dl  ON jobs(downloader_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_cat ON jobs(category)")


def get_conn():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _ensure_columns(_conn)
            _conn.commit()
        return _conn


def init_db():
    get_conn()


def _exec(sql, params=()):
    with _lock:
        c = get_conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur


def _query(sql, params=()):
    with _lock:
        c = get_conn()
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _one(sql, params=()):
    rows = _query(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------- 日志

MAX_MESSAGE = 4000


def log_write(level, message, source="system", job_id=None):
    level = (level or "INFO").upper()
    msg = str(message)
    if len(msg) > MAX_MESSAGE:
        msg = msg[:MAX_MESSAGE] + " ...[truncated]"
    cur = _exec(
        "INSERT INTO logs (ts, level, source, job_id, message) VALUES (?,?,?,?,?)",
        (now_str(), level, source, job_id, msg),
    )
    return cur.lastrowid


def log_query(level=None, source=None, since_id=0, limit=500, keyword=None):
    """返回 (rows, last_id)。rows 按 id 升序，便于前端增量追加。"""
    sql = "SELECT id, ts, level, source, job_id, message FROM logs WHERE id > ?"
    params = [int(since_id or 0)]
    if level and level.upper() != "ALL":
        sql += " AND level = ?"
        params.append(level.upper())
    if source and source != "all":
        sql += " AND source = ?"
        params.append(source)
    if keyword:
        sql += " AND message LIKE ?"
        params.append("%" + keyword + "%")
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(int(limit))
    rows = _query(sql, params)
    last_id = rows[-1]["id"] if rows else int(since_id or 0)
    return rows, last_id


def log_tail(limit=200):
    """最近 N 条（倒序取再正序返回）。"""
    rows = _query(
        "SELECT id, ts, level, source, job_id, message FROM logs "
        "ORDER BY id DESC LIMIT ?", (int(limit),))
    return list(reversed(rows))


def log_clear(keep_last=0):
    """清空日志；keep_last>0 时仅保留最近 N 条。返回删除行数。"""
    if keep_last and int(keep_last) > 0:
        cur = _exec("DELETE FROM logs WHERE id NOT IN "
                    "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (int(keep_last),))
    else:
        cur = _exec("DELETE FROM logs")
    return cur.rowcount or 0


def log_sources():
    return [r["source"] for r in _query(
        "SELECT DISTINCT source FROM logs ORDER BY source")]


def log_purge(retain_days=30):
    if not retain_days or int(retain_days) <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=int(retain_days))).strftime("%Y-%m-%d %H:%M:%S")
    cur = _exec("DELETE FROM logs WHERE ts < ?", (cutoff,))
    return cur.rowcount or 0


# ---------------------------------------------------------------- 任务 / 历史

JOB_FIELDS = ("site_id", "site_label", "torrent_id", "torrent_name", "torrent_hash",
              "subid", "sub_title", "status", "message", "save_path", "file_name",
              "file_size", "extracted", "source", "downloader_id", "category",
              "created_at", "started_at", "finished_at", "duration_ms")

# 建表时 NOT NULL DEFAULT，写库前要把 None 归一成空串/0，否则会撞约束
NUMERIC_FIELDS = {"file_size", "extracted", "duration_ms"}
NULLABLE_FIELDS = {"started_at", "finished_at"}


def job_upsert(site_id, torrent_id, subid, **fields):
    """
    幂等写入任务。已存在则刷新元数据（名称等）但不重置状态，返回 (id, created)。
    """
    site_id, torrent_id, subid = str(site_id), str(torrent_id), str(subid)
    exist = _one(
        "SELECT id FROM jobs WHERE site_id=? AND torrent_id=? AND subid=?",
        (site_id, torrent_id, subid))
    if exist:
        upd = {k: v for k, v in fields.items() if k in JOB_FIELDS
               and k not in ("status", "created_at")}
        upd = {k: v for k, v in upd.items() if v not in (None, "")}
        if upd:
            sets = ", ".join("%s=?" % k for k in upd)
            _exec("UPDATE jobs SET %s WHERE id=?" % sets,
                  tuple(upd.values()) + (exist["id"],))
        return exist["id"], False

    row = {}
    for k in JOB_FIELDS:
        v = fields.get(k)
        if v is None and k not in NULLABLE_FIELDS:
            v = 0 if k in NUMERIC_FIELDS else ""
        row[k] = v
    row.update(dict(site_id=site_id, torrent_id=torrent_id, subid=subid,
                    status=fields.get("status") or ST_PENDING,
                    created_at=fields.get("created_at") or now_str()))
    cols = ", ".join(JOB_FIELDS)
    marks = ", ".join("?" * len(JOB_FIELDS))
    cur = _exec("INSERT INTO jobs (%s) VALUES (%s)" % (cols, marks),
                tuple(row[k] for k in JOB_FIELDS))
    return cur.lastrowid, True


def job_update(job_id, **fields):
    """局部更新。非空字段忽略 None（避免误清空）；finished_at/started_at 允许写 NULL。"""
    upd = {}
    for k, v in fields.items():
        if k not in JOB_FIELDS:
            continue
        if v is None and k not in NULLABLE_FIELDS:
            continue
        upd[k] = v
    if not upd:
        return
    sets = ", ".join("%s=?" % k for k in upd)
    _exec("UPDATE jobs SET %s WHERE id=?" % sets, tuple(upd.values()) + (int(job_id),))


def job_get(job_id):
    return _one("SELECT * FROM jobs WHERE id=?", (int(job_id),))


def job_find(site_id, torrent_id, subid):
    """按 (站点, 种子, 字幕包) 三元组查任务，用于幂等展示与去重判断。"""
    return _one("SELECT * FROM jobs WHERE site_id=? AND torrent_id=? AND subid=?",
                (str(site_id), str(torrent_id), str(subid)))


def _job_where(status=None, statuses=None, site_id=None, keyword=None,
               downloader_id=None, category=None):
    """任务列表/计数的公共筛选条件，保证两者永远同源。"""
    sql = " WHERE 1=1"
    params = []
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        params.extend(statuses)
    if site_id and site_id != "all":
        sql += " AND site_id=?"
        params.append(site_id)
    if keyword:
        sql += " AND (torrent_name LIKE ? OR sub_title LIKE ?)"
        params.extend(["%" + keyword + "%"] * 2)
    if downloader_id and downloader_id != "all":
        sql += " AND downloader_id=?"
        params.append(downloader_id)
    if category and category != "all":
        if category == "__uncat__":
            sql += " AND category=''"
        else:
            # category 可能是多标签逗号串（TR），用两侧补逗号做整词匹配
            sql += " AND instr(',' || category || ',', ',' || ? || ',') > 0"
            params.append(category)
    return sql, params


def job_list(status=None, statuses=None, site_id=None, limit=200, offset=0,
             keyword=None, downloader_id=None, category=None, order="id DESC"):
    where, params = _job_where(status, statuses, site_id, keyword,
                               downloader_id, category)
    sql = "SELECT * FROM jobs" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return _query(sql, params)


def job_count(status=None, statuses=None, site_id=None, keyword=None,
              downloader_id=None, category=None):
    where, params = _job_where(status, statuses, site_id, keyword,
                               downloader_id, category)
    return _one("SELECT COUNT(*) AS n FROM jobs" + where, params)["n"]


def job_categories():
    """任务里出现过的分类（按下载器拆分，多标签逐个计数）。"""
    rows = _query(
        "SELECT downloader_id, category, COUNT(*) AS n FROM jobs "
        "GROUP BY downloader_id, category")
    agg = {}
    for r in rows:
        did = r["downloader_id"] or ""
        for name in (r["category"] or "").split(","):
            name = name.strip()
            agg[(did, name)] = agg.get((did, name), 0) + r["n"]
    out = [{"downloader_id": did, "category": name, "count": n}
           for (did, name), n in agg.items()]
    out.sort(key=lambda x: (-x["count"], x["category"].lower(), x["downloader_id"]))
    return out


def job_stats():
    rows = _query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    stats = {k: 0 for k in STATUS_LABEL}
    for r in rows:
        stats[r["status"]] = r["n"]
    stats["total"] = sum(stats.values())
    agg = _one("SELECT COALESCE(SUM(file_size),0) AS size, "
               "COALESCE(SUM(extracted),0) AS ext FROM jobs")
    stats["total_size"] = agg["size"]
    stats["total_extracted"] = agg["ext"]
    stats["today"] = _one(
        "SELECT COUNT(*) AS n FROM jobs WHERE date(finished_at)=date('now','localtime') "
        "AND status IN ('success','exists')")["n"]
    return stats


def job_stats_by_site():
    return _query(
        "SELECT site_id, site_label, COUNT(*) AS total, "
        "SUM(CASE WHEN status IN ('success','exists') THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        "FROM jobs GROUP BY site_id ORDER BY total DESC")


def job_delete(job_id):
    cur = _exec("DELETE FROM jobs WHERE id=?", (int(job_id),))
    return cur.rowcount or 0


def job_reset_stale():
    """进程重启后，把卡在 running 的任务复位为 pending，避免永久假运行。"""
    cur = _exec(
        "UPDATE jobs SET status=?, message=? WHERE status=?",
        (ST_PENDING, "进程重启，任务重新排队", ST_RUNNING))
    return cur.rowcount or 0


def known_subids(site_id, torrent_id):
    """已成功处理过的 subid 集合（用于幂等跳过）。"""
    rows = _query(
        "SELECT subid FROM jobs WHERE site_id=? AND torrent_id=? "
        "AND status IN (?,?,?)",
        (str(site_id), str(torrent_id), ST_SUCCESS, ST_EXISTS, ST_SKIPPED))
    return {str(r["subid"]) for r in rows}


def history_rows(site_id=None, status=None, keyword=None, start=None, end=None,
                 limit=100, offset=0, order="id DESC"):
    """历史记录 = 已进入终态的任务。"""
    sql = ("SELECT * FROM jobs WHERE status IN (%s)"
           % ",".join("?" * len(FINAL_STATES)))
    params = list(FINAL_STATES)
    if site_id and site_id != "all":
        sql += " AND site_id=?"
        params.append(site_id)
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if keyword:
        sql += " AND (torrent_name LIKE ? OR sub_title LIKE ? OR file_name LIKE ?)"
        params.extend(["%" + keyword + "%"] * 3)
    if start:
        sql += " AND COALESCE(finished_at, created_at) >= ?"
        params.append(start)
    if end:
        sql += " AND COALESCE(finished_at, created_at) <= ?"
        params.append(end)
    sql += " ORDER BY " + order + " LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return _query(sql, params)


def history_count(site_id=None, status=None, keyword=None, start=None, end=None):
    sql = ("SELECT COUNT(*) AS n FROM jobs WHERE status IN (%s)"
           % ",".join("?" * len(FINAL_STATES)))
    params = list(FINAL_STATES)
    if site_id and site_id != "all":
        sql += " AND site_id=?"
        params.append(site_id)
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if keyword:
        sql += " AND (torrent_name LIKE ? OR sub_title LIKE ? OR file_name LIKE ?)"
        params.extend(["%" + keyword + "%"] * 3)
    if start:
        sql += " AND COALESCE(finished_at, created_at) >= ?"
        params.append(start)
    if end:
        sql += " AND COALESCE(finished_at, created_at) <= ?"
        params.append(end)
    return _one(sql, params)["n"]


HISTORY_COLUMNS = [
    ("id", "编号"),
    ("finished_at", "完成时间"),
    ("site_label", "来源网站"),
    ("site_id", "站点标识"),
    ("torrent_name", "视频名称"),
    ("category", "来源分类"),
    ("sub_title", "字幕名称"),
    ("file_name", "文件名"),
    ("file_size", "大小(字节)"),
    ("extracted", "解压字幕数"),
    ("status", "状态"),
    ("message", "说明"),
    ("save_path", "保存路径"),
]


def history_csv(site_id=None, status=None, keyword=None, start=None, end=None,
                limit=50000):
    rows = history_rows(site_id, status, keyword, start, end, limit=limit, offset=0,
                        order="id DESC")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in HISTORY_COLUMNS])
    for r in rows:
        out = []
        for key, _ in HISTORY_COLUMNS:
            v = r.get(key)
            if key == "status":
                v = STATUS_LABEL.get(v, v)
            out.append("" if v is None else v)
        writer.writerow(out)
    return buf.getvalue(), len(rows)


# ---------------------------------------------------------------- 统计聚合

_OK_SQL = "CASE WHEN status IN ('success','exists') THEN 1 ELSE 0 END"
_FAIL_SQL = "CASE WHEN status='failed' THEN 1 ELSE 0 END"
_SKIP_SQL = "CASE WHEN status='skipped' THEN 1 ELSE 0 END"
_OPEN_SQL = "CASE WHEN status IN ('pending','running') THEN 1 ELSE 0 END"
_AVG_SQL = "AVG(CASE WHEN duration_ms > 0 THEN duration_ms END)"


def stats_bundle(days=30):
    """
    统计面板数据。窗口按「完成时间，缺省用创建时间」计，日期按本地时区。

    返回的 daily 会把窗口内没有任务的日期补 0，前端直接画柱状图，
    不用再处理空档。
    """
    days = max(1, min(3650, int(days or 30)))
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    where = "COALESCE(finished_at, created_at) >= ?"

    rows = _query(
        "SELECT date(COALESCE(finished_at, created_at)) AS d, COUNT(*) AS total, "
        "SUM(%s) AS ok, SUM(%s) AS failed, SUM(%s) AS skipped, SUM(%s) AS open, "
        "COALESCE(SUM(file_size),0) AS size, COALESCE(SUM(extracted),0) AS extracted "
        "FROM jobs WHERE %s GROUP BY d ORDER BY d" % (
            _OK_SQL, _FAIL_SQL, _SKIP_SQL, _OPEN_SQL, where), (since,))
    by_day = {r["d"]: r for r in rows}
    daily = []
    for i in range(days - 1, -1, -1):
        key = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append(by_day.get(key) or {
            "d": key, "total": 0, "ok": 0, "failed": 0, "skipped": 0,
            "open": 0, "size": 0, "extracted": 0})

    totals = _one(
        "SELECT COUNT(*) AS total, SUM(%s) AS ok, SUM(%s) AS failed, "
        "SUM(%s) AS skipped, SUM(%s) AS open, COALESCE(SUM(file_size),0) AS size, "
        "COALESCE(SUM(extracted),0) AS extracted, %s AS avg_ms "
        "FROM jobs WHERE %s" % (_OK_SQL, _FAIL_SQL, _SKIP_SQL, _OPEN_SQL,
                                _AVG_SQL, where), (since,)) or {}
    totals["avg_ms"] = int(totals.get("avg_ms") or 0)

    by_site = _query(
        "SELECT site_id, MAX(site_label) AS label, COUNT(*) AS total, "
        "SUM(%s) AS ok, SUM(%s) AS failed, SUM(%s) AS skipped, "
        "COALESCE(SUM(file_size),0) AS size, %s AS avg_ms "
        "FROM jobs WHERE %s GROUP BY site_id ORDER BY total DESC"
        % (_OK_SQL, _FAIL_SQL, _SKIP_SQL, _AVG_SQL, where), (since,))
    for r in by_site:
        r["avg_ms"] = int(r.get("avg_ms") or 0)

    failures = _query(
        "SELECT substr(message,1,80) AS reason, COUNT(*) AS n FROM jobs "
        "WHERE status='failed' AND %s GROUP BY reason ORDER BY n DESC LIMIT 8"
        % where, (since,))

    by_category = _query(
        "SELECT category, COUNT(*) AS total, SUM(%s) AS ok FROM jobs "
        "WHERE %s GROUP BY category ORDER BY total DESC LIMIT 20" % (_OK_SQL, where),
        (since,))

    by_source = _query(
        "SELECT source, COUNT(*) AS total, SUM(%s) AS ok FROM jobs "
        "WHERE %s GROUP BY source ORDER BY total DESC" % (_OK_SQL, where), (since,))

    return {
        "days": days,
        "since": since,
        "daily": daily,
        "totals": totals,
        "by_site": by_site,
        "failures": failures,
        "by_category": by_category,
        "by_source": by_source,
        "all_time": job_stats(),
        "generated_at": now_str(),
    }


# ---------------------------------------------------------------- KV

def kv_get(key, default=None):
    row = _one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else default


def kv_set(key, value):
    _exec("INSERT INTO kv (key, value) VALUES (?,?) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (key, str(value)))


# ---------------------------------------------------------------- 分类快照
#
# 分类清单只能从下载器读，下载器离线时界面仍要能按上次结果筛选，所以把整份
# 读取结果落进 kv。它是「某次读取的快照」而不是需要关联查询的数据，用 JSON
# 存比建表更贴合；结构随 /api/categories 的返回一起演进。

CATEGORIES_KV = "categories_snapshot"


def save_categories(data):
    kv_set(CATEGORIES_KV, json.dumps(data, ensure_ascii=False))


def load_categories():
    raw = kv_get(CATEGORIES_KV)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("downloaders"), list):
        return None
    return data


def import_legacy_state(path):
    """
    导入旧版 qbit_sub_sync_state.json：
        {torrent_id: {"subids": [...], "name": "..."}}
    仅登记为历史（status=success, source=legacy），不重复下载。
    返回导入条数。
    """
    import json
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return 0
    n = 0
    for tid, rec in (state or {}).items():
        if not isinstance(rec, dict):
            continue
        name = rec.get("name") or ""
        for sid in rec.get("subids", []) or []:
            _, created = job_upsert(
                "kamept", str(tid), str(sid),
                status=ST_SUCCESS, torrent_name=name, site_label="kamept",
                source="legacy", message="由旧版状态文件导入",
                created_at=now_str(), finished_at=now_str(),
            )
            if created:
                n += 1
    return n
