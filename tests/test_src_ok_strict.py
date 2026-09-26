"""src_ok 读取侧「严格布尔、非字典/非布尔一律未校验」回归（lg-fix-src-ok-nondict）。

钉住的事（scripts/k2_extract_backfill.py 唯一入口 src_ok_strict / src_ok_state /
integrity_flag_state）：
1. 十种输入逐条钉死返回值：{} / {"src_ok": true} / {"src_ok": false} /
   {"src_ok": "false"} / {"src_ok": 1} / [1,2] / "ok" / null / 空串 / 非法 JSON；
2. 任何输入**绝不抛异常**（旧实现在合法 JSON 但非字典时对 .get 抛
   AttributeError，整批中断——本回归把这条形状钉死为 False=未校验不过闸）；
3. 三态口径：False（查过判坏）与 None（未校验）可区分（export_training 的
   bad_src / src_unverified 互斥口径依赖它）；
4. 消费方同源：normalize_typos._json_ok 与 factorial_blind_multicorpus._eligible
   走同一判定层，bool(...) 松判形状不再存在。
"""
from __future__ import annotations

import sys
from pathlib import Path
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import k2_extract_backfill as k2b                     # noqa: E402
import normalize_typos as NT                          # noqa: E402
import factorial_blind_multicorpus as FBM             # noqa: E402

# ── 十种输入逐条钉死（integrity 原文形态）────────────────────────────
CASES = [
    ("{}",                    False),   # 空字典：无 src_ok 键 → 未校验
    ('{"src_ok": true}',      True),    # 唯一过闸形状：JSON 布尔 true
    ('{"src_ok": false}',     False),   # 查过且判坏
    ('{"src_ok": "false"}',   False),   # 字符串 false（bool() 旧缺陷形状）
    ('{"src_ok": 1}',         False),   # 数字 1（bool() 旧缺陷形状）
    ("[1,2]",                 False),   # 合法 JSON 非字典：旧实现此处抛 AttributeError
    ('"ok"',                  False),   # 合法 JSON 标量字符串：旧实现同样炸 .get
    ("null",                  False),   # 合法 JSON null：旧实现同样炸 .get
    ("",                      False),   # 空串
    ("{not json at all",      False),   # 非法 JSON
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_src_ok_strict_pins_ten_inputs(raw, expected):
    assert k2b.src_ok_strict(raw) is expected


@pytest.mark.parametrize("raw,_expected", CASES)
def test_src_ok_strict_never_raises_string(raw, _expected):
    try:
        k2b.src_ok_strict(raw)
    except Exception as e:                              # noqa: BLE001
        pytest.fail(f"src_ok_strict({raw!r}) 抛异常：{type(e).__name__}: {e}")


@pytest.mark.parametrize("raw,expected", CASES)
def test_src_ok_strict_accepts_segment_objects(raw, expected):
    seg = types.SimpleNamespace(integrity=raw)
    assert k2b.src_ok_strict(seg) is expected


@pytest.mark.parametrize("raw,expected", [
    (None,  False),   # 列 NULL
    ({},   False),   # 已解析的空 dict
    ({"src_ok": True}, True),
    ({"src_ok": False}, False),
    ({"src_ok": "true"}, False),
    ([1, 2], False),   # 非字典对象
    (42,   False),   # 杂类型
])
def test_src_ok_strict_edge_shapes(raw, expected):
    assert k2b.src_ok_strict(raw) is expected


# ── 三态口径（export_training bad_src / src_unverified 互斥报告的依赖）──
@pytest.mark.parametrize("raw,state", [
    ('{"src_ok": true}',  True),
    ('{"src_ok": false}', False),
    ("{}",                   None),   # 缺键 = 未校验
    ('{"src_ok": "false"}', None),   # 类型不严 = 未校验（不许当成"查过"）
    ('{"src_ok": 1}',       None),
    ('{"src_ok": null}',    None),
    ("[1,2]",               None),   # 非字典 = 未校验，不抛
    ("bogus{",              None),   # 非法 JSON = 未校验，不抛
])
def test_src_ok_state_tristate(raw, state):
    assert k2b.src_ok_state(raw) is state


def test_bad_src_and_unverified_are_distinguishable():
    """False（判坏）与「缺键/非布尔」（未校验）互斥：bad_src 侧必须不是 None。"""
    bad = k2b.src_ok_state('{"src_ok": false}')
    unverified = k2b.src_ok_state("{}")
    assert bad is False and unverified is None
    assert not k2b.src_ok_strict('{"src_ok": false}')
    assert not k2b.src_ok_strict("{}")


# ── 通用 flag（factorial 的 eligible 闸同源：严格布尔，去掉 bool(...)）──
@pytest.mark.parametrize("raw,expected", [
    ('{"eligible": true}',   True),
    ('{"eligible": false}',  False),
    ('{"eligible": "false"}', False),  # bool("false")==True 的旧缺陷形状必须不过闸
    ('{"eligible": 1}',       False),  # bool(1)==True 同上
    ("[1,2]",                 False),  # 旧实现在非字典 JSON 上抛 AttributeError
    ("",                      False),
    ("nope{",                 False),
])
def test_integrity_flag_state_eligible(raw, expected):
    assert (k2b.integrity_flag_state(raw, "eligible") is True) is expected


# ── 消费方同源（判定层塌到唯一入口，行为一致）────────────────────────
def test_normalize_typos_json_ok_is_same_source():
    for raw, expected in CASES:
        assert NT._json_ok(raw) is expected, f"_json_ok({raw!r}) 与唯一入口口径漂移"
    assert NT.src_ok_strict is k2b.src_ok_strict   # 真同源：同一函数对象


def test_factorial_eligible_rejects_bool_shape():
    ok = types.SimpleNamespace(integrity='{"eligible": true}')
    sfalse = types.SimpleNamespace(integrity='{"eligible": "false"}')
    nondict = types.SimpleNamespace(integrity='[1,2]')
    empty = types.SimpleNamespace(integrity="")
    assert FBM._eligible(ok) is True
    assert FBM._eligible(sfalse) is False       # bool(...) 松判形状已消灭
    assert FBM._eligible(nondict) is False      # 不再抛 AttributeError
    assert FBM._eligible(empty) is False


def test_consumers_import_entry_from_k2_single_source():
    """调用点必须 import 唯一入口，不许再手写 json.loads(...).get("src_ok")。"""
    import re
    pat = re.compile(r"""json\.loads\([^)]*\)\s*\.get\(\s*["']src_ok["']""")
    for fname in ("normalize_typos.py", "benchmark_build.py", "goldpick_build.py",
                  "export_training.py", "factorial_blind_multicorpus.py",
                  "k2_extract_backfill.py"):
        text = (ROOT / "scripts" / fname).read_text(encoding="utf-8")
        assert not pat.search(text), f"{fname} 仍有手写 src_ok 解析（口径漂移源）"
