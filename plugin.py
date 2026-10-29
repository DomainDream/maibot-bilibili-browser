# -*- coding: utf-8 -*-
"""刷B站插件主入口。

两条能力，对应两种完全不同的驱动力：

1. ``@Tool("bilibili_watch")`` —— **主人点名要看**。消息里出现 BV 号、av 号或
   B站链接时，planner 调这个工具。走的是正常的模型调用链，看完的内容天然留在
   上下文里，她之后接得上话。
2. ``@Tool("bilibili_browse")`` —— **自己去刷**。这条工具常驻注册且默认启用，所以
   当别的插件（比如定时作息插件）触发一轮主动思考时，planner 的工具清单里就摆着
   「自己去B站刷一个」这个选项，模型可以自己挑它。

外加一个诊断命令 ``/bili_status``。

早先还有个 ``/bili`` 命令，已经删掉了：命令命中后宿主会拦下整条消息
（``continue_process=False``），既不进 LLM 上下文，插件自己发出去的话也不进，
结果是她刷完转头就忘。想让她看某个视频，直接发 BV 号走 Tool 那条路。

业务逻辑都在 ``bili/`` 子模块里，本文件只做装配与派发。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .bili.client import BiliClient, BiliError, VideoPacket
from .bili.credential import ensure_credential_file, resolve_credential
from .bili.digest import human_duration, render_browse, render_watch
from .bili.store import BrowseStore
from .bili.target import parse_target
from .config import BilibiliBrowserConfig

#: 抓一个视频（详情 + 字幕 + 评论 + 弹幕 + 相关推荐）的 RPC 上限。
#: timeout_ms 必须写在组件声明顶层 metadata 里，Host 只读那里。
WATCH_TIMEOUT_MS = 120_000
#: 自己刷还要先拉推荐流/搜索，比单纯看一个视频多一轮，给宽一点。
BROWSE_TIMEOUT_MS = 150_000

_SOURCE_LABELS = {
    "recommend": "首页推荐",
    "popular": "综合热门",
    "ranking": "全站排行榜",
    "mix": "热门 + 排行榜 + 推荐",
    "search": "关键词搜索",
}

#: 各来源的回落顺序。``ranking/v2`` 对无登录态常年回 ``-352`` 风控，所以排在最后。
_SOURCE_CHAIN = {
    "recommend": ("recommend", "popular", "ranking"),
    "popular": ("popular", "recommend", "ranking"),
    "ranking": ("ranking", "popular", "recommend"),
    "mix": ("popular", "recommend", "ranking"),
}


class BilibiliBrowserPlugin(MaiBotPlugin):
    """刷B站插件。"""

    config_model = BilibiliBrowserConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: BiliClient | None = None
        self._store: BrowseStore | None = None
        #: 登录凭证文件的实际路径，初始化后才知道
        self._cred_path: Path | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def on_load(self) -> None:
        """建立 HTTP 连接池与浏览记录。"""

        await self._ensure_runtime()
        self.ctx.logger.info(
            "刷B站插件已加载：登录状态=%s，默认来源=%s",
            "已配 SESSDATA" if self._client and self._client.logged_in else "未登录（读不到字幕）",
            getattr(self.config.browse, "source", "?"),
        )

    async def on_unload(self) -> None:
        """释放连接池。"""

        if self._client is not None:
            await self._client.close()
            self._client = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：重建客户端（cookie / 代理 / UA 都可能变了）。"""

        del config_data
        self.ctx.logger.info("刷B站配置更新：scope=%s version=%s", scope, version)
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self._ensure_runtime()

    def get_components(self) -> list[dict[str, Any]]:
        """注入 RPC 超时，并按配置决定「自己刷」是否暴露给 planner。"""

        components = super().get_components()
        browse_enabled = True
        try:
            browse_enabled = bool(self.config.browse.enabled)
        except Exception:  # noqa: BLE001  # 配置尚未注入时按默认值放行
            pass
        for component in components:
            metadata = component.get("metadata")
            if not isinstance(metadata, dict):
                continue
            name = str(component.get("name") or "")
            if name == "bilibili_watch":
                metadata["timeout_ms"] = WATCH_TIMEOUT_MS
            elif name == "bilibili_browse":
                metadata["timeout_ms"] = BROWSE_TIMEOUT_MS
                metadata["enabled"] = browse_enabled
        return components

    # ------------------------------------------------------------------ #
    # 运行时装配
    # ------------------------------------------------------------------ #

    async def _ensure_runtime(self) -> None:
        """懒初始化：工具被调用时若 on_load 还没跑过也能工作。"""

        cfg = self.config
        data_dir = self._data_dir()
        if self._client is None:
            credential, cred_path = resolve_credential(
                config_sessdata=cfg.auth.sessdata,
                config_bili_jct=cfg.auth.bili_jct,
                config_user_agent=cfg.auth.user_agent,
                data_dir=data_dir,
                configured_path=cfg.auth.credential_file,
            )
            # 第一次跑就把凭证模板落下去，主人打开就能照着填
            if ensure_credential_file(cred_path):
                self.ctx.logger.info("已生成B站登录凭证模板：%s", cred_path.name)
            self._cred_path = cred_path
            self._client = BiliClient(
                sessdata=credential.sessdata,
                bili_jct=credential.bili_jct,
                user_agent=credential.user_agent,
                proxy=cfg.network.proxy,
                timeout_seconds=cfg.network.timeout_seconds,
            )
            self._client.credential_source = credential.source
            await self._client.start()
        if self._store is None:
            self._store = BrowseStore(data_dir / "history.json")

    def _data_dir(self) -> Path:
        """插件自己的数据目录，凭证与浏览记录都放这儿。"""

        data_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", None)
        if data_dir is None:
            data_dir = Path("data") / "plugins" / "bilibili-browser"
        return Path(data_dir)

    async def _client_ready(self) -> BiliClient:
        await self._ensure_runtime()
        if self._client is None:  # pragma: no cover - 理论上不会走到
            raise BiliError(-1, "B站客户端初始化失败")
        return self._client

    async def _store_ready(self) -> BrowseStore:
        await self._ensure_runtime()
        if self._store is None:  # pragma: no cover
            raise BiliError(-1, "浏览记录初始化失败")
        return self._store

    # ------------------------------------------------------------------ #
    # 内部动作
    # ------------------------------------------------------------------ #

    async def _open_video(self, raw_target: str) -> tuple[VideoPacket | None, str]:
        """按一句话/一串编码找到并读完一个视频。

        Returns:
            ``(视频素材, 失败原因)``。成功时失败原因为空串。
        """

        client = await self._client_ready()
        cfg = self.config
        target = parse_target(raw_target)
        if target is None:
            return None, "没看懂要看哪个视频"

        bvid = target.bvid
        aid = target.aid
        if target.needs_short_link_resolve:
            try:
                final_url = await client.resolve_short_link(target.short_link)
            except BiliError as exc:
                return None, str(exc)
            resolved = parse_target(final_url)
            if resolved is None or resolved.kind != "video" or not (resolved.bvid or resolved.aid):
                return None, "这条短链没解析出视频"
            bvid, aid, page = resolved.bvid, resolved.aid, resolved.page
        else:
            page = target.page

        if target.kind == "search":
            try:
                hits = await client.search(keyword=target.keyword, limit=3)
            except BiliError as exc:
                return None, str(exc)
            if not hits:
                return None, f"搜「{target.keyword}」什么都没搜到"
            bvid, aid = hits[0].bvid, 0
            page = 1

        try:
            packet = await client.fetch_video(
                bvid=bvid,
                aid=aid,
                page=page,
                want_subtitle=bool(cfg.watch.subtitle),
                subtitle_max_chars=int(cfg.watch.subtitle_max_chars),
                comment_limit=int(cfg.watch.comment_limit),
                danmaku_limit=int(cfg.watch.danmaku_limit) if cfg.watch.danmaku else 0,
                related_limit=int(cfg.watch.related_limit),
            )
        except BiliError as exc:
            return None, str(exc)

        store = await self._store_ready()
        await store.mark_seen(packet.bvid, packet.title)
        return packet, ""

    async def _pick_candidates(self, keyword: str = "") -> tuple[list[Any], str]:
        """准备一批候选视频。返回 ``(候选列表, 来源说明)``。"""

        client = await self._client_ready()
        cfg = self.config
        browse_cfg = cfg.browse

        if keyword.strip():
            hits = await client.search(keyword=keyword.strip(), limit=12)
            return hits, f"搜「{keyword.strip()}」"

        use_keyword = bool(browse_cfg.keywords) and random.random() < max(0.0, min(1.0, float(browse_cfg.keyword_chance)))
        if use_keyword:
            picked_keyword = random.choice(list(browse_cfg.keywords))
            try:
                hits = await client.search(keyword=picked_keyword, limit=12)
                if hits:
                    return hits, f"搜「{picked_keyword}」"
            except BiliError:
                pass  # 搜索挂了就退回推荐流

        source = str(browse_cfg.source or "recommend").strip().lower()
        if source == "mix":
            merged: list[Any] = []
            for name in ("popular", "recommend", "ranking"):
                try:
                    merged.extend(await self._fetch_source(client, name, limit=8))
                except BiliError:
                    continue
            if merged:
                random.shuffle(merged)
                return merged, _SOURCE_LABELS["mix"]
            source = "popular"

        chain = _SOURCE_CHAIN.get(source, _SOURCE_CHAIN["recommend"])
        last_error: BiliError | None = None
        for name in chain:
            try:
                hits = await self._fetch_source(client, name, limit=14)
            except BiliError as exc:
                last_error = exc
                self.ctx.logger.info("来源 %s 不可用（%s），试下一个", name, exc)
                continue
            if hits:
                return hits, _SOURCE_LABELS.get(name, name)

        if last_error is not None:
            raise last_error
        return [], "所有来源都空着"

    @staticmethod
    async def _fetch_source(client: BiliClient, name: str, *, limit: int) -> list[Any]:
        """按名字取一个来源的候选列表。"""

        if name == "popular":
            return await client.popular(limit=limit)
        if name == "ranking":
            return await client.ranking(limit=limit)
        return await client.recommend(limit=limit)

    @staticmethod
    def _duration_ok(packet_duration: int, min_seconds: int, max_seconds: int) -> bool:
        if min_seconds > 0 and 0 < packet_duration < min_seconds:
            return False
        if max_seconds > 0 and packet_duration > max_seconds:
            return False
        return True

    async def _browse_once(self, keyword: str = "") -> tuple[str, str]:
        """自己去刷一次。

        Returns:
            ``(给模型看的素材文本, 失败原因)``。
        """

        client = await self._client_ready()
        store = await self._store_ready()
        cfg = self.config
        browse_cfg = cfg.browse

        ok, reason = await store.check_budget(
            cooldown_minutes=int(browse_cfg.cooldown_minutes),
            daily_limit=int(browse_cfg.daily_limit),
        )
        if not ok:
            return "", reason

        try:
            candidates, source_label = await self._pick_candidates(keyword)
        except BiliError as exc:
            return "", str(exc)
        if not candidates:
            return "", "推荐流和搜索都没给出东西，这次先不刷了"

        candidates = await store.filter_fresh(candidates, days=int(browse_cfg.avoid_repeat_days))
        candidates = [
            item
            for item in candidates
            if self._duration_ok(
                int(item.duration_seconds),
                int(browse_cfg.min_duration_seconds),
                int(browse_cfg.max_duration_seconds),
            )
        ]
        if not candidates:
            return "", "筛完时长和看过的之后没剩下能看的，这次先不刷了"

        # 播放量高的稍作加权，但不做硬性排名，保留「随机刷到」的偶然感
        candidates.sort(key=lambda item: -int(item.play or 0))
        pool = candidates[:6]
        chosen = random.choice(pool) if pool else candidates[0]

        try:
            packet = await client.fetch_video(
                bvid=chosen.bvid,
                want_subtitle=bool(cfg.watch.subtitle),
                subtitle_max_chars=int(cfg.watch.subtitle_max_chars),
                comment_limit=int(browse_cfg.comment_limit),
                danmaku_limit=int(browse_cfg.danmaku_limit) if cfg.watch.danmaku else 0,
                related_limit=0,
            )
        except BiliError as exc:
            return "", str(exc)

        await store.note_browse(bvid=packet.bvid, title=packet.title)
        text = render_browse(
            candidates,
            packet,
            source_label=source_label,
            desc_max_chars=int(cfg.watch.desc_max_chars),
            danmaku_limit=int(browse_cfg.danmaku_limit),
        )
        return text, ""

    # ------------------------------------------------------------------ #
    # Tool：主人点名要看
    # ------------------------------------------------------------------ #

    @Tool(
        "bilibili_watch",
        description=(
            "打开一个B站视频，读出它的字幕、简介、弹幕和热门评论，用来陪主人一起看视频、聊视频内容。"
            "只要主人发来 BV 号、av 号、B站视频链接（包括 b23.tv 短链），或者明确让你去看某个视频，"
            "就必须调用这个工具；主人只给了标题或关键词时也可以传关键词，它会先搜再打开最匹配的那条。"
            "它返回的是视频素材原文：你要用自己的语气讲自己在看什么、看到了什么、弹幕和评论区在聊什么，"
            "不要逐条复述字段，也不要提「素材」这个词。"
        ),
        parameters=[
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="BV号、av号、B站视频链接，或视频标题关键词",
                required=True,
            )
        ],
    )
    async def handle_bilibili_watch(self, target: str = "", **kwargs: Any) -> dict[str, Any]:
        """看一个指定的B站视频。"""

        del kwargs
        raw = str(target or "").strip()
        if not raw:
            return {"name": "bilibili_watch", "content": "（主人没说要我看哪个视频，问一句是哪个 BV 号或链接。）"}

        packet, reason = await self._open_video(raw)
        if packet is None:
            return {
                "name": "bilibili_watch",
                "content": f"（这次没能看成：{reason}。可以如实告诉主人没看成，别编视频内容。）",
            }

        material = render_watch(
            packet,
            desc_max_chars=int(self.config.watch.desc_max_chars),
            danmaku_limit=int(self.config.watch.danmaku_limit),
            related_limit=int(self.config.watch.related_limit),
        )
        self.ctx.logger.info("已看完视频 %s《%s》", packet.bvid, packet.title)
        return {"name": "bilibili_watch", "content": material}

    # ------------------------------------------------------------------ #
    # Tool：自己去刷
    # ------------------------------------------------------------------ #

    @Tool(
        "bilibili_browse",
        description=(
            "自己去B站刷一个视频：从首页推荐、全站排行榜或按自己的兴趣搜索里挑一条，"
            "读出它的字幕、简介、弹幕和热门评论。"
            "当主人没点名看什么、你自己闲着、想找点东西看看，或者聊天气氛需要你说点刚刷到的东西时用它。"
            "有冷却时间和每日次数限制：被限制会直接告诉你原因，那就老实待着，不要反复调用。"
            "它返回视频素材原文：你要用自己的语气讲刚刷到了什么、有什么想说的。"
        ),
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="可选：想刷的方向或关键词（比如「机械键盘」「老番」）。不填就按配置自己挑",
                required=False,
            )
        ],
    )
    async def handle_bilibili_browse(self, keyword: str = "", **kwargs: Any) -> dict[str, Any]:
        """自己刷一个B站视频。"""

        del kwargs
        if not self.config.browse.enabled:
            return {"name": "bilibili_browse", "content": "（自己刷B站的功能被关掉了。）"}
        material, reason = await self._browse_once(keyword=str(keyword or ""))
        if not material:
            return {"name": "bilibili_browse", "content": f"（这次没刷：{reason}。不用勉强，也别换个花样再调一次。）"}
        self.ctx.logger.info("自己刷了一次B站")
        return {"name": "bilibili_browse", "content": material}

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #

    @Command("bili_status", description="查看刷B站插件的状态", pattern=r"^/bili_status$")
    async def handle_bili_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        """诊断用：登录状态、今日次数、冷却、最近看过什么。"""

        group_id = str(kwargs.get("group_id") or "").strip()
        del kwargs  # stream_id 后面要用来发送，别删
        if group_id:
            # 群聊里不响应。插件配置是实例级唯一的（不存在「每人一份」），诊断输出里
            # 又有凭证文件名和B站昵称，没必要让整个群的成员看到。
            self.ctx.logger.info("/bili_status 在群聊 %s 里被忽略", group_id)
            return True, "群聊里不响应该诊断命令", True
        client = await self._client_ready()
        store = await self._store_ready()
        cfg = self.config
        snap = await store.snapshot()

        lines = [
            "【刷B站插件状态】",
            f"凭证来源：{client.credential_source or '未配置'}",
            f"凭证文件：{self._cred_path.name if self._cred_path else '未初始化'}（在插件数据目录下）",
        ]
        if client.logged_in:
            is_login, detail = await client.check_login()
            if is_login:
                lines.append(f"B站登录：有效（{detail}）")
            else:
                lines.append(f"B站登录：{detail} —— 字幕暂时读不到，刷视频不受影响")
        else:
            lines.append("B站登录：未配置（字幕读不到；把 SESSDATA 填进上面那个凭证文件即可）")
        lines.extend(
            [
                f"自己刷：{'开启' if cfg.browse.enabled else '关闭'}"
                f"（来源 {_SOURCE_LABELS.get(str(cfg.browse.source), cfg.browse.source)}，"
                f"冷却 {int(cfg.browse.cooldown_minutes)} 分钟，每日上限 {int(cfg.browse.daily_limit)} 次）",
                f"今天自己刷了：{snap['today_count']} 次",
                f"累计看过：{snap['total_seen']} 个视频",
            ]
        )
        last_ago = float(snap["last_browse_ago"] or 0)
        if last_ago > 0:
            when = "刚刚" if last_ago < 60 else f"{human_duration(int(last_ago))} 前"
            lines.append(f"上次自己刷：{when}")
        if snap["recent"]:
            lines.append("最近看过：")
            for item in snap["recent"][:5]:
                title = item["title"] or item["bvid"]
                lines.append(f"· {title}（{item['bvid']}）")
        else:
            lines.append("最近还没看过视频")

        body = "\n".join(lines)
        # 命令的返回值只会被写进日志、不会自动发给主人（宿主只 log 不 send），必须自己发
        if stream_id:
            await self.ctx.send.text(body, stream_id)
            return True, "已输出插件状态", True
        return True, body, True


def create_plugin() -> BilibiliBrowserPlugin:
    """插件工厂，宿主按此创建实例。"""

    return BilibiliBrowserPlugin()
