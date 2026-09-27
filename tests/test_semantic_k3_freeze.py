"""Approved K2 receipts and K3 package insertion share one SQLite write txn."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import config, knowledge_query as kq
from app.models import KnowledgePackage as StoredPackage
from app.scene_runtime.contracts import (Change, Fact, PlannedEvent,
                                         ScenePlan, World)
from app.scene_runtime.knowledge_v2 import frozen_package_for_scene
from app.scene_runtime.store import Store
from test_k5_promotion_write import (_reviewable_round, _synthetic_vote,
                                     _verdict, k5w)


def test_approved_freeze_is_atomic_and_preserves_untested(monkeypatch,
                                                          tmp_path):
    database, engine, sid = _reviewable_round(tmp_path, monkeypatch)
    _synthetic_vote(monkeypatch, engine, sid, "a")
    _synthetic_vote(monkeypatch, engine, sid, "b")
    k5w.commit_promotion(database, _verdict(database, to="verified"))
    store = Store(tmp_path / "scene.sqlite")
    store.create_world(World(
        book_id="WK-A", revision=0, characters={"lin": "林穗"},
        facts={"coins": Fact(value=3, visible_to=["lin"])}, rules=[]))
    plan = ScenePlan(
        book_id="WK-A", scene_id="scene-1", idempotency_key="freeze-1",
        expected_revision=0, pov="lin", goal="支付", style="克制",
        events=[PlannedEvent(event_id="pay", description="支付",
                             changes=[Change(fact="coins", before=3,
                                             after=2)])])
    monkeypatch.setattr(config, "LLM_MODE", "real")
    try:
        with Session(engine) as session:
            pkg, meta = frozen_package_for_scene(store, session, plan)
            assert meta["n_selected"] == 1
            assert pkg.approval_manifest.entries[0].kind == "pre_promotion"
            assert pkg.techniques[0].evidence_status == "untested"
            assert session.query(StoredPackage).count() == 1
        with Session(engine) as session:
            next_plan = plan.model_copy(update={"idempotency_key": "freeze-2",
                                                "scene_id": "scene-2"})
            original = kq.freeze_package

            def fail_after_insert(response, inner, *, commit=True):
                original(response, inner, commit=commit)
                raise RuntimeError("injected_failure")

            monkeypatch.setattr(kq, "freeze_package", fail_after_insert)
            with pytest.raises(RuntimeError, match="injected_failure"):
                frozen_package_for_scene(store, session, next_plan)
        with Session(engine) as session:
            assert session.query(StoredPackage).count() == 1
    finally:
        engine.dispose()
