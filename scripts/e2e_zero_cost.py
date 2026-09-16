#!/usr/bin/env python3
"""制品层端到端驱动（**零计费**）：打一个**真的跑起来的**服务，上游是 `mock_upstream/`。

与 `tests/test_engine.py` 的分工
------------------------------
`test_engine.py` 在自己的进程里用 `ASGITransport` 打自己的 app —— 它验证的是**引擎语义**。
本脚本打的是**制品**（镜像 + compose + 入口），验证的是"装起来还对不对"。两者不可互替：
进程内测试永远发现不了"镜像里少拷了一个目录""compose 少传了一个环境变量"这类问题。

零成本怎么保证
--------------
上游地址由请求头 `X-Upstream-Url` 决定，指向**同 compose 网络里的假上游**；
本脚本**不读也不使用任何真实上游的地址与 Key**。

断言落在哪
----------
每条断言尽量落到**假上游实际收到的请求**上（`/__control/*`），而不是被测服务的自述。
"服务声称做了什么"与"它真的发了什么"是两件事 —— 后者才是适配层的产出。

跑法
----
    docker compose -f docker-compose.yml -f docker-compose.audit.yml --profile mock up -d --build
    E2E_ADAPTER_KEY=<.env 的 ADAPTER_KEY> python3 scripts/e2e_zero_cost.py
    docker compose -f docker-compose.yml -f docker-compose.audit.yml --profile mock down -v

环境变量（默认值适配上面的 compose）：

    E2E_BASE         默认 http://127.0.0.1:8000    被测服务（宿主视角）
    E2E_MOCK_BASE    默认 http://127.0.0.1:39013   假上游控制面（audit 层映射）
    E2E_ADAPTER_KEY  **必填**，与 .env 的 ADAPTER_KEY 一致
    E2E_UPSTREAM_URL 默认 http://mock-upstream:9000 适配器视角的上游地址（容器名）
    E2E_RESULTS      默认 /tmp/video-adapter-e2e-results.json

只依赖标准库：`requests` / `httpx` 会被环境代理改写结论（本机实测过 `HTTP_PROXY` 把
回环也代理走、返回网关错误体而非 connection refused）。
"""

from __future__ import annotations

import http.client
import json
import os
import pathlib
import sys
import time
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 与假上游**共用**同一份常量：产物字节、模型白名单都不在两处各写一遍。
from mock_upstream.mock_aivideomaker import FAKE_MP4, KNOWN_MODELS  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8000")
MOCK = os.environ.get("E2E_MOCK_BASE", "http://127.0.0.1:39013")
UPSTREAM_URL = os.environ.get("E2E_UPSTREAM_URL", "http://mock-upstream:9000")
RESULTS_PATH = os.environ.get("E2E_RESULTS", "/tmp/video-adapter-e2e-results.json")
#: 单次 HTTP 超时。默认 40s 对本地栈绰绰有余；但在**同时跑着十几个其它容器**的机器上，
#: Docker Desktop 的端口转发/DNS 会偶发被拖慢到几十秒 ⇒ 那时调大它，别把环境抖动
#: 误读成适配器缺陷（本脚本曾在一次 27 分钟的构建后踩到：适配器 610ms 就答了 404，
#: 而客户端 40s 没收到 —— 请求/响应根本没走通，不是服务端的问题）。
TIMEOUT = float(os.environ.get("E2E_TIMEOUT", "40"))

ADAPTER_KEY = os.environ.get("E2E_ADAPTER_KEY", "")
UPSTREAM_KEY_A = "ak_e2e_key_A_0001"
UPSTREAM_KEY_B = "ak_e2e_key_B_0002"
TASKS = "/api/v3/contents/generations/tasks"
SCRIPT_REF = "aivideomaker/video@v1"
FAKE_MP4_LEN = len(FAKE_MP4)
FAKE_MP4_MAGIC = b"ftypmp42"

OPTS_T2V = json.dumps({"provider": "aivideomaker", "max_credits": 20, "max_concurrency": 4})
OPTS_DYN = json.dumps({"provider": "aivideomaker", "max_credits": 5000, "allow_unpriced": True})

RESULTS: list[dict] = []
SECTION = {"name": ""}


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append({"section": SECTION["name"], "ok": bool(ok), "label": label,
                    "detail": str(detail)[:400]})
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    return bool(ok)


def note(label: str, detail: str = "") -> None:
    RESULTS.append({"section": SECTION["name"], "ok": True, "label": label,
                    "detail": str(detail)[:400], "note": True})
    print(f"  --    {label}" + (f"   [{detail}]" if detail else ""))


def section(name: str) -> None:
    SECTION["name"] = name
    print(f"\n[{name}]")


# ---------------------------------------------------------------- HTTP client
def call_raw(method: str, path: str, headers: dict | None = None, body=None, base: str = BASE):
    """原始返回：二进制响应（如产物下载）不能用 json 解析。"""
    p = urlsplit(base)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=TIMEOUT)
    hdrs = {k: v for k, v in (headers or {}).items() if v is not None}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    raw = r.read()
    status, resp_headers = r.status, {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return status, resp_headers, raw


def call(method: str, path: str, headers: dict | None = None, body=None, base: str = BASE):
    status, resp_headers, raw = call_raw(method, path, headers, body, base)
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw.decode("utf-8", "replace")
    return status, resp_headers, parsed


def channel(upstream_url=UPSTREAM_URL, credential=UPSTREAM_KEY_A, options=OPTS_T2V,
            script_ref=SCRIPT_REF, adapter_key=ADAPTER_KEY, **extra) -> dict:
    h = {
        "X-Adapter-Key": adapter_key,
        "X-Upstream-Url": upstream_url,
        "X-Script-Ref": script_ref,
        "X-Auth-Emit": "header:key:",
        "X-Channel-Options": options,
        "Authorization": f"Bearer {credential}",
    }
    h.update({k.replace("_", "-"): v for k, v in extra.items()})
    return h


def body_t2v(text="一只猫在打哈欠", **rest):
    b = {"model": "aivideomaker/t2v",
         "content": [{"type": "text", "text": text}],
         "duration": 5, "resolution": "720p", "ratio": "16:9"}
    b.update(rest)
    return b


# ---------------------------------------------------------------- mock 控制
def mock_ctl(path: str, method: str = "GET", payload=None):
    return call(method, path, base=MOCK, body=payload)


def mock_count(m: str, prefix: str) -> int:
    """prefix 是**完整路径前缀**（如 /api/v1/tasks/），不是末段。"""
    _, _, j = mock_ctl(f"/__control/count?method={m}&prefix={prefix}")
    return int(j["count"]) if isinstance(j, dict) else -1


def mock_reset() -> None:
    mock_ctl("/__control/reset", "POST")


def mock_inject(**kw) -> None:
    mock_ctl("/__control/inject", "POST", kw)


#: **原生响应字段集**（`docs/seedance-api-reference.md` §4.1）。
#: 这里**刻意硬编码**而不是 import `adapter.seedance`：E2E 是独立验收，
#: 引用被测代码自己的常量等于把断言写成同义反复（实现改了、断言跟着改，永远绿）。
NATIVE_TASK_KEYS = frozenset(
    {
        "id", "status", "content", "error", "usage", "created_at", "updated_at",
        "seed", "resolution", "ratio", "duration", "frames", "framespersecond",
        "service_tier", "execution_expires_after", "generate_audio", "draft", "priority",
    }
)


def newest_upstream_task() -> str:
    """从**假上游自己**取最近创建的上游 task id。

    响应体里已没有 `upstream_task_id`（原生契约只给原生字段）⇒ 需要它时从上游侧取。
    这顺带证明一件事：对调用方隐藏上游 id 之后，"和上游对工单"仍然做得到，
    只是改从上游侧对 —— 那本来也是对账的正确方向。
    """
    _, _, j = mock_ctl("/__control/state")
    ids = sorted((j.get("tasks") or {}).keys()) if isinstance(j, dict) else []
    return ids[-1] if ids else ""


def manifest_digest() -> str:
    """从 `script_store/manifest.json` 取 `aivideomaker/video@v1` 的 sha256。

    **不硬编码**：硬编码的摘要会在脚本改版后与 manifest 漂移，
    于是"摘要一致"这条断言会变成一句永远为真的废话。
    """
    path = ROOT / "script_store" / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["scripts"]["aivideomaker/video"]
    return entry["digests"][entry["latest"]]


# ---------------------------------------------------------------- 闸门辅助
def queue_channels() -> dict:
    _, _, j = call("GET", "/healthz")
    return ((j.get("queue") or {}).get("channels") or {}) if isinstance(j, dict) else {}


def inflight_holders() -> int:
    return sum(int(v.get("active") or 0) for v in queue_channels().values())


def wait_terminal(task_id: str, timeout=40.0, options=OPTS_T2V):
    """跨过查询缓存窗口（2s）轮询直到终态。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, _, j = call("GET", f"{TASKS}/{task_id}", channel(options=options))
        last = j
        if isinstance(j, dict) and j.get("status") in ("succeeded", "failed", "expired"):
            return j
        time.sleep(2.2)
    return last


def drain_inflight(options) -> tuple[int, list[str]]:
    """撤销本凭证下所有未终态任务，回收并发槽位。

    ⚠️ 两个必须守住的顺序/判据（都是实测踩出来的）：
      1. **必须在 mock_reset 之前做**。取消要过上游确认（PUT /cancel）；若先把上游的
         任务记录清掉，取消会拿到 404 ⇒ 适配层**刻意不释放槽位**（"上游任务可能还在跑"）
         ⇒ 清理形同没做，后面的闸门用例必然假失败。
      2. **必须检查返回码**。只看"尝试了几个"会把失败当成功。
    """
    _, _, lst = call("GET", f"{TASKS}?page_num=1&page_size=100", channel(options=options))
    items = (lst.get("items") or []) if isinstance(lst, dict) else []
    ok, errors = 0, []
    for item in items:
        if item.get("status") in ("queued", "running"):
            st, _, j = call("DELETE", f"{TASKS}/{item['id']}", channel(options=options))
            if st == 200:
                ok += 1
            else:
                errors.append(f"{item['id']} HTTP{st} {json.dumps(j, ensure_ascii=False)[:60]}")
    return ok, errors


# ================================================================ 用例
def main() -> int:
    if not ADAPTER_KEY:
        print("E2E_ADAPTER_KEY is required (must equal the ADAPTER_KEY in .env)")
        return 2
    print(f"target = {BASE}   (mock audit = {MOCK}, upstream url = {UPSTREAM_URL})")

    # ---------------------------------------------------------------- A 健康
    section("A. 健康与制品一致性")
    st, _, j = call("GET", "/healthz")
    check(st == 200, f"/healthz → 200（实得 {st}）")
    version = j.get("version") if isinstance(j, dict) else None
    check(bool(version), f"/healthz 报告镜像版本（实得 {version!r}）")
    if version == "dev":
        note("version == dev（源码直跑或未注入 APP_VERSION；制品应为 x.y.z）")
    check(j.get("task_store") == "redis", f"task_store == redis（实得 {j.get('task_store')}）")
    rl = j.get("rate_limit") or {}
    check(rl.get("scope") == "shared", f"限流桶 scope == shared（实得 {rl.get('scope')}）")
    check((rl.get("backend") or {}).get("ok") is True, "限流后端 backend.ok == true")
    check(j.get("credential_fingerprint") == "hmac-sha256",
          f"凭证指纹算法 == hmac-sha256（实得 {j.get('credential_fingerprint')}）")
    note(f"logfire.emitting = {(j.get('logfire') or {}).get('emitting')}"
         "（本脚本不要求它外发：测试流量不该进生产项目）")

    # 冷启动前置：闸门槽位是**进程内**状态（"占到终态"），而任务记录在 redis 里活 7 天。
    # 两个生命周期不同 ⇒ 上一轮遗留的、**从未被查询过**的任务会一直占着槽位。
    holders = inflight_holders()
    note(f"槽位占用 = {holders}（若 > 0 且闸门用例失败，先重启 adapter 清空进程内闸门）")
    mock_reset()

    # ---------------------------------------------------------------- B 准入
    section("B. 准入（X-Adapter-Key）")
    st, _, j = call("POST", TASKS, channel(adapter_key=None), body_t2v())
    check(st == 401, f"不带 X-Adapter-Key → 401（实得 {st}）")
    check((j.get("error") or {}).get("code") == "AuthenticationError",
          f"error.code == AuthenticationError（实得 {(j.get('error') or {}).get('code')}）")
    st, _, j = call("POST", TASKS, channel(adapter_key="ak_wrong_key"), body_t2v())
    check(st == 401, f"错误 X-Adapter-Key → 401（实得 {st}）")
    check(mock_count("POST", "/api/v1/generate") == 0, "★ 准入失败时上游零请求（非真空对照）")

    # ---------------------------------------------------------------- C 渠道头
    section("C. 渠道头契约")
    st, _, j = call("POST", TASKS, channel(upstream_url=None), body_t2v())
    check(st == 400, f"缺 X-Upstream-Url → 400（实得 {st}）")
    st, _, j = call("POST", TASKS, channel(script_ref=None), body_t2v())
    check(st == 400, f"缺 X-Script-Ref → 400（实得 {st}）")
    st, _, j = call("POST", TASKS, channel(**{"X-Script": "def hi(): pass"}), body_t2v())
    check(st == 400 and "inline" in json.dumps(j).lower(),
          f"内联脚本 X-Script 被拒（ADR-005）（实得 {st}）")
    st, _, j = call("POST", TASKS, channel(script_ref="aivideomaker/video@v99"), body_t2v())
    check(st != 200, f"不存在的 script ref 被拒（实得 {st}）")
    st, _, j = call("POST", TASKS, channel(upstream_url="/relative/path"), body_t2v())
    check(st == 400, f"相对 X-Upstream-Url → 400（实得 {st}）")
    st, _, j = call("POST", TASKS, channel(options="{not json"), body_t2v())
    check(st == 400, f"X-Channel-Options 非法 JSON → 400（实得 {st}）")
    check(mock_count("POST", "/api/v1/generate") == 0, "★ 渠道头失败时上游零请求（非真空对照）")

    # ---------------------------------------------------------------- D dry-run
    section("D. dry-run（零消耗，验证将要发出的原文）")
    before = mock_count("POST", "/api/v1/generate")
    st, _, j = call("POST", TASKS, {**channel(), "X-Dry-Run": "1"}, body_t2v())
    check(st == 200, f"dry-run → 200（实得 {st}）")
    check(j.get("dry_run") is True, "响应 dry_run=true")
    ub = ((j.get("upstream") or {}).get("body") or {})
    check(ub.get("prompt") == "一只猫在打哈欠", f"上游 body.prompt 正确（实得 {ub.get('prompt')!r}）")
    check(ub.get("duration") == "5", f"t2v 的 duration 是**字符串** '5'（实得 {ub.get('duration')!r}）")
    check(ub.get("aspectRatio") == "16:9", f"比例字段名是 aspectRatio（实得 {sorted(ub.keys())}）")
    check(mock_count("POST", "/api/v1/generate") == before, "★ dry-run 上游零请求（非真空对照）")

    # ---------------------------------------------------------------- E 创建
    section("E. 创建（**只回 id** —— 原生契约）")
    mock_reset()
    st, _, j = call("POST", TASKS, channel(), body_t2v())
    check(st == 200, f"创建 → 200（实得 {st}）")
    cid = j.get("id") if isinstance(j, dict) else None
    check(bool(cid) and str(cid).startswith("cgt-"), f"id 形如 cgt-…（实得 {cid}）")
    # 🔴 原生契约（`seedance-api-reference.md` §3.3）：创建成功体**只有 `id`**。
    # 没有 status，也**没有任何上报块** —— 上游 task id / 脚本摘要 / 请求响应留档
    # 全部改走 logfire（适配层 `task.snapshot` span）。
    check(set(j) == {"id"} if isinstance(j, dict) else False,
          f"★ 创建响应**逐键只有 id**（实得 {sorted(j) if isinstance(j, dict) else j}）")
    note(f"脚本摘要（manifest: {manifest_digest()[:16]}…）不再回显；"
         "装载期的强校验（SCRIPT_PIN_MANIFEST_DIGESTS）才是它的一致性地基："
         "摘要不符时**每个请求**都会 400，本脚本根本走不到这里")

    _, _, mj = mock_ctl("/__control/requests?method=POST&prefix=/api/v1/generate")
    items = (mj.get("items") or []) if isinstance(mj, dict) else []
    check(len(items) == 1, f"上游恰好收到 1 次创建（实得 {len(items)}）")
    if items:
        got = items[0]
        check(got["path"] == "/api/v1/generate/t2v", f"上游路径 = /api/v1/generate/t2v（实得 {got['path']}）")
        check("key" in got["headers"],
              f"★ 凭证按 X-Auth-Emit 发在 **key** 头（实得头名 {sorted(got['headers'])[:6]}…）")
        check(got["headers"].get("key") == UPSTREAM_KEY_A,
              f"key 头是**裸值**（无 Bearer 前缀）（实得 {str(got['headers'].get('key'))[:18]}…）")
        check("authorization" not in got["headers"], "★ 没有多余的 Authorization 头（凭证只发在 key 头）")
        gb = got.get("body") or {}
        check(gb.get("prompt") == "一只猫在打哈欠" and gb.get("duration") == "5"
              and gb.get("aspectRatio") == "16:9",
              f"上游 body 是干净投影（实得 {json.dumps(gb, ensure_ascii=False)[:110]}）")

    call("POST", TASKS, channel(), body_t2v("一只猫打哈欠 --rs 720p --dur 5"))
    _, _, mj = mock_ctl("/__control/requests?method=POST&prefix=/api/v1/generate")
    last_body = ((mj.get("items") or [{}])[-1]).get("body") or {}
    check("--rs" not in str(last_body.get("prompt")),
          f"★ 弱校验后缀已剥离，上游收到干净 prompt（实得 {last_body.get('prompt')!r}）")

    # ---------------------------------------------------------------- F 查询
    section("F. 查询（降频、凭证绑定、终态本地作答）")
    mock_reset()
    st, _, created = call("POST", TASKS, channel(), body_t2v())
    tid = created.get("id")
    check(st == 200 and bool(tid), "前置：创建成功")

    st, _, q1 = call("GET", f"{TASKS}/{tid}", channel())
    check(st == 200, f"查询 → 200（实得 {st}）")
    check(q1.get("status") in ("queued", "running", "succeeded"),
          f"返回六态 status（实得 {q1.get('status')}）")
    check("content" in q1 and "video_url" in (q1.get("content") or {}), "契约字段 content.video_url 存在")
    check("usage" in q1, "契约字段 usage 存在")
    # 逐键等于原生字段集：**不多**（诊断块已移除）、**不少**（原生字段齐全）
    diff = sorted(set(q1) ^ NATIVE_TASK_KEYS)
    check(not diff, f"★ 查询体逐键等于原生字段集（差异 {diff}）")
    check("model" not in q1, "★ 响应体不含 model（上游模型名很乱，已从契约里删除）")
    n_after_first = mock_count("GET", "/api/v1/tasks/")

    for _ in range(3):
        call("GET", f"{TASKS}/{tid}", channel())
    n_after_burst = mock_count("GET", "/api/v1/tasks/")
    check(n_after_burst == n_after_first,
          f"★ 查询缓存生效：窗口内 3 次重复查询，上游请求数不变（{n_after_first} → {n_after_burst}）")

    time.sleep(2.4)
    call("GET", f"{TASKS}/{tid}", channel())
    check(mock_count("GET", "/api/v1/tasks/") == n_after_burst + 1,
          "★ 跨过缓存窗口后上游恰好 +1（既不是 0 也不是 3）")

    final = wait_terminal(tid)
    check(final.get("status") == "succeeded", f"最终 status == succeeded（实得 {final.get('status')}）")
    check(bool((final.get("content") or {}).get("video_url")),
          f"终态带回 video_url（{str((final.get('content') or {}).get('video_url'))[:60]}）")
    n_terminal = mock_count("GET", "/api/v1/tasks/")
    call("GET", f"{TASKS}/{tid}", channel())
    check(mock_count("GET", "/api/v1/tasks/") == n_terminal,
          "★ 已终态再查**不打上游**（本地作答，7 天窗口内可用）")

    before_x = mock_count("GET", "/api/v1/tasks/")
    st, _, j = call("GET", f"{TASKS}/{tid}", channel(credential=UPSTREAM_KEY_B))
    check(st == 404, f"★ 换钥匙查 → 404（实得 {st}）")
    check((j.get("error") or {}).get("code") == "InvalidEndpoint.NotFound",
          f"错误码与「不存在」同码，不泄露归属（实得 {(j.get('error') or {}).get('code')}）")
    check(mock_count("GET", "/api/v1/tasks/") == before_x,
          "★ 凭证不符时**本地直接 404**，不带错钥匙去问上游（上游零查询）")

    # ---------------------------------------------------------------- G 列表
    section("G. 列表（只看得见本凭证的任务）")
    st, _, lst = call("GET", f"{TASKS}?page_num=1&page_size=50", channel())
    check(st == 200 and "items" in lst and "total" in lst,
          f"列表形状 {{items,total}}（实得 {sorted(lst.keys()) if isinstance(lst, dict) else lst}）")
    mine = {i.get("id") for i in (lst.get("items") or [])}
    check(tid in mine, "自己的任务在列表里")
    st, _, lst_b = call("GET", f"{TASKS}?page_num=1&page_size=50", channel(credential=UPSTREAM_KEY_B))
    other = {i.get("id") for i in ((lst_b.get("items") or []) if isinstance(lst_b, dict) else [])}
    check(tid not in other, "★ 另一把钥匙的列表里**看不到**本任务（按 (provider, 凭证指纹) 过滤）")

    # ---------------------------------------------------------------- H 取消/删除
    section("H. 取消与删除")
    mock_reset()
    st, _, c2 = call("POST", TASKS, channel(), body_t2v())
    tid2 = c2.get("id")
    st, _, j = call("DELETE", f"{TASKS}/{tid2}", channel())
    check(st == 200 and j.get("status") == "cancelled",
          f"queued 任务取消 → cancelled（实得 {st}/{j.get('status')}）")
    n_cancel = mock_count("PUT", "/api/v1/tasks/")
    check(n_cancel == 1, f"★ 取消**用 PUT** 打到上游（实得 {n_cancel} 次）")
    st, _, j = call("DELETE", f"{TASKS}/{tid}", channel())
    check(st == 200 and j.get("deleted") is True, f"终态任务 DELETE → 删除本地记录（实得 {st}/{j}）")
    st, _, j = call("GET", f"{TASKS}/{tid}", channel())
    check(st == 404, f"删除后再查 → 404（实得 {st}）")

    # ---------------------------------------------------------------- I 计费闸门
    section("I. 支出上限（本地强制，上游无前闸门）")
    mock_reset()
    st, _, j = call("POST", TASKS, channel(options=json.dumps({"provider": "aivideomaker"})), body_t2v())
    check(st == 400, f"缺 max_credits → 400（实得 {st}）")
    check("max_credits" in json.dumps(j), f"param 指向 max_credits（实得 {json.dumps(j, ensure_ascii=False)[:130]}）")
    st, _, j = call("POST", TASKS,
                    channel(options=json.dumps({"provider": "aivideomaker", "max_credits": 3})), body_t2v())
    check(st == 400, f"估算 15 > 上限 3 → 400（实得 {st}）")
    check(mock_count("POST", "/api/v1/generate") == 0, "★ 两道闸门都在发出请求**之前**（上游零请求）")
    st, _, j = call("POST", TASKS,
                    channel(options=json.dumps({"provider": "aivideomaker", "max_credits": 5000})),
                    body_t2v(model="aivideomaker/seedance20"))
    check(st == 400, f"动态计价且未 opt-in → 400（实得 {st}）")
    st, _, j = call("POST", TASKS, channel(options=OPTS_DYN), body_t2v(model="aivideomaker/seedance20"))
    check(st == 200, f"显式 allow_unpriced 后放行（实得 {st}）")

    section("I2. 模型路由断言（防误路由到别的上游并计费）")
    st, _, j = call("POST", TASKS, channel(), body_t2v(model="someoneelse/t2v"))
    check(st == 400, f"provider 与渠道声明不一致 → 400（实得 {st}）")
    check("provider" in json.dumps(j) or "model" in json.dumps(j), "param 指向 model/provider")
    st, _, j = call("POST", TASKS, channel(), body_t2v(model="aivideomaker/nonexistent_model"))
    check(st == 400, f"未知模型 → 400 且不落默认上游（实得 {st}）")
    check(mock_count("POST", "/api/v1/generate") == 1,
          "★ 上述两类失败都没有打到上游（上游创建请求数仍为 1）")
    note(f"假上游白名单与脚本同集合：{len(KNOWN_MODELS)} 个模型"
         f"（契约一致性由 tests/test_mock_upstream.py 守）")

    # --- 模型映射：**渠道可配置**，默认透传 ---------------------------------
    # 这个开关的值是控制面塞在 `X-Channel-Options` 头里的 JSON，经 channel 解析后
    # 才成为脚本的 `ctx.options` ⇒ **只有端到端能验**"开关真的送到了脚本"
    # （单测里 options 是直接传进去的，实现对了但没人传也永远是绿的）。
    section("I3. 模型映射（渠道可配置；默认透传，不猜不兜底）")
    mock_reset()
    opts_map = json.dumps({
        "provider": "aivideomaker", "max_credits": 20, "max_concurrency": 4,
        "model_map": {"doubao-seedance-2-0-260128": "t2v"},
    })
    st, _, j = call("POST", TASKS, channel(options=opts_map),
                    body_t2v(model="doubao-seedance-2-0-260128"))
    check(st == 200, f"配了 model_map 后原生 ID 可用（实得 {st}）")
    _, _, mj = mock_ctl("/__control/requests?method=POST&prefix=/api/v1/generate")
    sent = [it.get("path") for it in (mj.get("items") or [])]
    check(sent == ["/api/v1/generate/t2v"],
          f"★ 映射真的落到了上游请求上（实得 {sent}）")
    # 差分对的另一半：**同一个原生 ID**，在没配映射表的渠道上必须 400。
    # 这一条防的是"默认偷偷兜底到某个槽位"——那正是 7.3 倍账单的来源。
    st, _, j = call("POST", TASKS, channel(), body_t2v(model="doubao-seedance-2-0-260128"))
    check(st == 400, f"未配映射表时同一个原生 ID → 400（实得 {st}）")
    check("model_map" in json.dumps(j), "错误里点名 model_map 是配置入口")

    # ---------------------------------------------------------------- J 转存
    section("J. 素材转存（rehost，逐渠道开关）")
    mock_reset()
    opts_rehost = json.dumps({"provider": "aivideomaker", "max_credits": 5000,
                              "allow_unpriced": True, "rehost": True})
    st, _, c3 = call("POST", TASKS, channel(options=opts_rehost), body_t2v())
    tid3 = c3.get("id")
    fin = wait_terminal(tid3, options=opts_rehost)
    vurl = str((fin.get("content") or {}).get("video_url"))
    check(fin.get("status") == "succeeded", f"开启 rehost 后任务成功（实得 {fin.get('status')}）")
    check("mock-upstream" not in vurl, f"★ video_url 已换成本服务自有地址（实得 {vurl[:70]}）")
    check(bool((fin.get("rehost") or {}).get("upstream_url")),
          "上游原始地址保留在 rehost.upstream_url 里（可回溯）")
    n_dl = mock_count("GET", "/media/")
    check(n_dl >= 1, f"★ 产物真被下载过（上游 /media 被请求 {n_dl} 次）")
    path = urlsplit(vurl).path or vurl
    st, hdrs, blob = call_raw("GET", path)
    check(st == 200, f"GET {path} → 200（自有地址可下载，实得 {st}）")
    check(isinstance(blob, bytes) and len(blob) == FAKE_MP4_LEN and blob[4:12] == FAKE_MP4_MAGIC,
          f"★ 落盘字节与上游产物逐字节一致（len={len(blob) if isinstance(blob, bytes) else 'n/a'}"
          f"，magic={blob[4:12] if isinstance(blob, bytes) else 'n/a'}）")
    st, _, j = call("GET", "/files/..%2f..%2fetc%2fpasswd")
    check(st in (400, 404), f"/files 拒绝路径穿越（实得 {st}）")

    n_before = mock_count("GET", "/media/")
    call("GET", f"{TASKS}/{tid3}", channel(options=opts_rehost))
    check(mock_count("GET", "/media/") == n_before, "★ 转存幂等（重复查询不重复下载产物）")

    section("J2. 转存失败只降级（不改变任务结果）")
    mock_reset()
    st, _, c4 = call("POST", TASKS, channel(options=opts_rehost), body_t2v())
    tid4 = c4.get("id")
    mock_ctl("/__control/product", "POST", {"task_id": newest_upstream_task(), "bad": True})
    fin4 = wait_terminal(tid4, options=opts_rehost)
    check(fin4.get("status") == "succeeded", f"★ 产物抓不到时任务仍 succeeded（实得 {fin4.get('status')}）")

    # ---------------------------------------------------------------- K 错误映射
    section("K. 上游错误映射")
    mock_reset()
    mock_inject(create_status=500, create_error_body={"status": "FAILED", "message": "upstream boom"})
    st, _, j = call("POST", TASKS, channel(), body_t2v())
    check(st == 502, f"上游 5xx → 502（实得 {st}）")
    check((j.get("error") or {}).get("code") == "InternalServiceError",
          f"error.code == InternalServiceError（实得 {(j.get('error') or {}).get('code')}）")

    mock_reset()
    st, _, j = call("POST", TASKS, channel(upstream_url="http://mock-upstream:9999"), body_t2v())
    check(st == 502, f"上游不可达 → 502（实得 {st}）")
    check((j.get("error") or {}).get("code") == "UpstreamUnavailable",
          f"★ error.code == UpstreamUnavailable（与 5xx 分开）（实得 {(j.get('error') or {}).get('code')}）")

    mock_reset()
    mock_inject(create_status=200, create_error_body={"status": "FAILED", "message": "Insufficient credits"})
    st, _, j = call("POST", TASKS, channel(), body_t2v())
    check(st == 429, f"上游积分不足 → 429（实得 {st}）")

    # ---------------------------------------------------------------- L 并发闸门
    section("L. 本地并发闸门")
    opts_c1 = json.dumps({"provider": "aivideomaker", "max_credits": 5000,
                          "allow_unpriced": True, "max_concurrency": 1})
    # ⚠️ 顺序关键：**先取消（要过上游确认），再 reset 假上游**。
    #    反过来会让取消拿到上游 404 ⇒ 适配层刻意不释放槽位 ⇒ 闸门用例假失败。
    drained, errors = drain_inflight(opts_c1)
    note(f"闸门前置清理：成功撤销 {drained} 个未终态任务" + (f"；失败 {errors}" if errors else ""))
    check(not errors, f"清理无失败（失败项：{errors}）")
    mock_reset()
    mock_inject(create_status=None, create_error_body=None, advance_after=999)
    holders = inflight_holders()
    check(holders == 0, f"清理后槽位归零（实得 {holders}）")

    st1, _, cA = call("POST", TASKS, channel(options=opts_c1), body_t2v())
    st2, _, jB = call("POST", TASKS, channel(options=opts_c1), body_t2v())
    check(st1 == 200, f"第一条占住唯一槽位（实得 {st1}）")
    check(st2 == 429, f"★ 槽位满 → 429（实得 {st2}）")
    check((jB.get("error") or {}).get("code") == "ServerOverloaded",
          f"error.code == ServerOverloaded（实得 {(jB.get('error') or {}).get('code')}）")
    if st1 == 200:
        call("DELETE", f"{TASKS}/{cA.get('id')}", channel(options=opts_c1))
    check(inflight_holders() == 0, f"取消后槽位释放（实得 {inflight_holders()}）")

    # ---------------------------------------------------------------- M 请求关联
    section("M. 响应头与请求关联")
    st, hdrs, _ = call("GET", "/healthz", {"x-request-id": "req-e2e-0001"})
    check(hdrs.get("x-request-id") == "req-e2e-0001", "x-request-id 按调用方给的值回显")

    # ---------------------------------------------------------------- 汇总
    total = len([r for r in RESULTS if not r.get("note")])
    passed = len([r for r in RESULTS if not r.get("note") and r["ok"]])
    failed = [r for r in RESULTS if not r.get("note") and not r["ok"]]
    print(f"\n==== 制品层结果：{passed}/{total} 通过（零计费）====")
    for r in failed:
        print(f"  FAIL [{r['section']}] {r['label']}  {r['detail']}")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"target": BASE, "upstream_url": UPSTREAM_URL, "total": total,
                   "passed": passed, "failed": len(failed), "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"results -> {RESULTS_PATH}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
