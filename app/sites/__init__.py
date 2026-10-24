# -*- coding: utf-8 -*-
"""
站点注册表
==========

内置站点全部基于同一套 NexusPHP 字幕机制，差异只在 base_url / 域名。
扩展新站点有两种方式：

  A. 同构站点（NexusPHP）—— 在下面 `BUILTIN` 里加一条即可：

        {"id": "hdsky", "label": "HDSky", "base_url": "https://hdsky.me",
         "domain": "hdsky.me", "aliases": ("hdsky.org",)}

  B. 异构站点（非 NexusPHP）—— 写一个继承 SiteAdapter 的类，实现
     parse_torrent_id / parse_subtitles / parse_name 三个纯函数，
     然后用 `register(MyAdapter(...))` 挂进来。适配器不负责网络 IO。

站点开关与 base_url 可被 config.json 的 `sites.overrides` 覆盖，界面上可改。
"""

import threading

from .base import SiteAdapter
from .nexusphp import NexusPHPAdapter

__all__ = ["SiteAdapter", "NexusPHPAdapter", "REGISTRY", "register", "get",
           "all_sites", "enabled_sites", "apply_config", "site_ids"]


class _BuiltinNexusPHP(NexusPHPAdapter):
    """由 BUILTIN 配置表生成的 NexusPHP 适配器实例。"""


# 内置站点清单（id / 显示名 / 站点根地址 / CookieCloud 域名 / 域名别名）
BUILTIN = [
    {
        "id": "kamept",
        "label": "KamePT",
        "base_url": "https://kamept.com",
        "domain": "kamept.com",
        "aliases": (),
        "note": "已验证：详情页挂「AI机翻字幕」等字幕包",
    },
    {
        "id": "hdarea",
        "label": "HDArea",
        "base_url": "https://hdarea.club",
        "domain": "hdarea.club",
        "aliases": ("hdarea.co",),
        "note": "NexusPHP，字幕包接口同构",
    },
    {
        "id": "pandapt",
        "label": "PandaPT",
        "base_url": "https://pandapt.net",
        "domain": "pandapt.net",
        "aliases": (),
        "note": "NexusPHP，字幕包接口同构",
    },
    {
        "id": "pttime",
        "label": "PTTime",
        "base_url": "https://www.pttime.org",
        "domain": "pttime.org",
        "aliases": ("pttime.fun",),
        "note": "NexusPHP，字幕包接口同构",
    },
    {
        "id": "u2",
        "label": "U2",
        "base_url": "https://u2.dmhy.org",
        "domain": "u2.dmhy.org",
        "aliases": (),
        "note": "NexusPHP（动漫），字幕包接口同构",
    },
]

REGISTRY = {}
_lock = threading.RLock()


def _build(cfg):
    adapter = _BuiltinNexusPHP(
        base_url=cfg["base_url"], label=cfg["label"], enabled=True)
    adapter.id = cfg["id"]
    adapter.domain = cfg["domain"]
    adapter.host_aliases = tuple(cfg.get("aliases") or ())
    adapter.note = cfg.get("note", "")
    adapter.enabled_by_default = cfg.get("enabled", True)
    adapter._enabled = adapter.enabled_by_default
    return adapter


def _init():
    with _lock:
        if REGISTRY:
            return
        for cfg in BUILTIN:
            adapter = _build(cfg)
            REGISTRY[adapter.id] = adapter


def register(adapter):
    """挂载自定义适配器。"""
    _init()
    with _lock:
        REGISTRY[adapter.id] = adapter
        return adapter


def get(site_id):
    _init()
    return REGISTRY.get((site_id or "").strip().lower())


def all_sites():
    _init()
    return list(REGISTRY.values())


def enabled_sites():
    return [s for s in all_sites() if s.enabled]


def site_ids():
    return [s.id for s in all_sites()]


def apply_config(sites_cfg):
    """
    应用 config.json 的 `sites` 段：
        enabled         总开关
        overrides       {site_id: {enabled, base_url, label}}
    返回生效后的适配器列表。
    """
    _init()
    sites_cfg = sites_cfg or {}
    master = sites_cfg.get("enabled", True)
    overrides = sites_cfg.get("overrides") or {}

    with _lock:
        for sid, ad in REGISTRY.items():
            ov = overrides.get(sid) or {}
            if "base_url" in ov and ov["base_url"]:
                ad.base_url = str(ov["base_url"]).rstrip("/")
            if "label" in ov and ov["label"]:
                ad.label = ov["label"]
            if "enabled" in ov and ov["enabled"] is not None:
                ad._enabled = bool(ov["enabled"])
            elif master is False:
                ad._enabled = False
            else:
                ad._enabled = ad.enabled_by_default
        return list(REGISTRY.values())


def find_by_host(host):
    """按域名反查站点（CookieCloud 域名清单 → 站点）。"""
    for ad in all_sites():
        if ad.host_match(host):
            return ad
    return None
