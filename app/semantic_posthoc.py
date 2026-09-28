"""Release an already-verified card only after a current K2 review round.

The historical promotion audit is never rewritten or backdated. Callers may
preview with a read-only Session; the write entry point repeats every check
under a SQLite writer reservation before appending one approval link.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .knowledge_query import canonical_json, policy_sha256
from .models import ExpressionStrategyV2
from .promotion_audits import require_promotion_audit_schema
from .semantic_admission import approved_selected
from .semantic_approval import ApprovalError, _utc, admission_current_approval
from .semantic_receipts import require_semantic_schema


def inspect_posthoc_release(session: Session, strategy_id: str) -> dict:
    """Preview one historical release; this is not an admission decision."""
    if session.new or session.dirty or session.deleted:
        raise ApprovalError("posthoc_requires_clean_session")
    if not isinstance(strategy_id, str) or not strategy_id:
        raise ApprovalError("strategy_id_invalid")
    require_promotion_audit_schema(session.connection())
    require_semantic_schema(session.connection())
    card = session.get(ExpressionStrategyV2, strategy_id)
    if (card is None or card.status != "verified" or
            card.observation_status != "replicated"):
        raise ApprovalError("historical_verified_card_missing")
    audits = session.execute(text("""
        SELECT * FROM promotion_audits
        WHERE strategy_id=:sid AND strategy_version=:version
          AND to_status='verified'
    """), {"sid": strategy_id, "version": card.version}).mappings().all()
    if len(audits) != 1:
        raise ApprovalError("historical_verified_audit_missing_or_ambiguous")
    audit = dict(audits[0])
    try:
        evidence = json.loads(audit["evidence_ref"])
        columns = json.loads(audit["columns_written"])
        audit_identity = {
            key: audit[key] for key in ("strategy_id", "from_status",
                                        "to_status", "scope_to", "gate_version")}
        audit_identity["evidence_ref"] = evidence
        expected_id = "PAUD-" + hashlib.sha256(canonical_json(
            audit_identity).encode("utf-8")).hexdigest()[:24]
        if (audit["tool"] != "k5_promotion_write" or
                audit["audit_id"] != expected_id or
                audit["gate_version"] != "k5_promotion_write/v1" or
                audit["strategy_key"] != card.strategy_key or
                audit["from_status"] != "replicated" or
                audit["status_column_from"] != "hypothesis" or
                audit["status_column_to"] != "verified" or
                audit["observation_from"] != "replicated" or
                audit["observation_to"] != "replicated" or
                audit["scope_to"] != card.scope or
                audit["scope_ids"] != canonical_json(
                    json.loads(audit["scope_ids"])) or
                canonical_json(json.loads(audit["scope_ids"])) !=
                canonical_json(card.scope_ids or []) or
                audit["scope_basis"] != card.scope_basis or
                audit["scope_rule_version"] != "scope-derive-1" or
                # Historical K5 promotions used the fixed, unfiltered K2
                # policy. A future policy needs a new review contract.
                audit["policy_sha256"] != policy_sha256({}) or
                not isinstance(audit["reviewer"], str) or
                not audit["reviewer"].strip() or
                not isinstance(evidence, list) or not evidence or
                audit["evidence_ref"] != canonical_json(evidence) or
                any(not isinstance(ref, str) or not ref for ref in evidence) or
                len(set(evidence)) != len(evidence) or
                audit["evidence_count"] != len(evidence) or
                columns != {"status": "verified",
                            "observation_status": "replicated",
                            "scope": card.scope}):
            raise ValueError("historical audit differs from card or policy")
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalError("historical_verified_audit_invalid") from exc

    claim = {"scope_to": card.scope, "scope_ids": card.scope_ids,
             "scope_basis": card.scope_basis}
    approval = admission_current_approval(
        session, strategy_id, card.version, claim,
        audit["scope_rule_version"])
    snapshot = session.execute(text("""
        SELECT created_at, replicated_audit_id
        FROM semantic_review_snapshots WHERE snapshot_id=:sid
    """), {"sid": approval["snapshot_id"]}).mappings().one_or_none()
    if snapshot is None:
        raise ApprovalError("posthoc_snapshot_missing")
    replicated_at = session.execute(text("""
        SELECT ts FROM promotion_audits WHERE audit_id=:aid
    """), {"aid": snapshot["replicated_audit_id"]}).scalar_one_or_none()
    if replicated_at is None:
        raise ApprovalError("posthoc_replicated_audit_missing")
    if not _utc(replicated_at) <= _utc(audit["ts"]) < _utc(snapshot["created_at"]):
        raise ApprovalError("posthoc_audit_chronology_invalid")
    existing = session.execute(text("""
        SELECT link_id FROM semantic_approval_links
        WHERE verified_audit_id=:aid AND snapshot_id=:sid
    """), {"aid": audit["audit_id"],
           "sid": approval["snapshot_id"]}).scalar_one_or_none()
    if existing is not None:
        raise ApprovalError("posthoc_round_already_linked")
    return {"strategy_id": strategy_id, "strategy_version": card.version,
            "verified_audit_id": audit["audit_id"], **approval}


def commit_posthoc_release(engine: Engine, strategy_id: str) -> dict:
    """Append one link after rechecking card, audit and real-call receipts.

    BEGIN IMMEDIATE is acquired before the first read. A failed insert or
    downstream admission check rolls back the entire release.
    """
    with engine.connect() as conn:
        if conn.engine.url.get_backend_name() != "sqlite":
            raise ApprovalError("posthoc_requires_sqlite")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            with Session(bind=conn, autoflush=False) as session:
                release = inspect_posthoc_release(session, strategy_id)
                now = datetime.now(timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z")
                if (not release["vote_a_id"] or
                        not release["vote_b_id"] or
                        release["vote_a_id"] == release["vote_b_id"]):
                    raise ApprovalError("posthoc_vote_identity_invalid")
                vote_times = session.execute(text("""
                    SELECT created_at FROM semantic_review_votes
                    WHERE snapshot_id=:sid AND vote_id IN (:a, :b)
                """), {"a": release["vote_a_id"],
                       "b": release["vote_b_id"],
                       "sid": release["snapshot_id"]}).scalars().all()
                if len(vote_times) != 2 or any(
                        _utc(now) < _utc(ts) for ts in vote_times):
                    raise ApprovalError("posthoc_vote_chronology_invalid")
                link_id = "SAP-" + hashlib.sha256(
                    (release["verified_audit_id"] + "\n" +
                     release["snapshot_id"] + "\nposthoc_release").encode(
                         "utf-8")).hexdigest()[:24]
                conn.exec_driver_sql("""
                    INSERT INTO semantic_approval_links
                    (link_id,strategy_id,strategy_version,verified_audit_id,
                     snapshot_id,vote_a_id,vote_b_id,kind,content_sha256,
                     created_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (link_id, strategy_id, release["strategy_version"],
                      release["verified_audit_id"], release["snapshot_id"],
                      release["vote_a_id"], release["vote_b_id"],
                      "posthoc_release", release["content_sha256"], now))
                card = session.get(ExpressionStrategyV2, strategy_id)
                selected = [{key: getattr(card, key) for key in (
                    "strategy_key", "status", "scope", "observation_status",
                    "effect_status", "abstract_operation", "invariants",
                    "failure_modes", "effect_hypothesis")} |
                    {"strategy_id": strategy_id, "version": card.version}]
                admitted = approved_selected(session, selected)
                if len(admitted) != 1 or admitted[0]["link_id"] != link_id:
                    raise ApprovalError("posthoc_admission_check_failed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {**release, "link_id": link_id, "kind": "posthoc_release",
            "created_at": now}
