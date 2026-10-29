# -*- coding: utf-8 -*-
"""把主人丢过来的一段话解析成「要看什么」。

支持：BV 号、av 号、完整链接（含分P参数）、b23.tv 短链，其余当作关键词搜索。
解析顺序很重要 —— 先找视频标识，再退化到搜索，这样「帮我看看 BV1xx 怎么样」
不会被当成关键词去搜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BV_RE = re.compile(r"[Bb][Vv]([0-9A-Za-z]{10})")
AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])(?:av|aid=)(\d{1,12})", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s，。；、）)\]]+")
SHORT_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "bili33.cn")
PAGE_RE = re.compile(r"[?&]p=(\d+)")
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


@dataclass
class WatchTarget:
    """解析结果。"""

    kind: str  # "video" | "search"
    bvid: str = ""
    aid: int = 0
    page: int = 1
    keyword: str = ""
    short_link: str = ""

    @property
    def needs_short_link_resolve(self) -> bool:
        return bool(self.short_link)


def parse_target(raw: str) -> WatchTarget | None:
    """解析一段文本。返回 ``None`` 表示什么都没解析出来。"""

    text = CODE_FENCE_RE.sub(" ", str(raw or "")).strip()
    if not text:
        return None

    # 1) 完整链接
    for match in URL_RE.finditer(text):
        url = match.group(0)
        page = 1
        page_match = PAGE_RE.search(url)
        if page_match:
            page = max(1, int(page_match.group(1)))
        bv = BV_RE.search(url)
        if bv:
            return WatchTarget(kind="video", bvid="BV" + bv.group(1), page=page)
        av = AV_RE.search(url)
        if av:
            return WatchTarget(kind="video", aid=int(av.group(1)), page=page)
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if any(host == h or host.endswith("." + h) for h in SHORT_HOSTS):
            return WatchTarget(kind="video", short_link=url)

    # 2) 裸 BV 号
    bv = BV_RE.search(text)
    if bv:
        page = 1
        page_match = PAGE_RE.search(text)
        if page_match:
            page = max(1, int(page_match.group(1)))
        return WatchTarget(kind="video", bvid="BV" + bv.group(1), page=page)

    # 3) 裸 av 号
    av = AV_RE.search(text)
    if av:
        return WatchTarget(kind="video", aid=int(av.group(1)))

    # 4) 关键词
    keyword = re.sub(r"\s+", " ", text).strip()
    keyword = re.sub(r"^[#/]+", "", keyword).strip()
    if not keyword:
        return None
    return WatchTarget(kind="search", keyword=keyword[:60])


def looks_like_video(text: str) -> bool:
    """只判断有没有视频标识，不做搜索退化。"""

    blob = str(text or "")
    if BV_RE.search(blob) or AV_RE.search(blob):
        return True
    for match in URL_RE.finditer(blob):
        host = match.group(0).split("//", 1)[-1].split("/", 1)[0].lower()
        if "bilibili.com" in host or any(host == h or host.endswith("." + h) for h in SHORT_HOSTS):
            return True
    return False
