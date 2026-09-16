"""senseaudio/video@v1: SenseAudio 视频开放接口 → 火山 Seedance 视频生成协议。

渠道配置（控制面）：

    X-Upstream-Url:    https://api.senseaudio.cn        # base 即可，脚本自己拼 /v1/video/*
                       （写成 .../v1/video/create 也认，见 _create_url）
    X-Script-Ref:      senseaudio/video@v1
    X-Channel-Options: {"provider": "senseaudio", "max_credits": 5000, "max_concurrency": 1}
    可选：{"credit_table": {"doubao-seedance-2-0-260128": 12},   # 上游不公布价格 ⇒ 可估
           "allow_unpriced": true,                              # 或显式接受不可验证成本
           "model_map": {"<调用方写的名字>": "doubao-seedance-2-0-260128"},
           "rehost": true,
           "status_binding": "credential",                      # 默认（见下）；另一形态 task_id
           "query_id_param": "id",                              # 只在 status_binding=task_id 时用
           "retry_after_seconds": 5}                            # 上游没说等多久时的退避值

🔴 **`generate_audio` 固定发嵌套形态**（`provider_specific.generate_audio`）：上游**只认**嵌套，
扁平只有火山（原生）吃 —— 见决定 1，**默认开声音**。

🔴 **`max_concurrency` 建议配 1**：上游的查询接口没有参数、**按 API key 回答**（一次只认得
一个任务），所以同一把钥匙上放多个在跑任务时，旧任务的状态就再也读不到了
（脚本会**验明记录归属**并响亮失败，见决定 9）。引擎的并发闸门键正是
`provider:凭证指纹`，配 1 后两者语义对齐。

**不需要 `X-Auth-Emit`**：上游是标准 `Authorization: Bearer <key>`，留空即原样转发
（`aivideomaker/video@v1` 需要 `header:key:`，因为那边是裸 key 头）。

上游契约见 `docs/upstreams/senseaudio-official-api.md`（本文件的唯一依据）。

=============================================================================
本脚本负责什么、不负责什么
=============================================================================

**负责**：把 Seedance 规范请求**降维投影**成上游的 `{model, content[], duration,
resolution, ratio, watermark, provider_specific, timeout}`，并把上游任务记录
**升维**回 Seedance 查询响应片段。

**不负责**（属引擎，见 `docs/03_引擎架构.md`）：

    callback_url            引擎留存并推送（上游没有 webhook）
    execution_expires_after 引擎看门狗（上游没有 expired 态）
    dry_run                 引擎按 X-Dry-Run 返回将要发出的 payload
    并发闸门 / 幂等 / 重试  引擎
    素材转存                引擎（逐渠道开关 rehost）
    列表                    引擎按本地任务表实现

⇒ 上面这些字段**不进 `unsupported[]`** —— 它们被兑现了，只是不由上游兑现。
把它们报成"上游不支持"会让调用方以为能力缺失。
（`execution_expires_after` 是唯一的例外形态：上游有对应的 `timeout` 字段，
所以它是**真翻译**，见 `resolve_timeout`。）

=============================================================================
九个决定，都是"会真花钱"或"会静默出错"的那一类
=============================================================================

1. 🔴 **`watermark` 与 `generate_audio` 一律显式发送**
   两侧默认值不一致或未文档化：`watermark` 原生默认 `false`、上游默认 **`true`**；
   `generate_audio`（2.x 原生默认 `true`）上游藏在 `provider_specific` 里且没写默认。
   省略任何一个 = 把"实际生效值"交给别人的默认值决定，而响应体已不再是降级告知通道
   （`ADR-011`）⇒ 调用方会拿到一个**没人要的水印 / 没人要的音轨**且无从察觉。
   ⚠️ **`generate_audio` 的位置**：火山原生是**扁平**顶层字段，而**上游只认嵌套**
   （`provider_specific.generate_audio`）—— 用户 2026-09-17 明确："上游不接受扁平
   generate_audio，只有火山接受扁平。" ⇒ 本脚本**固定发嵌套**，且**默认开声音**
   （原生 2.x 的默认值就是 `true`）。曾经的 `generate_audio_field` 开关已撤除：
   形态已经实测确定，再留一个能改成扁平/嵌套的旋钮，只会留出"配错了就没声音"的空间
   （静默、且响应体里看不出来）。

2. 🔴 **媒体顺序一字不动，绝不重排**
   Seedance 的提示词里有 `@图像1` / `@视频1` 编号，**编号按媒体的出现顺序算**。
   按"先首帧、再参考图、再视频"分组重排会让 `@图像3` 指向另一张图，且几乎无法归因。
   ⇒ `_split_content` 单次遍历、`build_content` 原序产出。

3. 🔴 **首尾帧与参考素材不可混用 ⇒ 400，不静默丢任何一侧**
   上游把两者做成互斥的两种模式。丢掉参考素材改变的是"用户想要什么"
   （playbook §3.2 的铁律），所以只能 400。同理：**只给尾帧**（无首帧）⇒ 400 ——
   那是另一个请求，不是"降一档"。

4. **模型名透传**（与 `aivideomaker/video@v1` 同一条纪律）
   本层不改值，只认上游那一个模型名（`MODELS`）；需要别名时由**渠道**配 `model_map`。
   其余一律 400 并列出合法值 —— 绝不落默认值（兜底 = 写错一个字母就发到别的模型
   并直接计费）。

5. **支出上限是提交前置条件，且本地先算一遍**
   上游文档**没有公布任何价格或计费公式**，也没有成本预估端点 ⇒ 只有渠道给了
   `credit_table` 才算得出。算不出时**默认拒绝提交**，要显式 `allow_unpriced` 才放行
   —— 与 `seedance20`（动态计价）走同一条 `ADR-004` 路径。

6. **调用方的错、运维的错、上游的错分开报**
   请求体写错 → 400 `InvalidParameter`；渠道头配错（`model_map` 值非法 / 重复键、
   遗留已撤除的 `X-Channel-Options.model`、缺 `max_credits`）→ `channel_config_error`；
   上游返回 2xx 却没有 `task_id` → `InternalServiceError`（**上游违约不是渠道配错**）。

7. 🔴 **本脚本不声明 cancel 相位**（上游没有取消端点）
   `PHASES` 里没有 `cancel_*`。对未终态任务发 `DELETE` 会在引擎侧**响亮失败**
   （`ADR-014`），而不是在本地伪造一个 `cancelled` 却让上游任务继续跑（继续计费）、
   并把并发槽位提前释放掉。

8. 🔴 **上游业务码由错误相位映射**（`create_error` / `query_error`，`ADR-015`）
   非 2xx 的错误体**不会**经过 `*_response` 相位（引擎在 `raise_for_status` 处就抛了），
   所以"上游并发已满"（`400015`）与"余额不足"（`400001`）原先都会落成
   400 `InvalidParameter` —— 调用方会读成"我的请求写错了"，**不退避、直接放弃**。
   现在这两个相位把语义明确的业务码翻译成契约里已有的 code：

       400015 并发已满        → 429 ServerOverloaded（带 Retry-After）
       400001 使用限制/余额不足 → 429 QuotaExceeded
       400900/901/902 计费账户 → 403 AccountOverdueError
       429000 过于频繁        → 429 RateLimitExceeded.ModelAccountRpmExceeded
       429002 使用限制        → 429 QuotaExceeded
       invalid / 400000       → 400 InvalidParameter

   ⚠️ 三条边界：① 上游**错误体的字段名未证实**（`ref_code` / `code` / `error.code` 都认，
   都认不出就**不拦**、交回引擎的通用映射）；② `500000`（"服务繁忙"又写"非法 model"）
   **刻意不映射**，留给 5xx 的通用规则（502），因为它自相矛盾、猜一半就是赌；
   ③ `Retry-After` **只透传上游给的值**，上游没说时用渠道声明的 `retry_after_seconds`，
   两者都没有就给不出这个头（**不编一个数**）。

9. 🔴 **查询接口没有参数 ⇒ 任务身份来自 API key，因此必须「验明记录归属」**
   官方文档写了 query 参数 `id`，但**实测该接口不接受参数**（用户 2026-09-17 给的 curl
   只有 `Authorization` 头 ⇒ `GET /v1/video/status`）。身份因此来自**凭证**，这带来一个
   最坏形态的错：

   > #A 已在上游跑完、但没人轮询过（本地仍非终态）；同一把钥匙随后建了 #B。
   > 这时查 #A，上游回的是**它当前那个任务** —— 若直接采信，就会把 **#B 的状态与产物
   > 写到 #A 上**。产物链接是真的、状态是真的，只是**属于另一个任务**，从响应体上
   > 完全看不出来（`ADR-011` 已把诊断挪出响应体）。

   ⇒ `status_binding`（默认 `credential`）下，`query_response` 先比对返回记录里的
   `task_id` 与本任务的 `upstream_task_id`：不一致就 **400 并说明原因**，绝不采信。
   记录里没有 `task_id` 字段时**放行**（无据可依，不能凭空拒绝）。

   两种绑定形态：

       credential（默认）：`GET /v1/video/status` 不带参数 + **校验 task_id 归属**
       task_id          ：`GET /v1/video/status?<query_id_param>=<task_id>`（文档形态）
                          —— 上游自己按参数过滤，不需要我们校验

   ⚠️ 连带结论：**同一把钥匙同一时间只该有一个在跑任务**（上游一次只认得一个）。
   本层无法替上游兜住这件事，但渠道可以：配 `max_concurrency: 1`（引擎闸门键正是
   `provider:凭证指纹`）。配了之后 mismatch 只可能出现在"旧任务被新任务顶掉"那一种情形，
   也就是上面那段 —— 那时 400 是唯一诚实的选择。

=============================================================================
参数口径（与目标契约的差值，逐条对应上游文档 §7 的差异表）
=============================================================================

    duration   4–15 整数；原生 `-1`（模型自选）⇒ 取原生默认 5 + warning；
               缺省 ⇒ 同样补原生默认 5 + warning（上游必填）
    frames     上游无此字段 ⇒ 按 24fps 换算成 duration + warning
    resolution 缺省补原生默认 720p；4k 越界 ⇒ 钳到 1080p + warning（上游必填）
    ratio      缺省补原生默认 16:9；`21:9` 按最接近宽高比吸附；
               `adaptive` 上游无语义 ⇒ 落 16:9 + warning（与兄弟脚本同口径）
    timeout    由 `execution_expires_after` 翻译：区间内原样发；高于上界钳到 172800；
               **低于下界不发**（绝不为满足上游下限而延长调用方给的上限）
    seed/camera_fixed/return_last_frame/draft/service_tier/priority/… ⇒ unsupported[]

⚠️ "就近吸附"只用在**越界的取值**上（与兄弟脚本一致），且一律写 warning；
`docs/04_能力映射与降级.md` §2.4 的顺序（先定最终取值 → 再估成本 → 再比上限 →
才发上游）在本文件里就是 `plan_create` 的语句顺序。
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote   # 查询参数值转义（沙箱的白名单按根模块判定，`urllib` 在里）

#: 本脚本实现的相位（03_引擎架构.md §5.1）。**没有 cancel_***：上游无取消端点（决定 7）。
#: `create_error` / `query_error` 是**错误相位**（`ADR-015`）：上游非 2xx 时由引擎调用，
#: 把厂商业务码映射成契约里已有的 `error.code`；不声明它们就只剩通用映射（HTTP 状态）。
PHASES = (
    "create_request",
    "create_response",
    "create_error",
    "query_request",
    "query_response",
    "query_error",
)

#: 上游认识的模型名（官方文档当前只列这一个）。`POST /v1/video/create` 的 **body** 字段。
MODELS: tuple[str, ...] = ("doubao-seedance-2-0-260128",)

#: Seedance 规范的 ratio / resolution 全集（前门允许的值）。
ARK_RATIOS = frozenset({"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"})
ARK_RESOLUTIONS = frozenset({"480p", "720p", "1080p", "4k"})

#: Seedance 规范里已知的 `content[].type`（用来区分"上游没这能力"与"请求写错了"）。
NATIVE_CONTENT_TYPES = frozenset({"text", "image_url", "video_url", "audio_url", "draft_task"})

#: 上游确实没有对应能力的字段 —— 进 `unsupported[]`，**不发**。
#: ⚠️ 判断时 `body[k]` 与 `body.extra_body[k]` 都要查（未建模字段走 extra_body）。
UNSUPPORTED_FIELDS = (
    "seed",                  # 无随机种子
    "camera_fixed",          # 无固定镜头
    "return_last_frame",     # 无尾帧产出（另有一条 warning 提示拼接链路会断）
    "draft",                 # 无样片模式
    "service_tier",          # 无离线推理
    "priority",
    "safety_identifier",
    "tools",
    "omni_reference_task_type",
    "output_format",
)

#: 上游 TaskStatus → Seedance 六态（上游文档 §4.2）。
STATUS_TO_ARK = {
    "pending": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
}

#: 上游 `ref_code` → 出口 `(error.code, HTTP)`（决定 8）。
#: **只收语义明确的那些**；`500000`（"服务繁忙"又叫"非法 model"）刻意不映射 ——
#: 它自相矛盾，猜一半就是赌，交给 5xx 的通用规则（502）更诚实。
ERROR_CODES: dict[str, tuple[str, int]] = {
    "invalid": ("InvalidParameter", 400),
    "400000": ("InvalidParameter", 400),
    "400015": ("ServerOverloaded", 429),
    "400001": ("QuotaExceeded", 429),
    "400900": ("AccountOverdueError", 403),
    "400901": ("AccountOverdueError", 403),
    "400902": ("AccountOverdueError", 403),
    "429000": ("RateLimitExceeded.ModelAccountRpmExceeded", 429),
    "429002": ("QuotaExceeded", 429),
}

#: 错误体里可能承载业务码 / 人话的键。⚠️ 官方文档**没有给错误响应体示例** ⇒
#: 只做"多认几个常见位置"，一个都没认出来就**不拦**（决定 8 边界①）——
#: 猜错方向的代价是双向的：猜"读得出"会读错码，猜"读不出"就永远落回 400。
_ERROR_CODE_KEYS = ("ref_code", "code", "error_code")
_ERROR_MESSAGE_KEYS = ("message", "msg", "error_message", "detail")

#: 上游 `timeout` 的合法区间（官方字段表）。
UPSTREAM_TIMEOUT_MIN = 3600
UPSTREAM_TIMEOUT_MAX = 172800

#: 单一模型的规格表（口径见模块 docstring）。**每模型一张表**是本项目的硬纪律 ——
#: 一张全局表套所有模型正是旧实现被撤掉的写法。
MODEL_SPECS: dict[str, dict[str, Any]] = {
    "doubao-seedance-2-0-260128": {
        "ratio_field": "ratio",
        "ratios": ("16:9", "4:3", "1:1", "3:4", "9:16"),
        "ratio_default": "16:9",
        # 连续区间 ⇒ **钳制**，不做就近吸附（吸附是离散档位才需要的动作，且它是跨档涨价的入口）
        "duration": {"type": int, "min": 4, "max": 15, "default": 5},
        "resolution": {
            "type": str,
            "allowed": ("480p", "720p", "1080p"),
            "default": "720p",
        },
        "content": "senseaudio",
        # 原生 2.x 的 `generate_audio` 默认 true；上游把它藏在 provider_specific 里。
        "generate_audio_default": True,
    },
}

#: 2026-09-16 撤除的渠道键（渠道级"钉住槽位"）。**遗留即响亮失败**，见 `ADR-012`：
#: 以为它还生效的运维会以为这个渠道只跑某个模型，而实际上任何合法模型名都会被逐字发出去。
_REMOVED_OPTION_KEYS = ("model",)

#: 布尔字段的容错取值。⚠️ 不能直接用 `bool()`：字符串 `"false"` 是**真值**，
#: 会把"别加水印"变成"加水印"—— 而这个字段的默认值两侧刚好相反（决定 1），
#: 一旦读反就是静默交付一个带水印的产物。
_TRUE = ("1", "true", "yes", "on", "t", "y")
_FALSE = ("0", "false", "no", "off", "f", "n", "")


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _option_choice(
    ctx,
    options: Mapping[str, Any] | None,
    key: str,
    allowed: tuple[str, ...],
    default: str,
) -> str:
    """读一个**取值受枚举约束**的渠道选项。非法值 ⇒ `channel_config_error`（运维的错）。

    选枚举而不是"随便填"的理由：这个选项会**改变发出去的请求形状**（查询带不带参数、参数叫什么）。
    静默接受一个拼错的值，就等于让请求悄悄退回默认形态 —— 而"我明明配了"这种认知偏差最难查。
    """
    raw = (options or {}).get(key)
    if raw in (None, ""):
        return default
    value = str(raw).strip()
    if value not in allowed:
        _fail_config(
            ctx,
            f'X-Channel-Options.{key}="{value}" is not supported '
            f"(expected one of: {', '.join(allowed)})",
        )
    return value


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
    一定从那条路径进来（`/docs` 试调、将来的别的调用点都会走这里）。
    """
    name = str(model or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[1].strip()
    return name


def resolve_model_map(options: Mapping[str, Any] | None, ctx) -> dict[str, str]:
    """渠道配置的模型映射表：键 = **调用方写什么**，值 = **上游认什么**。

    值必须是 `MODELS` 之一。键里含 `*` 即为通配模式：`*` 是唯一元字符（可出现在任意位置），
    大小写敏感，`?` / `[` / `]` 按字面量处理。精确命中永远压过通配。

    ⚠️ 本上游的模型名**与火山原生 ID 相同** ⇒ 绝大多数渠道**不需要配这张表**。
    保留它是为了两件事：① 别名/跨代次折叠；② 与兄弟脚本行为一致（同一个机制，
    换上游不用换脑子）。
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
            '{"<name the caller sends>": "<upstream model>"}',
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
        if slot not in MODELS:
            _fail_config(
                ctx,
                f"model_map[{name!r}]={slot!r} is not an upstream model "
                f"(expected one of: {', '.join(MODELS)})",
            )
        # JSON 同名键会**静默覆盖**，而"哪一条生效"直接决定账单 ⇒ 显式拒绝。
        # 大小写不同也拒绝：`Doubao-…` 与 `doubao-…` 是同一个意图，取其一就是猜。
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


def reject_removed_option_keys(options: Mapping[str, Any] | None, ctx) -> None:
    """撤除过的渠道键一旦出现 ⇒ `channel_config_error`（配置问题，修复人在运维）。

    静默忽略是更坏的选择：这个键的语义是"本渠道只服务某个模型"，运维据此认为
    "调用方传错名字会被拦住"；一旦它不再生效却仍被接受，那层保护就无声消失了。
    """
    for key in _REMOVED_OPTION_KEYS:
        if key in (options or {}):
            _fail_config(
                ctx,
                f"X-Channel-Options.{key} was removed on 2026-09-16: the channel-level "
                "pinned slot is gone. Use model_map (exact match: the name the caller "
                "sends -> the upstream model), or have callers send the model name "
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
    """裸模型名 → `(上游模型名, 是否走了渠道映射表, 命中的通配模式或 None)`。

    **本函数不猜、不兜底**（通配不是兜底：它是渠道显式声明的策略）。

    判定顺序：① 精确命中映射表；② **唯一**的通配命中（含 `*` 单键）；
    ③ 名字本身就是上游模型名 ⇒ 逐字透传；④ 其余 ⇒ 400。
    ⚠️ ② 在 ③ **之前** ⇒ `{"*": "…"}` 会把合法模型名也改写（这是本决定要的语义）。
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
        elif name in MODELS:
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
                f"must be one of the upstream models: {', '.join(MODELS)}.{hint}",
                "model",
            )

    return upstream, mapped, pattern


def _aspect_value(ratio: Any) -> float:
    m = re.match(r"^(\d+)\s*:\s*(\d+)$", str(ratio).strip())
    if not m:
        return 1.0  # adaptive 之类，取中性值
    w, h = int(m.group(1)), int(m.group(2))
    return (w / h) if h else 1.0


def resolve_ratio(ratio: Any, model: str, warnings: list[str]) -> str:
    """Seedance ratio → 上游比例字面量。上游 `ratio` **是必填项**，所以这里总有返回值。

    - `adaptive`：上游没有自选语义，而本层**读不到首帧图的真实尺寸**（沙箱无网络）
      ⇒ 落原生默认 `16:9` + warning（与 `aivideomaker/video@v1` 同口径）。
      ⚠️ 代价：竖屏素材的 `adaptive` 会变成横屏 —— 要精确比例请调用方显式传。
    - 允许集外的具体比例（`21:9`）：按**最接近的宽高比**吸附 + warning。
    """
    spec = MODEL_SPECS[model]
    value = str(ratio or "").strip()
    if not value:
        warnings.append(
            f"ratio was not given; defaulted to {spec['ratio_default']} (the native default "
            "for this model; the upstream requires this field)"
        )
        return spec["ratio_default"]
    allowed = spec["ratios"]
    if value == "adaptive":
        warnings.append(
            'this upstream has no "adaptive" ratio; fell back to 16:9. adaptive needs the '
            "first-frame image's own aspect ratio, which this layer cannot read."
        )
        return "16:9"
    if value in allowed:
        return value
    target = _aspect_value(value)
    nearest = min(allowed, key=lambda a: (abs(_aspect_value(a) - target), a))
    warnings.append(f'ratio "{value}" is not supported by this upstream; snapped to "{nearest}"')
    return nearest


def _numeric_resolution(value: Any) -> int | None:
    """`"720p"` → 720、`"4k"` → 4000（`k` 是 1000 的简写）。

    ⚠️ 必须吃下 `4k`：前门允许它（原生枚举里有），而只认 `\\d{3,4}` 会把它读成"认不出"
    ⇒ 走进缺省分支变成 **720p**：一次静默的、向下跳两档的降质。实测就是这么发现的。
    """
    m = re.search(r"(\d+)\s*([kK])?", str(value or ""))
    if not m:
        return None
    number = int(m.group(1))
    return number * 1000 if m.group(2) else number


def resolve_resolution(resolution: Any, model: str, warnings: list[str]) -> str:
    """Seedance resolution → 上游字面量（`480p` / `720p` / `1080p`）。

    上游 `resolution` **是必填项**（原生可省略），所以缺省时补**原生默认** `720p`
    并写 warning —— 不补就等于把必填项交给上游报错，而那个报错发生在请求已经发出之后。
    `4k` 超出上游能力 ⇒ 钳到 `1080p` + warning（向下，不涨价）。
    """
    spec = MODEL_SPECS[model]["resolution"]
    value = str(resolution or "").strip()
    num = _numeric_resolution(value) if value else None
    if num is None:
        warnings.append(
            f"resolution was not given; defaulted to {spec['default']} (the native default "
            "for this model; the upstream requires this field)"
        )
        return spec["default"]

    allowed = spec["allowed"]
    for literal in allowed:
        if _numeric_resolution(literal) == num:
            return literal
    picked = allowed[-1] if num > max(_numeric_resolution(a) for a in allowed) else allowed[0]
    warnings.append(f"this upstream accepts resolution {list(allowed)} only; {num} -> {picked}")
    return picked


def resolve_duration(duration: Any, model: str, warnings: list[str]) -> int:
    """Seedance duration → 上游整数秒。

    上游 `duration` 是 **4–15 的连续区间**（不是档位）⇒ **直接钳制，不做就近吸附**；
    吸附是跨档涨价的入口，而区间类根本没有"档"可吸附。缺省补**原生默认** 5（上游必填）。
    """
    spec = MODEL_SPECS[model]["duration"]
    lo, hi = int(spec["min"]), int(spec["max"])
    if duration in (None, ""):
        picked = int(spec["default"])
        warnings.append(
            f"duration was not given; defaulted to {picked}s (the native default for this "
            "model; the upstream requires this field)"
        )
        return picked
    value = int(float(duration))
    picked = min(max(value, lo), hi)
    if picked != value:
        warnings.append(
            f"duration {value}s is outside this upstream's range [{lo},{hi}]; clamped to {picked}s"
        )
    return picked


def resolve_timeout(expires_after: Any, warnings: list[str]) -> int | None:
    """`execution_expires_after` → 上游 `timeout`（可选字段）。返回 None = **不发这个字段**。

    三条规则，方向都是"不给账单和时长送惊喜"：

    - 落在 `[3600, 172800]` ⇒ 原样发；
    - **高于上界** ⇒ 钳到 172800 + warning（向下，安全方向）；
    - **低于下界** ⇒ **不发** + warning：上游最小值 3600 比调用方要的更**长**，
      为满足它而延长超时属于"向上回退"（`docs/04` §2.2 默认禁止）。调用方的上限仍由
      引擎看门狗按原值执行，所以"不发"并没有丢语义，只是不把上游的过期判定推到更晚；
    - 没给 ⇒ 不发（保持上游自己的默认）。
    """
    if expires_after in (None, ""):
        return None
    try:
        value = int(float(expires_after))
    except (TypeError, ValueError):
        warnings.append(
            f"execution_expires_after={expires_after!r} is not a number; the upstream timeout "
            "was left unset (the local watchdog still applies)"
        )
        return None
    if value > UPSTREAM_TIMEOUT_MAX:
        warnings.append(
            f"execution_expires_after={value}s exceeds this upstream's maximum "
            f"{UPSTREAM_TIMEOUT_MAX}s; the upstream timeout was clamped to {UPSTREAM_TIMEOUT_MAX}s"
        )
        return UPSTREAM_TIMEOUT_MAX
    if value < UPSTREAM_TIMEOUT_MIN:
        warnings.append(
            f"execution_expires_after={value}s is below this upstream's minimum "
            f"{UPSTREAM_TIMEOUT_MIN}s; the upstream timeout was omitted rather than extended "
            "(the local watchdog still expires the task at your value)"
        )
        return None
    return value


def _split_content(items: list) -> dict:
    """拆 `content[]` → 文本、**有序媒体**、样片引用、以及无法表达的错误清单。

    🔴 **媒体保持调用方给的相对顺序**（`media` 是一个有序列表）。这不是洁癖：
    Seedance 的提示词里有 `@图像1` / `@视频1` 这类编号，**编号按媒体的出现顺序算**
    （`seedance-api-reference.md` §3.2）。若这里按"先首帧、再参考图、再视频"分组重排，
    调用方写的 `@图像3` 就会指向另一张图 —— 而且几乎无法归因。
    ⇒ 单次遍历、逐项改写类型与字段位置，**绝不重排**。

    两类"坏输入"分开处理，因为它们指向的修复动作完全不同：

    - **上游没有这个能力** ⇒ `unsupported`：目前只有 `draft_task`（上游无样片模式）；
    - **请求本身写错了** ⇒ `errors`（400）：认不出的 `type`（不是原生类型）、
      或认得类型却没有 URL。报成 `unsupported[]` 会把一个畸形请求说成能力缺口
      （`aivideomaker/video@v1` 在这一处把它们混在一起，本脚本刻意分开）。
    """
    out: dict[str, Any] = {
        "texts": [], "media": [], "draft": [], "unsupported": [], "errors": [],
    }
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            out["errors"].append(f"content[{index}] is not an object")
            continue
        kind = str(item.get("type") or "")
        if kind == "text":
            if str(item.get("text") or "").strip():
                out["texts"].append(str(item["text"]))
            continue
        if kind == "draft_task":
            holder = item.get("draft_task")
            if isinstance(holder, Mapping) and holder.get("id"):
                out["draft"].append(holder["id"])
            else:
                out["errors"].append(f"content[{index}] draft_task has no id")
            continue
        if kind not in NATIVE_CONTENT_TYPES:
            out["errors"].append(
                f'content[{index}].type="{kind}" is not a Seedance content type '
                f"({', '.join(sorted(NATIVE_CONTENT_TYPES))})"
            )
            continue

        role = str(item.get("role") or "").strip()
        holder_key = {"image_url": "image", "video_url": "video", "audio_url": "audio"}.get(kind)
        holder = item.get(kind)
        url = ""
        if isinstance(holder, Mapping):
            url = str(holder.get("url") or "").strip()
            if not role:
                # 兼容把 role 写进 holder 内部的第三方形态（前门已归一化，这里只做兜底）
                role = str(holder.get("role") or "").strip()
        if not url:
            out["errors"].append(
                f"content[{index}] ({kind}) has no url — this upstream does support "
                f"{holder_key} inputs, so this is a request error, not a capability gap"
            )
            continue
        out["media"].append({"kind": holder_key, "role": role, "url": url})
    return out


def build_content(prompt: str, media: list, model: str, warnings: list[str], ctx) -> list:
    """有序媒体 → 上游 `content[]`（类型名与字段位置都在这里改写）。

    上游的两种模式**互斥**（首尾帧 / 参考素材），且 `audio` 不能单独出现。
    这些是**能力边界**，不是可以悄悄省略的细节 ⇒ 违反即 400。
    """
    first = [m for m in media if m["kind"] == "image" and m["role"] == "first_frame"]
    last = [m for m in media if m["kind"] == "image" and m["role"] == "last_frame"]
    ref_img = [
        m for m in media
        if m["kind"] == "image" and m["role"] not in ("first_frame", "last_frame")
    ]
    ref_vid = [m for m in media if m["kind"] == "video"]
    ref_aud = [m for m in media if m["kind"] == "audio"]

    if len(first) > 1:
        _reject(
            ctx,
            f"first_frame accepts exactly one image, got {len(first)}; a second image with "
            "role=last_frame is how you express a last frame",
            "content",
        )
    if len(last) > 1:
        _reject(ctx, f"last_frame accepts exactly one image, got {len(last)}", "content")

    # ---- 模式互斥（上游 §3.3）------------------------------------------------
    has_frame = bool(first or last)
    has_ref = bool(ref_img or ref_vid or ref_aud)
    if has_frame and has_ref:
        _reject(
            ctx,
            "this upstream cannot combine first/last-frame inputs with reference assets in one "
            "request (the two modes are mutually exclusive). Send one mode or the other: "
            "dropping either side would change what was asked for.",
            "content",
        )
    if last and not first:
        _reject(
            ctx,
            "this upstream's first/last-frame mode requires a first frame; a last-frame-only "
            'request cannot be expressed. Send a first frame, or move the image to a reference '
            '(role="reference_image").',
            "content",
        )

    # ---- 参考素材上限（上游 §3.3；与原生上限一致，这里只是不让越界请求发出去）----
    if len(ref_img) > 9:
        _reject(ctx, f"this upstream accepts at most 9 reference images, got {len(ref_img)}", "content")
    if len(ref_vid) > 3:
        _reject(ctx, f"this upstream accepts at most 3 reference videos, got {len(ref_vid)}", "content")
    if len(ref_aud) > 3:
        _reject(ctx, f"this upstream accepts at most 3 reference audios, got {len(ref_aud)}", "content")

    # ---- 仅 audio 不合法（上游明文规定）--------------------------------------
    if ref_aud and not (ref_img or ref_vid):
        _reject(
            ctx,
            "this upstream rejects reference audio on its own; it must be paired with at least "
            "one reference image or video",
            "content",
        )

    if not prompt and not media:
        _reject(ctx, "content is required: send a text prompt and/or media", "content")

    # ---- 改写：类型名 + URL 位置 + role 名（上游 §3.2）-----------------------
    out: list = []
    if prompt:
        out.append({"type": "text", "text": prompt})
    for item in media:
        kind, role, url = item["kind"], item["role"], item["url"]
        if kind == "image":
            if role in ("first_frame", "last_frame"):
                out.append({"type": "image", "url": url, "role": role})
                continue
            if role and role != "reference_image":
                warnings.append(
                    f'role "{role}" is not an image role in the Seedance contract; it was sent '
                    "as a reference image"
                )
            out.append({"type": "image", "url": url, "role": "reference"})
        elif kind == "video":
            out.append({"type": "video", "video_url": url})
        else:  # audio
            out.append({"type": "audio", "audio_url": url})
    return out


def build_upstream_body(
    model: str, parts: Mapping[str, Any], plan: Mapping[str, Any], warnings: list[str], ctx
) -> dict:
    """投影成上游创建请求体（字段表见上游契约 §3.1）。"""
    if parts["errors"]:
        _reject(ctx, "cannot express this request: " + "; ".join(parts["errors"]), "content")
    if parts["draft"]:
        _reject(
            ctx,
            "draft_task references are not supported by this upstream (it has no draft mode)",
            "content",
        )

    texts = parts["texts"]
    prompt = "\n".join(texts).strip()
    if len(texts) > 1:
        # 上游的 content 只接受**一条** text。合并而不是只取最后一条：丢掉任何一条都改变了
        # 提示词。合并保持媒体顺序不变，所以 `@图像n` 编号不受影响。
        warnings.append(
            f"{len(texts)} text items were merged into a single prompt (this upstream accepts "
            "one text item)"
        )
    content = build_content(prompt, parts["media"], model, warnings, ctx)

    if plan.get("return_last_frame_requested"):
        warnings.append(
            "return_last_frame is not supported by this upstream: no tail-frame image is "
            "produced, so a continuous-video-splicing chain that depends on it will break"
        )

    body: dict[str, Any] = {
        "model": model,
        "content": content,
        "duration": plan["duration"],
        "resolution": plan["resolution"],
        "ratio": plan["ratio"],
        # 🔴 显式发送：上游默认 `true`、原生默认 `false`（决定 1）。
        #    省略它 = 调用方会拿到一个没人要的水印，而响应体已不再是降级告知通道。
        "watermark": bool(plan["watermark"]),
    }
    # 🔴 `generate_audio` **固定发嵌套形态**（决定 1）：上游只认 `provider_specific.generate_audio`，
    #    扁平只有火山原生吃。发错形态 = **静默没声音**（成品没音轨，而响应体里看不出来），
    #    所以这里不留开关、也不给"两种都发"的余地。
    body["provider_specific"] = {"generate_audio": bool(plan["generate_audio"])}
    if plan.get("timeout"):
        body["timeout"] = plan["timeout"]
    return body


def estimate_credits(
    model: str, duration: Any, table: Mapping[str, Any] | None = None
) -> int | None:
    """上游积分估算。返回 None = 无法预估。

    🔴 上游**不公布计费公式、也没有成本预估端点** ⇒ 唯一能算出数字的途径是渠道给
    `X-Channel-Options.credit_table`（`{模型名: 每秒费率}`）。计费知识本来就属控制面
    （架构 D1），所以这里只读配置、不写死任何费率。给不出就返回 None，由 `plan_create`
    要求调用方显式接受"成本不可在提交前验证"，**而不是编一个数字**。
    """
    if model not in MODEL_SPECS:
        return None
    try:
        seconds = int(float(duration))
    except (TypeError, ValueError):
        return None
    if isinstance(table, Mapping):
        per_second = table.get(model)
        if per_second is not None:
            try:
                return int(round(seconds * float(per_second)))
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
    raw = extra.get("senseaudio_max_credits")
    if raw is None or raw == "":
        return cap
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        _reject(
            ctx,
            "extra_body.senseaudio_max_credits must be a non-negative integer",
            "extra_body.senseaudio_max_credits",
        )
    return raw if cap is None else min(raw, cap)


def plan_create(body: Mapping[str, Any], options: Mapping[str, Any] | None, ctx) -> dict:
    """Seedance 创建请求 → 完整翻译计划（纯函数，不联网、不读环境变量）。

    语句顺序就是 `docs/04_能力映射与降级.md` §2.4 的顺序：
    定最终取值 → 估成本 → 比上限 → 才发上游。
    """
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
    unsupported.extend(parts["unsupported"])

    extra = body.get("extra_body") if isinstance(body.get("extra_body"), Mapping) else {}
    for key in UNSUPPORTED_FIELDS:
        if body.get(key) is not None or extra.get(key) is not None:
            unsupported.append(key)

    # ---- 比例（前门只拒绝"对任何上游都畸形"的值）-----------------------------
    ratio = str(body.get("ratio") or "").strip()
    if ratio and ratio not in ARK_RATIOS:
        _reject(ctx, f'ratio: invalid enum value "{ratio}"', "ratio")

    # ---- 分辨率（大小写不承载语义：上游要小写，这里统一后再校验）-------------
    resolution = None
    if body.get("resolution") not in (None, ""):
        value = str(body["resolution"]).strip()
        if value.lower() not in ARK_RESOLUTIONS:
            _reject(ctx, f'resolution: invalid enum value "{value}"', "resolution")
        resolution = value.lower()

    # ---- 时长：`frames` 优先于 `duration`（原生契约），两者都缺则补原生默认 ----
    duration = None
    frames = body.get("frames")
    if frames not in (None, ""):
        try:
            seconds = max(1, round(float(frames) / 24))
        except (TypeError, ValueError):
            _reject(ctx, f"frames: invalid value {frames!r}", "frames")
        duration = seconds
        warnings.append(
            f"frames={frames} converted to {seconds}s at 24fps (this upstream has no frames field)"
        )
    elif body.get("duration") not in (None, ""):
        duration = body["duration"]
        try:
            if int(float(duration)) == -1:
                # `-1` = 模型自选。上游要具体整数 ⇒ 取**原生默认值** 5 承接"你自选"
                # （区间下界 4 纯属本层挑的数字，理由见上游契约 §8）。
                duration = spec["duration"]["default"]
                warnings.append(
                    f"duration=-1 (model-chosen) cannot be expressed upstream; mapped to "
                    f"{duration}s (the native default for this model)"
                )
        except (TypeError, ValueError):
            _reject(ctx, f"duration: invalid value {duration!r}", "duration")

    plan = {
        "duration": resolve_duration(duration, upstream_model, warnings),
        "resolution": resolve_resolution(resolution, upstream_model, warnings),
        "ratio": resolve_ratio(ratio, upstream_model, warnings),
        # 原生默认：`watermark` false / 2.x `generate_audio` true。两个都**显式**落到请求里，
        # 因为上游的默认值与前者相反、后者未文档化（决定 1）。
        "watermark": _as_bool(body.get("watermark"), False),
        "generate_audio": _as_bool(
            body.get("generate_audio"), bool(spec["generate_audio_default"])
        ),
        "timeout": resolve_timeout(body.get("execution_expires_after"), warnings),
        "return_last_frame_requested": _as_bool(body.get("return_last_frame"), False),
    }
    upstream_body = build_upstream_body(upstream_model, parts, plan, warnings, ctx)

    # ---- 计费护栏（顺序：先定最终取值 → 再估成本 → 再比上限）-----------------
    max_credits = resolve_max_credits(body, options, ctx)
    if max_credits is None:
        _reject(
            ctx,
            "this upstream is billed on submit and exposes no server-side spend cap. Provide one "
            "via `extra_body.senseaudio_max_credits` or `X-Channel-Options.max_credits`; "
            "refusing to submit without it.",
            "extra_body.senseaudio_max_credits",
        )

    estimated = estimate_credits(upstream_model, plan["duration"], options.get("credit_table"))
    if estimated is None:
        # 上游不公布价格 ⇒ 算不出成本时默认**拒绝提交**，只有显式接受才放行。
        # 这是刻意的摩擦：算不出来不该由我们替用户拍板放行（ADR-004）。
        advice = (
            "this upstream publishes no price list and has no cost-estimate endpoint, so the "
            "spend cap cannot be verified before submit. Give a per-second rate via "
            "`X-Channel-Options.credit_table`, or accept an unverifiable cost via "
            "`X-Channel-Options.allow_unpriced` / `extra_body.senseaudio_allow_unpriced`."
        )
        allowed = bool(options.get("allow_unpriced")) or bool(
            extra.get("senseaudio_allow_unpriced")
        )
        if not allowed:
            _reject(ctx, advice, "extra_body.senseaudio_max_credits")
        warnings.append(
            f"cost is not verifiable before submit and the caller accepted that: {advice}"
        )
    elif estimated > max_credits:
        _reject(
            ctx,
            f"estimated cost {estimated} credits exceeds the spend cap {max_credits}; "
            "refusing to submit",
            "extra_body.senseaudio_max_credits",
        )

    return {
        "requested": {
            "model": body.get("model"),
            "content": items,
            "ratio": body.get("ratio"),
            "resolution": body.get("resolution"),
            "duration": body.get("duration"),
            "frames": body.get("frames"),
            "watermark": body.get("watermark"),
            "generate_audio": body.get("generate_audio"),
            "execution_expires_after": body.get("execution_expires_after"),
            "callback_url": body.get("callback_url"),
        },
        "effective": {
            "upstream_model": upstream_model,
            # "我请求的 vs 实际跑的"这一对：响应体里没有 `model` 字段（ADR-011），
            # 所以判据只剩这里（→ logfire 的 task.effective.*）与 dry-run。
            "model_requested": str(body.get("model") or ""),
            "model_map_applied": model_map_applied,
            "model_map_pattern": model_map_pattern,
            "ratio": plan["ratio"],
            "resolution": plan["resolution"],
            "duration": plan["duration"],
            "watermark": plan["watermark"],
            "generate_audio": plan["generate_audio"],
            "timeout": plan["timeout"],
            "billed": True,
            "billing_note": (
                "this upstream bills on submit and publishes no price list; the local estimate "
                "(credit_table) is the only pre-submit guard"
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


def _as_int_or_none(value: Any) -> int | None:
    """数字字段的容错转换（上游可能把数字回成字符串）。取不到就 `None`，**不编默认值**。"""
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def map_status(raw_status: Any) -> str:
    """上游状态 → Seedance 六态。未知/缺失一律按 `running`（**不猜终态**）。

    猜成终态会触发落库、释放并发槽位与回调推送 —— 那比"多轮询一次"贵得多。
    """
    return STATUS_TO_ARK.get(str(raw_status or "").strip().lower(), "running")


def normalize_task(raw: Mapping[str, Any] | None) -> dict:
    """上游任务记录 → Seedance 查询响应片段（`03_引擎架构.md` §5.1 的形状）。

    🔴 上游**没有任何计费 / 用量字段** ⇒ 这里不产出 `usage`（响应里是 `null`）。
    这是事实，不是缺陷：宁可说"不知道消耗了多少"，也不编一个 token 数出来
    （`ADR-010`：计费对账以 new-api 与上游账单两侧的真实数据为准）。

    `seed` / `framespersecond` 之类的原生默认值**不在这里产出** —— 引擎的
    `seedance.create_echo` / `render_task` 已负责回填原生字段集。脚本层只报**真知道的值**，
    免得"看起来很完整"的假值顺着 trace 继续流传。
    """
    raw = raw if isinstance(raw, Mapping) else {}
    status = map_status(raw.get("status"))
    error_message = str(raw.get("error_message") or "").strip()
    video_url = str(raw.get("video_url") or "").strip()

    return {
        "status": status,
        # 上游只在 completed 后给产物地址；其余状态一律 None（原生也只在 succeeded 给值）
        "video_url": (video_url or None) if status == "succeeded" else None,
        "duration": _as_int_or_none(raw.get("duration")),
        "ratio": str(raw.get("ratio") or "").strip() or None,
        "resolution": str(raw.get("resolution") or "").strip() or None,
        "error": (
            {
                "code": "GenerationFailed",
                "message": error_message or "video generation failed upstream",
            }
            if status == "failed"
            else None
        ),
        "warnings": [],
        # 上游时间戳本来就是 epoch **秒**（与原生契约一致，无 ISO 转换）
        "created_at": _as_int_or_none(raw.get("created_at")),
        "updated_at": _as_int_or_none(raw.get("completed_at")) or _as_int_or_none(raw.get("created_at")),
        # 上游记录原文留档：只进 logfire 明细（`task.upstream_response`），不进响应体。
        "upstream": dict(raw),
    }


# =============================================================================
# 相位包装（薄）—— 逻辑都在上面
# =============================================================================

_CREATE_PATH = "/v1/video/create"
_STATUS_PATH = "/v1/video/status"


def _base(ctx) -> str:
    base = str(ctx.upstream_url or "").rstrip("/")
    if not base:
        _fail_config(ctx, "X-Upstream-Url is required")
    return base


def _create_url(ctx) -> str:
    """拼创建地址。容两种渠道配法：base（推荐）或整条 endpoint。

    上游的模型在 **body** 里而不是路径里 ⇒ 路径是常量，但渠道仍可能把整条 endpoint
    配进 `X-Upstream-Url`；不剥尾就会变成 `.../v1/video/create/v1/video/create`。
    """
    base = _base(ctx)
    for suffix in (_CREATE_PATH, _STATUS_PATH):
        if base.endswith(suffix):
            return f"{base[: -len(suffix)]}{_CREATE_PATH}"
    return f"{base}{_CREATE_PATH}"


#: 查询的身份绑定形态（决定 9）。
_STATUS_BINDINGS = ("credential", "task_id")


def _status_binding(ctx) -> str:
    return _option_choice(ctx, ctx.options, "status_binding", _STATUS_BINDINGS, "credential")


def _upstream_task_id(ctx) -> str:
    task_id = str(ctx.task.upstream_task_id if ctx.task else "").strip()
    if not task_id:
        _fail_config(ctx, "task has no upstream_task_id")
    return task_id


def _status_url(ctx) -> str:
    """查询地址。**默认不带任何参数** —— 该接口按 API key 回答（模块 docstring 决定 9）。

    | `status_binding` | 请求 | 谁保证"这是本任务" |
    | --- | --- | --- |
    | `credential`（默认） | `GET /v1/video/status` | **我们**：`query_response` 校验返回记录的 `task_id` |
    | `task_id`（文档形态） | `GET /v1/video/status?<query_id_param>=<task_id>` | 上游按参数过滤 |

    ⚠️ 官方文档把 `id` 标为必填 query 参数，但**实测该接口不接受参数**
    （用户 2026-09-17 提供的 curl 只有 `Authorization` 头）。默认跟事实走；
    `task_id` 形态下保留文档的参数名，`query_id_param` 可换名（只允许
    `[A-Za-z_][A-Za-z0-9_]*`，防 URL 注入）。
    """
    base = f"{_base(ctx)}{_STATUS_PATH}"
    if _status_binding(ctx) == "task_id":
        param = str((ctx.options or {}).get("query_id_param") or "id").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", param):
            _fail_config(
                ctx,
                f'X-Channel-Options.query_id_param="{param}" is not a valid query parameter name',
            )
        return f"{base}?{param}={quote(_upstream_task_id(ctx), safe='')}"
    return base


def assert_record_belongs_to_task(ctx, payload) -> None:
    """🔴 验明"上游回的是本任务的记录"（决定 9）。**只在 `credential` 绑定下需要**。

    这是本脚本唯一一处"拿到 2xx 却必须拒绝"的地方，理由是最坏形态的静默错：
    同一把钥匙上，旧任务被新任务顶掉之后，查旧任务会拿到**新任务**的状态与产物 ——
    一切看起来都正常（产物链接真的能下载），只是属于另一个任务。

    三种情况**放行**：绑定形态是 `task_id`（上游自己过滤）、返回体不是对象、
    记录里没有 `task_id` 字段（无据可依 —— 不能凭空拒绝）。
    """
    if _status_binding(ctx) != "credential":
        return
    if not isinstance(payload, Mapping):
        return
    got = str(payload.get("task_id") or "").strip()
    want = str(ctx.task.upstream_task_id if ctx.task else "").strip()
    if not got or not want or got == want:
        return
    ctx.fail(
        "this upstream answers the status endpoint per API key (the endpoint takes no task "
        f"parameter), and the record it returned belongs to task {got!r} — not to this task "
        f"({want!r}). Per-task status for an older task is unavailable once a newer task "
        "exists on the same credential. Poll tasks to a terminal state before creating the "
        "next one, and set X-Channel-Options.max_concurrency=1 so the local gate matches the "
        "upstream's one-task-per-key reality.",
        code="InvalidParameter",
        param="id",
        status=400,
    )


async def create_request(ctx, payload):
    """规范体 → 上游创建请求计划。"""
    plan = plan_create(payload, dict(ctx.options or {}), ctx)
    ctx.plan = plan
    return {
        "method": "POST",
        "url": _create_url(ctx),
        "body": plan["upstream_body"],
    }


async def create_response(ctx, payload):
    """上游创建响应 → `{"task_id": ...}`。

    ⚠️ 非 2xx **到不了这里**（引擎的 `raise_for_status` 先于本相位），所以本相位只管
    2xx 的成功体。两种异常分开报：

    - 响应不是 JSON 对象（多半是 URL 配错、打到了 HTML 页面）⇒ `channel_config_error`（运维）；
    - 是 JSON 但没有 `task_id`（上游契约漂移）⇒ `InternalServiceError`（**不是**调用方的问题）
      —— 引擎自己也有一条同样的兜底断言，两处刻意一致。
    """
    if not isinstance(payload, Mapping):
        _fail_config(
            ctx,
            "upstream create response is not a JSON object (is X-Upstream-Url pointing at the "
            "right host?)",
        )
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        ctx.fail(
            "upstream create response has no task_id (keys received: "
            f"{', '.join(sorted(str(k) for k in payload)) or 'none'})",
            code="InternalServiceError",
            status=502,
        )
    return {"task_id": task_id}


async def query_request(ctx, payload):
    """`ctx.task` → 查询请求计划。"""
    return {"method": "GET", "url": _status_url(ctx)}


async def query_response(ctx, payload):
    """上游任务记录 → 规范化片段。**先验明这条记录是本任务的**（决定 9）。"""
    assert_record_belongs_to_task(ctx, payload)
    return normalize_task(payload)


# =============================================================================
# 错误相位（`ADR-015`）：上游业务码 → 契约里已有的 `error.code`
# =============================================================================

def _upstream_error_code(payload: Any) -> str:
    """从错误体里取上游业务码。**形状未证实** ⇒ 只认几个常见位置，认不出返回空串。

    返回空串 = **不拦**（引擎用 HTTP 状态的通用映射兜底）。这条边界要守住：
    宁可漏认，也不能把别的东西（比如 `error.code` 里放的英文错误名）误当业务码映射出去 ——
    那会把"上游 500"变成"你的参数错了"。
    """
    if not isinstance(payload, Mapping):
        return ""
    for key in _ERROR_CODE_KEYS:
        value = payload.get(key)
        if value not in (None, "") and not isinstance(value, Mapping):
            return str(value).strip()
    nested = payload.get("error")
    if isinstance(nested, Mapping):
        for key in _ERROR_CODE_KEYS:
            value = nested.get(key)
            if value not in (None, ""):
                return str(value).strip()
    return ""


def _upstream_error_message(payload: Any, status: Any) -> str:
    """上游原话。取不到就给一句中性描述 —— **不编原因**。"""
    if isinstance(payload, Mapping):
        for key in _ERROR_MESSAGE_KEYS:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        nested = payload.get("error")
        if isinstance(nested, Mapping):
            for key in _ERROR_MESSAGE_KEYS:
                value = nested.get(key)
                if value not in (None, ""):
                    return str(value)
    return f"upstream returned HTTP {status}"


def _retry_after(ctx) -> float | None:
    """退避秒数：**上游给的优先**，其次渠道声明的 `retry_after_seconds`，都没有就不给。

    ⚠️ "不给"不是疏忽：`Retry-After` 表达的是"上游说要等多久"这个**事实**，
    编一个数字等于伪造它（`ADR-004` 同一口径）。上游没说、而运营方希望给调用方一个
    可退避的值时，由**渠道**声明 —— 配置即事实。
    """
    info = ctx.upstream_error if isinstance(ctx.upstream_error, Mapping) else {}
    for raw in (info.get("retry_after"), (ctx.options or {}).get("retry_after_seconds")):
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


async def _map_upstream_error(ctx, payload) -> None:
    """错误相位共用逻辑：映射得到就 `ctx.fail(...)` 抛出；否则**正常返回**（交回通用映射）。"""
    code = _upstream_error_code(payload)
    mapped = ERROR_CODES.get(code)
    if mapped is None:
        return
    out_code, out_status = mapped
    info = ctx.upstream_error if isinstance(ctx.upstream_error, Mapping) else {}
    ctx.fail(
        f"upstream rejected the request ({code}): {_upstream_error_message(payload, info.get('status'))}",
        code=out_code,
        status=out_status,
        retry_after=_retry_after(ctx) if out_status == 429 else None,
    )


async def create_error(ctx, payload):
    """创建的非 2xx（见模块 docstring 决定 8）。"""
    await _map_upstream_error(ctx, payload)


async def query_error(ctx, payload):
    """查询的非 2xx（与创建同一套映射）。"""
    await _map_upstream_error(ctx, payload)
