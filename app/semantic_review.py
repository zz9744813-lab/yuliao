"""K2 current-evidence snapshot for one strategy, without database writes.

This is the content half of semantic-evidence/v1. A digest is not an approval:
votes, append-only receipts, and Writer admission are separate gates.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import knowledge as K
from . import knowledge_query as KQ
from .models import (ExpressionStrategyV2, Segment, StrategyCondition,
                     StrategyInstance, WorkSource)

ALGORITHM_VERSION = "semantic-evidence/v1"
SCHEMA_VERSION = 1


class SnapshotError(ValueError):
    """Current evidence cannot be frozen into a reviewable snapshot."""


def _digest(value) -> str:
    return hashlib.sha256(KQ.canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _integrity_value(value: str | None):
    try:
        return json.loads(value) if value is not None else None
    except (TypeError, ValueError):
        return value


def _scope_claim(value: dict, rule_version: str) -> dict:
    required = {"scope_to", "scope_ids", "scope_basis"}
    if not isinstance(value, dict) or not required <= set(value):
        raise SnapshotError("invalid_scope_claim")
    ids = value["scope_ids"]
    if (not isinstance(ids, list) or
            any(not isinstance(i, str) or not i.strip() for i in ids) or
            not isinstance(value["scope_to"], str) or
            not isinstance(value["scope_basis"], str)):
        raise SnapshotError("invalid_scope_claim")
    if (value["scope_to"] not in {"WORK", "AUTHOR", "GENRE"} or
            not ids or not value["scope_basis"].strip()):
        raise SnapshotError("unreviewable_scope_claim")
    return {"scope_to": value["scope_to"], "scope_ids": sorted(set(ids)),
            "scope_basis": value["scope_basis"],
            "scope_rule_version": rule_version}


def build_snapshot(s, strategy_id: str, strategy_version: int,
                   scope_claim: dict) -> dict:
    """Compute a deterministic one-card digest and frozen review input.

    The caller supplies a session in one consistent read transaction. The
    source policy is fixed here: caller policy cannot widen K3 source gates.
    This function neither inserts receipts nor treats the digest as approval.
    """
    # Import locally so the K5 writer can later call this module without an
    # import cycle. Reuse its strict src_ok and reviewed-evidence predicates.
    from scripts import k5_promotion_write as KP

    card = s.get(ExpressionStrategyV2, strategy_id)
    if card is None or card.version != strategy_version:
        raise SnapshotError("strategy_version_not_found")
    if card.observation_status != "replicated":
        raise SnapshotError("observation_not_replicated")

    try:
        anchor = s.execute(text(
            "SELECT audit_id, ts, evidence_ref FROM promotion_audits "
            "WHERE strategy_id=:sid AND strategy_version=:version "
            "AND to_status='replicated' "
            "ORDER BY ts DESC, audit_id DESC LIMIT 1"),
            {"sid": strategy_id, "version": strategy_version}).mappings().first()
    except SQLAlchemyError as exc:
        raise SnapshotError("replicated_audit_unavailable") from exc
    if anchor is None:
        raise SnapshotError("replicated_audit_missing")
    try:
        anchor_refs = json.loads(anchor["evidence_ref"])
        audit_ts = datetime.fromisoformat(anchor["ts"])
    except (TypeError, ValueError) as exc:
        raise SnapshotError("replicated_audit_invalid") from exc
    if (not isinstance(anchor_refs, list) or not anchor_refs or
            any(not isinstance(i, str) or not i for i in anchor_refs) or
            len(anchor_refs) != len(set(anchor_refs)) or
            audit_ts.tzinfo is None):
        raise SnapshotError("replicated_audit_invalid")

    claim = _scope_claim(scope_claim, KP.SCOPE_RULE_VERSION)
    card_payload = {
        "id": card.id, "strategy_key": card.strategy_key,
        "version": card.version, "abstract_operation": card.abstract_operation,
        "invariants": card.invariants, "effect_hypothesis": card.effect_hypothesis,
        "failure_modes": card.failure_modes, "source": card.source,
        "legacy_strategy_id": card.legacy_strategy_id,
    }
    all_instances = (s.query(StrategyInstance)
                     .filter_by(strategy_id=strategy_id)
                     .order_by(StrategyInstance.id).all())
    mismatched = [i.id for i in all_instances
                  if i.strategy_version != strategy_version]
    if mismatched:
        raise SnapshotError("strategy_version_mismatch:" + ",".join(mismatched))
    if not set(anchor_refs) <= {i.id for i in all_instances}:
        raise SnapshotError("replicated_audit_evidence_missing")
    seg_ids = sorted({i.segment_id for i in all_instances})
    work_ids = sorted({i.work_id for i in all_instances})
    segments = {x.id: x for x in (s.query(Segment)
                .filter(Segment.id.in_(seg_ids)).all() if seg_ids else [])}
    sources = {x.work_id: x for x in (s.query(WorkSource)
               .filter(WorkSource.work_id.in_(work_ids)).all()
               if work_ids else [])}
    if len(segments) != len(seg_ids):
        raise SnapshotError("segment_missing")
    if len(sources) != len(work_ids):
        raise SnapshotError("source_registry_missing")

    instance_rows = []
    review_instances = []
    for i in all_instances:
        seg = segments[i.segment_id]
        if seg.work_id != i.work_id:
            raise SnapshotError("segment_work_mismatch:" + i.id)
        instance_rows.append({
            "id": i.id, "strategy_version": i.strategy_version,
            "status": i.status, "reviewer_version": i.reviewer_version,
            "work_id": i.work_id, "segment_id": i.segment_id,
            "frame_id": i.frame_id, "text_version": i.text_version,
            "span_start": i.span_start, "span_end": i.span_end,
            "evidence_sha256": i.evidence_sha256,
            "evidence_text_sha256": _text_digest(i.evidence_text),
            "observed_content_sha256": _text_digest(i.observed_content),
            "conditions_observed": i.conditions_observed,
            "effect_ref_sha256": _text_digest(i.effect_ref),
            "extractor_model": i.extractor_model,
        })
        review_instances.append({
            "instance_id": i.id, "status": i.status,
            "reviewer_version": i.reviewer_version,
            "work_id": i.work_id, "segment_id": i.segment_id,
            "text_version": i.text_version,
            "span": [i.span_start, i.span_end],
            "evidence_text": i.evidence_text,
            "evidence_sha256": i.evidence_sha256,
            "observed_content": i.observed_content,
            "conditions_observed": i.conditions_observed,
            "effect_ref": i.effect_ref,
            "segment_text": seg.text_clean if seg.text_clean is not None else seg.text,
        })

    segment_rows = []
    for seg_id in seg_ids:
        seg = segments[seg_id]
        segment_rows.append({
            "id": seg.id, "work_id": seg.work_id, "role": seg.role,
            "seg_version": seg.seg_version,
            "text_sha256": _text_digest(seg.text),
            "effective_text_sha256": _text_digest(
                seg.text_clean if seg.text_clean is not None else seg.text),
            "integrity_sha256": _digest(_integrity_value(seg.integrity)),
            "src_ok": KP._src_ok_true(seg.integrity),
        })
    source_rows = []
    for work_id in work_ids:
        src = sources[work_id]
        source_rows.append({
            "id": src.id, "work_id": src.work_id,
            "canonical_work_id": src.canonical_work_id,
            "author_id": src.author_id, "genre_ids": src.genre_ids,
            "source_type": src.source_type,
            "text_version": src.text_version,
            "text_sha256": src.text_sha256,
            "purpose_basis": src.purpose_basis,
            "identity_purposes": src.identity_purposes,
            "license_purposes": src.license_purposes,
            "license_basis": src.license_basis,
            "metadata_status": src.metadata_status,
            "metadata_basis": src.metadata_basis,
        })
    conditions = (s.query(StrategyCondition)
                  .filter_by(strategy_id=strategy_id,
                             strategy_version=strategy_version)
                  .order_by(StrategyCondition.id).all())
    condition_rows = [{
        "id": c.id, "strategy_version": c.strategy_version,
        "kind": c.kind, "dimension": c.dimension,
        "operator": c.operator, "value": c.value,
        "required": c.required, "predicate_state": c.predicate_state,
        "evidence_refs": c.evidence_refs, "version": c.version,
    } for c in conditions]

    source_policy = {}
    query_policy = {"source_policy": source_policy}
    refs, _, stripped = KQ._evidence_for(s, strategy_id, query_policy)
    facts = KP.evidence_facts(s, strategy_id, query_policy)
    by_id = {i.id: i for i in all_instances}
    for instance_id in facts["reviewed_ids"]:
        ins = by_id[instance_id]
        seg = segments[ins.segment_id]
        effective = seg.text_clean if seg.text_clean is not None else seg.text
        if (not K.verify_instance_span(effective, ins.span_start,
                                       ins.span_end, ins.evidence_text) or
                K.evidence_sha256(ins.evidence_text) != ins.evidence_sha256):
            raise SnapshotError("positive_span_or_hash_mismatch:" + instance_id)

    policy = {
        "source_policy": source_policy,
        "excluded_source_types": sorted(KQ.DEFAULT_EXCLUDED_SOURCE_TYPES),
        "excluded_uses": sorted(KQ.DEFAULT_EXCLUDED_USES),
        "allowed_text_versions": sorted(KQ.DEFAULT_ALLOWED_TEXT_VERSIONS),
        "eligible_instance_status": sorted(KQ.ELIGIBLE_INSTANCE_STATUS),
        "review_marker": KP.KE.REVIEW_MARKER_NEW_DEF,
    }
    evidence = {
        "instances": instance_rows, "segments": segment_rows,
        "sources": source_rows, "conditions": condition_rows,
        "admitted_refs": sorted(refs, key=lambda r: r["instance_id"]),
        "stripped": sorted(stripped),
        "facts": {**facts,
                  "instance_ids": sorted(facts["instance_ids"]),
                  "src_ok_ids": sorted(facts["src_ok_ids"]),
                  "reviewed_ids": sorted(facts["reviewed_ids"])},
        "replicated_audit": {
            "audit_id": anchor["audit_id"],
            "ts": anchor["ts"],
            "evidence_ref": sorted(anchor_refs),
        },
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "strategy": card_payload, "scope_claim": claim,
        "policy": policy, "evidence": evidence,
    }
    review_input = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "strategy": card_payload, "scope_claim": claim,
        "instances": review_instances, "sources": source_rows,
        "segments": segment_rows, "conditions": condition_rows,
        "admitted_refs": evidence["admitted_refs"],
        "stripped": evidence["stripped"],
        "facts": evidence["facts"],
        "replicated_audit_id": anchor["audit_id"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "replicated_audit_id": anchor["audit_id"],
        "card_sha256": _digest(card_payload),
        "evidence_sha256": _digest(evidence),
        "scope_claim_sha256": _digest(claim),
        "policy_sha256": _digest(policy),
        "content_sha256": _digest(payload),
        "payload": payload,
        "review_input_sha256": _digest(review_input),
        "review_input": review_input,
    }
