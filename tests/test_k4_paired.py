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
from app.scene_runtime.contracts import Budget, RuntimeFault  # noqa: E402
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
    for tin, tout in card["input"].get("pre_spend", []):
        b.spend(tin, tout)                   # 卡里显式预算（不耦合魔数）
    fx = _Fx(payload, bad=card["input"].get("bad"))
    try:
        r = KE.extract_segment(fx, strategy_id="A",
                               strategy_version=1, work_id="W",
                               segment_id="S", text=TEXT,
                               text_version="v", budget=b)
        # 预算卡（calls=2）：第二次显式抽取必须抛——第一次成功不算数
        if card["input"].get("calls") == 2:
            with pytest.raises(KE.ExtractBudgetExceeded) as ei:
                KE.extract_segment(fx, strategy_id="A",
                                  strategy_version=1, work_id="W",
                                  segment_id="S", text=TEXT,
                                  text_version="v", budget=b)
            return {"status": "blocked", "error": str(ei.value)}
        return {"status": r.get("status"), "error": None}
    except KE.ExtractBudgetExceeded as e:
        return {"status": "blocked", "error": str(e)}


def _run_card_scene(card, tmp_path):
    seed_knowledge()
    store = _mk_store(tmp_path)
    # 卡可指定场景；不指定才默认 SCENES[0]（会审五轮：卡意图生效）
    want = card["input"].get("scene", k4.SCENES[0][0])
    match = [s for s in k4.SCENES if s[0] == want]
    assert match, f"卡指定的场景不在 SCENES：{want}"
    scene_id, rev, before, after, idem = match[0]
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
    # 严格消费（双侧）：期望与输入里打了不认识的键=拼写错，必须红
    # （会审五轮：input 键拼错曾会被静默忽略）
    handled_exp = {"status", "error_contains", "not_silent"}
    assert set(exp) <= handled_exp, f"卡期望有未处理键：{set(exp) - handled_exp}"
    handled_in = {"extract": {"max_calls", "max_tokens", "pre_spend",
                            "payload", "bad", "calls"},
                 "scene": {"scene", "bad_book", "second_goal"}}
    extra_in = set(card["input"]) - handled_in[card["kind"]]
    assert not extra_in, (f"卡输入有未处理键：{extra_in}——"
                         "驱动器没消费它，期望即失去覆盖力")


def test_live_guard_requires_real_mode():
    with pytest.raises(RuntimeError, match="live_client_requires_real_mode|"
                       "module 'app.scene_runtime.client'"):
        from app.scene_runtime.client import GatewayClient
        GatewayClient("a", "b")     # LLM_MODE=mock（测试环境）→ 显式拒


def test_freeze_false_writes_nothing_to_lg_db(tmp_path):
    """离线零库写：freeze=False 跑完，knowledge_packages 行数不变。"""
    from app.models import KnowledgePackage
    seed_knowledge()
    with db.session() as s:
        before = s.query(KnowledgePackage).count()
    dirs = {"n": 0}

    def factory():
        d = tmp_path / f"z{dirs['n']}"; dirs["n"] += 1
        st = Store(d / "k4.sqlite")
        st.create_world(k4.build_world())
        return st
    with db.session() as s:
        four = k4.run_paired(factory, k4.FxClient(), s, live=False,
                              freeze=False)
    assert not four["failures"], four["failures"]
    with db.session() as s:
        after = s.query(KnowledgePackage).count()
    assert after == before, f"离线跑写了 LG 库：{before}→{after}（纪律违背）"


def test_out_existing_refused(tmp_path, monkeypatch):
    """--out 已存在即拒（脚本实现必须被测试钉住，防静默覆盖）。"""
    d = tmp_path / "already"
    d.mkdir()
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--out", str(d)])
    with pytest.raises(SystemExit, match="已存在"):
        k4.main()


def test_live_double_gate(tmp_path, monkeypatch):
    """--live 双闸：无 K4_ALLOW_LIVE=1 → 拒；有闸但 LLM_MODE=mock → 也拒。
    第二段前提显式钉死（A2：不赌环境恰好 mock）。绑定方式已核实：
    GatewayClient.__init__ 内 `from app import config` 后取
    `config.LLM_MODE`——调用期模块属性访问，setattr 即确定生效；
    守卫在构造函数内先于任何网络构造 raise，「RuntimeFault 抛出」
    本身就是「未发起真实调用」的直接断言。"""
    monkeypatch.delenv("K4_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(sys, "argv", ["k4", "--live",
                                     "--writer-model", "a",
                                     "--verifier-model", "b"])
    with pytest.raises(SystemExit, match="K4_ALLOW_LIVE"):
        k4.main()
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    monkeypatch.setenv("LG_LLM_MODE", "mock")
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "LLM_MODE", "mock")
    assert _cfg.LLM_MODE == "mock"      # 前提钉死，不赌环境默认值
    with pytest.raises(RuntimeFault, match="live_client_requires_real_mode"):
        k4.main()


def test_rollback_failure_recorded_not_swallowed(tmp_path):
    """A1：lg_session.rollback() 自身抛异常——必须并进本臂 failure 记录
    （rollback_failed=True + rollback_error=回滚异常类名）且主异常不被吞、
    不重复记录。"""
    seed_knowledge()
    dirs = {"n": 0}

    def factory():
        d = tmp_path / f"rb{dirs['n']}"; dirs["n"] += 1
        st = Store(d / "k4.sqlite")
        st.create_world(k4.build_world())
        return st

    calls = {"rollback": 0}

    class _BoomRollbackSession:
        def rollback(self):
            calls["rollback"] += 1
            raise RuntimeError("rb-boom")

    import unittest.mock as _mock
    # run_paired 是 from-import 绑定——必须钉 k4 模块自己的名字（A1 教训：
    # 钉源头模块不生效，arm A 实际跑真 bridge 撞 AttributeError）。
    # 失败注入：arm A 首场 frozen_package_for_scene 即抛 → except 分支。
    with _mock.patch.object(k4, "frozen_package_for_scene",
                             side_effect=RuntimeFault("bridge_boom")):
        four = k4.run_paired(factory, k4.FxClient(), _BoomRollbackSession(),
                             live=False, freeze=False)
    assert four["failures"], "主异常必须被记录，不许吞"
    rb = [f for f in four["failures"] if f.get("rollback_failed")]
    assert rb, "「失败且回滚也炸」必须能从收据非空地区分出来"
    assert all(f["error_type"] == "RuntimeFault"
              for f in rb), four["failures"]
    assert all(f.get("rollback_error") == "RuntimeError"
               for f in rb), four["failures"]   # 回滚异常类名随记录携带
    assert calls["rollback"] >= 1, "rollback 确实炸过（前提成立）"
    # 不重：rollback_failed 只并进本臂记录，无独立重复条目
    assert not [f for f in four["failures"]
                if f["error_type"] == "rollback_failed"], four["failures"]
