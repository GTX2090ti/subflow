# -*- coding: utf-8 -*-
"""
站点适配器基类
==============

一个适配器要回答四个问题，全部是纯函数（不发起网络请求，IO 由引擎负责）：

    parse_torrent_id(comment)      qB 种子的 comment 里有没有本站的详情页链接？
    detail_url(torrent_id)         详情页地址
    subtitle_url(torrent_id, subid) 字幕包下载地址
    parse_subtitles(html, tid)     详情页 HTML 里有哪些字幕包？
    parse_name(html)               详情页 HTML 里的资源标题（手动提交时补全名称）

另外用 `domain` 声明 CookieCloud 中对应的域名，引擎据此自动取登录态。
新增站点只需继承一个基类并在 `sites/__init__.py` 注册。

两级字幕来源
------------
* **种子级**：详情页上挂在某个种子下的字幕包（`parse_subtitles`）。
  需要本地 qB 里存在该种子才能定位保存目录，因此只在扫描 / 手动提交时使用。
* **站点级**：站点维护的字幕汇总页（如 NexusPHP 的 `subtitles.php` 字幕区），
  跨种子罗列全部字幕，**包含种子已被删除但仍留在站内的字幕**。
  由 `supports_subtitle_zone` 声明，`parse_subtitle_zone` 负责解析。

两者互不替代：种子级能精确落到视频目录，站点级能兜住历史与失效种子。
"""

import html as _html
import re


class SiteAdapter:
    # ---- 子类必须提供的元信息 ----
    id = ""            # 唯一标识，用于任务记录与配置覆盖
    label = ""         # 界面显示名
    base_url = ""      # 站点根地址，如 https://kamept.com
    domain = ""        # CookieCloud 域名关键词，如 kamept.com
    cookie_names = ()  # 只取这些 Cookie（留空表示全部）
    note = ""          # 界面上的一句话说明
    enabled_by_default = True

    # ---- 可选 ----
    supports_scan = True       # 是否参与全库扫描（关掉可只用手动提交）
    supports_subtitle_zone = False   # 是否有站点级「字幕区」汇总页
    zone_page_size = 30        # 字幕区每页条数，仅用于进度提示
    referer_path = "/"

    def __init__(self, base_url=None, enabled=None, label=None):
        if base_url:
            self.base_url = base_url.rstrip("/")
        if label:
            self.label = label
        self._enabled = self.enabled_by_default if enabled is None else bool(enabled)

    # ------------------------------------------------------------ 开关

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)

    # ------------------------------------------------------------ 地址

    def detail_url(self, torrent_id):
        return "%s/details.php?id=%s" % (self.base_url, torrent_id)

    def subtitle_url(self, torrent_id, subid):
        return "%s/downloadsubs.php?torrentid=%s&subid=%s" % (
            self.base_url, torrent_id, subid)

    def referer(self, torrent_id=None):
        if torrent_id:
            return self.detail_url(torrent_id)
        return self.base_url + self.referer_path

    # ------------------------------------------------------------ 解析（子类实现）

    def parse_torrent_id(self, comment):
        """从 qB 种子的 comment 提取本站 torrent_id，不匹配返回 None。"""
        raise NotImplementedError

    def parse_subtitles(self, html, torrent_id):
        """返回 [(subid(str), title(str)), ...]，按页面顺序去重。"""
        raise NotImplementedError

    def parse_name(self, html):
        """从详情页解析资源标题，解析不到返回空串。"""
        return ""

    # ------------------------------------------------------------ 站点级字幕区

    def subtitle_zone_url(self, page=0):
        """站点级字幕汇总页地址（page 从 0 开始）。不支持则返回空串。"""
        return ""

    def parse_subtitle_zone(self, html):
        """
        解析字幕区列表页，返回 [{torrent_id, subid, title, lang, added,
        size, clicks, uploader, url}, ...]。
        """
        return []

    def looks_logged_out(self, html):
        """粗判登录态是否失效（页面出现登录表单）。"""
        head = html[:6000].lower()
        if "login.php" in head and "logout.php" not in html.lower():
            return True
        return False

    # ------------------------------------------------------------ 工具

    @staticmethod
    def clean_text(raw):
        """去标签、还原实体、压缩空白。"""
        if not raw:
            return ""
        text = re.sub(r"<[^>]+>", " ", raw)
        text = _html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "domain": self.domain,
            "enabled": self.enabled,
            "supports_scan": self.supports_scan,
            "supports_subtitle_zone": self.supports_subtitle_zone,
            "note": self.note,
        }
