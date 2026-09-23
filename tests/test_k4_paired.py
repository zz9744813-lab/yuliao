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


def test_budget_default_pinned():
    """K5-A §4 前提钉死：运行口径 Budget.max_calls=6（契约默认值）——
    10 场最坏 120 调用、止损 800 万的自洽推导全部依赖它，漂移即红。"""
    assert Budget().max_calls == 6


def test_scenes_for_pins_default_and_extension():
    """--scenes 派生（纯函数）：n<=3 恒等于 SCENES 前缀（默认口径零
    改动）；n=10 时 10 条、id/幂等键全唯一、rev 逐场递增、扩展场
    （s4+）转 received 且 before 与世界实际状态精确匹配；n<1 拒。"""
    base = k4.scenes_for(3)
    assert [(sp["scene_id"], sp["rev"], sp["before"], sp["after"],
             sp["idem"], sp["fact"]) for sp in base] == \
        [(s, r, b, a, i, "coins") for (s, r, b, a, i) in k4.SCENES]
    assert k4.scenes_for(1) == [base[0]]
    with pytest.raises(ValueError):
        k4.scenes_for(0)
    ten = k4.scenes_for(10)
    assert len(ten) == 10
    assert [sp["scene_id"] for sp in ten] == [f"s{i}" for i in range(1, 11)]
    assert len({sp["idem"] for sp in ten}) == 10
    assert [sp["rev"] for sp in ten] == list(range(10))
    for sp in ten[3:]:
        n = int(sp["scene_id"][1:])
        assert sp["fact"] == "received"
        assert sp["before"] == n - 4 and sp["after"] == n - 3


def test_run_paired_ten_scenes_offline(tmp_path):
    """10 场 × 2 臂离线端到端：20 份正文/20 张收据/10 个 A 臂冻结包；
    收据 usage 三键恒在（tokens 缺记=0——§6 止损命令不因缺键空转）；
    分析行数=10。默认 3 场口径不受影响（SCENES 常量原样）。"""
    seed_knowledge()
    dirs = {"n": 0}

    def factory():
        d = tmp_path / f"arm{dirs['n']}"; dirs["n"] += 1
        store = Store(d / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    with db.session() as s:
        four = k4.run_paired(factory, k4.FxClient(), s, live=False,
                             n_scenes=10)
    assert not four["failures"], four["failures"]
    assert len(four["prose"]) == 20 and len(four["receipts"]) == 20
    assert len(four["packages"]) == 10
    assert {p["scene"] for p in four["prose"]} == \
        {f"s{i}" for i in range(1, 11)}
    assert all({"calls", "duration_ms", "tokens"} <= set(r["usage"])
               and r["usage"]["tokens"] >= 0 for r in four["receipts"]), \
        four["receipts"]
    assert all(p["n_techniques"] >= 0 for p in four["packages"])
    an = k4.paired_analysis(four, k4.scenes_for(10))
    assert len(an["rows"]) == 10 and an["n_failures"] == 0


def test_scene_budget_gate_fires_not_silent(tmp_path):
    """③ 预算闸：改稿循环烧穿 max_calls → RuntimeFault(call_budget_
    exhausted) 必抛——预算耗尽不可能静默成功（驱动器 except→failures
    落账不吞已由 bridge_boom 用例钉住，本用例补「闸真的会触发」）。"""
    seed_knowledge()
    store = _mk_store(tmp_path)
    scene_id, rev, before, after, idem = k4.SCENES[0]
    plan = k4.build_plan(scene_id, rev, before, after, idem)
    pkg = k4.KnowledgePackage(package_id="empty-x", book_id=plan.book_id,
                              source_kind="empty", techniques=[])

    class _NeverPassVerify:
        models = {"writer": "fx", "verifier": "fx", "transport": "fixture"}

        def invoke(self, *, role, system, payload, max_tokens, timeout):
            body = ({"text": "林穗把一枚钱放在桌上。"} if role == "writer"
                    else {"issues": ["事件未在正文发生"]})
            return {"text": json.dumps(body, ensure_ascii=False),
                    "tokens_in": 1, "tokens_out": 1, "actual_model": "fx",
                    "finish_reason": "stop"}
    with pytest.raises(RuntimeFault,
                       match="call_budget_exhausted|rewrite_budget_exhausted"):
        SceneRunner(store, _NeverPassVerify()).run(plan, pkg,
                                                   Budget(max_calls=2))


def test_main_ten_scenes_out_and_refusal(tmp_path, monkeypatch):
    """main --scenes 10 --out：产物落盘且含 10 场；再次运行同 --out
    → 拒覆盖（§7 第二道保险对 10 场路径同样生效）；--scenes 0 → 拒。"""
    out = tmp_path / "out_k4_10"
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--scenes", "10", "--out", str(out)])
    k4.main()
    art = json.loads((out / "k4_paired.json").read_text(encoding="utf-8"))
    assert art["live"] is False
    scenes = {p["scene"] for p in art["artifacts"]["prose"]}
    assert scenes == {f"s{i}" for i in range(1, 11)}, scenes
    with pytest.raises(SystemExit, match="已存在"):
        k4.main()
    monkeypatch.setattr(sys, "argv", ["k4", "--scenes", "0"])
    with pytest.raises(SystemExit, match="1"):
        k4.main()


def test_failed_scene_marks_rest_skipped_not_phantom(tmp_path):
    """会审整改（89f779e BLOCK·设计缺陷真修）：断臂即停——本臂某场失败后
    后续场**不执行、不烧调用**，单列 skipped（skipped_after 指向首个失败
    场）；failures 只记真实独立失败（C2/止损台账不被 world_revision_
    conflict 连锁幻影污染）；另一臂独立世界照常跑完。"""
    seed_knowledge()
    dirs = {"n": 0}

    def factory():
        d = tmp_path / f"sk{dirs['n']}"; dirs["n"] += 1
        store = Store(d / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    import unittest.mock as _mock
    # arm A 首场冻结包即炸 → 断臂；arm B 走空包对照，正常跑完 3 场
    with _mock.patch.object(k4, "frozen_package_for_scene",
                             side_effect=RuntimeFault("bridge_boom")):
        with db.session() as s:
            four = k4.run_paired(factory, k4.FxClient(), s, live=False)
    assert len(four["failures"]) == 1, four["failures"]
    f0 = four["failures"][0]
    assert (f0["scene"], f0["arm"]) == ("s1", "A")
    assert f0["error_type"] == "RuntimeFault"
    assert [(sk["scene"], sk["arm"], sk["skipped_after"])
            for sk in four["skipped"]] == \
        [("s2", "A", "s1"), ("s3", "A", "s1")], four["skipped"]
    assert all("未执行" in sk["reason"] for sk in four["skipped"])
    # 断臂不跨臂传染：B 臂 3 场正文/收据齐全
    assert {(p["scene"], p["arm"]) for p in four["prose"]} == \
        {(f"s{i}", "B") for i in (1, 2, 3)}
    assert len(four["receipts"]) == 3
    an = k4.paired_analysis(four)
    assert an["n_failures"] == 1, "分析必须只计真实失败——幻影不入台账"
    # C2/P1 口径=failures 计数：skipped 从不混入 failures
    assert not [f for f in four["failures"]
                if f.get("error_type") == "skipped_after_failure"]


def test_channel_changed_marked_on_receipts(tmp_path):
    """C5 口径（证据 §3.3）：换通道真跑必须自报 channel_changed——收据
    逐条携带；默认 False（同通道基线跑不带），回归钉死两种形态。"""
    seed_knowledge()
    dirs = {"n": 0}

    def factory():
        d = tmp_path / f"cc{dirs['n']}"; dirs["n"] += 1
        store = Store(d / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    with db.session() as s:
        four = k4.run_paired(factory, k4.FxClient(), s, live=False,
                            channel_changed=True)
    assert len(four["receipts"]) == 6 and \
        all(r["channel_changed"] is True for r in four["receipts"])
    # 主控 K4-健壮化取证件（2026-09-23）：通道/模型/重试留痕逐条携带
    for r in four["receipts"]:
        assert set(r["models"]) == {"writer", "verifier"}
        assert r["retried"] is False and \
            r["usage"]["verifier_invalid_retries"] == 0
        assert isinstance(r["verifier_attempts"], list) and \
            all(a["stage"].startswith("verifier") for a in r["verifier_attempts"])
        assert r["gateway_host"] == ""      # 离线：无网关主机
    with db.session() as s:
        four2 = k4.run_paired(factory, k4.FxClient(), s, live=False)
    assert all(r["channel_changed"] is False for r in four2["receipts"]), \
        "默认（同通道基线）不得带 channel_changed=true"


def test_worlds_dir_recorded_in_output(tmp_path, monkeypatch):
    """审计非阻断项收口：worlds_dir 记入产物（live 留库作收据——tokens
    对账要读 arm*/k4.sqlite 的 calls 表）；离线跑也记录（可诊断、可清理）。"""
    out_dir = tmp_path / "out"
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--out", str(out_dir)])
    k4.main()
    art = json.loads((out_dir / "k4_paired.json").read_text(encoding="utf-8"))
    assert Path(art["worlds_dir"]).is_dir() or not art["live"], art["worlds_dir"]
    assert "worlds_dir" in art and art["live"] is False


def test_main_live_refused_when_lock_held(monkeypatch):
    """R6 接线回归 + 顺序钉：锁被持有时 --live 必须**守卫先抛**
    SystemExit(互斥守卫)，GatewayClient 构造与 run_paired 零触达。
    与环境无关（2026-09-23 实测教训：干净 worktree 无 .env 时旧顺序
    GatewayClient(...) 构造先抛 gateway_not_configured，抢掉守卫的
    SystemExit——本用例把构造换成计数替身：一旦被触达即 AssertionError
    即红，等于钉死「即使网关未配置/构造必炸，锁在也必须守卫先拒」，
    不再依赖本机是否配了网关）。"""
    from app import live_guard as LG
    import app.scene_runtime.client as client_mod
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")   # live 前提（结果与 mock/real 无关）
    built = {"client": 0, "run": 0}

    class _BoomClient:
        def __init__(self, *a, **k):
            built["client"] += 1
            raise AssertionError("锁被持有时不许构造 GatewayClient——守卫必须前置于构造")

    def _boom(*a, **k):
        built["run"] += 1
        raise AssertionError("锁被持有时 run_paired 不许被调用")
    monkeypatch.setattr(client_mod, "GatewayClient", _BoomClient)
    monkeypatch.setattr(k4, "run_paired", _boom)
    monkeypatch.setattr(sys, "argv", ["k4", "--live",
                                      "--writer-model", "a",
                                      "--verifier-model", "b"])
    with LG.live_lock("t-other-live"):
        with pytest.raises(SystemExit, match="互斥守卫"):
            k4.main()
    assert built == {"client": 0, "run": 0}, \
        f"守卫未前置：客户端构造 {built['client']} 次、run_paired {built['run']} 次"
