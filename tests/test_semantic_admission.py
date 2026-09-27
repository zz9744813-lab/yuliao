"""Synthetic K2 approvals can admit K3; unapproved/forged packages cannot."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import config, knowledge_query as kq
from app.models import ExpressionStrategyV2, KnowledgePackage as StoredPackage
from app.scene_runtime.contracts import (Budget, Change, Fact, PlannedEvent,
                                         RuntimeFault, ScenePlan, World)
from app.scene_runtime.knowledge_v2 import frozen_package_for_scene
from app.scene_runtime.pipeline import SceneRunner
from app.scene_runtime.store import Store
from app.semantic_admission import approved_selected
from app.scene_runtime.approval_gate import verify_frozen_package
from app.semantic_approval import ApprovalError
from test_k5_promotion_write import (_reviewable_round, _synthetic_vote,
                                     _verdict, k5w)


def _plan():
    return ScenePlan(book_id="WK-A", scene_id="admission-scene",
                     idempotency_key="admission-one", expected_revision=0,
                     pov="lin", goal="支付一枚钱", style="克制", min_chars=1,
                     max_chars=300, events=[PlannedEvent(
                         event_id="pay", description="支付",
                         changes=[Change(fact="coins", before=3, after=2)])])


def _world():
    return World(book_id="WK-A", revision=0,
                 characters={"lin": "林穗"},
                 facts={"coins": Fact(value=3, visible_to=["lin"])},
                 rules=[])


def _approved(monkeypatch, tmp_path):
    database, engine, snapshot_id = _reviewable_round(tmp_path, monkeypatch)
    _synthetic_vote(monkeypatch, engine, snapshot_id, "a")
    _synthetic_vote(monkeypatch, engine, snapshot_id, "b")
    verdict = _verdict(database, to="verified")
    assert verdict["decision"] == k5w.PROMOTE
    k5w.commit_promotion(database, verdict)
    return engine


def test_approved_query_freezes_manifest_and_direct_verifier_accepts(
        monkeypatch, tmp_path):
    engine = _approved(monkeypatch, tmp_path)
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(_world())
    monkeypatch.setattr(config, "LLM_MODE", "real")
    try:
        with Session(engine) as session:
            pkg, meta = frozen_package_for_scene(store, session, _plan())
            assert meta["n_selected"] == 1
            assert pkg.approval_manifest is not None
            assert pkg.techniques[0].evidence_status == "untested"
            assert pkg.approval_manifest.entries[0].kind == "pre_promotion"
            verify_frozen_package(session, _plan(), pkg)
            assert session.query(StoredPackage).count() == 1
        store.prepare(_plan(), pkg, Budget(),
                      {"writer": "synthetic-w", "verifier": "synthetic-v"})
        with Session(engine) as session:
            recovered, meta = frozen_package_for_scene(store, session,
                                                        _plan())
            assert meta["reused"] is True
            assert recovered == pkg
        forged = pkg.model_copy(deep=True)
        forged.approval_manifest.entries[0].content_sha256 = "0" * 64
        with Session(engine) as session:
            with pytest.raises(ApprovalError, match="approval_manifest_stale"):
                verify_frozen_package(session, _plan(), forged)
        with Session(engine) as session:
            row = session.query(StoredPackage).one()
            changed = [dict(item) for item in row.selected]
            changed[0]["abstract_operation"] = "被改写的操作"
            row.selected = changed
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ApprovalError, match="frozen_package_changed"):
                verify_frozen_package(session, _plan(), recovered)
    finally:
        engine.dispose()


def test_direct_writer_refuses_forged_manifest_before_model_call(
        monkeypatch, tmp_path):
    engine = _approved(monkeypatch, tmp_path)
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(_world())
    monkeypatch.setattr(config, "LLM_MODE", "real")

    class Client:
        models = {"writer": "synthetic-w", "verifier": "synthetic-v"}
        calls = 0

        def invoke(self, **kwargs):
            self.calls += 1
            raise RuntimeError("stop synthetic generation")

    client = Client()
    try:
        with Session(engine) as session:
            pkg, _ = frozen_package_for_scene(store, session, _plan())
        forged = pkg.model_copy(deep=True)
        forged.approval_manifest.entries[0].vote_a_id = "SV-forged"
        with pytest.raises(RuntimeFault, match="semantic_review_unverifiable"):
            SceneRunner(store, client, lg_engine=engine).run(
                _plan(), forged, Budget())
        assert client.calls == 0
        with pytest.raises(RuntimeFault, match="client_failure:RuntimeError"):
            SceneRunner(store, client, lg_engine=engine).run(
                _plan(), pkg, Budget())
        assert client.calls == 1
    finally:
        engine.dispose()


def test_new_review_round_revokes_old_manifest(monkeypatch, tmp_path):
    engine = _approved(monkeypatch, tmp_path)
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(_world())
    monkeypatch.setattr(config, "LLM_MODE", "real")
    try:
        with Session(engine) as session:
            pkg, _ = frozen_package_for_scene(store, session, _plan())
        from app.semantic_review_store import freeze_snapshot
        with Session(engine) as session:
            card = session.get(ExpressionStrategyV2, "ESV2-T")
            claim = {"scope_to": card.scope, "scope_ids": card.scope_ids,
                     "scope_basis": card.scope_basis}
        freeze_snapshot(engine, "ESV2-T", 1, claim)
        with Session(engine) as session:
            with pytest.raises(ApprovalError, match="two_pass_votes_missing"):
                verify_frozen_package(session, _plan(), pkg)
    finally:
        engine.dispose()


def test_historical_verified_card_needs_posthoc_release(monkeypatch, tmp_path):
    database, engine, sid = _reviewable_round(tmp_path, monkeypatch)
    _synthetic_vote(monkeypatch, engine, sid, "a")
    _synthetic_vote(monkeypatch, engine, sid, "b")
    verdict = _verdict(database, to="verified")
    plan, approval = verdict["plan"], verdict["approval"]
    old_ts = "2026-09-27T12:00:00Z"
    try:
        with engine.begin() as conn:
            assert conn.exec_driver_sql(
                k5w.CAS_VERIFIED_SQL,
                k5w._cas_values(plan, verdict, verified=True)).rowcount == 1
            conn.exec_driver_sql(k5w._audit_insert_sql(),
                                 k5w._audit_values(plan, verdict, old_ts))
        with Session(engine) as session:
            selected = kq.query_knowledge({"book_id": "WK-A"}, session)[
                "selected"]
            assert selected
            with pytest.raises(ApprovalError, match="approval_link_missing"):
                approved_selected(session, selected)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with engine.begin() as conn:
            conn.exec_driver_sql("""
                INSERT INTO semantic_approval_links
                (link_id,strategy_id,strategy_version,verified_audit_id,
                 snapshot_id,vote_a_id,vote_b_id,kind,content_sha256,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, ("SAP-posthoc-synthetic", "ESV2-T", 1,
                  k5w.audit_id_for(plan), sid, approval["vote_a_id"],
                  approval["vote_b_id"], "posthoc_release",
                  approval["content_sha256"], stamp))
        with Session(engine) as session:
            entries = approved_selected(session, selected)
            assert entries[0]["kind"] == "posthoc_release"
    finally:
        engine.dispose()


def test_freeze_rolls_back_package_if_receipt_step_fails(monkeypatch, tmp_path):
    engine = _approved(monkeypatch, tmp_path)
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(_world())
    monkeypatch.setattr(config, "LLM_MODE", "real")
    original = kq.freeze_package

    def fail_after_insert(response, session, *, commit=True):
        original(response, session, commit=commit)
        raise RuntimeError("synthetic receipt failure")

    monkeypatch.setattr(kq, "freeze_package", fail_after_insert)
    try:
        with Session(engine) as session:
            with pytest.raises(RuntimeError, match="synthetic receipt failure"):
                frozen_package_for_scene(store, session, _plan())
        with Session(engine) as session:
            assert session.query(StoredPackage).count() == 0
        with store.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        engine.dispose()


def test_freeze_holds_writer_reservation_through_approval(monkeypatch,
                                                           tmp_path):
    engine = _approved(monkeypatch, tmp_path)
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(_world())
    monkeypatch.setattr(config, "LLM_MODE", "real")
    import app.semantic_admission as admission
    original = admission.approved_selected
    checked = []

    def check_lock(session, selected):
        with engine.connect() as other:
            other.exec_driver_sql("PRAGMA busy_timeout=50")
            with pytest.raises(OperationalError, match="locked"):
                other.exec_driver_sql("BEGIN IMMEDIATE")
            other.rollback()
        checked.append(True)
        return original(session, selected)

    monkeypatch.setattr(admission, "approved_selected", check_lock)
    try:
        with Session(engine) as session:
            pkg, _ = frozen_package_for_scene(store, session, _plan())
            assert pkg.approval_manifest is not None
        assert checked == [True]
    finally:
        engine.dispose()


def test_existing_frozen_policy_can_have_bounded_extra_limit(monkeypatch,
                                                             tmp_path):
    engine = _approved(monkeypatch, tmp_path)
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(_world())
    monkeypatch.setattr(config, "LLM_MODE", "real")
    try:
        with Session(engine) as session:
            pkg, _ = frozen_package_for_scene(store, session, _plan())
            row = session.query(StoredPackage).one()
            policy = {**row.policy,
                      "limits": {**row.policy["limits"],
                                 "max_context_chars": 800}}
            row.policy = policy
            row.policy_sha256 = kq.policy_sha256(policy)
            package_sha = hashlib.sha256(kq.canonical_json({
                "policy": policy,
                "selected_ids": [item["strategy_id"] for item in row.selected],
                "snapshot": row.snapshot_fingerprint,
            }).encode("utf-8")).hexdigest()
            row.package_sha256 = package_sha
            session.commit()
        pkg.package_id = "kq-" + package_sha[:20]
        pkg.approval_manifest.package_sha256 = package_sha
        with Session(engine) as session:
            verify_frozen_package(session, _plan(), pkg)
    finally:
        engine.dispose()
