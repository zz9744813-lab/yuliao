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


def _fake_execute_factory(monkeypatch, *, delay=0.0, steal_after=False,
                         block_event=None, started_event=None):
    """替身阶段体：计数 + 写 done 记录（模拟真实 _execute 的提交行为）。

    steal_after=True：本趟把 run_owner 改成别人——单元级模拟夺权。
    block_event：让赢家的阶段体停住（测试 2/3 用）。
    started_event：阶段体入口握手——替代 time.sleep 时序断言（会审三轮：
    慢 CI 上 sleep 0.4 会假绿/假红）。"""
    def fake(s, exp, name):
        CALLS.append(name)
        if started_event is not None:
            started_event.set()
        if block_event is not None:
            block_event.wait(timeout=10)
        eng = dict(exp.stats or {}).get("engine") or {}
        stg = dict(eng.get("stages") or {})
        stg[name] = {"status": "done", "attempted": 1, "ok": 1, "failed": 0,
                    "skipped": 0, "tokens": 0, "seconds": 0}
        eng["stages"] = stg
        exp.stats = {**(exp.stats or {}), "engine": eng}
        if steal_after:
            exp.run_owner = "RUN-other"     # 单元级模拟中途被夺权
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
    block, started = threading.Event(), threading.Event()
    _fake_execute_factory(monkeypatch, block_event=block, started_event=started)
    winner_result: list = []
    t = threading.Thread(target=lambda: winner_result.append(
        engine.run("EXP-A07C2", ["plan"])))
    t.start()
    assert started.wait(timeout=10), "赢家已领权并进入阶段体（Event 握手替代 sleep）"
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
    """卡死恢复显式：带 owner 的 running 挡住新 run → release 放行 → 重领执行。

    （三轮注：running+NULL-owner 是迁移前存量形状，已由自动对账直接可领
    ——见 test_legacy_running_null_owner_claimable；本测钉的是**新制度**
    的卡死：runner 领了权、人死了、token 还挂着。）"""
    _seed("EXP-A07C4", status="running")
    with db.session() as s:
        s.get(Experiment, "EXP-A07C4").run_owner = "RUN-ghost"
        s.commit()
    blocked = engine.run("EXP-A07C4", ["plan"])
    assert blocked.get("status") == "already_running", "卡死的 running 必须挡住新 run"
    out = engine.release_run("EXP-A07C4")
    assert out["released"] is True and out["run_owner"] == "RUN-ghost"
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


# ── 会审三轮 BLOCK 项：release 语义与存量对账 ──────────────────

def test_release_hits_live_runner_stops_it(monkeypatch):
    """三轮严重项①②：release 打到**还活着**的 runner——立即夺权
    （owner 清空 + status=failed，条件 UPDATE 原子语义），活 runner 在
    下一道持有权校验（owner+status 双匹配）停下，不再继续烧钱。"""
    _seed("EXP-A07C7")
    block, started = threading.Event(), threading.Event()
    _fake_execute_factory(monkeypatch, block_event=block, started_event=started)
    errs: list = []
    t = threading.Thread(target=lambda: errs.append(
        _capture(engine.run, "EXP-A07C7", ["plan", "source_check"])))
    t.start()
    assert started.wait(timeout=10)
    out = engine.release_run("EXP-A07C7")     # 误判卡死、其实还活着
    assert out["released"] is True and out["run_owner"] is not None
    block.set()                               # 阶段体继续走完 plan
    t.join()
    assert CALLS == ["plan"], "被夺权的活 runner 不许再执行 source_check"
    assert any("失去运行持有权" in str(e) for e in errs if e), \
        "活 runner 必须在下一道校验被停（status=failed 兜底）"
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07C7")
        assert exp.status == "failed" and exp.run_owner is None, \
            "release 立即夺权：owner 清空，status 翻 failed"


def _capture(fn, *a, **kw):
    """线程体里把异常吞进列表（threading 不传异常）。"""
    try:
        return fn(*a, **kw)
    except Exception as e:                     # noqa: BLE001
        return e


def test_release_then_reclaim_real_takeover(monkeypatch):
    """真夺权路径（三轮一般项：不许只靠 fake 改 owner 模拟）：
    release → 新 run 领走 → 新主正常执行并收尾。"""
    _seed("EXP-A07C8")
    block, started = threading.Event(), threading.Event()
    _fake_execute_factory(monkeypatch, block_event=block, started_event=started)
    t = threading.Thread(target=_capture, args=(engine.run, "EXP-A07C8", ["plan"]))
    t.start()
    assert started.wait(timeout=10)
    assert engine.release_run("EXP-A07C8")["released"] is True
    block.set()
    t.join()
    # 真实路径夺权后重领：新主执行、收尾、status=done
    _fake_execute_factory(monkeypatch)
    result = engine.run("EXP-A07C8", ["plan"])
    assert result.get("stages"), "release 后新主可重领执行"
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C8").status == "done"


def test_legacy_running_null_owner_claimable(monkeypatch):
    """三轮严重项③：存量对账——迁移前卡在 running、run_owner=NULL 的
    老行必须可领（否则永久 already_running、点运行没反应）。"""
    _seed("EXP-A07C9", status="running")      # 直插旧行形状：running + 无 owner
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C9").run_owner is None
    _fake_execute_factory(monkeypatch)
    result = engine.run("EXP-A07C9", ["plan"])
    assert result.get("stages"), "存量 NULL-owner 卡死行必须可领（自动对账）"
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C9").status == "done"


def test_error_cleared_on_successful_rerun(monkeypatch):
    """三轮一般项：领取时 error=None（旧代码在领取块里清过，重构漏了）——
    失败实验重跑成功后不许挂着上一次的报错。"""
    _seed("EXP-A07C10", status="failed")
    with db.session() as s:
        s.get(Experiment, "EXP-A07C10").error = "上一次的报错"
        s.commit()
    _fake_execute_factory(monkeypatch)
    engine.run("EXP-A07C10", ["plan"])
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07C10")
        assert exp.status == "done" and exp.error is None, \
            "重跑成功后 error 必须干净（领取时清空）"


def test_list_stuck_finds_running_rows(monkeypatch):
    """A07 运维发现手段：--list-stuck 的底层——running 行可见、带 owner。"""
    _seed("EXP-A07C11", status="running")
    with db.session() as s:
        s.get(Experiment, "EXP-A07C11").run_owner = "RUN-ghost"
        s.commit()
    rows = engine.list_stuck()
    hit = [r for r in rows if r["id"] == "EXP-A07C11"]
    assert hit and hit[0]["run_owner"] == "RUN-ghost", "卡死实验必须可被发现"
