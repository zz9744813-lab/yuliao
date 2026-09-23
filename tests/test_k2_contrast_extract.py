"""K2 成对对照抽取器回归（scripts/k2_contrast_extract.py，主控派工 2026-09-23）。

钉住的事：
1. 三道机械门各自的接受与拒绝（拒绝理由可读、含既定形态文本）；
2. gate_pair 收全理由不短路；summarize 给通过/拒绝计数与理由分类；
3. --dry-run 零库写（临时 sqlite，strategy_instances 行数不变）；
4. 落库幂等：同 (策略, 版本, human_sha256) 重跑不重复落库；
5. --live 缺环境变量 K2CONTRAST_ALLOW_LIVE=1 ⇒ 拒绝执行（fail-closed）；
6. 破形对永不落库；span 对登记段核不上不落（既有门禁 verify_instance_span
   照用，不放宽）。

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
SCENES = {"林昭", "临江城"}


def _pair(strategy_key, human, ai, *, scene_keys=SCENES, span=(0, None),
          meta=None) -> k2c.ContrastPair:
    start, end = span
    return k2c.ContrastPair(
        human_text=human, ai_text=ai, scene_keys=scene_keys,
        strategy_key=strategy_key, span_start=start,
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
    assert not ok and len(reasons) >= 3, \
        f"三道门全破时理由须收全不短路：{reasons}"
    pairs = [_pair(k2c.S1_KEY, HUMAN_S1, AI_S1),
             _pair(k2c.S2_KEY, HUMAN_S2, AI_S2), bad]
    rep = k2c.summarize(pairs)
    assert rep["n_pairs"] == 3 and rep["passed"] == 2 and rep["rejected"] == 1
    assert set(rep["reject_reason_classes"]) == {
        "S1 双向未命中", "场景指称缺失", "长度比超界"}, \
        f"拒绝理由必须分类可读：{rep['reject_reason_classes']}"
    assert rep["reject_samples"] and rep["reject_samples"][0]["reasons"]


# ------------------------------------------------------- build_pairs
def test_build_pairs_injects_ai_side_and_hashes():
    specs = [{"human_text": HUMAN_S1, "ai_text": AI_S1,
              "scene_keys": list(SCENES), "span_start": 0,
              "span_end": len(HUMAN_S1), "segment_id": "seg-1",
              "text_version": "tv-1"}]
    pairs = k2c.build_pairs(specs, default_strategy_key=k2c.S1_KEY)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.strategy_key == k2c.S1_KEY and p.scene_keys == SCENES
    assert p.meta["segment_id"] == "seg-1" and p.meta["text_version"] == "tv-1"
    import hashlib
    assert p.human_sha256 == hashlib.sha256(
        HUMAN_S1.encode("utf-8")).hexdigest(), "human_sha256 必须现算可对账"


# ---------------------------------------------- dry-run 零库写（CLI）
def test_dry_run_zero_writes(tmp_path, monkeypatch):
    _seed()
    pf = tmp_path / "pairs.json"
    pf.write_text(json.dumps(
        {"strategy_key": k2c.S1_KEY,
         "pairs": [{"human_text": HUMAN_S1, "ai_text": AI_S1,
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
        assert cond["ai_side_sha256"] and cond["ai_side_chars"] == len(AI_S1)
        assert sorted(cond["gates"]) == ["keyword_cooccurrence",
                                         "length_ratio", "scene_reference"]
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
    assert rep3["passed"] == 1           # 三道门过了（拦点在库侧，不在门侧）
    assert rep3["skipped"].get("skip_no_strategy") == 1
    with db.session() as s:
        assert (s.query(StrategyInstance)
                .filter_by(segment_id=seg_id).count()) == 0, \
            "不许自动建卡/落行（本夹具段上零实例）"
