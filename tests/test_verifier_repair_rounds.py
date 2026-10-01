"""核验返修失败 → 可重试轮次（不放宽任何判据）。

事故分布（K5 十场 live，46 条真实臂失败的历史汇总）：
``verifier_contract_repair_exhausted`` 13 / ``invalid_model_contract:Draft`` 13 /
``rewrite_budget_exhausted:*`` 10 / ``transport`` 6 /
``verifier_state_repair_exhausted`` 3 / ``call_budget_exhausted`` 1。

改动前 `pipeline.py` 在核验工件（review JSON / evidence 引用 / changes 清单）
返修一次后仍不合格时 `raise RuntimeFault(...)`，**当场炸掉整批**：写手剩下的
`max_rewrites` 轮次被白白丢弃，写手拿不到本轮反馈。改动后它是**本轮失败**：
错误连同 contract_errors 原文回灌写手，写手轮次循环继续；只有轮次预算用尽
才失败，且失败码**保留原名**。

本文件的判据纪律：
- 判据（``validate_review`` / ``align_quotes`` / plan / 字数 / 计划事件）**一字未动**，
  用例只证明「预算与控制流变了」；
- 核验席恒产出非法工件 ⇒ **必须仍失败**，任何 ``max_verifier_repairs`` 取值都
  不许把同一个非法 review 判成合法；
- 全程 fixture / 假 client：不写真库、不调真实模型。
"""
import json

import pytest
from pydantic import ValidationError

from app.scene_runtime.contracts import (Budget, Change, Fact, KnowledgePackage, PlannedEvent,
                                         Review, RuntimeFault, ScenePlan, World, canonical,
                                         validate_review)
from app.scene_runtime.pipeline import (SceneRunner, align_quotes, parse_result)
from app.scene_runtime.store import Store

TEXT = "林穗把一枚钱推到桌上。她还剩两枚。"
BAD_JSON = "这不是 JSON，只是模型跑偏了自由散文。"
CONTRACT_CODE = "verifier_contract_repair_exhausted"
STATE_CODE = "verifier_state_repair_exhausted"


def ok_review(payload, *, quote=None, changes_extra=(), changes_drop=(),
              issues=(), events_duplicate=False):
    """Plan-conformant review for the payload's own plan/prose."""
    plan, text = payload["plan"], payload["text"]
    quote = text if quote is None else quote
    changes = [{"fact": c["fact"], "after": c["after"], "quote": quote}
               for e in plan["events"] for c in e["changes"]
               if c["fact"] not in changes_drop]
    changes.extend({"fact": f, "after": a, "quote": quote} for f, a in changes_extra)
    events = [{"event_id": e["event_id"], "quote": quote} for e in plan["events"]]
    if events_duplicate and events:
        events = events + [dict(events[0])]
    return canonical({"issues": list(issues), "events": events, "changes": changes})


def bad_quote_review(payload):
    """evidence_not_in_text：引用是编造的原文（隔离复现的形状）。"""
    return ok_review(payload, quote="沈砚当众宣布此事")


def bad_state_review(payload):
    """state_patch_not_authorized_by_plan：未变化的 lamp 被误列进 changes。"""
    return ok_review(payload, changes_extra=(("lamp", False),))


def hard_issue_review(payload):
    """unresolved_hard_issue + 补丁失配：真·正文缺陷信号，不是工件缺陷。"""
    return ok_review(payload, changes_extra=(("lamp", False),),
                     issues=({"kind": "hard", "description": "正文没写支付动作",
                              "quote": payload["text"]},))


def duplicate_event_review(payload):
    """duplicate_event_evidence：同一计划事件被登记两次。"""
    return ok_review(payload, events_duplicate=True)


class ScriptedClient:
    """Fixture client driven by per-role scripts (stage names never reach invoke).

    Each script item is either a raw reply text or a callable(payload) -> text;
    the last item repeats once the script runs out.
    """

    models = {"writer": "fixture-w", "verifier": "fixture-v", "transport": "fixture"}

    def __init__(self, writer=(canonical({"text": TEXT}),), verifier=()):
        self.writer_script, self.verifier_script = list(writer), list(verifier)
        self.writer_calls, self.verifier_calls = 0, 0
        self.writer_payloads, self.verifier_payloads = [], []

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            index = self.writer_calls
            self.writer_calls += 1
            self.writer_payloads.append(payload)
            script = self.writer_script
        else:
            index = self.verifier_calls
            self.verifier_calls += 1
            self.verifier_payloads.append(payload)
            script = self.verifier_script
        if not script:
            raise AssertionError(f"unexpected {role} call #{index + 1}")
        item = script[index] if index < len(script) else script[-1]
        text = item(payload) if callable(item) else item
        return {"text": text, "requested_model": self.models[role], "actual_model": "fixture",
                "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}


@pytest.fixture()
def world_plan(tmp_path):
    store = Store(tmp_path / "runtime.sqlite")
    store.create_world(World(
        book_id="book-a", revision=0, characters={"lin": "林穗"},
        facts={"coins": Fact(value=3, visible_to=["lin"]),
               "received": Fact(value=0, visible_to=["lin"]),
               "lamp": Fact(value=True, visible_to=["lin"])}, rules=[]))
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1",
                     expected_revision=0, pov="lin", goal="支付一枚钱", style="简洁",
                     min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱",
                                          changes=[Change(fact="coins", before=3, after=2),
                                                   Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a",
                                 source_kind="empty", techniques=[])
    return store, plan, knowledge


def stages_of(store, plan=None):
    """Call ledger stages in dispatch order, optionally narrowed to one job."""
    with store.connection() as db:
        if plan is None:
            rows = db.execute("SELECT stage FROM calls ORDER BY rowid").fetchall()
        else:
            rows = db.execute(
                "SELECT c.stage FROM calls c JOIN jobs j ON j.id=c.job "
                "WHERE j.idem=? ORDER BY c.rowid", (plan.idempotency_key,)).fetchall()
    return [row[0] for row in rows]


def fault_code(excinfo):
    """RuntimeFault code = text before ':' (errors keep their original names)."""
    return str(excinfo.value).split(":", 1)[0]


def run(store, plan, knowledge, budget, client):
    return SceneRunner(store, client).run(plan, knowledge, budget)


# ---------------------------------------------------------------- 预算域本身

def test_budget_defaults_unchanged_and_only_upper_bounds_widened():
    """默认值一字未改（未显式配置 ⇒ 与改动前逐字一致）；只放宽 le、新增字段默认 1。"""
    default = Budget()
    assert (default.max_calls, default.max_rewrites, default.max_verifier_repairs) == (6, 2, 1)
    assert default.style_feedback is False
    # 新上限域可达，默认闸仍原样生效。
    assert Budget(max_calls=60, max_rewrites=4, max_verifier_repairs=4).max_calls == 60
    for over in ({"max_calls": 61}, {"max_rewrites": 5}, {"max_verifier_repairs": 5},
                 {"max_calls": 1}, {"max_rewrites": -1}, {"max_verifier_repairs": -1}):
        with pytest.raises(ValidationError):
            Budget(**over)
    with pytest.raises(ValidationError):      # strict=True 契约仍在
        Budget(max_calls="6")


def test_default_budget_run_is_unchanged_two_calls(world_plan):
    """未配置新字段时 happy path 逐字不变：writer.0 + verifier.0 就提交。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[ok_review])
    result = run(store, plan, knowledge, Budget(), client)
    assert result["status"] == "committed" and client.writer_calls == 1
    assert client.verifier_calls == 1
    assert stages_of(store) == ["writer.0", "verifier.0"]
    assert store.audit("book-a")["ok"] is True


# ------------------------------------------------- 核心正向用例：第 2 轮修好

def test_writer_round_two_recovers_from_verifier_artifact_failure(world_plan):
    """核心正向：第 1 轮核验工件不合格（编造引用），写手第 2 轮拿到反馈后
    核验通过 ⇒ 现在必须能提交（旧实现在此当场 raise verifier_contract_repair_
    exhausted，整批炸掉）。正文两轮逐字相同 ⇒ 收益只可能来自控制流。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(writer=(canonical({"text": TEXT}), canonical({"text": TEXT})),
                            verifier=[bad_quote_review, bad_quote_review, ok_review])
    budget = Budget(max_calls=12, max_rewrites=2, max_verifier_repairs=1)
    result = run(store, plan, knowledge, budget, client)
    assert result["status"] == "committed"
    assert store.audit("book-a")["commits"] == 1
    assert stages_of(store) == ["writer.0", "verifier.0", "verifier.0.contract1",
                                "writer.1", "verifier.1"]
    assert store.snapshot("book-a").facts["coins"].value == 2


def test_illegal_review_json_round_one_then_round_two_commits(world_plan):
    """同款正向用例（非法 JSON 变体）：核验席跑偏的工件不再炸掉整批。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[BAD_JSON, BAD_JSON, ok_review])
    result = run(store, plan, knowledge,
                 Budget(max_calls=12, max_rewrites=2, max_verifier_repairs=1), client)
    assert result["status"] == "committed" and client.writer_calls == 2
    assert stages_of(store) == ["writer.0", "verifier.0", "verifier.0.contract1",
                                "writer.1", "verifier.1"]


def test_round_failure_feedback_carries_the_verbatim_error(world_plan):
    """回灌必须是**原文**：错误名 + contract_errors 原文进写手 mechanical_errors，
    且 previous_draft 逐字回传（写手修稿输入本来就支持这两项）。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[BAD_JSON, BAD_JSON, ok_review])
    run(store, plan, knowledge, Budget(max_calls=12, max_rewrites=2, max_verifier_repairs=1),
        client)
    feedback = client.writer_payloads[1]
    assert feedback["previous_draft"] == TEXT
    assert feedback["mechanical_errors"] == ["verifier_contract_repair_exhausted:invalid_review_json"]
    assert CONTRACT_CODE in feedback["instruction"]
    # 返修载荷只回灌核验席，正文与计划一字不动。
    assert client.verifier_payloads[1]["text"] == TEXT
    assert client.verifier_payloads[1]["previous_review"] == BAD_JSON
    assert client.verifier_payloads[1]["contract_errors"] == ["invalid_review_json"]


# ------------------------------------------------ 反向自检：恒非法 ⇒ 恒失败

@pytest.mark.parametrize("poison", [BAD_JSON, bad_quote_review, bad_state_review])
def test_permanently_illegal_artifact_never_becomes_a_pass(world_plan, poison):
    """核验席恒产出非法工件 ⇒ 最终**仍必须失败**（判据未动 ⇒ 不可能变成通过），
    且失败码保留原名。额度给足（max_calls=60）以便真的走完所有轮次。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[poison])
    budget = Budget(max_calls=60, max_rewrites=2, max_verifier_repairs=1)
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge, budget, client)
    code = fault_code(excinfo)
    assert code in {CONTRACT_CODE, STATE_CODE}
    assert str(excinfo.value).split(":", 1)[1]           # contract_errors 原文保留
    assert client.writer_calls == budget.max_rewrites + 1
    assert client.verifier_calls == client.writer_calls * 2
    assert store.audit("book-a")["commits"] == 0
    assert store.snapshot("book-a").revision == 0
    assert store.snapshot("book-a").facts["coins"].value == 3      # 正史零改动


def test_rounds_spent_keeps_the_original_failure_codes(world_plan):
    """轮次用尽 ⇒ 失败原因仍是原名（RuntimeFault 的 code 断言），不是新名字，
    也不是把失败写成通过。两轮都用满（每轮 1 写手 + 1 核验 + 2 次返修）。"""
    for index, (code, poison, kind) in enumerate(
            ((CONTRACT_CODE, bad_quote_review, "contract"),
             (STATE_CODE, bad_state_review, "state"))):
        store, plan, knowledge = world_plan
        plan = plan.model_copy(update={"scene_id": f"s{index}",
                                       "idempotency_key": f"s{index}-v1"})
        client = ScriptedClient(verifier=[poison])
        with pytest.raises(RuntimeFault) as excinfo:
            run(store, plan, knowledge,
                Budget(max_calls=60, max_rewrites=1, max_verifier_repairs=2), client)
        assert fault_code(excinfo) == code
        assert client.writer_calls == 2
        expected = []
        for round_index in (0, 1):
            expected += [f"writer.{round_index}", f"verifier.{round_index}"]
            expected += [f"verifier.{round_index}.{kind}{i}" for i in (1, 2)]
        assert stages_of(store, plan) == expected
        assert store.audit("book-a")["commits"] == 0


@pytest.mark.parametrize("max_verifier_repairs", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("shape,expected", [
    (bad_quote_review, CONTRACT_CODE),
    (bad_state_review, STATE_CODE),
    (duplicate_event_review, CONTRACT_CODE),
])
def test_same_illegal_review_stays_illegal_under_every_repair_budget(
        world_plan, shape, expected, max_verifier_repairs):
    """同一份非法 review 在**任意** max_verifier_repairs 下都判非法：判据与预算
    无关。先用同一输入直接对照 validate_review / align_quotes，再跑整条链。"""
    store, plan, knowledge = world_plan
    payload = {"plan": plan.model_dump(), "text": TEXT}
    review = parse_result(shape(payload), Review)
    assert review is not None
    probe = validate_review(plan, TEXT, review)
    # align_quotes 不许把编造引用"修好"，也不许改变判据结论。
    assert align_quotes(TEXT, review) is not None
    assert probe == validate_review(plan, TEXT, align_quotes(TEXT, review))
    assert probe, "fixture 必须真的非法，否则本用例失去反向自检意义"
    expected_errors = {
        bad_quote_review: "evidence_not_in_text",
        bad_state_review: "state_patch_not_authorized_by_plan",
        duplicate_event_review: "duplicate_event_evidence",
    }[shape]
    assert expected_errors in probe
    client = ScriptedClient(verifier=[shape])
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge,
            Budget(max_calls=60, max_rewrites=0, max_verifier_repairs=max_verifier_repairs),
            client)
    assert fault_code(excinfo) == expected
    assert store.audit("book-a")["commits"] == 0
    assert client.writer_calls == 1                     # 只一轮，正文不重写


# ---------------------------------------- 返修次数 / stage 名受预算字段控制

@pytest.mark.parametrize("max_verifier_repairs,repair_stages", [
    (0, []), (1, ["contract1"]), (2, ["contract1", "contract2"]),
    (3, ["contract1", "contract2", "contract3"]),
    (4, ["contract1", "contract2", "contract3", "contract4"])])
def test_contract_repair_stages_follow_max_verifier_repairs(
        world_plan, max_verifier_repairs, repair_stages):
    """stage = verifier.{round}.contract{i}，i 从 1 到 max_verifier_repairs；
    默认 1 ⇒ 与改动前写死的 contract1 逐字一致。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[bad_quote_review])
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge,
            Budget(max_calls=60, max_rewrites=0, max_verifier_repairs=max_verifier_repairs),
            client)
    assert fault_code(excinfo) == CONTRACT_CODE
    assert stages_of(store) == ["writer.0", "verifier.0"] + \
        [f"verifier.0.{stage}" for stage in repair_stages]


@pytest.mark.parametrize("max_verifier_repairs,repair_stages", [
    (0, []), (1, ["state1"]), (3, ["state1", "state2", "state3"])])
def test_state_repair_stages_follow_max_verifier_repairs(
        world_plan, max_verifier_repairs, repair_stages):
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[bad_state_review])
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge,
            Budget(max_calls=60, max_rewrites=0, max_verifier_repairs=max_verifier_repairs),
            client)
    assert fault_code(excinfo) == STATE_CODE
    assert stages_of(store) == ["writer.0", "verifier.0"] + \
        [f"verifier.0.{stage}" for stage in repair_stages]


def test_second_repair_attempt_can_recover_inside_one_round(world_plan):
    """返修预算 >1 时，第 i 次返修成功即过：写手不被拖进下一轮。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[BAD_JSON, BAD_JSON, ok_review])
    result = run(store, plan, knowledge,
                 Budget(max_calls=12, max_rewrites=0, max_verifier_repairs=2), client)
    assert result["status"] == "committed"
    assert client.writer_calls == 1 and client.verifier_calls == 3
    assert stages_of(store) == ["writer.0", "verifier.0",
                                "verifier.0.contract1", "verifier.0.contract2"]


# ------------------------------------------------------ 判据/闸门未被放宽

def test_call_budget_is_still_a_hard_gate_on_retry_rounds(world_plan):
    """默认预算（max_calls=6）下恒非法工件：调用闸**照旧是硬闸**——调用数
    恰好等于 max_calls、全部 succeeded（干净闸拒）、零提交；但顶层失败码是
    **工件根因**（verifier_contract_repair_exhausted），不是症状
    `call_budget_exhausted`（2026-10-01 会审修正：额度付不起下一轮时，
    失败码保留原名，否则这次改动要保住的诊断信息会丢）。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[bad_quote_review])
    budget = Budget()
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge, budget, client)
    assert str(excinfo.value).startswith("verifier_contract_repair_exhausted:")
    assert client.writer_calls + client.verifier_calls == budget.max_calls
    assert client.writer_calls == 2                      # 2 轮各 1 次写手
    assert client.verifier_calls == 4                    # 2 轮 × (首验 + 1 次返修)
    with store.connection() as db:
        rows = db.execute("SELECT status FROM calls WHERE job IS NOT NULL").fetchall()
    assert len(rows) == budget.max_calls and {r[0] for r in rows} == {"succeeded"}
    assert store.audit("book-a")["commits"] == 0


def test_default_budget_reports_the_artifact_root_cause_not_the_symptom(world_plan):
    """默认 Budget() 下「核验工件恒非法」的收敛码 = 工件根因（原名），
    且**不是** call_budget_exhausted —— 会审（qwen 席）点名的归因回归固化，
    这里反向锁定。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[bad_state_review])
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge, Budget(), client)
    code = str(excinfo.value)
    assert code.startswith("verifier_state_repair_exhausted:")
    assert not code.startswith("call_budget_exhausted")
    assert store.audit("book-a")["commits"] == 0


def test_repairable_artifact_never_burns_a_writer_round(world_plan):
    """A10 旧契约不误伤：工件第 1 次返修即合格 ⇒ 写手只发一次，且未变化的
    lamp 绝不许进正史补丁（不吞未经确认的状态变化）。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[bad_state_review, ok_review])
    result = run(store, plan, knowledge, Budget(), client)
    assert result["status"] == "committed"
    assert client.writer_calls == 1 and client.verifier_calls == 2
    assert stages_of(store) == ["writer.0", "verifier.0", "verifier.0.state1"]
    with store.connection() as db:
        patch = json.loads(db.execute(
            "SELECT patch FROM commits WHERE book='book-a'").fetchone()[0])
    assert {c["fact"] for c in patch} == {"coins", "received"}
    assert store.snapshot("book-a").facts["lamp"].value is True
    assert store.audit("book-a")["ok"] is True


def test_real_prose_defect_still_reaches_the_writer_not_the_artifact_repair(world_plan):
    """对照（正文缺陷不许被"工件返修"吞掉）：hard issue + 补丁失配 ⇒ 不进
    核验返修分支，照旧走写手修稿轮，直到 rewrite_budget_exhausted。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[hard_issue_review])
    budget = Budget()
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge, budget, client)
    assert str(excinfo.value).startswith("rewrite_budget_exhausted:")
    assert not [s for s in stages_of(store) if ".contract" in s or ".state" in s]
    assert client.writer_calls == budget.max_rewrites + 1
    assert store.audit("book-a")["commits"] == 0


def test_unrepairable_artifact_never_becomes_canon_or_operator_decision(world_plan):
    """恒非法工件全程不落 apply_review_decisions、不落正史：失败必须干净。"""
    store, plan, knowledge = world_plan
    client = ScriptedClient(verifier=[bad_state_review])
    with pytest.raises(RuntimeFault) as excinfo:
        run(store, plan, knowledge,
            Budget(max_calls=60, max_rewrites=2, max_verifier_repairs=1), client)
    assert fault_code(excinfo) == STATE_CODE
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 0
    assert store.audit("book-a") == {"book_id": "book-a", "branch_id": "main", "revision": 0,
                                     "commits": 0, "pending_projections": 0, "errors": [],
                                     "ok": True}


def test_schema_violations_remain_contract_violations(world_plan):
    """判据未动的另一条：review schema 仍 strict（extra key / 类型错仍拒），
    只是在流水线里被记为一次失败返修，不再炸掉整批。"""
    store, plan, knowledge = world_plan
    bad_extra = canonical({"issues": [], "events": [], "changes": [], "score": "9"})
    bad_type = canonical({"issues": [], "events": [{"event_id": 1, "quote": TEXT}],
                          "changes": []})
    for raw in (bad_extra, bad_type):
        with pytest.raises(RuntimeFault, match="invalid_model_contract:Review"):
            parse_result(raw, Review)
    client = ScriptedClient(verifier=[bad_extra, ok_review])
    result = run(store, plan, knowledge, Budget(max_calls=12, max_rewrites=0,
                                                max_verifier_repairs=1), client)
    assert result["status"] == "committed"        # 一次返修即修好，不写脏正史
    assert store.audit("book-a")["commits"] == 1


def test_faulted_run_replays_by_stage_without_second_billing(world_plan):
    """可重放性：同一 (plan, budget) 重跑按 (job, stage) 回放缓存，不重复计费，
    失败码逐字一致——控制流改动没破坏 resume 语义。"""
    store, plan, knowledge = world_plan
    budget = Budget(max_calls=60, max_rewrites=2, max_verifier_repairs=1)
    client = ScriptedClient(verifier=[bad_quote_review])
    with pytest.raises(RuntimeFault) as first:
        run(store, plan, knowledge, budget, client)
    calls_after_first = client.writer_calls + client.verifier_calls
    with pytest.raises(RuntimeFault) as second:
        run(store, plan, knowledge, budget, client)
    assert str(second.value) == str(first.value)
    assert client.writer_calls + client.verifier_calls == calls_after_first
    assert store.audit("book-a")["commits"] == 0
