"""上游 HTTP 客户端。

**窄重试通道**（架构 §7）：只重发"能确认请求被拒"的调用。

    创建 / 取消（非幂等）  仅重试 **429** —— 429 表示请求被拒，没有计费；
                          5xx / 超时 / 不可达一律**不重发**（可能已被受理，重发即二次计费）。
    查询（幂等 GET）      429 与 5xx / 网络错误都可重试。

默认**不信环境代理**（`trust_env=False`）：macOS 的 `scutil` 代理会把回环地址也代理走，
表现为拿到一个网关错误体而不是 connection refused —— 那会把"本地假上游"的排障带偏。

**这里也是上报的唯一收口**：每一次上游调用（含重试的每一次）都开一条 `upstream.call`
子 span，带发出去的 request 与收到的 response 原文，以及上游 task id（能从调用上下文拿到时）。
放在收口处而不是各调用点，是为了让"哪条路由漏了埋点"这种问题不可能发生。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import observability
from .observability import UpstreamTrace
from .settings import Settings


@dataclass
class UpstreamResult:
    status: int
    body: Any
    text: str
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _retry_after_seconds(headers: dict[str, str], default: float = 1.0) -> float:
    raw = str(headers.get("retry-after") or "").strip()
    if not raw:
        return default
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
    except Exception:  # noqa: BLE001
        return default


class UpstreamClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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
    ) -> UpstreamResult:
        if self._client is None:
            await self.start()
        assert self._client is not None

        trace = trace or UpstreamTrace()
        report_bodies = bool(self._settings.obs_report_bodies)

        attempts = max(1, self._settings.upstream_retry_attempts)
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
                        raise
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

            retryable = response.status_code == 429 or (idempotent and response.status_code >= 500)
            if not retryable or attempt == attempts:
                return result
            last = result
            delay = _retry_after_seconds(header_map, default=min(8.0, 0.5 * (2 ** (attempt - 1))))
            await asyncio.sleep(min(delay, 10.0))
        return last  # pragma: no cover - 循环内已 return
