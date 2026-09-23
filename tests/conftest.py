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

# R6 守卫（会审 89f779e；9e02916 会审修正：两侧看同一把锁）：live 实跑进行中
# 拒跑全量 pytest——全绿结论不许被并发 live 污染（2026-09-22 瞬态红教训）。
# 共享单实现（app.live_guard.refuse_if_live_running，损坏锁安全、盯**生产**
# 锁位 repo/data/live_run.lock——本文件上方已把测试 DATA_DIR 覆写成 _TMP，
# 盯 config.DATA_DIR 会恰好看不到生产 live 的锁）。env 已设完，此处 import
# app 安全（与测试模块同序）。
from app.live_guard import refuse_if_live_running
refuse_if_live_running("全量 pytest")
