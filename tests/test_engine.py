"""引擎端到端测试：**对着一个本地假上游跑**，零真实生成请求。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_engine.py

覆盖：创建 → 查询 → 终态；**凭证绑定**（换钥匙查必须 404 且不发上游请求）；
支出上限与本地估算；dry_run 不提交；弱校验后缀剥离；provider 断言；并发闸门。
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import pathlib
import socket
import sys
import threading

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapter.main import create_app  # noqa: E402
from adapter.settings import Settings  # noqa: E402

SCRIPT_STORE = str(ROOT / "script_store")
ADAPTER_KEY = "adapter-test-key"
UPSTREAM_KEY = "ak_test_upstream_key"
MODEL = "aivideomaker/seedance20"
# seedance20 是**动态计价**、上游又没有计费前闸门 ⇒ 默认必须显式接受"不可验证成本"才放行
# （ADR-004）。绝大多数用例只验链路，所以默认给上；专门的拒绝用例单独关掉它。
OPTS = json.dumps({"provider": "aivideomaker", "max_credits": 5000, "allow_unpriced": True})


class FakeUpstream:
    """记录收到的每个请求；可控地返回状态，绝不联网出本机。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.create_status: int | None = None
        self.create_error_body: dict | None = None
        self.next_query_status: str | None = None
        self.httpd: http.server.ThreadingHTTPServer | None = None
        self.port = 0

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"].startswith(prefix))

    def start(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        handler = functools.partial(_Handler)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
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

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
        state.requests.append(
            {
                "method": method,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

        if method == "POST" and self.path.startswith("/api/v1/generate/"):
            if state.create_status:
                return self._json(state.create_status, state.create_error_body or {"message": "boom"})
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
                # 产物放在**假上游自己**身上 —— 这样转存开关能被真正端到端地验证
                payload["output"] = {"url": f"{state.base_url}/media/{task_id}.mp4"}
            return self._json(200, payload)

        if method == "GET" and self.path.startswith("/media/"):
            media_id = self.path.rsplit("/", 1)[-1].split(".", 1)[0]
            # 故障注入：用例把 task["bad_product"] 置真，就能让"产物抓不到"这条分支被真正走到
            if state.tasks.get(media_id, {}).get("bad_product"):
                return self._json(404, {"status": "FAILED", "message": "product expired"})
            blob = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128      # 假 mp4：magic 正确即可
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return

        if method == "PUT" and self.path.endswith("/cancel"):
            return self._json(200, {"status": "CANCEL"})

        return self._json(404, {"status": "FAILED", "message": f"no route for {method} {self.path}"})


class Client:
    """把调用方会带的头一次配好。"""

    def __init__(self, upstream_url: str, *, credential: str = UPSTREAM_KEY, options: str = OPTS,
                 script_ref: str = "aivideomaker/video@v1", extra: dict | None = None):
        self.headers = {
            "X-Adapter-Key": ADAPTER_KEY,
            "X-Upstream-Url": upstream_url,
            "X-Script-Ref": script_ref,
            "X-Auth-Emit": "header:key:",
            "X-Channel-Options": options,
            "Authorization": f"Bearer {credential}",
        }
        if extra:
            self.headers.update(extra)


def make_settings(upstream: FakeUpstream, **overrides) -> Settings:
    values = {
        "adapter_key": ADAPTER_KEY,
        "upstream_allow_private_network": True,
        "upstream_trust_env": False,
        "task_store": "memory",
        "script_store_dir": SCRIPT_STORE,
        "task_key_fingerprint_secret": "test-secret",
        "default_max_concurrency": 2,
        "queue_wait_seconds": 0.3,
        "upstream_retry_attempts": 1,
        # ⚠️ 本文件测的是**链路语义**（创建 → 查询 → 终态、闸门、转存），不是降频。
        #    查询缓存（生产默认 2s）会把"连着查两次"合并成一次上游调用，从而改变
        #    "第二次查询才到终态"这类断言的节奏 —— 所以这里显式关掉它。
        #    降频行为由 tests/test_rate_limit.py 在**真实的默认配置**下单独验证。
        "query_cache_seconds": 0.0,
    }
    values.update(overrides)
    return Settings.from_env(**values)


class Harness:
    def __init__(self, upstream: FakeUpstream, settings: Settings):
        self.upstream = upstream
        self.settings = settings


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


async def run_case(upstream: FakeUpstream, case) -> None:
    settings = make_settings(upstream)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as client:
            await case(client, app, upstream)


def case(fn):
    """把 async case 包成一个可独立运行的测试。"""

    def wrapper() -> None:
        upstream = FakeUpstream()
        upstream.start()
        try:
            asyncio.run(run_case(upstream, fn))
        finally:
            upstream.stop()

    wrapper.__name__ = fn.__name__
    return wrapper


TASKS = "/api/v3/contents/generations/tasks"


# =============================================================================
# 1. 全链路：创建 → 查询 → 终态
# =============================================================================

@case
async def test_create_query_until_terminal(client, app, upstream):
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["id"].startswith("cgt-")
    assert "status" not in created, "create must not report a status (Seedance contract)"
    # 上报块：上游 task id + 脚本身份 + 请求/响应留档（不脱敏）
    assert created["upstream_task_id"] == "ck001"
    assert created["provider"] == "aivideomaker"
    assert created["script_ref"] == "aivideomaker/video@v1"
    assert created["script_sha256"]
    assert created["upstream_report"]["request"]["method"] == "POST"
    assert created["upstream_report"]["request"]["url"].endswith("/api/v1/generate/seedance20")
    assert created["upstream_report"]["request"]["body"]["prompt"] == "a cat yawning"
    assert created["upstream_report"]["response"]["status"] == 200
    assert created["upstream_report"]["response"]["body"]["taskId"] == "ck001"

    # 上游确实收到了裸 key 头 + 投影后的 body
    create_call = upstream.requests[0]
    assert create_call["headers"]["key"] == UPSTREAM_KEY
    assert "authorization" not in create_call["headers"]
    assert create_call["path"] == "/api/v1/generate/seedance20"
    assert create_call["body"] == {
        "prompt": "a cat yawning", "duration": 5, "resolution": 720, "ratio": "16:9"
    }

    first = await client.get(f"{TASKS}/{created['id']}", headers=ch.headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "running"

    second = await client.get(f"{TASKS}/{created['id']}", headers=ch.headers)
    body = second.json()
    assert body["status"] == "succeeded", body
    assert body["content"]["video_url"].endswith("/media/ck001.mp4")
    assert body["usage"]["credits"] == 15
    assert isinstance(body["created_at"], int)
    # 上报块在查询侧同样带着：上游 task id + 查询留档（不脱敏）
    assert body["upstream_task_id"] == "ck001"
    assert body["provider"] == "aivideomaker"
    assert body["upstream_report"]["query_count"] == 2
    assert body["upstream_report"]["query_response"]["body"]["status"] == "COMPLETED"
    assert body["upstream_report"]["request"]["url"].endswith("/api/v1/generate/seedance20")


# =============================================================================
# 2. 转存开关（rehost）
# =============================================================================

def _rehost_options(**extra) -> str:
    base = {"provider": "aivideomaker", "max_credits": 5000, "allow_unpriced": True}
    base.update(extra)
    return json.dumps(base)


@case
async def test_rehost_off_by_default_and_serves_the_upstream_url(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    body = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert body["content"]["video_url"].startswith(upstream.base_url)   # 透传上游地址
    assert "rehost" not in body


@case
async def test_rehost_on_stores_the_product_and_serves_our_own_url(client, app, upstream, tmp_path=None):
    ch = Client(upstream.base_url, options=_rehost_options(rehost=True))
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    body = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()

    assert body["status"] == "succeeded", body
    moved = body["rehost"]
    assert moved["ok"] is True, moved
    assert moved["bytes"] > 0
    assert moved["content_type"] == "video/mp4"
    assert moved["upstream_url"].startswith(upstream.base_url)          # 原地址留档
    # 对外给的是**自有地址**
    assert body["content"]["video_url"] == moved["url"]
    assert "/files/" in moved["url"]
    # PUBLIC_BASE_URL 没设 ⇒ 相对路径 + 如实告警（不静默）
    assert moved["url"].startswith("/files/")
    assert any("PUBLIC_BASE_URL" in w for w in body["warnings"])

    # 转存后的产物由本服务直接提供，且可重复取
    served = await client.get(moved["url"])
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("video/mp4")
    assert moved["object"] in moved["url"]


@case
async def test_rehost_is_idempotent_across_queries(client, app, upstream):
    ch = Client(upstream.base_url, options=_rehost_options(rehost=True))
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    first = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    downloads_before = upstream.count("GET", "/media/")

    third = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert third["status"] == "succeeded"
    assert upstream.count("GET", "/media/") == downloads_before, "must not re-download a stored product"
    assert third["content"]["video_url"] == first["content"]["video_url"]


@case
async def test_rehost_failure_degrades_without_failing_the_task(client, app, upstream):
    """转存是增强：抓不到产物时给上游地址 + warning，不能让成功的任务看起来失败。"""
    ch = Client(upstream.base_url, options=_rehost_options(rehost=True))
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    upstream.tasks["ck001"]["bad_product"] = True          # 让产物地址失效
    body = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert body["status"] == "succeeded"
    assert body["rehost"]["ok"] is False
    assert body["content"]["video_url"].startswith(upstream.base_url)
    assert any("rehost failed" in w for w in body["warnings"])


@case
async def test_media_route_rejects_a_traversal_name(client, app, upstream):
    for bad in ("..%2F..%2Fetc%2Fpasswd", "notahash.mp4"):
        response = await client.get(f"/files/{bad}")
        assert response.status_code == 404, bad


# =============================================================================
# 3. 凭证绑定：换一把钥匙查必须 404，且**不发上游请求**
# =============================================================================

@case
async def test_cross_credential_query_is_404_and_never_reaches_upstream(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    upstream.requests.clear()

    other = Client(upstream.base_url, credential="ak_somebody_elses_key")
    response = await client.get(f"{TASKS}/{task_id}", headers=other.headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "InvalidEndpoint.NotFound"
    assert upstream.requests == [], "must not ask upstream with the wrong key"


@case
async def test_cross_credential_delete_is_404(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    other = Client(upstream.base_url, credential="ak_somebody_elses_key")
    response = await client.delete(f"{TASKS}/{task_id}", headers=other.headers)
    assert response.status_code == 404


@case
async def test_list_only_shows_own_credential(client, app, upstream):
    ch = Client(upstream.base_url)
    await client.post(TASKS, json=_body(), headers=ch.headers)
    other = Client(upstream.base_url, credential="ak_other")
    other_body = _body(model="aivideomaker/seedance20")
    await client.post(TASKS, json=other_body, headers=other.headers)

    mine = (await client.get(TASKS, headers=ch.headers)).json()
    theirs = (await client.get(TASKS, headers=other.headers)).json()
    assert mine["total"] == 1 and theirs["total"] == 1
    assert mine["items"][0]["id"] != theirs["items"][0]["id"]


# =============================================================================
# 3. 模型名与 provider 断言
# =============================================================================

@case
async def test_unknown_model_is_400_not_defaulted(client, app, upstream):
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(model="aivideomaker/nope"), headers=ch.headers)
    assert response.status_code == 400, response.text
    assert upstream.count("POST") == 0


@case
async def test_provider_mismatch_is_400(client, app, upstream):
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(model="otherprovider/seedance20"), headers=ch.headers)
    assert response.status_code == 400
    assert "does not match" in response.json()["error"]["message"]
    assert upstream.count("POST") == 0


@case
async def test_bare_model_without_declared_provider_is_400(client, app, upstream):
    ch = Client(upstream.base_url, options=json.dumps({"max_credits": 5000}))
    response = await client.post(TASKS, json=_body(model="seedance20"), headers=ch.headers)
    assert response.status_code == 400
    assert "refusing to guess" in response.json()["error"]["message"]


# =============================================================================
# 4. 支出上限：提交前置条件 + 本地估算
# =============================================================================

@case
async def test_missing_spend_cap_is_refused_without_touching_upstream(client, app, upstream):
    ch = Client(upstream.base_url, options=json.dumps({"provider": "aivideomaker"}))
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400, response.text
    assert "refusing to submit without it" in response.json()["error"]["message"]
    assert upstream.requests == []


@case
async def test_estimate_above_cap_is_refused_before_submit(client, app, upstream):
    # 用**有计费公式**的模型：t2v 5s × 3 = 15 积分 > 上限 10 ⇒ 必须在发出请求前拒绝。
    # （seedance20 是动态计价、算不出来，见 test_dry_run… 的断言。）
    ch = Client(upstream.base_url, options=json.dumps({"provider": "aivideomaker", "max_credits": 10}))
    response = await client.post(TASKS, json=_body(model="aivideomaker/t2v", duration=5), headers=ch.headers)
    assert response.status_code == 400, response.text
    assert "exceeds the spend cap" in response.json()["error"]["message"]
    assert "15" in response.json()["error"]["message"]
    assert upstream.requests == []


# =============================================================================
# 5. dry_run：跑完整翻译但不提交
# =============================================================================

@case
async def test_dry_run_returns_payload_and_does_not_submit(client, app, upstream):
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(), headers={**ch.headers, "X-Dry-Run": "1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["upstream"]["url"] == f"{upstream.base_url}/api/v1/generate/seedance20"
    assert body["upstream"]["body"]["resolution"] == 720
    # seedance20 由服务端动态计价、公式不公开 ⇒ 本地算不出，如实为 None + 告警，不编数字
    assert body["effective"]["estimated_credits"] is None
    assert any("not verifiable" in w for w in body["warnings"])
    assert upstream.requests == []
    # 也不该留下任务
    assert (await client.get(TASKS, headers=ch.headers)).json()["total"] == 0


@case
async def test_unpriced_model_without_opt_in_is_refused_and_never_submits(client, app, upstream):
    """上游没有计费前闸门（ADR-004）：算不出成本时**默认拒绝**，而不是"提交了再说"。"""
    ch = Client(
        upstream.base_url,
        options=json.dumps({"provider": "aivideomaker", "max_credits": 5000}),
    )
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400, response.text
    assert "credit_table" in response.json()["error"]["message"]
    assert upstream.requests == []


@case
async def test_channel_credit_table_makes_a_dynamic_model_priceable(client, app, upstream):
    ch = Client(
        upstream.base_url,
        options=json.dumps(
            {"provider": "aivideomaker", "max_credits": 50, "credit_table": {"seedance20": 12}}
        ),
    )
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400, response.text          # 5 × 12 = 60 > 50
    assert "exceeds the spend cap" in response.json()["error"]["message"]
    assert upstream.requests == []


# =============================================================================
# 6. 弱校验后缀剥离
# =============================================================================

@case
async def test_weak_inline_params_are_stripped_and_applied(client, app, upstream):
    ch = Client(upstream.base_url)
    payload = _body(content=[{"type": "text", "text": "a cat --rs 720p --dur 5 --rt 16:9"}])
    payload.pop("resolution")
    payload.pop("ratio")
    payload.pop("duration")
    response = await client.post(TASKS, json=payload, headers=ch.headers)
    assert response.status_code == 200, response.text
    sent = upstream.requests[0]["body"]
    assert sent["prompt"] == "a cat", sent
    assert sent["duration"] == 5 and sent["resolution"] == 720 and sent["ratio"] == "16:9"


@case
async def test_implicit_first_last_frame_is_normalised(client, app, upstream):
    ch = Client(upstream.base_url)
    payload = _body(model="aivideomaker/minimax", resolution="720p", duration=6)
    payload["content"] = [
        {"type": "text", "text": "morph"},
        {"type": "image_url", "image_url": {"url": "a.png"}},
        {"type": "image_url", "image_url": {"url": "b.png"}},
    ]
    response = await client.post(TASKS, json=payload, headers=ch.headers)
    assert response.status_code == 200, response.text
    sent = upstream.requests[0]["body"]
    assert sent["imageUrl"] == "a.png" and sent["lastFrameUrl"] == "b.png"


# =============================================================================
# 7. 渠道契约
# =============================================================================

@case
async def test_inline_script_is_rejected(client, app, upstream):
    ch = Client(upstream.base_url, extra={"X-Script": "def create_request(ctx, p): return {}"})
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "channel_config_error"
    assert "X-Script-Ref" in response.json()["error"]["message"]


@case
async def test_wrong_adapter_key_is_401(client, app, upstream):
    ch = Client(upstream.base_url, extra={"X-Adapter-Key": "wrong"})
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 401


@case
async def test_digest_mismatch_is_channel_error(client, app, upstream):
    ch = Client(upstream.base_url, extra={"X-Script-Sha256": "0" * 64})
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "channel_config_error"
    assert "mismatch" in response.json()["error"]["message"]


@case
async def test_relative_upstream_url_is_rejected(client, app, upstream):
    ch = Client("https://aivideomaker.ai/not-v1", extra={})
    ch.headers["X-Upstream-Url"] = "not-a-url"
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "channel_config_error"


# =============================================================================
# 8. 取消 / 删除
# =============================================================================

@case
async def test_cancel_uses_put_and_marks_cancelled(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    response = await client.delete(f"{TASKS}/{task_id}", headers=ch.headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert upstream.count("PUT") == 1


@case
async def test_cancel_refuses_a_running_task(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)  # → running
    response = await client.delete(f"{TASKS}/{task_id}", headers=ch.headers)
    assert response.status_code == 400
    assert "only queued" in response.json()["error"]["message"]


@case
async def test_delete_removes_a_terminal_task(client, app, upstream):
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)   # → succeeded
    response = await client.delete(f"{TASKS}/{task_id}", headers=ch.headers)
    assert response.json() == {"id": task_id, "deleted": True}
    assert (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).status_code == 404


# =============================================================================
# 9. 上游失败的映射
# =============================================================================

@case
async def test_upstream_5xx_becomes_502(client, app, upstream):
    upstream.create_status = 500
    upstream.create_error_body = {"message": "kaboom"}
    ch = Client(upstream.base_url)
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "InternalServiceError"


@case
async def test_upstream_insufficient_credits_becomes_429(client, app, upstream):
    upstream.create_status = 200
    ch = Client(upstream.base_url)
    # 让上游回一个 2xx 的失败信封（文档只给了信封，没给状态码）
    original = _Handler._route

    def patched(self, method):
        state = self.server.state
        if method == "POST":
            self._json(200, {"status": "FAILED", "message": "Insufficient credits"})
            return
        return original(self, method)

    _Handler._route = patched
    try:
        response = await client.post(TASKS, json=_body(), headers=ch.headers)
    finally:
        _Handler._route = original
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "QuotaExceeded"


# =============================================================================
# 10. 并发闸门
# =============================================================================

@case
async def test_concurrency_gate_saturates_and_recovers(client, app, upstream):
    ch = Client(upstream.base_url, options=json.dumps({"provider": "aivideomaker", "max_credits": 5000, "max_concurrency": 1, "allow_unpriced": True}))
    first = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert first.status_code == 200

    second = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert second.status_code == 429, second.text          # 槽位占到终态
    assert "concurrency gate is full" in second.json()["error"]["message"]

    # 把第一个推到终态 → 槽位释放 → 新任务可以进
    task_id = first.json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)
    third = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert third.status_code == 200, third.text


@case
async def test_failed_task_releases_the_slot(client, app, upstream):
    ch = Client(upstream.base_url, options=json.dumps({"provider": "aivideomaker", "max_credits": 5000, "max_concurrency": 1, "allow_unpriced": True}))
    upstream.create_status = 500
    failed = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert failed.status_code == 502
    upstream.create_status = None
    retry = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert retry.status_code == 200, "a failed create must release its slot"


@case
async def test_healthz_reports_backend_and_queue(client, app, upstream):
    body = (await client.get("/healthz?deep=1")).json()
    assert body["status"] == "ok"
    assert body["task_store"] == "memory"
    assert body["credential_fingerprint"] == "hmac-sha256"
    assert "queue" in body and body["logfire"]["configured"] is False


@case
async def test_unreachable_upstream_becomes_502_envelope(client, app, upstream):
    """连不上上游 → **契约信封**的 502，而不是裸的 500（实测踩到过）。

    这类失败**没有 HTTP 应答**，所以 §10.2 那份"上游状态码 → 出口码"的映射表不适用。
    不转换的后果：`httpx.ConnectError` 一路冒到 ASGI 层，调用方拿到
    `Internal Server Error` —— 不在码表里、也没有可判别信息。
    """
    with socket.socket() as probe:          # 占一个端口再关掉：保证没人监听
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    response = await client.post(
        TASKS, json=_body(), headers=Client(f"http://127.0.0.1:{dead_port}").headers
    )
    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == "UpstreamUnavailable", error
    assert error["type"] == "InternalServerError"
    assert "Connect" in error["message"], error
    # 消息里不许带 URL（httpx 的异常原文带整条 URL，而 URL 可能含凭证查询串）
    assert str(dead_port) not in error["message"], error


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
