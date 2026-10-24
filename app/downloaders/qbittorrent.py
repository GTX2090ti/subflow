# -*- coding: utf-8 -*-
"""
qBittorrent 下载器（多版本兼容）
================================

qB 跨版本的差异主要在**鉴权方式**和**字段命名**上，这里各做了兜底。

鉴权方式（按候选顺序自动降级，成功后缓存）
------------------------------------------
1. `bearer`     ``Authorization: Bearer <API Key>``   —— qB >= 5.2 引入的 WebUI API Key
2. `x-api-key`  ``X-Api-Key: <API Key>``              —— 部分 4.6~5.1 构建 / 第三方分支
3. `cookie`     ``POST /api/v2/auth/login`` 拿 SID    —— 4.x 通用，需用户名密码
4. `none`       不带凭据                                —— qB 开了「对本地主机免密」时可用

踩坑记录：给 5.2 发错 Header（用 X-Api-Key）会吃 401；反过来给老版本发 Bearer 也可能
401。所以这里不假设版本，直接依次试，谁成功就记住谁。

字段差异
--------
- `save_path`：4.1~4.4 只有这一个；4.5+ 同时有 `download_path`（可能为空串）。
  这里按 ``save_path -> download_path -> content_path`` 顺序取第一个非空值。
- `comment`：**实测 5.2 的 `torrents/info` 直接返回 comment**（187 个种子里 185 个非空），
  所以默认不再逐条查 `torrents/properties`。若探测到该版本不返回 comment 字段，
  则标记 `info_has_comment=False`，由引擎按需逐条补——老版本才走这条慢路径。
- `category` / `tags`：`torrents/info` 一直带 `category`（未分类为空串）；
  `tags` 是**逗号分隔字符串**（TR 的 labels 是数组），本层统一成 list 后返回。
  分类的**定义**（含保存路径）另走 `torrents/categories`，见 `list_categories()`。
"""

import os

import requests

from .base import (DownloaderBase, DownloaderError, join_url, make_torrent,
                   unescape_comment)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 向后兼容旧名字（原 app/qbittorrent.py 的对外符号）
QBError = DownloaderError


class QbittorrentDownloader(DownloaderBase):
    type_name = "qbittorrent"
    label = "qBittorrent"

    AUTH_BEARER = "bearer"
    AUTH_HEADER = "x-api-key"
    AUTH_COOKIE = "cookie"
    AUTH_NONE = "none"

    def __init__(self, cfg=None):
        self.url = ""
        self.conf_path = ""
        self._api_key = ""
        self._auth_mode = None
        self._info_has_comment = True   # 乐观默认：不返回 comment 的版本才置 False
        self._sess = requests.Session()
        super().__init__(cfg)

    # ------------------------------------------------------------ 配置

    def configure(self, cfg):
        super().configure(cfg)
        self.url = str(self._cfg.get("url") or "").rstrip("/")
        self._api_key = str(self._cfg.get("api_key") or "").strip()
        self.conf_path = str(self._cfg.get("conf_path") or "")
        self._auth_mode = None
        self._sess = requests.Session()
        return self

    @property
    def api_key(self):
        """API Key 获取顺序：配置 -> qBittorrent.conf 的 WebUI\\APIKey=。"""
        if self._api_key:
            return self._api_key
        if self.conf_path and os.path.exists(self.conf_path):
            try:
                with open(self.conf_path, "r", encoding="utf-8",
                          errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("WebUI\\APIKey="):
                            return line.split("=", 1)[1].strip()
            except OSError as e:
                raise DownloaderError("读取 qBittorrent.conf 失败：%s" % e)
        return ""

    @property
    def info_has_comment(self):
        """该 qB 版本的 torrents/info 是否自带 comment 字段。"""
        return self._info_has_comment

    def _auth_plan(self):
        """按可用凭据列出候选鉴权方式。"""
        modes = []
        if self.api_key:
            modes += [self.AUTH_BEARER, self.AUTH_HEADER]
        if self._cfg.get("username") and self._cfg.get("password"):
            modes.append(self.AUTH_COOKIE)
        modes.append(self.AUTH_NONE)
        return modes

    # ------------------------------------------------------------ 传输

    def _base_headers(self):
        return {"User-Agent": UA,
                "Referer": self.url or "http://localhost",
                "Origin": self.url or "http://localhost"}

    def _ensure_cookie(self):
        """cookie 模式：登录一次，SID 存在 Session 里。"""
        user = self._cfg.get("username") or ""
        pwd = self._cfg.get("password") or ""
        r = self._sess.post(join_url(self.url, "api/v2/auth/login"),
                            data={"username": user, "password": pwd},
                            headers=self._base_headers(), timeout=self.timeout)
        if r.status_code >= 400 or (r.text or "").strip() not in ("Ok.", ""):
            raise DownloaderError(
                "qBittorrent 登录失败（HTTP %s，响应 %r），请检查用户名/密码"
                % (r.status_code, (r.text or "")[:60]))

    def _send(self, method, path, mode, params=None, data=None, timeout=None):
        url = join_url(self.url, "api/v2/" + str(path).lstrip("/"))
        headers = self._base_headers()
        if mode == self.AUTH_BEARER:
            key = self.api_key
            if not key:
                raise DownloaderError("未配置 API Key")
            headers["Authorization"] = "Bearer " + key
        elif mode == self.AUTH_HEADER:
            key = self.api_key
            if not key:
                raise DownloaderError("未配置 API Key")
            headers["X-Api-Key"] = key
        elif mode == self.AUTH_COOKIE:
            self._ensure_cookie()
        # AUTH_NONE：不带任何凭据

        try:
            return self._sess.request(method, url, headers=headers, params=params,
                                      data=data, timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise DownloaderError("%s 请求失败（%s）：%s" % (self.label, url, e))

    def _request(self, method, path, params=None, data=None, timeout=None):
        """
        统一入口：依次尝试候选鉴权方式，谁成功记住谁。
        网络层错误直接抛（换鉴权方式也救不了），HTTP 401/403 才继续降级。
        """
        plan = self._auth_plan()
        ordered = ([self._auth_mode] if self._auth_mode in plan else []) + \
                  [m for m in plan if m != self._auth_mode]
        last = ""
        for mode in ordered:
            try:
                r = self._send(method, path, mode, params, data, timeout)
            except DownloaderError as e:
                # cookie 登录失败属于鉴权问题，继续试下一种
                if mode != self.AUTH_COOKIE:
                    raise
                last = str(e)
                self._auth_mode = None
                continue
            if r.status_code in (401, 403):
                last = "HTTP %s" % r.status_code
                self._auth_mode = None
                continue
            if r.status_code >= 400:
                raise DownloaderError("%s 返回 HTTP %s：%s"
                                      % (self.label, r.status_code, r.text[:200]))
            self._auth_mode = mode
            return r
        raise DownloaderError(
            "%s 鉴权全部失败（%s）。请在「设置」中填写 WebUI API Key；"
            "老版本 qB（< 5.2）没有 API Key，请改填用户名和密码。"
            % (self.label, last or "无可用的鉴权方式"))

    # ------------------------------------------------------------ 能力

    def version(self):
        for path in ("app/version", "app/webapiVersion"):
            try:
                txt = (self._request("GET", path, timeout=self.timeout).text or "").strip()
            except DownloaderError:
                continue
            if txt and txt.lower() != "forbidden":
                return txt
        raise DownloaderError("%s 无法读取版本信息" % self.label)

    def list_torrents(self, limit=5000, timeout=90):
        """
        取种子列表（按添加时间倒序）。

        limit 给极大值（如 10 万）会让 qB 服务端明显变慢甚至读超时，
        按需取最近的一批即可。
        """
        params = {"limit": int(limit)}
        params["sort"] = "added_on"
        params["reverse"] = "true"
        data = self._request("GET", "torrents/info", params=params,
                             timeout=timeout).json()
        if not isinstance(data, list):
            raise DownloaderError("torrents/info 返回结构异常")

        # 探测该版本是否自带 comment 字段，决定引擎要不要走逐条补取的慢路径
        self._info_has_comment = bool(data) and ("comment" in data[0])

        out = []
        for t in data:
            # qB 的 tags 是逗号分隔字符串，这里统一成 list（TR 的 labels 本就是 list）
            raw_tags = t.get("tags") or ""
            if isinstance(raw_tags, str):
                tags = [x.strip() for x in raw_tags.split(",") if x.strip()]
            else:
                tags = [str(x) for x in raw_tags]
            out.append(make_torrent(
                hash=t.get("hash") or t.get("infohash_v1") or "",
                name=t.get("name") or "",
                # 4.5+ 的 download_path 可能是空串，必须按非空顺序取
                save_path=(t.get("save_path") or t.get("download_path")
                           or t.get("content_path") or ""),
                added_on=t.get("added_on") or 0,
                size=t.get("size") or t.get("total_size") or 0,
                comment=t.get("comment") or "",
                category=t.get("category") or "",
                tags=tags,
                state=t.get("state") or "",
                raw=t,
            ))
        return out

    def list_categories(self):
        """
        分类定义：``GET /api/v2/torrents/categories``。

        qB 返回的是 ``{"电影": {"name": "电影", "savePath": "/downloads/电影"}, ...}``。
        个别第三方分支/老版本返回过数组形态，这里两种都吃。
        该接口在 qB 4.x~5.x 均存在；若被权限拦住，引擎会退化为按种子统计。
        """
        data = self._request("GET", "torrents/categories",
                             timeout=self.timeout).json()
        rows = []
        if isinstance(data, dict):
            for key, node in data.items():
                node = node if isinstance(node, dict) else {}
                rows.append({"name": str(node.get("name") or key),
                             "save_path": str(node.get("savePath") or "")})
        elif isinstance(data, list):
            for node in data:
                if isinstance(node, dict):
                    rows.append({"name": str(node.get("name") or ""),
                                 "save_path": str(node.get("savePath") or "")})
        # 分类名是用户自建的，空名条目没有意义，直接丢弃
        rows = [x for x in rows if x["name"]]
        rows.sort(key=lambda x: x["name"].lower())
        return rows

    def torrent_comment(self, torrent_hash):
        """老版本降级路径：逐条查 properties 取 comment。"""
        if not torrent_hash:
            return ""
        try:
            d = self._request("GET", "torrents/properties",
                              params={"hash": torrent_hash},
                              timeout=self.timeout).json()
        except DownloaderError:
            return ""
        return unescape_comment((d or {}).get("comment") or "")

    def torrent_files(self, torrent_hash):
        """
        取种子文件列表：``[{"name": "dir/video.mp4", "size": 123, "priority": 1}]``。

        name 是相对 save_path 的完整相对路径（含目录），size/priority 为 int。
        请求失败或结构异常时返回 None，由调用方决定回退行为。
        """
        if not torrent_hash:
            return None
        try:
            data = self._request("GET", "torrents/files",
                                 params={"hash": torrent_hash},
                                 timeout=self.timeout).json()
        except Exception:  # noqa: BLE001 网络/鉴权等任何异常都不致命
            return None
        if not isinstance(data, list):
            return None
        out = []
        for f in data:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            try:
                out.append({"name": str(f["name"]),
                            "size": int(f.get("size") or 0),
                            "priority": int(f.get("priority") or 0)})
            except (TypeError, ValueError):
                continue
        return out or None

    # ------------------------------------------------------------ 兼容旧符号

    def torrents(self, limit=5000, sort="added_on", reverse=True, timeout=90):
        """旧 QBClient 的方法名，保留以免外部脚本失效。"""
        return self.list_torrents(limit=limit, timeout=timeout)

    def comment(self, torrent_hash):
        return self.torrent_comment(torrent_hash)

    def properties(self, torrent_hash):
        return self._request("GET", "torrents/properties",
                             params={"hash": torrent_hash},
                             timeout=self.timeout).json()


# 旧类名别名（老的导入路径 app.qbittorrent.QBClient 仍可用）
QBClient = QbittorrentDownloader
_unescape_comment = unescape_comment
