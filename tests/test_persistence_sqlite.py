"""P4 验收项：**重启后仍能 GET**（架构 §14 P4；playbook 用例 13）。

这是本项目最容易被"抄图片项目"抄坏的一条：图片项目的状态存储允许
"Redis 缺失则进程内 dict 降级"，那在视频侧是**直接违约** —— 重启后 404，
而调用方以为任务还在跑。这里用**两个独立的应用实例 + 同一个 sqlite 文件**
证明它真的不丢。

跑法：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_persistence_sqlite.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

import httpx

TESTS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

from adapter.main import create_app  # noqa: E402
from adapter.settings import Settings  # noqa: E402
from test_engine import (  # noqa: E402
    ADAPTER_KEY,
    MODEL,
    OPTS,
    Client,
    FakeUpstream,
    _body,
)

TASKS = "/api/v3/contents/generations/tasks"


def _settings(db_path: str) -> Settings:
    return Settings.from_env(
        adapter_key=ADAPTER_KEY,
        upstream_allow_private_network=True,
        upstream_trust_env=False,
        task_store="sqlite",
        task_store_path=db_path,
        script_store_dir=str(ROOT / "script_store"),
        task_key_fingerprint_secret="persistence-secret",
        upstream_retry_attempts=1,
        # ⚠️ 本用例测的是**跨重启的持久化**，不是降频。查询缓存（默认 2s）会把
        #    "连着查两次把任务推到终态"合并成一次上游调用，从而改变这里的断言节奏。
        #    同 `test_engine.make_settings`；降频由 tests/test_rate_limit.py 专门验证。
        query_cache_seconds=0.0,
    )


async def _run() -> None:
    upstream = FakeUpstream()
    upstream.start()
    tmpdir = tempfile.mkdtemp(dir="/tmp", prefix="video-adapter-persist-")
    db_path = f"{tmpdir}/tasks.sqlite3"
    channel = Client(upstream.base_url)

    try:
        # ---------- 第一个进程：建任务，推到终态 ----------
        app_a = create_app(_settings(db_path))
        async with app_a.router.lifespan_context(app_a):
            transport = httpx.ASGITransport(app=app_a)
            async with httpx.AsyncClient(transport=transport, base_url="http://a.test") as client:
                created = await client.post(TASKS, json=_body(), headers=channel.headers)
                assert created.status_code == 200, created.text
                task_id = created.json()["id"]

                # 再建一个**停在非终态**的任务，用来验证重启后还能回查上游
                pending = await client.post(TASKS, json=_body(), headers=channel.headers)
                pending_id = pending.json()["id"]
                assert pending_id != task_id

                await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
                final = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
                assert final.json()["status"] == "succeeded", final.text

        queries_before = upstream.count("GET", "/api/v1/tasks/")

        # ---------- 第二个进程：同一个 sqlite 文件，全新的应用实例 ----------
        app_b = create_app(_settings(db_path))
        async with app_b.router.lifespan_context(app_b):
            transport = httpx.ASGITransport(app=app_b)
            async with httpx.AsyncClient(transport=transport, base_url="http://b.test") as client:
                # 终态任务：直接由存储回答，**不再打扰上游**
                revived = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
                assert revived.status_code == 200, revived.text
                body = revived.json()
                assert body["status"] == "succeeded", body
                assert body["content"]["video_url"], "the product url must be preserved across restart"
                assert body["content"]["video_url"].endswith(".mp4")
                assert upstream.count("GET", "/api/v1/tasks/") == queries_before, (
                    "a terminal task must be answered from the store, not by re-querying upstream"
                )

                # 非终态任务：重启后仍可回查上游（说明 upstream_task_id 也保住了）
                live = await client.get(f"{TASKS}/{pending_id}", headers=channel.headers)
                assert live.status_code == 200, live.text
                assert live.json()["status"] in ("queued", "running", "succeeded")
                assert upstream.count("GET", "/api/v1/tasks/") > queries_before, (
                    "a non-terminal task must be re-queried upstream after restart"
                )

                # 凭证绑定也要跨重启生效
                other = Client(upstream.base_url, credential="ak_somebody_else")
                assert (await client.get(f"{TASKS}/{task_id}", headers=other.headers)).status_code == 404

                # 列表也跨重启可用，且仍按凭证过滤
                listing = (await client.get(TASKS, headers=channel.headers)).json()
                assert listing["total"] == 2, listing
    finally:
        upstream.stop()


def test_sqlite_survives_a_restart() -> None:
    asyncio.run(_run())


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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
