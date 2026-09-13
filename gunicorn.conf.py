"""gunicorn 配置 —— 按本服务的负载形状与**状态所在**调，不是照抄模板。

本服务是**异步任务适配层**：请求本身不重（JSON 解析 + 一次上游调用），
但"一次上游调用"可能占住协程几十秒（`REQUEST_TIMEOUT_SECONDS` 默认 60s），
外加排队等待（`QUEUE_WAIT_SECONDS` 默认 20s）。这个形状决定了下面每一条。

## worker 模型：异步 worker，但**默认只开 1 个**

- 用 `uvicorn_worker.UvicornWorker`（asyncio），不用同步 worker —— 同步 worker 一次只服务
  一个请求，几个慢上游就能把池子占满。
  ⚠️ worker_class 写 `uvicorn_worker.UvicornWorker` **不是** `uvicorn.workers.UvicornWorker`：
  后者在 uvicorn 0.52 上会打印 `DeprecationWarning`（实测），官方指定改用 `uvicorn-worker` 包。
- 🔴 **`workers` 默认 1，这与同类项目的"一核一 worker"相反，理由是状态**：
  ① 并发闸门 `ConcurrencyGate` 是**进程内**状态（槽位从创建占到终态）—— 多 worker 会让
     同一渠道的并发上限被放大成 `N × limit`，闸门形同虚设；
  ② 默认任务后端是 **sqlite 单文件**，多 worker 并发写会互相抢锁。
  ⇒ 要开多 worker，必须同时满足：`TASK_STORE=redis`（共享任务表）**且**接受
  "闸门按 worker 各自计数"这一语义变化。`WEB_CONCURRENCY` 可覆盖。
  高可用靠**多副本 + 共享存储**（见 docker-compose.yml），不是靠单容器的 worker 数。

## 超时：这是**存活看门狗**，不是请求预算

`timeout` 必须**大于**单个请求可能的最长耗时（排队 20s + 上游 60s + 重试），
否则一个正在合法等待慢上游的 worker 会被 SIGKILL —— 把"慢成功"变成 502。
请求预算由 `REQUEST_TIMEOUT_SECONDS`（httpx 客户端）逐次执行，跟这里无关。

`graceful_timeout` 决定收到 SIGTERM 后还能优雅多久。它必须**同时**覆盖：
正在飞行的那次上游调用（≤ 60s）＋ 退出时的 `flush_spans()`（5s）＋ 收尾。
默认 120s，且要 **小于** 编排层的 `stop_grace_period`（compose 里给 150s），
否则编排层先 SIGKILL，优雅关机就成了摆设。

## 其余

`max_requests` + jitter 是有意的**滚动重启**：把长生命周期进程里的任何慢泄漏摊平，
jitter 防止所有 worker 在同一次请求上一起退休（那会出现一段"全都在重启"的空窗）。
`worker_tmp_dir=/tmp` 是因为容器根文件系统只读 —— 默认的心跳文件写不进去。
`preload_app=False`：httpx 连接池、logfire 导出线程、并发闸门都是**每进程**资源，
preload 会让它们跨进程共享同一个对象（fork 出来的副本各自持有一份错的状态）。
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _str_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


bind = f"0.0.0.0:{_int_env('PORT', 8000)}"

#: ⚠️ 默认 1。见模块 docstring：本服务的并发闸门是进程内状态、默认任务后端是 sqlite。
#: 想开多 worker：先切 `TASK_STORE=redis`，再改这个值。
workers = _int_env("WEB_CONCURRENCY", 1)

worker_class = "uvicorn_worker.UvicornWorker"

#: 每个 worker 同时接受的连接数上限。真正的节流在**闸门**那一层
#: （`X-Channel-Options.max_concurrency`），这里只是让过载以"排队"而不是"被拒"体现。
worker_connections = _int_env("WORKER_CONNECTIONS", 1000)

#: 存活看门狗 > 排队(20s) + 上游超时(60s) + 重试余量。
timeout = _int_env("GUNICORN_TIMEOUT", 300)

#: 优雅关机窗口：必须 > 在飞的上游调用 + flush_spans()，且 < compose 的 stop_grace_period。
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 120)

#: 高于常见反向代理的 idle 超时（多为 60s），让代理决定长连接何时结束。
keepalive = _int_env("GUNICORN_KEEPALIVE", 65)

#: 滚动重启（摊平慢泄漏）+ 抖动（避免 workers 集体退休）。
max_requests = _int_env("GUNICORN_MAX_REQUESTS", 10_000)
max_requests_jitter = _int_env("GUNICORN_MAX_REQUESTS_JITTER", 1_000)

#: 根文件系统只读 ⇒ 心跳文件必须落 tmpfs。
worker_tmp_dir = _str_env("GUNICORN_WORKER_TMP_DIR", "/tmp")

#: 突发容忍：worker 忙时先把连接收下来排队，而不是直接拒。
backlog = _int_env("GUNICORN_BACKLOG", 2048)

#: 12-factor：日志一律 stdout/stderr，由平台收；不要写文件（容器里没人去读）。
accesslog = "-"
errorlog = "-"
loglevel = _str_env("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

#: 每进程自建资源（httpx 连接池 / logfire 导出 / 并发闸门）。
preload_app = False

# 关于 worker 退出的可见性：**不挂 `worker_exit` 钩子**。
#
# 实测（gunicorn 26.2.0）：钩子在 worker **自回收**时确实会触发，但
# ① gunicorn 自己已经打了 `Worker exiting (pid: …)`，钩子只是重复一行；
# ② `master` 收到 SIGTERM 的关机路径上，worker 由信号处理器直接退出，
#    **走不到** `Arbiter.init_process()` 的 finally ⇒ 钩子不触发；
# ③ 在 worker 进程里 `worker.exitcode` 尚未由 master 回填（实测打印 `?`），
#    想靠它上报退出码是拿不到值的。
# 结论：停机期间"是否优雅排空"该由**应用自己**报 —— 见
# `adapter/observability.py::flush_spans()` 在退出时打印的那一行。
