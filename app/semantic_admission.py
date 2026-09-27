"""Read-only admission of current K2 approvals for a K3 selected set.

The caller holds a SQLite writer reservation when this result authorizes a
freeze. A CLI preflight may use it without a reservation as a preview only.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from .knowledge_query import canonical_json
from .models import ExpressionStrategyV2
from .promotion_audits import require_promotion_audit_schema
from .semantic_approval import (ApprovalError, _utc,
                                admission_current_approval)
from .semantic_receipts import require_semantic_schema


def approved_selected(session: Session, selected: list[dict]) -> list[dict]:
    """Return a manifest entry for *every* selected card or refuse all."""
    if session.new or session.dirty or session.deleted:
        raise ApprovalError("admission_requires_clean_session")
    require_promotion_audit_schema(session.connection())
    require_semantic_schema(session.connection())
    if not selected or not isinstance(selected, list):
        raise ApprovalError("selected_missing")
    entries = []
    seen = set()
    for item in selected:
        try:
            strategy_id, version = item["strategy_id"], item["version"]
            if (not isinstance(strategy_id, str) or not strategy_id or
                    type(version) is not int or version < 1 or
                    strategy_id in seen):
                raise ValueError("selected identity")
            seen.add(strategy_id)
            card = session.get(ExpressionStrategyV2, strategy_id)
            if (card is None or card.version != version or
                    card.status != "verified" or
                    card.observation_status != "replicated" or
                    any(item[key] != getattr(card, key) for key in (
                        "strategy_key", "status", "scope", "observation_status",
                        "effect_status", "abstract_operation", "invariants",
                        "failure_modes", "effect_hypothesis"))):
                raise ValueError("selected card changed")
            audits = session.execute(text("""
                SELECT * FROM promotion_audits
                WHERE strategy_id=:sid AND strategy_version=:version
                  AND to_status='verified'
            """), {"sid": strategy_id, "version": version}).mappings().all()
            if not audits:
                raise ApprovalError("verified_audit_missing:" + strategy_id)
            audit = dict(max(audits, key=lambda a: (_utc(a["ts"]),
                                                    a["audit_id"])))
            if (audit["strategy_key"] != card.strategy_key or
                    audit["status_column_to"] != "verified" or
                    audit["observation_to"] != "replicated" or
                    audit["scope_to"] != card.scope or
                    canonical_json(json.loads(audit["scope_ids"])) !=
                    canonical_json(card.scope_ids or []) or
                    audit["scope_basis"] != card.scope_basis or
                    not audit["scope_rule_version"]):
                raise ValueError("verified audit differs from card")
            claim = {"scope_to": card.scope, "scope_ids": card.scope_ids,
                     "scope_basis": card.scope_basis}
            approval = admission_current_approval(
                session, strategy_id, version, claim,
                audit["scope_rule_version"])
            links = session.execute(text("""
                SELECT * FROM semantic_approval_links
                WHERE strategy_id=:sid AND strategy_version=:version
                  AND verified_audit_id=:aid AND snapshot_id=:snapshot
            """), {"sid": strategy_id, "version": version,
                   "aid": audit["audit_id"],
                   "snapshot": approval["snapshot_id"]}).mappings().all()
            if len(links) != 1:
                raise ApprovalError("approval_link_missing_or_ambiguous:" +
                                    strategy_id)
            link = dict(links[0])
            if (link["kind"] not in {"pre_promotion", "posthoc_release"} or
                    link["content_sha256"] != approval["content_sha256"] or
                    {link["vote_a_id"], link["vote_b_id"]} !=
                    {approval["vote_a_id"], approval["vote_b_id"]}):
                raise ValueError("approval link differs from current votes")
            snapshot_time = session.execute(text("""
                SELECT created_at FROM semantic_review_snapshots
                WHERE snapshot_id=:sid
            """), {"sid": approval["snapshot_id"]}).scalar_one()
            vote_times = session.execute(text("""
                SELECT created_at FROM semantic_review_votes
                WHERE vote_id IN (:a, :b)
            """), {"a": approval["vote_a_id"],
                   "b": approval["vote_b_id"]}).scalars().all()
            if len(vote_times) != 2:
                raise ValueError("approval vote disappeared")
            # Existing K5 audit/link timestamps have whole-second precision.
            # Allow only that one-second truncation, not an older approval.
            slop = timedelta(seconds=1)
            if (_utc(link["created_at"]) + slop < _utc(snapshot_time) or
                    any(_utc(link["created_at"]) + slop < _utc(ts)
                        for ts in vote_times) or
                    _utc(link["created_at"]) + slop < _utc(audit["ts"]) or
                    (link["kind"] == "pre_promotion" and
                     (_utc(audit["ts"]) + slop < _utc(snapshot_time) or
                      abs((_utc(link["created_at"]) -
                           _utc(audit["ts"])).total_seconds()) > 1)) or
                    (link["kind"] == "posthoc_release" and
                     _utc(audit["ts"]) >= _utc(snapshot_time))):
                raise ValueError("approval chronology invalid")
            entries.append({"strategy_id": strategy_id,
                            "strategy_version": version,
                            "snapshot_id": approval["snapshot_id"],
                            "link_id": link["link_id"],
                            "vote_a_id": approval["vote_a_id"],
                            "vote_b_id": approval["vote_b_id"],
                            "verified_audit_id": audit["audit_id"],
                            "kind": link["kind"],
                            "content_sha256": approval["content_sha256"]})
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ApprovalError):
                raise
            raise ApprovalError("selected_approval_invalid:" +
                                str(item.get("strategy_id", "unknown")
                                    if isinstance(item, dict) else "unknown")) from exc
    return sorted(entries, key=lambda e: e["strategy_id"])
