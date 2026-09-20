"""Failure-path contracts for the scene pilot; these fixtures are not prose evidence."""
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from app.scene_runtime.client import GatewayClient, OutcomeUnknown
from app.scene_runtime.contracts import (Budget, Change, Fact, KnowledgePackage, PlannedEvent,
                                        RuntimeFault, ScenePlan, World, canonical)
from app.scene_runtime.pipeline import SceneRunner
from app.scene_runtime.store import Store


class FixtureClient:
    models = {"writer": "test-writer", "verifier": "test-verifier", "transport": "fixture"}

    def __init__(self, *, hard=False, forged=False, unknown=False, fake_quote=False):
        self.n = 0
        self.hard, self.forged, self.unknown, self.fake_quote = hard, forged, unknown, fake_quote

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        self.n += 1
        if self.unknown:
            raise OutcomeUnknown("injected_unknown")
        if role == "writer":
            text = "林穗把一枚钱放在桌上。她还剩两枚。沈砚收起这一枚钱。"
            result = {"text": text}
        else:
            plan = payload["plan"]
            quote = "不存在的原文" if self.fake_quote else payload["text"]
            result = {"issues": [{"kind": "hard", "description": "改设定才可通过", "quote": quote}] if self.hard else [],
                      "events": [{"event_id": e["event_id"], "quote": quote} for e in plan["events"]],
                      "changes": [{"fact": c["fact"], "after": 999 if self.forged else c["after"], "quote": quote}
                                  for e in plan["events"] for c in e["changes"]]}
        return {"text": canonical(result), "requested_model": self.models[role], "actual_model": "fixture",
                "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "runtime.sqlite")
    world = World(book_id="book-a", revision=0, characters={"lin": "林穗", "shen": "沈砚"},
                  facts={"coins": Fact(value=3, visible_to=["lin"]),
                         "received": Fact(value=0, visible_to=["lin", "shen"]),
                         "secret": Fact(value="HIDDEN-FUTURE-ANSWER", visible_to=["shen"], reader_visible=False),
                         "rule": Fact(value="immutable", visible_to=["lin"], mutable=False)}, rules=["No magic"])
    store.create_world(world)
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1", expected_revision=0,
                     pov="lin", goal="支付一枚钱", style="简洁", min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱", changes=[
                         Change(fact="coins", before=3, after=2), Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a", source_kind="empty", techniques=[])
    return store, world, plan, knowledge


def test_real_atomic_path_reuse_and_pov_filter(setup):
    store, world, plan, knowledge = setup
    client = FixtureClient()
    runner = SceneRunner(store, client)
    result = runner.run(plan, knowledge, Budget())
    assert result["status"] == "committed" and result["revision"] == 1
    assert client.n == 2
    assert "HIDDEN-FUTURE-ANSWER" not in store.job(result["job_id"])["context"]
    assert store.snapshot("book-a").facts["coins"].value == 2
    assert store.audit("book-a")["pending_projections"] == 1
    assert runner.run(plan, knowledge, Budget())["reused"] is True
    assert client.n == 2 and store.audit("book-a")["commits"] == 1
    assert store.project() == 1 and store.project() == 0
    assert store.audit("book-a") == {"book_id": "book-a", "branch_id": "main", "revision": 1,
                                     "commits": 1, "pending_projections": 0, "errors": [], "ok": True}


@pytest.mark.parametrize("point", ["after_scene_insert", "after_world_update"])
def test_transaction_failure_rolls_back_everything_then_recovers(setup, point):
    store, _, plan, knowledge = setup
    client = FixtureClient()
    result = SceneRunner(store, client).run(plan, knowledge, Budget(), stop_after_verified=True)

    def fail(stage):
        if stage == point:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected"):
        store.commit(result["job_id"], fault=fail)
    assert store.snapshot("book-a").revision == 0
    assert store.snapshot("book-a").facts["coins"].value == 3
    assert store.audit("book-a")["commits"] == 0
    assert store.audit("book-a")["pending_projections"] == 0
    # New process equivalent: reopen database and reuse durable verified checkpoint.
    reopened = Store(store.path)
    assert SceneRunner(reopened, client).run(plan, knowledge, Budget())["revision"] == 1
    assert client.n == 2


def test_outbox_failure_does_not_undo_canon_or_half_write_projection(setup):
    store, _, plan, knowledge = setup
    SceneRunner(store, FixtureClient()).run(plan, knowledge, Budget())
    with pytest.raises(RuntimeError):
        store.project(fault=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
    assert store.audit("book-a")["revision"] == 1
    assert store.audit("book-a")["pending_projections"] == 1
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert store.project() == 1


def test_concurrent_commits_have_one_winner(setup):
    store, _, plan, knowledge = setup
    r = SceneRunner(store, FixtureClient()).run(plan, knowledge, Budget(), stop_after_verified=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: Store(store.path).commit(r["job_id"]), range(2)))
    assert outcomes[0]["commit_id"] == outcomes[1]["commit_id"]
    assert store.audit("book-a")["commits"] == 1


def test_other_verified_scene_becomes_stale_after_first_commit(setup):
    store, _, plan, knowledge = setup
    runner = SceneRunner(store, FixtureClient())
    a = runner.run(plan, knowledge, Budget(), stop_after_verified=True)
    other = plan.model_copy(update={"scene_id": "s2", "idempotency_key": "s2-v1"})
    b = runner.run(other, knowledge, Budget(), stop_after_verified=True)
    store.commit(a["job_id"])
    with pytest.raises(RuntimeFault, match="revision_conflict"):
        store.commit(b["job_id"])
    assert store.audit("book-a")["commits"] == 1


@pytest.mark.parametrize("mutation,error", [
    ({"expected_revision": 1}, "revision_conflict"),
    ({"pov": "stranger"}, "unknown_pov"),
    ({"book_id": "book-b"}, "world_not_found"),
])
def test_scope_and_preconditions_fail_before_calls(setup, mutation, error):
    store, _, plan, knowledge = setup
    client = FixtureClient()
    with pytest.raises(RuntimeFault, match=error):
        SceneRunner(store, client).run(plan.model_copy(update=mutation), knowledge, Budget())
    assert client.n == 0


def test_simulation_never_enters_commit_pipeline(setup):
    store, world, plan, knowledge = setup
    store.create_world(world.model_copy(update={"branch_id": "dream-1"}), kind="simulation")
    with pytest.raises(RuntimeFault, match="simulation_cannot_commit"):
        SceneRunner(store, FixtureClient()).run(plan.model_copy(update={"branch_id": "dream-1"}), knowledge, Budget())
    assert store.snapshot("book-a").revision == 0
    assert store.snapshot("book-a", "dream-1").revision == 0


@pytest.mark.parametrize("fact,before,error", [("rule", "immutable", "immutable"),
    ("missing", 1, "unknown"), ("coins", 99, "precondition"), ("secret", "HIDDEN-FUTURE-ANSWER", "outside_pov")])
def test_cannot_authorize_impossible_plan(setup, fact, before, error):
    store, _, plan, knowledge = setup
    changed = plan.model_copy(update={"events": [PlannedEvent(event_id="bad", description="bad",
        changes=[Change(fact=fact, before=before, after="changed")])]})
    with pytest.raises(RuntimeFault, match=error):
        SceneRunner(store, FixtureClient()).run(changed, knowledge, Budget())


def test_idempotency_input_change_including_budget_is_rejected(setup):
    store, _, plan, knowledge = setup
    runner = SceneRunner(store, FixtureClient())
    runner.run(plan, knowledge, Budget())
    with pytest.raises(RuntimeFault, match="idempotency_input_conflict"):
        runner.run(plan, knowledge, Budget(max_calls=10))
    with pytest.raises(RuntimeFault, match="knowledge_scope_conflict"):
        runner.run(plan.model_copy(update={"idempotency_key": "new", "expected_revision": 1}),
                   knowledge.model_copy(update={"book_id": "book-b"}), Budget())


@pytest.mark.parametrize("config", [{"hard": True}, {"forged": True}])
def test_bad_review_cannot_grant_permission_and_stops_at_limit(setup, config):
    store, _, plan, knowledge = setup
    client = FixtureClient(**config)
    runner = SceneRunner(store, client)
    with pytest.raises(RuntimeFault, match="rewrite_budget_exhausted"):
        runner.run(plan, knowledge, Budget())
    assert client.n == 6 and store.snapshot("book-a").revision == 0
    with pytest.raises(RuntimeFault, match="rewrite_budget_exhausted"):
        runner.run(plan, knowledge, Budget())
    assert client.n == 6  # resume never resets the rewrite/call budget


def test_broken_verifier_quote_does_not_cause_prose_rewrite(setup):
    store, _, plan, knowledge = setup
    client = FixtureClient(fake_quote=True)
    with pytest.raises(RuntimeFault, match="verifier_contract_repair_exhausted"):
        SceneRunner(store, client).run(plan, knowledge, Budget())
    assert client.n == 3 and store.snapshot("book-a").revision == 0
    with store.connection() as db:
        stages = [r[0] for r in db.execute("SELECT stage FROM calls ORDER BY rowid")]
    assert stages == ["writer.0", "verifier.0", "verifier.0.contract1"]


def test_verifier_contract_can_recover_with_original_prose(setup):
    class Repaired(FixtureClient):
        def invoke(self, **kwargs):
            self.fake_quote = "repair_instruction" not in kwargs["payload"]
            return super().invoke(**kwargs)
    store, _, plan, knowledge = setup
    client = Repaired()
    result = SceneRunner(store, client).run(plan, knowledge, Budget())
    assert result["status"] == "committed" and client.n == 3
    assert store.audit("book-a")["commits"] == 1


def test_call_budget_is_hard_limit_even_with_rewrites_left(setup):
    store, _, plan, knowledge = setup
    client = FixtureClient(hard=True)
    with pytest.raises(RuntimeFault, match="call_budget_exhausted"):
        SceneRunner(store, client).run(plan, knowledge, Budget(max_calls=2))
    assert client.n == 2
    assert store.snapshot("book-a").revision == 0


def test_oversized_input_fails_without_model_call(setup):
    store, _, plan, knowledge = setup
    client = FixtureClient()
    with pytest.raises(RuntimeFault, match="context_budget_exceeded"):
        SceneRunner(store, client).run(plan, knowledge, Budget(max_input_chars=100))
    assert client.n == 0


def test_unknown_result_requires_reconciliation_not_automatic_retry(setup):
    store, _, plan, knowledge = setup
    client = FixtureClient(unknown=True)
    runner = SceneRunner(store, client)
    with pytest.raises(RuntimeFault, match="injected_unknown"):
        runner.run(plan, knowledge, Budget())
    with pytest.raises(RuntimeFault, match="requires_reconciliation:unknown"):
        SceneRunner(Store(store.path), client).run(plan, knowledge, Budget())
    assert client.n == 1 and store.snapshot("book-a").revision == 0


def test_interrupted_dispatched_call_is_not_resent(setup):
    store, _, plan, knowledge = setup
    client = FixtureClient()
    job = store.prepare(plan, knowledge, Budget(), client.models)
    store.reserve_call(job, "writer.0", {"system": "test", "input": {}})
    with pytest.raises(RuntimeFault, match="reconciliation:dispatched"):
        store.reserve_call(job, "writer.0", {"system": "test", "input": {}})
    assert client.n == 0


def test_audit_catches_tampered_canon(setup):
    store, _, plan, knowledge = setup
    SceneRunner(store, FixtureClient()).run(plan, knowledge, Budget())
    with store.connection() as db:
        db.execute("UPDATE commits SET text='tampered'")
    assert not store.audit("book-a")["ok"]


def test_strict_schema_rejects_similar_types_and_unknown_authority():
    with pytest.raises(ValidationError):
        Budget(max_calls="6")
    with pytest.raises(ValidationError):
        Change(fact="x", before=0, after=1, authorize_canon_change=True)


def test_refuses_research_database(tmp_path):
    import sqlite3
    path = tmp_path / "research.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE works(id TEXT)")
    with pytest.raises(RuntimeFault, match="refuse_non_runtime"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["works"]


def test_gateway_one_request_actual_model_and_no_hidden_retry(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "LLM_MODE", "real")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "https://gateway.invalid/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "test-key")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "actual-revision", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 4, "completion_tokens": 2}})

    client = GatewayClient("requested-w", "requested-v", transport=httpx.MockTransport(handler))
    result = client.invoke(role="writer", system="test", payload={}, max_tokens=123, timeout=5)
    assert result["actual_model"] == "actual-revision"
    assert json.loads(calls[0].content)["max_tokens"] == 123 and len(calls) == 1
    client.transport = httpx.MockTransport(lambda _: httpx.Response(503, text="SECRET_RESPONSE"))
    with pytest.raises(RuntimeFault, match="gateway_http_503") as e:
        client.invoke(role="writer", system="test", payload={}, max_tokens=123, timeout=5)
    assert "SECRET" not in str(e.value)


def test_last_response_over_time_budget_cannot_commit_or_resume_past_limit(setup, monkeypatch):
    store, _, plan, knowledge = setup
    client = FixtureClient()
    # First call costs 1s, second costs 3s: the last response overruns a 3s budget.
    ticks = iter([0.0, 1.0, 1.0, 4.0])
    monkeypatch.setattr("app.scene_runtime.pipeline.time.monotonic", lambda: next(ticks))
    runner = SceneRunner(store, client)
    with pytest.raises(RuntimeFault, match="completed_call_exceeded_budget"):
        runner.run(plan, knowledge, Budget(max_elapsed_seconds=3))
    with pytest.raises(RuntimeFault, match="completed_call_exceeded_budget"):
        runner.run(plan, knowledge, Budget(max_elapsed_seconds=3))
    assert client.n == 2 and store.snapshot("book-a").revision == 0


def test_source_adapter_is_readonly_and_excludes_reference_prose(tmp_path):
    import sqlite3
    from app.scene_runtime.knowledge import genome_package
    path = tmp_path / "research.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE expression_strategies(id TEXT,name TEXT,conditions TEXT,recommended TEXT,avoid TEXT,version TEXT,examples TEXT)")
        db.execute("INSERT INTO expression_strategies VALUES(?,?,?,?,?,?,?)", ("ES-1", "dialogue", '["negotiation"]',
                   '["keep intent"]', '["redundancy"]', "v1", "MUST_NOT_ENTER_WRITER"))
    before = path.read_bytes()
    package = genome_package(path, "book-a", ["ES-1"])
    assert "MUST_NOT_ENTER_WRITER" not in canonical(package)
    assert path.read_bytes() == before
    assert package.techniques[0].evidence_status == "hypothesis"
    with pytest.raises(RuntimeFault, match="strategy_not_found"):
        genome_package(path, "book-a", ["missing"])


def test_three_scene_chain_uses_committed_context_and_resources(setup):
    store, _, plan, knowledge = setup
    runner = SceneRunner(store, FixtureClient())
    for i in range(3):
        p = plan.model_copy(update={"expected_revision": i, "scene_id": f"s{i}", "idempotency_key": f"run{i}",
            "events": [PlannedEvent(event_id=f"pay{i}", description="test transfer", changes=[
                Change(fact="coins", before=3-i, after=2-i), Change(fact="received", before=i, after=i+1)])]})
        result = runner.run(p, knowledge, Budget())
        context = json.loads(store.job(result["job_id"])["context"])
        assert context["facts"]["coins"]["value"] == 3-i
        assert len(context["recent_committed_scenes"]) == i
        with store.connection() as db:
            request = json.loads(db.execute("SELECT request FROM calls WHERE job=? AND stage='verifier.0'", (result["job_id"],)).fetchone()[0])
        assert request["input"]["recent_committed_scenes"] == context["recent_committed_scenes"]
    assert store.snapshot("book-a").facts["coins"].value == 0
    assert store.snapshot("book-a").facts["received"].value == 3
    assert store.audit("book-a")["commits"] == 3


def test_quote_alignment_preserves_exact_span_and_rejects_fabrication():
    from app.scene_runtime.contracts import Review
    from app.scene_runtime.pipeline import align_quotes, parse_result
    raw = '```json\n{"issues":[],"events":[],"changes":[{"fact":"lamp","after":true,"quote":"火起。灯亮了。"}]}\n```'
    review = parse_result(raw, Review)
    assert align_quotes("火起。\n\n灯亮了。", review).changes[0].quote == "火起。\n\n灯亮了。"
    assert align_quotes("火起。灯灭了。", review).changes[0].quote == "火起。灯亮了。"
    with pytest.raises(RuntimeFault, match="invalid_model_contract"):
        parse_result("Some extra prose\n" + raw, Review)


def test_confirmed_operator_issue_blocks_existing_verification(setup):
    from app.scene_runtime.contracts import Issue
    store, _, plan, knowledge = setup
    client = FixtureClient()
    result = SceneRunner(store, client).run(plan, knowledge, Budget(), stop_after_verified=True)
    job = store.job(result["job_id"])
    text = json.loads(job["verified"])["text"]
    store.add_confirmed_issue(job["id"], text, Issue(kind="hard", description="本稿存在已确认事实问题", quote=text))
    with pytest.raises(RuntimeFault, match="unverified_scene"):
        store.commit(job["id"])
    with pytest.raises(RuntimeFault, match="rewrite_budget_exhausted"):
        SceneRunner(store, client).run(plan, knowledge, Budget())
    assert store.snapshot("book-a").revision == 0 and client.n == 6


def test_operator_cannot_annotate_unknown_or_committed_artifact(setup):
    from app.scene_runtime.contracts import Issue
    store, _, plan, knowledge = setup
    r = SceneRunner(store, FixtureClient()).run(plan, knowledge, Budget())
    with pytest.raises(RuntimeFault, match="cannot_annotate_committed"):
        store.add_confirmed_issue(r["job_id"], "text", Issue(kind="hard", description="change", quote="text"))


@pytest.mark.parametrize("forged_patch", [False, True])
def test_audited_false_positive_resume_keeps_budget_and_plan_gates(setup, forged_patch):
    from app.scene_runtime.contracts import Issue, Review, digest
    from app.scene_runtime.pipeline import parse_result
    store, _, plan, knowledge = setup
    first = SceneRunner(store, FixtureClient()).run(plan, knowledge, Budget())
    second = plan.model_copy(update={"expected_revision": 1, "scene_id": "s2", "idempotency_key": "s2",
        "events": [PlannedEvent(event_id="pay2", description="second transfer", changes=[
            Change(fact="coins", before=2, after=1), Change(fact="received", before=1, after=2)])]})
    client = FixtureClient(hard=True, forged=forged_patch)
    budget = Budget(max_calls=2)
    runner = SceneRunner(store, client)
    with pytest.raises(RuntimeFault, match="call_budget_exhausted"):
        runner.run(second, knowledge, budget)
    job_id = store.prepare(second, knowledge, budget, client.models)
    with store.connection() as db:
        call = db.execute("SELECT * FROM calls WHERE job=? AND stage='verifier.0'", (job_id,)).fetchone()
    text = json.loads(call["request"])["input"]["text"]
    review = parse_result(json.loads(call["response"])["text"], Review)
    issue = review.issues[0]
    source = {"reviewer": "test-operator", "reason": "fixture adjudication of a previous-scene detail",
              "evidence_commit_id": first["commit_id"], "evidence_quote": "沈砚收起这一枚钱。"}
    with pytest.raises(RuntimeFault, match="invalid_canon_evidence"):
        store.dismiss_review_issue(job_id, "verifier.0", issue, **{**source, "evidence_quote": "fabricated"})
    with pytest.raises(RuntimeFault, match="unknown_issue"):
        store.dismiss_review_issue(job_id, "verifier.0", issue.model_copy(update={"description": "different"}), **source)
    store.dismiss_review_issue(job_id, "verifier.0", issue, **source)
    store.dismiss_review_issue(job_id, "verifier.0", issue, **source)  # idempotent
    assert len(store.review_decisions(job_id, digest(text))) == 1
    assert store.apply_review_decisions(job_id, "verifier.1", text, review).issues == [issue]
    assert store.apply_review_decisions(job_id, "verifier.0", text + "different", review).issues == [issue]
    additional = Issue(kind="hard", description="independent unresolved defect", quote=text)
    with_extra = review.model_copy(update={"issues": [issue, additional]})
    assert store.apply_review_decisions(job_id, "verifier.0", text, with_extra).issues == [additional]
    if forged_patch:
        with pytest.raises(RuntimeFault, match="call_budget_exhausted"):
            runner.run(second, knowledge, budget)
        assert store.snapshot("book-a").revision == 1
    else:
        result = runner.run(second, knowledge, budget)
        assert result["revision"] == 2 and result["usage"]["calls"] == 2
        assert store.export("book-a")[1]["review_decisions"][0]["reviewer"] == "test-operator"
        with pytest.raises(RuntimeFault, match="cannot_annotate_committed"):
            store.dismiss_review_issue(job_id, "verifier.0", issue, **source)
    assert client.n == 2 and store.receipt(first["job_id"]) == {k: v for k, v in first.items() if k not in {"usage", "reused"}}
