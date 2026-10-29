# -*- coding: utf-8 -*-
"""刷B站插件的配置模型。

字段全部给出默认值 —— 宿主会据此生成默认 ``config.toml`` 与 WebUI 表单。
"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class AuthConfig(PluginConfigBase):
    """B站登录状态。部分视频的字幕需要登录才能读。"""

    __ui_label__ = "B站登录"
    __ui_icon__ = "key"
    __ui_order__ = 1

    sessdata: str = Field(
        default="",
        description=(
            "这里填 B站 Cookie 里 SESSDATA 的值本身（一长串，以数字字母开头，中间带 %2C），"
            "不要带「SESSDATA=」前缀，也不要带后面的分号。留空则去凭证文件里读，两边都没配也能刷视频，"
            "只是读不到字幕。获取步骤见插件 README 的「登录 token」一节"
        ),
        json_schema_extra={"ui_widget": "password"},
    )
    bili_jct: str = Field(default="", description="B站 bili_jct，本插件只读不写，用不到，留空即可")
    user_agent: str = Field(default="", description="自定义 User-Agent，留空用内置的桌面 Chrome UA")
    credential_file: str = Field(
        default="",
        description=(
            "【这里填路径，不是填 token】想给凭证换个文件名时才填，必须是插件数据目录内的"
            "相对路径（绝对路径、含 .. 的路径会被忽略并回退默认）。留空即用默认位置："
            "<MaiBot>/data/plugins/maibot.bilibili-browser/bili_credential.json，"
            "插件首次启动会自动生成带填写说明的模板"
        ),
    )


class WatchConfig(PluginConfigBase):
    """看视频时要抓哪些内容。"""

    __ui_label__ = "看视频"
    __ui_icon__ = "video"
    __ui_order__ = 2

    subtitle: bool = Field(default=True, description="是否读字幕（未登录时 B站不返回字幕，会自动降级）")
    subtitle_max_chars: int = Field(default=3000, description="字幕最多取多少字")
    desc_max_chars: int = Field(default=500, description="简介最多取多少字")
    comment_limit: int = Field(default=12, description="热门评论取几条，0 表示不取")
    danmaku: bool = Field(default=True, description="是否抽样弹幕（弹幕能反映视频里的梗和重点）")
    danmaku_limit: int = Field(default=40, description="弹幕抽样最多几条")
    related_limit: int = Field(default=4, description="相关推荐取几条，0 表示不取")


class BrowseConfig(PluginConfigBase):
    """自己刷B站：从哪挑、多久能刷一次。"""

    __ui_label__ = "自己刷"
    __ui_icon__ = "compass"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否给 LLM 提供「自己去刷B站」这个选项")
    source: str = Field(
        default="recommend",
        description=(
            "默认从哪挑：recommend=首页推荐流（个人化，最像「自己刷」）／"
            "popular=综合热门／ranking=全站排行榜（无登录态常被风控，不推荐）／"
            "mix=三者混合。取不到时会自动按 popular→recommend→ranking 的顺序回落"
        ),
    )
    keywords: list[str] = Field(
        default=["科技", "数码", "音乐", "游戏", "科普", "美食", "电影解说"],
        description="没有指定方向时，偶尔会按这些关键词自己搜一个看",
    )
    keyword_chance: float = Field(default=0.3, description="按关键词搜索的概率（0~1），其余走首页推荐/排行")
    cooldown_minutes: int = Field(default=30, description="两次「自己刷」之间至少隔多少分钟，0 表示不限制")
    daily_limit: int = Field(default=8, description="每天最多自己刷几次，0 表示不限制")
    avoid_repeat_days: int = Field(default=7, description="多少天内看过的视频不再重复刷，0 表示不查重")
    min_duration_seconds: int = Field(default=60, description="太短的视频跳过（秒）")
    max_duration_seconds: int = Field(default=3600, description="太长的视频跳过（秒），0 表示不限")
    comment_limit: int = Field(default=6, description="自己刷时评论取几条")
    danmaku_limit: int = Field(default=30, description="自己刷时弹幕抽样几条")


class NetworkConfig(PluginConfigBase):
    """网络。"""

    __ui_label__ = "网络"
    __ui_icon__ = "wifi"
    __ui_order__ = 5

    proxy: str = Field(default="", description="HTTP 代理，例如 http://127.0.0.1:7890；留空直连")
    timeout_seconds: int = Field(default=20, description="单次请求超时（秒）")


class BilibiliBrowserConfig(PluginConfigBase):
    """刷B站插件总配置。"""

    __ui_label__ = "刷B站"

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    browse: BrowseConfig = Field(default_factory=BrowseConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
