# -*- coding: utf-8 -*-
"""B站登录凭证的存放与读取。

为什么不放 ``config.toml``：那份配置会被 WebUI 读写、可能被导出分享、也可能进版本库，
把 Cookie 混进去很容易泄漏。所以凭证单独一个文件，插件启动时自己来读。

存放位置（默认）::

    <data_dir>/bili_credential.json
    # 实际路径通常是
    # <MaiBot>/data/plugins/maibot.bilibili-browser/bili_credential.json

优先级：``config.toml`` 的 ``[auth]`` 里填了就用配置，没填才读这个文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: 凭证文件名，放在插件自己的 data 目录下。
DEFAULT_CREDENTIAL_NAME = "bili_credential.json"

#: 首次运行自动写出的模板。JSON 不支持注释，所以用 ``_`` 开头的键放说明。
CREDENTIAL_TEMPLATE = {
    "_说明": (
        "这个文件只放B站登录凭证，不会被 WebUI 或插件更新覆盖。"
        "把浏览器里 B站的 SESSDATA 填到下面的 sessdata 字段即可；不填也能正常刷视频，只是读不到字幕。"
    ),
    "_怎么拿": (
        "电脑浏览器登录 bilibili.com → 按 F12 → Application（应用/存储）→ Cookies → "
        "选 https://www.bilibili.com → 找到名为 SESSDATA 的那一行 → 双击复制整串 value"
    ),
    "_注意": "SESSDATA 等于你的登录态，别发给别人、别贴到公开的地方。失效了重新复制一次即可。",
    "sessdata": "",
    "bili_jct": "",
    "user_agent": "",
}


@dataclass
class Credential:
    """一份解析好的登录凭证。"""

    sessdata: str = ""
    bili_jct: str = ""
    user_agent: str = ""
    #: 来源说明，用于日志和状态命令：``配置`` / ``文件: <路径>`` / ``空``
    source: str = ""

    @property
    def has_login(self) -> bool:
        """是否配了 SESSDATA。"""

        return bool(self.sessdata)


def _looks_like_token(raw: str) -> bool:
    """判断一个值是不是被误填进来的 Cookie / token。

    有人会在 ``credential_file`` 那格里直接粘整串 Cookie（含分号、``%2C``、
    ``SESSDATA=`` 前缀）。这种值当路径用会把目录名搞成一坨乱码，所以识别出来
    就当作「没填」，回退默认位置。
    """

    low = raw.lower()
    if "sessdata=" in low or "bili_jct=" in low or "%2c" in low:
        return True
    return ";" in raw


def resolve_credential_path(data_dir: Path, configured: str = "") -> Path:
    """算出凭证文件的实际路径。

    只接受插件 data 目录**里面**的相对路径。绝对路径、含 ``..`` 的路径、以及经由符号
    链接跳出 data 目录的路径，一律回退默认名——这条配置不该成为往任意位置写文件的口子。
    误填成 token 时同样按留空处理。
    """

    base = Path(data_dir)
    raw = (configured or "").strip()
    if not raw or _looks_like_token(raw):
        return base / DEFAULT_CREDENTIAL_NAME
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or ".." in candidate.parts:
        return base / DEFAULT_CREDENTIAL_NAME
    target = (base / candidate).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:  # 符号链接跳出 data 目录
        return base / DEFAULT_CREDENTIAL_NAME
    return target


def load_credential(path: Path) -> Credential | None:
    """读凭证文件。文件不存在、读不动、JSON 坏了都返回 ``None``，绝不抛。"""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return Credential(
        sessdata=str(data.get("sessdata") or "").strip(),
        bili_jct=str(data.get("bili_jct") or "").strip(),
        user_agent=str(data.get("user_agent") or "").strip(),
        source=f"文件: {path}",
    )


def ensure_credential_file(path: Path) -> bool:
    """凭证文件不存在时写一份模板出来。

    Returns:
        是否真的写了（已存在返回 ``False``）。
    """

    target = Path(path)
    if target.exists():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(CREDENTIAL_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def save_credential(path: Path, *, sessdata: str, bili_jct: str = "", user_agent: str = "") -> bool:
    """把凭证写回文件；文件已存在时保留其中的说明字段。"""

    target = Path(path)
    payload: dict[str, str] = dict(CREDENTIAL_TEMPLATE)
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        payload = {str(k): str(v) for k, v in existing.items()}
    payload["sessdata"] = str(sessdata or "").strip()
    payload["bili_jct"] = str(bili_jct or "").strip()
    payload["user_agent"] = str(user_agent or "").strip()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def resolve_credential(
    *,
    config_sessdata: str,
    config_bili_jct: str,
    config_user_agent: str,
    data_dir: Path,
    configured_path: str = "",
) -> tuple[Credential, Path]:
    """定出这次要用的凭证。

    配置里填了 SESSDATA 就优先用配置（有人在 WebUI 里填了就按他填的走），
    否则读凭证文件。

    Returns:
        ``(凭证, 凭证文件路径)``。
    """

    path = resolve_credential_path(data_dir, configured_path)
    sessdata = (config_sessdata or "").strip()
    if sessdata:
        return (
            Credential(
                sessdata=sessdata,
                bili_jct=(config_bili_jct or "").strip(),
                user_agent=(config_user_agent or "").strip(),
                source="配置 [auth]",
            ),
            path,
        )

    loaded = load_credential(path)
    if loaded is not None and loaded.has_login:
        # 配置里的 UA 若填了，仍然优先
        if (config_user_agent or "").strip():
            loaded.user_agent = config_user_agent.strip()
        return loaded, path

    return (
        Credential(
            bili_jct=(config_bili_jct or "").strip(),
            user_agent=(config_user_agent or "").strip(),
            source="未配置",
        ),
        path,
    )
