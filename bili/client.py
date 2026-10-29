# -*- coding: utf-8 -*-
"""B站公开 Web 接口客户端。

只依赖 ``httpx``（宿主 python-env 已自带），不需要登录就能读：视频详情、热门评论、
弹幕、排行榜、首页推荐流、搜索、相关推荐。**字幕**是唯一需要登录 cookie 的接口
（``SESSDATA``），未登录时 ``x/player/wbi/v2`` 仍返回 ``code=0`` 但字幕列表为空，
这一情况会被显式记进 :attr:`VideoPacket.subtitle_reason`，交给上层降级。

签名规则（WBI）与错误码取自 B站网页端实际行为，不是猜的：

- ``nav`` 接口即使未登录（``code=-101``）也会返回 ``wbi_img``，可用来算 mixin key；
- 需要签名的接口必须带 ``wts`` 与 ``w_rid``，参数按 key 升序、去掉 ``!'()*`` 后
  urlencode，再用 ``md5(query + mixin_key)``；
- 风控命中会返回 ``-352`` / ``-412`` / ``-509``，本模块统一抛 :class:`BiliError`。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx

UA_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_FINGER = "https://api.bilibili.com/x/frontend/finger/spi"
API_PLAYER_WBI = "https://api.bilibili.com/x/player/wbi/v2"
API_REPLY = "https://api.bilibili.com/x/v2/reply"
API_DANMAKU = "https://api.bilibili.com/x/v1/dm/list.so"
API_RANKING = "https://api.bilibili.com/x/web-interface/ranking/v2"
API_POPULAR = "https://api.bilibili.com/x/web-interface/popular"
API_RCMD = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"
API_SEARCH = "https://api.bilibili.com/x/web-interface/wbi/search/type"
API_RELATED = "https://api.bilibili.com/x/web-interface/archive/related"

#: WBI mixin key 混淆表（网页端硬编码，64 项）。
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

_CODE_HINTS = {
    -101: "这个接口要求登录，需要配 SESSDATA",
    -400: "参数不对",
    -403: "无权访问（可能要登录或大会员）",
    -404: "视频不存在或已删除",
    -412: "请求被B站风控拦截",
    -352: "触发B站风控校验",
    -509: "请求过于频繁，被限流",
}


class BiliError(Exception):
    """B站接口返回业务错误。"""

    def __init__(self, code: int, message: str = "", *, api: str = "") -> None:
        self.code = int(code)
        self.api = api
        hint = _CODE_HINTS.get(self.code, "")
        detail = message or hint or "未知错误"
        if hint and message and hint not in message:
            detail = f"{message}（{hint}）"
        super().__init__(f"B站接口错误 {self.code}: {detail}")


@dataclass
class Comment:
    """一条评论。"""

    user: str
    like: int
    message: str


@dataclass
class Candidate:
    """一个「可以去看」的视频候选，用于首页推荐 / 搜索 / 相关推荐。"""

    bvid: str
    title: str
    owner: str = ""
    duration_seconds: int = 0
    play: int = 0
    desc: str = ""


@dataclass
class VideoPacket:
    """一个视频的全部可读素材。"""

    bvid: str
    aid: int
    cid: int
    title: str
    desc: str = ""
    owner: str = ""
    owner_mid: int = 0
    duration_seconds: int = 0
    pubdate: int = 0
    tname: str = ""
    page: int = 1
    page_count: int = 1
    stat: dict[str, int] = field(default_factory=dict)
    subtitle_lines: list[str] = field(default_factory=list)
    subtitle_lang: str = ""
    subtitle_reason: str = ""
    comments: list[Comment] = field(default_factory=list)
    danmaku_samples: list[str] = field(default_factory=list)
    danmaku_total: int = 0
    related: list[Candidate] = field(default_factory=list)

    @property
    def url(self) -> str:
        suffix = f"?p={self.page}" if self.page > 1 else ""
        return f"https://www.bilibili.com/video/{self.bvid}{suffix}"

    @property
    def subtitle_text(self) -> str:
        return _join_subtitle_lines(self.subtitle_lines)


# ------------------------------------------------------------------ 文本工具


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_html(text: Any) -> str:
    """去掉搜索结果里的 ``<em class="keyword">`` 之类标签。"""

    return re.sub(r"<[^>]+>", "", str(text or "")).replace("&quot;", '"').strip()


def _duration_from_text(text: Any) -> int:
    """把 ``"12:34"`` / ``"1:02:03"`` 转成秒。"""

    parts = [p for p in str(text or "").split(":") if p.strip().isdigit()]
    if not parts:
        return 0
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _join_subtitle_lines(lines: list[str], *, max_chars: int = 0) -> str:
    """把字幕行拼成可读段落：过短的行并入同一段，超长行硬切。"""

    if not lines:
        return ""
    chunks: list[str] = []
    buffer = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if len(line) > 60 and not re.search(r"[。！？.!?，,；;、]", line):
            pieces = [line[i : i + 40] for i in range(0, len(line), 40)]
        else:
            pieces = [line]
        for piece in pieces:
            if not buffer:
                buffer = piece
                continue
            if len(buffer) + len(piece) <= 45:
                buffer += piece
            else:
                chunks.append(buffer)
                buffer = piece
            if re.search(r"[。！？.!?]", buffer):
                chunks.append(buffer)
                buffer = ""
    if buffer:
        chunks.append(buffer)

    # 去掉相邻重复（字幕常见「啊啊啊」叠行）
    deduped: list[str] = []
    for chunk in chunks:
        if deduped and deduped[-1] == chunk:
            continue
        deduped.append(chunk)
    text = "".join(deduped)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…（字幕截断）"
    return text


# ------------------------------------------------------------------ 客户端


class BiliClient:
    """B站接口客户端。整个插件共用一个实例，``httpx.AsyncClient`` 复用连接。"""

    def __init__(
        self,
        *,
        sessdata: str = "",
        bili_jct: str = "",
        user_agent: str = "",
        proxy: str = "",
        timeout_seconds: float = 20.0,
    ) -> None:
        self._sessdata = (sessdata or "").strip()
        self._bili_jct = (bili_jct or "").strip()
        self._user_agent = (user_agent or "").strip() or UA_DEFAULT
        self._proxy = (proxy or "").strip()
        self._timeout = max(5.0, float(timeout_seconds or 20.0))
        self._client: httpx.AsyncClient | None = None
        self._mixin_key = ""
        self._mixin_key_at = 0.0
        self._mixin_lock = asyncio.Lock()
        self._buvid_ready = False
        #: 登录态缓存：``None`` 表示还没验证过。
        self.login_verified: bool | None = None
        #: 验证通过时的B站昵称。
        self.login_uname = ""
        #: 凭证来源说明（配置 / 文件: 路径 / 未配置），由插件层写入。
        self.credential_source = ""

    # -- 生命周期 --------------------------------------------------

    @property
    def logged_in(self) -> bool:
        """是否「配了」登录 cookie —— 只代表有没有填，不代表它还有效。"""

        return bool(self._sessdata)

    async def __aenter__(self) -> "BiliClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        """建立连接池。重复调用安全。"""

        if self._client is not None:
            return
        cookies = {}
        if self._sessdata:
            cookies["SESSDATA"] = self._sessdata
        if self._bili_jct:
            cookies["bili_jct"] = self._bili_jct
        kwargs: dict[str, Any] = {
            "headers": {
                "User-Agent": self._user_agent,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
            },
            "timeout": httpx.Timeout(self._timeout),
            "follow_redirects": True,
        }
        if cookies:
            kwargs["cookies"] = cookies
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._client = httpx.AsyncClient(**kwargs)
        await self._ensure_buvid()

    async def close(self) -> None:
        """关闭连接池。"""

        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("BiliClient 尚未 start()")
        return self._client

    # -- 底层请求 --------------------------------------------------

    async def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        api: str = "",
        allow_codes: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """GET 一个 JSON 接口。

        Args:
            url: 接口地址。
            params: 查询参数。
            api: 报错时用的接口名。
            allow_codes: 允许通过的 ``code`` 白名单。``nav`` 这类接口在「未登录」时
                会返回 ``-101`` 但**数据仍然有效**（``wbi_img`` 就在里面），必须放行。
        """

        client = self._require_client()
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BiliError(-1, f"网络请求失败: {exc}", api=api or url) from exc
        if resp.status_code in (412, 429):
            raise BiliError(-412 if resp.status_code == 412 else -509, f"HTTP {resp.status_code}", api=api or url)
        if resp.status_code >= 400:
            raise BiliError(-resp.status_code, f"HTTP {resp.status_code}", api=api or url)
        try:
            data = resp.json()
        except ValueError as exc:
            raise BiliError(-1, "返回内容不是 JSON（可能被风控或需要登录）", api=api or url) from exc
        if not isinstance(data, dict):
            raise BiliError(-1, "返回结构异常", api=api or url)
        code = _as_int(data.get("code"), 0)
        if code != 0 and code not in allow_codes:
            raise BiliError(code, str(data.get("message") or ""), api=api or url)
        return data

    # -- 设备指纹 --------------------------------------------------

    async def _ensure_buvid(self) -> None:
        """拿到 ``buvid3`` / ``buvid4`` / ``b_nut`` 设备指纹。

        这一步是**必需的**：没有设备指纹时，B站会把搜索结果、首页推荐流一律回成
        ``-101``（看着像「未登录」，其实是缺 buvid），排行榜则回 ``-352`` 风控。

        两步都要做，缺一不可（实测）：
        1. 访问一次网页首页 —— 服务端会种下 ``b_nut`` 与 ``buvid3``；
        2. 调 ``x/frontend/finger/spi`` 补 ``buvid4``。
        只做第 2 步时搜索与推荐流仍然回 ``-101``。
        """

        if self._buvid_ready:
            return
        self._buvid_ready = True
        client = self._client
        if client is None:
            return
        try:
            await client.get("https://www.bilibili.com/")
        except httpx.HTTPError:
            pass
        try:
            data = await self._get_json(API_FINGER, api="finger")
        except BiliError:
            return
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        for cookie_name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
            value = str(payload.get(key) or "").strip()
            if not value:
                continue
            try:
                client.cookies.set(cookie_name, value, domain=".bilibili.com")
            except (TypeError, ValueError):
                client.cookies.set(cookie_name, value)

    # -- 登录态 ----------------------------------------------------

    def _absorb_nav(self, nav: dict[str, Any]) -> None:
        """从 ``nav`` 的返回里顺手记下登录态，供状态命令用。"""

        data = nav.get("data")
        if not isinstance(data, dict) or "isLogin" not in data:
            return
        self.login_verified = bool(data.get("isLogin"))
        if self.login_verified:
            self.login_uname = str(data.get("uname") or "")

    async def check_login(self) -> tuple[bool, str]:
        """实时确认 SESSDATA 还有没有效。

        ``nav`` 是唯一一个「未登录也返回 200、但 ``isLogin`` 如实反映登录态」的接口，
        所以拿它当探针最准。

        Returns:
            ``(是否已登录, 昵称或原因)``。
        """

        if not self._sessdata:
            return False, "没配 SESSDATA"
        try:
            nav = await self._get_json(API_NAV, api="nav", allow_codes=(-101,))
        except BiliError as exc:
            return False, f"验证失败: {exc}"
        self._absorb_nav(nav)
        if self.login_verified:
            return True, self.login_uname or "（没读到昵称）"
        return False, "SESSDATA 失效或已过期，需要重新复制一份"

    # -- WBI 签名 --------------------------------------------------

    async def _ensure_mixin_key(self) -> str:
        """取并缓存 mixin key（B站每天轮换，这里按 2 小时刷新）。"""

        async with self._mixin_lock:
            now = time.time()
            if self._mixin_key and now - self._mixin_key_at < 7200:
                return self._mixin_key
            # nav 在未登录时返回 -101，但 data.wbi_img 照样有效，必须放行
            nav = await self._get_json(API_NAV, api="nav", allow_codes=(-101,))
            self._absorb_nav(nav)
            wbi = ((nav.get("data") or {}).get("wbi_img") or {})
            img_url = str(wbi.get("img_url") or "")
            sub_url = str(wbi.get("sub_url") or "")

            def _key_of(url: str) -> str:
                return url.rsplit("/", 1)[-1].split(".")[0]

            raw = _key_of(img_url) + _key_of(sub_url)
            if len(raw) < 64:
                raise BiliError(-1, "拿不到 WBI 密钥（nav 返回缺少 wbi_img）", api="nav")
            self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
            self._mixin_key_at = now
            return self._mixin_key

    async def _signed_params(self, params: dict[str, Any]) -> dict[str, str]:
        mixin_key = await self._ensure_mixin_key()
        signed = {str(k): str(v) for k, v in params.items()}
        signed["wts"] = str(int(time.time()))
        signed = {k: re.sub(r"[!'()*]", "", v) for k, v in sorted(signed.items())}
        query = urllib.parse.urlencode(signed)
        signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return signed

    async def _get_signed_json(self, url: str, params: dict[str, Any], *, api: str = "") -> dict[str, Any]:
        signed = await self._signed_params(params)
        return await self._get_json(url, signed, api=api or url)

    # -- 具体接口 --------------------------------------------------

    async def view(self, *, bvid: str = "", aid: int = 0) -> dict[str, Any]:
        """视频详情（``x/web-interface/view``，免签）。"""

        params: dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
        data = await self._get_json(API_VIEW, params, api="view")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise BiliError(-404, "视频详情为空", api="view")
        return payload

    async def subtitle(self, *, bvid: str, aid: int, cid: int) -> tuple[list[str], str, str]:
        """取字幕。

        Returns:
            ``(行列表, 语言, 失败原因)``。未登录 / 无字幕时行列表为空，原因非空。
        """

        if not self.logged_in:
            return [], "", "没有配置 SESSDATA，B站未登录时不返回字幕"
        try:
            data = await self._get_signed_json(
                API_PLAYER_WBI, {"bvid": bvid, "aid": aid, "cid": cid}, api="player.wbi"
            )
        except BiliError as exc:
            return [], "", f"字幕接口失败：{exc}"
        subtitle = ((data.get("data") or {}).get("subtitle") or {})
        entries = subtitle.get("subtitles") or []
        if not isinstance(entries, list) or not entries:
            return [], "", "这个视频没有字幕（UP 未上传，或需要大会员）"
        # 优先中文，其次第一个
        chosen = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("lan") or "").startswith("zh"):
                chosen = entry
                break
        if chosen is None:
            chosen = next((e for e in entries if isinstance(e, dict)), None)
        if not chosen:
            return [], "", "字幕列表结构异常"
        url = str(chosen.get("subtitle_url") or "").strip()
        if not url:
            return [], "", "字幕地址为空"
        if url.startswith("//"):
            url = "https:" + url
        client = self._require_client()
        try:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return [], "", f"字幕下载失败：HTTP {resp.status_code}"
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return [], "", f"字幕下载失败：{exc}"
        items = (body or {}).get("body") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return [], "", "字幕内容为空"
        lines = [str(item.get("content") or "").strip() for item in items if isinstance(item, dict)]
        lines = [line for line in lines if line]
        if not lines:
            return [], "", "字幕内容为空"
        return lines, str(chosen.get("lan_doc") or chosen.get("lan") or "字幕"), ""

    async def comments(self, *, aid: int, limit: int = 12) -> list[Comment]:
        """热门评论（``sort=2`` 按热度）。"""

        if limit <= 0:
            return []
        params = {"type": 1, "oid": aid, "sort": 2, "ps": min(20, max(1, limit)), "pn": 1}
        data = await self._get_json(API_REPLY, params, api="reply")
        payload = data.get("data") or {}
        result: list[Comment] = []
        seen: set[str] = set()
        for bucket in (payload.get("top_replies"), payload.get("replies")):
            if not isinstance(bucket, list):
                continue
            for item in bucket:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") or {}
                message = str(content.get("message") or "").strip()
                if not message:
                    continue
                message = re.sub(r"\s+", " ", message)
                if message in seen:
                    continue
                seen.add(message)
                member = item.get("member") or {}
                result.append(
                    Comment(
                        user=str(member.get("uname") or "匿名"),
                        like=_as_int(item.get("like")),
                        message=message,
                    )
                )
                if len(result) >= limit:
                    return result
        return result

    async def danmaku(self, *, cid: int, limit: int = 60) -> tuple[list[str], int]:
        """弹幕抽样：按出现次数排序（重复越多越说明是视频里的梗）。

        Returns:
            ``(抽样列表, 弹幕总数)``。
        """

        if limit <= 0:
            return [], 0
        client = self._require_client()
        try:
            resp = await client.get(API_DANMAKU, params={"oid": cid})
        except httpx.HTTPError:
            return [], 0
        if resp.status_code >= 400:
            return [], 0
        text = resp.content.decode("utf-8", "ignore")
        if "<d p=" not in text:
            # httpx 通常已解压；极少数情况返回原始 deflate，兜底再解一次
            try:
                text = zlib.decompressobj(-zlib.MAX_WBITS).decompress(resp.content).decode("utf-8", "ignore")
            except zlib.error:
                return [], 0
        raw_items = re.findall(r"<d p=\"[^\"]*\">(.*?)</d>", text, flags=re.DOTALL)
        if not raw_items:
            return [], 0
        counter: dict[str, int] = {}
        for item in raw_items:
            content = re.sub(r"\s+", " ", item).strip()
            if not content or len(content) > 40:
                continue
            if not re.search(r"[\w\u4e00-\u9fff]", content):
                continue
            counter[content] = counter.get(content, 0) + 1
        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], len(kv[0])))
        samples = [f"{content}（×{count}）" if count > 1 else content for content, count in ranked[:limit]]
        return samples, len(raw_items)

    async def popular(self, *, limit: int = 14) -> list[Candidate]:
        """综合热门（免签、免 buvid 也基本可用，是最稳的免登录来源）。"""

        data = await self._get_json(API_POPULAR, {"ps": min(20, max(1, limit)), "pn": 1}, api="popular")
        items = ((data.get("data") or {}).get("list") or [])
        return [cand for cand in (self._candidate_from(it, extra_desc=True) for it in items) if cand][:limit]

    async def ranking(self, *, limit: int = 12) -> list[Candidate]:
        """全站排行榜（免签）。"""

        data = await self._get_json(API_RANKING, {"rid": 0, "type": "all"}, api="ranking")
        items = ((data.get("data") or {}).get("list") or [])
        return [cand for cand in (self._candidate_from(it, extra_desc=True) for it in items) if cand][:limit]

    async def recommend(self, *, limit: int = 12) -> list[Candidate]:
        """首页推荐流（需签名）。"""

        params = {"fresh_type": 3, "ps": min(20, max(1, limit)), "fresh_idx": 1, "fresh_idx_1h": 1, "feed_version": "V8"}
        data = await self._get_signed_json(API_RCMD, params, api="rcmd")
        items = ((data.get("data") or {}).get("item") or [])
        return [cand for cand in (self._candidate_from(it, extra_desc=True) for it in items) if cand][:limit]

    async def search(self, *, keyword: str, limit: int = 12) -> list[Candidate]:
        """按关键词搜视频（需签名）。"""

        params = {"search_type": "video", "keyword": keyword, "page": 1, "page_size": min(20, max(1, limit))}
        data = await self._get_signed_json(API_SEARCH, params, api="search")
        items = ((data.get("data") or {}).get("result") or [])
        return [cand for cand in (self._candidate_from(it) for it in items) if cand][:limit]

    async def related(self, *, bvid: str, limit: int = 6) -> list[Candidate]:
        """相关推荐（免签）。"""

        data = await self._get_json(API_RELATED, {"bvid": bvid}, api="related")
        items = data.get("data")
        if not isinstance(items, list):
            return []
        return [cand for cand in (self._candidate_from(it) for it in items) if cand][:limit]

    @staticmethod
    def _candidate_from(item: Any, *, extra_desc: bool = False) -> Candidate | None:
        if not isinstance(item, dict):
            return None
        bvid = str(item.get("bvid") or "").strip()
        if not bvid.startswith("BV"):
            return None
        owner = item.get("owner")
        owner_name = ""
        if isinstance(owner, dict):
            owner_name = str(owner.get("name") or "")
        duration = item.get("duration")
        seconds = _as_int(duration) if not isinstance(duration, str) else _duration_from_text(duration)
        play = item.get("play")
        if play is None:
            stat = item.get("stat")
            play = (stat or {}).get("view") if isinstance(stat, dict) else 0
        return Candidate(
            bvid=bvid,
            title=_clean_html(item.get("title")),
            owner=owner_name or str(item.get("author") or ""),
            duration_seconds=max(0, seconds),
            play=max(0, _as_int(play)),
            desc=_clean_html(item.get("desc")) if extra_desc else "",
        )

    # -- 组合抓取 --------------------------------------------------

    async def fetch_video(
        self,
        *,
        bvid: str = "",
        aid: int = 0,
        page: int = 1,
        want_subtitle: bool = True,
        subtitle_max_chars: int = 3000,
        comment_limit: int = 12,
        danmaku_limit: int = 60,
        related_limit: int = 6,
    ) -> VideoPacket:
        """抓一个视频的全部素材。单个可选项失败不影响整体。"""

        payload = await self.view(bvid=bvid, aid=aid)
        real_bvid = str(payload.get("bvid") or bvid)
        real_aid = _as_int(payload.get("aid"), aid)
        pages = payload.get("pages") if isinstance(payload.get("pages"), list) else []
        page_index = min(max(1, int(page or 1)), max(1, len(pages) or 1))
        if pages:
            current = pages[page_index - 1] if isinstance(pages[page_index - 1], dict) else {}
            cid = _as_int(current.get("cid"), _as_int(payload.get("cid")))
            part_title = str(current.get("part") or "")
        else:
            cid = _as_int(payload.get("cid"))
            part_title = ""
        if not cid:
            raise BiliError(-404, "拿不到 cid，无法取字幕与弹幕", api="view")

        owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
        stat_raw = payload.get("stat") if isinstance(payload.get("stat"), dict) else {}
        stat = {
            key: _as_int(stat_raw.get(key))
            for key in ("view", "danmaku", "reply", "favorite", "coin", "share", "like")
        }

        title = _clean_html(payload.get("title"))
        if part_title and page_index > 1:
            title = f"{title}（P{page_index} {part_title}）"

        packet = VideoPacket(
            bvid=real_bvid,
            aid=real_aid,
            cid=cid,
            title=title or real_bvid,
            desc=str(payload.get("desc") or "").strip(),
            owner=str(owner.get("name") or ""),
            owner_mid=_as_int(owner.get("mid")),
            duration_seconds=_as_int(payload.get("duration")),
            pubdate=_as_int(payload.get("pubdate")),
            tname=str(payload.get("tname") or ""),
            page=page_index,
            page_count=max(1, len(pages) or 1),
            stat=stat,
        )

        tasks: list[tuple[str, Any]] = []
        if want_subtitle:
            tasks.append(("subtitle", self.subtitle(bvid=real_bvid, aid=real_aid, cid=cid)))
        if comment_limit > 0:
            tasks.append(("comments", self.comments(aid=real_aid, limit=comment_limit)))
        if danmaku_limit > 0:
            tasks.append(("danmaku", self.danmaku(cid=cid, limit=danmaku_limit)))
        if related_limit > 0:
            tasks.append(("related", self.related(bvid=real_bvid, limit=related_limit)))

        if not tasks:
            return packet

        outcomes = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        for (name, _), outcome in zip(tasks, outcomes):
            if isinstance(outcome, BaseException):
                if name == "subtitle":
                    packet.subtitle_reason = f"字幕获取失败：{outcome}"
                continue
            if name == "subtitle":
                lines, lang, reason = outcome
                packet.subtitle_lines = lines
                packet.subtitle_lang = lang
                packet.subtitle_reason = reason
                if lines and subtitle_max_chars > 0:
                    joined = _join_subtitle_lines(lines, max_chars=subtitle_max_chars)
                    packet.subtitle_lines = [joined]
            elif name == "comments":
                packet.comments = list(outcome)
            elif name == "danmaku":
                samples, total = outcome
                packet.danmaku_samples = samples
                packet.danmaku_total = total
            elif name == "related":
                packet.related = list(outcome)
        return packet

    async def resolve_short_link(self, url: str) -> str:
        """跟随 ``b23.tv`` 短链，拿到最终地址。"""

        client = self._require_client()
        try:
            resp = await client.get(url)
            return str(resp.url)
        except httpx.HTTPError as exc:
            raise BiliError(-1, f"短链解析失败: {exc}", api="b23") from exc
