"""脚本仓库：ref 解析 + manifest 摘要锁定 + 沙箱装载。

ref 形态：`<vendor>/<capability>[@<version>|@<alias>]`，例 `aivideomaker/video@v1`。
省略版本时用 manifest 的 `latest`；`@stable` 这类别名由 manifest 的 `aliases` 展开。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import channel_error
from .sandbox import ScriptSecurityError, load_module
from .settings import Settings

REF_RE = re.compile(
    r"^(?P<vendor>[a-z0-9][a-z0-9_-]*)/(?P<capability>[a-z0-9][a-z0-9_-]*)"
    r"(?:@(?P<version>[A-Za-z0-9][A-Za-z0-9._-]*))?$"
)

_CACHE: dict[tuple[str, str, str], "LoadedScript"] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class LoadedScript:
    ref: str              # 规范化后的 ref，如 aivideomaker/video@v1
    path: Path
    digest: str           # 脚本文件 raw bytes 的 sha256
    namespace: dict


def parse_ref(raw: str) -> tuple[str, str, str | None]:
    match = REF_RE.match(str(raw or "").strip())
    if not match:
        raise channel_error(
            f'X-Script-Ref="{raw}" is malformed; expected `<vendor>/<capability>[@<version>]`'
        )
    return match.group("vendor"), match.group("capability"), match.group("version")


def _manifest(settings: Settings) -> dict:
    path = Path(settings.script_store_dir) / "manifest.json"
    if not path.is_file():
        return {"scripts": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise channel_error(f"script_store/manifest.json is not valid JSON ({exc})") from exc


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve(settings: Settings, ref: str) -> tuple[str, Path, str, str | None]:
    """→ (规范化 ref, 路径, 实际版本, manifest 里登记的摘要或 None)。"""
    vendor, capability, version = parse_ref(ref)
    name = f"{vendor}/{capability}"
    entry = (_manifest(settings).get("scripts") or {}).get(name)
    if version is None:
        if not entry or not entry.get("latest"):
            raise channel_error(
                f'X-Script-Ref="{ref}" omits a version and manifest.json has no "{name}" entry'
            )
        version = str(entry["latest"])
    elif entry and isinstance(entry.get("aliases"), dict):
        version = str(entry["aliases"].get(version, version))

    path = Path(settings.script_store_dir) / vendor / f"{capability}@{version}.py"
    if not path.is_file():
        raise channel_error(f'X-Script-Ref="{ref}" resolved to a missing file: {path}')

    expected = None
    if entry and isinstance(entry.get("digests"), dict):
        registered = entry["digests"].get(version)
        expected = str(registered).lower() if registered else None
    return f"{name}@{version}", path, version, expected


def load(
    settings: Settings, ref: str, *, expected_sha256: str | None = None
) -> LoadedScript:
    """装载脚本。**生产默认强制与 manifest 摘要一致**（`SCRIPT_PIN_MANIFEST_DIGESTS`）。"""
    normalized, path, version, registered = resolve(settings, ref)
    actual = digest(path)

    if settings.pin_manifest_digests and registered is None:
        raise channel_error(
            f'"{normalized}" is not pinned in script_store/manifest.json; '
            "add its sha256 before running with SCRIPT_PIN_MANIFEST_DIGESTS=1"
        )
    if registered and actual != registered:
        raise channel_error(
            f'script "{normalized}" digest mismatch: manifest says {registered}, file is {actual}. '
            "The file changed without the manifest being updated."
        )
    if expected_sha256 and actual != expected_sha256.lower():
        raise channel_error(
            f'X-Script-Sha256 mismatch for "{normalized}": header says {expected_sha256}, '
            f"file is {actual}"
        )

    key = (normalized, actual, str(path))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    try:
        namespace = load_module(path, normalized)
    except ScriptSecurityError as exc:
        raise channel_error(str(exc)) from exc
    loaded = LoadedScript(ref=normalized, path=path, digest=actual, namespace=namespace)
    with _LOCK:
        _CACHE[key] = loaded
    return loaded


def phases(script: LoadedScript) -> tuple[str, ...]:
    """脚本声明的相位。缺失或非法即渠道配置错误。"""
    declared = script.namespace.get("PHASES")
    if not isinstance(declared, (list, tuple)) or not declared:
        raise channel_error(f'script "{script.ref}" must declare a module-level PHASES tuple')
    missing = [p for p in declared if not callable(script.namespace.get(p))]
    if missing:
        raise channel_error(
            f'script "{script.ref}" declares phases it does not implement: {missing}'
        )
    return tuple(str(p) for p in declared)
