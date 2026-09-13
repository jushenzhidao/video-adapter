"""错误体系：出口信封（Seedance 形状）+ 上游/本地错误的 code 落位。

**不发明新 code** —— 出口只用 `docs/seedance-api-reference.md` §8 那两个表里已有的值，
因为那是调用方已经写好分支判断的一份。
"""

from __future__ import annotations

#: code → (HTTP 状态, 信封里的 `type`)
REGISTRY: dict[str, tuple[int, str]] = {
    "MissingParameter": (400, "InvalidRequest"),
    "InvalidParameter": (400, "InvalidRequest"),
    # 渠道头配错是**运维**的问题。image-adapter 用 400 + 这个 code，这里沿用：
    # 报错层次错了（报成 InvalidParameter）会把人送去查调用方的请求体。
    "channel_config_error": (400, "InvalidRequest"),
    "AuthenticationError": (401, "Unauthorized"),
    "AccessDenied": (403, "Forbidden"),
    "AccountOverdueError": (403, "Forbidden"),
    "InvalidEndpoint.NotFound": (404, "NotFound"),
    "InvalidEndpointOrModel.NotFound": (404, "NotFound"),
    "ModelNotOpen": (404, "NotFound"),
    "RateLimitExceeded.EndpointRPMExceeded": (429, "TooManyRequests"),
    "RateLimitExceeded.ModelAccountRpmExceeded": (429, "TooManyRequests"),
    "QuotaExceeded": (429, "TooManyRequests"),
    "ServerOverloaded": (429, "TooManyRequests"),
    "InternalServiceError": (500, "InternalServerError"),
    "UpstreamUnavailable": (502, "InternalServerError"),
}

DEFAULT_CODE = "InternalServiceError"


class AdapterError(Exception):
    """出口错误。`code` 决定 HTTP 状态与 `type`，除非显式覆盖。

    `retry_after`（秒）在 429 上**必须**给：调用方要靠它决定退避多久，
    而上游自己也是用 `Retry-After` 表达同一件事的（见 `upstreams/aivideomaker-official-api.md` §6）。
    不给的话调用方只能瞎猜间隔，反而更容易持续撞线。
    """

    def __init__(
        self,
        message: str,
        code: str = "InvalidParameter",
        param: str | None = None,
        status: int | None = None,
        type_: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = code if code in REGISTRY else DEFAULT_CODE
        self.param = param
        self.retry_after = retry_after
        default_status, default_type = REGISTRY[self.code]
        self.status = int(status) if status is not None else default_status
        self.type = type_ or default_type

    def envelope(self) -> dict:
        body = {"code": self.code, "message": self.message, "type": self.type}
        if self.param:
            body["param"] = self.param
        return {"error": body}

    @property
    def retry_after_header(self) -> str | None:
        """`Retry-After` 头的值（秒）。`None` = 不该加这个头。

        **向上取整**：HTTP 的 `Retry-After` 是秒粒度，宁可让调用方多等一点，
        也不要让它提前回来撞线（提前回来只会再吃一个 429，白白消耗配额）。
        """
        if self.retry_after is None:
            return None
        whole = int(self.retry_after)
        if float(self.retry_after) > whole:
            whole += 1
        return str(max(1, whole))

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"{self.code}({self.status}): {self.message}"


def channel_error(message: str) -> AdapterError:
    """渠道配置错误 —— 指向运维，不指向调用方。"""
    return AdapterError(message, code="channel_config_error")


def task_not_found(task_id: str = "") -> AdapterError:
    """任务不存在 / 不属于本次凭证。

    ⚠️ 刻意与"未知 provider"共用 `InvalidEndpoint.NotFound`：两者都不该泄露
    "它其实存在，只是不属于你"。取这个 code 而不是新造一个，见模块 docstring。
    """
    suffix = f": {task_id}" if task_id else ""
    return AdapterError(f"task not found{suffix}", code="InvalidEndpoint.NotFound")


#: 上游 HTTP 状态 → 出口 code（上游文档未给出错误状态码，所以这里按通用语义映射；
#: 2xx 上的 `{"status":"FAILED"}` 信封由脚本的 response 相位处理）。
UPSTREAM_HTTP_TO_CODE: dict[int, str] = {
    400: "InvalidParameter",
    401: "AuthenticationError",
    403: "AccountOverdueError",
    404: "InvalidEndpointOrModel.NotFound",
    409: "InvalidParameter",
    422: "InvalidParameter",
    429: "RateLimitExceeded.ModelAccountRpmExceeded",
}


def from_upstream_http(
    status: int, message: str = "", retry_after: float | None = None
) -> AdapterError:
    """上游非 2xx → 出口错误。5xx 一律落 `InternalServiceError` 并以 **502** 出口（§10.2）。

    `retry_after` 只在 429 上有意义：上游既然给了这个头，就没有理由把它吞掉 ——
    调用方要靠它决定退避多久（上游文档 §6 就是这么用的）。
    """
    if status >= 500:
        return AdapterError(
            message or f"upstream returned {status}", code="InternalServiceError", status=502
        )
    code = UPSTREAM_HTTP_TO_CODE.get(status, "InternalServiceError")
    out_status = status if code != "InternalServiceError" else 502
    return AdapterError(
        message or f"upstream returned {status}",
        code=code,
        status=out_status,
        retry_after=retry_after,
    )


#: 传输层异常类型 → 给调用方看的一句人话（**不含 URL / 凭证**）。
#: ⚠️ 刻意不把 `str(exc)` 放进信封：httpx 的异常消息里带整条 URL，
#: 而 URL 可能含凭证（渠道可以把凭证放进查询串）—— 那会让"给调用方看的错误"
#: 同时泄露凭证明文到**日志**里。细节留在 span（已打码）与服务端 traceback 里。
_UNREACHABLE_HINTS: dict[str, str] = {
    "ConnectError": "connection refused / DNS failure",
    "ConnectTimeout": "connect timed out",
    "ReadTimeout": "read timed out (upstream accepted but did not answer)",
    "WriteTimeout": "write timed out",
    "PoolTimeout": "connection pool exhausted",
    "RemoteProtocolError": "upstream closed the connection mid-response",
    "ReadError": "connection reset while reading the response",
}


def upstream_unreachable(exc: BaseException) -> AdapterError:
    """连不上上游 / 超时 / 连接中断 → **502 `UpstreamUnavailable`**。

    这类失败**没有 HTTP 应答**，所以没有上游状态码可用（§10.2 的映射表不适用）。
    不转换的后果是调用方拿到裸的 `500 Internal Server Error` —— 既不在码表里，
    也不含可判别信息（实测踩到过：本地假上游端口写错，返回的就是它）。
    """
    kind = type(exc).__name__
    hint = _UNREACHABLE_HINTS.get(kind, "transport-level failure")
    return AdapterError(f"cannot reach the upstream: {kind} ({hint})", code="UpstreamUnavailable", status=502)


def local_rate_limited(retry_after: float, detail: str = "") -> AdapterError:
    """**本地**主动限流把请求挡下了 —— 上游**一次都没被调用**。

    与 `from_upstream_http(429)` 的区别只在 message 里说清"是谁在限"：出口形状相同
    （契约里 429 就一种），但排障方向正好相反 ——
    一个要去看上游的配额，一个要看本层的限流配置。混成同一句话会把人送去查错方向。
    """
    why = f" ({detail})" if detail else ""
    return AdapterError(
        f"query rate limit reached locally; the upstream was not called{why}",
        code="RateLimitExceeded.ModelAccountRpmExceeded",
        retry_after=retry_after,
    )
