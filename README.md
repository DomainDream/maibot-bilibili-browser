# 刷B站（bilibili-browser）

让麦麦自己去刷 B 站：你丢一个 BV 号或链接，它就跑去看视频、读字幕、翻热门评论和弹幕；
它自己闲下来的时候，也能从首页推荐、综合热门或按兴趣搜索里挑一个看。

作者：梦域 & 玄鲸 ｜ 许可证：MIT

## 安装

**从插件市场装（推荐）**——WebUI 里打开插件市场，搜「刷B站」，点安装，然后重载插件。

**手动装**——把整个目录放到 `<MaiBot>/plugins/bilibili-browser/`，重启 MaiBot。
依赖只有 `httpx`，宿主会自动装好。

装完随便发一句 `/bili_status`，能收到回复就说明通了。

## 两条能力

**一、你点名要看**

- 消息里出现 BV 号、av 号、`bilibili.com/video/...` 链接、`b23.tv` 短链，或者你说「去看这个视频」，
  麦麦会调用工具 `bilibili_watch` 把内容读完，再用自己的语气跟你聊。
- 只给标题或关键词也行，它会先搜再打开最匹配的那条。
- 走的是正常的模型调用链，所以看完的内容留在上下文里，你隔几轮再问「刚看的那个视频讲什么的」，
  她接得上。

**二、它自己去刷**

- 工具 `bilibili_browse` 常驻注册且默认启用。别的插件（比如定时作息那类）触发一轮主动思考时，
  麦麦的工具清单里就摆着「自己去B站刷一个」这个选项 —— 它可以自己挑它。
- 来源：首页推荐流 / 综合热门 / 全站排行榜 / 按关键词搜索，取不到会自动按
  `popular → recommend → ranking` 回落。
- 有冷却时间（默认 30 分钟）和每日上限（默认 8 次），7 天内看过的视频不会重复刷。

诊断用 `/bili_status` 看登录状态、今天刷了几次、最近看过什么。

## 装在哪

放到 MaiBot 的 `plugins/` 下面，目录名保持 `bilibili-browser`：

```
<MaiBot>/plugins/bilibili-browser/
├── _manifest.json
├── config.toml
├── config.py
├── plugin.py
├── README.md
└── bili/
    ├── __init__.py
    ├── client.py      # B站接口 + WBI 签名 + 设备指纹
    ├── credential.py  # 登录 token 的存放与读取
    ├── target.py      # 从一句话里认出 BV/av/链接/关键词
    ├── digest.py      # 把抓到的素材拼成给模型读的文本
    └── store.py       # 浏览记录、冷却、每日配额
```

运行时还会在插件自己的数据目录里放两个文件：

```
<MaiBot>/data/plugins/maibot.bilibili-browser/
├── bili_credential.json   # 登录 token，首次启动自动生成模板
└── history.json           # 看过的视频、冷却、每日次数
```

OneKey 版的真实插件目录是
`%APPDATA%\MaiBotOneKeyDesktop\<实例哈希>\modules\MaiBot\plugins\`，
不是 `E:\MaiBot\MaiBot OneKey`（那只是 Electron 外壳）。

装完重启 MaiBot，或者在 WebUI 的插件页重新加载。

## 配置

改 `config.toml` 或在 WebUI 插件设置里改，六个小节：

`[plugin]` —— `enabled` 总开关，`config_version` 别动。

`[auth]` —— **只影响字幕**。B站的字幕接口要求登录，不填也能刷视频、看评论、看弹幕。

- `credential_file` 想给凭证换个文件名时才需要填，只接受插件数据目录内的相对路径，
  留空就用默认的 `data/plugins/maibot.bilibili-browser/bili_credential.json`。
  绝对路径和带 `..` 的路径会被忽略、自动回退到默认位置。
- `sessdata` 想直接写在配置里就填这儿，优先级高于凭证文件。
- `bili_jct` 可选，一般不填也能用。

怎么拿 token、填在哪、失效了怎么办，见下面单独一节。

`[watch]` —— 看视频抓什么：字幕开关与字数上限、简介字数、评论条数、弹幕开关与抽样条数、相关推荐条数。

`[browse]` —— 自己刷的节奏：
`source`（recommend / popular / ranking / mix）、`keywords` 兴趣池、`keyword_chance` 按关键词搜的概率、
`cooldown_minutes`、`daily_limit`、`avoid_repeat_days`、时长上下限、评论与弹幕条数。

`[network]` —— `proxy` 和超时。

## 登录 token

### 存在哪

插件把登录凭证单独放一个文件，**不混在 `config.toml` 里**——那份配置会被 WebUI 读写、
可能被导出分享、也可能进版本库，Cookie 混进去很容易泄漏。

默认位置：

```
<MaiBot>/data/plugins/maibot.bilibili-browser/bili_credential.json
```

OneKey 版的实际路径通常是：

```
%APPDATA%\MaiBotOneKeyDesktop\<实例哈希>\modules\MaiBot\data\plugins\maibot.bilibili-browser\bili_credential.json
```

插件**第一次启动会自动把这个文件生成出来**，里面带着填写说明。想换位置就把
`config.toml` 里的 `[auth] credential_file` 改成别的路径（绝对路径或相对数据目录的路径）。

文件内容长这样，只需要填 `sessdata` 一项：

```json
{
  "_说明": "……",
  "_怎么拿": "……",
  "sessdata": "从浏览器复制的那一长串",
  "bili_jct": "",
  "user_agent": ""
}
```

以 `_` 开头的键只是给人看的说明，插件读取时会忽略。

### 怎么拿

**电脑浏览器（推荐）**

1. 用浏览器打开并登录 `https://www.bilibili.com`，确认右上角是自己头像。
2. 按 `F12` 打开开发者工具。
3. 切到 **Application** 面板（中文版叫「应用」或「存储」）。
4. 左侧找到 **Cookies** → 点开 `https://www.bilibili.com`。
5. 在列表里找名为 **`SESSDATA`** 的那一行。
6. 双击它的 **Value** 列，全选复制（是一长串，通常含 `%2C` 这种编码，不要自己解码）。
7. 粘到 `bili_credential.json` 的 `"sessdata"` 里，保存。
8. 重启 MaiBot，或在 WebUI 插件页重新加载插件。

Firefox 是 `F12` → **存储** → **Cookie** → `https://www.bilibili.com`，同样找 `SESSDATA`。

**手机 / 不方便开 F12 时**

浏览器装个「cookie 导出」类扩展，或者用桌面模式登录后从同样的位置复制。
不管用哪种方式，要的就是 `SESSDATA` 这一项。

### 只有它能读字幕

- 不填：刷视频、看评论、看弹幕、自己刷，全都正常；只是**读不到字幕正文**，
  素材里会明确写「字幕没拿到，只能靠弹幕和评论判断」，模型不会硬编内容。
- 填了：多一份字幕正文，而且音视频类的视频效果提升最明显。

### 怎么知道它还灵不灵

发 `/bili_status`，会显示三层信息：

```
凭证来源：文件: ...\bili_credential.json
凭证文件：...\bili_credential.json
B站登录：有效（你的B站昵称）
```

如果显示「SESSDATA 失效或已过期，需要重新复制一份」，就按上面步骤重新拿一次——
B站的登录态会到期，换设备登录、改密码、长时间不活跃都可能让它失效，
这是正常现象，重新复制覆盖即可。

### 安全提醒

`SESSDATA` 等于你的登录态，拿到的人能以你的身份操作账号。所以：别发给别人、
别贴到公开的聊天记录或仓库里。放这个文件里就是因为它不进配置、也不会被导出。

## 已知限制

- **字幕需要登录**。没配 SESSDATA 时 `x/player/wbi/v2` 不返回字幕轨，素材里会明确写
  「字幕没拿到，只能靠弹幕和评论判断」，模型不会硬编内容。
- **`ranking/v2` 对无登录态常年回 `-352` 风控**，所以它排在回落链最后，默认不用它。
- **B站的 AI 视频总结接口也要登录**，本插件没用到它，内容靠字幕 + 弹幕 + 评论三方交叉。
- 首次请求会先访问一次 `https://www.bilibili.com/` 再取设备指纹（`buvid3`/`buvid4`/`b_nut`）。
  这一步是必须的：缺设备指纹时 B站会把搜索结果和首页推荐一律回成 `-101`，
  看着像「未登录」，其实只是没注册设备。

## 素材长什么样

`bilibili_watch` 返回的是给模型读的原文，大致这样：

```
【视频】标题
UP：xxx ｜ 时长 12:34 ｜ 播放 1234.6万 ｜ 点赞 100万 ｜ 弹幕 8888 ｜ 评论 4321 ｜ 生活 ｜ 2019-10-18
链接：https://www.bilibili.com/video/BVxxxx
简介：……
【字幕正文（中文（自动生成））】
……
【弹幕抽样·共 7200 条】前方高能（×12） / 这里好听
【热门评论】
1. [赞77.1万] 路人甲：好耶
【相关推荐】……
```

模型拿到这段之后自己组织语言，不会逐条念字段。
