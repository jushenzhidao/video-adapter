# 火山方舟 Seedance 视频生成协议 — 目标契约参考

> **定位**：本文件是「把任意上游适配为火山 Seedance 协议」时的**目标契约唯一真源**。
> 适配层前门（对调用方暴露的接口）必须严格按本文件实现；上游侧差异由翻译层吸收。
>
> **整理日期**：2026-09-13
> **主文档**：`https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1520758`（用户指定）
> **可信度标注**：`[官方]` = 来自 volcengine 官方文档/官方 SDK 示例；`[镜像]` = 来自第三方兼容网关文档（网易云信 / 融云 / 302.AI 等），仅用于交叉验证缺口，**不作为实现依据**。

---

## 0. 一句话模型

Seedance 是一个**异步任务式**视频生成协议：`POST` 建任务拿到 `id` → `GET` 轮询到终态 → 从 `content.video_url` 取产物；或传 `callback_url` 由方舟主动推送（推送体与查询接口响应体同构）。

**创建接口只返回 `{"id": "cgt-..."}`，不返回状态。** 任何"创建后直接读 status"的实现都是错的。

### 0.0 🔴 响应的**唯一形状**：原生字段集（2026-09-16 起，本服务的硬约束）

适配层对调用方的响应**逐键等于原生任务对象**：创建只回 `id`；查询只回 §4.1 列出的那些字段。
实现点是 `adapter/seedance.py`（`NATIVE_TASK_KEYS` / `render_created` / `render_task`），
`tests/test_seedance_contract.py` 逐键钉住。**三条"不许做"**（都曾真实发生过）：

| 不许 | 为什么 |
| --- | --- |
| 创建响应多加任何键（曾加过 `provider` / `upstream_task_id` / `script_ref` / `script_sha256` / `upstream_report`） | 原生没有上报块。按原生契约写**严格校验 / schema / SDK 反序列化**的接入方会当场失败；"响应变宽无害"是错的直觉 |
| 查询回显 `model` | 上游模型名很乱（8 个槽位名与各家原生 ID 混写）。本层已改为**模型名透传**（§1.1），回显它对调用方没有信息增量 |
| 查询带回任何诊断块（`requested` / `effective` / `warnings[]` / `unsupported[]` / `upstream`） | 它们是本服务的内部诊断。**改道 logfire**（`task.snapshot` span），排障能力不减、契约不被污染 |

⚠️ **代价（已接受）**：响应体**不再是降级告知通道**。参数被吸附/丢弃、模型被映射，
只在 logfire 与 `dry-run`（`X-Dry-Run: 1`）里可见。`ADR-009` D7 已随之回写。

### 0.1 为什么把 Seedance 选作**目标协议**（而不是自己设计一套）

因为它是目前少见的**规范化的超集协议**：生成模式全部收敛到同一组端点 + 同一套字段体系，覆盖面横跨

| 生成模式 | Seedance 表达方式 |
| --- | --- |
| 文生视频 (t2v) | `content=[{text}]` |
| 图生视频 (i2v) · 首帧 | `content=[{text},{image_url,role:first_frame}]`，或仅 1 张无 role 图 |
| 图生视频 · 首尾帧 | 2 张 `image_url`（`first_frame` + `last_frame`） |
| 视频生视频 (v2v) / 风格·运镜参考 | `{video_url, role:reference_video}` |
| 全能参考 (omni-reference) | text + image + video + audio 自由组合（`omni_reference_task_type`） |
| 有声视频 | `generate_audio: true`（音画联合生成，非后期叠加） |
| 编辑视频 / 延长视频 | 参考素材 + `omni_reference_task_type: edit / extend` |
| 样片模式（低价预览） | `draft: true` |
| 连续视频拼接 | `return_last_frame: true` → 尾帧作为下一任务首帧 |

**推论（决定了整个适配层架构）**：
Seedance 是**超集**、上游是**子集** ⇒ 适配工作的主干是**降维投影（down-projection）**，
而不是"发明新接口"。因此适配层的第一等公民是**降级报告**（哪些能力被丢掉、哪些参数被吸附），
而不是路由或鉴权。详见 `adapter-playbook.md` §3。

---

## 1. 目标路由与鉴权

**火山原生路径**（本文档的权威形态）：

```
/api/v3/contents/generations/tasks
/api/v3/contents/generations/tasks/{id}
```

**本服务的路径逐字等于原生**（不加前缀、不加路径段）；多上游靠 `model` 字段区分：

```
"model": "aivideomaker/doubao-seedance-2-5-260628"
          └── provider ──┘ └──────── 模型 ────────┘
```

- 一个 `base_url` 服务所有上游，**调用方不必为每个上游改 base_url**。
- `provider` 段是本服务的约定，**不属于火山原生契约**（原生 `model` 就是裸模型 ID）；
  它必须与渠道声明的 provider 一致，**不一致返回 400** —— 专门防止"模型名写错 → 静默落到别的上游 → 直接付费"。
- 语法硬约束：`<provider>/<model>`，`provider` 卡 `^[a-z0-9][a-z0-9_-]{0,62}$`，**只允许一个 `/`**。
- 不带 `/` 的裸模型名走渠道声明的默认 provider；渠道未声明 ⇒ 400，**不猜**。

### 1.1 `<model>` 段：**透传**，映射表由渠道配置（2026-09-16 起）

**本层不改模型值**：`<model>` 段剥掉后逐字发给上游。所以合法值就是上游认识的槽位名。
调用方写原生 ID（`doubao-seedance-2-0-260128` 这类）时，由**渠道**给出映射表：

```json
X-Channel-Options: {"model_map": {"doubao-seedance-2-0-260128": "seedance20"}}
```

| 规则 | 内容 |
| --- | --- |
| **解析顺序** | ① 名字**精确命中** `model_map` ⇒ 用表里的**值**；② 名字命中**唯一**的通配模式（含 `*` 单键）⇒ 用表里的**值** —— ⚠️ ② 在 ③ **之前**，所以 `{"*": "t2v"}` 会把合法槽位名也改写掉；③ 名字**本身就是**上游槽位名 ⇒ 逐字透传；④ 其余 ⇒ **400**。**没有别的入口** |
| 匹配方式 | **精确匹配优先**。键里含 `*` 即为通配模式：`*` 是**唯一**元字符（匹配任意字符序列，含空，可出现在名字任意位置），**大小写敏感**，`?` / `[` / `]` 按字面量。**不排序、不取最长** —— 两条通配同时命中 ⇒ `channel_config_error`（旧实现的正则表就是这么把 5 个代次压进同一槽位、账单差 7.3 倍的） |
| 缺省行为 | **不映射**：名字本身必须是上游槽位名。两边都不认识 ⇒ **400**，消息里同时列出合法槽位与渠道已配的键 |
| 值域 | 映射的**值**必须是上游槽位名；否则 `channel_config_error`（配置错误，与"调用方传错"分开报） |
| 重复键 | **显式拒绝**（JSON 同名键会静默覆盖，而"哪条生效"直接决定账单；大小写不同也拒绝） |
| 命中可见性 | 写进 `effective.model_map_applied`（→ logfire 的 `task.effective.model_map_applied`）；命中的**通配模式**另记 `effective.model_map_pattern`（→ `task.effective.model_map_pattern`）—— 通配能**改写**模型值，这一项就是"为什么被改了"的答案。**响应体里没有 `model`**，所以"我请求的 vs 实际跑的"只能靠它或 `dry-run` 核 |
| 已撤除的能力 | **没有**渠道级"钉住槽位"：`X-Channel-Options.model` 于 **2026-09-16 撤除**（它只做一致性断言、不承担任何名字转换，与 `model_map` 重复）。遗留该键一律 `channel_config_error`（**值合法时也照拒**），**不静默忽略** —— 以为它还"钉着"的运维会以为这个渠道只跑某个槽位，而实际上那个依赖已经没有任何代码支撑。该能力现由**通配**承接（`{"*": "<槽位>"}`），⚠️ 但语义不等价：通配是**改写模型值**（请求照样放行），钉住是**拦下请求** |

> **为什么不用正则**（原实现的形态）：`_MODEL_PATTERNS` 第一条是 `(r"seedance", "seedance20")`，
> 于是 `doubao-seedance-2-0-260128` / `…-1-0-pro-250528` / `…-2-5-260628` **全落同一槽位**，
> 且无任何告警。2026-09-16 真上游实测：同一个原生 ID 落进 `seedance20` 的账单是 `t2v` 的
> **7.3 倍**（110 vs 15 积分），且因它是动态计价 ⇒ 支出上限从"提交前可验证"退化成"事后才知道"。
> 教训：**名字→身份的映射必须精确、必须可查、必须由控制面声明**。
- 🔴 **`provider` 段永远是「API 供给方」，不是「模型出品方」**。同一个 `deepseek-v3` 至少有三家供给方，
  端点、凭证、计费、限流全不同，所以三者互不相等：
  `deepseek-official/deepseek-v3` ≠ `aliyun-bailian/deepseek-v3` ≠ `volcengine-ark/deepseek-v3`。
  **不要写 `deepseek/...`** —— 那会让"官方线"和"阿里托管"撞在同一个 token 上，直接误路由。
  模型出品方不进 `model` 前缀、也不加进响应体；归属与分账由控制面的既有映射处理。
- 命名取 `provider`（不用 `biz`，也不用 `vendor`：在 `aliyun-bailian/deepseek-v3` 里
  阿里是**供给方**而非 DeepSeek 的 vendor，用 `vendor` 会让人分不清该填厂商还是供给方）。
  同一供给方有多条接入线时，值取「供给方-线路」（如 `aivideomaker-official`），**字段名不变**。
  详见 `03_引擎架构.md` §2.3。

**鉴权**：`Authorization: Bearer $ARK_API_KEY` + `Content-Type: application/json`。
API Key 为长效 Key，不自动过期（仍建议定期轮换）。

> 🔴 **范围收敛**：**只做原生这一族路径**，不做 LAS 算子与 Agent Plan 企业版的前缀兼容。
> 以下两者是同一逻辑 API 的另两种投放形态，**仅作背景知识**：
>
> | 形态 | Base URL | 与目标的关系 |
> | --- | --- | --- |
> | 方舟标准 `[官方]` | `https://ark.cn-beijing.volces.com/api/v3` | **目标**；路径即 `/contents/generations/*` |
> | 方舟 Agent Plan 企业版 `[官方]` | `.../api/plan/v3` | 非目标；专 Key + 专 BaseURL，混用会失败或额外计费 |
> | LAS 算子 `[官方]` | `https://operator.las.cn-beijing.volces.com/api/v1` | 非目标；字段集为方舟子集 |

---

## 2. 端点总表 `[官方]`

| # | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 1 | `POST` | `/contents/generations/tasks` | 创建视频生成任务 |
| 2 | `GET` | `/contents/generations/tasks/{id}` | 查询单个任务状态与结果 |
| 3 | `GET` | `/contents/generations/tasks` | 查询任务列表（仅最近 7 天） |
| 4 | `DELETE` | `/contents/generations/tasks/{id}` | 取消或删除任务 |

### 2.4 取消 / 删除 `[官方]`

- 同一路径 `DELETE`：对**排队中**任务 = 取消；对**终态**任务 = 删除记录。
- **只有 `queued` 状态的任务可以被取消**（源码表述：`cancelled` 仅支持 `queued` 状态任务被取消）。
- 进入 `cancelled` 后 24h 自动删除。
- 用户主动 `DELETE` 成功后**不触发回调**（`[镜像]` 网易云信明确此语义，与官方"状态变更才回调"一致）。

---

## 3. 创建任务：`POST /contents/generations/tasks`

### 3.1 Body 字段表 `[官方]`

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `model` | string | ✅ | — | 模型 ID（见 §7），非展示名。**本服务约定**：写成 `<provider>/<model>`（如 `aivideomaker/doubao-seedance-2-5-260628`）以区分多上游，见 §1 |
| `content` | object[] | ✅ | — | 多模态输入列表，见 §3.2 |
| `callback_url` | string | ❌ | — | 状态变更回调地址，推送体 = 查询接口响应体 |
| `resolution` | string | ❌ | 见 §7 | `480p` / `720p` / `1080p` / `4k`（4k 仅 2.0 标准版） |
| `ratio` | string | ❌ | `16:9` | `16:9` `4:3` `1:1` `3:4` `9:16` `21:9` `adaptive` |
| `duration` | integer | ❌ | `5` | 秒；与 `frames` 二选一，`frames` 优先 |
| `frames` | integer | ❌ | — | 帧数，需满足 `25+4n`；1.5 pro 不支持 |
| `generate_audio` | boolean | ❌ | 1.x=`false` / 2.x=`true` | 生成与画面同步的音频 |
| `watermark` | boolean | ❌ | `false` | 是否加水印 |
| `seed` | integer | ❌ | `-1` | `[-1, 2^32-1]`；`-1` = 随机 |
| `camera_fixed` | boolean | ❌ | `false` | 参考图场景不支持 |
| `return_last_frame` | boolean | ❌ | `false` | 返回尾帧 PNG，用于连续视频拼接 |
| `draft` | boolean | ❌ | `false` | 样片模式（低价预览），仅部分模型 |
| `service_tier` | string | ❌ | `default` | `default` 在线 / `flex` 离线；**不支持修改已提交任务** |
| `execution_expires_after` | integer | ❌ | `172800` | 超时阈值秒，`[3600, 259200]`；从 `created_at` 起算 |
| `priority` | integer | ❌ | `0` | 执行优先级 |
| `safety_identifier` | string | ❌ | — | 调用方用户标识（合规追溯） |
| `tools` | object[] | ❌ | — | 工具配置 |
| `omni_reference_task_type` | string | ❌ | `auto` | 任务类型引导，见 §9（未建模字段，走 `extra_body`） |
| `output_format` | string | ❌ | `mp4` | 输出容器格式（如 `mov`）；未建模字段 |

> `omni_reference_task_type` / `output_format` 属于**未建模字段**：官方 SDK 走 `extra_body` 传入。
> 适配层判断"上游不支持某参数"时，`body[k]` 与 `body.extra_body[k]` **都要查**。

### 3.2 `content[]` 结构 `[官方]`

> 🔴 **最高频踩坑点：`role` 是 content 项的顶层兄弟字段，不在 `image_url` 对象内部。**
> 官方 SDK 示例（Seedance 2.5 多模态参考）：
> ```json
> { "type": "image_url", "image_url": { "url": "https://..." }, "role": "reference_image" }
> ```
> 但**部分第三方镜像文档**把 `role` 写进 `image_url` 对象内（`{"image_url": {"url": "...", "role": "first_frame"}}`）。
> 二者在线上都出现过 —— **以官方顶层写为准**，前门做兼容解析时可两种都收，出口一律按官方顶层产出。

| `type` | 子字段 | `role` 取值 | 说明 |
| --- | --- | --- | --- |
| `text` | `text` | — | 提示词；可含 `@图像1` / `@视频1` 引用语法 |
| `image_url` | `image_url.url` | `first_frame` / `last_frame` / `reference_image` | 不传 `role` 时，图数量与位置隐含首尾帧语义（见下） |
| `video_url` | `video_url.url` | `reference_video` | Seedance 2.0+ 支持 |
| `audio_url` | `audio_url.url` | `reference_audio` | Seedance 2.0+ 支持，需搭配图片/视频 |
| `draft_task` | `draft_task.id` | — | 引用历史样片任务 ID（1.5 pro 支持） |

**首尾帧的隐式规则** `[镜像]`：仅当**文本提示词同时存在**时，传 2 张无 `role` 的 `image_url` 会被推断为首帧+尾帧，且**顺序即语义**（先首帧后尾帧）。

**`adaptive` 的判定规则** `[官方]`：
- 文生视频 → 依提示词意图选择；
- 首帧/首尾帧 → 依首帧图比例选最近宽高比；
- 多模态参考 → 若是首帧/编辑/延长意图以该图/视频为准，否则以**传入的第一个媒体文件**为准（**优先级：视频 > 图片**）。

### 3.3 响应 `[官方]`

创建成功**只返回任务 ID**：

```json
{ "id": "cgt-20260414114820-*****" }
```

> 🔴 **逐键只有 `id`** —— 本服务的实现必须连"上报块"都不带（本文档 §0.0）。
> 上游 task id、脚本摘要、请求/响应留档改由 logfire 承载。

### 3.4 官方调用示例 `[官方]`

```python
import os, time
from arkruntime import Ark

client = Ark(base_url="https://ark.cn-beijing.volces.com/api/v3",
             api_key=os.environ.get("ARK_API_KEY"))

create_result = client.content_generation.tasks.create(
    model="doubao-seedance-2-5-260628",
    content=[
        {"type": "text", "text": "..."},
        {"type": "image_url",
         "image_url": {"url": "https://.../ref1.png"},
         "role": "reference_image"},
        {"type": "video_url",
         "video_url": {"url": "https://.../ref2.mp4"},
         "role": "reference_video"},
    ],
    generate_audio=True, ratio="16:9", duration=15,
    extra_body={"omni_reference_task_type": "reference", "output_format": "mov"},
)
task_id = create_result.id  # 注意：create_result 里只有 id
```

---

## 4. 查询任务：`GET /contents/generations/tasks/{id}`

### 4.1 响应字段表 `[官方]`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 任务 ID（**仅保留 7 天**，从 `created_at` 起算） |
| `model` | string | 模型名-版本。⚠️ **本服务刻意不回显它**（理由见 §0.0 与 §1.1）：上游模型名很乱，且本层已改为透传 |
| `status` | string | 见 §5 状态机 |
| `error` | object\|null | 成功为 `null`；失败时 `{code, message}` |
| `content.video_url` | string | 产物 MP4 URL（**24h 后清理**） |
| `content.last_frame_url` | string | 尾帧 PNG URL（`return_last_frame:true` 时返回，24h 有效、无水印、与视频同尺寸） |
| `content.file_url` | string\|null | 非 mp4 容器（如 flv）时使用 |
| `created_at` / `updated_at` | integer | **epoch 秒**（非 ISO 字符串） |
| `seed` | integer | 本次实际使用的种子 |
| `resolution` | string | 实际产出分辨率 |
| `ratio` | string | 实际产出宽高比 |
| `duration` | integer | 秒；与 `frames` **只会返回一个** |
| `frames` | integer | 帧数；指定了 `frames` 时返回 |
| `framespersecond` | integer | 帧率（如 24） |
| `generate_audio` | boolean | 仅 1.5 pro 返回 |
| `draft` | boolean | 是否样片；仅 1.5 pro 返回 |
| `draft_task_id` | string | 基于样片生成正式视频时返回 |
| `service_tier` / `execution_expires_after` | — | 回显 |
| `output_format` | string | 回显（如 `mov`） |
| `usage.completion_tokens` | integer | 输出 token 数 |
| `usage.total_tokens` | integer | = `completion_tokens`（视频模型不统计输入 token） |

> ⚠️ 同一字段在不同响应里大小写不一致：`framespersecond`（小写）与 `framesPerSecond` 都出现过，解析需容错。

### 4.2 查询示例响应 `[官方]`

```json
{
  "id": "cgt-20251119202422-jcfm2",
  "model": "doubao-seedance-1-0-pro-250528",
  "status": "succeeded",
  "content": {
    "video_url": "https://ark-content-generation-cn-beijing.tos-cn-beijing.volces.com/xxx.mp4",
    "last_frame_url": "https://ark-content-generation-cn-beijing.tos-cn-beijing.volces.com/xxx.png",
    "file_url": null
  },
  "usage": { "completion_tokens": 295800 },
  "frames": 145,
  "framespersecond": 24,
  "created_at": 1763555062,
  "updated_at": 1763555155,
  "seed": 655,
  "ratio": "9:16",
  "resolution": "1080p"
}
```

### 4.3 本服务的取值保证（§4.1 之外，实现侧的四条）

| 保证 | 内容 |
| --- | --- |
| **键集恒定** | `GET` 恒返回 §4.1 的字段集（**不含 `model`**）：未知的给 `null`，不因状态变化而增删键。`content` 恒有 `video_url` / `last_frame_url` / `file_url` 三键 |
| `content.video_url` | **仅 `succeeded` 时有值**，其余状态为 `null`（未成功就交地址等于撒谎） |
| `usage` | 只含 `completion_tokens` / `total_tokens`。上游的积分字段**不进响应体**（计费归属控制面，见 `ADR-010`）；原始积分与折算倍率在 logfire 的 `task.usage.*` |
| `status` | **只会是 §5 的六个值之一**。上游给出越界值时**收敛为 `running`**（非终态），并在 logfire 记 `task.status.unnormalized=true`。收敛方向刻意选"还在跑"—— 把不认识的状态当终态会触发落库、释放并发槽位与回调推送 |
| `resolution` | 归一成原生写法（`720` / `"720P"` → `720p`），与上游的大小写差异解耦 |
| `created_at` / `updated_at` | epoch 秒；`created_at` 是本层受理时刻（不是上游的），用于 7 天窗口与列表排序 |

**回显语义分两类**（这一条最容易误读）：

| 类别 | 字段 | 取谁 |
| --- | --- | --- |
| 本层原样带过去的调优项 | `service_tier` / `execution_expires_after` / `priority` | **请求值**（缺失给原生默认：`default` / `172800` / `0`） |
| 描述产物的规格 | `resolution` / `ratio` / `duration` / `frames` / `framespersecond` / `seed` / `generate_audio` / `draft` | **实际生效值** |

⇒ 上游不支持某项能力时（如"生成有声视频"），`generate_audio` 报 `false` 而**不是**回显请求里的 `true`
—— 回显请求值会是一个**假承诺**。想知道"为什么是 false"，读 logfire 的 `task.unsupported` 或 `dry-run`。

---

## 5. 任务状态机 `[官方]`

```
                 ┌──────────┐
                 │  queued  │ ← 创建后初始态
                 └────┬─────┘
        DELETE（唯一可取消的态）│
                 ┌────▼─────┐        ┌───────────┐
                 │ running  │───────▶│ cancelled │ (24h 后自动删除)
                 └────┬─────┘        └───────────┘
        ┌─────────────┼─────────────┐
   ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
   │succeeded│   │ failed  │   │ expired │
   └─────────┘   └─────────┘   └─────────┘
```

| 状态 | 语义 | 回调是否推送 |
| --- | --- | --- |
| `queued` | 排队中 | ✅ |
| `running` | 运行中 | ✅ |
| `succeeded` | 成功 | ✅ |
| `failed` | 失败 | ✅ |
| `expired` | 超时（`queued`/`running` 超过 `execution_expires_after`） | ✅ |
| `cancelled` | 取消（仅 `queued` 可被取消） | ❌（主动 DELETE 不回调） |

---

## 6. 回调 `callbask_url` 语义 `[官方]`

- 触发：任务**状态实际变更**时，方舟向 `callback_url` 发 `POST`。
- **请求体结构与查询任务 API 的响应体完全一致**（实现时可复用同一个序列化器）。
- 推送状态集合：`queued` / `running` / `succeeded` / `failed` / `expired`。
- 可靠性：**5 秒内未收到成功发送的确认，会重试，最多 3 次**（`succeeded` / `failed` 明确写了此语义）。
- 业务方应按 `id + status` 做幂等处理。

---

## 7. 模型能力矩阵

### 7.1 模型 ID `[官方]`

| 系列 | 模型 ID |
| --- | --- |
| Seedance 2.5 | `doubao-seedance-2-5-260628` |
| Seedance 2.0 | `doubao-seedance-2-0-260128` |
| Seedance 2.0 fast | `doubao-seedance-2-0-fast-260128` |
| Seedance 2.0 mini | `doubao-seedance-2-0-mini-260615` |
| Seedance 1.5 pro | `doubao-seedance-1-5-pro-251215` |
| Seedance 1.0 pro | `doubao-seedance-1-0-pro-250528` |
| Seedance 1.0 pro fast | `doubao-seedance-1-0-pro-fast-251015` |

### 7.2 能力差异 `[官方]+[镜像]`

| 能力 | 1.0 pro | 1.0 pro fast | 1.5 pro | 2.0 / fast / mini | 2.5 |
| --- | --- | --- | --- | --- | --- |
| 文生视频 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 首帧图生视频 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 首尾帧 | ✅ | ❌ | ✅ | ✅ | ✅ |
| 图片参考（`reference_image`，≤9 张） | ❌ | ❌ | ✅ | ✅ | ✅ |
| 视频参考 | ❌ | ❌ | ❌ | ✅（≤3，2–15s，≤50MB） | ✅ |
| 音频参考 | ❌ | ❌ | ❌ | ✅（≤3，MP3，≤15MB） | ✅ |
| 编辑视频 / 延长视频 | ❌ | ❌ | ❌ | ✅ | ✅ |
| 生成有声视频 | ❌ | ❌ | ✅ | ✅（默认 `true`） | ✅ |
| 样片模式 `draft` | ❌ | ❌ | ✅ | ❌ | ❌ |
| `duration` 范围 | 2–12 | 2–12 | 4–12 或 `-1` | 4–15 或 `-1` | 4–30 或 `-1` |
| `resolution` | 480p/720p/1080p | 480p/720p/1080p | 480p/720p/1080p | 480p/720p/1080p；`4k` 仅 2.0 标准版；fast/mini 上限 720p | 480p/720p/1080p |
| 默认 `resolution` | 1080p | 1080p | 720p | 720p | 720p |
| 离线推理 `flex` | ❌ | ❌ | ❌ | ❌（2.0 系不支持） | — |

**`duration: -1`** = 由模型在合法范围内自选整数秒（1.5 pro / 2.0 / 2.5 支持），实际时长须从查询响应的 `duration` 读回。

### 7.3 输入素材限制 `[官方]`

| 类型 | 上限 | 附加约束 |
| --- | --- | --- |
| 图片 | 9 张，单张 ≤30MB | 格式 jpeg/png/webp/bmp/tiff/gif（2.0 起加 heic/heif）；宽高比 `[0.4, 2.5]`；边长 `[300, 6000]` px |
| 视频 | 3 段，每段 ≤50MB | 2–15 秒 |
| 音频 | 3 个，每个 ≤15MB | MP3 |
| 请求体 | ≤64MB | — |

**输入路径仅三种**：公网 URL（`http/https`，**不支持需登录态或额外 Header 鉴权的地址**，临时 URL 须在任务执行期间有效）、Base64 data URI（**仅图片**，格式 `data:image/<小写格式>;base64,...`）、素材 ID（`asset://<ASSET_ID>`，需加白开通，LAS 素材库）。

### 7.4 产物与保留期 `[官方]`

| 项 | 值 |
| --- | --- |
| 产物 URL 类型 | 预签名链接 |
| 视频 URL 有效期 | **24h**（须及时转存） |
| 任务记录保留 | **7 天**（从 `created_at` 起） |
| Seedance 2.5 产物 URL | **下载次数上限 100 次** |
| `cancelled` 记录 | 24h 自动删除 |
| 输出帧率 | 固定不可调（1.x 为 24 fps） |

---

## 8. 错误码

响应结构：**HTTP 状态码 + `error.code` 双字段**。先看 HTTP 定方向，再看 `code` 精确定位。

| code | HTTP | 含义 | 处理建议 |
| --- | --- | --- | --- |
| `MissingParameter` | 400 | 缺必填参数 | 对照文档补全 |
| `InvalidParameter` | 400 | 参数非法（如 model ID 错） | 检查参数格式 |
| `InputTextSensitiveContentDetected` | 400 | 输入触发内容审核 | 修改输入（中国大陆合规） |
| `InvalidEndpoint.ClosedEndpoint` | 400 | 接入点被关闭/暂不可用 | 稍后重试 |
| `AuthenticationError` | 401 | Key 缺失/无效 | 检查鉴权凭证 |
| `ApiKey.Invalid` | 401 | API Key 不合法（LAS 侧） | 同上 |
| `AccessDenied` | 403 | 无该资源权限 | 检查开通与白名单 |
| `AccountOverdueError` | 403 | 账户欠费 | 充值 |
| `ModelNotOpen` | 404 | 模型未开通 | 控制台「开通管理」激活 |
| `InvalidEndpointOrModel.NotFound` | 404 | 模型/接入点 ID 错 | 核实 ID 与原厂完全一致 |
| `RateLimitExceeded.*`（`.EndpointRPMExceeded` / `.EndpointTPMExceeded` / `ModelAccountRpm(RateLimit)Exceeded` / `ModelAccountTpm…`） | 429 | 超 RPM/TPM 配额 | **指数退避**；扩容有效 |
| `ServerOverloaded` / `RequestBurstTooFast` | 429 | 突发流量保护 | **扩容无效**；需控制爬坡斜率（建议 token 增速 < 每 3 分钟 20%）+ 排队 + 重试 |
| `QuotaExceeded` | 429 | 免费额度耗尽 | 转付费或买资源包 |
| `ModelLoadingError` | 429 | 模型加载中 | 稍后重试 |
| `InternalServiceError` | 500 | 服务端内部错误 | 退避重试，持续则提工单 |

> ⚠️ **429 有两种截然不同的病因**：配额限流（扩容有效）与突发保护（扩容无效）。
> 适配层做重试策略时必须先看 `error.code` 区分，否则会"加大并发把故障放大"。
> 排障优先级：API Key 有效性 → 模型是否开通 → 账户余额 → 请求参数 → 限流。

**容量配额（模型侧）** `[官方]`：Seedance 1.x 在 LAS 侧最大 RPM 600、最大并发 10。方舟侧按模型/接入点分别限流，具体见「模型列表」。

> 模型开通是方舟特有门槛：**拿到 API Key ≠ 能调用任意模型**，必须先在控制台逐个开通，否则得到看起来像"模型不存在"的 404 `ModelNotOpen`。

---

## 9. 两种参数传入方式（弱校验模式）`[官方]`

Seedance 支持把参数**内联在提示词文本末尾**，以 `--[参数]` 后缀形式：

```
"小猫对着镜头打哈欠 --rs 720p --rt 16:9 --dur 5 --seed 11 --cf false --wm true"
// 全称写法：
"小猫对着镜头打哈欠 --resolution 720p --ratio 16:9 --duration 5 --seed 11 --camerafixed false --watermark true"
```

| 简称 | 全称 | 含义 |
| --- | --- | --- |
| `--rs` | `--resolution` | 分辨率 |
| `--rt` | `--ratio` | 宽高比 |
| `--dur` | `--duration` | 时长（秒） |
| `--seed` | `--seed` | 种子 |
| `--cf` | `--camerafixed` | 固定镜头 |
| `--wm` | `--watermark` | 水印 |

- 常规方式（推荐）= 参数放 request body（**强校验**，参数填错直接报错）。
- 弱校验方式 = 内联后缀，**参数写错会被忽略或触发报错**（语义不确定）。
- `omni_reference_task_type`（`auto` / `reference` / `edit` / `extend`）= 多模态参考场景的任务类型引导。

> 🔴 **适配层必须处理这个后缀**：调用方可能用弱校验方式传参（这是官方支持的形态），
> 若前门只解析结构化字段而把文本原样透传，上游会收到一段带 `--rs 720p` 的污染提示词。

---

## 10. 文档获取经验（本次实际路径）

| 路径 | 结果 |
| --- | --- |
| `console.volcengine.com/.../docs/<id>`（用户给的链接） | ❌ JS 渲染，只能拿到标题 |
| `www.volcengine.com/docs/<id>` | ⚠️ 部分页面仍是空壳（只回标题与目录链接） |
| `docs.volcengine.com/docs/<id>` | ✅ **多数正文页可拿到完整字段表 + 官方 SDK 代码 + 响应示例** |
| 联网检索厂商文档的片段 | ✅ 搜索引擎返回的页面正文片段常包含**完整参数表**，是补缺口最有效的手段 |
| 第三方兼容网关文档（网易云信 / 融云 / 302.AI / reAPI / CrazyRouter） | ⚠️ **交叉验证用**：能确认字段名与状态语义，但**存在 divergent 写法**（如 `role` 的位置），不可作为实现依据 |

**要点**：同一份逻辑 API 被多家兼容网关（网易云信 `ai.yunxinapi.com/hub/volcengine`、融云 `/llm/v1/...`、CrazyRouter `/volc/v1/...`）原样代理，说明"把任意上游适配为 Seedance 形态"是成熟套路 —— 这些网关的文档可以直接当**反向参考实现**读。
