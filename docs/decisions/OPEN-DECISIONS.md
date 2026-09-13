# OPEN-DECISIONS：悬而未决登记册

> 规则：**只追加 + 就地关闭**（OPEN → RESOLVED，补 Resolution 字段）。
> 每次开工先把本表复现到上下文最前面，逐条判断能否关闭。
> 已关闭项在适当时升格为 `ADR-XXX.md`。
>
> 三类 slug：`waiting-on-external-condition` / `design-decision-to-evaluate` / `existing-design-boundary`

**汇总：3 已决 · 8 未决**

| Date | Source | Open Item | Related Constraints | Current Leaning | Blocked By | Resolves When | Status |
|------|--------|-----------|---------------------|-----------------|------------|---------------|--------|
| 2026-09-13 | 上游文档 §7 差异 3 | **上游有无计费前闸门** | 上游只列 `key`/`Content-Type`/`webhookUrl` 三个头；越界信封是 `{"status":"FAILED","message":"Insufficient credits"}` | 无闸门 ⇒ 上限只能由本层本地估算强制 | — | — | **RESOLVED**（2026-09-13 用户确认：**上游确实没有计费前闸门**）。Resolution：支出上限完全由本地强制 —— 拿不到 `max_credits` → 400；本地估算 > 上限 → 400；**动态计价模型（`seedance20`）无法估算 ⇒ 必须显式 opt-in 才允许提交**（`X-Channel-Options.allow_unpriced` 或 `extra_body.aivideomaker_allow_unpriced`），否则 400。渠道可用 `credit_table` 给动态模型补费率，从而恢复可估。见 `ADR-004` |
| 2026-09-13 | §6.3b | **凭证指纹密钥的轮换策略** | 指纹 = `HMAC-SHA256(secret, key)`，存在任务记录里；**换 secret 会让所有存量任务读不出**（算出的指纹与记录不符 → 404），最长影响 7 天 | 目前只支持单 secret。倾向：支持**双密钥验证窗口**（`SECRET` + `SECRET_PREVIOUS`），新任务用新密钥，查询时两把都试 | 需要一个"轮换窗口 ≥ 任务保留期"的运维约定 | 决定是否实现双密钥（或接受"轮换即放弃存量任务"） | OPEN · `design-decision-to-evaluate` |
| 2026-09-13 | §6.2 | **生产任务后端选型** | 契约要求 `GET` 在 7 天窗口可用；multi-instance 下必须共享存储 | sqlite 单实例即可，已验证重启不丢；多实例必须 redis | 等部署拓扑（单实例还是多副本） | 部署方案确定后 | OPEN · `waiting-on-external-condition` |
| 2026-09-13 | §2.5 | **DELETE 的响应形状** | 上游文档未规定；Seedance 契约只规定语义（`queued` 取消 / 终态删除） | 现在：`queued` → 返回任务对象（status=cancelled）；终态 → `{"id":…,"deleted":true}` | 官方文档未见 DELETE 响应示例 | 找到官方响应示例，或确认自定义形状可接受 | OPEN · `design-decision-to-evaluate` |
| 2026-09-13 | §10.1 | **"任务不存在"用哪个错误码** | 官方码表没有专门的 task-not-found | 复用 `InvalidEndpoint.NotFound`（404）—— 与"未知 provider"同码，**不泄露"它存在但不属于你"** | 官方码表无对应项 | 找到官方码表里的对应项 | OPEN · `existing-design-boundary` |
| 2026-09-13 | §10.2 | **本地并发满用哪个错误码** | 官方码表里 429 的几个 code 都指上游侧瓶颈 | 暂用 `ServerOverloaded`（429），message 明说"本地闸门满" | 官方码表无"调用方并发超限"这一类 | 找到更贴切的官方 code | OPEN · `design-decision-to-evaluate` |
| 2026-09-13 | §8.2 | **`usage` 口径：积分 → token 的倍率** | 上游按积分、Seedance 按 token；控制面计费依赖这个口径 | 默认 1:1 折算（`credits_per_token` 可覆盖），**同时保留原始积分字段**不丢信息 | 控制面的计费口径确认 | 控制面确认按哪种口径消费 | OPEN · `waiting-on-external-condition` |
| 2026-09-13 | §9.2 | **dry-run 开关形态** | 架构 D1 说"配置全走请求头"，而 dry-run 是调试开关不是渠道配置 | 已实现三个入口：`X-Dry-Run: 1` / 体键 `dry_run` / `extra_body.dry_run` | 用户确认是否保留三入口 | 用户确认 | OPEN · `design-decision-to-evaluate` |
| 2026-09-13 | §6.5 | **协调器是否启用** | 它没有调用方请求可借钥匙 ⇒ 无法自己回查上游；只能做本地可判定的事（超时置 `expired`） | 默认关闭；回调推送改为"调用方查询观察到状态变更时机会式推送"（已实现） | 需要"渠道在运行期注册凭证"的机制 | 决定是否投入做凭证注册 | OPEN · `design-decision-to-evaluate` |
| 2026-09-13 | §8.2 | **是否需要素材转存（`media.py`）** | aivideomaker 产物是**公开 24h URL**，与 Seedance 原生语义一致 | 倾向不实现（透传即可）；仅当接入"产物需鉴权/有效期 <24h"的上游时再补 | 第二个上游的实际产物语义 | 接入需要转存的上游时 | **RESOLVED**（2026-09-13：落成**逐渠道开关** `X-Channel-Options.rehost`，**默认关**）。Resolution：默认仍是"透传"（与"公开 24h URL"这一事实一致），需要时按渠道开启即可，不必等第二个上游 —— 条件变成**配置**而不是代码。开了之后：`GET /files/{name}` 给自有地址、对象名 `sha256(URL)[:24]` 使重复转存幂等、**转存失败只降级**（任务结果不变，`warnings` 里如实说明）、`rehost.upstream_url` 保留原地址。见 `adapter/media.py` 与 `tests/test_engine.py` 的转存用例 |
| 2026-09-13 | §12 | **可观测（span / Logfire）接线优先级** | 未配置时必须静默降级为 no-op；`/healthz` 要分两个字段报"已配置"与"真的会外发" | 倾向在 P8 一并做（有 logfire token 才生效） | 用户对可观测的需求强度 | 用户确认是否现在做 | **RESOLVED**（2026-09-13 用户确认：**现在做**，且上报信息要更详细 —— 含上游 task id、request/response **原文不脱敏**（凭证除外），并要求**线上线下一起校验**）。Resolution：见 `ADR-006`；`/healthz` 再补 `ready`/`reason` 两个字段；验收 = 离线 17 项（`tests/test_observability.py`，零网络）＋ 线上探针（`scripts/logfire_online_probe.py`，需 token）|
