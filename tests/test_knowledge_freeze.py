"""K3-B 回归：Runtime 每场查询/冻结/恢复适配（监督 2026-09-22 验收点）。

验收点全覆盖：empty / error / 升级(不匹配) / 恢复 / 幂等冲突 / 跨库拒绝 /
收据可读不重写。全部离线：FixtureClient 无真实模型调用。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: F401  (确保 app 导入链可用)
from knowledge_seed import seed_knowledge  # noqa: E402

from app import db, knowledge_query as kq  # noqa: E402
from app.scene_runtime.contracts import (Budget, Change, Fact,  # noqa: E402
                                          KnowledgePackage, PlannedEvent,
                                          RuntimeFault, ScenePlan, World)
from app.scene_runtime.knowledge_v2 import frozen_package_for_scene  # noqa: E402
from app.scene_runtime.pipeline import SceneRunner  # noqa: E402
from app.scene_runtime.store import Store  # noqa: E402


class _Client:
    models = {"writer": "t-w", "verifier": "t-v", "transport": "fixture"}

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            import json as _j
            return {"text": _j.dumps(
                        {"text": "林穗把一枚钱放在桌上。她还剩两枚。"},
                        ensure_ascii=False),
                    "requested_model": "t-w", "actual_model": "f",
                    "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}
        import json as _j
        ev_ids = [ev["event_id"] for ev in payload["plan"]["events"]]
        return {"text": _j.dumps(
                    {"issues": [],
                     "events": [{"event_id": ev_id,
                                 "quote": payload["text"]}
                                for ev_id in ev_ids],
                     "changes": [{"fact": "coins",
                                  "after": [c["after"] for ev in
                                             payload["plan"]["events"]
                                             for c in ev["changes"]][0],
                                  "quote": payload["text"]}]},
                    ensure_ascii=False),
                "requested_model": "t-v", "actual_model": "f",
                "tokens_in": 1, "tokens_out": 1, "finish_reason": "stop"}


def _world():
    return World(book_id="WK-ALPHA", revision=0, characters={"lin": "林穗"},
                 facts={"coins": Fact(value=3, visible_to=["lin"]),
                        "received": Fact(value=0, visible_to=["lin"])},
                 rules=[])


def _plan(idem="k3b-v1", goal="支付一枚钱"):
    return ScenePlan(book_id="WK-ALPHA", scene_id="s1",
                     idempotency_key=idem, expected_revision=0, pov="lin",
                     goal=goal, style="简洁", min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付",
                          changes=[Change(fact="coins", before=3, after=2)])])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    seed_knowledge()
    store = Store(tmp_path / "rt.sqlite")
    store.create_world(_world())
    return store, _Client()


def test_freeze_before_prepare_recovery_no_requery(env, monkeypatch):
    store, client = env
    calls = {"n": 0}
    orig = kq.query_knowledge
    monkeypatch.setattr(kq, "query_knowledge",
                        lambda p, s: (calls.__setitem__("n", calls["n"] + 1)
                                      or orig(p, s)))
    pkg, meta = frozen_package_for_scene(store, db.session(), _plan())
    assert meta["reused"] is False and pkg.source_kind == "knowledge_query_v2"
    assert calls["n"] == 1, "首 prepare 前查询恰好一次"
    r1 = SceneRunner(store, client).run(_plan(), pkg, Budget())
    assert r1["status"] == "committed"
    # 同任务恢复：job 已存在 → 不重查，复用 job 冻结包
    pkg2, meta2 = frozen_package_for_scene(store, db.session(), _plan())
    assert meta2["reused"] is True and calls["n"] == 1, "恢复不许重查"
    r2 = SceneRunner(store, client).run(_plan(), pkg2, Budget())
    assert r2["reused"] is True, "同幂等键同输入 → 复用不重跑"
    assert store.audit("WK-ALPHA")["ok"] is True, "收据可读：audit 过"


def test_same_idem_key_different_plan_conflicts(env):
    store, client = env
    pkg, _ = frozen_package_for_scene(store, db.session(), _plan())
    SceneRunner(store, client).run(_plan(), pkg, Budget())
    # 同幂等键异输入：桥复用旧包 + 新 plan → request 哈希不符 → 冲突
    pkg_old, meta = frozen_package_for_scene(store, db.session(), _plan())
    assert meta["reused"] is True
    with pytest.raises(RuntimeFault, match="idempotency_input_conflict"):
        SceneRunner(store, client).run(_plan(goal="改了的目标"), pkg_old, Budget())


def test_empty_query_zero_techniques(env, tmp_path, monkeypatch):
    store, client = env
    store.create_world(World(book_id="WK-NONE", revision=0,
                             characters={"x": "某"},
                             facts={"coins": Fact(value=3, visible_to=["x"])},
                             rules=[]))
    plan = ScenePlan(book_id="WK-NONE", scene_id="s9",
                     idempotency_key="k3b-empty", expected_revision=0,
                     pov="x", goal="无事发生", style="简洁", min_chars=1,
                     max_chars=500,
                     events=[PlannedEvent(event_id="pay", description="支付",
                              changes=[Change(fact="coins", before=3, after=2)])])
    pkg, meta = frozen_package_for_scene(store, db.session(), plan)
    assert meta["query_status"] == "empty" and pkg.techniques == []
    r = SceneRunner(store, client).run(plan, pkg, Budget())
    assert r["status"] == "committed", "诚实空包照常跑（不硬凑知识）"


def test_unavailable_raises_no_fallback(env, monkeypatch):
    store, client = env
    monkeypatch.setattr(kq, "query_knowledge",
                        lambda p, s: {"status": "unavailable",
                                      "reason": "x", "selected": [],
                                      "rejected": []})
    with pytest.raises(RuntimeFault, match="knowledge_query_unavailable"):
        frozen_package_for_scene(store, db.session(), _plan())
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0, \
            "服务故障不许留下半成品 job"


def test_cross_book_rejected(env):
    store, client = env
    pkg, meta = frozen_package_for_scene(store, db.session(), _plan())
    assert pkg.book_id == "WK-ALPHA", "policy.book_id 由 plan 构造——不跨书"
    bad = pkg.model_copy(update={"book_id": "WK-OTHER"})
    with pytest.raises(RuntimeFault, match="knowledge_scope_conflict"):
        SceneRunner(store, client).run(
            _plan(), bad, Budget()), "validate_plan 跨书双闸"


def test_manual_mode_and_receipts_untouched(env):
    store, client = env
    pkg, _ = frozen_package_for_scene(store, db.session(), _plan())
    SceneRunner(store, client).run(_plan(), pkg, Budget())
    export = store.export("WK-ALPHA")
    assert len(export) == 1 and export[0]["text"], "既有收据路径可读"
    # 旧手选模式：直接传手选包仍可跑（不重写任何旧收据）
    manual = KnowledgePackage(package_id="manual-1", book_id="WK-ALPHA",
                              source_kind="genome_snapshot",
                              techniques=[])
    plan2 = _plan(idem="k3b-manual").model_copy(
        update={"expected_revision": 1, "idempotency_key": "k3b-manual",
                "scene_id": "s2",
                "events": [PlannedEvent(event_id="pay2", description="再支付",
                           changes=[Change(fact="coins", before=2, after=1)])]})
    r = SceneRunner(store, client).run(plan2, manual, Budget())
    assert r["status"] == "committed"
    assert store.audit("WK-ALPHA")["ok"] is True
