"""aivideomaker/video@v1: aivideomaker 官方 API 线 → 火山 Seedance 视频生成协议。

渠道配置（控制面）：

    X-Upstream-Url:    https://aivideomaker.ai            # base 即可，脚本自己拼 /api/v1/...
                       （写成 .../api/v1/generate/{model} 也认，见 _upstream_url）
    X-Script-Ref:      aivideomaker/video@v1
    key:               <AIVIDEOMAKER_API_KEY>             # ⚠️ 裸 key 头，无 Bearer
    X-Auth-Emit:       header:key:
    X-Channel-Options: {"max_credits": 300}               # 必填，见"支出上限"
    可选：{"webhook_url": "https://.../internal/hook", "credits_per_token": 1,
           "model_map": {"doubao-seedance-2-0-260128": "seedance20"}}

上游契约见 `docs/upstreams/aivideomaker-official-api.md`（本文件的唯一依据）。

=============================================================================
本脚本负责什么、不负责什么
=============================================================================

**负责**：把 Seedance 规范请求**降维投影**成 8 个上游模型各自的请求体，
并把上游任务记录**升维**回 Seedance 任务对象。

**不负责**（属引擎，见 `docs/03_引擎架构.md`）：

    callback_url            引擎留存并推送（**绝不透传给上游的 webhookUrl**）
    execution_expires_after 引擎看门狗（上游没有 expired 态）
    dry_run                 引擎按 X-Dry-Run 返回将要发出的 payload
    并发闸门 / 幂等 / 重试  引擎
    素材转存                引擎

⇒ 上面这些字段**不进 `unsupported[]`** —— 它们被兑现了，只是不由上游兑现。
把它们报成"上游不支持"会让调用方以为能力缺失。

=============================================================================
四个决定，都是"会真花钱"或"会静默出错"的那一类
=============================================================================

1. 模型名**透传**：本层不改值，只在渠道显式配了映射表时才映射
   调用方写什么名字，就发到 `POST /api/v1/generate/{那个名字}`。合法值就是上游那 8 个槽位
   （`OFFICIAL_MODELS`）；**其余一律 400**，并列出合法值与渠道已配的映射键 ——
   绝不落默认值（旧实现的兜底是 `return "seedance20"`：写错一个字母就落到一个
   **提交即计费**的槽位）。需要"调用方写原生 ID、上游认槽位名"时由渠道给映射表，见下节。
   ⇒ 名字转换**只有** `model_map` 一处配置：渠道级的"钉住槽位"（`X-Channel-Options.model`）
   2026-09-16 已撤除，原因见下节。

2. 参考类素材**不能静默丢**
   判定准则：丢掉它会不会改变"用户想要什么"？
   `seedance20` 只吃**单个** image / video / audio，而 Seedance 规范允许 9 张图 / 3 段视频 /
   3 个音频。传 3 张参考图 → 上游只认 1 张 → **400**，不静默取第一张。
   唯一可降的是**尾帧**：`seedance20` / `wan27` / `happyhorse` 没有尾帧字段，
   有首帧时取首帧 + warning；**只给尾帧**（无首帧）仍 400 —— 那是另一个请求。

3. 支出上限是**提交前置条件**，且本地先算一遍
   官方文档只列了 `key` / `Content-Type` / `webhookUrl` 三个请求头，**没有计费前闸门**
   （旧实现把 `X-Max-Credits` 当上游能力，见上游文档 §7 差异 3）。⇒ 上限只能由本层强制：

     · 拿不到 `max_credits`（`X-Channel-Options` 或 `extra_body.aivideomaker_max_credits`）→ **400**；
     · 7 个模型有官方计费公式，**估算值 > 上限 → 400**（在任何请求发出之前）；
     · `seedance20` 服务端动态计价、公式不公开 ⇒ 无法预估，**如实告警**而不是假装守住。

4. 调用方的错与运维的错分开报
   调用方传错 `model` / `ratio` → 400 `InvalidParameter`；
   渠道头配错（`model_map` 值非法 / 重复键、遗留已撤除的 `X-Channel-Options.model`、
   缺 `max_credits`、缺 `X-Upstream-Url`）→ `channel_config_error`。
   报错层次错了，排查就会去错的地方。

=============================================================================
模型映射：默认透传，需要时由渠道配置
=============================================================================

本层**不改模型值**（判据原话："代码里不改模型值"）。所以能用的名字就是上游 8 个槽位名。
调用方（或它的控制面）写火山原生 ID 时，由**渠道**给映射表：

    X-Channel-Options: {"provider": "aivideomaker",
                        "model_map": {"doubao-seedance-2-0-260128": "seedance20",
                                      "doubao-seedance-1-0-pro-250528": "t2v"}}

五条约束，每条都对应一次实测：

1. **精确优先，通配只有一个元字符。** 键里含 `*` 即为通配模式（`doubao-seedance-*`、`*`），
   `*` 匹配任意字符序列（含空），可出现在名字任意位置；**匹配大小写敏感**，
   `?` / `[` / `]` 是**字面量**，不是元字符。精确命中永远压过通配。
   ⚠️ **绝不做顺序敏感的多规则匹配**：两条通配同时命中 ⇒ `channel_config_error`，
   不排序、不取最长 —— 旧实现的正则表就是这样把 5 个代次压进同一槽位、账单差 7.3 倍的。
2. **重复键显式拒绝。** JSON 里同名键会静默覆盖，而"哪一条生效"决定了账单，
   所以重复键（大小写不同也算）一律 `channel_config_error`，不取后者。
3. **不猜缺省。** 没配表、名字又不认识 ⇒ 400，并同时列出合法槽位与渠道已配的映射键。
   "兜底到某个槽位"是最坏的选项：错名字应当**响亮**，不该变成一张账单。
4. **命中必须可查。** 命中写进 `effective.model_map_applied`（引擎再上报 logfire）。
   响应体里已没有 `model` 字段 ⇒ "我请求的 vs 实际跑的"只能靠这里与 dry-run 核。

5. **通配可以强转，所以命中必须指名。** 通配（含 `*` 单键）**优先于**
   "名字本身就是槽位名"：`{"*": "t2v"}` 就是渠道级的"本渠道只跑 t2v" ——
   任何名字（含合法槽位名 `wan27`）都会被改写成 `t2v`。它是**渠道声明**、不是本层兜底，
   代价是"我以为请求的是 A、实际跑的是 B"，所以命中的**模式**必须可查：
   写进 `effective.model_map_pattern`（dry-run 与 logfire 都能回答"为什么被改了"）。

⚠️ 映射表**只在渠道声明时生效**，服务端不持任何模型/渠道知识（架构 D1）。

**唯一入口**：`model_map` 之外没有第二个名字转换点。渠道级的"钉住槽位"（`X-Channel-Options.model`）
2026-09-16 **撤除** —— 它只做一致性断言、不承担翻译，与"一个渠道一套配置"的其余部分重复；
它换来的"这个渠道只服务某个槽位"这条本地前置拦截，**改由 `model_map` 的通配承接**
（约束 5 的 `{"*": "t2v"}`）。⚠️ 语义并不等价：通配是**改写模型值**（请求照样放行，
命中的模式记在 `effective.model_map_pattern`），钉住是**拦下请求**。
⚠️ 遗留该键 ⇒ `channel_config_error`（**不静默忽略**）：以为它还钉着的运维会以为这个渠道只跑某个槽位，
而实际上任何合法槽位名都会被逐字发到那个上游端点。

旧实现用**一张全局档位表**套所有模型，并在注释里承认"官方 seedance20 的真实档位未验证"。
官方文档给了 8 个模型各自的档位，其中三个是**连续区间**而非离散档位：

    model          duration 类型   合法时长              resolution        比例字段     比例必填
    t2v            string         "5" "8"               —                 aspectRatio  是
    i2v            string         "5" "8"               —                 （无）       —
    t2v_v3         string         "5" "10" "15" "20"    —                 aspectRatio  是
    i2v_v3         string         "5" "10" "15" "20"    —                 （无）       —
    minimax        number         5–20                  "720p"/"1080p"    aspectRatio  否
    seedance20     number         4–15                  480/720 (number)  ratio        是
    wan27          string         "5" "10" "15"         "720P"/"1080P"    ratio        是
    happyhorse     number         3–15                  "720P"/"1080P"    ratio        否（i2v 下不用）

**区间类模型直接钳制到区间，不做"就近吸附"** —— 吸附是离散档位才需要的动作，
而它恰恰是跨进更贵档的入口（旧实现里 `duration=8` 对 480p 会吸到 10s）。

⚠️ `i2v` / `i2v_v3` **没有比例字段**（官方参数表未列），输出比例由首帧图决定。
调用方若传了 `ratio`，这里**不发**并告警 —— 发未文档化的字段是 `INVALID_PAYLOAD` 的常见来源。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Mapping

#: 本脚本实现的相位（03_引擎架构.md §5.1）。
PHASES = (
    "create_request",
    "create_response",
    "query_request",
    "query_response",
    "cancel_request",
    "cancel_response",
)

#: 上游 8 个模型（`POST /api/v1/generate/{model}` 的路径参数）。
OFFICIAL_MODELS: tuple[str, ...] = (
    "t2v", "i2v", "t2v_v3", "i2v_v3", "minimax", "seedance20", "wan27", "happyhorse",
)

#: Seedance 规范的 ratio / resolution 全集。
ARK_RATIOS = frozenset({"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"})
ARK_RESOLUTIONS = frozenset({"480p", "720p", "1080p"})

#: 引擎兑现的字段 —— **不报 unsupported**（见模块 docstring）。
ENGINE_LEVEL_FIELDS = ("callback_url", "execution_expires_after")

#: 上游确实没有对应能力的字段。
UNSUPPORTED_FIELDS = (
    "watermark",            # 8 个模型都没有水印开关
    "generate_audio",       # 无一支持"生成有声视频"（seedance20 的 audio 是**输入**）
    "seed",                 # 无随机种子
    "camera_fixed",         # 无固定镜头
    "return_last_frame",    # 无尾帧**产出**（有尾帧**输入**，是两件事）
    "draft",                # 无样片模式
    "service_tier",         # 无离线推理
    "priority",
    "safety_identifier",
    "tools",
    "omni_reference_task_type",
    "output_format",
)

#: 上游 TaskStatus → Seedance 六态。
STATUS_TO_ARK = {
    "submitted": "queued",
    "progress": "running",
    "completed": "succeeded",
    "failed": "failed",
    "cancel": "cancelled",
    "cancelled": "cancelled",
}

#: 各模型的比例允许集。
_RATIOS_169 = ("16:9", "9:16", "1:1")
_RATIOS_WAN = ("16:9", "9:16", "1:1", "4:3", "3:4")
_RATIOS_HH = ("16:9", "9:16", "3:4", "4:3", "1:1")
_RATIOS_MM = ("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")

#: 各模型规格。
#:   duration      : `allowed`（离散档位）或 `min`/`max`（连续区间）；`type` 是上游要的类型
#:   resolution    : None = 该模型无此字段；否则给出 type/allowed/required/default
#:   ratio_field   : None = 该模型**没有**比例字段（发了会 INVALID_PAYLOAD）
#:   ratio_required: 调用方不给就 400
#:   content       : 该模型能承接哪些内容（build_upstream_body 按此分派）
MODEL_SPECS: dict[str, dict[str, Any]] = {
    "t2v": {
        "ratio_field": "aspectRatio", "ratios": _RATIOS_169, "ratio_required": True,
        "duration": {"type": str, "allowed": (5, 8)},
        "resolution": None,
        "content": "text_only",
        "credits_per_second": 3,
    },
    "i2v": {
        "ratio_field": None, "ratios": _RATIOS_169, "ratio_required": False,
        "duration": {"type": str, "allowed": (5, 8)},
        "resolution": None,
        "content": "first_frame_required",
        "credits_per_second": 3,
    },
    "t2v_v3": {
        "ratio_field": "aspectRatio", "ratios": _RATIOS_169, "ratio_required": True,
        "duration": {"type": str, "allowed": (5, 10, 15, 20)},
        "resolution": None,
        "content": "text_only",
        "credits_per_second": 4,
    },
    "i2v_v3": {
        "ratio_field": None, "ratios": _RATIOS_169, "ratio_required": False,
        "duration": {"type": str, "allowed": (5, 10, 15, 20)},
        "resolution": None,
        "content": "first_frame_required",
        "credits_per_second": 4,
    },
    "minimax": {
        "ratio_field": "aspectRatio", "ratios": _RATIOS_MM, "ratio_required": False,
        "duration": {"type": int, "min": 5, "max": 20},
        "resolution": {"type": str, "allowed": ("720p", "1080p"), "default": "720p"},
        "content": "full",
        "tier": True,
    },
    "seedance20": {
        "ratio_field": "ratio", "ratios": _RATIOS_169, "ratio_required": True,
        "duration": {"type": int, "min": 4, "max": 15},
        "resolution": {"type": int, "allowed": (480, 720), "required": True},
        "content": "single_ref",
    },
    "wan27": {
        "ratio_field": "ratio", "ratios": _RATIOS_WAN, "ratio_required": True,
        "duration": {"type": str, "allowed": (5, 10, 15)},
        "resolution": {"type": str, "allowed": ("720P", "1080P"), "required": True},
        "content": "first_frame_optional",
        "prompt_extend": True,
    },
    "happyhorse": {
        "ratio_field": "ratio", "ratios": _RATIOS_HH, "ratio_required": False,
        "duration": {"type": int, "min": 3, "max": 15},
        "resolution": {"type": str, "allowed": ("720P", "1080P"), "required": True},
        "content": "first_or_multi_ref",
    },
}

#: ⚠️ 这里**曾经**有一张 `_MODEL_PATTERNS` 正则表（`(r"seedance", "seedance20")` 打头）。
#: 2026-09-16 删除，原因是它把 5 个不同代次的原生 ID 静默压进同一个槽位、且不回告警，
#: 实测账单差 7.3 倍。**不要再加回来** —— 映射知识属于控制面（`X-Channel-Options.model_map`）。


# =============================================================================
# 纯函数核心（零 IO）—— 测试主要压这一段
# =============================================================================

def _fail_config(ctx, message: str):
    """渠道配置错误：运维的问题。`channel_config_error` 指向渠道头，指向对的层。"""
    ctx.fail(message, code="channel_config_error")
    raise AssertionError("ctx.fail returned without raising")


def _reject(ctx, message: str, param: str):
    """调用方请求无法在上游表达：400，并指名是哪个字段/哪一项。"""
    ctx.fail(message, code="InvalidParameter", param=param, status=400)
    raise AssertionError("ctx.fail returned without raising")


def bare_model_name(model: Any) -> str:
    """剥掉 `provider/` 段，留下调用方写的**裸模型名**。

    引擎（`resolve_provider`）已经剥过一次；这里再剥是防御性的 —— 脚本不假设调用方
    一定是从那条路径进来的（内联脚本、`/docs` 试调、将来的别的调用点都会走这里）。
    """
    name = str(model or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[1].strip()
    return name


def resolve_model_map(options: Mapping[str, Any] | None, ctx) -> dict[str, str]:
    """渠道配置的模型映射表，形如 `{"doubao-seedance-2-0-260128": "seedance20"}`。

    键名取 `model_map`。⚠️ **`upstream_model_map` 作为别名同样接受**：
    `docs/03_引擎架构.md` §4.2 早先登记过这个名字（当时并未实现），运维可能照那份文档配 ——
    两个都写且内容不同 ⇒ `channel_config_error`（自相矛盾的配置不该由我们挑一个）。

    **配置非法是运维的错** ⇒ `channel_config_error`（与"调用方传错模型"分开报，
    见模块 docstring 决定 4）。值必须是上游槽位之一：给一个上游不认识的值，
    等于把 400 推到上游 —— 而那时请求已经带着凭证发出去了。

    键里含 `*` 即为**通配模式**：`*` 是唯一元字符（可出现在任意位置），大小写敏感，
    `?` / `[` / `]` 按字面量处理。通配与精确键共用同一套校验（值域、重复键、非空）。
    """
    raw = (options or {}).get("model_map")
    alias = (options or {}).get("upstream_model_map")
    if raw is not None and alias is not None and raw != alias:
        _fail_config(
            ctx,
            "X-Channel-Options.model_map and upstream_model_map are both set and differ; keep "
            "exactly one (they are two names for the same table)",
        )
    raw = raw if raw is not None else alias
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _fail_config(
            ctx,
            "X-Channel-Options.model_map must be a JSON object of "
            '{"<name the caller sends>": "<upstream slot>"}',
        )
    out: dict[str, str] = {}
    lowered: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        slot = str(value or "").strip()
        if not name or not slot:
            _fail_config(
                ctx, f"model_map entries must be non-empty strings (got {key!r}: {value!r})"
            )
        if slot not in OFFICIAL_MODELS:
            _fail_config(
                ctx,
                f"model_map[{name!r}]={slot!r} is not an upstream model "
                f"(expected one of: {', '.join(OFFICIAL_MODELS)})",
            )
        # JSON 同名键会**静默覆盖**，而"哪一条生效"直接决定账单 ⇒ 显式拒绝。
        # 大小写不同也拒绝：`Wan27` 与 `wan27` 是同一个意图，取其一就是猜。
        fold = name.lower()
        if fold in lowered:
            _fail_config(
                ctx,
                f"model_map has duplicate entries for {name!r} and {lowered[fold]!r} "
                "(JSON would silently keep only one of them)",
            )
        lowered[fold] = name
        out[name] = slot
    return out


#: 2026-09-16 撤除的渠道键。它当时的名字是"钉住槽位"（`X-Channel-Options.model`）：
#: 只做一致性断言（与本次解析结果不一致 ⇒ `channel_config_error`），既不承担任何名字转换，
#: 也没表达出"这个渠道只服务某个槽位"以外的语义 —— 与 `model_map` 职责重复。
#: ⚠️ **遗留该键必须响亮失败，不能静默忽略**：以为它还生效的运维会以为这个渠道只跑
#: 某个槽位，而实际上任何合法槽位名都会被逐字发往那个上游端点。
_REMOVED_OPTION_KEYS = ("model",)


def reject_removed_option_keys(options: Mapping[str, Any] | None, ctx) -> None:
    """撤除过的渠道键一旦出现 ⇒ `channel_config_error`（配置问题，修复人在运维）。

    静默忽略是更坏的选择：这个键的语义是"本渠道只服务某个槽位"，
    运维据此认为"调用方传错名字会被拦住"；一旦它不再生效却仍被接受，
    那层保护就无声消失了。
    """
    for key in _REMOVED_OPTION_KEYS:
        if key in (options or {}):
            _fail_config(
                ctx,
                f"X-Channel-Options.{key} was removed on 2026-09-16: the channel-level "
                "pinned slot is gone. Use model_map (exact match: the name the caller "
                "sends -> the upstream slot name), or have callers send the slot name "
                "verbatim. "
                f'Remove the "{key}" key from the channel configuration.',
            )
def _glob_matches(pattern: str, name: str) -> bool:
    """`*` 是唯一元字符（匹配任意字符序列，含空）；其余字符按字面量，大小写敏感。

    ⚠️ 不用 `fnmatch`：① 它不在沙箱的 `ALLOWED_IMPORTS` 里（`import fnmatch` 会被
    `ScriptSecurityError` 拒掉）；② `fnmatch.fnmatch` 会经 `normcase` **折叠大小写**
    （那是文件系统语义），而模型名匹配必须大小写敏感。
    """
    pieces = []
    for ch in pattern:
        pieces.append(".*" if ch == "*" else re.escape(ch))
    return re.fullmatch("".join(pieces), name) is not None


def resolve_upstream_model(
    model: Any, options: Mapping[str, Any] | None, ctx
) -> tuple[str, bool, str | None]:
    """裸模型名 → `(上游槽位名, 是否走了渠道映射表, 命中的通配模式或 None)`。

    **本函数不猜、不兜底**（通配不是兜底：它是渠道显式声明的策略，见五条约束）。

    判定顺序：① 精确命中映射表；② **唯一**的通配命中（含 `*` 单键）；
    ③ 名字本身就是上游槽位名 ⇒ 逐字透传；④ 其余 ⇒ 400。
    ⚠️ ② 在 ③ **之前** ⇒ `{"*": "t2v"}` 会把合法槽位名也改成 `t2v`（本决定要的语义）。

    ⚠️ 2026-09-16 起**没有**渠道级"钉住槽位"这一步：`X-Channel-Options.model`
    已撤除，遗留该键由 `reject_removed_option_keys` 拦下（响亮失败，不静默忽略）。
    """
    options = options or {}
    reject_removed_option_keys(options, ctx)
    name = bare_model_name(model)
    if not name:
        _reject(ctx, "model is required", "model")

    mapping = resolve_model_map(options, ctx)
    pattern: str | None = None
    if name in mapping:
        upstream, mapped = mapping[name], True
    else:
        hits = [key for key in mapping if "*" in key and _glob_matches(key, name)]
        if len(hits) > 1:
            _fail_config(
                ctx,
                f"model {name!r} matches {len(hits)} wildcard entries in "
                f"X-Channel-Options.model_map ({', '.join(sorted(hits))}); refusing to pick "
                "one, because order-sensitive matching is exactly what this table replaced. "
                "Keep exactly one pattern that matches.",
            )
        if hits:
            pattern = hits[0]
            upstream, mapped = mapping[pattern], True
        elif name in OFFICIAL_MODELS:
            upstream, mapped = name, False
        else:
            hint = (
                f" The channel's model_map knows: {', '.join(sorted(mapping))}."
                if mapping
                else " No model_map is configured on this channel."
            )
            _reject(
                ctx,
                f'unknown model "{name}". This layer forwards the model name verbatim, so it '
                f"must be one of the upstream models: {', '.join(OFFICIAL_MODELS)}.{hint}",
                "model",
            )

    return upstream, mapped, pattern


def _aspect_value(ratio: Any) -> float:
    m = re.match(r"^(\d+)\s*:\s*(\d+)$", str(ratio).strip())
    if not m:
        return 1.0  # auto / adaptive 之类，取中性值
    w, h = int(m.group(1)), int(m.group(2))
    return (w / h) if h else 1.0


def resolve_ratio(ratio: Any, model: str, warnings: list[str]) -> str | None:
    """Seedance ratio → 该模型的比例允许集。

    - 模型**没有**比例字段（`i2v` 系）⇒ 返回 None 并告警被忽略（发未文档化字段会 INVALID_PAYLOAD）。
    - `adaptive`：`minimax` 显式支持 `auto`，原样映射；其余模型无此语义 ⇒ 告警后落 16:9。
    - 允许集外的具体比例（如 `21:9` 对 `wan27`）：按**最接近的宽高比**吸附 + warning。
    """
    spec = MODEL_SPECS[model]
    value = str(ratio or "").strip()
    if spec["ratio_field"] is None:
        if value:
            warnings.append(
                f"{model} has no ratio field; {value!r} was dropped — the output ratio comes "
                "from the first frame image"
            )
        return None
    if not value:
        return None
    allowed = spec["ratios"]
    if value == "adaptive":
        if "auto" in allowed:
            return "auto"
        warnings.append(
            f'{model} has no "adaptive" ratio; fell back to 16:9. adaptive needs the first-frame '
            "image's own aspect ratio, which this layer cannot read."
        )
        return "16:9"
    if value in allowed:
        return value
    target = _aspect_value(value)
    nearest = min(allowed, key=lambda a: (abs(_aspect_value(a) - target), a))
    warnings.append(f'ratio "{value}" is not supported by {model}; snapped to "{nearest}"')
    return nearest


def _numeric_resolution(value: Any) -> int | None:
    m = re.search(r"(\d{3,4})", str(value or ""))
    return int(m.group(1)) if m else None


def resolve_resolution(resolution: Any, model: str, warnings: list[str]) -> Any:
    """Seedance resolution → 该模型要求的**类型与字面量**。

    ⚠️ 大小写是上游契约的一部分（`minimax` 要 `720p`，`wan27`/`happyhorse` 要 `720P`），
    所以返回值直接取自 `allowed` 里的字面量，不做大小写转换。
    """
    spec = MODEL_SPECS[model].get("resolution")
    value = str(resolution or "").strip()
    if spec is None:
        if value:
            warnings.append(f"{model} has no resolution parameter; {value!r} was dropped")
        return None

    num = _numeric_resolution(value) if value else None
    if num is None:
        return spec.get("default")   # 缺必填项由 plan_create 统一报 400

    allowed = spec["allowed"]
    if spec["type"] is int:
        if num in allowed:
            return int(num)
        picked = 720 if num > max(allowed) else min(allowed)
        warnings.append(f"{model} accepts resolution {list(allowed)} only; {num} -> {picked}")
        return int(picked)

    for literal in allowed:                     # 按数值匹配，返回上游要求的大小写
        if _numeric_resolution(literal) == num:
            return literal
    picked = allowed[-1] if num > max(_numeric_resolution(a) for a in allowed) else allowed[0]
    warnings.append(f"{model} accepts resolution {list(allowed)} only; {num} -> {picked}")
    return picked


def resolve_duration(duration: Any, model: str, warnings: list[str]) -> Any:
    """Seedance duration → 该模型要求的类型与档位。

    区间类模型**直接钳制**（不做就近吸附）；离散档位才吸附，并点明是否跨档。
    """
    spec = MODEL_SPECS[model]["duration"]
    try:
        value = int(float(duration))
    except (TypeError, ValueError):
        value = 5

    if "allowed" in spec:
        allowed = spec["allowed"]
        if value in allowed:
            picked = value
        else:
            picked = min(allowed, key=lambda a: (abs(a - value), a))
            note = f"duration {value}s is not accepted by {model}; snapped to {picked}s"
            if picked > value:
                note += " (snapped UP — the longer clip costs more)"
            warnings.append(note)
    else:
        lo, hi = int(spec["min"]), int(spec["max"])
        picked = min(max(value, lo), hi)
        if picked != value:
            warnings.append(
                f"duration {value}s is outside {model} range [{lo},{hi}]; clamped to {picked}s"
            )

    return str(picked) if spec["type"] is str else int(picked)


def _split_content(items: list) -> dict:
    """拆 `content[]` → 各类输入 + 不认识的 type。

    `role` 按官方放在 content 项**顶层**；部分第三方兼容网关把它写进 `image_url`
    对象内部，两种都收（出口只按官方形态产出）。
    """
    out: dict[str, list] = {
        "texts": [], "first": [], "last": [], "ref_img": [],
        "ref_vid": [], "ref_aud": [], "draft": [], "unknown": [],
    }
    for item in items:
        if not isinstance(item, Mapping):
            out["unknown"].append("content[] (non-object)")
            continue
        kind = str(item.get("type") or "")
        if kind == "text":
            if item.get("text"):
                out["texts"].append(str(item["text"]))
            continue
        if kind == "draft_task":
            holder = item.get("draft_task")
            if isinstance(holder, Mapping) and holder.get("id"):
                out["draft"].append(holder["id"])
            else:
                out["unknown"].append("draft_task without an id")
            continue

        role = str(item.get("role") or "").strip()
        holder: Mapping | None = None
        url = None
        for key in ("image_url", "video_url", "audio_url"):
            candidate = item.get(key)
            if isinstance(candidate, Mapping):
                holder = candidate
                if candidate.get("url"):
                    url = candidate["url"]
                    break
        if url is None:
            out["unknown"].append(f"{kind or 'content item'} without a url")
            continue
        if not role and holder is not None:
            role = str(holder.get("role") or "").strip()

        if kind == "image_url":
            bucket = "first" if role == "first_frame" else "last" if role == "last_frame" else "ref_img"
            out[bucket].append(url)
        elif kind == "video_url":
            out["ref_vid"].append(url)
        elif kind == "audio_url":
            out["ref_aud"].append(url)
        else:
            out["unknown"].append(f'content[].type="{kind}"')
    return out


def build_upstream_body(
    model: str, parts: Mapping[str, Any], plan: Mapping[str, Any], warnings: list[str], ctx
) -> dict:
    """投影成上游请求体。8 个模型 4 种字段写法（上游文档 §5）。"""
    spec = MODEL_SPECS[model]
    shape = spec["content"]
    ratio_field = spec["ratio_field"]
    prompt = "\n".join(parts["texts"]).strip()
    first, last = parts["first"], parts["last"]
    ref_img, ref_vid, ref_aud = parts["ref_img"], parts["ref_vid"], parts["ref_aud"]

    if parts["draft"]:
        _reject(ctx, "draft_task references are not supported by this upstream", "content")

    # ---- 纯文生：任何图像/视频/音频都不可表达 -------------------------------
    if shape == "text_only":
        if first or last or ref_img or ref_vid or ref_aud:
            _reject(
                ctx,
                f"{model} is text-to-video only; the supplied image/video/audio cannot be "
                "expressed. Pick an image-capable model instead of silently degrading to text.",
                "content",
            )
        if not prompt:
            _reject(ctx, f"{model} requires a text prompt", "content")
        return {"prompt": prompt, "duration": plan["duration"], ratio_field: plan["ratio"]}

    # ---- i2v / i2v_v3：必须有首帧；尾帧可降；参考素材不可表达 ---------------
    if shape == "first_frame_required":
        if not first:
            if last:
                _reject(
                    ctx,
                    f"{model} has no last-frame input; a last-frame-only request cannot be expressed",
                    "content",
                )
            _reject(ctx, f'{model} requires a first frame (content[] with role="first_frame")', "content")
        if ref_img or ref_vid or ref_aud:
            _reject(ctx, f"{model} accepts a first frame only, no reference assets", "content")
        if last:
            warnings.append(
                f"{model} has no last-frame input; the last frame was dropped (first frame kept)"
            )
        body = {"image": first[0], "duration": plan["duration"]}
        if prompt:
            body["prompt"] = prompt
        if plan["ratio"]:
            body[ratio_field] = plan["ratio"]
        return body

    # ---- wan27：首帧可选（传了即 i2v），无尾帧、无参考素材 -------------------
    if shape == "first_frame_optional":
        if last:
            _reject(ctx, f"{model} has no last-frame input", "content")
        if ref_img or ref_vid or ref_aud:
            _reject(ctx, f"{model} does not accept reference assets", "content")
        if not prompt:
            _reject(ctx, f"{model} requires a text prompt", "content")
        body = {
            "prompt": prompt,
            "duration": plan["duration"],
            "resolution": plan["resolution"],
            ratio_field: plan["ratio"],
        }
        if first:
            body["image"] = first[0]
        if plan.get("prompt_extend") is not None:
            body["promptExtend"] = bool(plan["prompt_extend"])
        return body

    # ---- seedance20：image/video/audio 各**单个**，可组合；无尾帧 -----------
    if shape == "single_ref":
        if last:
            if not first:
                _reject(
                    ctx,
                    "seedance20 has no last-frame input; a last-frame-only request cannot be expressed",
                    "content",
                )
            warnings.append(
                "seedance20 has no last-frame input; the last frame was dropped (first frame kept)"
            )
        if len(ref_img) > 1:
            _reject(
                ctx,
                f"seedance20 accepts exactly one reference image, got {len(ref_img)}. "
                "Dropping the extras would change what was asked for.",
                "content",
            )
        if len(ref_vid) > 1:
            _reject(ctx, f"seedance20 accepts exactly one reference video, got {len(ref_vid)}", "content")
        if len(ref_aud) > 1:
            _reject(ctx, f"seedance20 accepts exactly one reference audio, got {len(ref_aud)}", "content")

        image = first[0] if first else (ref_img[0] if ref_img else None)
        video = ref_vid[0] if ref_vid else None
        audio = ref_aud[0] if ref_aud else None
        if not (prompt or image or video):
            _reject(ctx, "seedance20 requires at least one of prompt / image / video", "content")
        if audio and not (prompt or image or video):
            _reject(ctx, "seedance20 audio cannot be used on its own", "content")

        body: dict[str, Any] = {
            "duration": plan["duration"],
            "resolution": plan["resolution"],
            ratio_field: plan["ratio"],
        }
        if prompt:
            body["prompt"] = prompt
        if image:
            body["image"] = image
        if video:
            body["video"] = video
        if audio:
            body["audio"] = audio
        return body

    # ---- happyhorse：单图 = i2v，多图 = r2v；无尾帧、无视频/音频参考 --------
    if shape == "first_or_multi_ref":
        if last:
            _reject(ctx, f"{model} has no last-frame input", "content")
        if ref_vid or ref_aud:
            _reject(ctx, f"{model} does not accept video/audio references", "content")
        if not prompt:
            _reject(ctx, f"{model} requires a text prompt", "content")
        images = [url for url in (first + ref_img) if url]
        body = {"prompt": prompt, "duration": plan["duration"], "resolution": plan["resolution"]}
        if images:
            # 官方文档：ratio 在 i2v/r2v 模式下不使用
            body["image"] = images[0] if len(images) == 1 else images
            if plan["ratio"]:
                warnings.append(f"{model} ignores ratio in i2v/r2v mode; {plan['ratio']!r} was dropped")
            if len(images) > 1 and len(first) > 1:
                warnings.append(
                    "multiple first-frame images were sent as a multi-image reference (r2v); "
                    "the upstream has no first+last-frame mode"
                )
        elif plan["ratio"]:
            body[ratio_field] = plan["ratio"]
        return body

    # ---- minimax：唯一支持首尾帧 + 参考素材的模型，二者互斥 ----------------
    if shape == "full":
        has_frame = bool(first or last)
        has_ref = bool(ref_img or ref_vid or ref_aud)
        if has_frame and has_ref:
            _reject(
                ctx,
                "minimax cannot combine frame inputs (first/last frame) with reference assets; "
                "send one or the other",
                "content",
            )
        if ref_aud and not (ref_img or ref_vid):
            _reject(
                ctx,
                "minimax reference audio must be paired with a reference image or video; "
                "the upstream documents that audio cannot be used alone",
                "content",
            )
        if not prompt:
            _reject(ctx, "minimax requires a text prompt (upstream field name is `content`)", "content")
        if len(ref_img) > 4:
            _reject(ctx, f"minimax accepts at most 4 reference images, got {len(ref_img)}", "content")
        if len(ref_aud) > 2:
            _reject(ctx, f"minimax accepts at most 2 reference audio files, got {len(ref_aud)}", "content")
        if len(ref_vid) > 1:
            _reject(ctx, f"minimax accepts one reference video, got {len(ref_vid)}", "content")

        body = {
            "content": prompt,                 # ⚠️ minimax 的提示词字段叫 content
            "duration": plan["duration"],
            "resolution": plan["resolution"],
            "tier": plan["tier"],
        }
        if first:
            body["imageUrl"] = first[0]
        if last:
            body["lastFrameUrl"] = last[0]
        if ref_img:
            body["referenceImageUrls"] = list(ref_img)
        if ref_vid:
            body["referenceVideoUrl"] = ref_vid[0]
        if ref_aud:
            body["referenceAudioUrls"] = list(ref_aud)
        if plan["ratio"]:
            body[ratio_field] = plan["ratio"]
        return body

    raise AssertionError(f"unhandled content shape {shape!r} for model {model!r}")


def estimate_credits(
    model: str,
    duration: Any,
    resolution: Any,
    tier: str = "turbo",
    table: Mapping[str, Any] | None = None,
) -> int | None:
    """上游积分估算（官方公式，上游文档 §4）。返回 None = 无法预估。

    `seedance20` 由服务端价格计算器动态计价、公式不公开。**渠道可以用
    `X-Channel-Options.credit_table` 给出每秒费率**（计费知识本来就属控制面，
    这也符合 D1"服务端不持计费知识"）—— 给了就当可估；不给则 None，
    由调用方把"守不住上限"如实说出去，**而不是编一个数字**。
    """
    spec = MODEL_SPECS.get(model)
    if spec is None:
        return None
    try:
        d = int(float(duration))
    except (TypeError, ValueError):
        return None
    if spec.get("credits_per_second"):
        return d * int(spec["credits_per_second"])
    res = str(resolution or "").lower()
    if model == "minimax":
        total = d * (4 if res.startswith("1080") else 3)
        return total + d if tier == "base" else total
    if model == "wan27":
        return d * (15 if res.startswith("1080") else 10)
    if model == "happyhorse":
        return d * (50 if res.startswith("1080") else 25)
    if isinstance(table, Mapping):
        per_second = table.get(model)
        if per_second is not None:
            try:
                return int(round(d * float(per_second)))
            except (TypeError, ValueError):
                return None
    return None


def resolve_max_credits(body: Mapping[str, Any], options: Mapping[str, Any] | None, ctx):
    """支出上限（03_引擎架构.md §9.1）。请求级**只能收紧**渠道级。"""
    options = options or {}
    channel = options.get("max_credits")
    cap = None
    if channel is not None:
        if isinstance(channel, bool) or not isinstance(channel, int) or channel < 0:
            _fail_config(ctx, "X-Channel-Options.max_credits must be a non-negative integer")
        cap = channel

    extra = body.get("extra_body") if isinstance(body.get("extra_body"), Mapping) else {}
    raw = extra.get("aivideomaker_max_credits")
    if raw is None or raw == "":
        return cap
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        _reject(ctx, "extra_body.aivideomaker_max_credits must be a non-negative integer", "extra_body.aivideomaker_max_credits")
    return raw if cap is None else min(raw, cap)


def plan_create(body: Mapping[str, Any], options: Mapping[str, Any] | None, ctx) -> dict:
    """Seedance 创建请求 → 完整翻译计划（纯函数，不联网、不读环境变量）。"""
    options = options or {}
    if not isinstance(body, Mapping):
        _reject(ctx, "body must be a JSON object", "body")

    warnings: list[str] = []
    unsupported: list[str] = []

    upstream_model, model_map_applied, model_map_pattern = resolve_upstream_model(
        body.get("model"), options, ctx
    )
    spec = MODEL_SPECS[upstream_model]

    items = body.get("content")
    if not isinstance(items, list) or not items:
        _reject(ctx, "content is required", "content")
    parts = _split_content(items)
    unsupported.extend(parts["unknown"])

    extra = body.get("extra_body") if isinstance(body.get("extra_body"), Mapping) else {}
    for key in UNSUPPORTED_FIELDS:
        if body.get(key) is not None or extra.get(key) is not None:
            unsupported.append(key)

    ratio = str(body.get("ratio") or "").strip()
    if ratio and ratio not in ARK_RATIOS:
        _reject(ctx, f'ratio: invalid enum value "{ratio}"', "ratio")
    resolution = None
    if body.get("resolution") not in (None, ""):
        value = str(body["resolution"]).strip()
        # 前门只拒绝**对任何上游都畸形**的输入。大小写不承载语义（上游侧三种模型要三种写法，
        # 那由 resolve_resolution 按各自的字面量产出），所以这里统一小写后再校验，
        # 不因为调用方写了 `720P` 就 400。
        if value.lower() not in ARK_RESOLUTIONS:
            _reject(ctx, f'resolution: invalid enum value "{value}"', "resolution")
        resolution = value.lower()

    duration = None
    if body.get("duration") not in (None, ""):
        duration = _as_float(body["duration"])
        if duration == -1:
            duration = 5
            warnings.append("duration=-1 (model-chosen) is not supported upstream; mapped to 5s")
    elif body.get("frames") is not None:
        duration = max(1, round(_as_float(body["frames"]) / 24))
        warnings.append(f"frames={body['frames']} converted to ~{duration}s at 24fps")
    if duration is None:
        duration = spec["duration"].get("min", 5)
        warnings.append(f"duration was not given; defaulted to {duration}s")

    plan_ratio = resolve_ratio(ratio, upstream_model, warnings)
    if plan_ratio is None and spec["ratio_required"]:
        _reject(
            ctx,
            f"{upstream_model} requires {spec['ratio_field']} (one of {list(spec['ratios'])})",
            "ratio",
        )

    plan_resolution = resolve_resolution(resolution, upstream_model, warnings)
    if plan_resolution is None:
        res_spec = spec.get("resolution")
        if res_spec and res_spec.get("required"):
            _reject(
                ctx,
                f"{upstream_model} requires resolution (one of {list(res_spec['allowed'])})",
                "resolution",
            )

    tier = "turbo"
    override = extra.get("aivideomaker_tier")
    if override is not None:
        if override in ("turbo", "base") and spec.get("tier"):
            tier = str(override)
        else:
            warnings.append(
                f'extra_body.aivideomaker_tier={override!r} ignored '
                f"({'expected turbo|base' if spec.get('tier') else 'this model has no tier switch'})"
            )

    plan = {
        "duration": resolve_duration(duration, upstream_model, warnings),
        "resolution": plan_resolution,
        "ratio": plan_ratio,
        "tier": tier,
        "prompt_extend": extra.get("aivideomaker_prompt_extend"),
    }
    upstream_body = build_upstream_body(upstream_model, parts, plan, warnings, ctx)

    max_credits = resolve_max_credits(body, options, ctx)
    if max_credits is None:
        _reject(
            ctx,
            "official upstream is billed on submit and exposes no server-side spend cap. "
            "Provide one via `extra_body.aivideomaker_max_credits` or "
            "`X-Channel-Options.max_credits`; refusing to submit without it.",
            "extra_body.aivideomaker_max_credits",
        )

    estimated = estimate_credits(
        upstream_model,
        plan["duration"],
        plan["resolution"],
        plan["tier"],
        options.get("credit_table"),
    )
    if estimated is None:
        # 上游**没有计费前闸门**（已确认）⇒ 算不出成本时默认**拒绝提交**，
        # 只有调用方/渠道显式接受"不可验证成本"才放行。
        # 这是刻意的摩擦：算不出来不该由我们替用户拍板放行。
        advice = (
            f"{upstream_model} is priced dynamically by the upstream and the formula is not "
            "published, so the spend cap cannot be verified before submit. Give a per-second "
            "rate via `X-Channel-Options.credit_table`, or accept an unverifiable cost via "
            "`X-Channel-Options.allow_unpriced` / "
            "`extra_body.aivideomaker_allow_unpriced`."
        )
        allowed = bool(options.get("allow_unpriced")) or bool(
            extra.get("aivideomaker_allow_unpriced")
        )
        if not allowed:
            _reject(ctx, advice, "extra_body.aivideomaker_max_credits")
        warnings.append(
            f"cost is not verifiable before submit and the caller accepted that: {advice}"
        )
    elif estimated > max_credits:
        _reject(
            ctx,
            f"estimated cost {estimated} credits exceeds the spend cap {max_credits}; refusing to submit",
            "extra_body.aivideomaker_max_credits",
        )

    return {
        "requested": {
            "model": body.get("model"),
            "content": items,
            "ratio": body.get("ratio"),
            "resolution": body.get("resolution"),
            "duration": body.get("duration"),
            "frames": body.get("frames"),
            "callback_url": body.get("callback_url"),
        },
        "effective": {
            "upstream_model": upstream_model,
            # "我请求的 vs 实际跑的"这一对：响应体里已经没有 `model` 字段了，
            # 所以判据只剩这里（→ logfire 的 task.effective.*）与 dry-run。
            "model_requested": str(body.get("model") or ""),
            "model_map_applied": model_map_applied,
            # 命中的**模式**（含 "*"）。通配能强转模型值（② 优先于 ③）⇒
            # "我请求 A、实际跑 B"必须指名道姓，光一个布尔值回答不了"为什么"。
            "model_map_pattern": model_map_pattern,
            "ratio": plan["ratio"],
            "resolution": plan_resolution,
            "duration": plan["duration"],
            "tier": tier if spec.get("tier") else None,
            "billed": True,
            "billing_note": (
                "official upstream: every submit is billed, there is no free window and no "
                "server-side spend cap — the local credit estimate is the only guard"
            ),
            "estimated_credits": estimated,
        },
        "warnings": warnings,
        "unsupported": sorted(set(unsupported)),
        "upstream_model": upstream_model,
        "upstream_body": upstream_body,
        "estimated_credits": estimated,
        "max_credits": max_credits,
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int_or_none(value: Any) -> int | None:
    """数字字段的容错转换。**上游把 `duration` 当字符串回**（`"5"`），
    而 Seedance 契约里它是 integer 秒 —— 真实冒烟才发现：不转就会被调用方
    按字符串处理，做算术或比较时出错。"""
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _epoch(value: Any) -> int | None:
    """ISO8601 → epoch 秒（Seedance 用 epoch 秒，上游给 ISO）。"""
    if not value:
        return None
    try:
        return int(_dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def map_status(raw_status: Any) -> str:
    """上游 TaskStatus → Seedance 六态。未知值按 `running`（不猜终态）。"""
    return STATUS_TO_ARK.get(str(raw_status or "SUBMITTED").strip().lower(), "running")


def normalize_task(raw: Mapping[str, Any] | None, *, credits_per_token: int = 1) -> dict:
    """上游任务记录 → Seedance 查询响应片段（`03_引擎架构.md` §5.1 的形状）。

    ⚠️ `usage` 口径：上游按**积分**计费、Seedance 按 **token**。
    按 §8.2 的默认把净积分 1:1 折算成 token 当量（倍率由渠道选项 `credits_per_token` 覆盖），
    并**同时**保留原始积分字段，让控制面能按自己的口径重算 —— 不伪造、也不丢原始值。
    """
    raw = raw if isinstance(raw, Mapping) else {}
    status = map_status(raw.get("status"))
    charged = int(raw.get("creditsCharged") or 0)
    refunded = int(raw.get("creditsRefunded") or 0)
    net = charged - refunded
    source = raw.get("input") if isinstance(raw.get("input"), Mapping) else {}
    output = raw.get("output") if isinstance(raw.get("output"), Mapping) else {}
    num_res = _numeric_resolution(source.get("resolution"))

    return {
        "status": status,
        "video_url": output.get("url") if status == "succeeded" else None,
        "last_frame_url": None,          # 上游不产出尾帧（只有尾帧输入）
        "file_url": None,
        "duration": _as_int_or_none(source.get("duration")),
        "frames": None,
        "framespersecond": 24,
        "ratio": source.get("ratio") or source.get("aspectRatio"),
        "resolution": f"{num_res}p" if num_res else None,
        "seed": -1,
        "usage": {
            "completion_tokens": net * credits_per_token,
            "total_tokens": net * credits_per_token,
            "credits": net,
            "credits_charged": charged,
            "credits_refunded": refunded,
        },
        "error": (
            {
                "code": "GenerationFailed",
                "message": output.get("error") or raw.get("message") or "video generation failed",
            }
            if status == "failed"
            else None
        ),
        "warnings": [],
        "created_at": _epoch(raw.get("createdAt")),
        "updated_at": _epoch(raw.get("completedAt") or raw.get("createdAt")),
        "upstream": dict(raw),
    }


# =============================================================================
# 相位包装（薄）—— 逻辑都在上面
# =============================================================================

_PATH_TEMPLATE = "/api/v1/generate/{model}"


def _upstream_url(ctx, upstream_model: str) -> str:
    """拼创建任务地址。

    容两种渠道配法：base（推荐）或整条 endpoint。带 `{model}` 的模板由这里展开 ——
    路径参数**每请求**都在变，不能靠渠道配置写死。
    """
    base = str(ctx.upstream_url or "").rstrip("/")
    if not base:
        _fail_config(ctx, "X-Upstream-Url is required")
    if "{model}" in base:
        return base.replace("{model}", upstream_model)
    if re.search(r"/api/v1/generate/[^/]*$", base):
        return re.sub(r"/api/v1/generate/[^/]*$", f"/api/v1/generate/{upstream_model}", base)
    return f"{base}{_PATH_TEMPLATE.format(model=upstream_model)}"


def _task_url(ctx, suffix: str = "") -> str:
    base = str(ctx.upstream_url or "").rstrip("/")
    base = re.sub(r"/api/v1/generate(/[^/]*)?$", "", base) or base
    task_id = str(ctx.task.upstream_task_id if ctx.task else "")
    if not task_id:
        _fail_config(ctx, "task has no upstream_task_id")
    return f"{base}/api/v1/tasks/{task_id}{suffix}"


async def create_request(ctx, payload):
    """规范体 → 上游创建请求计划。"""
    plan = plan_create(payload, dict(ctx.options or {}), ctx)
    headers = {}
    webhook = (ctx.options or {}).get("webhook_url")
    if webhook:
        # 只认渠道配置的**内部**回调地址。调用方给的 callback_url 由引擎留存并推送，
        # 绝不透传 —— 否则我们的回调重试语义会被上游的 webhook 语义顶掉。
        headers["webhookUrl"] = str(webhook)
    ctx.plan = plan
    return {
        "method": "POST",
        "url": _upstream_url(ctx, plan["upstream_model"]),
        "body": plan["upstream_body"],
        "headers": headers,
    }


async def create_response(ctx, payload):
    """上游创建响应 → `{"task_id": ...}`。

    ⚠️ 上游文档**没有给出错误响应的 HTTP 状态码**，只有 `status`/`message` 信封 ⇒
    这里按信封判定，不依赖状态码（这也是为什么本相位必须存在）。
    """
    if not isinstance(payload, Mapping):
        _fail_config(ctx, "upstream create response is not a JSON object")
    status = str(payload.get("status") or "").strip().upper()
    task_id = str(payload.get("taskId") or "").strip()
    if status == "FAILED" or not task_id:
        message = str(payload.get("message") or "upstream rejected the task")
        if "credit" in message.lower():
            ctx.fail(message, code="QuotaExceeded", status=429)
        ctx.fail(message, code="InvalidParameter", status=400)
    return {"task_id": task_id}


async def query_request(ctx, payload):
    """`ctx.task` → 查询请求计划。

    用**详情**接口而不是 `/status`：`/status` 只回状态、没有产物地址，
    而查询相位的职责正是把产物地址带回去。
    """
    return {"method": "GET", "url": _task_url(ctx)}


async def query_response(ctx, payload):
    """上游任务记录 → 规范化片段。"""
    multiplier = 1
    raw_multiplier = (ctx.options or {}).get("credits_per_token")
    if isinstance(raw_multiplier, int) and not isinstance(raw_multiplier, bool) and raw_multiplier > 0:
        multiplier = raw_multiplier
    return normalize_task(payload, credits_per_token=multiplier)


async def cancel_request(ctx, payload):
    """`ctx.task` → 取消请求计划（**必须 PUT**；POST / DELETE 均 405）。"""
    status = str(ctx.task.status if ctx.task else "")
    if status and status != "queued":
        ctx.fail(
            f"only queued tasks can be cancelled; this one is {status}",
            code="InvalidParameter",
            param="id",
            status=400,
        )
    return {"method": "PUT", "url": _task_url(ctx, "/cancel")}


async def cancel_response(ctx, payload):
    """上游取消响应 → `{"status": "cancelled"}`。"""
    body = payload if isinstance(payload, Mapping) else {}
    if str(body.get("status") or "").strip().upper() == "FAILED":
        ctx.fail(
            str(body.get("message") or "upstream refused to cancel"),
            code="InvalidParameter",
            param="id",
            status=400,
        )
    return {"status": "cancelled"}
