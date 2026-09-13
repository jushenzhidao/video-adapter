# ADR-006: 上报保真 —— 上游 request/response 原文（含上游 task id），凭证除外

## Status
Accepted (2026-09-13)。**用户指令：上报信息要更详细，包括上游 task id；
request 与 response 不需要考虑脱敏；线上线下一起校验。**

## Background
适配层的失败几乎都长在"上游到底收到了什么 / 回了什么"上：
字段名猜错、`duration` 被发成字符串、位置报错被折成 400、参考素材被静默丢…
把上报折叠成"状态码 + 一句 message"，等于在需要证据的时刻把证据丢掉。

此前 §12 只写了"别泄凭证"（`inspect_arguments=False`、密钥不入 trace），
**没有写"该上报什么"**，于是接线时最容易被做成"只上报结果字段"。
另有一处反向风险：logfire 的默认脱敏器会把命中的**叶子值整条替换**，
一句含 `cookie` / `secret` / `session` 的提示词会被换成 `[Scrubbed due to 'cookie']`
—— **"上报原文"会被第三方默认行为静默作废**（5.1.0 实测）。

## Decision
1. **每次上游调用一条 `upstream.call`，带 request 与 response 原文**（含 body），
   字段表见 `docs/03_引擎架构.md` §12.2。正文可用 `OBS_REPORT_BODIES=false` 关掉，
   关掉即**不出现**（不写空占位）。
2. **上游 task id 必须显式在场**（`task.upstream_id`）：创建时在唯一"刚学到它"的地方补记，
   查询/取消/上游调用从任务记录带。它是与上游对工单的唯一凭据。
3. **没有应答就不写状态码**：连接失败 / 超时时不写 `upstream.response.status` ——
   伪造 0/502 会让"从没收到应答"与"上游回了 5xx"在上报里长得一样。
4. **凭证除外，且是三道**：① 源头打码（名字表 + **值里含渠道凭证的任意头**，
   后者不依赖名字表，因为 `X-Auth-Emit` 允许渠道自选头名）；② logfire 脱敏回调兜底，
   只遮凭证类、其余**放行**；③ `LOGFIRE_CAPTURE_HEADERS=true` **拒绝装配**。
   打码只留长度（低熵 key 的前缀等于半个答案、裸哈希可离线爆破 —— 同 ADR-003）。
5. **验收分两层**：离线（`tests/test_observability.py`，零网络，断言上报内容）
   ＋ 线上（`scripts/logfire_online_probe.py`，真外发一条，验通路）。

## Consequences
- **正面**：排障从"猜"变成"读"—— 上游收到的字节与回来的字节都在同一条 trace 上，
  且带着上游 task id 可以直接找上游对工单。
- **正面**：上报**内容**（离线）与上报**通路**（线上）分开验收，
  不会出现"字段对但发不出去"或"发得出去但字段被脱敏器吃掉"这类单边绿灯。
- **负面**：上报体量变大（body 可能含 base64 素材）⇒ 逐字符串截断
  `OBS_BODY_MAX_CHARS`（默认 20k）并**显式标注**截掉多少，不静默丢。
- **负面**：正文进第三方 SaaS 是**有意的取舍**（用户确认"不需要考虑脱敏"）。
  代价与边界写死在 §12.3：凭证一律不入，`completion_tokens` 这类**用量计数不能遮**
  （遮了等于静默删掉对账字段）。
- **负面（已实测的坑）**：`logfire.configure()` **不复用**上一次的 `scrubbing=` ——
  谁在装配之后再 configure 一次，就必须带上 `observability.scrubbing_options()`，
  否则默认脱敏回来、正文被整条替换而**没有任何报错**。
  这条写进 §12.4 与 `scrubbing_options()` 的 docstring，因为它只会在排障时才发现。
- **边界**：§12 的字段表是**契约**，改名 = 破坏调用方的仪表盘与告警；
  改字段表要同时改测试与本文档。

## Related ADRs
ADR-003（凭证绑定：存指纹不落明文）、ADR-004（本地支出上限）、
并取代 `image-adapter` 那种"只上报折叠结果"的立场（那两个项目的上游语义不同）。
