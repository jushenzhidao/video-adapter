# ADR-005: 翻译脚本写在项目里，只接受 `X-Script-Ref`

## Status
Accepted (2026-09-13)

## Background
参考实现 `image-adapter` 允许调用方在请求头里**内联脚本**（`X-Script` / `X-Script-64`），
那是"简单上游字段映射的草稿纸"，所以它默认开着（`ALLOW_INLINE_SCRIPT=true`）。

本项目的翻译逻辑同时承担**语义判断**：判断"丢掉某一项会不会改变用户想要什么"
（判定准则见 `docs/adapter-playbook.md` §3.2）。这种逻辑一旦出错，后果是
**参考素材被静默丢弃**或**错误地打到别的上游并计费**。

## Decision
**只接受 `X-Script-Ref`** 命名引用（如 `aivideomaker/video@v1`），
**内联脚本（`X-Script` / `X-Script-64`）一律拒绝**，报 `channel_config_error` 并点名"请改用 `X-Script-Ref`"。

- `ALLOW_INLINE_SCRIPT` 默认 **false**（与 image-adapter **相反**）。
- 脚本随镜像发版，**可 review、可追溯**。
- 生产同时开 `SCRIPT_PIN_MANIFEST_DIGESTS=true`：ref 内容与 `script_store/manifest.json`
  的 sha256 逐字节比对；ref 未登记在 manifest 里也直接拒绝（不允许"漂着的脚本"）。
- 脚本在 **AST 沙箱**中装载：白名单 stdlib + 受限 builtins；禁 `exec/eval/open/getattr/setattr`
  与 dunder 属性访问 —— 基础设施只能经 `ctx` 触达。
- 引擎额外把关：脚本返回的请求计划**只能改路径，不能改源站**
  （`executor._plan_to_request` 校验 origin 与渠道 `X-Upstream-Url` 一致）。

## Consequences
- **正面**：降级报告这类语义判断必须经过 review 才能上线；调用方无法现场投喂逻辑。
- **正面**：脚本内容被 sha256 钉住，"线上跑的是哪一版"可回答。
- **负面**：新增/修改上游翻译必须发版，不能靠改请求头热修 —— 这是刻意用便利换可审计性。
- **负面 / 承重墙**：沙箱禁 `getattr` 会让脚本不能用 `getattr(obj, "field", default)`
  这类容错写法（`getattr(x, "__class__")` 能绕过 dunder 的 AST 检查）。
  **不能为了脚本方便放宽沙箱**，要改脚本。

## Related ADRs
ADR-001（目标协议）、ADR-003（凭证绑定）
