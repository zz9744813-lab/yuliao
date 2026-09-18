import pytest

from app.frames_schema import validate_frame


def test_frame_s_minimal_ok():
    m, err = validate_frame("S", {"event": "两人对峙"})
    assert err is None
    assert m.event == "两人对峙"
    assert m.granularity == "S"


def test_frame_m_full():
    m, err = validate_frame("M", {
        "event": "他怀疑对方说谎",
        "facts": [{"statement": "对方有异常", "certainty": "likely", "source": "主角"}],
        "reader_should_infer": ["他没揭穿"],
        "must_not_state": ["对方一定撒谎"],
        "expression_constraints": {"explicitness": "low"},
    })
    assert err is None
    assert m.facts[0].certainty == "likely"
    assert m.expression_constraints.explicitness == "low"


def test_frame_m_bad_explicitness_rejected():
    _, err = validate_frame("M", {
        "event": "x",
        "expression_constraints": {"explicitness": "VERY_SECRET"},
    })
    assert err is not None


def test_frame_l_beats():
    m, err = validate_frame("L", {
        "event": "x",
        "beats": ["他进门", "看见账目", "没说话"],
        "emotion_intensity": 0.4,
        "pov": "贴主角",
    })
    assert err is None
    assert m.beats[1] == "看见账目"
    assert m.emotion_intensity == 0.4


# ── M-gap 根因修复（2026-09-14）：normalize_frame_payload 契约宽容层 ──


def test_normalize_fact_alias_content_rescued():
    m, err = validate_frame("M", {"event": "x",
                                  "facts": [{"content": "对方有异常", "certainty": "likely"}]})
    assert err is None and m.facts[0].statement == "对方有异常"


def test_normalize_fact_alias_fact_rescued():
    m, err = validate_frame("M", {"event": "x", "facts": [{"fact": "他已离开"}]})
    assert err is None and m.facts[0].statement == "他已离开"


def test_normalize_null_lists_become_empty():
    m, err = validate_frame("M", {
        "event": "x",
        "facts": None, "reader_should_infer": None, "must_not_state": None,
        "character_state": {"knows": None, "does_not_know": None},
    })
    assert err is None
    assert m.facts == [] and m.reader_should_infer == [] and m.must_not_state == []
    assert m.character_state.knows == [] and m.character_state.does_not_know == []


def test_normalize_semicolon_string_becomes_list():
    m, err = validate_frame("M", {
        "event": "x",
        "reader_should_infer": "两人有权力差；她选择硬撑；反抗被打断",
        "character_state": {"knows": "自己再败的事实", "does_not_know": "失败的原因"},
    })
    assert err is None
    assert m.reader_should_infer == ["两人有权力差", "她选择硬撑", "反抗被打断"]
    assert m.character_state.knows == ["自己再败的事实"]
    assert m.character_state.does_not_know == ["失败的原因"]


def test_normalize_unwraps_single_key_wrapper():
    m, err = validate_frame("M", {"frame_m": {"event": "他离开", "facts": None}})
    assert err is None and m.event == "他离开"


def test_normalize_multichar_state_NOT_rescued():
    # 按角色拆数组语义有损（取谁的态都算编造），必须保持失败
    _, err = validate_frame("M", {
        "event": "x",
        "character_state": [{"character": "甲", "visible_emotion": "怒"},
                            {"character": "乙", "visible_emotion": "惧"}],
    })
    assert err is not None


def test_normalize_does_not_touch_valid_statement():
    m, err = validate_frame("M", {
        "event": "x",
        "facts": [{"statement": "正常的", "certainty": "certain"}],
        "character_state": {"knows": ["a"], "does_not_know": ["b"]},
    })
    assert err is None and m.facts[0].statement == "正常的"
    assert m.character_state.knows == ["a"]
