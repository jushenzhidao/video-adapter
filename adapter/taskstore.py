"""任务持久化后端：**redis（生产，唯一）** 与 **memory（仅开发）**。

🔴 **必须持久化**（架构 §6.2）：契约要求 `GET /tasks/{id}` 在 **7 天窗口**内可用。
image-adapter 那套"Redis 缺失则进程内 dict 降级"**不能照搬** —— 那个降级对图片是
"丢一轮对话上下文"，对本项目是**直接违约**（重启后 404，而调用方以为任务还在跑）。

## 为什么**没有 sqlite**（2026-09-13 移除）

曾经有第三个后端 `sqlite`（定位是"无 Redis 部署的合规降级：落盘、重启不丢"）。
移除它不是因为不可靠，而是因为它与这个服务的方向**冲突**：

1. **它是单机存储**，而任务与配额都属于**跨副本共享**的语义（限流桶按出口 IP 共享、
   任务表要能被任一副本读到）。多副本下 sqlite 直接失效 ⇒ 留着它等于长期维护一条
   注定不可用的部署路径。
2. **容器里它还要额外三件事**：持久卷（否则重建即丢）、单 worker（否则多进程抢锁）、
   优雅停机（WAL 需要干净退出）。这些成本换来的只是"少跑一个 redis"。
3. **两套后端 = 两套测试路径 + 两种部署形态**，而其中一条在多副本下必然违约。

⇒ 收敛为：

    redis   生产（唯一）。**启动即探测**，连不上直接失败（不静默降级、也没有退路）
    memory  **仅开发**的显式开关，启动时打 warning；它不是隐式降级

## key 布局

    <prefix>:item:<local_id>                     任务记录（JSON 原文）
    <prefix>:index:<provider>:<credential_id>    ZSET 索引（score = created_at，供列表分页）

`<prefix>` 由 `TASK_STORE_KEY_PREFIX` 给出（默认 `task`）：**多个部署共用同一个 redis
时必须区分**，否则彼此的列表会互相看见；测试也靠它拿到完全隔离的空间。
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod

from .seedance import TERMINAL_STATUSES  # noqa: F401  —— 单一真源在 seedance.py（原生六态）
from .settings import Settings

log = logging.getLogger("video_adapter.taskstore")


class TaskStore(ABC):
    backend = "abstract"

    @abstractmethod
    async def put(self, record: dict) -> None: ...

    @abstractmethod
    async def get(self, local_id: str) -> dict | None: ...

    @abstractmethod
    async def update(self, local_id: str, **fields) -> dict | None: ...

    @abstractmethod
    async def delete(self, local_id: str) -> bool: ...

    @abstractmethod
    async def list_for(
        self,
        *,
        credential_id: str,
        provider: str | None = None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]: ...

    @abstractmethod
    async def purge_expired(self, now: int) -> int: ...

    async def start(self) -> None:
        """启动探测。默认无资源可探（memory 后端）。"""
        return None

    async def close(self) -> None:  # pragma: no cover - 默认无资源
        return None


class MemoryStore(TaskStore):
    """进程内 dict —— **只允许作为显式开发开关**。"""

    backend = "memory"

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: dict) -> None:
        async with self._lock:
            self._records[record["local_id"]] = dict(record)

    async def get(self, local_id: str) -> dict | None:
        async with self._lock:
            found = self._records.get(local_id)
            return dict(found) if found else None

    async def update(self, local_id: str, **fields) -> dict | None:
        async with self._lock:
            found = self._records.get(local_id)
            if found is None:
                return None
            found.update(fields)
            return dict(found)

    async def delete(self, local_id: str) -> bool:
        async with self._lock:
            return self._records.pop(local_id, None) is not None

    async def list_for(
        self, *, credential_id: str, provider: str | None = None, page_num: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        async with self._lock:
            rows = [
                dict(r)
                for r in self._records.values()
                if r.get("credential_id") == credential_id
                and (provider is None or r.get("provider") == provider)
            ]
        rows.sort(key=lambda r: (r.get("created_at") or 0, r.get("local_id") or ""), reverse=True)
        total = len(rows)
        start = (max(1, page_num) - 1) * page_size
        return rows[start : start + page_size], total

    async def purge_expired(self, now: int) -> int:
        async with self._lock:
            dead = [k for k, r in self._records.items() if (r.get("expires_at") or 0) < now]
            for key in dead:
                self._records.pop(key, None)
        return len(dead)


class RedisStore(TaskStore):
    """Redis 主后端。**缺依赖 / 连不上都在启动时报错**，不退化成内存。"""

    backend = "redis"

    def __init__(self, url: str, prefix: str = "task") -> None:
        try:
            import redis.asyncio as redis_async  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于部署
            raise RuntimeError(
                "TASK_STORE=redis requires the `redis` package (it is a declared dependency "
                "in requirements.txt). Silently falling back to an in-process dict would break "
                "the 7-day GET contract."
            ) from exc
        self._prefix = (prefix or "task").strip() or "task"
        self._redis = redis_async.from_url(url, decode_responses=True)
        self._url = url

    def _item_key(self, local_id: str) -> str:
        return f"{self._prefix}:item:{local_id}"

    def _index_key(self, provider: str | None, credential_id: str) -> str:
        return f"{self._prefix}:index:{provider}:{credential_id}"

    async def start(self) -> None:
        """启动即探测。**连不上直接失败** —— sqlite 退路已被移除，别等到第一个请求才炸。"""
        try:
            await self._redis.ping()
        except Exception as exc:  # noqa: BLE001
            # 只报 host（剥掉 `user:pass@`）：这条消息会进日志，不该带凭证。
            host = (self._url or "").rsplit("@", 1)[-1]
            raise RuntimeError(
                f"cannot reach redis ({host}): {exc} — tasks must live in shared storage to honour "
                "the 7-day GET contract. Fix TASK_STORE_URL, or use TASK_STORE=memory for local "
                "development only."
            ) from exc

    async def put(self, record: dict) -> None:
        pipe = self._redis.pipeline()
        pipe.set(self._item_key(record["local_id"]), json.dumps(record, ensure_ascii=False))
        pipe.zadd(
            self._index_key(record.get("provider"), record.get("credential_id")),
            {record["local_id"]: int(record.get("created_at") or 0)},
        )
        await pipe.execute()

    async def get(self, local_id: str) -> dict | None:
        raw = await self._redis.get(self._item_key(local_id))
        return json.loads(raw) if raw else None

    async def update(self, local_id: str, **fields) -> dict | None:
        current = await self.get(local_id)
        if current is None:
            return None
        current.update(fields)
        await self.put(current)
        return current

    async def delete(self, local_id: str) -> bool:
        current = await self.get(local_id)
        if current is None:
            return False
        pipe = self._redis.pipeline()
        pipe.delete(self._item_key(local_id))
        pipe.zrem(self._index_key(current.get("provider"), current.get("credential_id")), local_id)
        await pipe.execute()
        return True

    async def list_for(
        self, *, credential_id: str, provider: str | None = None, page_num: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        index = self._index_key(provider, credential_id)
        total = int(await self._redis.zcard(index))
        start = (max(1, page_num) - 1) * page_size
        ids = await self._redis.zrevrange(index, start, start + page_size - 1)
        records = [await self.get(i) for i in ids]
        return [r for r in records if r], total

    async def purge_expired(self, now: int) -> int:
        """兜底扫描：任务记录**本身没有 TTL**，过期清理靠这里（以及看门狗置 `expired`）。

        ⚠️ 只扫 `<prefix>:item:*` —— 索引（`<prefix>:index:*`）是 ZSET，不是 JSON，
        一起扫会在 `json.loads` 上炸。两个命名空间刻意写全，别靠"单复数"这类巧合区分。
        """
        removed = 0
        async for key in self._redis.scan_iter(f"{self._prefix}:item:*"):
            raw = await self._redis.get(key)
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:  # pragma: no cover - 极小概率的脏数据
                continue
            if int(record.get("expires_at") or 0) < now:
                await self.delete(record["local_id"])
                removed += 1
        return removed

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(settings: Settings) -> TaskStore:
    backend = (settings.task_store or "redis").strip().lower()
    if backend == "memory":
        log.warning(
            "TASK_STORE=memory — tasks live in this process only and vanish on restart. "
            "The 7-day GET contract cannot be honoured. Development use only."
        )
        return MemoryStore()
    if backend == "redis":
        url = (settings.task_store_url or "").strip()
        if not url:
            raise RuntimeError(
                "TASK_STORE=redis needs TASK_STORE_URL. (The sqlite backend was removed: it is "
                "single-host storage and cannot serve the shared-storage semantics this service "
                "needs.) Provide a redis URL, or TASK_STORE=memory for local development only."
            )
        return RedisStore(url, prefix=settings.task_store_key_prefix)
    raise RuntimeError(
        f"unknown TASK_STORE={settings.task_store!r} (expected redis|memory). "
        "The sqlite backend was removed on 2026-09-13."
    )
