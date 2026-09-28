"""A historical verified audit gains a link only after current K2 receipts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import knowledge_query as kq
from app.semantic_admission import approved_selected
from app.semantic_approval import ApprovalError
from app.semantic_posthoc import (commit_posthoc_release,
                                  inspect_posthoc_release)
from app.semantic_review_store import freeze_snapshot
from app.semantic_receipts import ReceiptSchemaError
from test_k5_promotion_write import (_card_row, _reviewable_round, _rows,
                                     _synthetic_vote, _verdict, k5w)


def _historical(tmp_path, monkeypatch, *, audit_changes=None,
                audit_time=None):
    db, engine, sid = _reviewable_round(tmp_path, monkeypatch)
    pending = _verdict(db, to="verified")
    plan = pending["proposal"]
    assert pending["decision"] == k5w.NO_PROMOTE
    replicated_at = _rows(db, "promotion_audits", "ts",
                          " WHERE to_status='replicated'")[0][0]
    ts = audit_time or replicated_at
    values = list(k5w._audit_values(plan, pending, ts))
    for key, value in (audit_changes or {}).items():
        values[k5w.AUDIT_COLUMNS.index(key)] = value
    with engine.begin() as conn:
        assert conn.exec_driver_sql(
            k5w.CAS_VERIFIED_SQL,
            k5w._cas_values(plan, pending, verified=True)).rowcount == 1
        conn.exec_driver_sql(k5w._audit_insert_sql(), tuple(values))
    return db, engine, sid, k5w.audit_id_for(plan)


def test_posthoc_release_appends_one_link_without_rewriting_history(
        tmp_path, monkeypatch):
    db, engine, sid, audit_id = _historical(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        card_before = _card_row(db)
        audits_before = _rows(db, "promotion_audits")
        with Session(engine) as session:
            preview = inspect_posthoc_release(session, "ESV2-T")
            assert preview["verified_audit_id"] == audit_id
            assert _rows(db, "semantic_approval_links") == []
        receipt = commit_posthoc_release(engine, "ESV2-T")
        assert receipt["snapshot_id"] == sid
        assert receipt["kind"] == "posthoc_release"
        assert _card_row(db) == card_before
        assert _rows(db, "promotion_audits") == audits_before
        assert _rows(db, "semantic_approval_links", "kind,link_id") == [
            ("posthoc_release", receipt["link_id"])]
        with Session(engine) as session:
            selected = kq.query_knowledge({"book_id": "WK-A"}, session)[
                "selected"]
            assert approved_selected(session, selected)[0]["link_id"] == \
                receipt["link_id"]
        with pytest.raises(ApprovalError, match="posthoc_round_already_linked"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links", "COUNT(*)") == [(1,)]
    finally:
        engine.dispose()


@pytest.mark.parametrize("second_verdict", [None, "BLOCK", "ABSTAIN"])
def test_posthoc_refuses_incomplete_or_nonpass_round(
        tmp_path, monkeypatch, second_verdict):
    db, engine, sid, _ = _historical(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        if second_verdict:
            _synthetic_vote(monkeypatch, engine, sid, "b", second_verdict)
        with pytest.raises(ApprovalError):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


def test_posthoc_latest_round_and_evidence_changes_revoke_old_votes(
        tmp_path, monkeypatch):
    db, engine, sid, _ = _historical(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        card = _rows(db, "expression_strategies_v2",
                     "scope,scope_ids,scope_basis", " WHERE id='ESV2-T'")[0]
        claim = {"scope_to": card[0], "scope_ids": json.loads(card[1]),
                 "scope_basis": card[2]}
        freeze_snapshot(engine, "ESV2-T", 1, claim)
        with pytest.raises(ApprovalError, match="two_pass_votes_missing"):
            commit_posthoc_release(engine, "ESV2-T")
        with sqlite3.connect(db.as_posix()) as conn:
            conn.execute("UPDATE segments SET text=text || '。' "
                         "WHERE id='SG-WK-A'")
        with pytest.raises(ApprovalError, match="snapshot_stale"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("changes", [
    {"policy_sha256": "0" * 64},
    {"scope_rule_version": "unknown"},
    {"evidence_count": 0},
    {"audit_id": "PAUD-forged-historical-audit"},
])
def test_posthoc_rejects_bad_historical_audit(
        tmp_path, monkeypatch, changes):
    db, engine, sid, _ = _historical(
        tmp_path, monkeypatch, audit_changes=changes)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        with pytest.raises(ApprovalError,
                           match="historical_verified_audit_invalid"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


def test_posthoc_rejects_audit_after_review(tmp_path, monkeypatch):
    future = datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat()
    db, engine, sid, _ = _historical(
        tmp_path, monkeypatch, audit_time=future)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        with pytest.raises(ApprovalError,
                           match="posthoc_audit_chronology_invalid"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


def test_posthoc_rejects_future_vote_time(tmp_path, monkeypatch):
    db, engine, sid, _ = _historical(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        import app.semantic_review_runner as runner
        monkeypatch.setattr(
            runner, "_now",
            lambda: datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat())
        _synthetic_vote(monkeypatch, engine, sid, "b")
        with pytest.raises(ApprovalError,
                           match="posthoc_vote_chronology_invalid"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


def test_posthoc_requires_schema_and_rolls_back_failed_link(
        tmp_path, monkeypatch):
    db, engine, sid, _ = _historical(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        with sqlite3.connect(db.as_posix()) as conn:
            conn.execute("CREATE TRIGGER synthetic_posthoc_fail BEFORE INSERT "
                         "ON semantic_approval_links BEGIN SELECT "
                         "RAISE(ABORT, 'synthetic posthoc failure'); END")
        before = _rows(db, "promotion_audits")
        with pytest.raises(IntegrityError, match="synthetic posthoc failure"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "promotion_audits") == before
        assert _rows(db, "semantic_approval_links") == []
        with sqlite3.connect(db.as_posix()) as conn:
            conn.execute("DROP TRIGGER synthetic_posthoc_fail")
            conn.execute("DROP TRIGGER semantic_approval_links_no_delete")
        with pytest.raises(ReceiptSchemaError, match="trigger_schema_drift"):
            commit_posthoc_release(engine, "ESV2-T")
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


def test_posthoc_holds_writer_reservation_through_recheck(
        tmp_path, monkeypatch):
    db, engine, sid, _ = _historical(tmp_path, monkeypatch)
    _synthetic_vote(monkeypatch, engine, sid, "a")
    _synthetic_vote(monkeypatch, engine, sid, "b")
    import app.semantic_posthoc as posthoc
    original = posthoc.inspect_posthoc_release

    def check_lock(session, strategy_id):
        with engine.connect() as other:
            other.exec_driver_sql("PRAGMA busy_timeout=50")
            with pytest.raises(OperationalError, match="locked"):
                other.exec_driver_sql("BEGIN IMMEDIATE")
            other.rollback()
        return original(session, strategy_id)

    monkeypatch.setattr(posthoc, "inspect_posthoc_release", check_lock)
    try:
        assert commit_posthoc_release(engine, "ESV2-T")["kind"] == \
            "posthoc_release"
    finally:
        engine.dispose()
