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

# R6 守卫（会审 89f779e，app/live_guard.py 的 pytest 侧）：live 实跑进行中
# 拒跑全量 pytest——全绿结论不许被并发 live 污染（2026-09-22 瞬态红教训）。
# 直查锁文件而不 import app（conftest 纪律：app import 前先设完环境）。
_live_lock = _TMP / "live_run.lock"
if _live_lock.exists():
    raise SystemExit(
        "[live/pytest 互斥守卫] live 实跑进行中（"
        + _live_lock.read_text(encoding="utf-8")[:200]
        + "）——拒绝并发 pytest（R6 纪律）。等 live 结束；若 live 已崩溃"
          "遗留死锁，人工核实后清除：" + str(_live_lock))
