"""aivideomaker/video@v1 的翻译层测试。**零消耗**：不联网、不发任何生成请求。

跑法（不需要 pytest）：

    python3 tests/test_aivideomaker_video_v1.py

覆盖 `docs/adapter-playbook.md` §9 的 14 项里**属于脚本职责**的部分
（并发闸门 / 持久化 / 回调 / dry_run / 转存属引擎，见 03_引擎架构.md）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script_store" / "aivideomaker" / "video@v1.py"

DATA_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
DATA_MP4 = "https://cdn.example.com/ref.mp4"      # 只做字段投影，不下载
DATA_MP3 = "https://cdn.example.com/ref.mp3"


def _load():
    spec = importlib.util.spec_from_file_location("avm_video_v1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


avm = _load()


class Failure(Exception):
    def __init__(self, message, code=None, param=None, status=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.param = param
        self.status = status


class FakeCtx:
    """假 ctx：只实现脚本用到的成员。`fail` 必抛，与真引擎一致。"""

    def __init__(self, options=None, upstream_url="https://aivideomaker.ai", task=None):
        self.options = options or {}
        self.upstream_url = upstream_url
        self.task = task
        self.plan = None

    def fail(self, message, code=None, param=None, status=None):
        raise Failure(message, code=code, param=param, status=status)


class FakeTask:
    def __init__(self, upstream_task_id="ck123", status="queued"):
        self.upstream_task_id = upstream_task_id
        self.status = status


def body(model, content, **rest):
    payload = {"model": model, "content": content}
    payload.update(rest)
    return payload


def text(prompt):
    return {"type": "text", "text": prompt}


def image(url, role=None):
    item = {"type": "image_url", "image_url": {"url": url}}
    if role:
        item["role"] = role
    return item


def opts(**kwargs):
    """渠道选项的默认值。

    默认带 `allow_unpriced`：`seedance20` 是动态计价、上游又没有计费前闸门（ADR-004），
    用这个模型就必须显式接受"不可验证成本" —— 绝大多数用例只验投影，所以默认给上；
    **专门验计费闸门的用例显式传 `allow_unpriced=False`**。
    """
    kwargs.setdefault("max_credits", 10_000)
    kwargs.setdefault("allow_unpriced", True)
    return kwargs


def expect_failure(fn):
    try:
        fn()
    except Failure as exc:
        return exc
    raise AssertionError("expected a failure, got none")


# =============================================================================
# 1. 模型路由：**透传**（本层不改模型值），需要时由渠道配映射表
# =============================================================================
#
# 2026-09-16 起本层不再做名字→槽位的映射。旧实现是顺序敏感的正则
# （`(r"seedance", "seedance20")`），把 5 个不同代次的原生 ID 静默压进同一槽位，
# 实测账单差 7.3 倍（110 vs 15 积分）。现在的规则有四条：
#   ① 名字**精确命中**渠道 `model_map` ⇒ 按表替换；
#   ② 名字命中**唯一**的通配模式（含 `*` 单键）⇒ 按表替换 —— ⚠️ ② 在 ③ **之前**，
#      所以 `{"*": "t2v"}` 会把合法槽位名也改写掉（这是明确要的语义）；
#   ③ 名字本身是上游槽位 ⇒ 逐字透传；
#   ④ 其它 ⇒ 400（列出合法槽位 + 渠道已配的键），**绝不落默认值**。
# 两条通配同时命中 ⇒ `channel_config_error`：不排序、不取最长。

def test_upstream_slot_names_pass_through_untouched():
    """8 个槽位名逐字透传 —— 这是"代码里不改模型值"的直接断言。"""
    for name in avm.OFFICIAL_MODELS:
        got = avm.resolve_upstream_model(name, {}, FakeCtx(opts()))
        assert got == (name, False, None), f"{name} → {got}"


def test_provider_segment_is_stripped_when_engine_has_not():
    """只有 `provider/` 这一段被剥掉（它是路由信息），模型名本身不动。"""
    assert avm.resolve_upstream_model("aivideomaker/seedance20", {}, FakeCtx(opts())) == (
        "seedance20",
        False,
        None,
    )


def test_unknown_model_is_rejected_with_the_legal_list():
    ctx = FakeCtx(opts())
    exc = expect_failure(lambda: avm.resolve_upstream_model("totally-made-up", {}, ctx))
    assert exc.code == "InvalidParameter", exc.code
    assert exc.param == "model"
    for name in avm.OFFICIAL_MODELS:            # 合法值一个不落地列出来
        assert name in exc.message, name
    assert "verbatim" in exc.message            # 讲清了"本层不改模型值"
    assert "No model_map is configured" in exc.message


def test_model_map_translates_native_ids_and_says_it_hit():
    """渠道配了映射表 ⇒ 原生 ID 可用，且**命中可查**（响应体里已没有 model）。"""
    mapping = {
        "doubao-seedance-2-0-260128": "seedance20",
        "doubao-seedance-1-0-pro-250528": "t2v",
    }
    options = opts(model_map=mapping)
    plan = avm.plan_create(
        body("doubao-seedance-2-0-260128", [text("hi")], duration=5, resolution="720p", ratio="16:9"),
        options,
        FakeCtx(options),
    )
    assert plan["upstream_model"] == "seedance20"
    assert plan["effective"]["model_map_applied"] is True
    assert plan["effective"]["model_requested"] == "doubao-seedance-2-0-260128"


def test_model_map_is_exact_match_not_a_regex():
    """`doubao-seedance-2-5-260628` **不会**被 `seedance` 那条规则吞掉（这是 G1 的根因）。"""
    options = opts(model_map={"doubao-seedance-2-0-260128": "seedance20"})
    exc = expect_failure(
        lambda: avm.resolve_upstream_model("doubao-seedance-2-5-260628", options, FakeCtx(options))
    )
    assert exc.code == "InvalidParameter", exc.code
    assert "doubao-seedance-2-0-260128" in exc.message   # 提示渠道已配了哪些键


def test_model_map_value_must_be_an_upstream_model():
    """映射表的值必须是上游认识的槽位：给个上游不认识的值 = 把 400 推到上游。"""
    options = opts(model_map={"x": "not-upstream"})
    exc = expect_failure(lambda: avm.resolve_upstream_model("x", options, FakeCtx(options)))
    assert exc.code == "channel_config_error", exc.code


def test_model_map_accepts_the_documented_alias_name():
    """`upstream_model_map` 是 `docs/03` §4.2 登记过的旧名 ⇒ 同样接受（别名，不是新机制）。

    `docs/03` 当时登记的 `upstream_model_map` 从未实现过；运维照那份文档配是**很可能**的事，
    于是这里接受两个名字。两个都写且不同 ⇒ 配置自相矛盾，报错而不是替它挑一个。
    """
    options = opts(upstream_model_map={"doubao-seedance-2-0-260128": "seedance20"})
    assert avm.resolve_upstream_model(
        "doubao-seedance-2-0-260128", options, FakeCtx(options)
    ) == ("seedance20", True, None)

    both = opts(
        model_map={"a": "t2v"},
        upstream_model_map={"a": "wan27"},
    )
    exc = expect_failure(lambda: avm.resolve_upstream_model("a", both, FakeCtx(both)))
    assert exc.code == "channel_config_error", exc.code

    same = opts(model_map={"a": "t2v"}, upstream_model_map={"a": "t2v"})
    assert avm.resolve_upstream_model("a", same, FakeCtx(same)) == ("t2v", True, None)


def test_model_map_rejects_duplicate_keys_json_would_collapse():
    """JSON 同名键会静默覆盖，而"哪一条生效"决定账单 ⇒ 显式拒绝（大小写不同也拒）。"""
    options = opts(model_map={"a": "t2v", "A": "wan27"})
    exc = expect_failure(lambda: avm.resolve_upstream_model("t2v", options, FakeCtx(options)))
    assert exc.code == "channel_config_error", exc.code


def test_removed_channel_model_pin_is_rejected_loudly():
    """`X-Channel-Options.model`（渠道钉住槽位）2026-09-16 **撤除** ⇒ 遗留该键一律响亮失败。

    ⚠️ **值合法时也照拒**：撤除之后不存在"一致就放行"这条语义了。留一个"恰好一致的钉住"
    被静默接受，正好会养出"我以为它还钉着"的依赖 —— 而那个依赖已经没有任何代码支撑。
    修复动作是**删键**，不是让它过。
    """
    for value in ("wan27", "t2v", "not-a-model"):
        options = opts(model=value)
        exc = expect_failure(
            lambda: avm.resolve_upstream_model("t2v", options, FakeCtx(options))
        )
        assert exc.code == "channel_config_error", f"{value}: {exc.code}"
        assert "removed" in exc.message, exc.message          # 讲清是"撤除"，不是"拼错"
        assert "model_map" in exc.message, exc.message        # 指着替代方案


def test_wildcard_prefix_maps_a_whole_family():
    """`doubao-seedance-*` ⇒ 整个家族落到一个槽位，且**命中的模式可查**。"""
    options = opts(model_map={"doubao-seedance-*": "seedance20"})
    assert avm.resolve_upstream_model(
        "doubao-seedance-2-5-260628", options, FakeCtx(options)
    ) == ("seedance20", True, "doubao-seedance-*")


def test_single_star_forces_every_name_including_real_slot_names():
    """🔴 本决定的**语义核心**：通配优先于"名字本身就是槽位名"。

    `{"*": "t2v"}` = 渠道级的"本渠道只跑 t2v"，**合法槽位名 `wan27` 也会被改写**。
    这条断言是拿账单换来的：谁把顺序改回"槽位名优先"，它当场变红。
    """
    options = opts(model_map={"*": "t2v"})
    assert avm.resolve_upstream_model("wan27", options, FakeCtx(options)) == ("t2v", True, "*")
    assert avm.resolve_upstream_model("i2v_v3", options, FakeCtx(options)) == ("t2v", True, "*")


def test_exact_key_always_beats_a_wildcard():
    """精确命中压过通配 —— 否则"给某一个名字开例外"就做不到了。"""
    options = opts(
        model_map={"doubao-seedance-*": "t2v", "doubao-seedance-2-0-260128": "seedance20"}
    )
    assert avm.resolve_upstream_model(
        "doubao-seedance-2-0-260128", options, FakeCtx(options)
    ) == ("seedance20", True, None)
    assert avm.resolve_upstream_model(
        "doubao-seedance-2-5-260628", options, FakeCtx(options)
    ) == ("t2v", True, "doubao-seedance-*")


def test_two_matching_wildcards_are_refused_not_ranked():
    """两条通配同时命中 ⇒ 配置错误。**不排序、不取最长** —— 那是旧正则失败模式的复现。"""
    options = opts(model_map={"a*": "t2v", "*b": "wan27"})
    exc = expect_failure(lambda: avm.resolve_upstream_model("ab", options, FakeCtx(options)))
    assert exc.code == "channel_config_error", exc.code
    assert "a*" in exc.message and "*b" in exc.message, exc.message   # 两条都点名
    assert "refusing to pick" in exc.message, exc.message             # 明说不替它选


def test_question_mark_and_brackets_are_literals_not_metacharacters():
    """`*` 是**唯一**元字符：`?` / `[` / `]` 按字面量处理（少一个元字符就少一类误命中）。"""
    options = opts(model_map={"doubao?": "t2v"})
    exc = expect_failure(lambda: avm.resolve_upstream_model("doubaoX", options, FakeCtx(options)))
    assert exc.code == "InvalidParameter", exc.code


def test_wildcard_match_is_case_sensitive():
    """匹配大小写敏感 —— `fnmatch` 的 `normcase` 折叠是文件系统语义，不是模型名语义。"""
    options = opts(model_map={"doubao-*": "t2v"})
    exc = expect_failure(
        lambda: avm.resolve_upstream_model("DOUBAO-seedance-1", options, FakeCtx(options))
    )
    assert exc.code == "InvalidParameter", exc.code


def test_wildcard_hit_reaches_effective_so_the_rewrite_is_explainable():
    """🔴 **接线断言**（不是返回值断言）：强转能改模型值 ⇒"为什么被改了"必须能查到。

    响应体里没有 `model`（`ADR-011`）⇒ `effective.model_map_pattern` 是唯一的解释来源之一。
    只断言 `resolve_upstream_model` 的返回值 **不算** —— 那条路不经过 `effective`。
    """
    options = opts(model_map={"*": "t2v"})
    plan = avm.plan_create(
        body("wan27", [text("hi")], duration=5, resolution="720p", ratio="16:9"),
        options,
        FakeCtx(options),
    )
    assert plan["upstream_model"] == "t2v"
    assert plan["effective"]["model_map_pattern"] == "*", plan["effective"]
    assert plan["effective"]["model_requested"] == "wan27"


def test_wan27_resolution_literal_case_is_produced_by_the_script():
    """调用方按 Seedance 契约写 `720p`，脚本产出上游要求的 `720P`。"""
    plan = avm.plan_create(
        body("wan27", [text("x")], duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["resolution"] == "720P"


def test_resolution_case_is_tolerated_at_the_front_door():
    """大小写不承载语义 —— 不该因为调用方写了大写就 400。"""
    plan = avm.plan_create(
        body("happyhorse", [text("x")], duration=5, resolution="720P"), opts(), FakeCtx(opts())
    )
    assert plan["upstream_body"]["resolution"] == "720P"
    for bogus in ("4k", "2160p", "720"):
        exc = expect_failure(lambda b=bogus: avm.plan_create(
            body("happyhorse", [text("x")], duration=5, resolution=b), opts(), FakeCtx(opts()),
        ))
        assert "invalid enum value" in exc.message


# =============================================================================
# 2. 按模型分档：类型 / 档位 / 区间
# =============================================================================

def test_seedance20_sends_number_duration_and_number_resolution():
    plan = avm.plan_create(
        body("seedance20", [text("a cat")], duration=9, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    sent = plan["upstream_body"]
    assert sent["duration"] == 9 and isinstance(sent["duration"], int)
    assert sent["resolution"] == 720 and isinstance(sent["resolution"], int)
    assert sent["ratio"] == "16:9"
    assert sent["prompt"] == "a cat"


def test_seedance20_duration_clamped_to_interval_not_snapped():
    plan = avm.plan_create(
        body("seedance20", [text("x")], duration=30, resolution="480p", ratio="1:1"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["duration"] == 15          # 区间钳制
    assert any("outside seedance20 range" in w for w in plan["warnings"])


def test_t2v_duration_is_a_string_ladder():
    plan = avm.plan_create(
        body("t2v", [text("x")], duration=8, ratio="16:9"), opts(), FakeCtx(opts())
    )
    assert plan["upstream_body"]["duration"] == "8"


def test_t2v_duration_snap_says_it_crossed_up():
    plan = avm.plan_create(
        body("t2v", [text("x")], duration=6, ratio="16:9"), opts(), FakeCtx(opts())
    )
    assert plan["upstream_body"]["duration"] == "5"          # 就近 → 5（未跨档）
    plan = avm.plan_create(
        body("t2v", [text("x")], duration=7, ratio="16:9"), opts(), FakeCtx(opts())
    )
    assert plan["upstream_body"]["duration"] == "8"
    assert any("snapped UP" in w for w in plan["warnings"])


def test_wan27_keeps_resolution_case_and_string_duration():
    plan = avm.plan_create(
        body("wan27", [text("x")], duration=10, resolution="1080P", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    sent = plan["upstream_body"]
    assert sent["resolution"] == "1080P"
    assert sent["duration"] == "10" and isinstance(sent["duration"], str)


def test_minimax_resolution_is_lowercase_p():
    plan = avm.plan_create(
        body("minimax", [text("x")], duration=6, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["resolution"] == "720p"
    assert plan["upstream_body"]["tier"] == "turbo"


def test_minimax_has_no_480p_and_warns():
    plan = avm.plan_create(
        body("minimax", [text("x")], duration=6, resolution="480p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["resolution"] == "720p"
    assert any("480" in w for w in plan["warnings"])


def test_happyhorse_duration_is_number_in_interval():
    plan = avm.plan_create(
        body("happyhorse", [text("x")], duration=20, resolution="720P"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["duration"] == 15
    assert isinstance(plan["upstream_body"]["duration"], int)


# =============================================================================
# 3. 比例：必填 / 缺失 / adaptive / 吸附
# =============================================================================

def test_i2v_has_no_ratio_field_and_says_so():
    plan = avm.plan_create(
        body("i2v", [image(DATA_PNG, "first_frame")], duration=5, ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert "aspectRatio" not in plan["upstream_body"]
    assert "ratio" not in plan["upstream_body"]
    assert any("no ratio field" in w for w in plan["warnings"])


def test_seedance20_requires_ratio():
    exc = expect_failure(lambda: avm.plan_create(
        body("seedance20", [text("x")], duration=5, resolution="720p"),
        opts(), FakeCtx(opts()),
    ))
    assert exc.param == "ratio"
    assert "requires ratio" in exc.message


def test_minimax_maps_adaptive_to_auto():
    plan = avm.plan_create(
        body("minimax", [text("x")], duration=6, resolution="720p", ratio="adaptive"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["aspectRatio"] == "auto"


def test_seedance20_has_no_adaptive_and_falls_back_with_warning():
    plan = avm.plan_create(
        body("seedance20", [text("x")], duration=5, resolution="720p", ratio="adaptive"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["ratio"] == "16:9"
    assert any("adaptive" in w for w in plan["warnings"])


def test_unsupported_ratio_snaps_by_aspect_value():
    plan = avm.plan_create(
        body("wan27", [text("x")], duration=5, resolution="720P", ratio="21:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["ratio"] == "16:9"       # 21:9 不在 wan27 允许集
    assert any("snapped" in w for w in plan["warnings"])


# =============================================================================
# 4. content[] 降维：参考类不静默丢
# =============================================================================

def test_text_only_model_rejects_image_instead_of_degrading():
    exc = expect_failure(lambda: avm.plan_create(
        body("t2v", [text("x"), image(DATA_PNG, "first_frame")], duration=5, ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "silently degrading" in exc.message


def test_seedance20_multiple_reference_images_is_a_400():
    exc = expect_failure(lambda: avm.plan_create(
        body("seedance20", [text("x"), image("u1", "reference_image"), image("u2", "reference_image")],
             duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "exactly one reference image" in exc.message
    assert exc.code == "InvalidParameter"


def test_seedance20_last_frame_is_dropped_with_warning_when_first_exists():
    plan = avm.plan_create(
        body("seedance20", [text("x"), image("first.png", "first_frame"), image("last.png", "last_frame")],
             duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["image"] == "first.png"
    assert "lastFrameImage" not in plan["upstream_body"]
    assert any("last frame was dropped" in w for w in plan["warnings"])


def test_last_frame_only_is_a_400():
    exc = expect_failure(lambda: avm.plan_create(
        body("seedance20", [text("x"), image("last.png", "last_frame")],
             duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "last-frame-only" in exc.message


def test_role_is_accepted_both_at_item_level_and_inside_holder():
    at_item = avm.plan_create(
        body("seedance20", [text("x"), image("a.png", "first_frame")], duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )["upstream_body"]["image"]
    inside_holder = {"type": "image_url", "image_url": {"url": "a.png", "role": "first_frame"}}
    other = avm.plan_create(
        body("seedance20", [text("x"), inside_holder], duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )["upstream_body"]["image"]
    assert at_item == other == "a.png"


def test_multiple_text_items_are_joined_in_order():
    plan = avm.plan_create(
        body("seedance20", [text("first"), text("second")], duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["prompt"] == "first\nsecond"


def test_minimax_full_surface_maps_field_names():
    plan = avm.plan_create(
        body("minimax", [
            text("prompt here"),
            image("f.png", "first_frame"),
            image("l.png", "last_frame"),
        ], duration=10, resolution="1080p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    sent = plan["upstream_body"]
    assert sent["content"] == "prompt here"        # minimax 用 content 而非 prompt
    assert sent["imageUrl"] == "f.png" and sent["lastFrameUrl"] == "l.png"
    assert "prompt" not in sent


def test_minimax_frames_and_references_are_mutually_exclusive():
    exc = expect_failure(lambda: avm.plan_create(
        body("minimax", [
            text("x"), image("f.png", "first_frame"), image("r.png", "reference_image"),
        ], duration=10, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "cannot combine" in exc.message


def test_minimax_reference_audio_needs_image_or_video():
    exc = expect_failure(lambda: avm.plan_create(
        body("minimax", [text("x"), {"type": "audio_url", "audio_url": {"url": DATA_MP3},
                                    "role": "reference_audio"}],
             duration=10, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "audio" in exc.message


def test_minimax_reference_limits_are_enforced():
    exc = expect_failure(lambda: avm.plan_create(
        body("minimax", [text("x")] + [image(f"r{i}.png", "reference_image") for i in range(5)],
             duration=10, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "at most 4 reference images" in exc.message


def test_happyhorse_multi_image_becomes_an_array():
    plan = avm.plan_create(
        body("happyhorse", [text("x"), image("a.png", "reference_image"), image("b.png", "reference_image")],
             duration=8, resolution="720P", ratio="16:9"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["image"] == ["a.png", "b.png"]
    assert "ratio" not in plan["upstream_body"]        # i2v/r2v 模式下上游不使用 ratio
    assert any("ignores ratio" in w for w in plan["warnings"])


def test_happyhorse_t2v_keeps_ratio():
    plan = avm.plan_create(
        body("happyhorse", [text("x")], duration=8, resolution="720P", ratio="9:16"),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["ratio"] == "9:16"
    assert "image" not in plan["upstream_body"]


def test_i2v_requires_a_first_frame():
    exc = expect_failure(lambda: avm.plan_create(
        body("i2v", [text("x")], duration=5, ratio="16:9"), opts(), FakeCtx(opts()),
    ))
    assert "requires a first frame" in exc.message


def test_i2v_drops_last_frame_with_warning():
    plan = avm.plan_create(
        body("i2v_v3", [image("f.png", "first_frame"), image("l.png", "last_frame")], duration=10),
        opts(), FakeCtx(opts()),
    )
    assert plan["upstream_body"]["image"] == "f.png"
    assert any("last frame was dropped" in w for w in plan["warnings"])


def test_draft_task_is_rejected():
    exc = expect_failure(lambda: avm.plan_create(
        body("seedance20", [text("x"), {"type": "draft_task", "draft_task": {"id": "d1"}}],
             duration=5, resolution="720p", ratio="16:9"),
        opts(), FakeCtx(opts()),
    ))
    assert "draft_task" in exc.message


def test_wan27_prompt_extend_passthrough():
    payload = body("wan27", [text("x")], duration=5, resolution="720P", ratio="16:9")
    payload["extra_body"] = {"aivideomaker_prompt_extend": True}
    plan = avm.plan_create(payload, opts(), FakeCtx(opts()))
    assert plan["upstream_body"]["promptExtend"] is True


# =============================================================================
# 5. unsupported：引擎兑现的字段不报，上游没有的才报
# =============================================================================

def test_engine_level_fields_are_not_reported_unsupported():
    payload = body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9",
                   callback_url="https://me.example/cb", execution_expires_after=3600)
    plan = avm.plan_create(payload, opts(), FakeCtx(opts()))
    assert "callback_url" not in plan["unsupported"]
    assert "execution_expires_after" not in plan["unsupported"]


def test_upstream_missing_capabilities_are_reported():
    payload = body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9",
                   watermark=True, seed=7, return_last_frame=True, draft=True)
    plan = avm.plan_create(payload, opts(), FakeCtx(opts()))
    for key in ("watermark", "seed", "return_last_frame", "draft"):
        assert key in plan["unsupported"], key


def test_unsupported_fields_are_looked_up_in_extra_body_too():
    payload = body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9")
    payload["extra_body"] = {"omni_reference_task_type": "reference", "output_format": "mov"}
    plan = avm.plan_create(payload, opts(), FakeCtx(opts()))
    assert "omni_reference_task_type" in plan["unsupported"]
    assert "output_format" in plan["unsupported"]


# =============================================================================
# 6. 支出上限：提交前置条件
# =============================================================================

def test_missing_spend_cap_is_refused():
    payload = body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9")
    exc = expect_failure(lambda: avm.plan_create(payload, {}, FakeCtx({})))
    assert "refusing to submit without it" in exc.message
    assert exc.param == "extra_body.aivideomaker_max_credits"


def test_estimate_above_cap_is_refused_before_any_request():
    payload = body("happyhorse", [text("x")], duration=15, resolution="1080P")
    exc = expect_failure(lambda: avm.plan_create(payload, opts(max_credits=100), FakeCtx(opts(max_credits=100))))
    assert "exceeds the spend cap" in exc.message
    assert "750" in exc.message          # 15 × 50


def test_request_level_cap_can_only_tighten_the_channel_cap():
    payload = body("t2v", [text("x")], duration=5, ratio="16:9")
    payload["extra_body"] = {"aivideomaker_max_credits": 99_999}
    plan = avm.plan_create(payload, opts(max_credits=30), FakeCtx(opts(max_credits=30)))
    assert plan["max_credits"] == 30
    assert plan["estimated_credits"] == 15      # 5 × 3


def test_unpriced_model_is_refused_without_explicit_opt_in():
    """上游没有计费前闸门 ⇒ 算不出成本时**默认拒绝**，只有显式 opt-in 才放行（ADR-004）。"""
    options = opts(allow_unpriced=False)
    exc = expect_failure(lambda: avm.plan_create(
        body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9"),
        options, FakeCtx(options),
    ))
    assert "credit_table" in exc.message
    assert "allow_unpriced" in exc.message
    assert exc.param == "extra_body.aivideomaker_max_credits"


def test_channel_credit_table_prices_a_dynamic_model():
    """渠道给出每秒费率 ⇒ 动态计价模型恢复可估，不再需要 opt-in。"""
    options = opts(allow_unpriced=False, credit_table={"seedance20": 12})
    plan = avm.plan_create(
        body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9"),
        options, FakeCtx(options),
    )
    assert plan["estimated_credits"] == 60           # 5 × 12
    assert not any("not verifiable" in w for w in plan["warnings"])


def test_channel_credit_table_can_also_block_over_cap():
    options = opts(allow_unpriced=False, max_credits=50, credit_table={"seedance20": 12})
    exc = expect_failure(lambda: avm.plan_create(
        body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9"),
        options, FakeCtx(options),
    ))
    assert "exceeds the spend cap" in exc.message


def test_unpriced_model_passes_with_channel_opt_in_and_says_so():
    options = opts(allow_unpriced=True)
    plan = avm.plan_create(
        body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9"),
        options, FakeCtx(options),
    )
    assert plan["estimated_credits"] is None
    assert any("not verifiable" in w for w in plan["warnings"])


def test_request_level_opt_in_also_works():
    """请求级也能开 —— 渠道没开时，个别调用方可以自己认下不可验证成本。"""
    payload = body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9")
    payload["extra_body"] = {"aivideomaker_allow_unpriced": True}
    options = opts(allow_unpriced=False)
    plan = avm.plan_create(payload, options, FakeCtx(options))
    assert plan["estimated_credits"] is None


def test_credit_formulas_match_the_official_doc():
    assert avm.estimate_credits("t2v", 8, None) == 24
    assert avm.estimate_credits("i2v_v3", 20, None) == 80
    assert avm.estimate_credits("minimax", 10, "720p", "turbo") == 30
    assert avm.estimate_credits("minimax", 10, "1080p", "base") == 50
    assert avm.estimate_credits("wan27", 15, "720P") == 150
    assert avm.estimate_credits("wan27", 15, "1080P") == 225
    assert avm.estimate_credits("happyhorse", 3, "720P") == 75
    assert avm.estimate_credits("seedance20", 5, 720) is None


# =============================================================================
# 7. 状态与任务规范化
# =============================================================================

def test_status_mapping_covers_the_documented_set():
    assert avm.map_status("SUBMITTED") == "queued"
    assert avm.map_status("PROGRESS") == "running"
    assert avm.map_status("COMPLETED") == "succeeded"
    assert avm.map_status("FAILED") == "failed"
    assert avm.map_status("CANCEL") == "cancelled"
    assert avm.map_status("SOMETHING_NEW") == "running"      # 未知不猜终态


def test_normalize_task_shape_and_epoch_seconds():
    raw = {
        "id": "ck1", "status": "COMPLETED", "model": "seedance20",
        "input": {"prompt": "x", "duration": 5, "resolution": 720, "ratio": "16:9"},
        "output": {"url": "https://cdn.example.com/out.mp4"},
        "createdAt": "2026-03-19T08:00:00.000Z",
        "completedAt": "2026-03-19T08:05:00.000Z",
        "creditsCharged": 12, "creditsRefunded": 0,
    }
    task = avm.normalize_task(raw)
    assert task["status"] == "succeeded"
    assert task["video_url"] == "https://cdn.example.com/out.mp4"
    assert task["resolution"] == "720p"
    assert task["ratio"] == "16:9"
    assert isinstance(task["created_at"], int) and task["created_at"] > 1_700_000_000
    assert task["error"] is None
    assert task["usage"]["credits"] == 12
    assert task["usage"]["completion_tokens"] == 12
    assert task["upstream"]["id"] == "ck1"


def test_failed_task_reports_error_and_net_zero_credits():
    raw = {"id": "ck2", "status": "FAILED", "input": {}, "output": {},
           "creditsCharged": 20, "creditsRefunded": 20, "message": "Insufficient credits"}
    task = avm.normalize_task(raw)
    assert task["status"] == "failed"
    assert task["error"]["code"] == "GenerationFailed"
    assert task["usage"]["credits"] == 0
    assert task["usage"]["completion_tokens"] == 0


def test_duration_is_normalised_to_a_number():
    """真实冒烟发现的偏差：上游把 duration 当**字符串**回（"5"），
    而 Seedance 契约里它是 integer 秒 —— 不转就会被调用方按字符串处理。"""
    raw = {"status": "COMPLETED", "input": {"duration": "5"}, "output": {"url": "u"}}
    task = avm.normalize_task(raw)
    assert task["duration"] == 5
    assert isinstance(task["duration"], int)

    numeric = avm.normalize_task({"status": "PROGRESS", "input": {"duration": 7}, "output": {}})
    assert numeric["duration"] == 7 and isinstance(numeric["duration"], int)

    missing = avm.normalize_task({"status": "PROGRESS", "input": {}, "output": {}})
    assert missing["duration"] is None


def test_credits_per_token_multiplier_is_configurable():
    raw = {"status": "COMPLETED", "input": {}, "output": {"url": "u"}, "creditsCharged": 5}
    task = avm.normalize_task(raw, credits_per_token=100)
    assert task["usage"]["completion_tokens"] == 500
    assert task["usage"]["credits"] == 5


# =============================================================================
# 8. 相位：URL 拼装 / 创建响应 / 取消
# =============================================================================

def test_create_request_builds_url_from_base():
    ctx = FakeCtx(opts())
    plan = asyncio.run(avm.create_request(
        ctx, body("seedance20", [text("x")], duration=5, resolution="720p", ratio="16:9")
    ))
    assert plan["url"] == "https://aivideomaker.ai/api/v1/generate/seedance20"
    assert plan["method"] == "POST"


def test_create_request_expands_model_placeholder_in_channel_url():
    ctx = FakeCtx(opts(), upstream_url="https://aivideomaker.ai/api/v1/generate/{model}")
    plan = asyncio.run(avm.create_request(
        ctx, body("wan27", [text("x")], duration=5, resolution="720P", ratio="16:9")
    ))
    assert plan["url"] == "https://aivideomaker.ai/api/v1/generate/wan27"


def test_create_request_rewrites_a_full_endpoint_url():
    ctx = FakeCtx(opts(), upstream_url="https://aivideomaker.ai/api/v1/generate/t2v")
    plan = asyncio.run(avm.create_request(
        ctx, body("i2v_v3", [image(DATA_PNG, "first_frame")], duration=5)
    ))
    assert plan["url"] == "https://aivideomaker.ai/api/v1/generate/i2v_v3"


def test_webhook_header_comes_only_from_channel_options():
    ctx = FakeCtx(opts(webhook_url="https://internal.example/hook"))
    payload = body("t2v", [text("x")], duration=5, ratio="16:9", callback_url="https://caller.example/cb")
    plan = asyncio.run(avm.create_request(ctx, payload))
    assert plan["headers"]["webhookUrl"] == "https://internal.example/hook"
    assert "callback_url" not in plan["body"]        # 调用方的回调绝不下发给上游


def test_create_response_returns_task_id():
    out = asyncio.run(avm.create_response(FakeCtx(opts()), {"status": "SUBMITTED", "taskId": "ck9"}))
    assert out == {"task_id": "ck9"}


def test_create_response_failed_with_credits_maps_to_429():
    exc = expect_failure(lambda: asyncio.run(avm.create_response(
        FakeCtx(opts()), {"status": "FAILED", "message": "Insufficient credits"}
    )))
    assert exc.code == "QuotaExceeded" and exc.status == 429


def test_create_response_without_task_id_is_an_error():
    exc = expect_failure(lambda: asyncio.run(avm.create_response(
        FakeCtx(opts()), {"status": "SUBMITTED"}
    )))
    assert exc.code == "InvalidParameter"


def test_query_request_uses_the_detail_endpoint():
    ctx = FakeCtx(opts(), task=FakeTask("ck7", "running"))
    plan = asyncio.run(avm.query_request(ctx, {}))
    assert plan["url"] == "https://aivideomaker.ai/api/v1/tasks/ck7"
    assert plan["method"] == "GET"


def test_cancel_request_is_put_to_the_cancel_path():
    ctx = FakeCtx(opts(), task=FakeTask("ck7", "queued"))
    plan = asyncio.run(avm.cancel_request(ctx, {}))
    assert plan["url"] == "https://aivideomaker.ai/api/v1/tasks/ck7/cancel"
    assert plan["method"] == "PUT"


def test_cancel_refuses_a_non_queued_task():
    ctx = FakeCtx(opts(), task=FakeTask("ck7", "running"))
    exc = expect_failure(lambda: asyncio.run(avm.cancel_request(ctx, {})))
    assert "only queued tasks can be cancelled" in exc.message


def test_cancel_response_normalises_to_cancelled():
    out = asyncio.run(avm.cancel_response(FakeCtx(opts()), {"status": "CANCEL"}))
    assert out == {"status": "cancelled"}


def test_query_response_uses_credits_per_token_option():
    ctx = FakeCtx(opts(credits_per_token=10), task=FakeTask())
    out = asyncio.run(avm.query_response(ctx, {
        "status": "COMPLETED", "input": {}, "output": {"url": "u"}, "creditsCharged": 3,
    }))
    assert out["usage"]["completion_tokens"] == 30


# =============================================================================
# 9. 相位声明与 purity 守卫
# =============================================================================

def test_phases_are_declared():
    for phase in ("create_request", "create_response", "query_request",
                  "query_response", "cancel_request", "cancel_response"):
        assert phase in avm.PHASES
        assert callable(getattr(avm, phase))


def test_script_never_reads_the_environment():
    """纯函数纪律：脚本不得自行读环境变量（env 由引擎显式注入）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("os.environ", "getenv", "import os"):
        assert forbidden not in source, forbidden


def test_script_does_not_do_network_io():
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("requests.", "httpx", "urllib.request", "socket."):
        assert forbidden not in source, forbidden


def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:                     # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"  ok    {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        print("\nFailed:")
        for name, exc in failed:
            print(f"  - {name}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
