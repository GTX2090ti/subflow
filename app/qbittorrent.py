# -*- coding: utf-8 -*-
"""
qBittorrent WebAPI 客户端
=========================

只用到一个鉴权细节：qB >= 5.2 的 WebUI API Key 走
``Authorization: Bearer <key>``，而不是老版本的 ``X-Api-Key``。
带错 Header 会拿到 401，是最常见的踩坑点。

API Key 的获取顺序：
    1. 配置里的 qbittorrent.api_key
    2. qBittorrent.conf 中的 ``WebUI\\APIKey=``
"""

import os
import re
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class QBError(RuntimeError):
    pass


class QBClient:
    def __init__(self, url="", api_key="", conf_path="", timeout=15, save_map=""):
        self.configure(url, api_key, conf_path, timeout, save_map)

    def configure(self, url, api_key, conf_path="", timeout=15, save_map=""):
        self.url = (url or "").rstrip("/")
        self._api_key = api_key or ""
        self.conf_path = conf_path or ""
        self.timeout = int(timeout or 15)
        self.save_map = save_map or ""

    # ------------------------------------------------------------ 鉴权

    @property
    def api_key(self):
        if self._api_key:
            return self._api_key.strip()
        if self.conf_path and os.path.exists(self.conf_path):
            try:
                with open(self.conf_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("WebUI\\APIKey="):
                            return line.split("=", 1)[1].strip()
            except OSError as e:
                raise QBError("读取 qBittorrent.conf 失败：%s" % e)
        return ""

    def _headers(self):
        key = self.api_key
        if not key:
            raise QBError(
                "未配置 qBittorrent API Key。请在「设置」中填写，"
                "或在 qBittorrent.conf 中确认存在 WebUI\\APIKey。")
        return {"Authorization": "Bearer " + key, "User-Agent": UA}

    def _get(self, path, params=None, timeout=None):
        url = "%s/api/v2/%s" % (self.url, path.lstrip("/"))
        try:
            r = requests.get(url, headers=self._headers(), params=params,
                             timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise QBError("请求 qBittorrent 失败（%s）：%s" % (url, e))
        if r.status_code == 403:
            raise QBError("qBittorrent 拒绝访问(403)。若 qB 开了「对本地主机免密」，"
                          "容器化部署时来源 IP 是网桥地址，需要改用 API Key 鉴权。")
        if r.status_code == 401:
            raise QBError("qBittorrent 鉴权失败(401)，请检查 API Key 是否正确。")
        if r.status_code >= 400:
            raise QBError("qBittorrent 返回 HTTP %s：%s" % (r.status_code, r.text[:200]))
        return r

    # ------------------------------------------------------------ 接口

    def version(self):
        return self._get("app/version").text.strip()

    def torrents(self, limit=5000, sort="added_on", reverse=True, timeout=90):
        """
        取种子列表。默认按添加时间倒序并限量——qB 的 limit 参数给极大值（如 10 万）
        会让服务端明显变慢甚至读超时，按需取最近的一批即可。
        """
        params = {"limit": int(limit)}
        if sort:
            params["sort"] = sort
            params["reverse"] = "true" if reverse else "false"
        data = self._get("torrents/info", params=params, timeout=timeout).json()
        if not isinstance(data, list):
            raise QBError("torrents/info 返回结构异常")
        return data

    def properties(self, torrent_hash):
        return self._get("torrents/properties", params={"hash": torrent_hash}).json()

    def comment(self, torrent_hash):
        try:
            return (self.properties(torrent_hash) or {}).get("comment", "") or ""
        except QBError:
            return ""

    # ------------------------------------------------------------ 路径映射

    def map_save_path(self, container_path):
        """qB 容器内路径 -> 宿主机真实路径，用于宿主机直接写文件。"""
        path = container_path or ""
        for pair in (self.save_map or "").split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            src, dst = pair.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if src and path.startswith(src):
                return dst + path[len(src):]
        return path

    def health(self):
        """连通性自检，返回 (ok, 说明)。"""
        started = time.time()
        try:
            ver = self.version()
            return True, "qBittorrent %s（%d ms）" % (ver, (time.time() - started) * 1000)
        except Exception as e:  # noqa: BLE001 - 探测接口需要吞掉所有异常
            return False, str(e)


def parse_torrent_id_from_comment(comment, adapter):
    """便捷函数：用适配器从 comment 提取 torrent_id。"""
    if not comment or adapter is None:
        return None
    return adapter.parse_torrent_id(comment)


def _unescape_comment(raw):
    """部分站点的 comment 是 HTML 片段或转义过的 URL，先还原再匹配。"""
    if not raw:
        return ""
    text = raw.replace("&amp;", "&").replace("&#38;", "&")
    return re.sub(r"\s+", " ", text).strip()
