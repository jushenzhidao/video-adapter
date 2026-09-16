"""Seedance 原生任务对象 —— 对调用方暴露的**唯一响应形状**。

本模块是"什么是原生"的**唯一实现点**。三条立场，每条都对应一次实测或一次修正：

## 1. 创建只回 `{"id": ...}`

原生 `POST /contents/generations/tasks` 的成功体**只有 `id`**（`seedance-api-reference.md` §3.3）。
2026-09-16 的实测报告里曾把本地多带的 `provider` / `upstream_task_id` / `script_ref` /
`script_sha256` / `upstream_report` 一并描述成"与原生契约一致 —— 只回 id + 上报块"，
**那句是错的**：原生没有上报块。调用方按原生契约写解析时，多出来的键是噪音；
按原生契约写**校验**（严格模式、schema 校验、SDK 反序列化）时，它们是失败源。

## 2. 查询体逐字段对齐原生，且**不含 `model`**

`render_task()` 只产出 `seedance-api-reference.md` §4.1 列出的字段。
刻意**不产出**的非原生键（原先在响应里）：

    model              上游模型名很乱（8 个槽位名 + 各家原生 ID 混写），且本层已改为
                       **模型名透传**（写什么发什么）⇒ 回显它对调用方没有信息增量
    provider / upstream_task_id / script_ref / script_sha256
    upstream_report / upstream / requested / effective / warnings / unsupported / rehost

它们**全部改道 logfire**（`observability.task_snapshot_attributes` 一份不落），
排障能力不减，只是不再污染对调用方的契约。⚠️ 代价是**响应体不再是降级告知通道**
（`ADR-009` D7 原文要求"改动必须在响应体显式告知"）—— 该条已随本次改动回写为
"降级告知的出口是 logfire 与 dry-run，响应体只对调用方承诺原生字段"。
调用方若需要"我请求的 vs 我实际得到的"，读 `dry_run`（`X-Dry-Run: 1`）或 logfire。

## 3. `status` 只能是原生六态

`queued / running / succeeded / failed / expired / cancelled`。
未知值一律**收敛到 `running`**（非终态）—— "不认识的中间态"不该被当成终态，
因为终态会触发落库、释放并发槽位与回调推送。`coerce_status` 的第二个返回值
把这个事实告诉调用方（引擎会把它记进 logfire），**不打进响应体**（那不是原生字段）。

## 4. `resolution` 归一成原生写法

原生给的是 `480p` / `720p` / `1080p`（小写带 p）。上游侧三种写法并存
（`720` / `"720p"` / `"720P"`）⇒ 出口统一归一，与上游大小写解耦。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: 原生六态（`seedance-api-reference.md` §5）。**枚举顺序即状态机顺序**。
ARK_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "expired",
    "cancelled",
)

#: 终态。`cancelled` 在其中（原生：终态任务 `DELETE` = 删除记录，而非取消）。
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "expired", "cancelled"})

#: 收敛未知状态时的落点。**不能是终态** —— 见模块 docstring §3。
UNKNOWN_STATUS_FALLBACK = "running"

#: 原生默认值（`seedance-api-reference.md` §3.1）。
DEFAULT_SERVICE_TIER = "default"
DEFAULT_EXECUTION_EXPIRES_AFTER = 172800
DEFAULT_PRIORITY = 0
DEFAULT_SEED = -1
#: 上游输出帧率固定不可调（原生文档 §7.4）。上游契约未给可变量，故为常量。
FPS = 24

#: `render_task()` 产出的顶层键，**顺序即"重要度"**：先 `id` / `status` / `content`
#: （调用方真正要读的三个），再账目与规格回显。契约测试按这份清单逐键断言。
NATIVE_TASK_KEYS: tuple[str, ...] = (
    "id",
    "status",
    "content",
    "error",
    "usage",
    "created_at",
    "updated_at",
    "seed",
    "resolution",
    "ratio",
    "duration",
    "frames",
    "framespersecond",
    "service_tier",
    "execution_expires_after",
    "generate_audio",
    "draft",
    "priority",
)

#: `content` 子对象（原生 §4.1）。三个键恒在，未产出时为 `null`。
NATIVE_CONTENT_KEYS: tuple[str, ...] = ("video_url", "last_frame_url", "file_url")

#: `usage` 暴露给调用方的字段。**只有两个** —— 上游的积分字段（`credits` /
#: `credits_charged` / `credits_refunded`）不进响应体，它们属于对账口径（ADR-010：
#: 计费归属 new-api），本层只把上游**实收**积分按渠道倍率折算成 token 当量。
NATIVE_USAGE_KEYS: tuple[str, ...] = ("completion_tokens", "total_tokens")

_RESOLUTION_RE = re.compile(r"(\d{3,4})")


def coerce_status(value: Any) -> tuple[str, bool]:
    """任意上游状态 → `(原生六态之一, 是否发生了收敛)`。

    第二个返回值是给**上报**用的：收敛意味着"这个上游又报了一个我们不认识的状态"，
    属于需要人看一眼的事（上游改了状态词表 / 脚本漏了映射），但它不该出现在响应体里。
    """
    text = str(value or "").strip().lower()
    if text in ARK_STATUSES:
        return text, False
    return UNKNOWN_STATUS_FALLBACK, True


def native_resolution(value: Any) -> str | None:
    """分辨率 → 原生写法（`720` / `"720P"` / `"1080p"` 一律 → `720p` / `1080p`）。

    取不出数字时返回 `None`（**不编一个默认分辨率**：`null` 是"不知道"，
    编成 `720p` 会让调用方以为上游真的产出了 720p）。
    """
    match = _RESOLUTION_RE.search(str(value or ""))
    return f"{match.group(1)}p" if match else None


def native_usage(usage: Any) -> dict[str, int] | None:
    """上游用量 → 原生 `usage`（只留 token 两项）。取不到整数时 `null`。"""
    if not isinstance(usage, Mapping):
        return None
    out: dict[str, int] = {}
    for key in NATIVE_USAGE_KEYS:
        try:
            out[key] = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return None
    return out


def create_echo(payload: Mapping[str, Any], effective: Mapping[str, Any] | None) -> dict:
    """创建时就能确定的那部分原生字段。

    原生 `POST` 不回这些，但紧接着的 `GET` 必须给 —— 否则调用方第一次轮询会拿到
    一整排 `null`，分不清"参数没生效"和"还没开始算"。

    两类字段的来源刻意不同：

    | 类别 | 字段 | 取谁 |
    | --- | --- | --- |
    | 本层原样带过去的调优项 | `service_tier` / `execution_expires_after` / `priority` | **请求值**（或原生默认） |
    | 描述产物的规格 | `resolution` / `ratio` / `duration` / `generate_audio` / `draft` / `seed` | **实际生效值** |

    第二类取"实际生效"而不是"请求值"：`generate_audio` 若回显调用方写的 `true`，
    就是一个**假的承诺**（本上游无一支持生成有声视频）。⚠️ 这也是"响应体不再是降级
    告知通道"的代价所在 —— 调用方看到 `false` 时无法从响应体知道"是因为上游不支持"，
    那个理由在 logfire 的 `task.effective.*` 与 `task.warnings`（dry-run 亦可）。
    """
    effective = effective if isinstance(effective, Mapping) else {}
    payload = payload if isinstance(payload, Mapping) else {}

    def _echo_int(key: str, default: int) -> int:
        raw = payload.get(key)
        if raw in (None, ""):
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    return {
        "seed": DEFAULT_SEED,
        "resolution": native_resolution(effective.get("resolution")),
        "ratio": effective.get("ratio"),
        "duration": _echo_int("duration", 0) or _as_int(effective.get("duration")),
        "frames": None,
        "framespersecond": FPS,
        "service_tier": str(payload.get("service_tier") or DEFAULT_SERVICE_TIER),
        "execution_expires_after": _echo_int(
            "execution_expires_after", DEFAULT_EXECUTION_EXPIRES_AFTER
        ),
        # 上游没有这两项能力 ⇒ 实际生效值恒为 False（见 docstring 表格）。
        "generate_audio": False,
        "draft": False,
        "priority": _echo_int("priority", DEFAULT_PRIORITY),
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def render_task(record: Mapping[str, Any]) -> dict[str, Any]:
    """任务记录 → 原生任务对象。**上游原记录、诊断块、上报块一概不出现在这里。**

    `record["view"]` 是脚本 `query_response` 相位给出的规范化片段；查询还没发生时
    （刚创建）它为空，此时用创建时算出的 `record["native"]` 回显 —— 两条来源按字段
    逐项 fallback，所以"刚创建就查"与"查过之后"拿到的是**同一套键**。
    """
    view = record.get("view") if isinstance(record.get("view"), Mapping) else {}
    echo = record.get("native") if isinstance(record.get("native"), Mapping) else {}
    status, _unknown = coerce_status(record.get("status") or view.get("status"))

    video_url = view.get("video_url") if status == "succeeded" else None
    rehost = record.get("rehost_result") or {}
    if video_url and rehost.get("ok"):
        # 转存成功 ⇒ 对外给**自有地址**；上游原地址仍在 logfire 的 task.rehost 里可查。
        video_url = rehost["url"]

    return {
        "id": record.get("local_id"),
        "status": status,
        "content": {
            "video_url": video_url,
            "last_frame_url": view.get("last_frame_url"),
            "file_url": view.get("file_url"),
        },
        "error": view.get("error"),
        "usage": native_usage(view.get("usage")),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "seed": _first_not_none(view.get("seed"), echo.get("seed"), DEFAULT_SEED),
        "resolution": _first_not_none(
            native_resolution(view.get("resolution")), echo.get("resolution")
        ),
        "ratio": _first_not_none(view.get("ratio"), echo.get("ratio")),
        "duration": _first_not_none(view.get("duration"), echo.get("duration")),
        "frames": view.get("frames"),
        "framespersecond": _first_not_none(view.get("framespersecond"), echo.get("framespersecond")),
        "service_tier": _first_not_none(echo.get("service_tier"), DEFAULT_SERVICE_TIER),
        "execution_expires_after": _first_not_none(
            echo.get("execution_expires_after"), DEFAULT_EXECUTION_EXPIRES_AFTER
        ),
        "generate_audio": _first_not_none(echo.get("generate_audio"), False),
        "draft": _first_not_none(echo.get("draft"), False),
        "priority": _first_not_none(echo.get("priority"), DEFAULT_PRIORITY),
    }


def render_created(record: Mapping[str, Any]) -> dict[str, Any]:
    """创建响应 = `{"id": ...}`，**没有别的键、没有 status**（原生 §3.3）。"""
    return {"id": record.get("local_id")}
