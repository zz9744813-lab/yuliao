"""R6 live/pytest 互斥守卫——跨进程竞态回归（2026-09-23 审计 P1 验收项）。

钉住的事（同锁协议见 app/live_guard.py + app/config.py 单点导出）：
1. **路径单点**：live 侧与 pytest 侧的锁路径都只出自 app/config.py
   （LOCK_FILE_NAME / prod_lock_path() / live_lock_path()）；LG_LOCK_DIR
   一个旋钮让两侧同时跟随——本文件全部子进程用例用它隔离到临时目录，
   **绝不触碰真实生产锁位**（repo/data/live_run.lock）；
2. **方向 A**：pytest 整轮持锁期间，live 进程启动必须被拒且给出明确
   原因（跨进程实证，不再是「查一次不持锁」）；
3. **方向 B**：live 进程真实持锁期间，pytest 启动（其 conftest 在
   pytest_configure 取锁）必须非零退出、明确拒绝——不得静默放行；
4. **方向 C**：pytest 整轮结束（pytest_sessionfinish 释放）后锁位无
   残留，live 可正常获取；
5. **竞态**：两个 live 进程同一时刻起跑，O_EXCL 原子性保证恰好一个赢。

纪律：只起子进程验证协议，不碰 DB、不跑 --live 开放路径。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as CFG                     # noqa: E402
from app import live_guard as LG                  # noqa: E402

TIMEOUT_S = 120

# ── 子进程脚本：只 import app.config / app.live_guard，不触 DB/LLM ──

# live 角色：尝试取锁并跑起来（取锁失败 → SystemExit，非零退出）
LIVE_TRY = (
    "import time\n"
    "from app import live_guard as lg\n"
    "with lg.live_lock('t-child-live'):\n"
    "    print('CHILD_LIVE_STARTED', flush=True)\n"
    "    time.sleep(0.5)\n"
)

# live 角色（握手版）：持锁直到父进程放置 RELEASE 文件才释放
LIVE_HOLD = (
    "import os, time\n"
    "from app import live_guard as lg\n"
    "ready, release = os.environ['LG_CHILD_READY'], os.environ['LG_CHILD_RELEASE']\n"
    "with lg.live_lock('t-child-live'):\n"
    "    with open(ready, 'w') as f:\n"
    "        f.write(str(os.getpid()))\n"
    "    end = time.time() + 60\n"
    "    while not os.path.exists(release) and time.time() < end:\n"
    "        time.sleep(0.05)\n"
    "print('CHILD_LIVE_RELEASED', flush=True)\n"
)

# live 角色（赛跑版）：等 GO 门文件后同时冲锁，赢家持 0.3s
LIVE_RACE = (
    "import os, time\n"
    "from app import live_guard as lg\n"
    "go = os.environ['LG_CHILD_GO']\n"
    "while not os.path.exists(go):\n"
    "    time.sleep(0.01)\n"
    "with lg.live_lock('t-race'):\n"
    "    print('CHILD_ACQUIRED', flush=True)\n"
    "    time.sleep(0.3)\n"
)


def _child_env(lock_dir: Path, **extra: object) -> dict:
    env = os.environ.copy()
    env["LG_LOCK_DIR"] = str(lock_dir)      # 锁目录单点覆盖：两侧同时跟随
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_live_child(lock_dir: Path, snippet: str, **extra: object):
    return subprocess.run(
        [sys.executable, "-c", snippet], cwd=str(ROOT),
        env=_child_env(lock_dir, **extra),
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TIMEOUT_S)


def _run_pytest_child(lock_dir: Path, keyword: str):
    """子进程跑一轮真 pytest（本文件、只命中无副作用的 child_probe 用例）：
    其 tests/conftest.py 会在 pytest_configure 检锁+取锁、sessionfinish 释放。"""
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)),
         # 清掉 pyproject.toml 的 addopts="-q"：否则与本处的 -q 叠成 -qq，
         # pytest 会吞掉 "1 passed" 总结行（方向 C 曾据此误判失败）。
         "-o", "addopts=",
         "-q", "-k", keyword, "-p", "no:cacheprovider"],
        cwd=str(ROOT), env=_child_env(lock_dir),
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=TIMEOUT_S)


def _wait_until(cond, what: str, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    pytest.fail(f"等待超时：{what}")


# ── 1. 锁路径单点导出（P1 错位钉死）──────────────────────────────

def test_lock_path_single_source_exported_by_config(tmp_path, monkeypatch):
    """两侧锁路径只出自 app/config.py；LG_LOCK_DIR 让两侧同时跟随；
    生产锁位**不随** LG_DATA_DIR（pytest 覆写的临时目录）漂移——
    2026-09-23 P1「守卫盯错目录」的不变式。"""
    monkeypatch.delenv("LG_LOCK_DIR", raising=False)
    # pytest 侧缺省=固定生产锁位 repo/data/live_run.lock
    assert LG.prod_lock_path() == CFG.prod_lock_path()
    assert LG.prod_lock_path() == ROOT / "data" / CFG.LOCK_FILE_NAME
    monkeypatch.setenv("LG_DATA_DIR", str(tmp_path / "pytest_tmp_data"))
    assert LG.prod_lock_path() == ROOT / "data" / CFG.LOCK_FILE_NAME, \
        "生产锁位随 LG_DATA_DIR 漂移=P1 错位复发"
    # live 侧跟随本进程 DATA_DIR（conftest/测试用 monkeypatch 隔离）
    monkeypatch.setattr(CFG, "DATA_DIR", tmp_path / "live_data")
    assert LG.lock_path() == CFG.live_lock_path()
    assert LG.lock_path() == tmp_path / "live_data" / CFG.LOCK_FILE_NAME
    # LG_LOCK_DIR 一个旋钮，两侧同移（子进程协议测试的隔离通道）
    monkeypatch.setenv("LG_LOCK_DIR", str(tmp_path / "lock_dir"))
    assert LG.prod_lock_path() == LG.lock_path() \
        == tmp_path / "lock_dir" / CFG.LOCK_FILE_NAME


# ── 2. 方向 A：pytest 持锁 ⇒ live 启动被拒 ──────────────────────

def test_direction_a_live_refused_while_pytest_holds_lock(tmp_path):
    """pytest 侧整轮锁（purpose="pytest"，conftest 同款）持有期间，
    另起**真实子进程** live：必须非零退出、原因明确指向 pytest 持锁，
    live 侧未发起（无 CHILD_LIVE_STARTED）、pytest 锁原封不动。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    with LG.whole_run_lock("测试内 pytest 整轮持锁", watch=lock_file):
        assert json.loads(lock_file.read_text(encoding="utf-8")) \
            ["purpose"] == LG.PYTEST_LOCK_PURPOSE
        r = _run_live_child(tmp_path, LIVE_TRY)
        out = r.stdout + r.stderr
        assert r.returncode != 0, \
            "pytest 持锁期间 live 竟启动了——P1「只查不持」竞态复发"
        assert "互斥守卫" in out, f"拒绝须给明确原因：{out}"
        assert LG.PYTEST_LOCK_PURPOSE in out, f"原因须指明 pytest 持锁：{out}"
        assert "CHILD_LIVE_STARTED" not in r.stdout
        assert json.loads(lock_file.read_text(encoding="utf-8")) \
            ["purpose"] == LG.PYTEST_LOCK_PURPOSE, "live 被拒后不得动 pytest 的锁"
    assert not lock_file.exists(), "整轮结束不得残留锁"


# ── 3. 方向 B：live 持锁 ⇒ pytest 启动明确被拒（跨进程真 pytest）──

def test_direction_b_pytest_refused_while_live_holds_lock(tmp_path):
    """真实 live 子进程持锁（握手确保已持锁）→ 子进程真跑 `python -m
    pytest`：其 conftest 在 pytest_configure 检锁拒跑——非零退出、输出
    含互斥守卫原因；live 的锁不被 pytest 侧删除。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    ready, release = tmp_path / "child.ready", tmp_path / "child.release"
    holder = subprocess.Popen(
        [sys.executable, "-c", LIVE_HOLD], cwd=str(ROOT),
        env=_child_env(tmp_path, LG_CHILD_READY=ready, LG_CHILD_RELEASE=release),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        # 主控修正（2026-09-23）：原交付把 bool 当可调用传入（ready.exists()
        # 已求值）→ _wait_until 内 cond() 抛 TypeError，方向 B 必红。
        _wait_until(lambda: ready.exists(), "live 子进程持锁就绪")
        r = _run_pytest_child(tmp_path, "child_probe")
        out = r.stdout + r.stderr
        assert r.returncode != 0, \
            f"live 持锁时 pytest 静默放行——守卫失能：{out[-800:]}"
        assert "互斥守卫" in out, f"pytest 拒跑须给明确原因：{out[-800:]}"
        assert "1 passed" not in out, "被拒的 pytest 不得进入测试阶段"
        held = json.loads(lock_file.read_text(encoding="utf-8"))
        assert held["purpose"] == "t-child-live", "pytest 被拒时不得动 live 的锁"
    finally:
        release.touch()
        try:
            so, se = holder.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            holder.kill()
            so, se = holder.communicate(timeout=TIMEOUT_S)
    assert "CHILD_LIVE_RELEASED" in (so or ""), f"live 握手释放异常：{se}"
    assert not lock_file.exists(), "live 正常退出后不得残留锁"


# ── 4. 方向 C：pytest 整轮结束后锁无残留、live 可正常获取 ────────

def test_direction_c_live_can_lock_after_pytest_session(tmp_path):
    """无 live 干扰时子进程真 pytest 一轮：正常通过（证明 configure 取锁
    成功）；退出后锁位**无残留**（sessionfinish 释放）；随后 live 子进程
    可在同一锁位正常取锁-释放。"""
    lock_file = tmp_path / CFG.LOCK_FILE_NAME
    r = _run_pytest_child(tmp_path, "child_probe")
    assert r.returncode == 0, f"无并发时子进程 pytest 应正常通过：{r.stdout[-800:]}{r.stderr[-800:]}"
    assert "1 passed" in r.stdout, \
        f"探针用例须真正执行（锁应被持有过）：{r.stdout[-800:]}"
    assert not lock_file.exists(), \
        "pytest 整轮结束（pytest_sessionfinish）后锁残留——live 将被误拒"
    r2 = _run_live_child(tmp_path, LIVE_TRY)
    assert r2.returncode == 0, f"pytest 结束后 live 应可正常取锁：{r2.stdout}{r2.stderr}"
    assert "CHILD_LIVE_STARTED" in r2.stdout
    assert not lock_file.exists(), "live 取锁-释放循环后不得残留"


# ── 5. 真竞态：双 live 同刻起跑，O_EXCL 恰好一个赢 ───────────────

def test_race_two_live_children_o_excl_one_winner(tmp_path):
    """两进程同一 GO 门同时冲锁：恰一个成功（CHILD_ACQUIRED、exit 0），
    另一个被 O_EXCL 拒绝（互斥守卫、非零）——不存在双持锁窗口。"""
    go = tmp_path / "child.go"
    kids = [subprocess.Popen(
        [sys.executable, "-c", LIVE_RACE], cwd=str(ROOT),
        env=_child_env(tmp_path, LG_CHILD_GO=go),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace") for _ in range(2)]
    try:
        go.touch()
        results = [k.communicate(timeout=TIMEOUT_S) for k in kids]
    finally:
        for k in kids:
            if k.poll() is None:
                k.kill()
    codes = [k.returncode for k in kids]
    started = [o for (o, _e), c in zip(results, codes)
               if c == 0 and "CHILD_ACQUIRED" in o]
    refused = [(o + e) for (o, e), c in zip(results, codes)
               if c != 0 and "互斥守卫" in o + e]
    assert len(started) == 1 and len(refused) == 1, \
        f"O_EXCL 竞态破口：codes={codes}, 输出={results}"


# ── 探针用例：供方向 B/C 的子进程 pytest 以 -k child_probe 命中。
#    在子进程里钉住「测试阶段进行中，本 pytest 进程确实持有整轮锁」
#    （pytest_configure 取锁、尚未释放）；父轮里为纯 no-op。

def test_child_probe_noop_for_nested_pytest_runs():
    lock_dir = os.environ.get("LG_LOCK_DIR")
    if not lock_dir:
        return                                    # 父轮正常收集，无副作用
    p = Path(lock_dir) / CFG.LOCK_FILE_NAME
    info = json.loads(p.read_text(encoding="utf-8"))
    assert info["purpose"] == LG.PYTEST_LOCK_PURPOSE, \
        "测试阶段 pytest 侧未持有整轮锁——只查不持复发"
    assert info["pid"] == os.getpid(), "整轮锁持有者须是本 pytest 进程"
