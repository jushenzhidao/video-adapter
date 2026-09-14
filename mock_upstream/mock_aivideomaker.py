#!/usr/bin/env python3
"""aivideomaker 官方线的**独立假上游** —— 制品层零计费 E2E 的必备件。

为什么需要这个文件
------------------
本项目的假上游原先只存在于 `tests/test_engine.py` 的**进程内** `FakeUpstream` 里，
它随测试进程生灭，**无法给"容器里真正跑起来的服务"当上游**。
⇒ 没有可独立部署的 mock 时，制品层 E2E 就只能打真实上游（**计费**）。
（姊妹项目 `image-adapter` 有 `mock_upstream/`，本项目此前缺，此为回填。）

端点严格对齐 `docs/upstreams/aivideomaker-official-api.md` §2：

    POST /api/v1/generate/{model}      创建，回 taskId + 三个链接
    GET  /api/v1/tasks                 列出**当前 Key 名下**的任务（创建时间倒序）
    GET  /api/v1/tasks/{id}            详情（脚本的查询相位用的是**这个**，不是 /status）
    GET  /api/v1/tasks/{id}/status     仅状态
    PUT  /api/v1/tasks/{id}/cancel     取消（**POST / DELETE 均 405**，契约如此）
    GET  /media/{id}.mp4               假产物（验证 rehost 转存）

另有 `/__control/*` 控制面，供**外部**断言"上游实际收到了几次、收到了什么" ——
这是"限流降频真的生效了吗""错误前置真的没发上游吗"唯一有判别力的观测点
（不能信被测服务自己的计数）。

零依赖：只用标准库。可在测试里进程内起（`create_server(port=0)`），
也可单独跑进程 / 进容器（`python mock_aivideomaker.py`）。

⚠️ 与本文件配套的**契约一致性测试**是 `tests/test_mock_upstream.py`：
mock 改了路由/语义而没同步测试，会在那里红。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

#: 官方 `docs/upstreams/aivideomaker-official-api.md` §4 的 8 个合法 `{model}` 值。
#: 🔴 与 `script_store/aivideomaker/video@v1.py` 的 `OFFICIAL_MODELS` **必须是同一集合**：
#: 不一致就意味着"脚本认定的上游模型"与"上游实际接受的模型"漂移。
KNOWN_MODELS: tuple[str, ...] = (
    "t2v", "i2v", "t2v_v3", "i2v_v3", "minimax", "seedance20", "wan27", "happyhorse",
)

DEFAULT_PORT = 9000

#: 假的 mp4：magic 正确（`ftypmp42`）即可，够验证"产物真被下载过"。
#: 长度 = 12 + 128 = 140 字节（驱动脚本按这个数断言下载完整性）。
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class MockState:
    """假上游的全部状态。**加锁访问**（`ThreadingHTTPServer` ⇒ 处理函数并发）。"""

    def __init__(self, *, strict_models: bool = True) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []      # 收到的每个请求（原文留档）
        self.tasks: dict[str, dict] = {}    # task_id -> {..., "key": 创建时的 key}
        self.seq = 0
        self.strict_models = strict_models
        #: 写进**产物 URL** 的前缀。必须让**适配器**访问得到（`create_server` 会填）。
        self.public_base: str = ""
        self.port: int = 0
        self.inject: dict = {
            # 创建：让 create_status 非空即整段失败（用于构造"上游拒绝"的对照组）
            "create_status": None,
            "create_error_body": None,
            # 查询：固定回一个状态（None = 按 advance_after 自动推进）
            "query_status": None,
            "advance_after": 2,        # 第 N 次查询起返回 COMPLETED
            # 第 N 个创建请求起返回 429 + Retry-After（验证限流/冷却）
            "create_429_after": None,
            "cancel_status": None,
        }

    # --- 查询辅助 ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "tasks": {k: dict(v) for k, v in self.tasks.items()},
                "inject": dict(self.inject),
                "requests": len(self.requests),
                "strict_models": self.strict_models,
                #: 创建请求的单调序号。**被拒绝的模型名也会消耗它** ⇒ 它不等于
                #: "任务数"，而是"上游被创建接口碰过几次"。显式暴露，免得外部
                #: 用 `max(task_id)` 去猜（会差 1，本文件的测试就踩过）。
                "seq": self.seq,
            }

    def count(self, *, method: str | None = None, prefix: str = "") -> int:
        with self.lock:
            return sum(
                1 for r in self.requests
                if (method is None or r["method"] == method) and r["path"].startswith(prefix)
            )

    def filter_requests(self, *, method: str | None = None, prefix: str = "") -> list[dict]:
        with self.lock:
            return [
                r for r in self.requests
                if (method is None or r["method"] == method) and r["path"].startswith(prefix)
            ]

    def reset_observations(self) -> dict:
        """只清**观测计数**，**刻意不动业务状态**。理由见 `/__control/reset` 分支。"""
        with self.lock:
            self.requests = []
            self.inject = {
                "create_status": None, "create_error_body": None, "query_status": None,
                "advance_after": 2, "create_429_after": None, "cancel_status": None,
            }
            return {"seq_kept": self.seq, "tasks_kept": len(self.tasks)}


def _task_payload(state: MockState, task_id: str, task: dict, status: str) -> dict:
    payload = {
        "id": task_id,
        "model": task["model"],
        "status": status,
        "createdAt": task["created_at"],
        "completedAt": None,
        "input": {"duration": 5, "resolution": 720, "ratio": "16:9", "prompt": "a cat yawning"},
        "creditsCharged": 15,
    }
    if status == "COMPLETED":
        payload["completedAt"] = "2026-09-14T00:05:00.000Z"
        # 故障注入：把 bad_product 置真 ⇒ 产物抓不到（验证"转存失败只降级"）
        name = f"missing-{task_id}.mp4" if task["bad_product"] else f"{task_id}.mp4"
        payload["output"] = {"url": f"{state.public_base}/media/{name}"}
    return payload


def build_handler():
    """构造 Handler 类。状态挂在 `server.state` 上 ⇒ 同一进程可起多个互不干扰的实例。"""

    class Handler(BaseHTTPRequestHandler):  # noqa: N801
        protocol_version = "HTTP/1.1"

        @property
        def state(self) -> MockState:
            return self.server.state  # type: ignore[attr-defined]

        def log_message(self, *args) -> None:  # 静音 access log
            return

        # --- 基础回写 -----------------------------------------------------
        def _json(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, code: int, blob: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        # --- 入口 ---------------------------------------------------------
        def do_GET(self) -> None:      # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:     # noqa: N802
            self._route("POST")

        def do_PUT(self) -> None:      # noqa: N802
            self._route("PUT")

        def do_DELETE(self) -> None:   # noqa: N802
            # 上游契约：取消只认 PUT，POST/DELETE 均 405 —— 假上游必须复现这条，
            # 否则"改用 DELETE 也能过"会让适配层的 PUT 约定失去保护。
            self._json(405, {"status": "FAILED", "message": "Method Not Allowed"})

        def _route(self, method: str) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = raw.decode("utf-8", "replace")
            record = {
                "ts": time.time(),
                "method": method,
                "path": path,
                "query": parse_qs(parsed.query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
            with self.state.lock:
                self.state.requests.append(record)

            if path.startswith("/__control/"):
                return self._control(method, path, parsed.query, body)
            if path == "/__healthz":
                return self._json(200, {"ok": True, "mock": "aivideomaker",
                                        "models": list(KNOWN_MODELS)})

            # 契约 §2：取消**只认 PUT**，POST / DELETE 均 405。
            # 是 405 而不是 404 —— 路径存在、方法不对，两者对调用方的诊断价值完全不同
            # （404 会被读成"这个任务不存在"，把排障带到错方向）。
            if path.endswith("/cancel") and method in ("POST", "DELETE"):
                return self._json(405, {"status": "FAILED", "message": "Method Not Allowed"})

            # --- 业务面 ---------------------------------------------------
            if method == "POST" and path.startswith("/api/v1/generate/"):
                return self._create(path, record)
            if method == "GET" and path.startswith("/api/v1/tasks/"):
                return self._task_detail(path)
            if method == "GET" and path == "/api/v1/tasks":
                return self._task_list()
            if method == "PUT" and path.endswith("/cancel"):
                return self._cancel(path)
            if method == "GET" and path.startswith("/media/"):
                return self._media(path)
            return self._json(404, {"status": "FAILED", "message": f"no route for {method} {path}"})

        # --- 鉴权归属 -----------------------------------------------------
        def _caller_key(self) -> str:
            return (self.headers.get("key") or "").strip()

        def _owned_task(self, task_id: str) -> dict | None:
            """取任务，但**必须是当前 Key 创建的那个**。

            🔴 这条不是装饰：官方任务是**按 Key 归属**的（文档 §2）。假上游若不校验，
            适配层"跳过 `credential_id` 指纹校验、拿错钥匙去问上游"这类回归**测不出来**
            （假上游照样把任务还给它）⇒ 假绿。拒了才与上游同形。
            """
            with self.state.lock:
                task = self.state.tasks.get(task_id)
            if task is None or task["key"] != self._caller_key():
                return None
            return task

        # --- 业务实现 -----------------------------------------------------
        def _create(self, path: str, record: dict) -> None:
            inj = self.state.inject
            with self.state.lock:
                self.state.seq += 1
                seq = self.state.seq
            if inj["create_429_after"] is not None and seq > int(inj["create_429_after"]):
                return self._json(
                    429,
                    {"status": "FAILED", "message": "rate limited"},
                    {"Retry-After": "2"},
                )
            if inj["create_status"]:
                return self._json(int(inj["create_status"]),
                                  inj["create_error_body"] or {"message": "boom"})
            model = path[len("/api/v1/generate/"):]
            if self.state.strict_models and model not in KNOWN_MODELS:
                # 与官方同形的拒绝信封（HTTP 码官方文档未给，取 400）。
                # 🔴 这条是**误路由的检测器**：适配层若把模型名映射错、或未知模型没被拦下，
                # 真实上游会落到默认模型并**直接计费**；假上游必须在零成本层把这一步挡住。
                return self._json(400, {
                    "errorCode": "INVALID_MODEL",
                    "message": f"Unsupported model: {model}",
                })
            task_id = f"ck{seq:04d}"
            with self.state.lock:
                self.state.tasks[task_id] = {
                    "queries": 0,
                    "model": model,
                    "bad_product": False,
                    "key": self._caller_key(),          # 归属键（不落真凭据也无所谓：本就来自请求）
                    "created_at": "2026-09-14T00:00:00.000Z",
                }
            base = self.state.public_base
            self._json(200, {
                "status": "SUBMITTED",
                "taskId": task_id,
                "responseUrl": f"{base}/api/v1/tasks/{task_id}",
                "statusUrl": f"{base}/api/v1/tasks/{task_id}/status",
                "cancelUrl": f"{base}/api/v1/tasks/{task_id}/cancel",
            })

        def _advance(self, task: dict) -> str:
            inj = self.state.inject
            with self.state.lock:
                task["queries"] += 1
                n = task["queries"]
            return inj["query_status"] or ("COMPLETED" if n >= int(inj["advance_after"]) else "PROGRESS")

        def _task_detail(self, path: str) -> None:
            rest = path[len("/api/v1/tasks/"):]
            parts = rest.split("/")
            task_id = parts[0]
            suffix = parts[1] if len(parts) > 1 else ""
            task = self._owned_task(task_id)
            if task is None:
                return self._json(404, {"status": "FAILED", "message": "unknown task"})
            status = self._advance(task)
            if suffix == "status":
                return self._json(200, {"status": status})
            if suffix:
                return self._json(404, {"status": "FAILED", "message": f"no route {path}"})
            return self._json(200, _task_payload(self.state, task_id, task, status))

        def _task_list(self) -> None:
            """列出当前 Key 名下的任务（倒序）。

            ⚠️ **响应形状官方文档未给出**（§2 只写了用途），此处是**本 mock 的猜测**，
            不构成契约 —— 适配层不依赖它（列表由任务表本地提供），仅作人工核对用。
            """
            key = self._caller_key()
            with self.state.lock:
                mine = [(tid, dict(t)) for tid, t in self.state.tasks.items() if t["key"] == key]
            mine.reverse()   # 倒序 = 最近创建在前（task_id 单调递增）
            return self._json(200, {
                "status": "OK",
                "tasks": [_task_payload(self.state, tid, t, "SUBMITTED") for tid, t in mine],
            })

        def _cancel(self, path: str) -> None:
            task_id = path[len("/api/v1/tasks/"):].split("/")[0]
            if self._owned_task(task_id) is None:
                return self._json(404, {"status": "FAILED", "message": "unknown task"})
            if self.state.inject["cancel_status"]:
                return self._json(200, {"status": self.state.inject["cancel_status"]})
            return self._json(200, {"status": "CANCEL"})

        def _media(self, path: str) -> None:
            media_id = path[len("/media/"):].split(".", 1)[0]
            missing = media_id.startswith("missing-")
            real_id = media_id[len("missing-"):] if missing else media_id
            with self.state.lock:
                known = real_id in self.state.tasks
            if missing or not known:
                return self._json(404, {"status": "FAILED", "message": "product expired"})
            return self._bytes(200, FAKE_MP4, "video/mp4")

        # --- 控制面 -------------------------------------------------------
        def _control(self, method: str, path: str, query: str, body) -> None:
            action = path[len("/__control/"):].strip("/")
            q = parse_qs(query)
            meth = (q.get("method") or [None])[0]
            prefix = (q.get("prefix") or [""])[0]
            if action == "requests":
                items = self.state.filter_requests(method=meth, prefix=prefix)
                return self._json(200, {"count": len(items), "items": items})
            if action == "count":
                return self._json(200, {"count": self.state.count(method=meth, prefix=prefix)})
            if action == "state":
                return self._json(200, self.state.snapshot())
            if action == "reset":
                # ⚠️ **刻意不清 `tasks`、也不重置 `seq`** —— 两条都是实测踩出来的：
                #   · 清 `tasks` ⇒ 之后对这些任务的**取消**会拿到上游 404；适配层刻意
                #     **不释放并发槽位**（"上游任务可能还在跑"）⇒ 槽位泄漏，闸门用例
                #     假失败，而且从响应上看像适配器坏了。
                #   · 重置 `seq` ⇒ task_id 跨轮重复 ⇒ 产物 URL 重复 ⇒ 转存按
                #     sha256(URL)[:24] 幂等命中、**不再下载** ⇒ "产物被下载过"假失败。
                #   本控制面只负责"清空**观测计数**"，不负责"抹掉业务状态"。
                out = self.state.reset_observations()
                return self._json(200, {"reset": True, **out})
            if action == "inject":
                if not isinstance(body, dict):
                    return self._json(400, {"status": "FAILED", "message": "inject needs a JSON object"})
                with self.state.lock:
                    self.state.inject.update(body)
                    return self._json(200, {"inject": dict(self.state.inject)})
            if action == "product":
                # 把某个任务的产物置为"抓不到"，验证转存失败路径
                if not isinstance(body, dict) or not body.get("task_id"):
                    return self._json(400, {"status": "FAILED", "message": "need task_id"})
                with self.state.lock:
                    task = self.state.tasks.get(str(body["task_id"]))
                    if task is None:
                        return self._json(404, {"status": "FAILED", "message": "unknown task"})
                    task["bad_product"] = bool(body.get("bad", True))
                return self._json(200, {"task_id": body["task_id"],
                                        "bad_product": bool(body.get("bad", True))})
            return self._json(404, {"status": "FAILED", "message": f"unknown control action {action}"})

    return Handler


def create_server(host: str = "0.0.0.0", port: int = 0, *,
                  public_base: str | None = None,
                  strict_models: bool = True) -> tuple[ThreadingHTTPServer, MockState]:
    """起一个假上游。`port=0` = 让内核挑一个空闲端口（测试用）。

    `public_base` 是写进**产物 URL** 的前缀：必须让**适配器**能访问到，
    否则 rehost 相位会去抓一个够不到的地址（容器里填 `http://mock-upstream:9000`）。
    """
    server = ThreadingHTTPServer((host, port), build_handler())
    state = MockState(strict_models=strict_models)
    state.port = server.server_address[1]
    state.public_base = public_base or f"http://127.0.0.1:{state.port}"
    server.state = state                         # type: ignore[attr-defined]
    return server, state


def main() -> None:
    port = int(os.environ.get("MOCK_PORT", str(DEFAULT_PORT)))
    strict = _flag("MOCK_STRICT_MODELS", True)
    default_base = f"http://0.0.0.0:{port}"
    server, state = create_server(
        host="0.0.0.0", port=port,
        public_base=os.environ.get("MOCK_PUBLIC_BASE") or default_base,
        strict_models=strict,
    )
    print(
        f"aivideomaker fake upstream listening on :{port} "
        f"(public_base={state.public_base}, strict_models={strict}, "
        f"models={','.join(KNOWN_MODELS)})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
