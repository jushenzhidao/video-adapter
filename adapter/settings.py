"""数据面策略。**不含任何渠道配置** —— 渠道知识全部来自请求头（架构 D1）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    """运行期策略。全部可由环境变量覆盖，测试里用 `replace()` 覆写。"""

    # --- 准入 ---
    adapter_key: str = ""

    # --- 安全：脚本来源 ---
    allow_inline_script: bool = False          # 本项目默认关（架构 D2 / ADR-005）
    allow_remote_script: bool = False
    pin_manifest_digests: bool = True
    script_store_dir: str = str(REPO_ROOT / "script_store")

    # --- 安全：出站 ---
    upstream_allow_private_network: bool = False
    upstream_trust_env: bool = False           # 默认**不信**环境代理：macOS scutil 代理会把回环也代理走

    # --- 任务持久化（架构 §6.2）---
    task_store: str = "sqlite"                 # sqlite | memory | redis
    task_store_path: str = str(REPO_ROOT / ".tasks.sqlite3")
    task_store_url: str = ""                   # redis://…
    task_retention_days: int = 7
    task_key_fingerprint_secret: str = ""      # 缺失 → 退化 sha256 + 启动告警（§6.3b / ADR-003）

    # --- 素材转存（rehost；由渠道选项 `rehost` 逐渠道开启，见 §8.2）---
    #   ⚠️ 默认存储后端是**本地目录 + 由本服务对外提供**（零依赖、开箱可用）。
    #   S3/MinIO 后端留作扩展点（见 media.py 的 MediaStore 协议）。
    media_dir: str = str(REPO_ROOT / ".media")
    public_base_url: str = ""                  # 对外地址前缀；空则按请求的 Host 推导
    rehost_max_bytes: int = 512 * 1024 * 1024
    rehost_timeout_seconds: float = 120.0

    # --- 并发与超时 ---
    default_max_concurrency: int = 4
    queue_wait_seconds: float = 20.0
    request_timeout_seconds: float = 60.0
    upstream_retry_attempts: int = 3

    # --- 请求体 ---
    body_limit_bytes: int = 64 * 1024 * 1024   # 与 Seedance 契约一致

    # --- 协调器（默认关，§6.5）---
    reconciler_enabled: bool = False
    reconciler_interval_seconds: float = 15.0

    # --- 可观测（架构 §12）---
    log_level: str = "INFO"
    logfire_token: str = ""
    logfire_service_name: str = "video-adapter"
    environment: str = ""
    logfire_console: bool = False              # 本地看 span 用；生产关
    #: true 直接**拒绝装配**：宽头抓取会把 X-Adapter-Key / Authorization /
    #: X-Auth-Emit 指向的那个头（渠道自选名字）一起抓走，固定名字表不可能覆盖并集。
    logfire_capture_headers: bool = False
    #: 上游 request / response 原文是否上报（含 body；凭证在源头打码，见 observability.py）
    obs_report_bodies: bool = True
    obs_body_max_chars: int = 20_000

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        base = cls(
            adapter_key=_text("ADAPTER_KEY"),
            allow_inline_script=_flag("ALLOW_INLINE_SCRIPT", False),
            allow_remote_script=_flag("ALLOW_REMOTE_SCRIPT", False),
            pin_manifest_digests=_flag("SCRIPT_PIN_MANIFEST_DIGESTS", True),
            script_store_dir=_text("SCRIPT_STORE_DIR", str(REPO_ROOT / "script_store")),
            upstream_allow_private_network=_flag("UPSTREAM_ALLOW_PRIVATE_NETWORK", False),
            upstream_trust_env=_flag("UPSTREAM_TRUST_ENV", False),
            task_store=_text("TASK_STORE", "sqlite"),
            task_store_path=_text("TASK_STORE_PATH", str(REPO_ROOT / ".tasks.sqlite3")),
            task_store_url=_text("TASK_STORE_URL"),
            task_retention_days=_num("TASK_RETENTION_DAYS", 7),
            task_key_fingerprint_secret=_text("TASK_KEY_FINGERPRINT_SECRET"),
            media_dir=_text("MEDIA_DIR", str(REPO_ROOT / ".media")),
            public_base_url=_text("PUBLIC_BASE_URL"),
            rehost_max_bytes=_num("REHOST_MAX_BYTES", 512 * 1024 * 1024),
            rehost_timeout_seconds=float(_num("REHOST_TIMEOUT_SECONDS", 120)),
            default_max_concurrency=_num("DEFAULT_MAX_CONCURRENCY", 4),
            queue_wait_seconds=float(_num("QUEUE_WAIT_SECONDS", 20)),
            request_timeout_seconds=float(_num("REQUEST_TIMEOUT_SECONDS", 60)),
            upstream_retry_attempts=_num("UPSTREAM_RETRY_ATTEMPTS", 3),
            body_limit_bytes=_num("BODY_LIMIT_BYTES", 64 * 1024 * 1024),
            reconciler_enabled=_flag("RECONCILER_ENABLED", False),
            reconciler_interval_seconds=float(_num("RECONCILER_INTERVAL_SECONDS", 15)),
            log_level=_text("LOG_LEVEL", "INFO"),
            logfire_token=_text("LOGFIRE_TOKEN"),
            logfire_service_name=_text("LOGFIRE_SERVICE_NAME", "video-adapter"),
            environment=_text("ENVIRONMENT"),
            logfire_console=_flag("LOGFIRE_CONSOLE", False),
            logfire_capture_headers=_flag("LOGFIRE_CAPTURE_HEADERS", False),
            obs_report_bodies=_flag("OBS_REPORT_BODIES", True),
            obs_body_max_chars=_num("OBS_BODY_MAX_CHARS", 20_000),
        )
        return replace(base, **overrides) if overrides else base

    @property
    def fingerprint_algorithm(self) -> str:
        return "hmac-sha256" if self.task_key_fingerprint_secret else "sha256"
