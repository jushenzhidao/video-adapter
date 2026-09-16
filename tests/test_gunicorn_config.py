"""部署参数的**不变式**测试（gunicorn.conf.py ⇄ docker-compose.yml）。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_gunicorn_config.py

为什么值得单独一套：这个文件里的每一条都是"改错不会报错、只会在故障时暴露"的类型 ——
比如把 `timeout` 调到小于上游超时，一个正在合法等待慢上游的 worker 会被 SIGKILL，
表现出来是"偶发 502"，而配置本身完全语法正确。这里的断言就是把这些关系写成可执行的门禁。

三条实测背景（2026-09-16，真 gunicorn 起服务、原始 socket 发请求）：

1. **头部大小不是瓶颈**：20KB / 70KB / 120KB / 293KB 全部进到应用层
   ⇒ `limit_request_field_size` / `worker_connections` 这类旋钮属于**安慰剂**（见下面 T3）。
2. **`worker_connections` 对 `UvicornWorker` 零作用**：`uvicorn_worker` 的源码里不读它。
3. `graceful_timeout` 必须 **小于** 编排层的 `stop_grace_period`：compose 到期直接 SIGKILL，
   优雅关机（在飞的上游调用 + `flush_spans()`）就白做了 —— 这条**跨两个文件**，最容易漂移。
"""

from __future__ import annotations

import os
import pathlib
import re
import runpy
import sys
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONF = ROOT / "gunicorn.conf.py"
COMPOSE = ROOT / "docker-compose.yml"

#: 这四项**故意不该出现在配置里**（键名 → 为什么）。
#: 2026-09-16 实测：它们要么只作用于 gunicorn 自己的解析器/worker 类型，要么被 uvicorn 忽略。
PLACEBO_KEYS = {
    "worker_connections": "UvicornWorker 源码里不读它（连接数由 uvicorn/libuv + backlog 决定）",
    "limit_request_field_size": "只作用于 gunicorn 自己的解析器（同步 worker 才走它）",
    "limit_request_fields": "同上；实测头部 293KB 都能进应用",
}


@contextmanager
def env(**values):
    """临时改环境变量（`gunicorn.conf.py` 是读 env 的，必须控制住它才谈得上"默认值"）。"""
    saved = {}
    for key, value in values.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_config(**env_values) -> dict:
    """在指定 env 下求值配置，返回模块级命名空间。"""
    with env(**env_values):
        return runpy.run_path(str(CONF))


# =============================================================================
# T1 worker 模型
# =============================================================================

def test_worker_class_is_the_asyncio_one_and_not_the_deprecated_path():
    ns = load_config()
    assert ns["worker_class"] == "uvicorn_worker.UvicornWorker", ns["worker_class"]
    # `uvicorn.workers.UvicornWorker` 在 uvicorn 0.52 上弃用（实测打 DeprecationWarning）
    assert "uvicorn.workers" not in ns["worker_class"]
    import uvicorn_worker  # 可导入 = 装机里有（requirements.txt 里有 uvicorn-worker）

    assert hasattr(uvicorn_worker, "UvicornWorker")


def test_workers_default_to_one_because_the_concurrency_gate_is_per_process():
    """并发闸门是**进程内**状态 ⇒ 多 worker 会把渠道并发上限放大成 N×limit（闸门失效）。"""
    ns = load_config(WEB_CONCURRENCY=None)
    assert ns["workers"] == 1, "默认必须 1：闸门是进程内状态"
    assert load_config(WEB_CONCURRENCY=4)["workers"] == 4, "显式开启的口子要留着"


def test_preload_is_off_because_resources_are_per_process():
    ns = load_config()
    assert ns["preload_app"] is False, "httpx 连接池 / logfire 导出线程 / 闸门都是每进程资源"


# =============================================================================
# T2 超时：由 env 推导，而不是写死
# =============================================================================

def _worst_case(ns_env: dict) -> float:
    queue = float(ns_env.get("QUEUE_WAIT_SECONDS", 20))
    request = float(ns_env.get("REQUEST_TIMEOUT_SECONDS", 60))
    attempts = max(1, int(ns_env.get("UPSTREAM_RETRY_ATTEMPTS", 3)))
    return queue + request * attempts


def test_timeout_exceeds_the_worst_case_of_a_single_request():
    """存活看门狗必须大于"排队 + 每次尝试的上游超时 × 重试次数"，否则合法等待会被 SIGKILL。"""
    ns = load_config()
    worst = _worst_case({})
    assert ns["timeout"] > worst, f"timeout={ns['timeout']} 必须 > 最坏耗时 {worst}"


def test_timeout_follows_the_request_budget_instead_of_being_a_magic_number():
    """把上游超时调大 ⇒ 看门狗必须自动跟着放大（否则"调大超时"会让人开始遇到偶发 502）。"""
    ns = load_config(REQUEST_TIMEOUT_SECONDS=120, UPSTREAM_RETRY_ATTEMPTS=3)
    worst = _worst_case({"REQUEST_TIMEOUT_SECONDS": "120"})
    assert ns["timeout"] > worst, f"推导失效：timeout={ns['timeout']} 未覆盖 {worst}"
    assert ns["timeout"] > load_config()["timeout"], "默认值不该是一个写死的常量"


def test_graceful_timeout_is_above_one_inflight_call_and_below_the_orchestrator():
    """优雅关机窗口：> 一次在飞的上游调用 + flush(5s)，且**明显小于** compose 的 stop_grace_period。

    余量取 20s 而不是"1s 就行"：compose 到期直接 SIGKILL，而中间还要走
    ① master 收到 SIGTERM → 转发给 worker；② worker 跑完 lifespan shutdown（flush_spans ≤5s）。
    留 1s 的"关系成立"在真实停机里等于没有余量。
    """
    ns = load_config()
    request = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", 60))
    stop_grace = _compose_stop_grace_seconds()
    assert ns["graceful_timeout"] > request + 5, (
        f"graceful={ns['graceful_timeout']} 必须覆盖 一次上游调用({request}s) + flush(5s)"
    )
    assert ns["graceful_timeout"] + 20 <= stop_grace, (
        f"graceful={ns['graceful_timeout']} 与 compose 的 stop_grace_period={stop_grace}s 余量不足 20s："
        "编排层会先 SIGKILL，优雅关机成了摆设。两边必须**一起**调"
    )


def _compose_stop_grace_seconds() -> int:
    """从 docker-compose.yml 里读 stop_grace_period。

    刻意用文本扫描而不是 yaml 解析：本测试**跨文件**读的是两个具体数字，
    引入一个解析依赖只为取两行不值当（注释里写明取值点，改错时看得见）。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"stop_grace_period:\s*(\d+)s", text)
    assert match, "docker-compose.yml 里找不到 stop_grace_period —— 这条跨文件约束断了"
    return int(match.group(1))


def _service_blocks() -> dict[str, str]:
    """按 2 空格缩进的顶层键切出各服务块（只为定位"哪个服务漏了加固"）。"""
    body = COMPOSE.read_text(encoding="utf-8").split("\nservices:", 1)[1]
    blocks: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        found = re.match(r"^  ([a-z0-9][a-z0-9_-]*):\s*$", line)
        if found:
            if name:
                blocks[name] = "\n".join(lines)
            name, lines = found.group(1), []
        elif name:
            lines.append(line)
    if name:
        blocks[name] = "\n".join(lines)
    return blocks


def test_every_read_only_service_mounts_tmpfs_for_slash_tmp():
    """根文件系统只读 ⇒ **每个** `read_only: true` 的服务都要把 /tmp 挂成 tmpfs。

    gunicorn 的心跳文件写在 `worker_tmp_dir`（= /tmp），写不进去 worker 会被误判为僵死。
    ⚠️ 只数"文件里有没有 `/tmp:size=`"是不够的：本文件里有两个服务都挂了 /tmp，
    删掉其中一个那种写法仍会数到 1 ⇒ 断言假绿（2026-09-16 变异自证时实测踩到）。
    所以要**按服务块**逐个查。
    """
    blocks = _service_blocks()
    assert "adapter" in blocks, f"compose 里找不到 adapter 服务（实得 {sorted(blocks)}）"
    # gunicorn 把心跳文件写在哪，compose 就必须把哪挂成 tmpfs —— 两处必须一致
    ns = load_config()
    assert ns["worker_tmp_dir"] == "/tmp", (
        f"gunicorn 的 worker_tmp_dir={ns['worker_tmp_dir']!r}；compose 只给 /tmp 挂了 tmpfs，"
        "改这里要连 compose 一起改"
    )
    hardened = {name: block for name, block in blocks.items() if "read_only: true" in block}
    assert "adapter" in hardened, "adapter 必须声明 read_only: true（加固基线）"
    for name, block in hardened.items():
        assert re.search(r"-\s*/tmp:size=\d+m", block), (
            f"服务 {name} 声明了 read_only: true，却没把 /tmp 挂成 tmpfs —— "
            "容器里 gunicorn 的心跳文件（worker_tmp_dir=/tmp）写不进去"
        )


# =============================================================================
# T3 安慰剂：不该出现的旋钮（防止"调了个没用的参数"）
# =============================================================================

def test_placebo_knobs_are_absent():
    """这些旋钮对 `UvicornWorker` 无效 ⇒ 出现即"以为调了"（回归门禁，改回去会当场红）。"""
    ns = load_config()
    present = sorted(key for key in PLACEBO_KEYS if key in ns)
    assert not present, (
        "以下旋钮对异步 worker 不生效，不该出现在配置里（各自原因见 PLACEBO_KEYS）："
        f"{ {k: PLACEBO_KEYS[k] for k in present} }"
    )


def test_no_custom_worker_module_is_referenced():
    """自定义 worker 曾经被加进来抬 h11 上限 —— 实测打在不生效的层（解析器是 httptools），已删。"""
    ns = load_config()
    assert "adapter." not in str(ns["worker_class"]), ns["worker_class"]
    assert not (ROOT / "adapter" / "http_worker.py").exists(), (
        "adapter/http_worker.py 不该存在：h11 的上限当前架构下读不到（httptools 才是活跃解析器）。"
        "真实头部上限实测 293KB 都够用，别再加回来"
    )


# =============================================================================
# T4 日志与队列
# =============================================================================

def test_logs_go_to_stdout_and_are_never_written_to_files():
    """12-factor：容器里没人去读文件，日志一律 stdout/stderr 交给平台。"""
    ns = load_config()
    assert ns["accesslog"] == "-" and ns["errorlog"] == "-"
    assert "%(M)s" in ns["access_log_format"], "访问日志要带耗时（排障第一眼要看它）"


def test_backlog_and_keepalive_are_sized_for_a_polling_client():
    ns = load_config()
    assert ns["backlog"] >= 1024, ns["backlog"]
    # 高于常见反向代理的 idle 超时（多为 60s），让代理解决定长连接何时结束
    assert ns["keepalive"] >= 60, ns["keepalive"]
    assert ns["max_requests"] > 0 and ns["max_requests_jitter"] > 0, "滚动重启 + 抖动都必须在"


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
