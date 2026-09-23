"""live/pytest 并发窗互斥守卫回归（app/live_guard.py，会审 89f779e R6 待办
落地；9e02916 会审 7 条整改）。

钉住的事：
1. 互斥：锁被持有时，第二个 live 入口与 pytest 入口都必须 fail-fast 拒绝；
2. 释放：with 正常/异常退出都释放；**保守释放**——运行中锁被人工清除、
   他人接管后，前一实例退出不得删掉接管者的锁（pid+purpose 比对）；
3. 两侧同锁口径（会审严重项钉死）：pytest 侧默认盯**生产锁位**
   repo/data/live_run.lock（conftest 已把测试 DATA_DIR 覆写成临时目录，
   盯 config.DATA_DIR 会恰好看不到生产 live 的锁）；
4. 原子性（O_EXCL）：不存在「两个 live 同时拿到」的竞态窗口；
5. 损坏锁按「存在」处理（宁可拦，不可猜）；测试全部隔离锁路径（绝不
   触碰真实生产锁位）；
6. 顺序钉（driver 侧，2026-09-23 回归）：live 驱动的互斥守卫必须**前置**
   于任何客户端构造/模型解析/预检/init_db——锁在 ⇒ 守卫先抛，下游副作用
   路径零触达（计数替身断言，与环境是否配置网关无关）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import live_guard as LG                       # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """锁路径隔离（9e02916 会审一般项）：全部用例盯 tmp_path，绝不写/删
    真实生产锁位。"""
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    assert LG.live_run_active() is None, "前置：隔离目录不应有锁"


def test_lock_exclusive_release_cycle():
    with LG.live_lock("t-first"):
        held = LG.live_run_active()
        assert held["purpose"] == "t-first" and "pid" in held
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-second"):     # 持锁期间第二个 live 必须被拒
                pass
    assert LG.live_run_active() is None, "with 退出必须释放锁"
    with LG.live_lock("t-again"):              # 释放后可再获取
        pass
    assert LG.live_run_active() is None


def test_lock_released_on_exception():
    with pytest.raises(RuntimeError, match="boom"):
        with LG.live_lock("t-crash"):
            raise RuntimeError("boom")
    assert LG.live_run_active() is None, "异常退出也必须释放锁"


def test_release_only_deletes_own_lock():
    """保守释放（9e02916 会审一般项）：运行中锁被人工清除、他人接管后，
    前一实例退出不得删掉接管者的锁。"""
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


def test_refuse_watches_provided_path():
    LG.refuse_if_live_running("pytest", watch=LG.lock_path())   # 无锁放行
    with LG.live_lock("t-held"):
        with pytest.raises(SystemExit, match="live/pytest 互斥守卫"):
            LG.refuse_if_live_running("全量 pytest", watch=LG.lock_path())
    LG.refuse_if_live_running("pytest", watch=LG.lock_path())   # 释放后恢复


def test_refuse_default_watches_prod_path_not_test_dir():
    """两侧同锁口径不变式（9e02916 会审严重项的钉死）：pytest 侧默认盯
    **生产锁位**（repo/data/live_run.lock）——测试侧 DATA_DIR 里的锁
    （conftest 覆写场景）不影响生产口径检查；生产位构造与 config 默认
    DATA_DIR 同源。生产位本身绝不创建/删除，只钉不变式。"""
    from app import config
    assert LG.prod_lock_path() == \
        Path(config.ROOT) / "data" / "live_run.lock"
    with LG.live_lock("t-testdir"):             # 锁在测试 DATA_DIR（tmp）
        LG.refuse_if_live_running("pytest")     # 默认盯生产位 → 不受影响
    assert LG.prod_lock_path().name == "live_run.lock"


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
            LG.refuse_if_live_running("pytest", watch=p)
    finally:
        p.unlink(missing_ok=True)               # 测试自清，不留死锁给后续会话
    assert LG.live_run_active() is None


def test_k2_live_guard_precedes_preflight_and_client(monkeypatch):
    """顺序钉（k2 driver 侧，2026-09-23 回归，与 k4 同口径）：--live 锁被
    持有时守卫必须先抛——网络预检 require_models、客户端构造、init_db、
    run_backfill 全部零触达。环境无关：下游路径都是计数替身，任一被触达
    即 AssertionError 红——不依赖本机是否配置网关。"""
    import importlib.util as _u
    spec = _u.spec_from_file_location(
        "k2_guard_order", ROOT / "scripts" / "k2_extract_backfill.py")
    k2b = _u.module_from_spec(spec)
    spec.loader.exec_module(k2b)               # 顶层只建路径与 import，不执行
    import preflight_models as PF
    touches = {"preflight": 0, "client": 0, "init_db": 0, "run": 0}

    def _boom(key):
        def _f(*a, **k):
            touches[key] += 1
            raise AssertionError(
                f"锁被持有时不许触达 {key}——互斥守卫必须前置于一切构造/预检")
        return _f
    monkeypatch.setattr(PF, "require_models", _boom("preflight"))
    monkeypatch.setattr(k2b, "_GatewayAdapter", _boom("client"))
    monkeypatch.setattr(k2b.db, "init_db", _boom("init_db"))
    monkeypatch.setattr(k2b, "run_backfill", _boom("run"))
    monkeypatch.setenv("K2_ALLOW_LIVE", "1")
    monkeypatch.setattr(k2b, "LLM_MODE", "real")   # live 前提（替身与 mock/real 无关）
    monkeypatch.setattr(sys, "argv", ["k2", "--live",
                                      "--extractor-model", "m", "--limit", "1"])
    with LG.live_lock("t-other-live"):
        with pytest.raises(SystemExit, match="互斥守卫"):
            k2b.main()
    assert touches == {"preflight": 0, "client": 0, "init_db": 0, "run": 0}, \
        f"守卫未前置，下游被触达：{touches}"
