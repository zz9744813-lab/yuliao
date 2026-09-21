"""A09 回归：Runtime 恢复审计必须核验事件/收据/上下文/投影一致性
（审查 20260920-1810）。

事故（审查在三场运行库副本上隔离复现）：把第一场的事件引文换成不存在
的文字、把收据 text_hash 改成全零——audit 仍 ok=true。旧审计只查正文
哈希、补丁证据与状态重放；而它又被用作代码升级前的正史检查，漏过损坏
的收据或事件等于给坏账盖章。

验收（审查口径）：**逐字段损坏注入，必须项项报错**；干净跑零误伤。
"""
import json

import pytest

from app.scene_runtime.client import OutcomeUnknown  # noqa: F401  对齐既有夹具依赖面
from app.scene_runtime.contracts import (Budget, Change, Fact, KnowledgePackage,
                                         PlannedEvent, ScenePlan, World, canonical)
from app.scene_runtime.pipeline import SceneRunner
from app.scene_runtime.store import Store


class _Client:
    models = {"writer": "t-w", "verifier": "t-v", "transport": "fixture"}

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            return {"text": canonical({"text": "林穗把一枚钱放在桌上。她还剩两枚。"}),
                    "requested_model": "t-w", "actual_model": "f",
                    "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}
        plan = payload["plan"]
        return {"text": canonical({"issues": [],
                                   "events": [{"event_id": e["event_id"],
                                               "quote": payload["text"]}
                                              for e in plan["events"]],
                                   "changes": [{"fact": c["fact"], "after": c["after"],
                                                "quote": payload["text"]}
                                               for e in plan["events"]
                                               for c in e["changes"]]}),
                "requested_model": "t-v", "actual_model": "f",
                "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}


@pytest.fixture()
def committed(tmp_path):
    """一条干净提交 + 投影处理完的运行库。"""
    store = Store(tmp_path / "runtime.sqlite")
    world = World(book_id="book-a", revision=0,
                  characters={"lin": "林穗"},
                  facts={"coins": Fact(value=3, visible_to=["lin"]),
                         "received": Fact(value=0, visible_to=["lin"])},
                  rules=[])
    store.create_world(world)
    plan = ScenePlan(book_id="book-a", scene_id="s1", idempotency_key="s1-v1",
                     expected_revision=0, pov="lin", goal="支付一枚钱", style="简洁",
                     min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付一枚钱",
                                          changes=[Change(fact="coins", before=3, after=2),
                                                   Change(fact="received", before=0, after=1)])])
    knowledge = KnowledgePackage(package_id="empty-1", book_id="book-a",
                                  source_kind="empty", techniques=[])
    SceneRunner(store, _Client()).run(plan, knowledge, Budget())
    store.project()
    return store


def _corrupt(store, sql, args=()):
    with store.connection(transaction=True) as db:
        db.execute(sql, args)


def _errors(store):
    return store.audit("book-a")["errors"]


def test_clean_run_has_no_false_positives(committed):
    """干净跑零误伤：扩展后的审计对正常提交+投影必须 ok（含精确形状）。"""
    assert committed.audit("book-a") == {"book_id": "book-a", "branch_id": "main",
                                         "revision": 1, "commits": 1,
                                         "pending_projections": 0,
                                         "errors": [], "ok": True}


def test_event_quote_corruption_detected(committed):
    """审查复现①：事件引文换成不存在的文字——必须 event_quote。"""
    ev = json.loads(_row(committed)["events"])
    ev[0]["quote"] = "根本不存在的引文"
    _corrupt(committed, "UPDATE commits SET events=? WHERE book='book-a'",
             (canonical(ev),))
    assert "event_quote" in _errors(committed)


def test_event_id_corruption_detected(committed):
    r = _row(committed)
    ev = json.loads(r["events"])
    ev[0]["event_id"] = "not-in-plan"
    _corrupt(committed, "UPDATE commits SET events=? WHERE book='book-a'",
             (canonical(ev),))
    errs = _errors(committed)
    assert "event_plan_ref" in errs and "event_plan_coverage" in errs
    # 投影记忆也随之失配——一致性核验要抓全链
    assert "projection_payload" in errs


def test_patch_event_ref_corruption_detected(committed):
    r = _row(committed)
    patch = json.loads(r["patch"])
    patch[0]["event_id"] = "bogus-event"
    _corrupt(committed, "UPDATE commits SET patch=? WHERE book='book-a'",
             (canonical(patch),))
    assert "patch_event_ref" in _errors(committed)


def test_receipt_text_hash_corruption_detected(committed):
    """审查复现②：收据 text_hash 改全零——必须 receipt_hash。"""
    r = _row(committed)
    rc = json.loads(r["receipt"])
    rc["text_hash"] = "0" * 64
    _corrupt(committed, "UPDATE commits SET receipt=? WHERE book='book-a'",
             (canonical(rc),))
    assert "receipt_hash" in _errors(committed)


def test_receipt_field_corruption_detected(committed):
    r = _row(committed)
    rc = json.loads(r["receipt"])
    rc["revision"] = 99
    _corrupt(committed, "UPDATE commits SET receipt=? WHERE book='book-a'",
             (canonical(rc),))
    assert "receipt_fields" in _errors(committed)


def test_context_hash_corruption_detected(committed):
    """提交行 context_hash 被改 → 与收据失配（receipt_hash）+ 与 job
    冻结上下文失配（context_hash）双报。"""
    _corrupt(committed,
             "UPDATE commits SET context_hash='deadbeef' WHERE book='book-a'")
    errs = _errors(committed)
    assert "receipt_hash" in errs and "context_hash" in errs


def test_projection_payload_corruption_detected(committed):
    _corrupt(committed,
             "UPDATE memories SET payload='{}' WHERE book='book-a'")
    assert "projection_payload" in _errors(committed)


def test_outbox_missing_detected(committed):
    _corrupt(committed, "DELETE FROM outbox WHERE commit_id IN "
             "(SELECT id FROM commits WHERE book='book-a')")
    assert "outbox_missing" in _errors(committed)


def test_projection_missing_detected(committed):
    """outbox 标了 processed 却没有投影记忆——半写态的镜像面。"""
    _corrupt(committed, "DELETE FROM memories WHERE book='book-a'")
    assert "projection_missing" in _errors(committed)


def _row(store):
    with store.connection() as db:
        return db.execute("SELECT * FROM commits WHERE book='book-a'").fetchone()
