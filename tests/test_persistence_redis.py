"""P4 验收项：**重启后仍能 GET**，且**跨实例可见**（架构 §14 P4；playbook 用例 13）。

这一条最容易被"抄图片项目"抄坏：图片侧的状态存储允许"Redis 缺失则进程内 dict 降级"，
那在视频侧是**直接违约** —— 重启后 404，而调用方以为任务还在跑。

⚠️ 2026-09-13 起任务后端只剩 **redis**（sqlite 已移除：它是单机存储，与"任务/配额跨副本
共享"的语义冲突）。所以本文件验证两件事，用两个**独立的应用实例** + 同一个 redis：

  ① 换一个全新的实例（= 重启 / 另一个副本）仍然读得到 —— 而且**终态任务由存储回答、
     不再打扰上游**，非终态任务仍能回查上游（说明 upstream_task_id 也保住了）；
  ② 凭证绑定与列表过滤**跨实例**同样生效。

跑法（需要真 Redis，默认 db 15；**连不上就 FAIL，不跳过** —— 静默跳过等于虚假绿灯）：

    redis-server --port 6379 --save '' --appendonly no      # 或用 docker
    python tests/test_persistence_redis.py

也可以用 `RATE_LIMIT_TEST_REDIS_URL` 指向已有实例（与 test_rate_limit.py 共用同一个变量，
避免两处各配一个）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import sys
import uuid

import httpx

TESTS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS_DIR))

from adapter.main import create_app  # noqa: E402
from adapter.settings import Settings  # noqa: E402
from test_engine import ADAPTER_KEY, Client, FakeUpstream, _body  # noqa: E402

TASKS = "/api/v3/contents/generations/tasks"
TEST_REDIS_URL = os.environ.get("RATE_LIMIT_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


def _settings(prefix: str) -> Settings:
    return Settings.from_env(
        adapter_key=ADAPTER_KEY,
        upstream_allow_private_network=True,
        upstream_trust_env=False,
        task_store="redis",
        task_store_url=TEST_REDIS_URL,
        # 每个用例一套**唯一前缀** ⇒ 与共享实例上的其它数据完全隔离（也便于跑完清理）。
        task_store_key_prefix=prefix,
        script_store_dir=str(ROOT / "script_store"),
        task_key_fingerprint_secret="persistence-secret",
        upstream_retry_attempts=1,
        # 本文件测的是**持久化**，不是降频：缓存会把"连着查两次推进到终态"合并成一次。
        query_cache_seconds=0.0,
    )


@contextlib.asynccontextmanager
async def _app(prefix: str):
    """起一个**完整且全新**的应用实例（含 lifespan）。"""
    app = create_app(_settings(prefix))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as client:
            yield client, app


async def _purge(prefix: str) -> None:
    """清掉本用例写进 redis 的键（测试不该在共享实例上留垃圾）。"""
    import redis.asyncio as redis_async

    client = redis_async.from_url(TEST_REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    try:
        async for key in client.scan_iter(match=f"{prefix}:*", count=200):
            await client.delete(key)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


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


async def _run() -> None:
    upstream = FakeUpstream()
    upstream.start()
    prefix = f"persist-test-{uuid.uuid4().hex[:10]}"
    channel = Client(upstream.base_url)
    try:
        # ---------- 第一个实例：建两个任务，把其中一个推到终态 ----------
        async with _app(prefix) as (client, _app_instance):
            created = await client.post(TASKS, json=_body(), headers=channel.headers)
            assert created.status_code == 200, created.text
            task_id = created.json()["id"]

            pending = await client.post(TASKS, json=_body(), headers=channel.headers)
            pending_id = pending.json()["id"]
            assert pending_id != task_id

            await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
            final = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
            assert final.json()["status"] == "succeeded", final.text

        queries_before = upstream.count("GET", "/api/v1/tasks/")

        # ---------- 第二个实例：全新的 app（= 重启 / 另一个副本），只共享 redis ----------
        async with _app(prefix) as (client, _app_instance):
            # 终态任务：直接由存储回答，**不再打扰上游**
            revived = await client.get(f"{TASKS}/{task_id}", headers=channel.headers)
            assert revived.status_code == 200, revived.text
            body = revived.json()
            assert body["status"] == "succeeded", body
            assert body["content"]["video_url"], "产物地址必须跨重启保留"
            assert body["content"]["video_url"].endswith(".mp4")
            assert upstream.count("GET", "/api/v1/tasks/") == queries_before, (
                "终态任务必须由存储回答，不该再查上游"
            )

            # 非终态任务：重启后仍可回查上游（说明 upstream_task_id 也保住了）
            live = await client.get(f"{TASKS}/{pending_id}", headers=channel.headers)
            assert live.status_code == 200, live.text
            assert live.json()["status"] in ("queued", "running", "succeeded")
            assert upstream.count("GET", "/api/v1/tasks/") > queries_before, (
                "非终态任务重启后必须能回查上游"
            )

            # 凭证绑定跨实例生效：换一把钥匙仍然 404
            other = Client(upstream.base_url, credential="ak_somebody_else")
            denied = await client.get(f"{TASKS}/{task_id}", headers=other.headers)
            assert denied.status_code == 404, denied.text

            # 列表跨实例可用，且仍按凭证过滤
            listing = (await client.get(TASKS, headers=channel.headers)).json()
            assert listing["total"] == 2, listing
    finally:
        upstream.stop()
        await _purge(prefix)


def test_start_fails_when_redis_is_unreachable() -> None:
    """连不上 redis ⇒ **启动失败**（sqlite 退路已移除）。

    这条断言的意义：`RedisStore.start()` 是"实现了但可能没接线"的典型位置 ——
    它在 lifespan 里被调用，漏调一次就会让服务"看起来正常，直到第一个请求才炸"。
    所以要用一个**连不上**的地址真跑一遍 lifespan，才算证明它接上了。
    """
    dead = "redis://127.0.0.1:1/0"  # 连接必被拒（端口 1 上没有监听）
    settings = Settings.from_env(
        adapter_key=ADAPTER_KEY,
        upstream_allow_private_network=True,
        upstream_trust_env=False,
        task_store="redis",
        task_store_url=dead,
        task_store_key_prefix="startup-probe",
        script_store_dir=str(ROOT / "script_store"),
        task_key_fingerprint_secret="persistence-secret",
    )
    app = create_app(settings)

    async def _enter() -> None:
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan 不该成功进入")  # pragma: no cover

    try:
        asyncio.run(_enter())
    except RuntimeError as exc:
        assert "cannot reach redis" in str(exc), exc
        return
    raise AssertionError("连不上 redis 时启动**必须**失败（sqlite 退路已移除）")


def test_redis_survives_a_restart() -> None:
    asyncio.run(_run())


#: 需要真 Redis 的用例（另一条用例自己就是"连不上"的场景，不需要）。
_NEEDS_REDIS = frozenset({"test_redis_survives_a_restart"})
_ALL_CASES = ("test_start_fails_when_redis_is_unreachable", "test_redis_survives_a_restart")


def _run_all() -> int:
    # ⚠️ 连不上 Redis ⇒ 需要它的用例**记为 FAIL 而不是跳过**：静默跳过等于给虚假绿灯，
    # 而"任务记录跨实例可见"恰恰是这一版唯一能证明共享语义的地方。
    ok, detail = asyncio.run(_redis_reachable())
    if not ok:
        print(
            f"\n⚠️  连不上 Redis（{TEST_REDIS_URL}）：{detail}\n"
            "   需要 Redis 的用例**不会**被跳过。起一个（任选其一）：\n"
            "     redis-server --port 6379 --save '' --appendonly no\n"
            "     docker run -d -p 6379:6379 redis:7-alpine\n"
            "   或设 RATE_LIMIT_TEST_REDIS_URL 指向已有实例。\n"
        )
    failures: list[str] = []
    for name in _ALL_CASES:
        if name in _NEEDS_REDIS and not ok:
            failures.append(name)
            print(f"  FAIL  {name}\n        需要真 Redis：{detail}")
            continue
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001 - 自带 runner 要打印任何失败
            failures.append(name)
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(_ALL_CASES) - len(failures)}/{len(_ALL_CASES)} passed")
    for name in failures:
        print(f"  - {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
