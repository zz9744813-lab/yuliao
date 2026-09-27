"""Offline checks of K2 receipt fields used to prove independent K5 seats."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.orm import Session

import app.semantic_review_runner as runner
from app.knowledge_query import canonical_json
from app.semantic_approval import (ApprovalError, _check_vote,
                                   admission_current_approval,
                                   pre_promotion_approval)
from app.semantic_review_store import freeze_snapshot
from scripts.k5_promotion_write import SCOPE_RULE_VERSION
from test_semantic_review_snapshot import CLAIM
from test_semantic_review_runner import _ready, _response, _run


def test_upstream_model_and_pinned_channel_are_independently_bound(
        monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_post_once", lambda raw, timeout: _response())
    try:
        result = _run(engine, sid, tmp_path)
        with engine.connect() as conn:
            snapshot = dict(conn.exec_driver_sql(
                "SELECT * FROM semantic_review_snapshots WHERE snapshot_id=?",
                (sid,)).mappings().one())
            vote = dict(conn.exec_driver_sql(
                "SELECT * FROM semantic_review_votes WHERE vote_id=?",
                (result["vote_id"],)).mappings().one())
            call = dict(conn.exec_driver_sql(
                "SELECT * FROM semantic_review_calls WHERE call_id=?",
                (result["call_id"],)).mappings().one())
        ids = {"SI-A", "SI-Z"}
        _check_vote(vote, call, snapshot, ids)
        with pytest.raises(ApprovalError, match="vote_receipt_invalid"):
            _check_vote({**vote, "model_id": "forged-model"}, call,
                        snapshot, ids)
        envelope = json.loads(call["response_json"])
        envelope["pinned_route"]["channel_id"] = "forged-channel"
        with pytest.raises(ApprovalError, match="vote_receipt_invalid"):
            _check_vote(vote, {**call, "response_json":
                              canonical_json(envelope)}, snapshot, ids)
    finally:
        engine.dispose()


def _two_pass_round(monkeypatch, tmp_path):
    engine, sid = _ready(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_post_once", lambda raw, timeout: _response())
    _run(engine, sid, tmp_path)
    review = {"verdict": "PASS", "reason": "独立核对当前证据",
              "cited_instance_ids": ["SI-A"], "concerns": []}
    response = httpx.Response(200, headers={
        "X-LG-Upstream-Provider": "provider-b",
        "X-LG-Upstream-Model": "actual-b",
        "X-LG-Upstream-Channel-Id": "channel-8",
        "X-LG-Upstream-Request-Id": "upstream-b"},
        json={"id": "upstream-b", "model": "actual-b",
              "choices": [{"finish_reason": "stop", "message": {
                  "role": "assistant", "content": json.dumps(
                      review, ensure_ascii=False)}}]})
    monkeypatch.setattr(runner, "_post_once", lambda raw, timeout: response)
    runner.review_snapshot(
        engine, sid, runner.ReviewRoute("requested-b", "provider-b",
                                       "actual-b", "channel-8"),
        max_output_tokens=512, max_request_bytes=100_000,
        timeout_seconds=15)
    with engine.connect() as conn:
        payload = json.loads(conn.exec_driver_sql(
            "SELECT payload_json FROM semantic_review_snapshots "
            "WHERE snapshot_id=?", (sid,)).scalar())
    plan = {**CLAIM,
            "evidence_ref": payload["evidence"]["facts"]["reviewed_ids"]}
    return engine, sid, plan


def test_current_two_pass_round_and_newer_unfinished_round(monkeypatch,
                                                             tmp_path):
    engine, sid, plan = _two_pass_round(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            approval = pre_promotion_approval(
                session, "ESV2-S", 1, plan, SCOPE_RULE_VERSION)
        assert approval["snapshot_id"] == sid
        assert approval["vote_a_id"] != approval["vote_b_id"]
        newer = freeze_snapshot(engine, "ESV2-S", 1, CLAIM)
        assert newer["snapshot_id"] != sid
        with Session(engine) as session:
            with pytest.raises(ApprovalError, match="two_pass_votes_missing"):
                pre_promotion_approval(session, "ESV2-S", 1, plan,
                                       SCOPE_RULE_VERSION)
    finally:
        engine.dispose()


def test_promotion_evidence_is_mandatory_but_admission_is_explicit(
        monkeypatch, tmp_path):
    engine, sid, plan = _two_pass_round(monkeypatch, tmp_path)
    try:
        with Session(engine) as session:
            for invalid in (None, [], "SI-A", [None]):
                with pytest.raises(ApprovalError,
                                   match="promotion_evidence_ref_invalid"):
                    pre_promotion_approval(
                        session, "ESV2-S", 1,
                        {**plan, "evidence_ref": invalid},
                        SCOPE_RULE_VERSION)
            with pytest.raises(ApprovalError, match="snapshot_plan_mismatch"):
                pre_promotion_approval(
                    session, "ESV2-S", 1,
                    {**plan, "evidence_ref": ["SI-forged"]},
                    SCOPE_RULE_VERSION)
            assert admission_current_approval(
                session, "ESV2-S", 1, CLAIM,
                SCOPE_RULE_VERSION)["snapshot_id"] == sid
    finally:
        engine.dispose()


def test_extra_recorded_attempt_cannot_borrow_one_pass_vote(monkeypatch,
                                                             tmp_path):
    engine, sid, plan = _two_pass_round(monkeypatch, tmp_path)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""
                INSERT INTO semantic_review_calls
                (call_id,snapshot_id,channel,provider,model_id,model_identity,
                 requested_model,upstream_request_id,input_sha256,prompt_sha256,
                 response_sha256,response_text,request_json,response_json,
                 completed_at)
                SELECT 'extra-call',snapshot_id,channel,provider,model_id,
                       model_identity,requested_model,'extra-request',input_sha256,
                       prompt_sha256,response_sha256,response_text,request_json,
                       response_json,completed_at
                FROM semantic_review_calls WHERE model_id='actual-a'
            """)
        with Session(engine) as session:
            with pytest.raises(ApprovalError,
                               match="model_attempt_count_invalid"):
                pre_promotion_approval(session, "ESV2-S", 1, plan,
                                       SCOPE_RULE_VERSION)
    finally:
        engine.dispose()
