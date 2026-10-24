# -*- coding: utf-8 -*-
"""
任务引擎
========

职责边界
--------
* **分类（categories）**：读下载器的分类清单（qB 分类 / TR 标签）并统计每个分类
  下的种子数量，供界面选择。
* **扫描（scan）**：读下载器种子列表（可按分类筛选、可限定最近 N 天）→ 用各站点
  适配器从 comment 反查详情页 → 解析出字幕包 → 落库为 `pending` 任务。
  只读站点详情页，不下载。
* **提交（submit）**：手动按站点 + 种子 ID / 详情页 URL 建任务，用于补漏。
* **执行（worker）**：从队列取 `pending` 任务，下载字幕包到视频同目录，
  zip 自动解压，状态流转 `running → success/exists/failed`。

幂等性
------
同一个 (站点, 种子, 字幕包) 只会有一条任务记录；已知成功的 subid 会被
`db.known_subids()` 过滤掉，因此重复扫描不会产生重复请求或重复下载。

线程模型
--------
扫描在调用线程执行（可通过接口观察进度）；下载交给固定大小的 worker 池，
受 `download.workers` 控制。站点请求之间有 `download.req_interval` 节流。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests

from . import cookiecloud, db, sites
from .config import config
# 下载器统一走抽象层：引擎不直接依赖 qB 或 TR 的具体实现。
# `unescape_comment` 保留旧别名，避免改动散落各处的调用点。
from .downloaders import (DownloaderError, DownloaderPool,
                          unescape_comment as _unescape_comment)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SRC_SYS = "system"
SRC_SCAN = "scan"
SRC_TASK = "task"
SRC_CC = "cookiecloud"
SRC_QB = "qbittorrent"

# 定时调度在 kv 里留的时间戳（epoch 秒）与上一次结果
SCHEDULE_KV_LAST = "schedule_last_run"
SCHEDULE_KV_RESULT = "schedule_last_result"

SUB_MAGIC_ZIP = b"PK"
SUB_MAGIC_RAR = b"Rar!"
SUB_MAGIC_7Z = b"7z\xbc\xaf\x27\x1c"

# 裸字幕（未压缩）的内容特征。PT 站点的 downloadsubs.php 既可能回压缩包，
# 也可能直接回一个 .srt/.ass 文本，必须按内容判定扩展名，否则会存成 .bin。
SRT_TIMELINE = re.compile(rb"\d{1,4}\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->")
BOM = b"\xef\xbb\xbf"

# 字幕文件扩展名（配置项 download.sub_exts 缺省时使用）
DEFAULT_SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt",
                    ".smi", ".txt"}

# 压缩包扩展名：站点会在 Content-Disposition 里直接给出（如 xxx.chs.rar），
# 出现时不得再追加嗅探后缀，否则会得到 .rar.rar 这类重复后缀。
ARCHIVE_EXTS = {".rar", ".zip", ".7z"}

# 视频文件扩展名：用于在种子文件列表里定位「视频真正所在的目录」。
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".flv",
              ".mpg", ".mpeg", ".webm", ".rmvb", ".rm", ".m4v", ".vob", ".iso"}

# 字幕文件名里的语言码后缀（video.chs.srt / video.zh-cn.srt 这类）。
# Stash 的 caption 规范是 {视频名}.{语言码}.srt，这类后缀必须保留。
LANG_TAG_RE = re.compile(
    r"\.(zh|zh-cn|zh-tw|zh-hans|zh-hant|chs|cht|sc|tc|gb|big5|"
    r"eng|en|jp|ja|jpn|kor|ko|chinese|unknown)$", re.IGNORECASE)


def log(level, msg, source=SRC_SYS, job_id=None):
    db.log_write(level, msg, source=source, job_id=job_id)


def _short(obj, limit=180):
    """把异常或长文本压成单行短描述，用于日志与界面提示。"""
    text = re.sub(r"\s+", " ", str(obj) or type(obj).__name__).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


# 分类筛选里的「未分类」占位符：前端传 "" / __none__ / __uncategorized__ 都认
UNCATEGORIZED_ALIASES = ("", "__none__", "__uncategorized__")


_BSDTAR_CACHE = []


def _find_bsdtar():
    """
    返回可用的 bsdtar 可执行文件路径（无则 None），结果缓存。

    除了名为 bsdtar 的命令，还要识别「名为 tar、实为 bsdtar」的情况：
    Windows 10+ 自带的 C:\\Windows\\system32\\tar.exe 就是 libarchive 版，
    它能解 RAR/7z；而 Linux 上的 GNU tar 不能，故必须用 --version 判别。
    """
    if _BSDTAR_CACHE:
        return _BSDTAR_CACHE[0]
    found = shutil.which("bsdtar") or ""
    if not found:
        cand = shutil.which("tar")
        if cand:
            try:
                probe = subprocess.run([cand, "--version"], capture_output=True,
                                       timeout=10)
                text = (probe.stdout or b"").decode("utf-8", "replace").lower()
                if "bsdtar" in text or "libarchive" in text:
                    found = cand
            except (OSError, subprocess.SubprocessError):
                pass
    _BSDTAR_CACHE.append(found)
    return found


class SubtitleEngine:
    def __init__(self):
        self.dls = DownloaderPool()
        self.cc = cookiecloud.client
        self._dl_desc = []
        self._lock = threading.RLock()
        self._q = queue.Queue()
        self._workers = []
        self._stop_flag = threading.Event()
        self._running_jobs = set()
        self._site_status = {}
        self._scan_state = {"running": False, "phase": "", "progress": 0,
                            "total": 0, "started_at": None, "result": None}
        self._sched_stop = threading.Event()
        self._sched_thread = None
        self._session = threading.local()
        self.reload_config()

    # ------------------------------------------------------------ 配置

    def reload_config(self):
        cfg = config.all()
        c = cfg.get("cookiecloud", {})
        # 下载器：配置层负责归一（含旧 qbittorrent 段自动迁移成默认实例），
        # 这里只做装配。返回值里的错误项单独记日志，不阻断启动。
        n, errs = self.dls.configure(cfg)
        for e in errs:
            log("ERROR", "下载器配置无效（%s）：%s" % (e.get("id"), e.get("message")),
                SRC_SYS)
        desc = self.dls.describe()
        if desc != self._dl_desc:      # 只在下载器集合变化时记一条，避免刷屏
            self._dl_desc = desc
            log("INFO", "已装配 %d 个下载器：%s" % (n, "、".join(desc) or "（无）"),
                SRC_SYS)
        self.cc.configure(
            url=c.get("url", ""), uuid=c.get("uuid", ""),
            password=c.get("password", ""), timeout=c.get("timeout", 20),
            cache_ttl=c.get("cache_ttl", 300))
        sites.apply_config(cfg.get("sites", {}))
        self._dl = cfg.get("download", {})
        self._proxy_cfg = cfg.get("proxy", {})
        return cfg

    def _proxy(self):
        if not (self._proxy_cfg or {}).get("enabled"):
            return None
        http = self._proxy_cfg.get("http") or ""
        https = self._proxy_cfg.get("https") or http
        if not http and not https:
            return None
        return {"http": http or https, "https": https or http}

    def _http(self):
        s = getattr(self._session, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": UA})
            self._session.s = s
        return s

    def _get(self, url, cookie=None, referer=None, timeout=None, stream=False):
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if referer:
            headers["Referer"] = referer
        return self._http().get(
            url, headers=headers, timeout=timeout or int(self._dl.get("timeout", 20)),
            proxies=self._proxy(), allow_redirects=True, stream=stream)

    # ------------------------------------------------------------ worker 池

    def start_workers(self):
        with self._lock:
            if self._workers and any(w.is_alive() for w in self._workers):
                return
            self._stop_flag.clear()
            n = max(1, min(8, int(self._dl.get("workers", 3))))
            for i in range(n):
                t = threading.Thread(target=self._worker_loop, name="subflow-w-%d" % i,
                                     daemon=True)
                t.start()
                self._workers.append(t)
            log("INFO", "下载工作线程已启动：%d 个" % n, SRC_SYS)

    def stop_workers(self):
        self._stop_flag.set()
        for _ in self._workers:
            self._q.put(None)
        self._workers = []

    def _worker_loop(self):
        while not self._stop_flag.is_set():
            try:
                job_id = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self.run_job(job_id)
            except Exception as e:  # noqa: BLE001 - worker 不能因单任务异常退出
                log("ERROR", "任务 #%s 执行异常：%s: %s" % (job_id, type(e).__name__, e),
                    SRC_TASK, job_id)
                try:
                    db.job_update(job_id, status=db.ST_FAILED, message=str(e)[:500],
                                  finished_at=db.now_str())
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._running_jobs.discard(job_id)

    def enqueue(self, job_ids):
        n = 0
        for jid in job_ids or []:
            with self._lock:
                if jid in self._running_jobs:
                    continue
                self._running_jobs.add(jid)
            self._q.put(jid)
            n += 1
        return n

    def enqueue_pending(self, site_id=None):
        rows = db.job_list(status=db.ST_PENDING, site_id=site_id, limit=10000)
        return self.enqueue([r["id"] for r in rows])

    def queue_size(self):
        return self._q.qsize()

    def busy(self):
        with self._lock:
            return len(self._running_jobs)

    # ------------------------------------------------------------ 定时调度
    #
    # 只做一件事：按配置周期调用 scan()。不新建第二条扫描链路，
    # 因此分类/时间窗口/站点筛选/幂等全部复用同一套逻辑。
    # 时间基准存 kv（epoch 秒），容器重启不会把周期清零。

    def start_scheduler(self):
        with self._lock:
            if self._sched_thread and self._sched_thread.is_alive():
                return
            self._sched_stop.clear()
            self._sched_thread = threading.Thread(
                target=self._scheduler_loop, name="subflow-scheduler", daemon=True)
            self._sched_thread.start()
        log("INFO", "定时调度线程已启动", SRC_SYS)

    def stop_scheduler(self):
        self._sched_stop.set()
        self._sched_thread = None

    def _scheduler_loop(self):
        while not self._sched_stop.wait(20):
            try:
                self._scheduler_tick()
            except Exception as e:  # noqa: BLE001 - 调度线程不能因单次异常退出
                log("WARN", "定时调度检查失败：%s" % e, SRC_SYS)

    @staticmethod
    def _schedule_cfg():
        cfg = config.section("schedule")
        hours = float(cfg.get("interval_hours") or 12)
        return cfg, max(0.25, hours) * 3600

    def _scheduler_tick(self):
        cfg, interval = self._schedule_cfg()
        if not cfg.get("enabled"):
            return
        now = time.time()
        raw = db.kv_get(SCHEDULE_KV_LAST)
        try:
            last = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if last <= 0:
            # 首次启用只记基准时间，避免一重启就立刻扫一轮
            db.kv_set(SCHEDULE_KV_LAST, "%d" % int(now))
            log("INFO", "定时扫描已启用：每 %.1f 小时一次，下一轮 %s"
                % (interval / 3600.0,
                   datetime.fromtimestamp(now + interval).strftime("%Y-%m-%d %H:%M")),
                SRC_SYS)
            return
        if now - last < interval:
            return
        if self.scan_state().get("running"):
            return

        db.kv_set(SCHEDULE_KV_LAST, "%d" % int(now))
        days = int(cfg.get("days") or 3)
        limit = int(cfg.get("limit") or 0) or None
        log("INFO", "定时扫描触发（每 %.1f 小时）：窗口 %d 天%s"
            % (interval / 3600.0, days, "，最多 %d 个" % limit if limit else ""),
            SRC_SCAN)
        stats = self.scan(days=days, limit=limit,
                          refresh_cookie=bool(cfg.get("refresh_cookie")))
        summary = {k: stats.get(k) for k in
                   ("ok", "error", "scanned", "matched", "with_subs",
                    "jobs_created", "jobs_queued", "errors", "elapsed_ms")}
        summary["finished_at"] = db.now_str()
        try:
            db.kv_set(SCHEDULE_KV_RESULT, json.dumps(summary, ensure_ascii=False))
        except (TypeError, ValueError):
            pass

    def schedule_state(self):
        cfg, interval = self._schedule_cfg()
        raw = db.kv_get(SCHEDULE_KV_LAST)
        try:
            last = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            last = 0.0
        result = None
        raw_result = db.kv_get(SCHEDULE_KV_RESULT)
        if raw_result:
            try:
                result = json.loads(raw_result)
            except ValueError:
                result = None
        return {
            "enabled": bool(cfg.get("enabled")),
            "interval_hours": round(interval / 3600.0, 2),
            "days": int(cfg.get("days") or 3),
            "limit": int(cfg.get("limit") or 0),
            "refresh_cookie": bool(cfg.get("refresh_cookie")),
            "last_run": (datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
                         if last else ""),
            "next_run": (datetime.fromtimestamp(last + interval).strftime("%Y-%m-%d %H:%M:%S")
                         if last else ""),
            "thread_alive": bool(self._sched_thread and self._sched_thread.is_alive()),
            "scan_running": bool(self.scan_state().get("running")),
            "last_result": result,
            "now": db.now_str(),
        }

    # ------------------------------------------------------------ Cookie

    def cookie_for(self, adapter, force=False):
        """返回 (cookie_str, err)。结果按站缓存到 _site_status。"""
        try:
            if force:
                self.cc.fetch(force=True)
            cookie, count, expired = self.cc.cookie_string(
                adapter.domain, adapter.cookie_names)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            self._set_site_status(adapter.id, False, msg)
            return None, msg

        if not cookie:
            msg = ("CookieCloud 中未找到 %s 的 Cookie，请确认浏览器插件已同步该站点"
                   % adapter.domain)
            self._set_site_status(adapter.id, False, msg)
            return None, msg

        self._set_site_status(
            adapter.id, True,
            "已取到 %d 条 Cookie%s" % (count, ("，过滤过期 %d 条" % len(expired)) if expired else ""))
        return cookie, None

    def _set_site_status(self, site_id, ok, message):
        with self._lock:
            self._site_status[site_id] = {
                "cookie_ok": bool(ok),
                "message": message,
                "checked_at": db.now_str(),
            }

    def site_status(self):
        with self._lock:
            return {k: dict(v) for k, v in self._site_status.items()}

    # ------------------------------------------------------------ 分类
    #
    # 「读取下载器分类 → 按分类批量下载字幕」是这里的两件事：
    #   1) categories()  读分类清单 + 统计每个分类下的种子数量（给界面选）
    #   2) scan(categories=[...])  只扫描落在这些分类里的种子（全量，不受时间窗口限制）
    #
    # 分类是**筛选条件**而非独立流程：扫描的其余部分（站点反查、字幕解析、落盘）
    # 完全复用，所以它不会成为第二条需要单独维护的链路。

    def categories(self, downloader_id=None, keyword=""):
        """
        读取各下载器的分类清单，并统计每个分类下的种子数量。

        两条数据来源合并成一份结果：
          * **分类定义**：qB 走 ``torrents/categories``（含保存路径）；
            TR 没有这个概念，返回空表。
          * **种子数量**：一律从种子列表本地统计。
        所以即使分类接口不可用（老版本 / 权限受限 / TR），界面依然能看到
        「实际出现过的分类」，只是少了保存路径与空分类。

        每次成功读取都会把整份结果写成快照（``db.save_categories``），
        下载器离线时用 ``cached_categories()`` 取回上次结果。

        返回::

            {ok, elapsed_ms, errors:[...], saved_at,
             downloaders:[{id, name, type, ok, message, total, categorized,
                           uncategorized, cached,
                           categories:[{name, label, count, save_path}]}]}
        """
        self.reload_config()
        t0 = time.time()
        out = {"ok": True, "downloaders": [], "errors": [], "elapsed_ms": 0,
               "saved_at": ""}
        kw = (keyword or "").strip().lower()

        dls = [d for d in self.dls.enabled()
               if not downloader_id or d.did == downloader_id]
        if not dls:
            out.update(ok=False,
                       errors=["没有启用任何下载器，请先在「设置」中添加并启用"])
            out["elapsed_ms"] = int((time.time() - t0) * 1000)
            return out

        snapshot = db.load_categories() or {}
        cached_nodes = {n.get("id"): n for n in snapshot.get("downloaders", [])
                        if isinstance(n, dict)}
        fresh_nodes = []

        for dl in dls:
            node = {"id": dl.did, "name": dl.name, "type": dl.type_name,
                    "ok": True, "message": "", "total": 0, "categorized": 0,
                    "uncategorized": 0, "cached": False, "categories": []}

            # 1) 分类定义（拿不到就退化为只统计）
            defs = {}
            try:
                for c in (dl.list_categories() or []):
                    name = str(c.get("name") or "").strip()
                    if name:
                        defs[name] = str(c.get("save_path") or "")
                node["message"] = ("分类定义 %d 个" % len(defs)) if defs else \
                    "该下载器没有独立分类接口，按实际出现的分类统计"
            except Exception as e:  # noqa: BLE001 - 分类接口失败不该中断读取
                node["message"] = "分类接口不可用（%s），已改为按实际出现的分类统计" % _short(e)

            # 2) 种子列表 -> 分类计数
            try:
                torrents = dl.list_torrents(limit=dl.max_torrents,
                                            timeout=dl.list_timeout)
            except Exception as e:  # noqa: BLE001
                msg = "读取种子列表失败：%s" % _short(e)
                out["errors"].append("[%s] %s" % (dl.name, msg))
                prev = cached_nodes.get(dl.did)
                if prev:
                    # 离线时退回上次快照，至少还能按旧分类筛选
                    node = dict(prev)
                    node.update(id=dl.did, name=dl.name, type=dl.type_name,
                                ok=False, cached=True,
                                cached_at=snapshot.get("saved_at", ""),
                                message=msg + "；显示上次读取结果")
                    node["categories"] = [dict(r) for r in
                                          (prev.get("categories") or [])
                                          if isinstance(r, dict)]
                    node["categories"] = [r for r in node["categories"]
                                          if not kw or kw in str(r.get("label") or "").lower()]
                else:
                    node["ok"] = False
                    node["message"] = msg
                out["downloaders"].append(node)
                continue

            agg = {}
            for t in torrents:
                # 未分类的种子也要计数，否则数量对不上种子总数
                for c in (dl.torrent_categories(t) or [""]):
                    agg[c] = agg.get(c, 0) + 1
            node["total"] = len(torrents)
            node["uncategorized"] = agg.pop("", 0)
            node["categorized"] = node["total"] - node["uncategorized"]

            rows = []
            if node["uncategorized"]:
                rows.append({"name": "", "label": "未分类",
                             "count": node["uncategorized"], "save_path": ""})
            for name in sorted(set(defs) | set(agg), key=lambda x: x.lower()):
                rows.append({"name": name, "label": name,
                             "count": agg.get(name, 0),
                             "save_path": defs.get(name, "")})
            # 数量多的排前面：一眼就能挑出大分类直接批量处理
            rows.sort(key=lambda r: -r["count"])
            fresh_nodes.append(dict(node, categories=rows))
            node["categories"] = [r for r in rows
                                  if not kw or kw in r["label"].lower()]
            out["downloaders"].append(node)

        # 用这次成功读到的节点覆盖快照；离线节点保留旧数据但不刷新快照时间
        if fresh_nodes:
            merged = {n.get("id"): n for n in snapshot.get("downloaders", [])
                      if isinstance(n, dict)}
            for n in fresh_nodes:
                merged[n["id"]] = n
            ordered = [merged[d.did] for d in self.dls.enabled() if d.did in merged]
            out["saved_at"] = self._save_category_snapshot(ordered)
        else:
            out["saved_at"] = snapshot.get("saved_at", "")

        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        stale = sum(1 for d in out["downloaders"] if d.get("cached"))
        log("INFO", "分类读取完成：%s（%.1fs%s）"
            % ("；".join("%s 共 %d 个种子 / %d 个分类 / 未分类 %d"
                         % (d["name"], d["total"], len(d["categories"]),
                            d["uncategorized"]) for d in out["downloaders"]),
               out["elapsed_ms"] / 1000.0,
               "，%d 个下载器使用缓存" % stale if stale else ""), SRC_SCAN)
        return out

    @staticmethod
    def _save_category_snapshot(nodes):
        """把完整分类结果写入快照，返回本次保存时间。"""
        saved_at = db.now_str()
        try:
            db.save_categories({"saved_at": saved_at, "downloaders": nodes})
            log("INFO", "分类快照已保存：%d 个下载器 / %d 个分类"
                % (len(nodes), sum(len(n.get("categories") or []) for n in nodes)),
                SRC_SCAN)
        except Exception as e:  # noqa: BLE001 - 快照失败不影响本次读取
            log("WARN", "分类快照保存失败：%s" % _short(e), SRC_SCAN)
        return saved_at

    def cached_categories(self, keyword=""):
        """读取上次分类快照，供界面启动时直接回显；没有快照返回空表。"""
        data = db.load_categories()
        kw = (keyword or "").strip().lower()
        out = {"ok": True, "cached": bool(data), "saved_at": "",
               "errors": [], "elapsed_ms": 0, "downloaders": []}
        if not data:
            return out
        out["saved_at"] = data.get("saved_at", "")
        for raw in data["downloaders"]:
            if not isinstance(raw, dict):
                continue
            node = dict(raw)
            node["cached"] = True
            rows = [r for r in (raw.get("categories") or [])
                    if isinstance(r, dict)]
            if kw:
                rows = [r for r in rows
                        if kw in str(r.get("label") or "").lower()]
            node["categories"] = rows
            out["downloaders"].append(node)
        return out

    @staticmethod
    def _split_cat_item(raw, dl_ids):
        """
        解析一条分类筛选项，返回 (下载器ID, 分类名)；空名表示「未分类」。

        线上格式为 ``<下载器ID>:<分类名>``（多下载器时用于消歧）或裸的分类名。
        只有前缀确实是已知下载器 ID 时才按分隔符拆——分类名本身可能含冒号，
        所以不能无脑 split。
        """
        s = str(raw or "").strip()
        if not s:
            return None
        did, name = "", s
        if ":" in s:
            head, tail = s.split(":", 1)
            if head in (dl_ids or ()):
                did, name = head, tail.strip()
        if name in UNCATEGORIZED_ALIASES:
            name = ""
        return did, name

    @classmethod
    def _parse_category_filter(cls, items, dl_ids):
        """
        把分类筛选归一成 ``{下载器ID 或 "*": {分类名, ...}}``。

        空 dict 表示「不筛选」——这是扫描默认放行全部种子的判据。
        ``"*"`` 桶用于裸分类名：同名的分类在哪个下载器上都算命中。
        """
        spec = {}
        for raw in (items or []):
            pair = cls._split_cat_item(raw, dl_ids)
            if not pair:
                continue
            did, name = pair
            spec.setdefault(did or "*", set()).add(name)
        return spec

    @classmethod
    def _category_filter_labels(cls, items, dl_ids=None):
        """分类筛选的可读标签，用于日志与界面回显。"""
        out = []
        for raw in (items or []):
            pair = cls._split_cat_item(raw, dl_ids or ())
            if not pair:
                continue
            did, name = pair
            label = name or "未分类"
            out.append("%s / %s" % (did, label) if did else label)
        return out

    @staticmethod
    def _cat_match(spec, did, cats):
        """
        判断一个种子是否命中分类筛选。

        * ``spec`` 为空 = 不筛选，全部放行
        * ``cats`` 为该种子归属的分类（可为空 = 未分类）
        """
        if not spec:
            return True
        has = set(cats or [])
        for names in (spec.get(did), spec.get("*")):
            if not names:
                continue
            if "" in names and not has:      # 选中的「未分类」
                return True
            if has & names:
                return True
        return False

    @staticmethod
    def _category_join(dl, torrent):
        """种子 -> 落库用的分类字符串（多标签用逗号连接）。"""
        try:
            return ",".join(dl.torrent_categories(torrent) or [])
        except Exception:  # noqa: BLE001 - 分类只作展示，绝不能拖垮扫描
            return ""

    # ------------------------------------------------------------ 扫描

    def scan_state(self):
        with self._lock:
            return dict(self._scan_state)

    def _scan_progress(self, phase, done, total):
        with self._lock:
            self._scan_state.update(phase=phase, progress=done, total=total)
        if total and done % 25 == 0:
            log("INFO", "扫描进度：%s %d/%d" % (phase, done, total), SRC_SCAN)

    def scan(self, site_ids=None, days=None, limit=None, dry_run=False,
             refresh_cookie=False, categories=None):
        """
        扫描下载器种子库并建立字幕下载任务。

        * ``days``：只处理最近 N 天添加的种子；**<= 0 表示不限时间窗口**
          （按分类全量处理时必须能这么用，所以不能写成 `days or 默认值`）。
        * ``categories``：分类筛选，元素形如 ``"<下载器ID>:<分类名>"`` 或裸分类名，
          ``""``/``__none__`` 表示未分类。空/None 表示不筛选。

        返回统计 dict。
        """
        with self._lock:
            if self._scan_state.get("running"):
                return {"ok": False, "error": "已有扫描在进行中"}
            self._scan_state = {"running": True, "phase": "启动", "progress": 0,
                                "total": 0, "started_at": db.now_str(), "result": None}

        t0 = time.time()
        stats = {"ok": True, "qb_version": "", "scanned": 0, "matched": 0,
                 "details_ok": 0, "with_subs": 0, "jobs_created": 0,
                 "jobs_queued": 0, "skipped_dirs": 0, "errors": 0,
                 "skipped_category": 0, "categories": [],
                 "sites": {}, "dry_run": bool(dry_run), "elapsed_ms": 0}

        try:
            self.reload_config()
            # days=None -> 用配置里的默认窗口；显式传 0 -> 不限窗口
            days = int(self._dl.get("scan_days", 30) if days is None else days)
            cat_ids = {d.did for d in self.dls.enabled()}
            cat_spec = self._parse_category_filter(categories, cat_ids)
            cat_labels = self._category_filter_labels(categories, cat_ids)
            stats["categories"] = cat_labels
            targets = self._target_sites(site_ids)
            if not targets:
                raise RuntimeError("没有启用任何站点，请先在「设置」中开启站点")

            log("INFO", "=== 扫描开始：站点 %s，窗口 %s%s%s ==="
                % (", ".join(s.id for s in targets),
                   ("%d 天" % days) if days > 0 else "不限",
                   ("，分类 " + "、".join(cat_labels)) if cat_spec else "",
                   "（演练模式）" if dry_run else ""), SRC_SCAN)

            # 1) 探测各下载器
            dls = self.dls.enabled()
            if not dls:
                raise RuntimeError("没有启用任何下载器，请先在「设置」中添加")
            self._scan_progress("连接下载器", 0, len(dls))
            for i, dl in enumerate(dls, 1):
                ok, msg = dl.health()
                log("INFO" if ok else "ERROR", "下载器 %s：%s" % (dl.name, msg), SRC_SCAN)
                if not ok:
                    raise RuntimeError("下载器 %s 不可用：%s" % (dl.name, msg))
                self._scan_progress("连接下载器", i, len(dls))
            stats["downloaders"] = [dl.to_dict() for dl in dls]

            # 2) 逐个下载器取种子列表
            # 按添加时间倒序限量拉取：limit 给极大值会让服务端读超时，
            # 而扫描本身就是按「最近 N 天」筛，取回最近的一批即可。
            self._scan_progress("读取种子列表", 0, len(dls))
            cutoff = (datetime.now() - timedelta(days=days)) if days > 0 else None
            recent = []        # [(下载器, 归一化种子)]
            for i, dl in enumerate(dls, 1):
                try:
                    ts = dl.list_torrents(limit=dl.max_torrents,
                                          timeout=dl.list_timeout)
                except DownloaderError as e:
                    # 单个下载器挂掉不该让整轮扫描失败，记错并继续
                    stats["errors"] += 1
                    log("ERROR", "读取 %s 的种子列表失败，已跳过：%s"
                        % (dl.name, e), SRC_SCAN)
                    continue
                n0 = len(recent)
                for t in ts:
                    # 分类筛选先于时间窗口：不符合分类的连时间都不用算
                    if not self._cat_match(cat_spec, dl.did, dl.torrent_categories(t)):
                        stats["skipped_category"] += 1
                        continue
                    try:
                        added = datetime.fromtimestamp(t.get("added_on", 0) or 0)
                    except (OSError, ValueError):
                        continue
                    if cutoff and added < cutoff:
                        continue
                    recent.append((dl, t))
                log("INFO", "%s：种子 %d 个，按%s纳入 %d 个"
                    % (dl.name, len(ts),
                       ("近 %d 天窗口" % days) if days > 0 else "不限时间窗口",
                       len(recent) - n0), SRC_SCAN)
                self._scan_progress("读取种子列表", i, len(dls))
            recent.sort(key=lambda x: x[1].get("added_on", 0) or 0, reverse=True)
            if limit:
                recent = recent[:int(limit)]
            stats["scanned"] = len(recent)
            if cat_spec:
                log("INFO", "分类筛选 %s：排除 %d 个不在所选分类内的种子"
                    % ("、".join(cat_labels), stats["skipped_category"]), SRC_SCAN)
            log("INFO", "合计 %d 个种子纳入扫描" % len(recent), SRC_SCAN)

            # 3) 归集 comment
            # 实测：qB 5.2 的 torrents/info 与 TR 的 torrent-get 都直接返回 comment，
            # 所以默认零额外请求（旧实现对每个种子都发一次 properties，纯属浪费）。
            # 只有列表接口不带该字段的下载器（老版本 qB）才走逐条补取的慢路径，
            # 由 downloader.info_has_comment 判定。
            comments = {}
            slow = []          # [(下标, 下载器, 种子)]
            for i, (dl, t) in enumerate(recent):
                cm = t.get("comment") or ""
                comments[i] = _unescape_comment(cm)
                if not cm and not getattr(dl, "info_has_comment", True):
                    slow.append((i, dl, t))
            if slow:
                log("INFO", "%d 个种子需逐条补取 comment（该下载器列表接口不返回此字段）"
                    % len(slow), SRC_SCAN)
                with ThreadPoolExecutor(max_workers=8) as pool:
                    def fetch(item):
                        idx, dl, t = item
                        try:
                            return idx, dl.torrent_comment(t.get("hash"))
                        except Exception:
                            return idx, ""
                    done = 0
                    for idx, cm in pool.map(fetch, slow):
                        comments[idx] = _unescape_comment(cm)
                        done += 1
                        self._scan_progress("补取种子备注", done, len(slow))
            else:
                self._scan_progress("读取种子备注", 1, 1)

            # 4) 站点匹配
            todo = []          # (下载器, torrent, adapter, torrent_id, real_dir)
            for i, (dl, t) in enumerate(recent):
                cm = comments.get(i, "")
                if not cm:
                    continue
                for ad in targets:
                    tid = ad.parse_torrent_id(cm)
                    if not tid:
                        continue
                    stats["matched"] += 1
                    # 路径映射跟着种子的来源下载器走：多个下载器的挂载点可能不同
                    real_dir = dl.map_save_path(t.get("save_path", ""))
                    todo.append((dl, t, ad, tid, real_dir))
                    break

            log("INFO", "命中本站点种子 %d 个" % len(todo), SRC_SCAN)
            if not todo:
                stats["elapsed_ms"] = int((time.time() - t0) * 1000)
                self._scan_progress("完成", 1, 1)
                return stats

            # 5) 逐个站点拉详情页解析字幕
            interval = float(self._dl.get("req_interval", 1.5))
            max_subs = int(self._dl.get("max_subs_per_job", 0) or 0)
            cookie_cache = {}

            for idx, (dl, t, ad, tid, real_dir) in enumerate(todo, 1):
                name = (t.get("name") or "")[:120]
                self._scan_progress("解析字幕 (%s)" % ad.label, idx, len(todo))

                if ad.id not in cookie_cache:
                    cookie, err = self.cookie_for(ad, force=refresh_cookie)
                    cookie_cache[ad.id] = cookie
                    if err:
                        log("ERROR", "[%s] Cookie 不可用：%s" % (ad.label, err), SRC_CC)
                cookie = cookie_cache.get(ad.id)
                if not cookie:
                    stats["errors"] += 1
                    continue

                stat = stats["sites"].setdefault(
                    ad.id, {"label": ad.label, "checked": 0, "with_subs": 0,
                            "jobs": 0, "errors": 0})
                stat["checked"] += 1

                if not os.path.isdir(real_dir):
                    stats["skipped_dirs"] += 1
                    log("WARN", "[%s] 保存目录不存在，跳过：%s -> %s"
                        % (ad.label, name, real_dir), SRC_SCAN)
                    continue

                html, err = self.fetch_detail(ad, tid, cookie)
                if html is None:
                    stats["errors"] += 1
                    stat["errors"] += 1
                    if err == "cookie":
                        log("ERROR", "[%s] Cookie 已失效，请重新登录站点让插件同步"
                            % ad.label, SRC_CC)
                        break
                    continue
                stats["details_ok"] += 1
                time.sleep(interval)

                subs = ad.parse_subtitles(html, tid)
                if not subs:
                    continue
                if max_subs:
                    subs = subs[:max_subs]
                stats["with_subs"] += 1
                stat["with_subs"] += 1

                known = db.known_subids(ad.id, tid)
                fresh = [(sid, title) for sid, title in subs if str(sid) not in known]
                if not fresh:
                    continue
                log("INFO", "[%s] %s：发现 %d 个字幕包（新增 %d）"
                    % (ad.label, name, len(subs), len(fresh)), SRC_SCAN)

                if dry_run:
                    for sid, title in fresh:
                        log("INFO", "  [演练] 将下载 subid=%s %s -> %s"
                            % (sid, title, real_dir), SRC_SCAN)
                    stats["jobs_created"] += len(fresh)
                    continue

                job_ids = []
                for sid, title in fresh:
                    jid, created = db.job_upsert(
                        ad.id, tid, sid, site_label=ad.label,
                        torrent_name=name, torrent_hash=t.get("hash", ""),
                        sub_title=title, status=db.ST_PENDING, save_path=real_dir,
                        downloader_id=dl.did, category=self._category_join(dl, t),
                        source="scan")
                    if created:
                        stats["jobs_created"] += 1
                        stat["jobs"] += 1
                        job_ids.append(jid)
                stats["jobs_queued"] += self.enqueue(job_ids)

            if not dry_run:
                self.enqueue_pending()

            stats["elapsed_ms"] = int((time.time() - t0) * 1000)
            log("INFO", "=== 扫描完成：命中 %d，有字幕 %d，新建任务 %d，入队 %d，错误 %d%s，耗时 %.1fs ==="
                % (stats["matched"], stats["with_subs"], stats["jobs_created"],
                   stats["jobs_queued"], stats["errors"],
                   ("，分类外跳过 %d" % stats["skipped_category"]) if cat_spec else "",
                   stats["elapsed_ms"] / 1000.0),
                SRC_SCAN)
            self._scan_progress("完成", 1, 1)
            return stats

        except Exception as e:  # noqa: BLE001
            stats["ok"] = False
            stats["error"] = str(e)
            stats["elapsed_ms"] = int((time.time() - t0) * 1000)
            log("ERROR", "扫描失败：%s" % e, SRC_SCAN)
            self._scan_progress("失败", 0, 0)
            return stats
        finally:
            with self._lock:
                self._scan_state["running"] = False
                self._scan_state["result"] = stats

    def _target_sites(self, site_ids):
        enabled = sites.enabled_sites()
        if not site_ids:
            return [s for s in enabled if s.supports_scan]
        want = {str(s).strip().lower() for s in site_ids if str(s).strip()}
        return [s for s in enabled if s.id in want and s.supports_scan]

    # ------------------------------------------------------------ 详情页

    def fetch_detail(self, adapter, torrent_id, cookie):
        """返回 (html, err)。err 为 'cookie' 时表示登录态失效。"""
        url = adapter.detail_url(torrent_id)
        try:
            r = self._get(url, cookie=cookie, referer=adapter.referer())
        except requests.RequestException as e:
            log("ERROR", "[%s] 详情页请求失败 tid=%s：%s" % (adapter.label, torrent_id, e),
                SRC_TASK)
            return None, "network"

        if r.status_code in (301, 302, 403, 404) or r.status_code >= 500:
            log("WARN", "[%s] 详情页 HTTP %s tid=%s" % (adapter.label, r.status_code, torrent_id),
                SRC_TASK)
            return None, "http"
        html = r.text or ""
        if not html.strip():
            return None, "empty"
        if adapter.looks_logged_out(html):
            return None, "cookie"
        return html, None

    # ------------------------------------------------------------ 手动提交

    def submit(self, site_id, target, save_dir=None, refresh_cookie=False,
               auto_run=True):
        """
        手动建任务。
        target 支持多行，每行可以是：
          * 纯数字种子 ID           43480
          * 详情页 URL              https://kamept.com/details.php?id=43480
          * 字幕页 URL 片段          downloadsubs.php?torrentid=43480&subid=123
        save_dir 为空时使用配置项 download.manual_save_dir。
        """
        self.reload_config()
        out = {"ok": True, "site": site_id, "parsed": 0, "jobs_created": 0,
               "jobs_queued": 0, "errors": [], "items": []}
        adapter = sites.get(site_id)
        if not adapter:
            out.update(ok=False, errors=["未知站点：%s" % site_id])
            return out

        cookie, err = self.cookie_for(adapter, force=refresh_cookie)
        if not cookie:
            out.update(ok=False, errors=[err or "Cookie 不可用"])
            return out

        lines = [x.strip() for x in re.split(r"[\r\n,;]+", target or "") if x.strip()]
        if not lines:
            out.update(ok=False, errors=["请填写种子 ID 或详情页链接"])
            return out

        job_ids, seen_tids = [], {}
        for line in lines:
            tid, subid = self._parse_target(adapter, line)
            if not tid:
                out["errors"].append("无法解析：%s" % line[:80])
                continue
            out["parsed"] += 1
            if tid not in seen_tids:
                html, ferr = self.fetch_detail(adapter, tid, cookie)
                if html is None:
                    out["errors"].append(
                        "获取详情页失败 tid=%s（%s）" % (tid, "Cookie 失效" if ferr == "cookie" else ferr))
                    seen_tids[tid] = None
                    continue
                seen_tids[tid] = {
                    "name": adapter.parse_name(html) or ("种子 %s" % tid),
                    "subs": adapter.parse_subtitles(html, tid),
                }
                time.sleep(float(self._dl.get("req_interval", 1.5)))

            info = seen_tids.get(tid)
            if not info:
                continue
            if not info["subs"]:
                out["items"].append({"torrent_id": tid, "name": info["name"],
                                     "created": 0, "note": "该种子没有字幕包"})
                continue

            # 指定了具体 subid 就只建那一个，否则全建
            picked = [s for s in info["subs"] if subid and str(s[0]) == str(subid)] or \
                     ([] if subid else info["subs"])
            if subid and not picked:
                out["errors"].append("种子 %s 上未找到 subid=%s" % (tid, subid))
                continue

            # 手动提交无法从 qB 拿到保存目录，用入参或兜底目录
            target_dir = save_dir or self._manual_save_dir()
            if not target_dir:
                out["errors"].append("请指定保存目录（或在设置中配置 download.manual_save_dir）")
                continue
            if not os.path.isdir(target_dir):
                try:
                    os.makedirs(target_dir, exist_ok=True)
                except OSError as e:
                    out["errors"].append("保存目录不可用：%s（%s）" % (target_dir, e))
                    continue

            created_here = 0
            for sid, title in picked:
                jid, created = db.job_upsert(
                    adapter.id, tid, sid, site_label=adapter.label,
                    torrent_name=info["name"], sub_title=title,
                    status=db.ST_PENDING, save_path=target_dir, source="manual")
                if created:
                    created_here += 1
                    job_ids.append(jid)
            out["jobs_created"] += created_here
            out["items"].append({"torrent_id": tid, "name": info["name"],
                                 "created": created_here})

        if auto_run and job_ids:
            out["jobs_queued"] = self.enqueue(job_ids)
        log("INFO", "[%s] 手动提交：解析 %d 条，新建任务 %d，入队 %d"
            % (adapter.label, out["parsed"], out["jobs_created"], out["jobs_queued"]), SRC_SCAN)

        # 顺手回填种子名称，便于历史记录展示
        return out

    def _manual_save_dir(self):
        d = self._dl.get("manual_save_dir") or ""
        if d:
            os.makedirs(d, exist_ok=True)
            return d
        # 退而求其次：用下载根目录
        root = self._dl.get("manual_root") or ""
        if root and os.path.isdir(root):
            return root
        return ""

    def _parse_target(self, adapter, line):
        """把一行输入解析成 (torrent_id, subid|None)。"""
        sid = None
        sm = re.search(r"[?&](?:amp;)?subid=(\d+)", line)
        if sm:
            sid = sm.group(1)
        m = re.search(r"details\.php\?[^#\s]*?\bid=(\d+)", line, re.I)
        if m:
            return m.group(1), sid
        m = re.search(r"downloadsubs\.php\?[^#\s]*?torrentid=(\d+)", line, re.I)
        if m:
            return m.group(1), sid
        if re.fullmatch(r"\d{1,12}", line):
            return line, sid
        # 其它形式的 URL 交给适配器按域名解析
        tid = adapter.parse_torrent_id(line)
        return (tid, sid) if tid else (None, None)

    # ------------------------------------------------------------ 站点级字幕区
    #
    # 与「扫描」的根本区别：
    #   * 扫描是**以本地种子为起点**反查站点（种子级）——需要本地有该种子，
    #     才能把字幕落到视频同目录；
    #   * 字幕区是**以站点为起点**罗列全部字幕（站点级）——覆盖本地没有的种子、
    #     以及详情页已失效但字幕仍留在站内的历史条目。
    # 两者互补，互不替代。

    def zone_list(self, site_id="all", pages=1, keyword="", refresh_cookie=False):
        """
        读取站点字幕区。
        pages <= 0 表示自动翻到最后一页（依据站点的分页链接判断）。
        返回 {ok, entries, sites, errors, elapsed_ms}
        """
        self.reload_config()
        t0 = time.time()
        out = {"ok": True, "entries": [], "sites": {}, "errors": [], "elapsed_ms": 0}

        targets = self._zone_sites(site_id)
        if not targets:
            out.update(ok=False, errors=["没有启用支持字幕区的站点"])
            return out

        idx = self._local_torrent_index()
        kw = (keyword or "").strip().lower()
        interval = float(self._dl.get("req_interval", 1.5))

        for ad in targets:
            stat = {"label": ad.label, "pages": 0, "fetched": 0, "matched": 0,
                    "error": ""}
            out["sites"][ad.id] = stat

            cookie, err = self.cookie_for(ad, force=refresh_cookie)
            if not cookie:
                stat["error"] = err or "Cookie 不可用"
                out["errors"].append("[%s] %s" % (ad.label, stat["error"]))
                continue

            want = int(pages or 0)
            all_pages = want <= 0
            page = 0
            while True:
                try:
                    r = self._get(ad.subtitle_zone_url(page), cookie=cookie,
                                  referer=ad.referer())
                except requests.RequestException as e:
                    stat["error"] = "请求失败：%s" % e
                    out["errors"].append("[%s] %s" % (ad.label, stat["error"]))
                    break
                if r.status_code != 200:
                    stat["error"] = "HTTP %s" % r.status_code
                    out["errors"].append("[%s] 字幕区页面 HTTP %s"
                                         % (ad.label, r.status_code))
                    break

                html = r.text or ""
                if ad.looks_logged_out(html):
                    stat["error"] = "Cookie 已失效"
                    log("ERROR", "[%s] 字幕区读取失败：登录态失效" % ad.label, SRC_CC)
                    out["errors"].append("[%s] Cookie 已失效，请重新登录站点让插件同步"
                                         % ad.label)
                    break

                rows = ad.parse_subtitle_zone(html)
                stat["pages"] += 1
                stat["fetched"] += len(rows)

                for row in rows:
                    row["site_id"] = ad.id
                    row["site_label"] = ad.label
                    rec = self._match_local(idx, ad, row["torrent_id"],
                                            row.get("title", ""))
                    row["local"] = rec or {}
                    row["matched"] = bool(rec)
                    if rec:
                        stat["matched"] += 1
                    job = db.job_find(ad.id, row["torrent_id"], row["subid"])
                    row["job_id"] = job["id"] if job else None
                    row["job_status"] = job["status"] if job else ""
                    if kw:
                        hay = "%s %s" % (row.get("title", ""),
                                         (rec or {}).get("name", ""))
                        if kw not in hay.lower():
                            continue
                    out["entries"].append(row)

                if not all_pages and page + 1 >= want:
                    break
                if all_pages and page >= ad.zone_last_page(html):
                    break
                page += 1
                time.sleep(interval)

        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        log("INFO", "字幕区读取完成：%d 个站点，%d 条条目，命中本地 %d 条，耗时 %.1fs"
            % (len(targets), len(out["entries"]),
               sum(s["matched"] for s in out["sites"].values()),
               out["elapsed_ms"] / 1000.0), SRC_SCAN)
        return out

    def _zone_sites(self, site_id):
        if site_id and site_id != "all":
            ad = sites.get(site_id)
            return [ad] if ad and ad.supports_subtitle_zone else []
        return [s for s in sites.enabled_sites() if s.supports_subtitle_zone]

    def _local_torrent_index(self):
        """
        本地种子索引：(站点, 种子ID) -> 记录，以及归一化名称 -> 记录。

        遍历所有启用的下载器——同一部片子可能挂在 qB、也可能挂在 TR，
        路径映射按各下载器自己的 save_map 走（挂载点不一定相同）。
        """
        idx = {"by_tid": {}, "by_name": {}, "ok": False, "downloaders": []}
        dls = self.dls.enabled()
        if not dls:
            return idx

        total = 0
        for dl in dls:
            try:
                torrents = dl.list_torrents(limit=dl.max_torrents,
                                            timeout=dl.list_timeout)
            except Exception as e:  # noqa: BLE001 - 索引失败只降级，不阻断字幕区浏览
                log("WARN", "读取 %s 的种子库失败，其条目将无法自动定位本地目录：%s"
                    % (dl.name, e), SRC_SCAN)
                continue
            idx["ok"] = True
            idx["downloaders"].append(dl.did)
            total += len(torrents)
            for t in torrents:
                rec = {"hash": t.get("hash", ""),
                       "name": t.get("name") or "",
                       "downloader_id": dl.did,
                       "category": self._category_join(dl, t),
                       "save_path": dl.map_save_path(t.get("save_path") or "")}
                cm = t.get("comment") or ""
                if not cm and not getattr(dl, "info_has_comment", True):
                    cm = dl.torrent_comment(t.get("hash"))
                cm = _unescape_comment(cm)
                if cm:
                    for ad in sites.all_sites():
                        tid = ad.parse_torrent_id(cm)
                        if tid:
                            idx["by_tid"].setdefault((ad.id, str(tid)), rec)
                            break
                key = self._norm_name(rec["name"])
                if key and key not in idx["by_name"]:
                    idx["by_name"][key] = rec
        if idx["ok"]:
            log("INFO", "本地种子索引：%d 个下载器 / %d 个种子"
                % (len(idx["downloaders"]), total), SRC_SCAN)
        return idx

    @staticmethod
    def _norm_name(name):
        """归一化资源名用于粗匹配：去掉发布标记，只留字母数字与 CJK。"""
        s = (name or "").lower()
        s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s)
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)

    @staticmethod
    def _match_local(idx, adapter, torrent_id, title):
        """字幕区条目 -> 本地种子记录。先按 (站点,种子ID) 精确匹配，再按名称包含。"""
        rec = idx["by_tid"].get((adapter.id, str(torrent_id)))
        if rec:
            return rec
        key = SubtitleEngine._norm_name(title)
        if len(key) < 8:
            return None
        for k, cand in idx["by_name"].items():
            if len(k) >= 8 and (k in key or key in k):
                return cand
        return None

    # ------------------------------------------------------------ 缺字幕清单
    #
    # 与扫描的分工：扫描以站点详情页为准（站点有字幕包才建任务）；
    # 缺字幕清单以**本地文件**为准 —— 先找出旁边没有字幕的视频，
    # 再从种子 comment 反查它属于哪个站点，才能一键补。
    # 这一步只读本地文件系统，不产生站点请求。

    def missing_config(self):
        video = {str(x).lower() for x in (config.get("missing", "video_exts") or [])}
        try:
            max_files = int(config.get("missing", "max_files", 3000) or 3000)
        except (TypeError, ValueError):
            max_files = 3000
        return {
            "video_exts": video or set(VIDEO_EXTS),
            "max_depth": max(0, int(config.get("missing", "max_depth", 2) or 0)),
            "max_files": max(50, max_files),
            "sub_exts": {str(x).lower() for x in (self._dl.get("sub_exts") or [])}
                        or DEFAULT_SUB_EXTS,
        }

    @staticmethod
    def _walk_videos(root, exts, max_depth, budget):
        """有界遍历：限制深度与文件数，避免超大合集拖慢整轮检查。"""
        out = []
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            return out
        base = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.rstrip(os.sep).count(os.sep) - base
            if depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in exts:
                    out.append(os.path.join(dirpath, fn))
                    if len(out) >= budget:
                        return out
        return out

    @classmethod
    def _has_subtitle(cls, video_path, sub_exts, cache=None):
        """
        判断视频旁边是否已有字幕。

        认三种位置：同目录、常见的 Subs/Subtitles 子目录、以视频基名命名的子目录。
        文件名判定与下载落盘保持一致：基名相等，或以「基名.」开头（语言码后缀）。
        """
        folder = os.path.dirname(video_path)
        stem = os.path.splitext(os.path.basename(video_path))[0].lower()
        folders = [folder,
                   os.path.join(folder, "Subs"), os.path.join(folder, "subs"),
                   os.path.join(folder, "Subtitles"), os.path.join(folder, "subtitles"),
                   os.path.join(folder, stem)]
        for d in folders:
            key = d.lower()
            names = cache.get(key) if cache is not None else None
            if names is None:
                try:
                    names = frozenset(os.listdir(d))
                except OSError:
                    names = frozenset()
                if cache is not None:
                    cache[key] = names
            for fn in names:
                if os.path.splitext(fn)[1].lower() not in sub_exts:
                    continue
                stem2 = os.path.splitext(fn)[0].lower()
                if stem2 == stem or stem2.startswith(stem + "."):
                    return True
        return False

    def missing_scan(self, days=None, downloader_id=None, category="", keyword="",
                     limit=200, only_site_matched=False):
        """
        扫描本地视频目录，列出没有字幕的视频。

        * ``days=None`` 用配置的扫描窗口；``<= 0`` 表示不限时间。
        * ``only_site_matched`` 只返回能从 comment 反查到站点的条目（可直接补字幕）。
        """
        self.reload_config()
        t0 = time.time()
        mc = self.missing_config()
        out = {"ok": True, "items": [], "scanned_torrents": 0, "checked_videos": 0,
               "missing_videos": 0, "skipped_dirs": 0, "truncated": False,
               "errors": [], "elapsed_ms": 0, "days": 0,
               "max_depth": mc["max_depth"], "max_files": mc["max_files"]}

        dls = [d for d in self.dls.enabled()
               if not downloader_id or d.did == downloader_id]
        if not dls:
            out.update(ok=False, errors=["没有启用任何下载器，请先在「设置」中添加并启用"])
            out["elapsed_ms"] = int((time.time() - t0) * 1000)
            return out

        days = int(self._dl.get("scan_days", 30) if days is None else days)
        out["days"] = days
        cutoff = (datetime.now() - timedelta(days=days)) if days > 0 else None
        cat = (category or "").strip()
        kw = (keyword or "").strip().lower()
        adapters = sites.all_sites()
        cache = {}
        limit = max(1, int(limit or 200))
        stop = False

        for dl in dls:
            try:
                torrents = dl.list_torrents(limit=dl.max_torrents,
                                            timeout=dl.list_timeout)
            except Exception as e:  # noqa: BLE001 单个下载器失败不影响其它
                out["errors"].append("%s：读取种子列表失败（%s）" % (dl.name, _short(e)))
                continue

            for t in torrents:
                if stop:
                    break
                if cat and cat not in dl.torrent_categories(t):
                    continue
                added = t.get("added_on") or 0
                if cutoff:
                    try:
                        if datetime.fromtimestamp(added) < cutoff:
                            continue
                    except (OSError, ValueError):
                        continue
                name = t.get("name") or ""
                if kw and kw not in name.lower():
                    continue

                out["scanned_torrents"] += 1
                real = dl.map_save_path(t.get("save_path") or "")
                root = os.path.join(real, name)
                # 单文件种子（name 就是文件名）只检查它自己；多文件种子检查 name 目录。
                # 两者都不存在时直接跳过：绝不能退回到 save_path 整目录，
                # 那会把同一目录下其它种子的视频错算到当前种子上。
                if os.path.isfile(root):
                    base = os.path.dirname(root)
                    videos = ([root] if os.path.splitext(root)[1].lower()
                              in mc["video_exts"] else [])
                elif os.path.isdir(root):
                    base = root
                    videos = self._walk_videos(root, mc["video_exts"],
                                               mc["max_depth"], mc["max_files"])
                    if len(videos) >= mc["max_files"]:
                        out["truncated"] = True
                else:
                    out["skipped_dirs"] += 1
                    continue
                if not videos:
                    continue

                missing = []
                for v in videos:
                    out["checked_videos"] += 1
                    if self._has_subtitle(v, mc["sub_exts"], cache):
                        continue
                    try:
                        size = os.path.getsize(v)
                    except OSError:
                        size = 0
                    missing.append({"file": os.path.relpath(v, base),
                                    "dir": os.path.dirname(v), "size": size})
                if not missing:
                    continue

                cm = _unescape_comment(t.get("comment") or "")
                site_id, tid = "", ""
                if cm:
                    for ad in adapters:
                        found = ad.parse_torrent_id(cm)
                        if found:
                            site_id, tid = ad.id, str(found)
                            break
                if only_site_matched and not site_id:
                    continue

                out["missing_videos"] += len(missing)
                item = {
                    "hash": t.get("hash", ""),
                    "name": name,
                    "downloader_id": dl.did,
                    "downloader_name": dl.name,
                    "category": self._category_join(dl, t),
                    "save_path": real,
                    "root": base,
                    "site_id": site_id,
                    "site_label": (sites.get(site_id).label if site_id else ""),
                    "torrent_id": tid,
                    "video_count": len(videos),
                    "missing_count": len(missing),
                    "missing": missing[:50],
                    "added_on": added,
                }
                out["items"].append(item)
                if len(out["items"]) >= limit:
                    out["truncated"] = True
                    stop = True
                    break

        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        log("INFO", "缺字幕检查：%d 个种子 / %d 个视频，缺字幕 %d 个（%d 条记录），"
                    "跳过目录 %d，耗时 %.1fs"
            % (out["scanned_torrents"], out["checked_videos"], out["missing_videos"],
               len(out["items"]), out["skipped_dirs"], out["elapsed_ms"] / 1000.0),
            SRC_SCAN)
        return out

    def missing_repair(self, items, auto_run=True):
        """把缺字幕条目转成常规下载任务（复用 submit，不另开链路）。"""
        out = {"ok": True, "created": 0, "queued": 0, "items": [], "errors": []}
        seen = set()
        for it in items or []:
            site_id = str((it or {}).get("site_id") or "").strip()
            tid = str((it or {}).get("torrent_id") or "").strip()
            name = str((it or {}).get("name") or "")
            if not site_id or not tid:
                out["errors"].append("无法反查站点：%s" % (name[:80] or "（未命名）"))
                continue
            if (site_id, tid) in seen:
                continue
            seen.add((site_id, tid))
            res = self.submit(site_id, tid,
                              save_dir=(it or {}).get("save_dir") or None,
                              auto_run=auto_run)
            if res.get("ok") is False:
                out["errors"].extend(res.get("errors") or
                                     ["%s/%s 提交失败" % (site_id, tid)])
                continue
            out["created"] += res.get("jobs_created", 0)
            out["queued"] += res.get("jobs_queued", 0)
            out["items"].append({"site_id": site_id, "torrent_id": tid,
                                 "name": name, "created": res.get("jobs_created", 0),
                                 "queued": res.get("jobs_queued", 0),
                                 "errors": res.get("errors") or []})
        if not out["items"] and not out["errors"]:
            out.update(ok=False, errors=["没有可提交的条目"])
        log("INFO", "缺字幕补单：提交 %d 个种子，新建任务 %d，入队 %d，错误 %d"
            % (len(out["items"]), out["created"], out["queued"], len(out["errors"])),
            SRC_SCAN)
        return out

    def zone_submit(self, site_id, items, save_dir="", auto_run=True):
        """
        把选中的字幕区条目建成下载任务（source=zone）。
        落盘目录优先级：能对上本地种子 -> 该视频目录；否则用 save_dir / 默认目录。
        """
        self.reload_config()
        out = {"ok": True, "created": 0, "queued": 0, "skipped": 0,
               "errors": [], "items": []}
        adapter = sites.get(site_id)
        if not adapter or not adapter.supports_subtitle_zone:
            out.update(ok=False, errors=["站点不支持字幕区：%s" % site_id])
            return out

        items = [x for x in (items or []) if isinstance(x, dict)]
        if not items:
            out.update(ok=False, errors=["没有选中任何字幕"])
            return out

        idx = self._local_torrent_index()
        fallback = (save_dir or "").strip() or self._manual_save_dir()
        job_ids = []

        for it in items:
            tid = str(it.get("torrent_id") or "").strip()
            sid = str(it.get("subid") or "").strip()
            if not tid or not sid:
                out["errors"].append("条目缺少 torrent_id / subid")
                continue
            title = (it.get("title") or "").strip()
            rec = self._match_local(idx, adapter, tid, title)
            target = (rec or {}).get("save_path") or fallback
            if not target:
                out["errors"].append(
                    "tid=%s 本地无匹配种子且未指定保存目录，已跳过" % tid)
                continue
            if not os.path.isdir(target):
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as e:
                    out["errors"].append("保存目录不可用：%s（%s）" % (target, e))
                    continue

            jid, created = db.job_upsert(
                adapter.id, tid, sid, site_label=adapter.label,
                torrent_name=(rec or {}).get("name") or title or ("种子 %s" % tid),
                torrent_hash=(rec or {}).get("hash", ""),
                # 能对上本地种子时，顺带记下它挂在哪个下载器上、属于哪个分类
                downloader_id=(rec or {}).get("downloader_id", ""),
                category=(rec or {}).get("category", ""),
                sub_title=title, status=db.ST_PENDING, save_path=target,
                source="zone")
            if created:
                out["created"] += 1
                job_ids.append(jid)
            else:
                out["skipped"] += 1

        if auto_run and job_ids:
            out["queued"] = self.enqueue(job_ids)
        log("INFO", "[%s] 字幕区提交：新建 %d，已存在跳过 %d，入队 %d，错误 %d"
            % (adapter.label, out["created"], out["skipped"], out["queued"],
               len(out["errors"])), SRC_SCAN)
        return out

    # ------------------------------------------------------------ 执行单个任务

    def run_job(self, job_id):
        job = db.job_get(job_id)
        if not job:
            return
        if job["status"] not in (db.ST_PENDING, db.ST_RUNNING):
            return

        t0 = time.time()
        adapter = sites.get(job["site_id"])
        if not adapter:
            self._fail(job_id, "站点适配器不存在：%s" % job["site_id"])
            return

        db.job_update(job_id, status=db.ST_RUNNING, started_at=db.now_str(),
                      message="")
        log("INFO", "[%s] 开始下载字幕 subid=%s | %s"
            % (adapter.label, job["subid"], (job["torrent_name"] or "")[:80]),
            SRC_TASK, job_id)

        save_dir = job["save_path"] or ""
        if not save_dir or not os.path.isdir(save_dir):
            self._fail(job_id, "保存目录不存在：%s" % (save_dir or "(空)"), t0)
            return

        # 字幕跟随视频：按种子内真实文件列表定位视频目录，
        # 把字幕写进视频所在目录（Stash 等媒体服务器只认同目录的同名字幕）。
        dl = next((d for d in self.dls.enabled()
                   if d.did == job.get("downloader_id")), None)
        placement = None
        if dl and self._dl.get("follow_video", True):
            placement = self._subtitle_placement(dl, job, save_dir)
            if placement:
                log("INFO", "字幕跟随视频目录：%s" % placement["primary_dir"],
                    SRC_TASK, job_id)
                save_dir = placement["primary_dir"]

        cookie, err = self.cookie_for(adapter)
        if not cookie:
            self._fail(job_id, err or "Cookie 不可用", t0)
            return

        try:
            status, fname, size, extracted, note = self.download_package(
                adapter, job["torrent_id"], job["subid"], cookie, save_dir,
                job_id=job_id)
        except Exception as e:  # noqa: BLE001
            self._fail(job_id, "%s: %s" % (type(e).__name__, e), t0)
            return

        dur = int((time.time() - t0) * 1000)
        if status == db.ST_FAILED:
            db.job_update(job_id, status=db.ST_FAILED, message=note, duration_ms=dur,
                          finished_at=db.now_str())
            log("ERROR", "[%s] 下载失败 subid=%s：%s" % (adapter.label, job["subid"], note),
                SRC_TASK, job_id)
            return

        # 多视频种子 / 基名不一致时，把新字幕按视频逐一归位
        if placement and status in (db.ST_SUCCESS, db.ST_EXISTS):
            moved = self._relocate_subs(save_dir, placement, since_ts=t0,
                                        job_id=job_id)
            if moved:
                log("INFO", "按视频整理字幕 %d 个" % moved, SRC_TASK, job_id)

        db.job_update(job_id, status=status, file_name=fname, file_size=size,
                      extracted=extracted, message=note, duration_ms=dur,
                      finished_at=db.now_str())
        log("INFO", "[%s] %s subid=%s -> %s（%.1f KB%s，%d ms）"
            % (adapter.label, "已存在" if status == db.ST_EXISTS else "下载完成",
               job["subid"], fname, size / 1024.0,
               "，解压 %d 个字幕" % extracted if extracted else "", dur),
            SRC_TASK, job_id)

    def _subtitle_placement(self, dl, job, fallback_dir):
        """
        依据种子内真实文件列表定位字幕应放置的目录（跟随视频）。

        返回 {"primary_dir": 最大视频所在目录,
              "videos": [(视频基名小写, 目录)], "single": 是否唯一视频}；
        取不到文件列表 / 无视频文件 / 下载器不支持时返回 None，调用方回退旧行为。
        """
        getter = getattr(dl, "torrent_files", None)
        if not getter or not job.get("torrent_hash"):
            return None
        try:
            files = getter(job["torrent_hash"])
        except Exception:  # noqa: BLE001 任何网络/接口异常都不阻塞下载主流程
            return None
        if not files:
            return None
        videos = []
        for f in files:
            name = str(f.get("name") or "").replace("\\", "/")
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            if f.get("priority") == 0:  # 被跳过不下载的文件不算
                continue
            base = os.path.basename(name)
            if not base:
                continue
            rel_dir = os.path.dirname(name)
            video_dir = os.path.join(fallback_dir, rel_dir) if rel_dir \
                else fallback_dir
            videos.append((os.path.splitext(base)[0].lower(), video_dir,
                           int(f.get("size") or 0)))
        if not videos:
            return None
        primary = max(videos, key=lambda x: x[2])
        single = (len({v[0] for v in videos}) == 1
                  and len({v[1] for v in videos}) == 1)
        return {"primary_dir": primary[1],
                "videos": [(v[0], v[1]) for v in videos],
                "single": single}

    def _relocate_subs(self, stage_dir, placement, since_ts, job_id=None):
        """
        把 stage_dir 里本次新生成的字幕文件搬到对应视频目录，返回搬运数量。

        匹配规则（按优先级）：
          1) 字幕基名 == 某视频基名           -> 放到该视频目录（原名保留）
          2) 字幕基名与视频基名互为前缀       -> 同上（保留语言码后缀）
          3) 种子只有一个视频                 -> 放到该视频目录，并重命名成
                                                 视频基名（保留语言码后缀）
          4) 都不匹配                         -> 留在原地
        """
        n = 0
        for root, _dirs, files in os.walk(stage_dir):
            for fn in files:
                path = os.path.join(root, fn)
                try:
                    if os.path.getmtime(path) < since_ts:
                        continue
                except OSError:
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext not in DEFAULT_SUB_EXTS:
                    continue
                stem = os.path.splitext(fn)[0].lower()
                m = LANG_TAG_RE.search(stem)
                lang = m.group(1) if m else ""
                stem_no_lang = stem[:m.start()] if m else stem
                hit_dir, hit_base = None, None
                for base, vdir in placement["videos"]:
                    if stem_no_lang == base or stem == base:
                        hit_dir, hit_base = vdir, base
                        break
                if hit_dir is None:
                    for base, vdir in placement["videos"]:
                        if base and (stem_no_lang.startswith(base)
                                     or base.startswith(stem_no_lang)):
                            hit_dir, hit_base = vdir, base
                            break
                if hit_dir is None and placement["single"]:
                    hit_base = placement["videos"][0][0]
                    hit_dir = placement["videos"][0][1]
                if hit_dir is None:
                    continue
                new_name = hit_base + ("." + lang if lang else "") + ext
                target = os.path.join(hit_dir, new_name)
                if os.path.abspath(path) == os.path.abspath(target):
                    continue
                if os.path.exists(target):
                    continue
                try:
                    os.makedirs(hit_dir, exist_ok=True)
                    shutil.move(path, target)
                    try:
                        os.chmod(target, 0o644)
                    except OSError:
                        pass
                    n += 1
                except OSError as e:
                    log("WARN", "字幕整理失败 %s -> %s：%s" % (fn, target, e),
                        SRC_TASK, job_id)
        return n

    def _fail(self, job_id, message, t0=None):
        dur = int((time.time() - t0) * 1000) if t0 else 0
        db.job_update(job_id, status=db.ST_FAILED, message=str(message)[:500],
                      duration_ms=dur, finished_at=db.now_str())
        log("ERROR", "任务 #%s 失败：%s" % (job_id, message), SRC_TASK, job_id)

    # ------------------------------------------------------------ 下载与解压

    def download_package(self, adapter, torrent_id, subid, cookie, save_dir,
                         job_id=None):
        """
        返回 (status, file_name, file_size, extracted_count, note)。
        status 取 db.ST_SUCCESS / db.ST_EXISTS / db.ST_FAILED。
        """
        url = adapter.subtitle_url(torrent_id, subid)
        try:
            r = self._get(url, cookie=cookie, referer=adapter.referer(torrent_id),
                          stream=True)
        except requests.RequestException as e:
            return db.ST_FAILED, "", 0, 0, "请求失败：%s" % e

        if r.status_code == 404:
            return db.ST_FAILED, "", 0, 0, "站点返回 404，字幕可能已被删除"
        if r.status_code in (301, 302, 403):
            return db.ST_FAILED, "", 0, 0, "站点返回 HTTP %s" % r.status_code
        if r.status_code >= 400:
            return db.ST_FAILED, "", 0, 0, "站点返回 HTTP %s" % r.status_code

        data = r.content
        if not data:
            return db.ST_FAILED, "", 0, 0, "响应为空"

        ctype = (r.headers.get("Content-Type") or "").lower()
        # 拿到 HTML 基本意味着被重定向到登录页或出错页
        if data[:64].lstrip().lower().startswith((b"<!doctype html", b"<html")) \
                or "text/html" in ctype:
            hint = "Cookie 可能已失效" if b"login" in data[:8000].lower() else "站点返回 HTML 而非字幕包"
            return db.ST_FAILED, "", 0, 0, hint

        ext = self._sniff_ext(data)
        fname = self._filename_from_headers(
            r, "subtitle_%s_%s%s" % (torrent_id, subid, ext))
        fname = self.normalize_name(fname, ext)
        path = os.path.join(save_dir, fname)

        if os.path.exists(path):
            return db.ST_EXISTS, fname, os.path.getsize(path), 0, "同名文件已存在"

        try:
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError as e:
            return db.ST_FAILED, "", 0, 0, "写入失败：%s" % e

        extracted, note = self._unpack(path, fname, save_dir, subid, job_id)
        return db.ST_SUCCESS, fname, len(data), extracted, note

    def _unpack(self, path, fname, save_dir, subid, job_id):
        """压缩包按配置解压出字幕文件；裸字幕本身就是最终产物。返回 (数量, 说明)。"""
        low = fname.lower()
        keep = bool(self._dl.get("keep_archive", True))

        def _drop_archive(note):
            if keep:
                return note
            try:
                os.remove(path)
                return note + "，已按配置删除压缩包"
            except OSError:
                return note

        if low.endswith(".zip"):
            if not self._dl.get("extract_zip", True):
                return 0, "zip 已保存（配置为不解压）"
            n = self.extract_zip_subs(path, save_dir)
            if not n:
                return 0, "zip 内未找到字幕文件，已保留原包"
            return n, _drop_archive("解压出 %d 个字幕文件" % n)

        if low.endswith((".rar", ".7z")):
            if not self._dl.get("extract_archive", True):
                return 0, "压缩包已保存（配置为不解压）"
            n, how = self.extract_archive_subs(path, save_dir)
            if n:
                return n, _drop_archive("解压出 %d 个字幕文件（%s）" % (n, how))
            log("WARN", "subid=%s 压缩包未解压（%s）：%s"
                % (subid, how or "无可用解压后端", fname), SRC_TASK, job_id)
            return 0, ("已保留原压缩包：%s" % how if how
                       else "已保留原压缩包，运行环境缺少 unrar/7z/bsdtar，无法解压")

        return 0, "字幕文件已保存"

    @staticmethod
    def _sniff_ext(data):
        """判定字幕内容类型：先看压缩包魔术字节，再看裸字幕文本特征。"""
        if data[:4] == SUB_MAGIC_RAR:
            return ".rar"
        if data[:2] == SUB_MAGIC_ZIP:
            return ".zip"
        if data[:6] == SUB_MAGIC_7Z:
            return ".7z"

        body = data[:8192]
        if body.startswith(BOM):
            body = body[len(BOM):]
        body = body.lstrip()
        low = body[:64].lower()
        if low.startswith(b"[script info]") or low.startswith(b"dialogue:"):
            return ".ass"
        if low.startswith(b"webvtt"):
            return ".vtt"
        if b"[v4+ styles]" in body[:2048].lower():
            return ".ass"
        if SRT_TIMELINE.search(body):
            return ".srt"
        return ".bin"

    @staticmethod
    def normalize_name(fname, ext):
        """
        把站点给的文件名与内容判定结果归一，返回安全的落盘文件名。

        站点在 Content-Disposition 里给的后缀不一定可信，分三种情形：
          1) 后缀与内容判定一致        -> 直接用（否则会出现 .rar.rar / .ass.ass）
          2) 后缀是已知类型但与内容不符 -> 以内容判定为准（如把 rar 标成 .txt）
          3) 后缀不认识（常见是语言码） -> 补上内容后缀（如 xxx.chs -> xxx.chs.rar）
        """
        # 不用 os.path.basename：它在 Windows/Linux 对 "\\" 的处理不一致，
        # 显式替换分隔符既跨平台一致，也顺带消除 "..\\" 这类路径穿越风险。
        fname = re.sub(r"[\\/]+", "_", (fname or "").strip())
        if fname in ("", ".", ".."):
            fname = "subtitle"
        cur = os.path.splitext(fname)[1].lower()
        if cur != ext:
            if cur in ARCHIVE_EXTS or cur in DEFAULT_SUB_EXTS:
                fname = os.path.splitext(fname)[0] + ext
            else:
                fname += ext
        return fname

    # ---------------------------------------------------------------- 解压后端

    def extract_archive_subs(self, apath, save_dir):
        """
        从 rar / 7z 中提取字幕文件，返回 (提取数量, 所用方式)。
        按可用性依次尝试，全部不可用时返回 (0, 原因)。
        """
        exts = {e.lower() for e in self._dl.get("sub_exts", [])} or DEFAULT_SUB_EXTS
        tmpdir = tempfile.mkdtemp(prefix="subflow-arc-")
        tried = []
        try:
            for how, argv in self._archive_commands(apath, tmpdir):
                tried.append(how)
                try:
                    proc = subprocess.run(argv, capture_output=True, timeout=240)
                except (OSError, subprocess.SubprocessError):
                    continue
                moved = self._collect_subs(tmpdir, save_dir, exts)
                if moved:
                    return moved, how
                if proc.returncode != 0:
                    continue
            return 0, ("、".join(tried) + " 均未提取到字幕") if tried else ""
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _archive_commands(apath, tmpdir):
        """按可用性列出解压命令。bsdtar(libarchive) 对 RAR5 支持最好。"""
        out = []
        bsdtar = _find_bsdtar()
        if bsdtar:
            out.append(("bsdtar", [bsdtar, "-xf", apath, "-C", tmpdir]))
        for exe in ("7z", "7za", "7zz", "7zr"):
            if shutil.which(exe):
                out.append((exe, [exe, "x", "-y", "-bso0", "-bsp0",
                                  "-o" + tmpdir, apath]))
                break
        if shutil.which("unrar"):
            out.append(("unrar", ["unrar", "x", "-y", apath, tmpdir + os.sep]))
        return out

    @staticmethod
    def _collect_subs(tmpdir, save_dir, exts):
        """把临时目录里的字幕文件平铺搬到目标目录，返回搬运数量。"""
        n = 0
        for root, _dirs, files in os.walk(tmpdir):
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in exts:
                    continue
                safe = os.path.basename(fn).replace("/", "_").replace("\\", "_")
                target = os.path.join(save_dir, safe)
                if os.path.exists(target):
                    continue
                try:
                    shutil.copy2(os.path.join(root, fn), target)
                    n += 1
                except OSError:
                    continue
        return n

    @staticmethod
    def _filename_from_headers(resp, fallback):
        """
        优先 RFC 5987 的 filename*，兼容普通 filename。
        部分站点不带 charset 直接回 UTF-8 字节，requests 会按 latin-1 解码，
        这里再按 UTF-8 复原一次，否则中文文件名会变成乱码。
        """
        cd = resp.headers.get("Content-Disposition", "") or ""
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
        if m:
            try:
                return urllib.parse.unquote(m.group(1))
            except Exception:
                pass
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
        if m:
            raw = m.group(1)
            try:
                return raw.encode("latin-1").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                return raw
        return fallback

    def extract_zip_subs(self, zpath, save_dir):
        """从 zip 中平铺解压字幕文件，返回成功数量。防路径穿越。"""
        exts = {e.lower() for e in self._dl.get(
            "sub_exts", [".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi"])}
        n = 0
        try:
            with zipfile.ZipFile(zpath) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    base = os.path.basename(info.filename)
                    if not base or os.path.splitext(base)[1].lower() not in exts:
                        continue
                    safe = base.replace("/", "_").replace("\\", "_")
                    target = os.path.join(save_dir, safe)
                    if os.path.exists(target):
                        continue
                    with z.open(info) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    n += 1
        except zipfile.BadZipFile as e:
            log("WARN", "zip 解析失败 %s：%s" % (os.path.basename(zpath), e), SRC_TASK)
        except OSError as e:
            log("WARN", "解压写入失败 %s：%s" % (os.path.basename(zpath), e), SRC_TASK)
        return n

    # ------------------------------------------------------------ 自检

    def health(self):
        out = {"ok": True, "downloaders": [], "cookiecloud": {}, "sites": [],
               "queue": self.queue_size(), "busy": self.busy()}
        # 多下载器：逐个探测，任一不可用都判为不健康，但明细全返回便于定位
        out["downloaders"] = self.dls.health_all()
        # 兼容字段：老前端与既有外部脚本读的是 qbittorrent
        first = out["downloaders"][0] if out["downloaders"] else {}
        out["qbittorrent"] = {
            "ok": bool(first.get("ok")),
            "message": first.get("message") or "未配置下载器",
        }
        if not out["downloaders"] or any(not d["ok"] for d in out["downloaders"]):
            out["ok"] = False

        try:
            data = self.cc.fetch(force=False)
            meta = self.cc.meta()
            out["cookiecloud"] = {
                "ok": True,
                "message": "%d 个域名 / %d 条 Cookie（%s）"
                           % (meta.get("domains", len(data)), meta.get("cookies", 0),
                              meta.get("format", "?")),
                "meta": meta,
            }
        except Exception as e:  # noqa: BLE001
            out["cookiecloud"] = {"ok": False, "message": str(e)}
            out["ok"] = False

        site_status = self.site_status()
        for ad in sites.all_sites():
            st = site_status.get(ad.id, {})
            out["sites"].append({
                **ad.to_dict(),
                "cookie_ok": st.get("cookie_ok"),
                "cookie_message": st.get("message", ""),
            })
        return out


engine = SubtitleEngine()
