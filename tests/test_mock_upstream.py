"""假上游的**契约一致性测试**：`mock_upstream/mock_aivideomaker.py` 是否还等于
`docs/upstreams/aivideomaker-official-api.md` 描述的那个上游。

为什么要单独守它：
    假上游的失败模式不是"跑不起来"，而是**太宽松** —— 被测服务出错了它照样点头，
    于是端到端全绿而缺陷活着。所以"假上游自己的保真度"必须是一等被测对象，
    否则 `scripts/e2e_zero_cost.py` 的全绿没有意义。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_mock_upstream.py

覆盖：8 模型白名单与拒绝信封 / 未知模型可被观测（不是静默 404）/ 关闭白名单 /
Key 归属（异 Key 查·取消一律 404）/ 取消只认 PUT（POST·DELETE 均 405）/
三个链接的形状 / 控制面 count·requests·state·inject·product·reset 语义 /
`reset` 的"只清计数、不动业务状态"反证 / 产物字节与 magic /
**与翻译脚本 `OFFICIAL_MODELS` 的集合一致性**（脚本与假上游漂移 = 白名单形同虚设）。
"""

from __future__ import annotations

import ast
import json
import pathlib
import socket
import sys
import threading
import urllib.error
import urllib.request as urlreq

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mock_upstream.mock_aivideomaker import (  # noqa: E402
    FAKE_MP4,
    KNOWN_MODELS,
    create_server,
)

PUBLIC_BASE = "http://mock-upstream:9000"
SCRIPT_PATH = ROOT / "script_store" / "aivideomaker" / "video@v1.py"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label, str(detail)[:300]))
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    return bool(ok)


# --------------------------------------------------------------------- 传输
class Client:
    """只用标准库（`requests`/`httpx` 会被环境代理改写结论）。"""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None, key: str | None = None):
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["key"] = key
        req = urlreq.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlreq.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, self._parse(raw), resp.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, self._parse(raw), exc.headers

    @staticmethod
    def _parse(raw: bytes):
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def count(self, method: str = "", prefix: str = "") -> int:
        _, payload, _ = self.call("GET", f"/__control/count?method={method}&prefix={prefix}")
        return int(payload["count"])


def official_models_from_script() -> tuple[list[str] | None, str]:
    """从翻译脚本源码里取出 `OFFICIAL_MODELS`。

    脚本与假上游**必须**认同一个集合：脚本负责把调用方的模型名映射成上游模型名，
    假上游负责拒绝非法的上游模型名。两者漂移的话，白名单就形同虚设 ——
    脚本认得的模型假上游拒绝（E2E 假红），或假上游接受的模型脚本不产出（白名单空转）。
    """
    try:
        tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 拿不到就如实失败，不跳过
        return None, f"cannot parse script: {exc}"
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "OFFICIAL_MODELS":
            try:
                return list(ast.literal_eval(node.value)), ""
            except Exception as exc:  # noqa: BLE001
                return None, f"OFFICIAL_MODELS is not a literal: {exc}"
    return None, "OFFICIAL_MODELS not found in script"


def main() -> int:
    server, state = create_server(port=0, public_base=PUBLIC_BASE)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(f"http://127.0.0.1:{state.port}")
    try:
        run_all(client, state)
    finally:
        server.shutdown()
        server.server_close()

    print("\n[宽松模式（MOCK_STRICT_MODELS=0，独立实例）]")
    strict_off_case()

    failed = [label for ok, label, _ in RESULTS if not ok]
    total = len(RESULTS)
    print(f"\n{'=' * 66}\n{total - len(failed)}/{total} passed")
    for label in failed:
        print(f"  FAILED: {label}")
    return 1 if failed else 0


def run_all(c: Client, state) -> None:
    # ------------------------------------------------------------ 模型白名单
    print("\n[模型白名单 —— 官方 §4 的 8 个值]")
    created = {}
    for model in KNOWN_MODELS:
        status, payload, _ = c.call("POST", f"/api/v1/generate/{model}", {"prompt": "x"}, key="k1")
        created[model] = payload
        check(
            status == 200 and payload.get("taskId") and payload.get("status") == "SUBMITTED",
            f"create {model} accepted",
            f"status={status} body={payload}",
        )
    check(len(KNOWN_MODELS) == 8, "白名单恰好 8 个模型", f"got {len(KNOWN_MODELS)}")

    print("\n[三个链接 —— 官方 §2 的创建响应形状]")
    link_task = created["t2v"]
    check(link_task["responseUrl"] == f"{PUBLIC_BASE}/api/v1/tasks/{link_task['taskId']}",
          "responseUrl 指向 PUBLIC_BASE", link_task["responseUrl"])
    check(link_task["statusUrl"].endswith("/status"), "statusUrl 带 /status 后缀", link_task["statusUrl"])
    check(link_task["cancelUrl"].endswith("/cancel"), "cancelUrl 带 /cancel 后缀", link_task["cancelUrl"])

    print("\n[未知名模型 —— 必须是可观测的拒绝，不是静默 404]")
    before = c.count("POST", "/api/v1/generate/")
    status, payload, _ = c.call("POST", "/api/v1/generate/doubao-seedance-9", {"prompt": "x"}, key="k1")
    after = c.count("POST", "/api/v1/generate/")
    check(status == 400, "拒绝码 400", f"got {status}")
    check(payload.get("errorCode") == "INVALID_MODEL", "信封含 INVALID_MODEL", payload)
    check(payload.get("message", "").startswith("Unsupported model:"), "message 点名了模型", payload.get("message"))
    check(after == before + 1, "拒绝的请求**仍被留档**（否则无从排查）", f"{before} -> {after}")

    print("\n[与翻译脚本的模型集合一致性]")
    script_models, err = official_models_from_script()
    check(script_models is not None, f"能从脚本取出 OFFICIAL_MODELS（{SCRIPT_PATH.name}）", err)
    if script_models is not None:
        check(set(script_models) == set(KNOWN_MODELS),
              "脚本 OFFICIAL_MODELS == 假上游 KNOWN_MODELS",
              f"script={sorted(script_models)} mock={sorted(KNOWN_MODELS)}")

    # ------------------------------------------------------------ Key 归属
    print("\n[Key 归属 —— 官方 §2：任务按 Key 归属]")
    tid = created["i2v"]["taskId"]
    for name, path in (("详情", f"/api/v1/tasks/{tid}"), ("状态", f"/api/v1/tasks/{tid}/status")):
        s_same, _, _ = c.call("GET", path, key="k1")
        s_other, _, _ = c.call("GET", path, key="k2")
        check(s_same == 200, f"同 Key 可读{name}", s_same)
        check(s_other == 404, f"异 Key 读{name} → 404（不复现则「跳过指纹校验」测不出来）", s_other)
    s_put, _, _ = c.call("PUT", f"/api/v1/tasks/{tid}/cancel", key="k2")
    check(s_put == 404, "异 Key 取消 → 404", s_put)
    s_none, _, _ = c.call("GET", f"/api/v1/tasks/{tid}")
    check(s_none == 404, "不带 Key → 404", s_none)

    print("\n[任务列表按 Key 过滤]")
    _, list_k1, _ = c.call("GET", "/api/v1/tasks", key="k1")
    _, list_k2, _ = c.call("GET", "/api/v1/tasks", key="k2")
    k1_ids = {t["id"] for t in list_k1.get("tasks", [])}
    check(tid in k1_ids, "k1 能看到自己的任务", sorted(k1_ids))
    check(list_k2.get("tasks") == [], "k2 看不到 k1 的任务", list_k2)

    # ------------------------------------------------------------ 取消方法
    print("\n[取消只认 PUT —— 官方 §2]")
    s_del, _, _ = c.call("DELETE", f"/api/v1/tasks/{tid}/cancel", key="k1")
    check(s_del == 405, "DELETE 取消 → 405", s_del)
    s_post, _, _ = c.call("POST", f"/api/v1/tasks/{tid}/cancel", key="k1")
    check(s_post == 405, "POST 取消 → 405（不是 404：路径在、方法不对）", s_post)
    s_ok, body_ok, _ = c.call("PUT", f"/api/v1/tasks/{tid}/cancel", key="k1")
    check(s_ok == 200 and body_ok.get("status") == "CANCEL", "PUT 取消 → CANCEL", body_ok)

    # ------------------------------------------------------------ 状态与产物
    print("\n[状态推进与产物]")
    query_tid = created["t2v_v3"]["taskId"]
    _, inj, _ = c.call("POST", "/__control/inject", {"advance_after": 3})
    check(inj["inject"]["advance_after"] == 3, "inject 生效", inj)
    seen = []
    for _ in range(3):
        _, st_body, _ = c.call("GET", f"/api/v1/tasks/{query_tid}", key="k1")
        seen.append(st_body["status"])
    check(seen == ["PROGRESS", "PROGRESS", "COMPLETED"], "第 3 次查询起 COMPLETED", seen)
    _, detail, _ = c.call("GET", f"/api/v1/tasks/{query_tid}", key="k1")
    check(detail.get("output", {}).get("url", "").startswith(PUBLIC_BASE + "/media/"),
          "产物 URL 用 PUBLIC_BASE（适配器视角可达）", detail.get("output"))
    check(detail.get("completedAt"), "COMPLETED 时带 completedAt", detail.get("completedAt"))

    print("\n[故障注入：产物抓不到]")
    _, prod, _ = c.call("POST", "/__control/product", {"task_id": query_tid, "bad": True})
    check(prod.get("bad_product") is True, "product 注入生效", prod)
    _, detail_bad, _ = c.call("GET", f"/api/v1/tasks/{query_tid}", key="k1")
    check("missing-" in detail_bad["output"]["url"], "产物 URL 变为不可达", detail_bad["output"])
    s_media_bad, _, _ = c.call("GET", f"/media/missing-{query_tid}.mp4", key="k1")
    check(s_media_bad == 404, "抓不到的产物 → 404", s_media_bad)

    print("\n[产物字节]")
    s_media, blob, headers = c.call("GET", f"/media/{query_tid}.mp4", key="k1")
    check(s_media == 200, "已知产物 → 200", s_media)
    check(headers.get("Content-Type") == "video/mp4", "Content-Type 是 video/mp4", headers.get("Content-Type"))
    check(len(blob) == len(FAKE_MP4) == 140, "字节数与驱动脚本的断言一致", f"{len(blob)} vs {len(FAKE_MP4)}")
    check(blob[4:12] == b"ftypmp42", "magic 正确（ftypmp42）", blob[:12])

    # ------------------------------------------------------------ 控制面
    print("\n[控制面：计数与留档]")
    gen_count = c.count("POST", "/api/v1/generate/")
    check(gen_count == 9, "count 精确统计创建请求（8 合法 + 1 拒绝）", gen_count)
    _, reqs, _ = c.call("GET", "/__control/requests?method=POST&prefix=/api/v1/generate/t2v")
    check(reqs["count"] >= 1 and reqs["items"][0]["headers"].get("key") == "k1",
          "requests 留档含 headers（凭证位置可核）", reqs["count"])
    check(isinstance(reqs["items"][0]["body"], dict), "requests 留档含 body 原文", type(reqs["items"][0]["body"]).__name__)

    print("\n[控制面：reset 只清计数、不动业务状态（反证）]")
    _, before_state, _ = c.call("GET", "/__control/state")
    seq_before = before_state["seq"]
    max_task_before = max(int(t[2:]) for t in before_state["tasks"])
    check(seq_before > max_task_before,
          "seq 大于最大任务号（被拒绝的模型名也消耗 seq —— 外部不要用 max(task_id) 猜它）",
          f"seq={seq_before} max_task={max_task_before}")
    _, out, _ = c.call("POST", "/__control/reset", {})
    check(out.get("reset") is True, "reset 返回成功", out)
    check(c.count("POST", "/api/v1/generate/") == 0, "计数已归零", c.count("POST", "/api/v1/generate/"))
    check(out.get("tasks_kept") == len(before_state["tasks"]), "任务表**未**被清（否则取消会误得 404）", out)
    check(out.get("seq_kept") == seq_before, "seq **未**重置（否则 task_id 重复 ⇒ 转存幂等命中）", f"{out} vs {seq_before}")
    s_old, _, _ = c.call("GET", f"/api/v1/tasks/{tid}", key="k1")
    check(s_old == 200, "旧任务 reset 后仍可读（真凭据还在）", s_old)
    s_new, new_body, _ = c.call("POST", "/api/v1/generate/t2v", {"prompt": "x"}, key="k1")
    check(int(new_body["taskId"][2:]) > seq_before, "新任务 id 严格大于旧 id（产物 URL 不重复）",
          f"{new_body['taskId']} vs seq {seq_before}")

    print("\n[控制面：429 注入]")
    _, _, _ = c.call("POST", "/__control/inject", {"create_429_after": 1})
    c.call("POST", "/api/v1/generate/t2v", {"prompt": "x"}, key="k1")
    s_429, body_429, headers_429 = c.call("POST", "/api/v1/generate/t2v", {"prompt": "x"}, key="k1")
    check(s_429 == 429, "超过阈值后 429", s_429)
    check(headers_429.get("Retry-After") == "2", "429 带 Retry-After（契约要求的退避提示）",
          headers_429.get("Retry-After"))
    check(body_429.get("status") == "FAILED", "429 信封与官方失败信封同形", body_429)

    print("\n[控制面：strict 开关]")
    _, inj_off, _ = c.call("POST", "/__control/inject", {"create_status": None})
    check(inj_off["inject"]["create_status"] is None, "create_status 已复位", inj_off)

    print("\n[healthz]")
    s_hz, body_hz, _ = c.call("GET", "/__healthz")
    check(s_hz == 200 and body_hz.get("ok") is True, "健康探针可用", body_hz)
    check(set(body_hz.get("models", [])) == set(KNOWN_MODELS), "healthz 暴露模型清单", body_hz.get("models"))


def strict_off_case() -> None:
    """`MOCK_STRICT_MODELS=0` 时才接受任意模型名（排查非模型类问题用）。"""
    server, state = create_server(port=0, strict_models=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = Client(f"http://127.0.0.1:{state.port}")
        s, body, _ = c.call("POST", "/api/v1/generate/whatever", {"prompt": "x"}, key="k")
        check(s == 200 and body.get("taskId"), "宽松模式接受任意模型名", f"{s} {body}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
