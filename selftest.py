# -*- coding: utf-8 -*-
"""SubFlow 离线自检：导入、解析器、SQLite 持久化。不需要联网。"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 用临时库，避免污染真实数据
os.environ["SUBFLOW_DB"] = os.path.join(tempfile.gettempdir(), "subflow_selftest.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(os.environ["SUBFLOW_DB"] + suffix)
    except OSError:
        pass

from app import db, sites  # noqa: E402
from app.config import Config  # noqa: E402
from app.downloaders import (DownloaderPool, QbittorrentDownloader,  # noqa: E402
                             TransmissionDownloader, build_configs, create,
                             make_torrent)
from app.downloaders.base import DownloaderError  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  PASS  %-46s %s" % (name, got))
    else:
        FAIL += 1
        print("  FAIL  %-46s got=%r want=%r" % (name, got, want))


print("=" * 70)
print("1) 站点注册表")
print("=" * 70)
check("站点数量", len(sites.site_ids()), 5)
ad = sites.get("kamept")

print()
print("=" * 70)
print("2) comment -> torrent_id")
print("=" * 70)
check("标准链接", ad.parse_torrent_id("https://kamept.com/details.php?id=43480&hit=1"), "43480")
check("http 无参数", ad.parse_torrent_id("see http://kamept.com/details.php?id=7"), "7")
check("别站不误判", ad.parse_torrent_id("https://hdarea.club/details.php?id=99"), None)
check("无链接", ad.parse_torrent_id("plain comment"), None)
check("空值", ad.parse_torrent_id(""), None)
check("子域匹配", sites.get("pttime").parse_torrent_id(
    "https://www.pttime.org/details.php?id=123"), "123")

print()
print("=" * 70)
print("3) 详情页 -> 字幕包列表")
print("=" * 70)
HTML = """<html><head><title>Buncos 3P1 :: KamePT</title></head><body>
<h1 align=center id=top>Buncos 3P1 1080p</h1>
<td class=rowfollow><a href="downloadsubs.php?torrentid=43480&amp;subid=9182"><u>AI\u673a\u7ffb\u5b57\u5e55 \u7b80\u4f53</u></a></td>
<td><a href="downloadsubs.php?torrentid=43480&amp;subid=9183">\u5b98\u65b9\u7e41\u4e2d</a></td>
downloadsubs.php?torrentid=43480&amp;subid=9184 ... <u>\u82f1\u6587\u5b57\u5e55</u>
<a href="downloadsubs.php?torrentid=99999&amp;subid=1">\u522b\u7684\u79cd\u5b50</a>
<a href="logout.php">\u767b\u51fa</a></body></html>"""
subs = ad.parse_subtitles(HTML, 43480)
check("解析出字幕包数", len(subs), 3)
check("第一个 subid", subs[0][0], "9182")
check("第一个标题", subs[0][1], "AI\u673a\u7ffb\u5b57\u5e55 \u7b80\u4f53")
check("第二个标题(锚文本)", subs[1][1], "\u5b98\u65b9\u7e41\u4e2d")
check("第三个标题(<u>邻近)", subs[2][1], "\u82f1\u6587\u5b57\u5e55")
check("不串到别站种子", [s for s in subs if s[0] == "1"], [])
check("空 HTML", ad.parse_subtitles("", 1), [])
check("详情页标题", ad.parse_name(HTML), "Buncos 3P1 1080p")
check("已登录判定", ad.looks_logged_out(HTML), False)
check("未登录判定", ad.looks_logged_out(
    '<html><body><form action="takelogin.php">login</form></body></html>'), True)

print()
print("=" * 70)
print("4) 地址生成")
print("=" * 70)
check("详情页 URL", ad.detail_url("43480"), "https://kamept.com/details.php?id=43480")
check("字幕 URL", ad.subtitle_url("43480", "9182"),
      "https://kamept.com/downloadsubs.php?torrentid=43480&subid=9182")

print()
print("=" * 70)
print("5) SQLite 持久化")
print("=" * 70)
db.init_db()
jid, created = db.job_upsert("kamept", "43480", "9182",
                             site_label="KamePT", torrent_name="Buncos 3P1",
                             sub_title="AI机翻字幕", status=db.ST_PENDING,
                             save_path="/tmp", source="selftest")
check("首次插入 created", created, True)
jid2, created2 = db.job_upsert("kamept", "43480", "9182",
                               torrent_name="Buncos 3P1 (renamed)")
check("重复插入复用同一条", jid2, jid)
check("重复插入 created=False", created2, False)
check("幂等后总数", db.job_count(), 1)

# 分类筛选：多标签以逗号串落库，未分类是空串
jid_cat, _ = db.job_upsert("kamept", "50001", "s1", status=db.ST_SUCCESS,
                           torrent_name="Cat Film", downloader_id="qb1",
                           category="电影,动画")
jid_unc, _ = db.job_upsert("kamept", "50002", "s2", status=db.ST_SUCCESS,
                           torrent_name="Uncat", downloader_id="qb1",
                           category="")
jid_other, _ = db.job_upsert("kamept", "50003", "s3", status=db.ST_SUCCESS,
                             torrent_name="TV", downloader_id="tr1",
                             category="剧集")
check("分类筛选命中多标签", len(db.job_list(category="电影")), 1)
check("分类整词匹配不误伤", len(db.job_list(category="影")), 0)
check("未分类筛选", len(db.job_list(category="__uncat__")), 2)  # 含最早的 jid
check("按下载器筛选", len(db.job_list(downloader_id="tr1")), 1)
check("分类+下载器组合", len(db.job_list(downloader_id="qb1", category="动画")), 1)
check("计数与列表同源", db.job_count(category="电影"),
      len(db.job_list(category="电影")))
cat_map = {(c["downloader_id"], c["category"]): c["count"]
           for c in db.job_categories()}
check("分类聚合展开多标签", cat_map.get(("qb1", "电影")), 1)
check("分类聚合未分类", cat_map.get(("qb1", "")), 1)
check("分类聚合按下载器隔离", cat_map.get(("tr1", "剧集")), 1)
for _id in (jid_cat, jid_unc, jid_other):
    db.job_delete(_id)
check("清理后回到 1 条", db.job_count(), 1)

db.job_update(jid, status=db.ST_SUCCESS, file_name="sub.zip", file_size=2048,
              extracted=3, finished_at=db.now_str(), duration_ms=1234)
row = db.job_get(jid)
check("状态已更新", row["status"], "success")
check("解压数记录", row["extracted"], 3)
check("known_subids 生效", db.known_subids("kamept", "43480"), {"9182"})

st = db.job_stats()
check("统计 success", st["success"], 1)
check("统计 total", st["total"], 1)
check("今日完成数", st["today"], 1)

# 历史记录
rows = db.history_rows()
check("历史记录条数", len(rows), 1)
check("历史含视频名", rows[0]["torrent_name"], "Buncos 3P1 (renamed)")
csv_text, n = db.history_csv()
check("CSV 导出行数", n, 1)
check("CSV 含表头", csv_text.splitlines()[0].startswith("编号"), True)

# 日志
db.log_write("INFO", "selftest info", source="system")
db.log_write("ERROR", "selftest error", source="task", job_id=jid)
logs, last = db.log_query(since_id=0)
check("日志条数", len(logs), 2)
inc, _ = db.log_query(since_id=last - 1)
check("增量拉取 1 条", len(inc), 1)
errs, _ = db.log_query(level="ERROR", since_id=0)
check("按级别过滤", len(errs), 1)
check("日志来源清单", "task" in db.log_sources(), True)

check("KV 读写", (db.kv_set("k", "v"), db.kv_get("k"))[1], "v")

# 状态复位
db.job_update(jid, status=db.ST_RUNNING)
check("复位中断任务", db.job_reset_stale(), 1)

print()
print("=" * 70)
print("6) 站点级字幕区（pandapt 真实样本）")
print("=" * 70)
# 样本取自 pandapt.net/subtitles.php 的真实返回，注意锚点标签是未闭合的畸形 HTML：
#   <a href="downloadsubs.php?...&subid=120"<b>标题</b></a>
ZONE_HTML = """<table><tr><td class=colhead>语言</td><td>标题</td></tr>
<tr><td class=rowfollow align=center valign=middle><img border="0" src="pic/flag/china.gif" alt="\u7b80\u4f53\u4e2d\u6587" title="\u7b80\u4f53\u4e2d\u6587"/></td> <td class=rowfollow align=left><a href="downloadsubs.php?torrentid=133488&subid=120"<b>[\u5fb7\u74e6\u8fbe\u65af].DC.2026.1080p.SunNXT</b></a></td> <td class=rowfollow align=center><nobr><span title="2026-09-09 10:29:26">10\u59290\u65f6</span></nobr></td> <td class=rowfollow align=center>430.16&nbsp;KB</td> <td class=rowfollow align=center>1</td> <td class=rowfollow align=center><i>\u533f\u540d</i></td> <td class=rowfollow align=center><a href="report.php?subtitle=120">x</a></td></tr>
<tr><td class=rowfollow align=center valign=middle><img border="0" src="pic/flag/china.gif" alt="\u7b80\u4f53\u4e2d\u6587" title="\u7b80\u4f53\u4e2d\u6587"/></td> <td class=rowfollow align=left><a href="downloadsubs.php?torrentid=119315&amp;subid=111">Queer.as.Folk.US.S05.chs</a></td> <td class=rowfollow align=center><nobr><span title="2026-05-04 00:56:08">4\u670818\u5929</span></nobr></td> <td class=rowfollow align=center>236.8&nbsp;KB</td> <td class=rowfollow align=center>7</td> <td class=rowfollow align=center><i>alice</i></td> <td class=rowfollow align=center></td></tr>
<tr><td class=rowfollow align=center>no</td><td class=rowfollow>no subtitle link here</td></tr>
<tr><td class=rowfollow align=left><a href="downloadsubs.php?torrentid=133488&subid=120"><b>dup</b></a></td></tr>
<tr><td><a href="subtitles.php?&amp;page=3">91 - 114</a></td></tr></table>"""
zone = ad.parse_subtitle_zone(ZONE_HTML)
check("字幕区条目数(去重/跳过无链接行)", len(zone), 2)
check("条目1 torrent_id", zone[0]["torrent_id"], "133488")
check("条目1 subid", zone[0]["subid"], "120")
check("条目1 语言", zone[0]["lang"], "\u7b80\u4f53\u4e2d\u6587")
check("条目1 畸形锚点标题", zone[0]["title"], "[\u5fb7\u74e6\u8fbe\u65af].DC.2026.1080p.SunNXT")
check("条目1 添加时间", zone[0]["added"], "2026-09-09 10:29:26")
check("条目1 大小", zone[0]["size"], "430.16 KB")
check("条目1 上传者", zone[0]["uploader"], "\u533f\u540d")
check("条目2 正常锚点标题", zone[1]["title"], "Queer.as.Folk.US.S05.chs")
check("条目2 &amp; 转义兼容", zone[1]["subid"], "111")
check("条目2 上传者", zone[1]["uploader"], "alice")
check("条目 URL", zone[1]["url"],
      "https://kamept.com/downloadsubs.php?torrentid=119315&subid=111")
check("字幕区 URL(首页)", ad.subtitle_zone_url(0), "https://kamept.com/subtitles.php")
check("字幕区 URL(第2页)", ad.subtitle_zone_url(1), "https://kamept.com/subtitles.php?page=1")
check("最大页码", ad.zone_last_page(ZONE_HTML), 3)
check("空 HTML", ad.parse_subtitle_zone(""), [])
check("非字幕区页面", ad.parse_subtitle_zone("<html>nothing</html>"), [])

print()
print("=" * 70)
print("7) 字幕内容类型嗅探与文件名还原")
print("=" * 70)
from app.engine import SubtitleEngine  # noqa: E402

sniff = SubtitleEngine._sniff_ext
check("rar 包", sniff(b"Rar!\x1a\x07\x00rest"), ".rar")
check("zip 包", sniff(b"PK\x03\x04rest"), ".zip")
check("7z 包", sniff(b"7z\xbc\xaf\x27\x1crest"), ".7z")
check("带 BOM 的 ass", sniff(b"\xef\xbb\xbf[Script Info]\r\nScriptType: v4.00+"), ".ass")
check("无 BOM 的 ass", sniff(b"[Script Info]\nScriptType: v4.00+"), ".ass")
check("ass 靠样式段识别", sniff(b"whatever\n[V4+ Styles]\nFormat: Name"), ".ass")
check("带 BOM 的 srt", sniff(b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:04,000\r\nhi"), ".srt")
check("裸 srt", sniff(b"1\n00:00:01,000 --> 00:00:04,000\nhello"), ".srt")
check("webvtt", sniff(b"WEBVTT\n\n00:00.000 --> 00:02.000"), ".vtt")
check("未知类型", sniff(b"\x89PNG\r\n\x1a\n"), ".bin")

check("名称归一化",
      SubtitleEngine._norm_name("[Group] Show.Name.S01.1080p.WEB-DL [CHS]"),
      "shownames011080pwebdl")
check("短名称不参与包含匹配",
      SubtitleEngine._match_local({"by_tid": {}, "by_name": {}}, ad, "1", "短标题"), None)
check("按种子ID命中本地",
      SubtitleEngine._match_local(
          {"by_tid": {("kamept", "43480"): {"name": "X"}}, "by_name": {}},
          ad, "43480", "任意")["name"], "X")
check("按名称包含命中本地",
      SubtitleEngine._match_local(
          {"by_tid": {}, "by_name": {"shownames011080pwebdl": {"name": "Y"}}},
          ad, "9", "[Group] Show.Name.S01.1080p.WEB-DL.chs")["name"], "Y")


class _FakeResp:
    def __init__(self, cd):
        self.headers = {"Content-Disposition": cd}


# 站点回的是 UTF-8 字节但未声明 charset，requests 按 latin-1 解出乱码
fname_utf8 = "[\u5fb7\u74e6\u8fbe\u65af\u4e0e\u94b1\u5fb7\u62c9].DC.ass"
cd = 'attachment; filename="%s"' % fname_utf8.encode("utf-8").decode("latin-1")
check("Content-Disposition 乱码还原",
      SubtitleEngine._filename_from_headers(_FakeResp(cd), "fb"), fname_utf8)
check("RFC5987 filename*",
      SubtitleEngine._filename_from_headers(
          _FakeResp("attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.srt"), "fb"),
      "\u4e2d\u6587.srt")
check("无 Content-Disposition 用兜底",
      SubtitleEngine._filename_from_headers(_FakeResp(""), "fallback.srt"),
      "fallback.srt")

# 文件名归一：真实站点会给 xxx.chs.rar（后缀已正确），必须避免 .rar.rar
norm = SubtitleEngine.normalize_name
check("后缀与内容一致不重复追加",
      norm("Queer.as.Folk.US.S05.1080p.AMZN.WEB-DL.DDP5.1.x264-NTb.chs.rar", ".rar"),
      "Queer.as.Folk.US.S05.1080p.AMZN.WEB-DL.DDP5.1.x264-NTb.chs.rar")
check("ass 后缀一致不重复追加", norm("show.ass", ".ass"), "show.ass")
check("语言码后缀补内容后缀", norm("show.chs", ".rar"), "show.chs.rar")
check("裸名补内容后缀", norm("subtitle_1_2", ".ass"), "subtitle_1_2.ass")
check("已知后缀与内容不符以内容为准", norm("show.txt", ".rar"), "show.rar")
check("路径分隔符被清洗", norm("a/b\\c.srt", ".srt"), "a_b_c.srt")
check("空名兜底", norm("", ".srt"), "subtitle.srt")
check("7z 后缀一致", norm("pack.7z", ".7z"), "pack.7z")

print()
print("=" * 70)
print("8) 多下载器抽象层")
print("=" * 70)

# ---- 配置归一：新数组优先，为空时从旧的 qbittorrent 段迁移 ----
_legacy = {"qbittorrent": {"url": "http://h:8080", "api_key": "legacykey",
                           "save_map": "/downloads=/mnt/disk1"}}
check("旧配置迁移出 1 个下载器", len(build_configs(_legacy)), 1)
check("迁移后 id", build_configs(_legacy)[0]["id"], "default-qb")
check("迁移后类型", build_configs(_legacy)[0]["type"], "qbittorrent")

_new = {"qbittorrent": {"url": "http://old:8080"},
        "downloaders": [
            {"id": "a", "type": "qbittorrent", "url": "http://h:8080"},
            {"id": "b", "type": "transmission", "url": "http://h:9091"},
            {"id": "c", "type": "qbittorrent", "url": ""},          # 空条目应被过滤
        ]}
check("新数组优先且过滤空条目", [x["id"] for x in build_configs(_new)], ["a", "b"])
check("完全没有下载器时返回空", build_configs({}), [])

_pool = DownloaderPool(_new)
check("池内数量", len(_pool), 2)
check("按 id 取名", _pool.get("b").name, "Transmission")
check(
    "未知 id 退化为首个启用项",
    _pool.get_or_first("不存在").did, "a")
_n, _errs = _pool.configure({"downloaders": [{"id": "x", "type": "aria2",
                                              "url": "http://h"}]})
check("未知类型不炸且装配数为 0", (_n, len(_errs)), (0, 1))

# ---- 路径映射跟着各自的下载器走 ----
_qb = QbittorrentDownloader({"id": "q", "type": "qbittorrent", "url": "http://h",
                             "save_map": "/downloads=/mnt/disk1, /data=/mnt/disk2"})
check("映射命中第一条", _qb.map_save_path("/downloads/PT/a"), "/mnt/disk1/PT/a")
check("映射命中第二条", _qb.map_save_path("/data/b"), "/mnt/disk2/b")
check("未命中原样返回", _qb.map_save_path("/other/c"), "/other/c")

# ---- qB：鉴权候选顺序随凭据变化 ----
check("有 API Key 走 bearer 优先",
      QbittorrentDownloader({"id": "q", "api_key": "k" * 32})._auth_plan()[:1], ["bearer"])
check("无 Key 有密码走 cookie 优先",
      QbittorrentDownloader({"id": "q", "username": "u",
                             "password": "p"})._auth_plan()[:1], ["cookie"])
check("无凭据只剩 none",
      QbittorrentDownloader({"id": "q"})._auth_plan(), ["none"])


class _FakeJsonResp:
    """伪造 requests 响应，用于验证字段归一逻辑。"""

    def __init__(self, payload, text=""):
        self.status_code = 200
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


# ---- qB：save_path 为空时回退 download_path；探测 info 是否自带 comment ----
_qb2 = QbittorrentDownloader({"id": "q", "type": "qbittorrent", "url": "http://h",
                              "api_key": "k"})
_qb2._request = lambda method, path, params=None, data=None, timeout=None: \
    _FakeJsonResp([
        {"hash": "h1", "name": "有 comment", "save_path": "/downloads/A",
         "added_on": 200, "size": 10, "comment": "https://kamept.com/details.php?id=1",
         "state": "seeding"},
        {"hash": "h2", "name": "save_path 为空", "save_path": "",
         "download_path": "/downloads/B", "added_on": 100, "size": 20,
         "comment": "", "state": "stopped"},
    ])
_ts = _qb2.list_torrents(limit=10)
check("qB 种子数", len(_ts), 2)
check("qB 字段归一 hash/name", (_ts[0]["hash"], _ts[0]["name"]), ("h1", "有 comment"))
check("qB 直接带回 comment", _ts[0]["comment"], "https://kamept.com/details.php?id=1")
check("qB save_path 为空回退 download_path", _ts[1]["save_path"], "/downloads/B")
check("qB 探测到 info 自带 comment", _qb2.info_has_comment, True)

# 老版本 qB：info 不含 comment 字段 -> 标记 False，触发引擎的补取慢路径
_qb3 = QbittorrentDownloader({"id": "q", "type": "qbittorrent", "url": "http://h",
                              "api_key": "k"})
_qb3._request = lambda method, path, params=None, data=None, timeout=None: \
    _FakeJsonResp([{"hash": "h", "name": "旧版本", "save_path": "/d"}])
_qb3.list_torrents(limit=1)
check("老版本 qB 判定为不带 comment", _qb3.info_has_comment, False)


# ---- Transmission：409 会话握手、字段映射、URL 归一化 ----
class _FakeTRResp:
    def __init__(self, code, payload, headers=None):
        self.status_code = code
        self._payload = payload
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class _FakeTRSession:
    """
    模拟 TR 的固定握手：不带 session id 必回 409 并下发 id，带上后返回正常 JSON。
    顺便记录每次请求的头，用来断言「id 被缓存、没有反复 409」。
    """

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, json=None, headers=None, auth=None, timeout=None):
        self.calls.append({
            "url": url,
            "sid": (headers or {}).get("X-Transmission-Session-Id"),
            "method": (json or {}).get("method"),
            "auth": auth,
        })
        if not (headers or {}).get("X-Transmission-Session-Id"):
            return _FakeTRResp(409, {}, {"X-Transmission-Session-Id": "SID-1"})
        return _FakeTRResp(200, self.payloads.pop(0), {})


_tr = TransmissionDownloader({"id": "t", "type": "transmission",
                              "url": "http://h:9091", "save_map": "/downloads=/mnt/disk1"})
check("TR 补全 rpc 路径", _tr.rpc_url, "http://h:9091/transmission/rpc")
_tr.configure({"id": "t", "type": "transmission", "url": "http://h:9091/transmission/rpc"})
check("TR 已带 rpc 不重复补", _tr.rpc_url, "http://h:9091/transmission/rpc")

_tr.configure({"id": "t", "type": "transmission", "url": "http://h:9091",
               "save_map": "/downloads=/mnt/disk1"})
_sess = _FakeTRSession([
    {"result": "success", "arguments": {"version": "4.0.6", "rpc-version": 17}},
    {"result": "success", "arguments": {"torrents": [
        {"hashString": "abc123", "name": "Show S01",
         "comment": "https://kamept.com/details.php?id=43480",
         "downloadDir": "/downloads/PT/Show", "addedDate": 1789726218,
         "totalSize": 4096, "status": 6},
        {"hashString": "def456", "name": "老片", "downloadDir": "/downloads/PT/Old",
         "addedDate": 100, "totalSize": 512, "status": 4},      # 无 comment
    ]}},
])
_tr._sess = _sess
check("TR 版本串", _tr.version(), "4.0.6 (RPC 17)")
check("TR 首访先吃 409 再成功", len(_sess.calls), 2)
check("TR 首次未带 session id", _sess.calls[0]["sid"], None)
check("TR 重试带上 session id", _sess.calls[1]["sid"], "SID-1")

_tl = _tr.list_torrents(limit=10)
check("TR 第二次调用复用 session id 不再 409", len(_sess.calls), 3)
check("TR 复用已缓存的 id", _sess.calls[2]["sid"], "SID-1")
check("TR 字段映射 hashString->hash", _tl[0]["hash"], "abc123")
check("TR 字段映射 downloadDir->save_path", _tl[0]["save_path"], "/downloads/PT/Show")
check("TR comment 直接带回", _tl[0]["comment"], "https://kamept.com/details.php?id=43480")
check("TR status 数字转可读", _tl[0]["state"], "seeding")
check("TR 本地按 addedDate 倒序", [x["name"] for x in _tl], ["Show S01", "老片"])
check("TR 路径映射生效", _tr.map_save_path(_tl[0]["save_path"]), "/mnt/disk1/PT/Show")


class _Fake401Session:
    def post(self, url, json=None, headers=None, auth=None, timeout=None):
        return _FakeTRResp(401, {}, {})


_tr401 = TransmissionDownloader({"id": "t", "type": "transmission",
                                 "url": "http://h:9091"})
_tr401._sess = _Fake401Session()
try:
    _tr401.version()
    check("TR 401 应抛错", "没抛", "抛错")
except DownloaderError as e:
    check("TR 401 提示鉴权", "鉴权失败" in str(e), True)


# ---- 多下载器凭据的掩码与「留空=不改」语义（用临时配置文件，不碰真实配置）----
_cfg_path = os.path.join(tempfile.gettempdir(), "subflow_selftest_cfg.json")
try:
    os.remove(_cfg_path)
except OSError:
    pass
_c = Config(_cfg_path)
_c.update({"downloaders": [{"id": "z", "type": "qbittorrent", "url": "http://h",
                            "api_key": "qbt_REALKEY1234567890",
                            "password": "tr-pwd-abcdef"}]})
_m = _c.masked()["downloaders"][0]
check("数组内 api_key 被掩码", "*" in _m["api_key"], True)
check("数组内 password 被掩码", "*" in _m["password"], True)
check("数组内 _set 标记", (_m["api_key_set"], _m["password_set"]), (True, True))

_c.update({"downloaders": [{"id": "z", "url": "http://h", "api_key": _m["api_key"],
                            "password": _m["password"]}]})
check("原样回传掩码不写坏凭据",
      _c.all()["downloaders"][0]["api_key"], "qbt_REALKEY1234567890")
_c.update({"downloaders": [{"id": "z", "url": "http://h", "api_key": "",
                            "password": ""}]})
check("留空保持原值", _c.all()["downloaders"][0]["password"], "tr-pwd-abcdef")
_c.update({"downloaders": [{"id": "z", "url": "http://h", "api_key": "__CLEAR__"}]})
check("__CLEAR__ 才清空", _c.all()["downloaders"][0]["api_key"], "")
_c.update({"downloaders": [{"id": "z", "url": "http://h", "api_key": "brand-new"}]})
check("传新值即覆盖", _c.all()["downloaders"][0]["api_key"], "brand-new")
try:
    os.remove(_cfg_path)
except OSError:
    pass

print()
print("=" * 70)
print("9) 下载器分类（读取分类 -> 按分类批量下载字幕）")
print("=" * 70)

# ---- 归一化种子的分类字段 ----
check("make_torrent 默认无分类", make_torrent()["category"], "")
check("make_torrent 默认 tags 为空表", make_torrent()["tags"], [])
_mt = make_torrent(name="X", category="电影", tags=["A", "B", ""])
check("make_torrent 带 category", _mt["category"], "电影")
check("make_torrent tags 去空", _mt["tags"], ["A", "B"])


def _qb_with(info, categories):
    """造一个只回固定 JSON 的 qB，用于验证分类字段归一。"""
    q = QbittorrentDownloader({"id": "q", "type": "qbittorrent",
                               "url": "http://h", "api_key": "k"})
    q._request = lambda method, path, params=None, data=None, timeout=None: \
        _FakeJsonResp(categories if "categories" in path else info)
    return q


# ---- qB：torrents/info 的 category / tags，torrents/categories 的分类定义 ----
_qb4 = _qb_with([
    {"hash": "h1", "name": "电影", "save_path": "/d", "category": "电影",
     "tags": "HDR, 中字", "added_on": 300},
    {"hash": "h2", "name": "剧集", "save_path": "/d", "category": "剧集",
     "tags": "", "added_on": 200},
    {"hash": "h3", "name": "未分类", "save_path": "/d", "added_on": 100},
], {"电影": {"name": "电影", "savePath": "/downloads/电影"},
    "剧集": {"name": "剧集", "savePath": "/downloads/剧集"},
    "空分类": {"savePath": "/downloads/empty"},      # 无 name，应回退到 key
    "": {"name": ""}})                              # 空名，应被丢弃
_qb_ts = _qb4.list_torrents(limit=10)
check("qB 归一 category", [t["category"] for t in _qb_ts], ["电影", "剧集", ""])
check("qB tags 逗号串拆成 list", _qb_ts[0]["tags"], ["HDR", "中字"])
check("qB 无 tags 为空表", _qb_ts[1]["tags"], [])
check("qB 分类归属", [_qb4.torrent_categories(t) for t in _qb_ts],
      [["电影"], ["剧集"], []])
_cat_map = {c["name"]: c["save_path"] for c in _qb4.list_categories()}
check("qB 分类定义数(丢弃空名)", len(_cat_map), 3)
check("qB 分类保存路径", _cat_map.get("电影"), "/downloads/电影")
check("qB 缺 name 时用 key 兜底", _cat_map.get("空分类"), "/downloads/empty")
check("qB 空名分类被丢弃", "" in _cat_map, False)
check("qB 分类定义按名称排序",
      [c["name"] for c in _qb4.list_categories()]
      == sorted(_cat_map, key=lambda x: x.lower()), True)

_qb5 = QbittorrentDownloader({"id": "q", "url": "http://h", "api_key": "k"})
_qb5._request = lambda *a, **k: _FakeJsonResp([{"name": "A", "savePath": "/a"}])
check("qB 分类接口返回数组也能吃", _qb5.list_categories(),
      [{"name": "A", "save_path": "/a"}])

# ---- TR：labels 等价表达分类 ----
_tr2 = TransmissionDownloader({"id": "t", "type": "transmission",
                               "url": "http://h:9091"})
_tr2._sess = _FakeTRSession([{"result": "success", "arguments": {"torrents": [
    {"hashString": "x", "name": "多标签", "downloadDir": "/d",
     "labels": ["剧集", "中字"], "addedDate": 5, "totalSize": 1, "status": 6},
    {"hashString": "y", "name": "无标签", "downloadDir": "/d",
     "addedDate": 4, "totalSize": 1, "status": 6},
]}}])
_tl2 = _tr2.list_torrents(limit=5)
check("TR 首个标签落到 category", _tl2[0]["category"], "剧集")
check("TR labels 全量保留", _tl2[0]["tags"], ["剧集", "中字"])
check("TR 无标签 category 为空", _tl2[1]["category"], "")
check("TR 分类=多标签(与 qB 单值语义不同)",
      _tr2.torrent_categories(_tl2[0]), ["剧集", "中字"])
check("TR 无分类定义接口(引擎会退化为统计)", _tr2.list_categories(), [])

# ---- 引擎：分类筛选的解析与匹配（纯函数，不联网）----
E = SubtitleEngine
_ids = {"dl1", "dl2"}
check("分类筛选空输入=不筛选", E._parse_category_filter([], _ids), {})
_spec = E._parse_category_filter(["dl1:电影", "剧集", "dl1:", ""], _ids)
check("带下载器前缀的项", sorted(_spec["dl1"]), ["", "电影"])
check("裸分类名归入 * 桶", _spec["*"], {"剧集"})
check("未知前缀不拆(分类名可能含冒号)",
      E._parse_category_filter(["某站:电影"], _ids)["*"], {"某站:电影"})
check("未分类别名 __none__", E._parse_category_filter(["__none__"], _ids)["*"], {""})
check("筛选项回显",
      E._category_filter_labels(["dl1:电影", "剧集", "dl1:"], _ids),
      ["dl1 / 电影", "剧集", "dl1 / 未分类"])

check("不筛选时全放行", E._cat_match({}, "dl1", []), True)
check("命中指定下载器的分类", E._cat_match({"dl1": {"电影"}}, "dl1", ["电影"]), True)
check("别的下载器同名分类不误命中",
      E._cat_match({"dl1": {"电影"}}, "dl2", ["电影"]), False)
check("裸分类名跨下载器命中",
      E._cat_match({"*": {"电影"}}, "dl2", ["电影"]), True)
check("多标签任一命中即可",
      E._cat_match({"*": {"中字"}}, "dl1", ["剧集", "中字"]), True)
check("未分类命中空分类",
      E._cat_match({"*": {""}}, "dl1", []), True)
check("选了具名分类不命中未分类种子",
      E._cat_match({"*": {"电影"}}, "dl1", []), False)

# ---- 任务/历史落库带分类 ----
jid_c, _new = db.job_upsert("kamept", "99001", "1", category="电影",
                            torrent_name="分类落库测试", status=db.ST_PENDING)
check("任务记录分类", db.job_get(jid_c)["category"], "电影")
db.job_update(jid_c, status=db.ST_SUCCESS, finished_at=db.now_str())
_hit = db.history_rows(keyword="分类落库测试")
check("历史记录带分类", _hit[0]["category"] if _hit else "", "电影")
csv2, _n2 = db.history_csv()
check("CSV 含来源分类列", "来源分类" in csv2.splitlines()[0], True)

print()
print("=" * 70)
print("10) 缺字幕检测 / 统计聚合 / 访问控制配置")
print("=" * 70)

# ---- 视频遍历：深度与数量上限 ----
_miss_dir = tempfile.mkdtemp(prefix="subflow_miss_")
os.makedirs(os.path.join(_miss_dir, "S01"), exist_ok=True)
os.makedirs(os.path.join(_miss_dir, "S01", "deep"), exist_ok=True)
for p in ("a.mkv", "S01/b.mp4", "S01/deep/c.mkv", "S01/d.txt"):
    open(os.path.join(_miss_dir, p), "w").close()

_vids = SubtitleEngine._walk_videos(_miss_dir, {".mkv", ".mp4"}, 1, 100)
check("遍历深度 1 只到第一层", sorted(os.path.basename(x) for x in _vids), ["a.mkv", "b.mp4"])
_vids2 = SubtitleEngine._walk_videos(_miss_dir, {".mkv", ".mp4"}, 3, 100)
check("遍历深度 3 覆盖深层", len(_vids2), 3)
check("遍历数量上限", len(SubtitleEngine._walk_videos(_miss_dir, {".mkv", ".mp4"}, 3, 1)), 1)
check("非目录返回空", SubtitleEngine._walk_videos(
    os.path.join(_miss_dir, "a.mkv"), {".mkv"}, 3, 100), [])

# ---- 字幕同目录/语言码/Subs 子目录判断 ----
open(os.path.join(_miss_dir, "withsub.mkv"), "w").close()
open(os.path.join(_miss_dir, "withsub.chs.srt"), "w").close()
open(os.path.join(_miss_dir, "nosub.mkv"), "w").close()
open(os.path.join(_miss_dir, "nosub2.ass"), "w").close()  # 基名不同，不算
open(os.path.join(_miss_dir, "S01", "insubs.mkv"), "w").close()
os.makedirs(os.path.join(_miss_dir, "S01", "Subs"), exist_ok=True)
open(os.path.join(_miss_dir, "S01", "Subs", "insubs.zh.srt"), "w").close()
open(os.path.join(_miss_dir, "S01", "stemdird.mkv"), "w").close()
os.makedirs(os.path.join(_miss_dir, "S01", "stemdird"), exist_ok=True)
open(os.path.join(_miss_dir, "S01", "stemdird", "stemdird.srt"), "w").close()

_subs = {".srt", ".ass"}
_hs = lambda name: SubtitleEngine._has_subtitle(
    os.path.join(_miss_dir, name), _subs)
check("语言码后缀算已有字幕", _hs("withsub.mkv"), True)
check("无字幕判缺", _hs("nosub.mkv"), False)
check("Subs 子目录算已有字幕", _hs(os.path.join("S01", "insubs.mkv")), True)
check("同名子目录算已有字幕", _hs(os.path.join("S01", "stemdird.mkv")), True)

# ---- 统计聚合（用增量断言，前面的用例已经写过库）----
_before_bundle = db.stats_bundle(days=7)
_before = _before_bundle["totals"]

_j1, _ = db.job_upsert("kamept", "88001", "1", status=db.ST_SUCCESS,
                       file_size=100, extracted=2, site_label="KamePT",
                       category="电影", source="scan")
_j2, _ = db.job_upsert("kamept", "88002", "1", status=db.ST_FAILED,
                       message="站点无字幕", site_label="KamePT", source="scan")
_j3, _ = db.job_upsert("hdarea", "88003", "1", status=db.ST_EXISTS,
                       file_size=50, site_label="HDArea", source="manual")
for _jid in (_j1, _j2, _j3):
    db.job_update(_jid, finished_at=db.now_str(), duration_ms=1200)
_after_bundle = db.stats_bundle(days=7)
_after = _after_bundle["totals"]

check("统计窗口补零到 7 天", len(_after_bundle["daily"]), 7)
check("统计窗口内新增总数", _after["total"] - _before["total"], 3)
check("统计成功=success+exists", _after["ok"] - _before["ok"], 2)
check("统计失败数", _after["failed"] - _before["failed"], 1)
check("统计总大小", _after["size"] - _before["size"], 150)
check("统计解压字幕数", _after["extracted"] - _before["extracted"], 2)
check("统计平均耗时取有效任务", _after["avg_ms"] > 0, True)
check("每日最后一格含今日新增", _after_bundle["daily"][-1]["total"] >= 3, True)


def _agg(bundle, group, rowkey, val="total"):
    return {r[rowkey]: r[val] for r in bundle.get(group) or []}


_b_site, _a_site = _agg(_before_bundle, "by_site", "site_id"), _agg(_after_bundle, "by_site", "site_id")
check("按站点统计 hdarea 新增", _a_site.get("hdarea", 0) - _b_site.get("hdarea", 0), 1)
check("按站点统计 kamept 新增", _a_site.get("kamept", 0) - _b_site.get("kamept", 0), 2)
_b_cat, _a_cat = _agg(_before_bundle, "by_category", "category"), _agg(_after_bundle, "by_category", "category")
check("按分类统计电影新增", _a_cat.get("电影", 0) - _b_cat.get("电影", 0), 1)
_b_src, _a_src = _agg(_before_bundle, "by_source", "source"), _agg(_after_bundle, "by_source", "source")
check("按来源统计 manual 新增", _a_src.get("manual", 0) - _b_src.get("manual", 0), 1)
check("失败原因聚合含站点无字幕",
      "站点无字幕" in [r["reason"] for r in _after_bundle["failures"]], True)
check("全量统计与窗口统计独立", _after_bundle["all_time"]["total"] >= 3, True)

# ---- 访问控制：默认关闭 + 密码脱敏 ----
_auth_cfg_path = os.path.join(tempfile.gettempdir(), "subflow_selftest_cfg.json")
try:
    os.remove(_auth_cfg_path)
except OSError:
    pass
_cfg_auth = Config(_auth_cfg_path)
_auth = _cfg_auth.section("auth")
check("访问控制默认关闭", _auth["enabled"], False)
check("默认允许本机免认证", _auth["allow_localhost"], True)
_cfg_auth.update({"auth": {"enabled": True, "username": "u",
                           "password": "very-secret-pw"}})
_masked = _cfg_auth.masked()["auth"]
check("认证密码不回传明文", "very-secret-pw" in str(_masked), False)
check("认证密码标记已设置", _masked["password_set"], True)
_cfg_auth.update({"auth": {"password": ""}})
check("密码留空保持原值", _cfg_auth.section("auth")["password"], "very-secret-pw")
_cfg_auth.update({"auth": {"password": "__CLEAR__"}})
check("认证密码可显式清空", _cfg_auth.section("auth")["password"], "")
try:
    os.remove(_cfg_auth.path)
except OSError:
    pass

# ---- 定时扫描 / 缺字幕默认值 ----
_sched_path = os.path.join(tempfile.gettempdir(), "subflow_selftest_sched.json")
try:
    os.remove(_sched_path)
except OSError:
    pass
_sched = Config(_sched_path)
check("定时扫描默认关闭", _sched.section("schedule")["enabled"], False)
check("定时扫描默认间隔 12h", _sched.section("schedule")["interval_hours"], 12)
check("缺字幕默认目录深度 2", _sched.section("missing")["max_depth"], 2)
try:
    os.remove(_sched.path)
except OSError:
    pass

# ---- 分类读取结果持久化（kv 快照）----
_snap = {"saved_at": "2026-10-24 05:00:00", "downloaders": [
    {"id": "dl1", "name": "qB", "type": "qbittorrent", "ok": True,
     "message": "", "total": 3, "categorized": 3, "uncategorized": 0,
     "cached": False,
     "categories": [{"name": "电影", "label": "电影", "count": 2,
                     "save_path": "/mnt/disk1/x"},
                    {"name": "剧集", "label": "剧集", "count": 1,
                     "save_path": ""}]}]}
db.save_categories(_snap)
_loaded = db.load_categories()
check("分类快照可回读", _loaded["downloaders"][0]["categories"][0]["name"], "电影")
check("分类快照保留保存时间", _loaded["saved_at"], "2026-10-24 05:00:00")
check("分类快照保留保存路径",
      _loaded["downloaders"][0]["categories"][0]["save_path"], "/mnt/disk1/x")
db.kv_set(db.CATEGORIES_KV, "not-json")
check("分类快照损坏时忽略", db.load_categories(), None)
db.kv_set(db.CATEGORIES_KV, '{"foo": 1}')
check("分类快照结构不对时忽略", db.load_categories(), None)
db.kv_set(db.CATEGORIES_KV, "")

import shutil  # noqa: E402
shutil.rmtree(_miss_dir, ignore_errors=True)

print()
print("=" * 70)
print("结果：PASS=%d  FAIL=%d" % (PASS, FAIL))
print("=" * 70)
sys.exit(1 if FAIL else 0)
