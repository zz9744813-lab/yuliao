"""live/pytest 互斥守卫——「死 pid 锁」自愈回归（2026-09-25 事故收口）。

事故（docs/live锁死pid自愈_20260925.md）：2026-09-25 11:40 主控全量
pytest 被拒，锁 data/live_run.lock 内容合法（purpose/pid/started_at 齐
全）但 pid 29828 早已不存在（硬杀残留，锁在盘上停 44 分钟把整套测试
brick 掉，只能人工删锁）。根因：既有两条自愈路径都覆盖不到「合法 JSON +
死 pid」——_corrupt_and_stale() 只在内容不可解析时生效，refuse_if_live_
running()/ _acquire_lock() 对能解析的锁一律硬拦。

本文件钉死的自愈口径（app/live_guard.py `_dead_pid_and_stale`，与损坏锁
超宽限**同口径**）：
  锁内容合法 + 锁内 pid 在本机进程表中**确定不存在** + 锁文件 mtime 超过
  CORRUPT_LOCK_GRACE_SECONDS → 判定为硬杀崩溃残留，记 warning、自愈接管
  （refuse 放行 / 取锁方 unlink 后 O_EXCL 重建）。三者缺一即拦：
  - pid 仍存在（包括被系统复用的无关进程）→ 按「有人持有」拦；
  - pid 查询失败 / 权限不足 / 平台无法核验 → 按「存在」拦，**不删锁**；
  - 锁文件新落盘（mtime 在宽限内）→ 按「存在」拦，宁可拦不可猜。

不变式守护：既有语义全部不回归——损坏锁宽限行为、_release_lock 保守释放
（pid+purpose 只删自己的锁）、whole_run_lock 整轮持锁、refuse_if_live_
running 的 watch 参数语义。
全部用例隔离开临时锁位（tmp_path + 覆写 config.DATA_DIR），**绝不触碰
真实生产锁位** F:/agi/language-genome/data/live_run.lock。
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import live_guard as LG                       # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """锁路径隔离：全部用例盯 tmp_path，绝不写/删真实生产锁位。"""
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LG_LOCK_DIR", raising=False)
    assert LG.live_run_active() is None, "前置：隔离目录不应有锁"


def _dead_pid() -> int:
    """拿到一个**确定不存在**的 pid：helper 子进程产一个短命孙进程后自身
    退出（所有句柄随之关闭、进程对象销毁），孙进程 pid 从进程表释放；
    再经 os.kill(pid, 0) 实证其确实不在才返回（防 pid 被即时复用）。"""
    helper = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', ''], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
        "p.wait()\n"
        "del p\n"
    )
    for _ in range(30):
        try:
            out = subprocess.run([sys.executable, "-c", helper],
                                 capture_output=True, text=True,
                                 timeout=60, errors="replace")
        except subprocess.TimeoutExpired:
            continue
        try:
            pid = int(out.stdout.strip())
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError as e:                      # Windows 死 pid：EINVAL/87
            if e.errno == errno.EINVAL or getattr(e, "winerror", None) == 87:
                return pid
        except Exception:                         # noqa: BLE001
            pass
        time.sleep(0.05)
    pytest.fail("无法获得确定不存在的 pid（pid 被反复即时复用）")


def _write_lock(p: Path, purpose: str, pid: int, *, fresh: bool = True,
                age: float | None = None) -> None:
    """写内容合法的锁；fresh=False 时把 mtime 拨到自愈宽限之外（与既有
    损坏锁超宽限测试同手法：os.utime 拨 mtime）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    info = {"purpose": purpose, "pid": pid,
            "started_at": "2026-09-25T10:57:05"}
    p.write_text(json.dumps(info), encoding="utf-8")
    if not fresh:
        old = time.time() - (age if age is not None
                             else LG.CORRUPT_LOCK_GRACE_SECONDS + 1)
        os.utime(p, (old, old))


def test_valid_json_dead_pid_lock_refuse_passes_not_bricked():
    """事故主回归：内容合法 + 死 pid + 超宽限 → refuse_if_live_running
    **放行**（不再 brick 整个套件）并记 warning；取锁方可接管。"""
    p = LG.lock_path()
    dpid = _dead_pid()
    _write_lock(p, LG.PYTEST_LOCK_PURPOSE, dpid, fresh=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        LG.refuse_if_live_running("复现 全量 pytest", watch=p)   # 不放 SystemExit
    assert any("死 pid" in str(w.message) or "不存在" in str(w.message)
               for w in caught), f"自愈接管必须记 warning：{caught}"
    # refuse 只放行；unlink+O_EXCL 重建由随后的取锁完成（与损坏锁同口径）
    with LG.whole_run_lock("pytest", watch=p):
        held = json.loads(p.read_text(encoding="utf-8"))
        assert held["purpose"] == LG.PYTEST_LOCK_PURPOSE
        assert held["pid"] == os.getpid(), "接管必须是真的 O_EXCL 重建（写入我方 pid）"
    assert LG.live_run_active() is None, "接管-释放后锁位无残留"


def test_valid_json_dead_pid_lock_live_lock_takeover():
    """事故第二形态：死 pid 锁挡不住 live_lock——`with live_lock` 正常拿到
    锁（O_EXCL 接管重建），退出释放。"""
    p = LG.lock_path()
    dpid = _dead_pid()
    _write_lock(p, "k2_extract_backfill", dpid, fresh=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with LG.live_lock("t-takeover"):
            held = LG.live_run_active()
            assert held["pid"] == os.getpid(), "接管后锁里必须是我方 pid"
    assert any("死 pid" in str(w.message) for w in caught)
    assert LG.live_run_active() is None


def test_live_pid_lock_still_blocked():
    """不许因加了死 pid 判定就把活锁也放行：锁内 pid=本进程 pid（确定活）
    + mtime 拨超宽限 → refuse 与 live_lock **仍必须拦**，且锁原封不动
    （不删、不覆盖）。"""
    p = LG.lock_path()
    _write_lock(p, "t-live", os.getpid(), fresh=False)
    with pytest.raises(SystemExit, match="互斥守卫"):
        LG.refuse_if_live_running("pytest", watch=p)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="互斥守卫"):
        with LG.live_lock("t-x"):
            pass
    assert p.read_text(encoding="utf-8") == before, "活锁被拒后不得被改/删"
    # 跨进程实证：真实存活子进程的 pid 同样必须拦
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _write_lock(p, "t-child-live", child.pid, fresh=False)
        with pytest.raises(SystemExit, match="互斥守卫"):
            LG.refuse_if_live_running("pytest", watch=p)
        assert json.loads(p.read_text(encoding="utf-8"))["pid"] == child.pid
    finally:
        child.terminate()
        child.wait(timeout=30)


def test_pid_query_failure_blocks_and_never_deletes():
    """pid 存活查询失败/异常 → 一律按「存在」拦，**绝不删锁**（宁可拦，
    不可猜——与既有损坏锁口径一致）。"""
    p = LG.lock_path()
    dpid = _dead_pid()
    _write_lock(p, "k2_extract_backfill", dpid, fresh=False)
    # 场景 1：查询函数本体意外抛错
    def _boom(*a, **k):
        raise RuntimeError("pid 查询不可用")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(LG, "_pid_looks_live", _boom)
    try:
        with pytest.raises(SystemExit, match="互斥守卫"):
            LG.refuse_if_live_running("pytest", watch=p)
        assert p.exists(), "查询失败时不许删锁"
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-x"):
                pass
        assert p.exists(), "查询失败时 live_lock 不许接管/删锁"
    finally:
        monkeypatch.undo()
    # 场景 2：底层 os.kill(pid,0) 抛异常（权限/平台不支持）
    def _kill_boom(pid, sig):
        raise PermissionError(13, "Access is denied")
    monkeypatch2 = pytest.MonkeyPatch()
    monkeypatch2.setattr(LG.os, "kill", _kill_boom)
    try:
        with pytest.raises(SystemExit, match="互斥守卫"):
            LG.refuse_if_live_running("pytest", watch=p)
        assert p.exists(), "os.kill 抛异常（按存在拦）时不许删锁"
    finally:
        monkeypatch2.undo()
    before = p.read_text(encoding="utf-8")
    assert json.loads(before)["pid"] == dpid


def test_fresh_dead_pid_lock_still_blocks_within_grace():
    """新落盘的死 pid 锁（mtime 在宽限内）→ 按「存在」硬拦、不删——与损坏
    锁「宽限内宁拦不可猜」同口径（也保住既有 test_live_blocks_while_
    pytest_lock_held 用 pid 999999999 模拟的活锁语义）；mtime 拨超宽限后
    才自愈，证明死 pid 判定确实依赖「超宽限」这一防误判条件。"""
    p = LG.lock_path()
    dpid = _dead_pid()
    _write_lock(p, LG.PYTEST_LOCK_PURPOSE, dpid, fresh=True)
    with pytest.raises(SystemExit, match="互斥守卫"):
        LG.refuse_if_live_running("pytest", watch=p)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="互斥守卫"):
        with LG.live_lock("t-x"):
            pass
    assert p.read_text(encoding="utf-8") == before, "宽限内不许删锁"
    old = time.time() - LG.CORRUPT_LOCK_GRACE_SECONDS - 1
    os.utime(p, (old, old))                       # mtime 拨过宽限 → 才自愈
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        LG.refuse_if_live_running("pytest", watch=p)
    with LG.whole_run_lock("pytest", watch=p):
        pass
    assert LG.live_run_active() is None


def test_pid_not_int_lock_blocks_not_deleted():
    """pid 字段不是 int（不可核验）→ 交给既有「存在」口径硬拦、不删——
    自愈只认「确定不存在」，不猜。"""
    p = LG.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"purpose": "t-strpid", "pid": "29828",
                             "started_at": "2026-09-25T10:57:05"}),
                 encoding="utf-8")
    with pytest.raises(SystemExit, match="互斥守卫"):
        LG.refuse_if_live_running("pytest", watch=p)
    assert p.exists() and p.read_text(encoding="utf-8") != "", "不可核验时不删锁"


def test_corrupt_lock_fresh_blocks_stale_selfheals_not_regression():
    """损坏锁既有行为不回归：宽限内拦（宁拦不可猜）/ 超宽限自愈接管。"""
    p = LG.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("不是json", encoding="utf-8")
    with pytest.raises(SystemExit, match="互斥守卫"):
        LG.refuse_if_live_running("pytest", watch=p)
    with pytest.raises(SystemExit, match="互斥守卫"):
        with LG.live_lock("t-corrupt"):
            pass
    old = time.time() - LG.CORRUPT_LOCK_GRACE_SECONDS - 1
    os.utime(p, (old, old))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        LG.refuse_if_live_running("pytest", watch=p)   # 超宽限不再拦
    assert any("损坏锁" in str(w.message) for w in caught)
    with LG.whole_run_lock("pytest", watch=p):          # 接管成功
        assert json.loads(p.read_text(encoding="utf-8"))["purpose"] == \
            LG.PYTEST_LOCK_PURPOSE
    assert LG.live_run_active() is None


def test_release_lock_conservative_after_takeover_not_regression():
    """_release_lock 保守释放不回归：运行中锁被人工清除、他人接管后，前一
    实例退出不得删掉接管者的锁（pid+purpose 比对只删自己的）。"""
    first = LG.live_lock("t-first")
    first.__enter__()
    p = LG.lock_path()
    p.unlink()                                 # 模拟人工清锁
    with LG.live_lock("t-second"):             # 接管者正常持锁
        first.__exit__(None, None, None)       # 前一实例退出
        held = LG.live_run_active()
        assert held and held["purpose"] == "t-second", \
            "前一实例退出删掉了接管者的锁——互斥被无声破坏"
    assert LG.live_run_active() is None