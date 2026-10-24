# -*- coding: utf-8 -*-
"""
下载器注册表与下载器池
=======================

职责：
1. **注册表**：类型名 -> 实现类。新增下载器只改这一处。
2. **配置归一**：把配置文件里的 `downloaders` 数组转成实例；
   若没有 `downloaders`（老部署），从旧的 `qbittorrent` 段自动生成一个，
   保证升级后不改配置也能直接跑。
3. **下载器池**：引擎持有的多下载器容器，负责装配与批量健康检查。
"""

from .base import (DownloaderBase, DownloaderError, join_url, make_torrent,
                   unescape_comment)
from .qbittorrent import QBClient, QBError, QbittorrentDownloader
from .transmission import TransmissionDownloader

# 类型注册表：新增下载器只需在这一行后面加一项
REGISTRY = {
    QbittorrentDownloader.type_name: QbittorrentDownloader,
    TransmissionDownloader.type_name: TransmissionDownloader,
}

# 界面下拉框用的类型清单
TYPE_LABELS = [
    {"type": QbittorrentDownloader.type_name,
     "label": QbittorrentDownloader.label,
     "hint": "填 WebUI API Key（qB ≥ 5.2）；老版本改填用户名 + 密码"},
    {"type": TransmissionDownloader.type_name,
     "label": TransmissionDownloader.label,
     "hint": "TR 没有 API Key，填 RPC 用户名 + 密码（未开认证则留空）"},
]


def create(cfg):
    """按配置造一个下载器实例。"""
    t = str((cfg or {}).get("type") or "qbittorrent").strip().lower()
    cls = REGISTRY.get(t)
    if cls is None:
        raise DownloaderError("未知的下载器类型：%s（可选：%s）"
                              % (t, "、".join(sorted(REGISTRY))))
    return cls(cfg)


def build_configs(cfg):
    """
    从整份配置里解析出下载器配置列表。

    优先用 `downloaders` 数组；为空时回退到旧的单 qB 配置，
    这样从旧版本升级上来的实例不需要手工改配置文件。
    """
    raw = cfg.get("downloaders")
    items = []
    if isinstance(raw, list):
        for i, it in enumerate(raw):
            if not isinstance(it, dict):
                continue
            node = dict(it)
            node.setdefault("id", "dl%d" % (i + 1))
            node.setdefault("type", "qbittorrent")
            items.append(node)

    # 过滤掉明显没填的条目（用户在界面上新加一行但还没填地址）
    items = [x for x in items if str(x.get("url") or "").strip()
             or str(x.get("api_key") or "").strip()
             or str(x.get("conf_path") or "").strip()]

    if items:
        return items

    # ---- 兼容旧配置：从 qbittorrent 段生成一个默认下载器 ----
    q = cfg.get("qbittorrent") or {}
    if not isinstance(q, dict) or not (q.get("url") or q.get("api_key")
                                       or q.get("conf_path")):
        return []
    node = {"id": "default-qb", "type": "qbittorrent",
            "name": q.get("name") or "qBittorrent（由旧配置迁移）",
            "enabled": True}
    for k in ("url", "api_key", "conf_path", "username", "password",
              "save_map", "max_torrents", "timeout"):
        if q.get(k) not in (None, ""):
            node[k] = q.get(k)
    return [node]


class DownloaderPool:
    """多下载器容器。引擎通过它访问所有下载器，不直接 import 具体实现。"""

    def __init__(self, cfg=None):
        self._items = []
        if cfg is not None:
            self.configure(cfg)

    def configure(self, cfg):
        """按配置重建。返回 (成功数, 失败明细)。"""
        items, errors = [], []
        for node in build_configs(cfg or {}):
            try:
                items.append(create(node))
            except DownloaderError as e:
                errors.append({"id": node.get("id"), "message": str(e)})
        self._items = items
        return len(items), errors

    # ------------------------------------------------------------ 访问

    def all(self):
        return list(self._items)

    def enabled(self):
        return [x for x in self._items if x.enabled]

    def get(self, did):
        for x in self._items:
            if x.did == did:
                return x
        return None

    def get_or_first(self, did=None):
        """按 id 取；没给 id 或找不到时给第一个启用的（单下载器场景的便捷路径）。"""
        if did:
            hit = self.get(did)
            if hit is not None:
                return hit
        pool = self.enabled()
        return pool[0] if pool else None

    def __len__(self):
        return len(self._items)

    # ------------------------------------------------------------ 自检

    def health_all(self):
        """逐个探测。返回 [{id,name,type,enabled,ok,message}]。"""
        out = []
        for dl in self._items:
            ok, msg = dl.health()
            out.append({"id": dl.did, "name": dl.name, "type": dl.type_name,
                        "enabled": dl.enabled, "ok": ok, "message": msg})
        return out

    def describe(self):
        return ["%s(%s)" % (x.name, x.type_name) for x in self._items]


__all__ = [
    "DownloaderBase", "DownloaderError", "QbittorrentDownloader",
    "TransmissionDownloader", "DownloaderPool", "QBClient", "QBError",
    "REGISTRY", "TYPE_LABELS", "create", "build_configs",
    "make_torrent", "unescape_comment", "join_url",
]
