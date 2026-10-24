# -*- coding: utf-8 -*-
"""
Transmission 下载器（RPC 协议）
===============================

与 qBittorrent 最大的协议差异有两处，都在这里处理掉了：

1. **会话 ID 握手**：TR 的 RPC 要求请求头带 ``X-Transmission-Session-Id``。
   首次（或 TR 重启后）请求必然返回 **409**，响应头里给出新的 session id，
   客户端须带上它重发。这里做了「409 -> 记下 id -> 重发一次」的自动处理，
   并在会话失效时再次自愈，不需要人工干预。

2. **认证**：TR 走 HTTP Basic Auth（4.x 的 ``rpc-authentication-required``），
   不像 qB 有 API Key 一说。URL 里内嵌 ``user:pass@`` 也支持。

字段映射（TR -> 归一化）
------------------------
    hashString   -> hash
    name         -> name
    comment      -> comment        （TR 的 torrent-get 直接返回，零额外请求）
    downloadDir  -> save_path      （对应 qB 的 save_path）
    addedDate    -> added_on
    totalSize    -> size
    status(数字) -> 可读状态字符串
    labels       -> category/tags  （TR 无「分类」，用标签等价表达，可多值）

TR 不支持服务端排序，所以取回后在本地按 addedDate 倒序。
"""

import requests

from .base import (DownloaderBase, DownloaderError, make_torrent,
                   unescape_comment)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# torrent-get 里我们真正需要的字段。只取必要字段能让报文小很多。
TORRENT_FIELDS = ["id", "hashString", "name", "comment", "downloadDir",
                  "addedDate", "totalSize", "sizeWhenDone", "status",
                  "percentDone", "labels", "error", "errorString"]

# TR 的 status 是数字枚举，转成可读字符串便于和 qB 的状态对齐观察
TR_STATUS = {
    0: "stopped",      # 已暂停
    1: "check-wait",
    2: "checking",
    3: "download-wait",
    4: "downloading",
    5: "seed-wait",
    6: "seeding",      # 做种中（等于 qB 的做种态）
}


class TransmissionDownloader(DownloaderBase):
    type_name = "transmission"
    label = "Transmission"

    def __init__(self, cfg=None):
        self.url = ""
        self._session_id = ""
        self._sess = requests.Session()
        super().__init__(cfg)

    # ------------------------------------------------------------ 配置

    def configure(self, cfg):
        super().configure(cfg)
        self.url = str(self._cfg.get("url") or "").rstrip("/")
        self._session_id = ""
        self._sess = requests.Session()
        return self

    @property
    def rpc_url(self):
        """
        容忍用户填各种形态的地址：
            http://host:9091                    -> /transmission/rpc
            http://host:9091/                   -> /transmission/rpc
            http://host:9091/transmission       -> /transmission/rpc
            http://host:9091/transmission/rpc   -> 原样
        """
        u = self.url.rstrip("/")
        if u.endswith("/rpc"):
            return u
        if u.endswith("/transmission"):
            return u + "/rpc"
        return u + "/transmission/rpc"

    def _auth(self):
        user = self._cfg.get("username") or ""
        pwd = self._cfg.get("password") or ""
        return (user, pwd) if user else None

    # ------------------------------------------------------------ 传输

    def _rpc(self, method, arguments=None, timeout=None):
        """发一次 RPC。409 时取回 session id 自动重发（TR 的固定握手流程）。"""
        payload = {"method": method}
        if arguments is not None:
            payload["arguments"] = arguments
        url = self.rpc_url

        for attempt in range(2):
            headers = {"Content-Type": "application/json", "User-Agent": UA}
            if self._session_id:
                headers["X-Transmission-Session-Id"] = self._session_id
            try:
                r = self._sess.post(url, json=payload, headers=headers,
                                    auth=self._auth(),
                                    timeout=timeout or self.timeout)
            except requests.RequestException as e:
                raise DownloaderError("%s 请求失败（%s）：%s" % (self.label, url, e))

            # 409 不是错误，是 TR 要求客户端先取会话 ID
            if r.status_code == 409:
                sid = r.headers.get("X-Transmission-Session-Id")
                if not sid:
                    raise DownloaderError(
                        "%s 返回 409 但未给出会话 ID，请确认 %s 是 Transmission 的 RPC 地址"
                        % (self.label, url))
                self._session_id = sid
                continue

            if r.status_code == 401:
                raise DownloaderError(
                    "%s 鉴权失败(401)：TR 开启了 RPC 认证，请填写用户名与密码" % self.label)
            if r.status_code == 403:
                raise DownloaderError(
                    "%s 拒绝访问(403)：请检查 TR 的 rpc-whitelist 是否允许本机 IP" % self.label)
            if r.status_code >= 400:
                raise DownloaderError("%s 返回 HTTP %s：%s"
                                      % (self.label, r.status_code, r.text[:200]))

            try:
                data = r.json()
            except ValueError:
                raise DownloaderError(
                    "%s 返回的不是 JSON，地址可能不是 Transmission RPC（%s）"
                    % (self.label, url))

            result = (data or {}).get("result")
            if result != "success":
                raise DownloaderError("%s 调用 %s 失败：%s" % (self.label, method, result))
            return (data or {}).get("arguments") or {}

        raise DownloaderError("%s 会话握手失败（409 重试后仍未成功）" % self.label)

    # ------------------------------------------------------------ 能力

    def version(self):
        args = self._rpc("session-get", timeout=self.timeout)
        ver = str(args.get("version") or "").strip()
        rpc = args.get("rpc-version")
        if ver and rpc:
            return "%s (RPC %s)" % (ver, rpc)
        if ver:
            return ver
        raise DownloaderError("%s 无法读取版本信息" % self.label)

    def list_torrents(self, limit=5000, timeout=90):
        args = self._rpc("torrent-get", {"fields": TORRENT_FIELDS},
                         timeout=timeout)
        data = args.get("torrents") or []
        if not isinstance(data, list):
            raise DownloaderError("torrent-get 返回结构异常")

        out = []
        for t in data:
            st = t.get("status")
            labels = t.get("labels") or []
            if not isinstance(labels, list):
                labels = [x.strip() for x in str(labels).split(",") if x.strip()]
            out.append(make_torrent(
                hash=t.get("hashString") or "",
                name=t.get("name") or "",
                save_path=(t.get("downloadDir") or t.get("download-dir") or ""),
                added_on=t.get("addedDate") or 0,
                size=t.get("totalSize") or t.get("sizeWhenDone") or 0,
                comment=unescape_comment(t.get("comment") or ""),
                # TR 没有分类，用标签等价表达；category 取首个标签便于通用展示
                category=(labels[0] if labels else ""),
                tags=labels,
                state=TR_STATUS.get(st, str(st) if st is not None else ""),
                raw=t,
            ))
        # TR 无服务端排序，本地按添加时间倒序后再截断
        out.sort(key=lambda x: x["added_on"], reverse=True)
        return out[:int(limit)] if limit else out

    def torrent_categories(self, torrent):
        """
        TR 的「分类」= 标签。一个种子可以有多个标签，全部返回。

        与 qB 的差异要记清楚：qB 的 category 是**排他的单值**，TR 的 label 是**多值**。
        因此同一个种子在分类统计里会被计入多个分类，各分类数量之和可能大于种子总数。
        """
        tags = (torrent or {}).get("tags") or []
        if not isinstance(tags, list):
            tags = [x.strip() for x in str(tags).split(",")]
        return [str(x).strip() for x in tags if str(x).strip()]

    def list_categories(self):
        """
        TR 没有分类定义接口，返回空表。

        引擎会自动退化为「从种子列表统计实际出现过的标签」，
        所以界面上依然能看到 TR 的标签清单与数量，只是拿不到「空标签」而已。
        """
        return []

    def torrent_comment(self, torrent_hash):
        """TR 的列表接口已经带回 comment，这里只在需要精确补取时用。"""
        if not torrent_hash:
            return ""
        try:
            args = self._rpc("torrent-get",
                             {"fields": ["comment"], "ids": [torrent_hash]},
                             timeout=self.timeout)
        except DownloaderError:
            return ""
        for t in (args.get("torrents") or []):
            cm = t.get("comment")
            if cm:
                return unescape_comment(cm)
        return ""

    # ------------------------------------------------------------ 会话探测

    def session_info(self):
        """额外能力：取 TR 的会话配置，便于设置页展示下载目录等。"""
        try:
            args = self._rpc("session-get", timeout=self.timeout)
        except DownloaderError:
            return {}
        return {
            "download_dir": args.get("download-dir") or "",
            "rpc_version": args.get("rpc-version"),
            "version": args.get("version") or "",
            "incomplete_dir": args.get("incomplete-dir") or "",
            "incomplete_dir_enabled": bool(args.get("incomplete-dir-enabled")),
        }
