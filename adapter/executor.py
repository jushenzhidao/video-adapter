"""相位管线：调用脚本的相位函数，并按它给出的"请求计划"发一次上游调用。

脚本返回的计划形状（架构 §5.1）：

    {"method": "POST", "url": "https://…/api/v1/generate/seedance20",
     "body": {…}, "headers": {…}}

🔴 **脚本只能改路径，不能改源站。** 渠道的 `X-Upstream-Url` 定义了允许的 origin，
计划里的 URL 必须落在同一个 origin 上 —— 否则一个被投毒的脚本就能把凭证发到别处。
脚本是 pinned + review 过的，但这条防线不该只依赖"我们信任脚本"。

`rate_limit_lane` 由调用方（`tasks.py`）按语义给出：查询传 `QUERY`（上游按 IP 限的就是它），
创建 / 取消不传（不计入配额，但它们的 429 仍会回灌冷却）。见 `ratelimit.py`。
"""

from __future__ import annotations

import inspect
import time
from urllib.parse import urlsplit

from . import observability
from .ctx import Context
from .errors import channel_error, from_upstream_http
from .observability import UpstreamTrace
from .scriptstore import LoadedScript
from .transport import UpstreamClient, UpstreamResult

CREATE_PHASES = ("create_request", "create_response")
QUERY_PHASES = ("query_request", "query_response")
CANCEL_PHASES = ("cancel_request", "cancel_response")


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(str(url or ""))
    return (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port)


async def call_phase(script: LoadedScript, phase: str, ctx: Context, payload):
    """调用一个相位。脚本里写同步函数也可以。

    每个相位一条 `script.phase` span：翻译层与上游网络由此在 trace 上分开，
    "是脚本炸了还是上游慢了"不需要靠猜。属性只放相位名与耗时，**不放 payload**
    （payload 里可能有 base64 素材，正文由 `upstream.call` 承担）。
    """
    fn = script.namespace.get(phase)
    if not callable(fn):
        raise channel_error(f'script "{script.ref}" does not implement phase {phase!r}')
    started = time.perf_counter()
    with observability.span(
        "script.phase",
        secret=ctx.credential,
        **{
            "script.ref": script.ref,
            "script.digest": script.digest,
            "phase": phase,
            "request.id": ctx.request_id or observability.current_request_id(),
        },
    ) as handle:
        result = fn(ctx, payload)
        if inspect.isawaitable(result):
            result = await result
        handle.set_attribute("script.duration_ms", round((time.perf_counter() - started) * 1000, 2))
    return result


def _plan_to_request(plan, channel_url: str) -> tuple[str, str, dict, dict]:
    if not isinstance(plan, dict):
        raise channel_error("a request phase must return a dict request plan")
    method = str(plan.get("method") or "POST").upper()
    url = str(plan.get("url") or "")
    if not url:
        raise channel_error("a request phase must return a `url`")
    body = plan.get("body")
    headers = plan.get("headers") if isinstance(plan.get("headers"), dict) else {}

    if _origin(url) != _origin(channel_url):
        raise channel_error(
            "a request phase produced a url on a different origin than X-Upstream-Url "
            f"({_origin(url)} vs {_origin(channel_url)}); a script may vary the path, not the host"
        )
    return method, url, (body if isinstance(body, dict) else {}), {str(k): str(v) for k, v in headers.items()}


async def request_upstream(
    script: LoadedScript,
    request_phase: str,
    ctx: Context,
    payload,
    *,
    client: UpstreamClient,
    channel_url: str,
    auth_headers: dict[str, str],
    idempotent: bool,
    trace: UpstreamTrace | None = None,
    rate_limit_lane: str | None = None,
) -> tuple[UpstreamResult, dict]:
    """跑 `<x>_request` 相位 → 发一次上游调用 → 返回 (结果, 脚本给出的计划)。"""
    plan = await call_phase(script, request_phase, ctx, payload)
    method, url, body, extra_headers = _plan_to_request(plan, channel_url)
    headers = dict(auth_headers)
    headers.update(extra_headers)
    if body:
        headers["Content-Type"] = "application/json"

    # 上报留档：**请求全量**（method / url / body / 非凭证头），**不做脱敏**。
    # 唯一刻意不入档的是**凭证头的值** —— 它不在 body 里，而"密钥不入库"是独立于
    # 脱敏的一条纪律；任务记录里的 credential_id 指纹已能回答"当初是哪把钥匙"。
    archived_headers = dict(extra_headers)
    if body:
        archived_headers["Content-Type"] = "application/json"
    plan["sent"] = {
        "method": method,
        "url": url,
        "body": body if body else None,
        "headers": archived_headers or None,
    }

    result = await client.request(
        method, url, json=body if body else None, headers=headers,
        idempotent=idempotent, trace=_derive_trace(request_phase, ctx, trace),
        rate_limit_lane=rate_limit_lane,
    )
    return result, plan


def _derive_trace(
    request_phase: str, ctx: Context, trace: "UpstreamTrace | None"
) -> UpstreamTrace:
    """调用方没给上报上下文时**从 ctx 推**（`create_request` → `create` …）。

    这是防"接线漏掉一条路由"的兜底：漏传时 span 少了 phase / provider / 上游 task id，
    trace 会静默退化，而没人会为此收到报错。宁可在这里推一次。
    """
    if trace is not None:
        return trace
    task = ctx.task
    return UpstreamTrace(
        phase=request_phase.split("_", 1)[0],
        provider=str((ctx.options or {}).get("provider") or ""),
        upstream_task_id=(task.upstream_task_id if task else ""),
        credential=ctx.credential,
    )


def raise_for_status(result: UpstreamResult, *, phase: str) -> None:
    """非 2xx → 出口错误。

    2xx 上仍可能是失败信封（上游文档只给了 `{"status":"FAILED","message":…}`，
    **没给 HTTP 状态码**）—— 那是 `*_response` 相位的职责。

    429 会把上游的 `Retry-After` 一起带出去（`errors.from_upstream_http`），
    否则调用方拿到一个 429 却不知道该等多久。
    """
    if result.ok:
        return
    message = ""
    if isinstance(result.body, dict):
        message = str(result.body.get("message") or "")
    if not message:
        message = f"upstream returned HTTP {result.status} during {phase}"
    raise from_upstream_http(result.status, message, retry_after=result.retry_after)
