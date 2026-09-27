"""Pure K2 snapshot tests; all data lives in an in-memory synthetic database."""
from __future__ import annotations

import hashlib
import json
from threading import Event, Thread

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app import knowledge as K
from app import knowledge_extract as KE
from app import knowledge_query as KQ
from app.db import Base
from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,
                        StrategyInstance, Work, WorkSource)
from app.semantic_review import SnapshotError, build_snapshot
from app.semantic_review_store import freeze_snapshot, verify_current_snapshot
from app.semantic_receipts import ReceiptSchemaError, ensure_semantic_schema
import app.semantic_review_store as snapshot_store

TEXT = "夜里起了风，他坐在桌前。"
CLAIM = {"scope_to": "WORK", "scope_ids": ["WK-A"],
         "scope_basis": "两条已登记证据"}


def _seed(reverse=False, db_path=None):
    url = "sqlite://" if db_path is None else f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True,
                           connect_args=({"timeout": 0.2} if db_path else {}))
    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with engine.begin() as con:
        con.execute(text(
            "CREATE TABLE promotion_audits (audit_id TEXT PRIMARY KEY, "
            "strategy_id TEXT, strategy_version INTEGER, to_status TEXT, "
            "evidence_ref TEXT, ts TEXT)"))
        con.execute(text(
            "INSERT INTO promotion_audits "
            "(audit_id,strategy_id,strategy_version,to_status,evidence_ref,ts) VALUES "
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


def test_freeze_creates_independent_immutable_rounds_on_same_content():
    engine, seed_session = _seed()
    try:
        seed_session.close()
        ensure_semantic_schema(engine)
        first = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        second = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        assert first["snapshot_id"] != second["snapshot_id"]
        assert first["content_sha256"] == second["content_sha256"]
        assert first["review_input_sha256"] == second["review_input_sha256"]
        with Session(engine) as check:
            assert verify_current_snapshot(check, first["snapshot_id"], CLAIM)[
                "content_sha256"] == first["content_sha256"]
            assert verify_current_snapshot(check, second["snapshot_id"], CLAIM)[
                "content_sha256"] == second["content_sha256"]
            assert check.execute(text(
                "SELECT COUNT(*) FROM semantic_review_snapshots")).scalar() == 2
            assert check.execute(text(
                "SELECT COUNT(*) FROM semantic_review_votes")).scalar() == 0
            assert check.execute(text(
                "SELECT COUNT(*) FROM semantic_approval_links")).scalar() == 0
    finally:
        seed_session.close(); engine.dispose()


def test_freeze_refuses_invalid_claim_without_partial_row():
    engine, seed_session = _seed()
    try:
        seed_session.close()
        ensure_semantic_schema(engine)
        with pytest.raises(SnapshotError, match="scope_claim_unbacked"):
            freeze_snapshot(engine, "ESV2-S", 1, {
                **CLAIM, "scope_ids": ["OTHER-WORK"]})
        with Session(engine) as check:
            assert check.execute(text(
                "SELECT COUNT(*) FROM semantic_review_snapshots")).scalar() == 0
    finally:
        seed_session.close(); engine.dispose()


def test_freeze_refuses_missing_or_weakened_receipt_schema():
    engine, seed_session = _seed()
    try:
        seed_session.close()
        with pytest.raises(ReceiptSchemaError, match="table_schema_drift"):
            freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        ensure_semantic_schema(engine)
        frozen = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "DROP TRIGGER semantic_review_snapshots_no_update")
            conn.exec_driver_sql(
                "CREATE TRIGGER semantic_review_snapshots_no_update "
                "BEFORE UPDATE ON semantic_review_snapshots BEGIN SELECT 1; END")
        with pytest.raises(ReceiptSchemaError, match="trigger_schema_drift"):
            freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        with Session(engine) as check:
            with pytest.raises(ReceiptSchemaError, match="trigger_schema_drift"):
                verify_current_snapshot(check, frozen["snapshot_id"], CLAIM)
            assert check.execute(text(
                "SELECT COUNT(*) FROM semantic_review_snapshots")).scalar() == 1
    finally:
        seed_session.close(); engine.dispose()


def test_saved_round_rejects_changed_evidence_or_scope():
    engine, seed_session = _seed()
    try:
        seed_session.close()
        ensure_semantic_schema(engine)
        frozen = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        with Session(engine) as check:
            changed_claim = {**CLAIM, "scope_basis": "另一审查依据"}
            with pytest.raises(SnapshotError, match="snapshot_stale"):
                verify_current_snapshot(check, frozen["snapshot_id"],
                                        changed_claim)
            check.get(ExpressionStrategyV2,
                      "ESV2-S").abstract_operation += " 现在改写"
            with pytest.raises(SnapshotError,
                               match="snapshot_verify_requires_clean_session"):
                verify_current_snapshot(check, frozen["snapshot_id"], CLAIM)
            check.flush()
            with pytest.raises(SnapshotError, match="snapshot_stale"):
                verify_current_snapshot(check, frozen["snapshot_id"], CLAIM)
            check.rollback()
        with Session(engine) as check:
            assert verify_current_snapshot(check, frozen["snapshot_id"], CLAIM)[
                "snapshot_id"] == frozen["snapshot_id"]
    finally:
        seed_session.close(); engine.dispose()


def test_recheck_compares_canonical_json_not_python_container_types(monkeypatch):
    engine, seed_session = _seed()
    try:
        seed_session.close()
        ensure_semantic_schema(engine)
        frozen = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        original = snapshot_store.build_snapshot

        def tuple_equivalent(*args):
            result = original(*args)
            result["payload"]["scope_claim"]["scope_ids"] = tuple(
                result["payload"]["scope_claim"]["scope_ids"])
            return result

        monkeypatch.setattr(snapshot_store, "build_snapshot", tuple_equivalent)
        with Session(engine) as check:
            assert verify_current_snapshot(check, frozen["snapshot_id"], CLAIM)[
                "snapshot_id"] == frozen["snapshot_id"]
    finally:
        seed_session.close(); engine.dispose()


def test_freeze_holds_file_database_writer_reservation(tmp_path, monkeypatch):
    engine, seed_session = _seed(db_path=tmp_path / "review.db")
    entered, release = Event(), Event()
    results, errors = [], []
    original = snapshot_store.build_snapshot

    def held_snapshot(*args):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test_release_timeout")
        return original(*args)

    try:
        seed_session.close()
        ensure_semantic_schema(engine)
        monkeypatch.setattr(snapshot_store, "build_snapshot", held_snapshot)

        def freeze():
            try:
                results.append(freeze_snapshot(engine, "ESV2-S", 1, CLAIM))
            except Exception as exc:
                errors.append(exc)

        worker = Thread(target=freeze, daemon=True)
        worker.start()
        assert entered.wait(3)
        with engine.connect() as conn:
            with pytest.raises(OperationalError, match="locked"):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            conn.rollback()
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert not errors
        assert len(results) == 1
    finally:
        release.set()
        seed_session.close(); engine.dispose()


def test_freeze_refuses_future_anchor_or_bad_digest(monkeypatch):
    engine, seed_session = _seed()
    try:
        seed_session.close()
        ensure_semantic_schema(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE promotion_audits SET ts='2099-01-01T00:00:00Z'")
        with pytest.raises(SnapshotError, match="snapshot_before_replicated_audit"):
            freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE promotion_audits SET ts='2026-09-27T00:00:00Z'")
        original = snapshot_store.build_snapshot

        def corrupted(*args):
            result = original(*args)
            return {**result, "content_sha256": "0" * 64}

        monkeypatch.setattr(snapshot_store, "build_snapshot", corrupted)
        with pytest.raises(SnapshotError, match="snapshot_digest_mismatch"):
            freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        with Session(engine) as check:
            assert check.execute(text(
                "SELECT COUNT(*) FROM semantic_review_snapshots")).scalar() == 0
    finally:
        seed_session.close(); engine.dispose()


def test_same_content_has_same_digest_despite_insertion_order_and_row_timestamp():
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
        if change in {"reviewer_marker", "segment_role", "segment_integrity"}:
            with pytest.raises(SnapshotError):
                build_snapshot(s, "ESV2-S", 1, claim)
        else:
            assert build_snapshot(s, "ESV2-S", 1, claim)[
                "content_sha256"] != before
    finally:
        s.close(); engine.dispose()


@pytest.mark.parametrize("failure", [
    "wrong_version", "missing_source", "bad_positive_span", "bad_positive_hash",
    "missing_audit", "missing_segment", "segment_work_mismatch",
    "invalid_integrity", "observation_not_replicated", "audit_ref_missing",
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
        elif failure == "missing_segment":
            s.delete(s.get(Segment, "SEG-A"))
        elif failure == "segment_work_mismatch":
            s.get(Segment, "SEG-A").work_id = "WK-M"
        elif failure == "invalid_integrity":
            s.get(Segment, "SEG-A").integrity = "not-json"
        elif failure == "observation_not_replicated":
            s.get(ExpressionStrategyV2, "ESV2-S").observation_status = "observed"
        elif failure == "audit_ref_missing":
            s.execute(text("UPDATE promotion_audits SET evidence_ref='[\"MISSING\"]'"))
        s.flush()
        with pytest.raises(SnapshotError):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
    finally:
        s.close(); engine.dispose()


def test_default_source_policy_is_enforced_and_frozen_in_review_input(monkeypatch):
    engine, s = _seed()
    try:
        before = build_snapshot(s, "ESV2-S", 1, CLAIM)
        monkeypatch.setattr(
            KQ, "DEFAULT_EXCLUDED_SOURCE_TYPES",
            KQ.DEFAULT_EXCLUDED_SOURCE_TYPES | frozenset({"new_excluded_type"}))
        after = build_snapshot(s, "ESV2-S", 1, CLAIM)
        assert after["review_input_sha256"] != before["review_input_sha256"]
        assert after["content_sha256"] != before["content_sha256"]

        s.query(WorkSource).filter_by(work_id="WK-A").one().source_type = "fixture"
        s.flush()
        claim = {**CLAIM, "scope_ids": ["WK-M"]}
        filtered = build_snapshot(s, "ESV2-S", 1, claim)
        assert "SI-A:excluded_source_type:fixture" in filtered["payload"][
            "evidence"]["stripped"]
        assert filtered["payload"]["evidence"]["facts"][
            "reviewed_ids"] == ["SI-Z"]
        assert filtered["review_input"]["policy"] == filtered["payload"]["policy"]

        s.query(WorkSource).filter_by(work_id="WK-A").one().source_type = "human_fiction"
        s.get(StrategyInstance, "SI-A").text_version = "corpus-v9"
        s.flush()
        version_filtered = build_snapshot(s, "ESV2-S", 1, claim)
        assert "SI-A:text_version:corpus-v9" in version_filtered["payload"][
            "evidence"]["stripped"]
    finally:
        s.close(); engine.dispose()


def test_filtered_verified_instance_with_bad_span_still_blocks_snapshot():
    engine, s = _seed()
    try:
        s.query(WorkSource).filter_by(work_id="WK-A").one().source_type = "fixture"
        s.get(StrategyInstance, "SI-A").evidence_text = "伪造的原文"
        s.flush()
        with pytest.raises(SnapshotError, match="positive_span_or_hash_mismatch:SI-A"):
            build_snapshot(s, "ESV2-S", 1,
                           {**CLAIM, "scope_ids": ["WK-M"]})
    finally:
        s.close(); engine.dispose()


def test_anchor_uses_chronological_utc_order_not_text_order():
    engine, s = _seed()
    try:
        s.execute(text(
            "INSERT INTO promotion_audits "
            "(audit_id,strategy_id,strategy_version,to_status,evidence_ref,ts) VALUES "
            "('AUD-EARLIER', 'ESV2-S', 1, 'replicated', "
            "'[\"SI-A\"]', '2026-09-27T01:00:00+08:00')"))
        first = build_snapshot(s, "ESV2-S", 1, CLAIM)
        assert first["replicated_audit_id"] == "AUD-R"
        s.execute(text(
            "INSERT INTO promotion_audits "
            "(audit_id,strategy_id,strategy_version,to_status,evidence_ref,ts) VALUES "
            "('AUD-LATER', 'ESV2-S', 1, 'replicated', "
            "'[\"SI-A\"]', '2026-09-27T09:00:00+08:00')"))
        second = build_snapshot(s, "ESV2-S", 1, CLAIM)
        assert second["replicated_audit_id"] == "AUD-LATER"
        assert second["content_sha256"] != first["content_sha256"]
    finally:
        s.close(); engine.dispose()


def test_invalid_scope_and_audit_inputs_fail_closed():
    engine, s = _seed()
    try:
        with pytest.raises(SnapshotError, match="strategy_version_not_found"):
            build_snapshot(s, "ESV2-S", 2, CLAIM)
        with pytest.raises(SnapshotError, match="unreviewable_scope_claim"):
            build_snapshot(s, "ESV2-S", 1, {**CLAIM, "scope_ids": []})
        with pytest.raises(SnapshotError, match="scope_claim_unbacked"):
            build_snapshot(s, "ESV2-S", 1,
                           {**CLAIM, "scope_ids": ["OTHER-WORK"]})
        s.execute(text("UPDATE promotion_audits SET ts='2026-09-27T00:00:00'"))
        with pytest.raises(SnapshotError, match="replicated_audit_invalid"):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
    finally:
        s.close(); engine.dispose()


def test_null_and_empty_effect_reference_have_different_digests():
    engine, s = _seed()
    try:
        before = build_snapshot(s, "ESV2-S", 1, CLAIM)["content_sha256"]
        s.get(StrategyInstance, "SI-A").effect_ref = ""
        s.flush()
        assert build_snapshot(s, "ESV2-S", 1, CLAIM)["content_sha256"] != before
    finally:
        s.close(); engine.dispose()


def test_missing_anchor_instance_and_unbacked_interval_are_typed_refusals():
    engine, s = _seed()
    try:
        s.execute(text("UPDATE promotion_audits SET evidence_ref='[\"MISSING\"]'"))
        with pytest.raises(SnapshotError, match="replicated_audit_evidence_missing"):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
        s.add(StrategyInstance(
            id="SI-NEG", strategy_id="ESV2-S", strategy_version=1,
            work_id="WK-A", segment_id="SEG-A", text_version="corpus-v1",
            span_start=6, span_end=11, evidence_text=TEXT[6:11],
            evidence_sha256=K.evidence_sha256(TEXT[6:11]),
            conditions_observed={}, observed_content="反例",
            extractor_model="seed", status="rejected"))
        s.flush()
        s.execute(text("UPDATE promotion_audits SET evidence_ref='[\"SI-NEG\"]'"))
        with pytest.raises(SnapshotError, match="replicated_audit_evidence_unbacked"):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
    finally:
        s.close(); engine.dispose()


def test_historical_bad_audit_timestamp_blocks_newer_valid_anchor():
    engine, s = _seed()
    try:
        s.execute(text(
            "INSERT INTO promotion_audits "
            "(audit_id,strategy_id,strategy_version,to_status,evidence_ref,ts) VALUES "
            "('AUD-BAD', 'ESV2-S', 1, 'replicated', '[\"SI-A\"]', 'not-a-time')"))
        with pytest.raises(SnapshotError, match="replicated_audit_invalid"):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
    finally:
        s.close(); engine.dispose()


def test_evidence_facts_schema_drift_is_not_silently_hashed(monkeypatch):
    from scripts import k5_promotion_write as KP
    engine, s = _seed()
    original = KP.evidence_facts
    try:
        monkeypatch.setattr(KP, "evidence_facts", lambda *args: {
            **original(*args), "unversioned_new_field": ["x"]})
        with pytest.raises(SnapshotError, match="evidence_facts_contract_changed"):
            build_snapshot(s, "ESV2-S", 1, CLAIM)
    finally:
        s.close(); engine.dispose()


def test_missing_integrity_is_recorded_without_claiming_src_ok():
    engine, s = _seed()
    try:
        s.get(Segment, "SEG-M").integrity = None
        s.flush()
        snapshot = build_snapshot(s, "ESV2-S", 1, CLAIM)
        row = next(x for x in snapshot["payload"]["evidence"]["segments"]
                   if x["id"] == "SEG-M")
        assert row["src_ok"] is False
    finally:
        s.close(); engine.dispose()
