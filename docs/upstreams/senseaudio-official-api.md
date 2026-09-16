# 上游契约：SenseAudio 视频开放接口（`api.senseaudio.cn`）

> **性质**：上游侧契约的**唯一真源**。脚本 `script_store/senseaudio/video@v1.py` 逐条实现本文件。
> **来源**：用户提供的官方文档两页（2026-09-17 取得，`WebFetch` 直取正文，两页字段表完整）：
> - 创建：`https://docs.senseaudio.cn/api-reference/endpoint/video/create`
> - 查询：`https://docs.senseaudio.cn/api-reference/endpoint/video/status`
> **与 `docs/seedance-api-reference.md` 的关系**：那份是**目标**契约（我们要长成的样子），
> 这份是**上游**契约（我们实际要打的东西）。差异见 §7，脚本负责投影。

---

## 1. 基础

| 项 | 值 |
| --- | --- |
| Base URL | `https://api.senseaudio.cn` |
| 鉴权 | `Authorization: Bearer <API_KEY>`（**标准 Bearer**） |
| Content-Type | `application/json`（文档把 GET 也标了该头，实际只有带体的 POST 需要） |
| 请求体上限 | 64 MB |
| 回调 | **文档未提供**（无 `callback_url` / webhook 参数） |

> ⇒ 渠道配置**不需要** `X-Auth-Emit`：留空即原样转发调用方的 `Authorization: Bearer …`。

---

## 2. 端点

| # | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 1 | POST | `/v1/video/create` | 创建任务 → `{"task_id": "..."}` |
| 2 | GET | `/v1/video/status` | 查询状态 / 进度 / 产物 URL。🔴 **实测不带任何参数**（文档写的 `?id=` 与事实不符，见 §2.2） |

### 2.1 没有取消端点（这是本上游最重要的范围事实）

官方文档只列了上面两个端点：**没有取消、没有删除、没有列表**。
⇒ 脚本**不声明** `cancel_request` / `cancel_response` 相位，本服务对它的 `DELETE` 请求
**响亮失败**（见 `ADR-014`）—— 不能"本地置为 `cancelled`"而让上游任务继续跑并继续计费。

### 2.2 🔴 查询接口**不接受参数**：任务身份来自 API key

文档把 `id` 标为必填 query 参数并给了 `?id=task_1234567890` 的示例；但**实测的调用形态是**
（用户 2026-09-17 提供）：

```bash
curl -X GET "https://api.senseaudio.cn/v1/video/status" \
     -H "Authorization: <token>"
```

**没有参数** ⇒ 上游回答的是"**这把钥匙当前的那个任务**"。两个直接后果，接入方必须知道：

1. **旧任务会被新任务顶掉**：同一把钥匙上一旦建了 #B，查 #A 拿到的就是 #B 的记录 ——
   所以本层会**验明记录归属**（比对返回的 `task_id`），不一致就 400 并说明原因，
   **绝不把 #B 的状态/产物写到 #A 上**（`ADR-016`）。
2. **同一把钥匙同一时间只该有一个在跑任务** ⇒ 渠道建议配 `X-Channel-Options.max_concurrency: 1`
   （引擎的并发闸门键正是 `provider:凭证指纹`，与上游这层语义同轴）。

两种绑定形态由渠道选项 `status_binding` 选择：

| 值 | 请求 | 谁保证"这是本任务" |
| --- | --- | --- |
| `credential`（**默认**，跟实测事实） | `GET /v1/video/status` | 本层校验返回记录的 `task_id` |
| `task_id`（跟文档形态） | `GET /v1/video/status?<query_id_param>=<task_id>`（默认参数名 `id`） | 上游按参数过滤，本层不校验 |

⚠️ 另外记住：响应里的 `id` 与 `task_id` 是**两个不同的值**（记录 ID vs 任务 ID）；
用来比对的必须是 `task_id`（创建时返回的那个）。

---

## 3. 创建任务 `POST /v1/video/create`

### 3.1 顶层字段 `[官方]`

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `model` | string | ✅ | — | 枚举，文档当前只列 **`doubao-seedance-2-0-260128`** |
| `content` | object[] | ✅ | — | 文本 / 图片 / 音频 / 视频，见 §3.2 |
| `duration` | int | ✅ | — | **4 ~ 15 之间的整数**（连续区间，非档位） |
| `resolution` | string | ✅ | — | `480p` / `720p` / `1080p`（**无 4k**） |
| `ratio` | string | ✅ | — | `16:9` / `4:3` / `1:1` / `3:4` / `9:16`（**无 `21:9`、无 `adaptive`**） |
| `timeout` | int | ❌ | — | 最大超时秒数，`[3600, 172800]` |
| `watermark` | bool | ❌ | **`true`** | 是否加水印 —— ⚠️ **默认加**，与 Seedance 原生默认 `false` **相反** |
| `provider_specific` | object | ❌ | — | 厂商特定参数；文档对本模型只给 `{"generate_audio": true}`，且**不认识的键会被静默忽略**。⚠️ **本层默认改发扁平 `generate_audio`**（用户 2026-09-17："火山是扁平的"）⇒ 默认**不发**这个字段，见 §7 差异 3 与 §9 未证实项 4/8 |

### 3.2 `content[]` 元素 `[官方]`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | `text` / `image` / `audio` / `video` |
| `text` | string | `type=text` 时的提示词 |
| `url` | string | ⚠️ **`type=image` 时用 `url`**（http/https 或 data URL），**不是** `image_url` 对象 |
| `role` | string | `type=image` 时为 `first_frame` / `last_frame` / `reference` |
| `audio_url` | string | `type=audio` 时的音频地址 |
| `video_url` | string | `type=video` 时的视频地址 |

🔴 **与原生 `content[]` 的三处形状差异**（脚本必须逐项改写，见 §7 差异 1）：

| | Seedance 原生 | SenseAudio |
| --- | --- | --- |
| 图片 URL 位置 | `image_url.url`（对象内） | **`url`（平铺）** |
| 图片类型名 | `image_url` | **`image`** |
| `reference` 的 role 名 | `reference_image` | **`reference`** |
| 视频 / 音频 | `video_url.url` / `audio_url.url`（对象内） | **`video_url` / `audio_url`（平铺）** |

### 3.3 内容组合：**两种模式，不可混用** `[官方]`

| 模式 | 组成 | 上限 |
| --- | --- | --- |
| **首尾帧模式** | `text`（可选，最多 1 条）+ `image`（仅 `first_frame` / `last_frame`） | **首帧必传**，尾帧可选（即最多 2 张图） |
| **参考素材模式** | `text`（可选，最多 1 条）+ `reference` 图片 + `audio` + `video` | 参考图 **≤9 张**、音频 **≤3 条**、视频 **≤3 段** |

三条硬规则（原文）：

1. **不支持同一请求中混用首尾帧与参考素材**；
2. **仅传 `audio` 不合法** —— 必须至少搭配 `image` 或 `video`；
3. `text` 最多 1 条。

### 3.4 素材格式要求 `[官方]`

| 类型 | 格式 | 时长 / 尺寸 | 大小 |
| --- | --- | --- | --- |
| 图片 | jpeg / png / webp / bmp / tiff / gif | 宽高比 `(0.4, 2.5)`；边长 `(300, 6000)` px | 单张 ≤30 MB；**大文件请勿用 Base64** |
| 音频 | wav / mp3 | 单条 `[2, 15]` s，≤3 条，总时长 ≤15 s | 单条 ≤15 MB |
| 视频 | mp4 / mov | 单条 `[2, 15]` s，≤3 段，总时长 ≤15 s；分辨率 480p/720p；宽高比 `[0.4, 2.5]`；边长 `[300, 6000]` px；总像素 `[640×640, 834×1112]`；帧率 `[24, 60]` | 单段 ≤50 MB |

### 3.5 响应 `[官方]`

```json
{ "task_id": "task_1234567890" }
```

**只有 `task_id`**（与原生 `{"id": …}` 同构，键名不同）。

---

## 4. 查询任务 `GET /v1/video/status`（**无参数**，见 §2.2）

### 4.1 响应字段 `[官方]`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | **记录 ID**（与 `task_id` 不同值） |
| `model` | string | 模型名（示例里写的是 `Seedance-2.0` 这种**展示名**） |
| `task_id` | string | 任务 ID |
| `status` | string | `pending` / `processing` / `completed` / `failed` |
| `progress` | int64 | 进度百分比 |
| `video_url` | string | 产物 URL（**`completed` 后返回**） |
| `duration` | int64 | **实际**视频时长（秒） |
| `is_new` | bool | 是否为新视频 |
| `error_message` | string | 错误信息（`failed` 时返回） |
| `created_at` | int64 | 创建时间戳（**epoch 秒**，与 Seedance 原生一致） |
| `completed_at` | int64 | 完成时间戳 |
| `prompt` | string | 提示词 |
| `resolution` | string | 分辨率（如 `720p`） |
| `ratio` | string | 宽高比（如 `16:9`） |
| `content` | array | 原样回显的输入内容 |
| `provider_specific` | object | 原样回显 |

> 🔴 **没有任何计费 / 用量字段**（无 token、无积分、无金额）⇒ 本适配层无法折算 `usage`，
> 只能如实给 `null`（见 §7 差异 5、`ADR-010`）。

### 4.2 状态机 `[官方]`

```
pending ──▶ processing ──▶ completed
                       └─▶ failed
```

| 上游 | → Seedance 六态 |
| --- | --- |
| `pending` | `queued` |
| `processing` | `running` |
| `completed` | `succeeded` |
| `failed` | `failed` |

⚠️ 上游**没有** `cancelled` / `expired`：前者因为无取消端点，后者由本层看门狗兜底
（`execution_expires_after`，见 `docs/03_引擎架构.md` §6.5）。

### 4.3 轮询建议 `[官方]`

每 5–10 秒一次；长时间 `processing` 建议重建任务。⚠️ 文档**未给出查询接口的频率限额**
（只在错误码表里出现 `429000` / `429002`）⇒ 本层沿用部署级默认（`RATE_LIMIT_QUERY_RPM=30`，
见 `docs/03_引擎架构.md` §7.1），不因为"文档没写"就贴着上限跑。

---

## 5. 错误码 `[官方]`

| HTTP | `ref_code` | 含义 |
| --- | --- | --- |
| 400 | `invalid` | 请求体解析或参数校验失败 |
| 400 | `400000` | 参数错误：ratio / resolution / duration / 链接等 |
| 400 | `400015` | **已达到最大并发数量**，请稍后再试 |
| 400 | `400001` | **已达到使用限制或余额不足** |
| 400 | `400900` | 计费账户不存在 |
| 400 | `400901` | 计费账户已被冻结 |
| 400 | `400902` | 未找到计费交易，可能已过期 |
| 429 | `429000` | 请求过于频繁，请稍后再试 |
| 429 | `429002` | 已达到使用限制，请稍后再试 |
| 500 | `500000` | 服务繁忙，请稍后再试（非法 model） |

⚠️ **文档没有给出错误响应体示例**（只有上面这张码表）⇒ 错误体的 JSON 形状
（是 `{"code":…,"message":…}` 还是 `{"error":{…}}`）**未证实**，见 §9 未证实项 2。

### 5.1 本层怎么映射（2026-09-17 起由**错误相位**完成，`ADR-015`）

非 2xx 的响应体**到不了** `*_response` 相位（引擎在 `raise_for_status` 处就抛了）。
2026-09-17 为此加了**可选错误相位** `<phase>_error`：由脚本把业务码映射成契约里已有的 code
（引擎不解释厂商业务码 —— 那是厂商知识，`ADR-015`）。

| 上游 `ref_code` | 含义 | 出口 `error.code` | HTTP |
| --- | --- | --- | --- |
| `invalid` | 请求体解析或参数校验失败 | `InvalidParameter` | 400 |
| `400000` | 参数错误（ratio / resolution / duration / 链接） | `InvalidParameter` | 400 |
| `400015` | **已达到最大并发数量**，稍后再试 | `ServerOverloaded` | 429（带 `Retry-After`） |
| `400001` | 已达到使用限制 / 余额不足 | `QuotaExceeded` | 429 |
| `400900` | 计费账户不存在 | `AccountOverdueError` | 403 |
| `400901` | 计费账户已被冻结 | `AccountOverdueError` | 403 |
| `400902` | 未找到计费交易，可能已过期 | `AccountOverdueError` | 403 |
| `429000` | 请求过于频繁 | `RateLimitExceeded.ModelAccountRpmExceeded` | 429 |
| `429002` | 已达到使用限制 | `QuotaExceeded` | 429 |
| `500000` | "服务繁忙"，同码又被注为"（非法 model）" | **刻意不映射** ⇒ 走 5xx 通用规则 | 502 `InternalServiceError` |
| 认不出的码 / 非 JSON 错误体 | — | **不拦** ⇒ 走 HTTP 状态的通用映射 | 按状态 |

三条边界（都写进了脚本的 `create_error` / `query_error`）：

1. **错误体形状未证实**（见 §9 未证实项 2）⇒ 只认 `ref_code` / `code` / `error_code`
   以及 `error{…}` 子对象里同名的这几个位置；**都认不出就不拦** —— 猜错方向会把
   "上游 500"说成"你的参数错了"；
2. `500000` 自相矛盾（"服务繁忙"却注"非法 model"），一半可重试一半不可 ⇒ **不猜**；
3. `Retry-After` **只透传事实**：上游给了用它，其次渠道 `X-Channel-Options.retry_after_seconds`，
   都没有就**不给这个头**（不编数字）；非 429 出口不挂它。

---

## 6. 计费

- 文档**没有公布任何价格或计费公式**，也没有成本预估端点。
- ⇒ 脚本 `estimate_credits` 只在渠道给了 `X-Channel-Options.credit_table`
  （`{"doubao-seedance-2-0-260128": <每秒费率>}`）时才返回数字；否则为 `None`，
  由调用方显式接受"成本不可在提交前验证"（`allow_unpriced`）才放行 ——
  与 `seedance20` 走的**同一条** `ADR-004` 路径。
- `max_credits`（渠道 `X-Channel-Options.max_credits` 或 `extra_body.senseaudio_max_credits`）
  仍是**提交前置条件**：拿不到就 400，判断在任何请求发出之前。

---

## 7. 与目标契约的差异核对（本节最贵）

差异逐条来自"官方字段表 vs `docs/seedance-api-reference.md`"。**脚本按本表投影**。

| # | 项 | Seedance 原生 | SenseAudio | 本层处理 |
| --- | --- | --- | --- | --- |
| 1 | `content[]` 形状 | `image_url.url` / `video_url.url` / `audio_url.url` 对象内，`type=image_url`，`role=reference_image` | `url` / `video_url` / `audio_url` **平铺**，`type=image`，`role=reference` | 谱照上游改写（出口仍只收原生形状，前门归一化已把 `role` 提到顶层） |
| 2 | `watermark` 默认 | `false` | **`true`** | 🔴 **必须显式发 `watermark`**：调用方没写时要发 `false`，否则会拿到一个**没人要的水印** |
| 3 | `generate_audio` | 2.x 默认 `true`；**独立顶层字段（扁平）** | 文档示例放在 `provider_specific` 里，默认未写 | **默认发顶层扁平字段**（2026-09-17 用户确认："generate_audio 火山是扁平的"）；渠道可用 `generate_audio_field=provider_specific` 切回文档形态 |
| 4 | `resolution` | 480p/720p/1080p/**4k**；2.0 默认 720p；可省略 | 480p/720p/1080p；**必填** | 缺省补原生默认 `720p`；`4k` 越界 ⇒ 钳到 `1080p` + warning |
| 5 | `ratio` | 含 `21:9` 与 `adaptive`；默认 `16:9`；可省略 | 只有 5 种，**无 `adaptive`**；必填 | 缺省补 `16:9`；`21:9` 按最接近宽高比吸附；`adaptive` 无上游语义 ⇒ 落 `16:9` + warning（与 `aivideomaker/video@v1` 同口径） |
| 6 | `duration` | 4–15 或 `-1`（模型自选） | **4–15 整数，必填，无 `-1`** | `-1` ⇒ 取原生默认 `5`（不是区间下界，理由见 §8）；越界钳制 |
| 7 | `frames` | 支持（`25+4n`，优先于 `duration`） | **无** | 换算成 `duration`（`round(frames/24)`）+ warning |
| 8 | `seed` / `camera_fixed` / `return_last_frame` / `draft` / `service_tier` / `priority` / `safety_identifier` / `tools` / `output_format` / `omni_reference_task_type` | 有 | **全部无** | 进 `unsupported[]`，不发 |
| 9 | `execution_expires_after` | `[3600, 259200]`，默认 172800 | `timeout` `[3600, 172800]`；可选 | 在区间内 ⇒ 原样发 `timeout`；高于上界 ⇒ 钳到 172800 + warning；**低于下界 ⇒ 不发** + warning（绝不为满足上游下限而延长调用方的超时） |
| 10 | 首尾帧 + 参考素材 | 可同请求混用 | **互斥** | 混用 ⇒ **400**（丢掉任何一侧都改变了"用户想要什么"） |
| 11 | `role=last_frame` 单图 | 首帧必传 | **首帧必传** | 只给尾帧 ⇒ **400**（与 `aivideomaker/video@v1` 一致） |
| 12 | 列表 / 取消 | 有（可选） | **都没有** | 不声明 cancel 相位 ⇒ `DELETE` 响亮失败（`ADR-014`）；列表是本层本地实现，与上游无关 |
| 13 | 回调 | `callback_url` | 无 | 引擎留存并自行推送（上游无 webhook，所以只能由调用方轮询触发机会式推送，§6.5 的既有语义） |
| 14 | 产物 URL 有效期 | 24h | **未文档化** | 默认透传；需要稳定地址的渠道开 `rehost`（`docs/03_引擎架构.md` §8.2） |
| 15 | `usage` | `usage.completion_tokens` | **无任何用量字段** | `usage: null`（**不编数字**）—— 下游对账只能靠 new-api 与上游账单（`ADR-010`） |

---

## 8. 两个刻意的取值决定（可被否决，但要有理由才能改）

**① `duration: -1` → `5`，不是区间下界 `4`。**
`docs/04_能力映射与降级.md` §2.3b 对"区间类"给的建议是取 `min`。这里取 `5` 是因为
`-1` 的语义是"**由模型自选**"，而原生 `duration` 的**默认值本身就是 `5`**
（`seedance-api-reference.md` §3.1）—— 用模型自己的默认值承接"你自选"，
比用区间端点更接近原意（端点纯属本层挑的数字）。两者都进 warning。

**② `watermark` 与 `generate_audio` 一律显式发送，哪怕调用方没写。**
这两个字段的**两侧默认值不一致或未文档化**（`watermark`：原生 `false` / 上游 `true`；
`generate_audio`：原生 2.x `true` / 上游未写）。省略任何一个都等于把"实际生效值"
交给上游的默认值决定 —— 那是**静默**改变产物，而响应体已不再是降级告知通道
（`ADR-011`）。显式发送的成本是一次字段占用。

---

## 9. 未证实项（**别当成已验证的事实**）

| # | 待证实 | 现状 | 影响 |
| --- | --- | --- | --- |
| ~~1~~ | ~~查询参数 `id` 到底吃 `task_id` 还是记录 `id`~~ | **已定（2026-09-17 用户实测）：该接口根本没有参数** —— 身份来自 API key，见 §2.2 | 默认 `status_binding=credential` + 验明记录归属（`ADR-016`）；文档那个 `?id=` 形态留成开关 |
| 2 | **错误响应体的 JSON 形状** | 文档只有码表，**没有响应体示例**。脚本按"多认几个位置"处理（`ref_code` / `code` / `error_code` / `error{}`） | 若实际形状不在其中，业务码映射会**静默退化为不拦**（落回 HTTP 状态的通用映射）。⚠️ 观测点：出口码是否仍是 `InvalidParameter` |
| ~~3~~ | ~~`400015` / `400001` 等非参数类错误的出口语义~~ | **已修（2026-09-17）**：新增错误相位，映射表见 §5.1（`ADR-015`） | 已不再把"上游忙 / 账户欠费"读成"请求写错" |
| 4 | `provider_specific.generate_audio=false` 是否被接受 | 文档只给了 `true` 的示例，并明确"不支持的字段会被忽略"。**现在默认根本不发嵌套形态**（发扁平，见 §7 差异 3） | 若上游**只认**嵌套形态，扁平会被忽略 ⇒ 拿到无声产物（观测点：产物是否带音轨）。应急开关：`generate_audio_field=provider_specific` |
| 5 | 产物 URL 的有效期与是否需鉴权 | 未文档化 | 决定 `rehost` 是否需要默认打开（现仍默认关） |
| 6 | 查询接口的频率限额 | 未文档化 | 沿用部署默认 30 rpm；若上游实际更严，会表现为 429（本层有全局冷却兜底） |
| 7 | `is_new` / `progress` 的语义边界 | 字段表只给了字面描述 | 不进对外响应（原生字段集里没有它们），只留在 logfire 明细里 |
| 8 | 扁平 `generate_audio` 与嵌套 `provider_specific` 哪个是真形态 | 用户口述"火山是扁平的"（与官方示例相反），**未实测** | 见 4：两个方向都表现为"静默"，所以给了渠道开关 + `effective.generate_audio_field` 可查 |
| 9 | **同一把钥匙的并发上限**（`400015` 说的那个"最大并发数量"到底是几） | 未文档化。但查询接口只能回答"当前那一个任务" ⇒ **在跑任务 >1 时，其余任务的状态读不到** | 渠道配 `max_concurrency: 1` 可完全规避；不配就可能出现"旧任务查询 400"（本层会明确说出来，不静默） |
| 10 | 被顶掉的旧任务的**最终归宿** | 未文档化（会不会被上游清理、多久） | 本层对它的查询会 400；本地记录活到 `execution_expires_after`（默认 48h）由看门狗置 `expired` |

---

## 10. 抓取记录（下次别重走弯路）

两页文档都是**可被 `WebFetch` 直取正文的静态文档站**（`docs.<domain>`，非 SPA），
一次抓取即拿到完整字段表 + 枚举 + 错误码表 —— 与火山方舟（SPA 外壳、只能靠搜索片段）
完全不同。⇒ 后续同类上游优先 `docs.<域名>`，不要先试控制台页面。
