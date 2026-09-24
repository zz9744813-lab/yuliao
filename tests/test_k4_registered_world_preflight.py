"""K4 登记世界 + 只读预检回归（审计 P0 主线第 2 条，2026-09-24）。

验收点：
① 未登记 book_id → 预检非零退出（夹具库）；
② 已登记 book_id + 空包 → 预检非零退出，且「没有发生任何真实调用」（假 client 计数断言 0）；
③ 已登记 book_id + 非空包（夹具里塞一条 verified 策略 + 合格证据）→ 预检通过并给
   n_techniques 真值；
④ 默认路径回归：不传 --book-id 时 build_world()/build_plan() 产物与改动前逐字一致；
⑤ --live 预检闸（真实模式生效）：世界未登记/空包 → 拒绝起跑、零真实调用。

纪律：默认（离线夹具）路径与既有 test_k4_paired.py 全绿；preflight 只读、零生成调用。
"""
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
                        ExpressionStrategyV2, StrategyCondition,
                        StrategyInstance, Segment)
import knowledge_seed as KS                            # noqa: E402


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
        s.flush()
        if author_id is not None:
            ws0.author_id = author_id
            s.flush()
        return wid
    s.add(WorkSource(work_id=wid, canonical_work_id=wid, source_type=source_type,
                     text_version="corpus-v1", purpose_basis="test",
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


# ── ③ 已登记 book_id + 非空包 → 预检通过，给 n_techniques 真值 ───────────────

def test_preflight_registered_nonempty_package_passes(monkeypatch):
    KS.seed_knowledge()
    with db.session() as s:
        _register_world(s, "WK-REG2", author_id="AUTH-1")  # 匹配 AUTHOR 策略
        _add_guaranteed_verified_strategy(s, "WK-REG2")     # 一条 verified + 实例
        s.commit()   # 主控修：同上——跨 Session 可见性必须落库
    # 函数层：matched + n_techniques 真值
    with db.session() as s:
        pre = k4.preflight_world("WK-REG2", s)
    assert pre["registered"] is True
    assert pre["k3_status"] == "matched", pre
    assert pre["n_techniques"] == len(pre["selected_ids"]) > 0, pre
    assert pre["empty_reason"] is None
    # CLI 层：通过、不抛 SystemExit
    monkeypatch.setattr(sys, "argv",
                        ["k4", "--preflight", "--book-id", "WK-REG2"])
    k4.main()                                      # 不抛即过闸


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


# ── ⑤ --live 预检闸（真实模式生效）：未登记/空包 → 拒绝起跑、零真实调用 ──────

def test_live_gate_refuses_unregistered_real_mode(monkeypatch):
    """--live 叠加同一道闸：LLM_MODE=real 时，世界未登记（空包同理）即拒绝起跑，
    非零退出、零真实调用（GatewayClient 不得构造）。mock 模式不触发本闸（由
    test_k4_paired.test_live_double_gate 覆盖 GatewayClient 拒构）。"""
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
