"""Validate one proposed scene sequence without database or model access.

Passing proves only that the scene contracts can advance one offline world.
It does not compare generated A/B arms or authorize a K4/K5 live run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import (Budget, KnowledgePackage, RuntimeFault, ScenePlan,
                        World, digest, validate_plan)

MAX_BUNDLE_BYTES = 1024 * 1024
SAFE_PLAN_FAULTS = frozenset({
    "world_revision_conflict", "knowledge_scope_conflict", "unknown_pov",
    "unknown_or_immutable_fact", "plan_precondition_conflict",
    "fact_type_change_not_supported", "empty_state_change",
    "planned_fact_outside_pov",
})


class SceneBundleError(ValueError):
    """Stable rejection code; never includes story text."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Source(_Strict):
    title: str = Field(min_length=1)
    production_pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_contract: int = Field(ge=1)
    scope: str = Field(min_length=1)


class _BudgetDraft(_Strict):
    per_scene_per_arm_max_calls: int = Field(ge=2, le=20)
    per_scene_per_arm_max_output_tokens_per_call: int = Field(ge=100, le=8000)
    per_scene_per_arm_max_elapsed_seconds: int = Field(ge=1, le=3600)
    route_and_price_verified: bool
    monetary_cap_approved: bool


class _Bundle(_Strict):
    status: Literal["offline_candidate_not_canon_approved"]
    source: _Source
    unresolved_canon: list[str] = Field(min_length=1)
    world: World
    plans: list[ScenePlan] = Field(min_length=1, max_length=10)
    runtime_budget_draft: _BudgetDraft


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _replay(initial: World, plans: list[ScenePlan]) -> str:
    world = World.model_validate(initial.model_dump(mode="json"))
    empty = KnowledgePackage(package_id="offline-empty",
                             book_id=world.book_id, source_kind="empty",
                             techniques=[])
    for plan in plans:
        validate_plan(plan, world, empty)
        state = world.model_dump(mode="json")
        for event in plan.events:
            for change in event.changes:
                state["facts"][change.fact]["value"] = change.after
        state["revision"] += 1
        world = World.model_validate(state)
    return digest(world)


def check_offline_scene_bundle(path: str | Path, *,
                               expected_pack_sha256: str | None = None) -> dict:
    """Return structural evidence; `live_ready` is always false.

    An expected pack hash should come from an independent pack validation.
    Matching only compares two declarations; this function never opens that
    pack or proves where the expected value came from.
    """
    if expected_pack_sha256 is not None and (
            not isinstance(expected_pack_sha256, str) or
            len(expected_pack_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in expected_pack_sha256)):
        raise SceneBundleError("expected_pack_digest_invalid")
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
    except (OSError, TypeError, ValueError) as exc:
        raise SceneBundleError("bundle_unreadable") from exc
    if not raw or len(raw) > MAX_BUNDLE_BYTES:
        raise SceneBundleError("bundle_size_invalid")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise SceneBundleError("bundle_json_invalid") from exc
    try:
        bundle = _Bundle.model_validate(data)
        if any(not item.strip() for item in bundle.unresolved_canon):
            raise SceneBundleError("bundle_unresolved_canon_invalid")
        if expected_pack_sha256 is not None and (
                bundle.source.production_pack_sha256 != expected_pack_sha256):
            raise SceneBundleError("source_pack_digest_mismatch")
        ids = [plan.scene_id for plan in bundle.plans]
        idempotency = [plan.idempotency_key for plan in bundle.plans]
        if len(ids) != len(set(ids)) or len(idempotency) != len(set(idempotency)):
            raise SceneBundleError("bundle_scene_identity_duplicate")
        Budget(max_calls=bundle.runtime_budget_draft.per_scene_per_arm_max_calls,
               max_output_tokens=(bundle.runtime_budget_draft
                                  .per_scene_per_arm_max_output_tokens_per_call),
               max_elapsed_seconds=(bundle.runtime_budget_draft
                                    .per_scene_per_arm_max_elapsed_seconds))
        final_world = _replay(bundle.world, bundle.plans)
    except SceneBundleError:
        raise
    except ValidationError:
        raise SceneBundleError("bundle_schema_invalid") from None
    except RuntimeFault as exc:
        code = str(exc)
        raise SceneBundleError("bundle_plan_invalid:" +
                               (code if code in SAFE_PLAN_FAULTS else "unknown")) from None
    # Keep SceneBundleError above ValueError; suppress raw input in tracebacks.
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SceneBundleError("bundle_replay_invalid") from None
    return {
        "structure_pass": True,
        "live_ready": False,
        "offline_replay_count": 1,
        "paired_generated_arms_checked": False,
        "book_id": bundle.world.book_id,
        "branch_id": bundle.world.branch_id,
        "scene_ids": ids,
        "scene_count": len(ids),
        "initial_world_sha256": digest(bundle.world),
        "plans_sha256": digest([plan.model_dump(mode="json")
                                for plan in bundle.plans]),
        "final_world_sha256": final_world,
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "source_pack_sha256_declared": bundle.source.production_pack_sha256,
        "declared_pack_digest_matched_expected": expected_pack_sha256 is not None,
        "source_pack_file_checked": False,
        "db_registration_checked": False,
        "route_and_price_verified_declared": bundle.runtime_budget_draft.route_and_price_verified,
        "monetary_cap_approved_declared": bundle.runtime_budget_draft.monetary_cap_approved,
        "model_calls": 0,
        "reason": "canon_rights_budget_k2_and_live_entry_not_approved",
    }
