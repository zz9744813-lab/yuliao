"""Freeze and recheck one K2 semantic review round.

The round contains review material, not a verdict. Consumers must still
verify real model-call receipts and an approval link before admission.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .knowledge_query import canonical_json
from .semantic_receipts import require_semantic_schema
from .semantic_review import (ALGORITHM_VERSION, SCHEMA_VERSION,
                              SnapshotError, build_snapshot)


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_time(value: str) -> datetime:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (TypeError, AttributeError, ValueError) as exc:
        raise SnapshotError("snapshot_time_invalid") from exc


def freeze_snapshot(engine: Engine, strategy_id: str, strategy_version: int,
                    scope_claim: dict) -> dict:
    """Insert a new immutable review round over one consistent DB state.

    A new UUID is intentional: a BLOCK or ABSTAIN round can be retried on
    identical content without rewriting its prior votes. Model calls happen
    *after* this transaction, using the saved ``review_input_json``.
    """
    with engine.connect() as conn:
        # SQLite's legacy driver does not start a read transaction on SELECT.
        # Acquire the writer reservation before the first evidence read so no
        # concurrent edit can slip between hashing and inserting the receipt.
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            require_semantic_schema(conn)
            with Session(bind=conn, autoflush=False) as session:
                snapshot = build_snapshot(
                    session, strategy_id, strategy_version, scope_claim)
            payload = snapshot["payload"]
            review_input = snapshot["review_input"]
            if (_digest(payload) != snapshot["content_sha256"] or
                    _digest(review_input) != snapshot["review_input_sha256"]):
                raise SnapshotError("snapshot_digest_mismatch")
            created_at = datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z")
            anchor_at = _utc_time(payload["evidence"]["replicated_audit"]["ts"])
            if _utc_time(created_at) < anchor_at:
                raise SnapshotError("snapshot_before_replicated_audit")
            snapshot_id = "SS-" + uuid.uuid4().hex
            conn.execute(text("""
                INSERT INTO semantic_review_snapshots
                (snapshot_id, schema_version, algorithm_version,
                 strategy_id, strategy_version, replicated_audit_id,
                 card_sha256, evidence_sha256, scope_claim_sha256,
                 policy_sha256, content_sha256, payload_json,
                 review_input_sha256, review_input_json, created_at)
                VALUES (:snapshot_id, :schema_version, :algorithm_version,
                        :strategy_id, :strategy_version, :replicated_audit_id,
                        :card_sha256, :evidence_sha256, :scope_claim_sha256,
                        :policy_sha256, :content_sha256, :payload_json,
                        :review_input_sha256, :review_input_json, :created_at)
            """), {
                **{key: snapshot[key] for key in (
                    "schema_version", "algorithm_version", "strategy_id",
                    "strategy_version", "replicated_audit_id", "card_sha256",
                    "evidence_sha256", "scope_claim_sha256", "policy_sha256",
                    "content_sha256", "review_input_sha256")},
                "snapshot_id": snapshot_id,
                "payload_json": canonical_json(payload),
                "review_input_json": canonical_json(review_input),
                "created_at": created_at,
            })
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"snapshot_id": snapshot_id, "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "content_sha256": snapshot["content_sha256"],
            "review_input_sha256": snapshot["review_input_sha256"],
            "created_at": created_at}


def verify_current_snapshot(session: Session, snapshot_id: str,
                            scope_claim: dict) -> dict:
    """Check a saved round against current evidence in the caller's txn.

    The caller supplies the *expected* scope claim (current card for K4,
    proposed target for K5); a stored claim cannot authorize itself.
    """
    require_semantic_schema(session.connection())
    row = session.execute(text(
        "SELECT * FROM semantic_review_snapshots WHERE snapshot_id=:sid"),
        {"sid": snapshot_id}).mappings().one_or_none()
    if row is None:
        raise SnapshotError("snapshot_missing")
    if (row["schema_version"] != SCHEMA_VERSION or
            row["algorithm_version"] != ALGORITHM_VERSION):
        raise SnapshotError("snapshot_algorithm_mismatch")
    try:
        payload = json.loads(row["payload_json"])
        review_input = json.loads(row["review_input_json"])
    except (TypeError, ValueError) as exc:
        raise SnapshotError("snapshot_json_invalid") from exc
    if not isinstance(payload, dict) or not isinstance(review_input, dict):
        raise SnapshotError("snapshot_json_invalid")
    if (canonical_json(payload) != row["payload_json"] or
            canonical_json(review_input) != row["review_input_json"] or
            _digest(payload) != row["content_sha256"] or
            _digest(review_input) != row["review_input_sha256"] or
            _digest(payload.get("strategy")) != row["card_sha256"] or
            _digest(payload.get("evidence")) != row["evidence_sha256"] or
            _digest(payload.get("scope_claim")) != row["scope_claim_sha256"] or
            _digest(payload.get("policy")) != row["policy_sha256"]):
        raise SnapshotError("stored_snapshot_digest_mismatch")
    current = build_snapshot(session, row["strategy_id"],
                             row["strategy_version"], scope_claim)
    if (row["replicated_audit_id"] != current["replicated_audit_id"] or
            row["content_sha256"] != current["content_sha256"] or
            row["review_input_sha256"] != current["review_input_sha256"] or
            payload != current["payload"] or
            review_input != current["review_input"]):
        raise SnapshotError("snapshot_stale")
    if (_utc_time(row["created_at"]) <
            _utc_time(payload["evidence"]["replicated_audit"]["ts"])):
        raise SnapshotError("snapshot_before_replicated_audit")
    return {"snapshot_id": row["snapshot_id"],
            "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"],
            "replicated_audit_id": row["replicated_audit_id"],
            "content_sha256": row["content_sha256"],
            "review_input_sha256": row["review_input_sha256"],
            "created_at": row["created_at"]}
