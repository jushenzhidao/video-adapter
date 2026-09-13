"""把 README「接入示例」那一节的每条示例**真跑一遍**（本地假上游 + 真 HTTP）。

跑法：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python scripts/verify_docs_examples.py

为什么要有这个脚本：文档里的 curl 会腐烂（头名改了、状态码改了、响应字段改名了），
而**没人会因为文档过期而收到告警**。这里把那一节变成可执行断言 —— 四类文档：
正文里的示例、正文里的状态码表、正文里的字段名，都在这条链路里过一遍。

零成本：只打到**本地假上游**（`tests/test_engine.py` 的 `FakeUpstream`），
不碰任何真实上游、不发任何生成请求、不计费。
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402

from test_engine import FakeUpstream  # noqa: E402

ADAPTER_KEY = "ak_demo_adapter_key"
UPSTREAM_KEY = "ak_upstream_demo_key"
OPTIONS = (
    '{"provider":"aivideomaker","max_credits":5000,'
    '"max_concurrency":4,"allow_unpriced":true}'
)
TASKS = "/api/v3/contents/generations/tasks"
BODY = {
    "model": "aivideomaker/seedance20",
    "content": [{"type": "text", "text": "一只猫在打哈欠"}],
    "duration": 5,
    "resolution": "720p",
    "ratio": "16:9",
}

_checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    _checks.append((bool(ok), label))
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")


def main() -> int:
    # 假上游：端口由它自己挑（0 = 让内核分配），避免和本机在跑的东西撞。
    upstream = FakeUpstream()
    upstream.start()

    # 适配层：真 uvicorn、真 HTTP、真头解析（不用 ASGITransport —— 那样测不到真实传输层）。
    import os
    import socket

    os.environ.update(
        {
            "ADAPTER_KEY": ADAPTER_KEY,
            "TASK_KEY_FINGERPRINT_SECRET": "demo-fingerprint-secret",
            "TASK_STORE": "memory",
            "SCRIPT_STORE_DIR": str(ROOT / "script_store"),
            "LOG_LEVEL": "WARNING",
            "UPSTREAM_ALLOW_PRIVATE_NETWORK": "1",   # 假上游在 127.0.0.1
            "UPSTREAM_TRUST_ENV": "0",
            "UPSTREAM_RETRY_ATTEMPTS": "1",
            "REQUEST_TIMEOUT_SECONDS": "10",
        }
    )

    import uvicorn

    # 🔴 用 `create_app(settings)` 拿**实例**交给 uvicorn，不要写 "adapter.main:app" 字符串。
    # 上面的 `from test_engine import FakeUpstream` 已经 import 过 `adapter.main`，那个
    # 模块级 `app` 是在**设置这些环境变量之前**用当时的 env 建好的 ⇒ 用字符串启动等于
    # 跑旧配置，表现为"每个用例都 401，而服务本身没问题"（实测踩到）。
    from adapter.main import create_app
    from adapter.settings import Settings

    settings = Settings.from_env()
    app = create_app(settings)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        adapter_port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=adapter_port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.2)

    base = f"http://127.0.0.1:{adapter_port}"
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": upstream.base_url,
        "X-Script-Ref": "aivideomaker/video@v1",
        "X-Auth-Emit": "header:key:",
        "X-Channel-Options": OPTIONS,
        "Authorization": f"Bearer {UPSTREAM_KEY}",
        "Content-Type": "application/json",
    }

    # trust_env=False：环境里的 HTTP_PROXY 会把回环地址也代理走（会把结论带偏）。
    with httpx.Client(trust_env=False, timeout=20.0, base_url=base) as client:
        try:
            print("\n[0] dry-run：跑完整翻译与计费校验，不发上游请求")
            before = upstream.count("POST", "/api/v1/generate")
            r = client.post(TASKS, json=BODY, headers={**headers, "X-Dry-Run": "1"})
            check(r.status_code == 200, f"dry-run → 200（实得 {r.status_code}）")
            dry = r.json()
            check(dry.get("dry_run") is True, "响应里 dry_run=true")
            check("prompt" in (dry.get("upstream", {}).get("body") or {}), "响应里带将发出的 upstream.body")
            check(upstream.count("POST", "/api/v1/generate") == before, "dry-run **没有**发出上游请求")

            print("\n[1] 创建 → 回 id（+ 上报块）")
            r = client.post(TASKS, json=BODY, headers=headers)
            check(r.status_code == 200, f"创建 → 200（实得 {r.status_code}）")
            created = r.json()
            task_id = created.get("id", "")
            check(task_id.startswith("cgt-"), f"id 形状 cgt-<时间戳>-<随机>（实得 {task_id}）")
            check("status" not in created, "创建响应**没有** status（契约：只能轮询或走回调）")
            check(created.get("upstream_task_id") == "ck001", "上报块带上游 task id")
            check("upstream_report" in created, "上报块带 request/response 留档")

            print("\n[2] 查询（同一把 Key）→ 六态；终态带产物地址")
            r1 = client.get(f"{TASKS}/{task_id}", headers=headers)
            check(r1.status_code == 200 and r1.json()["status"] == "running", "首次查询 → running")
            # ⚠️ 第二次查询必须**跨过查询缓存窗口**再发（`QUERY_CACHE_SECONDS`，默认 2s）。
            #    窗口内的重复查询会被刻意回放同一份快照 —— 那是降频机制（§7.1）在正常工作，
            #    不是缺陷。对调用方的含义：**轮询间隔应 ≥ QUERY_CACHE_SECONDS**。
            #    这里从 settings 取窗口值而不是写死 2.0，避免与默认值漂移。
            time.sleep(settings.query_cache_seconds + 0.2)
            r2 = client.get(f"{TASKS}/{task_id}", headers=headers)
            done = r2.json()
            check(done["status"] == "succeeded", "二次查询 → succeeded")
            check(bool(done["content"]["video_url"]), "终态 content.video_url 非空")
            check(done["usage"]["credits"] == 15, "usage 带原始积分（对账用）")

            print("\n[3] 列表")
            r = client.get(f"{TASKS}?page_num=1&page_size=5", headers=headers)
            page = r.json()
            check(r.status_code == 200 and page["total"] >= 1, "列表返回 items/total")

            print("\n[4] 取消（只有 queued 可取消）")
            second = client.post(TASKS, json=BODY, headers=headers).json()["id"]
            r = client.delete(f"{TASKS}/{second}", headers=headers)
            check(r.status_code == 200 and r.json()["status"] == "cancelled", "DELETE → cancelled")
            r = client.delete(f"{TASKS}/{task_id}", headers=headers)
            check(r.status_code == 200 and r.json().get("deleted") is True, "对终态任务 → 删除记录")

            print("\n[5] 错误路径（README 的错误表逐行核对）")
            no_cap = {**headers, "X-Channel-Options": '{"provider":"aivideomaker"}'}
            r = client.post(TASKS, json=BODY, headers=no_cap)
            check(
                r.status_code == 400 and r.json()["error"]["code"] == "InvalidParameter",
                "拿不到 max_credits → 400 InvalidParameter",
            )
            wrong_provider = {**headers, "X-Channel-Options": OPTIONS}
            r = client.post(TASKS, json={**BODY, "model": "someone-else/seedance20"}, headers=wrong_provider)
            check(
                r.status_code == 400 and r.json()["error"]["param"] == "model",
                "model 的 provider 与渠道不一致 → 400（param=model）",
            )
            r = client.get(f"{TASKS}/cgt-20260101000000-zzzzzz", headers=headers)
            check(
                r.status_code == 404 and r.json()["error"]["code"] == "InvalidEndpoint.NotFound",
                "未知任务 → 404 InvalidEndpoint.NotFound",
            )
            r = client.get(f"{TASKS}?page_num=1", headers={**headers, "X-Adapter-Key": "wrong"})
            check(
                r.status_code == 401 and r.json()["error"]["code"] == "AuthenticationError",
                "准入密钥错 → 401 AuthenticationError",
            )
            with __import__("socket").socket() as probe:
                probe.bind(("127.0.0.1", 0))
                dead = probe.getsockname()[1]
            r = client.post(TASKS, json=BODY, headers={**headers, "X-Upstream-Url": f"http://127.0.0.1:{dead}"})
            check(
                r.status_code == 502 and r.json()["error"]["code"] == "UpstreamUnavailable",
                "上游不可达 → 502 UpstreamUnavailable（不是裸 500）",
            )
            upstream.create_status = 500
            r = client.post(TASKS, json=BODY, headers=headers)
            check(
                r.status_code == 502 and r.json()["error"]["code"] == "InternalServiceError",
                "上游 5xx → 502 InternalServiceError",
            )
        finally:
            upstream.stop()

    failed = [label for ok, label in _checks if not ok]
    print(f"\n{len(_checks) - len(failed)}/{len(_checks)} 项通过")
    for label in failed:
        print(f"  - 与文档不一致：{label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
