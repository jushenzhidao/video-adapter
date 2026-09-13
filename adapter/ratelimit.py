"""上游限流的**主动**降频与限速（架构 §7.1）。

上游（`docs/upstreams/aivideomaker-official-api.md` §6）对**任务查询接口**按 **IP**
限制 60 次/分钟，超限回 HTTP 429 + `Retry-After`。本模块把那件事**前移**到我们这一侧：
在请求发出去**之前**决定"现在能不能发、要不要等"。

## 三个由"按 IP"直接推出的结论

限流的计量主体是**出口 IP** —— 不是 API Key、不是 `provider`、也不是 worker 进程。
每一条推论都对应一个具体的错误做法：

| 推论 | 正确做法 | 错误做法的后果 |
| --- | --- | --- |
| 配额属于**出口身份** | 桶键 = 上游 `origin`（`scheme://host:port`） | 按 `provider` 分桶 ⇒ 两条渠道各拿 60，合起来仍发 120/min，照样被拒 |
| 配额是**共享**的（同一出口 IP 上还有别的消费者） | 默认只声明部分额度（`RATE_LIMIT_QUERY_RPM=30`） | 贴着 60 跑 ⇒ 与别人的流量叠加后必然超限 |
| 429 是**全局**信号 | 任一请求踩到 429 ⇒ 冷却该 origin 的**所有**后续查询 | 只让触发它的请求退避 ⇒ 同 origin 的其他查询继续撞墙 |

## 为什么"共享"是这一层的关键

配额既然属于**出口**而不是进程，桶就必须是**跨进程**的。两个后端：

| `RATE_LIMIT_STORE` | 语义 | 适用 |
| --- | --- | --- |
| `process`（默认） | 进程内状态，`scope=process` | 单副本。实际配额 = 该进程的 rpm |
| `redis` | **共享桶**，`scope=shared` | 多副本 / 多 worker。所有进程合起来才是配置的 rpm |

Redis 后端用 **Redis 服务器时间**（Lua 里的 `TIME`）而不是各进程的本地时钟 ——
多副本的本地时钟必然漂移，用它算令牌补充会让各副本算出**不同**的配额。

> 未显式配置 `RATE_LIMIT_STORE` 时**跟随任务后端**：`TASK_STORE=redis` ⇒ 桶也用 redis。
> 多副本部署本来就必须把任务表放到共享存储上（§6.2），这条推断能省掉一个要配的旋钮，
> 也少一个"任务表共享了、限流桶忘了共享"的错配机会。

## 降级：Redis 挂了怎么办

**默认 `RATE_LIMIT_FAIL_MODE=open`**：后端不可用时回退到**进程内桶**（仍然限速，只是不再精确），
并在连续失败到阈值后**短路**一段时间，避免每个请求都去等一个连不上的后端。

为什么不是 fail-closed：限流器保护的是**配额**，配额超了是"可恢复的软故障"（退避即可，
上游还有 429 兜底）；而把整个服务的查询打死是**硬故障**。宁可降级也不要雪崩。

⚠️ 但**绝不静默**：`/healthz` 的 `rate_limit.backend` 报 `ok=false` + `degraded=true` + 原因，
`scope` 也**如实退回 `process`**（此刻它确实不再是共享的）。
要改成拒绝式就配 `RATE_LIMIT_FAIL_MODE=closed`（那时 Redis 成为可用性硬依赖）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .settings import Settings

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
#: 连续失败多少次判定后端不可用（进入短路）。
_BREAKER_THRESHOLD = 3
#: 短路窗口：这段时间内不再尝试后端，直接用降级层。
_BREAKER_SECONDS = 5.0


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
    #: granted | no_token | cooldown | fail_closed
    reason: str
    waited_ms: float = 0.0
    #: 对外用的整数秒（HTTP `Retry-After`）。
    retry_after: int = 0
    #: 内部用的精确秒（给 `sleep`；`retry_after` 向上取整会多睡最多 1 秒）。
    wait_seconds: float = 0.0


# =============================================================================
# 后端协议：只管"原子的取令牌 / 设冷却 / 读状态"，**不管等待**（等待是门面的事）
# =============================================================================


class BucketStore(Protocol):
    name: str

    async def try_take(self, key: str, *, capacity: float, rate: float) -> Decision: ...

    async def set_cooldown(self, key: str, seconds: float) -> float: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@dataclass
class _Bucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float
    cooldown_until: float = 0.0


class ProcessBuckets:
    """进程内令牌桶。单副本下的正确实现，也是 Redis 故障时的**降级层**。

    🔴 内部方法**不含任何 `await` 点** ⇒ 在单事件循环下天然原子，不需要锁。
    （一旦有人往里面加 `await`，就破坏了原子性 —— 那时必须重新引入锁。）
    降级路径需要**同步**调用它（它被异常处理器调用，那里不能 await），
    所以这里同时提供 `*_sync` 版本。
    """

    name = "process"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._clock = clock

    async def try_take(self, key: str, *, capacity: float, rate: float) -> Decision:
        return self.try_take_sync(key, capacity=capacity, rate=rate)

    def try_take_sync(self, key: str, *, capacity: float, rate: float) -> Decision:
        now = self._clock()
        bucket = self._bucket(key, capacity=capacity, rate=rate, now=now)
        self._refill(bucket, now)
        if now < bucket.cooldown_until:
            need = bucket.cooldown_until - now
            return Decision(False, "cooldown", retry_after=_ceil_seconds(need), wait_seconds=need)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return Decision(True, "granted")
        need = (1.0 - bucket.tokens) / bucket.refill_per_second
        return Decision(False, "no_token", retry_after=_ceil_seconds(need), wait_seconds=need)

    async def set_cooldown(self, key: str, seconds: float) -> float:
        return self.set_cooldown_sync(key, seconds)

    def set_cooldown_sync(self, key: str, seconds: float) -> float:
        now = self._clock()
        bucket = self._bucket(key, capacity=1.0, rate=1.0, now=now)
        bucket.cooldown_until = max(bucket.cooldown_until, now + max(0.0, seconds))
        return bucket.cooldown_until - now

    async def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        buckets: dict[str, Any] = {}
        for key, bucket in self._buckets.items():
            self._refill(bucket, now)
            buckets[key] = {
                "tokens": round(bucket.tokens, 2),
                "burst": bucket.capacity,
                "cooldown_remaining": round(max(0.0, bucket.cooldown_until - now), 1),
            }
        return {"buckets": buckets}

    async def close(self) -> None:
        return None

    def _bucket(self, key: str, *, capacity: float, rate: float, now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            # 新桶从**满**开始：首次访问不该被自己的限速器挡住。
            bucket = _Bucket(
                capacity=capacity, refill_per_second=rate, tokens=capacity, updated_at=now
            )
            self._buckets[key] = bucket
        else:
            bucket.capacity = capacity
            bucket.refill_per_second = rate
        return bucket

    @staticmethod
    def _refill(bucket: _Bucket, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        if elapsed:
            bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second)
            bucket.updated_at = now


# --- Redis 后端 ------------------------------------------------------------
#
# 两个脚本都是**单次往返的原子操作**。用 `redis.call('TIME')` 取服务器时钟：
# 多副本的本地时钟必然漂移，用它算令牌补充会让各副本算出不同的配额。
#
# ⚠️ Lua 的**返回值**里 number 会被截断成整数（Redis 的协议转换规则），
# 而**命令参数**走 `lua_tostring`（保留小数）。所以：
#   · 要返回的小数一律显式 `tostring()`；
#   · 写回 Redis 的浮点也一律 `tostring()`，别指望隐式转换。

_TAKE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local cd = tonumber(redis.call('GET', KEYS[2]) or '0')
if cd > now then
  return {0, 'cooldown', tostring((cd - now) / 1000)}
end

local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local vals = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(vals[1])
local ts = tonumber(vals[2])
if tokens == nil or ts == nil then
  tokens = cap
  ts = now
end
if now > ts then
  tokens = math.min(cap, tokens + (now - ts) / 1000 * rate)
  ts = now
end

local ttl = math.ceil(cap / rate * 1000) + 60000
if tokens >= 1 then
  redis.call('HSET', KEYS[1], 'tokens', tostring(tokens - 1), 'ts', tostring(ts))
  redis.call('PEXPIRE', KEYS[1], ttl)
  return {1, 'ok', '0'}
end

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(ts))
redis.call('PEXPIRE', KEYS[1], ttl)
return {0, 'no_token', tostring((1 - tokens) / rate)}
"""

_COOLDOWN_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local until_ms = now + math.floor(tonumber(ARGV[1]) * 1000)
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if until_ms > cur then
  cur = until_ms
end
local left = (cur - now) / 1000
if left < 0 then left = 0 end
redis.call('SET', KEYS[1], tostring(cur), 'PX', math.floor(left * 1000) + 60000)
return tostring(left)
"""

_STATE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local cd = tonumber(redis.call('GET', KEYS[2]) or '0')
local left = 0
if cd > now then left = (cd - now) / 1000 end
local vals = redis.call('HMGET', KEYS[1], 'tokens')
return {vals[1] or '', tostring(left)}
"""


class RedisBuckets:
    """跨进程共享桶。**Redis 服务器时钟是唯一时基**（多副本本地时钟会漂移）。"""

    name = "redis"

    def __init__(self, url: str, *, prefix: str, timeout: float = 0.5) -> None:
        try:
            import redis.asyncio as redis_async  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于部署
            raise RuntimeError(
                "RATE_LIMIT_STORE=redis requires the `redis` package; install it or use "
                "`process`. Silently falling back to a per-process bucket would make the "
                "effective upstream quota scale with the number of processes."
            ) from exc
        self._prefix = prefix
        self._redis = redis_async.from_url(
            url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
        )
        #: 供 `/healthz` 显示。**不带密码 / 查询串**（与 `origin_of` 同一个理由）。
        self.endpoint = safe_endpoint(url)
        self._take = self._redis.register_script(_TAKE_LUA)
        self._cooldown = self._redis.register_script(_COOLDOWN_LUA)
        self._state = self._redis.register_script(_STATE_LUA)

    def _bucket_key(self, key: str) -> str:
        return f"{self._prefix}:bucket:{key}"

    def _cool_key(self, key: str) -> str:
        return f"{self._prefix}:cool:{key}"

    async def try_take(self, key: str, *, capacity: float, rate: float) -> Decision:
        granted, reason, raw = await self._take(
            keys=[self._bucket_key(key), self._cool_key(key)],
            args=[str(capacity), str(rate)],
        )
        wait = float(raw or 0.0)
        if int(granted) == 1:
            return Decision(True, "granted")
        if reason == "cooldown":
            return Decision(False, "cooldown", retry_after=_ceil_seconds(wait), wait_seconds=wait)
        return Decision(False, "no_token", retry_after=_ceil_seconds(wait), wait_seconds=wait)

    async def set_cooldown(self, key: str, seconds: float) -> float:
        raw = await self._cooldown(keys=[self._cool_key(key)], args=[str(max(0.0, seconds))])
        return float(raw or 0.0)

    async def snapshot(self) -> dict[str, Any]:
        """⚠️ 用 `SCAN` 遍历，只为 `/healthz` 服务（调用频率低）。最多取 50 个 origin。"""
        buckets: dict[str, Any] = {}
        marker = f"{self._prefix}:bucket:"
        async for raw_key in self._redis.scan_iter(match=f"{marker}*", count=100):
            key = raw_key[len(marker) :]
            tokens_raw, cooldown_raw = await self._state(
                keys=[self._bucket_key(key), self._cool_key(key)]
            )
            try:
                tokens: float | None = round(float(tokens_raw), 2)
            except (TypeError, ValueError):
                tokens = None
            buckets[key] = {
                "tokens": tokens,
                "cooldown_remaining": round(float(cooldown_raw or 0.0), 1),
            }
            if len(buckets) >= 50:
                break
        return {"buckets": buckets}

    async def ping(self) -> None:
        await self._redis.ping()

    async def close(self) -> None:
        await self._redis.aclose()


def safe_endpoint(url: str) -> str:
    """`redis://user:pass@host:6379/0` → `redis://host:6379/0`（**去掉凭证**）。"""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return _UNPARSABLE
    host = parts.hostname or ""
    if not host:
        return _UNPARSABLE
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme or 'redis'}://{host}{port}{parts.path or ''}"


# =============================================================================
# 门面：等待（排队）、计数、降级与短路
# =============================================================================


class RateLimiter:
    """令牌桶 + 全局冷却。**键是上游 origin**（理由见模块 docstring）。"""

    def __init__(
        self,
        *,
        rpm: int,
        burst: int,
        cooldown_max_seconds: float,
        store: BucketStore,
        fallback: ProcessBuckets | None = None,
        fail_mode: str = "open",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rpm = max(1, int(rpm))
        self._capacity = float(max(1, int(burst)))
        self._refill_per_second = self._rpm / 60.0
        self._cooldown_max = max(0.0, float(cooldown_max_seconds))
        self._store = store
        #: 降级层只能是**进程内**的（降级到另一个远程后端没有意义）。
        self._fallback = fallback
        self._fail_mode = "closed" if str(fail_mode).lower() == "closed" else "open"
        self._clock = clock
        self._breaker_until = 0.0
        self._failures = 0
        self._degraded_reason = ""
        self.counters: dict[str, int] = {
            "served": 0,
            "blocked_no_token": 0,
            "blocked_cooldown": 0,
            "cooldowns_from_429": 0,
            "cooldown_truncated": 0,
            "backend_failures": 0,
            "served_by_fallback": 0,
            "rejected_by_fail_mode": 0,
        }

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        """探测后端。**探测失败只降级、不致命**（除非 `fail_mode=closed`）。"""
        probe = getattr(self._store, "ping", None)
        if probe is None:
            return
        try:
            await probe()
        except Exception as exc:  # noqa: BLE001 - 任何连不上都是"降级"，不是崩溃
            self._note_backend_failure(exc)
            if self._fail_mode == "closed":
                log.error(
                    "RATE_LIMIT_STORE=%s is unreachable and RATE_LIMIT_FAIL_MODE=closed — queries "
                    "will be REJECTED until it recovers: %s",
                    self._store.name,
                    exc,
                )
            else:
                log.warning(
                    "RATE_LIMIT_STORE=%s is unreachable at startup — falling back to a per-process "
                    "bucket: the quota is NO LONGER shared across processes until it recovers: %s",
                    self._store.name,
                    exc,
                )

    async def close(self) -> None:
        await self._store.close()
        if self._fallback is not None:
            await self._fallback.close()

    # ---------------------------------------------------------------- 取令牌
    async def acquire(self, key: str, *, wait_seconds: float) -> Decision:
        """取一个令牌。

        - 有令牌 ⇒ 立即授予（`granted`）；
        - 无令牌但能在 `wait_seconds` 内等到 ⇒ 等待后授予（`waited_ms` 有值）；
        - 超出等待预算 ⇒ 拒绝（`no_token`），`retry_after` 是还需等多久；
        - **冷却期内 ⇒ 不排队**，直接拒绝（`cooldown`）—— 上游已明确说"别来了"，
          排队只会把连接堆在这里，等到的仍然是 429。
        """
        started = self._clock()
        deadline = started + max(0.0, float(wait_seconds))
        slept = False
        while True:
            decision = await self._take(key)
            if decision.granted:
                waited_ms = (self._clock() - started) * 1000.0
                # 只有**真的 sleep 过**才算"排队等待"。Redis 后端的一次往返也要 1ms 量级，
                # 把它标成 waited 会让这个标签失去意义（"等待"应当等于"配额不足而让路"）。
                return Decision(
                    True, "waited" if slept else "granted", waited_ms=waited_ms
                )
            if decision.reason != "no_token":
                return decision  # cooldown / fail_closed：都不排队
            remaining = deadline - self._clock()
            if remaining <= 0:
                return decision
            await asyncio.sleep(min(max(decision.wait_seconds, 0.01), remaining))
            slept = True

    async def _take(self, key: str) -> Decision:
        if self._fallback is not None and self._clock() < self._breaker_until:
            return self._take_from_fallback(key)
        try:
            decision = await self._store.try_take(
                key, capacity=self._capacity, rate=self._refill_per_second
            )
        except Exception as exc:  # noqa: BLE001 - 后端故障统一按"降级"处理
            self._note_backend_failure(exc)
            if self._fail_mode == "closed" or self._fallback is None:
                self.counters["rejected_by_fail_mode"] += 1
                return Decision(False, "fail_closed", retry_after=1, wait_seconds=1.0)
            return self._take_from_fallback(key)
        self._failures = 0
        self._degraded_reason = ""
        if decision.granted:
            self.counters["served"] += 1
        elif decision.reason == "cooldown":
            self.counters["blocked_cooldown"] += 1
        else:
            self.counters["blocked_no_token"] += 1
        return decision

    def _take_from_fallback(self, key: str) -> Decision:
        assert self._fallback is not None
        self.counters["served_by_fallback"] += 1
        return self._fallback.try_take_sync(
            key, capacity=self._capacity, rate=self._refill_per_second
        )

    def _note_backend_failure(self, exc: Exception) -> None:
        self.counters["backend_failures"] += 1
        self._failures += 1
        self._degraded_reason = f"{type(exc).__name__}: {exc}"[:200]
        if self._failures >= _BREAKER_THRESHOLD:
            self._breaker_until = self._clock() + _BREAKER_SECONDS
            self._failures = 0
            log.warning(
                "rate limit backend %s failed %d times in a row — short-circuiting for %.0fs "
                "(falling back to the in-process bucket)",
                self._store.name,
                _BREAKER_THRESHOLD,
                _BREAKER_SECONDS,
            )

    # ---------------------------------------------------------------- 429 刹车
    async def note_rate_limited(self, key: str, retry_after: float) -> tuple[int, bool]:
        """上游回了 429 ⇒ 刹车。返回 `(实际冷却秒数, 是否被上限截断)`。

        截断必须被告知（返回值第二项）—— 静默截断会让人以为"上游让等多久就等多久"。
        """
        wanted = max(0.0, float(retry_after))
        applied = wanted
        truncated = False
        if self._cooldown_max > 0 and wanted > self._cooldown_max:
            applied = self._cooldown_max
            truncated = True
        if self._fallback is not None and self._clock() < self._breaker_until:
            self._fallback.set_cooldown_sync(key, applied)
        else:
            try:
                await self._store.set_cooldown(key, applied)
            except Exception as exc:  # noqa: BLE001
                self._note_backend_failure(exc)
                if self._fallback is not None:
                    self._fallback.set_cooldown_sync(key, applied)
        self.counters["cooldowns_from_429"] += 1
        if truncated:
            self.counters["cooldown_truncated"] += 1
        return int(applied), truncated

    # ---------------------------------------------------------------- 观测
    async def snapshot(self) -> dict[str, Any]:
        """`/healthz` 用。**不含任何凭证**（origin 已过 `origin_of`，端点已过 `safe_endpoint`）。"""
        degraded = self._fallback is not None and self._clock() < self._breaker_until
        backend: dict[str, Any] = {
            "name": self._store.name,
            "ok": not degraded,
            "degraded": degraded,
            "detail": (self._degraded_reason if degraded else ""),
        }
        endpoint = getattr(self._store, "endpoint", None)
        if endpoint:
            backend["endpoint"] = endpoint
        if not degraded:
            probe = getattr(self._store, "ping", None)
            if probe is not None:
                try:
                    await probe()
                except Exception as exc:  # noqa: BLE001
                    backend["ok"] = False
                    backend["degraded"] = True
                    backend["detail"] = f"{type(exc).__name__}: {exc}"[:200]
        buckets: dict[str, Any] = {}
        try:
            buckets = (await self._store.snapshot()).get("buckets", {})
        except Exception as exc:  # noqa: BLE001
            backend["ok"] = False
            backend["detail"] = backend["detail"] or f"{type(exc).__name__}: {exc}"[:200]
        shared = self._store.name == "redis" and not backend["degraded"]
        return {
            "enabled": True,
            # 降级时**如实退回 `process`** —— 此刻的桶确实不再是共享的。
            "scope": "shared" if shared else "process",
            "rpm": self._rpm,
            "burst": self._capacity,
            "fail_mode": self._fail_mode,
            "backend": backend,
            "counters": dict(self.counters),
            "buckets": buckets,
        }


# =============================================================================
# 装配
# =============================================================================


def store_choice(settings: Settings) -> tuple[str, str]:
    """决定用哪个后端、以及它的地址。**空配置跟随任务后端**（见模块 docstring）。"""
    kind = (settings.rate_limit_store or "").strip().lower()
    url = (settings.rate_limit_store_url or "").strip()
    if not kind:
        kind = "redis" if (settings.task_store or "").strip().lower() == "redis" else "process"
    if kind == "redis" and not url:
        url = (settings.task_store_url or "").strip()
    return kind, url


def _worker_count() -> int:
    """本进程看到的 gunicorn worker 数（`WEB_CONCURRENCY`）。读不到就算 1。

    ⚠️ 它数的是**同一容器内**的 worker，不是**副本数** —— 副本数从进程内看不见。
    """
    try:
        return max(1, int(os.environ.get("WEB_CONCURRENCY") or "1"))
    except ValueError:
        return 1


def build_rate_limiter(settings: Settings) -> RateLimiter | None:
    """按配置装配。关闭时返回 `None`（传输层据此整段跳过限流分支）。"""
    if not settings.rate_limit_enabled:
        log.warning(
            "RATE_LIMIT_ENABLED=false — queries go out unthrottled; the upstream answers 429 when "
            "the per-IP quota is exceeded and only passive backoff remains (see §7.1)."
        )
        return None

    kind, url = store_choice(settings)
    prefix = (settings.rate_limit_key_prefix or "ratelimit").strip() or "ratelimit"
    fail_mode = "closed" if str(settings.rate_limit_fail_mode).lower() == "closed" else "open"

    fallback: ProcessBuckets | None = None
    if kind == "redis":
        if not url:
            raise RuntimeError(
                "RATE_LIMIT_STORE=redis needs RATE_LIMIT_STORE_URL (or TASK_STORE_URL). Falling "
                "back to a per-process bucket would silently multiply the upstream quota by the "
                "number of processes."
            )
        store: BucketStore = RedisBuckets(
            url, prefix=prefix, timeout=settings.rate_limit_redis_timeout_seconds
        )
        # 后端故障时退回进程内桶：仍然限速，只是不再精确（`scope` 会如实退回 process）。
        if fail_mode == "open":
            fallback = ProcessBuckets()
    else:
        store = ProcessBuckets()

    limiter = RateLimiter(
        rpm=settings.rate_limit_query_rpm,
        burst=settings.rate_limit_query_burst,
        cooldown_max_seconds=settings.rate_limit_cooldown_max_seconds,
        store=store,
        fallback=fallback,
        fail_mode=fail_mode,
    )
    log.info(
        "RATE_LIMIT: query lane capped at %d rpm per upstream origin (burst %d, store=%s, "
        "scope=%s, fail_mode=%s, key_prefix=%s)",
        settings.rate_limit_query_rpm,
        settings.rate_limit_query_burst,
        store.name,
        "shared" if kind == "redis" else "process",
        fail_mode,
        prefix,
    )
    workers = _worker_count()
    if kind == "process" and workers > 1:
        log.warning(
            "RATE_LIMIT: %d gunicorn workers each keep their **own** token bucket — the effective "
            "rate against one upstream becomes ~%d× the configured %d rpm. Use "
            "RATE_LIMIT_STORE=redis for a shared bucket, divide RATE_LIMIT_QUERY_RPM by the worker "
            "count, or keep WEB_CONCURRENCY=1. The same multiplication applies per replica "
            "(not visible from inside a process).",
            workers,
            workers,
            settings.rate_limit_query_rpm,
        )
    return limiter
