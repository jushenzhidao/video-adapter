# 上游契约：aivideomaker 官方 API 线

> **性质**：上游侧契约的**唯一真源**。脚本 `script_store/aivideomaker/video@v1.py` 逐条实现本文件。
> **来源**：用户提供的官方 API 文档（2026-09-13 提供）。
> **与 `docs/seedance-api-reference.md` 的关系**：那份是**目标**契约（我们要长成的样子），
> 这份是**上游**契约（我们实际要打的东西）。两者形状不同 —— 本文件记录差异，脚本负责投影。

---

## 1. 基础

| 项 | 值 |
| --- | --- |
| Base URL | `https://aivideomaker.ai` |
| 鉴权 | 请求头 `key: <API_KEY>`（**裸 key，无 `Bearer` 前缀**） |
| Content-Type | `application/json`（**仅**带 JSON 体的 POST 需要） |
| 回调 | 请求头 `webhookUrl`（可选，任务状态通知） |

> ⇒ 渠道配置里 `X-Auth-Emit` 需为 `header:key:` 形式，**不能**用默认的 `Authorization: Bearer`。

---

## 2. 端点

| # | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 1 | POST | `/api/v1/generate/{model}` | 创建任务，返回 `taskId` + 三个链接 |
| 2 | GET | `/api/v1/tasks` | 列出当前 Key 下全部任务（创建时间倒序） |
| 3 | GET | `/api/v1/tasks/{taskId}` | 任务详情（含 `input` / `output` / `status` / `completedAt`） |
| 4 | GET | `/api/v1/tasks/{taskId}/status` | **仅**返回 `{"status": "..."}`，适合轮询 |
| 5 | PUT | `/api/v1/tasks/{taskId}/cancel` | 取消任务，**仅在 `SUBMITTED` 状态有效** |

⚠️ **取消是 `PUT`**（POST / DELETE 均 405）。

**创建成功响应**

```json
{
  "status": "SUBMITTED",
  "taskId": "ckxxxxxxxx",
  "responseUrl": "https://aivideomaker.ai/api/v1/tasks/ckxxxxxxxx",
  "statusUrl":   "https://aivideomaker.ai/api/v1/tasks/ckxxxxxxxx/status",
  "cancelUrl":   "https://aivideomaker.ai/api/v1/tasks/ckxxxxxxxx/cancel"
}
```

⚠️ 三个链接是**上游自己给的绝对地址**，但**不要照用** —— 查询/取消的地址应由渠道的
`X-Upstream-Url` 决定（否则渠道指向测试环境时会被响应里的生产地址带跑）。

🔴 **任务是按 API Key 归属的**：`GET /api/v1/tasks` 只返回**当前 Key 名下**的任务，
查询与取消也必须用**创建时那把 Key**。

⇒ 适配层必须记住 **`Key ⇄ task_id`** 的绑定关系。注意"记住"不等于"存下来"：
存的是**创建时那把 Key 的指纹**（`credential_id`，HMAC-SHA256 + 服务端 secret），
**永不落盘明文 Key**；查询/取消时指纹不符 ⇒ **本地直接 404**，不带着错的钥匙去问上游。
详见 `03_引擎架构.md` §6.3(b)。

> 换一把 Key 查旧任务会得到上游 404 —— 这是上游的**归属语义**，不是缺陷；
> 也不要为了"让旧任务还能查"而把 Key 快照进库（凭证落盘 + 轮换后同样失效）。

**错误响应**（注意：**HTTP 状态码未在文档中给出**，只给了信封）

```json
{ "status": "FAILED", "message": "Insufficient credits" }
```

> ⇒ 脚本只能从 `status == "FAILED"` + `message` 判定失败，
> **不能依赖 HTTP 状态码做分类**（见 §7 的差异核对）。

---

## 3. 任务状态

| 值 | 含义 | 映射到 Seedance |
| --- | --- | --- |
| `SUBMITTED` | 已提交，等待处理 | `queued` |
| `PROGRESS` | 处理中 | `running` |
| `COMPLETED` | 成功完成 | `succeeded` |
| `FAILED` | 失败（**积分自动退还**） | `failed` |
| `CANCEL` | 已取消 | `cancelled` |

⇒ 上游**没有 `expired`**，超时须由引擎看门狗兜底（`03_引擎架构.md` §6.5）。

---

## 4. 各模型参数与计费

`{model}` 路径参数的可选值共 8 个：
`t2v` / `i2v` / `minimax` / `t2v_v3` / `i2v_v3` / `seedance20` / `wan27` / `happyhorse`

### 4.1 `t2v` — 文生视频

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `prompt` | string | 是 | — |
| `aspectRatio` | string | 是 | `16:9` / `9:16` / `1:1` |
| `duration` | **string** | 是 | `"5"` 或 `"8"` |

计费：`duration × 3` 积分

### 4.2 `i2v` — 图生视频

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `image` | string | 是 | 公网 URL 或 `data:image/...;base64,...` |
| `prompt` | string \| null | 否 | — |
| `duration` | **string** | 是 | `"5"` 或 `"8"` |

计费：`duration × 3` 积分

### 4.3 `t2v_v3` / `i2v_v3`

同 4.1 / 4.2，但 `duration` 为 **`5` / `10` / `15` / `20`**，且
`aspectRatio` 为 `16:9` / `9:16` / `1:1`。

计费：`duration × 4` 积分

### 4.4 `minimax` — MiniMax H3

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `content` | string | 是 | 文本提示词（**字段名是 `content`，不是 `prompt`**） |
| `imageUrl` | string \| null | 否 | 首帧 |
| `lastFrameUrl` | string \| null | 否 | 尾帧 |
| `referenceImageUrls` | string[] | 否 | 参考图，**最多 4 张** |
| `referenceVideoUrl` | string \| null | 否 | 参考视频 |
| `referenceAudioUrls` | string[] | 否 | 参考音频，**最多 2 个** |
| `aspectRatio` | string | 否 | `auto` / `21:9` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16`（默认 `16:9`） |
| `duration` | **number** | 否 | 5–20 秒（默认 5） |
| `resolution` | string | 否 | `720p` / `1080p`（默认 `720p`；**不支持 480p**） |
| `tier` | string | 否 | `turbo` / `base`（默认 `turbo`） |

🔴 **首尾帧输入与参考素材不能同时使用。**
计费：`duration × (720p→3 / 1080p→4) + duration × 1（当 tier=base）`

### 4.5 `seedance20` — Seedance 2.0

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `prompt` | string \| null | 否* | — |
| `image` | string \| null | 否* | **单张**参考图 |
| `video` | string \| null | 否* | **单个**参考视频 |
| `audio` | string \| null | 否* | **单个**参考音频（**不能单独使用**） |
| `duration` | **number** | 是 | **4–15 秒** |
| `resolution` | **number** | 是 | `480` 或 `720` |
| `ratio` | string | 是 | `16:9` / `9:16` / `1:1`（**无 `adaptive`**） |

\* `prompt`、`image`、`video` **至少需要一个**；`audio` 只能与其中一种输入搭配。

计费：**由服务端价格计算器按分辨率/比例/时长动态计算，文档不提供固定每秒费率。**
⇒ 本地**无法预估**（见 §7 差异 3）。

### 4.6 `wan27` — Wan 2.7

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `prompt` | string | 是 | — |
| `image` | string \| null | 否 | 传了即图生视频，否则文生视频 |
| `duration` | **string** | 是 | `"5"` / `"10"` / `"15"` |
| `resolution` | string | 是 | `720P` / `1080P` |
| `ratio` | string | 是 | `16:9` / `9:16` / `1:1` / `4:3` / `3:4`（**无 `21:9`、无 `adaptive`**） |
| `promptExtend` | boolean | 否 | 提示词增强（默认 false） |

计费：`duration × 10`（720P）/ `duration × 15`（1080P）

### 4.7 `happyhorse` — HappyHorse 1.1

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `prompt` | string | 是 | — |
| `image` | string \| string[] \| null | 否 | 单张 = i2v；**多张 = r2v**；null = t2v |
| `duration` | **number** | 是 | **3–15 秒** |
| `resolution` | string | 是 | `720P` / `1080P` |
| `ratio` | string \| null | 否 | `16:9` / `9:16` / `3:4` / `4:3` / `1:1`（默认 `16:9`；**i2v 模式下不使用**） |

计费：`duration × 25`（720P）/ `duration × 50`（1080P）

---

## 5. 字段类型差异速查（`INVALID_PAYLOAD` 的首要成因）

同一个语义字段，8 个模型有 4 种写法：

| 模型 | `duration` 类型 | 合法档位 | `resolution` | 比例字段名 |
| --- | --- | --- | --- | --- |
| `t2v` / `i2v` | string | `"5"` `"8"` | — | `aspectRatio` |
| `t2v_v3` / `i2v_v3` | string | `"5"` `"10"` `"15"` `"20"` | — | `aspectRatio` |
| `minimax` | number | 5–20 | `720p` / `1080p` | `aspectRatio` |
| `seedance20` | number | 4–15 | **number** `480` / `720` | `ratio` |
| `wan27` | string | `"5"` `"10"` `"15"` | `720P` / `1080P` | `ratio` |
| `happyhorse` | number | 3–15 | `720P` / `1080P` | `ratio` |

---

## 6. 限流

任务查询接口**按 IP 限制 60 次/分钟**；超限返回 **HTTP 429**，响应头带 `Retry-After`。

> ⇒ 引擎的退避策略必须**读 `Retry-After`**，而不是只做指数退避。

---

## 7. 与既有假设的差异核对（本次最重要的一节）

旧实现 `videos/src/ark_compat/translate.py` 是针对这条线的**网页线**观测写的，
其中若干处对官方线是**猜测**（源码里明确标了 "unverified"）。本文件给出官方文档后，
以下 6 处需要修正：

| # | 旧实现 | 官方文档 | 处理 |
| --- | --- | --- | --- |
| 1 | 全局一张 `DURATION_ALLOWED` 档位表（480p→5/10/15/20、720p→5..20、1080p→5/10），注释写"官方 seedance20 真实档位未验证" | **每个模型各自的档位**：t2v/i2v 只有 5/8；\_v3 是 5/10/15/20；seedance20 是 4–15 **连续**；minimax 5–20；wan27 5/10/15；happyhorse 3–15 | 改成**按模型的档位表**；seedance20/happyhorse/minimax 是**区间**而非离散档位 ⇒ 不再需要"就近吸附"，直接钳制到区间（消除跨档风险） |
| 2 | `seedance20` 的 `lastFrameImage`、`referenceImages`、`referenceVideoUrl`/`referenceAudioUrls` 字段**"未验证"**，仍照发 + 告警 | 官方字段是 `image` / `video` / `audio`，各**单个**；**没有** `lastFrameImage`、没有 `referenceImages` 数组 | 用真字段名；尾帧 → 降级（取首帧 + warning）；参考图 >1 张 → **400**（参考类不能静默丢） |
| 3 | `X-Max-Credits` 被当作上游的计费前闸门，`BUDGET_EXCEEDED` 422 | 本文档**只列了 `key` / `Content-Type` / `webhookUrl` 三个请求头**，没有 `X-Max-Credits`，也没有 422 | ✅ **已确认（2026-09-13 用户）**：上游**确实没有计费前闸门** ⇒ 支出上限只能由适配层**本地估算**强制。7 个模型有官方公式；`seedance20` 动态计价 ⇒ 必须由渠道给 `credit_table`、或显式 `allow_unpriced` 才放行。见 `docs/decisions/ADR-004` |
| 4 | 未知模型名**默认落 `seedance20`** | 文档给出 8 个合法值 | 改为 **400 且带可用清单**（`03_引擎架构.md` §2.3：未知 model 绝不落默认上游 —— 那正是"误路由 → 直接付费"的入口） |
| 5 | `adaptive` 直接丢弃（`ratio` 不发） | `seedance20` **`ratio` 是必填**；`minimax` 的 `aspectRatio` **显式支持 `auto`** | minimax：`adaptive → auto` 原样发；seedance20：`adaptive` 无法满足 ⇒ 明确告警并落到具体比例；其余模型按允许集**就近吸附 + warning** |
| 6 | `happyhorse` / `wan27` 的 `duration` 发成 string | 二者都是 **number**（happyhorse 3–15、wan27 5/10/15） | 按文档改；`wan27` 的 `duration` 是 **string**，`happyhorse` 是 **number** |

另需注意（旧实现未覆盖）：

- **`promptExtend`**（wan27）与 **`image` 数组**（happyhorse r2v）此前未被支持，现已纳入；
- **429 + `Retry-After`** 的限流语义（§6）；
- **取消只对 `SUBMITTED` 有效**（旧实现未做状态校验）。
