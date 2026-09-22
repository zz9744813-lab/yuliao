"""K4-A 离线预置回归（零配额）：配对驱动端到端 + 卡驱动（期望手写）。

验收点：预算超限拒绝 / 输出门不完整拒绝 / 跨书与幂等冲突不得静默通过；
四类产物结构（prose/packages/receipts/failures）；分析不判质量。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util as _u
_spec = _u.spec_from_file_location("k4", ROOT / "scripts" / "k4_paired_scenes.py")
k4 = _u.module_from_spec(_spec); _spec.loader.exec_module(k4)

from knowledge_seed import seed_knowledge          # noqa: E402
from app import db, knowledge_extract as KE        # noqa: E402
from app.scene_runtime.contracts import (Budget, KnowledgePackage,  # noqa: E402
                                          RuntimeFault)
from app.scene_runtime.pipeline import SceneRunner  # noqa: E402
from app.scene_runtime.store import Store           # noqa: E402

CARDS = ROOT / "tests" / "paired_cards"
TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"


class _Fx:
    def __init__(self, payload=None, bad=None):
        self.payload, self.bad = payload, bad

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if self.bad == "json":
            return {"text": "x", "tokens_in": 5, "tokens_out": 5,
                    "actual_model": "fx"}
        body = self.payload if self.payload is not None else {
            "span_start": 0, "span_end": 8,
            "evidence_text": TEXT[0:8], "observed_content": "沉默"}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 15, "actual_model": "fx",
                "finish_reason": "stop"}


def _mk_store(tmp_path, book_id="WK-K4"):
    store = Store(tmp_path / "k4.sqlite")
    store.create_world(k4.build_world().model_copy(
        update={"book_id": book_id}))
    return store


def test_offline_paired_e2e(tmp_path):
    seed_knowledge()
    dirs = {"n": 0}

    def factory():
        d = tmp_path / f"arm{dirs['n']}"; dirs["n"] += 1
        store = Store(d / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    with db.session() as s:
        four = k4.run_paired(factory, k4.FxClient(), s, live=False)
    assert not four["failures"], four["failures"]
    assert len(four["prose"]) == 6, "3 场 × 2 臂 = 6 份正文"
    assert len(four["packages"]) == 3, "每场一个 A 臂冻结包"
    assert all(p["n_techniques"] >= 0 for p in four["packages"])
    assert len(four["receipts"]) == 6 and all(
        r["usage"] or r["live"] is False for r in four["receipts"])
    an = k4.paired_analysis(four)
    assert len(an["rows"]) == 3 and an["n_failures"] == 0
    assert "不判质量" in an["quality_verdict"], "配对分析不许判质量"


def _run_card_extract(card):
    b = KE.ExtractBudget(max_calls=card["input"]["max_calls"],
                         max_tokens=card["input"].get("max_tokens", 50_000))
    payload = card["input"].get("payload")
    if card["input"].get("max_tokens") == 25:
        b.spend(15, 10)                     # 先耗掉大半 token 预算
    try:
        r = KE.extract_segment(_Fx(payload), strategy_id="A",
                               strategy_version=1, work_id="W",
                               segment_id="S", text=TEXT,
                               text_version="v", budget=b)
        # 预算卡（dev-01 max_calls=1）要抽第二次触发闸——第一次会成功
        if card["input"]["max_calls"] == 1:
            KE.extract_segment(_Fx(payload), strategy_id="A",
                              strategy_version=1, work_id="W",
                              segment_id="S", text=TEXT,
                              text_version="v", budget=b)
        return {"status": r.get("status"), "error": None}
    except KE.ExtractBudgetExceeded as e:
        return {"status": "blocked", "error": str(e)}


def _run_card_scene(card, tmp_path):
    seed_knowledge()
    store = _mk_store(tmp_path)
    scene_id, rev, before, after, idem = k4.SCENES[0]
    plan = k4.build_plan(scene_id, rev, before, after, idem)
    if card["input"].get("bad_book"):
        plan = plan.model_copy(update={"book_id": "WK-OTHER"})
    if card["input"].get("second_goal"):
        with db.session() as s:
            pkg, _ = k4.frozen_package_for_scene(store, s, plan)
        SceneRunner(store, k4.FxClient()).run(plan, pkg, Budget())
        plan = plan.model_copy(update={"goal": card["input"]["second_goal"]})
    try:
        with db.session() as s:
            pkg, _ = k4.frozen_package_for_scene(store, s, plan)
        r = SceneRunner(store, k4.FxClient()).run(plan, pkg, Budget())
        return {"status": r["status"], "error": None}
    except RuntimeFault as e:
        return {"status": "failed", "error": str(e)}


def _cards():
    out = []
    for sub in ("dev", "acceptance"):
        for p in sorted((CARDS / sub).glob("*.json")):
            out.append(pytest.param(json.loads(
                p.read_text(encoding="utf-8")), id=f"{sub}-{p.stem}"))
    return out


@pytest.mark.parametrize("card", _cards())
def test_card(card, tmp_path):
    exp = card["expected"]
    if card["kind"] == "extract":
        got = _run_card_extract(card)
    else:
        got = _run_card_scene(card, tmp_path)
    if exp.get("error_contains"):
        assert exp["error_contains"] in (got["error"] or ""), got
    if "status" in exp:
        assert got["status"] == exp["status"], got
    if exp.get("not_silent"):
        assert got["error"], "冲突不许静默通过"


def test_live_guard_requires_real_mode():
    with pytest.raises(RuntimeError, match="live_client_requires_real_mode|"
                       "module 'app.scene_runtime.client'"):
        from app.scene_runtime.client import GatewayClient
        GatewayClient("a", "b")     # LLM_MODE=mock（测试环境）→ 显式拒
