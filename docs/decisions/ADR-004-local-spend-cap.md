# ADR-004: 上游无计费前闸门 ⇒ 支出上限由本层强制（动态计价须显式 opt-in）

## Status
Accepted (2026-09-13)。**用户确认：上游确实没有计费前闸门。**

## Background
官方线**提交即计费、无免费窗口**。旧实现（针对网页线观测写的）假设上游有
`X-Max-Credits` 请求头与 422 `BUDGET_EXCEEDED` 拒付语义，于是把"守住上限"这件事交给了上游。

官方文档到手后核对：**只列了 `key` / `Content-Type` / `webhookUrl` 三个请求头**，
没有 `X-Max-Credits`，也没有 422；越界提交的失败信封是 `{"status":"FAILED","message":"Insufficient credits"}`
—— 也就是**事后告知，不是事前拒绝**。

⇒ 若不自己拦，"支出上限"就是一句空话。

## Decision
支出上限是**提交的前置条件**，三道闸门全部在本层：

| # | 闸门 | 行为 |
| --- | --- | --- |
| 1 | 拿不到 `max_credits`（`X-Channel-Options` 或 `extra_body.aivideomaker_max_credits`） | **400**，且在任何上游请求之前 |
| 2 | 本地按官方公式估算出的积分 > 上限 | **400**，同样在发出请求之前 |
| 3 | **动态计价模型无法估算**（`seedance20`，公式不公开） | **默认 400**，必须显式 opt-in 才允许提交 |

第 3 条的两个出口：

- **给费率即可估**：`X-Channel-Options.credit_table = {"seedance20": 12}`（每秒积分由**控制面**提供
  —— 计费知识本来就属控制面，符合 D1"服务端不持计费知识"）；有费率就走闸门 2。
- **明确接受不可验证**：`X-Channel-Options.allow_unpriced = true`
  或请求级 `extra_body.aivideomaker_allow_unpriced = true`；放行但**在 `warnings` 里如实说明
  "本单成本无法在提交前验证"**。

**估算值只用于拒绝，不用于计费**：真实扣费以上游返回的 `creditsCharged` 为准。

## Consequences
- **正面**：不可能因为"上游没守"而产生意外账单；这是本项目唯一由本层承担的资金安全责任。
- **正面**：估算与真实扣费已被实测校准 —— 真实冒烟 `t2v` 5s：本地估算 15，上游
  `creditsCharged` **也是 15**。
- **负面**：动态计价模型默认不可用，接入方要么提供费率表、要么显式接受不可验证成本。
  这是刻意的摩擦：**"算不出来"不该由我们替用户拍板放行**。
- **负面**：官方公式随上游调价会过期 ⇒ 公式表要跟随 `docs/upstreams/<vendor>-*.md` 更新，
  且**本地估算不得被当作报价**。
- **注意**：`0` 是合法上限（等于禁止一切提交）。

## Related ADRs
ADR-001（目标协议）、ADR-003（凭证绑定）
