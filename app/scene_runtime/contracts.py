"""Frozen, strict scene contracts. The planner's permitted changes are authoritative."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")]
Value = str | int | bool


def canonical(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Fact(Contract):
    value: Value
    visible_to: list[Identifier]
    reader_visible: bool = True
    mutable: bool = True


class World(Contract):
    book_id: Identifier
    branch_id: Identifier = "main"
    revision: int = Field(ge=0)
    characters: dict[Identifier, str]
    facts: dict[Identifier, Fact]
    rules: list[str]


class Change(Contract):
    fact: Identifier
    before: Value
    after: Value


class PlannedEvent(Contract):
    event_id: Identifier
    description: str = Field(min_length=1, max_length=600)
    changes: list[Change] = Field(min_length=1, max_length=15)


class ScenePlan(Contract):
    book_id: Identifier
    branch_id: Identifier = "main"
    scene_id: Identifier
    idempotency_key: Identifier
    expected_revision: int = Field(ge=0)
    pov: Identifier
    goal: str = Field(min_length=1, max_length=1500)
    style: str = Field(min_length=1, max_length=1500)
    events: list[PlannedEvent] = Field(min_length=1, max_length=10)
    min_chars: int = Field(default=200, ge=1, le=10000)
    max_chars: int = Field(default=1400, ge=1, le=10000)

    @model_validator(mode="after")
    def unique_changes(self):
        ids = [e.event_id for e in self.events]
        keys = [c.fact for e in self.events for c in e.changes]
        if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
            raise ValueError("Event IDs and changed fact keys must be unique within a scene")
        if self.max_chars < self.min_chars:
            raise ValueError("max_chars < min_chars")
        return self


class Technique(Contract):
    id: Identifier
    operation: str
    conditions: list[str] = Field(min_length=1)
    exceptions: list[str]
    source_refs: list[str] = Field(min_length=1)
    evidence_status: Literal["hypothesis", "pilot_verified", "quality_supported"]


class KnowledgePackage(Contract):
    schema_version: Literal["scene-knowledge/1"] = "scene-knowledge/1"
    package_id: Identifier
    book_id: Identifier
    source_kind: Literal["curated_hypothesis", "genome_snapshot", "distiller_snapshot", "empty"]
    data_split: Literal["runtime_reference"] = "runtime_reference"
    techniques: list[Technique] = Field(max_length=3)


class Budget(Contract):
    # 语感体检开关：只把"干瘪/AI 味"变成修稿指令与体检数据，
    # 不参与 hard 判定（见 app/style_contract.py 的失败留档）。
    style_feedback: bool = False
    max_calls: int = Field(default=6, ge=2, le=20)
    max_rewrites: int = Field(default=2, ge=0, le=2)
    max_input_chars: int = Field(default=24000, ge=100, le=100000)
    max_output_tokens: int = Field(default=3000, ge=100, le=8000)
    max_elapsed_seconds: int = Field(default=600, ge=1, le=3600)


class Draft(Contract):
    text: str = Field(min_length=1, max_length=30000)


class Evidence(Contract):
    fact: Identifier
    after: Value
    quote: str = Field(min_length=1)


class EventEvidence(Contract):
    event_id: Identifier
    quote: str = Field(min_length=1)


class Issue(Contract):
    kind: Literal["hard", "style"]
    description: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Review(Contract):
    issues: list[Issue]
    changes: list[Evidence]
    events: list[EventEvidence]


class RuntimeFault(RuntimeError):
    """Safe, stable error code. Never contains upstream response bodies or secrets."""


def validate_plan(plan: ScenePlan, world: World, knowledge: KnowledgePackage) -> None:
    if (plan.book_id, plan.branch_id, plan.expected_revision) != (world.book_id, world.branch_id, world.revision):
        raise RuntimeFault("world_revision_conflict")
    if knowledge.book_id != plan.book_id:
        raise RuntimeFault("knowledge_scope_conflict")
    if plan.pov not in world.characters:
        raise RuntimeFault("unknown_pov")
    for event in plan.events:
        for change in event.changes:
            fact = world.facts.get(change.fact)
            if fact is None or not fact.mutable:
                raise RuntimeFault("unknown_or_immutable_fact")
            if canonical(fact.value) != canonical(change.before):
                raise RuntimeFault("plan_precondition_conflict")
            if type(change.after) is not type(fact.value):
                raise RuntimeFault("fact_type_change_not_supported")
            if canonical(change.before) == canonical(change.after):
                raise RuntimeFault("empty_state_change")
            if plan.pov not in fact.visible_to:
                raise RuntimeFault("planned_fact_outside_pov")


def validate_review(plan: ScenePlan, text: str, review: Review) -> list[str]:
    """Mechanical checks supplement, never replace, semantic review."""
    errors = []
    if not plan.min_chars <= len(text) <= plan.max_chars:
        errors.append("text_length_outside_plan")
    if len({e.event_id for e in review.events}) != len(review.events):
        errors.append("duplicate_event_evidence")
    if {e.event_id for e in review.events} != {e.event_id for e in plan.events}:
        errors.append("missing_or_unplanned_event")
    expected = {c.fact: c.after for e in plan.events for c in e.changes}
    actual = {c.fact: c.after for c in review.changes}
    if len(actual) != len(review.changes) or canonical(actual) != canonical(expected):
        errors.append("state_patch_not_authorized_by_plan")
    for evidence in [*review.events, *review.changes, *review.issues]:
        if evidence.quote not in text:
            errors.append("evidence_not_in_text")
    if any(i.kind == "hard" for i in review.issues):
        errors.append("unresolved_hard_issue")
    return sorted(set(errors))
