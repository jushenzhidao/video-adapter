"""并发闸门：**槽位从创建占到终态**，不是创建请求返回就释放。

上游普遍限制同时在跑的任务数。直连转发在第 N+1 个并发请求就会开始报错，
所以超出上限要**排队**而不是失败（架构 §7），并显式打印退避日志。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger("video_adapter.queue")


@dataclass
class _Slot:
    limit: int
    holders: set[str] = field(default_factory=set)


class ConcurrencyGate:
    """按渠道键限流。释放是幂等的（终态可能被多次观察到）。"""

    def __init__(self) -> None:
        self._slots: dict[str, _Slot] = {}
        self._condition = asyncio.Condition()
        self._waiting: dict[str, int] = {}

    def _slot(self, key: str, limit: int) -> _Slot:
        slot = self._slots.get(key)
        if slot is None:
            slot = _Slot(limit=limit)
            self._slots[key] = slot
        else:
            slot.limit = limit
        return slot

    async def acquire(self, key: str, limit: int, holder_id: str, timeout: float) -> bool:
        """占用一个槽位。返回 False 表示排队超时（调用方应回 429）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        async with self._condition:
            attempt = 0
            while True:
                slot = self._slot(key, limit)
                if holder_id in slot.holders:
                    return True
                if len(slot.holders) < slot.limit:
                    slot.holders.add(holder_id)
                    return True
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                attempt += 1
                self._waiting[key] = self._waiting.get(key, 0) + 1
                log.warning(
                    "UPSTREAM FULL (attempt %d) — %s holds %d/%d slots, waiting up to %.1fs",
                    attempt,
                    key,
                    len(slot.holders),
                    slot.limit,
                    remaining,
                )
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return False

    async def release(self, key: str, holder_id: str) -> bool:
        """终态后释放。重复释放返回 False（不算错误）。"""
        async with self._condition:
            slot = self._slots.get(key)
            if slot is None or holder_id not in slot.holders:
                return False
            slot.holders.discard(holder_id)
            self._condition.notify_all()
            return True

    def active(self, key: str | None = None) -> int:
        if key is not None:
            slot = self._slots.get(key)
            return len(slot.holders) if slot else 0
        return sum(len(s.holders) for s in self._slots.values())

    def stats(self) -> dict:
        return {
            "active": self.active(),
            "channels": {
                key: {"active": len(slot.holders), "limit": slot.limit}
                for key, slot in self._slots.items()
            },
        }
