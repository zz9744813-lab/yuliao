"""上下文供给回归（2026-09-14）。

事故：preference judge 最初只把两段裸文本发给评委，而前端取题是**带上文**的
（scene_context 回溯到场景起点）。这造成"用户带 4304 字上下文判、Judge 空手判"的
不对称比较，实测把评委的挑 candidate 比例推到 0.93–1.00，κ 压到 ≈0。
而 calibration-report-v1 §三 早已立下**强制**规程：human vs candidate 必须供 ≥prev1 上下文
（segment_only 下 human 1/10，带 prev1 后 7/10）。

本文件锁死：① scene_context 的行为（含 seg_version 过滤）；
② judge_preference 必须把 context 真的写进 prompt。
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app import judges as J
from app.context_ablation import scene_context
from app.models import Segment, Work


def _mk_work(segments):
    """segments = [(ordinal, seg_version, text, scene_boundary_bool), ...]"""
    db.init_db()
    with db.session() as s:
        w = Work(title="ctx-test", source="test:seed")
        s.add(w)
        s.flush()
        for ord_, ver, text, sb in segments:
            s.add(Segment(work_id=w.id, ordinal=ord_, seg_version=ver, text=text,
                          n_sentences=1, n_chars=len(text),
                          integrity=json.dumps({"scene_boundary": 1.0 if sb else 0.0})))
        s.commit()
        wid = w.id
    return wid


def test_scene_context_stops_at_scene_boundary():
    """回溯到 scene_boundary==1 的段（含）即停，且按阅读顺序返回。"""
    wid = _mk_work([(0, 2, "段0", False), (1, 2, "段1", False), (2, 2, "段2", True),
                    (3, 2, "段3", False), (4, 2, "段4", False)])
    with db.session() as s:
        cur = s.query(Segment).filter_by(work_id=wid, ordinal=4).one()
        texts, mode = scene_context(s, cur)
    assert texts == ["段2", "段3"], f"应停在场景起点并保持顺序，实得 {texts}"
    assert mode == "scene2"


def test_scene_context_filters_by_seg_version():
    """v1/v2 混存时 ordinal 是两套坐标系，必须按 seg_version 过滤（串线 bug 回归）。"""
    wid = _mk_work([(0, 2, "v2-段0", False), (1, 2, "v2-段1", False), (2, 2, "v2-段2", False),
                    (1, 1, "v1-段1", False), (2, 1, "v1-段2", False)])
    with db.session() as s:
        cur = s.query(Segment).filter_by(work_id=wid, seg_version=2, ordinal=2).one()
        texts, _ = scene_context(s, cur)
    assert all(t.startswith("v2-") for t in texts), f"混入了别的切分版本：{texts}"
    assert texts == ["v2-段0", "v2-段1"]


def test_scene_context_respects_limit():
    wid = _mk_work([(i, 2, f"段{i}", False) for i in range(10)])
    with db.session() as s:
        cur = s.query(Segment).filter_by(work_id=wid, ordinal=9).one()
        texts, mode = scene_context(s, cur, limit=3)
    assert texts == ["段6", "段7", "段8"] and mode == "scene3"


def _capture(monkeypatch):
    box = {}

    def fake(prompt, purpose, version, model):
        box["prompt"] = prompt
        return {"status": "ok", "payload": {"winner": "A", "confidence": 0.9,
                                            "reasons": ["winner:动作外显"]}}

    monkeypatch.setattr(J, "_judge_call", fake)
    return box


def test_judge_preference_puts_context_in_prompt(monkeypatch):
    """核心回归：给了 context 就必须出现在 prompt 里。"""
    box = _capture(monkeypatch)
    J.judge_preference(human_text="人类那一段", candidate_text="候选那一段",
                       model="m", rng=random.Random(1), context="这是前文的上文内容")
    assert "这是前文的上文内容" in box["prompt"]
    assert "人类那一段" in box["prompt"] and "候选那一段" in box["prompt"]


def test_judge_preference_without_context_is_explicit(monkeypatch):
    """没给 context 时要显式标注"无上文"，不能静默变成裸比较。"""
    box = _capture(monkeypatch)
    J.judge_preference(human_text="人类", candidate_text="候选",
                       model="m", rng=random.Random(1), context=None)
    assert "无上文" in box["prompt"]


def test_judge_preference_warns_against_self_containment(monkeypatch):
    """prompt 必须含"别因为更自足就判更好"的约束——这是段生候选的天然优势，需明示抵消。"""
    box = _capture(monkeypatch)
    J.judge_preference(human_text="人类", candidate_text="候选",
                       model="m", rng=random.Random(1), context="上文")
    assert "独立成篇" in box["prompt"]


def test_prompt_template_has_context_placeholder():
    assert "{context_block}" in J.PREFERENCE_PROMPT


def test_prompt_carries_author_preference_rubric():
    """v4 起 rubric 写入集霸偏好的可测维度（紧凑而流畅）。

    这些维度来自 `scripts/pref_drivers.py` 的统计（句数少/句长更长/连接词少/整体不啰嗦，
    嵌套 CV AUC 0.650）。锁在测试里，防止后续改 prompt 时被无声删掉——
    否则"对齐口径"实验就变成空转。
    """
    p = J.PREFERENCE_PROMPT
    assert "句子少而长" in p, "缺「句子少而长」偏好"
    assert "连接词少" in p, "缺「连接词少」偏好"
    assert "紧凑而流畅" in p, "缺总体风格定调"
    assert "长句占比高" in p, "缺长句占比的方向说明"


def test_prompt_version_bumped_for_rubric_change():
    """rubric 变了必须升版本，否则幂等键会把旧产物当新产物复用（P1-2 教训）。"""
    assert J.PREFERENCE_PROMPT_VERSION >= "judge_preference_v4"
