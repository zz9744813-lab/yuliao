"""Receipt DDL acceptance on disposable SQLite databases only."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db
from app.db import Base
from app.models import ExpressionStrategyV2
from app.promotion_audits import (AUDIT_COLUMNS, AUDIT_DDL,
                                  PromotionAuditSchemaError,
                                  ensure_promotion_audit_schema)
from app.semantic_receipts import (TABLE_DDL, TRIGGER_DDL,
                                   ReceiptSchemaError, ensure_semantic_schema)

HASH_A = "a" * 64
HASH_B = "b" * 64
RESPONSE_TEXT = "synthetic response"
HASH_RESPONSE = hashlib.sha256(RESPONSE_TEXT.encode("utf-8")).hexdigest()
NOW = "2026-09-27T12:00:00.000000Z"


def _engine(*, foreign_keys=True):
    engine = create_engine("sqlite://", future=True)
    if foreign_keys:
        @event.listens_for(engine, "connect")
        def _fk(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return engine


def _audit(con, audit_id, to_status):
    values = {name: "seed" for name in AUDIT_COLUMNS}
    values.update(audit_id=audit_id, strategy_id="ESV2-S",
                  strategy_key="seed", strategy_version=1,
                  to_status=to_status, from_status="replicated",
                  evidence_ref='["SI-A"]', evidence_count=1,
                  scope_ids='["WK-A"]', policy_sha256=HASH_A,
                  ts=NOW)
    cols = ",".join(AUDIT_COLUMNS)
    marks = ",".join("?" for _ in AUDIT_COLUMNS)
    con.exec_driver_sql(
        f"INSERT INTO promotion_audits ({cols}) VALUES ({marks})",
        tuple(values[name] for name in AUDIT_COLUMNS))


def _seed():
    engine = _engine()
    with Session(engine) as s:
        s.add(ExpressionStrategyV2(
            id="ESV2-S", strategy_key="seed", version=1,
            abstract_operation="动作后留白", invariants=[],
            effect_hypothesis="x", failure_modes=[], source="seed",
            status="verified", observation_status="replicated",
            effect_status="untested", scope="WORK", scope_ids=["WK-A"],
            scope_basis="seed"))
        s.commit()
    with engine.begin() as con:
        con.exec_driver_sql(AUDIT_DDL)
        _audit(con, "AUD-R", "replicated")
        _audit(con, "AUD-V", "verified")
    return engine


def _snapshot(con):
    con.exec_driver_sql(
        "INSERT INTO semantic_review_snapshots "
        "(snapshot_id,schema_version,algorithm_version,strategy_id,"
        "strategy_version,replicated_audit_id,card_sha256,evidence_sha256,"
        "scope_claim_sha256,policy_sha256,content_sha256,payload_json,"
        "review_input_sha256,review_input_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("SNAP-1", 1, "semantic-evidence/v1", "ESV2-S", 1, "AUD-R",
         HASH_A, HASH_A, HASH_A, HASH_A, HASH_A, json.dumps({}),
         HASH_A, json.dumps({}), NOW))


def _call(con, vote_id, provider, model_id, *, identity=None,
          upstream_request_id=None, snapshot_id="SNAP-1", input_sha=HASH_A):
    identity = identity or f"{provider}/{model_id}"
    con.exec_driver_sql(
        "INSERT INTO semantic_review_calls "
        "(call_id,snapshot_id,channel,provider,model_id,model_identity,"
        "requested_model,upstream_request_id,input_sha256,prompt_sha256,"
        "response_sha256,response_text,request_json,response_json,completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("CALL-" + vote_id, snapshot_id, "openai_http", provider,
         model_id, identity, model_id,
         upstream_request_id or "UP-" + vote_id, input_sha,
         HASH_A, HASH_RESPONSE, RESPONSE_TEXT, "{}", "{}", NOW))


def _vote(con, vote_id, identity, verdict="PASS", *, response_sha=HASH_RESPONSE):
    provider, model_id = identity.split("/", 1)
    _call(con, vote_id, provider, model_id)
    con.exec_driver_sql(
        "INSERT INTO semantic_review_votes "
        "(vote_id,snapshot_id,judge_kind,provider,model_id,model_identity,"
        "call_receipt_id,verdict,input_sha256,response_sha256,review_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (vote_id, "SNAP-1", "semantic_current", provider, model_id,
         identity, "CALL-" + vote_id, verdict, HASH_A, response_sha,
         json.dumps({"reason": "seed"}), NOW))


def _link(con, vote_a="VOTE-A", vote_b="VOTE-B", link_id="LINK-1"):
    con.exec_driver_sql(
        "INSERT INTO semantic_approval_links "
        "(link_id,strategy_id,strategy_version,verified_audit_id,"
        "snapshot_id,vote_a_id,vote_b_id,kind,content_sha256,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (link_id, "ESV2-S", 1, "AUD-V", "SNAP-1", vote_a, vote_b,
         "posthoc_release", HASH_A, NOW))


def test_add_only_receipts_keep_old_audits_and_reinitialize_idempotently():
    engine = _seed()
    try:
        with engine.connect() as con:
            before = con.exec_driver_sql(
                "SELECT * FROM promotion_audits ORDER BY audit_id").all()
        ensure_semantic_schema(engine)
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            assert con.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert con.exec_driver_sql(
                "SELECT * FROM promotion_audits ORDER BY audit_id").all() == before
            _snapshot(con)
            _vote(con, "VOTE-A", "upstream/model-a")
            _vote(con, "VOTE-B", "upstream/model-b")
            _link(con)
        ensure_semantic_schema(engine)
        with engine.connect() as con:
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_approval_links").scalar() == 1
            assert con.exec_driver_sql(
                "SELECT * FROM promotion_audits ORDER BY audit_id").all() == before
    finally:
        engine.dispose()


def test_existing_three_table_schema_upgrades_without_changing_snapshots():
    engine = _seed()
    try:
        with engine.begin() as con:
            for name, ddl in TABLE_DDL.items():
                if name != "semantic_review_calls":
                    con.exec_driver_sql(ddl)
            for name, ddl in TRIGGER_DDL.items():
                if (not name.startswith("semantic_review_calls_") and
                        name != "semantic_review_votes_validate_call"):
                    con.exec_driver_sql(ddl)
            _snapshot(con)
            old_row = con.exec_driver_sql(
                "SELECT * FROM semantic_review_snapshots").one()
        ensure_semantic_schema(engine)
        with engine.connect() as con:
            assert con.exec_driver_sql(
                "SELECT * FROM semantic_review_snapshots").one() == old_row
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_calls").scalar() == 0
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_votes").scalar() == 0
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_approval_links").scalar() == 0
    finally:
        engine.dispose()


def test_upgrade_refuses_legacy_vote_without_call_provenance():
    engine = _seed()
    try:
        with engine.begin() as con:
            for name, ddl in TABLE_DDL.items():
                if name != "semantic_review_calls":
                    con.exec_driver_sql(ddl)
            for name, ddl in TRIGGER_DDL.items():
                if (not name.startswith("semantic_review_calls_") and
                        name != "semantic_review_votes_validate_call"):
                    con.exec_driver_sql(ddl)
            _snapshot(con)
            con.exec_driver_sql(
                "INSERT INTO semantic_review_votes "
                "(vote_id,snapshot_id,judge_kind,provider,model_id,"
                "model_identity,call_receipt_id,verdict,input_sha256,"
                "response_sha256,review_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("VOTE-LEGACY", "SNAP-1", "semantic_current", "upstream",
                 "model-a", "upstream/model-a", "CALL-MISSING", "PASS",
                 HASH_A, HASH_B, "{}", NOW))
        with pytest.raises(ReceiptSchemaError,
                           match="vote_without_matching_call:VOTE-LEGACY"):
            ensure_semantic_schema(engine)
        with engine.connect() as con:
            assert con.exec_driver_sql(
                "SELECT verdict FROM semantic_review_votes "
                "WHERE vote_id='VOTE-LEGACY'").scalar() == "PASS"
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_snapshots").scalar() == 1
    finally:
        engine.dispose()


def test_existing_promotion_audit_rows_survive_reinit_and_weak_trigger_is_rejected():
    engine = _seed()
    try:
        with engine.connect() as con:
            before = con.exec_driver_sql(
                "SELECT * FROM promotion_audits ORDER BY audit_id").all()
        ensure_promotion_audit_schema(engine)
        ensure_promotion_audit_schema(engine)
        with engine.connect() as con:
            assert con.exec_driver_sql(
                "SELECT * FROM promotion_audits ORDER BY audit_id").all() == before
        with engine.begin() as con:
            con.exec_driver_sql("DROP TRIGGER promotion_audits_no_update")
            con.exec_driver_sql(
                "CREATE TRIGGER promotion_audits_no_update "
                "BEFORE UPDATE ON promotion_audits BEGIN SELECT 1; END")
        with pytest.raises(PromotionAuditSchemaError,
                           match="trigger_schema_drift:promotion_audits_no_update"):
            ensure_promotion_audit_schema(engine)
    finally:
        engine.dispose()


def test_receipt_rows_are_immutable_and_foreign_keys_enforced():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
            _vote(con, "VOTE-A", "upstream/model-a")
            _vote(con, "VOTE-B", "upstream/model-b")
            _link(con)
        with engine.connect() as con:
            keys = {"semantic_review_snapshots": ("snapshot_id", "SNAP-1", "created_at"),
                    "semantic_review_calls": ("call_id", "CALL-VOTE-A", "completed_at"),
                    "semantic_review_votes": ("vote_id", "VOTE-A", "created_at"),
                    "semantic_approval_links": ("link_id", "LINK-1", "created_at")}
            for table, (pk, key, stamp) in keys.items():
                with pytest.raises(IntegrityError, match="append-only"):
                    con.exec_driver_sql(
                        f"UPDATE {table} SET {stamp}='later' WHERE {pk}=?",
                        (key,))
                con.rollback()
                with pytest.raises(IntegrityError, match="append-only"):
                    con.exec_driver_sql(
                        f"DELETE FROM {table} WHERE {pk}=?",
                        (key,))
                con.rollback()
            with pytest.raises(IntegrityError,
                               match="semantic vote call receipt invalid"):
                con.exec_driver_sql(
                    "INSERT INTO semantic_review_votes "
                    "(vote_id,snapshot_id,judge_kind,provider,model_id,"
                    "model_identity,call_receipt_id,verdict,input_sha256,"
                    "response_sha256,review_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("ORPHAN", "MISSING", "semantic_current", "p", "m", "m",
                     "CALL-O", "PASS", HASH_A, HASH_B, "{}", NOW))
            con.rollback()
    finally:
        engine.dispose()


def test_approval_requires_two_independent_current_passes_and_no_block():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
            _vote(con, "VOTE-A", "upstream/model-a")
            _vote(con, "VOTE-B", "upstream/model-b", verdict="ABSTAIN")
        with engine.connect() as con:
            with pytest.raises(IntegrityError, match="semantic approval"):
                _link(con)
            con.rollback()
        # A new review round is required after ABSTAIN; the immutable vote
        # cannot be edited into PASS or hidden by deleting it.
        with engine.connect() as con:
            with pytest.raises(IntegrityError, match="append-only"):
                con.exec_driver_sql(
                    "UPDATE semantic_review_votes SET verdict='PASS' "
                    "WHERE vote_id='VOTE-B'")
            con.rollback()
            with pytest.raises(IntegrityError, match="UNIQUE"):
                _vote(con, "VOTE-C", "upstream/model-a")
            con.rollback()
    finally:
        engine.dispose()


def test_same_round_block_vote_prevents_approval_even_with_two_passes():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
            _vote(con, "VOTE-A", "upstream/model-a")
            _vote(con, "VOTE-B", "upstream/model-b")
            _vote(con, "VOTE-C", "upstream/model-c", verdict="BLOCK")
        with engine.connect() as con:
            with pytest.raises(IntegrityError, match="semantic approval"):
                _link(con)
            con.rollback()
    finally:
        engine.dispose()


def test_approved_round_rejects_late_block_and_alias_model_pair():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
            _vote(con, "VOTE-A", "upstream/model-a")
        with engine.connect() as con:
            with pytest.raises(IntegrityError,
                               match="semantic call input or identity invalid"):
                _call(con, "VOTE-ALIAS", "upstream", "model-a",
                      identity="alias-a")
            con.rollback()
        with engine.begin() as con:
            _vote(con, "VOTE-B", "upstream/model-b")
            _link(con)
        with engine.connect() as con:
            with pytest.raises(IntegrityError, match="approved review round is closed"):
                _vote(con, "VOTE-D", "upstream/model-d", verdict="BLOCK")
            con.rollback()
    finally:
        engine.dispose()


def test_vote_requires_matching_immutable_call_receipt():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
        with engine.connect() as con:
            with pytest.raises(IntegrityError,
                               match="semantic vote call receipt invalid"):
                con.exec_driver_sql(
                    "INSERT INTO semantic_review_votes "
                    "(vote_id,snapshot_id,judge_kind,provider,model_id,"
                    "model_identity,call_receipt_id,verdict,input_sha256,"
                    "response_sha256,review_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("VOTE-FAKE", "SNAP-1", "semantic_current", "upstream",
                     "model-a", "upstream/model-a", "CALL-MISSING", "PASS",
                     HASH_A, HASH_B, "{}", NOW))
            con.rollback()
            with pytest.raises(IntegrityError,
                               match="semantic vote call receipt invalid"):
                _vote(con, "VOTE-BAD-HASH", "upstream/model-b",
                      response_sha=HASH_A)
            con.rollback()
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_calls").scalar() == 0
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_votes").scalar() == 0
    finally:
        engine.dispose()


def test_call_rejects_wrong_snapshot_or_frozen_input():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
        with engine.connect() as con:
            assert any(row[2] == "semantic_review_snapshots" for row in
                       con.exec_driver_sql(
                           "PRAGMA foreign_key_list(semantic_review_calls)"))
            with pytest.raises(IntegrityError,
                               match="semantic call input or identity invalid"):
                _call(con, "BAD-SNAPSHOT", "upstream", "model-a",
                      snapshot_id="MISSING")
            con.rollback()
            with pytest.raises(IntegrityError,
                               match="semantic call input or identity invalid"):
                _call(con, "BAD-INPUT", "upstream", "model-a",
                      input_sha=HASH_B)
            con.rollback()
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_calls").scalar() == 0
    finally:
        engine.dispose()


def test_schema_refuses_preexisting_call_with_wrong_input():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
            con.exec_driver_sql("DROP TRIGGER semantic_review_calls_validate")
            _call(con, "LEGACY", "upstream", "model-a", input_sha=HASH_B)
            con.exec_driver_sql(TRIGGER_DDL["semantic_review_calls_validate"])
        with pytest.raises(ReceiptSchemaError,
                           match="call_row_invalid:CALL-LEGACY"):
            ensure_semantic_schema(engine)
        with engine.connect() as con:
            assert con.exec_driver_sql(
                "SELECT input_sha256 FROM semantic_review_calls "
                "WHERE call_id='CALL-LEGACY'").scalar() == HASH_B
    finally:
        engine.dispose()


def test_upstream_request_id_is_unique_within_provider():
    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            _snapshot(con)
            _call(con, "VOTE-A", "provider-a", "model-a",
                  upstream_request_id="REQ-1")
            _call(con, "VOTE-B", "provider-b", "model-b",
                  upstream_request_id="REQ-1")
            assert con.exec_driver_sql(
                "SELECT COUNT(*) FROM semantic_review_calls "
                "WHERE upstream_request_id='REQ-1'").scalar() == 2
        with engine.connect() as con:
            with pytest.raises(IntegrityError, match="UNIQUE"):
                _call(con, "VOTE-C", "provider-a", "model-c",
                      upstream_request_id="REQ-1")
            con.rollback()
    finally:
        engine.dispose()


def test_snapshot_rejects_replicated_audit_from_another_card():
    engine = _seed()
    try:
        with engine.begin() as con:
            con.exec_driver_sql(
                "INSERT INTO expression_strategies_v2 "
                "(id,strategy_key,version,abstract_operation,invariants,"
                "effect_hypothesis,failure_modes,status,source,scope,scope_ids,"
                "scope_basis,observation_status,effect_status,created_at) "
                "VALUES ('ESV2-OTHER','other',1,'x','[]','x','[]',"
                "'verified','seed','WORK','[\"WK-A\"]','seed','replicated',"
                "'untested',?)", (NOW,))
            values = {name: "seed" for name in AUDIT_COLUMNS}
            values.update(audit_id="AUD-OTHER", strategy_id="ESV2-OTHER",
                          strategy_version=1, to_status="replicated",
                          evidence_count=1)
            con.exec_driver_sql(
                f"INSERT INTO promotion_audits ({','.join(AUDIT_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in AUDIT_COLUMNS)})",
                tuple(values[name] for name in AUDIT_COLUMNS))
        ensure_semantic_schema(engine)
        with engine.connect() as con:
            with pytest.raises(IntegrityError, match="replicated audit does not match"):
                con.exec_driver_sql(
                    "INSERT INTO semantic_review_snapshots "
                    "(snapshot_id,schema_version,algorithm_version,strategy_id,"
                    "strategy_version,replicated_audit_id,card_sha256,evidence_sha256,"
                    "scope_claim_sha256,policy_sha256,content_sha256,payload_json,"
                    "review_input_sha256,review_input_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("SNAP-X", 1, "semantic-evidence/v1", "ESV2-S", 1,
                     "AUD-OTHER", HASH_A, HASH_A, HASH_A, HASH_A, HASH_A,
                     "{}", HASH_A, "{}", NOW))
            con.rollback()
    finally:
        engine.dispose()


def test_schema_refuses_fk_disabled_or_existing_shape_drift():
    without_fk = _engine(foreign_keys=False)
    try:
        with pytest.raises(ReceiptSchemaError, match="foreign_keys_disabled"):
            ensure_semantic_schema(without_fk)
    finally:
        without_fk.dispose()

    drifted = _engine()
    try:
        with drifted.begin() as con:
            con.exec_driver_sql(AUDIT_DDL)
            con.exec_driver_sql("CREATE TABLE semantic_review_votes (vote_id TEXT)")
        with pytest.raises(ReceiptSchemaError, match="table_schema_drift"):
            ensure_semantic_schema(drifted)
    finally:
        drifted.dispose()


def test_schema_requires_parent_audit_and_detects_trigger_drift():
    missing = _engine()
    try:
        with pytest.raises(ReceiptSchemaError, match="parent_schema_missing:promotion_audits"):
            ensure_semantic_schema(missing)
    finally:
        missing.dispose()

    engine = _seed()
    try:
        ensure_semantic_schema(engine)
        with engine.begin() as con:
            con.exec_driver_sql("DROP TRIGGER semantic_review_votes_closed")
            con.exec_driver_sql(
                "CREATE TRIGGER semantic_review_votes_closed "
                "BEFORE INSERT ON semantic_review_votes BEGIN SELECT 1; END")
        with pytest.raises(ReceiptSchemaError, match="trigger_schema_drift"):
            ensure_semantic_schema(engine)
    finally:
        engine.dispose()


def test_init_db_creates_empty_receipt_tables_on_fresh_sqlite(monkeypatch):
    engine = _engine()
    try:
        monkeypatch.setattr(db, "engine", engine)
        db.init_db()
        with engine.connect() as con:
            names = {row[0] for row in con.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'semantic_%'")}
            assert names == {"semantic_review_snapshots",
                             "semantic_review_calls",
                             "semantic_review_votes",
                             "semantic_approval_links"}
            for table in names:
                assert con.exec_driver_sql(
                    f"SELECT COUNT(*) FROM {table}").scalar() == 0
    finally:
        engine.dispose()
