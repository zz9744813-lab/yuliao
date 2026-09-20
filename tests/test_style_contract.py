"""语感契约与"干瘪体检"的契约测试。

只锁行为边界：契约文本必须真的进了写手提示词、体检只产出 style 类意见、
开关默认关闭时产线行为与返回值零变化、提示词里不许出现历史上那句"骨架中没有的信息不要添加"。
这里**不**断言"哪段写得好" —— 干瘪是缺席特征，词表抓不到（见 app/style_contract.py 的失败留档）。
"""
import pytest

from app.scene_runtime.contracts import (Budget, Change, Fact, KnowledgePackage, PlannedEvent,
                                        ScenePlan, World, canonical)
from app.scene_runtime.pipeline import WRITER_SYSTEM, SceneRunner
from app.scene_runtime.store import Store
from app.style_contract import issues, probe, sentences

FLAT = "林穗把一枚钱放在桌上。她还剩两枚。沈砚收起这一枚钱。她站起来。她拿起钥匙。她走出门。"

VIVID = ("铜钱按在桌面上，铜面还带着她掌心的温度。推过去的时候指甲刮过木纹，发出很轻的一声。"
         "沈砚没立刻拿，指腹在钱边压了压，像是在掂它。“一枚？”他问。"
         "“一枚。”她把手抽回来，指尖有点凉。门外的风从窗缝里挤进来，灯芯歪了一下，"
         "墙上两个人的影子跟着晃，晃得那条缝更黑。她忽然想起昨夜那盏没修好的灯，"
         "到嘴边的话又咽了回去，只把手在衣角上蹭了蹭。钥匙还在他那边，铜环上系着一段褪色的红绳。"
         "“钥匙在我手里，不代表我会借。”他说。她没接话，听见自己呼吸落在胸腔里的声音，"
         "闷闷的，一声挨着一声。灯又歪了一下。")


def test_sentences_merges_trailing_quote():
    assert sentences("他说：“走吧。”") == ["他说：“走吧。”"]
    assert sentences("冷。她没动。") == ["冷。", "她没动。"]


def test_probe_separates_flat_from_detailed():
    flat, vivid = probe(FLAT), probe(VIVID)
    assert vivid["sensory_per_1k"] > flat["sensory_per_1k"]
    assert vivid["chars"] > flat["chars"]
    assert vivid["sent_sd"] > flat["sent_sd"]


def test_issues_only_produce_style_kind_and_quote_from_text():
    out = issues(FLAT, min_chars=600)
    assert out, "干瘪且远低于字数下限的文本必须给出修稿指令"
    assert {i["kind"] for i in out} == {"style"}
    assert all(i["quote"] in FLAT for i in out)


def test_issues_quiet_on_detailed_text_meeting_min_chars():
    assert issues(VIVID, min_chars=100) == []


def test_writer_prompt_keeps_hard_constraints_and_carries_contract():
    assert "非持久细节" in WRITER_SYSTEM and "句长起伏" in WRITER_SYSTEM
    assert "唯一允许的持久状态变化" in WRITER_SYSTEM, "事实预算必须留在提示词里"
    assert "不得新造持久设定" not in WRITER_SYSTEM.replace("这些键之外的持久状态一律不得变更", ""), \
        "旧的笼统措辞已被更精确的事实预算取代"
    assert "骨架中没有的信息不要添加" not in WRITER_SYSTEM


def test_budget_defaults_to_no_style_feedback():
    assert Budget().style_feedback is False


class EchoClient:
    """写手恒返回干瘪文本；验证器按计划给出证据（不引入 hard 问题）。"""
    models = {"writer": "test-writer", "verifier": "test-verifier", "transport": "fixture"}

    def __init__(self, text=FLAT):
        self.text, self.n, self.writer_payloads = text, 0, []

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        self.n += 1
        if role == "writer":
            self.writer_payloads.append(payload)
            result = {"text": self.text}
        else:
            plan = payload["plan"]
            result = {"issues": [], "changes": [{"fact": c["fact"], "after": c["after"],
                                                 "quote": payload["text"]}
                                                for e in plan["events"] for c in e["changes"]],
                      "events": [{"event_id": e["event_id"], "quote": payload["text"]}
                                 for e in plan["events"]]}
        return {"text": canonical(result), "requested_model": self.models[role], "actual_model": "fixture",
                "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}


@pytest.fixture
def scene(tmp_path):
    store = Store(tmp_path / "runtime.sqlite")
    world = World(book_id="book-a", revision=0, characters={"lin": "林穗", "shen": "沈砚"},
                  facts={"coins": Fact(value=3, visible_to=["lin"]),
                         "received": Fact(value=0, visible_to=["lin", "shen"])},
                  rules=["No magic"])
    store.create_world(world)
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1", expected_revision=0,
                     pov="lin", goal="支付一枚钱", style="简洁", min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱", changes=[
                         Change(fact="coins", before=3, after=2), Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a", source_kind="empty", techniques=[])
    return store, plan, knowledge


def test_style_diagnostics_absent_by_default_and_present_when_enabled(scene):
    store, plan, knowledge = scene
    off = SceneRunner(store, EchoClient()).run(plan, knowledge, Budget())
    assert "style" not in off, "开关默认关闭时返回值必须与既有产线完全一致"

    # 同一 store 上换 idempotency_key 拿一个新 job：世界已推进（revision 1、coins 2/received 1），
    # 计划前置值必须跟着改，否则撞 plan_precondition_conflict。
    plan2 = plan.model_copy(update={
        "scene_id": "s2", "idempotency_key": "s2-v1", "expected_revision": 1,
        "events": [PlannedEvent(event_id="pay2", description="再支付一枚钱", changes=[
            Change(fact="coins", before=2, after=1), Change(fact="received", before=1, after=2)])]})
    on = SceneRunner(store, EchoClient()).run(plan2, knowledge, Budget(style_feedback=True))
    assert on["style"]["chars"] == len(FLAT) and on["style"]["min_chars"] == 1
    assert on["style"]["sensory_per_1k"] < 12, "干瘪文本必然低于感官代理门槛（这正是体检的意义）"
    assert "flavor_score" in on["style"]


def test_style_feedback_never_creates_hard_failure(scene):
    """语感指令只挂在修稿轮上；校验通过时不会因为"文风"把任务判死。"""
    store, plan, knowledge = scene
    client = EchoClient()
    result = SceneRunner(store, client).run(plan, knowledge, Budget(style_feedback=True))
    assert result["status"] == "committed" and client.n == 2
    assert store.snapshot("book-a").facts["coins"].value == 2
