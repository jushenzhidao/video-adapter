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
    """出口错误。`code` 决定 HTTP 状态与 `type`，除非显式覆盖。"""

    def __init__(
        self,
        message: str,
        code: str = "InvalidParameter",
        param: str | None = None,
        status: int | None = None,
        type_: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = code if code in REGISTRY else DEFAULT_CODE
        self.param = param
        default_status, default_type = REGISTRY[self.code]
        self.status = int(status) if status is not None else default_status
        self.type = type_ or default_type

    def envelope(self) -> dict:
        body = {"code": self.code, "message": self.message, "type": self.type}
        if self.param:
            body["param"] = self.param
        return {"error": body}

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


def from_upstream_http(status: int, message: str = "") -> AdapterError:
    """上游非 2xx → 出口错误。5xx 一律落 `InternalServiceError` 并以 **502** 出口（§10.2）。"""
    if status >= 500:
        return AdapterError(
            message or f"upstream returned {status}", code="InternalServiceError", status=502
        )
    code = UPSTREAM_HTTP_TO_CODE.get(status, "InternalServiceError")
    out_status = status if code != "InternalServiceError" else 502
    return AdapterError(message or f"upstream returned {status}", code=code, status=out_status)
