"""四条路由 + 错误信封 + `/healthz`。

路由形态与 `model` 的约定见架构 §2：

    POST   /api/v3/contents/generations/tasks        → {"id": "cgt-…"}   （**只回 id**）
    GET    /api/v3/contents/generations/tasks/{id}   → 原生任务对象（六态，**不含 model**）
    GET    /api/v3/contents/generations/tasks        → {"items": […], "total": N}
    DELETE /api/v3/contents/generations/tasks/{id}   → 取消（仅 queued）/ 删除记录
    GET    /healthz

路由**只有火山原生这一族**（不加前缀、不加路径段）；多上游靠 `model = provider/model` 区分。

🔴 **响应体只含原生字段**（形状见 `adapter/seedance.py`）：创建只回 `id`、
查询不含 `model` 与任何诊断块。上游 task id / 脚本摘要 / 请求响应留档 /
`requested` / `effective` / 降级告警**只走 logfire**（服务层 `task.snapshot` span）。
本文件的 span 因此只负责**路由层的三个事实**：谁（provider / request.id）、
对哪个任务（task.id）、最后什么状态（task.status）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from .channel import ChannelConfig, parse_channel
from .errors import AdapterError
from .settings import Settings
from .tasks import TaskService

from . import observability

log = logging.getLogger("video_adapter.api")

TASKS_PATH = "/api/v3/contents/generations/tasks"


def build_router(settings: Settings) -> APIRouter:
    """只依赖 settings + `app.state`；**不在导入期 touch 任务后端**（/docs 可离线生成）。"""
    router = APIRouter()

    def _service(request: Request) -> TaskService:
        service = getattr(request.app.state, "service", None)
        if service is None:
            raise AdapterError(
                "service is not initialised yet", code="InternalServiceError", status=503
            )
        return service

    async def channel(request: Request) -> ChannelConfig:
        # 用 request.headers 解析，而不是逐个 Header 参数：头的拼写大小写不该影响结果。
        # 下面那些声明的作用是让 /docs 能直接试调（契约表与实现不漂移）。
        return parse_channel(request.headers, settings)

    @router.post(TASKS_PATH, summary="创建视频生成任务")
    async def create_task(
        request: Request,
        payload: dict[str, Any] = Body(...),
        channel_config: ChannelConfig = Depends(channel),
        x_adapter_key: str | None = Header(None, alias="X-Adapter-Key", description="本服务准入密钥"),
        x_upstream_url: str | None = Header(None, alias="X-Upstream-Url", description="上游地址"),
        x_script_ref: str | None = Header(None, alias="X-Script-Ref", description="脚本命名引用（唯一接受的来源）"),
        x_script: str | None = Header(None, alias="X-Script", description="内联脚本 —— 本部署拒绝"),
        x_script_64: str | None = Header(None, alias="X-Script-64", description="base64 内联脚本 —— 本部署拒绝"),
        x_upstream_method: str | None = Header(None, alias="X-Upstream-Method"),
        x_auth_emit: str | None = Header(None, alias="X-Auth-Emit", description="凭证发射位置，如 header:key:"),
        x_channel_options: str | None = Header(None, alias="X-Channel-Options", description="JSON：provider / max_credits / max_concurrency / model …"),
        x_script_sha256: str | None = Header(None, alias="X-Script-Sha256", description="脚本完整性锁定"),
        authorization: str | None = Header(None, alias="Authorization", description="上游厂商凭证（原样转发或按 X-Auth-Emit 改造）"),
        x_dry_run: str | None = Header(None, alias="X-Dry-Run", description="1 = 跑完整翻译但不提交上游"),
    ) -> dict:
        dry_run = str(x_dry_run or "").strip() in ("1", "true", "yes", "on") or _body_dry_run(payload)
        with observability.span(
            "task.create",
            secret=channel_config.credential,
            **{
                "task.provider": channel_config.provider or "",
                "task.model": str(payload.get("model") or ""),
                "task.script_ref": channel_config.script_ref,
                "task.dry_run": dry_run,
                "request.id": _request_id(request) or observability.current_request_id(),
            },
        ) as handle:
            result = await _service(request).create(
                channel_config, payload, request_id=_request_id(request), dry_run=dry_run
            )
            # 响应体只剩 `id`（原生契约）⇒ 路由层能记的也只有它。**上游 task id /
            # 脚本摘要 / 请求响应留档 / 实际生效值全部由服务层那条 `task.snapshot`
            # span 承担** —— 只有那里拿得到完整的任务记录。
            if isinstance(result, dict) and result.get("id"):
                handle.set_attribute("task.id", result["id"])
        return result

    @router.get(TASKS_PATH + "/{task_id}", summary="查询视频生成任务")
    async def get_task(
        task_id: str,
        request: Request,
        channel_config: ChannelConfig = Depends(channel),
        x_adapter_key: str | None = Header(None, alias="X-Adapter-Key"),
        x_upstream_url: str | None = Header(None, alias="X-Upstream-Url"),
        x_script_ref: str | None = Header(None, alias="X-Script-Ref"),
        x_script: str | None = Header(None, alias="X-Script"),
        x_script_64: str | None = Header(None, alias="X-Script-64"),
        x_upstream_method: str | None = Header(None, alias="X-Upstream-Method"),
        x_auth_emit: str | None = Header(None, alias="X-Auth-Emit"),
        x_channel_options: str | None = Header(None, alias="X-Channel-Options"),
        x_script_sha256: str | None = Header(None, alias="X-Script-Sha256"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        with observability.span(
            "task.query",
            secret=channel_config.credential,
            **{
                "task.id": task_id,
                "task.provider": channel_config.provider or "",
                "request.id": _request_id(request) or observability.current_request_id(),
            },
        ) as handle:
            result = await _service(request).get(
                channel_config, task_id, request_id=_request_id(request)
            )
            # 路由层只记**原生字段**里对排障有用的那两个：状态与产物。
            # 上游 task id、`requested` / `effective` / 降级告警在 `task.snapshot` 上。
            handle.set_attribute("task.status", result.get("status"))
            video_url = (result.get("content") or {}).get("video_url")
            if video_url:
                handle.set_attribute("task.artifacts.video_url", video_url)
        return result

    @router.get(TASKS_PATH, summary="查询视频生成任务列表")
    async def list_tasks(
        request: Request,
        channel_config: ChannelConfig = Depends(channel),
        page_num: int = Query(1, ge=1, le=500),
        page_size: int = Query(20, ge=1, le=500),
        x_adapter_key: str | None = Header(None, alias="X-Adapter-Key"),
        x_upstream_url: str | None = Header(None, alias="X-Upstream-Url"),
        x_script_ref: str | None = Header(None, alias="X-Script-Ref"),
        x_script: str | None = Header(None, alias="X-Script"),
        x_script_64: str | None = Header(None, alias="X-Script-64"),
        x_upstream_method: str | None = Header(None, alias="X-Upstream-Method"),
        x_auth_emit: str | None = Header(None, alias="X-Auth-Emit"),
        x_channel_options: str | None = Header(None, alias="X-Channel-Options"),
        x_script_sha256: str | None = Header(None, alias="X-Script-Sha256"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        with observability.span(
            "task.list",
            secret=channel_config.credential,
            **{
                "task.provider": channel_config.provider or "",
                "task.list.page_num": page_num,
                "task.list.page_size": page_size,
                "request.id": _request_id(request) or observability.current_request_id(),
            },
        ) as handle:
            result = await _service(request).list(
                channel_config, page_num=page_num, page_size=page_size
            )
            handle.set_attribute("task.list.total", result.get("total"))
            items = result.get("items") or []
            # 仅**摘要**（id + status）：列表页可能 500 条，逐条上报摘要会撑大 trace，
            # 而"我看得见谁"这件事由 total 与 id 列表回答就够（详情在各自的查询 span 上）。
            handle.set_attribute(
                "task.list.items",
                [{"id": i.get("id"), "status": i.get("status")} for i in items],
            )
        return result

    @router.delete(TASKS_PATH + "/{task_id}", summary="取消或删除视频生成任务")
    async def delete_task(
        task_id: str,
        request: Request,
        channel_config: ChannelConfig = Depends(channel),
        x_adapter_key: str | None = Header(None, alias="X-Adapter-Key"),
        x_upstream_url: str | None = Header(None, alias="X-Upstream-Url"),
        x_script_ref: str | None = Header(None, alias="X-Script-Ref"),
        x_script: str | None = Header(None, alias="X-Script"),
        x_script_64: str | None = Header(None, alias="X-Script-64"),
        x_upstream_method: str | None = Header(None, alias="X-Upstream-Method"),
        x_auth_emit: str | None = Header(None, alias="X-Auth-Emit"),
        x_channel_options: str | None = Header(None, alias="X-Channel-Options"),
        x_script_sha256: str | None = Header(None, alias="X-Script-Sha256"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        with observability.span(
            "task.cancel",
            secret=channel_config.credential,
            **{
                "task.id": task_id,
                "task.provider": channel_config.provider or "",
                "request.id": _request_id(request) or observability.current_request_id(),
            },
        ) as handle:
            result = await _service(request).delete(
                channel_config, task_id, request_id=_request_id(request)
            )
            handle.set_attribute("task.status", result.get("status") or "cancelled")
            handle.set_attribute("task.deleted", bool(result.get("deleted")))
        return result

    @router.get("/healthz", summary="存活探针")
    async def healthz(request: Request, deep: int = Query(0)) -> dict:
        state = request.app.state
        store = getattr(state, "store", None)
        gate = getattr(state, "gate", None)
        limiter = getattr(state, "ratelimit", None)
        observation = getattr(state, "observability", None) or observability.observation_state()
        # 限流器状态。⚠️ 先看 `scope`：`shared`（redis 桶，跨进程共享）还是 `process`
        # （进程内桶 ⇒ 多 worker / 多副本时实际配额会按进程数放大）。
        # `backend.degraded=true` 表示主后端不可用、当前走的是降级层（`scope` 也会如实退回 process）。
        rate_limit_state = (
            await limiter.snapshot() if limiter is not None else {"enabled": False}
        )
        body = {
            "status": "ok",
            # 镜像版本（CI 构建时注入）：值班第一句话是"跑的是哪个版本"
            "version": settings.adapter_version,
            "task_store": getattr(store, "backend", None),
            "queue": gate.stats() if gate is not None else None,
            "rate_limit": rate_limit_state,
            "credential_fingerprint": settings.fingerprint_algorithm,
            # 「已配置」与「真的会外发」必须分成两个字段：合成一个会让运维误判。
            # `ready`/`reason` 是第三件事：装配**成没成**、以及为什么不成（不静默）。
            "logfire": observation.as_health(),
        }
        if deep:
            body["script_store"] = settings.script_store_dir
            body["reconciler"] = {"enabled": settings.reconciler_enabled}
            body["observability"] = {
                "upstream_bodies": observation.report_bodies,
                "body_max_chars": observation.body_max_chars,
            }
        return body

    @router.get("/files/{name}", summary="转存后的产物", include_in_schema=False)
    async def serve_media(name: str):
        """把转存到本服务的产物发出去（rehost 的 local 后端）。

        名字必须匹配 `sha256(url)[:24] + 扩展名` 的形状 —— 这既是幂等键，
        也是**路径穿越的防线**（不合法一律 404，不泄露存储布局）。
        """
        from fastapi.responses import FileResponse

        from . import media

        path = media.local_path(name, settings)
        return FileResponse(path, media_type=media.content_type_for(name))

    return router


def _body_dry_run(payload: dict[str, Any]) -> bool:
    if payload.get("dry_run"):
        return True
    extra = payload.get("extra_body")
    return bool(isinstance(extra, dict) and extra.get("dry_run"))


def _request_id(request: Request) -> str:
    return str(request.headers.get("x-request-id") or "")[:128]


async def adapter_error_handler(request: Request, exc: AdapterError):
    log.warning("%s %s → %s %s", request.method, request.url.path, exc.status, exc.code)
    response = JSONResponse(status_code=exc.status, content=exc.envelope())
    retry_after = exc.retry_after_header
    if retry_after:
        # 429 **必须**告诉调用方该等多久。上游用 `Retry-After` 表达同一件事
        # （`upstreams/aivideomaker-official-api.md` §6），我们把上游给的值、或本地桶
        # 算出的值透出去。不透出去，调用方只能自己猜间隔 —— 猜小了会持续撞线。
        response.headers["Retry-After"] = retry_after
    return response
