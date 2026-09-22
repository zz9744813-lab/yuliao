"""K2-A 放量驱动回归（scripts/k2_extract_backfill.py，主控派工 2026-09-23）。

钉住的事：
1. 幂等：已落行（verified / rejected_evidence）的对重跑跳过，不重复劳动、
   不重复烧预算；unverified（无行可落）重跑会再试（预算闸兜底）；
2. 预算闸：超限显式 blocked_budget 收尾，已抽候选照实提交保留；
3. --dry-run 零调用零库写（预演只出计数）；
4. --limit + 策略间轮转：单策略不垄断预算（每策略各得其一）；
5. 来源合格闸：src_ok 非 true / text_clean 空的段不入宇宙；缺 text_version
   登记的对 skip（K1-A 契约）；
6. main 双闸：--live 无 K2_ALLOW_LIVE=1 拒；无 --extractor-model 拒；
   LG_LLM_MODE≠real 拒；不带任何模式拒——四条拒绝路径全测，**开放路径
   不在测试里真跑**（那是一条真实调用，纪律禁）。
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
    "k2b", ROOT / "scripts" / "k2_extract_backfill.py")
k2b = _u.module_from_spec(_spec); _spec.loader.exec_module(k2b)

from app import db, knowledge_extract as KE              # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        StrategyInstance, Work, WorkSource)

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"
_n = [0]


class _FxOK:
    def invoke(self, *, role, system, payload, max_tokens, timeout):
        body = {"span_start": 0, "span_end": 8,
                "evidence_text": TEXT[0:8], "observed_content": "克制沉默"}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}


class _FxBadJson:
    def invoke(self, *, role, system, payload, max_tokens, timeout):
        return {"text": "不是json", "tokens_in": 1, "tokens_out": 1,
                "actual_model": "fx"}


def _seed(n_seg=3, *, bad_src=False, no_tv=False, scope="WORK"):
    """一套独立夹具：1 作品 + n_seg 基准段 + 登记行 + 1 条假设策略。
    每次调用用全局序号保证跨用例唯一；默认 scope=WORK 绑自家作品——
    共享测试库里 UNCERTAIN=全集会把别的用例的段吸进来（行为测试用
    WORK 隔离；UNCERTAIN 全集语义另有专项用例钉）。"""
    _n[0] += 1
    key = f"k2b-{_n[0]}"
    db.init_db()
    with db.session() as s:
        w = Work(title=f"t-k2b-{key}", source="test:k2b")
        s.add(w)
        s.flush()
        for i in range(n_seg):
            integ = '{"src_ok": %s}' % ("false" if bad_src else "true")
            s.add(Segment(work_id=w.id, ordinal=i, text=TEXT,
                          text_clean=("" if bad_src else TEXT),
                          role="benchmark", n_chars=len(TEXT), n_sentences=1,
                          integrity=integ))
        s.flush()                       # Work 先落（FK 顺序），段齐后才算锚
        import register_work_sources as REG   # 单一哈希口径（锚复核纪律）
        sha, _ = REG._work_sha256(s, w.id)
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type="fixture",
                         text_version="" if no_tv else f"tv-{key}",
                         text_sha256=sha,
                         purpose_basis="测试夹具：只验管线契约",
                         identity_purposes=["research"], license_purposes=[],
                         license_basis="seed", metadata_status="verified",
                         metadata_basis="seed"))
        st = ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            effect_hypothesis=f"假设-{key}", status="hypothesis",
            scope=scope, scope_ids=[w.id] if scope == "WORK" else [])
        s.add(st)
        s.commit()
        return key


def _rows(key):
    with db.session() as s:
        st = (s.query(ExpressionStrategyV2)
              .filter_by(strategy_key=key).one())
        return s.query(StrategyInstance).filter_by(
            strategy_id=st.id).all()


def test_dry_run_zero_writes_preview():
    key = _seed(n_seg=3)
    with db.session() as s:
        before = s.query(StrategyInstance).count()
        rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                               strategy_keys=(key,))
        after = s.query(StrategyInstance).count()
    assert after == before, "dry-run 写了库——违反零库写承诺"
    assert rep["mode"] == "dry_run" and rep["would_attempt"] == 3
    assert rep["n_pending_pairs"] == 3 and rep["skips"]["skipped_done"] == 0
    assert rep["attempted"] == 0 and rep["verified"] == 0


def test_run_persists_verified_and_rerun_idempotent():
    key = _seed(n_seg=3)
    with db.session() as s:
        rep1 = k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    assert rep1["attempted"] == 3 and rep1["verified"] == 3
    assert rep1["written"] == 3 and not rep1["blocked_budget"]
    rows = _rows(key)
    assert len(rows) == 3 and all(r.status == "verified" for r in rows)
    for r in rows:                      # 落库行必须机械可核对（单一口径）
        assert TEXT[r.span_start:r.span_end] == r.evidence_text
        assert r.text_version.startswith("tv-") and r.extractor_model == "fx"
    with db.session() as s:             # 幂等重跑：全跳、零新行
        rep2 = k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    assert rep2["attempted"] == 0 and rep2["written"] == 0
    assert rep2["skips"]["skipped_done"] == 3
    assert len(_rows(key)) == 3


def test_budget_breakpoint_keeps_candidates():
    key = _seed(n_seg=3)
    with db.session() as s:
        rep = k2b.run_backfill(s, _FxOK(), limit=48, max_calls=2,
                               strategy_keys=(key,))
    assert rep["blocked_budget"] is True, "预算断点必须显式（禁静默放行）"
    assert rep["attempted"] == 2 and rep["written"] == 2   # 已抽候选保留
    assert "blocked_at" in rep and rep["budget"]["calls"] == 2
    with db.session() as s:             # 续跑：已落 2 对跳过，余下继续
        rep2 = k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    assert rep2["skips"]["skipped_done"] == 2
    assert rep2["attempted"] == 1 and rep2["written"] == 1
    assert len(_rows(key)) == 3


def test_round_robin_limit_fairness():
    ka = _seed(n_seg=1)
    kb = _seed(n_seg=1)
    with db.session() as s:
        rep = k2b.run_backfill(s, _FxOK(), limit=2, strategy_keys=(ka, kb))
    assert rep["attempted"] == 2 and rep["written"] == 2
    assert len(_rows(ka)) == 1 and len(_rows(kb)) == 1, \
        "轮转公平：limit 内每策略各得其一，单策略不垄断预算"


def test_unverified_not_persisted_but_reported():
    key = _seed(n_seg=2)
    with db.session() as s:
        rep = k2b.run_backfill(s, _FxBadJson(), limit=48, strategy_keys=(key,))
    assert rep["attempted"] == 2 and rep["unverified"] == 2
    assert rep["written"] == 0 and not _rows(key), \
        "unverified 无行可落——不许伪造 span 行"
    assert rep["unverified_details"][0]["reason"] == "invalid_extraction_json"
    with db.session() as s:             # 未落行不占幂等位：重跑再试
        rep2 = k2b.run_backfill(s, _FxBadJson(), limit=48, strategy_keys=(key,))
    assert rep2["skips"]["skipped_done"] == 0 and rep2["attempted"] == 2


def test_rejected_evidence_branch_persisted_and_idempotent(monkeypatch):
    key = _seed(n_seg=1)
    monkeypatch.setattr(KE, "gate_evidence",
                        lambda clean, text: "rejected_evidence")
    with db.session() as s:
        rep = k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    assert rep["rejected_evidence"] == 1 and rep["written"] == 1
    rows = _rows(key)
    assert len(rows) == 1 and rows[0].status == "rejected_evidence"
    with db.session() as s:             # rejected 也算已处理（幂等位占用）
        rep2 = k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    assert rep2["attempted"] == 0 and rep2["skips"]["skipped_done"] == 1


def test_source_gate_src_ok_and_text_version():
    key_bad = _seed(n_seg=2, bad_src=True)      # src_ok=false + text_clean 空
    with db.session() as s:
        rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                               strategy_keys=(key_bad,))
    assert rep["n_pending_pairs"] == 0, "来源不合格段不得入试点宇宙"
    key_notv = _seed(n_seg=2, no_tv=True)      # 登记行缺 text_version
    with db.session() as s:
        rep2 = k2b.run_backfill(s, None, limit=48, dry_run=True,
                                strategy_keys=(key_notv,))
    assert rep2["n_pending_pairs"] == 0
    assert rep2["skips"]["skipped_no_text_version"] == 2


def test_uncertain_scope_uses_full_pilot_universe():
    """UNCERTAIN/空身份 = 试点全集语义：策略的段池不限于自家作品——
    须包含 scope 外作品的段（实例用于之后建立 scope，全集试点是载体）。
    ≥ 断言抗共享库残留。"""
    _seed(n_seg=1, scope="WORK")        # 另一作品的合格段（在全集里）
    kb = _seed(n_seg=1, scope="UNCERTAIN")
    with db.session() as s:
        rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                               strategy_keys=(kb,))
    assert rep["n_pending_pairs"] >= 2, \
        "UNCERTAIN 段池须含 scope 外作品的段（全集语义被破坏）"


def test_main_refusal_paths(monkeypatch, tmp_path):
    """main 四条拒绝路径全测；开放路径不在测试真跑（那是真实调用）。"""
    monkeypatch.delenv("K2_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(sys, "argv", ["k2b"])
    with pytest.raises(SystemExit, match="二选一"):
        k2b.main()
    monkeypatch.setattr(sys, "argv", ["k2b", "--live",
                                      "--extractor-model", "m"])
    with pytest.raises(SystemExit, match="K2_ALLOW_LIVE"):
        k2b.main()
    monkeypatch.setenv("K2_ALLOW_LIVE", "1")
    monkeypatch.setattr(sys, "argv", ["k2b", "--live"])
    with pytest.raises(SystemExit, match="extractor-model"):
        k2b.main()
    monkeypatch.setattr(sys, "argv", ["k2b", "--live",
                                      "--extractor-model", "m"])
    monkeypatch.setattr(k2b, "LLM_MODE", "mock")
    with pytest.raises(SystemExit, match="LG_LLM_MODE=real"):
        k2b.main()
    monkeypatch.setattr(sys, "argv", ["k2b", "--dry-run", "--limit", "0"])
    with pytest.raises(SystemExit, match="1"):
        k2b.main()


def test_observe_update_fact_layer_only_and_idempotent():
    """K2 收尾件（strategy_observe_update）：观察态=hypothesis→observed 需要
    verified 实例 ≥1（事实层机械判据）；**status（语义审查拍板项）绝不许被
    碰**；幂等；0 实例不动；dry-run 零库写。"""
    import strategy_observe_update as SO
    key = _seed(n_seg=1)
    rep0 = SO.run(apply=False)                     # 抽取前：无实例不迁
    assert all(r["strategy_key"] != key for r in rep0["detail"]), \
        "无 verified 实例的策略不许被迁移"
    with db.session() as s:
        k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(key,))
    rep1 = SO.run(apply=False)
    assert rep1["mode"] == "dry_run" and rep1["would_update"] >= 1
    assert any(r["strategy_key"] == key and r["verified_instances"] == 1
               for r in rep1["detail"]), rep1["detail"][:3]
    rep2 = SO.run(apply=True)
    assert rep2["applied"] >= 1
    with db.session() as s:
        st = (s.query(ExpressionStrategyV2)
              .filter_by(strategy_key=key).one())
        assert st.observation_status == "observed"
        assert st.status == "hypothesis", "status 是拍板项，本工具不许动"
    rep3 = SO.run(apply=True)
    assert rep3["would_update"] == 0, "幂等：observed 的不再动"
