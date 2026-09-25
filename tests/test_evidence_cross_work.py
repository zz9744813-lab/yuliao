"""K3 证据跨作品如实报告与同作品证据收窄回归。"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import knowledge_query as kq
from app.db import Base
from app.models import (ExpressionStrategyV2, Segment,
                        StrategyInstance, Work, WorkSource)

TEXT = "灯花轻轻跳了一下，他终于开口。"
SHA256 = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_work(session, work_id, *, canonical=None, source_type="human_fiction",
              license_purposes=None, role="train") -> str:
    session.add(Work(id=work_id, title=work_id, source="test:evidence-cross-work"))
    session.flush()
    segment = Segment(
        work_id=work_id,
        ordinal=0,
        text=TEXT,
        text_clean=TEXT,
        n_sentences=1,
        n_chars=len(TEXT),
        role=role,
    )
    session.add(segment)
    session.flush()
    session.add(WorkSource(
        work_id=work_id,
        canonical_work_id=canonical or work_id,
        source_type=source_type,
        text_version="corpus-v1",
        text_sha256=SHA256,
        purpose_basis="test fixture",
        identity_purposes=["research"],
        license_purposes=license_purposes or [],
        license_basis="test fixture" if license_purposes else None,
        metadata_status="verified",
        metadata_basis="test fixture",
    ))
    session.flush()
    return segment.id


def _add_strategy(session, strategy_id, strategy_key, book_id) -> None:
    session.add(ExpressionStrategyV2(
        id=strategy_id,
        strategy_key=strategy_key,
        version=1,
        abstract_operation="test operation",
        invariants=[],
        effect_hypothesis="test hypothesis",
        failure_modes=[],
        status="verified",
        source="test",
        scope="WORK",
        scope_ids=[book_id],
        scope_basis="test fixture",
        observation_status="observed",
        effect_status="untested",
    ))


def _add_instance(session, instance_id, strategy_id, work_id, segment_id, *,
                  start=0, end=8, text_version="corpus-v1") -> None:
    evidence = TEXT[start:end]
    session.add(StrategyInstance(
        id=instance_id,
        strategy_id=strategy_id,
        strategy_version=1,
        work_id=work_id,
        segment_id=segment_id,
        frame_id=None,
        text_version=text_version,
        span_start=start,
        span_end=end,
        evidence_text=evidence,
        evidence_sha256=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        conditions_observed={},
        observed_content="test observation",
        extractor_model="test",
        reviewer_version="test",
        status="verified",
    ))


def _seed(session) -> dict:
    ids = {}

    ids["same_query"] = "WK-same-query"
    ids["same_root"] = "WK-same-root"
    same_root_segment = _add_work(
        session, ids["same_root"])
    _add_work(
        session, ids["same_query"], canonical=ids["same_root"])
    _add_strategy(
        session, "ESV2-same-root", "cross-same-root", ids["same_query"])
    _add_instance(
        session, "SI-same-root", "ESV2-same-root",
        ids["same_root"], same_root_segment)

    ids["cross_query"] = "WK-cross-query"
    _add_work(session, ids["cross_query"])
    ids["cross_roots"] = ["WK-cross-a", "WK-cross-z"]
    for index, work_id in enumerate(ids["cross_roots"]):
        segment_id = _add_work(session, work_id)
        _add_instance(
            session, f"SI-cross-{index}", "ESV2-cross-roots",
            work_id, segment_id, start=index * 4, end=index * 4 + 8)
    _add_strategy(
        session, "ESV2-cross-roots", "cross-different-roots",
        ids["cross_query"])

    ids["fuhan"] = "WK-dc90993434e9"
    ids["qiongming"] = "WK-6e5d2623"
    qiongming_segment = _add_work(session, ids["qiongming"])
    _add_work(session, ids["fuhan"])
    _add_strategy(session, "ESV2-fuhan-s3", "fuhan-s3", ids["fuhan"])
    _add_instance(
        session, "SI-fuhan-cross", "ESV2-fuhan-s3",
        ids["qiongming"], qiongming_segment)

    ids["missing_query"] = "WK-missing-query"
    session.add(Work(
        id=ids["missing_query"],
        title=ids["missing_query"],
        source="test:evidence-cross-work",
    ))
    session.flush()
    missing_evidence_segment = _add_work(session, "WK-missing-evidence")
    _add_strategy(
        session, "ESV2-missing-registry", "missing-registry",
        ids["missing_query"])
    _add_instance(
        session, "SI-missing-registry", "ESV2-missing-registry",
        "WK-missing-evidence", missing_evidence_segment)
    ids["missing_evidence"] = "WK-missing-evidence"

    gate_specs = {
        "benchmark": {
            "source_type": "human_fiction",
            "role": "benchmark",
        },
        "excluded_source_type": {
            "source_type": "fixture",
            "role": "train",
        },
        "excluded_use": {
            "source_type": "human_fiction",
            "role": "train",
            "license_purposes": ["research"],
        },
        "text_version": {
            "source_type": "human_fiction",
            "role": "train",
            "text_version": "corpus-v9",
        },
        "mirror_dedup": {
            "source_type": "human_fiction",
            "role": "train",
            "mirrored": True,
        },
    }
    ids["gates"] = {}
    for name, spec in gate_specs.items():
        book_id = f"WK-gate-{name}"
        segment_id = _add_work(
            session,
            book_id,
            source_type=spec["source_type"],
            license_purposes=spec.get("license_purposes"),
            role=spec["role"],
        )
        strategy_id = f"ESV2-gate-{name}"
        _add_strategy(session, strategy_id, f"gate-{name}", book_id)
        _add_instance(
            session,
            f"SI-gate-{name}-1",
            strategy_id,
            book_id,
            segment_id,
            text_version=spec.get("text_version", "corpus-v1"),
        )
        case = {
            "book_id": book_id,
            "strategy_id": strategy_id,
            "source_policy": (
                {"excluded_uses": ["research"]}
                if name == "excluded_use" else {}
            ),
        }
        if spec.get("mirrored"):
            mirror_id = f"{book_id}-mirror"
            mirror_segment = _add_work(
                session, mirror_id, canonical=book_id)
            _add_instance(
                session,
                f"SI-gate-{name}-2",
                strategy_id,
                mirror_id,
                mirror_segment,
            )
        ids["gates"][name] = case

    session.commit()
    return ids


@pytest.fixture()
def evidence_store(tmp_path):
    db_path = tmp_path / "evidence-cross-work.db"
    write_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(write_engine)
    with sessionmaker(bind=write_engine)() as session:
        ids = _seed(session)
    write_engine.dispose()
    before = _sha256(db_path)

    db_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    read_engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(db_uri, uri=True),
        future=True,
    )

    @event.listens_for(read_engine, "connect")
    def _set_query_only(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    try:
        with sessionmaker(bind=read_engine)() as session:
            query_only = session.connection().exec_driver_sql(
                "PRAGMA query_only").scalar()
            assert query_only == 1
            yield session, ids
    finally:
        read_engine.dispose()
    assert _sha256(db_path) == before


def _policy(book_id, source_policy=None):
    policy = {
        "contract_version": 2,
        "book_id": book_id,
        "semantic_requirements": {},
    }
    if source_policy is not None:
        policy["source_policy"] = source_policy
    return policy


def _signature(refs, count, stripped):
    return (
        count,
        sorted(refs, key=lambda ref: ref["instance_id"]),
        sorted(stripped),
    )


def test_same_root_evidence_reports_not_cross_work(evidence_store):
    session, ids = evidence_store
    response = kq.query_knowledge(_policy(ids["same_query"]), session)

    assert response["status"] == "matched"
    selected = response["selected"][0]
    assert selected["evidence_cross_work"] is False
    assert selected["evidence_root_works"] == [ids["same_root"]]
    assert "evidence_cross_work_note" not in selected
    assert selected["score_components"]["evidence_count"] == 1


def test_cross_root_reporting_does_not_change_evidence_count(evidence_store):
    session, ids = evidence_store
    policy = _policy(ids["cross_query"])
    refs, count, _stripped = kq._evidence_for(
        session, "ESV2-cross-roots", policy)
    response = kq.query_knowledge(policy, session)

    assert response["status"] == "matched"
    selected = response["selected"][0]
    assert count == 2
    assert selected["score_components"]["evidence_count"] == count
    assert len(selected["evidence"]) == count
    assert selected["evidence_cross_work"] is True
    assert selected["evidence_root_works"] == sorted(ids["cross_roots"])
    assert selected["evidence_root_works"] == sorted(
        ref["canonical_work"] for ref in refs)


def test_fuhan_s3_shape_selected_zero_when_same_book_required(evidence_store):
    session, ids = evidence_store
    default = kq.query_knowledge(_policy(ids["fuhan"]), session)
    explicit_off = kq.query_knowledge(
        _policy(ids["fuhan"], {"require_same_book_evidence": False}),
        session,
    )
    narrowed = kq.query_knowledge(
        _policy(ids["fuhan"], {"require_same_book_evidence": True}),
        session,
    )

    assert len(default["selected"]) == 1
    assert [entry["strategy_id"] for entry in explicit_off["selected"]] == \
        [entry["strategy_id"] for entry in default["selected"]]
    assert default["selected"][0]["evidence_cross_work"] is True
    assert narrowed["status"] == "empty"
    assert narrowed["selected"] == []
    assert {
        "strategy_key": "fuhan-s3",
        "reason": "excluded_no_evidence",
    } in narrowed["rejected"]
    refs, count, stripped = kq._evidence_for(
        session,
        "ESV2-fuhan-s3",
        _policy(ids["fuhan"], {"require_same_book_evidence": True}),
    )
    assert refs == []
    assert count == 0
    assert "SI-fuhan-cross:cross_work" in stripped


@pytest.mark.parametrize("gate_name", [
    "benchmark",
    "excluded_source_type",
    "excluded_use",
    "text_version",
    "mirror_dedup",
])
def test_same_book_switch_does_not_change_five_existing_gates(
        evidence_store, gate_name):
    session, ids = evidence_store
    case = ids["gates"][gate_name]
    without_switch = _policy(case["book_id"], case["source_policy"])
    with_switch = _policy(case["book_id"], {
        **case["source_policy"],
        "require_same_book_evidence": True,
    })

    baseline = kq._evidence_for(
        session, case["strategy_id"], without_switch)
    narrowed = kq._evidence_for(
        session, case["strategy_id"], with_switch)

    assert _signature(*narrowed) == _signature(*baseline)
    reasons = baseline[2]
    expected = {
        "benchmark": "benchmark_source",
        "excluded_source_type": "excluded_source_type:fixture",
        "excluded_use": "excluded_use",
        "text_version": "text_version:corpus-v9",
        "mirror_dedup": "mirror_dedup",
    }[gate_name]
    assert any(reason.endswith(f":{expected}") for reason in reasons)
    assert baseline[1] == (1 if gate_name == "mirror_dedup" else 0)


def test_missing_query_registry_reports_not_determined_with_note(evidence_store):
    session, ids = evidence_store
    response = kq.query_knowledge(_policy(ids["missing_query"]), session)

    assert response["status"] == "matched"
    selected = response["selected"][0]
    assert selected["evidence_cross_work"] is False
    assert selected["evidence_root_works"] == [ids["missing_evidence"]]
    assert "WorkSource" in selected["evidence_cross_work_note"]
    assert "not determined" in selected["evidence_cross_work_note"]


def test_capabilities_describes_fields_and_default_off_switch(evidence_store):
    session, _ids = evidence_store
    provenance = kq.capabilities(session)["evidence_provenance"]

    assert {
        "evidence_cross_work",
        "evidence_root_works",
        "evidence_cross_work_note",
    } <= set(provenance["package_fields"])
    switch = provenance["require_same_book_evidence"]
    assert switch["location"] == "source_policy.require_same_book_evidence"
    assert switch["default"] is False
    assert "canonical" in switch["semantics"]
