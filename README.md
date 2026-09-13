# video-adapter

把**任意视频生成上游**适配为**火山方舟 Seedance 原生协议**的适配层项目。

调用方只改 `Base URL` 与 `API Key`，即可把现有 Seedance 集成无痛切到我们的上游。

## 为什么目标是 Seedance

Seedance 是一份**规范化的超集协议**：文生视频 / 图生视频（首帧、首尾帧）/ 视频生视频 /
全能参考（图+视频+音频组合）/ 音画联合生成 / 视频编辑与延长 / 样片模式 / 连续视频拼接，
全部收敛在**同一组端点 + 同一套字段体系**下。

因此适配层的工程主干是**降维投影（超集 → 子集）+ 显式降级报告**，
而不是自己发明一套接口。

## 知识库

三份文档分工明确，读之前先建立这张地图：

| 文档 | 回答的问题 | 性质 |
| --- | --- | --- |
| [`docs/seedance-api-reference.md`](docs/seedance-api-reference.md) | **目标长什么样**：4 个端点、创建/查询字段表、`content[]` 多模态结构、六态状态机、回调语义、模型能力矩阵、素材限制、错误码、弱校验后缀 | 目标契约（**唯一真源**，逐字实现） |
| [`docs/adapter-playbook.md`](docs/adapter-playbook.md) | **怎么投影**：上游三形态归类、`content[]` 降维矩阵与判定准则、参数降维策略、产物回填、回调形状转换、并发与幂等、计费护栏、14 项零消耗测试清单 | 适配方法论（与具体上游无关） |
| [`docs/03_引擎架构.md`](docs/03_引擎架构.md) | **服务怎么搭**：渠道契约（11 个头）、脚本契约（相位 + `ctx`）、任务持久化、模块划分、实施顺序 | 引擎架构（**取代 playbook §10 的分层清单**） |
| [`docs/upstreams/aivideomaker-official-api.md`](docs/upstreams/aivideomaker-official-api.md) | **上游长什么样**：aivideomaker 官方线 8 个模型的字段表与类型、计费公式、状态、限流，以及**与旧实现的 6 处差异核对** | 上游契约（脚本逐条实现本文件） |
| [`docs/decisions/`](docs/decisions/) | **为什么这么做**：`OPEN-DECISIONS.md`（悬而未决登记册，只追加 + 就地关闭）＋ `ADR-001…005`（已锁定的架构决策及其代价） | 决策台账（每次开工先复现未决项） |

## 前门契约速查

```
POST   /api/v3/contents/generations/tasks        → {"id": "cgt-YYYYMMDDHHMMSS-xxxxx"}   # 只回 id
GET    /api/v3/contents/generations/tasks/{id}   → 完整任务对象（status 六态）
GET    /api/v3/contents/generations/tasks        → {items:[...], total}
DELETE /api/v3/contents/generations/tasks/{id}   → 取消（仅 queued 可取消）/ 删除
```

**路径逐字等于原生**（不加前缀、不加路径段）；**多上游靠 `model = provider/model` 区分** ——
一个 `base_url` 服务所有上游，如 `"model": "aliyun-bailian/deepseek-v3"`。

`provider` 指的是 **API 供给方，不是模型出品方**：同一个 `deepseek-v3`，
`deepseek-official/`、`aliyun-bailian/`、`volcengine-ark/` 是**三个不同上游**、三套凭证与计费，
**不能写成 `deepseek/...`**（否则不同供给方撞成一个 token，直接误路由）。
`provider` 与渠道声明不一致 → 400（防止"模型名写错 → 静默落到别的上游 → 直接付费"）。
不做 `/api/plan/v3`（Agent Plan 企业版）与 `/api/v1`（LAS 算子）的前缀兼容。

状态机：`queued → running → succeeded | failed | expired`，`queued --DELETE--> cancelled`。

## 架构决策（2026-09-13 确认）

1. **渠道配置来源与 image-adapter 完全一致**：11 个 HTTP 头驱动，服务端不持有渠道/模型/计费知识。
2. **翻译脚本写死在项目**：只接受 `X-Script-Ref` 命名引用，内联脚本被拒绝 —— 降级报告要经 review、可追溯。
3. **引擎移植 image-adapter**：脚本 + AST 沙箱 + 相位（create/query/cancel）+ `ctx` API + 任务持久化。
4. **首批上游**：aivideomaker 官方 API 线（`key` 头 + `/api/v1/*`，8 个模型）。

详见 [`docs/03_引擎架构.md`](docs/03_引擎架构.md) §1。

## 三条硬纪律

1. **适配 ≠ 真实生成**：翻译层正确性由单测 + `dry_run` 证明，不发真实生成请求。
   要端到端实跑只用最便宜档位，且**发之前先问**。
2. **参考类素材不能静默丢**：`reference_image` / `video_url` / `audio_url` 缺失对应能力时应返回 `400`，
   而不是悄悄降质 —— 丢掉它们等于改变了"用户想要什么"。
3. **任务记录必须持久化 ≥7 天且重启不丢**（契约要求 `GET` 在 7 天窗口内可用），
   因此**不允许"Redis 缺失则进程内 dict 降级"**的做法。
4. **异步任务必须记住 "API Key ⇄ task_id" 的绑定**：上游的任务查询要**同一把钥匙**，
   且上游侧任务就按钥匙归属（任务列表只返回当前 Key 名下的）。
   任务记录里存**创建时那把 Key 的指纹**（`credential_id`，HMAC，**不落明文**），
   查询/取消时指纹不符 ⇒ **本地直接 404**，不带着错的钥匙去问上游。
   轮换钥匙后旧任务只能用旧钥匙查 —— 那是上游语义，不是本服务的缺陷。

## 状态

- [x] 目标协议契约固化（`seedance-api-reference.md`）
- [x] 适配方法论梳理（`adapter-playbook.md`）
- [x] 引擎架构与实施计划（`03_引擎架构.md`，**待确认后开工**）
- [x] 上游契约固化（`docs/upstreams/aivideomaker-official-api.md`，含与旧实现的 6 处差异核对）
- [x] 首个上游脚本 `script_store/aivideomaker/video@v1.py` + `manifest.json`（sha256 锁定）
      ＋ **63 项零消耗测试全绿**（`python3 tests/test_aivideomaker_video_v1.py`）
- [x] 引擎骨架（P1–P4）：4 路由 + 归一化 + 渠道与脚本装载 + 任务持久化 + 并发闸门
- [x] 端到端联通（引擎 + 脚本，**本地假上游**跑通 create → query → cancel；**重启后仍能 GET**）
- [x] 素材转存 `media.py`（逐渠道开关 `rehost`，默认关）+ `GET /files/{name}`
- [x] **可观测（span / Logfire）接线**：每次上游调用带 **request/response 原文 + 上游 task id**，
      凭证在源头打码；字段表见 [`docs/03_引擎架构.md`](docs/03_引擎架构.md) §12，
      决策见 [`ADR-006`](docs/decisions/ADR-006-report-fidelity.md)
- [x] 四套测试共 **117 项**全绿，零消耗（见下）
- [x] **容器化与发版**：`Dockerfile`（非 root / 只读根 / 自带健康检查）＋ `gunicorn.conf.py`
      （高可用调参，理由写在文件里）＋ `docker-compose.yml`（默认单副本 + 持久卷；多副本形态见文末）
      ＋ `.github/workflows/release.yml`（**推 tag 即发布**：构建并推 GHCR 三个 tag + 建 Release）
- [ ] 接入示例（`curl` / SDK 片段）

## 怎么跑

```bash
# 依赖（隔离环境，别污染系统 Python）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 四套测试：全部零消耗（零网络、零真实生成请求）
python tests/test_aivideomaker_video_v1.py    # 68 项 翻译层
python tests/test_engine.py                   # 31 项 引擎端到端（本地假上游）
python tests/test_persistence_sqlite.py       #  1 项 重启后仍能 GET
python tests/test_observability.py            # 17 项 上报内容（离线；logfire 不在场会红）

# 上报通路（线上，**会真外发**一条合成 span，需 LOGFIRE_TOKEN）
LOGFIRE_TOKEN="$(cat /tmp/.logfire_token)" python scripts/logfire_online_probe.py

# 起服务（本地直跑：gunicorn，配置即生产那份）
cp .env.example .env    # 至少填 ADAPTER_KEY 与 TASK_KEY_FINGERPRINT_SECRET
.venv/bin/gunicorn adapter.main:app --config gunicorn.conf.py
# 契约表与试调：http://127.0.0.1:8000/docs

# 起服务（容器，推荐：带持久卷与健康检查）
docker compose up -d --build
curl -s localhost:8000/healthz      # 里面能看到 version / logfire / queue 三块状态
```

**发版**（两条入口，工作流都会跑测试 → 推镜像 → 建 Release）：

```bash
# ① 日常：push 到 main 即**自增 patch** 并发布（v0.1.0 → v0.1.1 → …）
git push origin main

# ② 需要 minor/major、或契约不向后兼容时：人工打 tag，不做自动递增
git tag -a v0.2.0 -m "Release v0.2.0" && git push origin v0.2.0

# ③ 补发某个已存在的 tag（可重入：检出的就是该 tag，Release 已存在则跳过）
gh workflow run release.yml -f tag=v0.1.0

docker pull ghcr.io/jushenzhidao/video-adapter:latest   # 镜像 tag 不带 v 前缀
```

⚠️ 纯文档 / CI 自身的提交不该切版本时，在 commit message 里写中括号的 skip release 标记
（字面量见 workflow 的 `if:`）—— 它匹配整条 message，所以"描述这个标记"的提交自己也会被跳过。
自动递增只覆盖 patch：**对外契约发生变化时必须走 ②**，由人决定版本号。

扩容/高可用的三处改动、以及"并发闸门是进程内状态"这条语义代价，见
[`docs/03_引擎架构.md`](docs/03_引擎架构.md) §16 与 `docker-compose.yml` 文末。

接入方按渠道配置请求头即可（一个渠道一套）：`X-Upstream-Url` / `X-Script-Ref` /
`X-Auth-Emit` / `X-Channel-Options`，凭证放 `Authorization`。
⚠️ **查询与取消也要带同一套头**（含同一把 Key）—— 任务与凭证是绑定的，见下条硬纪律。

**`X-Channel-Options` 的四个关键键**（完整清单见 `docs/03_引擎架构.md` §4.2）：

| 键 | 为什么必须有 |
| --- | --- |
| `provider` | 与 `model` 里的 provider 段做断言，不一致 → 400（防止误路由到别的上游并计费） |
| `max_credits` | 上限是**提交前置条件**：上游没有计费前闸门，拿不到上限就 400 |
| `credit_table` | 给**动态计价模型**（`seedance20`）补每秒费率，让它恢复可估 |
| `allow_unpriced` | 显式接受"成本不可在提交前验证"；不给且算不出成本 → 400 |
