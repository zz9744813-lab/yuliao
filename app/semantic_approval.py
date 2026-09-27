"""Read-only K2 receipt verification for a proposed K5 promotion.

    Read-only previews may call this without a writer reservation. A caller
    writing a verified audit and link must re-run it under BEGIN IMMEDIATE.
    Stored gateway metadata still depends on an accepted provenance route.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from .knowledge_query import canonical_json
from .semantic_receipts import require_semantic_schema
from .semantic_review import SnapshotError, _digest, _scope_claim
from .semantic_review_store import verify_current_snapshot


class ApprovalError(Exception):
    """No current, internally consistent two-model approval is available."""


def _utc(value: str) -> datetime:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ApprovalError("receipt_time_invalid") from exc


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check_vote(vote: dict, call: dict, snapshot: dict,
                instance_ids: set[str]) -> None:
    """Recheck the immutable row chain, including the runner's JSON envelope."""
    from .semantic_review_runner import MAX_OUTPUT_TOKENS, PROMPT_REGISTRY

    try:
        request = json.loads(call["request_json"])
        response = json.loads(call["response_json"])
        review = json.loads(vote["review_json"])
        parsed_text = json.loads(call["response_text"])
        body = response["body"]
        attestation = response["attestation"]
        pinned_route = response["pinned_route"]
        prompt = PROMPT_REGISTRY[response["prompt_version"]]
        choice = body["choices"][0]
        messages = request["messages"]
        cited = review["cited_instance_ids"]
        concerns = review["concerns"]
        if (canonical_json(request) != call["request_json"] or
                canonical_json(response) != call["response_json"] or
                canonical_json(review) != vote["review_json"] or
                parsed_text != review or
                _sha(call["request_json"]) != call["prompt_sha256"] or
                _sha(call["response_text"]) != call["response_sha256"] or
                call["response_sha256"] != vote["response_sha256"] or
                call["snapshot_id"] != snapshot["snapshot_id"] or
                vote["snapshot_id"] != snapshot["snapshot_id"] or
                vote["provider"] != call["provider"] or
                vote["model_id"] != call["model_id"] or
                call["input_sha256"] != snapshot["review_input_sha256"] or
                vote["input_sha256"] != snapshot["review_input_sha256"] or
                request["model"] != call["requested_model"] or
                len(messages) != 2 or
                messages[0] != {"role": "system", "content": prompt} or
                messages[1] != {"role": "user", "content":
                                snapshot["review_input_json"]} or
                request["temperature"] != 0 or
                request["stream"] is not False or
                type(request["max_tokens"]) is not int or
                not 1 <= request["max_tokens"] <= MAX_OUTPUT_TOKENS or
                body["id"] != call["upstream_request_id"] or
                body["model"] != call["model_id"] or
                len(body["choices"]) != 1 or
                choice["finish_reason"] != "stop" or
                choice["message"].get("role") != "assistant" or
                choice["message"].get("content") != call["response_text"] or
                set(pinned_route) != {"provider", "model", "channel_id",
                                      "requested_model"} or
                pinned_route["provider"] != call["provider"] or
                pinned_route["model"] != call["model_id"] or
                pinned_route["requested_model"] != call["requested_model"] or
                attestation["provider"] != pinned_route["provider"] or
                attestation["model"] != pinned_route["model"] or
                attestation["channel-id"] != pinned_route["channel_id"] or
                attestation["request-id"] != call["upstream_request_id"] or
                not isinstance(pinned_route["channel_id"], str) or
                not pinned_route["channel_id"].strip() or
                call["channel"] != "openai_http" or
                call["model_identity"] !=
                call["provider"] + "/" + call["model_id"] or
                vote["model_identity"] != call["model_identity"] or
                vote["judge_kind"] != "semantic_current" or
                review["verdict"] != vote["verdict"] or
                not isinstance(review["reason"], str) or
                not review["reason"].strip() or
                not isinstance(cited, list) or not cited or
                len(cited) != len(set(cited)) or
                not set(cited) <= instance_ids or
                not isinstance(concerns, list) or
                any(not isinstance(x, str) or not x.strip() for x in concerns) or
                (vote["verdict"] == "BLOCK" and not concerns) or
                _utc(call["completed_at"]) < _utc(snapshot["created_at"]) or
                _utc(vote["created_at"]) < _utc(call["completed_at"])):
            raise ValueError("receipt mismatch")
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise ApprovalError("vote_receipt_invalid") from exc


def _current_two_pass_approval(session: Session, strategy_id: str,
                               strategy_version: int, claim: dict,
                               scope_rule_version: str, *,
                               evidence_ref: list[str] | None,
                               admission: bool) -> dict:
    """Choose the newest current review round for the explicit scope claim.

    A newer incomplete/BLOCK/ABSTAIN round takes precedence over an older PASS
    round on the same content. A fresh round may resolve a prior disagreement.
    K5 additionally binds its proposed evidence list; K4/Writer has no new
    evidence proposal and must explicitly select ``admission=True``.
    """
    if not admission and evidence_ref is None:
        raise ApprovalError("promotion_evidence_ref_invalid")
    require_semantic_schema(session.connection())
    try:
        claim_digest = _digest(_scope_claim(claim, scope_rule_version))
    except SnapshotError as exc:
        raise ApprovalError("promotion_scope_claim_invalid") from exc
    rows = session.execute(text("""
        SELECT * FROM semantic_review_snapshots
        WHERE strategy_id=:strategy_id AND strategy_version=:version
          AND scope_claim_sha256=:claim_digest
    """), {"strategy_id": strategy_id, "version": strategy_version,
           "claim_digest": claim_digest}).mappings().all()
    if not rows:
        raise ApprovalError("snapshot_missing")
    # Receipts may contain different timezone spellings. Sort by actual UTC
    # instant, then stable ID, rather than lexicographic timestamp text.
    rows = sorted(rows, key=lambda row: (_utc(row["created_at"]),
                                         row["snapshot_id"]), reverse=True)
    latest = None
    for row in rows:
        try:
            verify_current_snapshot(session, row["snapshot_id"], claim)
        except SnapshotError as exc:
            if exc.code == "snapshot_stale":
                continue
            raise ApprovalError("snapshot_invalid") from exc
        latest = dict(row)
        break
    if latest is None:
        raise ApprovalError("snapshot_stale")
    try:
        payload = json.loads(latest["payload_json"])
        frozen = json.loads(latest["review_input_json"])
        reviewed = payload["evidence"]["facts"]["reviewed_ids"]
        instance_ids = {i["instance_id"] for i in frozen["instances"]}
        if ((not admission and sorted(evidence_ref) != sorted(reviewed)) or
                len(instance_ids) != len(frozen["instances"])):
            raise ValueError("planned evidence differs from reviewed evidence")
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalError("snapshot_plan_mismatch") from exc
    votes = session.execute(text("""
        SELECT * FROM semantic_review_votes WHERE snapshot_id=:snapshot_id
        ORDER BY vote_id
    """), {"snapshot_id": latest["snapshot_id"]}).mappings().all()
    if len(votes) < 2:
        raise ApprovalError("two_pass_votes_missing")
    models: set[str] = set()
    passes: list[str] = []
    for vote_row in votes:
        vote = dict(vote_row)
        call_row = session.execute(text("""
            SELECT * FROM semantic_review_calls WHERE call_id=:call_id
        """), {"call_id": vote["call_receipt_id"]}).mappings().one_or_none()
        if call_row is None:
            raise ApprovalError("call_receipt_missing")
        _check_vote(vote, dict(call_row), latest, instance_ids)
        model_id = vote["model_id"].casefold()
        if model_id in models:
            raise ApprovalError("same_upstream_model")
        models.add(model_id)
        attempts = session.execute(text("""
            SELECT COUNT(*) FROM semantic_review_calls
            WHERE snapshot_id=:snapshot_id AND lower(model_id)=:model_id
        """), {"snapshot_id": latest["snapshot_id"],
               "model_id": model_id}).scalar_one()
        if attempts != 1:
            raise ApprovalError("model_attempt_count_invalid")
        if vote["verdict"] == "PASS":
            passes.append(vote["vote_id"])
        else:
            code = (vote["verdict"] if vote["verdict"] in
                    {"BLOCK", "ABSTAIN"} else "INVALID")
            raise ApprovalError("non_pass_vote:" + code)
    return {"snapshot_id": latest["snapshot_id"],
            "content_sha256": latest["content_sha256"],
            "vote_a_id": passes[0], "vote_b_id": passes[1]}


def pre_promotion_approval(session: Session, strategy_id: str,
                           strategy_version: int, plan: dict,
                           scope_rule_version: str) -> dict:
    """K5 must match the planned evidence as well as the current two seats."""
    try:
        claim = {key: plan[key] for key in
                 ("scope_to", "scope_ids", "scope_basis")}
    except (KeyError, TypeError) as exc:
        raise ApprovalError("promotion_scope_claim_missing") from exc
    evidence_ref = plan.get("evidence_ref") if isinstance(plan, dict) else None
    if (not isinstance(evidence_ref, list) or not evidence_ref or
            any(not isinstance(ref, str) or not ref for ref in evidence_ref)):
        raise ApprovalError("promotion_evidence_ref_invalid")
    return _current_two_pass_approval(
        session, strategy_id, strategy_version, claim,
        scope_rule_version, evidence_ref=evidence_ref, admission=False)


def admission_current_approval(session: Session, strategy_id: str,
                               strategy_version: int, claim: dict,
                               scope_rule_version: str) -> dict:
    """K4/Writer verifies current evidence without inventing a K5 proposal."""
    return _current_two_pass_approval(
        session, strategy_id, strategy_version, claim,
        scope_rule_version, evidence_ref=None, admission=True)
