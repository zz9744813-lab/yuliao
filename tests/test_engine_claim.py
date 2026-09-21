"""A07 回归：同一实验并发运行不许重复执行阶段（审查 20260920-1810）。

事故（审查复现口径）：两个线程对同一实验的同一阶段调用 engine.run，
通过屏障控制顺序后，**阶段体执行 2 次、两边都返回成功**——API 的
「检查 running」和后台线程的「写 running」不在一个原子领取操作内，
CLI 与 API 同时启动、双请求竞争都读到「阶段尚未完成」；真实阶段则
重复生成与计费。

修复契约（监督 2026-09-21 口径）：
1. **数据库条件更新领取执行权**：_claim_run 原子 UPDATE——只有把
   status 翻成 running 的那一个赢；owner/lease 随领取写入；
2. **提交阶段结果时校验持有权**：阶段体执行前 run_owner 必须仍是
   自己的 token；中途失去持有权的 runner 停止提交、不收尾别人的 run；
3. UI 禁用按钮 / 单纯读 status 不能替代领取事务（api.py 快路径只省
   线程，不承担闸）；
4. 卡死恢复显式（release_run / CLI --release）：无自动 TTL——数小时
   级长跑中途被误抢就是 A07 换姿势重演。

验收（监督口径「N 次 HTTP 尝试 = N 条记录」的引擎镜像）：并发双线程
→ 阶段体恰好执行 1 次，输家拿 already_running。
"""
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.engine as engine                 # noqa: E402
from app import db                          # noqa: E402
from app.models import Experiment           # noqa: E402

CALLS: list = []


@pytest.fixture(autouse=True)
def _clean_calls():
    """CALLS 是模块级计数器——每个用例前清零，不许跨用例累积。"""
    CALLS.clear()
    yield
    CALLS.clear()


def _seed(exp_id, *, status="created", config=None, stats=None, frozen=False):
    db.init_db()
    with db.session() as s:
        s.query(Experiment).filter_by(id=exp_id).delete()
        s.add(Experiment(id=exp_id, name="t-a07", status=status,
                        config=(config or {}) | ({"frozen": True} if frozen else {}),
                        stats=stats or {}))
        s.commit()


def _fake_execute_factory(monkeypatch, *, delay=0.0, steal_after=False, block_event=None):
    """替身阶段体：计数 + 写 done 记录（模拟真实 _execute 的提交行为）。

    steal_after=True：本趟把 run_owner 改成别人——模拟持有权被夺，
    验证 run() 的阶段前持有权校验与收尾校验。
    block_event：测试 2 用——让赢家的阶段体停住，主线程去抢执行权。"""
    def fake(s, exp, name):
        CALLS.append(name)
        if block_event is not None:
            block_event.wait(timeout=10)
        eng = dict(exp.stats or {}).get("engine") or {}
        stg = dict(eng.get("stages") or {})
        stg[name] = {"status": "done", "attempted": 1, "ok": 1, "failed": 0,
                    "skipped": 0, "tokens": 0, "seconds": 0}
        eng["stages"] = stg
        exp.stats = {**(exp.stats or {}), "engine": eng}
        if steal_after:
            exp.run_owner = "RUN-other"     # 模拟中途被夺权（release 后被别人领走）
        s.commit()
        if delay:
            time.sleep(delay)
    monkeypatch.setattr(engine, "_execute", fake)


def test_concurrent_runs_execute_stage_once(monkeypatch):
    """审查复现主案（修复后口径）：双线程屏障同时起跑——阶段体恰好执行
    1 次（旧实现 2 次、两边都成功），输家返回 already_running。"""
    _seed("EXP-A07C1")
    _fake_execute_factory(monkeypatch, delay=0.3)   # 放大竞争窗口
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait()
        results.append(engine.run("EXP-A07C1", ["plan"]))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert CALLS.count("plan") == 1, \
        f"阶段体必须恰好执行 1 次（旧事故=2 次重复生成重复计费），实跑 {CALLS}"
    losers = [r for r in results if r.get("status") == "already_running"]
    assert len(losers) == 1, "竞争输家必须拿到 already_running，而不是再跑一遍"
    assert "原子" in losers[0].get("note", ""), "拒收理由要点明是领取竞争"
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C1").status == "done"


def test_second_run_while_holding_returns_already_running(monkeypatch):
    """赢家阶段体在跑时，第二个 run 直接输——不碰阶段体。"""
    _seed("EXP-A07C2")
    block = threading.Event()
    _fake_execute_factory(monkeypatch, block_event=block)
    winner_result: list = []
    t = threading.Thread(target=lambda: winner_result.append(
        engine.run("EXP-A07C2", ["plan"])))
    t.start()
    time.sleep(0.4)                     # 等赢家领到执行权、阶段体停在 block
    second = engine.run("EXP-A07C2", ["plan"])
    assert second.get("status") == "already_running"
    assert CALLS.count("plan") == 1
    block.set()
    t.join()
    assert winner_result[0].get("stages"), "赢家正常拿到阶段结果"
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C2").status == "done"


def test_lost_ownership_blocks_stage_and_finalize(monkeypatch):
    """阶段提交校验持有权：中途被夺权的 runner——下一阶段立即停、
    finally 不许替新主人收尾（status 留在 running，不写成 done/failed）。"""
    _seed("EXP-A07C3")
    _fake_execute_factory(monkeypatch, steal_after=True)
    with pytest.raises(engine.EngineError, match="失去运行持有权"):
        engine.run("EXP-A07C3", ["plan", "source_check"])
    assert CALLS == ["plan"], "夺权后 source_check 的阶段体绝不能执行"
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07C3")
        assert exp.status == "running", \
            "失去持有权的 runner 不许把别人的 run 收尾成 done/failed"
        assert exp.run_owner == "RUN-other"


def test_release_stuck_then_reclaim(monkeypatch):
    """卡死恢复显式：running 挡住新 run → release_run 放行 → 重新领取执行。"""
    _seed("EXP-A07C4", status="running")          # 模拟 runner 已死、状态卡住
    blocked = engine.run("EXP-A07C4", ["plan"])
    assert blocked.get("status") == "already_running", "卡死的 running 必须挡住新 run"
    out = engine.release_run("EXP-A07C4")
    assert out["released"] is True
    _fake_execute_factory(monkeypatch)
    result = engine.run("EXP-A07C4", ["plan"])
    assert result.get("stages"), "release 后可重新领取并执行"
    assert CALLS.count("plan") == 1
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C4").status == "done"
    # 非 running 态 release：无操作（幂等）
    assert engine.release_run("EXP-A07C4")["released"] is False


def test_frozen_blocks_before_claim(monkeypatch):
    """冻结实验：领取前就拒——status 不许被翻成 running。"""
    _seed("EXP-A07C5", frozen=True)
    _fake_execute_factory(monkeypatch)
    with pytest.raises(engine.EngineError, match="已冻结"):
        engine.run("EXP-A07C5", ["plan"])
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07C5")
        assert exp.status == "created" and exp.run_owner is None
    assert CALLS == []


def test_done_stage_skipped_not_reexecuted(monkeypatch):
    """阶段级幂等不回归：stats 已 done 的阶段跳过，不重复执行。"""
    _seed("EXP-A07C6", stats={"engine": {"stages": {
        "plan": {"status": "done"}}}})
    _fake_execute_factory(monkeypatch)
    engine.run("EXP-A07C6", ["plan"])
    assert CALLS == [], "已 done 的阶段不许再执行（续跑幂等）"
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C6").status == "done"
