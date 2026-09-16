"""原生响应契约测试：**对调用方暴露的形状就是原生形状**（零网络、零计费）。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_seedance_contract.py

为什么单独一个文件：这个契约（创建只回 id / 查询不含 model / status 只能六态 /
原生字段集逐一在位）是**调用方编译期就依赖的东西**，而它极易被"顺手加个诊断字段"
破坏 —— 加字段的人不会觉得那是破坏性变更（响应变宽通常被当成兼容），
但按原生契约写**严格校验**的 SDK / schema 会当场失败。

三条"必须不成立"的事，各自对应用例：

    ① 创建响应出现 id 以外的键；② 查询体出现 `model` 或任何诊断块（provider /
    upstream_task_id / script_ref / upstream_report / requested / effective /
    warnings / unsupported / upstream）；③ status 出现原生六态之外的取值。

本文件还包含一条**只在接缝上能测**的护栏：上游给出越界状态时必须收敛成非终态
（本仓脚本按构造不会产出越界值，所以只能直接压引擎的 `_apply_fragment` 接缝）。
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from adapter import observability  # noqa: E402
from adapter.main import create_app  # noqa: E402
from adapter.seedance import (  # noqa: E402
    ARK_STATUSES,
    NATIVE_CONTENT_KEYS,
    NATIVE_TASK_KEYS,
    NATIVE_USAGE_KEYS,
    TERMINAL_STATUSES,
    coerce_status,
    native_resolution,
    render_created,
    render_task,
)

# 复用引擎端到端测试的假上游与客户端（同目录，零额外维护）
from test_engine import Client, FakeUpstream, _body, make_settings  # noqa: E402

TASKS = "/api/v3/contents/generations/tasks"

#: 曾经的"诊断块"清单 —— 它们**必须**不出现在任何响应里（改回任何一个都会红）。
BANNED_RESPONSE_KEYS = (
    "model", "provider", "upstream_task_id", "script_ref", "script_sha256",
    "upstream_report", "upstream", "requested", "effective", "warnings",
    "unsupported", "rehost",
)


@contextlib.contextmanager
def snapshots():
    """收集 `task.snapshot` 上报（被移出响应体的诊断都从这里走）。"""
    seen: list[dict] = []

    def sink(record):
        if record.name == "task.snapshot":
            seen.append(record.attributes)

    observability.add_span_sink(sink)
    try:
        yield seen
    finally:
        observability.remove_span_sink(sink)


def _settings(upstream: FakeUpstream, **overrides):
    base = make_settings(upstream)
    import dataclasses

    values = {
        "adapter_key": "adapter-test-key",
        "task_store": "memory",
        "query_cache_seconds": 0.0,
        "upstream_retry_attempts": 1,
    }
    values.update(overrides)
    return dataclasses.replace(base, **values)


def _drive(case, **overrides):
    async def _run():
        upstream = FakeUpstream()
        upstream.start()
        observability.reset_state()
        app = create_app(_settings(upstream, **overrides))
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as c:
                    await case(c, app, upstream)
        finally:
            upstream.stop()
            observability.reset_state()

    asyncio.run(_run())


def case(fn):
    def wrapper() -> None:
        _drive(fn)

    wrapper.__name__ = fn.__name__
    return wrapper


# =============================================================================
# 1. 纯函数层：枚举与形状
# =============================================================================

def test_status_enum_matches_the_official_six_states():
    assert ARK_STATUSES == ("queued", "running", "succeeded", "failed", "expired", "cancelled")
    assert TERMINAL_STATUSES == frozenset({"succeeded", "failed", "expired", "cancelled"})
    # 终态必须是枚举的子集（否则引擎会"释放槽位"而状态却不在契约里）
    assert TERMINAL_STATUSES <= set(ARK_STATUSES)


def test_unknown_status_collapses_to_a_non_terminal_state():
    """越界状态 **绝不** 被当成终态：终态会落库、释放并发槽位、推回调。"""
    for raw in ("WEIRD", "", None, "progress", "SUBMITTED"):
        status, unknown = coerce_status(raw)
        assert status in ARK_STATUSES, (raw, status)
        if raw in ("progress", "submitted", "SUBMITTED"):
            continue                       # 这些是上游词，正常映射后不该落到这里
        assert status == "running" and unknown is True, (raw, status, unknown)


def test_known_status_is_not_flagged_as_unnormalised():
    for raw in ARK_STATUSES:
        assert coerce_status(raw.upper()) == (raw, False)


def test_resolution_is_normalised_to_the_native_form():
    assert native_resolution(720) == "720p"
    assert native_resolution("720P") == "720p"
    assert native_resolution("1080p") == "1080p"
    assert native_resolution(None) is None      # 不知道就说不知道，不编一个默认分辨率


def test_render_task_emits_exactly_the_native_key_set():
    record = {
        "local_id": "cgt-1", "status": "queued", "created_at": 1, "updated_at": 2,
        "native": {"seed": -1, "resolution": "720p", "ratio": "16:9", "duration": 5,
                   "frames": None, "framespersecond": 24, "service_tier": "default",
                   "execution_expires_after": 172800, "generate_audio": False,
                   "draft": False, "priority": 0},
    }
    out = render_task(record)
    assert tuple(out) == NATIVE_TASK_KEYS, sorted(set(out) ^ set(NATIVE_TASK_KEYS))
    assert tuple(out["content"]) == NATIVE_CONTENT_KEYS
    for banned in BANNED_RESPONSE_KEYS:
        assert banned not in out, banned
    assert render_created(record) == {"id": "cgt-1"}


def test_video_url_is_only_exposed_on_success():
    base = {"local_id": "cgt-1", "created_at": 1, "updated_at": 2, "native": {}}
    running = render_task({**base, "status": "running",
                           "view": {"video_url": "https://x/y.mp4"}})
    assert running["content"]["video_url"] is None, "未成功就交出产物地址等于撒谎"
    done = render_task({**base, "status": "succeeded", "view": {"video_url": "https://x/y.mp4"}})
    assert done["content"]["video_url"] == "https://x/y.mp4"


# =============================================================================
# 2. HTTP 层：真 app + 假上游
# =============================================================================

@case
async def test_create_returns_only_id(client, app, upstream):
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"id"}, f"创建只应回 id，实得 {sorted(body)}"
    assert str(body["id"]).startswith("cgt-")


@case
async def test_get_and_list_are_pure_native(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]

    # ① 刚创建、**还没查过上游**：原生字段也必须齐（否则调用方分不清
    #    "参数没生效"与"还没开始算"）
    fresh = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert set(fresh) == set(NATIVE_TASK_KEYS), sorted(set(fresh) ^ set(NATIVE_TASK_KEYS))
    assert fresh["status"] == "running"
    assert fresh["resolution"] == "720p"          # 归一后的原生写法
    assert fresh["ratio"] == "16:9"
    assert fresh["duration"] == 5
    assert fresh["seed"] == -1
    assert fresh["framespersecond"] == 24
    assert fresh["service_tier"] == "default"
    assert fresh["execution_expires_after"] == 172800
    assert fresh["priority"] == 0
    # ⚠️ 报的是**实际生效值**：本上游无一支持生成有声视频 ⇒ 恒 false。
    # 请求里写 generate_audio=true 也不会变成 true（那会是一个假承诺）。
    assert fresh["generate_audio"] is False
    assert fresh["draft"] is False
    assert fresh["usage"] is None or set(fresh["usage"]) == set(NATIVE_USAGE_KEYS)
    for banned in BANNED_RESPONSE_KEYS:
        assert banned not in fresh, banned

    # ② 终态
    final = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    final = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert final["status"] == "succeeded", final
    assert set(final) == set(NATIVE_TASK_KEYS)
    assert set(final["usage"]) == set(NATIVE_USAGE_KEYS)
    assert final["content"]["video_url"].endswith("/media/ck001.mp4")
    assert final["error"] is None

    # ③ 列表项同形（同一套渲染）
    page = (await client.get(TASKS, headers=ch.headers)).json()
    assert page["items"], page
    assert set(page["items"][0]) == set(NATIVE_TASK_KEYS), sorted(page["items"][0])


@case
async def test_request_echo_fields_follow_the_request(client, app, upstream):
    """原生回显：本层原样带过去的调优项按**请求值**回显（含边界值）。"""
    ch = Client(upstream.base_url)
    payload = _body(service_tier="flex", execution_expires_after=3600, priority=7,
                    generate_audio=True, draft=True)
    task_id = (await client.post(TASKS, json=payload, headers=ch.headers)).json()["id"]
    body = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert body["service_tier"] == "flex"
    assert body["execution_expires_after"] == 3600
    assert body["priority"] == 7
    # 这两项上游没有对应能力 ⇒ 报**实际生效值**（False），而不是回显请求值
    assert body["generate_audio"] is False
    assert body["draft"] is False
    # 但"为什么是 false"必须查得到 —— 从响应体里查不到，就去上报里查
    with snapshots() as snaps:
        await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert snaps and snaps[-1]["task.unsupported"]


@case
async def test_snapshot_reports_everything_the_response_no_longer_carries(client, app, upstream):
    ch = Client(upstream.base_url)
    with snapshots() as snaps:
        task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
        create_snap = snaps[-1]
        # 逐项在位（这是"响应体可以少、上报不可以少"的可执行版本）
        assert create_snap["task.id"] == task_id
        assert create_snap["task.upstream_id"] == "ck001"
        assert create_snap["task.provider"] == "aivideomaker"
        assert create_snap["task.credential_id"].startswith(("hmac-sha256:", "sha256:"))
        assert create_snap["task.script.ref"] == "aivideomaker/video@v1"
        assert create_snap["task.script.sha256"]
        assert create_snap["task.status"] == "queued"
        assert create_snap["task.status.history"] == [
            {"status": "queued", "at": create_snap["task.created_at"]}
        ]
        assert create_snap["task.effective.upstream_model"] == "seedance20"
        assert create_snap["task.effective.model_requested"] == "aivideomaker/seedance20"
        assert create_snap["task.effective.model_map_applied"] is False
        assert create_snap["task.requested"]["model"] == "aivideomaker/seedance20"
        assert create_snap["task.report.request"]["method"] == "POST"
        assert create_snap["task.report.response"]["status"] == 200
        # 凭证本身任何形态都不许出现在上报里
        dumped = repr(create_snap)
        assert "ak_test_upstream_key" not in dumped
        assert "adapter-test-key" not in dumped


@case
async def test_status_transitions_are_recorded_and_bounded(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    with snapshots() as snaps:
        await client.get(f"{TASKS}/{task_id}", headers=ch.headers)      # → running
        await client.get(f"{TASKS}/{task_id}", headers=ch.headers)      # → succeeded
        history = snaps[-1]["task.status.history"]
    assert [h["status"] for h in history] == ["queued", "running", "succeeded"], history
    # 有界：只记**变更**，所以 36 次轮询不会撑出 36 条
    assert snaps[-1]["task.status.changes"] == 3
    assert snaps[-1]["task.status.previous"] == "running"
    assert snaps[-1]["task.status.changed"] is True


@case
async def test_terminal_read_does_not_touch_upstream_and_says_so(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    before = upstream.count("GET", "/api/v1/tasks/")
    with snapshots() as snaps:
        await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert upstream.count("GET", "/api/v1/tasks/") == before, "终态任务不该再打上游"
    assert snaps[-1]["task.query.served"] == "local-terminal"


@case
async def test_engine_collapses_an_out_of_enum_status_and_flags_it(client, app, upstream):
    """越界状态只能在**接缝**上测：本仓脚本按构造只会产出原生六态。

    护栏的意义在于"将来有第二个上游/脚本时，它产出越界值也不会把任务误判成终态"，
    所以这里直接压引擎的 `_apply_fragment`：这是唯一能造出越界值的地方。
    """
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    service = app.state.service
    record = await service.store.get(task_id)
    with snapshots() as snaps:
        rendered = await service._apply_fragment(record, {"status": "WEIRD_FROM_UPSTREAM"})
    assert rendered["status"] == "running", "越界状态必须收敛成非终态"
    assert rendered["status"] in ARK_STATUSES
    assert snaps[-1]["task.status.unnormalized"] is True
    # 任务没被误判成终态 ⇒ 并发槽位仍然占着（否则闸门会被静默放宽）
    assert not any(r["local_id"] == task_id and r["status"] in TERMINAL_STATUSES
                   for r in [await service.store.get(task_id)])


@case
async def test_create_report_bodies_ride_only_on_the_create_span(client, app, upstream):
    """创建侧留档的**全文只有一份**（在创建那条 span 上）；轮询只带定位键。

    理由：一次任务 36 次轮询各带一份创建原文 ⇒ 每个任务多几十 KB，而信息量为零
    （上游 URL 里若塞了 base64 素材，重复代价还要乘 `OBS_BODY_MAX_CHARS`）。
    但**不能反过来一点不给**：单条 `GET` 的 trace 也得能回答"创建时打的是哪个端点"，
    所以查询侧保留三个便宜的定位键，并指明全文在哪条 span 上。
    """
    ch = Client(upstream.base_url)
    with snapshots() as snaps:
        task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
        await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
        await client.get(f"{TASKS}/{task_id}", headers=ch.headers)

    assert len(snaps) == 3, f"创建 + 两次查询应各一条快照（实得 {len(snaps)}）"
    create_snap, *polls = snaps
    # ① 创建：全文留档在这儿
    assert "task.report.request" in create_snap
    assert create_snap["task.report.request"]["method"] == "POST"
    assert create_snap["task.report.request"]["body"]["prompt"] == "a cat yawning"
    assert create_snap["task.report.response"]["body"]["taskId"] == "ck001"
    # ② 轮询：不带全文，但定位键齐
    for snap in polls:
        assert "task.report.request" not in snap, "轮询不该重复带创建原文"
        assert "task.report.response" not in snap, "轮询不该重复带创建应答原文"
        assert snap["task.report.request.method"] == "POST"
        assert snap["task.report.request.url"].endswith("/api/v1/generate/seedance20")
        assert snap["task.report.full_on_span"] == "task.create"
        # 每次查询的**本次**上游原文照旧带着（那才是"越详细越好"要的东西）
        assert "task.upstream.raw" in snap


@case
async def test_dry_run_still_carries_requested_and_effective(client, app, upstream):
    """dry-run 是**刻意的**非原生出口（ADR-010 D5）：它必须继续带诊断。

    防的是"顺手把 dry-run 也删干净" —— 那会让"我请求的 vs 实际生效的"彻底无处可查。
    """
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(), headers={**ch.headers, "X-Dry-Run": "1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["effective"]["upstream_model"] == "seedance20"
    assert body["effective"]["model_map_applied"] is False
    assert body["warnings"], "dry-run 必须带着降级/计价告警"
    assert upstream.count("POST", "/api/v1/generate") == 0


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    for name, exc in failed:
        print(f"  - {name}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
