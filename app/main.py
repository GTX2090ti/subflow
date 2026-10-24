# -*- coding: utf-8 -*-
"""
SubFlow HTTP 接口层
===================

统一约定
--------
* 所有接口返回 JSON：成功 ``{"ok": true, ...}``，失败 ``{"ok": false, "error": "..."}``。
* 耗时操作（扫描）后台线程执行，接口立即返回，前端轮询 ``/api/scan/state``。
* 日志与任务列表都支持增量/分页拉取，前端按 ``since_id`` 追加，避免整页刷新。

接口总览
--------
GET    /api/overview              仪表盘汇总
GET    /api/health                依赖自检（下载器 / CookieCloud / 站点）
GET    /api/downloaders           下载器清单与可用类型
POST   /api/downloaders/test      测试下载器连接（不写配置）
GET    /api/categories            下载器分类清单（含每个分类的种子数量）
GET    /api/categories/cached     上次分类快照（不访问下载器）
GET    /api/sites                 站点清单与 Cookie 状态
POST   /api/scan                  触发扫描（可按分类筛选，days<=0 不限时间窗口）
GET    /api/scan/state            扫描进度
GET    /api/schedule              定时扫描状态
GET    /api/stats                 统计聚合（每日 / 站点 / 分类 / 失败原因）
GET    /api/missing               缺字幕清单（以本地视频文件为准）
POST   /api/missing/repair        批量补字幕（复用提交链路）
POST   /api/submit                手动提交任务
POST   /api/zone/list             读取站点字幕区（站点级字幕来源）
POST   /api/zone/submit           提交选中的字幕区条目
GET    /api/jobs                  任务列表
POST   /api/jobs/{id}/retry       重试单个任务
POST   /api/jobs/retry-failed     批量重试失败任务
POST   /api/jobs/run-pending      把待处理任务全部入队
DELETE /api/jobs/{id}             删除任务记录
GET    /api/history               历史记录（分页 + 筛选）
GET    /api/history/export.csv    导出历史 CSV
GET    /api/logs                  日志增量拉取
GET    /api/logs/download         下载完整日志
DELETE /api/logs                  清空日志
GET    /api/config                读取配置（敏感字段脱敏）
POST   /api/config                保存配置
POST   /api/cookiecloud/refresh   强制刷新 CookieCloud 缓存
"""

import base64
import hmac
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cookiecloud, db, sites
from .config import APP_NAME, APP_VERSION, BASE_DIR, config
from .downloaders import TYPE_LABELS, create
from .engine import SRC_SYS, engine, log

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def ok(**kw):
    kw.setdefault("ok", True)
    return kw


def err(message, **kw):
    kw.update(ok=False, error=str(message))
    return kw


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    n = db.job_reset_stale()
    log("INFO", "%s v%s 启动，数据库 %s%s"
        % (APP_NAME, APP_VERSION, db.DB_PATH, "，复位 %d 个中断任务" % n if n else ""),
        SRC_SYS)

    # 首次启动导入旧版状态文件，避免历史丢失
    legacy = config.get("download", "legacy_state_file", "")
    if legacy and os.path.exists(legacy) and db.kv_get("legacy_imported") != "1":
        try:
            cnt = db.import_legacy_state(legacy)
            db.kv_set("legacy_imported", "1")
            log("INFO", "已从旧版状态文件导入 %d 条历史记录：%s" % (cnt, legacy), SRC_SYS)
        except Exception as e:  # noqa: BLE001
            log("WARN", "导入旧版状态文件失败：%s" % e, SRC_SYS)

    db.log_purge(config.get("log", "retain_days", 30))
    engine.start_workers()
    engine.enqueue_pending()
    engine.start_scheduler()
    try:
        yield
    finally:
        engine.stop_scheduler()
        engine.stop_workers()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


# ================================================================ 访问控制
#
# HTTP Basic：够用、零额外依赖、所有浏览器原生支持。
# 密码为空时一律放行 —— 配置只改了一半（勾了启用但没填密码）不能把人锁在门外。
# 本机请求默认豁免，容器内健康检查（127.0.0.1/api/overview）因此不受影响。

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    cfg = config.section("auth")
    password = str(cfg.get("password") or "")
    if not cfg.get("enabled") or not password:
        return await call_next(request)

    host = request.client.host if request.client else ""
    if cfg.get("allow_localhost", True) and host in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header[:6].lower() == "basic ":
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8")
            user, _, pwd = raw.partition(":")
        except Exception:  # noqa: BLE001 - 非法 Base64 一律当作未认证
            user = pwd = ""
        if (hmac.compare_digest(user, str(cfg.get("username") or ""))
                and hmac.compare_digest(pwd, password)):
            return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"ok": False, "error": "需要登录"},
        headers={"WWW-Authenticate": 'Basic realm="%s"'
                                     % (cfg.get("realm") or "SubFlow")})


# ================================================================ 系统

@app.get("/api/overview")
def api_overview():
    stats = db.job_stats()
    return ok(stats=stats, by_site=db.job_stats_by_site(),
              queue=engine.queue_size(), busy=engine.busy(),
              scan=engine.scan_state(), version=APP_VERSION)


@app.get("/api/health")
def api_health():
    return ok(**engine.health())


@app.get("/api/stats")
def api_stats(days: int = 30):
    return ok(**db.stats_bundle(days))


@app.get("/api/sites")
def api_sites():
    status = engine.site_status()
    out = []
    for ad in sites.all_sites():
        st = status.get(ad.id, {})
        out.append({**ad.to_dict(),
                    "cookie_ok": st.get("cookie_ok"),
                    "cookie_message": st.get("message", "")})
    domains = []
    cc_ok, cc_msg = True, ""
    try:
        domains = engine.cc.domains()
    except Exception as e:  # noqa: BLE001
        cc_ok, cc_msg = False, str(e)
    return ok(sites=out, cookiecloud_domains=domains,
              cookiecloud_ok=cc_ok, cookiecloud_message=cc_msg,
              meta=engine.cc.meta())


# ================================================================ 分类
#
# 「读取分类 → 按分类批量下载字幕」的前半段：把下载器里的分类连同
# 各自的种子数量列出来，供界面勾选，勾选结果再作为 /api/scan 的
# categories 参数提交。分类本身不是一个独立流程，而是扫描的筛选条件。

@app.get("/api/categories")
def api_categories(downloader: str = "", keyword: str = ""):
    res = engine.categories(downloader_id=downloader or None,
                            keyword=keyword or "")
    return res if res.get("ok") is False else ok(**res)


@app.get("/api/categories/cached")
def api_categories_cached(keyword: str = ""):
    """上次读取的分类快照（不访问下载器），页面启动时直接回显。"""
    return ok(**engine.cached_categories(keyword=keyword or ""))


# ================================================================ 扫描

class ScanReq(BaseModel):
    site_ids: list[str] | None = None
    # None = 用配置里的默认窗口；<= 0 = 不限时间窗口（按分类全量处理时用）
    days: int | None = None
    limit: int | None = None
    # 分类筛选：["<下载器ID>:<分类名>", ...] 或裸分类名；空/None 表示不筛选。
    # 分类名传 ""/__none__ 表示「未分类」。
    categories: list[str] | None = None
    dry_run: bool = False
    refresh_cookie: bool = False


@app.post("/api/scan")
def api_scan(req: ScanReq):
    if engine.scan_state().get("running"):
        return err("已有扫描在进行中，请等待完成")
    kwargs = dict(site_ids=req.site_ids, days=req.days, limit=req.limit,
                  categories=req.categories,
                  dry_run=req.dry_run, refresh_cookie=req.refresh_cookie)
    threading.Thread(target=engine.scan, kwargs=kwargs,
                     name="subflow-scan", daemon=True).start()
    log("INFO", "扫描任务已提交（后台执行）%s"
        % ("，分类 " + "、".join(str(x) for x in req.categories)
           if req.categories else ""), SRC_SYS)
    return ok(message="扫描已启动")


@app.get("/api/scan/state")
def api_scan_state():
    return ok(**engine.scan_state())


@app.get("/api/schedule")
def api_schedule():
    return ok(**engine.schedule_state())


# ================================================================ 缺字幕清单

@app.get("/api/missing")
def api_missing(days: int | None = None, downloader: str = "",
                category: str = "", keyword: str = "",
                limit: int = 200, only_site_matched: bool = False):
    """
    按本地视频文件检查缺字幕情况。

    只读文件系统 + 下载器种子列表，不访问站点；``only_site_matched=true``
    时只返回能通过 comment 反查到站点的条目，这类条目可以直接补字幕。
    """
    res = engine.missing_scan(days=days, downloader_id=downloader or None,
                              category=category, keyword=keyword,
                              limit=limit, only_site_matched=only_site_matched)
    return res if res.get("ok") is False else ok(**res)


class MissingItem(BaseModel):
    site_id: str = ""
    torrent_id: str = ""
    save_dir: str = ""
    name: str = ""


class MissingRepairReq(BaseModel):
    items: list[MissingItem]
    auto_run: bool = True


@app.post("/api/missing/repair")
def api_missing_repair(req: MissingRepairReq):
    """把选中的缺字幕条目转成常规下载任务（复用扫描同一条落盘链路）。"""
    res = engine.missing_repair([i.model_dump() for i in req.items],
                                auto_run=req.auto_run)
    return res if res.get("ok") is False else ok(**res)


# ================================================================ 手动提交

class SubmitReq(BaseModel):
    site_id: str
    target: str
    save_dir: str | None = None
    refresh_cookie: bool = False
    auto_run: bool = True


@app.post("/api/submit")
def api_submit(req: SubmitReq):
    res = engine.submit(req.site_id, req.target, save_dir=req.save_dir,
                        refresh_cookie=req.refresh_cookie, auto_run=req.auto_run)
    return res if res.get("ok") is False else ok(**res)


# ================================================================ 站点级字幕区

class ZoneListReq(BaseModel):
    site_id: str = "all"
    # <= 0 表示自动翻到最后一页
    pages: int = 1
    keyword: str = ""
    refresh_cookie: bool = False


@app.post("/api/zone/list")
def api_zone_list(req: ZoneListReq):
    res = engine.zone_list(site_id=req.site_id, pages=req.pages,
                           keyword=req.keyword, refresh_cookie=req.refresh_cookie)
    return res if res.get("ok") is False else ok(**res)


class ZoneItem(BaseModel):
    torrent_id: str
    subid: str
    title: str = ""


class ZoneSubmitReq(BaseModel):
    site_id: str
    items: list[ZoneItem]
    save_dir: str | None = None
    auto_run: bool = True


@app.post("/api/zone/submit")
def api_zone_submit(req: ZoneSubmitReq):
    res = engine.zone_submit(req.site_id, [i.model_dump() for i in req.items],
                             save_dir=req.save_dir or "", auto_run=req.auto_run)
    return res if res.get("ok") is False else ok(**res)


# ================================================================ 任务

@app.get("/api/jobs")
def api_jobs(status: str = "all", site: str = "all", keyword: str = "",
             category: str = "", downloader: str = "",
             limit: int = 200, offset: int = 0):
    """任务列表。``category`` 支持 ``__uncat__``（未分类）与多标签整词匹配。"""
    cat = category if category not in ("", "all") else None
    dl = downloader or None
    rows = db.job_list(status=status, site_id=site, keyword=keyword or None,
                       downloader_id=dl, category=cat,
                       limit=limit, offset=offset)
    return ok(jobs=[_job_view(r) for r in rows],
              total=db.job_count(status=status, site_id=site,
                                 keyword=keyword or None,
                                 downloader_id=dl, category=cat),
              statuses=db.STATUS_LABEL)


@app.get("/api/jobs/categories")
def api_job_categories():
    """任务表里实际出现过的分类，供筛选下拉使用（含未分类）。"""
    names = {d.did: d.name for d in engine.dls.all()}
    rows = []
    for r in db.job_categories():
        did = r["downloader_id"]
        rows.append(dict(r, downloader=names.get(did, did) or did,
                         label=r["category"] or "未分类"))
    return ok(categories=rows)


def _job_view(row):
    row = dict(row)
    row["status_label"] = db.STATUS_LABEL.get(row.get("status"), row.get("status"))
    return row


@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: int):
    row = db.job_get(job_id)
    if not row:
        return err("任务不存在")
    return ok(job=_job_view(row))


@app.post("/api/jobs/{job_id}/retry")
def api_job_retry(job_id: int):
    row = db.job_get(job_id)
    if not row:
        return err("任务不存在")
    db.job_update(job_id, status=db.ST_PENDING, message="手动重试", finished_at=None,
                  duration_ms=0)
    engine.enqueue([job_id])
    log("INFO", "任务 #%s 已重新入队" % job_id, SRC_SYS)
    return ok(message="已重新入队")


@app.post("/api/jobs/retry-failed")
def api_retry_failed(site: str = "all"):
    rows = db.job_list(status=db.ST_FAILED, site_id=site, limit=10000)
    ids = [r["id"] for r in rows]
    for jid in ids:
        db.job_update(jid, status=db.ST_PENDING, message="批量重试", finished_at=None,
                      duration_ms=0)
    n = engine.enqueue(ids)
    log("INFO", "批量重试失败任务 %d 个" % n, SRC_SYS)
    return ok(count=n, message="已重新入队 %d 个任务" % n)


@app.post("/api/jobs/run-pending")
def api_run_pending(site: str = "all"):
    n = engine.enqueue_pending(site_id=site)
    return ok(count=n, message="已入队 %d 个待处理任务" % n)


@app.delete("/api/jobs/{job_id}")
def api_job_delete(job_id: int):
    n = db.job_delete(job_id)
    return ok(message="已删除 %d 条记录" % n) if n else err("任务不存在")


# ================================================================ 历史记录

@app.get("/api/history")
def api_history(site: str = "all", status: str = "all", keyword: str = "",
                start: str = "", end: str = "", limit: int = 50, offset: int = 0):
    rows = db.history_rows(site_id=site, status=status, keyword=keyword or None,
                           start=start or None, end=end or None,
                           limit=limit, offset=offset)
    total = db.history_count(site_id=site, status=status, keyword=keyword or None,
                             start=start or None, end=end or None)
    return ok(records=[_job_view(r) for r in rows], total=total,
              limit=limit, offset=offset, statuses=db.STATUS_LABEL)


@app.get("/api/history/export.csv")
def api_history_export(site: str = "all", status: str = "all", keyword: str = "",
                       start: str = "", end: str = ""):
    content, n = db.history_csv(site_id=site, status=status,
                                keyword=keyword or None,
                                start=start or None, end=end or None)
    stamp = db.now_str().replace("-", "").replace(":", "").replace(" ", "_")
    filename = "subflow_history_%s.csv" % stamp
    # BOM 让 Excel 正确识别 UTF-8
    return PlainTextResponse(
        "\ufeff" + content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename})


# ================================================================ 日志

@app.get("/api/logs")
def api_logs(since_id: int = 0, level: str = "ALL", source: str = "all",
             keyword: str = "", limit: int = 500):
    rows, last_id = db.log_query(level=level, source=source, since_id=since_id,
                                 limit=limit, keyword=keyword or None)
    return ok(logs=rows, last_id=last_id,
              sources=db.log_sources(),
              levels=["ALL", "INFO", "WARN", "ERROR", "FATAL"])


@app.get("/api/logs/download")
def api_logs_download():
    rows = db.log_query(limit=200000)
    lines = ["[%s] [%s] [%s] %s" % (r["ts"], r["level"], r["source"], r["message"])
             for r in rows[0]]
    body = "\n".join(lines) + "\n"
    stamp = db.now_str().replace("-", "").replace(":", "").replace(" ", "_")
    return PlainTextResponse(
        body, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="subflow_log_%s.txt"' % stamp})


@app.delete("/api/logs")
def api_logs_clear(keep_last: int = 0):
    n = db.log_clear(keep_last)
    log("INFO", "日志已清理，删除 %d 条" % n, SRC_SYS)
    return ok(message="已清理 %d 条日志" % n)


# ================================================================ 配置

@app.get("/api/config")
def api_config_get():
    return ok(config=config.masked(), config_file=config.path)


class ConfigPatch(BaseModel):
    patch: dict


@app.post("/api/config")
def api_config_set(req: ConfigPatch):
    """
    深合并写入配置。

    敏感字段的「保持原值」语义由配置层统一处理：`qbittorrent.api_key`、
    `cookiecloud.password`，以及 `downloaders` 数组里每一项的 `api_key` /
    `password`，留空或原样回传掩码都视为不修改，传 "__CLEAR__" 才是显式清空。
    这样前端可以直接把 GET 回来的脱敏配置改几个字段就提交。
    """
    config.update(dict(req.patch or {}))
    engine.reload_config()
    log("INFO", "配置已更新并热加载", SRC_SYS)
    return ok(config=config.masked(), message="配置已保存")


# ---- 下载器 ----

@app.get("/api/downloaders")
def api_downloaders():
    """下载器清单 + 可选的类型（供「新增下载器」的下拉框使用）。"""
    return ok(downloaders=[d.to_dict() for d in engine.dls.all()],
              types=TYPE_LABELS,
              count=len(engine.dls))


class DownloaderProbe(BaseModel):
    """测试连接用的临时配置，字段与 downloaders 数组条目一致。"""
    type: str = "qbittorrent"
    url: str = ""
    api_key: str = ""
    username: str = ""
    password: str = ""
    conf_path: str = ""
    save_map: str = ""
    timeout: int = 15


@app.post("/api/downloaders/test")
def api_downloader_test(req: DownloaderProbe):
    """
    就地测试一份下载器配置能否连通（读版本 + 拉一条种子），不写入配置。

    有了它就能在「设置」里先把地址与凭据试对再保存，避免存进错误配置后
    扫描整轮失败。掩码形式的凭据会用已保存的同名值兜底。
    """
    cfg = req.model_dump() if hasattr(req, "model_dump") else req.dict()

    # 前端回显出来的掩码不是真凭据，用已保存的同类型下载器的值补上
    for k in ("api_key", "password"):
        val = str(cfg.get(k) or "")
        if val and "*" in val:
            for d in engine.dls.all():
                if d.type_name == req.type and d.cfg(k):
                    cfg[k] = d.cfg(k)
                    break

    try:
        dl = create(cfg)
    except Exception as e:  # noqa: BLE001
        return err(str(e))

    probe_ok, msg = dl.health()
    if not probe_ok:
        return err(msg)
    out = {"message": msg}
    try:
        ts = dl.list_torrents(limit=1, timeout=dl.list_timeout)
        out["probe"] = "列表接口正常，返回 %d 条" % len(ts)
    except Exception as e:  # noqa: BLE001
        out["probe"] = "版本可读但列表接口异常：%s" % e
    return ok(**out)


@app.post("/api/cookiecloud/refresh")
def api_cc_refresh():
    try:
        data = engine.cc.fetch(force=True)
        meta = engine.cc.meta()
        log("INFO", "CookieCloud 已强制刷新：%d 个域名 / %d 条 Cookie"
            % (meta.get("domains", len(data)), meta.get("cookies", 0)), SRC_SYS)
        return ok(meta=meta, domains=engine.cc.domains())
    except Exception as e:  # noqa: BLE001
        log("ERROR", "CookieCloud 刷新失败：%s" % e, SRC_SYS)
        return err(str(e))


# ================================================================ 静态资源

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    log("ERROR", "接口异常 %s：%s: %s" % (request.url.path, type(exc).__name__, exc),
        SRC_SYS)
    return JSONResponse(status_code=500,
                        content={"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
