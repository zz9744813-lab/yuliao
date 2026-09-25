"""简报 C 选项代价数字的离线钉子（只读真库、零模型、零写库）。

2026-09-25 在基线 c1d045b 的真库只读实测：原始 C 场景下，覆汉
WK-dc90993434e9 为 1 张非空包，琼明神女录 WK-6e5d2623 为 1 张非空包，
合计 2 张；两张都引用策略 ESV2-0450910b989f 的同一条证据，根作品均为
WK-6e5d2623。若上游按“查询作品 allowed_purposes 为空则不得出包”收紧，
覆汉 1→0 且归零，琼明 1→1 且不受影响，合计 2→1。

这里固定的是简报中的反事实代价算术，不把当前 query_knowledge 描述成已经
执行用途闸：原始选择与收紧后的有效张数都来自同一次只读 C 查询，后者只按
查询作品的 allowed_purposes 做包级准入掩码。测试不修改登记行，也不 flush
或提交任何对象；引擎回读 mode_ro=True、PRAGMA query_only=1。

反向自检（两次均用同一命令）：
- 临时把钉子②的收紧后期望 1 改成 0：
  `F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_brief_cost_numbers.py -q`
  → exit 1，`.F.`，1 failed / 2 passed；失败为 `assert 1 == 0`。
- 恢复期望 1 后重跑同一命令 → exit 0，`...`，3 passed。
未自跑清单：无。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "brief_cost_k2_unlock_sim", ROOT / "scripts" / "k2_unlock_sim.py")
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

REAL_DB = Path("F:/agi/language-genome/data/language_genome.db")
FUHAN = "WK-dc90993434e9"
QIONGMING = "WK-6e5d2623"
STRATEGY_ID = "ESV2-0450910b989f"
C_SPEC = {
    "status": "verified",
    "scope": "WORK",
    "scope_ids": ["<BOOK>"],
    "scope_basis": sim.SIM_SCOPE_BASIS,
}


def _measure_c_cost(session, strategies, snap, book: str) -> dict:
    sim.apply_scenario(session, strategies, C_SPEC, book)
    try:
        response = sim.KQ.query_knowledge(sim.sim_policy(book), session)
        selected = list(response.get("selected") or [])
        registry = session.query(sim.WorkSource).filter_by(work_id=book).one()
        purposes = list(registry.allowed_purposes or [])
        admitted = bool(selected) and bool(purposes)
        return {
            "allowed_purposes": tuple(purposes),
            "raw_package_count": int(bool(selected)),
            "raw_status": response.get("status"),
            "package_count_after_tightening": int(admitted),
            "zero_after_tightening": not admitted,
            "selected_ids": tuple(entry.get("strategy_id") for entry in selected),
            "evidence_root_works": tuple(
                tuple(entry.get("evidence_root_works") or ())
                for entry in selected
            ),
        }
    finally:
        sim.restore(session, strategies, snap)


@pytest.fixture(scope="module")
def c_cost_observation() -> dict:
    try:
        if not REAL_DB.is_file():
            pytest.skip(f"真库缺失或不可读：{REAL_DB}")
        engine, info = sim.build_readonly_engine(REAL_DB)
    except (OSError, sim.SimError, SQLAlchemyError) as exc:
        pytest.skip(f"真库缺失或不可读：{REAL_DB}（{type(exc).__name__}: {exc}）")

    session = None
    strategies = []
    snap = None
    try:
        assert info["mode_ro"] is True
        assert "mode=ro" in info["dbapi_uri"]
        assert info["query_only"] == 1
        session = sim.session_for(engine)
        strategies = sim.all_strategies(session)
        if not strategies:
            pytest.skip(f"真库不可用于本场景：{REAL_DB} 中没有可仿真策略")
        snap = sim.snapshot(strategies)
        return {
            "fuhan": _measure_c_cost(session, strategies, snap, FUHAN),
            "qiongming": _measure_c_cost(session, strategies, snap, QIONGMING),
        }
    except (OSError, sim.SimError, SQLAlchemyError) as exc:
        pytest.skip(f"真库缺失或不可读：{REAL_DB}（{type(exc).__name__}: {exc}）")
    finally:
        if session is not None:
            if strategies and snap is not None:
                sim.restore(session, strategies, snap)
            session.close()
        engine.dispose()


def test_fuhan_zero_allowed_purposes_drops_its_c_package(c_cost_observation):
    observed = c_cost_observation["fuhan"]

    assert observed["allowed_purposes"] == ()
    assert observed["raw_status"] == "matched"
    assert observed["raw_package_count"] == 1
    assert observed["package_count_after_tightening"] == 0
    assert observed["zero_after_tightening"] is True


def test_qiongming_package_survives_fuhan_zero_authorization(c_cost_observation):
    observed = c_cost_observation["qiongming"]

    assert observed["allowed_purposes"] == (
        "research", "training_source", "benchmark_source")
    assert observed["raw_status"] == "matched"
    assert observed["raw_package_count"] == 1
    assert observed["package_count_after_tightening"] == 1
    assert observed["zero_after_tightening"] is False
    assert observed["selected_ids"] == (STRATEGY_ID,)
    assert observed["evidence_root_works"] == ((QIONGMING,),)


def test_c_nonempty_package_count_changes_from_two_to_one(c_cost_observation):
    before = (
        c_cost_observation["fuhan"]["raw_package_count"]
        + c_cost_observation["qiongming"]["raw_package_count"]
    )
    after = (
        c_cost_observation["fuhan"]["package_count_after_tightening"]
        + c_cost_observation["qiongming"]["package_count_after_tightening"]
    )

    assert before == 2
    assert after == 1
    assert c_cost_observation["fuhan"]["raw_package_count"] == 1
    assert c_cost_observation["fuhan"]["package_count_after_tightening"] == 0
    assert c_cost_observation["qiongming"]["raw_package_count"] == 1
    assert c_cost_observation["qiongming"]["package_count_after_tightening"] == 1
