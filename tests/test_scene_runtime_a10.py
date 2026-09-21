"""A10 回归：核验器抽取错误不烧 Writer 修稿额度（审查 20260920-1810）。

事故（审查隔离复现）：正文与计划事件一字未动，仅核验器把**未变化**的
lamp.lit=false 多列进 changes → 被当成 `state_patch_not_authorized_by_plan`
正文缺陷 → 3 次 Writer + 3 次 Verifier 耗尽修稿额度。上一阶段第三场也
曾因两枚未变化的硬币被误列补丁而多改一次正文。

修复契约（监督口径）：
1. 区分**正文事实缺陷**与**核验工件缺陷**：正文零缺陷信号（无 hard
   issue、无长度越界）+ 只有补丁清单失配 = 工件缺陷；
2. 已识别的抽取错误进入**有上限**的核验返修（正文一字不动）；返修
   不了就如实失败（verifier_state_repair_exhausted）；
3. 绝不静默吞掉未经确认的状态变化，也绝不为核验器的错改正文；
4. 真正文缺陷（含 hard issue 信号）照旧走 Writer 修稿——不误伤旧路径。

fixture 以载荷语义区分首验/返修轮：`contract_errors` 键只在返修载荷里
出现（stage 名不进 client.invoke，拿不到）。
"""
import json

import pytest

from app.scene_runtime.contracts import (Budget, Change, Fact, KnowledgePackage,
                                         PlannedEvent, RuntimeFault, ScenePlan, World,
                                         canonical)
from app.scene_runtime.pipeline import SceneRunner
from app.scene_runtime.store import Store

WRITER_TEXT = "林穗把一枚钱放在桌上。她还剩两枚。"


class _Client:
    """poison=extra_change：首验把未变化的 lamp 误列 change（审查复现形状）；
    repair_stub=poison：返修轮也原样回毒（耗尽路径）。"""

    models = {"writer": "t-w", "verifier": "t-v", "transport": "fixture"}

    def __init__(self, *, poison=None, repair_stub=None):
        self.poison, self.repair_stub = poison, repair_stub
        self.writer_calls, self.verifier_calls = 0, 0

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            self.writer_calls += 1
            return {"text": canonical({"text": WRITER_TEXT}), "requested_model": "t-w",
                    "actual_model": "f", "tokens_in": 1, "tokens_out": 1,
                    "finish_reason": "stop"}
        self.verifier_calls += 1
        is_repair = "contract_errors" in payload
        plan = payload["plan"]
        quote = payload["text"]
        changes = [{"fact": c["fact"], "after": c["after"], "quote": quote}
                   for e in plan["events"] for c in e["changes"]]
        issues = []
        if self.poison == "extra_change" and not is_repair:
            # 首验投毒：未变化的 lamp 被误列进 changes（正文没动过它）
            changes.append({"fact": "lamp", "after": False, "quote": quote})
        if self.poison == "extra_change" and is_repair and self.repair_stub == "poison":
            # 耗尽路径：返修轮也回毒
            changes.append({"fact": "lamp", "after": False, "quote": quote})
        if self.poison == "missing_change":
            changes = changes[1:]            # 正文真的没实现该变化
            issues = [{"kind": "hard", "description": "正文没写支付动作",
                       "quote": quote}]
        result = {"issues": issues,
                  "events": [{"event_id": e["event_id"], "quote": quote}
                             for e in plan["events"]],
                  "changes": changes}
        return {"text": canonical(result), "requested_model": "t-v",
                "actual_model": "f", "tokens_in": 1, "tokens_out": 1,
                "finish_reason": "stop"}


@pytest.fixture()
def world_plan():
    world = World(book_id="book-a", revision=0, characters={"lin": "林穗"},
                  facts={"coins": Fact(value=3, visible_to=["lin"]),
                         "received": Fact(value=0, visible_to=["lin"]),
                         "lamp": Fact(value=True, visible_to=["lin"])},
                  rules=[])
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1",
                     expected_revision=0, pov="lin", goal="支付一枚钱", style="简洁",
                     min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱",
                                          changes=[Change(fact="coins", before=3, after=2),
                                                   Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a",
                                  source_kind="empty", techniques=[])
    return world, plan, knowledge


def test_unchanged_fact_mislisted_repairs_without_burning_writer(tmp_path, world_plan):
    """主案（审查复现）：未变化事实被误列 change——核验返修一轮即过，
    Writer 只发一次（旧实现 3 Writer + 3 Verifier 耗尽额度）。"""
    world, plan, knowledge = world_plan
    store = Store(tmp_path / "runtime.sqlite")
    store.create_world(world)
    client = _Client(poison="extra_change")
    result = SceneRunner(store, client).run(plan, knowledge, Budget())
    assert result["status"] == "committed"
    assert client.writer_calls == 1, "正文零缺陷：Writer 不许为核验器的错重写"
    assert client.verifier_calls == 2, "首验 + 一轮有上限核验返修"
    with store.connection() as db:
        patch = json.loads(db.execute(
            "SELECT patch FROM commits WHERE book='book-a'").fetchone()[0])
    assert {c["fact"] for c in patch} == {"coins", "received"}, \
        "未变化的 lamp 绝不许进正史补丁（不吞未经确认的状态变化）"
    assert store.audit("book-a")["ok"] is True


def test_state_repair_exhausted_fails_honestly(tmp_path, world_plan):
    """返修轮仍回毒工件 → 如实失败，Writer 额度照旧只花一次。"""
    world, plan, knowledge = world_plan
    store = Store(tmp_path / "runtime.sqlite")
    store.create_world(world)
    client = _Client(poison="extra_change", repair_stub="poison")
    with pytest.raises(RuntimeFault, match="verifier_state_repair_exhausted"):
        SceneRunner(store, client).run(plan, knowledge, Budget())
    assert client.writer_calls == 1
    assert store.audit("book-a")["commits"] == 0, "未经确认的状态变化绝不落正史"


def test_real_prose_defect_still_reaches_writer(tmp_path, world_plan):
    """对照（不误伤旧路径）：正文真没实现计划变化 + 核验器如实报 hard
    issue → 照旧烧 Writer 修稿额度直到耗尽——正文缺陷走 Writer 是对的。"""
    world, plan, knowledge = world_plan
    store = Store(tmp_path / "runtime.sqlite")
    store.create_world(world)
    client = _Client(poison="missing_change")
    budget = Budget()
    with pytest.raises(RuntimeFault, match="rewrite_budget_exhausted"):
        SceneRunner(store, client).run(plan, knowledge, budget)
    assert client.writer_calls == budget.max_rewrites + 1, \
        "真正文缺陷必须照旧走满修稿轮（A10 不许吞掉正文缺陷路径）"
    assert client.verifier_calls == budget.max_rewrites + 1
