"""测试专用环境：独立临时 SQLite + mock LLM，必须在任何 app import 前设好。"""
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
