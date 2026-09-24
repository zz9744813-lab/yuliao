"""测试专用环境：独立临时 SQLite + mock LLM，必须在任何 app import 前设好。"""
import atexit
import gc

# 收尾段 GC 会卸载扩展 DLL：SQLAlchemy 2.0.44 + greenlet 的 instrumented C 对象在模块
# teardown 之后被遍历，触发 MSVCP140.dll 崩溃（退出码 0xC0000409，不是测试失败）。
# 全程关 GC，退出前把仍存活的对象冻结进永久代，避开这条收尾遍历。
gc.disable()
atexit.register(gc.freeze)

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="lg_test_"))
os.environ["LG_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["LG_DATA_DIR"] = str(_TMP)
os.environ["LG_LLM_MODE"] = "mock"
# 关掉远程访问门：TestClient 的客户端地址是 "testclient"（非 127.0.0.1），
# 门会把它判为远程而返回 401，导致所有接口测试变红。
# 访问门本身由 tests/test_access_gate.py 直接构造 app 单独验证（不走这里）。
os.environ["REVIEW_NO_AUTH"] = "1"

# R6 live/pytest 互斥守卫（2026-09-23 审计 P1 收口：路径错位 + 整轮持锁）：
# - 与 live 侧同一跨进程互斥协议（app.live_guard：O_EXCL 原子取锁 + 回读
#   pid/purpose 保守释放，只删自己的锁）、同一固定锁路径——由
#   app/config.py 单点导出（config.prod_lock_path()：默认 repo/
#   data/live_run.lock，LG_LOCK_DIR 显式覆盖时两侧同时跟随）。上方覆写的
#   LG_DATA_DIR 临时目录**不是**观察/持锁目标——pytest 盯自己临时目录的锁
#   恰好看不到生产 live 的锁，即 P1「守卫形同虚设」的错位根源。
# - pytest_configure 取锁（live 已持锁则 fail-fast 拒跑且**不**持锁——绝不
#   删 live 的锁）、整轮持有，pytest_sessionfinish 释放；atexit 兜底异常
#   退出路径（释放幂等，重复调用不会误删继任者的锁）。
# - 只用既有 pytest hook：pluggy 会把 conftest 命名空间里 `pytest_*` 开头
#   的名字一律当钩子名校验，自定义名（上一版 pytest_running_lock）直接
#   PluginValidationError exit 3——守卫辅助函数不得用该前缀。
import pytest

from app.live_guard import refuse_if_live_running, whole_run_lock

_run_guard = None


def _release_run_guard() -> None:
    global _run_guard
    guard, _run_guard = _run_guard, None
    if guard is not None:
        guard.__exit__(None, None, None)


def pytest_configure(config) -> None:
    """整轮取锁：本会话开始时（收集前）检锁并持有生产锁位。

    拒跑出口用 pytest.exit（规范出口：打印原因 + 指定 returncode），不用裸
    SystemExit——裸 SystemExit 从 pytest_configure 冒泡时的退出码/输出行为
    不受 pytest 保证（实测一轮子进程 pytest 竟 exit 0 静默放行，方向 B 失归；
    pytest.exit 即使未被特判也会以 INTERNALERROR 非零收场，绝不静默）。"""
    global _run_guard
    try:
        refuse_if_live_running("全量 pytest")
        _run_guard = whole_run_lock("全量 pytest")
        _run_guard.__enter__()
    except SystemExit as e:
        pytest.exit(str(e) or "live/pytest 互斥守卫拒绝重叠", returncode=2)
    atexit.register(_release_run_guard)


def pytest_sessionfinish(session, exitstatus) -> None:
    """整轮结束释放（内部只删自己的锁；未持锁时为 no-op）。"""
    _release_run_guard()
