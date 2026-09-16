"""任务编排：id 生成、凭证指纹、状态机、惰性查询、回调推送、超时看门狗。

四条不可协商的规则：

1. **响应体只含原生字段**（`adapter/seedance.py` 是形状的唯一实现点）：
   创建**只回 `{"id": …}`**、没有 status；查询不含 `model`、也不含任何诊断块。
2. **诊断改道 logfire**：上游 task id / 脚本身份 / 请求响应留档 / 实际生效值 /
   降级告警**全部**经 `_emit_snapshot()` 上报 —— 每个出口都要调它，
   少调一处就是静默丢证据（响应体已经不是它们的地盘了）。
3. **任务与凭证绑定**（§6.3b）：任务记录里存创建时那把 Key 的**指纹**，
   查询/取消时指纹不符 ⇒ **本地 404**，根本不发上游请求。
4. **锁链要闭环**：并发槽位从创建占到终态；每个失败出口都必须释放它。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import logging
import random
import string
import time
from typing import Any, Mapping

from . import media, observability, scriptstore, seedance
from .channel import ChannelConfig, build_auth_headers
from .ctx import Context, TaskView
from .errors import AdapterError, task_not_found
from .executor import (
    CANCEL_PHASES,
    CREATE_PHASES,
    QUERY_PHASES,
    call_phase,
    raise_for_upstream_error,
    request_upstream,
)
from .normalize import normalize_payload
from .queue import ConcurrencyGate
from .ratelimit import QUERY
from .seedance import TERMINAL_STATUSES
from .settings import Settings
from .taskstore import TaskStore
from .transport import UpstreamClient

log = logging.getLogger("video_adapter.tasks")

DEFAULT_EXECUTION_EXPIRES_AFTER = 172800  # 上游文档默认 48h
_ID_ALPHABET = string.ascii_lowercase + string.digits

#: 状态变更历史的保留条数（**有界**）。报告 F8 记的是"中间态被逐次覆盖 ⇒ 看不到状态
#: 推进"；只在状态**真的变了**时追加一条，36 次轮询通常只有 2~3 条，所以这个上限
#: 只在异常长的任务上才会被触到 —— 触到时丢的是**最旧**的那几条。
STATUS_HISTORY_LIMIT = 20


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
        #: 查询结果的短 TTL 缓存：`key → (monotonic 截止, 渲染结果)`。
        #: **这是降频的主力** —— 调用方的轮询间隔通常比上游的限额密，窗口内直接回放，
        #: 一次上游请求都不发。记录里的 `upstream_report.query_count`（→ logfire 的
        #: `task.query.count`）统计的是**真实上游查询数**，缓存命中**不**计入 ——
        #: 那个数字正是我们要压下去的；`task.query.cache_hit` 才是"这次省了"的标记。
        self._query_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        #: single-flight：同一任务的并发查询合并成一次上游调用。
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}

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
            # 非 2xx：**先让脚本映射厂商业务码**（如"上游并发已满" → 429 + Retry-After），
            # 脚本不声明 `<phase>_error` 或不敢认 ⇒ 回落通用映射（ADR-015）。
            await raise_for_upstream_error(script, "create", result, ctx)
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
            # 原生回显块：查询还没发生时靠它让第一次 `GET` 就能给出完整的原生字段集
            # （而不是一排 `null` —— 那分不清"参数没生效"与"还没开始算"）。
            "native": seedance.create_echo(payload, plan.get("effective")),
            # 有界的状态变更历史（见 `STATUS_HISTORY_LIMIT`）。初始态 `queued` 就是第一条。
            "status_history": [{"status": "queued", "at": now}],
            "view": None,
            "upstream": result.body,
        }
        await self.store.put(record)
        # `include_report=True`：**创建是唯一带 request/response 原文的出口**（见 `_emit_snapshot`）。
        self._emit_snapshot(record, include_report=True)
        log.info(
            "task created local_id=%s provider=%s model=%s upstream_task_id=%s",
            local_id,
            provider,
            bare_model,
            upstream_task_id,
        )
        # 原生契约：**只有 id**（`seedance-api-reference.md` §3.3）。其余全部进 logfire。
        return seedance.render_created(record)

    # ------------------------------------------------------------------ 查询
    async def get(self, channel: ChannelConfig, local_id: str, *, request_id: str = "") -> dict:
        """查询任务。**尽量不去打上游** —— 四级降频，越靠前省得越多（架构 §7.2）：

            ① 终态任务 → 本地记录直接渲染（本来就不查上游）；
            ② TTL 缓存命中 → 回放上一次结果（**0 次**上游请求）；
            ③ 同一任务的并发查询 → 合并成一次上游调用（single-flight）；
            ④ 真要打上游 → 过限流桶（排队或让路），并标记查询车道。

        凭证校验**必须在降频之前**：缓存与合并都不得绕过 `_authorize`（否则降频会变成越权）。
        """
        record = await self._authorize(channel, local_id)
        if record["status"] in TERMINAL_STATUSES:
            # 终态任务**本地直接作答**（本来就不查上游）；上报里标明这一跳的来源。
            self._emit_snapshot(record, served="local-terminal")
            return self._render(record)

        # 键含 `credential_id`：local_id 本身全局唯一，把凭证一起放进来是零成本的一道保险
        # —— 降频**绝不**可以跨凭证复用，那会把 A 的结果给到 B。
        cache_key = (str(record.get("credential_id") or ""), local_id)

        cached = self._cache_get(cache_key)
        if cached is not None:
            # 降频命中：这一次**没有发上游请求**。`cache_hit` 是降频唯一可核的观测量
            # （`ADR-008`），所以它必须进上报 —— 从响应体里看不出来。
            self._emit_snapshot(record, served="cache", cache_hit=True)
            return cached

        task = self._inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                self._query_upstream(channel, record, request_id=request_id)
            )
            self._inflight[cache_key] = task
            task.add_done_callback(lambda t, key=cache_key: self._retire_inflight(key, t))
        # shield：某个等待者断开（调用方取消）不该打断在飞的上游查询 —— 其他等待者还在等它。
        return await asyncio.shield(task)

    async def _query_upstream(
        self, channel: ChannelConfig, record: dict, *, request_id: str
    ) -> dict:
        """真正发一次上游查询（`get` 的第 ④ 步）。"""
        local_id = record["local_id"]
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
                # **只有查询**走限流桶：上游按 IP 限的就是它（创建/取消不计入配额，
                # 但它们的 429 仍会回灌冷却 —— 见 ratelimit.py 的 QUERY 注释）。
                rate_limit_lane=QUERY,
            )
            await raise_for_upstream_error(script, "query", result, ctx)
            fragment = await call_phase(script, "query_response", ctx, result.body)
        except AdapterError as exc:
            # 上游 404 可能意味着"上游侧任务没了"；不能因此丢掉本地记录（契约要 7 天可用），
            # 但也**不能假装任务还在跑**。如实把失败上报，任务留在本地。
            log.warning("query failed for %s: %s", local_id, exc)
            raise

        rendered = await self._apply_fragment(record, fragment, raw=result.body)
        # 只在**成功**路径写缓存：失败结果缓存下来会让一次抖动看起来像持续故障。
        self._cache_put((str(record.get("credential_id") or ""), local_id), rendered)
        return rendered

    # ------------------------------------------------------------------ 降频辅助
    def _retire_inflight(self, cache_key: tuple[str, str], task: asyncio.Task) -> None:
        """查完立刻摘牌。不摘的话，下一次查询会拿到这个**已完成**的结果，而不是重新去查。"""
        self._inflight.pop(cache_key, None)
        if not task.cancelled():
            # 消费掉异常：创建者被取消且没有其他等待者时，它本来会变成
            # "exception was never retrieved" 噪声。
            task.exception()

    def _cache_get(self, cache_key: tuple[str, str]) -> dict | None:
        entry = self._query_cache.get(cache_key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            self._query_cache.pop(cache_key, None)
            return None
        # 浅拷贝：调用方拿到的顶层键与缓存解耦（嵌套的 content 是只读语义，不必深拷）。
        return dict(payload)

    def _cache_put(self, cache_key: tuple[str, str], payload: dict) -> None:
        ttl = max(0.0, float(self.settings.query_cache_seconds))
        if ttl <= 0:
            return
        self._query_cache[cache_key] = (time.monotonic() + ttl, payload)

    async def _apply_fragment(self, record: dict, fragment: dict, *, raw: Any = None) -> dict:
        fragment = dict(fragment or {})
        previous = record.get("status")
        # 状态**必须落在原生六态里**（`seedance.ARK_STATUSES`）：脚本给的值也可能越界
        # （上游新增状态词 / 脚本漏了映射）。收敛放在引擎这一层 —— 不认识的中间态
        # 绝不能被当成终态，终态会落库、释放并发槽位、推回调。
        status, unnormalized = seedance.coerce_status(fragment.get("status") or previous)
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

        if status != previous:
            history = list(record.get("status_history") or [])
            history.append({"status": status, "at": now})
            updates["status_history"] = history[-STATUS_HISTORY_LIMIT:]

        record = await self.store.update(record["local_id"], **updates) or record

        if status in TERMINAL_STATUSES and previous not in TERMINAL_STATUSES:
            await self.gate.release(record.get("gate_key", ""), record["local_id"])
            if status != "cancelled":
                self._schedule_callback(record, fragment)
        self._emit_snapshot(
            record,
            previous_status=previous,
            served="upstream",
            cache_hit=False,
            unnormalized=unnormalized,
            upstream_raw=raw,
        )
        return self._render(record)

    # ------------------------------------------------------------------ 取消 / 删除
    async def delete(self, channel: ChannelConfig, local_id: str, *, request_id: str = "") -> dict:
        record = await self._authorize(channel, local_id)
        status = record.get("status")

        if status in TERMINAL_STATUSES:
            # 原生终态 `DELETE` = **删记录**（不是取消）。删掉之后本地不再有它的上报，
            # 所以这一条快照是它在 trace 上的最后一份证据。
            self._emit_snapshot(record, served="delete", deleted=True)
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
        if "cancel_request" not in declared:
            # 🔴 **上游没有取消端点时，必须响亮失败，不能假装取消了。**
            # 这条曾经静默走到下面那段（把本地记录置成 `cancelled`、释放并发槽位并返回
            # `status: cancelled`）——三个后果都是实质性的：
            #   ① 调用方以为任务停了，上游其实**还在跑并继续计费**；
            #   ② 并发槽位提前释放 ⇒ 闸门少算一个在途任务（真实天花板是在途数）；
            #   ③ 本地记录与上游状态从此永久不一致，且没有任何出口能看出这件事。
            # 未终态任务只能由**上游**取消，本层无法替它做 ⇒ 400 并说明原因（`ADR-014`）。
            # 已终态任务的 `DELETE` = 删本地记录，在本函数更早处返回，不受此影响。
            raise AdapterError(
                "this channel's upstream exposes no cancel endpoint, so a running task cannot "
                "be cancelled; it will keep running (and keep being billed) upstream. Poll it "
                "until it reaches a terminal status, then DELETE to drop the local record. "
                'See docs/decisions/ADR-014-no-cancel-endpoint-delete-semantics.md',
                code="InvalidParameter",
                param="id",
            )
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
            await raise_for_upstream_error(script, "cancel", result, ctx)
            if "cancel_response" in declared:
                await call_phase(script, "cancel_response", ctx, result.body)
        except Exception:
            # 取消失败**不能**释放槽位：上游任务可能还在跑，槽位必须占到它真的结束
            raise

        now = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
        history = list(record.get("status_history") or [])
        history.append({"status": "cancelled", "at": now})
        record = await self.store.update(
            local_id,
            status="cancelled",
            updated_at=now,
            status_history=history[-STATUS_HISTORY_LIMIT:],
        ) or record
        await self.gate.release(record.get("gate_key", ""), local_id)
        self._emit_snapshot(record, previous_status=status, served="cancel")
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
            "items": [seedance.render_task(r) for r in rows],
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
    def _render(record: dict) -> dict:
        """任务记录 → **原生任务对象**。

        刻意只做转发：形状一旦散落在引擎里，就会出现"某个出口忘了跟着改"。
        创建、查询、列表、回调推送**共用这一个函数**正是为了堵住它。
        原先在响应体里的 `provider` / `upstream_task_id` / `script_ref` / `upstream_report`
        / `requested` / `effective` / `warnings[]` / `unsupported[]` / `model` 全部**移除**，
        改由 `_emit_snapshot()` 上报（形状的唯一真源见 `adapter/seedance.py`）。
        """
        return seedance.render_task(record)

    def _emit_snapshot(
        self,
        record: dict,
        *,
        previous_status: str | None = None,
        cache_hit: bool | None = None,
        served: str = "",
        unnormalized: bool = False,
        upstream_raw: Any = None,
        deleted: bool | None = None,
        include_report: bool = False,
    ) -> None:
        """把这份任务记录的**全部诊断**上报（响应体已收敛为原生字段）。

        每个出口都必须调它：字段表在 `observability.task_snapshot_attributes()`，
        这里只负责把结论 + 原文送到 span 上。`served` 说明这一跳的结果从哪来
        （`local-terminal` / `cache` / `upstream` / `cancel` / `delete`），
        它是"降频到底有没有生效"的判据。

        🔴 `include_report` 决定**创建侧的 request/response 原文**带不带（**只有创建带**）。
        查询侧改带三个**便宜**的定位键（`task.report.request.method/url` +
        `task.report.full_on_span=task.create`）：
        一次任务的 36 次轮询各带一份创建原文 ≈ 每个任务多几十 KB，而信息量为零
        （2026-09-16 实测：4 次操作即 679 字符/条的重复）。上游 URL 里若塞了 base64 素材，
        重复代价还要乘 `OBS_BODY_MAX_CHARS`。
        ⇒ 单条 `GET` 的 trace 仍能回答"创建时打的是哪个端点、上游是哪个 task"，
        完整原文在**创建那条 span** 上。

        上报**永远不能影响业务**：整段包在 try 里 —— sink 或 logfire 炸了，
        请求照常返回（可观测性的失败不该变成可用性故障）。
        """
        try:
            report = record.get("upstream_report")
            report = report if isinstance(report, dict) else {}
            attributes = observability.task_snapshot_attributes(
                record,
                previous_status=previous_status,
                cache_hit=cache_hit,
                unnormalized_status=unnormalized,
            )
            if served:
                attributes["task.query.served"] = served
            if deleted is not None:
                attributes["task.deleted"] = bool(deleted)
            request = report.get("request")
            if isinstance(request, Mapping) and not include_report:
                attributes["task.report.request.method"] = str(request.get("method") or "")
                attributes["task.report.request.url"] = str(request.get("url") or "")
                attributes["task.report.full_on_span"] = "task.create"
            with observability.span("task.snapshot", **attributes) as handle:
                if include_report:
                    if report.get("request"):
                        handle.set_body_attribute("task.report.request", report["request"])
                    if report.get("response"):
                        handle.set_body_attribute("task.report.response", report["response"])
                if upstream_raw is not None:
                    handle.set_body_attribute("task.upstream.raw", upstream_raw)
        except Exception as exc:  # noqa: BLE001 - 上报不得影响业务
            log.debug("task snapshot 上报失败：%s", exc)

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
            history = list(record.get("status_history") or [])
            history.append({"status": "expired", "at": now})
            updated = await self.store.update(
                record["local_id"],
                status="expired",
                updated_at=now,
                status_history=history[-STATUS_HISTORY_LIMIT:],
                view={
                    "status": "expired",
                    "error": {"code": "Expired", "message": "task exceeded execution_expires_after"},
                },
            )
            if updated:
                await self.gate.release(updated.get("gate_key", ""), updated["local_id"])
                self._schedule_callback(updated, {"status": "expired"})
                # 看门狗没有调用方请求可借钥匙，但它是**唯一**能发现超时的地方 ⇒
                # 必须上报，否则"任务卡住了"在 trace 上完全没有痕迹。
                self._emit_snapshot(updated, previous_status=previous, served="reconciler")
                touched += 1
        return touched

    async def _scan_active(self) -> list[dict]:
        """扫描未终态任务。memory / redis 两种后端都用一致的窄接口实现。"""
        scan = getattr(self.store, "scan_active", None)
        if callable(scan):
            return await scan()
        return []  # 后端未实现扫描时，看门狗退化为"不动作"（由调用方查询兜底）
