# -*- coding: utf-8 -*-
"""
下载器抽象层
=============

引擎只跟这层打交道，不关心背后是 qBittorrent、Transmission 还是别的。
新增一种下载器只需继承 `DownloaderBase` 并在 `downloaders/__init__.py` 注册。

统一契约（4 个能力 + 1 个可选）
--------------------------------
    health()                          -> (ok: bool, message: str)
    version()                         -> str
    list_torrents(limit, timeout)     -> List[Torrent]  归一化种子列表
    map_save_path(path)               -> str            下载器视角路径 -> 本机真实路径
    torrent_comment(hash)   [可选]     -> str            info 不带 comment 时按需补

`Torrent` 是归一化后的普通 dict，字段与具体下载器无关：

    hash       str   种子哈希（qB 的 hash / TR 的 hashString）
    name       str   种子名称
    save_path  str   下载器视角的保存目录（qB 的 save_path / TR 的 downloadDir）
    added_on   int   添加时间，Unix 时间戳
    size       int   字节数
    comment    str   备注（通常是发布页 URL）——**能省一次请求就省**
    category   str   所属分类（qB 的 category / TR 的首个 label），未分类为空串
    tags       list  全部分类/标签。qB 的 tags 是逗号串，这里统一成 list
    state      str   下载器原始状态字符串
    raw        dict  原始返回项，便于排查问题时回溯

关于「分类」的抽象（qB 分类 vs TR 标签）
----------------------------------------
qB 有独立的「分类」（category，可配保存路径，有专门的接口）；
TR 没有分类，功能上等价的是「标签」（label，一个种子可以打多个）。
两者在本层被统一成「一个种子归属若干分类」：

    list_categories()        该下载器的分类**定义**（qB 走接口；TR 无此概念返回空表）
    torrent_categories(t)    该种子**实际归属**的分类列表（空列表 = 未分类）

引擎据此做两件事：统计每个分类下的种子数量、按分类筛选扫描范围。
qB 分类接口不可用时（老版本 / 权限受限）引擎会自动退化为「从种子列表里
统计实际出现过的分类」，功能不受影响。

关于 `comment` 的取舍（实测驱动，不是拍脑袋）
--------------------------------------------
早期实现假设 qB 的 `torrents/info` 不返回 comment，于是对每个种子额外发一次
`torrents/properties`。实测 qB 5.2 的 `torrents/info` 返回 66 个字段、其中**含
comment**（187 个种子里 185 个非空），Transmission 的 `torrent-get` 同样直接给出
comment。所以 **`list_torrents` 应尽量把 comment 一起带回来**，`torrent_comment()`
只在老版本下载器（如 qB 4.x 的 info 不含该字段）时才被调用，作为降级补漏。
"""

import re

# 路径分隔符统一处理，避免 Windows/Linux 行为差异
_SEP_RE = re.compile(r"[\\/]+")


class DownloaderError(RuntimeError):
    """下载器交互失败（连接、鉴权、协议错误等）。"""


def unescape_comment(raw):
    """部分站点的 comment 是 HTML 片段或转义过的 URL，先还原再匹配。"""
    if not raw:
        return ""
    text = str(raw).replace("&amp;", "&").replace("&#38;", "&")
    return re.sub(r"\s+", " ", text).strip()


def make_torrent(hash="", name="", save_path="", added_on=0, size=0,
                 comment="", category="", tags=None, state="", raw=None):
    """构造归一化种子记录。缺失字段一律给安全默认值，调用方不必判 None。"""
    return {
        "hash": str(hash or ""),
        "name": str(name or ""),
        "save_path": str(save_path or ""),
        "added_on": int(added_on or 0),
        "size": int(size or 0),
        "comment": str(comment or ""),
        "category": str(category or ""),
        "tags": [str(x) for x in (tags or []) if str(x).strip()],
        "state": str(state or ""),
        "raw": raw if raw is not None else {},
    }


class DownloaderBase:
    """
    下载器基类。

    子类必须设置 `type_name`，并实现 `_do_health` / `list_torrents`。
    配置通过 `configure(cfg)` 注入，cfg 即配置文件中该下载器的那一项。
    """

    type_name = "base"
    # 界面上展示的默认名称
    label = "下载器"

    def __init__(self, cfg=None):
        self._cfg = {}
        self.did = ""
        self.name = ""
        self.enabled = True
        self.save_map = ""
        self.timeout = 15
        self.configure(cfg or {})

    # ------------------------------------------------------------ 配置

    def configure(self, cfg):
        """注入配置。子类覆写时记得先调 super()。"""
        self._cfg = dict(cfg or {})
        self.did = str(self._cfg.get("id") or self.type_name)
        self.name = str(self._cfg.get("name") or self.label)
        self.enabled = bool(self._cfg.get("enabled", True))
        self.save_map = str(self._cfg.get("save_map") or "")
        self.timeout = int(self._cfg.get("timeout", 15) or 15)
        # 单个下载器一次最多取回多少个种子：给极大值会让服务端明显变慢
        self.max_torrents = int(self._cfg.get("max_torrents", 5000) or 5000)
        # 取种子列表的超时。列表接口比一般的探测慢很多，单独放宽并保底 60s
        self.list_timeout = max(60, self.timeout * 4)
        return self

    def cfg_dict(self):
        """归一化后的配置副本（只读用途）。"""
        return dict(self._cfg)

    def cfg(self, key, default=None):
        return self._cfg.get(key, default)

    # ------------------------------------------------------------ 能力

    def health(self):
        """连通性自检，返回 (ok, 说明)。不抛异常——探测接口必须吞掉所有错误。"""
        try:
            started = _now_ms()
            ver = self.version()
            return True, "%s %s（%d ms）" % (self.label, ver, _now_ms() - started)
        except Exception as e:  # noqa: BLE001 - 探测接口需要吞掉所有异常
            return False, _short_err(e)

    def version(self):
        raise NotImplementedError

    def list_torrents(self, limit=5000, timeout=90):
        """
        返回归一化种子列表，按添加时间倒序。
        子类应尽量在这一次请求里把 comment 一起取回。
        """
        raise NotImplementedError

    def torrent_comment(self, torrent_hash):
        """按需补取单条 comment。仅当 list_torrents 没带回 comment 时才需要。"""
        return ""

    # ------------------------------------------------------------ 分类

    #: 该下载器是否有「分类」概念。qB 有；无此概念的实现置 False，
    #: 界面上对应的分类区块会被隐藏。
    supports_categories = True

    def list_categories(self):
        """
        返回分类**定义**列表 ``[{"name": str, "save_path": str}]``。

        默认返回空表：表示「没有独立的分类接口」。引擎遇到空表不会报错，
        而是退化为「从种子列表统计实际出现过的分类」——功能一致，只是
        统计不到「存在但一个种子都没有」的空分类（那本来也没用）。
        """
        return []

    def torrent_categories(self, torrent):
        """
        单个种子**归属**的分类名列表。空列表 = 未分类。

        基类按归一化字段 `category` 判定；TR 这种一个种子多标签的
        实现应覆写本方法返回全部标签。
        """
        cat = str((torrent or {}).get("category") or "").strip()
        return [cat] if cat else []

    # ------------------------------------------------------------ 路径映射

    def map_save_path(self, remote_path):
        """
        下载器容器内路径 -> 本机真实路径，用于本机直接写文件。

        save_map 形如 "/downloads=/path/to/downloads"，逗号分隔可配多组。
        没命中任何规则时原样返回（说明下载器与 SubFlow 看到的是同一套路径）。
        """
        path = str(remote_path or "")
        for pair in (self.save_map or "").split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            src, dst = pair.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if src and path.startswith(src):
                return dst + path[len(src):]
        return path

    # ------------------------------------------------------------ 展示

    def to_dict(self, probe=False):
        """序列化给前端。凭据一律不外传。"""
        out = {
            "id": self.did,
            "type": self.type_name,
            "label": self.label,
            "name": self.name,
            "enabled": self.enabled,
            "url": self.cfg("url", ""),
            "save_map": self.save_map,
            "timeout": self.timeout,
            "supports_categories": self.supports_categories,
        }
        if probe:
            ok, msg = self.health()
            out["ok"] = ok
            out["message"] = msg
        return out


# ---------------------------------------------------------------- 小工具

def _now_ms():
    import time
    return int(time.time() * 1000)


def _short_err(e, limit=220):
    msg = str(e) or e.__class__.__name__
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:limit] + ("…" if len(msg) > limit else "")


def join_url(base, path):
    return "%s/%s" % (str(base or "").rstrip("/"), str(path or "").lstrip("/"))
