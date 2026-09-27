"""Pure K2 snapshot tests; all data lives in an in-memory synthetic database."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import knowledge as K
from app import knowledge_extract as KE
from app.db import Base
from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,
                        StrategyInstance, Work, WorkSource)
from app.semantic_review import SnapshotError, build_snapshot

TEXT = "夜里起了风，他坐在桌前。"
CLAIM = {"scope_to": "WORK", "scope_ids": ["WK-A"],
         "scope_basis": "两条已登记证据"}


def _seed(reverse=False):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as con:
        con.execute(text(
            "CREATE TABLE promotion_audits (audit_id TEXT PRIMARY KEY, "
            "strategy_id TEXT, strategy_version INTEGER, to_status TEXT, "
            "evidence_ref TEXT, ts TEXT)"))
        con.execute(text(
            "INSERT INTO promotion_audits VALUES "
            "('AUD-R', 'ESV2-S', 1, 'replicated', '[\"SI-A\",\"SI-Z\"]', "
            "'2026-09-27T00:00:00Z')"))
    Session = sessionmaker(bind=engine, autoflush=False)
    s = Session()
    s.add(ExpressionStrategyV2(
        id="ESV2-S", strategy_key="慢速动作", version=1,
        abstract_operation="动作后留一拍", invariants=["不改变事实"],
        effect_hypothesis="减速", failure_modes=["拖沓"], source="seed",
        status="hypothesis", observation_status="replicated",
        effect_status="untested", scope="UNCERTAIN", scope_ids=[],
        scope_basis=""))
    s.add(StrategyCondition(
        id="SC-S", strategy_id="ESV2-S", strategy_version=1,
        kind="good_when", dimension="节奏", operator="eq",
        value={"v": "慢"}, required=False, predicate_state="unknown",
        evidence_refs=[]))
    for work_id, seg_id in (("WK-A", "SEG-A"), ("WK-M", "SEG-M")):
        s.add(Work(id=work_id, title=work_id, source="file:seed"))
        s.flush()
        s.add(Segment(
            id=seg_id, work_id=work_id, ordinal=0, text=TEXT,
            role="train", n_sentences=1, n_chars=len(TEXT),
            integrity=json.dumps({"src_ok": True})))
        s.add(WorkSource(
            id="SRC-" + work_id, work_id=work_id,
            canonical_work_id="WK-A", source_type="human_fiction",
            text_version="corpus-v1", text_sha256=hashlib.sha256(
                TEXT.encode("utf-8")).hexdigest(),
            purpose_basis="seed", identity_purposes=["research"],
            license_purposes=[], metadata_status="verified",
            metadata_basis="seed", genre_ids=[]))
    evidence = TEXT[:5]
    for ins_id, work_id, seg_id in (("SI-Z", "WK-M", "SEG-M"),
                                    ("SI-A", "WK-A", "SEG-A"))[::(-1 if reverse else 1)]:
        s.add(StrategyInstance(
            id=ins_id, strategy_id="ESV2-S", strategy_version=1,
            work_id=work_id, segment_id=seg_id, frame_id=None,
            text_version="corpus-v1", span_start=0, span_end=5,
            evidence_text=evidence,
            evidence_sha256=K.evidence_sha256(evidence),
            conditions_observed={"节奏": "慢"}, observed_content="留白",
            effect_ref=None, extractor_model="seed",
            reviewer_version=KE.REVIEW_MARKER_NEW_DEF, status="verified"))
    s.commit()
    return engine, s


def test_same_content_has_same_digest_despite_insertion_order_and_timestamps():
    e1, s1 = _seed()
    e2, s2 = _seed(reverse=True)
    try:
        first = build_snapshot(s1, "ESV2-S", 1, CLAIM)
        second = build_snapshot(s2, "ESV2-S", 1, CLAIM)
        assert first["content_sha256"] == second["content_sha256"]
        assert first["review_input_sha256"] == second["review_input_sha256"]
        assert first["payload"]["evidence"]["admitted_refs"][0][
            "instance_id"] == "SI-A"
        assert "SI-Z:mirror_dedup" in first["payload"]["evidence"]["stripped"]

        s1.get(ExpressionStrategyV2, "ESV2-S").status = "verified"
        s1.get(StrategyInstance, "SI-A").created_at = "2099-01-01"
        s1.flush()
        assert build_snapshot(s1, "ESV2-S", 1, CLAIM)[
            "content_sha256"] == first["content_sha256"]
    finally:
        s1.close(); s2.close(); e1.dispose(); e2.dispose()


@pytest.mark.parametrize("change", [
    "card", "condition", "negative_instance", "reviewer_marker",
    "segment_role", "segment_integrity", "segment_text_outside_span",
    "source_license", "scope_claim",
])
def test_material_change_invalidates_snapshot(change):
    engine, s = _seed()
    try:
        before = build_snapshot(s, "ESV2-S", 1, CLAIM)["content_sha256"]
        claim = dict(CLAIM)
        if change == "card":
            s.get(ExpressionStrategyV2, "ESV2-S").abstract_operation += " 再停一拍"
        elif change == "condition":
            s.get(StrategyCondition, "SC-S").value = {"v": "快"}
        elif change == "negative_instance":
            s.add(StrategyInstance(
                id="SI-NEG", strategy_id="ESV2-S", strategy_version=1,
                work_id="WK-A", segment_id="SEG-A", text_version="corpus-v1",
                span_start=6, span_end=11, evidence_text=TEXT[6:11],
                evidence_sha256=K.evidence_sha256(TEXT[6:11]),
                conditions_observed={}, observed_content="反例",
                extractor_model="seed", status="rejected"))
        elif change == "reviewer_marker":
            s.get(StrategyInstance, "SI-A").reviewer_version = "old"
        elif change == "segment_role":
            s.get(Segment, "SEG-A").role = "benchmark"
        elif change == "segment_integrity":
            s.get(Segment, "SEG-A").integrity = json.dumps({"src_ok": False})
        elif change == "segment_text_outside_span":
            s.get(Segment, "SEG-A").text = TEXT[:5] + "故事改了另一句。"
        elif change == "source_license":
            s.query(WorkSource).filter_by(work_id="WK-A").one().license_purposes = [
                "benchmark_source"]
        elif change == "scope_claim":
            claim["scope_basis"] = "另一份审查依据"
        s.flush()
        assert build_snapshot(s, "ESV2-S", 1, claim)[
            "content_sha256"] != before
    finally:
        s.close(); engine.dispose()


@pytest.mark.parametrize("failure", [
    "wrong_version", "missing_source", "bad_positive_span", "bad_positive_hash",
    "missing_audit",
])
def test_unverifiable_evidence_refuses_snapshot(failure):
    engine, s = _seed()
    try:
        if failure == "wrong_version":
            s.get(StrategyInstance, "SI-A").strategy_version = 2
        elif failure == "missing_source":
            s.delete(s.query(WorkSource).filter_by(work_id="WK-A").one())
        elif failure == "bad_positive_span":
            s.get(StrategyInstance, "SI-A").evidence_text = "不是原文"
        elif failure == "bad_positive_hash":
            s.get(StrategyInstance, "SI-A").evidence_sha256 = "0" * 64
        elif failure == "missing_audit":
            s.execute(text("DELETE FROM promotion_audits"))
        s.flush()
        with pytest.raises(SnapshotError):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
    finally:
        s.close(); engine.dispose()
