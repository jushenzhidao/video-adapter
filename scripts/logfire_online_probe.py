"""**线上**校验：把一条真实的上报发到 Logfire，确认链路真的通（会真外发）。

与 `tests/test_observability.py` 的分工：

| | 离线（tests） | 线上（本脚本） |
|---|---|---|
| 出口 | 进程内内存 exporter | 真实的 Logfire 项目 |
| 网络 | 零 | 一次 OTLP 导出 |
| 验的是 | 上报**内容**（字段、原文、打码） | 上报**通路**（token / 网络 / 出口是否接受） |

两件都要做：内容对但发不出去、或发得出去但字段被脱敏器吃掉，都是失败。

跑法（token 从环境变量或 --token-file 读）：

    LOGFIRE_TOKEN="$(cat /tmp/.logfire_token)" \\
      /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python scripts/logfire_online_probe.py

⚠️ 这个脚本**故意会外发**：它发的是一条合成的 `upstream.call`（假凭证、假上游），
用来验证"我们以为发出去的东西"与"服务端真的收下的东西"之间没有断点。
凭证不走真值：脚本用一个**明显是假的** key（`ak_fake_probe_...`），
并且走的是与生产同一条打码路径 —— 你可以在 Logfire 里核验它被打成了 `[redacted …]`。
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapter import observability  # noqa: E402
from adapter.settings import Settings  # noqa: E402

FAKE_CREDENTIAL = "ak_fake_probe_key_do_not_use_" + "0" * 12
PROMPT = "a dog eating a cookie at a secret beach session (online probe)"
MARKER = f"probe-{int(time.time())}"


class ExportFailureCatcher(logging.Handler):
    """捕获导出层的报错。**没有报错**才算这条路走通了。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if "export" in text.lower() or "trace" in text.lower():
            self.messages.append(f"{record.levelname}: {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="把一条真实上报发到 Logfire")
    parser.add_argument("--token", default="", help="LOGFIRE_TOKEN（不给则读环境变量）")
    parser.add_argument("--token-file", default="/tmp/.logfire_token", help="从文件读 token")
    parser.add_argument("--service-name", default="video-adapter-online-probe")
    args = parser.parse_args()

    token = args.token.strip()
    if not token:
        path = pathlib.Path(args.token_file)
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
    if not token:
        print("没有 token ⇒ 拒绝运行（这个脚本的意义就是真外发，不做静默降级）", file=sys.stderr)
        return 2

    catcher = ExportFailureCatcher()
    logging.getLogger().addHandler(catcher)
    logging.getLogger("opentelemetry").setLevel(logging.WARNING)

    settings = Settings.from_env(
        logfire_token=token,
        logfire_service_name=args.service_name,
        environment="probe",
        logfire_console=False,
        obs_report_bodies=True,
    )
    state = observability.setup_observability(settings)
    print(f"装配：ready={state.ready} configured={state.configured} emitting={state.emitting} reason={state.reason!r}")
    if not state.emitting:
        print("emitting=False ⇒ 不会外发，先查 token", file=sys.stderr)
        return 3

    trace = observability.UpstreamTrace(
        phase="create",
        provider="aivideomaker",
        local_id=f"cgt-probe-{MARKER}",
        credential_id="hmac-sha256:probe-fingerprint",
        credential=FAKE_CREDENTIAL,
    )
    request_headers = {
        "key": FAKE_CREDENTIAL,                     # 必须被打码
        "Content-Type": "application/json",
        "X-Probe-Marker": MARKER,
    }
    request_body = {"prompt": PROMPT, "duration": 5, "resolution": 720, "ratio": "16:9"}

    attributes = observability.upstream_request_attributes(
        method="POST",
        url="https://upstream.invalid/api/v1/generate/t2v?api_key=" + FAKE_CREDENTIAL,
        headers=request_headers,
        body=request_body,
        attempt=1,
        idempotent=False,
        trace=trace,
        report_bodies=True,
    )
    print("将要上报的内容（摘要）：")
    preview = dict(attributes)
    print("  " + json.dumps(preview, ensure_ascii=False, indent=2)[:1200])
    assert FAKE_CREDENTIAL not in json.dumps(preview), "本地打码就没过 —— 不要外发"

    with observability.span("upstream.call", secret=FAKE_CREDENTIAL, **attributes) as handle:
        # 假装收到上游应答（不真发上游请求；这个脚本只验上报通路）
        for key, value in observability.upstream_response_attributes(
            status=200,
            headers={"content-type": "application/json", "set-cookie": "session=abc123"},
            body={"status": "SUBMITTED", "taskId": f"ck-{MARKER}"},
            text="",
            duration_ms=42.0,
            credential=FAKE_CREDENTIAL,
            report_bodies=True,
        ).items():
            handle.set_attribute(key, value)

    flushed = observability.flush_spans()
    print(f"\nflush={flushed}  导出层报错条数={len(catcher.messages)}")
    for message in catcher.messages:
        print(f"  {message}")

    print(
        "\n在 Logfire 里按以下两条找这条 span：\n"
        f"  service.name = {args.service_name}\n"
        f"  task.id      = cgt-probe-{MARKER}\n"
        "核验三点：① 正文里的提示词原文活着（没有被换成 [Scrubbed due to …]）；\n"
        "          ② 请求头 key 与 URL 里的凭证是 [redacted …]；\n"
        "          ③ upstream.response.status / task.upstream_id 都在。"
    )
    ok = bool(flushed) and not catcher.messages
    print("\n结论：" + ("线上链路可用" if ok else "线上链路**不可用**（见上面的报错）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
