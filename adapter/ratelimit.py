"""按上游出口身份的**主动**限流（架构 §7.2）。

上游（`docs/upstreams/aivideomaker-official-api.md` §6）对**任务查询接口**按 **IP**
限制 60 次/分钟，超限回 HTTP 429 + `Retry-After`。本模块把那件事**前移**到我们这一侧：
在请求发出去**之前**决定"现在能不能发、要不要等"。

## 三个由"按 IP"直接推出的结论

限流的计量主体是**出口 IP** —— 不是 API Key、不是 `provider`、也不是 worker 进程。
每一条推论都对应一个具体的错误做法：

| 推论 | 正确做法 | 错误做法的后果 |
| --- | --- | --- |
| 配额属于**出口身份** | 桶键 = 上游 `origin`（`scheme://host:port`） | 按 `provider` 分桶 ⇒ 两条渠道各拿 60，发出去仍合起来 120/min，照样被拒 |
| 配额是**共享**的（同一出口 IP 上还有别的消费者） | 默认只声明部分额度（`RATE_LIMIT_QUERY_RPM=30`，见 §7.2） | 贴着 60 跑 ⇒ 与别人的流量叠加后必然超限 |
| 429 是**全局**信号 | 任一请求踩到 429 ⇒ 冷却该 origin 的**所有**后续查询 | 只让触发它的那个任务退避 ⇒ 同 origin 的其他查询继续撞墙 |

## 为什么主动限速比被动退避更关键

被动退避只在**已经违规之后**才起作用，而那次违规还带着一次重试 ——
在 60/min 的硬顶下，重试把请求数放大到配额之上，表现出来就是
"永远卡在 429 上"（旧行为：429 后硬等 10s 再重试，见 `transport.py`）。
主动限速把它换成**排队**（短等待即可满足）或**快速失败 + `Retry-After`**（契约内语义）。

## ⚠️ 桶是进程内状态（已知边界，不静默）

多 worker / 多副本时实际配额会按进程数放大（`副本数 × WEB_CONCURRENCY × rpm`）。
这与并发闸门是**同一类**边界（`docker-compose.yml` 文末）。本模块不假装共享：
`scope` 会出现在 `/healthz`、启动时也会告警；多副本下把 `rpm` 按进程数**整除**
即可恢复精确（见 `docs/03_引擎架构.md` §7.2）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger("video_adapter.ratelimit")

#: 限流车道。只区分"要不要计入配额"这一件事：
#:   `QUERY` —— 幂等查询。上游按 IP 限的就是它。
#:   `None`  —— 创建 / 取消。**不计入**：文档只对查询接口声明了 IP 限额，
#:              而创建是计费动作、低频，并发已由 `ConcurrencyGate` 管住。
#:              但它们收到 429 时**仍要回灌冷却**（见 `transport.py`）——
#:              429 说明"这个出口已被上游盯上"，对查询同样成立。
QUERY = "query"

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNPARSABLE = "<unparsable>"


def origin_of(url: str) -> str:
    """`https://user:pass@host:8443/a/b?k=v` → `https://host:8443`。

    **刻意用 `hostname` / `port` 而不是 `netloc`**：`netloc` 会把 `userinfo`
    一起带出来，而渠道允许把凭证放进 URL（`X-Auth-Emit` 支持查询串 / 路径形态）。
    桶键会进 `/healthz`，把凭证带进去等于让它出现在探针与日志里。
    路径与查询串同理被丢弃 —— 限流的粒度是出口身份，不是具体端点。
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return _UNPARSABLE
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return _UNPARSABLE
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _ceil_seconds(value: float) -> int:
    """给调用方看的等待时间：**向上取整且至少 1 秒**（`Retry-After` 是秒粒度）。"""
    return max(1, math.ceil(max(0.0, float(value))))


@dataclass
class Decision:
    """一次取令牌的结果。`retry_after` 会原样变成出口的 `Retry-After` 头。"""

    granted: bool
    reason: str  # granted | waited | no_token | cooldown
    waited_ms: float = 0.0
    retry_after: int = 0


@dataclass
class _Bucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float
    cooldown_until: float = 0.0
    served: int = 0
    cooldowns: int = 0
    blocked_cooldown: int = 0
    blocked_no_token: int = 0


class RateLimiter:
    """令牌桶 + 全局冷却。**键是上游 origin**（理由见模块 docstring）。"""

    def __init__(
        self,
        *,
        rpm: int,
        burst: int,
        cooldown_max_seconds: float,
        scope: str = "process",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rpm = max(1, int(rpm))
        self._capacity = float(max(1, int(burst)))
        self._refill_per_second = self._rpm / 60.0
        self._cooldown_max = max(0.0, float(cooldown_max_seconds))
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        # 扣减令牌必须串行（否则并发请求会超发）；冷却写入不需要（见 note_rate_limited）。
        self._lock = asyncio.Lock()
        self.scope = scope

    # ------------------------------------------------------------------ 取令牌
    async def acquire(self, key: str, *, wait_seconds: float) -> Decision:
        """取一个令牌。

        - 有令牌 ⇒ 立即授予（`granted`）；
        - 无令牌但能在 `wait_seconds` 内等到 ⇒ 等待后授予（`waited`）；
        - 超出等待预算 ⇒ 拒绝（`no_token`），`retry_after` 是还需等多久；
        - **冷却期内 ⇒ 不排队**，直接拒绝（`cooldown`）—— 上游已明确说"别来了"，
          排队只会把连接堆在这里，等到的仍然是 429。
        """
        started = self._clock()
        deadline = started + max(0.0, float(wait_seconds))
        while True:
            async with self._lock:
                now = self._clock()
                bucket = self._bucket(key)
                self._refill(bucket, now)
                if now < bucket.cooldown_until:
                    bucket.blocked_cooldown += 1
                    return Decision(
                        False, "cooldown", retry_after=_ceil_seconds(bucket.cooldown_until - now)
                    )
                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    bucket.served += 1
                    waited_ms = (self._clock() - started) * 1000.0
                    return Decision(
                        True,
                        "waited" if waited_ms > 1.0 else "granted",
                        waited_ms=waited_ms,
                    )
                need = (1.0 - bucket.tokens) / self._refill_per_second

            remaining = deadline - self._clock()
            if remaining <= 0:
                async with self._lock:
                    bucket = self._bucket(key)
                    now = self._clock()
                    self._refill(bucket, now)
                    if now < bucket.cooldown_until:  # 等待期间别人踩了 429
                        bucket.blocked_cooldown += 1
                        return Decision(
                            False, "cooldown", retry_after=_ceil_seconds(bucket.cooldown_until - now)
                        )
                    bucket.blocked_no_token += 1
                    need = (1.0 - bucket.tokens) / self._refill_per_second
                return Decision(False, "no_token", retry_after=_ceil_seconds(need))

            # 出锁再睡：持锁睡会让所有请求串行排队。
            await asyncio.sleep(min(need, remaining))

    # ------------------------------------------------------------------ 429 刹车
    def note_rate_limited(self, key: str, retry_after: float) -> tuple[int, bool]:
        """上游回了 429 ⇒ 刹车。返回 `(实际冷却秒数, 是否被上限截断)`。

        **为什么是同步方法**：它被调用的位置（`transport.py` 的 429 分支）没有可用的
        锁上下文，而这里要写的只有 `cooldown_until` 一个标量 —— 单事件循环下赋值不会
        撕裂。令牌**扣减**必须持锁（否则超发），冷却**写入**不需要：最坏情况是晚一个
        请求生效，而它的语义本来就是"尽快刹车"。

        截断必须被告知（返回值第二项）—— 静默截断会让人以为"上游让等多久就等多久"。
        """
        now = self._clock()
        wanted = max(0.0, float(retry_after))
        applied = wanted
        truncated = False
        if self._cooldown_max > 0 and wanted > self._cooldown_max:
            applied = self._cooldown_max
            truncated = True
        bucket = self._bucket(key)
        bucket.cooldown_until = max(bucket.cooldown_until, now + applied)
        bucket.cooldowns += 1
        return int(applied), truncated

    # ------------------------------------------------------------------ 观测
    def snapshot(self) -> dict[str, Any]:
        """`/healthz` 用。**不含任何凭证**（键已经过 `origin_of` 剥离 userinfo）。"""
        now = self._clock()
        buckets: dict[str, Any] = {}
        for key, bucket in self._buckets.items():
            self._refill(bucket, now)
            buckets[key] = {
                "tokens": round(bucket.tokens, 2),
                "burst": bucket.capacity,
                "cooldown_remaining": round(max(0.0, bucket.cooldown_until - now), 1),
                "served": bucket.served,
                "cooldowns_from_429": bucket.cooldowns,
                "blocked_cooldown": bucket.blocked_cooldown,
                "blocked_no_token": bucket.blocked_no_token,
            }
        return {
            "enabled": True,
            "scope": self.scope,
            "rpm": self._rpm,
            "burst": self._capacity,
            "buckets": buckets,
        }

    # ------------------------------------------------------------------ 内部
    def _bucket(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            # 新桶从**满**开始：首次访问不该被自己的限速器挡住。
            bucket = _Bucket(
                capacity=self._capacity,
                refill_per_second=self._refill_per_second,
                tokens=self._capacity,
                updated_at=self._clock(),
            )
            self._buckets[key] = bucket
        return bucket

    @staticmethod
    def _refill(bucket: _Bucket, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        if elapsed:
            bucket.tokens = min(
                bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second
            )
            bucket.updated_at = now
