"""K2 成对对照抽取器回归（scripts/k2_contrast_extract.py，主控派工 2026-09-23）。

钉住的事：
1. 既有三道机械门（关键词/场景/长度）各自的接受与拒绝（拒绝理由可读、
   含既定形态文本）；
2. gate_pair 收全理由不短路；summarize 给通过/拒绝计数与理由分类；
3. --dry-run 零库写（临时 sqlite，strategy_instances 行数不变）；
4. 落库幂等：同 (策略, 版本, human_sha256) 重跑不重复落库；
5. --live 缺环境变量 K2CONTRAST_ALLOW_LIVE=1 ⇒ 拒绝执行（fail-closed）；
6. 破形对永不落库；span 对登记段核不上不落（既有门禁 verify_instance_span
   照用，不放宽）。
7. 按构造标注（独立审查 REVISE 修法）：op 必填，S1/S2 归属由 label_of(op)
   直接决定（唯一映射、判定确定）；四个 op 各有**对着自己**的构造断言；
   混合 op（跨标签证据形态混入同一对）必须被拒。

纪律：测试**从不**执行 CLI --live 开放路径（那是真落库）；库函数
run_contrast(live=True) 只对 conftest 的临时 sqlite 用。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2c", ROOT / "scripts" / "k2_contrast_extract.py")
k2c = _u.module_from_spec(_spec)
# 必须先登记进 sys.modules 再 exec_module：模块里的 @dataclass 在 3.11 会回查
# sys.modules[cls.__module__] 解析字符串注解，未登记 ⇒ AttributeError 收集期即炸。
sys.modules["k2c"] = k2c
_spec.loader.exec_module(k2c)

from app import db                                        # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,   # noqa: E402
                        StrategyInstance, Work)

_n = [0]

HUMAN_S1 = "林昭把杯子放下，没接话。窗外有人喊了一嗓子，她朝那边看了一眼，还是没说。"
AI_S1 = ("林昭把杯子放下，没有接话。其实她心里明白，这件事再争也没有用，"
         "因为说了他也不会改。窗外有人喊了一嗓子，她朝那边看了一眼，"
         "还是没说，说到底不过是懒得再提。")
HUMAN_S2 = "林昭推门进来，把伞收了，靠在门边喘气。"
AI_S2 = ("林昭推门进来，把伞收了，靠在门边喘气。忽然她像是想起了什么，"
         "缓缓直起身，然后一步一步走回桌边，仿佛屋里的一切都慢了下来。")
HUMAN_PSYCH = "林昭把杯子放下，没接话。"
AI_PSYCH = "林昭把杯子放下，没接话。她心里明白，这件事再争也不会有结果。"
SCENES = {"林昭", "临江城"}

# 既有用例沿用 strategy_key 口径时的缺省 op（按构造标注：每对必须声明一个 op）
_DEFAULT_OP = {k2c.S1_KEY: k2c.OP_ADD_INTERPRETATION,
               k2c.S2_KEY: k2c.OP_SPLIT_BEATS}


def _pair(strategy_key, human, ai, *, op=None, scene_keys=SCENES, span=(0, None),
          meta=None) -> k2c.ContrastPair:
    start, end = span
    return k2c.ContrastPair(
        human_text=human, ai_text=ai, scene_keys=scene_keys,
        op=op or _DEFAULT_OP[strategy_key], span_start=start,
        span_end=len(human) if end is None else end, meta=meta or {})


def _seed(*, human_text: str = HUMAN_S1, strategy_key: str = k2c.S1_KEY):
    """独立夹具：1 作品 + 1 段（text_clean=human_text）+ 1 条假设策略卡。
    返回 (segment_id, text_version, strategy_key)。"""
    _n[0] += 1
    tag = f"k2c-{_n[0]}"
    db.init_db()
    with db.session() as s:
        w = Work(title=f"t-{tag}", source="test:k2c")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=human_text,
                      text_clean=human_text, role="pilot",
                      n_chars=len(human_text), n_sentences=1,
                      integrity='{"src_ok": true}')
        s.add(seg)
        s.flush()
        st = ExpressionStrategyV2(
            strategy_key=strategy_key, abstract_operation=f"操作-{tag}",
            effect_hypothesis=f"假设-{tag}", status="hypothesis",
            scope="UNCERTAIN", scope_ids=[])
        s.add(st)
        s.commit()
        return seg.id, f"tv-{tag}", strategy_key


# ------------------------------------------------------- 门 1：特征共现
def test_gate_keyword_accept_and_reject():
    ok, why = k2c.gate_keyword_cooccurrence(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], "AI 命中且 human 不命中 ⇒ 接受"

    ok, why = k2c.gate_keyword_cooccurrence(
        _pair(k2c.S1_KEY, HUMAN_S1 + "其实她早就想走了。", AI_S1))
    assert not ok and any("S1 双向命中" in r and "其实" in r for r in why), why

    ok, why = k2c.gate_keyword_cooccurrence(
        _pair(k2c.S1_KEY, HUMAN_S1, "林昭把杯子放下，没有接话，看了一眼窗外。"))
    assert not ok and any("S1 双向未命中" in r for r in why), why

    ok, why = k2c.gate_keyword_cooccurrence(_pair(k2c.S2_KEY, HUMAN_S2, AI_S2))
    assert ok, why
    ok, why = k2c.gate_keyword_cooccurrence(
        _pair(k2c.S2_KEY, HUMAN_S2 + "她忽然有点想笑。", AI_S2))
    assert not ok and any("S2 双向命中" in r for r in why), why


# ------------------------------------------------------- 门 2：场景指称
def test_gate_scene_reference_accept_and_reject():
    ok, why = k2c.gate_scene_reference(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1, scene_keys=SCENES))
    assert ok and why == [], "两侧都引用 林昭 ⇒ 同一场景"

    ai_other = AI_S1.replace("林昭", "陈默").replace("她", "他")
    ok, why = k2c.gate_scene_reference(
        _pair(k2c.S1_KEY, HUMAN_S1, ai_other, scene_keys=SCENES))
    assert not ok and any("场景指称交集为空" in r for r in why), why

    ok, why = k2c.gate_scene_reference(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1, scene_keys=set()))
    assert not ok and any("场景指称缺失" in r for r in why), why


# ------------------------------------------------------- 门 3：长度比
def test_gate_length_ratio_accept_and_reject():
    ok, why = k2c.gate_length_ratio(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == []

    ok, why = k2c.gate_length_ratio(
        _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1))          # 等长：ratio=1.0
    assert not ok and any("长度比超界" in r for r in why), why

    ok, why = k2c.gate_length_ratio(
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1 * 12))        # ratio > 6.0
    assert not ok and any("长度比超界" in r for r in why), why


# ------------------------------------------------- 汇总：计数+理由分类
def test_gate_pair_collects_all_and_summarize_classes():
    bad = _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1, scene_keys=set())
    ok, reasons = k2c.gate_pair(bad)
    assert not ok and len(reasons) >= 4, \
        f"四道门全破时理由须收全不短路：{reasons}"
    pairs = [_pair(k2c.S1_KEY, HUMAN_S1, AI_S1),
             _pair(k2c.S2_KEY, HUMAN_S2, AI_S2), bad]
    rep = k2c.summarize(pairs)
    assert rep["n_pairs"] == 3 and rep["passed"] == 2 and rep["rejected"] == 1
    assert set(rep["reject_reason_classes"]) == {
        "add_interpretation 构造不符", "S1 双向未命中",
        "场景指称缺失", "长度比超界"}, \
        f"拒绝理由必须分类可读：{rep['reject_reason_classes']}"
    assert rep["by_op"] == {"add_interpretation": {"passed": 1, "rejected": 1},
                            "add_psych_narration": {"passed": 0, "rejected": 0},
                            "split_beats": {"passed": 1, "rejected": 0},
                            "dilute_modifiers": {"passed": 0, "rejected": 0}}, \
        f"summarize 必须给按 op 的通过/拒绝计数：{rep['by_op']}"
    assert rep["reject_samples"] and rep["reject_samples"][0]["reasons"] \
        and rep["reject_samples"][0]["op"] == "add_interpretation"


# ------------------------------------------------------- build_pairs
def test_build_pairs_injects_ai_side_and_hashes():
    specs = [{"human_text": HUMAN_S1, "ai_text": AI_S1,
              "op": k2c.OP_ADD_INTERPRETATION,
              "scene_keys": list(SCENES), "span_start": 0,
              "span_end": len(HUMAN_S1), "segment_id": "seg-1",
              "text_version": "tv-1"}]
    pairs = k2c.build_pairs(specs)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.op == k2c.OP_ADD_INTERPRETATION
    assert p.strategy_key == k2c.S1_KEY, "strategy_key 须由 op 推导"
    assert p.scene_keys == SCENES
    assert p.meta["segment_id"] == "seg-1" and p.meta["text_version"] == "tv-1"
    import hashlib
    assert p.human_sha256 == hashlib.sha256(
        HUMAN_S1.encode("utf-8")).hexdigest(), "human_sha256 必须现算可对账"
    # fail-closed：规格缺 op（事后归类口径）直接拒
    with pytest.raises(ValueError, match="op"):
        k2c.build_pairs([{k: v for k, v in specs[0].items() if k != "op"}])


# ---------------------------------------------- dry-run 零库写（CLI）
def test_dry_run_zero_writes(tmp_path, monkeypatch):
    _seed()
    pf = tmp_path / "pairs.json"
    pf.write_text(json.dumps(
        {"pairs": [{"human_text": HUMAN_S1, "ai_text": AI_S1,
                    "op": k2c.OP_ADD_INTERPRETATION,
                    "scene_keys": list(SCENES), "span_start": 0,
                    "span_end": len(HUMAN_S1)}]},
        ensure_ascii=False), encoding="utf-8")
    with db.session() as s:
        before = s.query(StrategyInstance).count()
    monkeypatch.setattr(sys, "argv",
                        ["k2c", "--dry-run", "--pairs-file", str(pf)])
    k2c.main()                          # 默认即 dry-run，不应碰库
    with db.session() as s:
        after = s.query(StrategyInstance).count()
    assert after == before, "dry-run 写了库——违反零库写承诺"


# ----------------------------------------------- live 缺环境变量 ⇒ 拒
def test_live_without_env_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("K2CONTRAST_ALLOW_LIVE", raising=False)
    pf = tmp_path / "pairs.json"
    pf.write_text(json.dumps({"pairs": []}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["k2c", "--live", "--pairs-file", str(pf)])
    with pytest.raises(SystemExit, match="K2CONTRAST_ALLOW_LIVE"):
        k2c.main()
    monkeypatch.setenv("K2CONTRAST_ALLOW_LIVE", "1")
    monkeypatch.setattr(sys, "argv", ["k2c", "--live"])
    with pytest.raises(SystemExit, match="pairs-file"):
        k2c.main()


# --------------------------------------------- 落库：幂等 + 破形不落
def test_persist_idempotent_and_gated_pairs_never_land():
    seg_id, tv, key = _seed()
    good = _pair(key, HUMAN_S1, AI_S1,
                 meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:
        rep1 = k2c.run_contrast(s, [good], live=True)
    assert rep1["written"] == 1 and rep1["passed"] == 1
    with db.session() as s:
        rows = (s.query(StrategyInstance)
                .filter_by(evidence_sha256=good.human_sha256).all())
        assert len(rows) == 1 and rows[0].status == "proposed"
        cond = rows[0].conditions_observed
        assert cond["protocol"] == "paired_contrast_v2"
        assert cond["op"] == k2c.OP_ADD_INTERPRETATION
        assert cond["op_label"] == "S1"
        assert cond["ai_side_sha256"] and cond["ai_side_chars"] == len(AI_S1)
        assert sorted(cond["gates"]) == ["keyword_cooccurrence",
                                         "length_ratio",
                                         "op_construction",
                                         "scene_reference"]
    with db.session() as s:             # 同输入重跑：同 sha 不重复落库
        rep2 = k2c.run_contrast(s, [good], live=True)
    assert rep2["written"] == 0 and rep2["skipped"].get("skip_dup_sha") == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(evidence_sha256=good.human_sha256).count()) == 1
    bad = _pair(key, HUMAN_S2, HUMAN_S2, scene_keys=SCENES,
                meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:             # 破形对：门内拦掉，永不落库
        rep3 = k2c.run_contrast(s, [bad], live=True)
    assert rep3["written"] == 0 and rep3["rejected"] == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(evidence_sha256=bad.human_sha256).count()) == 0, \
            "破形对绝不许留库行"


# ------------------------------- 既有门禁照用：span 核不上 / 缺定位不落
def test_persist_span_and_meta_gates():
    seg_id, tv, key = _seed()
    off = _pair(key, HUMAN_S1, AI_S1, span=(3, len(HUMAN_S1) + 5),
                meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:             # span 对登记段核不上 ⇒ 不落（不放宽）
        rep = k2c.run_contrast(s, [off], live=True)
    assert rep["written"] == 0 and rep["skipped"].get("skip_span_mismatch") == 1

    no_seg = _pair(key, HUMAN_S1, AI_S1, meta={"text_version": tv})
    with db.session() as s:
        rep2 = k2c.run_contrast(s, [no_seg], live=True)
    assert rep2["skipped"].get("skip_no_segment") == 1

    no_strategy = _pair(k2c.S2_KEY, HUMAN_S2, AI_S2, span=(0, len(HUMAN_S2)),
                        meta={"segment_id": seg_id, "text_version": tv})
    with db.session() as s:             # 门全过但库内无该卡 ⇒ 跳过，不自动建卡
        rep3 = k2c.run_contrast(s, [no_strategy], live=True)
    assert rep3["passed"] == 1           # 四道门过了（拦点在库侧，不在门侧）
    assert rep3["skipped"].get("skip_no_strategy") == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(segment_id=seg_id).count()) == 0, \
            "不许自动建卡/落行（本夹具段上零实例）"


# ------------------------------------- 按构造标注：label_of 唯一映射
def test_label_of_mapping_deterministic_and_illegal():
    assert k2c.label_of(k2c.OP_ADD_INTERPRETATION) == "S1"
    assert k2c.label_of(k2c.OP_ADD_PSYCH_NARRATION) == "S1"
    assert k2c.label_of(k2c.OP_SPLIT_BEATS) == "S2"
    assert k2c.label_of(k2c.OP_DILUTE_MODIFIERS) == "S2"
    # 判定确定性：同 op 重复调用必得同一标签
    for op in k2c.OPS:
        assert k2c.label_of(op) == k2c.label_of(op), op
    # 非法 op fail-closed：映射抛错，构造对时也直接抛
    for bad_op in ("add_interpretation+split_beats", "", "bogus_op", None):
        with pytest.raises(ValueError, match="非法操作标签"):
            k2c.label_of(bad_op)
    with pytest.raises(ValueError, match="非法操作标签"):
        _pair(k2c.S1_KEY, HUMAN_S1, AI_S1, op="bogus_op")


# ------------------------------------- 判定确定性：同输入两次同标签同判定
def test_labeling_determinism_same_input_same_label():
    for key, human, ai, want in ((k2c.S1_KEY, HUMAN_S1, AI_S1, "S1"),
                                 (k2c.S2_KEY, HUMAN_S2, AI_S2, "S2")):
        p = _pair(key, human, ai)
        assert k2c.label_of(p.op) == k2c.label_of(p.op) == want
        assert k2c.gate_pair(p) == k2c.gate_pair(p), \
            "同一对输入两次，判定必须完全一致"
        assert k2c.gate_pair(p)[0] is True


# ------------------------------- 门0：四个 op 各自的接受与拒绝（理由可读）
def test_op_add_interpretation_accept_and_reject():
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], why
    # 拒：句数未增（无新增陈述）
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1))
    assert not ok and any("句数未增" in r for r in why), why
    # 拒：新增句未命中解释标记词表
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_S1, HUMAN_S1 + "窗外又起了风。"))
    assert not ok and any("未命中" in r and "解释" in r for r in why), why
    # 拒：人类侧命中词表（不构成对照）
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_S1 + "其实她早想走了。", AI_S1))
    assert not ok and any("人类侧命中" in r for r in why), why


def test_op_add_psych_narration_accept_and_reject():
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_PSYCH, AI_PSYCH,
              op=k2c.OP_ADD_PSYCH_NARRATION))
    assert ok and why == [], why
    # 拒：新增句未命中心理标记词表
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, HUMAN_PSYCH, HUMAN_PSYCH + "窗外起了风。",
              op=k2c.OP_ADD_PSYCH_NARRATION))
    assert not ok and any("未命中" in r and "心理" in r for r in why), why
    # 拒：人类侧本就命中心理标记（不构成对照）
    human2 = HUMAN_PSYCH + "她心里明白，再争也无益。"
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S1_KEY, human2, human2 + "窗外起了风。",
              op=k2c.OP_ADD_PSYCH_NARRATION))
    assert not ok and any("人类侧命中" in r for r in why), why


def test_op_split_beats_accept_and_reject():
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S2_KEY, HUMAN_S2, AI_S2, op=k2c.OP_SPLIT_BEATS))
    assert ok and why == [], why
    # 拒：实词集合 Jaccard 过低（命题/实词集合被改写，不是拆拍）
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, "她忽然放慢脚步，像是想起了什么，缓缓回头。",
        op=k2c.OP_SPLIT_BEATS))
    assert not ok and any("Jaccard" in r for r in why), why
    # 拒：节拍标记数未上升（密度变化不可测）
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, HUMAN_S2 + "桌边有杯凉茶。",
        op=k2c.OP_SPLIT_BEATS))
    assert not ok and any("节拍" in r and "未上升" in r for r in why), why


def test_op_dilute_modifiers_accept_and_reject():
    ok, why = k2c.gate_op_construction(
        _pair(k2c.S2_KEY, HUMAN_S2, AI_S2, op=k2c.OP_DILUTE_MODIFIERS))
    assert ok and why == [], why
    # 拒：修饰标记数未上升
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, HUMAN_S2 + "她走了两步。",
        op=k2c.OP_DILUTE_MODIFIERS))
    assert not ok and any("修饰" in r and "未上升" in r for r in why), why
    # 拒：实词集合 Jaccard 过低（命题被换掉，不是注水）
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, "屋外雨声很密，她仿佛没听见，只淡淡应了一声。",
        op=k2c.OP_DILUTE_MODIFIERS))
    assert not ok and any("Jaccard" in r for r in why), why


# ------------------------------- 混合 op：跨标签证据形态混入同一对 ⇒ 拒
def test_mixed_ops_rejected():
    # S2 对里混入 S1 形态：新增解释/心理句
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S2_KEY, HUMAN_S2, AI_S2 + "其实她心里明白，再留也无益。",
        op=k2c.OP_SPLIT_BEATS))
    assert not ok and any("混合操作" in r for r in why), why
    # S1 对里混入 S2 形态：节拍标记上升
    ok, why = k2c.gate_op_construction(_pair(
        k2c.S1_KEY, HUMAN_S1, AI_S1 + "然后她缓缓起身。",
        op=k2c.OP_ADD_INTERPRETATION))
    assert not ok and any("混合操作" in r and "节拍" in r for r in why), why


# ------------------------------- 标记词表是模块常量，测试可整体覆写
def test_marker_vocab_overridable(monkeypatch):
    monkeypatch.setattr(k2c, "INTERPRET_MARKERS", ())
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert not ok and any("未命中" in r for r in why), \
        "词表清空后同一对必须被拒（证明门读的是模块常量）"
    monkeypatch.setattr(k2c, "INTERPRET_MARKERS", ("争",))
    ok, why = k2c.gate_op_construction(_pair(k2c.S1_KEY, HUMAN_S1, AI_S1))
    assert ok and why == [], "换成自定义词表命中后同一对必须通过"
