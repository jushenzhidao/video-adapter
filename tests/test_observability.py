"""上报契约测试：**离线校验**（零网络、零真实生成请求、零计费）。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_observability.py

分两层，且**两层都必须真跑**（不允许静默跳过）：

1. **本地 sink 层**（不需要 logfire）：断言"上游 task id / request / response 原文确实进了上报"、
   "凭证明文不出现在任何一条记录里"、失败路径仍带着请求属性、`obs_report_bodies=false`
   时正文**不出现**（而不是写了空占位）。
2. **logfire 导出层**（logfire 不在场就**红**）：用内存 exporter 把 span 捞回来
   —— 这才是"客户端实际发出的东西"。断言属性**没有被默认脱敏器整值替换**
   （一句含 cookie/secret 的提示词必须原文活着），且凭证仍被遮住。

线上校验是另一件事（会真外发），见 `scripts/logfire_online_probe.py`：
本文件**绝不**配置 token，也不会往线上发一个字节。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import socket
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

# 🔴 先清掉环境里的 token：本地/CI 的 shell 里若导出过 LOGFIRE_TOKEN，
#    这个文件会变成"往线上发测试数据"而测试全绿 —— 必须物理排除。
os.environ.pop("LOGFIRE_TOKEN", None)

from adapter import observability  # noqa: E402
from adapter.main import create_app  # noqa: E402

# 复用引擎端到端测试的假上游与客户端（同目录，零额外维护）
from test_engine import (  # noqa: E402
    ADAPTER_KEY,
    TASKS,
    UPSTREAM_KEY,
    Client,
    FakeUpstream,
    _body,
    make_settings,
)

#: 含默认脱敏词（cookie / secret / session）的提示词：用来证明**原文活着**。
PROMPT = "a dog eating a cookie at a secret beach session"


def _settings(upstream: FakeUpstream, **overrides):
    """引擎测试的设置 + **强制空 token**（本文件永不外发）。"""
    base = make_settings(upstream)
    return dataclasses.replace(base, logfire_token="", logfire_console=False, **overrides)


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Recorder:
    """本地 sink：把每条上报记录收下来（与 logfire 是否在场无关）。"""

    def __init__(self) -> None:
        self.records: list[observability.SpanRecord] = []
        self._sink = self.records.append

    def __enter__(self) -> "Recorder":
        observability.add_span_sink(self._sink)
        return self

    def __exit__(self, *exc) -> bool:
        observability.remove_span_sink(self._sink)
        return False

    def named(self, name: str) -> list[observability.SpanRecord]:
        return [r for r in self.records if r.name == name]

    def one(self, name: str) -> observability.SpanRecord:
        found = self.named(name)
        assert len(found) == 1, f"expected exactly one {name!r} span, got {len(found)}"
        return found[0]

    def dump(self) -> str:
        return json.dumps([r.to_json() for r in self.records], ensure_ascii=False, default=str)


def _drive(case, **setting_overrides):
    """起真 app（真 lifespan）＋ 本地假上游 ＋ 进程内 ASGI 客户端，零真实调用。"""

    async def _run():
        upstream = FakeUpstream()
        upstream.start()
        observability.reset_state()
        app = create_app(_settings(upstream, **setting_overrides))
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as c:
                    await case(c, app, upstream)
        finally:
            upstream.stop()
            observability.clear_span_sinks()
            observability.reset_state()

    asyncio.run(_run())


def case(fn):
    """把 async case 包成一个可独立运行的测试。"""

    def wrapper() -> None:
        _drive(fn)

    wrapper.__name__ = fn.__name__
    return wrapper


# =============================================================================
# 1. 上报内容：上游 task id + request / response 原文
# =============================================================================

@case
async def test_create_reports_upstream_task_id_and_both_sides_verbatim(client, app, upstream):
    with Recorder() as rec:
        headers = Client(upstream.base_url, extra={"x-request-id": "req-obs-1"}).headers
        response = await client.post(TASKS, json=_body(content=[{"type": "text", "text": PROMPT}]), headers=headers)
        assert response.status_code == 200, response.text
        created = response.json()
        assert created["upstream_task_id"] == "ck001"

    # span 名是契约：创建 / 翻译相位 / 上游调用三条都要在
    assert {"task.create", "script.phase", "upstream.call"} <= {r.name for r in rec.records}

    create = rec.one("task.create")
    assert create.attributes["task.id"] == created["id"]
    assert create.attributes["task.upstream_id"] == "ck001", "上游 task id 必须进上报"
    assert create.attributes["request.id"] == "req-obs-1"

    call = rec.one("upstream.call")
    assert call.attributes["upstream.phase"] == "create"
    assert call.attributes["upstream.method"] == "POST"
    # 发出去的原文：prompt 与脚本写进 body 的档位字段都在，且**没有被脱敏改写**
    sent = call.attributes["upstream.request.body"]
    assert sent["prompt"] == PROMPT, sent
    assert sent["duration"] == 5 and sent["ratio"] == "16:9", sent
    assert call.attributes["upstream.response.status"] == 200
    # 收到的原文：上游 task id 就在响应体里（创建时这是唯一能拿到它的地方）
    assert call.attributes["upstream.response.body"]["taskId"] == "ck001"


@case
async def test_query_reports_upstream_task_id_and_raw_response(client, app, upstream):
    headers = Client(upstream.base_url).headers
    created = (await client.post(TASKS, json=_body(), headers=headers)).json()
    with Recorder() as rec:
        got = await client.get(f"{TASKS}/{created['id']}", headers=headers)
        assert got.status_code == 200, got.text

    query = rec.one("task.query")
    assert query.attributes["task.upstream_id"] == "ck001"
    assert query.attributes["task.id"] == created["id"]
    call = rec.one("upstream.call")
    assert call.attributes["upstream.phase"] == "query"
    assert call.attributes["task.upstream_id"] == "ck001", "查询相位也要带上游 task id"
    assert call.attributes["upstream.response.body"]["id"] == "ck001"


@case
async def test_dry_run_reports_no_upstream_call(client, app, upstream):
    with Recorder() as rec:
        response = await client.post(TASKS, json=_body(dry_run=True), headers=Client(upstream.base_url).headers)
        assert response.status_code == 200
    assert rec.named("upstream.call") == [], "dry_run 不能上报一次并不存在的上游调用"
    assert rec.one("task.create").attributes["task.dry_run"] is True

# =============================================================================
# 2. 凭证：明文一个字节都不能进上报
# =============================================================================

@case
async def test_upstream_credential_never_reaches_the_report(client, app, upstream):
    with Recorder() as rec:
        response = await client.post(TASKS, json=_body(), headers=Client(upstream.base_url).headers)
        assert response.status_code == 200

    dumped = rec.dump()
    assert UPSTREAM_KEY not in dumped, "上游凭证明文出现在上报里"
    assert ADAPTER_KEY not in dumped, "本服务准入密钥明文出现在上报里"
    headers = rec.one("upstream.call").attributes["upstream.request.headers"]
    assert headers["key"].startswith("[redacted"), headers


@case
async def test_a_custom_credential_header_is_masked_by_value_not_by_name(client, app, upstream):
    """渠道自选头名（`X-Auth-Emit: header:x-custom-token:`）也必须被遮。

    这是"名字表不可能穷举"那条判据的活证据：`x-custom-token` 不在任何名单里，
    能遮住它靠的是**值里含渠道凭证**。
    """
    headers = Client(upstream.base_url, extra={"X-Auth-Emit": "header:x-custom-token:"}).headers
    with Recorder() as rec:
        assert (await client.post(TASKS, json=_body(), headers=headers)).status_code == 200
    assert UPSTREAM_KEY not in rec.dump()
    sent = rec.one("upstream.call").attributes["upstream.request.headers"]
    assert sent["x-custom-token"].startswith("[redacted"), sent


def test_bodies_can_be_switched_off_without_placeholders():
    """`OBS_REPORT_BODIES=false` ⇒ 正文**不出现**，而不是写空占位。"""

    async def _case(client, app, upstream):
        with Recorder() as rec:
            response = await client.post(TASKS, json=_body(), headers=Client(upstream.base_url).headers)
            assert response.status_code == 200
        call = rec.one("upstream.call")
        assert "upstream.request.body" not in call.attributes, "关掉正文就该不出现，而不是写空占位"
        assert "upstream.response.body" not in call.attributes
        # 头仍然上报（凭证已打码）：开关只关正文，不是"关掉上报"
        assert "upstream.request.headers" in call.attributes

    _drive(_case, obs_report_bodies=False)


# =============================================================================
# 3. 失败路径：没有应答时不许伪造状态码
# =============================================================================

@case
async def test_transport_failure_keeps_request_attributes_and_omits_status(client, app, upstream):
    with Recorder() as rec:
        try:
            await client.post(TASKS, json=_body(), headers=Client(f"http://127.0.0.1:{_dead_port()}").headers)
        except Exception:  # noqa: BLE001 - 连接失败会以异常冒到 ASGI 层；这里只关心上报
            pass

    call = rec.one("upstream.call")
    assert call.status == "error"
    assert "upstream.request.body" in call.attributes, "连接失败也必须看得到我们发了什么"
    assert call.attributes["error.type"].startswith("Connect"), call.attributes
    # 🔴「从没收到应答」与「上游回了 5xx」不能在上报里长得一样
    assert "upstream.response.status" not in call.attributes


def test_idempotent_retry_reports_each_attempt():
    """幂等查询连接失败 → 重试 → **每次尝试一条 span**（重试本身要能被复盘）。"""

    async def _run():
        upstream = FakeUpstream()
        upstream.start()
        observability.reset_state()
        app = create_app(_settings(upstream, upstream_retry_attempts=2))
        try:
            with Recorder() as rec:
                async with app.router.lifespan_context(app):
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as c:
                        created = (
                            await c.post(TASKS, json=_body(), headers=Client(upstream.base_url).headers)
                        ).json()
                        headers = Client(f"http://127.0.0.1:{_dead_port()}").headers
                        try:
                            await c.get(f"{TASKS}/{created['id']}", headers=headers)
                        except Exception:  # noqa: BLE001
                            pass
            calls = [r for r in rec.named("upstream.call") if r.attributes["upstream.phase"] == "query"]
            assert [c.attributes["upstream.attempt"] for c in calls] == [1, 2], calls
            assert all(c.status == "error" for c in calls)
            assert all("upstream.response.status" not in c.attributes for c in calls)
        finally:
            upstream.stop()
            observability.clear_span_sinks()
            observability.reset_state()

    asyncio.run(_run())


# =============================================================================
# 4. 纯函数层：打码与截断
# =============================================================================

def test_credential_shaped_keys_are_masked_but_usage_counters_survive():
    cleaned = observability.scrub(
        {"api_key": "ak_live_should_not_appear", "completion_tokens": 15, "prompt": "a cat"},
        credential="",
    )
    assert cleaned["api_key"] == "[redacted 25 chars]"
    # 用量计数**必须**留原文：遮掉它等于把对账字段静默删除
    assert cleaned["completion_tokens"] == 15
    assert cleaned["prompt"] == "a cat"


def test_value_containing_the_channel_credential_is_masked_anywhere():
    cleaned = observability.scrub({"note": "use ak_live_key_xyz in the query"}, credential="ak_live_key_xyz")
    assert cleaned["note"].startswith("[redacted")
    assert observability.scrub({"note": "harmless"}, credential="ak_live_key_xyz")["note"] == "harmless"


def test_truncation_marks_what_was_dropped():
    cleaned = observability.scrub("x" * 100, max_chars=10)
    assert cleaned.startswith("x" * 10)
    assert "…[+90 chars truncated]" in cleaned
    assert observability.scrub("short", max_chars=10) == "short"


def test_nested_structures_are_walked():
    cleaned = observability.scrub(
        {"outer": [{"authorization": "Bearer xyz"}, {"fine": "ok"}]}, credential=""
    )
    assert cleaned["outer"][0]["authorization"] == "[redacted 10 chars]"
    assert cleaned["outer"][1]["fine"] == "ok"


def test_mask_headers_keeps_non_credential_headers_verbatim():
    masked = observability.mask_headers(
        {"Content-Type": "application/json", "Retry-After": "3", "key": "secret-value"},
        credential="secret-value",
    )
    assert masked["Content-Type"] == "application/json"
    assert masked["Retry-After"] == "3"
    assert masked["key"] == "[redacted 12 chars]"


# =============================================================================
# 5. logfire 导出层：客户端实际发出的东西（离线内存 exporter）
# =============================================================================

def _logfire_or_fail():
    try:
        import logfire  # noqa: F401
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: F401
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: F401
            InMemorySpanExporter,
        )
    except ImportError as exc:  # 依赖缺失时**红**，不能静默跳过（否则契约从没被验证）
        raise AssertionError(
            f"logfire 未安装 ⇒ 上报契约的导出层没被验证过。pip install logfire 后再跑：{exc}"
        ) from exc


def test_exported_spans_keep_prose_verbatim_and_credential_masked():
    _logfire_or_fail()
    import logfire
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    async def _run():
        upstream = FakeUpstream()
        upstream.start()
        observability.reset_state()
        app = create_app(_settings(upstream))
        exporter = InMemorySpanExporter()
        try:
            with Recorder() as rec:
                async with app.router.lifespan_context(app):
                    # 顺序很关键：必须在 lifespan **装配之后**再 configure 一次，
                    # 否则 app 的 setup_observability 会把我们的 exporter 顶掉（已踩过）。
                    logfire.configure(
                        send_to_logfire=False,
                        console=False,
                        # 🔴 必须带上我们的脱敏配置：`logfire.configure()` **不复用**
                        # 上一次的 scrubbing（实测），漏了它默认规则回来、正文被整条替换。
                        scrubbing=observability.scrubbing_options(),
                        additional_span_processors=[SimpleSpanProcessor(exporter)],
                    )
                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as c:
                        headers = Client(upstream.base_url, extra={"x-request-id": "req-export-1"}).headers
                        response = await c.post(
                            TASKS, json=_body(content=[{"type": "text", "text": PROMPT}]), headers=headers
                        )
                        assert response.status_code == 200
                    logfire.force_flush()

            spans = {s.name: s for s in exporter.get_finished_spans()}
            assert "upstream.call" in spans and "task.create" in spans, sorted(spans)
            attributes = dict(spans["upstream.call"].attributes or {})

            # 非原始类型属性导出时是 **JSON 字符串**（logfire 5.x 实测），断言要过一层 loads
            body = json.loads(attributes["upstream.request.body"])
            assert body["prompt"] == PROMPT, "提示词被脱敏器改写了 ⇒ 上报原文失效"
            assert json.loads(attributes["upstream.response.body"])["taskId"] == "ck001"
            assert spans["task.create"].attributes["task.upstream_id"] == "ck001"

            # 正文里**不许有脱敏痕迹**：这是"上报原文"的定义
            assert "Scrubbed" not in attributes["upstream.request.body"]
            assert "Scrubbed" not in attributes["upstream.response.body"]

            exported = json.dumps(
                {name: dict(s.attributes or {}) for name, s in spans.items()},
                ensure_ascii=False,
                default=str,
            )
            assert UPSTREAM_KEY not in exported, "凭证明文被导出了"
            # 头被遮住即可（形式不限：源头打码 `[redacted n chars]`，
            # 或 logfire 再按**属性名** `key` 二次脱敏成 `[Scrubbed due to 'key']`）。
            header_json = attributes["upstream.request.headers"]
            assert "redacted" in header_json or "Scrubbed" in header_json, header_json
            # …但**被脱敏的位置只能是凭证名**（不是随便什么正文）
            for entry in json.loads(attributes.get("logfire.scrubbed") or "[]"):
                assert observability.is_credential_name(entry["path"][-1]), entry
            # sink 与导出层看到的是同一份内容（否则"离线校验"就是自娱自乐）
            assert rec.one("task.create").attributes["task.upstream_id"] == "ck001"
        finally:
            upstream.stop()
            observability.clear_span_sinks()
            observability.reset_state()

    asyncio.run(_run())


def test_scrubbing_callback_passes_prose_and_redacts_credential_shapes():
    """第二道防线自证：散文原样活着，`name: value` 形状的凭证照遮。"""
    _logfire_or_fail()
    import logfire
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=observability.scrubbing_options(),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    prose = "a dog eating a cookie at a secret beach session"
    assignment = "Authorization: Bearer ak_live_do_not_export_me"
    header_line = "key: ak_live_do_not_export_me"
    with logfire.span("scrub.probe", prose=prose, assignment=assignment, header_line=header_line):
        pass
    logfire.force_flush()

    attributes = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attributes["prose"] == prose, "散文被整条替换 ⇒ 上报原文失效"
    # 不 pin logfire 的替换文案（那是第三方实现细节），只断言"凭证没了、值被改过"
    assert "ak_live_do_not_export_me" not in json.dumps(attributes, ensure_ascii=False)
    assert attributes["assignment"] != assignment
    assert attributes["header_line"] != header_line


# =============================================================================
# 6. /healthz：三种状态如实报告
# =============================================================================

@case
async def test_healthz_reports_configuration_and_reason(client, app, upstream):
    _logfire_or_fail()
    body = (await client.get("/healthz?deep=1")).json()
    logfire_field = body["logfire"]
    assert logfire_field["configured"] is False
    assert logfire_field["emitting"] is False, "没有 token 却报会外发 = 运维误判"
    assert logfire_field["ready"] is True, "logfire 在场就该报装配成功"
    assert "no LOGFIRE_TOKEN" in logfire_field["reason"], logfire_field
    assert body["observability"]["upstream_bodies"] is True


def test_healthz_reports_a_refused_capture_headers_deployment():
    """`LOGFIRE_CAPTURE_HEADERS=true` ⇒ 拒绝装配，并如实说明原因（不是静默按用户说的做）。"""
    observability.reset_state()
    settings = _settings(FakeUpstream(), logfire_capture_headers=True)
    state = observability.setup_observability(settings)
    assert state.ready is False and state.emitting is False
    assert "refused" in state.reason, state.reason
    assert observability.flush_spans() is True


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            failed.append((name, exc))
            print(f"  FAIL  {name}\n        {traceback.format_exc().rstrip()}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    for name, exc in failed:
        print(f"  - {name}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
