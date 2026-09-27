"""Add-only SQLite schema for versioned K2 semantic review receipts.

This module creates empty receipt tables and database-level immutability guards.
It does not cast a vote, approve a card, or admit a package to Writer.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine


class ReceiptSchemaError(RuntimeError):
    """A receipt table or its immutable trigger differs from the contract."""


TABLE_DDL = {
    "semantic_review_snapshots": """
CREATE TABLE IF NOT EXISTS semantic_review_snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL CHECK(length(snapshot_id) > 0),
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    algorithm_version TEXT NOT NULL CHECK(algorithm_version = 'semantic-evidence/v1'),
    strategy_id TEXT NOT NULL REFERENCES expression_strategies_v2(id),
    strategy_version INTEGER NOT NULL CHECK(strategy_version > 0),
    replicated_audit_id TEXT NOT NULL REFERENCES promotion_audits(audit_id),
    card_sha256 TEXT NOT NULL CHECK(length(card_sha256) = 64),
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
    scope_claim_sha256 TEXT NOT NULL CHECK(length(scope_claim_sha256) = 64),
    policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256) = 64),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    review_input_sha256 TEXT NOT NULL CHECK(length(review_input_sha256) = 64),
    review_input_json TEXT NOT NULL CHECK(json_valid(review_input_json)),
    created_at TEXT NOT NULL CHECK(length(created_at) > 0)
)""",
    "semantic_review_votes": """
CREATE TABLE IF NOT EXISTS semantic_review_votes (
    vote_id TEXT PRIMARY KEY NOT NULL CHECK(length(vote_id) > 0),
    snapshot_id TEXT NOT NULL REFERENCES semantic_review_snapshots(snapshot_id),
    judge_kind TEXT NOT NULL CHECK(judge_kind = 'semantic_current'),
    provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
    model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
    model_identity TEXT NOT NULL CHECK(length(trim(model_identity)) > 0),
    call_receipt_id TEXT NOT NULL UNIQUE CHECK(length(trim(call_receipt_id)) > 0),
    verdict TEXT NOT NULL CHECK(verdict IN ('PASS', 'BLOCK', 'ABSTAIN')),
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
    response_sha256 TEXT NOT NULL CHECK(length(response_sha256) = 64),
    review_json TEXT NOT NULL CHECK(json_valid(review_json)),
    created_at TEXT NOT NULL CHECK(length(created_at) > 0),
    UNIQUE(snapshot_id, model_identity)
)""",
    "semantic_approval_links": """
CREATE TABLE IF NOT EXISTS semantic_approval_links (
    link_id TEXT PRIMARY KEY NOT NULL CHECK(length(link_id) > 0),
    strategy_id TEXT NOT NULL REFERENCES expression_strategies_v2(id),
    strategy_version INTEGER NOT NULL CHECK(strategy_version > 0),
    verified_audit_id TEXT NOT NULL REFERENCES promotion_audits(audit_id),
    snapshot_id TEXT NOT NULL REFERENCES semantic_review_snapshots(snapshot_id),
    vote_a_id TEXT NOT NULL REFERENCES semantic_review_votes(vote_id),
    vote_b_id TEXT NOT NULL REFERENCES semantic_review_votes(vote_id),
    kind TEXT NOT NULL CHECK(kind IN ('pre_promotion', 'posthoc_release')),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    created_at TEXT NOT NULL CHECK(length(created_at) > 0),
    CHECK(vote_a_id <> vote_b_id),
    UNIQUE(snapshot_id, kind)
)""",
}

TRIGGER_DDL = {}
for _table in TABLE_DDL:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        TRIGGER_DDL[_name] = (
            f"CREATE TRIGGER IF NOT EXISTS {_name} BEFORE {_operation} "
            f"ON {_table} BEGIN SELECT RAISE(ABORT, "
            f"'{_table} is append-only'); END")

TRIGGER_DDL["semantic_approval_links_validate"] = """
CREATE TRIGGER IF NOT EXISTS semantic_approval_links_validate
BEFORE INSERT ON semantic_approval_links
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM semantic_review_snapshots s
        JOIN promotion_audits a ON a.audit_id = NEW.verified_audit_id
        JOIN semantic_review_votes va ON va.vote_id = NEW.vote_a_id
        JOIN semantic_review_votes vb ON vb.vote_id = NEW.vote_b_id
        WHERE s.snapshot_id = NEW.snapshot_id
          AND s.strategy_id = NEW.strategy_id
          AND s.strategy_version = NEW.strategy_version
          AND s.content_sha256 = NEW.content_sha256
          AND a.strategy_id = NEW.strategy_id
          AND a.strategy_version = NEW.strategy_version
          AND a.to_status = 'verified'
          AND va.snapshot_id = s.snapshot_id
          AND vb.snapshot_id = s.snapshot_id
          AND va.verdict = 'PASS' AND vb.verdict = 'PASS'
          AND va.input_sha256 = s.review_input_sha256
          AND vb.input_sha256 = s.review_input_sha256
          AND va.model_identity <> vb.model_identity
          AND (va.provider <> vb.provider OR va.model_id <> vb.model_id)
          AND NOT EXISTS (
              SELECT 1 FROM semantic_review_votes blocker
              WHERE blocker.snapshot_id = s.snapshot_id
                AND blocker.verdict = 'BLOCK'
          )
    ) THEN RAISE(ABORT, 'semantic approval evidence invalid') END;
END"""

TRIGGER_DDL["semantic_review_snapshots_validate"] = """
CREATE TRIGGER IF NOT EXISTS semantic_review_snapshots_validate
BEFORE INSERT ON semantic_review_snapshots
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM promotion_audits a
        WHERE a.audit_id = NEW.replicated_audit_id
          AND a.strategy_id = NEW.strategy_id
          AND a.strategy_version = NEW.strategy_version
          AND a.to_status = 'replicated'
    ) THEN RAISE(ABORT, 'replicated audit does not match snapshot') END;
END"""

TRIGGER_DDL["semantic_review_votes_closed"] = """
CREATE TRIGGER IF NOT EXISTS semantic_review_votes_closed
BEFORE INSERT ON semantic_review_votes
WHEN EXISTS (
    SELECT 1 FROM semantic_approval_links
    WHERE snapshot_id = NEW.snapshot_id
)
BEGIN
    SELECT RAISE(ABORT, 'approved review round is closed');
END"""


def _normalized(sql: str) -> str:
    # sqlite_master strips IF NOT EXISTS from the stored CREATE statement.
    return " ".join(sql.replace(" IF NOT EXISTS", "").split()).lower()


def _require_parents(conn) -> None:
    if conn.engine.url.get_backend_name() != "sqlite":
        raise ReceiptSchemaError("semantic_receipts_require_sqlite")
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
        raise ReceiptSchemaError("foreign_keys_disabled")
    # SQLite permits a child table to reference a nonexistent parent.
    for table, primary_key, required in (
            ("expression_strategies_v2", "id", {"id", "version"}),
            ("promotion_audits", "audit_id",
             {"audit_id", "strategy_id", "strategy_version", "to_status"})):
        info = conn.exec_driver_sql(f"PRAGMA table_info({table})").all()
        columns = {row[1] for row in info}
        if (not required <= columns or
                not any(row[1] == primary_key and row[5] == 1
                        for row in info)):
            raise ReceiptSchemaError(f"parent_schema_missing:{table}")


def require_semantic_schema(conn) -> None:
    """Read-only check of every required receipt table and trigger."""
    _require_parents(conn)
    for kind, definitions in (("table", TABLE_DDL),
                              ("trigger", TRIGGER_DDL)):
        for name, expected in definitions.items():
            stored = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                (kind, name)).scalar()
            if stored is None or _normalized(stored) != _normalized(expected):
                raise ReceiptSchemaError(f"{kind}_schema_drift:{name}")


def ensure_semantic_schema(engine: Engine) -> None:
    """Create only missing receipt objects, then reject any schema drift.

    Existing review and promotion rows are untouched. The caller's SQLite
    connection must enforce foreign keys; the app engine does so on connect.
    """
    with engine.begin() as conn:
        _require_parents(conn)
        for ddl in TABLE_DDL.values():
            conn.exec_driver_sql(ddl)
        for ddl in TRIGGER_DDL.values():
            conn.exec_driver_sql(ddl)
        require_semantic_schema(conn)
