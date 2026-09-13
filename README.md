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
| [`docs/decisions/`](docs/decisions/) | **为什么这么做**：`OPEN-DECISIONS.md`（悬而未决登记册，只追加 + 就地关闭）＋ `ADR-001…008`（已锁定的架构决策及其代价） | 决策台账（每次开工先复现未决项） |

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
- [x] 五套测试共 **137 项**全绿，零消耗（见下；其中 6+2 项需要真 redis）
- [x] **容器化与发版**：`Dockerfile`（非 root / 只读根 / 自带健康检查）＋ `gunicorn.conf.py`
      （高可用调参，理由写在文件里）＋ `docker-compose.yml`（默认单副本 + 持久卷；多副本形态见文末）
      ＋ `.github/workflows/release.yml`（**push main 即自增 patch**，人工 tag 留给不兼容变更，
      见 [`ADR-007`](docs/decisions/ADR-007-release-model.md)）
- [x] **接入示例**（本文件「5 分钟接通」＋ `scripts/verify_docs_examples.py`：22 项断言把那一节
      的状态码与字段名钉住，零成本可重跑）

## 怎么跑

```bash
# 依赖（隔离环境，别污染系统 Python）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 四套测试：全部零消耗（零网络、零真实生成请求）
python tests/test_aivideomaker_video_v1.py    # 68 项 翻译层
python tests/test_engine.py                   # 32 项 引擎端到端（本地假上游）
python tests/test_rate_limit.py               # 18 项 限流与降频（12 项进程内 + 6 项需 redis）
python tests/test_persistence_redis.py        #  2 项 重启后仍能 GET（跨实例可见）+ 连不上 redis 启动失败
python tests/test_observability.py            # 17 项 上报内容（离线；logfire 不在场会红）

# 文档门禁：把 README「接入示例」那一节当断言跑（零成本、只打本地假上游）
# 发版工作流也会跑它 —— 文档会腐烂，但没人会因为文档过期收到告警。
python scripts/verify_docs_examples.py        # 22 项 示例的状态码与字段名

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

## 接入示例（5 分钟接通）

> ⚠️ 这一节的每条都**可执行验证**：`python scripts/verify_docs_examples.py` 会把下面每个
> 状态码与字段名在本地假上游上重跑一遍（**22 项断言，零成本**、不碰真实上游）。
> 文档会腐烂，而没人会因为文档过期收到告警 —— 所以把它变成断言。

四个端点与火山原生一致，**只改 Base URL 与 Key**。四类请求都要带**同一套渠道头**：

| 头 | 示例 | 说明 |
| --- | --- | --- |
| `X-Adapter-Key` | `ak_…` | 本服务准入密钥（部署侧 `ADAPTER_KEY`；未配置则**拒绝所有**请求） |
| `X-Upstream-Url` | `https://upstream.example.com` | 上游源站。脚本只能改**路径**、不能改 host（换 origin 直接 `channel_config_error`） |
| `X-Script-Ref` | `aivideomaker/video@v1` | 翻译脚本引用；**内联脚本被拒绝**（ADR-005） |
| `X-Auth-Emit` | `header:key:` | 凭证发射位置，形态 `header:<名字>:<前缀>` |
| `X-Channel-Options` | 见下 | JSON：`provider` / `max_credits` / `max_concurrency` / `rehost` / `credit_table` |
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

### 1) 创建 → 只回 `id`（外加"上报块"）

```bash
curl -sS -X POST "$BASE/api/v3/contents/generations/tasks" "${CH[@]}" -d "$BODY"
```

```json
{"id": "cgt-20260913223613-z0u4nx",
 "provider": "aivideomaker", "upstream_task_id": "ck001",
 "script_ref": "aivideomaker/video@v1", "script_sha256": "f3fb92d5…",
 "upstream_report": {"request": {"method": "POST", "url": "…", "body": {…},
                                 "headers": {"Content-Type": "application/json"}},
                     "response": {"status": 200, "body": {"status": "SUBMITTED", "taskId": "ck001"}, "headers": {…}},
                     "query_count": 0}}
```

⚠️ **创建响应里没有 `status`**（契约如此）：只能轮询或走回调。
`upstream_task_id` 与 `upstream_report` 是排障资产：回答"这次发了什么、上游回了什么、对应上游哪个任务"。

### 2) 查询（**必须带同一把 Key**）

```bash
curl -sS "$BASE/api/v3/contents/generations/tasks/$ID" "${CH[@]}"
```

```json
{"id": "cgt-…", "model": "aivideomaker/seedance20", "status": "running",
 "content": {"video_url": null, "last_frame_url": null, "file_url": null},
 "usage": {"completion_tokens": 15, "credits": 15, "credits_charged": 15, "credits_refunded": 0},
 "upstream": {"status": "PROGRESS", "creditsCharged": 15, …}}
```

终态（`succeeded`）时 `content.video_url` 有值；`usage.credits` 是**上游原始积分**（对账用，
换算口径见 §9）。已终态的任务再查**不会**打上游 —— 直接返回本地记录（7 天窗口内可用）。

> 🔴 **轮询间隔建议 ≥ `QUERY_CACHE_SECONDS`（默认 2s）。**
> 上游对**查询接口**按 IP 限 60 次/分钟，所以本层会**刻意降频**：同一任务在窗口内的重复查询
> 直接回放上一次结果（`upstream_report.query_count` **不**增加，它计的是真实上游查询数），
> 并发查询会合并成一次上游调用。比这更密的轮询不会更快看到状态变化，只会把配额烧成 429。
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
| 未知任务 / 不属于本凭证 | 404 | `InvalidEndpoint.NotFound`（**刻意同码**：不泄露"它存在但归别人"） |
| 准入密钥错 / 未配置 | 401 | `AuthenticationError` |
| 本地并发闸门满 | 429 | `ServerOverloaded` |
| **上游不可达（连不上 / 超时 / 连接中断）** | 502 | `UpstreamUnavailable` |
| 上游 5xx | 502 | `InternalServiceError` |
| 未预期异常（兜底） | 500 | `InternalServiceError`（**信封不变形**，绝不给裸 500） |

信封恒为 `{"error": {"code", "message", "type", "param"?}}`（§10.1）——调用方按 `code` 分支即可。
