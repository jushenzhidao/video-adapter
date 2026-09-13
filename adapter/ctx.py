"""`ctx`：脚本能触达的全部基础设施（沙箱内没有别的通道，见 `sandbox.py`）。"""

from __future__ import annotations

import base64
import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import observability
from .errors import AdapterError

_log = logging.getLogger("video_adapter.script")

_URL_RE = re.compile(r"^https?://", re.I)

#: magic bytes → mime。**按内容判定，不信扩展名或 Content-Type**
#: （既有教训：`.jpg` 结尾、`Content-Type: image/jpg`，实际是 PNG）。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
    (b"\xff\xf2", "audio/mpeg"),
    (b"RIFF", "audio/wav"),
    (b"\xff\xf1", "audio/aac"),
    (b"\xff\xf9", "audio/aac"),
)

_FTYP_MIME = {
    "qt": "video/quicktime",
    "isom": "video/mp4",
    "mp42": "video/mp4",
    "avc1": "video/mp4",
    "iso2": "video/mp4",
    "M4A": "audio/mp4",
    "m4a": "audio/mp4",
    "heic": "image/heic",
    "heif": "image/heif",
    "avif": "image/avif",
}


def sniff_mime(data: bytes) -> str:
    """按 magic bytes 嗅探类型。认不出返回 `application/octet-stream`。"""
    if not data:
        return "application/octet-stream"
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            if magic == b"RIFF" and data[8:12] == b"WEBP":
                return "image/webp"
            if magic == b"RIFF" and data[8:12] == b"WAVE":
                return "audio/wav"
            return mime
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].decode("latin-1", "replace").strip()
        return _FTYP_MIME.get(brand, "video/mp4")
    return "application/octet-stream"


@dataclass
class TaskView:
    """脚本可见的任务视图（架构 §5.2）——**不含凭证**。"""

    upstream_task_id: str = ""
    status: str = ""
    model: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    created_at: int | None = None


class _ScriptObservability:
    """脚本可见的上报入口（架构 §5.2 的 `ctx.logfire`）。

    span 的名字与属性是**对外契约**（§12 的字段表），所以脚本不自己拼名字：
    `ctx.logfire.span("foo")` 落到 `script.foo`，凭证交给打码层，脚本拿不到也不需要拿。
    未装配 logfire 时它同样是可用的（记录照建，落本地 sink 与日志）。
    """

    def __init__(self, credential: str = "") -> None:
        self._credential = credential

    def span(self, name: str, **attributes):
        return observability.span(f"script.{name}", secret=self._credential, **attributes)

    def info(self, message: object, *args: object) -> None:
        _log.info(str(message), *args)


class Context:
    """相位函数的第一个参数。属性可写（脚本会挂自己的 `plan`）。"""

    def __init__(
        self,
        *,
        options: dict[str, Any] | None = None,
        upstream_url: str = "",
        request_id: str = "",
        credential: str = "",
        task: TaskView | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> None:
        self.options = options or {}
        self.upstream_url = upstream_url
        self.request_id = request_id
        self.key = credential          # 兼容既有脚本对 ctx.key 的用法
        self.credential = credential
        self.task = task
        self.upstream_error = upstream_error
        self.logfire = _ScriptObservability(credential)
        self.plan: dict[str, Any] | None = None

    # --- 失败 ---
    def fail(
        self,
        message: str,
        code: str = "InvalidParameter",
        param: str | None = None,
        status: int | None = None,
    ):
        raise AdapterError(str(message), code=code, param=param, status=status)

    # --- 小工具 ---
    @staticmethod
    def is_url(value: Any) -> bool:
        return bool(_URL_RE.match(str(value or "").strip()))

    def data_uri(self, data: bytes, mime: str | None = None) -> str:
        return f"data:{mime or sniff_mime(data)};base64,{self.encode_b64(data)}"

    @staticmethod
    def encode_b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def decode_b64(value: str) -> bytes:
        raw = str(value or "")
        if "," in raw and raw.strip().lower().startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            return base64.b64decode(raw, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"value is not valid base64 ({exc})", code="InvalidParameter") from exc

    sniff_mime = staticmethod(sniff_mime)

    @staticmethod
    def now_epoch() -> int:
        return int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
