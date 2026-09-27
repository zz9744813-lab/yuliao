"""Single SQLite schema source for the existing append-only K5 promotion audit."""
from __future__ import annotations

from sqlalchemy.engine import Engine

AUDIT_TABLE = "promotion_audits"
AUDIT_COLUMNS = (
    "audit_id", "tool", "gate_version", "strategy_id", "strategy_key",
    "strategy_version", "from_status", "to_status", "status_column_from",
    "status_column_to", "observation_from", "observation_to", "scope_from",
    "scope_to", "scope_ids", "scope_basis", "scope_rule_version",
    "evidence_ref", "evidence_count", "reviewer", "ts", "policy_sha256",
    "columns_written")
AUDIT_INTEGER_COLUMNS = frozenset({"strategy_version", "evidence_count"})
AUDIT_DDL = "CREATE TABLE IF NOT EXISTS {} (\n  {}\n)".format(
    AUDIT_TABLE, ",\n  ".join(
        [f"{AUDIT_COLUMNS[0]} TEXT PRIMARY KEY"] + [
            f"{c} {'INTEGER' if c in AUDIT_INTEGER_COLUMNS else 'TEXT'} NOT NULL"
            for c in AUDIT_COLUMNS[1:]]))
AUDIT_IMMUTABLE_DDL = (
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_update BEFORE UPDATE ON"
    f" {AUDIT_TABLE} BEGIN SELECT RAISE(ABORT,"
    f" '{AUDIT_TABLE} is append-only: UPDATE blocked'); END;",
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_delete BEFORE DELETE ON"
    f" {AUDIT_TABLE} BEGIN SELECT RAISE(ABORT,"
    f" '{AUDIT_TABLE} is append-only: DELETE blocked'); END;",
)


class PromotionAuditSchemaError(RuntimeError):
    """Existing audit DDL or immutability trigger has drifted."""


def _normalized(sql: str) -> str:
    # sqlite_master removes IF NOT EXISTS and the final statement semicolon.
    return " ".join(sql.strip().rstrip(";").replace(
        " IF NOT EXISTS", "").split()).lower()


def ensure_promotion_audit_schema(engine: Engine) -> None:
    """Only create a missing audit table/triggers; never rewrite existing rows."""
    if engine.url.get_backend_name() != "sqlite":
        raise PromotionAuditSchemaError("promotion_audits_require_sqlite")
    with engine.begin() as conn:
        conn.exec_driver_sql(AUDIT_DDL)
        for ddl in AUDIT_IMMUTABLE_DDL:
            conn.exec_driver_sql(ddl)
        objects = [("table", AUDIT_TABLE, AUDIT_DDL)]
        objects.extend(("trigger", ddl.split()[5], ddl)
                       for ddl in AUDIT_IMMUTABLE_DDL)
        for kind, name, expected in objects:
            stored = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                (kind, name)).scalar()
            if stored is None or _normalized(stored) != _normalized(expected):
                raise PromotionAuditSchemaError(f"{kind}_schema_drift:{name}")
