"""渠道契约：请求头 → `ChannelConfig`。服务端**不持有**渠道/模型/计费知识（D1）。

11 个头的名字与语义与 image-adapter 完全一致，只有两处来源被策略关闭：
`X-Script` / `X-Script-64`（内联脚本）在本部署被**拒绝** —— 降级报告承担语义判断，
必须可 review、可追溯、随镜像发版。
"""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import AdapterError, channel_error
from .settings import Settings
from .urlguard import check_upstream_url

PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

#: 调用方凭证可能出现的头（按优先级）。
_CREDENTIAL_HEADERS = ("authorization", "x-api-key", "api-key")


@dataclass
class ChannelConfig:
    upstream_url: str
    script_ref: str
    credential: str
    provider: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    upstream_method: str = "POST"
    auth_emit: str | None = None
    authorization_header: str | None = None
    script_sha256: str | None = None

    @property
    def max_concurrency(self) -> int | None:
        value = self.options.get("max_concurrency")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _header(headers: Mapping[str, str], name: str) -> str:
    for key in (name, name.lower(), name.title()):
        value = headers.get(key)
        if value:
            return str(value).strip()
    return ""


def extract_credential(headers: Mapping[str, str]) -> tuple[str, str | None]:
    """取调用方凭证 → (裸值, 原始 Authorization 头)。

    `Bearer ` 前缀在裸值里去掉（`X-Auth-Emit: header:key:` 要的是裸 key）；
    原始头留给"没有 X-Auth-Emit"的渠道原样转发。
    """
    raw = _header(headers, "authorization")
    if raw:
        if raw.lower().startswith("bearer "):
            return raw[7:].strip(), raw
        return raw, raw
    for name in _CREDENTIAL_HEADERS[1:]:
        value = _header(headers, name)
        if value:
            return value, None
    return "", None


def parse_channel(headers: Mapping[str, str], settings: Settings) -> ChannelConfig:
    # --- 准入 ---
    presented = _header(headers, "x-adapter-key")
    if not settings.adapter_key:
        raise AdapterError(
            "this deployment has no ADAPTER_KEY configured; every request is refused",
            code="AuthenticationError",
            status=401,
        )
    if not presented or not hmac.compare_digest(presented, settings.adapter_key):
        raise AdapterError("invalid or missing X-Adapter-Key", code="AuthenticationError", status=401)

    # --- 脚本来源（D2：只认 ref）---
    if _header(headers, "x-script") or _header(headers, "x-script-64"):
        raise channel_error(
            "inline scripts (X-Script / X-Script-64) are not accepted by this deployment; "
            "use X-Script-Ref naming a script pinned in the project"
        )
    script_ref = _header(headers, "x-script-ref")
    if not script_ref:
        raise channel_error("X-Script-Ref is required")

    # --- 上游地址 ---
    upstream_url = check_upstream_url(
        _header(headers, "x-upstream-url"),
        allow_private=settings.upstream_allow_private_network,
    )

    # --- 渠道选项 ---
    raw_options = _header(headers, "x-channel-options")
    options: dict[str, Any] = {}
    if raw_options:
        try:
            parsed = json.loads(raw_options)
        except json.JSONDecodeError as exc:
            raise channel_error(f"X-Channel-Options is not valid JSON ({exc})") from exc
        if not isinstance(parsed, dict):
            raise channel_error("X-Channel-Options must be a JSON object")
        options = parsed

    provider = options.get("provider")
    provider = str(provider).strip() if provider not in (None, "") else None
    if provider and not PROVIDER_RE.match(provider):
        raise channel_error(
            f'X-Channel-Options.provider="{provider}" must match {PROVIDER_RE.pattern}'
        )

    credential, authorization = extract_credential(headers)
    script_sha256 = _header(headers, "x-script-sha256") or None
    if script_sha256:
        script_sha256 = script_sha256.lower()

    return ChannelConfig(
        upstream_url=upstream_url,
        script_ref=script_ref,
        credential=credential,
        provider=provider,
        options=options,
        upstream_method=(_header(headers, "x-upstream-method") or "POST").upper(),
        auth_emit=_header(headers, "x-auth-emit") or None,
        authorization_header=authorization,
        script_sha256=script_sha256,
    )


def build_auth_headers(channel: ChannelConfig) -> dict[str, str]:
    """按 `X-Auth-Emit` 决定凭证怎么发。空则原样转发 `Authorization`。

    `X-Auth-Emit` 形态：`header:<名字>:<前缀>`，前缀可为空。
    例：`header:key:` → `key: <凭证>`；`header:Authorization:Bearer ` → `Authorization: Bearer <凭证>`。
    """
    emit = (channel.auth_emit or "").strip()
    if not emit:
        return {"Authorization": channel.authorization_header} if channel.authorization_header else {}
    parts = emit.split(":", 2)
    if parts[0].lower() != "header" or len(parts) < 2 or not parts[1].strip():
        raise channel_error(
            f'X-Auth-Emit="{emit}" is malformed; expected "header:<name>:<prefix>"'
        )
    name = parts[1].strip()
    prefix = parts[2] if len(parts) > 2 else ""
    value = f"{prefix}{channel.credential}"
    if name.lower() == "authorization" and not prefix:
        # 大多数上游要 `Bearer `；没写前缀时补上，避免发一个裸 token
        value = f"Bearer {channel.credential}"
    return {name: value}
