# -*- coding: utf-8 -*-
"""
NexusPHP 通用字幕适配器
=======================

NexusPHP 系站点（kamept / hdarea / pandapt / pttime / U2 等）共用同一套字幕
机制，因此一份实现即可覆盖多个站点：

    种子的 comment 里带详情页链接  ->  /details.php?id=<torrent_id>
    详情页「字幕」区挂字幕包        ->  /downloadsubs.php?torrentid=<tid>&subid=<sid>

站点之间的差异只有 base_url、域名与少量 HTML 细节，用类属性或子类覆写即可。
解析函数全部是纯函数，单元测试无需联网。

两级字幕来源
------------
* **种子级**（`parse_subtitles`）：详情页「字幕」区挂在具体种子下的字幕包，
  链接形如 ``downloadsubs.php?torrentid=<tid>&subid=<sid>``。
  需要本地 qB 有该种子才能定位落盘目录。
* **站点级**（`parse_subtitle_zone`）：``subtitles.php`` 字幕区，跨种子汇总，
  **包含种子已被删除、详情页已失效但字幕仍留在站内的条目**。
  分页形如 ``subtitles.php?page=N``（N 从 0 开始，30 条/页）。
"""

import re

from .base import SiteAdapter

# 详情页链接：兼容 http/https、任意路径前缀、参数带命中标记
_DETAIL_IN_URL = re.compile(r"details\.php\?[^#\s\"'<>]*?\bid=(\d+)", re.I)

# 字幕链接：优先匹配 <a href="...downloadsubs.php?...">标题</a>
_SUB_ANCHOR = re.compile(
    r"<a\b[^>]*?href\s*=\s*[\"']([^\"']*downloadsubs\.php\?[^\"']*)[\"'][^>]*>(.*?)</a>",
    re.I | re.S)

# 裸链接兜底（部分模板直接输出 URL 或标题在相邻节点）
_SUB_BARE = re.compile(
    r"downloadsubs\.php\?torrentid=(\d+)&(?:amp;)?subid=(\d+)", re.I)

# 标题取值顺序：<title> -> <h1> -> 明确的标题行
_TITLE_TAGS = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_TAGS = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)

# 站点名后缀，需要从标题里剥掉，如 "标题 :: KamePT"
_TITLE_SUFFIX = re.compile(r"\s*[:：|·\-–]{1,2}\s*(?:Powered\s+by|NexusPHP).*$", re.I)

# ---------------------------------------------------------------- 字幕区
# 字幕区行：<td>语言</td><td><a href="downloadsubs...">标题</a></td>
#          <td><nobr><span title="时间">相对</span></nobr></td><td>大小</td>
#          <td>点击</td><td>上传者</td><td>举报</td>
_ZONE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_ZONE_SUB = re.compile(
    r"downloadsubs\.php\?torrentid=(\d+)&(?:amp;)?subid=(\d+)", re.I)
_ZONE_LINK = re.compile(r"<a\b[^>]*downloadsubs[^>]*>(.*?)</a>", re.I | re.S)
_ZONE_FLAG = re.compile(r"pic/flag/[^\"']*[\"'][^>]*?(?:alt|title)=[\"']([^\"']+)[\"']", re.I)
_ZONE_TIME = re.compile(r"<span[^>]*title=[\"']([0-9]{4}-[0-9]{2}-[0-9]{2}[^\"']*)[\"']", re.I)
_ZONE_PAGE = re.compile(r"subtitles\.php\?[^\"'>]*?page=(\d+)", re.I)
_ZONE_CELL = re.compile(r"<td\b[^>]*>", re.I)


class NexusPHPAdapter(SiteAdapter):
    """NexusPHP 站点通用实现。"""

    # 可覆写：该适配器认领的域名列表（含别名域）
    host_aliases = ()

    # 详情页里字幕链接所在的容器名，仅用于日志说明
    subtitle_zone_hint = "字幕"

    # NexusPHP 自带「字幕区」汇总页，站点级字幕来源
    supports_subtitle_zone = True

    # ------------------------------------------------------------ 域名匹配

    def _hosts(self):
        hosts = {self.domain.lower().lstrip(".")}
        hosts.update(h.lower().lstrip(".") for h in self.host_aliases)
        if self.base_url:
            rest = self.base_url.split("://", 1)[-1]
            hosts.add(rest.split("/")[0].split("?")[0].lower())
        return {h for h in hosts if h}

    def host_match(self, host):
        host = (host or "").lower()
        if not host:
            return False
        return any(host == h or host.endswith("." + h) for h in self._hosts())

    # ------------------------------------------------------------ 解析

    def parse_torrent_id(self, comment):
        if not comment:
            return None
        for m in re.finditer(r"https?://[^\s\"'<>,;]+", comment):
            url = m.group(0)
            host = url.split("://", 1)[-1].split("/")[0].split("?")[0]
            if not self.host_match(host):
                continue
            dm = _DETAIL_IN_URL.search(url)
            if dm:
                return dm.group(1)
        # 站点域名对不上时不再猜测，避免把别站种子算到本站
        return None

    def parse_subtitles(self, html, torrent_id):
        """
        返回 [(subid, title), ...]。
        兼容两种模板：<a href="downloadsubs...">标题</a> 与裸链接 + 邻近 <u>标题</u>。
        """
        torrent_id = str(torrent_id)
        found = []           # [(subid, title)]
        seen = set()

        # 策略一：<a> 包裹，标题取锚文本
        for m in _SUB_ANCHOR.finditer(html or ""):
            href, inner = m.group(1), m.group(2)
            sm = _SUB_BARE.search(href)
            if not sm or sm.group(1) != torrent_id:
                continue
            subid = sm.group(2)
            if subid in seen:
                continue
            title = self._title_from(href, inner, html, m.end())
            seen.add(subid)
            found.append((subid, title))

        # 策略二：裸链接 + 后续节点里的 <u>标题</u>
        for m in _SUB_BARE.finditer(html or ""):
            tid, subid = m.group(1), m.group(2)
            if tid != torrent_id or subid in seen:
                continue
            tail = (html or "")[m.end(): m.end() + 600]
            um = re.search(r"<u>(.*?)</u>", tail, re.S)
            title = self.clean_text(um.group(1)) if um else self.clean_text(tail[:160])
            seen.add(subid)
            found.append((subid, title[:120]))

        return found

    def _title_from(self, href, inner, html, end_pos):
        """按可信度依次取字幕名。"""
        text = self.clean_text(inner)
        if text:
            return text[:120]
        # href 上的 title / data-* 属性
        attr = re.search(r"(?:title|data-original-title)\s*=\s*[\"']([^\"']+)[\"']", href)
        if attr:
            return self.clean_text(attr.group(1))[:120]
        # 邻近节点
        tail = html[end_pos: end_pos + 400]
        um = re.search(r"<u>(.*?)</u>", tail, re.S)
        if um:
            return self.clean_text(um.group(1))[:120]
        return ""

    def parse_name(self, html):
        """详情页资源标题。"""
        if not html:
            return ""
        m = _H1_TAGS.search(html)
        if m:
            name = self.clean_text(m.group(1))
            if name and len(name) > 2:
                return _TITLE_SUFFIX.sub("", name)[:200]
        m = _TITLE_TAGS.search(html)
        if m:
            raw = self.clean_text(m.group(1))
            # NexusPHP 的 <title> 常是 "标题 :: 站点名"
            parts = re.split(r"\s*[:：]{2}\s*", raw, maxsplit=1)
            name = parts[0].strip()
            if name and name.lower() not in ("nexusphp", "index"):
                return name[:200]
        return ""

    # ------------------------------------------------------------ 站点级字幕区

    def subtitle_zone_url(self, page=0):
        page = int(page or 0)
        if page <= 0:
            return "%s/subtitles.php" % self.base_url
        return "%s/subtitles.php?page=%d" % (self.base_url, page)

    def zone_last_page(self, html):
        """从分页链接推断最大页码（0 基），用于「全部页」模式。"""
        pages = []
        for raw in _ZONE_PAGE.findall(html or ""):
            try:
                pages.append(int(raw))
            except ValueError:
                continue
        return max(pages) if pages else 0

    def parse_subtitle_zone(self, html):
        """
        解析字幕区列表页。字段按 NexusPHP 标准模板的 7 列顺序取：
        语言 / 标题 / 添加时间 / 大小 / 点击 / 上传者 / 举报。

        注意：部分站点模板的锚点标签未闭合（``<a href="..."<b>标题</b></a>``），
        因此标题用「从 downloadsubs 锚点到 </a>」整段截取，兼容畸形 HTML。
        """
        out, seen = [], set()
        for m_row in _ZONE_ROW.finditer(html or ""):
            row = m_row.group(1)
            m_sub = _ZONE_SUB.search(row)
            if not m_sub:
                continue
            tid, sid = m_sub.group(1), m_sub.group(2)
            if (tid, sid) in seen:
                continue
            seen.add((tid, sid))

            cells = _ZONE_CELL.split(row)[1:]
            def cell(idx):
                return self.clean_text(cells[idx]) if idx < len(cells) else ""

            lang = ""
            fm = _ZONE_FLAG.search(cells[0] if cells else row)
            if fm:
                lang = self.clean_text(fm.group(1))

            am = _ZONE_LINK.search(row)
            title = self.clean_text(am.group(1)) if am else ""
            if not title:
                # 兜底：subid 之后到 </a> 之间的文本
                tail = row[m_sub.end():]
                title = self.clean_text(tail.split("</a>", 1)[0])[:200]

            tm = _ZONE_TIME.search(cells[2] if len(cells) > 2 else row)
            out.append({
                "torrent_id": tid,
                "subid": sid,
                "title": title[:200],
                "lang": lang,
                "added": tm.group(1) if tm else "",
                "size": cell(3)[:24],
                "clicks": cell(4)[:12],
                "uploader": cell(5)[:40],
                "url": self.subtitle_url(tid, sid),
            })
        return out

    # ------------------------------------------------------------ 登录态

    def looks_logged_out(self, html):
        if not html:
            return True
        low = html.lower()
        head = low[:8000]
        # NexusPHP 未登录时通常出现 login.php 表单或 "请先登录"
        if "logout.php" in low:
            return False
        if "takelogin.php" in head:
            return True
        if "login.php" in head and "userdetails.php" not in low:
            return True
        if "请先登录" in html or "you must login" in low[:4000]:
            return True
        return False
