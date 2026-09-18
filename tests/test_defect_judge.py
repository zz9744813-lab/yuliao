"""缺陷口径回归（2026-09-16）。

**为什么会有这个口径**：大样本诊断（n=99，少数类 36）显示「整段哪边更好」这个
整体偏好任务上评委的 AUC ≈ 0.50，即无可测信息。于是换任务：
让评委只做「指缺陷」，胜负由**缺陷计数**导出。

本文件锁死三件事：
1. **计数规则预先固定**：缺陷少者胜、相等为 equal。不允许事后调参（否则是过拟合）。
2. **非法条目不计入**：非法 kind / 空 quote 不得用于凑数——否则模型只要多吐几行
   就能操纵胜负。
3. **胜负映射到 human/candidate 时要用 human_was_a**，不能直接照抄 A/B。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.judges import (DEFECT_KINDS, PROMPT_VARIANTS,  # noqa: E402
                        PREFERENCE_PROMPT_DEFECT, _defect_winner)


# ── ① 计数规则 ────────────────────────────────────────────────
def test_fewer_defects_wins():
    w, na, nb = _defect_winner({"A": [], "B": [{"kind": "用词", "quote": "x"}]})
    assert (w, na, nb) == ("A", 0, 1), "A 缺陷少 → A 胜"


def test_tie_is_equal():
    w, na, nb = _defect_winner({"A": [{"kind": "用词", "quote": "x"}],
                               "B": [{"kind": "节奏", "quote": "y"}]})
    assert (w, na, nb) == ("equal", 1, 1)


def test_more_defects_loses_even_with_different_kinds():
    w, na, nb = _defect_winner({
        "A": [{"kind": "用词", "quote": "a"}],
        "B": [{"kind": "用词", "quote": "b"}, {"kind": "意象", "quote": "c"},
              {"kind": "逻辑", "quote": "d"}]})
    assert (w, na, nb) == ("A", 1, 3)


# ── ② 非法条目不计数（防凑数）────────────────────────────────
def test_invalid_kind_not_counted():
    """非法 kind 不能计入——否则模型随便写个类型就能操纵胜负。"""
    w, na, nb = _defect_winner({"A": [{"kind": "胡编的类型", "quote": "x"}], "B": []})
    assert (w, na, nb) == ("equal", 0, 0), "非法 kind 应被忽略"


def test_empty_quote_not_counted():
    """空/纯空白 quote 不能计入——否则可以只给类型不给位置来凑数。"""
    for bad in ("", "   ", None):
        w, na, _ = _defect_winner({"A": [{"kind": "用词", "quote": bad}], "B": []})
        assert na == 0, f"quote={bad!r} 不该被计入"


def test_non_dict_items_ignored():
    """模型偶尔会吐字符串数组而非对象数组——不能因此崩掉。"""
    w, na, nb = _defect_winner({"A": ["解释过度", 123, None], "B": []})
    assert (w, na, nb) == ("equal", 0, 0)


def test_missing_or_none_side_treated_as_zero():
    assert _defect_winner({"A": None, "B": [{"kind": "用词", "quote": "y"}]})[0] == "A"
    assert _defect_winner({"B": [{"kind": "用词", "quote": "y"}]})[0] == "A"
    assert _defect_winner({})[0] == "equal"


def test_non_list_side_treated_as_zero():
    assert _defect_winner({"A": "oops", "B": []})[0] == "equal"


def test_all_legal_kinds_accepted():
    """词表里每个类型都应被接受——词表与校验必须同源，否则某个类型白写。"""
    for k in DEFECT_KINDS:
        assert _defect_winner({"A": [{"kind": k, "quote": "x"}], "B": []})[1] == 1, k


# ── ③ 口径注册 ────────────────────────────────────────────────
def test_variant_registered():
    assert "defect" in PROMPT_VARIANTS
    prompt, version = PROMPT_VARIANTS["defect"]
    assert version == "judge_defect_v1_heldout"
    assert prompt is PREFERENCE_PROMPT_DEFECT


def test_prompt_requires_quotes_and_forbids_hard_finding():
    """提示词必须（a）要求引用原文（b）明确禁止凑数（c）明确不判整体偏好。

    这三条是防止"多吐几行操纵胜负"的关键，删掉任何一条都会让口径退化。
    """
    p = PREFERENCE_PROMPT_DEFECT
    assert "不要判断哪一段整体更好" in p, "必须明确不判整体偏好"
    assert "引用原文片段" in p, "必须要求引用原文——否则无法核验位置"
    assert "不要为了凑数硬找" in p, "必须禁止凑数"
    for k in DEFECT_KINDS:
        assert k in p, f"类型 {k} 未写进提示词"
