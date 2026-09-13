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

    def as_health(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "emitting": self.emitting,
            "ready": self.ready,
            "reason": self.reason,
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
            console=bool(getattr(settings, "logfire_console", False)),
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

    state.ready = True
    state.emitting = configured       # send_to_logfire="if-token-present" + token 在场
    state.reason = "" if configured else "no LOGFIRE_TOKEN: spans stay local"
    _STATE = state
    log.info(
        "可观测已装配 | service=%s emitting=%s token=%s bodies=%s max_chars=%s",
        getattr(settings, "logfire_service_name", "video-adapter"),
        state.emitting,
        "present" if configured else "absent",
        state.report_bodies,
        state.body_max_chars,
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


def flush_spans(timeout_millis: int = 5_000) -> bool:
    """退出前排空批量队列（daemon 线程 + 无 atexit ⇒ 不 flush 就丢）。"""
    if not _STATE.ready:
        return True
    try:
        import logfire

        flushed = bool(logfire.force_flush(timeout_millis=timeout_millis))
    except Exception as exc:  # noqa: BLE001
        log.debug("force_flush 失败：%s", exc)
        return False
    if not flushed:
        log.warning("logfire flush 在 %d ms 内未完成，队列里的 span 会丢", timeout_millis)
    return flushed


def observation_state() -> ObservationState:
    return _STATE


def reset_state() -> None:
    """测试用：把装配状态复位。"""
    global _STATE
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
