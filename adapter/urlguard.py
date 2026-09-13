"""出站目标准入。`X-Upstream-Url` 是**调用方可控的出站目标**，必须过 SSRF 校验。"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .errors import channel_error

ALLOWED_SCHEMES = ("http", "https")


def _is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_upstream_url(url: str, *, allow_private: bool = False) -> str:
    """校验并归一化上游地址。

    `allow_private=False` 时拒绝内网/回环/链路本地目标 —— 否则调用方能把本服务
    当跳板去打内网。**解析后逐个 IP 校验**，不只看主机名（域名可解析到内网）。
    """
    raw = str(url or "").strip()
    if not raw:
        raise channel_error("X-Upstream-Url is required")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise channel_error(
            f"X-Upstream-Url must be http(s), got scheme {parts.scheme!r}"
        )
    if not parts.hostname:
        raise channel_error("X-Upstream-Url has no host")
    if allow_private:
        return raw
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise channel_error(f"X-Upstream-Url host cannot be resolved: {parts.hostname} ({exc})") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _is_blocked(ip):
            raise channel_error(
                f"X-Upstream-Url resolves to a non-public address ({address}); "
                "set UPSTREAM_ALLOW_PRIVATE_NETWORK=1 only for trusted deployments"
            )
    return raw
