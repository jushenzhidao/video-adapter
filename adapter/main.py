"""ASGI 应用与生命周期。

**装配失败只降级不致命**的地方：可观测性未配置、协调器关闭、脚本仓库为空。
**硬依赖**：任务后端（缺 Redis 就是启动错误，绝不退化成内存，见 `taskstore.py`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import observability
from .api import adapter_error_handler, build_router
from .errors import AdapterError
from .queue import ConcurrencyGate
from .ratelimit import build_rate_limiter
from .settings import Settings
from .taskstore import build_store
from .tasks import TaskService
from .transport import UpstreamClient

log = logging.getLogger("video_adapter")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = build_store(settings)          # 缺 Redis ⇒ 这里抛错，不静默降级
        # 限流装配（§7.1）：上游按 IP 限 60 次/分钟，本层在**发出去之前**排队或让路。
        # 关闭时返回 None（传输层整段跳过），并在启动日志里说明"只剩被动退避"。
        limiter = build_rate_limiter(settings)
        if limiter is not None:
            # 探测共享后端（redis）。**失败只降级、不致命**（除非 fail_mode=closed）：
            # 退回进程内桶 + 告警 + `/healthz` 标记 degraded（见 ratelimit.py 模块 docstring）。
            await limiter.start()
        client = UpstreamClient(settings, limiter)
        await client.start()
        # 上报装配：**失败只降级**。没有 logfire / 没有 token / 拒绝宽头抓取，
        # 都只让「真的会外发」为 false，span 仍然构建并落到日志与本地 sink。
        app.state.observability = observability.setup_observability(settings)
        app.state.settings = settings
        app.state.store = store
        app.state.gate = ConcurrencyGate()
        # 给 /healthz 看：桶的状态与 scope 必须可查，否则"配了但没生效"无从判断。
        app.state.ratelimit = limiter
        app.state.service = TaskService(
            settings=settings, store=store, gate=app.state.gate, client=client
        )
        if not settings.task_key_fingerprint_secret:
            log.warning(
                "TASK_KEY_FINGERPRINT_SECRET is unset — credential fingerprints fall back to "
                "plain sha256 (offline-bruteforceable for low-entropy keys). Set it in production."
            )
        reconciler: asyncio.Task | None = None
        if settings.reconciler_enabled:
            reconciler = asyncio.create_task(_reconcile_loop(app))
        try:
            yield
        finally:
            if reconciler is not None:
                reconciler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reconciler
            await client.aclose()
            if limiter is not None:
                await limiter.close()
            await store.close()
            # 🔴 放在各资源 close **之后**：close 自己也会产生 span。
            # 批量导出挂在 daemon 线程上且不注册 atexit ⇒ 不 flush 就丢掉最后一批。
            observability.flush_spans()

    app = FastAPI(
        title="video-adapter",
        # 版本号的唯一来源是设置（镜像里由 APP_VERSION 注入）——写死字面量会让
        # /docs 上的版本与镜像 tag 各说各话（image-adapter 上真踩过）。
        version=settings.adapter_version,
        description=(
            "把任意视频生成上游适配为**火山方舟 Seedance** 原生异步任务协议。\n\n"
            "调用方只改 `Base URL` 与 `API Key`；多上游靠 `model = provider/model` 区分。"
        ),
        lifespan=lifespan,
    )
    app.add_exception_handler(AdapterError, adapter_error_handler)
    app.include_router(build_router(settings))

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """兜底：**任何**未预期异常也要以契约信封出去，绝不给裸 500。

        为什么必须有：调用方是按 `error.code` 做分支判断的，而裸的
        `500 Internal Server Error` 既不在 `docs/seedance-api-reference.md` 的码表里，
        也没有 request_id 可对账（实测踩到过：上游不可达时返回的就是它）。
        traceback 只进服务端日志；消息里只给异常类型名与 request_id ——
        不把 `str(exc)` 回给调用方（它可能含 URL、内部路径等）。
        """
        request_id = observability.current_request_id()
        log.exception(
            "unhandled %s on %s %s (request_id=%s)",
            type(exc).__name__,
            request.method,
            request.url.path,
            request_id,
        )
        error = AdapterError(
            f"unhandled {type(exc).__name__} inside the adapter; "
            f"see the service log (request_id={request_id})",
            code="InternalServiceError",
            type_="InternalServerError",
        )
        return JSONResponse(status_code=error.status, content=error.envelope())

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or ""
        # 调用方没给 id 也要有一条：上报与日志靠它把一次请求的 span 串起来。
        # 响应头仍然只在调用方给了 id 时回显 —— 不加协议里没有的字段。
        observability.bind_request_id(request_id or f"req-{uuid.uuid4().hex[:12]}")
        response = await call_next(request)
        if request_id:
            response.headers["x-request-id"] = request_id
        return response

    return app


async def _reconcile_loop(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.reconciler_interval_seconds)
        try:
            await app.state.service.reconcile_once()
        except Exception:  # noqa: BLE001 - 看门狗不能因为一轮失败就死掉
            log.exception("reconciler tick failed")


app = create_app()
