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
  并发闸门 `ConcurrencyGate` 是**进程内**状态（槽位从创建占到终态）—— 多 worker 会让
  同一渠道的并发上限被放大成 `N × limit`，闸门形同虚设。
  ⇒ 要开多 worker，必须接受"闸门按 worker 各自计数"这一语义变化。`WEB_CONCURRENCY` 可覆盖。
  高可用靠**多副本 + 共享存储**（见 docker-compose.yml），不是靠单容器的 worker 数。
  ✅ **任务表与限流桶不再是这条约束的理由**：两者都在 redis 上
  （`TASK_STORE=redis`；`RATE_LIMIT_STORE` 留空即跟随），多 worker / 多副本共享同一份状态。
  （sqlite 后端已移除 —— 它那种"多进程抢单文件锁"的问题随之消失。）

## 超时：这是**存活看门狗**，不是请求预算

`timeout` 必须**大于**单个请求可能的最长耗时（排队 20s + 上游 60s + 重试），
否则一个正在合法等待慢上游的 worker 会被 SIGKILL —— 把"慢成功"变成 502。
请求预算由 `REQUEST_TIMEOUT_SECONDS`（httpx 客户端）逐次执行，跟这里无关。

`graceful_timeout` 决定收到 SIGTERM 后还能优雅多久。它必须**同时**覆盖：
正在飞行的那次上游调用（≤ 60s）＋ 退出时的 `flush_spans()`（5s）＋ 收尾。
默认 120s，且要 **小于** 编排层的 `stop_grace_period`（compose 里给 150s），
否则编排层先 SIGKILL，优雅关机就成了摆设。

## 🔴 头部大小：实测结论 + 两个**不要调**的旋钮

2026-09-16 在真 gunicorn 上实测（原始 socket，**不走代理**）：

| 单请求头部合计 | 结果 |
| --- | --- |
| 20KB / 70KB / 120KB / **293KB** | ✅ 全部进到应用层（我们的 502 UpstreamUnavailable 就是证据） |
| 两字段各 70KB（合计 137KB） | ✅ 同上 |

⇒ **头部大小不是本服务的实际问题**：`X-Channel-Options` 的实际用量是"渠道级声明的几条到几十条
`model_map`"（约 1~3KB），离实测上限差两个数量级。所以：

| 旋钮 | 结论 |
| --- | --- |
| `limit_request_field_size`（gunicorn 默认 8190） | **故意不设**。它只作用于 gunicorn **自己的**解析器（同步 worker 走它）；异步 worker 把连接交给 uvicorn，与它无关。设了只会给人"已经按 gunicorn 那套放开了"的错觉 |
| `worker_connections` | **已删除**。`UvicornWorker` 的源码里**不读它**（`grep worker_connections uvicorn_worker/*.py` 无匹配）。连接数实际由 uvicorn/libuv + 内核 `backlog` 决定；留着它=以为连接有上限 |
| 抬高 h11 的 `max_incomplete_event_size` | **不做**。当前解析器是 **httptools**（`uvicorn[standard]` 装了它 ⇒ `http="auto"` 走 `HttpToolsProtocol`），h11 的上限根本读不到 ⇒ 改它是个 no-op。⚠️ 2026-09-16 我一度为此加了自定义 worker，**实测发现打在不生效的层，已删除**（真实上限见上表，远够用） |

⚠️ 顺带一条**排障纪律**：给 localhost 做上面这类探测时，`urllib` 会**读系统的 `HTTP_PROXY`**
（本机实测 `getproxies()` 非空），于是"超大头部"会被**代理**以 431 回掉 —— 那与 gunicorn 无关。
判据：同一个请求用**原始 socket**（天然不走代理）复测，两次结论不一致就先怀疑客户端那条路。

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

#: ⚠️ 默认 1。见模块 docstring：并发闸门是**进程内**状态（任务表与限流桶已经在 redis 上）。
#: 想开多 worker：先切 `TASK_STORE=redis`，再改这个值。
workers = _int_env("WEB_CONCURRENCY", 1)

#: ⚠️ 必须写 `uvicorn_worker.UvicornWorker`（不是 `uvicorn.workers.UvicornWorker`：
#: 后者在 uvicorn 0.52 上弃用、实测会打 DeprecationWarning）。
worker_class = "uvicorn_worker.UvicornWorker"

#: 🔴 `limit_request_field_size` / `limit_request_fields` **故意不设**：它们只作用于 gunicorn
#: 自己的解析器，对异步 worker 无效（= 安慰剂）；而头部大小实测远不是瓶颈
#: （293KB 都能进应用）。见模块 docstring 的实测表 —— 别再为此加自定义 worker。

#: 存活看门狗：排队 + **每次尝试**的上游超时 × 重试次数 + 收尾余量。
#: ⚠️ **由 env 推导**而不是写死 300：`REQUEST_TIMEOUT_SECONDS` 是常被调的旋钮，
#: 调大它却让 worker 变得可被 SIGKILL，正是"把慢成功变成 502"的经典路径。
_worst_case_seconds = (
    _int_env("QUEUE_WAIT_SECONDS", 20)
    + _int_env("REQUEST_TIMEOUT_SECONDS", 60) * max(1, _int_env("UPSTREAM_RETRY_ATTEMPTS", 3))
)
timeout = _int_env("GUNICORN_TIMEOUT", max(300, _worst_case_seconds + 60))

#: 优雅关机窗口：必须 > **一次**在飞的上游调用 + `flush_spans()`(5s)，
#: 且 **小于** 编排层的 `stop_grace_period`（compose 里给 150s；到期直接 SIGKILL，
#: 优雅关机就成了摆设）。推导式让"把上游超时调大"自动带出更长的排空窗口。
#: ⚠️ 调大 `REQUEST_TIMEOUT_SECONDS` 到 120 以上时，要**同时**把 compose 的
#: `stop_grace_period` 一起调 —— `tests/test_gunicorn_config.py` 会检查这个关系。
graceful_timeout = _int_env(
    "GUNICORN_GRACEFUL_TIMEOUT", max(120, _int_env("REQUEST_TIMEOUT_SECONDS", 60) + 30)
)

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
