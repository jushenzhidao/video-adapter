"""上游 HTTP 客户端。

**窄重试通道**（架构 §7）：只重发"能确认请求被拒"的调用。

    创建 / 取消（非幂等）  仅重试 **429** —— 429 表示请求被拒，没有计费；
                          5xx / 超时 / 不可达一律**不重发**（可能已被受理，重发即二次计费）。
    查询（幂等 GET）      429 与 5xx / 网络错误都可重试。

**429 的处理在拿到上游限流契约后变了**（`upstreams/aivideomaker-official-api.md` §6：
查询接口按 **IP** 60 次/分钟）：不再"硬等一小会儿再重试"。那样做会把请求数放大到
配额**之上** —— 60/min 的硬顶下每次违规还带一次重试，表现出来就是"永远卡在 429 上"。
现在的顺序是（架构 §7.2）：

    ① 查询车道先向 `RateLimiter` 取令牌 —— **主动降频**，超预算就按契约回 429；
    ② 真收到 429 ⇒ 给该 origin 设**全局冷却**（同一出口 IP 的所有查询一起刹车）；
    ③ 只在很小的预算内（`RATE_LIMIT_RETRY_BUDGET_SECONDS`，默认 1s）原地重试一次；
       超出预算就把 429 原样交给上层 —— 它会带着 `Retry-After` 出去，让调用方退避。

默认**不信环境代理**（`trust_env=False`）：macOS 的 `scutil` 代理会把回环地址也代理走，
表现为拿到一个网关错误体而不是 connection refused —— 那会把"本地假上游"的排障带偏。

**这里也是上报的唯一收口**：每一次上游调用（含重试的每一次）都开一条 `upstream.call`
子 span，带发出去的 request 与收到的 response 原文，以及上游 task id（能从调用上下文拿到时）。
放在收口处而不是各调用点，是为了让"哪条路由漏了埋点"这种问题不可能发生。

⚠️ **被本地限流挡下的请求不产生 `upstream.call`** —— 因为**没有发出调用**。伪造一条会让
上报里"发过的请求"比真实的多（与"没收到应答就不许写状态码"是同一条纪律）。它记在
日志与 `/healthz` 的计数器里。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import observability
from .errors import local_rate_limited, upstream_unreachable
from .observability import UpstreamTrace
from .ratelimit import QUERY, RateLimiter, origin_of
from .settings import Settings

log = logging.getLogger("video_adapter.transport")


@dataclass
class UpstreamResult:
    status: int
    body: Any
    text: str
    headers: dict[str, str]
    #: 上游给的 `Retry-After`（秒）。没给或解析不出时为 `None` ——
    #: **不编一个默认值**，因为"上游没说"和"上游说等 1 秒"是两件事，
    #: 差别会在出口的 `Retry-After` 头上体现出来。
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def parse_retry_after(headers: dict[str, str]) -> float | None:
    """`Retry-After` → 秒。两种合法格式都认：**delta-seconds** 与 **HTTP-date**。

    上游文档只写了"响应头包含 `Retry-After`"，没写用哪种格式 —— 两种都解析，
    不猜。都解析不出时返回 `None`（调用方会保守地退避，而不是当成 0 立刻重发）。
    """
    raw = str(headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        delta = (when - _dt.datetime.now(tz=_dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:  # noqa: BLE001 - 上游给了个谁也解析不了的字符串
        return None


def _retry_after_seconds(headers: dict[str, str], default: float = 1.0) -> float:
    value = parse_retry_after(headers)
    return default if value is None else value


class UpstreamClient:
    def __init__(self, settings: Settings, limiter: RateLimiter | None = None) -> None:
        self._settings = settings
        #: 查询车道的主动限流器。`None` = 关闭（`RATE_LIMIT_ENABLED=false`）。
        self._limiter = limiter
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.request_timeout_seconds,
                trust_env=self._settings.upstream_trust_env,
                follow_redirects=False,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        idempotent: bool = False,
        trace: UpstreamTrace | None = None,
        rate_limit_lane: str | None = None,
    ) -> UpstreamResult:
        if self._client is None:
            await self.start()
        assert self._client is not None

        trace = trace or UpstreamTrace()
        report_bodies = bool(self._settings.obs_report_bodies)
        origin = origin_of(url)

        # ① 主动降频：在请求发出去**之前**决定"现在能不能发"。
        #    被挡下时不发上游请求 —— 那正是限流存在的目的（少发即是规避）。
        if rate_limit_lane == QUERY and self._limiter is not None:
            decision = await self._limiter.acquire(
                origin, wait_seconds=self._settings.rate_limit_wait_seconds
            )
            if not decision.granted:
                if decision.reason == "cooldown":
                    detail = "a 429 cooldown is active for this upstream"
                elif decision.reason == "fail_closed":
                    detail = (
                        "the shared rate-limit backend is unreachable and "
                        "RATE_LIMIT_FAIL_MODE=closed"
                    )
                else:
                    detail = (
                        f"no token within the {self._settings.rate_limit_wait_seconds:.0f}s "
                        "wait budget"
                    )
                log.warning(
                    "rate limiter blocked a query to %s (%s, retry after %ss) — upstream not called",
                    origin,
                    decision.reason,
                    decision.retry_after,
                )
                raise local_rate_limited(decision.retry_after, detail=detail)

        attempts = max(1, self._settings.upstream_retry_attempts)
        budget = max(0.0, float(self._settings.rate_limit_retry_budget_seconds))
        last: UpstreamResult | None = None
        for attempt in range(1, attempts + 1):
            # 请求侧属性写在 span 构造里：**任何失败路径都带着它们**
            # （连接失败、超时、429 都还看得到"我们发了什么"）。
            attributes = observability.upstream_request_attributes(
                method=method,
                url=url,
                headers=headers,
                body=json,
                attempt=attempt,
                idempotent=idempotent,
                trace=trace,
                report_bodies=report_bodies,
            )
            started = time.perf_counter()
            with observability.span(
                "upstream.call", secret=trace.credential, **attributes
            ) as handle:
                try:
                    response = await self._client.request(
                        method.upper(), url, json=json, headers=headers or {}
                    )
                except httpx.HTTPError as exc:
                    # 没收到应答 ⇒ 不写 upstream.response.status（伪造状态码会让
                    # "从没应答"与"上游回了 5xx"在上报里长得一样）。
                    handle.record_error(exc)
                    if not idempotent or attempt == attempts:
                        # 🔴 传输层失败必须**转成契约信封**再抛出：直接放 httpx 的异常上去，
                        # 调用方拿到的是裸的 `500 Internal Server Error` —— 不在码表里、
                        # 也没说清是连不上还是超时（实测踩到过）。
                        raise upstream_unreachable(exc) from exc
                    await asyncio.sleep(min(2.0, 0.25 * attempt))
                    continue

                header_map = {k.lower(): v for k, v in response.headers.items()}
                body: Any
                try:
                    body = response.json()
                except ValueError:
                    body = None
                result = UpstreamResult(
                    status=response.status_code,
                    body=body,
                    text=response.text[:2000],
                    headers=header_map,
                    retry_after=parse_retry_after(header_map),
                )
                for key, value in observability.upstream_response_attributes(
                    status=result.status,
                    headers=header_map,
                    body=body,
                    text=result.text,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    credential=trace.credential,
                    report_bodies=report_bodies,
                ).items():
                    handle.set_attribute(key, value)

            # ② 429 ⇒ 全局刹车。这一条是"限流按 IP"的直接推论：踩线的不是这一个任务，
            #    而是**整个出口**；只让当前请求退避，同 origin 的其他查询会继续撞墙。
            if result.status == 429:
                if result.retry_after is None:
                    # "上游没说"必须可见 —— 静默按 1s 处理会让人以为上游给了建议。
                    log.warning(
                        "upstream 429 from %s carried no usable Retry-After; assuming 1s", origin
                    )
                if self._limiter is not None:
                    applied, truncated = await self._limiter.note_rate_limited(
                        origin, result.retry_after if result.retry_after is not None else 1.0
                    )
                    log.warning(
                        "upstream 429 from %s — origin cooled down for %ss%s (attempt %d/%d)",
                        origin,
                        applied,
                        " [clamped by RATE_LIMIT_COOLDOWN_MAX_SECONDS]" if truncated else "",
                        attempt,
                        attempts,
                    )
                # ③ 只在预算内原地重试；超出预算就把 429 交上去（调用方按 Retry-After 退避）。
                wait = result.retry_after if result.retry_after is not None else min(1.0, budget)
                if attempt < attempts and wait <= budget:
                    await asyncio.sleep(wait)
                    last = result
                    continue
                return result

            retryable_5xx = idempotent and response.status_code >= 500
            if not retryable_5xx or attempt == attempts:
                return result
            last = result
            await asyncio.sleep(min(2.0, 0.25 * attempt))
        return last  # pragma: no cover - 循环内已 return
