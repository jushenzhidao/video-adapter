"""产物转存（rehost）：把上游产物落到自有存储，对外给稳定地址。

**开关**：`X-Channel-Options.rehost = true`（逐渠道，默认 false）。
默认关闭的理由：上游产物若是**公开且有效期与原生语义一致**（aivideomaker 是公开 24h URL），
透传即可，多一跳只增加失败面。开启的场景是「产物需鉴权」或「有效期短于调用方预期」。

**为什么转存在引擎而不是脚本里做**：它不需要任何上游语义知识
（下载一个 URL、存下来、换个地址），而且能避免为了它给沙箱脚本开网络能力。

存储后端：

    local（默认）  写 `MEDIA_DIR`，由本服务 `GET /files/<name>` 对外提供 —— 零依赖、开箱可用
    s3             留作扩展点：实现同样的 `store()` 语义即可（见下方协议说明）

对象名是**产物 URL 的哈希**（`sha256(url)[:24] + 扩展名`）⇒ 同一个产物重复转存是**幂等**的，
不会因为多次查询而堆副本。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import AdapterError
from .settings import Settings

log = logging.getLogger("video_adapter.media")

#: 只允许哈希名 + 已知扩展名 —— 挡住 `../` 之类的路径穿越。
_NAME_RE = re.compile(r"^[a-f0-9]{16,64}\.[a-z0-9]{1,5}$")

_EXT_BY_MIME = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-flv": ".flv",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
}

_MIME_BY_EXT = {v: k for k, v in _EXT_BY_MIME.items()}
_MIME_BY_EXT[".jpeg"] = "image/jpeg"


def extension_for(url: str, mime: str = "") -> str:
    """扩展名优先取自内容类型，其次取自 URL 路径。"""
    ext = _EXT_BY_MIME.get((mime or "").split(";")[0].strip().lower())
    if ext:
        return ext
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 5 else ".bin"


def object_name(url: str, mime: str = "") -> str:
    """产物的存储名。**由 URL 决定 ⇒ 重复转存幂等。**"""
    return f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}{extension_for(url, mime)}"


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(str(name or "")))


def local_path(name: str, settings: Settings) -> Path:
    """校验并定位本地存储里的对象。**名字不合法一律拒绝**（路径穿越防线）。"""
    if not is_valid_name(name):
        raise AdapterError("invalid media name", code="InvalidEndpoint.NotFound", status=404)
    path = Path(settings.media_dir) / name
    if not path.is_file():
        raise AdapterError(f"media {name} is not stored here", code="InvalidEndpoint.NotFound", status=404)
    return path


def content_type_for(name: str) -> str:
    return _MIME_BY_EXT.get(Path(name).suffix.lower(), "application/octet-stream")


async def fetch(url: str, settings: Settings) -> tuple[bytes, str]:
    """取回产物字节。**带大小上限**，防止一个超大文件把内存打满。"""
    limit = settings.rehost_max_bytes
    async with httpx.AsyncClient(
        timeout=settings.rehost_timeout_seconds,
        trust_env=settings.upstream_trust_env,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise AdapterError(
                    f"product download returned HTTP {response.status_code}",
                    code="InternalServiceError",
                    status=502,
                )
            mime = (response.headers.get("content-type") or "").split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise AdapterError(
                        f"product exceeds REHOST_MAX_BYTES ({limit} bytes); refusing to buffer it",
                        code="InternalServiceError",
                        status=502,
                    )
                chunks.append(chunk)
    return b"".join(chunks), mime


def store(name: str, data: bytes, settings: Settings) -> Path:
    """落盘。**先写 `.part` 再原子改名** —— 另一半读到的要么没有、要么是完整文件。"""
    root = Path(settings.media_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    staging = target.with_name(target.name + ".part")
    staging.write_bytes(data)
    staging.replace(target)
    return target


def public_url(name: str, base: str) -> str:
    """对外地址。`base` 来自 `PUBLIC_BASE_URL`，或创建任务时捕获的请求 Host。"""
    prefix = (base or "").rstrip("/")
    return f"{prefix}/files/{name}" if prefix else f"/files/{name}"


async def rehost(url: str, *, settings: Settings, base: str) -> dict:
    """把上游产物转存到自有存储。返回可直接写进任务记录的字典。

    失败**不抛给调用方**：转存是增强，不该让一个已经成功的任务看起来失败。
    调用方拿到的是 `ok=False` + `error`，上层把它写进 `warnings`。
    """
    if not url:
        return {"ok": False, "error": "upstream produced no product url"}
    name = object_name(url)
    target = Path(settings.media_dir) / name
    if target.is_file():
        # 已经存过（同一产物被多次查询）—— 不重复下载
        return {
            "ok": True,
            "reused": True,
            "object": name,
            "url": public_url(name, base),
            "upstream_url": url,
            "bytes": target.stat().st_size,
            "content_type": content_type_for(name),
        }
    try:
        data, mime = await fetch(url, settings)
        final_name = object_name(url, mime) if mime else name
        store(final_name, data, settings)
    except Exception as exc:  # noqa: BLE001 - 转存失败只降级，不改变任务结果
        log.warning("rehost failed for %s: %s", url, exc)
        return {"ok": False, "error": str(exc), "upstream_url": url}
    return {
        "ok": True,
        "reused": False,
        "object": final_name,
        "url": public_url(final_name, base),
        "upstream_url": url,
        "bytes": len(data),
        "content_type": content_type_for(final_name),
    }
