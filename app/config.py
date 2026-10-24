# -*- coding: utf-8 -*-
"""
SubFlow 配置层
==============

三层覆盖，优先级由低到高：

    1. DEFAULTS            —— 代码内置默认值
    2. config.json         —— 持久化配置（界面上「设置」页写入）
    3. 环境变量            —— SUBFLOW_<SECTION>_<KEY>，便于 Docker / K8s 注入

示例：
    SUBFLOW_SERVER_PORT=8099
    SUBFLOW_QBITTORRENT_API_KEY=qbt_xxx
    SUBFLOW_COOKIECLOUD_UUID=0123456789abcdefghijkl

敏感字段（api_key / password）在对外输出时统一脱敏，见 mask_secrets()。
"""

import copy
import json
import os
import threading

APP_NAME = "SubFlow"
APP_VERSION = "1.1.0"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.getenv("SUBFLOW_CONFIG_FILE", os.path.join(BASE_DIR, "config.json"))

# ---------------------------------------------------------------- 默认配置

DEFAULTS = {
    "server": {
        "host": "0.0.0.0",
        "port": 8099,
        # 日志页面轮询间隔（秒），前端使用
        "poll_interval": 2.0,
    },
    # ---- 旧版单下载器配置（仅为向后兼容保留）----
    # 新的多下载器配置见下面的 "downloaders"。当 downloaders 为空时，
    # 引擎会自动从这里迁移出一个 qBittorrent 实例，老部署无需改配置。
    "qbittorrent": {
        "url": "http://127.0.0.1:8080",
        # qB >= 5.2 使用 WebUI\APIKey，鉴权 Header 为 Authorization: Bearer
        "api_key": "",
        # 留空则从 conf_path 中的 WebUI\APIKey 读取
        "conf_path": "",
        # qB 容器内保存路径 -> 宿主机真实路径，逗号分隔多组
        # 例："/downloads=/path/to/downloads"
        "save_map": "",
        "timeout": 15,
        # 单次拉取的种子条数上限（按添加时间倒序，取最近的一批）。
        # 注意：qB 的 limit 参数传极大值（如 10 万）会让服务端明显变慢直至读超时。
        "max_torrents": 5000,
    },
    # ---- 多下载器列表（可同时接多个 qB / Transmission 实例）----
    # 每个条目支持的键：
    #   id             唯一标识，任务历史里会记录来源下载器
    #   type           qbittorrent | transmission
    #   name           界面显示名
    #   enabled        是否参与扫描
    #   url            下载器地址。TR 填 http://host:9091 即可，会自动补 /transmission/rpc
    #   api_key        qB >= 5.2 的 WebUI API Key
    #   username/password  qB < 5.2 或 Transmission 使用；TR 未开 RPC 认证可留空
    #   conf_path      qB 的 qBittorrent.conf 路径，用于自动读取 API Key
    #   save_map       该下载器容器内路径 -> 本机真实路径，逗号分隔多组
    #   max_torrents   单次拉取上限
    #   timeout        请求超时（秒）
    "downloaders": [],
    "cookiecloud": {
        "url": "http://127.0.0.1:8188",
        "uuid": "",
        "password": "",
        # 解密结果缓存秒数，避免频繁解密
        "cache_ttl": 300,
        "timeout": 20,
    },
    "download": {
        # 只扫描最近 N 天添加的 qB 任务
        "scan_days": 30,
        # 站点请求间隔（秒），对站点友好
        "req_interval": 1.5,
        "timeout": 20,
        # 并发下载线程数
        "workers": 3,
        # zip 字幕包是否自动解压出字幕文件
        "extract_zip": True,
        # rar / 7z 字幕包是否自动解压（依赖运行环境有 bsdtar / 7z / unrar）
        "extract_archive": True,
        # 解压后是否保留原压缩包
        "keep_archive": True,
        # 单个任务最多下载的字幕包数，0 表示不限制
        "max_subs_per_job": 0,
        # 手动提交任务时的默认保存目录（界面可覆盖）
        "manual_save_dir": "",
        # 旧版 qbit_sub_sync_state.json 路径，首次启动时导入历史（留空不导入）
        "legacy_state_file": "",
        "sub_exts": [".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt",
                     ".smi", ".txt"],
    },
    "sites": {
        # 站点总开关；单个站点的 enabled 覆盖这里
        "enabled": True,
        # 站点覆盖项：{site_id: {"enabled": bool, "base_url": str, "label": str}}
        "overrides": {},
    },
    "log": {
        "level": "INFO",
        # 日志保留天数，超期自动清理
        "retain_days": 30,
        # 单页返回的最大日志条数
        "page_size": 500,
    },
    # ---- 访问控制（HTTP Basic）----
    # 密码为空时视为未启用，避免配置改一半把自己锁在门外。
    # allow_localhost：来自 127.0.0.1 的请求免认证，供容器健康检查使用。
    "auth": {
        "enabled": False,
        "username": "admin",
        "password": "",
        "realm": "SubFlow",
        "allow_localhost": True,
    },
    # ---- 定时扫描 ----
    # 每 interval_hours 小时自动跑一次「最近 days 天」的扫描。
    # 首次启动只记录时间、不立即扫描，避免重启风暴。
    "schedule": {
        "enabled": False,
        "interval_hours": 12,
        "days": 3,
        "limit": 0,
        "refresh_cookie": False,
    },
    # ---- 缺字幕检测 ----
    # 只按本地文件系统判断，不访问站点；视频扩展名决定「哪些文件需要字幕」。
    "missing": {
        "video_exts": [".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv",
                       ".flv", ".rmvb", ".webm", ".m4v", ".mpg", ".mpeg", ".vob"],
        # 目录递归深度（0 = 只看种子根目录）
        "max_depth": 2,
        # 单个种子最多检查多少个文件，防止超大合集拖慢整轮
        "max_files": 3000,
    },
    "proxy": {
        "enabled": False,
        "http": "",
        "https": "",
    },
}

_SECRET_PATHS = {
    ("qbittorrent", "api_key"),
    ("cookiecloud", "password"),
    ("auth", "password"),
}

# downloaders 数组条目里需要掩码的键（多下载器的凭据字段）
_LIST_SECRET_KEYS = ("api_key", "password")

# 敏感字段传该值表示「显式清空」，与「留空 = 不修改」区分开
SECRET_CLEAR = "__CLEAR__"


class Config:
    """线程安全的配置容器。"""

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self.reload()

    # ---------------------------------------------------------- 读写

    def reload(self):
        with self._lock:
            self._data = copy.deepcopy(DEFAULTS)
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        stored = json.load(f)
                    _deep_merge(self._data, stored or {})
                except Exception as e:
                    # 配置损坏不应导致应用起不来
                    print("[WARN] config.json 解析失败，已忽略: %s" % e, flush=True)
            _apply_env(self._data)
            return copy.deepcopy(self._data)

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def all(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name):
        with self._lock:
            return copy.deepcopy(self._data.get(name, {}))

    def get(self, section, key, default=None):
        with self._lock:
            return self._data.get(section, {}).get(key, default)

    def update(self, patch):
        """
        深合并写入并落盘。返回更新后的完整配置。

        写入前统一处理敏感字段，规则（顶层与 downloaders 数组一致）：
          * 缺省 / 空串                    -> 保留原值（前端留空表示「不修改」）
          * 与当前值的掩码完全一致          -> 保留原值（前端原样回显掩码后提交）
          * "__CLEAR__"                    -> 显式清空

        这样前端可以直接把 GET 回来的 masked 配置改几个字段就提交，
        不会因为「把掩码当密码写回去」而把凭据写坏。
        """
        with self._lock:
            fixed = self._restore_secrets(patch or {})
            _deep_merge(self._data, fixed)
            self.save()
            return copy.deepcopy(self._data)

    def _restore_secrets(self, patch):
        """把 patch 里的敏感字段还原成「该写什么」。返回处理后的副本。"""
        patch = copy.deepcopy(patch)

        # 1) 顶层：qbittorrent.api_key / cookiecloud.password
        for sec, key in _SECRET_PATHS:
            node = patch.get(sec)
            if isinstance(node, dict) and key in node:
                node[key] = self._resolve_secret(
                    self._data.get(sec, {}).get(key), node.get(key))

        # 2) downloaders 数组：按 id 匹配原条目取旧值
        items = patch.get("downloaders")
        if isinstance(items, list):
            originals = {}
            for x in (self._data.get("downloaders") or []):
                if isinstance(x, dict):
                    originals[str(x.get("id"))] = x
            for it in items:
                if not isinstance(it, dict):
                    continue
                orig = originals.get(str(it.get("id"))) or {}
                for k in _LIST_SECRET_KEYS:
                    if k in it:
                        it[k] = self._resolve_secret(orig.get(k), it.get(k))
        return patch

    @staticmethod
    def _resolve_secret(old, new):
        old_s = str(old or "")
        new_s = "" if new is None else str(new)
        if new_s == SECRET_CLEAR:
            return ""
        # 留空、或原样回传了掩码 -> 视为不修改
        if not new_s or (old_s and new_s == _mask(old_s)):
            return old_s
        return new_s

    # ---------------------------------------------------------- 辅助

    def masked(self):
        """导出配置，敏感字段以掩码返回（用于前端回显）。"""
        data = self.all()
        for sec, key in _SECRET_PATHS:
            val = data.get(sec, {}).get(key) or ""
            data.setdefault(sec, {})[key] = _mask(val)
            data[sec][key + "_set"] = bool(val)
        # 多下载器列表里的凭据同样要掩码，否则会把密码明文回传给前端
        for it in (data.get("downloaders") or []):
            if not isinstance(it, dict):
                continue
            for k in _LIST_SECRET_KEYS:
                val = it.get(k) or ""
                it[k] = _mask(val)
                it[k + "_set"] = bool(val)
        return data


def _mask(value):
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _deep_merge(base, patch):
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _coerce(text, template):
    """把环境变量字符串还原成与默认值同类型。"""
    if isinstance(template, bool):
        return str(text).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(template, int):
        try:
            return int(text)
        except ValueError:
            return template
    if isinstance(template, float):
        try:
            return float(text)
        except ValueError:
            return template
    if isinstance(template, list):
        return [x.strip() for x in str(text).split(",") if x.strip()]
    return text


def _apply_env(data):
    """SUBFLOW_<SECTION>_<KEY> 形式的扁平环境变量覆盖。"""
    prefix = "SUBFLOW_"
    reserved = {"SUBFLOW_CONFIG_FILE"}
    for env_key, raw in os.environ.items():
        if not env_key.startswith(prefix) or env_key in reserved:
            continue
        parts = env_key[len(prefix):].lower().split("_", 1)
        if len(parts) != 2:
            continue
        sec, key = parts
        if sec not in data or not isinstance(data[sec], dict):
            continue
        template = data[sec].get(key, "")
        data[sec][key] = _coerce(raw, template)
    return data


config = Config()
