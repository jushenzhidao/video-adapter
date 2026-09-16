# ADR-011: 对调用方的响应体**只含原生字段**，诊断全部改道 logfire

## Status
Accepted (2026-09-16)。**用户决定：「客户端请求严格遵守我以上说的」「响应里 model 字段可以删除，
上游模型名字比较乱，类似情况的干脆删掉」「logfire 上报越详细越好」。**

## Background

2026-09-16 的实测报告（`reports/2026-09-16_seedance-protocol-adapter/REPORT.md`）里有一句
**错的结论**：

> "创建响应里没有 `status` —— 与原生契约一致（创建只回 id + 上报块）"

**原生 `POST` 的成功体只有 `{"id": …}`，没有上报块**（`seedance-api-reference.md` §3.3）。
也就是说，本层此前在**每一个**响应里额外塞了这些键：

| 键 | 性质 |
| --- | --- |
| `provider` / `upstream_task_id` / `script_ref` / `script_sha256` / `upstream_report` / `upstream` | 本服务的内部诊断 |
| `requested` / `effective` / `warnings[]` / `unsupported[]` | 降级与生效值诊断（`ADR-009` 曾要求它们在响应体里） |
| `model` | 上游模型名回显 |

### 为什么"多几个键"确实是问题（而不是洁癖）

1. **按原生契约写严格校验的接入方会失败**：schema 校验、SDK 反序列化（`model_validate` 一类
   默认 `extra="forbid"`）、契约测试 —— 它们把"响应变宽"当**破坏性变更**。
   "加字段通常兼容"的直觉只在**宽松解析**的接入方上成立。
2. **`model` 回显是**误导源**：本层把调用方写的名字原样回显（`doubao-seedance-2-0-260128`），
   而实际跑到上游的是另一个槽位 —— 于是"看 `model` 字段"的接入方**看不出被替换**。
   `ADR-012` 已把模型名改为透传，回显它对调用方更是零信息增量。
3. **两处表示同一事实 = 迟早不一致**：`warnings[]`（散文）与 `effective`（结构化）并存，
   就会出现"改了 A 忘了 B"。收敛成一个出口反而更可靠。

## Decision

### D1. 响应形状的唯一实现点是 `adapter/seedance.py`

| 端点 | 响应 |
| --- | --- |
| `POST` | `{"id": …}` —— **逐键只有 id** |
| `GET {id}` | `NATIVE_TASK_KEYS` 列出的原生字段集（**不含 `model`**） |
| `GET`（列表） | `{items, total, page_num, page_size}`，item 与 `GET {id}` **同形**（同一个 `render_task`） |
| 回调推送 | 与 `GET {id}` 同形（原生语义：推送体 = 查询响应体） |

创建、查询、列表、回调**共用同一个渲染函数**：形状散落在多个出口时，必然出现"某个出口忘了跟着改"。

### D2. 诊断一律改道 logfire，且**必须更详细**

被移出响应体的每一项都要在 `task.snapshot` span 上查得到：

    task.upstream_id / task.provider / task.credential_id / task.script.{ref,sha256}
    task.model / task.model.bare / task.model.upstream
    task.status{,.previous,.changed,.history,.changes,.unnormalized}
    task.requested.* / task.effective.*（含 model_requested / model_map_applied / estimated_credits）
    task.warnings[] / task.unsupported[] / task.usage.* / task.artifacts.* / task.rehost
    task.query.{count,last_at,cache_hit,served}
    正文类：task.report.request / task.report.response（**只有创建那条 span 带全文**）/
             task.upstream.raw（本次查询的上游原文）；
             查询侧只带 task.report.request.method / .url / task.report.full_on_span

⇒ **纪律的形态变了**：从"响应体要如实"变成"**上报要如实且更细**"。
改这条链路时的自检问句是"**这个事实在 logfire 里还查得到吗**"。

### D3. `status` 收敛到原生六态，越界值落 `running`

`queued / running / succeeded / failed / expired / cancelled`。
上游给出越界值时**收敛为 `running`**（非终态）并上报 `task.status.unnormalized=true`。
方向刻意选"还在跑"：把不认识的状态当**终态**会触发落库、**释放并发槽位**与**推送回调** ——
那是把"上游改了词表"变成"我们提前放行了闸门"。

### D4. 原生回显分两类

- **本层原样带过去的调优项**（`service_tier` / `execution_expires_after` / `priority`）→ 按**请求值**回显；
- **描述产物的规格**（`resolution` / `ratio` / `duration` / `frames` / `framespersecond` /
  `seed` / `generate_audio` / `draft`）→ 按**实际生效值**回显。

🔴 第二类取"实际生效"而不是"请求值"：上游不支持"生成有声视频"时若回显 `generate_audio: true`，
那是一个**假承诺**。
⚠️ 代价：`generate_audio: false` 长得像"你请求的 false"，而真实原因是"上游没有这个能力" ——
这个原因只在 logfire 的 `task.unsupported` 或 `dry-run` 里。

### D5. 保留 `dry-run` 作为刻意的非原生出口

`X-Dry-Run: 1` 时返回 `requested` / `effective` / `warnings[]` / `unsupported[]`
（`ADR-010` D5 的既定形态，**不变**）。它不是原生契约的一部分，而是"花钱前自检"的开关 ——
把降级事实彻底抹掉会让接入方**无法在花钱前**发现参数被改。

## Consequences

- **正面**：接入方可以按原生契约做严格校验 / 用官方 SDK 的反序列化；响应体不再有"实现细节"。
- **正面**：形状有了唯一实现点与 14 项逐键断言（`tests/test_seedance_contract.py`），
  "顺手加个诊断字段"这类改动会在 CI 当场变红。
- **正面**：上报项一次性补齐（含**有界**的状态变更历史 `task.status.history`），
  顺手关掉了报告 F8 的"中间态被逐次覆盖 ⇒ 看不到状态推进"。
- **负面（已接受）**：**响应体不再是降级告知通道**。接入方若既不用 `dry-run`、也不看 logfire，
  就**感知不到**自己是否被降级（参数被吸附/丢弃、模型被映射）。`ADR-009` D7 的原始要求因此作废。
- **负面**：`upstream_task_id` 对调用方不可见 ⇒ 它成为**服务端凭据**（与上游对工单靠 logfire
  或上游侧的 Key 归属）。E2E 里改为从**上游侧**取（`/__control/state`）。
- **负面**：原本"顺带"提供的能力（例如从响应读 `usage.credits` 做对账）需要改读 logfire 的
  `task.usage.*`。⚠️ 对账方向的提醒：`ADR-010` 已定计费归属 new-api，
  对账本就应以**两侧真实数据**为准，不该依赖本层响应体。
- **风险**：如果将来"再顺手加一个字段"的诱惑出现，请先看本 ADR 的第一条正面理由
  —— 严格校验的接入方会把那当成破坏性变更。
