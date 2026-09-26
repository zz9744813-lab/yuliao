"""研究台（websrc 16 页）契约测试 —— 2026-09-19。

为什么有这个文件：HANDOVER §38 记着一条实账——
「前端没有构建产物也没有类型检查，"某个功能被顺手删掉"没有任何机制能发现。
2026-09-16 的 UI 重写就删掉了跨实验取题逻辑（batchMulti），后果是实的。」

websrc 16 页入库当天就是死页（不被 serve、10 页 slug 错位、信封未解、数据形状不符），
根因正是"没有任何机制能发现它接错了"。本文件把四类契约钉死：

A. **路由契约** —— 16 页可服务、no-store、未知页与目录穿越必须 404；
B. **slug 契约** —— lg.js 的模块表必须与后端 console.MODULE_ORDER **逐位相同**，
   每页声明的模块必须与文件一一对应（这是 10 页 404 的直接防线）；
C. **共享层契约** —— 每页引用 tokens.css / base.css / lg.js，且不留内联 <style>
   （防止有人把共享层又抄回页面里、色值再次分叉）；
D. **纪律契约** —— 不引外部 CDN（公网隧道暴露面）、本地存储只用 lg_* 命名空间
   （不碰盲评台的 rv_*）、页面只读（无写动词）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import console, db
from app.main import app

db.init_db()
client = TestClient(app)

WEBSRC = ROOT / "websrc"

# 页面 ↔ 后端模块 slug 的权威映射（与 app/api.py::_LAB_PAGES 同期维护）
PAGE_SLUG = {
    "overview.html": "dashboard",
    "corpus.html": "corpus",
    "frames.html": "semantic-lab",
    "arena.html": "reconstruction-arena",
    "residual.html": "expression-residual",
    "strategy.html": "strategy-atlas",
    "judges.html": "judge-arena",
    "preference.html": "preference-lab",
    "hardcase.html": "hard-cases",
    "benchmark.html": "benchmarks",
    "experiments.html": "experiments",
    "models.html": "models",
    "training.html": "training-data",
    "workflow.html": "workflow",
    "observability.html": "observability",
    "settings.html": "settings",
}


def _src(page: str) -> str:
    return (WEBSRC / page).read_text(encoding="utf-8")


# ── A 路由契约 ────────────────────────────────────────────────

def test_all_sixteen_pages_exist_on_disk():
    for page in PAGE_SLUG:
        assert (WEBSRC / page).is_file(), f"研究台缺页：{page}"


def test_lab_route_serves_every_page_with_no_store():
    for page in PAGE_SLUG:
        r = client.get(f"/lab/{page}")
        assert r.status_code == 200, (page, r.status_code)
        assert r.headers.get("cache-control") == "no-store", \
            f"{page} 缺 no-store —— 改完页面会被浏览器钉在旧版（Round 4 假阴性根因）"


def test_lab_root_redirects_to_dashboard_page():
    r = client.get("/lab", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/lab/overview.html"


def test_lab_unknown_page_is_404():
    assert client.get("/lab/nope.html").status_code == 404
    assert client.get("/lab/console.html").status_code == 404, "白名单外的页不许经 /lab 取"


def test_lab_path_traversal_is_blocked():
    for evil in ("/lab/..%2fapp%2fapi.py", "/lab/....//app/api.py", "/lab/%2e%2e/access.py"):
        assert client.get(evil).status_code == 404, evil


def test_shared_assets_are_served():
    for asset in ("tokens.css", "base.css", "editorial.css", "lg.js", "editorial.js"):
        r = client.get(f"/lab/_shared/{asset}")
        assert r.status_code == 200, asset
        assert len(r.text) > 200, f"{asset} 内容过短，可能被截断"


# ── B slug 契约 ───────────────────────────────────────────────

def test_lg_module_table_matches_backend_order_exactly():
    """lg.js 的 MODULES 顺序必须与 console.MODULE_ORDER 逐位相同。"""
    src = (WEBSRC / "_shared" / "lg.js").read_text(encoding="utf-8")
    block = re.search(r"var MODULES = \[(.*?)\n  \];", src, re.S)
    assert block, "lg.js 里找不到 MODULES 表"
    slugs = re.findall(r"slug: '([a-z\-]+)'", block.group(1))
    assert slugs == list(console.MODULE_ORDER), \
        f"导航顺序与后端不一致\n前端={slugs}\n后端={list(console.MODULE_ORDER)}"


def test_lg_module_table_pages_exist():
    src = (WEBSRC / "_shared" / "lg.js").read_text(encoding="utf-8")
    block = re.search(r"var MODULES = \[(.*?)\n  \];", src, re.S).group(1)
    pages = re.findall(r"page: '([a-z_]+\.html)'", block)
    assert pages == list(PAGE_SLUG), f"lg.js 页面清单与权威映射不一致：{pages}"


def test_each_page_declares_its_own_module():
    """每页 LG.init('<slug>') 必须等于该文件应属的 slug（10 页 404 的直接防线）。"""
    for page, slug in PAGE_SLUG.items():
        m = re.search(r"LG\.init\('([a-z\-]+)'", _src(page))
        assert m, f"{page} 没有调用 LG.init('<slug>', …)"
        assert m.group(1) == slug, f"{page} 声明的模块是 {m.group(1)}，应为 {slug}"


# ── C 共享层契约 ──────────────────────────────────────────────

def test_every_page_links_shared_assets():
    for page in PAGE_SLUG:
        src = _src(page)
        for asset in ("tokens.css", "base.css", "editorial.css"):
            assert f"/lab/_shared/{asset}" in src, f"{page} 未引用 {asset}"
        assert "/lab/_shared/lg.js" in src, f"{page} 未引用 lg.js"
        assert "/lab/_shared/editorial.js" in src, f"{page} 未引用 editorial.js"


def test_no_inline_style_blocks_left():
    """共享层抽取后的防线：页面不该再自带 <style> 块（色值一分叉就白干了）。"""
    for page in PAGE_SLUG:
        assert "<style" not in _src(page), \
            f"{page} 又出现内联 <style> —— 样式请回 _shared/tokens.css 或 base.css"


def test_no_hardcoded_github_dark_palette():
    """集霸决策 ①A：弃用 websrc 原 GitHub Dark 蓝。回归即红。"""
    for page in PAGE_SLUG:
        src = _src(page)
        for bad in ("#58a6ff", "#0e1116", "#161b22", "#c9d1d9"):
            assert bad not in src, f"{page} 残留 GitHub Dark 色值 {bad}"


# ── D 纪律契约 ────────────────────────────────────────────────

def test_no_external_cdn_references():
    """服务经公网隧道暴露（9-16 审计标过外部暴露风险），一律不引外部资源。"""
    for page in PAGE_SLUG:
        src = _src(page)
        for m in re.finditer(r'(?:src|href)="(https?:)?//[^"]*"', src):
            assert False, f"{page} 引用了外部资源：{m.group(0)}"


def test_localstorage_namespace_is_lg_only():
    """不碰盲评台的 rv_* 键（2026-09-18 教训：跨标签页持久化会把人钉在旧偏好上）。"""
    for page in PAGE_SLUG:
        src = _src(page)
        assert "rv_theme" not in src and "rv_font" not in src and "rv_diff" not in src, \
            f"{page} 动了盲评台的 rv_* 键"
    lg = (WEBSRC / "_shared" / "lg.js").read_text(encoding="utf-8")
    for m in re.finditer(r"localStorage\.(?:get|set|remove)Item\(", lg):
        seg = lg[m.end():m.end() + 120]
        assert "lg_" in seg or "THEME_KEY" in seg, f"lg.js 出现非 lg_* 命名空间的本地存储：{seg[:60]}"


def test_pages_are_get_only():
    """研究台只读：不得出现**真正发出去的**写请求。

    注意与 test_console_page.py::test_console_page_is_get_only 的区别：
    那边是全文 grep 写动词，本文件不能照抄 —— 研究台的页面会在正文里**说明**
    "写操作走 POST /experiments，本页只读"，那是交接信息，不是请求。
    这里只认真正的请求写法：fetch 的 method 选项、X-HTTP-Method 覆写。
    取数一律经 LG.fetchModule（GET + Accept: application/json）。
    """
    for page in PAGE_SLUG:
        src = _src(page)
        assert not re.search(r"method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]", src, re.I), \
            f"{page} 出现写方法（fetch method 选项）"
        assert "X-HTTP-Method" not in src and "XMLHttpRequest" not in src, \
            f"{page} 出现非 fetch 的请求通道"


# ── E 语法契约：不引入构建工具时的"编译检查" ─────────────────
# HANDOVER §38 的原始痛点就是"前端没有构建产物也没有类型检查"。
# 这条用 node --check 补上最低限度的语法校验：
# 2026-09-19 接通当天 frames.html 写了 `lk.over_0.6_by_layer`（属性名以数字开头，
# 非法语法），整块 renderer 静默不执行 —— 页面导航都建不出来。
# 静态正则和契约断言都查不出这类错，只有真正的解析器能。

def _node_bin():
    import shutil
    return shutil.which("node") or shutil.which("node.exe")


def _syntax_ok(js: str, node: str, label: str):
    import os
    import subprocess
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"{label} 语法错误：\n{r.stderr[:600]}"
    finally:
        os.unlink(path)


def test_page_inline_scripts_are_syntactically_valid():
    import pytest
    node = _node_bin()
    if not node:
        pytest.skip("环境无 node，跳过前端语法检查")
    for page in PAGE_SLUG:
        blocks = re.findall(r"<script>(.*?)</script>", _src(page), re.S)
        assert blocks, f"{page} 没有内联脚本"
        for i, js in enumerate(blocks):
            if js.strip():
                _syntax_ok(js, node, f"{page} 第 {i+1} 个内联脚本")


def test_shared_js_is_syntactically_valid():
    import pytest
    node = _node_bin()
    if not node:
        pytest.skip("环境无 node，跳过前端语法检查")
    _syntax_ok((WEBSRC / "_shared" / "lg.js").read_text(encoding="utf-8"), node, "_shared/lg.js")
    _syntax_ok((WEBSRC / "_shared" / "editorial.js").read_text(encoding="utf-8"), node, "_shared/editorial.js")
    _syntax_ok((WEBSRC / "_shared" / "selfcheck.html").read_text(encoding="utf-8")
               .split("<script>", 2)[-1].rsplit("</script>", 1)[0], node, "_shared/selfcheck.html")


# ── 冒烟：16 模块数据面可达且非空 ─────────────────────────────

def test_console_api_returns_non_empty_data_for_all_modules():
    for slug in console.MODULE_ORDER:
        r = client.get(f"/console/{slug}")
        assert r.status_code == 200, slug
        body = r.json()
        assert body["module"] == slug
        assert isinstance(body["data"], dict) and body["data"], \
            f"{slug} 的 data 为空 —— 页面会渲染成空态"
