"""可观测性装配 + **「上报什么」的契约**（架构 §12 / OPEN-DECISIONS #12）。

这个模块要回答的不是"怎么接 logfire"，而是两件更容易做错的事：
**上报到什么细度**、**什么绝对不能上报**。下面每条立场都对应一次实测或一处已知风险。

## 1. 上报细度 = 上游 request / response 原文 + 上游 task id

每次上游调用一条 `upstream.call` 子 span，带**发出去的**与**收到的**原文；
任务级 span 带 `task.upstream_id`。

理由：适配层的失败绝大多数长在"上游到底收到了什么 / 回了什么"上
（字段名猜错、`duration` 被发成字符串、位置报错被折成 400…）。
只上报折叠后的状态码，等于把唯一的证据丢掉。
⚠️ 这是**有意识的取舍**：与 `image-adapter` 的立场相反，那里只上报折叠结果；
本项目是"给不确定的上游做投影"，原文才是排障资产。

## 2. 原文 ≠ 无防护：三道，且判据一致

| 道 | 位置 | 覆盖什么 |
|---|---|---|
| ① 源头打码 | `mask_headers` / `mask_url` / `scrub` | 已知名字的凭证头；**值里含渠道凭证的任意头**（`X-Auth-Emit` 允许渠道自选头名，名字表不可能穷举） |
| ② 同一套判据进 logfire 脱敏回调 | `_scrubbing_callback` | logfire 默认词表命中的东西：只遮凭证类，其余**放行** |
| ③ 拒收宽头抓取 | `setup_observability` | `LOGFIRE_CAPTURE_HEADERS=true` 直接拒绝装配 |

②不是可选项。**2026-09-13 在 logfire 5.1.0 实测**：脱敏器会**递归进 dict、
还会解析 JSON 字符串**，把命中的**叶子值整条替换**成 `[Scrubbed due to 'X']`
（`logfire.scrubbed` 会记下路径）。默认词表含 `cookie / secret / session / jwt / csrf /
credential / api[._-]?key / password / private[._-]?key / auth(?!ors?\b)`
⇒ 一句 `"a dog eating a cookie"` 的提示词会被换成占位符，**上报原文当场失效**。
传 callback 后：命中凭证类的照遮，其余 `return match.value` 放行。
（另一个反直觉点：**值恰好等于命中词**时脱敏器不调回调、视为安全 —— 实测
`exact="cookie"` 无回调、`"x cookie y"` 有回调。）

⚠️ 脱敏配置**不会被后续的 `logfire.configure()` 继承**（实测）：装配之后谁再 configure 一次
（挂 exporter、改采样、接 instrumentation、测试夹具），就必须显式带上
`observability.scrubbing_options()`，否则默认规则回来、正文被整条替换 —— **静默失效**。

## 3. body 要**结构化**上报，不要预先 `json.dumps`

logfire 把非原始类型属性序列化成 JSON 字符串（并附 `logfire.json_schema`），
脱敏器逐叶值处理；预序列化后只剩一个叶子，一次命中就把整个 body 换掉，分辨力归零。
（实测：`{"prompt": "…cookie…"}` 会被遮成 `"[Scrubbed due to 'cookie']"`。）

## 4. 超长是我们自己的事

Seedance 的 `content[]` 里可能塞 base64 图。逐字符串截到 `obs_body_max_chars`，
并**显式标注**被截掉多少（不静默丢）。

## 5. 关闭前必须 flush

logfire 的批量导出挂在 **daemon 线程**上、不注册 `atexit` ⇒ 退出前不 flush，
最后一批 span 直接丢。`flush_spans()` 补上这个窗口。

## 6. 不做自动埋点

`logfire.instrument_fastapi()` / `instrument_httpx()` 需要额外的
`opentelemetry-instrumentation-*` 包（本环境实测**未安装**，调用即抛），
且它们上报的属性集不由我们控制。我们要的属性自己写，依赖面收窄到 `logfire` 本身。

## 7. 装配失败只降级

没有 logfire、装配抛错、token 缺失：**服务照常起**，span 仍然构建并落到日志与
本地 sink（这就是离线校验链路）；只有"真的会外发"这一项为 false，
且 `/healthz` 把「已配置」与「真的会外发」拆成两个字段如实报告。

## 8. 任务级快照：诊断的唯一出口（2026-09-16 起）

对调用方的响应体已收敛为**种子原生字段**（`adapter/seedance.py`，创建只回 `id`、
查询不含 `model` 与任何诊断块）。原先"顺带"暴露在响应里的上游 task id、脚本摘要、
请求/响应留档、`requested` / `effective` / `warnings` / `unsupported` **全部改道这里**：
每个任务级操作（`task.create` / `task.query` / `task.cancel`）收尾时开一条
`task.snapshot` span，属性表由 `task_snapshot_attributes()` 产出。

⇒ 纪律从"响应体要如实"变成"**上报要如实且更细**"：响应体可以少，
logfire 不可以少。改这条链路时先问"这个事实在 logfire 里还查得到吗"。

## 9. 两条日志桥：stdlib `logging` 与自己不用、但宿主在用的 loguru

| 门面 | 谁在用 | 桥接 | 级别 |
|---|---|---|---|
| stdlib `logging` | **本服务自己**（`main.py` 配 basicConfig） | `_attach_logging_bridge` | WARNING+ |
| loguru | **宿主应用 / 被嵌入时**（本仓零引用） | `_attach_loguru_bridge` | 默认 INFO+，`LOGFIRE_LOGURU_LEVEL` 可调 |

两条的**级别默认值刻意不同**：本服务自己的 INFO 行基本都有对应的 span 覆盖
（"做了什么"由 span 说），所以只把"出事了"那几行发出去就够；而 loguru 是**宿主的
事件流**，它的 INFO 行没有别的地方可查 —— 漏掉就是永久丢失。嫌吵时用
`LOGFIRE_LOGURU_LEVEL=WARNING` 调回去。

两条都必须**可解释**：`/healthz.logfire.loguru_bridge` 会给出
`on:INFO` / `off: loguru is not importable (ModuleNotFoundError)` 这类一行结论 ——
宿主"我日志怎么没进 logfire"的答案只能在这里。
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

log = logging.getLogger("video_adapter.observability")

#: 逐字符串截断上限（默认 20k 字符）。可用 `OBS_BODY_MAX_CHARS` 覆盖。
DEFAULT_BODY_MAX_CHARS = 20_000

# ---------------------------------------------------------------------------
# 凭证识别：名字表 + 值包含判定
# ---------------------------------------------------------------------------

#: 归一化（去掉 `-_ .` 并小写）后**恰好等于**这些名字的头 = 凭证载体。
#:
#: 注意这里没有 `token`：`completion_tokens` / `total_tokens` 是**用量计数**，
#: 遮掉它们等于把对账字段静默删除。凭证类 token 用后缀规则单独处理（见下）。
_EXACT_CREDENTIAL_NAMES = frozenset(
    {
        "authorization",
        "proxyauthorization",
        # ⚠️ `auth` 必须在场：logfire 默认词表是 `auth(?!ors?\b)`，它在 `Authorization`
        # 里匹配到的是 **`Auth` 这四个字符**（2026-09-13 实测）。少了这一条，
        # 回调会把"凭证形状"当散文放行 —— 而凭证是在源头就打了码的，
        # 会漏的是我们自己补的那条规则。
        "auth",
        "key",                 # aivideomaker 官方线的凭证头就叫 key（X-Auth-Emit: header:key:）
        "apikey",
        "xapikey",
        "xgoogapikey",
        "xadapterkey",         # 本服务自己的准入密钥
        "xauthemit",
        "cookie",
        "setcookie",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "privatekey",
        "credential",
        "credentials",
        "jwt",
        "csrf",
        "xsrf",
        "sessionid",
        "logfiretoken",
    }
)

#: `<base>token` / `<base>secret` / `<base>key` / `<base>password` —— 只有当 base
#: 属于这份名单时才视为凭证（`access_token` 是凭证，`completion_tokens` 不是）。
_CREDENTIAL_SUFFIX_BASES = frozenset(
    {
        "access",
        "refresh",
        "id",
        "auth",
        "api",
        "bearer",
        "client",
        "secret",
        "private",
        "signing",
        "session",
        "service",
        "master",
        "admin",
    }
)

_SUFFIXES = ("token", "secret", "password", "key")

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _normalise(text: Any) -> str:
    return _NON_ALNUM.sub("", str(text or "").lower())


def is_credential_name(name: Any) -> bool:
    """这个名字（头名 / JSON 键名）是不是"凭证就住在这里"。

    名字表永远不可能穷举（渠道自选 `X-Auth-Emit` 头名），所以它只是**第一层**；
    真正的兜底是"值里含渠道凭证"那条（`mask_headers` / `scrub` 里的 `credential` 参数）。
    """
    normalised = _normalise(name)
    if not normalised:
        return False
    if normalised in _EXACT_CREDENTIAL_NAMES:
        return True
    if normalised.startswith("pylf"):
        return True
    for suffix in _SUFFIXES:
        if normalised.endswith(suffix) and len(normalised) > len(suffix):
            return normalised[: -len(suffix)] in _CREDENTIAL_SUFFIX_BASES
    return False


def mask_value(value: Any) -> str:
    """凭证的值：**只留长度**（非字符串留类型名）。

    不留前缀、不留哈希：前缀对低熵可枚举的 key 等于给出半个答案，
    裸哈希可离线爆破（`ADR-003` 已经因为同一理由改用 HMAC + 服务端密钥）。
    要"是不是同一把钥匙"的信息请读 `task.credential_id`（那是 HMAC 指纹）。
    """
    if isinstance(value, str):
        return f"[redacted {len(value)} chars]"
    if value is None:
        return "[redacted]"
    return f"[redacted {type(value).__name__}]"


def _contains_credential(text: str, credential: str) -> bool:
    return bool(credential) and credential in text


def mask_headers(headers: Mapping[str, Any] | None, *, credential: str = "") -> dict[str, str]:
    """头 → 可上报副本。凭证头（按名字）与**含凭证的头**（按值）打码，其余原文。"""
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        text = "" if value is None else str(value)
        if is_credential_name(name) or _contains_credential(text, credential):
            out[str(name)] = mask_value(text)
        else:
            out[str(name)] = text
    return out


def mask_url(url: str, *, credential: str = "") -> str:
    """URL 里出现凭证就直接整条打码（`X-Auth-Emit` 也允许把凭证放进查询串）。"""
    text = str(url or "")
    if _contains_credential(text, credential):
        return mask_value(text)
    return text


def scrub(
    value: Any,
    *,
    credential: str = "",
    max_chars: int = DEFAULT_BODY_MAX_CHARS,
    key: str = "",
) -> Any:
    """把任意结构变成可上报的副本：**原文保留**，只做两件事 —— 凭证打码、超长截断。

    - 键名是凭证名 ⇒ 该键的值整条打码（`{"api_key": "…"}`）；
    - 字符串里含渠道凭证 ⇒ 整条打码（不依赖名字表，这是真正的兜底）；
    - 超过 `max_chars` 的字符串 ⇒ 截断并**标注**被截长度；
    - 其余一律原文 —— 包括 `duration` 这类"上游写成字符串"的字段，
      排障要的正是它们本来的样子。
    """
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if is_credential_name(name):
                # 键名就是凭证名 ⇒ 整条遮掉（值可能是嵌套结构，连结构一起遮，
                # 比"只遮叶子"更保守：`{"api_key": {"value": "..."}}`
                out[name] = mask_value(raw_value)
                continue
            out[name] = scrub(
                raw_value, credential=credential, max_chars=max_chars, key=name
            )
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v, credential=credential, max_chars=max_chars, key=key) for v in value]
    if isinstance(value, str):
        if _contains_credential(value, credential):
            return mask_value(value)
        if max_chars > 0 and len(value) > max_chars:
            return f"{value[:max_chars]}…[+{len(value) - max_chars} chars truncated]"
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value


# ---------------------------------------------------------------------------
# 上报记录：不依赖任何 SDK 的本地形态
# ---------------------------------------------------------------------------


@dataclass
class SpanRecord:
    """一条上报记录。**离线校验就断言这个对象**（无需 logfire、无需网络）。"""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    status: str = "ok"
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "attributes": self.attributes,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
        }


_SPAN_SINKS: list[Callable[[SpanRecord], None]] = []


def add_span_sink(sink: Callable[[SpanRecord], None]) -> None:
    """挂一个记录接收器（测试、本地调试、或自建转发都用它）。

    这是"没有 logfire 也能校验上报内容"的实现基础：记录在**任何配置下**都会构建，
    logfire 只是**多一个出口**。这样契约测试就不会因为第三方 SDK 缺席而静默跳过。
    """
    _SPAN_SINKS.append(sink)


def remove_span_sink(sink: Callable[[SpanRecord], None]) -> None:
    try:
        _SPAN_SINKS.remove(sink)
    except ValueError:  # pragma: no cover - 容错
        pass


def clear_span_sinks() -> None:
    _SPAN_SINKS.clear()


# ---------------------------------------------------------------------------
# request_id 上下文（架构 §12：每条上报都带 request_id）
# ---------------------------------------------------------------------------

_request_id: ContextVar[str] = ContextVar("video_adapter_request_id", default="")


def bind_request_id(request_id: str) -> None:
    _request_id.set(str(request_id or ""))


def current_request_id() -> str:
    return _request_id.get()


# ---------------------------------------------------------------------------
# span 句柄
# ---------------------------------------------------------------------------


class SpanHandle:
    """一次上报的句柄。**未装配 logfire 时完全可用**（记录照建，只是不外发）。"""

    def __init__(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        *,
        secret: str = "",
        report_bodies: bool = True,
        max_chars: int = DEFAULT_BODY_MAX_CHARS,
    ) -> None:
        self.name = name
        self.attributes: dict[str, Any] = dict(attributes or {})
        self._secret = secret
        self._report_bodies = report_bodies
        self._max_chars = max_chars
        self._logfire_cm = None
        self._logfire_span = None
        self._started = 0.0
        self._error: str | None = None

    # --- 属性 ---
    def set_attribute(self, key: str, value: Any) -> None:
        clean = scrub(value, credential=self._secret, max_chars=self._max_chars, key=key)
        self.attributes[key] = clean
        if self._logfire_span is not None:
            self._logfire_span.set_attribute(key, clean)

    def set_body_attribute(self, key: str, value: Any) -> None:
        """正文类属性：受 `obs_report_bodies` 开关控制（关掉则**不出现**，不写空占位）。"""
        if not self._report_bodies:
            return
        self.set_attribute(key, value)

    def record_error(self, exc: BaseException) -> None:
        """把异常折成**打码后**的一条属性。

        ⚠️ 不转发 `span.record_exception()`：OTel 的异常事件带原始 `str(exc)`，
        而 httpx 的错误消息里就含整条 URL（可能带凭证查询串）——
        那等于绕过我们自己的打码层。
        """
        self._error = scrub(f"{type(exc).__name__}: {exc}", credential=self._secret)
        self.set_attribute("error.type", type(exc).__name__)
        self.set_attribute("error.message", self._error)

    # --- 生命周期 ---
    def __enter__(self) -> "SpanHandle":
        self._started = time.perf_counter()
        if _STATE.ready:
            try:
                import logfire

                self._logfire_cm = logfire.span(self.name, **self.attributes)
                self._logfire_span = self._logfire_cm.__enter__()
            except Exception as exc:  # noqa: BLE001 - 上报永远不能影响业务
                log.debug("logfire span 创建失败（降级为仅本地记录）：%s", exc)
                self._logfire_cm = None
                self._logfire_span = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = round((time.perf_counter() - self._started) * 1000, 2)
        if exc is not None and self._error is None:
            self.record_error(exc)
        record = SpanRecord(
            name=self.name,
            attributes=self.attributes,
            duration_ms=duration_ms,
            # `_error` 非空也算失败：发起方可能记了错误之后**继续重试**
            # （那一次尝试确实失败了，不该在 trace 里显示成 ok）。
            status="error" if (exc is not None or self._error is not None) else "ok",
            error=self._error,
        )
        _emit(record)
        if self._logfire_cm is not None:
            try:
                # 只把**类型**交给 OTel（异常原文可能含 URL / 凭证，见 record_error）
                self._logfire_cm.__exit__(None, None, None)
            except Exception as exc2:  # noqa: BLE001
                log.debug("logfire span 收尾失败：%s", exc2)
            finally:
                self._logfire_cm = None
                self._logfire_span = None
        return False


def span(name: str, *, secret: str = "", **attributes: Any) -> SpanHandle:
    """统一的 span 入口。`secret` 是渠道凭证，**只用于打码，绝不作为属性上报**。"""
    handle = SpanHandle(
        name,
        attributes,
        secret=secret,
        report_bodies=_STATE.report_bodies,
        max_chars=_STATE.body_max_chars,
    )
    request_id = current_request_id()
    if request_id:
        handle.attributes.setdefault("request.id", request_id)
    return handle


def _emit(record: SpanRecord) -> None:
    """一条记录的所有出口：sink → 日志 → （装配后另由 logfire 外发）。"""
    for sink in list(_SPAN_SINKS):
        try:
            sink(record)
        except Exception as exc:  # noqa: BLE001 - 接收器出错不该影响业务
            log.debug("span sink 失败：%s", exc)
    summary = _summary(record)
    if record.status == "error":
        log.warning("%s", summary)
    else:
        log.info("%s", summary)
    log.debug("span %s 属性=%s", record.name, json.dumps(record.to_json(), ensure_ascii=False, default=str))


def _summary(record: SpanRecord) -> str:
    """一行摘要：正文类属性只在 DEBUG 打全文，避免生产日志被 base64 淹没。"""
    attrs = record.attributes
    bits = [f"span={record.name}"]
    for key in (
        "task.id",
        "task.upstream_id",
        "task.status",
        "upstream.phase",
        "upstream.method",
        "upstream.url",
        "upstream.response.status",
        "upstream.error",
        "callback.url",
        "callback.status",
        "script.ref",
        "phase",
        "phase.ok",
    ):
        if key in attrs:
            bits.append(f"{key}={attrs[key]}")
    bits.append(f"duration_ms={record.duration_ms}")
    if record.status == "error":
        bits.append("status=error")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


@dataclass
class ObservationState:
    """`/healthz` 报告的就是这个对象（「已配置」与「真的会外发」必须分开）。"""

    ready: bool = False          # logfire 装配成功
    configured: bool = False     # 有没有 token
    emitting: bool = False       # 真的会外发
    reason: str = ""             # 没装配/不外发的原因 —— 如实说，不静默
    report_bodies: bool = True
    body_max_chars: int = DEFAULT_BODY_MAX_CHARS
    #: loguru 桥接的状态：`on:INFO` / `off:loguru is not installed` 这类一行说明。
    #: **不能只有"桥上了/没桥上"**：宿主看到自己的 loguru 日志没进 logfire 时，
    #: 第一个要问的就是"为什么不进"，而答案只有这里能说清（没装包 / 没装配 / 级别挡了）。
    loguru_bridge: str = ""

    def as_health(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "emitting": self.emitting,
            "ready": self.ready,
            "reason": self.reason,
            "loguru_bridge": self.loguru_bridge,
        }


_STATE = ObservationState()

#: 我们认识的凭证名，给 logfire 的词表补上它默认没有的那几个。
#:
#: ⚠️ 只写**名字**，不要写 `name[:=]\s*\S+` 这种吃到串尾的形状：
#: 吃到串尾 = 匹配整个值 = logfire 视为"值就是这个词"而**跳过回调**，
#: 于是最该遮的那种值反而不遮，还会遮掉本来能命中的默认规则（先前项目实测过）。
_SCRUB_EXTRA_PATTERNS: tuple[str, ...] = (
    r"x-adapter-key",
    r"x-auth-emit",
    r"\bkey\b",
)


def _leaf_name(path: Any) -> str:
    for element in reversed(list(path or ())):
        if isinstance(element, str) and element != "attributes":
            return element
    return ""


def _is_assignment_after(value: Any, match: Any) -> bool:
    """命中之后是不是紧跟 `: value` / `= value`（`name: xxx` 形状）。"""
    if not isinstance(value, str):
        return False
    rest = value[match.end():]
    return bool(re.match(r"""[A-Za-z0-9._-]*\s*["']?\s*[:=]""", rest))


def _scrubbing_callback(match: Any) -> Any:
    """logfire 脱敏回调：**只遮凭证类，其余原文放行**。

    契约（logfire 5.1.0 实测）：回调返回非 `None` 即取消这次脱敏，
    所以 `return match.value` 就是"放行原文"。默认词表会命中
    `cookie/secret/session/authorization/…`，而我们要保留正文原文
    （一句"a dog eating a cookie"不该被替换）。
    """
    matched = str(match.pattern_match.group(0))
    if not is_credential_name(matched):
        return match.value
    if matched.lower() in _leaf_name(match.path).lower():
        return None                     # 键名本身就是凭证名 ⇒ 照遮
    if _is_assignment_after(match.value, match.pattern_match):
        return None                     # `Authorization: Bearer …` 形状 ⇒ 照遮
    return match.value


def scrubbing_options() -> Any:
    """本项目的脱敏配置。**再次调用 `logfire.configure()` 时必须显式带上它。**

    🔴 2026-09-13 在 logfire 5.1.0 实测：`logfire.configure()` **不复用**上一次的
    `scrubbing=` —— 第二次 configure 不带它，默认脱敏就会回来，于是
    `"a dog eating a cookie"` 这类**正文**会被整条替换成 `[Scrubbed due to 'cookie']`，
    **上报原文当场失效而没有任何报错**。谁在装配之后又 configure 一次
    （挂 exporter、改采样、挂 instrumentation、测试夹具），就要负责把它带上：

        logfire.configure(..., scrubbing=observability.scrubbing_options())
    """
    import logfire

    return logfire.ScrubbingOptions(
        extra_patterns=list(_SCRUB_EXTRA_PATTERNS), callback=_scrubbing_callback
    )


def setup_observability(settings: Any) -> ObservationState:
    """装配上报出口。**任何失败都只降级，不阻止服务启动。**"""
    global _STATE

    configured = bool(getattr(settings, "logfire_token", ""))
    state = ObservationState(
        configured=configured,
        emitting=False,
        report_bodies=bool(getattr(settings, "obs_report_bodies", True)),
        body_max_chars=int(getattr(settings, "obs_body_max_chars", DEFAULT_BODY_MAX_CHARS)),
    )

    if getattr(settings, "logfire_capture_headers", False):
        # 拒绝装配而不是"照做"：宽头抓取会把 X-Adapter-Key、Authorization、
        # X-Auth-Emit 指向的那个头（渠道自选名字）一起抓走，
        # 而固定名字表不可能覆盖它们的并集。
        state.reason = "LOGFIRE_CAPTURE_HEADERS=true is refused; headers are masked at the source instead"
        log.error("%s —— logfire 不回外发", state.reason)
        _STATE = state
        return state

    try:
        import logfire
    except Exception as exc:  # noqa: BLE001 - 没装 logfire 是合法部署
        state.reason = f"logfire is not importable ({type(exc).__name__})"
        log.warning("未装配 logfire：%s；上报只落本地日志与 sink", state.reason)
        _STATE = state
        return state

    try:
        logfire.configure(
            token=getattr(settings, "logfire_token", "") or None,
            service_name=str(getattr(settings, "logfire_service_name", "video-adapter")),
            environment=str(getattr(settings, "environment", "") or "") or None,
            send_to_logfire="if-token-present",
            # 🔴 `console=` 只接受 `ConsoleOptions | None`，**不接受 bool**。
            # 2026-09-16 实测：`LOGFIRE_CONSOLE=true` 时 `console=True` 让
            # `logfire.configure` 抛 `AttributeError: 'bool' object has no attribute
            # 'span_style'` ⇒ 整个可观测**静默降级**（进程照常 healthy，只有
            # `/healthz.logfire.ready` 能看见）。原先写成 `bool(...)` 是照抄了旧版
            # 文档里的写法；新版 SDK 换了签名。要开就传一个真的 `ConsoleOptions`。
            console=(logfire.ConsoleOptions() if getattr(settings, "logfire_console", False) else None),
            # 不把函数参数自动塞进 span：参数里就有凭证。
            inspect_arguments=False,
            scrubbing=scrubbing_options(),
        )
    except Exception as exc:  # noqa: BLE001
        state.reason = f"logfire.configure failed ({type(exc).__name__}: {exc})"
        log.warning("logfire 装配失败，降级为本地记录：%s", exc)
        _STATE = state
        return state

    _attach_logging_bridge(logfire)
    state.loguru_bridge = _attach_loguru_bridge(logfire, level=getattr(settings, "logfire_loguru_level", "INFO"))

    state.ready = True
    state.emitting = configured       # send_to_logfire="if-token-present" + token 在场
    state.reason = "" if configured else "no LOGFIRE_TOKEN: spans stay local"
    _STATE = state
    log.info(
        "可观测已装配 | service=%s emitting=%s token=%s bodies=%s max_chars=%s loguru=%s",
        getattr(settings, "logfire_service_name", "video-adapter"),
        state.emitting,
        "present" if configured else "absent",
        state.report_bodies,
        state.body_max_chars,
        state.loguru_bridge,
    )
    return state


def _attach_logging_bridge(logfire: Any) -> None:
    """`logging` → logfire，WARNING 起。只挂一次（多次会重复外发同一条）。

    INFO 不转发：进度类日志留给平台自己的 stdout 采集，只把"出事了"那几行外发。
    """
    root = logging.getLogger()
    if any(isinstance(h, logfire.LogfireLoggingHandler) for h in root.handlers):
        return
    try:
        root.addHandler(
            logfire.LogfireLoggingHandler(level=logging.WARNING, fallback=logging.NullHandler())
        )
    except Exception as exc:  # noqa: BLE001 - 桥接不可用不影响追踪
        log.debug("logging→logfire 桥接不可用：%s", exc)


#: loguru 级别名 → OTel 级别名。loguru 比 OTel 多两个：`TRACE`（比 DEBUG 还低）与
#: `SUCCESS`（loguru 自有的"成功"档，语义上属正常事件 ⇒ 折到 `INFO`）。
#: **不做字符串透传**：`SUCCESS` / `TRACE` 不是 OTel 的级别名，直接发出去会被
#: logfire 当成未知级别（静默落进别的桶），而级别是筛选的第一把刀。
_LOGURU_LEVELS = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}

#: loguru sink 的 id（`logger.add` 的返回值）。**全局唯一**：重复挂会把同一行日志
#: 发多遍，而"发了几遍"在 logfire 上看不出来（它长得就像真的发生了多次）。
_LOGURU_SINK_ID: int | None = None


def _attach_loguru_bridge(logfire_mod: Any, *, level: str = "INFO") -> str:
    """把 **loguru** 的日志接到 logfire。返回一行状态说明（进 `/healthz`，不静默）。

    **为什么是可选依赖**：本服务自己不用 loguru（全仓 stdlib `logging`）。这个桥接存在，
    是因为这套引擎是要被**移植/嵌进别的应用**的（同源引擎就活在别的宿主里），
    而宿主普遍拿 loguru 当日志门面 —— 那些行不接，trace 里就只剩 span、
    没有"当时那几行话"，而排障恰恰靠它们。

    四条实现纪律：

    1. **不在场就静默跳过**，只返回原因。为一个不存在的包让服务起不来，与
       "装配失败只降级"（本文档 §7）直接冲突。
    2. **不夺走宿主自己的 sink**。我们只是**多加一个** sink，宿主原有的 stderr
       输出照旧 —— 接管别人的日志配置是库最不该做的事。
    3. **级别按 OTel 级别发**（见 `_LOGURU_LEVELS`），`extra` 逐键变成结构化属性，
       不做 `json.dumps`（本文档 §3）。
    4. **sink 内绝不抛**。loguru 的 `catch=True` 会兜住异常，但那是"打印到 stderr"，
       在容器里等于静默 —— 自己吞掉并降级为一条 debug 日志。
    """
    global _LOGURU_SINK_ID

    try:
        from loguru import logger as loguru_logger
    except Exception as exc:  # noqa: BLE001 - 没装 loguru 是合法部署
        return f"off: loguru is not importable ({type(exc).__name__})"

    sink_level = str(level or "INFO").strip().upper() or "INFO"
    if _LOGURU_SINK_ID is not None:
        return f"on:{sink_level} (already attached)"

    def _sink(message: Any) -> None:
        try:
            record = getattr(message, "record", None)
            if not isinstance(record, Mapping):
                return
            raw_level = str(record.get("level") or {})
            level_name = getattr(record.get("level"), "name", None) or str(raw_level)
            attributes: dict[str, Any] = {
                "loguru.name": record.get("name"),
                "loguru.level": level_name,
                "code.filepath": _loguru_file(record),
                "code.lineno": record.get("line"),
                "code.function": record.get("function"),
            }
            for key, value in (record.get("extra") or {}).items():
                attributes[f"loguru.extra.{key}"] = value
            exc = record.get("exception")
            if exc is not None:
                # 只留类型与值：traceback 文本里可能带 URL / 凭证片段，而
                # "哪一行炸的"已经由 `code.filepath` + `code.lineno` 说清了。
                attributes["exception.type"] = getattr(exc, "type", None)
                attributes["exception.message"] = str(getattr(exc, "value", "") or "")
            logfire_mod.log(
                level=_LOGURU_LEVELS.get(str(level_name).upper(), "INFO"),
                msg_template=str(record.get("message") or ""),
                attributes={k: v for k, v in attributes.items() if v not in (None, "")},
            )
        except Exception as exc2:  # noqa: BLE001 - 日志桥接绝不反向影响业务
            log.debug("loguru→logfire sink 失败：%s", exc2)

    try:
        _LOGURU_SINK_ID = loguru_logger.add(_sink, level=sink_level)
    except Exception as exc:  # noqa: BLE001
        return f"off: loguru logger.add failed ({type(exc).__name__}: {exc})"
    log.info("loguru→logfire 桥接已挂（level=%s）", sink_level)
    return f"on:{sink_level}"


def _loguru_file(record: Mapping[str, Any]) -> str:
    """loguru 的 `file` 是具名元组（`path` / `name` / `type`），取 `.path`。"""
    holder = record.get("file")
    return str(getattr(holder, "path", "") or "")


def flush_spans(timeout_millis: int = 5_000) -> bool:
    """退出前排空批量队列（daemon 线程 + 无 atexit ⇒ 不 flush 就丢）。

    **必打一行日志**：停机期间"有没有把 span 排空"是运维唯一能核的事实，
    静默成功与静默丢数据在日志上长得一样。未装配时也照实说（"未装配"≠"排空了"）。
    """
    if not _STATE.ready:
        log.info("退出前 flush：未装配 logfire，无需排空（本地记录不受影响）")
        return True
    try:
        import logfire

        flushed = bool(logfire.force_flush(timeout_millis=timeout_millis))
    except Exception as exc:  # noqa: BLE001
        log.warning("退出前 flush 失败（%s）；队列里的 span 可能丢", exc)
        return False
    if flushed:
        log.info("退出前 flush：已排空（timeout=%dms）", timeout_millis)
    else:
        log.warning("退出前 flush 在 %d ms 内未完成，队列里的 span 会丢", timeout_millis)
    return flushed


def observation_state() -> ObservationState:
    return _STATE


def reset_state() -> None:
    """测试用：把装配状态复位。**连带摘掉 loguru sink** —— 不摘的话下一个用例会
    在"已经挂了 sink"的世界里跑，而重复挂 sink 会让同一行日志发多遍（看不出差别）。"""
    global _STATE, _LOGURU_SINK_ID
    if _LOGURU_SINK_ID is not None:
        try:
            from loguru import logger as loguru_logger

            loguru_logger.remove(_LOGURU_SINK_ID)
        except Exception as exc:  # noqa: BLE001 - loguru 不在场 / 已摘掉
            log.debug("摘除 loguru sink 失败（无害）：%s", exc)
        _LOGURU_SINK_ID = None
    _STATE = ObservationState()
    clear_span_sinks()


# ---------------------------------------------------------------------------
# 上游调用的上报内容（契约在这里，调用方只负责传事实）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamTrace:
    """一次上游调用的上下文。`credential` 只用于打码，**绝不作为属性上报**。"""

    phase: str = ""
    provider: str = ""
    local_id: str = ""
    upstream_task_id: str = ""
    credential_id: str = ""
    credential: str = ""


def upstream_request_attributes(
    *,
    method: str,
    url: str,
    headers: Mapping[str, Any] | None,
    body: Any,
    attempt: int,
    idempotent: bool,
    trace: UpstreamTrace,
    report_bodies: bool = True,
) -> dict[str, Any]:
    """**发出去的**那一次调用。请求侧属性写在 span 构造里 ⇒ 失败路径也带着它们。"""
    attributes: dict[str, Any] = {
        "upstream.phase": trace.phase,
        "upstream.method": str(method or "").upper(),
        "upstream.url": mask_url(url, credential=trace.credential),
        "upstream.attempt": attempt,
        "upstream.idempotent": bool(idempotent),
    }
    if trace.provider:
        attributes["task.provider"] = trace.provider
    if trace.local_id:
        attributes["task.id"] = trace.local_id
    if trace.upstream_task_id:
        attributes["task.upstream_id"] = trace.upstream_task_id
    if trace.credential_id:
        attributes["task.credential_id"] = trace.credential_id
    request_id = current_request_id()
    if request_id:
        attributes["request.id"] = request_id
    if headers:
        attributes["upstream.request.headers"] = mask_headers(
            headers, credential=trace.credential
        )
    if report_bodies and body is not None:
        attributes["upstream.request.body"] = body
    return attributes


# ---------------------------------------------------------------------------
# 任务级快照：**被移出响应体的那些诊断，全部从这里出去**
# ---------------------------------------------------------------------------

#: 快照里**属于正文**的键（受 `OBS_REPORT_BODIES` 控制，关掉时整键不出现）。
#: 之所以要这份名单：其余键是"结论"（状态、用量、账目），这几个是"原文"（可能几十 KB）。
TASK_BODY_ATTRIBUTES = frozenset(
    {
        "task.upstream.raw",
        "task.report.request",
        "task.report.response",
    }
)


def task_snapshot_attributes(
    record: Mapping[str, Any],
    *,
    previous_status: str | None = None,
    cache_hit: bool | None = None,
    upstream_slot: str | None = None,
    report: Mapping[str, Any] | None = None,
    slot_conflict: bool = False,
    unnormalized_status: bool = False,
) -> dict[str, Any]:
    """任务记录 → 一条**任务级**上报的属性集（2026-09-16 起这是诊断的唯一出口）。

    为什么要有它：对调用方的响应体已收敛为**原生字段**（`adapter/seedance.py`），
    原先"顺带"暴露在响应里的上游 task id、脚本摘要、请求/响应留档、实际生效值、
    降级告警全部**没有别的地方可去**。它们不是可有可无的排障装饰 —— 缺了
    `task.upstream_id` 就无法与上游对工单，缺了 `task.warnings` 就查不出
    "我请求的 720p 到底有没有生效"。所以这里是**契约**，键名改名等于破坏排障面板。

    设计取舍（三条，都是刻意的）：

    1. **同一份数据既展开又整块给。** 标量叶子展开成 `task.effective.upstream_model`
       这类键（可直接做过滤/告警），同时把 `task.requested` / `task.effective` /
       `task.usage` / `task.artifacts` 整块给一份（一次看全，不用拼）。体积换可读性。
    2. **数值不编。** 拿不到的字段**不出现**（而不是写 0 / null）：上报里
       "没有这个键"与"这个键是 0"是两件事，混起来会让对账算错。
    3. **凭证永不出现。** `credential_id` 是 HMAC 指纹（能回答"是不是同一把钥匙"而不可逆），
       它**是**上报字段；凭证本身只在 `SpanHandle(secret=…)` 里用于打码。
    """
    out: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is None or value == "":
            return                      # 见取舍 2：拿不到就不出现
        out[key] = value

    put("task.id", record.get("local_id"))
    put("task.provider", record.get("provider"))
    put("task.credential_id", record.get("credential_id"))
    put("task.upstream_id", record.get("upstream_task_id"))
    put("task.script.ref", record.get("script_ref"))
    put("task.script.sha256", record.get("script_digest"))
    put("task.model", record.get("model"))
    put("task.model.bare", record.get("bare_model"))
    # 实际发上游的那个名字。模型名已改为**透传**（写什么发什么），所以它通常等于
    # `task.model.bare` —— 留着是为了让"透传被谁改过"这件事在 trace 上可证伪。
    put("task.model.upstream", upstream_slot or record.get("bare_model"))

    put("task.status", record.get("status"))
    put("task.status.previous", previous_status)
    if previous_status is not None:
        out["task.status.changed"] = previous_status != record.get("status")
    if unnormalized_status:
        # 上游报了一个不在原生六态里的状态 ⇒ 已收敛到 running。**要有人看见**：
        # 它意味着上游改了状态词表（或脚本漏了映射），而收敛会掩盖它。
        out["task.status.unnormalized"] = True
    if slot_conflict:
        out["task.model.channel_conflict"] = True
    history = record.get("status_history")
    if history:
        out["task.status.history"] = list(history)
        out["task.status.changes"] = len(history)

    put("task.created_at", record.get("created_at"))
    put("task.updated_at", record.get("updated_at"))
    put("task.expires_at", record.get("expires_at"))
    put("task.execution_expires_at", record.get("execution_expires_at"))
    put("task.callback_url", record.get("callback_url"))

    report = report if isinstance(report, Mapping) else (record.get("upstream_report") or {})
    put("task.query.count", report.get("query_count"))
    put("task.query.last_at", report.get("last_query_at"))
    if cache_hit is not None:
        # 降频的观测量（`ADR-008`）：`True` = 这次查询**一次上游请求都没发**。
        out["task.query.cache_hit"] = bool(cache_hit)

    requested = record.get("requested")
    if isinstance(requested, Mapping):
        out["task.requested"] = dict(requested)
        for key in ("model", "ratio", "resolution", "duration", "frames"):
            put(f"task.requested.{key}", requested.get(key))
    effective = record.get("effective")
    if isinstance(effective, Mapping):
        out["task.effective"] = dict(effective)
        # `upstream_model` / `model_requested` 这一对是"我请求的 vs 实际跑的"。
        # 响应体里已经没有 `model` 字段（上游模型名太乱，见 `seedance.py`）⇒
        # **只有这里**能回答"我写 doubao-seedance-2-0-260128，实际跑的是哪个槽位"。
        for key in (
            "upstream_model",
            "model_requested",
            "model_map_applied",
            # 命中的**通配模式**（含 `"*"`）。通配优先于"名字本身就是槽位名" ⇒ 它能把一个
            # 合法槽位名改写成别的槽位；没有这一项，"为什么我请求 wan27、跑的是 t2v"
            # 在 trace 里无解（响应体已收敛为原生字段，见 `ADR-011`）。
            "model_map_pattern",
            "ratio",
            "resolution",
            "duration",
            "tier",
        ):
            put(f"task.effective.{key}", effective.get(key))
        put("task.effective.estimated_credits", effective.get("estimated_credits"))
        put("task.billing.billed", effective.get("billed"))
        put("task.billing.note", effective.get("billing_note"))

    warnings = record.get("warnings")
    if warnings:
        out["task.warnings"] = list(warnings)
        out["task.warnings.count"] = len(warnings)
    unsupported = record.get("unsupported")
    if unsupported:
        out["task.unsupported"] = list(unsupported)
        out["task.unsupported.count"] = len(unsupported)

    # `view` 是脚本规范化后的片段：用量与产物都住在它里面（记录顶层没有这两项）。
    view = record.get("view") if isinstance(record.get("view"), Mapping) else {}
    usage = view.get("usage")
    if isinstance(usage, Mapping):
        out["task.usage"] = dict(usage)
        for key in ("completion_tokens", "total_tokens", "credits", "credits_charged", "credits_refunded"):
            put(f"task.usage.{key}", usage.get(key))

    view = view if isinstance(view, Mapping) else {}
    artifacts = {
        "video_url": view.get("video_url"),
        "last_frame_url": view.get("last_frame_url"),
        "file_url": view.get("file_url"),
    }
    put("task.artifacts", {k: v for k, v in artifacts.items() if v})
    for key, value in artifacts.items():
        put(f"task.artifacts.{key}", value)
    put("task.artifacts.error", view.get("error"))
    moved = record.get("rehost_result")
    if moved:
        out["task.rehost"] = dict(moved)
        put("task.rehost.upstream_url", moved.get("upstream_url"))
    if record.get("rehost"):
        out["task.rehost.enabled"] = True
    put("task.gate_key", record.get("gate_key"))
    return out


def upstream_response_attributes(
    *,
    status: int,
    headers: Mapping[str, Any] | None,
    body: Any,
    text: str = "",
    duration_ms: float,
    credential: str = "",
    report_bodies: bool = True,
) -> dict[str, Any]:
    """**收到的**那一次应答。连接失败时**不要**调用它：没有应答就没有状态码。

    伪造一个 `status=0/502` 会让"从没收到应答"与"上游回了 5xx"在上报里长得一样
    （比缺字段更有害）。
    """
    attributes: dict[str, Any] = {
        "upstream.response.status": int(status),
        "upstream.duration_ms": round(float(duration_ms), 2),
    }
    if headers:
        attributes["upstream.response.headers"] = mask_headers(headers, credential=credential)
    if report_bodies:
        if body is not None:
            attributes["upstream.response.body"] = body
        elif text:
            attributes["upstream.response.text"] = text
    return attributes
