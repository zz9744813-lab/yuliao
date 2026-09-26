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
import time
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

from app.live_guard import prod_lock_path, refuse_if_live_running, whole_run_lock

_run_guard = None

# OPEN-4（2026-09-26 入册，测试卫生）：干净源检出（ROOT/data 尚不存在）里
# 裸跑 pytest，整轮锁位=ROOT/data，_acquire_lock 的 mkdir(parents=True) 会
# 在检出目录里**创建** data/——对「不在源检出里跑套件」的 reviewer/agent 是
# 意外副作用。收口双保险：
# 1) 「LG_LOCK_DIR 未设且锁目录原本不存在」时 pytest_configure 发响亮
#    warning（文本含 OPEN-4 / LG_LOCK_DIR 字面量，由 tests/test_live_guard_
#    mutex.py::test_open4_bare_pytest_warns_and_leaves_no_data_dir 在仿造
#    根里断言钉死）；跑套件建议先设 LG_LOCK_DIR=<临时目录>（见 docs/
#    R6守卫覆盖缺口入册第二批_OPEN4-5_20260926.md）。
# 2) 本轮创建的目录在 sessionfinish rmdir——只删空目录、只删本轮自己创建
#    的（非空/并发产物/他人目录一律保留）；LG_LOCK_DIR 显式覆盖时锁位在
#    检出之外，整条逻辑不适用。
_open4_cleanup_dir: Path | None = None


def _open4_notice_text(d: Path) -> str:
    return (
        "[R6 测试卫生 OPEN-4] 未设 LG_LOCK_DIR：本轮 pytest 以源检出目录 "
        f"{d} 为整轮锁位（将创建 {d / 'live_run.lock'}）——干净 worktree 里"
        "这是意外副作用。建议跑套件前设 LG_LOCK_DIR=<临时目录> 把锁隔离到"
        "检出之外；本轮若由本会话创建该目录，收尾时会自动移除空目录。")


def _release_run_guard() -> None:
    global _run_guard
    guard, _run_guard = _run_guard, None
    if guard is not None:
        guard.__exit__(None, None, None)


def _open4_remove_created_dir() -> None:
    """只 rmdir 本轮自创的空锁目录：非空（他人产物/并发会话）或已消失
    → 原样保留，绝不递归删。"""
    global _open4_cleanup_dir
    d, _open4_cleanup_dir = _open4_cleanup_dir, None
    if d is not None:
        try:
            d.rmdir()
        except OSError:
            pass


def pytest_configure(config) -> None:
    """整轮取锁：本会话开始时（收集前）检锁并持有生产锁位。

    拒跑出口用 pytest.exit（规范出口：打印原因 + 指定 returncode），不用裸
    SystemExit——裸 SystemExit 从 pytest_configure 冒泡时的退出码/输出行为
    不受 pytest 保证（实测一轮子进程 pytest 竟 exit 0 静默放行，方向 B 失归；
    pytest.exit 即使未被特判也会以 INTERNALERROR 非零收场，绝不静默）。"""
    global _run_guard, _open4_cleanup_dir
    lock_dir = prod_lock_path().parent
    if not (os.environ.get("LG_LOCK_DIR") or "").strip() \
            and not lock_dir.exists():
        # 判据必须在取锁（内部 mkdir）之前采集；取锁被拒时目录并未创建，
        # 收尾 rmdir 落空即 no-op。
        _open4_cleanup_dir = lock_dir
        config.issue_config_time_warning(
            UserWarning(_open4_notice_text(lock_dir)), stacklevel=2)
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
    _open4_remove_created_dir()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """OPEN-2 跨进程持锁窗（仅子进程设了 LG_CHILD_HOLD 时生效；其余会话纯
    no-op）：`call` 阶段报告产出后阻塞到放行文件出现——测试已跑完、teardown
    与 sessionfinish（上面释放整轮锁之处）都还没发生 ⇒ 父进程在该窗口内观测
    /抢锁时，整轮锁必仍在本真 pytest 子进程手里。

    **为何在 conftest 而不在测试模块（2026-09-26 主控亲修）**：pytest 只在
    conftest.py 与已注册插件里收集钩子，**测试模块里定义的 pytest_* 函数不
    会被注册**（pytest 9.1.1 实测：写在 tests/test_live_guard_mutex.py 里的
    hookwrapper 从不执行，子进程 pytest 跑完即退、锁已释放，主用例读锁文件
    直接 FileNotFoundError）。且不能靠 `pytest_runtest_call` 的 post-yield
    当窗口——持锁窗必须在「测试阶段报告已出、session 未收尾」处，本钩子的
    `when == "call"` 正是这一点。"""
    outcome = yield
    hold = os.environ.get("LG_CHILD_HOLD")
    if not hold or call.when != "call":
        return
    end = time.time() + 120                      # 兜底防呆：父进程失约也不永远挂
    while not os.path.exists(hold) and time.time() < end:
        time.sleep(0.05)
