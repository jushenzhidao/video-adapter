"""senseaudio/video@v1 的测试：**翻译层** + **引擎级端到端**。零消耗：零网络、零生成请求。

跑法（不需要 pytest）：

    /Users/betterme/.workbuddy/binaries/python/envs/video-adapter/bin/python tests/test_senseaudio_video_v1.py

两层，缺一不可（`docs/03_引擎架构.md` §14 的教训：**"实现了" ≠ "接线了"**）：

  A. **翻译层**（`FakeCtx`，纯函数）—— 快、定位准，但只断言"函数的返回值"；
  B. **引擎级**（本地假 SenseAudio 上游 + `ASGITransport`，手动跑 lifespan）——
     断言**真正发出去的 body 与 URL**，以及凭证绑定、原生响应形状、无取消相位的 DELETE。

覆盖 `docs/adapter-playbook.md` §9 里**属于脚本职责**的部分；引擎自身的并发闸门 / 持久化 /
回调 / 转存由 `tests/test_engine.py` 负责。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import importlib.util
import json
import pathlib
import socket
import sys
import threading
import urllib.parse

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapter import observability  # noqa: E402
from adapter.main import create_app  # noqa: E402
from adapter.seedance import NATIVE_TASK_KEYS  # noqa: E402
from adapter.settings import Settings  # noqa: E402

SCRIPT = ROOT / "script_store" / "senseaudio" / "video@v1.py"
SCRIPT_STORE = str(ROOT / "script_store")
SCRIPT_REF = "senseaudio/video@v1"
MODEL = "senseaudio/doubao-seedance-2-0-260128"
BARE_MODEL = "doubao-seedance-2-0-260128"
ADAPTER_KEY = "adapter-test-key"
UPSTREAM_KEY = "sk_senseaudio_test_key"

#: 上游不公布价格 ⇒ 默认必须显式接受"不可验证成本"才放行（ADR-004）。
OPTS = json.dumps({"provider": "senseaudio", "max_credits": 5000, "allow_unpriced": True})

DATA_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
REF_IMG = "https://cdn.example.com/ref1.png"
REF_IMG2 = "https://cdn.example.com/ref2.png"
REF_MP4 = "https://cdn.example.com/ref.mp4"
REF_MP3 = "https://cdn.example.com/ref.mp3"


# =============================================================================
# A. 翻译层
# =============================================================================

def _load():
    spec = importlib.util.spec_from_file_location("senseaudio_video_v1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sa = _load()


class Failure(Exception):
    def __init__(self, message, code=None, param=None, status=None, retry_after=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.param = param
        self.status = status
        self.retry_after = retry_after


class FakeCtx:
    """假 ctx：只实现脚本用到的成员。`fail` 必抛，与真引擎一致。

    `upstream_error` 只在错误相位里有值（引擎在调 `<phase>_error` 前填、出来后清空）。
    """

    def __init__(
        self,
        options=None,
        upstream_url="https://api.senseaudio.cn",
        task=None,
        upstream_error=None,
    ):
        self.options = options or {}
        self.upstream_url = upstream_url
        self.task = task
        self.upstream_error = upstream_error
        self.plan = None

    def fail(self, message, code=None, param=None, status=None, retry_after=None):
        raise Failure(message, code=code, param=param, status=status, retry_after=retry_after)


class FakeTask:
    def __init__(self, upstream_task_id="c4666acd-60fa-4e1e-8c8b-72ae129f7a4d", status="queued"):
        self.upstream_task_id = upstream_task_id
        self.status = status


def opts(**rest):
    base = {"provider": "senseaudio", "max_credits": 5000, "allow_unpriced": True}
    base.update(rest)
    return base


def body(content, **rest):
    payload = {
        "model": MODEL,
        "content": content,
        "duration": 6,
        "resolution": "720p",
        "ratio": "16:9",
    }
    payload.update(rest)
    return payload


def text(value):
    return {"type": "text", "text": value}


def image(url, role=None):
    item = {"type": "image_url", "image_url": {"url": url}}
    if role:
        item["role"] = role
    return item


def video(url):
    return {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}


def audio(url):
    return {"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"}


def plan(payload, options=None, ctx=None):
    ctx = ctx or FakeCtx(options if options is not None else opts())
    result = asyncio.run(sa.create_request(ctx, payload))
    return result, ctx


def expect_failure(payload, options=None, upstream_url=None):
    ctx = FakeCtx(options if options is not None else opts(), upstream_url or "https://api.senseaudio.cn")
    try:
        asyncio.run(sa.create_request(ctx, payload))
    except Failure as exc:
        return exc
    raise AssertionError(f"expected a failure, got a plan: {payload}")


# ---- 1. 请求形状 -----------------------------------------------------------

def test_minimal_text_to_video_body():
    """最小文生视频：字段集、类型、URL 都要逐项对（**断言将要发出的 body**）。"""
    result, ctx = plan(body([text("a cat yawning")]))
    assert result["method"] == "POST"
    assert result["url"] == "https://api.senseaudio.cn/v1/video/create"
    assert result["body"] == {
        "model": BARE_MODEL,                       # provider 前缀被剥掉，值本身逐字透传
        "content": [{"type": "text", "text": "a cat yawning"}],
        "duration": 6,
        "resolution": "720p",
        "ratio": "16:9",
        "watermark": False,                        # 🔴 原生默认 false，必须显式覆盖上游的 true
        "provider_specific": {"generate_audio": True},   # 🔴 上游只认嵌套；默认开声音
    }
    assert "timeout" not in result["body"]         # 没给 execution_expires_after ⇒ 不发
    assert "generate_audio" not in result["body"]  # 扁平形态上游不吃，**绝不发**（发了就没声音）


def test_content_shape_is_rewritten_to_upstream_form():
    """`image_url.url` → `url`、`type=image_url` → `image`、`reference_image` → `reference`。"""
    result, _ = plan(body([
        text("p"), image(DATA_PNG, "reference_image"), video(REF_MP4), audio(REF_MP3),
    ]))
    assert result["body"]["content"] == [
        {"type": "text", "text": "p"},
        {"type": "image", "url": DATA_PNG, "role": "reference"},     # 平铺 url + role 改名
        {"type": "video", "video_url": REF_MP4},                     # ⚠️ 上游用 video_url / audio_url
        {"type": "audio", "audio_url": REF_MP3},
    ]


def test_media_order_is_preserved():
    """🔴 媒体顺序一字不动 —— 提示词里的 `@图像3` 靠它对上号。

    刻意用"图、视频、图"这种交错顺序：任何按类型分组的实现都会把两张图并到一起，
    从而改变编号所指。
    """
    result, _ = plan(body([
        text("参考@图像1 @视频1 @图像2"),
        image(REF_IMG),
        video(REF_MP4),
        image(REF_IMG2),
    ]))
    kinds = [(c["type"], c.get("url") or c.get("video_url")) for c in result["body"]["content"][1:]]
    assert kinds == [
        ("image", REF_IMG),
        ("video", REF_MP4),
        ("image", REF_IMG2),
    ], kinds


def test_first_and_last_frame_are_kept():
    result, _ = plan(body([image(DATA_PNG, "first_frame"), image(REF_IMG, "last_frame")]))
    assert result["body"]["content"] == [
        {"type": "image", "url": DATA_PNG, "role": "first_frame"},
        {"type": "image", "url": REF_IMG, "role": "last_frame"},
    ]


def test_unroled_image_becomes_reference():
    result, _ = plan(body([text("p"), image(REF_IMG)]))
    assert result["body"]["content"][1] == {"type": "image", "url": REF_IMG, "role": "reference"}


def test_multiple_texts_are_merged_in_order():
    """上游只吃一条 text ⇒ 合并，**不能只取最后一条**（那会丢掉前一句）。"""
    result, ctx = plan(body([text("第一句"), text("第二句")]))
    assert result["body"]["content"][0] == {"type": "text", "text": "第一句\n第二句"}
    assert any("merged into a single prompt" in w for w in ctx.plan["warnings"]), ctx.plan["warnings"]


# ---- 2. 能力边界：一律 400，不静默丢 ---------------------------------------

def test_frames_and_references_cannot_be_mixed():
    exc = expect_failure(body([
        image(DATA_PNG, "first_frame"), image(REF_IMG, "reference_image"),
    ]))
    assert exc.code == "InvalidParameter" and exc.param == "content"
    assert "mutually exclusive" in exc.message


def test_last_frame_without_first_frame_is_rejected():
    exc = expect_failure(body([image(REF_IMG, "last_frame")]))
    assert exc.status == 400 and "requires a first frame" in exc.message


def test_audio_alone_is_rejected():
    exc = expect_failure(body([text("p"), audio(REF_MP3)]))
    assert exc.status == 400 and "on its own" in exc.message


def test_too_many_reference_images_is_rejected():
    exc = expect_failure(body([text("p")] + [image(f"https://cdn.example.com/{i}.png") for i in range(10)]))
    assert exc.status == 400 and "at most 9 reference images" in exc.message


def test_too_many_reference_videos_is_rejected():
    exc = expect_failure(body([text("p"), video(REF_MP4)] * 4))
    assert exc.status == 400 and "at most 3 reference videos" in exc.message


def test_unknown_content_type_is_a_request_error_not_a_capability_gap():
    """上游的 `type=image` 形状**不是**原生类型 ⇒ 400（不能报成"上游没这能力"）。"""
    exc = expect_failure(body([{"type": "image", "url": REF_IMG}]))
    assert exc.status == 400 and "is not a Seedance content type" in exc.message


def test_content_item_without_url_is_rejected():
    exc = expect_failure(body([text("p"), {"type": "image_url", "image_url": {}}]))
    assert exc.status == 400 and "has no url" in exc.message
    assert "not a capability gap" in exc.message


def test_draft_task_reference_is_rejected():
    exc = expect_failure(body([text("p"), {"type": "draft_task", "draft_task": {"id": "dt1"}}]))
    assert exc.status == 400 and "no draft mode" in exc.message


def test_empty_content_is_rejected():
    assert expect_failure({"model": MODEL, "content": []}).status == 400
    assert expect_failure(body([text("   ")])).status == 400


# ---- 3. 默认值与钳制（"两侧默认值不同"是最容易静默出错的一类）--------------

def test_watermark_is_always_sent_explicitly():
    """上游默认 `true`、原生默认 `false` ⇒ 必须显式发，否则会静默加上水印。"""
    assert plan(body([text("p")]))[0]["body"]["watermark"] is False
    assert plan(body([text("p")], watermark=True))[0]["body"]["watermark"] is True
    # 字符串形式也要读对：`bool("false")` 是 True，读反就是静默加水印
    assert plan(body([text("p")], watermark="false"))[0]["body"]["watermark"] is False


def test_generate_audio_is_default_on_and_always_nested():
    """🔴 两侧**同一个参数的位置不同**：火山原生扁平、上游只认 `provider_specific`。

    用户 2026-09-17 明确："上游不接受扁平 generate_audio，只有火山接受扁平。"
    ⇒ 固定发嵌套，且**默认开声音**（原生 2.x 的默认值就是 `true`）。
    发错形态是**静默没声音**（成品没音轨，响应体里看不出来），所以这条要钉死。
    """
    default = plan(body([text("p")]))[0]["body"]
    assert default["provider_specific"] == {"generate_audio": True}
    assert "generate_audio" not in default

    # 显式关声音要能一路透到嵌套里（读布尔别把 "false" 读成真）
    off = plan(body([text("p")], generate_audio=False))[0]["body"]
    assert off["provider_specific"] == {"generate_audio": False}
    off_str = plan(body([text("p")], generate_audio="false"))[0]["body"]
    assert off_str["provider_specific"] == {"generate_audio": False}


def test_duration_minus_one_maps_to_native_default():
    """`-1`（模型自选）上游不认 ⇒ 取**原生默认** 5s，并如实告警。"""
    result, ctx = plan(body([text("p")], duration=-1))
    assert result["body"]["duration"] == 5
    assert any("model-chosen" in w for w in ctx.plan["warnings"])


def test_duration_is_clamped_to_the_upstream_range():
    upper, ctx_up = plan(body([text("p")], duration=30))
    assert upper["body"]["duration"] == 15
    assert any("clamped to 15s" in w for w in ctx_up.plan["warnings"])
    lower, _ = plan(body([text("p")], duration=1))
    assert lower["body"]["duration"] == 4


def test_frames_are_converted_to_duration():
    result, ctx = plan(body([text("p")], frames=120))
    assert result["body"]["duration"] == 5
    assert not any("duration" in w and "clamped" in w for w in ctx.plan["warnings"])
    assert any("converted to 5s" in w for w in ctx.plan["warnings"])


def test_resolution_default_and_out_of_range():
    default, ctx_def = plan(body([text("p")], resolution=None))
    assert default["body"]["resolution"] == "720p"
    assert any("defaulted to 720p" in w for w in ctx_def.plan["warnings"])
    high, ctx_high = plan(body([text("p")], resolution="4k"))
    assert high["body"]["resolution"] == "1080p"        # 向下钳，不涨价
    assert any("1080" in w for w in ctx_high.plan["warnings"])


def test_ratio_default_snap_and_adaptive():
    default, ctx_def = plan(body([text("p")], ratio=None))
    assert default["body"]["ratio"] == "16:9"
    assert any("defaulted to 16:9" in w for w in ctx_def.plan["warnings"])

    snapped, ctx_snap = plan(body([text("p")], ratio="21:9"))
    assert snapped["body"]["ratio"] == "16:9"
    assert any("snapped to" in w for w in ctx_snap.plan["warnings"])

    adaptive, ctx_ad = plan(body([text("p")], ratio="adaptive"))
    assert adaptive["body"]["ratio"] == "16:9"
    assert any("adaptive" in w for w in ctx_ad.plan["warnings"])


def test_invalid_enums_are_rejected_at_the_front_door():
    assert expect_failure(body([text("p")], ratio="5:4")).param == "ratio"
    assert expect_failure(body([text("p")], resolution="2k")).param == "resolution"
    assert expect_failure(body([text("p")], duration="abc")).param == "duration"


def test_execution_expires_after_is_translated_to_timeout():
    """上游 `timeout` 区间 [3600, 172800]，三个方向分别处理（见上游契约 §8 与 §7 差异 9）。"""
    inside, _ = plan(body([text("p")], execution_expires_after=7200))
    assert inside["body"]["timeout"] == 7200

    high, ctx_high = plan(body([text("p")], execution_expires_after=200000))
    assert high["body"]["timeout"] == 172800
    assert any("clamped to 172800s" in w for w in ctx_high.plan["warnings"])

    # 🔴 低于上游下限 ⇒ **不发**（为满足上游下限而延长调用方的上限 = 向上回退）
    low, ctx_low = plan(body([text("p")], execution_expires_after=600))
    assert "timeout" not in low["body"]
    assert any("omitted rather than extended" in w for w in ctx_low.plan["warnings"])


def test_unsupported_fields_are_reported_and_never_sent():
    result, ctx = plan(body(
        [text("p")], seed=11, camera_fixed=True, draft=False, service_tier="flex",
        return_last_frame=True, tools=[{"type": "x"}],
    ))
    unsupported = ctx.plan["unsupported"]
    for key in ("seed", "camera_fixed", "draft", "service_tier", "return_last_frame", "tools"):
        assert key in unsupported, (key, unsupported)
        assert key not in result["body"], key
    # 尾帧缺失会断掉连续拼接链路 —— 要明说，不能只丢进 unsupported[]
    assert any("splicing" in w for w in ctx.plan["warnings"])


def test_unsupported_lookup_also_covers_extra_body():
    """未建模字段走 `extra_body` ⇒ 判断"上游不支持"时两处都要查。"""
    _, ctx = plan(body([text("p")], extra_body={"output_format": "mov"}))
    assert "output_format" in ctx.plan["unsupported"]


# ---- 4. 模型名：透传 + 渠道映射表 ------------------------------------------

def test_model_name_is_forwarded_verbatim():
    result, _ = plan(body([text("p")]))
    assert result["body"]["model"] == BARE_MODEL


def test_unknown_model_is_rejected_with_the_legal_list():
    exc = expect_failure(body([text("p")], model="senseaudio/whatever"))
    assert exc.code == "InvalidParameter" and exc.param == "model"
    assert BARE_MODEL in exc.message


def test_model_map_exact_and_wildcard():
    options = opts(model_map={"doubao-seedance-2-0-260128": BARE_MODEL})
    result, ctx = plan(body([text("p")], model="senseaudio/doubao-seedance-2-0-260128"), options)
    assert result["body"]["model"] == BARE_MODEL
    assert ctx.plan["effective"]["model_map_applied"] is True

    # 通配**优先于**"名字本来就是模型名" ⇒ 渠道级的 `{"*": X}` 是改写
    options = opts(model_map={"*": BARE_MODEL})
    result, ctx = plan(body([text("p")], model="senseaudio/brand-new-model"), options)
    assert result["body"]["model"] == BARE_MODEL
    assert ctx.plan["effective"]["model_map_pattern"] == "*"


def test_two_matching_wildcards_are_refused():
    options = opts(model_map={"doubao-*": BARE_MODEL, "*260128": BARE_MODEL})
    exc = expect_failure(body([text("p")], model="senseaudio/doubao-seedance-2-0-260128"), options)
    assert exc.code == "channel_config_error"


def test_bad_model_map_is_a_channel_error():
    assert expect_failure(
        body([text("p")]), opts(model_map={"x": "not-a-model"})
    ).code == "channel_config_error"
    exc = expect_failure(
        body([text("p")]), opts(model_map={"A": BARE_MODEL, "a": BARE_MODEL})
    )
    assert exc.code == "channel_config_error" and "duplicate" in exc.message


def test_removed_channel_option_is_refused_loudly():
    """已撤除的 `X-Channel-Options.model`（渠道钉住槽位）遗留 ⇒ 响亮失败（ADR-012）。"""
    exc = expect_failure(body([text("p")]), opts(model=BARE_MODEL))
    assert exc.code == "channel_config_error" and "was removed" in exc.message


# ---- 5. 计费护栏：判断必须发生在发上游之前 ---------------------------------

def test_missing_max_credits_is_refused():
    exc = expect_failure(body([text("p")]), {"provider": "senseaudio"})
    assert exc.status == 400 and "spend cap" in exc.message
    assert exc.param == "extra_body.senseaudio_max_credits"


def test_unpriced_cost_needs_explicit_opt_in():
    exc = expect_failure(body([text("p")]), {"provider": "senseaudio", "max_credits": 5000})
    assert exc.status == 400 and "cannot be verified" in exc.message
    # 请求级也能显式接受
    with_extra = body([text("p")], extra_body={"senseaudio_allow_unpriced": True})
    result, ctx = plan(with_extra, {"provider": "senseaudio", "max_credits": 5000})
    assert result["body"]["duration"] == 6
    assert any("not verifiable" in w for w in ctx.plan["warnings"])


def test_credit_table_makes_the_cost_verifiable_again():
    options = opts(credit_table={BARE_MODEL: 12})
    _, ctx = plan(body([text("p")]), options)
    assert ctx.plan["estimated_credits"] == 72          # 6s × 12 积分/秒
    assert not any("not verifiable" in w for w in ctx.plan["warnings"])

    exc = expect_failure(body([text("p")], duration=15), opts(max_credits=100, credit_table={BARE_MODEL: 12}))
    assert exc.status == 400 and "exceeds the spend cap" in exc.message


def test_request_level_cap_only_tightens_the_channel_cap():
    options = opts(max_credits=100, credit_table={BARE_MODEL: 1})
    # 请求级更严 ⇒ 生效；记账用的估算只有 6 积分，所以 30 不会误伤
    _, ctx = plan(body([text("p")], extra_body={"senseaudio_max_credits": 30}), options)
    assert ctx.plan["max_credits"] == 30                # 收紧
    _, ctx = plan(body([text("p")], extra_body={"senseaudio_max_credits": 900}), options)
    assert ctx.plan["max_credits"] == 100               # 放不宽


# ---- 6. 响应侧：状态映射与"只报真知道的值" --------------------------------

def test_status_mapping_covers_the_upstream_four():
    assert sa.map_status("pending") == "queued"
    assert sa.map_status("processing") == "running"
    assert sa.map_status("completed") == "succeeded"
    assert sa.map_status("failed") == "failed"
    # 未知/缺失**绝不落终态**（终态会落库、释放槽位、推回调）
    assert sa.map_status("weird") == "running"
    assert sa.map_status(None) == "running"


def test_normalize_completed_task():
    view = sa.normalize_task({
        "id": "52e0c397-2f78", "task_id": "c4666acd", "status": "completed",
        "video_url": "https://cdn.example.com/out.mp4", "duration": 10,
        "resolution": "720p", "ratio": "16:9",
        "created_at": 1773822549, "completed_at": 1773822600,
    })
    assert view["status"] == "succeeded"
    assert view["video_url"] == "https://cdn.example.com/out.mp4"
    assert view["duration"] == 10 and view["resolution"] == "720p" and view["ratio"] == "16:9"
    assert view["created_at"] == 1773822549 and view["updated_at"] == 1773822600
    assert view["error"] is None
    assert view["upstream"]["task_id"] == "c4666acd"      # 原文留档，只进 trace


def test_normalize_failed_task_carries_the_message():
    view = sa.normalize_task({"status": "failed", "error_message": "sensitive content"})
    assert view["status"] == "failed"
    assert view["video_url"] is None
    assert view["error"]["message"] == "sensitive content"


def test_normalize_does_not_invent_a_video_url_before_success():
    view = sa.normalize_task({"status": "processing", "video_url": "https://cdn.example.com/x.mp4"})
    assert view["status"] == "running" and view["video_url"] is None


def test_normalize_emits_no_fabricated_constants():
    """🔴 归一化层**不产出**编造常量 —— 值不知道就不产出（引擎负责原生默认值回填）。"""
    view = sa.normalize_task({"status": "processing"})
    for key in ("usage", "seed", "framespersecond", "service_tier", "frames", "file_url", "last_frame_url"):
        assert key not in view, key
    # 🔴 上游**没有任何用量字段** ⇒ 不折算、不编 token 数
    assert "completion_tokens" not in json.dumps(view)


# ---- 7. 相位与 URL ---------------------------------------------------------

def test_phases_declare_no_cancel_endpoint():
    """上游没有取消端点 ⇒ 不声明 cancel 相位；引擎据此**拒绝**未终态任务的 DELETE（ADR-014）。

    同时钉住错误相位的存在（`ADR-015`）—— 少了它们，"上游并发已满"会退回 400。
    """
    assert sa.PHASES == (
        "create_request", "create_response", "create_error",
        "query_request", "query_response", "query_error",
    )
    assert not [p for p in sa.PHASES if p.startswith("cancel")]
    for phase in sa.PHASES:
        assert callable(sa.__dict__[phase]), phase


def test_query_url_defaults_to_no_parameters():
    """🔴 实测该接口**不接受参数**（用户 2026-09-17 给的 curl 只有 `Authorization` 头）
    ⇒ 默认形态是**裸 URL**，任务身份由凭证承担（决定 9）。"""
    task = FakeTask(upstream_task_id="task_1234567890")
    result = asyncio.run(sa.query_request(FakeCtx(task=task), {"id": "cgt-local"}))
    assert result["method"] == "GET"
    assert result["url"] == "https://api.senseaudio.cn/v1/video/status"
    assert "?" not in result["url"]


def test_the_documented_task_id_form_is_still_available():
    """官方文档那个 `?id=<task_id>` 形态留成渠道开关：实测若发现要带参数，一行配置即可。"""
    task = FakeTask(upstream_task_id="task_1234567890")
    documented = FakeCtx(opts(status_binding="task_id"), task=task)
    assert asyncio.run(sa.query_request(documented, {}))["url"].endswith(
        "/v1/video/status?id=task_1234567890"
    )
    renamed = FakeCtx(opts(status_binding="task_id", query_id_param="task_id"), task=task)
    assert asyncio.run(sa.query_request(renamed, {}))["url"].endswith(
        "/v1/video/status?task_id=task_1234567890"
    )
    # 值里带特殊字符要被转义（防 URL 注入），不是拼字符串
    weird = FakeCtx(opts(status_binding="task_id"), task=FakeTask(upstream_task_id="a&b=c"))
    assert asyncio.run(sa.query_request(weird, {}))["url"].endswith("?id=a%26b%3Dc")


def test_status_binding_only_accepts_the_two_known_modes():
    ctx = FakeCtx(opts(status_binding="api_key"), task=FakeTask())
    try:
        asyncio.run(sa.query_request(ctx, {}))
    except Failure as exc:
        assert exc.code == "channel_config_error"
    else:
        raise AssertionError("未知的 status_binding 应当被拒")


def test_invalid_query_id_param_is_a_channel_error():
    """参数名只在 `status_binding=task_id` 形态下被读（默认形态压根不带参数）。"""
    ctx = FakeCtx(opts(status_binding="task_id", query_id_param="no good"), task=FakeTask())
    try:
        asyncio.run(sa.query_request(ctx, {}))
    except Failure as exc:
        assert exc.code == "channel_config_error", exc.code
    else:
        raise AssertionError("非法 query_id_param 应当被拒")


# ---- 9. 记录归属：本上游按 API key 回答 ⇒ 必须验明"这条记录是本任务的"------

def belongs(payload, *, upstream_task_id="c4666acd-0001", options=None):
    """跑一次归属校验。返回 `Failure`（拒了）或 `None`（放行）。"""
    ctx = FakeCtx(options if options is not None else opts(),
                  task=FakeTask(upstream_task_id=upstream_task_id))
    try:
        sa.assert_record_belongs_to_task(ctx, payload)
    except Failure as exc:
        return exc
    return None


def test_record_belonging_to_another_task_is_refused():
    """🔴 最坏形态的静默错：旧任务被新任务顶掉后，查旧任务会拿到**新任务**的状态与产物。"""
    other = {"task_id": "c4666acd-0002", "status": "completed", "video_url": "https://x/b.mp4"}
    exc = belongs(other, upstream_task_id="c4666acd-0001")
    assert exc is not None
    assert exc.status == 400 and exc.param == "id"
    assert "c4666acd-0002" in exc.message and "c4666acd-0001" in exc.message
    # 消息要给出**下一步动作**，否则调用方只能停在那儿
    assert "max_concurrency" in exc.message


def test_record_belonging_to_this_task_passes():
    mine = {"task_id": "c4666acd-0001", "status": "processing"}
    assert belongs(mine) is None


def test_ownership_check_does_not_over_reject():
    """三种情况必须放行，否则会把"不认识"当成"不是我的"（那就查不动了）。"""
    # ① 记录里没有 task_id ⇒ 无据可依，不能凭空拒绝
    assert belongs({"status": "processing"}) is None
    assert belongs({"task_id": ""}) is None
    # ② 不是 JSON 对象
    assert belongs("<html>") is None
    assert belongs(None) is None
    # ③ 上游自己按参数过滤的形态 ⇒ 归属由上游保证，不需要我们校验
    other = {"task_id": "c4666acd-0002"}
    assert belongs(other, options=opts(status_binding="task_id")) is None


def test_upstream_url_tolerates_a_full_endpoint():
    for configured, expected in (
        ("https://api.senseaudio.cn", "https://api.senseaudio.cn/v1/video/create"),
        ("https://api.senseaudio.cn/", "https://api.senseaudio.cn/v1/video/create"),
        ("https://api.senseaudio.cn/v1/video/create", "https://api.senseaudio.cn/v1/video/create"),
        ("https://api.senseaudio.cn/v1/video/status", "https://api.senseaudio.cn/v1/video/create"),
    ):
        ctx = FakeCtx(opts(), upstream_url=configured)
        result = asyncio.run(sa.create_request(ctx, body([text("p")])))
        assert result["url"] == expected, (configured, result["url"])


def test_create_response_shapes_the_task_id():
    ctx = FakeCtx()
    assert asyncio.run(sa.create_response(ctx, {"task_id": "task_1"})) == {"task_id": "task_1"}


def test_create_response_separates_operator_errors_from_upstream_drift():
    # 不是 JSON 对象（URL 打到了 HTML 页面）⇒ 运维的问题
    assert expect_create_response_failure("not-an-object").code == "channel_config_error"
    # 是 JSON 但没有 task_id ⇒ 上游契约漂移，**不是**调用方的问题
    assert expect_create_response_failure({"id": "52e0c397"}).code == "InternalServiceError"


def expect_create_response_failure(payload):
    ctx = FakeCtx()
    try:
        asyncio.run(sa.create_response(ctx, payload))
    except Failure as exc:
        return exc
    raise AssertionError(f"expected create_response to fail on {payload!r}")


# ---- 8. 错误相位：上游业务码 → 契约里已有的 code（ADR-015）-----------------

_ERROR_PHASES = {"create_error": sa.create_error, "query_error": sa.query_error}


def error_phase(payload, *, status=400, upstream_retry_after=None, options=None,
                phase="create_error"):
    """跑一次错误相位。返回 `Failure`（拦下了）或 `None`（不拦，交回通用映射）。"""
    ctx = FakeCtx(
        options if options is not None else opts(),
        upstream_error={"status": status, "retry_after": upstream_retry_after},
    )
    try:
        asyncio.run(_ERROR_PHASES[phase](ctx, payload))
    except Failure as exc:
        return exc
    return None


def test_error_phase_maps_the_known_business_codes():
    """上游那 9 个码各自落哪个出口码 —— 这是"上游忙"不再被读成"你写错了"的全部依据。"""
    table = [
        ("invalid", "InvalidParameter", 400),
        ("400000", "InvalidParameter", 400),
        ("400015", "ServerOverloaded", 429),                  # 上游并发已满
        ("400001", "QuotaExceeded", 429),                     # 使用限制/余额不足
        ("400900", "AccountOverdueError", 403),               # 计费账户不存在
        ("400901", "AccountOverdueError", 403),
        ("400902", "AccountOverdueError", 403),
        ("429000", "RateLimitExceeded.ModelAccountRpmExceeded", 429),
        ("429002", "QuotaExceeded", 429),
    ]
    for ref_code, code, status in table:
        exc = error_phase({"code": ref_code, "message": "上游原话"})
        assert exc is not None, ref_code
        assert (exc.code, exc.status) == (code, status), (ref_code, exc.code, exc.status)
        assert "上游原话" in exc.message and ref_code in exc.message


def test_error_phase_accepts_the_three_plausible_body_shapes():
    """⚠️ 上游没给错误体示例 ⇒ 三种常见位置都认（形状本身仍未证实）。"""
    for payload in (
        {"ref_code": "400015", "message": "并发已满"},
        {"code": 400015},
        {"error": {"code": "400015", "message": "并发已满"}},
    ):
        exc = error_phase(payload)
        assert exc is not None and exc.code == "ServerOverloaded", payload


def test_error_phase_refuses_to_guess():
    """认不出就**不拦**（返回 None）—— 猜错方向会把"上游 500"说成"你的参数错了"。"""
    for payload in (
        {"code": "500000", "message": "服务繁忙"},     # 自相矛盾的码，刻意不映射
        "<html>bad gateway</html>",                    # 不是 JSON
        {"code": "some-english-name"},                 # 认不出的码
        {},
        None,
    ):
        assert error_phase(payload) is None, payload


def test_error_phase_retry_after_is_a_fact_not_a_guess():
    # ① 上游给了 ⇒ 透传
    assert error_phase({"code": "429000"}, status=429, upstream_retry_after=12.5).retry_after == 12.5
    # ② 上游没给、渠道声明了 ⇒ 用渠道值
    assert error_phase({"code": "400015"}, options=opts(retry_after_seconds=7)).retry_after == 7.0
    # ③ 都没有 ⇒ 不给（不编一个数）
    assert error_phase({"code": "400015"}).retry_after is None
    # 非 429 出口不挂 Retry-After（403 等不是"等一会儿再来"）
    assert error_phase({"code": "400900"}, options=opts(retry_after_seconds=7)).retry_after is None


def test_query_error_shares_the_mapping():
    exc = error_phase({"code": "400001"}, phase="query_error")
    assert exc is not None and exc.code == "QuotaExceeded"


# =============================================================================
# B. 引擎级：本地假 SenseAudio 上游 + ASGITransport
# =============================================================================

class FakeSenseAudio:
    """记录收到的每个请求；**按 SenseAudio 的两个端点应答**，绝不出本机。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.tasks: dict[str, int] = {}          # task_id → 查询次数
        #: `Authorization` 头 → **该钥匙当前的任务 id**。
        #: 🔴 故意复现上游的真实语义（用户 2026-09-17 实测）：查询接口**不接受参数**、
        #: 按 API key 回答 ⇒ 同一把钥匙上的新任务会**顶掉**旧任务。
        #: 假上游若在这里"按参数查"，那条最危险的缺陷（把 #B 的状态写到 #A 上）
        #: 就永远测不出来 —— 这才是假上游该收紧的地方。
        self.current: dict[str, str] = {}
        self.create_status: int | None = None
        self.create_error_body: dict | None = None
        self.create_error_headers: dict | None = None
        self.next_status: str | None = None      # 强制下一次查询的状态
        self.httpd: http.server.ThreadingHTTPServer | None = None
        self.port = 0

    def count(self, method: str, prefix: str = "") -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"].startswith(prefix))

    def start(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), functools.partial(_Handler)
        )
        self.httpd.state = self  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音 access log
        return

    def _json(self, code: int, payload: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def _route(self, method: str) -> None:
        state: FakeSenseAudio = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        state.requests.append({
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })

        if method == "POST" and self.path.startswith("/v1/video/create"):
            if state.create_status:
                return self._json(
                    state.create_status,
                    state.create_error_body or {"message": "boom"},
                    state.create_error_headers,
                )
            task_id = f"c4666acd-{len(state.tasks) + 1:04d}"
            state.tasks[task_id] = 0
            # 新任务成为**这把钥匙的当前任务**（旧任务从此查不到了）
            state.current[self.headers.get("Authorization") or ""] = task_id
            return self._json(200, {"task_id": task_id})

        if method == "GET" and self.path.startswith("/v1/video/status"):
            # 🔴 与实测一致：这个接口**不吃参数**。带了参数直接报错 ——
            #    否则"默认不带参数"这条约束在测试里是空的。
            if urllib.parse.urlparse(self.path).query:
                return self._json(400, {"code": 400000, "message": "unexpected query parameter"})
            task_id = state.current.get(self.headers.get("Authorization") or "")
            if task_id is None:
                return self._json(404, {"code": 400000, "message": "unknown task"})
            state.tasks[task_id] += 1
            status = state.next_status or ("completed" if state.tasks[task_id] >= 2 else "processing")
            payload = {
                "id": "52e0c397-2f78-4c1f-8774-d51aba5e4e3c",
                "model": "Seedance-2.0",
                "task_id": task_id,
                "status": status,
                "progress": 100 if status == "completed" else 40,
                "duration": 6,
                "is_new": True,
                "created_at": 1773822549,
                "prompt": "a cat yawning",
                "resolution": "720p",
                "ratio": "16:9",
            }
            if status == "completed":
                payload["completed_at"] = 1773822600
                payload["video_url"] = f"{state.base_url}/media/{task_id}.mp4"
            if status == "failed":
                payload["error_message"] = "upstream refused the content"
            return self._json(200, payload)

        return self._json(404, {"message": f"no route for {method} {self.path}"})


class Client:
    """把调用方会带的头一次配好。⚠️ **不带 `X-Auth-Emit`** —— 上游要标准 Bearer。"""

    def __init__(self, upstream_url: str, *, credential: str = UPSTREAM_KEY, options: str = OPTS):
        self.headers = {
            "X-Adapter-Key": ADAPTER_KEY,
            "X-Upstream-Url": upstream_url,
            "X-Script-Ref": SCRIPT_REF,
            "X-Channel-Options": options,
            "Authorization": f"Bearer {credential}",
        }


def make_settings(**overrides) -> Settings:
    values = {
        "adapter_key": ADAPTER_KEY,
        "upstream_allow_private_network": True,
        "upstream_trust_env": False,
        "task_store": "memory",
        "script_store_dir": SCRIPT_STORE,
        "task_key_fingerprint_secret": "test-secret",
        "default_max_concurrency": 2,
        "queue_wait_seconds": 0.3,
        "upstream_retry_attempts": 1,
        # 本文件断言"第 N 次查询看到什么"，所以关掉查询缓存（降频由 test_rate_limit.py 验）
        "query_cache_seconds": 0.0,
    }
    values.update(overrides)
    return Settings.from_env(**values)


def _body(**rest) -> dict:
    content = [{"type": "text", "text": "a cat yawning"}]
    payload = {
        "model": MODEL,
        "content": content,
        "duration": 6,
        "resolution": "720p",
        "ratio": "16:9",
    }
    payload.update(rest)
    return payload


async def run_case(upstream: FakeSenseAudio, case) -> None:
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://adapter.test") as client:
            await case(client, app, upstream)


def case(fn):
    def wrapper() -> None:
        upstream = FakeSenseAudio()
        upstream.start()
        try:
            asyncio.run(run_case(upstream, fn))
        finally:
            upstream.stop()

    wrapper.__name__ = fn.__name__
    return wrapper


TASKS = "/api/v3/contents/generations/tasks"


@contextlib.contextmanager
def snapshots():
    """收集 `task.snapshot` 上报 —— 上游 task id / 实际生效值只能从这里取（ADR-011）。"""
    seen: list[dict] = []

    def sink(record):
        if record.name == "task.snapshot":
            seen.append(record.attributes)

    observability.add_span_sink(sink)
    try:
        yield seen
    finally:
        observability.remove_span_sink(sink)


@case
async def test_engine_create_asserts_the_body_that_went_out(client, app, upstream):
    """🔴 断言的是**上游实际收到的 body**，不是 `plan_create` 的返回值。"""
    ch = Client(upstream.base_url)
    with snapshots() as snaps:
        response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["id"].startswith("cgt-")
    assert len(created) == 1, created                    # 原生契约：创建只回 id

    assert upstream.count("POST", "/v1/video/create") == 1
    sent = upstream.requests[0]
    assert sent["body"] == {
        "model": BARE_MODEL,
        "content": [{"type": "text", "text": "a cat yawning"}],
        "duration": 6,
        "resolution": "720p",
        "ratio": "16:9",
        "watermark": False,
        "provider_specific": {"generate_audio": True},
    }, sent["body"]
    assert sent["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"   # 标准 Bearer 原样转发

    snap = snaps[-1]
    assert snap["task.upstream_id"] == "c4666acd-0001"
    assert snap["task.report.request"]["url"].endswith("/v1/video/create")
    assert snap["task.effective"]["upstream_model"] == BARE_MODEL


@case
async def test_engine_query_is_credential_bound_and_status_maps(client, app, upstream):
    ok = Client(upstream.base_url)
    created = (await client.post(TASKS, json=_body(), headers=ok.headers)).json()
    task_id = created["id"]

    # 换一把钥匙 ⇒ 本地 404，且**一个上游请求都不发**
    before = len(upstream.requests)
    other = Client(upstream.base_url, credential="sk_another_key").headers
    denied = await client.get(f"{TASKS}/{task_id}", headers=other)
    assert denied.status_code == 404, denied.text
    assert len(upstream.requests) == before, "凭证不符时不得打上游"

    # 正确的钥匙：processing → running
    first = (await client.get(f"{TASKS}/{task_id}", headers=ok.headers)).json()
    assert first["status"] == "running", first
    assert first["content"]["video_url"] is None
    assert set(first) == set(NATIVE_TASK_KEYS), sorted(set(first) ^ set(NATIVE_TASK_KEYS))

    # 第二次：completed → succeeded + 产物地址
    second = (await client.get(f"{TASKS}/{task_id}", headers=ok.headers)).json()
    assert second["status"] == "succeeded", second
    assert second["content"]["video_url"].endswith(".mp4")
    assert second["duration"] == 6 and second["resolution"] == "720p" and second["ratio"] == "16:9"
    assert second["error"] is None
    # 🔴 上游不提供任何用量字段 ⇒ `usage` 只能是 null（不编 token 数）
    assert second["usage"] is None, second

    # 🔴 上游的查询接口**不接受参数**：路径必须是裸的 `/v1/video/status`
    query_paths = [r["path"] for r in upstream.requests if r["method"] == "GET"]
    assert query_paths and all(p == "/v1/video/status" for p in query_paths), query_paths


@case
async def test_engine_refuses_a_status_record_that_belongs_to_another_task(client, app, upstream):
    """🔴 端到端复现"最坏形态的静默错"并证明它被拦下。

    场景：同一把钥匙先建 #A（没人轮询过），再建 #B。上游"当前任务"变成 #B，
    于是查 #A 会拿到 **#B 的记录**。若不拦，调用方会看到 #A **succeeded** 且拿到
    #B 的产物链接 —— 一切看起来都正常，只是东西是另一个任务的。
    """
    ch = Client(upstream.base_url)
    first = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    second = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    assert first != second

    response = await client.get(f"{TASKS}/{first}", headers=ch.headers)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "InvalidParameter" and error["param"] == "id"
    assert "belongs to task" in error["message"]

    # 本地记录**没有被新任务的状态污染**（仍是 queued，没被改成 succeeded 之类）
    record = await app.state.store.get(first)
    assert record["status"] == "queued", record["status"]

    # 而它自己的任务照常可查（同一把钥匙的最新任务）
    ok = (await client.get(f"{TASKS}/{second}", headers=ch.headers)).json()
    assert ok["id"] == second, ok
    final = (await client.get(f"{TASKS}/{second}", headers=ch.headers)).json()
    assert final["status"] == "succeeded", final
    # 产物是**它自己的**那一个（按本地记录的 upstream_task_id 对照，不靠猜）
    mine = (await app.state.store.get(second))["upstream_task_id"]
    assert final["content"]["video_url"].endswith(f"/media/{mine}.mp4"), final["content"]


@case
async def test_engine_delete_of_a_running_task_is_refused(client, app, upstream):
    """🔴 上游没有取消端点 ⇒ 未终态任务的 DELETE 必须**响亮失败**（ADR-014）。

    不能伪造 `cancelled`：那会让上游任务继续跑（继续计费），并提前释放并发槽位。
    """
    ch = Client(upstream.base_url)
    created = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()
    task_id = created["id"]

    before = len(upstream.requests)
    deleted = await client.delete(f"{TASKS}/{task_id}", headers=ch.headers)
    assert deleted.status_code == 400, deleted.text
    assert deleted.json()["error"]["code"] == "InvalidParameter"
    assert "no cancel endpoint" in deleted.json()["error"]["message"]
    assert len(upstream.requests) == before, "被拒的取消不得打上游"

    # 本地状态**没有被改写**：记录还在、仍是 queued，**并发槽位仍被占着**
    record = await app.state.store.get(task_id)
    assert record is not None and record["status"] == "queued", record
    assert app.state.gate.active() == 1, "被拒的取消不得释放并发槽位（真实天花板是在途数）"

    # 再查一次：非终态任务**会去打上游**（若被本地伪造成 cancelled，终态会直接本地作答）
    still = (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()
    assert still["id"] == task_id and still["status"] == "running", still


@case
async def test_engine_delete_of_a_terminal_task_drops_the_local_record(client, app, upstream):
    """终态任务的 DELETE = 删本地记录（原生语义），与上游有没有取消端点无关。"""
    ch = Client(upstream.base_url)
    task_id = (await client.post(TASKS, json=_body(), headers=ch.headers)).json()["id"]
    await client.get(f"{TASKS}/{task_id}", headers=ch.headers)          # processing
    assert (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).json()["status"] == "succeeded"

    deleted = await client.delete(f"{TASKS}/{task_id}", headers=ch.headers)
    assert deleted.status_code == 200 and deleted.json() == {"id": task_id, "deleted": True}
    assert (await client.get(f"{TASKS}/{task_id}", headers=ch.headers)).status_code == 404


@case
async def test_engine_implicit_first_last_frames_survive_the_pipeline(client, app, upstream):
    """前门把"2 张无 role 的图 + 文本"归一成首尾帧 ⇒ 脚本要按首尾帧模式发出去。"""
    ch = Client(upstream.base_url)
    payload = _body(content=[
        {"type": "text", "text": "a cat"},
        {"type": "image_url", "image_url": {"url": REF_IMG}},
        {"type": "image_url", "image_url": {"url": REF_IMG2}},
    ])
    response = await client.post(TASKS, json=payload, headers=ch.headers)
    assert response.status_code == 200, response.text
    assert upstream.requests[0]["body"]["content"] == [
        {"type": "text", "text": "a cat"},
        {"type": "image", "url": REF_IMG, "role": "first_frame"},
        {"type": "image", "url": REF_IMG2, "role": "last_frame"},
    ], upstream.requests[0]["body"]["content"]


@case
async def test_engine_dry_run_never_touches_the_upstream(client, app, upstream):
    ch = Client(upstream.base_url)
    headers = dict(ch.headers, **{"X-Dry-Run": "1"})
    response = await client.post(TASKS, json=_body(watermark=True), headers=headers)
    assert response.status_code == 200, response.text
    dry = response.json()
    assert dry["dry_run"] is True
    assert dry["upstream"]["url"].endswith("/v1/video/create")
    assert dry["upstream"]["body"]["watermark"] is True
    assert dry["provider"] == "senseaudio" and dry["model"] == BARE_MODEL
    assert upstream.requests == [], "dry_run 不得打上游"


@case
async def test_engine_spend_guard_blocks_before_any_request(client, app, upstream):
    """上限判断必须在**任何请求发出之前** —— 用"上游一个请求都没收到"来证明。"""
    ch = Client(upstream.base_url, options=json.dumps({"provider": "senseaudio", "max_credits": 5000}))
    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "InvalidParameter"
    assert upstream.requests == []


@case
async def test_engine_error_phase_maps_business_codes(client, app, upstream):
    """🔴 上游业务码经**错误相位**映射（`ADR-015`）——"上游忙"不再被读成"你写错了"。

    每一条都断言**出口的 HTTP + `error.code`**：调用方是按这两个分支决定退避还是改请求的。
    最后一条是**回落**：认不出的码不许猜，交给引擎按 HTTP 状态通用映射。
    """
    ch = Client(upstream.base_url)
    cases = [
        ({"code": 400015, "message": "已达到最大并发数量"}, 429, "ServerOverloaded"),
        ({"ref_code": "400001", "message": "余额不足"}, 429, "QuotaExceeded"),
        ({"error": {"code": "400901", "message": "计费账户已冻结"}}, 403, "AccountOverdueError"),
        ({"code": "500000", "message": "服务繁忙"}, 400, "InvalidParameter"),   # 刻意不映射 ⇒ 回落
        ({"code": "who-knows"}, 400, "InvalidParameter"),                       # 认不出 ⇒ 回落
    ]
    for payload, want_status, want_code in cases:
        upstream.create_status = 400
        upstream.create_error_body = payload
        response = await client.post(TASKS, json=_body(), headers=ch.headers)
        assert response.status_code == want_status, (payload, response.text)
        assert response.json()["error"]["code"] == want_code, payload
        assert str(upstream.requests[-1]["body"]["model"]) == BARE_MODEL   # 确实发过请求


@case
async def test_engine_error_phase_carries_the_upstream_retry_after(client, app, upstream):
    """429 必须带 `Retry-After`：上游给了就**透传**，不编数（`ADR-004` 同一口径）。"""
    upstream.create_status = 400
    upstream.create_error_body = {"code": 400015, "message": "已达到最大并发数量"}
    upstream.create_error_headers = {"Retry-After": "12"}
    ch = Client(upstream.base_url)

    response = await client.post(TASKS, json=_body(), headers=ch.headers)
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "ServerOverloaded"
    assert response.headers.get("Retry-After") == "12", dict(response.headers)
    # 上游并发满**不是**调用方的错 ⇒ 消息要带上上游原话，便于对工单
    assert "已达到最大并发数量" in response.json()["error"]["message"]


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    for name, exc in failed:
        print(f"  - {name}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
