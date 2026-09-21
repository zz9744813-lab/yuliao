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
    """CALLS 是模块级计数器——每个用例前清零，不许跨用例累积；
    EXP-A07* 用例数据随之清掉（四轮：共享库不许留 running/假 owner
    的残行——那是「挡住一切新 run」的形状，会让 --list-stuck 长期噪音）。"""
    CALLS.clear()
    yield
    CALLS.clear()
    with db.session() as s:
        for r in (s.query(Experiment)
                  .filter(Experiment.id.like("EXP-A07%")).all()):
            s.delete(r)
        s.commit()


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
    # 四轮：running --release--> releasing（不可领取），没有活 runner 收尾的
    # 行要二次 release 兜底翻 failed
    assert out["status"] == "releasing"
    again = engine.run("EXP-A07C4", ["plan"])
    assert again.get("status") == "already_running", "releasing 不许被领取"
    out2 = engine.release_run("EXP-A07C4")
    assert out2["released"] is True and out2["status"] == "failed", \
        "死透的 releasing 由二次 release 兜底"
    _fake_execute_factory(monkeypatch)
    result = engine.run("EXP-A07C4", ["plan"])
    assert result.get("stages"), "release 后可重新领取并执行"
    assert CALLS.count("plan") == 1
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07C4").status == "done"
    # 非 running/releasing 态 release：无操作（幂等）
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
    assert out["status"] == "releasing"
    # 四轮主案：releasing 不可领取——活 runner 还在阶段体里时，新 run
    # 立刻放行=A/B 双跑双计费+stats 互踩（本修复要消灭的事故形态）
    assert engine.run("EXP-A07C7", ["plan"]).get("status") == "already_running"
    block.set()                               # 阶段体继续走完 plan
    t.join()
    assert CALLS == ["plan"], "被夺权的活 runner 不许再执行 source_check"
    assert any("失去运行持有权" in str(e) for e in errs if e), \
        "活 runner 必须在下一道校验被停（status=releasing≠running 兜底）"
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07C7")
        assert exp.status == "failed" and exp.run_owner is None, \
            "release 夺权后由原 runner 的收尾让位：releasing→failed、owner 清空"
        assert "执行权被显式释放" in (exp.error or ""), \
            "释放是打掉别人长跑的生产操作，error 必须留痕（四轮一般项）"


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


# ── 会审四轮：领取即闸（API 409）+ releasing 语义 + CAS 单元 ──

def test_api_claim_gate_409_and_legacy_heal(monkeypatch):
    """四轮严重项①：API 领取即闸——领不到 409（不再发 started 假响应）；
    存量 running+NULL-owner 行在 API 路径也能自动对账（旧快路径永远挡回）。"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    spawned: list = []
    monkeypatch.setattr(engine, "run_experiment_background",
                        lambda eid, stages=None, token=None:
                        spawned.append((eid, token)) or _noop_thread())
    # 持有中 → 409
    _seed("EXP-A07D1", status="running")
    with db.session() as s:
        s.get(Experiment, "EXP-A07D1").run_owner = "RUN-holder"
        s.commit()
    r = client.post("/experiments/EXP-A07D1/run", json={})
    assert r.status_code == 409 and "already_running" in r.text, \
        "领不到执行权必须 409，不许发 started 假响应"
    assert not any(e == "EXP-A07D1" for e, _ in spawned)
    # 存量 NULL-owner 卡死行 → 自动对账后 200 started（旧快路径永远 already_running）
    _seed("EXP-A07D3", status="running")
    with db.session() as s:
        assert s.get(Experiment, "EXP-A07D3").run_owner is None
    r3 = client.post("/experiments/EXP-A07D3/run", json={})
    assert r3.status_code == 200 and r3.json()["status"] == "started"
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07D3")
        assert exp.run_owner, "API 侧领取已落凭据（存量行自动对账）"
    # 正常行 → 200 + 带凭据启动
    _seed("EXP-A07D2")
    r2 = client.post("/experiments/EXP-A07D2/run", json={})
    assert r2.status_code == 200 and r2.json()["status"] == "started"
    hit = [(e, t) for e, t in spawned if e == "EXP-A07D2"]
    assert hit and hit[0][1], "后台线程必须收到端点代领的凭据 token"


class _noop_thread:
    def start(self):
        return None


def test_cas_predicate_matches_is_null_owner():
    """CAS 谓词单元：run_owner == None 由 SQLAlchemy 渲染成 IS NULL——
    存量 running+NULL 行 release 得动（这条隐式行为值得显式钉住）。"""
    _seed("EXP-A07D5", status="running")     # owner NULL
    out = engine.release_run("EXP-A07D5")
    assert out["released"] is True and out["run_owner"] is None
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07D5")
        assert exp.status == "releasing" and exp.run_owner is None


def test_report_already_running_prints(capsys):
    """四轮：后台线程输家回显独立成函数——「线程连 note 都不看」的
    假象必须可测。"""
    engine._report_already_running("EXP-X", "note 内容")
    out = capsys.readouterr().out
    assert "EXP-X" in out and "note 内容" in out


# ── 会审五轮：冻结走 API 不领取 + 迁移回归 ──────────────────────

def test_api_frozen_rejected_before_claim(monkeypatch):
    """五轮：冻结必须在领取前拒——先领再拒会把行卡在 running+token
    （engine.run 的冻结检查晚于 API 领取），那种行谁也领不动。"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    spawned: list = []
    monkeypatch.setattr(engine, "run_experiment_background",
                        lambda eid, stages=None, token=None:
                        spawned.append(eid))
    _seed("EXP-A07F1", frozen=True)
    r = client.post("/experiments/EXP-A07F1/run", json={})
    assert r.status_code == 400, "冻结实验经 API 必须在领取前被拒"
    with db.session() as s:
        exp = s.get(Experiment, "EXP-A07F1")
        assert exp.status == "created" and exp.run_owner is None, \
            "冻结拒收不许留下 running+token 的卡死形状"
    assert not spawned


def test_migrate_adds_owner_columns_to_old_db(tmp_path):
    """五轮：迁移回归——旧库（无 run_owner/run_claimed_at 两列）跑 _migrate
    后两列存在且可写；_claim_run 只对 locked/busy 退避，迁移未命中会以
    OperationalError 全域炸，值得专门的测。"""
    import sqlite3
    from sqlalchemy import create_engine
    p = tmp_path / "old.db"
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE experiments (
        id TEXT PRIMARY KEY, name TEXT, status TEXT, config TEXT,
        stats TEXT, error TEXT, created_at TEXT, updated_at TEXT)""")
    # _migrate 的 additions 覆盖多张表——缺表跳过（建表归 create_all 全责，
    # K1-A 二轮改定）；本测钉的是「既有旧表缺列 → 迁移后可写」
    con.execute("""CREATE TABLE llm_calls (id TEXT PRIMARY KEY, purpose TEXT,
        model TEXT, prompt_version TEXT, tokens_in INTEGER, tokens_out INTEGER,
        latency_ms INTEGER, cost REAL, status TEXT, error TEXT, created_at TEXT)""")
    con.execute("""CREATE TABLE segments (id TEXT PRIMARY KEY, work_id TEXT,
        ordinal INTEGER, text TEXT, n_sentences INTEGER, n_chars INTEGER)""")
    con.execute("""CREATE TABLE works (id TEXT PRIMARY KEY, title TEXT,
        source TEXT, note TEXT, created_at TEXT)""")
    con.execute("INSERT INTO experiments VALUES ('EXP-OLD','n','running',"
                "'{}','{}',NULL,'t','t')")
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{p.as_posix()}")
    from app import db as app_db
    app_db._migrate(eng)                    # 只增列迁移
    con = sqlite3.connect(str(p))
    cols = {row[1] for row in con.execute("PRAGMA table_info(experiments)")}
    assert {"run_owner", "run_claimed_at"} <= cols, \
        "旧库迁移后必须带执行权两列——缺列时 _claim_run 会 OperationalError 全域炸"
    con.execute("UPDATE experiments SET run_owner='RUN-x', "
                "run_claimed_at='t2' WHERE id='EXP-OLD'")   # 领取形状可写
    con.commit()
    con.close()


def test_release_mark_uses_owner_prefix_only():
    """五轮安全口径：留痕只记 owner 前缀——token 是写权限凭据，全串落
    用户可见的 error 字段=给未来带凭据接口留劫持面。"""
    _seed("EXP-A07F2", status="running")
    long_owner = "RUN-abcdef0123456789"
    with db.session() as s:
        s.get(Experiment, "EXP-A07F2").run_owner = long_owner
        s.commit()
    engine.release_run("EXP-A07F2")
    with db.session() as s:
        err = s.get(Experiment, "EXP-A07F2").error or ""
    assert long_owner not in err, "全串 token 不许落入用户可见字段"
    assert long_owner[:12] in err, "前缀保留可对账性"
