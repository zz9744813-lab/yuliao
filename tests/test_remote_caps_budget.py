"""远程访问残留两项中危收口回归（审计残留 R1/R2，2026-09-23）。

R1 实验总预算闸（app/api.py run 入口）：
- 全局并发运行中实验数上限（默认 1，LG_MAX_RUNNING_EXPERIMENTS 可调高但恒 ≥1）；
- 原子性：检查+占用在同一把进程锁内（无先查后设的 TOCTOU），用多线程并发
  打 run 入口验证不被绕过；
- 超限 429 + 可读原因、零副作用；claim 失败（409）与后台 run 结束都要归还
  预算位，否则预算只漏不进。

R2 loopback 免令牌显式化（app/access.py）：
- 默认（LG_LOCAL_BYPASS 未设）：本机回环请求**也需令牌**（旧行为是隐式免令牌，
  本文件把它钉死为新默认）；
- 显式 LG_LOCAL_BYPASS=1：本机直连免令牌；
- 代理头一票否决：即使开关打开，带代理头的回环请求仍需令牌；
- 启动自检 self_check() 如实打印开关状态。

纯离线：TestClient / 单元级替身（stub 后台线程用 Event 控住不放），
不连真网关、不起真实服务、不跑真实引擎阶段。
"""
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import access as A                     # noqa: E402
from app import api, db, engine                 # noqa: E402
from app.main import app                        # noqa: E402
from app.models import Experiment               # noqa: E402


# ── 公共件 ────────────────────────────────────────────────────

def _seed(exp_id: str, *, status: str = "created") -> None:
    db.init_db()
    with db.session() as s:
        s.query(Experiment).filter_by(id=exp_id).delete()
        s.add(Experiment(id=exp_id, name="t-remote-caps", status=status,
                         config={}, stats={}))
        s.commit()


def _exp_row(exp_id: str) -> Experiment:
    with db.session() as s:
        return s.get(Experiment, exp_id)


class _Hold:
    """可控后台替身：run_experiment_background 返回一个真实 Thread，
    在 hold Event 上停住（= 实验"运行中"），set 后才结束。"""

    def __init__(self, monkeypatch):
        self.hold = threading.Event()
        self.started: list = []

        def fake(eid, stages=None, token=None):
            self.started.append((eid, token))
            t = threading.Thread(target=self.hold.wait, args=(30,), daemon=True)
            t.start()
            return t

        monkeypatch.setattr(engine, "run_experiment_background", fake)


def _wait_budget_free(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api._budget_active_count() == 0:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def _budget_clean():
    """每例前后：预算计数清零（测试隔离）；EXP-RCAP* 行清掉（共享测试库
    不留残行）。预算位的归还语义本身由 test_budget_released_* 显式钉住。"""
    api._reset_budget_for_tests()
    yield
    api._reset_budget_for_tests()
    # 主控修正（2026-09-23）：首个用例（test_budget_default_is_conservative）
    # 不建库，teardown 直接查 experiments 表会 ERROR（no such table）——
    # 清残行前先 init_db（幂等 create_all，与 _seed 同口径）。
    db.init_db()
    # 2026-09-24 主控修：本文件旧用 LIKE "EXP-RB%" 清残行，与
    # test_random_batch/test_review_batch 的 EXP-RB1/EXP-RB10/EXP-RB42 同前缀
    # 撞车——那些实验行带 frames/candidates/review_items 子行，删父行触发
    # FOREIGN KEY constraint failed（全量 pytest 12 例 teardown ERROR），
    # 且属跨测试互删。修法：①本文件专用命名空间 EXP-RCAP*；②清残行前先删
    # 子行（父行无子行时为空操作），避免任何残留子行再撞 FK。
    _CHILD_TABLES = ("frames", "candidates", "judge_runs", "review_items",
                     "report_files", "controlled_corruptions")
    with db.session() as s:
        ids = [r.id for r in s.query(Experiment).filter(
            Experiment.id.like("EXP-RCAP%")).all()]
        if ids:
            from sqlalchemy import text as _text
            for tbl in _CHILD_TABLES:
                for eid in ids:
                    s.execute(_text(f"DELETE FROM {tbl} WHERE experiment_id = :e"),
                              {"e": eid})
            for eid in ids:
                s.execute(_text("DELETE FROM experiments WHERE id = :e"),
                          {"e": eid})
        s.commit()


@pytest.fixture()
def client():
    db.init_db()
    return TestClient(app)


# ── R1：实验总预算闸 ──────────────────────────────────────────

def test_budget_default_is_conservative(monkeypatch):
    """默认上限 = 1（审计建议口径）；环境变量只能调高、恒 ≥1，
    且解析失败/非法值都落回 1——预算闸不许被环境变量关掉。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)
    assert api._budget_max() == 1
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "0")
    assert api._budget_max() == 1
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "-3")
    assert api._budget_max() == 1
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "abc")
    assert api._budget_max() == 1
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "3")
    assert api._budget_max() == 3


def test_budget_under_limit_starts(client, monkeypatch):
    """未超限正常：占位 → 领取 → started；计数=1。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)
    hold = _Hold(monkeypatch)
    _seed("EXP-RCAPU1")
    r = client.post("/experiments/EXP-RCAPU1/run", json={})
    assert r.status_code == 200 and r.json()["status"] == "started", r.text[:300]
    assert hold.started and hold.started[0][0] == "EXP-RCAPU1"
    assert api._budget_active_count() == 1
    hold.hold.set()


def test_budget_over_limit_429_zero_side_effect(client, monkeypatch):
    """超限被拒：429 + 可读原因（点名上限与环境变量名）；被拒请求
    零副作用——不领取、不启动、实验行原样。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)
    hold = _Hold(monkeypatch)
    _seed("EXP-RCAPO1")
    _seed("EXP-RCAPO2")
    assert client.post("/experiments/EXP-RCAPO1/run", json={}).status_code == 200
    r = client.post("/experiments/EXP-RCAPO2/run", json={})
    assert r.status_code == 429, f"应 429，实得 {r.status_code}: {r.text[:300]}"
    assert "预算" in r.text and "上限" in r.text, "拒收原因必须可读"
    assert len(hold.started) == 1, "超限请求不得启动后台 run"
    row = _exp_row("EXP-RCAPO2")
    assert row.status == "created" and row.run_owner is None, \
        "预算闸拒绝必须发生在领取之前（零副作用）"
    hold.hold.set()


def test_budget_not_bypassed_under_concurrency(client, monkeypatch):
    """并发不被绕过：N 线程同时打 run——预算闸的检查+占用在同一把锁内，
    恰好 cap 个成功，其余 429；成功者的实验行 running、失败者原样。"""
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "2")
    hold = _Hold(monkeypatch)
    ids = [f"EXP-RCAPC{i}" for i in range(6)]
    for eid in ids:
        _seed(eid)
    barrier = threading.Barrier(6)
    results: dict = {}
    lock = threading.Lock()

    def worker(eid: str):
        barrier.wait()
        c = TestClient(app)
        r = c.post(f"/experiments/{eid}/run", json={})
        with lock:
            results[eid] = r.status_code

    ts = [threading.Thread(target=worker, args=(eid,)) for eid in ids]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    ok = [e for e, code in results.items() if code == 200]
    rejected = [e for e, code in results.items() if code == 429]
    assert len(ok) == 2 and len(rejected) == 4, results
    assert api._budget_active_count() == 2, "占用数必须恰等于成功启动数"
    for eid in ok:
        row = _exp_row(eid)
        assert row.status == "running" and row.run_owner, f"{eid} 应已领取"
    for eid in rejected:
        row = _exp_row(eid)
        assert row.status == "created" and row.run_owner is None, \
            f"{eid} 被拒后不得领取执行权"
    hold.hold.set()


def test_budget_released_after_run_finishes(client, monkeypatch):
    """run 结束必须归还预算位：占住 → 放行后台线程 → 计数回零 →
    新 run 可再次启动（预算只漏不进=服务永久拒绝新实验，必须钉死）。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)
    hold = _Hold(monkeypatch)
    _seed("EXP-RCAPR1")
    _seed("EXP-RCAPR2")
    assert client.post("/experiments/EXP-RCAPR1/run", json={}).status_code == 200
    hold.hold.set()
    assert _wait_budget_free(), "后台 run 结束后预算位必须归还"
    r = client.post("/experiments/EXP-RCAPR2/run", json={})
    assert r.status_code == 200, f"归还后新 run 应可启动: {r.text[:300]}"
    hold.hold.set()


def test_budget_released_when_claim_fails(client, monkeypatch):
    """claim 失败（409 already_running）也要立刻归还预算位——否则一次
    竞争输家就把预算永久占掉一格。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)
    hold = _Hold(monkeypatch)
    _seed("EXP-RCAPL1", status="running")
    with db.session() as s:
        s.get(Experiment, "EXP-RCAPL1").run_owner = "RUN-holder"
        s.commit()
    r = client.post("/experiments/EXP-RCAPL1/run", json={})
    assert r.status_code == 409 and "already_running" in r.text, r.text[:300]
    assert len(hold.started) == 0
    assert api._budget_active_count() == 0, "claim 失败必须立刻归还预算位"
    # 归还后正常路径可用
    _seed("EXP-RCAPL2")
    assert client.post("/experiments/EXP-RCAPL2/run", json={}).status_code == 200
    hold.hold.set()


def test_budget_blocked_requests_leave_counter_untouched(client, monkeypatch):
    """连续超限请求不得让计数漂移（拒绝路径不碰计数）。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)
    hold = _Hold(monkeypatch)
    _seed("EXP-RCAPD1")
    for i in range(2, 5):
        _seed(f"EXP-RCAPD{i}")
    assert client.post("/experiments/EXP-RCAPD1/run", json={}).status_code == 200
    for i in range(2, 5):
        r = client.post(f"/experiments/EXP-RCAPD{i}/run", json={})
        assert r.status_code == 429
        assert api._budget_active_count() == 1, "拒绝不得增减计数"
    hold.hold.set()


# ── R2：loopback 免令牌显式化 ─────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_token():
    """_gate_app 会改模块全局 _TOKEN；不还原会污染其他测试模块。"""
    saved = A._TOKEN
    yield
    A._TOKEN = saved


def _gate_app(host: str, token: str | None = "TOK"):
    """与 test_access_gate 同款薄 ASGI 包装：真走完整中间件链，
    并把 scope 的 client 改写成指定 host。"""
    A._TOKEN = token
    inner = FastAPI()

    @inner.get("/")
    def _root():
        return {"ok": True}

    A.install(inner)

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await inner(scope, receive, send)

    return TestClient(wrapped)


def test_loopback_default_requires_token(monkeypatch):
    """R2 主回归：默认（LG_LOCAL_BYPASS 未设）时本机回环请求**也需令牌**。

    旧行为（隐式免令牌）正是审计 R2 点名的洞：任何本机回环代理漏传指定头
    ⇒ 全站免鉴权。新默认必须落在"要求令牌"这一侧。"""
    monkeypatch.delenv("LG_LOCAL_BYPASS", raising=False)
    c = _gate_app("127.0.0.1")
    r = c.get("/")
    assert r.status_code == 401, f"loopback 无令牌应 401，实得 {r.status_code}"
    assert "访问令牌" in r.text
    # ::1 同口径
    c2 = _gate_app("::1")
    assert c2.get("/").status_code == 401
    # 本机开发带令牌的既有用法不受影响：?t= 换 cookie 后放行
    r2 = _gate_app("127.0.0.1").get("/?t=TOK", follow_redirects=False)
    assert r2.status_code == 302


def test_loopback_bypass_only_with_explicit_env(monkeypatch):
    """显式 LG_LOCAL_BYPASS=1 才免令牌；其余取值（0/yes/true/空格1）不算开。"""
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    assert _gate_app("127.0.0.1").get("/").status_code == 200
    assert _gate_app("::1").get("/").status_code == 200
    for bad in ("0", "yes", "true", "on", " 1"):
        monkeypatch.setenv("LG_LOCAL_BYPASS", bad)
        assert A.local_bypass_enabled() is False, f"{bad!r} 不得算开"
        assert _gate_app("127.0.0.1").get("/").status_code == 401, \
            f"LG_LOCAL_BYPASS={bad!r} 时 loopback 仍需令牌"


def test_proxy_header_defeats_bypass(monkeypatch):
    """代理头一票否决：即使显式开了免令牌，带代理头的回环请求仍需令牌
    （回环代理漏传头不能变成全站免鉴权——R2 的洞面在这条上二次收口）。"""
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        r = _gate_app("127.0.0.1").get("/", headers={header: "203.0.113.9"})
        assert r.status_code == 401, f"开免令牌 + {header} 仍必须要求令牌"
    # 带令牌的代理回环请求照常可进（别把正常远程访问挡死）
    r = _gate_app("127.0.0.1").get("/?t=TOK",
                                   headers={"CF-Connecting-IP": "203.0.113.9"},
                                   follow_redirects=False)
    assert r.status_code == 302


def test_remote_needs_token_regardless_of_bypass(monkeypatch):
    """非回环来源与开关无关：开了免令牌，远程仍需令牌（开关只管 loopback）。"""
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    assert _gate_app("203.0.113.9").get("/").status_code == 401


def test_self_check_prints_bypass_state(monkeypatch, capsys):
    """启动自检如实打印免令牌状态与绑定面指引（审计 R2 诉求：不许隐式）。"""
    monkeypatch.delenv("LG_LOCAL_BYPASS", raising=False)
    A._TOKEN = "TOK"
    A.self_check()
    out = capsys.readouterr().out
    assert "关闭" in out and "LG_LOCAL_BYPASS" in out and "绑定面" in out
    monkeypatch.setenv("LG_LOCAL_BYPASS", "1")
    A.self_check()
    out = capsys.readouterr().out
    assert "开启" in out and "代理头" in out
    # 鉴权整体关闭（REVIEW_NO_AUTH=1 的测试形态）也要如实报告
    A._TOKEN = None
    A.self_check()
    out = capsys.readouterr().out
    assert "关闭" in out and "REVIEW_NO_AUTH" in out
