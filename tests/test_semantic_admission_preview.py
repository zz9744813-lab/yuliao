"""K4 preview accepts only the current, linked K2 two-seat round."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import knowledge_query as kq
from app.models import ExpressionStrategyV2
from app.semantic_admission import approved_selected
from app.semantic_approval import ApprovalError
from app.semantic_review_store import freeze_snapshot
from scripts.k4_paired_scenes import preflight_world
from test_k5_promotion_write import (_reviewable_round, _synthetic_vote,
                                     _verdict, k5w)


def test_linked_current_round_passes_then_new_round_vetoes(monkeypatch, tmp_path):
    database, engine, sid = _reviewable_round(tmp_path, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, sid, "a")
        _synthetic_vote(monkeypatch, engine, sid, "b")
        verdict = _verdict(database, to="verified")
        k5w.commit_promotion(database, verdict)
        with Session(engine) as session:
            selected = kq.query_knowledge({"book_id": "WK-A"}, session)[
                "selected"]
            assert approved_selected(session, selected)[0]["kind"] == \
                "pre_promotion"
            pre = preflight_world("WK-A", session)
            assert pre["review_status"] == "verified"
            assert pre["knowledge_ready"] is True
            assert pre["ready"] is False
            assert pre["world_reason"].startswith("real_scene_plan_unverified")
            card = session.get(ExpressionStrategyV2, "ESV2-T")
            claim = {"scope_to": card.scope, "scope_ids": card.scope_ids,
                     "scope_basis": card.scope_basis}
        freeze_snapshot(engine, "ESV2-T", 1, claim)
        with Session(engine) as session:
            with pytest.raises(ApprovalError, match="two_pass_votes_missing"):
                approved_selected(session, selected)
            pre = preflight_world("WK-A", session)
            assert pre["ready"] is False
            assert pre["review_status"] == "semantic_review_unverifiable"
    finally:
        engine.dispose()
