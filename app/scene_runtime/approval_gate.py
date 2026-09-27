"""Recheck a frozen K3 package and its K2 manifest before real Writer use."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import knowledge_query as kq
from ..knowledge import PACKAGE_CONTRACT_VERSION
from ..models import KnowledgePackage as StoredPackage
from ..semantic_admission import approved_selected
from ..semantic_approval import ApprovalError, _utc
from .contracts import KnowledgePackage, ScenePlan
from .knowledge_v2 import _techniques_from_selected, scene_policy


def verify_frozen_package(session: Session, plan: ScenePlan,
                          knowledge: KnowledgePackage) -> None:
    """Check recorded selection and current approvals, without re-querying K3."""
    manifest = knowledge.approval_manifest
    if manifest is None:
        raise ApprovalError("approval_manifest_missing")
    if (knowledge.book_id != plan.book_id or
            knowledge.schema_version != "scene-knowledge/2"):
        raise ApprovalError("frozen_scope_invalid")
    row = session.query(StoredPackage).filter_by(
        package_sha256=manifest.package_sha256).one_or_none()
    if row is None:
        raise ApprovalError("frozen_package_missing")
    policy = row.policy
    limits = policy.get("limits") if isinstance(policy, dict) else None
    if (not isinstance(limits, dict) or
            not {"context_items"} <= set(limits) <=
            {"context_items", "candidate_cap", "max_context_chars"} or
            type(limits["context_items"]) is not int or
            not 0 <= limits["context_items"] <= 3 or
            ("candidate_cap" in limits and
             (type(limits["candidate_cap"]) is not int or
              not 1 <= limits["candidate_cap"] <= kq.CANDIDATE_CAP_MAX)) or
            ("max_context_chars" in limits and
             (type(limits["max_context_chars"]) is not int or
              not 1 <= limits["max_context_chars"] <= 1200)) or
            {**policy, "limits": {"context_items": limits["context_items"]}} !=
            scene_policy(plan, context_items=limits["context_items"]) or
            row.contract_version != PACKAGE_CONTRACT_VERSION or
            row.policy_sha256 != kq.policy_sha256(policy)):
        raise ApprovalError("frozen_policy_invalid")
    selected = row.selected
    if not isinstance(selected, list) or not selected:
        raise ApprovalError("frozen_selection_missing")
    try:
        selected_sha = hashlib.sha256(kq.canonical_json(selected).encode(
            "utf-8")).hexdigest()
        package_sha = hashlib.sha256(kq.canonical_json({
            "policy": policy,
            "selected_ids": [item["strategy_id"] for item in selected],
            "snapshot": row.snapshot_fingerprint,
        }).encode("utf-8")).hexdigest()
        expected_techniques = _techniques_from_selected(selected)
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalError("frozen_selection_invalid") from exc
    if (manifest.package_sha256 != package_sha or
            manifest.selected_sha256 != selected_sha or
            row.snapshot_fingerprint != kq.fingerprint_knowledge(session) or
            knowledge.package_id != "kq-" + package_sha[:20] or
            [t.model_dump() for t in knowledge.techniques] !=
            [t.model_dump() for t in expected_techniques]):
        raise ApprovalError("frozen_package_changed")
    if (_utc(manifest.verified_at) >
            datetime.now(timezone.utc) + timedelta(seconds=1)):
        raise ApprovalError("manifest_time_invalid")
    current = approved_selected(session, selected)
    if [entry.model_dump() for entry in manifest.entries] != current:
        raise ApprovalError("approval_manifest_stale")
