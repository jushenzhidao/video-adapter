"""限流与降频的专项测试：**每条断言都落在"上游实际收到了几次请求"上**。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_rate_limit.py

为什么单独一个文件、而且非得断言"上游收到了几次"：

    降频这类实现**极易写出"函数返回值正确、但一次都没生效"**的代码 ——
    归一化模块写完、单测也绿，却没接进真实路径，线上一次都没用上（本项目踩过这个坑）。
    所以这里的观测量是假上游记录的**真实入站请求**（`upstream.queries`），
    而不是限流器自己的内部计数（那个数字只证明"限流器认为自己拦了"）。

`tests/test_engine.py` 把查询缓存显式关掉了（它测的是链路语义）；本文件用**生产默认值**
验证降频，并用 `test_defaults_keep_throttling_on` 守住"默认不能被悄悄改成关闭"。

零消耗：全部打本地假上游，不发任何真实生成请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import pathlib
import socket
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapter import observability  # noqa: E402
from adapter.main import create_app  # noqa: E402
from adapter.ratelimit import build_rate_limiter, origin_of  # noqa: E402
from adapter.settings import Settings  # noqa: E402
from adapter.transport import parse_retry_after  # noqa: E402

SCRIPT_STORE = str(ROOT / "script_store")
ADAPTER_KEY = "adapter-test-key"
UPSTREAM_KEY = "ak_test_upstream_key"
MODEL = "aivideomaker/seedance20"
TASKS = "/api/v3/contents/generations/tasks"
OPTS = json.dumps({"provider": "aivideomaker", "max_credits": 5000, "allow_unpriced": True})
#: Redis 后端的用例需要**真 Redis**（见 run_all 里的说明：连不上就 FAIL，不 skip）。
#: db 15 是刻意选的：本地/CI 上那台 redis 可能还有别的用途，别去碰 db 0。
TEST_REDIS_URL = os.environ.get("RATE_LIMIT_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
#: 一个**故意连不上**的地址（连接被拒，用来验证降级路径）。
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"


@contextlib.contextmanager
def snapshots():
    """收集 `task.snapshot` 上报（响应体已只剩原生字段，诊断证据在这里）。

    降频的观测量有两个，都要看：`upstream.queries`（上游**真的**收到几次）
    与上报里的 `task.query.cache_hit` / `task.query.count`（引擎**自认**省了几次）。
    只信后者会漏掉"函数返回值对、但一次都没生效"这类实现。
    """
    seen: list[dict] = []

    def sink(record):
        if record.name == "task.snapshot":
            seen.append(record.attributes)

    observability.add_span_sink(sink)
    try:
        yield seen
    finally:
        observability.remove_span_sink(sink)


class FakeUpstream:
    """只实现本文件需要的行为：创建、查询（可注入 429），并把入站请求全记下来。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.tasks: dict[str, dict] = {}
        #: 非 None ⇒ 查询接口回这个 HTTP 状态（带 `Retry-After`）。
        self.query_http_status: int | None = None
        self.query_retry_after: str = "2"
        #: 非 None ⇒ 查询固定回这个上游状态（否则第 2 次查询起 COMPLETED）。
        self.next_query_status: str | None = None
        self.httpd: http.server.ThreadingHTTPServer | None = None
        self.port = 0

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"].startswith(prefix))

    @property
    def queries(self) -> int:
        """上游收到的**查询**次数 —— 本文件的核心观测量。"""
        return sum(1 for r in self.requests if r["method"] == "GET" and "/api/v1/tasks" in r["path"])

    def start(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self.httpd.state = self  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音 access log
        return

    def _json(self, code: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def _route(self, method: str) -> None:
        state: FakeUpstream = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        state.requests.append({"method": method, "path": self.path, "body": body})

        if method == "POST" and self.path.startswith("/api/v1/generate/"):
            task_id = f"ck{len(state.tasks) + 1:03d}"
            state.tasks[task_id] = {"queries": 0}
            return self._json(
                200,
                {
                    "status": "SUBMITTED",
                    "taskId": task_id,
                    "responseUrl": f"{state.base_url}/api/v1/tasks/{task_id}",
                    "statusUrl": f"{state.base_url}/api/v1/tasks/{task_id}/status",
                    "cancelUrl": f"{state.base_url}/api/v1/tasks/{task_id}/cancel",
                },
            )

        if method == "GET" and self.path.startswith("/api/v1/tasks/"):
            # 限流注入点：真实上游超限时就是 429 + `Retry-After`（上游文档 §6）。
            if state.query_http_status:
                return self._json(
                    state.query_http_status,
                    {"status": "FAILED", "message": "too many requests"},
                    headers={"Retry-After": state.query_retry_after},
                )
            task_id = self.path.rsplit("/", 1)[-1]
            task = state.tasks.get(task_id)
            if task is None:
                return self._json(404, {"status": "FAILED", "message": "unknown task"})
            task["queries"] += 1
            status = state.next_query_status or ("COMPLETED" if task["queries"] >= 2 else "PROGRESS")
            payload = {
                "id": task_id,
                "model": "seedance20",
                "status": status,
                "createdAt": "2026-09-13T12:00:00.000Z",
                "completedAt": None,
                "input": {"duration": 5, "resolution": 720, "ratio": "16:9"},
                "creditsCharged": 15,
            }
            if status == "COMPLETED":
                payload["completedAt"] = "2026-09-13T12:05:00.000Z"
                payload["output"] = {"url": f"{state.base_url}/media/{task_id}.mp4"}
            return self._json(200, payload)

        return self._json(404, {"status": "FAILED", "message": f"no route for {method} {self.path}"})


class Client:
    def __init__(self, upstream_url: str, *, credential: str = UPSTREAM_KEY, options: str = OPTS):
        self.headers = {
            "X-Adapter-Key": ADAPTER_KEY,
            "X-Upstream-Url": upstream_url,
            "X-Script-Ref": "aivideomaker/video@v1",
            "X-Auth-Emit": "header:key:",
            "X-Channel-Options": options,
            "Authorization": f"Bearer {credential}",
        }


def _body(**rest) -> dict:
    payload = {
        "model": MODEL,
        "content": [{"type": "text", "text": "a cat yawning"}],
        "duration": 5,
        "resolution": "720p",
        "ratio": "16:9",
    }
    payload.update(rest)
    return payload


def settings_for(**overrides) -> Settings:
    """**不覆盖 `query_cache_seconds` / `rate_limit_*`** —— 除非用例显式要改。

    这样多数用例跑的就是生产默认（缓存 2s、30rpm、burst 5），
    而不是"测试里才成立的一套参数"。
    """
    values = {
        "adapter_key": ADAPTER_KEY,
        "upstream_allow_private_network": True,
        "upstream_trust_env": False,
        "task_store": "memory",
        "script_store_dir": SCRIPT_STORE,
        "task_key_fingerprint_secret": "test-secret",
        "default_max_concurrency": 4,
        "queue_wait_seconds": 0.2,
        "upstream_retry_attempts": 1,
    }
    values.update(overrides)
    return Settings.from_env(**values)


def case(*, needs_redis: bool = False, **overrides):
    """标记一个用例，并给它专属的 settings 覆盖项。

    `needs_redis=True` 的用例**必须**连上真 Redis —— 连不上就记 FAIL，**不是 skip**：
    Lua 脚本与 Redis 服务器时钟只可能在真实例上被验证，静默跳过等于给虚假绿灯。
    """

    def decorate(fn):
        fn._is_case = True  # type: ignore[attr-defined]
        fn._needs_redis = needs_redis  # type: ignore[attr-defined]
        fn._overrides = overrides  # type: ignore[attr-defined]
        return fn

    return decorate


async def _run(upstream: FakeUpstream, fn, **overrides) -> None:
    app = create_app(settings_for(**overrides))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as client:
            await fn(client, app, upstream)


# =============================================================================
# 1. 降频：缓存与合并（这两条才是"少发请求"的主力）
# =============================================================================


@case()
async def test_short_ttl_cache_absorbs_repeated_polls(client, app, upstream):
    """TTL 窗口内连着查 5 次 → 上游**只被查 1 次**（跑的是生产默认配置）。"""
    assert app.state.settings.query_cache_seconds > 0, "默认必须是开着的，否则本用例在测空气"
    ch = Client(upstream.base_url)
    created = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    statuses = []
    for _ in range(5):
        response = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
        assert response.status_code == 200, response.text
        statuses.append(response.json()["status"])

    assert upstream.queries == 1, f"5 次轮询应只打 1 次上游，实际打了 {upstream.queries} 次"
    # 5 次拿到的是**同一份快照** —— 这就是降频的语义：宁可短暂陈旧，也不打爆配额。
    assert len(set(statuses)) == 1, statuses


@case(query_cache_seconds=0.05)
async def test_cache_expiry_lets_a_fresh_query_through(client, app, upstream):
    """缓存过期后必须重新查上游 —— 否则任务会永远停在 queried 那一刻的状态。"""
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]

    first = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert first["status"] == "running"
    assert upstream.queries == 1

    await asyncio.sleep(0.08)  # 越过 TTL
    with snapshots() as snaps:
        second = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()

    assert upstream.queries == 2, upstream.queries
    assert second["status"] == "succeeded", "第 2 次上游查询返回 COMPLETED，应被如实反映"
    # `task.query.count` 统计的是**真实上游查询数** —— 缓存命中不增加它，
    # 所以它才是"省了多少"的证据（响应体里已不含这个数字，改从上报取）。
    assert snaps[-1]["task.query.count"] == 2
    assert snaps[-1]["task.query.cache_hit"] is False


@case(query_cache_seconds=0.0)
async def test_concurrent_polls_are_coalesced_into_one_upstream_call(client, app, upstream):
    """6 个并发查询同一个任务 → 只该有 1 次上游调用（single-flight）。

    关掉缓存是必须的：否则测的是缓存，合并逻辑一次都没被走到。
    """
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]

    responses = await asyncio.gather(
        *[client.get(f"{TASKS}/{task_id}", headers=ch.headers) for _ in range(6)]
    )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert upstream.queries == 1, f"并发查询应合并成 1 次上游调用，实际 {upstream.queries} 次"


@case(query_cache_seconds=0.0, rate_limit_query_rpm=600, rate_limit_query_burst=100)
async def test_coalescing_does_not_leak_across_credentials(client, app, upstream):
    """合并/缓存**不得跨凭证**：另一把钥匙来查同一个 id 必须仍然 404（且不打上游）。"""
    owner = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=owner.headers)).json()["id"]

    intruder = Client(upstream.base_url, credential="ak_someone_else")
    denied = await client.get(f"{TASKS}/{task_id}", headers=intruder.headers)
    assert denied.status_code == 404, denied.text
    assert upstream.queries == 0, "凭证不符必须在本地就断掉，不上游走一趟"


# =============================================================================
# 2. 主动限流：本地拦截（上游一次都不该被打到）
# =============================================================================


@case(
    rate_limit_query_rpm=60,
    rate_limit_query_burst=1,
    rate_limit_wait_seconds=0.0,
    query_cache_seconds=0.0,
)
async def test_token_bucket_rejects_locally_without_touching_upstream(client, app, upstream):
    """令牌耗尽 ⇒ **本地** 429 + `Retry-After`，且上游请求数**不增加**。"""
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]

    first = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert first.status_code == 200, first.text
    assert upstream.queries == 1

    blocked = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert blocked.status_code == 429, blocked.text
    body = blocked.json()["error"]
    assert body["code"] == "RateLimitExceeded.ModelAccountRpmExceeded"
    assert "locally" in body["message"], "必须说清是**本层**在限，否则排障会去查上游配额"
    assert blocked.headers.get("Retry-After") is not None, "429 必须带 Retry-After"
    assert upstream.queries == 1, "被本地限流挡下的请求**绝不能**打到上游"


@case(
    rate_limit_query_rpm=60,
    rate_limit_query_burst=1,
    rate_limit_wait_seconds=0.0,
    query_cache_seconds=0.0,
)
async def test_creates_do_not_consume_the_query_quota(client, app, upstream):
    """上游只对**查询**声明了 IP 限额 ⇒ 创建不该被查询桶挡住（否则会误伤计费路径）。"""
    ch = Client(upstream.base_url)
    for _ in range(3):
        response = await client.post(TASKS, json=_body(), headers=ch.headers)
        assert response.status_code == 200, response.text
    assert upstream.count("POST") == 3
    assert upstream.queries == 0


# =============================================================================
# 3. 上游 429：全局冷却 + Retry-After 透出
# =============================================================================


@case(
    rate_limit_query_rpm=600,
    rate_limit_query_burst=100,
    rate_limit_wait_seconds=0.0,
    query_cache_seconds=0.0,
)
async def test_upstream_429_sets_a_global_cooldown_and_surfaces_retry_after(client, app, upstream):
    """上游 429 ⇒ ① 出口带 `Retry-After`；② 该 origin 的后续查询被冷却挡住。"""
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]

    upstream.query_http_status = 429
    upstream.query_retry_after = "3"

    first = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert first.status_code == 429, first.text
    assert first.headers.get("Retry-After") == "3", dict(first.headers)
    assert upstream.queries == 1

    second = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert second.status_code == 429, second.text
    assert upstream.queries == 1, "冷却期内**不该**再打上游（那正是被 429 的原因）"
    remaining = int(second.headers["Retry-After"])
    assert 1 <= remaining <= 3, remaining
    assert "cooldown" in second.json()["error"]["message"]


@case(
    rate_limit_query_rpm=600,
    rate_limit_query_burst=100,
    rate_limit_wait_seconds=0.0,
    query_cache_seconds=0.0,
    rate_limit_cooldown_max_seconds=1.0,
)
async def test_cooldown_is_clamped_and_the_clamp_is_visible(client, app, upstream):
    """上游给一个巨大的 `Retry-After` 时按本地上限截断。

    截断**必须**能看出来：否则会以为"上游让等多久就等多久"，而实际只等了一秒就又开始发了。
    """
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    upstream.query_http_status = 429
    upstream.query_retry_after = "3600"

    first = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert first.status_code == 429
    assert first.headers["Retry-After"] == "3600", "上游的原话照传，不该被本地改写"

    blocked = await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) <= 1, "本地实际只冷却了 1s（上限截断生效）"
    assert upstream.queries == 1


# =============================================================================
# 4. 可观测与配置守卫
# =============================================================================


@case(rate_limit_query_rpm=17, rate_limit_query_burst=3)
async def test_healthz_reports_rate_limit_scope_and_rpm(client, app, upstream):
    """`/healthz` 必须能回答"限流到底生没生效、是不是按进程各算各的"。"""
    body = (await client.get("/healthz")).json()
    rate_limit = body["rate_limit"]
    assert rate_limit["enabled"] is True
    # `scope=process` 是**已知边界**的显式声明：多副本下配额会按进程数放大。
    assert rate_limit["scope"] == "process"
    assert rate_limit["rpm"] == 17
    assert rate_limit["burst"] == 3.0


@case()
async def test_defaults_keep_throttling_on(client, app, upstream):
    """配置守卫：默认值一旦被改成"关掉"，上面那些用例就都在测一个不存在的机制。"""
    settings = app.state.settings
    assert settings.rate_limit_enabled is True
    assert 0 < settings.rate_limit_query_rpm < 60, "默认必须给上游的 60/min 留余量，不能贴上去"
    assert settings.rate_limit_query_burst < settings.rate_limit_query_rpm
    assert settings.query_cache_seconds > 0
    assert settings.rate_limit_cooldown_max_seconds > 0


@case()
async def test_origin_key_strips_credentials_and_query(client, app, upstream):
    """桶键会进 `/healthz` ⇒ **绝不允许**把 userinfo / 查询串带进去。"""
    assert (
        origin_of("https://user:pw@up.example.com/api/v1/tasks/x?key=SECRET")
        == "https://up.example.com"
    )
    assert origin_of("https://up.example.com:8443/x") == "https://up.example.com:8443"
    assert origin_of("http://up.example.com/x") == "http://up.example.com"
    assert "SECRET" not in origin_of("https://up.example.com/x?key=SECRET")


@case()
async def test_retry_after_parses_both_legal_formats(client, app, upstream):
    """上游文档没写 `Retry-After` 用哪种格式 ⇒ 两种都认，都不认时**不编数字**。"""
    assert parse_retry_after({"retry-after": "2"}) == 2.0
    assert parse_retry_after({"retry-after": "2.5"}) == 2.5
    assert parse_retry_after({}) is None
    assert parse_retry_after({"retry-after": "not-a-number"}) is None
    http_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    parsed = parse_retry_after({"retry-after": http_date})
    assert parsed is not None and 20 <= parsed <= 30, parsed


# =============================================================================
# 5. Redis 共享桶（**真 Redis**，不用替身）
#
#    "共享"只在**多进程**语义下才有意义，所以证明方式是：造两个**独立实例**
#    （各自的内存状态），只让 redis 是共享的，看配额是否真的只有一份。
#    并且**同时给反证**（换成 process 后端时两个实例各有一份）——
#    否则那些断言对"任何实现"都会通过，等于没测。
# =============================================================================


def plain_case(*, needs_redis: bool = True):
    """不依赖 harness 那个 app 的用例（自己起实例、或直接造限流器）。"""

    def decorate(fn):
        fn._is_case = True  # type: ignore[attr-defined]
        fn._is_plain = True  # type: ignore[attr-defined]
        fn._needs_redis = needs_redis  # type: ignore[attr-defined]
        fn._overrides = {}  # type: ignore[attr-defined]
        return fn

    return decorate


async def _redis_reachable() -> tuple[bool, str]:
    try:
        import redis.asyncio as redis_async
    except ImportError as exc:
        return False, f"`redis` 包缺失：{exc}"
    client = redis_async.from_url(TEST_REDIS_URL, socket_timeout=1, socket_connect_timeout=1)
    try:
        await client.ping()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def _purge_prefix(prefix: str) -> None:
    """清掉本用例写进 redis 的键（测试不该在共享实例上留垃圾）。"""
    import redis.asyncio as redis_async

    client = redis_async.from_url(TEST_REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    try:
        async for key in client.scan_iter(match=f"{prefix}:*", count=100):
            await client.delete(key)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def _limiter(*, store: str, prefix: str, burst: int):
    settings = Settings.from_env(
        adapter_key="x",
        task_store="memory",
        rate_limit_store=store,
        rate_limit_store_url=TEST_REDIS_URL if store == "redis" else "",
        rate_limit_key_prefix=prefix or "ratelimit",
        rate_limit_query_rpm=60,
        rate_limit_query_burst=burst,
        rate_limit_wait_seconds=0.0,
    )
    limiter = build_rate_limiter(settings)
    assert limiter is not None
    return limiter


@contextlib.asynccontextmanager
async def _app_client(**overrides):
    """起一个**完整应用实例**（含 lifespan），返回挂在它上面的 httpx 客户端。"""
    app = create_app(settings_for(**overrides))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as client:
            yield client, app


@plain_case()
def test_redis_bucket_is_shared_across_instances() -> None:
    """两个**独立限流器实例**（= 两个副本各自的进程）共用同一个 redis 前缀 ⇒ 合计一份配额。

    ⚠️ 同时给反证：`process` 后端下两个实例**各有一份**。没有这条反证，上面的断言
    对任何实现都成立（比如一个"永远拒绝"的实现也能通过）。
    """
    asyncio.run(_shared_vs_process())


async def _shared_vs_process() -> None:
    key = "https://origin.example.com"
    prefix = f"t-{uuid.uuid4().hex[:10]}"
    first = _limiter(store="redis", prefix=prefix, burst=1)
    second = _limiter(store="redis", prefix=prefix, burst=1)
    try:
        await first.start()
        await second.start()
        assert (await first.acquire(key, wait_seconds=0)).granted is True
        blocked = await second.acquire(key, wait_seconds=0)
        assert blocked.granted is False, "共享桶：第一个实例用掉后，第二个不该还能取到"
        assert blocked.reason == "no_token"
    finally:
        await first.close()
        await second.close()
        await _purge_prefix(prefix)

    # 反证：进程内桶**不共享**，两个实例各有一份满额。
    local_a = _limiter(store="process", prefix="", burst=1)
    local_b = _limiter(store="process", prefix="", burst=1)
    assert (await local_a.acquire(key, wait_seconds=0)).granted is True
    assert (await local_b.acquire(key, wait_seconds=0)).granted is True, "进程内桶各有一份"


@plain_case()
def test_redis_cooldown_is_shared_across_instances() -> None:
    """一个实例踩到 429 ⇒ **另一个实例**的查询也被冷却挡住（"按 IP"的推论 3）。"""
    asyncio.run(_shared_cooldown())


async def _shared_cooldown() -> None:
    key = "https://origin.example.com"
    prefix = f"t-{uuid.uuid4().hex[:10]}"
    first = _limiter(store="redis", prefix=prefix, burst=10)
    second = _limiter(store="redis", prefix=prefix, burst=10)
    try:
        await first.start()
        await second.start()
        assert (await second.acquire(key, wait_seconds=0)).granted is True  # 配额充足
        await first.note_rate_limited(key, 5.0)  # A 踩到 429
        blocked = await second.acquire(key, wait_seconds=0)
        assert blocked.granted is False, "冷却必须跨实例生效"
        assert blocked.reason == "cooldown"
        assert blocked.retry_after >= 4
    finally:
        await first.close()
        await second.close()
        await _purge_prefix(prefix)


@plain_case()
def test_two_app_instances_share_one_quota_end_to_end() -> None:
    """端到端：两个**应用实例** + 一个假上游 + 共享 redis 桶 ⇒ 上游只被查一次。

    两个实例各有自己的内存任务表（`task_store=memory`），**只有 redis 是共享的** ——
    这正是"多副本"的形态，也是这条断言有意义的原因。
    """
    upstream = FakeUpstream()
    upstream.start()
    try:
        asyncio.run(_two_instances_end_to_end(upstream))
    finally:
        upstream.stop()


async def _two_instances_end_to_end(upstream: FakeUpstream) -> None:
    prefix = f"t-{uuid.uuid4().hex[:10]}"
    overrides = dict(
        rate_limit_store="redis",
        rate_limit_store_url=TEST_REDIS_URL,
        rate_limit_key_prefix=prefix,
        rate_limit_query_rpm=60,
        rate_limit_query_burst=1,  # 两个实例**合起来**只允许 1 次
        rate_limit_wait_seconds=0.0,
        query_cache_seconds=0.0,
    )
    channel = Client(upstream.base_url)
    try:
        async with _app_client(**overrides) as (client_a, _app_a):
            created = await client_a.post(TASKS, json=_body(), headers=channel.headers)
            task_a = created.json()["id"]
            first = await client_a.get(f"{TASKS}/{task_a}", headers=channel.headers)
            assert first.status_code == 200, first.text
            assert upstream.queries == 1

        async with _app_client(**overrides) as (client_b, _app_b):
            created = await client_b.post(TASKS, json=_body(), headers=channel.headers)
            task_b = created.json()["id"]
            blocked = await client_b.get(f"{TASKS}/{task_b}", headers=channel.headers)
            assert blocked.status_code == 429, blocked.text
            assert upstream.queries == 1, "共享桶已空 ⇒ 第二个实例的查询绝不该打到上游"
    finally:
        await _purge_prefix(prefix)


@case(
    rate_limit_store="redis",
    rate_limit_store_url=DEAD_REDIS_URL,
    rate_limit_redis_timeout_seconds=0.2,
    rate_limit_query_burst=1,
    rate_limit_wait_seconds=0.0,
    query_cache_seconds=0.0,
)
async def test_redis_outage_degrades_visibly(client, app, upstream):
    """Redis 不可用 ⇒ 退回进程内桶（**仍然限速**），且 `/healthz` **如实**标记 degraded、
    `scope` 退回 `process`（不假装共享）。"""
    channel = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=channel.headers)).json()["id"]

    health = (await client.get("/healthz")).json()["rate_limit"]
    assert health["scope"] == "process", "后端不可用时不该再声称是共享的"
    assert health["backend"]["degraded"] is True, "降级必须可见（不静默）"
    assert health["backend"]["ok"] is False

    first = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
    assert first.status_code == 200, first.text
    blocked = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
    assert blocked.status_code == 429, "降级后**仍然限速**（只是不再精确）"
    assert upstream.queries == 1


@case(
    rate_limit_store="redis",
    rate_limit_store_url=DEAD_REDIS_URL,
    rate_limit_redis_timeout_seconds=0.2,
    rate_limit_fail_mode="closed",
    rate_limit_wait_seconds=0.0,
    query_cache_seconds=0.0,
)
async def test_fail_closed_rejects_when_the_backend_is_down(client, app, upstream):
    """`fail_mode=closed`：后端不可用 ⇒ **拒绝**。这是把 Redis 当可用性硬依赖的显式选择。"""
    channel = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=channel.headers)).json()["id"]
    blocked = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
    assert blocked.status_code == 429, blocked.text
    assert "FAIL_MODE=closed" in blocked.json()["error"]["message"]
    assert blocked.headers.get("Retry-After") is not None
    assert upstream.queries == 0, "fail-closed 绝不该把请求放行到上游"


@case(
    needs_redis=True,
    task_store="redis",
    task_store_url=TEST_REDIS_URL,
    rate_limit_key_prefix="t-follow",
)
async def test_store_follows_the_task_backend(client, app, upstream):
    """留空 `RATE_LIMIT_STORE` 时**跟随任务后端** —— 多副本部署少配一个旋钮、少一处错配。"""
    state = (await client.get("/healthz")).json()["rate_limit"]
    assert state["backend"]["name"] == "redis"
    assert state["scope"] == "shared"


def run_all() -> int:
    cases = sorted(
        ((name, fn) for name, fn in globals().items() if getattr(fn, "_is_case", False)),
        key=lambda item: item[0],
    )
    redis_ok, redis_detail = asyncio.run(_redis_reachable())
    if not redis_ok:
        print(
            f"\n⚠️  连不上 Redis（{TEST_REDIS_URL}）：{redis_detail}\n"
            "   需要 Redis 的用例**不会**被跳过 —— 它们记为 FAIL。理由是静默跳过等于\n"
            "   给虚假绿灯：Lua 脚本与 Redis 服务器时钟只能在真实例上被验证。\n"
            "   起一个（任选其一）：\n"
            "     redis-server --port 6379 --save '' --appendonly no\n"
            "     docker run -d -p 6379:6379 redis:7-alpine\n"
            "   或设 RATE_LIMIT_TEST_REDIS_URL 指向已有实例。\n"
        )
    failures: list[str] = []
    for name, fn in cases:
        if getattr(fn, "_needs_redis", False) and not redis_ok:
            failures.append(name)
            print(f"  FAIL  {name}\n        需要真 Redis：{redis_detail}")
            continue
        try:
            if getattr(fn, "_is_plain", False):
                fn()
            else:
                upstream = FakeUpstream()
                upstream.start()
                try:
                    asyncio.run(_run(upstream, fn, **fn._overrides))
                finally:
                    upstream.stop()
        except Exception as exc:  # noqa: BLE001 - 自带 runner 要打印任何失败
            failures.append(name)
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(cases) - len(failures)}/{len(cases)} passed")
    for name in failures:
        print(f"  - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
