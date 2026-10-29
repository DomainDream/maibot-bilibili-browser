# -*- coding: utf-8 -*-
"""浏览记录：避免反复刷到同一个视频，并限制「自己去刷」的节奏。

落盘到插件自己的 ``data_dir/history.json``，写入用「临时文件 + replace」，
避免进程被强杀时留下半个 JSON 让插件下次启动直接崩。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import time
from pathlib import Path
from typing import Any

from .client import Candidate

_MAX_SEEN = 800
_MAX_LOG = 60


class BrowseStore:
    """看过什么、什么时候刷的、今天刷了几次。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"version": 1, "seen": {}, "last_browse_at": 0.0, "daily": {}}
        self._loaded = False

    # -- 读写 --------------------------------------------------

    def _load_sync(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        seen = data.get("seen")
        daily = data.get("daily")
        self._data = {
            "version": 1,
            "seen": seen if isinstance(seen, dict) else {},
            "last_browse_at": float(data.get("last_browse_at") or 0.0),
            "daily": daily if isinstance(daily, dict) else {},
        }

    def _save_sync(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # 记录失败不影响功能，下次照样能刷
            pass

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await asyncio.to_thread(self._load_sync)

    # -- 记录 --------------------------------------------------

    @staticmethod
    def _today() -> str:
        return _dt.date.today().isoformat()

    async def mark_seen(self, bvid: str, title: str = "") -> None:
        """记下看过这个视频。"""

        key = (bvid or "").strip()
        if not key:
            return
        async with self._lock:
            await self._ensure_loaded()
            seen = self._data["seen"]
            seen[key] = {"at": time.time(), "title": (title or "")[:120]}
            if len(seen) > _MAX_SEEN:
                ordered = sorted(seen.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
                self._data["seen"] = dict(ordered[-_MAX_SEEN:])
            await asyncio.to_thread(self._save_sync)

    async def seen_within(self, days: int) -> set[str]:
        """最近 ``days`` 天看过的 bvid 集合。"""

        if days <= 0:
            return set()
        async with self._lock:
            await self._ensure_loaded()
            cutoff = time.time() - days * 86400
            return {
                bvid
                for bvid, info in self._data["seen"].items()
                if float((info or {}).get("at") or 0) >= cutoff
            }

    async def filter_fresh(self, candidates: list[Candidate], *, days: int) -> list[Candidate]:
        """过滤掉最近看过的；全被过滤完就原样返回（宁可重复也不空手）。"""

        fresh_window = await self.seen_within(days)
        if not fresh_window:
            return list(candidates)
        kept = [item for item in candidates if item.bvid not in fresh_window]
        return kept or list(candidates)

    # -- 节奏 --------------------------------------------------

    async def check_budget(self, *, cooldown_minutes: int, daily_limit: int) -> tuple[bool, str]:
        """能不能再自己去刷一次。

        Returns:
            ``(可以吗, 不行时的原因)``。
        """

        async with self._lock:
            await self._ensure_loaded()
            now = time.time()
            last_at = float(self._data.get("last_browse_at") or 0.0)
            if cooldown_minutes > 0 and last_at > 0:
                elapsed = now - last_at
                if elapsed < cooldown_minutes * 60:
                    remain = int(cooldown_minutes - elapsed / 60) + 1
                    return False, f"距上次自己刷B站还不到 {cooldown_minutes} 分钟，大约还要等 {remain} 分钟"
            if daily_limit > 0:
                used = int((self._data["daily"] or {}).get(self._today()) or 0)
                if used >= daily_limit:
                    return False, f"今天已经自己刷了 {used} 次，够多了"
            return True, ""

    async def note_browse(self, *, bvid: str = "", title: str = "") -> None:
        """记一次「自己去刷」的账。"""

        async with self._lock:
            await self._ensure_loaded()
            now = time.time()
            self._data["last_browse_at"] = now
            today = self._today()
            daily = self._data["daily"]
            daily[today] = int(daily.get(today) or 0) + 1
            # 只留最近 14 天，避免无限增长
            if len(daily) > 14:
                keep = sorted(daily.items())[-14:]
                self._data["daily"] = dict(keep)
            if bvid:
                self._data["seen"][bvid] = {"at": now, "title": (title or "")[:120]}
            await asyncio.to_thread(self._save_sync)

    async def snapshot(self) -> dict[str, Any]:
        """给状态命令用的一份摘要。"""

        async with self._lock:
            await self._ensure_loaded()
            now = time.time()
            seen = self._data["seen"]
            recent = sorted(
                seen.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0), reverse=True
            )[:_MAX_LOG]
            return {
                "total_seen": len(seen),
                "today_count": int((self._data["daily"] or {}).get(self._today()) or 0),
                "last_browse_at": float(self._data.get("last_browse_at") or 0.0),
                "last_browse_ago": (now - float(self._data.get("last_browse_at") or 0.0))
                if self._data.get("last_browse_at")
                else 0.0,
                "recent": [
                    {"bvid": bvid, "title": str((info or {}).get("title") or "")}
                    for bvid, info in recent
                ],
            }
