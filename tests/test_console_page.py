"""T3 第二界面评审台（app/static/console.html）的回归测试。

沿 test_frontend_invariants.py 的思路：前端没有构建/类型检查，"功能被顺手删掉"
没有任何机制能发现，用静态断言钉住本页的硬契约：

1. 路由 /console/ui 可达 + no-store（控制台是仪器读数，禁缓存）；
2. 导航**不硬编码**模块清单 —— 必须从 GET /console 索引动态渲染
   （后端加模块自动出现；把 16 个 slug 写死进 HTML 反而会红）；
3. 只读：页面对后端只发 GET —— 出现任何 POST/DELETE/PUT 字样即红；
4. 一切插值过 esc()（库内字符串是数据不是 HTML）；
5. 取数失败显形（纪律④：静默失败比崩溃更糟）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import console, db
from app.main import app

db.init_db()
client = TestClient(app)

PAGE = ROOT / "app" / "static" / "console.html"


def test_console_ui_route_serves_page_no_store():
    r = client.get("/console/ui")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "第二界面评审台" in r.text
    # 与 index.html 同级同权：都必须是磁盘上的真实文件（不许内联改名漂移）
    assert PAGE.exists()


def test_nav_is_api_driven_not_hardcoded():
    src = PAGE.read_text(encoding="utf-8")
    assert "getJSON('/console')" in src or "getJSON(`/console`)" in src, \
        "模块索引必须来自 GET /console（导航不允许硬编码清单）"
    for slug in console.MODULE_ORDER:            # 16 个 slug 一个都不许写死进页面
        assert f"'{slug}'" not in src, f"模块 {slug} 被硬编码进 HTML——导航应由索引驱动"
    assert "loadModule" in src and "markNav" in src, "导航渲染/高亮逻辑没了"


def test_console_page_is_get_only():
    src = PAGE.read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert verb not in src, f"只读页面出现了 {verb} —— 写操作必须回第一界面"
    assert "fetch(" in src, "取数逻辑没了"


def test_console_page_escapes_interpolation():
    src = PAGE.read_text(encoding="utf-8")
    assert "const esc = s =>" in src, "HTML 转义函数没了（库内字符串不可信）"
    assert "innerHTML" in src, "渲染出口变了？若改成 textContent 请同步本断言"


def test_console_page_surfaces_fetch_errors():
    src = PAGE.read_text(encoding="utf-8")
    assert "加载失败" in src and "err" in src, "取数失败必须显形（纪律④），不许静默白屏"
    assert "localStorage.getItem" not in src and "localStorage.setItem" not in src, \
        "控制台读数不落本地存储（现拉现渲，防读到旧值）"
