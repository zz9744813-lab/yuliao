"""K4 登记世界 + 只读预检回归（审计 P0 主线第 2 条，2026-09-24）。

验收点：
① 未登记 book_id → 预检非零退出（夹具库）；
② 已登记 book_id + 空包 → 预检非零退出，且「没有发生任何真实调用」（假 client 计数断言 0）；
③ 已登记 book_id + 非空包（夹具里塞一条 verified 策略 + 合格证据）→
   如实给 n_techniques，但语义审查收据不可核验，仍拒绝起跑；
④ 默认路径回归：不传 --book-id 时 build_world()/build_plan() 产物与改动前逐字一致；
⑤ --live 预检闸（真实模式生效）：世界未登记/空包 → 拒绝起跑、零真实调用。
⑥ 非默认 --book-id 离线运行：世界创建与六份计划都使用同一本书，不回落 WK-K4。
⑦ 即使夹具里有两席 PASS，旧 strategy_reviews 无证据指纹/晋升审计绑定，
   --preflight 与真实模式 --live 都 fail-closed，GatewayClient 构造为零。

纪律：默认（离线夹具）路径与既有 test_k4_paired.py 全绿；preflight 只读、零生成调用。
"""
import json
import sys
from pathlib import Path

import pytest
import importlib.util as _u

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

_spec = _u.spec_from_file_location("k4", ROOT / "scripts" / "k4_paired_scenes.py")
k4 = _u.module_from_spec(_spec)
_spec.loader.exec_module(k4)

from app import db                                     # noqa: E402
from app.models import (Work, WorkSource,              # noqa: E402
                        ExpressionStrategyV2, StrategyCondition, StrategyReview,
                        StrategyInstance, Segment)
import knowledge_seed as KS                            # noqa: E402
from registry_anchor import anchor as _anchor          # noqa: E402  登记行内容锚同源


def _register_world(s, wid, *, author_id=None,
                    source_type="human_fiction") -> str:
    """登记一个世界（WorkSource 一行）。默认 human_fiction，可指定 author_id
    以匹配 AUTHOR 范围策略。"""
    # 主控修：同文件多测共享一个测试库，重复 id 会 IntegrityError（顺序依赖）。
    # 幂等化：已存在则复用，不再 INSERT（只补/更新 WorkSource 登记行）。
    w = s.query(Work).filter_by(id=wid).first()
    if w is None:
        s.add(Work(id=wid, title=wid, source="test:k4_preflight"))
        s.flush()
    ws0 = s.query(WorkSource).filter_by(work_id=wid).first()
    if ws0 is not None:
        ws0.source_type = source_type
        ws0.text_sha256 = _anchor(s, wid)     # 复用行也要带锚（同源）
        s.flush()
        if author_id is not None:
            ws0.author_id = author_id
            s.flush()
        return wid
    s.add(WorkSource(work_id=wid, canonical_work_id=wid, source_type=source_type,
                     text_version="corpus-v1", text_sha256=_anchor(s, wid),
                     purpose_basis="test",
                     identity_purposes=["research"], license_purposes=[],
                     license_basis=None, metadata_status="verified",
                     metadata_basis="test"))
    s.flush()
    if author_id is not None:
        # 直接补 author_id（WorkSource.author_id 可空，这里显式给以匹配 AUTHOR 策略）
        ws = s.query(WorkSource).filter_by(work_id=wid).first()
        ws.author_id = author_id
        s.flush()
    return wid


def _clear_strategies(s) -> None:
    """清空全库策略/条件/实例——用于「空包」场景的确定性（不依赖其他测试残留）。"""
    s.query(StrategyReview).delete(synchronize_session=False)
    s.query(StrategyInstance).delete(synchronize_session=False)
    s.query(StrategyCondition).delete(synchronize_session=False)
    s.query(ExpressionStrategyV2).delete(synchronize_session=False)
    s.flush()


def _add_guaranteed_verified_strategy(s, wid) -> None:
    """塞一条 AUTHOR 范围、无条件的 verified 策略 + 合格实例——保证 query_knowledge
    对该 world 返回 matched（不依赖 seed_knowledge 的匹配细节）。"""
    seg = s.query(Segment).filter_by(work_id="WK-α").first()
    s.add(ExpressionStrategyV2(
        id="K4T-MATCH", strategy_key="K4T-匹配", version=1,
        abstract_operation="K4 测试抽象操作", invariants=["不改动事实"],
        effect_hypothesis="对照任务", failure_modes=[], status="verified",
        source="test", scope="AUTHOR", scope_ids=["AUTH-1"],
        scope_basis="test", observation_status="observed",
        effect_status="pilot_verified"))
    s.add(StrategyInstance(
        id="K4T-SI", strategy_id="K4T-MATCH", strategy_version=1,
        work_id=wid, segment_id=seg.id, frame_id=None,
        text_version="corpus-v1", span_start=0, span_end=10,
        evidence_text="测试证据", evidence_sha256="0" * 64,
        conditions_observed={}, observed_content="测试",
        extractor_model="test", status="verified"))
    s.flush()


def _seed_matched_world(wid, *, two_pass=False):
    KS.seed_knowledge()
    with db.session() as s:
        _clear_strategies(s)
        _register_world(s, wid, author_id="AUTH-1")
        _add_guaranteed_verified_strategy(s, wid)
        if two_pass:
            for model in ("reviewer-one", "reviewer-two"):
                s.add(StrategyReview(
                    strategy_id="K4T-MATCH", judge_kind="semantic_card_v2",
                    reviewer_model=model, verdict="PASS",
                    evidence_support=1, distinct_flag=1))
        s.commit()


# ── ① 未登记 book_id → 预检非零退出 ────────────────────────────────────────

def test_preflight_unregistered_book_id_exits_nonzero(monkeypatch):
    KS.seed_knowledge()          # 登记 WK-α 等，但无 WK-UNREG
    with db.session() as s:
        pre = k4.preflight_world("WK-UNREG", s)
    assert pre["registered"] is False
    assert pre["empty_reason"] and "world_not_registered" in pre["empty_reason"]
    # CLI 形态：非零退出
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--preflight", "--book-id", "WK-UNREG"])
    with pytest.raises(SystemExit):
        k4.main()


# ── ② 已登记 book_id + 空包 → 预检非零退出，且没有任何真实调用 ────────────────

def test_preflight_registered_but_empty_package_exits_zero_calls(monkeypatch):
    db.init_db()
    with db.session() as s:
        _clear_strategies(s)                 # 无任何 verified 策略 ⇒ 空包
        _register_world(s, "WK-REG")         # 已登记但 A 臂空
        s.commit()   # 主控修：不 commit 则本行对 preflight 的新会话不可见
                     # （db.session() 每次新开 Session，旧行只 flush 未落库 →
                     #  registered 恒 False，测试假红/假绿）
    # 假 client 计数：preflight 只读、绝不触真客户端
    calls = {"fx": 0, "gw": 0}

    class _CountFx:
        models = {"writer": "fx", "verifier": "fx", "transport": "fixture"}

        def invoke(self, *, role, system, payload, max_tokens, timeout):
            calls["fx"] += 1
            return {"text": "{}", "tokens_in": 0, "tokens_out": 0,
                    "actual_model": "fx"}

    import app.scene_runtime.client as _cm

    class _BoomGateway:
        def __init__(self, *a, **k):
            calls["gw"] += 1
            raise AssertionError("预检未过不得构造 GatewayClient")

    monkeypatch.setattr(k4, "FxClient", _CountFx)
    monkeypatch.setattr(_cm, "GatewayClient", _BoomGateway)
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--preflight", "--book-id", "WK-REG"])
    with pytest.raises(SystemExit):
        k4.main()
    # 关键纪律：零真实调用（preflight 只跑只读 query_knowledge / WorkSource 查询）
    assert calls["fx"] == 0, "预检不得产生任何 FxClient 调用"
    assert calls["gw"] == 0, "预检不得构造 GatewayClient"
    # 且字典层也确认空包根因（无 verified 策略）
    with db.session() as s:
        pre = k4.preflight_world("WK-REG", s)
    assert pre["registered"] is True
    assert pre["k3_status"] != "matched"
    assert pre["empty_reason"] and "empty_package" in pre["empty_reason"]


# ── ③ 已登记 + 非空包，但语义审查不可核验 → 拒绝起跑 ───────────────────────

def test_preflight_registered_nonempty_package_reports_review_veto(monkeypatch):
    _seed_matched_world("WK-REG2")
    # K3 确实 matched；拒绝原因只能是审查收据不可核验，不得冒充空包。
    with db.session() as s:
        pre = k4.preflight_world("WK-REG2", s)
    assert pre["registered"] is True
    assert pre["k3_status"] == "matched", pre
    assert pre["n_techniques"] == len(pre["selected_ids"]) > 0, pre
    assert pre["empty_reason"] is None
    assert pre["review_status"] == "semantic_review_unverifiable"
    assert pre["ready"] is False
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--preflight", "--book-id", "WK-REG2"])
    with pytest.raises(SystemExit, match="semantic_review_unverifiable"):
        k4.main()


def test_two_pass_reviews_without_evidence_binding_still_refused(monkeypatch):
    """两个不同 reviewer_model 的 PASS 也不能为当前证据背书。"""
    wid = "WK-REG-TWO-PASS"
    _seed_matched_world(wid, two_pass=True)
    with db.session() as s:
        pre = k4.preflight_world(wid, s)
        votes = s.query(StrategyReview).filter_by(strategy_id="K4T-MATCH",
                                                  verdict="PASS").all()
    assert len({v.reviewer_model for v in votes}) == 2
    assert pre["selected_ids"] == ["K4T-MATCH"]
    assert pre["review_status"] == "semantic_review_unverifiable"
    assert pre["ready"] is False
    monkeypatch.setattr(sys, "argv", ["k4", "--preflight", "--book-id", wid])
    with pytest.raises(SystemExit, match="semantic_review_unverifiable"):
        k4.main()


# ── ④ 默认路径回归：不传 --book-id 时产物与改动前逐字一致 ───────────────────

def test_default_build_world_book_id_unchanged():
    w = k4.build_world()
    assert w.book_id == "WK-K4"
    assert w.characters == {"lin": "林穗"}
    assert w.facts["coins"].value == 3 and w.facts["received"].value == 0
    # 显式默认 == 无参（与改动前逐字一致）
    assert k4.build_world("WK-K4").model_dump() == w.model_dump()


def test_default_build_plan_book_id_unchanged():
    s0 = k4.SCENES[0]
    p = k4.build_plan(s0[0], s0[1], s0[2], s0[3], s0[4])
    assert p.book_id == "WK-K4"
    # 既有调用形态（5 位置参，无 book_id）产物不变
    assert k4.build_plan(s0[0], s0[1], s0[2], s0[3],
                          s0[4]).model_dump() == p.model_dump()
    # 显式 book_id="WK-K4" 同物
    assert k4.build_plan(s0[0], s0[1], s0[2], s0[3], s0[4],
                         book_id="WK-K4").model_dump() == p.model_dump()


def test_nondefault_book_id_reaches_world_and_every_plan(monkeypatch, tmp_path):
    """只用 FxClient 离线跑 CLI；同时钉世界工厂与逐臂计划的 book_id。"""
    KS.seed_knowledge()
    book_id = "WK-K4-NONDEFAULT"
    with db.session() as s:
        _register_world(s, book_id)
        s.commit()

    world_ids = []
    plan_ids = []
    build_world = k4.build_world
    build_plan = k4.build_plan

    def track_world(selected_book_id="WK-K4"):
        world_ids.append(selected_book_id)
        return build_world(selected_book_id)

    def track_plan(*args, **kwargs):
        plan_ids.append(kwargs.get("book_id", "WK-K4"))
        return build_plan(*args, **kwargs)

    monkeypatch.setattr(k4, "build_world", track_world)
    monkeypatch.setattr(k4, "build_plan", track_plan)
    out_dir = tmp_path / "nondefault"
    monkeypatch.setattr(sys, "argv", ["k4", "--book-id", book_id,
                                      "--out", str(out_dir)])
    k4.main()

    artifact = json.loads(
        (out_dir / "k4_paired.json").read_text(encoding="utf-8"))
    assert world_ids == [book_id, book_id]
    assert plan_ids == [book_id] * 6
    assert len(artifact["artifacts"]["receipts"]) == 6
    assert not artifact["artifacts"]["failures"]


# ── ⑤ --live 预检闸（真实模式生效）：未登记/空包 → 拒绝起跑、零真实调用 ──────

def test_live_gate_refuses_unregistered_real_mode(monkeypatch, tmp_path):
    """--live 叠加同一道闸：LLM_MODE=real 时，世界未登记（空包同理）即拒绝起跑，
    非零退出、零真实调用（GatewayClient 不得构造）。mock 模式不触发本闸（由
    test_k4_paired.test_live_double_gate 覆盖 GatewayClient 拒构）。"""
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "live-lock"))
    db.init_db()
    with db.session() as s:
        _clear_strategies(s)
        _register_world(s, "WK-REG")     # 登记但无 verified 策略 ⇒ A 臂空
        s.commit()   # 主控修：同上——否则本测只因「未登记」而过，未验空包路径
    calls = {"gw": 0}
    import app.scene_runtime.client as _cm

    class _BoomGateway:
        def __init__(self, *a, **k):
            calls["gw"] += 1
            raise AssertionError("预检未过不得构造 GatewayClient")

    monkeypatch.setattr(_cm, "GatewayClient", _BoomGateway)
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    from app import config as _cfg
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--live", "--book-id", "WK-REG",
                         "--writer-model", "a", "--verifier-model", "b"])
    with pytest.raises(SystemExit, match="preflight"):
        k4.main()
    assert calls["gw"] == 0, "预检未过不得构造 GatewayClient（零真实调用）"


def test_live_review_veto_precedes_gateway_client(monkeypatch, tmp_path):
    """K3 matched 且有两席 PASS，仍因缺当前证据绑定而零客户端构造。"""
    wid = "WK-REG-LIVE-VETO"
    _seed_matched_world(wid, two_pass=True)
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "live-lock"))
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    from app import config as _cfg
    import app.scene_runtime.client as _cm
    monkeypatch.setattr(_cfg, "LLM_MODE", "real")
    built = []

    class _BoomGateway:
        def __init__(self, *args, **kwargs):
            built.append(True)
            raise AssertionError("审查收据不可核验时不许构造 GatewayClient")

    monkeypatch.setattr(_cm, "GatewayClient", _BoomGateway)
    monkeypatch.setattr(sys, "argv", ["k4", "--live", "--book-id", wid,
                                      "--writer-model", "fixture-writer",
                                      "--verifier-model", "fixture-verifier"])
    with pytest.raises(SystemExit, match="semantic_review_unverifiable"):
        k4.main()
    assert built == []
