"""前门归一化：**先于脚本**。脚本侧永远看不到未归一化的输入（架构 §3）。

三件事：

1. **弱校验后缀**：调用方可能按官方"弱校验模式"把参数内联在提示词尾部
   （`--rs 720p --rt 16:9 --dur 5 --seed 11 --cf false --wm true`）。
   不剥离就会把 `--rs 720p` 当提示词发给上游，**画面里可能真的出现这些文字**。
   优先级：**显式结构化字段 > 内联后缀**（与官方"常规方式优先"一致）。
2. **`content[]` 的 `role` 位置**：官方放在 content 项顶层；有兼容网关写进
   `image_url` 对象内部。两种都收，出口只按官方形态（调用方原样看到的也是顶层）。
3. **首尾帧隐式语义**：2 张无 `role` 的图 + 有文本 ⇒ 按顺序 = 首帧、尾帧。
   显式归一化，免得这条隐式规则在上游侧失效。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 弱校验后缀的词表：简称与全称都认。
_INLINE_KEYS: dict[str, str] = {
    "rs": "resolution",
    "resolution": "resolution",
    "rt": "ratio",
    "ratio": "ratio",
    "dur": "duration",
    "duration": "duration",
    "seed": "seed",
    "cf": "camera_fixed",
    "camerafixed": "camera_fixed",
    "camera_fixed": "camera_fixed",
    "wm": "watermark",
    "watermark": "watermark",
}

_BOOL_KEYS = ("camera_fixed", "watermark")
_INT_KEYS = ("duration", "seed")


def _coerce(key: str, value: str) -> Any:
    if key in _BOOL_KEYS:
        return value.strip().lower() in ("1", "true", "yes", "on", "t", "y")
    if key in _INT_KEYS:
        try:
            return int(float(value))
        except ValueError:
            return value  # 让下游按自己的语义拒绝，别在这里编一个数
    return value


def strip_inline_params(text: str) -> tuple[str, dict[str, Any], list[str]]:
    """从提示词尾部剥离 `--name value` 组。返回 (干净文本, 覆盖项, warnings)。

    只从**尾部**向前扫，遇到不认识的 `--xxx` 就停 —— 正文里出现的 `--` 不动它，
    否则会把用户提示词里的破折号吃掉。右值优先（重复出现时取最靠右的）。
    """
    tokens = str(text or "").split()
    overrides: dict[str, Any] = {}
    warnings: list[str] = []

    index = len(tokens)
    while index >= 2:
        name_token = tokens[index - 2]
        if not name_token.startswith("--") or len(name_token) < 3:
            break
        key = _INLINE_KEYS.get(name_token[2:].lower())
        if key is None:
            warnings.append(
                f'unknown inline parameter "{name_token}" was left in the prompt'
            )
            break
        value = tokens[index - 1]
        if value.startswith("--"):
            warnings.append(f'inline parameter "{name_token}" has no value; left in the prompt')
            break
        overrides.setdefault(key, _coerce(key, value))
        index -= 2

    return " ".join(tokens[:index]).strip(), overrides, warnings


def _role_of(item: Mapping[str, Any]) -> tuple[str, Any, Mapping | None]:
    """取 (role, url, holder)。role 兼容两种位置。"""
    role = str(item.get("role") or "").strip()
    holder = None
    url = None
    for key in ("image_url", "video_url", "audio_url"):
        candidate = item.get(key)
        if isinstance(candidate, Mapping):
            holder = candidate
            if candidate.get("url"):
                url = candidate["url"]
                break
    if not role and isinstance(holder, Mapping):
        role = str(holder.get("role") or "").strip()
    return role, url, holder


def normalize_content(items: Any) -> tuple[list, list[str]]:
    """`content[]` 归一化：role 提到项顶层 + 首尾帧隐式语义显式化。"""
    warnings: list[str] = []
    if not isinstance(items, list):
        return items, warnings

    normalized: list[dict] = []
    for item in items:
        if not isinstance(item, Mapping):
            normalized.append(item)  # 交给脚本报 unsupported
            continue
        role, url, holder = _role_of(item)
        out = dict(item)
        if isinstance(holder, Mapping) and role and "role" in holder:
            # 出口只按官方形态：role 在项顶层，holder 内不再重复
            out[holder_key(item)] = {k: v for k, v in holder.items() if k != "role"}
        if role:
            out["role"] = role
        normalized.append(out)

    # 隐式首尾帧：2 张无 role 的图 + 至少一条非空文本
    images = [
        i for i in normalized
        if isinstance(i, Mapping) and str(i.get("type")) == "image_url" and not i.get("role")
    ]
    has_text = any(
        isinstance(i, Mapping)
        and str(i.get("type")) == "text"
        and str(i.get("text") or "").strip()
        for i in normalized
    )
    if len(images) == 2 and has_text:
        for item, role in zip(images, ("first_frame", "last_frame")):
            item["role"] = role
        warnings.append(
            "two images without a role and a text prompt were read as first_frame + last_frame (order is meaning)"
        )
    return normalized, warnings


def holder_key(item: Mapping[str, Any]) -> str:
    """该 content 项承载 URL 的键名。"""
    for key in ("image_url", "video_url", "audio_url"):
        if isinstance(item.get(key), Mapping):
            return key
    return "image_url"


def normalize_payload(payload: Any) -> tuple[dict, list[str]]:
    """把归一化的两件事一次做完，返回 (归一化后的请求体, warnings)。

    **必须在脚本之前调用**（架构 §3）—— 脚本侧永远看不到未归一化的输入：
    带 `--rs 720p` 的污染提示词、`role` 藏在 `image_url` 对象里、首尾帧靠隐式顺序。
    """
    if not isinstance(payload, Mapping):
        return payload, []
    out = dict(payload)
    warnings: list[str] = []

    items, content_warnings = normalize_content(out.get("content"))
    warnings.extend(content_warnings)

    if isinstance(items, list):
        inline: dict[str, Any] = {}
        kept: list = []
        for item in items:
            if isinstance(item, Mapping) and str(item.get("type")) == "text" and item.get("text"):
                clean, overrides, item_warnings = strip_inline_params(str(item["text"]))
                warnings.extend(item_warnings)
                for key, value in overrides.items():
                    inline.setdefault(key, value)
                if not clean:
                    warnings.append(
                        "a text item contained nothing but inline parameters and was dropped"
                    )
                    continue
                updated = dict(item)
                updated["text"] = clean
                kept.append(updated)
            else:
                kept.append(item)
        out["content"] = kept

        # 优先级：显式结构化字段 > 内联后缀（与官方"常规方式优先"一致）
        applied = []
        for key, value in inline.items():
            if out.get(key) in (None, ""):
                out[key] = value
                applied.append(key)
        ignored = sorted(set(inline) - set(applied))
        if applied:
            warnings.append(f"inline parameters applied: {', '.join(sorted(applied))}")
        if ignored:
            warnings.append(
                f"inline parameters ignored because the structured field was also set: "
                f"{', '.join(ignored)}"
            )
    return out, warnings
