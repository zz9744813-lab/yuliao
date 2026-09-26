"""live/pytest 并发窗互斥守卫回归（app/live_guard.py，会审 89f779e R6 待办
落地；9e02916 会审 7 条整改；2026-09-23 残留窗口收口）。

钉住的事：
1. 互斥：锁被持有时，第二个 live 入口与 pytest 入口都必须 fail-fast 拒绝；
2. 释放：with 正常/异常退出都释放；**保守释放**——运行中锁被人工清除、
   他人接管后，前一实例退出不得删掉接管者的锁（pid+purpose 比对）；
3. 两侧同锁口径（会审严重项钉死）：pytest 侧默认盯**生产锁位**
   repo/data/live_run.lock（conftest 已把测试 DATA_DIR 覆写成临时目录，
   盯 config.DATA_DIR 会恰好看不到生产 live 的锁）；
4. 原子性（O_EXCL）：不存在「两个 live 同时拿到」的竞态窗口；
5. 损坏锁：宽限内按「存在」拦（宁可拦，不可猜）；超宽限判定为崩溃残留
   自愈接管（不锁死套件）；测试全部隔离锁路径（绝不
   触碰真实生产锁位）；
6. 残留窗口收口（2026-09-23）：pytest 侧**整轮持锁**——pytest 持锁期间
   live 被拒、live 持锁期间 pytest 被拒（两方向）；损坏残留锁可自愈、
   不 brick 整个测试套件；
7. 顺序钉（driver 侧，2026-09-23 回归）：live 驱动的互斥守卫必须**前置**
   于任何客户端构造/模型解析/预检/init_db——锁在 ⇒ 守卫先抛，下游副作用
   路径零触达（计数替身断言，与环境是否配置网关无关）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import live_guard as LG                       # noqa: E402

# OPEN-5（2026-09-26 入册）：模块导入期采集外部 LG_LOCK_DIR 覆盖（autouse
# 夹具会逐用例 delenv 再恢复，会话级取值只能在这里采）。本文件的既有断言
# 钉的是**非覆盖**口径（conftest 整轮锁在 ROOT/data 生产位）；覆盖口径
# （一个旋钮两侧同跟随）由 test_live_guard_mutex.py::
# test_lock_path_single_source_exported_by_config 钉死，两不放松。
_ORIG_LOCK_DIR_OVERRIDE = (os.environ.get("LG_LOCK_DIR") or "").strip()


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """锁路径隔离（9e02916 会审一般项）：全部用例盯 tmp_path，绝不写/删
    真实生产锁位。

    OPEN-5 修正：config.live_lock_path() 里 LG_LOCK_DIR 环境变量的覆盖**优先
    于** monkeypatch 改 config.DATA_DIR——只覆 DATA_DIR 时，只要外部设了
    LG_LOCK_DIR，lock_path() 仍指覆盖目录，conftest 在该目录整轮持有的
    purpose="pytest" 锁会让前置断言炸掉全文件（主控实测 11 errors）。故必须
    先在夹具内显式清除该旋钮，隔离才真正生效。"""
    from app import config
    monkeypatch.delenv("LG_LOCK_DIR", raising=False)
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
    """两侧同锁口径不变式（9e02916 会审严重项钉死 + 2026-09-23 收口新行
    为）：pytest 侧默认盯**生产锁位** repo/data/live_run.lock。本 pytest
    进程的 conftest 已在生产位整轮持 purpose="pytest" 的锁——默认口径
    必须看到它并拦（恰证观察目标在生产位、与测试 DATA_DIR 互不相干）。
    生产位本身绝不创建/删除（conftest 守卫持有并自释放），只钉不变式。"""
    if _ORIG_LOCK_DIR_OVERRIDE:
        pytest.skip(
            "OPEN-5：本轮 pytest 在外部 LG_LOCK_DIR 覆盖下启动，conftest 的"
            "整轮锁随同一旋钮落在覆盖目录而非 ROOT/data 生产位——本用例钉的"
            "「非覆盖口径下默认盯生产位」前置不成立（断言原样保留、不放松）；"
            "覆盖口径的两侧同跟随不变式由 test_live_guard_mutex.py::"
            "test_lock_path_single_source_exported_by_config 钉死")
    from app import config
    assert LG.prod_lock_path() == \
        Path(config.ROOT) / "data" / "live_run.lock"
    with LG.live_lock("t-testdir"):            # 锁在测试 DATA_DIR（tmp）
        # conftest 的 pytest 守卫正持生产锁位 → 默认口径必须拦（收口行为）
        with pytest.raises(SystemExit, match="互斥守卫"):
            LG.refuse_if_live_running("pytest")
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

# ---------------------------------------------------------------------------
# 残留窗口收口回归（2026-09-23，审计 P1 余项）：pytest 侧整轮持锁
# ---------------------------------------------------------------------------

def test_pytest_holds_lock_while_running():
    """收口主回归：pytest_running_lock 持锁期间，live_lock 进入即
    SystemExit——「检查通过后 live 叠上来」的残留窗口不复存在。"""
    with LG.whole_run_lock("全量 pytest", watch=LG.lock_path()) as p:
        assert p == LG.lock_path()
        held = LG.live_run_active()
        assert held["purpose"] == LG.PYTEST_LOCK_PURPOSE and "pid" in held, \
            "pytest 必须真的持锁（非单向瞬时检查）"
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-live"):       # pytest 持锁期间 live 不得叠上
                pass
    assert LG.live_run_active() is None, "整轮结束（yield 退出）必须释放锁"


def test_live_blocks_while_pytest_lock_held():
    """模拟另一 pytest 进程已持锁（手工构造锁文件，不依赖本进程守卫）：
    live 被拒、未发起真跑、且不动 pytest 的锁。"""
    p = LG.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    info = {"purpose": LG.PYTEST_LOCK_PURPOSE, "pid": 999999999,
            "started_at": "2026-09-23T00:00:00"}
    p.write_text(json.dumps(info), encoding="utf-8")
    live_started = False
    try:
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-live"):
                live_started = True
        assert not live_started, "live 在 pytest 持锁期间发起了真跑"
        assert json.loads(p.read_text(encoding="utf-8"))["purpose"] == \
            LG.PYTEST_LOCK_PURPOSE, "live 被拒后不得动 pytest 的锁"
    finally:
        p.unlink(missing_ok=True)


def test_pytest_refuses_when_live_lock_held():
    """反方向：live 已持锁 → whole_run_lock 进入即 SystemExit（不
    覆盖、不等待），且 live 的锁原封不动；live 释放后 pytest 可获取。"""
    with LG.live_lock("t-live"):
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.whole_run_lock("全量 pytest", watch=LG.lock_path()):
                pass
        held = LG.live_run_active()
        assert held and held["purpose"] == "t-live", \
            "pytest 被拒时不得动 live 的锁"
    with LG.whole_run_lock("全量 pytest", watch=LG.lock_path()):
        pass                                    # live 释放后可正常获取
    assert LG.live_run_active() is None


def test_corrupt_lock_does_not_brick_pytest():
    """损坏锁口径（2026-09-23 收口选定）＝**按 mtime 宽限期自愈**：锁文件
    存在但不可解析（空文件/半截 JSON，典型于 O_EXCL 建文件与 json.dump
    写完之间进程被杀）——宽限 CORRUPT_LOCK_GRACE_SECONDS（15s）内按
    「存在」拦（宁可拦不可猜：可能是有进程正在写内容）；超宽限仍不可
    解析 → 判定为崩溃残留（健康进程毫秒级写完内容），记 warning 并按
    「不存在」处理、可被接管。理由：损坏锁无 pid 可核，一律硬拦会让一
    个空文件锁死所有 live 与整个 pytest 套件且只能人工清；宽限远大于正
    常写内容耗时，误接管概率可忽略——两害相权取「可自愈的边界」。"""
    p = LG.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # 崩溃窗口产物：O_EXCL 建文件成功、内容未写 → 空文件；mtime 拨到宽限外
    p.write_text("", encoding="utf-8")
    old = time.time() - LG.CORRUPT_LOCK_GRACE_SECONDS - 1
    os.utime(p, (old, old))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            LG.refuse_if_live_running("pytest", watch=p)   # 不再 SystemExit
        assert any("损坏锁" in str(w.message) for w in caught), \
            "自愈接管必须记 warning（可诊断）"
        with LG.whole_run_lock("pytest", watch=p):    # 接管成功
            assert json.loads(p.read_text(encoding="utf-8"))["purpose"] == \
                LG.PYTEST_LOCK_PURPOSE
        assert LG.live_run_active() is None, "接管-释放后套件不被锁死"
        # 反向钉死：宽限**内**的新鲜损坏锁仍按「存在」拦（宁可拦不可猜）
        p.write_text("{半截json", encoding="utf-8")
        with pytest.raises(SystemExit, match="互斥守卫"):
            LG.refuse_if_live_running("pytest", watch=p)
        with pytest.raises(SystemExit, match="互斥守卫"):
            with LG.live_lock("t-corrupt"):
                pass
    finally:
        p.unlink(missing_ok=True)               # 测试自清，不留死锁给后续会话
    assert LG.live_run_active() is None
