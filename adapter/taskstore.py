"""任务持久化后端。

🔴 **必须持久化**（架构 §6.2）：契约要求 `GET /tasks/{id}` 在 **7 天窗口**内可用。
image-adapter 那套"Redis 缺失则进程内 dict 降级"**不能照搬** —— 那个降级对图片是
"丢一轮对话上下文"，对本项目是**直接违约**（重启后 404，而调用方以为任务还在跑）。

因此：

    redis   主后端（多实例部署）
    sqlite  无 Redis 部署的**合规降级**（落盘、重启不丢）
    memory  **仅单实例开发模式的显式开关**，启动时打 warning；不是隐式降级
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

from .settings import Settings

log = logging.getLogger("video_adapter.taskstore")

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "expired", "cancelled"})


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


class SqliteStore(TaskStore):
    """落盘后端。**重启不丢**，作为无 Redis 部署的合规降级。"""

    backend = "sqlite"

    def __init__(self, path: str) -> None:
        self._path = str(path)
        target = Path(self._path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                local_id      TEXT PRIMARY KEY,
                provider      TEXT,
                credential_id TEXT,
                status        TEXT,
                created_at    INTEGER,
                expires_at    INTEGER,
                doc           TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_scope "
            "ON tasks (provider, credential_id, created_at DESC)"
        )
        self._conn.commit()
        self._lock = asyncio.Lock()

    def _write(self, record: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(local_id, provider, credential_id, status, created_at, expires_at, doc) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                record["local_id"],
                record.get("provider"),
                record.get("credential_id"),
                record.get("status"),
                int(record.get("created_at") or 0),
                int(record.get("expires_at") or 0),
                json.dumps(record, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    async def put(self, record: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, record)

    async def get(self, local_id: str) -> dict | None:
        async with self._lock:
            row = await asyncio.to_thread(
                lambda: self._conn.execute(
                    "SELECT doc FROM tasks WHERE local_id=?", (local_id,)
                ).fetchone()
            )
        return json.loads(row[0]) if row else None

    async def update(self, local_id: str, **fields) -> dict | None:
        current = await self.get(local_id)
        if current is None:
            return None
        current.update(fields)
        await self.put(current)
        return current

    async def delete(self, local_id: str) -> bool:
        async with self._lock:
            cursor = await asyncio.to_thread(
                lambda: self._conn.execute("DELETE FROM tasks WHERE local_id=?", (local_id,))
            )
            self._conn.commit()
        return cursor.rowcount > 0

    async def list_for(
        self, *, credential_id: str, provider: str | None = None, page_num: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        where = "WHERE credential_id=?" + (" AND provider=?" if provider else "")
        params: list = [credential_id] + ([provider] if provider else [])
        async with self._lock:
            total = await asyncio.to_thread(
                lambda: self._conn.execute(
                    f"SELECT COUNT(*) FROM tasks {where}", params
                ).fetchone()[0]
            )
            rows = await asyncio.to_thread(
                lambda: self._conn.execute(
                    f"SELECT doc FROM tasks {where} ORDER BY created_at DESC, local_id DESC LIMIT ? OFFSET ?",
                    params + [page_size, (max(1, page_num) - 1) * page_size],
                ).fetchall()
            )
        return [json.loads(r[0]) for r in rows], int(total)

    async def purge_expired(self, now: int) -> int:
        async with self._lock:
            cursor = await asyncio.to_thread(
                lambda: self._conn.execute("DELETE FROM tasks WHERE expires_at < ?", (now,))
            )
            self._conn.commit()
        return cursor.rowcount

    async def close(self) -> None:
        self._conn.close()


class RedisStore(TaskStore):
    """Redis 主后端。**缺依赖就在启动时报错**，不退化成 memory。"""

    backend = "redis"

    def __init__(self, url: str) -> None:
        try:
            import redis.asyncio as redis_async  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于部署
            raise RuntimeError(
                "TASK_STORE=redis requires the `redis` package; install it or switch to sqlite. "
                "Silently falling back to an in-process dict would break the 7-day GET contract."
            ) from exc
        self._redis = redis_async.from_url(url, decode_responses=True)
        self._url = url

    def _key(self, local_id: str) -> str:
        return f"task:{local_id}"

    async def put(self, record: dict) -> None:
        pipe = self._redis.pipeline()
        pipe.set(self._key(record["local_id"]), json.dumps(record, ensure_ascii=False))
        pipe.zadd(
            f"tasks:{record.get('provider')}:{record.get('credential_id')}",
            {record["local_id"]: int(record.get("created_at") or 0)},
        )
        await pipe.execute()

    async def get(self, local_id: str) -> dict | None:
        raw = await self._redis.get(self._key(local_id))
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
        pipe.delete(self._key(local_id))
        pipe.zrem(f"tasks:{current.get('provider')}:{current.get('credential_id')}", local_id)
        await pipe.execute()
        return True

    async def list_for(
        self, *, credential_id: str, provider: str | None = None, page_num: int = 1, page_size: int = 20
    ) -> tuple[list[dict], int]:
        index = f"tasks:{provider}:{credential_id}"
        total = int(await self._redis.zcard(index))
        start = (max(1, page_num) - 1) * page_size
        ids = await self._redis.zrevrange(index, start, start + page_size - 1)
        records = [await self.get(i) for i in ids]
        return [r for r in records if r], total

    async def purge_expired(self, now: int) -> int:
        # 任务键带 TTL（见 tasks.py 的 expires_at 设置），这里只做兜底扫描
        removed = 0
        async for key in self._redis.scan_iter("task:*"):
            raw = await self._redis.get(key)
            if not raw:
                continue
            record = json.loads(raw)
            if int(record.get("expires_at") or 0) < now:
                await self.delete(record["local_id"])
                removed += 1
        return removed

    async def close(self) -> None:
        await self._redis.aclose()


def build_store(settings: Settings) -> TaskStore:
    backend = (settings.task_store or "sqlite").strip().lower()
    if backend == "memory":
        log.warning(
            "TASK_STORE=memory — tasks live in this process only and vanish on restart. "
            "The 7-day GET contract cannot be honoured. Development use only."
        )
        return MemoryStore()
    if backend == "sqlite":
        return SqliteStore(settings.task_store_path)
    if backend == "redis":
        return RedisStore(settings.task_store_url)
    raise RuntimeError(f"unknown TASK_STORE={settings.task_store!r} (expected redis|sqlite|memory)")
