"""高开销实验入口「调用量 + 并发」上限回归（审计 P1 剩半，2026-09-26）。

审计 P1「评审令牌可调用任意文件导入/高开销实验」的导入半与档位半已修
（app/corpus.py 路径闸、app/access.py 两档令牌）；本文件钉死剩下的一半——
`app/api.py` 两个高开销入口（`POST /experiments`、`POST /experiments/{exp_id}/run`）
的服务端额度闸（R3 块）。既有 R1 预算闸（LG_MAX_RUNNING_EXPERIMENTS）只限
「同时在跑的实验数」，入口调用量与入口并发仍无界：等一个跑完立刻再发下一
个、循环创建实验，消耗照样放大。

钉住的不变量（对应硬契约 C1–C5）：
1. **C1** 上限 env > 默认，默认恒为有限值（并发 8 / 速率 60 每分钟）；
   0/负数/垃圾值一律钳到底 ≥1——闸不许被配置成"关"；
2. **C2** 超限显式 429（不静默排队、不降级）；响应体带 current/cap 数值与
   环境变量名；被拒请求零副作用（不建实验行、不占预算位、不领取执行权、
   不启动后台 run）；404/冻结这类非法请求在闸**之前**拒，不吃额度（与 R1
   同口径）；
3. 并发闸检查+占用同一持锁段（无 TOCTOU）：N 线程齐发恰好 cap 个通过；
4. 定长分钟窗：窗内封顶、翻窗恢复；拒绝不改动任何计数；
5. 错误路径（400/409/500 上抛）归还并发位——闸不能泄漏槽位；
6. **C3** 只加严不放宽：requires_admin 面与两档端点集合一字不动，
   新增的只是 GET /experiments/limits（只读配置面，评审档即可）；
7. **C5** 超限事件进日志（kind/current/cap/endpoint），上限配置可从
   GET /experiments/limits 的 JSON 读到（且该路由不被 /experiments/{exp_id} 遮蔽）。

纯离线：TestClient + 替身（create_experiment / run_experiment_background），
不连真库（conftest 临时 SQLite）、不发模型请求、不起真实服务。
时间敏感用例统一注入假时钟（monkeypatch api.time），防真实分钟翻转把
"窗内封顶"断言吹成随机红。
"""
import logging
import sys
import threading
import time as _real_time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import access as A                      # noqa: E402
from app import api, db, engine                 # noqa: E402
from app.main import app                        # noqa: E402
from app.models import Experiment               # noqa: E402


# ── 公共件 ────────────────────────────────────────────────────

def _seed(exp_id: str, *, status: str = "created", config: dict | None = None) -> None:
    db.init_db()
    with db.session() as s:
        s.query(Experiment).filter_by(id=exp_id).delete()
        s.add(Experiment(id=exp_id, name="t-quota", status=status,
                         config=config or {}, stats={}))
        s.commit()


def _exp_row(exp_id: str) -> Experiment:
    with db.session() as s:
        return s.get(Experiment, exp_id)


class _Clock:
    """假时钟：只喂 api 模块内的 time.time()（额度窗翻转唯一依赖它）。"""
    def __init__(self, t0: float = 1_800_000_000.0):
        self.t = t0

    def time(self) -> float:
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(api, "time", c)
    return c


@pytest.fixture(autouse=True)
def _quota_isolation(monkeypatch):
    """每例前后：两个新 env 与预算 env 复位、两套进程计数清零、EXP-QTA* 残行清掉。
    （模式照抄 tests/test_remote_caps_budget.py 的 _budget_clean。）"""
    for var in ("LG_EXPERIMENT_MAX_CONCURRENCY", "LG_EXPERIMENT_RATE_PER_MIN",
                "LG_MAX_RUNNING_EXPERIMENTS"):
        monkeypatch.delenv(var, raising=False)
    api._reset_entry_quota_for_tests()
    api._reset_budget_for_tests()
    yield
    api._reset_entry_quota_for_tests()
    api._reset_budget_for_tests()
    db.init_db()
    with db.session() as s:
        s.query(Experiment).filter(Experiment.id.like("EXP-QTA%")).delete(
            synchronize_session=False)
        s.commit()


@pytest.fixture()
def client():
    db.init_db()
    return TestClient(app)


class _RunStub:
    """engine.run_experiment_background 替身。block=True：处理器停在替身里
    （模拟"入口在途"，测并发闸）；否则返回一个立刻结束的线程（同 _Hold 口径，
    保证预算看护线程可 join）。"""

    def __init__(self, monkeypatch, block: bool = False):
        self.started: list = []
        self.gate = threading.Event()

        def fake(eid, stages=None, token=None):
            self.started.append((eid, token))
            if block:
                self.gate.wait(10)
            t = threading.Thread(target=lambda: None)
            t.start()
            return t

        monkeypatch.setattr(engine, "run_experiment_background", fake)


def _wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = _real_time.monotonic() + timeout
    while _real_time.monotonic() < deadline:
        if pred():
            return True
        _real_time.sleep(0.02)
    return False


# ── 1. C1：上限可配且有硬默认 ────────────────────────────────

def test_entry_quota_defaults_are_finite():
    """默认必须有限（不许 0=无限、None=无限）：并发 8、速率 60/分钟。"""
    assert api._entry_quota_max_concurrency() == api._ENTRY_QUOTA_DEFAULT_CONC == 8
    assert api._entry_quota_rate_per_min() == api._ENTRY_QUOTA_DEFAULT_RATE == 60
    assert 0 < api._entry_quota_max_concurrency() < float("inf")
    assert 0 < api._entry_quota_rate_per_min() < float("inf")


@pytest.mark.parametrize("raw,expect", [
    ("3", 3),                 # env > 默认
    ("0", 1),                 # 0 不等于关闸：钳到底
    ("-5", 1),                # 负数同钳底
    ("abc", None),            # 垃圾 → 回默认（None=占位，见下断言）
    ("  12  ", 12),           # strip 后按整数解析
])
def test_entry_quota_env_parsing_matrix(monkeypatch, raw, expect):
    for name, default in (("LG_EXPERIMENT_MAX_CONCURRENCY", 8),
                          ("LG_EXPERIMENT_RATE_PER_MIN", 60)):
        monkeypatch.setenv(name, raw)
        got = (api._entry_quota_max_concurrency() if name.endswith("CONCURRENCY")
               else api._entry_quota_rate_per_min())
        assert got == (default if expect is None else expect), \
            f"{name}={raw!r} 解析漂移：{got}"


def test_zero_env_never_opens_gate_at_runtime(clock, monkeypatch):
    """LG_EXPERIMENT_RATE_PER_MIN=0 在运行期也挡得住（恒 ≥1），
    绝不退化成"0=放行一切"。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "0")
    assert api._entry_quota_rate_per_min() == 1
    assert api._quota_try_acquire("POST /experiments", None) is None   # 第 1 次放行
    reject = api._quota_try_acquire("POST /experiments", None)          # 第 2 次必须拒
    assert reject is not None and "调用量" in reject
    api._quota_release()   # 只归还第 1 次真正占用的槽位


# ── 2. C2 + TOCTOU：并发闸的原子性与显式拒绝 ────────────────

def test_concurrency_gate_atomic_under_race(monkeypatch):
    """N 线程齐发抢额度（cap=1）：恰好 1 个通过——检查与占用同一持锁段，
    没有"先查后设"能把并发抬到 cap 之上。

    用例自身的时序纪律：赢家必须**在全部 N 次尝试都发生过之后**才归还槽位，
    否则"主线程先 set 放行事件"会让赢家瞬间归还、后来的线程依次成为赢家
    （那是用例的竞态，不是闸的漏洞）。故用第二道 `attempted` 栅栏：所有线程
    都尝试过 acquire 之后赢家才 release。"""
    monkeypatch.setenv("LG_EXPERIMENT_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "100")
    n = 6
    start = threading.Barrier(n)      # 齐发
    attempted = threading.Barrier(n)   # 全部尝试完毕（赢家仍持槽）
    wins: list[int] = []
    lock = threading.Lock()

    def worker(i: int):
        start.wait()
        got = api._quota_try_acquire("POST /experiments", f"race{i}")
        if got is None:
            with lock:
                wins.append(i)
        attempted.wait()               # 无人能早于此刻看到槽位被归还
        if got is None:
            api._quota_release()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert not [t for t in ts if t.is_alive()], "线程未收尾（栅栏死锁？）"
    assert len(wins) == 1, f"cap=1 时恰好 1 个赢家，实得 {wins}"
    assert api._entry_quota_state()["inflight"] == 0, "赢家归还后槽位必须回零"


def test_check_and_increment_share_one_lock(clock, monkeypatch):
    """确定性钉死"检查与占用同一持锁段"（TOCTOU 的正面判据）：
    主线程持住 `_ENTRY_QUOTA_LOCK` 时，另一线程的 `_quota_try_acquire` **必须被
    挡在锁外等**，不能先读完 in-flight 就返回拒因（那就是"锁外检查"漏洞：
    读—判—写之间无互斥，并发可被抬到 cap 之上）。

    上一条竞态用例（`test_concurrency_gate_atomic_under_race`）按"赢家持槽到
    全部尝试完毕"设计，窗口被用例自己消掉了，**抓不到**锁外检查（实测：把检查
    挪到锁外的突变仍让它绿）——所以另配这条确定性用例来承重那条不变量。"""
    monkeypatch.setenv("LG_EXPERIMENT_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "10")
    assert api._quota_try_acquire("POST /experiments", "holder") is None  # 吃掉 cap=1
    out: dict = {}
    done = threading.Event()

    def probe():
        out["reject"] = api._quota_try_acquire("POST /experiments", "probe")
        done.set()

    api._ENTRY_QUOTA_LOCK.acquire()
    try:
        th = threading.Thread(target=probe, daemon=True)
        th.start()
        blocked = not done.wait(1.0)
    finally:
        api._ENTRY_QUOTA_LOCK.release()
    assert blocked, ("持锁期间 _quota_try_acquire 仍返回了 → 并发检查在锁外，"
                     "读-判-写之间没有互斥（TOCTOU）")
    assert done.wait(10), "放锁后必须放行"
    assert out["reject"] is not None and "并发" in out["reject"]
    api._quota_release()
    assert api._entry_quota_state()["inflight"] == 0


def test_rejected_requests_do_not_move_counters(clock, monkeypatch):
    """被拒不产生副作用（额度层面）：不占并发槽、不追加分钟计数；
    翻窗后立刻恢复正常放行。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "1")
    assert api._quota_try_acquire("POST /experiments", None) is None
    api._quota_release()
    st_mid = api._entry_quota_state()
    assert st_mid["window_calls_this_minute"] == 1
    reject = api._quota_try_acquire("POST /experiments", None)
    assert reject is not None and "current=1" in reject and "cap=1" in reject
    st = api._entry_quota_state()
    assert st["inflight"] == 0, "并发拒绝/速率拒绝都不得增 in-flight"
    assert st["window_calls_this_minute"] == 1, "被拒请求不得再吃分钟计数"
    clock.t += 60   # 翻窗
    assert api._quota_try_acquire("POST /experiments", None) is None, "翻窗必须恢复"
    api._quota_release()


def test_rate_window_rolls_over_next_minute(clock, monkeypatch):
    """定长窗口径：同分钟封顶拒绝（429 文案含当前值/上限值/环境变量名），
    下一分钟桶清零恢复。假时钟 t0 落在整分钟桶首，+60 恰好翻窗。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "2")
    for _ in range(2):
        assert api._quota_try_acquire("POST /experiments/{e}/run", "EXP-X") is None
        api._quota_release()
    reject = api._quota_try_acquire("POST /experiments/{e}/run", "EXP-X")
    assert reject is not None
    assert "current=2" in reject and "cap=2" in reject
    assert "LG_EXPERIMENT_RATE_PER_MIN" in reject, "拒因必须点名可配置项"
    clock.t += 59          # 仍在同一分钟桶 → 依旧封顶（被拒不占槽、不再计数）
    assert api._quota_try_acquire("POST /experiments/{e}/run", "EXP-X") is not None
    clock.t += 1           # 跨过桶边界 → 恢复放行
    assert api._quota_try_acquire("POST /experiments/{e}/run", "EXP-X") is None
    api._quota_release()


# ── 3. HTTP 行为：run 入口 ───────────────────────────────────

def test_run_over_concurrency_429_zero_side_effect(client, clock, monkeypatch):
    """入口并发=1：第一个 run 处理器在途时，第二个 run 必须 429（不排队）、
    零副作用——不占预算位、不领取执行权、不启动第二个后台 run；第一个结束后
    槽位归还，第三个 run 正常放行。"""
    monkeypatch.setenv("LG_EXPERIMENT_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "10")
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "5")
    stub = _RunStub(monkeypatch, block=True)
    _seed("EXP-QTA1"); _seed("EXP-QTA2"); _seed("EXP-QTA3")

    results: dict = {}

    def t1():
        c = TestClient(app)
        results["r1"] = c.post("/experiments/EXP-QTA1/run", json={})

    th = threading.Thread(target=t1)
    th.start()
    assert _wait_until(lambda: api._entry_quota_state()["inflight"] == 1), \
        "第一个请求应停在入口处理中"
    r2 = client.post("/experiments/EXP-QTA2/run", json={})
    assert r2.status_code == 429, f"并发超限应 429，实得 {r2.status_code}: {r2.text[:300]}"
    assert "并发" in r2.text and "current=1" in r2.text and "cap=1" in r2.text
    assert "不排队" in r2.text, "拒因必须显式声明不静默排队"
    assert api._budget_active_count() == 1, "被拒请求不得占预算位（只有 T1 占着）"
    assert len(stub.started) == 1
    row2 = _exp_row("EXP-QTA2")
    assert row2.status == "created" and row2.run_owner is None, \
        "额度拒绝必须发生在领取之前（零副作用）"
    stub.gate.set()
    th.join(15)
    assert results["r1"].status_code == 200, results["r1"].text[:300]
    assert _wait_until(lambda: api._entry_quota_state()["inflight"] == 0), \
        "处理器结束后并发位必须归还"
    r3 = client.post("/experiments/EXP-QTA3/run", json={})
    assert r3.status_code == 200, f"归还后应可再进：{r3.text[:300]}"


def test_run_over_rate_429_body_numbers(client, clock, monkeypatch):
    """速率超限走 HTTP 也是 429（不是 500、不是排队）：响应体能核对当前值、
    上限值与环境变量名；第三次 run 零副作用。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "2")
    monkeypatch.setenv("LG_MAX_RUNNING_EXPERIMENTS", "5")
    stub = _RunStub(monkeypatch)
    _seed("EXP-QTAR1"); _seed("EXP-QTAR2"); _seed("EXP-QTAR3")
    assert client.post("/experiments/EXP-QTAR1/run", json={}).status_code == 200
    assert client.post("/experiments/EXP-QTAR2/run", json={}).status_code == 200
    r = client.post("/experiments/EXP-QTAR3/run", json={})
    assert r.status_code == 429, f"应 429，实得 {r.status_code}: {r.text[:300]}"
    assert "调用量" in r.text and "current=2" in r.text and "cap=2" in r.text
    assert "LG_EXPERIMENT_RATE_PER_MIN" in r.text
    assert len(stub.started) == 2, "超限请求不得启动后台 run"
    row = _exp_row("EXP-QTAR3")
    assert row.status == "created" and row.run_owner is None


def test_illegal_requests_do_not_consume_quota(client, clock, monkeypatch):
    """404（不存在）与 400（冻结）在闸之前拒——非法请求不吃额度（与 R1 的
    「预算不被探测消耗」同口径）；而 409（领取竞争输家）确实过闸一次、
    但槽位归还、不泄漏。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "3")
    assert client.post("/experiments/EXP-QTANONE/run", json={}).status_code == 404
    _seed("EXP-QTAFROZ", config={"frozen": True})
    assert client.post("/experiments/EXP-QTAFROZ/run", json={}).status_code == 400
    st = api._entry_quota_state()
    assert st["window_calls_this_minute"] == 0, "404/冻结请求不得吃额度"
    assert st["inflight"] == 0
    _seed("EXP-QTABUSY", status="running")
    with db.session() as s:
        s.get(Experiment, "EXP-QTABUSY").run_owner = "RUN-holder"
        s.commit()
    r = client.post("/experiments/EXP-QTABUSY/run", json={})
    assert r.status_code == 409, r.text[:300]
    st = api._entry_quota_state()
    assert st["window_calls_this_minute"] == 1, "领取竞争（409）算一次入口调用"
    assert st["inflight"] == 0, "409 路径必须归还并发槽（异常也要走 finally）"


# ── 4. HTTP 行为：create 入口 ────────────────────────────────

class _CreateStub:
    """experiments.create_experiment 替身：记录调用次数，可注入阻塞/异常。
    返回最小实验替身——create_exp 只读 .id 与 .config。"""

    def __init__(self, monkeypatch, *, block: threading.Event | None = None,
                 raise_exc: Exception | None = None):
        self.calls = 0
        self._block = block
        self._raise = raise_exc

        def fake(s, overrides=None):
            self.calls += 1
            if self._block is not None:
                self._block.wait(10)
            if self._raise is not None:
                raise self._raise
            return SimpleNamespace(id="EXP-QTAFAKE00000", config={"n_segments": 24})

        monkeypatch.setattr(api.experiments, "create_experiment", fake)


def test_create_over_rate_429_no_experiment_created(client, clock, monkeypatch):
    """创建入口同样受速率闸：第三次 429，且被拒请求**没走到**建实验逻辑。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "2")
    stub = _CreateStub(monkeypatch)
    assert client.post("/experiments", json={}).status_code == 200
    assert client.post("/experiments", json={}).status_code == 200
    r = client.post("/experiments", json={})
    assert r.status_code == 429, f"应 429，实得 {r.status_code}: {r.text[:300]}"
    assert "调用量" in r.text and "cap=2" in r.text
    assert stub.calls == 2, "超限拒绝不得产生建实验副作用"


def test_create_over_concurrency_429_and_error_releases_slot(client, clock, monkeypatch):
    """创建入口并发=1：在途一个（停在 create_experiment 里）时第二个 429；
    另外 ValueError→400 的错误路径必须归还槽位（闸不泄漏）。"""
    monkeypatch.setenv("LG_EXPERIMENT_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "10")
    gate = threading.Event()
    stub = _CreateStub(monkeypatch, block=gate)
    results: dict = {}

    def t1():
        c = TestClient(app)
        results["r1"] = c.post("/experiments", json={})

    th = threading.Thread(target=t1)
    th.start()
    assert _wait_until(lambda: api._entry_quota_state()["inflight"] == 1)
    r2 = client.post("/experiments", json={})
    assert r2.status_code == 429 and "并发" in r2.text
    assert stub.calls == 1, "并发被拒的创建请求不得进入建实验逻辑"
    gate.set()
    th.join(15)
    assert results["r1"].status_code == 200
    assert _wait_until(lambda: api._entry_quota_state()["inflight"] == 0)
    # 错误路径归还槽位：create 抛 ValueError → 400，随后入口仍可用
    boom = _CreateStub(monkeypatch, raise_exc=ValueError("没有语料"))
    r3 = client.post("/experiments", json={})
    assert r3.status_code == 400, r3.text[:300]
    assert api._entry_quota_state()["inflight"] == 0, "异常路径必须走 finally 归还"
    # 换回不抛的替身再试：证明 r4 的 200 来自"闸没泄漏槽位"，而不是替身仍抛
    ok = _CreateStub(monkeypatch)
    r4 = client.post("/experiments", json={})
    assert r4.status_code == 200, f"错误路径未泄漏槽位的证明：{r4.text[:300]}"
    assert ok.calls == 1 and boom.calls == 1


# ── 5. C5：可观测 ────────────────────────────────────────────

def test_reject_logged_with_endpoint_and_numbers(caplog, clock, monkeypatch):
    """超限事件进日志：端点、种类（rate/concurrency）、当前值、上限值都要可 grep。"""
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "1")
    with caplog.at_level(logging.WARNING, logger="app.api"):
        assert api._quota_try_acquire("POST /experiments/{exp_id}/run", "EXP-QTALOG") is None
        api._quota_release()
        reject = api._quota_try_acquire("POST /experiments/{exp_id}/run", "EXP-QTALOG")
    assert reject is not None
    text = caplog.text
    assert "exp-entry-quota reject" in text
    assert "kind=rate" in text and "current=1" in text and "cap=1" in text
    assert "POST /experiments/{exp_id}/run" in text and "EXP-QTALOG" in text


def test_limits_endpoint_exposes_quota_config(client, monkeypatch):
    """`--json` 类响应读到上限配置：GET /experiments/limits 返回 env>默认后的
    两个上限、默认值与当前在途/分钟计数；且不被 GET /experiments/{exp_id} 遮蔽。"""
    monkeypatch.setenv("LG_EXPERIMENT_MAX_CONCURRENCY", "5")
    monkeypatch.setenv("LG_EXPERIMENT_RATE_PER_MIN", "9")
    r = client.get("/experiments/limits")
    assert r.status_code == 200, \
        "limits 路由必须注册在 /experiments/{exp_id} 通配之前: " + r.text[:200]
    body = r.json()
    assert body["LG_EXPERIMENT_MAX_CONCURRENCY"] == 5
    assert body["LG_EXPERIMENT_RATE_PER_MIN"] == 9
    assert body["defaults"] == {"LG_EXPERIMENT_MAX_CONCURRENCY": 8,
                                "LG_EXPERIMENT_RATE_PER_MIN": 60}
    assert body["LG_MAX_RUNNING_EXPERIMENTS"] == 1, "预算闸配置同屏可读"
    assert body["inflight"] == 0
    # 通配路由不回归：真实实验 id 仍按实验解析
    _seed("EXP-QTAGET1")
    r2 = client.get("/experiments/EXP-QTAGET1")
    assert r2.status_code == 200 and r2.json()["id"] == "EXP-QTAGET1"
    assert client.get("/experiments/EXP-QTANOSUCH").status_code == 404


# ── 6. C3：只加严不放宽 ──────────────────────────────────────

def test_admin_surface_untouched():
    """管理档端点集合一字不动；新增的只有 GET /experiments/limits（只读，
    评审档即可，requires_admin 恒 False）。"""
    assert A._ADMIN_ONLY_POST == ("/corpus/import-inbox", "/corpus/import-file",
                                  "/corpus/import-distiller", "/experiments")
    assert A.requires_admin("POST", "/experiments") is True
    assert A.requires_admin("POST", "/experiments/EXP-1/run") is True
    assert A.requires_admin("GET", "/experiments/limits") is False
    for p in ("/corpus/import-inbox", "/corpus/import-file",
              "/corpus/import-distiller", "/experiments",
              "/experiments/EXP-x/run", "/review/r1/verdict", "/knowledge/query",
              "/works", "/segments", "/corpus/stats"):
        assert A.requires_admin("GET", p) is False, p
    # 新路由只加 GET，不加 POST（真实 app 的 POST 集合与两档清单仍完全一致）
    post_paths = {r.path for r in app.routes
                  if "POST" in (getattr(r, "methods", None) or set())}
    assert "/experiments/limits" not in post_paths
    expected_admin = set(A._ADMIN_ONLY_POST) | {"/experiments/{exp_id}/run"}
    expected_review = {"/review/{review_id}/verdict", "/knowledge/query"}
    assert post_paths == expected_admin | expected_review, post_paths


def test_budget_gate_layering_unchanged(client, clock, monkeypatch):
    """R3 只加严：R1 预算闸语义原样——预算满仍是"预算"文案的 429（额度充足
    时先过额度、后撞预算），两层各报各的，不互相吞语义。"""
    monkeypatch.delenv("LG_MAX_RUNNING_EXPERIMENTS", raising=False)   # 预算默认 1
    stub = _RunStub(monkeypatch, block=True)
    _seed("EXP-QTAB1"); _seed("EXP-QTAB2")

    results: dict = {}

    def t1():
        c = TestClient(app)
        results["r1"] = c.post("/experiments/EXP-QTAB1/run", json={})

    th = threading.Thread(target=t1)
    th.start()
    assert _wait_until(lambda: api._budget_active_count() == 1)
    # T1 还停在替身里（在途 1、并发默认 8 → 第二请求过得去额度闸），
    # 撞的是 R1 预算闸：拒因必须仍是"预算"，不是额度。
    r2 = client.post("/experiments/EXP-QTAB2/run", json={})
    assert r2.status_code == 429 and "预算" in r2.text and "并发" not in r2.text
    stub.gate.set()
    th.join(15)
    assert results["r1"].status_code == 200
    assert _wait_until(lambda: api._budget_active_count() == 0)
    assert api._entry_quota_state()["inflight"] == 0
