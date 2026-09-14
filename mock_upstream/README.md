# mock_upstream/ —— 可独立部署的假上游

`mock_aivideomaker.py` 是 **aivideomaker 官方线**的假上游，用途只有一个：
让**制品层端到端验证零成本**（不打真实上游、不产生任何积分消耗）。

## 为什么它在仓库里，而原先的假上游不够用

原先的 `FakeUpstream` 只存在于 `tests/test_engine.py` 的**测试进程内**。它足够验证引擎逻辑，
但**够不到"容器里真正跑起来的服务"** —— 制品层（镜像 + compose + nginx 入口）没有上游可用，
于是那层验证只能打真实上游，也就是**必须计费**。姊妹项目 `image-adapter` 一直有 `mock_upstream/`，
本项目此前缺，此目录即回填。

## 保真度：它刻意复现了三条会「假绿」的上游语义

假上游最危险的地方不是"跑不起来"，而是**太宽松** —— 被测服务出错了它照样点头，
于是测试全绿而缺陷活着。以下三条是对着官方契约（`docs/upstreams/aivideomaker-official-api.md`）
刻意做的**收紧**：

| 语义 | 官方依据 | 不复现会漏掉什么 |
|---|---|---|
| **模型白名单**：只接受 8 个合法 `{model}`，其余回 `INVALID_MODEL` | §4 | "模型名映射错 → 上游落到默认模型 → **直接计费**"这条最危险的失败路径。宽松的假上游会让它一路绿 |
| **任务按 Key 归属**：异 Key 查/取消同一个任务 → `404` | §2 | 适配层"跳过 `credential_id` 指纹校验、带着错钥匙去问上游"的回归 —— 宽松的假上游会把任务还给它 |
| **取消只认 `PUT`**：`POST` / `DELETE` 一律 `405` | §2 | 适配层"改用 DELETE 也能过"的漂移（`PUT` 是上游的硬约定） |

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/generate/{model}` | 创建，返回 `taskId` + 三个链接 |
| GET | `/api/v1/tasks` | 列出**当前 Key 名下**任务（⚠️ 响应形状官方未定义，见下） |
| GET | `/api/v1/tasks/{id}` | 详情（**脚本的查询相位用的是这个**，不是 `/status`） |
| GET | `/api/v1/tasks/{id}/status` | 仅状态 |
| PUT | `/api/v1/tasks/{id}/cancel` | 取消 |
| GET | `/media/{id}.mp4` | 假产物（140 字节、magic = `ftypmp42`），验证 rehost 转存 |
| GET | `/__healthz` | 给 compose healthcheck 用 |

控制面（**无鉴权**，所以只允许内网 / 回环访问）：

| 路径 | 用途 |
|---|---|
| `GET /__control/count?method=&prefix=` | 上游**实际收到**的请求数（断言"发了几次"的唯一可信来源） |
| `GET /__control/requests?method=&prefix=` | 上述请求的**原文**（method/path/headers/body） |
| `GET /__control/state` | 任务表 + 故障注入位 |
| `POST /__control/inject` | 注入故障：`create_status` / `create_429_after` / `query_status` / `advance_after` / `cancel_status` |
| `POST /__control/product` | 把某任务的产物置为"抓不到"，验证**转存失败只降级** |
| `POST /__control/reset` | 只清**观测计数**。⚠️ **刻意不清任务表、不重置 seq** —— 否则会引出两个假失败，理由写在代码注释里 |

## 跑法

零计费 E2E（推荐，全程不碰真实上游）：

```bash
# 1) 准备 .env（ADAPTER_KEY / TASK_KEY_FINGERPRINT_SECRET 必填）
cp .env.example .env

# 2) 起 redis + adapter + 假上游；audit 层额外给宿主侧一条回环控制面映射
docker compose -f docker-compose.yml -f docker-compose.audit.yml --profile mock up -d --build

# 3) 跑驱动（默认打 127.0.0.1:8000 与 127.0.0.1:39013）
E2E_BASE=http://127.0.0.1:8000 \
E2E_MOCK_BASE=http://127.0.0.1:39013 \
E2E_ADAPTER_KEY=<.env 里的 ADAPTER_KEY> \
python3 scripts/e2e_zero_cost.py

# 4) 收工
docker compose -f docker-compose.yml -f docker-compose.audit.yml --profile mock down -v
```

单独起假上游（不起适配器）：

```bash
python3 mock_upstream/mock_aivideomaker.py            # 监听 :9000
MOCK_PORT=9111 MOCK_STRICT_MODELS=0 python3 mock_upstream/mock_aivideomaker.py
```

进程内起（测试用，`port=0` 让内核挑空闲端口，互不干扰）：

```python
from mock_upstream.mock_aivideomaker import create_server, KNOWN_MODELS
server, state = create_server(port=0, public_base="http://mock-upstream:9000")
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOCK_PORT` | `9000` | 监听端口 |
| `MOCK_PUBLIC_BASE` | `http://0.0.0.0:<port>` | 写进**产物 URL** 的前缀，必须**适配器可达**（容器里填 `http://mock-upstream:9000`） |
| `MOCK_STRICT_MODELS` | `1` | 模型白名单。置 `0` 才接受任意模型名（**只在排查非模型类问题时用**） |

## 已知边界（有意不做的）

1. **`/api/v1/tasks` 的响应形状是猜测**。官方文档只写了用途，没给 body。
   适配层不依赖它（任务列表由本地任务表提供），故此处按 `{"status","tasks":[…]}` 返回，
   **不构成契约**。
2. **没有 `/api/v1/account` 与 `/api/v1/quote`**：适配器不用它们（那是 `aivideomaker-api` 技能
   用于报价/体检的端点）。要验证那两条线，走技能里的 `avm.py`。
3. **不做并发/超时/断连注入**：需要时用 `/__control/inject` 扩展，或直接用 `docker pause`。
4. **无鉴权**：控制面能读走上游收到的全部请求原文，因此**只允许内网/回环**访问（见 `docker-compose.audit.yml`）。

契约一致性由 `tests/test_mock_upstream.py` 守着：改了路由或语义而没同步，那里会红。
