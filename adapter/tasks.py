"""任务编排：id 生成、凭证指纹、状态机、惰性查询、回调推送、超时看门狗。

三条不可协商的规则：

1. **创建只返回 `{"id": …}`**，没有 status（Seedance 契约）—— 必须轮询或走回调。
2. **任务与凭证绑定**（§6.3b）：任务记录里存创建时那把 Key 的**指纹**，
   查询/取消时指纹不符 ⇒ **本地 404**，根本不发上游请求。
3. **锁链要闭环**：并发槽位从创建占到终态；每个失败出口都必须释放它。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import logging
import random
import string
from typing import Any, Mapping

from . import media, observability, scriptstore
from .channel import ChannelConfig, build_auth_headers
from .ctx import Context, TaskView
from .errors import AdapterError, task_not_found
from .executor import (
    CANCEL_PHASES,
    CREATE_PHASES,
    QUERY_PHASES,
    call_phase,
    raise_for_status,
    request_upstream,
)
from .normalize import normalize_payload
from .queue import ConcurrencyGate
from .settings import Settings
from .taskstore import TERMINAL_STATUSES, TaskStore
from .transport import UpstreamClient

log = logging.getLogger("video_adapter.tasks")

DEFAULT_EXECUTION_EXPIRES_AFTER = 172800  # 上游文档默认 48h
_ID_ALPHABET = string.ascii_lowercase + string.digits


def new_task_id(now: _dt.datetime | None = None) -> str:
    """`cgt-<YYYYMMDDHHMMSS>-<5~8 位随机>` —— 模仿原厂，让调用方的正则与日志关联不变。"""
    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d%H%M%S")
    suffix = "".join(random.choice(_ID_ALPHABET) for _ in range(6))
    return f"cgt-{stamp}-{suffix}"


def fingerprint(credential: str, settings: Settings) -> str:
    """凭证指纹。**要 HMAC 不要裸 sha256** —— API Key 是低熵可枚举空间，裸哈希可离线爆破。"""
    secret = settings.task_key_fingerprint_secret
    if secret:
        digest = hmac.new(secret.encode("utf-8"), credential.encode("utf-8"), hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    return f"{settings.fingerprint_algorithm}:{digest}"


def resolve_provider(model: str, channel: ChannelConfig) -> tuple[str, str]:
    """从 `model = provider/model` 解出 (provider, 裸模型名)，并**断言与渠道声明一致**。

    这条断言专门消灭"模型名写错/漏改 → 静默落到别的上游 → **直接付费**"。
    裸模型名走渠道声明的 provider；渠道没声明 ⇒ 400，**不猜**。
    """
    raw = str(model or "").strip()
    if not raw:
        raise AdapterError("model is required", code="MissingParameter", param="model")

    if "/" in raw:
        prefix, bare = raw.split("/", 1)
        prefix, bare = prefix.strip(), bare.strip()
        if not bare:
            raise AdapterError(f'model "{raw}" has an empty model name', code="InvalidParameter", param="model")
        declared = channel.provider
        if declared is None:
            raise AdapterError(
                f'model "{raw}" names provider "{prefix}" but this channel declares no '
                "`X-Channel-Options.provider`; the assertion cannot be checked",
                code="InvalidParameter",
                param="model",
            )
        if prefix != declared:
            raise AdapterError(
                f'model provider "{prefix}" does not match the channel provider "{declared}"',
                code="InvalidParameter",
                param="model",
            )
        return declared, bare

    if channel.provider is None:
        raise AdapterError(
            f'model "{raw}" has no provider prefix and this channel declares no '
            "`X-Channel-Options.provider`; refusing to guess which upstream to bill",
            code="InvalidParameter",
            param="model",
        )
    return channel.provider, raw


class TaskService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: TaskStore,
        gate: ConcurrencyGate,
        client: UpstreamClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.gate = gate
        self.client = client
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 创建
    async def create(
        self,
        channel: ChannelConfig,
        payload: Mapping[str, Any],
        *,
        request_id: str = "",
        dry_run: bool = False,
    ) -> dict:
        # 前门归一化（架构 §3）：弱校验后缀剥离 + content[] 的 role / 首尾帧归一化。
        # **必须在脚本之前** —— 否则上游会收到一段带 `--rs 720p` 的污染提示词，
        # 画面里可能真的出现这些文字。
        payload, normalize_warnings = normalize_payload(payload)

        provider, bare_model = resolve_provider(payload.get("model"), channel)
        credential_id = fingerprint(channel.credential, self.settings)

        script = scriptstore.load(
            self.settings, channel.script_ref, expected_sha256=channel.script_sha256
        )
        declared = scriptstore.phases(script)
        for phase in CREATE_PHASES:
            if phase not in declared:
                raise AdapterError(
                    f'script "{script.ref}" does not declare {phase!r}', code="channel_config_error"
                )

        local_id = new_task_id()
        gate_key = f"{provider}:{credential_id}"
        limit = channel.max_concurrency or self.settings.default_max_concurrency
        ctx = Context(
            options=dict(channel.options),
            upstream_url=channel.upstream_url,
            request_id=request_id,
            credential=channel.credential,
        )

        # dry_run：跑完整翻译（含本地校验与计费估算），**不占槽位、不提交、不落任务**。
        if dry_run:
            plan = await call_phase(script, "create_request", ctx, dict(payload))
            return {
                "dry_run": True,
                "upstream": {
                    "method": str((plan or {}).get("method") or channel.upstream_method),
                    "url": str((plan or {}).get("url") or ""),
                    "body": (plan or {}).get("body"),
                },
                "provider": provider,
                "model": bare_model,
                "script_ref": script.ref,
                "requested": (ctx.plan or {}).get("requested"),
                "effective": (ctx.plan or {}).get("effective"),
                "warnings": (ctx.plan or {}).get("warnings", []),
                "unsupported": (ctx.plan or {}).get("unsupported", []),
            }

        acquired = await self.gate.acquire(
            gate_key, limit, local_id, self.settings.queue_wait_seconds
        )
        if not acquired:
            raise AdapterError(
                f"channel concurrency gate is full ({limit} slots held until tasks finish); "
                "retry later",
                code="ServerOverloaded",
                status=429,
            )

        try:
            result, request_plan = await request_upstream(
                script,
                "create_request",
                ctx,
                dict(payload),
                client=self.client,
                channel_url=channel.upstream_url,
                auth_headers=build_auth_headers(channel),
                idempotent=False,
            )
            raise_for_status(result, phase="create")
            created = await call_phase(script, "create_response", ctx, result.body)
            upstream_task_id = str((created or {}).get("task_id") or "")
            if not upstream_task_id:
                raise AdapterError(
                    "the create_response phase returned no task_id", code="InternalServiceError"
                )
        except Exception:
            await self.gate.release(gate_key, local_id)
            raise

        plan = ctx.plan or {}
        now = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
        expires_after = int(payload.get("execution_expires_after") or DEFAULT_EXECUTION_EXPIRES_AFTER)
        record = {
            "local_id": local_id,
            "upstream_task_id": upstream_task_id,
            "provider": provider,
            "credential_id": credential_id,
            "credential_algo": self.settings.fingerprint_algorithm,
            "script_ref": script.ref,
            "script_digest": script.digest,
            "model": payload.get("model"),
            "bare_model": bare_model,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self.settings.task_retention_days * 86400,
            "execution_expires_at": now + expires_after,
            "gate_key": gate_key,
            "callback_url": payload.get("callback_url"),
            "request": dict(payload),
            "requested": plan.get("requested"),
            "effective": plan.get("effective"),
            "warnings": normalize_warnings + list(plan.get("warnings") or []),
            "unsupported": list(plan.get("unsupported") or []),
            # 转存开关按渠道落库：查询相位要用（那时只有任务记录，没有渠道头）
            "rehost": bool(channel.options.get("rehost")),
            "rehost_result": None,
            # 上报留档：**请求/响应全量，不做脱敏**。
            # 唯一刻意排除的是**凭证头的值** —— 它不在 body 里，而"密钥不入库"是
            # 独立于脱敏的一条纪律；记录里的 `credential_id` 指纹已能回答"当初是哪把钥匙"。
            "upstream_report": {
                "request": request_plan.get("sent"),
                "response": {
                    "status": result.status,
                    "body": result.body,
                    "headers": result.headers,
                },
                "query_count": 0,
            },
            "view": None,
            "upstream": result.body,
        }
        await self.store.put(record)
        log.info(
            "task created local_id=%s provider=%s model=%s upstream_task_id=%s",
            local_id,
            provider,
            bare_model,
            upstream_task_id,
        )
        return {"id": local_id, **self._report(record)}

    # ------------------------------------------------------------------ 查询
    async def get(self, channel: ChannelConfig, local_id: str, *, request_id: str = "") -> dict:
        record = await self._authorize(channel, local_id)
        if record["status"] in TERMINAL_STATUSES:
            return self._render(record)

        script = scriptstore.load(
            self.settings, channel.script_ref, expected_sha256=channel.script_sha256
        )
        ctx = Context(
            options=dict(channel.options),
            upstream_url=channel.upstream_url,
            request_id=request_id,
            credential=channel.credential,
            task=TaskView(
                upstream_task_id=record.get("upstream_task_id", ""),
                status=record.get("status", ""),
                model=record.get("model", ""),
                request=record.get("request") or {},
                created_at=record.get("created_at"),
            ),
        )
        try:
            result, _plan = await request_upstream(
                script,
                "query_request",
                ctx,
                {"id": local_id},
                client=self.client,
                channel_url=channel.upstream_url,
                auth_headers=build_auth_headers(channel),
                idempotent=True,
            )
            raise_for_status(result, phase="query")
            fragment = await call_phase(script, "query_response", ctx, result.body)
        except AdapterError as exc:
            # 上游 404 可能意味着"上游侧任务没了"；不能因此丢掉本地记录（契约要 7 天可用），
            # 但也**不能假装任务还在跑**。如实把失败上报，任务留在本地。
            log.warning("query failed for %s: %s", local_id, exc)
            raise

        return await self._apply_fragment(record, fragment, raw=result.body)

    async def _apply_fragment(self, record: dict, fragment: dict, *, raw: Any = None) -> dict:
        fragment = dict(fragment or {})
        previous = record.get("status")
        status = str(fragment.get("status") or previous or "running")
        now = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())

        # 超时看门狗：上游没有 expired 态，由本层兜底（§6.5）
        if (
            status not in TERMINAL_STATUSES
            and record.get("execution_expires_at")
            and now >= int(record["execution_expires_at"])
        ):
            status = "expired"
            fragment.setdefault("error", {"code": "Expired", "message": "task exceeded execution_expires_after"})

        # 上报：查询侧的响应也留档（**不脱敏**）+ 计数
        report = dict(record.get("upstream_report") or {})
        report["query_count"] = int(report.get("query_count") or 0) + 1
        report["last_query_at"] = now
        if raw is not None:
            report["query_response"] = {"body": raw}

        warnings = list(record.get("warnings") or [])
        updates: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "view": fragment,
            "upstream": raw if raw is not None else record.get("upstream"),
            "upstream_report": report,
        }

        # 转存（逐渠道开关 `rehost`）：只在**成功终态**做，且同一个产物只做一次
        if (
            status == "succeeded"
            and record.get("rehost")
            and not (record.get("rehost_result") or {}).get("ok")
        ):
            moved = await media.rehost(
                str(fragment.get("video_url") or ""),
                settings=self.settings,
                base=self.settings.public_base_url,
            )
            updates["rehost_result"] = moved
            if moved.get("ok"):
                if not self.settings.public_base_url:
                    warnings.append(
                        "rehost is on but PUBLIC_BASE_URL is unset, so content.video_url is "
                        "a relative path; set PUBLIC_BASE_URL to hand out absolute urls"
                    )
            else:
                # 转存是增强，不该让一个已经成功的任务看起来失败 —— 降级 + 如实告知
                warnings.append(
                    f"rehost failed; serving the upstream url instead ({moved.get('error')})"
                )
        if warnings != list(record.get("warnings") or []):
            updates["warnings"] = warnings

        record = await self.store.update(record["local_id"], **updates) or record

        if status in TERMINAL_STATUSES and previous not in TERMINAL_STATUSES:
            await self.gate.release(record.get("gate_key", ""), record["local_id"])
            if status != "cancelled":
                self._schedule_callback(record, fragment)
        return self._render(record)

    # ------------------------------------------------------------------ 取消 / 删除
    async def delete(self, channel: ChannelConfig, local_id: str, *, request_id: str = "") -> dict:
        record = await self._authorize(channel, local_id)
        status = record.get("status")

        if status in TERMINAL_STATUSES:
            await self.store.delete(local_id)
            return {"id": local_id, "deleted": True}

        if status != "queued":
            raise AdapterError(
                f"only queued tasks can be cancelled; this one is {status}",
                code="InvalidParameter",
                param="id",
            )

        script = scriptstore.load(
            self.settings, channel.script_ref, expected_sha256=channel.script_sha256
        )
        declared = scriptstore.phases(script)
        ctx = Context(
            options=dict(channel.options),
            upstream_url=channel.upstream_url,
            request_id=request_id,
            credential=channel.credential,
            task=TaskView(
                upstream_task_id=record.get("upstream_task_id", ""),
                status=status,
                model=record.get("model", ""),
                request=record.get("request") or {},
                created_at=record.get("created_at"),
            ),
        )
        try:
            if "cancel_request" in declared:
                result, _plan = await request_upstream(
                    script,
                    "cancel_request",
                    ctx,
                    {"id": local_id},
                    client=self.client,
                    channel_url=channel.upstream_url,
                    auth_headers=build_auth_headers(channel),
                    idempotent=False,
                )
                raise_for_status(result, phase="cancel")
                if "cancel_response" in declared:
                    await call_phase(script, "cancel_response", ctx, result.body)
        except Exception:
            # 取消失败**不能**释放槽位：上游任务可能还在跑，槽位必须占到它真的结束
            raise

        now = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
        record = await self.store.update(local_id, status="cancelled", updated_at=now) or record
        await self.gate.release(record.get("gate_key", ""), local_id)
        return self._render(record)

    # ------------------------------------------------------------------ 列表
    async def list(
        self, channel: ChannelConfig, *, page_num: int = 1, page_size: int = 20
    ) -> dict:
        credential_id = fingerprint(channel.credential, self.settings)
        rows, total = await self.store.list_for(
            credential_id=credential_id, provider=channel.provider, page_num=page_num, page_size=page_size
        )
        return {
            "items": [self._render(r) for r in rows],
            "total": total,
            "page_num": page_num,
            "page_size": page_size,
        }

    # ------------------------------------------------------------------ 内部
    async def _authorize(self, channel: ChannelConfig, local_id: str) -> dict:
        """取出任务并校验**凭证绑定**。不符 ⇒ 本地 404，不发上游请求。"""
        record = await self.store.get(local_id)
        if record is None:
            raise task_not_found(local_id)
        credential_id = fingerprint(channel.credential, self.settings)
        if record.get("credential_id") != credential_id:
            log.warning(
                "credential mismatch for %s: presenting a different key than the one that created it",
                local_id,
            )
            raise task_not_found(local_id)
        if channel.provider and record.get("provider") and channel.provider != record["provider"]:
            raise task_not_found(local_id)
        return record

    @staticmethod
    def _report(record: dict) -> dict:
        """上报块：**上游 task id + 脚本身份 + 请求/响应留档**（不脱敏）。

        创建与查询都带上它 —— 排障时最需要的就是"这次发了什么、上游回了什么、
        对应上游哪个任务"。**上游 task id 是与上游对工单的唯一凭据**，必须能拿到。
        """
        out = {
            "provider": record.get("provider"),
            "upstream_task_id": record.get("upstream_task_id"),
            "script_ref": record.get("script_ref"),
            "script_sha256": record.get("script_digest"),
            "upstream_report": dict(record.get("upstream_report") or {}),
        }
        moved = record.get("rehost_result")
        if moved:
            out["rehost"] = moved
        return out

    @classmethod
    def _render(cls, record: dict) -> dict:
        """任务记录 → Seedance 任务对象（附上报块）。"""
        view = record.get("view")
        if not isinstance(view, dict):
            view = {
                "status": record.get("status", "queued"),
                "video_url": None,
                "last_frame_url": None,
                "file_url": None,
                "error": None,
                "usage": None,
                "duration": None,
                "frames": None,
                "framespersecond": None,
                "ratio": None,
                "resolution": None,
                "seed": -1,
            }
        out = dict(view)
        out["id"] = record.get("local_id")
        out["model"] = record.get("model")
        out["status"] = record.get("status", view.get("status"))
        out["created_at"] = record.get("created_at")
        out["updated_at"] = record.get("updated_at")

        video_url = view.get("video_url") if out["status"] == "succeeded" else None
        rehost = record.get("rehost_result") or {}
        if video_url and rehost.get("ok"):
            # 转存成功 ⇒ 对外给**自有地址**；上游原地址仍在 rehost.upstream_url 里可查
            video_url = rehost["url"]
        out["content"] = {
            "video_url": video_url,
            "last_frame_url": view.get("last_frame_url"),
            "file_url": view.get("file_url"),
        }
        if record.get("requested") is not None:
            out["requested"] = record.get("requested")
        if record.get("effective") is not None:
            out["effective"] = record.get("effective")
        if record.get("warnings"):
            out["warnings"] = record.get("warnings")
        if record.get("unsupported"):
            out["unsupported"] = record.get("unsupported")
        out.update(cls._report(record))
        return out

    def _schedule_callback(self, record: dict, fragment: dict) -> None:
        url = record.get("callback_url")
        if not url:
            return
        task = asyncio.create_task(self._push_callback(url, self._render(record)))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _push_callback(self, url: str, body: dict) -> None:
        """推送体 = 查询接口响应体形状；**5s 无确认重试，最多 3 次**（契约语义）。"""
        import httpx  # 局部导入：只在真的要推送时才需要

        client = httpx.AsyncClient(timeout=5.0, trust_env=self.settings.upstream_trust_env)
        try:
            for attempt in range(1, 4):
                # 回调推送是"服务端主动出站到调用方给的地址"，出问题最需要能复盘
                # （推给了谁、推了什么、对方回了什么）——所以每次尝试一条 span。
                with observability.span(
                    "callback.push",
                    **{
                        "callback.url": url,
                        "callback.attempt": attempt,
                        "task.id": body.get("id") or "",
                        "task.upstream_id": body.get("upstream_task_id") or "",
                    },
                ) as handle:
                    handle.set_body_attribute("callback.body", body)
                    try:
                        response = await client.post(url, json=body)
                        handle.set_attribute("callback.status", response.status_code)
                        if response.status_code < 400:
                            log.info("callback pushed to %s (attempt %d)", url, attempt)
                            return
                        log.warning("callback to %s returned %s", url, response.status_code)
                    except httpx.HTTPError as exc:
                        handle.record_error(exc)
                        log.warning("callback to %s failed (attempt %d): %s", url, attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(1.0)
            log.error("callback to %s gave up after 3 attempts", url)
        finally:
            await client.aclose()

    async def reconcile_once(self) -> int:
        """看门狗一轮：把超时的未终态任务置 `expired`。

        ⚠️ 协调器**没有调用方请求可借钥匙**，所以它只能做"本地能判定"的事
        （超时置 expired）。真正的上游回查需要凭证，仍由调用方主动 `GET` 触发。
        """
        now = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
        touched = 0
        for record in await self._scan_active():
            if now < int(record.get("execution_expires_at") or 0):
                continue
            previous = record.get("status")
            if previous in TERMINAL_STATUSES:
                continue
            updated = await self.store.update(
                record["local_id"],
                status="expired",
                updated_at=now,
                view={
                    "status": "expired",
                    "error": {"code": "Expired", "message": "task exceeded execution_expires_after"},
                },
            )
            if updated:
                await self.gate.release(updated.get("gate_key", ""), updated["local_id"])
                self._schedule_callback(updated, {"status": "expired"})
                touched += 1
        return touched

    async def _scan_active(self) -> list[dict]:
        """扫描未终态任务。memory/sqlite/redis 三种后端都用一致的窄接口实现。"""
        scan = getattr(self.store, "scan_active", None)
        if callable(scan):
            return await scan()
        return []  # 后端未实现扫描时，看门狗退化为"不动作"（由调用方查询兜底）
