"""live/pytest 并发窗互斥守卫回归（app/live_guard.py，会审 89f779e R6 待办落地）。

钉住的事：
1. 互斥：锁被持有时，第二个 live 入口与 pytest 入口都必须 fail-fast 拒绝
   （不排队、不静默重叠）；
2. 释放：with 正常退出/异常退出都释放；释放后可再获取；
3. 原子性：O_EXCL 创建——不存在「两个 live 同时拿到」的竞态窗口；
4. 损坏锁也按「存在」处理（宁可拦，不可猜）；死锁只提示人工核清，不自动删。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import live_guard as LG                       # noqa: E402


def test_lock_exclusive_release_cycle():
    assert LG.live_run_active() is None, "前置：会话库不应残留 live 锁"
    with LG.live_lock("t-first"):
        held = LG.live_run_active()
        assert held["purpose"] == "t-first" and "pid" in held
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-second"):      # 持锁期间第二个 live 必须被拒
                pass
    assert LG.live_run_active() is None, "with 退出必须释放锁"
    with LG.live_lock("t-again"):          # 释放后可再获取
        pass
    assert LG.live_run_active() is None


def test_lock_released_on_exception():
    with pytest.raises(RuntimeError, match="boom"):
        with LG.live_lock("t-crash"):
            raise RuntimeError("boom")
    assert LG.live_run_active() is None, "异常退出也必须释放锁"


def test_refuse_if_live_running_reports_lock():
    assert LG.live_run_active() is None
    LG.refuse_if_live_running("pytest")    # 无锁：no-op（不抛）
    with LG.live_lock("t-held"):
        with pytest.raises(SystemExit, match="live/pytest 互斥守卫"):
            LG.refuse_if_live_running("全量 pytest")
    LG.refuse_if_live_running("pytest")    # 释放后恢复


def test_corrupt_lock_still_blocks():
    p = LG.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("不是json", encoding="utf-8")
    try:
        held = LG.live_run_active()
        assert held is not None and held.get("corrupt_lock") is True, \
            "损坏锁必须按「存在」处理——宁可拦，不可猜"
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-corrupt"):     # 损坏锁同样必须拒绝新 live
                pass
        with pytest.raises(SystemExit, match="互斥守卫"):
            LG.refuse_if_live_running("pytest")
    finally:
        p.unlink(missing_ok=True)           # 测试自清，不留死锁给后续会话
    assert LG.live_run_active() is None
