"""全库编译检查（2026-09-16）。

**为什么需要这个测试**：本项目反复（已 4 次）在写脚本时把中文全角引号
`“”` 放进 Python 的双引号字符串里，产生语法错误。这类错误：
- 语法层面**必然**能被发现（编译不过），却总在"跑起来才发现"；
- 每次都在浪费一次执行往返。

所以把"所有 .py 都要能编译"做成测试。这比 grep 可靠（grep 认不出全角引号
是否在字符串字面量里），也比人工 review 便宜。

排除 `.workbuddy-ai/`（工具目录）与虚拟环境。
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".workbuddy-ai"}


def _py_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_PARTS for part in p.parts):
            continue
        out.append(p)
    return sorted(out)


def test_all_python_files_compile():
    files = _py_files()
    assert len(files) > 20, f"只找到 {len(files)} 个 .py，路径可能不对：{ROOT}"
    bad = []
    for p in files:
        try:
            py_compile.compile(str(p), doraise=True, cfile=None)
        except py_compile.PyCompileError as e:
            # 只报相对路径 + 末行错误，避免刷屏
            bad.append(f"{p.relative_to(ROOT)}: {str(e).strip().splitlines()[-1][:160]}")
    assert not bad, ("以下文件编译失败（最常见原因：把中文全角引号「“”」当成了"
                     "Python 字符串的定界符）：\n  " + "\n  ".join(bad))

# 注：曾想再加一条"扫描全角引号"的检查，但它会误伤**合法**用法，例如
# `_PUNCTS = "，。！？；：“”"`（全角标点是数据，不是定界符）。
# 有假阳性的测试比没有测试更糟——会让人习惯性忽略失败。故只保留编译检查：
# 真正有害的那种写法必然是语法错误，编译检查能精确抓到。
