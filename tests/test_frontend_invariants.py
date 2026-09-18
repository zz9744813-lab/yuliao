"""前端不变量（2026-09-17）—— **改 `index.html` 之前必读**。

## 为什么需要这个文件

2026-09-16 的 UI 重写把前一天的**跨实验取题逻辑（`batchMulti`）整段删掉了**，
没有任何测试报警。后果是实的：跨语料批 `x50` 有三个实验的题，页面走单实验路径，
**斗罗那 3 题判完就显示「本批已全部判定」，而实际还有 39 题没判**。

前端没有构建产物、没有类型检查、也没有别的机制能发现"某个功能被顺手删了"。
所以这里用**静态断言**把几条硬契约钉住：删掉 → pytest 红 → 改的人立刻知道。

## 钉住的契约（每条都对应一次真实事故或有意的设计）

1. **跨实验取题**：`batchMulti` + 命中 `/review/next?batch=`。删了就重演 x50 那次误报。
2. **上下文口径**：`CTX_SCOPE` + `ctx=` 参数。单题阅读量 97% 在上文（4100 字 → 200 字），
   删了等于把"太折磨了"原样还回去。
3. **全场景可展开**：`context_full` / `toggleCtxScope`。近段是默认，但用户要能自己看全场景。
4. **批注代码块**：`// ── 噪点批注` 与 `// 选中 / 点删的委托` 两个标记必须都在 ——
   `test_marking_js.py` 靠这两个标记抽代码块跑断言；标记没了那个文件会收集失败。
5. **`MARK_KINDS` 字符串**：历史批注数据与 `noise_report.py` 按字符串聚合，**只能追加不能改**。
6. **DOM id**：`rv-*` / `mk-*` / `qtab-*` 等被 JS 与测试引用，改名必须同步。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def src() -> str:
    return HTML.read_text(encoding="utf-8")


# ── ① 跨实验取题（x50 事故）────────────────────────────────────
def test_cross_experiment_batch_routing_present(src):
    assert "batchMulti" in src, "跨实验批路由被删了 —— 会在第一个实验判完后误报「本批已全部判定」"
    assert "info.experiments" in src, "batchMulti 的置位依据（/review/batch 返回的 experiments）没了"


def test_load_next_hits_both_endpoints(src):
    """单实验 / 跨实验两条路径都要在，且都带上 ctx 口径。"""
    assert "/review/next?batch=" in src, "跨实验取题端点调用没了"
    assert "/experiments/${exp}/review/next?batch=" in src, "单实验路径没了"
    assert src.count("ctx=${CTX_SCOPE}") >= 2, "两条取题路径都必须带 ctx 口径"


# ── ② 上下文口径（"太折磨了"那次）──────────────────────────────
def test_context_scope_plumbing(src):
    assert "CTX_SCOPE" in src, "上文口径开关没了 —— 单题阅读量会回到 4100 字"
    assert "context_full" in src, "全场景文本没用上：近段是默认，但必须能展开"
    assert "toggleCtxScope" in src, "切换全场景的入口没了"


# ── ③ 批注代码块与词表（test_marking_js / noise_report 依赖）──
def test_marking_block_markers_present(src):
    assert "// ── 噪点批注" in src, "批注块起始标记没了（test_marking_js.py 靠它抽代码）"
    assert "// 选中 / 点删的委托" in src, "批注块结束标记没了"


def test_mark_kinds_append_only(src):
    """历史批注按字符串聚合，删/改会让老数据变成孤儿。"""
    for k in ("'用词'", "'解释过度'", "'节奏'", "'逻辑'", "'意象'", "'其他'"):
        assert k in src, f"MARK_KINDS 里的 {k} 不能删（历史数据与 noise_report 依赖）"


# ── ④ DOM id（JS 与测试都引用）────────────────────────────────
def test_key_dom_ids_present(src):
    for i in ("rv-context", "rv-ctx-btn", "rv-ctx-body", "rv-ab", "rv-a", "rv-b",
              "rv-dock", "rv-q-wrap", "rv-q-btn", "qtab-pending", "qtab-done",
              "mk-a", "mk-b", "mark-pop"):
        assert f'id="{i}"' in src, f"DOM id {i} 没了（JS/测试引用）"


# ── ⑤ 反锚定设计（判前不得暴露信号）────────────────────────────
def test_no_signal_leak_before_verdict(src):
    """判前不得渲染 reasons/score/stratum —— 2026-09-16 审查就抓到过这个。"""
    assert "lockChips" in src, "判前锁信号的逻辑没了"
