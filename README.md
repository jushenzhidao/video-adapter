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

文档分工明确，读之前先建立这张地图：

| 文档 | 回答的问题 | 性质 |
| --- | --- | --- |
| [`docs/seedance-api-reference.md`](docs/seedance-api-reference.md) | **目标长什么样**：4 个端点、创建/查询字段表、`content[]` 多模态结构、六态状态机、回调语义、模型能力矩阵、素材限制、错误码、弱校验后缀 | 目标契约（**唯一真源**，逐字实现） |
| [`docs/adapter-playbook.md`](docs/adapter-playbook.md) | **怎么投影**：上游三形态归类、`content[]` 降维矩阵与判定准则、参数降维策略、产物回填、回调形状转换、并发与幂等、计费护栏、14 项零消耗测试清单 | 适配方法论（与具体上游无关） |
| [`docs/03_引擎架构.md`](docs/03_引擎架构.md) | **服务怎么搭**：渠道契约（11 个头）、脚本契约（相位 + `ctx`）、任务持久化、模块划分、实施顺序 | 引擎架构（**取代 playbook §10 的分层清单**） |
| [`docs/04_能力映射与降级.md`](docs/04_能力映射与降级.md) | **两者对不上怎么办**：Seedance 具体模型 ID → 上游 8 个粗槽位的声明式映射表、参数回退方向（`6s=>5s`）、能力驱动的模型降级链、结构化 `degradations[]` 上报契约 | 适配层**策略**真源（不改两侧契约，只规定差值怎么处理） |
| [`docs/upstreams/aivideomaker-official-api.md`](docs/upstreams/aivideomaker-official-api.md) | **上游长什么样**：aivideomaker 官方线 8 个模型的字段表与类型、计费公式、状态、限流，以及**与旧实现的 6 处差异核对** | 上游契约（脚本逐条实现本文件） |
| [`docs/decisions/`](docs/decisions/) | **为什么这么做**：`OPEN-DECISIONS.md`（悬而未决登记册，只追加 + 就地关闭）＋ `ADR-001…013`（已锁定的架构决策及其代价；对调用方影响最大的两条是 [`ADR-011`](docs/decisions/ADR-011-native-only-response.md) 响应体只含原生字段、[`ADR-012`](docs/decisions/ADR-012-model-name-passthrough.md) 模型名透传；边界类一条是 [`ADR-013`](docs/decisions/ADR-013-batching-boundary.md) **攒批不在本层做**、归 atask-service） | 决策台账（每次开工先复现未决项） |

## 前门契约速查

```
POST   /api/v3/contents/generations/tasks        → {"id": "cgt-YYYYMMDDHHMMSS-xxxxx"}   # **只有 id**
GET    /api/v3/contents/generations/tasks/{id}   → 原生任务对象（**逐键等于原生字段集**，status 六态）
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

### 🔴 响应体只含**原生字段**（2026-09-16 起）

响应形状的唯一实现点是 `adapter/seedance.py`，判据是"火山原生任务对象长什么样"：

| 端点 | 响应 |
| --- | --- |
| `POST` 创建 | **只有 `id`** —— 没有 status，也**没有上报块** |
| `GET` 查询 | 原生字段集（`id` / `status` / `content` / `error` / `usage` / `created_at` / …） |
| `GET` 列表 | `{items, total, page_num, page_size}`，**item 与查询同形** |

**刻意不回显的三类字段**：

| 不回的字段 | 为什么 |
| --- | --- |
| `model` | 上游模型名很乱（8 个槽位名与各家原生 ID 混写），回显它没有信息增量。"我请求的 vs 实际跑的"改从 logfire 的 `task.effective.*` 与 `dry_run` 查 |
| `provider` / `upstream_task_id` / `script_ref` / `script_sha256` / `upstream_report` / `upstream` | 本服务的内部诊断，不属于原生契约 |
| `requested` / `effective` / `warnings[]` / `unsupported[]` / `rehost` | 同上（`warnings` 曾经是降级告知通道，见下） |

⚠️ **代价（已接受）**：响应体不再是**降级告知**通道 —— 参数被吸附/丢弃、模型被映射，只体现在
logfire 与 `dry-run` 里。接入方若需要"我请求的 vs 实际生效的"，用 `X-Dry-Run: 1` 或读 logfire。

### 模型名怎么写：默认**透传**，需要时由渠道配映射表

本层**不改模型值**：调用方写什么名字，就发到上游 `POST /api/v1/generate/{那个名字}`。
所以合法值就是上游认识的槽位名（当前上游 8 个：
`t2v` `i2v` `t2v_v3` `i2v_v3` `minimax` `seedance20` `wan27` `happyhorse`）。

写火山原生 ID（如 `doubao-seedance-2-0-260128`）时，由**渠道**给出映射表 ——
模型知识属于控制面，**不进代码**：

```json
X-Channel-Options: {"provider":"aivideomaker","max_credits":5000,
                    "model_map":{"doubao-seedance-2-0-260128":"seedance20",
                                 "doubao-seedance-1-0-pro-250528":"t2v"}}
```

五条约束：① **精确匹配优先**，`*` 是**唯一**元字符（`{"doubao-seedance-*": "seedance20"}`；
`?` / `[` / `]` 是字面量、大小写敏感）；② 重复键显式拒绝（JSON 会静默覆盖，而"哪条生效"决定
账单），**两条通配同时命中同样报错**（不排序、不取最长）；③ 没配表又不认识 ⇒ **400 并列出
合法值与已配的键**，绝不兜底；④ 命中可查（`dry_run` 与 logfire 的 `task.effective.model_map_applied`）
＋命中的**模式**见 `task.effective.model_map_pattern`；⑤ **通配优先于"名字本身就是槽位名"** ——
`{"*": "t2v"}` = 本渠道只跑 t2v，**连合法槽位名也会被改写**（`{"*": "h3"}` 这类非法槽位值会被拒）。
⚠️ 映射表放在请求头里 ⇒ **别堆成几百条**（HTTP 头有整体大小上限），它是渠道级声明，不是词库。

状态机：`queued → running → succeeded | failed | expired`，`queued --DELETE--> cancelled`。
`status` 只会是这六个值之一（上游给出别的值时会**收敛为 `running`**，并在 logfire 记
`task.status.unnormalized=true` —— 宁可"当成还在跑"，也绝不把不认识的状态当终态）。

## 架构决策（2026-09-13 确认）

1. **渠道配置来源与 image-adapter 完全一致**：11 个 HTTP 头驱动，服务端不持有渠道/模型/计费知识。
2. **翻译脚本写死在项目**：只接受 `X-Script-Ref` 命名引用，内联脚本被拒绝 —— 降级报告要经 review、可追溯。
3. **引擎移植 image-adapter**：脚本 + AST 沙箱 + 相位（create/query/cancel）+ `ctx` API + 任务持久化。
4. **首批上游**：aivideomaker 官方 API 线（`key` 头 + `/api/v1/*`，8 个模型）。

详见 [`docs/03_引擎架构.md`](docs/03_引擎架构.md) §1。

## 硬纪律

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
5. **本层不承担计费**：计费与配额在 **new-api**（本服务作为上游渠道挂在其后，见 `ADR-010`）。
   本层在"钱"上只做两件事：① 把上游**实收**积分折算成 `usage` 供 new-api 消费
   （`credits_per_token` 是运营方的**定价旋钮**，且**原始积分同时保留**，让下游能按自己的口径重算）；
   ② 用 `max_credits` 给**运营方的上游余额**加一道**提交前置**的止损。
   ⚠️ 后者防的是"误提交把上游余额花掉"，**不是向调用方收钱** —— 别把它当额度系统配。

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
- [x] 八套测试共 **233 项**全绿，零消耗（见下；其中 6+2 项需要真 redis）
- [x] **部署参数按实测重调**（2026-09-16）：删掉对异步 worker 无效的 `worker_connections`、
      `timeout`/`graceful_timeout` 改为**由上游超时推导**（不再写死 300/120，避免"调大上游超时
      就变得可被 SIGKILL"），并把 11 条不变式写成断言（`tests/test_gunicorn_config.py`）
- [x] **容器化与发版**：`Dockerfile`（非 root / 只读根 / 自带健康检查）＋ `gunicorn.conf.py`
      （高可用调参，理由写在文件里）＋ `docker-compose.yml`（默认单副本 + 持久卷；多副本形态见文末）
      ＋ `.github/workflows/release.yml`（**push main 即自增 patch**，人工 tag 留给不兼容变更，
      见 [`ADR-007`](docs/decisions/ADR-007-release-model.md)）
- [x] **接入示例**（本文件「5 分钟接通」＋ `scripts/verify_docs_examples.py`：35 项断言把那一节
      的状态码与字段名钉住，零成本可重跑）
- [x] **响应体收敛为原生形状 + 诊断改道 logfire**（2026-09-16）：创建只回 `id`、查询不含 `model`
      与任何诊断块；上游 task id / 脚本摘要 / 请求响应留档 / `requested` / `effective` / 降级告警
      全部改由 `task.snapshot` span 上报（[`ADR-011`](docs/decisions/ADR-011-native-only-response.md)）
- [x] **模型名透传 + 渠道可配映射表**（同上）：删掉脚本内的顺序敏感正则映射
      （它把 5 个代次的原生 ID 静默压进同一槽位，实测账单差 7.3 倍）

## 怎么跑

```bash
# 依赖（隔离环境，别污染系统 Python）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 八套测试：全部零消耗（零网络、零真实生成请求）
python tests/test_aivideomaker_video_v1.py    # 79 项 翻译层（透传/精确与通配映射/别名/遗留键拒绝）
python tests/test_engine.py                   # 32 项 引擎端到端（本地假上游）
python tests/test_seedance_contract.py        # 15 项 **原生响应契约**（形状/字段集/六态/上报不丢）
python tests/test_rate_limit.py               # 18 项 限流与降频（12 项进程内 + 6 项需 redis）
python tests/test_persistence_redis.py        #  2 项 重启后仍能 GET（跨实例可见）+ 连不上 redis 启动失败
python tests/test_observability.py            # 19 项 上报内容（离线；logfire 不在场会红）
python tests/test_mock_upstream.py            # 57 项 假上游的契约一致性（见下）
python tests/test_gunicorn_config.py          # 11 项 **部署参数不变式**（含跨文件的停机窗口检查）

# 文档门禁：把 README「接入示例」那一节当断言跑（零成本、只打本地假上游）
# 发版工作流也会跑它 —— 文档会腐烂，但没人会因为文档过期收到告警。
python scripts/verify_docs_examples.py        # 27 项 示例的状态码与字段名

# 上报通路（线上，**会真外发**一条合成 span，需 LOGFIRE_TOKEN）
LOGFIRE_TOKEN="$(cat /tmp/.logfire_token)" python scripts/logfire_online_probe.py

# 起服务（本地直跑：gunicorn，配置即生产那份）
cp .env.example .env    # 至少填 ADAPTER_KEY 与 TASK_KEY_FINGERPRINT_SECRET
.venv/bin/gunicorn adapter.main:app --config gunicorn.conf.py
# 契约表与试调：http://127.0.0.1:8000/docs

# 起服务（容器，推荐：带持久卷与健康检查）
docker compose up -d --build
curl -s localhost:8000/healthz      # 里面能看到 version / logfire / queue 三块状态

# 制品层端到端（**零计费**）：打一个真跑起来的容器栈，上游是同网络的假上游
export ADAPTER_KEY=ak_dev_local TASK_KEY_FINGERPRINT_SECRET=$(openssl rand -hex 32)
docker compose -f docker-compose.yml -f docker-compose.audit.yml --profile mock up -d --build
E2E_ADAPTER_KEY=$ADAPTER_KEY E2E_BASE=http://127.0.0.1:8000 \
  python scripts/e2e_zero_cost.py     # 96 项：含"上游实际收到几次请求"的断言
                                      # ＋模型映射的差分对（配了/没配 model_map 的两种结果）
docker compose -f docker-compose.yml -f docker-compose.audit.yml --profile mock down -v
```

**为什么要两套端到端**：`tests/test_engine.py` 在**同一进程内**用 `ASGITransport` 打自己的
app —— 它验证引擎语义，但**永远发现不了**"镜像少拷了一个目录""compose 少传了一个环境变量"
这类问题。`scripts/e2e_zero_cost.py` 打的是**制品**，两者不可互替。

**假上游为什么要单测**：`mock_upstream/` 的失败模式不是"跑不起来"，而是**太宽松** ——
被测服务出错了它照样点头，于是端到端全绿而缺陷活着。所以它刻意复现了三条会「假绿」的上游
语义（模型白名单、任务按 Key 归属、取消只认 `PUT`），并由 `tests/test_mock_upstream.py`
把这套语义钉住。详见 [`mock_upstream/README.md`](mock_upstream/README.md)。


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
| `max_credits` | 上限是**提交前置条件**：上游没有计费前闸门，拿不到上限就 400。⚠️ 它是**上游账户的止损栏杆**、**不是计费** —— 计费与配额在 new-api（`ADR-010`），本层不持单价、不做扣减 |
| `credit_table` | 给**动态计价模型**（`seedance20`）补每秒费率，让它恢复可估 |
| `allow_unpriced` | 显式接受"成本不可在提交前验证"；不给且算不出成本 → 400 |
| `model_map` | 调用方写火山原生 ID 时的映射表（`{调用方写的名字: 上游槽位名}`）。**精确优先**，也支持 `*` 通配（`{"doubao-seedance-*": "seedance20"}`）；**`{"*": "t2v"}` = 本渠道只跑 t2v，连合法槽位名也会被改写**（通配优先）；不配则模型名逐字透传，未知名 400 |

## 接入示例（5 分钟接通）

> ⚠️ 这一节的每条都**可执行验证**：`python scripts/verify_docs_examples.py` 会把下面每个
> 状态码与字段名在本地假上游上重跑一遍（**35 项断言，零成本**、不碰真实上游）。
> 文档会腐烂，而没人会因为文档过期收到告警 —— 所以把它变成断言。

四个端点与火山原生一致，**只改 Base URL 与 Key**。四类请求都要带**同一套渠道头**：

| 头 | 示例 | 说明 |
| --- | --- | --- |
| `X-Adapter-Key` | `ak_…` | 本服务准入密钥（部署侧 `ADAPTER_KEY`；未配置则**拒绝所有**请求） |
| `X-Upstream-Url` | `https://upstream.example.com` | 上游源站。脚本只能改**路径**、不能改 host（换 origin 直接 `channel_config_error`） |
| `X-Script-Ref` | `aivideomaker/video@v1` | 翻译脚本引用；**内联脚本被拒绝**（ADR-005） |
| `X-Auth-Emit` | `header:key:` | 凭证发射位置，形态 `header:<名字>:<前缀>` |
| `X-Channel-Options` | 见下 | JSON：`provider` / `max_credits` / `max_concurrency` / `rehost` / `credit_table` / `model_map` |
| `Authorization` | `Bearer <上游 Key>` | 上游凭证。**不落明文**，只存 HMAC 指纹（ADR-003） |

```bash
BASE=http://127.0.0.1:8000
CH=(-H "X-Adapter-Key: $ADAPTER_KEY" \
    -H "X-Upstream-Url: $UPSTREAM_URL" \
    -H "X-Script-Ref: aivideomaker/video@v1" \
    -H "X-Auth-Emit: header:key:" \
    -H 'X-Channel-Options: {"provider":"aivideomaker","max_credits":5000,"allow_unpriced":true}' \
    -H "Authorization: Bearer $UPSTREAM_KEY" \
    -H 'Content-Type: application/json')
BODY='{"model":"aivideomaker/seedance20","content":[{"type":"text","text":"一只猫在打哈欠"}],"duration":5,"resolution":"720p","ratio":"16:9"}'
```

### 0) 先 dry-run：跑完整翻译 + 计费校验，**不发上游请求、零消耗**

```bash
curl -sS -X POST "$BASE/api/v3/contents/generations/tasks" "${CH[@]}" -H 'X-Dry-Run: 1' -d "$BODY"
```

```json
{"dry_run": true,
 "upstream": {"method": "POST", "url": "…/api/v1/generate/seedance20",
              "body": {"duration": 5, "resolution": 720, "ratio": "16:9", "prompt": "一只猫在打哈欠"}},
 "provider": "aivideomaker", "model": "seedance20", "script_ref": "aivideomaker/video@v1",
 "warnings": ["cost is not verifiable before submit …"], "unsupported": []}
```

（节选）`upstream.body` 就是脚本**将要发出的原文** —— 接一个新上游时先用它对齐字段。

### 1) 创建 → **只回 `id`**

```bash
curl -sS -X POST "$BASE/api/v3/contents/generations/tasks" "${CH[@]}" -d "$BODY"
```

```json
{"id": "cgt-20260916223513-z0u4nx"}
```

**就这一个键**（火山原生契约：`POST` 成功体只有 `id`）。没有 `status`，也**没有上报块** ——
所以：① 必须轮询或走回调；② "上游 task id 是哪个 / 这次发了什么 / 上游回了什么" 去 **logfire**
的 `task.snapshot` span 里看（`task.upstream_id`、`task.report.request/response`）。

### 1.5) 模型名：默认透传，原生 ID 需要渠道配 `model_map`

```bash
# ① 没配映射表 ⇒ 上游不认识这个名字，400（不猜、不兜底 —— 兜底到别的槽位就是一张账单）
curl -sS -X POST "$BASE/api/v3/contents/generations/tasks" "${CH[@]}" \
  -d '{"model":"doubao-seedance-2-0-260128", …}'
# → {"error":{"code":"InvalidParameter","param":"model","message":"unknown model … must be one of …"}}

# ② 渠道配了 model_map ⇒ 同一个名字可用，且上游收到的是映射后的槽位
CH_MAP=(-H 'X-Channel-Options: {"provider":"aivideomaker","max_credits":5000,"allow_unpriced":true,"model_map":{"doubao-seedance-2-0-260128":"seedance20"}}' …)
curl -sS -X POST "$BASE/api/v3/contents/generations/tasks" "${CH_MAP[@]}" \
  -d '{"model":"doubao-seedance-2-0-260128", …}'
# → {"id": "cgt-…"}   ；上游实际收到 POST /api/v1/generate/seedance20

### 2) 查询（**必须带同一把 Key**）

```bash
curl -sS "$BASE/api/v3/contents/generations/tasks/$ID" "${CH[@]}"
```

```json
{"id": "cgt-…", "status": "running",
 "content": {"video_url": null, "last_frame_url": null, "file_url": null},
 "error": null,
 "usage": null,
 "created_at": 1789568751, "updated_at": 1789568754,
 "seed": -1, "resolution": "720p", "ratio": "16:9", "duration": 5, "frames": null,
 "framespersecond": 24, "service_tier": "default", "execution_expires_after": 172800,
 "generate_audio": false, "draft": false, "priority": 0}
```

终态时（`succeeded`）：

```json
{"id": "cgt-…", "status": "succeeded",
 "content": {"video_url": "https://…/ck001.mp4", "last_frame_url": null, "file_url": null},
 "error": null,
 "usage": {"completion_tokens": 15, "total_tokens": 15},
 …}
```

五条使用要点：

1. **逐键等于原生字段集** —— 不多（`model` / 诊断块都已移除）、不少（原生字段恒在）。
   按原生契约写严格校验 / schema 的 SDK 可以直接吃。
2. **`content.video_url` 只在 `succeeded` 时有值**（其余状态为 `null`）。
3. **`usage` 只有 token 两项**：上游的积分字段不进这里（计费归属 new-api，见 `ADR-010`）；
   原始积分与折算倍率在 logfire 的 `task.usage.*` 里可查。
4. **回显字段的含义分两类**：`service_tier` / `execution_expires_after` / `priority` 按**请求值**回显；
   `resolution` / `ratio` / `duration` / `generate_audio` / `draft` / `seed` 按**实际生效值**回显
   （例：本上游无一支持生成有声视频 ⇒ `generate_audio` 恒 `false`，即使你请求了 `true`）。
   想知道"我的 `720p` 到底有没有生效"，用 `X-Dry-Run: 1`（带 `warnings[]`）或读 logfire。
5. **已终态的任务再查不会打上游** —— 直接返回本地记录（7 天窗口内可用）。

> 🔴 **轮询间隔建议 ≥ `QUERY_CACHE_SECONDS`（默认 2s）。**
> 上游对**查询接口**按 IP 限 60 次/分钟，所以本层会**刻意降频**：同一任务在窗口内的重复查询
> 直接回放上一次结果（logfire 的 `task.query.cache_hit=true`，且 `task.query.count` **不**增加 ——
> 它计的是真实上游查询数），并发查询会合并成一次上游调用。比这更密的轮询不会更快看到状态变化，
> 只会把配额烧成 429。
> 机制与三档可调参数见 `docs/03_引擎架构.md` §7.1。
>
> 同理：**429 响应一定带 `Retry-After`**（可能是本层主动限流拦的，也可能是上游回的），
> 请按它退避后再重试 —— 立即盲目重试会持续撞线。

### 3) 列表

```bash
curl -sS "$BASE/api/v3/contents/generations/tasks?page_num=1&page_size=5" "${CH[@]}"
# → {"items": [ … ], "total": 1, "page_num": 1, "page_size": 5}
```

列表按 `(provider, 凭证指纹)` 过滤：**只看得到本 Key 建的任务**（上游侧也是这个语义）。

### 4) 取消 / 删除

```bash
curl -sS -X DELETE "$BASE/api/v3/contents/generations/tasks/$ID" "${CH[@]}"
```

`queued` → 取消（返回 `status: cancelled`）；已终态 → 删除本地记录（`{"id": …, "deleted": true}`）。
`running` 的任务**拒绝取消**（上游只允许取消未开始的）—— 这一点与原生契约一致。

### 错误码速查（`scripts/verify_docs_examples.py` 逐条断言）

| 场景 | HTTP | `error.code` |
| --- | --- | --- |
| 拿不到支出上限 | 400 | `InvalidParameter`（`param=extra_body.aivideomaker_max_credits`） |
| `model` 的 provider 与渠道声明不一致 | 400 | `InvalidParameter`（`param=model`） |
| 模型名既不是上游槽位、也不在渠道 `model_map` 里 | 400 | `InvalidParameter`（`param=model`，消息里列出合法值与已配的键） |
| 渠道 `model_map` 配错（值不是上游模型 / 重复键） | 400 | `channel_config_error`（运维的错，与调用方分开报） |
| 未知任务 / 不属于本凭证 | 404 | `InvalidEndpoint.NotFound`（**刻意同码**：不泄露"它存在但归别人"） |
| 准入密钥错 / 未配置 | 401 | `AuthenticationError` |
| 本地并发闸门满 | 429 | `ServerOverloaded` |
| **上游不可达（连不上 / 超时 / 连接中断）** | 502 | `UpstreamUnavailable` |
| 上游 5xx | 502 | `InternalServiceError` |
| 未预期异常（兜底） | 500 | `InternalServiceError`（**信封不变形**，绝不给裸 500） |

信封恒为 `{"error": {"code", "message", "type", "param"?}}`（§10.1）——调用方按 `code` 分支即可。
