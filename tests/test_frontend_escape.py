"""前端注入路径收口回归（2026-09-23，审计 P2）。

钉住两条持久化 XSS 路径的修复，防止回退：

1. **实验 granularities → innerHTML**：`loadExps()` 曾把 `(e.granularities||[]).join(',')`
   直接拼进 `<td>` 的 innerHTML。granularities 是用户建实验时的任意字符串，
   持久化在服务端，其他使用者打开实验列表就会触发。修复：整行改 DOM 构造
   （createElement + textContent）。
2. **批注 kind → title 属性**：`escHtml` 原来只转义 `< > &` 不转义引号，
   `title="${escHtml(mk.kind)}"` 里 kind 含 `"` 就能提前闭合属性注入事件处理器。
   修复：`escHtml` 补齐 `"` `'` `` ` `` 转义（index.html 与 diff.js 两份实现都要），
   `data-mid` 同样按不可信口径走转义。

约束：buildMarkedHtml / composeHtml 是纯字符串函数（node 测试无 DOM），
测试钉住二者逐字相等（test_diff_js），所以属性注入收口走「转义 + 覆盖断言」，
loadExps 只在浏览器跑、无 node 不变量钉它，直接改 DOM 构造。

node 不可用时 skip（口径同 test_marking_js），不 xfail。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "app" / "static" / "index.html"
DIFF_JS = ROOT / "app" / "static" / "diff.js"

INJ_GRAN = '"><img src=x onerror=alert(1)>'
INJ_KIND = '" onmouseover="x'


def _node() -> str:
    for cand in ("node", r"F:\Geek\node.exe",
                 r"C:\Users\6\.workbuddy-ai\binaries\node\versions\22.22.2-2\node.exe"):
        p = shutil.which(cand) if not cand.startswith(("F:", "C:")) else (
            cand if Path(cand).exists() else None)
        if p:
            return p
    pytest.skip("找不到可用的 node，跳过前端转义测试")


def _run_js(js: str) -> dict:
    proc = subprocess.run([_node(), "-e", js], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 失败:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _marking_block() -> str:
    src = HTML.read_text(encoding="utf-8")
    return src[src.index("// ── 噪点批注"):src.index("// 选中 / 点删的委托")]


def _loadexps_src() -> str:
    src = HTML.read_text(encoding="utf-8")
    start = src.index("async function loadExps()")
    end = src.index("async function createExp()")
    return src[start:end]


def _showexp_src() -> str:
    src = HTML.read_text(encoding="utf-8")
    start = src.index("async function showExp(")
    return src[start:src.index("function mdLite(", start)]


def _html_esc_src() -> str:
    src = HTML.read_text(encoding="utf-8")
    start = src.index("function escHtml(")
    return src[start:src.index("\nfunction buildMarkedHtml", start)]


def _diff_esc_html_src() -> str:
    """从 diff.js 抽出 escHtml 函数源码（到函数收口的缩进 `  }` 为止）。"""
    src = DIFF_JS.read_text(encoding="utf-8")
    start = src.index("function escHtml")
    end = src.index("\n  }", start) + len("\n  }")
    return src[start:end]


# ── escHtml：引号转义（两份实现都要）─────────────────────────

def test_esc_html_escapes_quotes_in_index_html():
    r = _run_js(f"""
      {_marking_block()}
      const out = {{
        dq: escHtml('"'),
        sq: escHtml("'"),
        bt: escHtml('`'),
        lt: escHtml('<'),
        gt: escHtml('>'),
        amp: escHtml('&'),
      }};
      console.log(JSON.stringify(out));
    """)
    assert r["dq"] == "&quot;", f'double quote 未转义：{r["dq"]!r}'
    assert r["sq"] == "&#39;", f"single quote 未转义：{r['sq']!r}"
    assert r["bt"] == "&#96;"
    assert r["lt"] == "&lt;" and r["gt"] == "&gt;" and r["amp"] == "&amp;"


def test_esc_html_escapes_quotes_in_diff_js():
    src = _diff_esc_html_src()
    r = _run_js(f"""
      {src}
      const out = {{
        dq: escHtml('"'),
        sq: escHtml("'"),
        bt: escHtml('`'),
        lt: escHtml('<'),
        gt: escHtml('>'),
        amp: escHtml('&'),
      }};
      console.log(JSON.stringify(out));
    """)
    assert r["dq"] == "&quot;", f'diff.js 的 escHtml 未转义 "：{r["dq"]!r}'
    assert r["sq"] == "&#39;", f"diff.js 的 escHtml 未转义 '：{r['sq']!r}"
    assert r["bt"] == "&#96;"
    assert r["lt"] == "&lt;" and r["gt"] == "&gt;" and r["amp"] == "&amp;"


def test_esc_html_implementations_behave_identically():
    """index.html 与 diff.js 两份实现必须行为一致（composeHtml 与 buildMarkedHtml
    无高亮时逐字相等的不变量依赖这一点）。"""
    cases = ['"', "'", '<script>', 'a&b', '`x`', '正常中文']
    # 反斜杠不能进 f-string 表达式（py3.11），先在外面改名
    renamed = re.sub(r"\bfunction escHtml\b", "function escHtmlA",
                     _diff_esc_html_src(), count=1)
    r2 = _run_js(f"""
      const a = {{}};
      {renamed}
      a.diff = {json.dumps(cases)}.map(c => escHtmlA(c));
      console.log(JSON.stringify(a));
    """)
    r1 = _run_js(f"""
      {_marking_block()}
      const a = {{}};
      a.html = {json.dumps(cases)}.map(c => escHtml(c));
      console.log(JSON.stringify(a));
    """)
    assert r1["html"] == r2["diff"], (
        f"两份 escHtml 行为不一致：index.html={r1['html']} diff.js={r2['diff']}")


# ── 批注 kind / data-mid：属性注入收口 ────────────────────────

def test_kind_injection_cannot_break_out_of_title_index_html():
    r = _run_js(f"""
      {_marking_block()}
      const h = buildMarkedHtml('正文文字', [{{id:1, start:0, end:2, kind:{json.dumps(INJ_KIND)}}}]);
      const out = {{ html: h }};
      out.noUnescapedOnmouseover = !h.includes('" onmouseover=');
      out.escapedPayload = h.includes('&quot; onmouseover=&quot;x');
      out.markOpen = (h.match(/<mark\\b[^>]*>/g) || []);
      out.plain = h.replace(/<[^>]+>/g, '');
      console.log(JSON.stringify(out));
    """)
    assert r["noUnescapedOnmouseover"], f"kind 挣脱了 title 属性：{r['html']}"
    assert r["escapedPayload"], f"payload 未按属性口径转义：{r['html']}"
    # mark 开标签只有一个引号配对完整：title 值内的引号都已变成实体
    assert len(r["markOpen"]) == 1
    assert r["markOpen"][0].count('"') % 2 == 0, f"title 属性提前闭合：{r['markOpen'][0]}"
    assert r["plain"] == "正文文字"


def test_kind_injection_cannot_break_out_of_title_diff_js():
    r = _run_js(f"""
      const DiffLite = require({json.dumps(str(DIFF_JS).replace(chr(92), '/'))});
      const h = DiffLite.composeHtml('正文文字',
        [{{id:1, start:0, end:2, kind:{json.dumps(INJ_KIND)}}}], null,
        ['用词','解释过度','情绪直给','节奏','逻辑','意象','其他']);
      const out = {{ html: h }};
      out.noUnescapedOnmouseover = !h.includes('" onmouseover=');
      out.escapedPayload = h.includes('&quot; onmouseover=&quot;x');
      out.markOpen = (h.match(/<mark\\b[^>]*>/g) || []);
      console.log(JSON.stringify(out));
    """)
    assert r["noUnescapedOnmouseover"], f"composeHtml 里 kind 挣脱了 title 属性：{r['html']}"
    assert r["escapedPayload"], f"composeHtml payload 未按属性口径转义：{r['html']}"
    assert len(r["markOpen"]) == 1
    assert r["markOpen"][0].count('"') % 2 == 0, f"title 属性提前闭合：{r['markOpen'][0]}"


def test_granularities_payload_rendered_inert_in_title():
    """完整 payload 也不该出现可执行的 onerror 属性。"""
    r = _run_js(f"""
      {_marking_block()}
      const h = buildMarkedHtml('正文', [{{id:1, start:0, end:2, kind:{json.dumps(INJ_GRAN)}}}]);
      const out = {{ html: h }};
      out.noImgTag = !/<img\\b/i.test(h);
      out.noOnerror = !h.includes('onerror=') || h.includes('&quot;onerror=') || !h.includes('" onerror=');
      console.log(JSON.stringify(out));
    """)
    assert r["noImgTag"], f"payload 生成了真实的 <img> 标签：{r['html']}"
    assert r["noOnerror"], f"onerror 属性未被转义隔离：{r['html']}"


def test_data_mid_treated_as_untrusted():
    """id 是服务端生成的，但按不可信处理：字符串 id 也必须走转义。"""
    r = _run_js(f"""
      {_marking_block()}
      const h = buildMarkedHtml('正文', [{{id:'1"><img src=x onerror=alert(1)>', start:0, end:2, kind:'用词'}}]);
      const out = {{ html: h }};
      out.noImgTag = !/<img\\b/i.test(h);
      out.plain = h.replace(/<[^>]+>/g, '');
      console.log(JSON.stringify(out));
    """)
    assert r["noImgTag"], f"data-mid 未转义，字符串 id 注入了标签：{r['html']}"
    assert r["plain"] == "正文"


# ── loadExps：DOM 构造（静态断言，函数只在浏览器跑）────────────

def test_loadexps_uses_dom_construction():
    src = _loadexps_src()
    assert "innerHTML" not in src, "loadExps 仍在拼 innerHTML —— granularities 是用户可控字符串"
    assert "createElement" in src, "loadExps 未改用 DOM 构造"
    assert "textContent" in src, "服务端字段必须走 textContent 而不是 HTML 拼接"
    assert "showExp(e.id)" in src, "详情按钮仍要能打开实验详情"
    # granularities 的 join 结果必须喂给 textContent
    assert "textContent = (e.granularities || []).join(',')" in src, (
        "granularities 应通过 textContent 渲染")


def test_loadexps_no_inline_event_handler():
    src = _loadexps_src()
    assert "onclick=" not in src, "loadExps 不该再拼内联 onclick（id 会进 JS 字符串上下文）"


def test_experiment_detail_escapes_job_errors_and_stage_stats():
    """网关错误和阶段统计是服务端数据，不能变成浏览器可执行的标签。"""
    payload = '<img src=x onerror=alert(1)>'
    js = """
      const nodes = Object.create(null);
      const $ = key => nodes[key] || (nodes[key] = {style:{}, textContent:'', innerHTML:''});
      const api = async () => ({status:'running',
        stats:{stages:{extract:{detail:PAYLOAD}}},
        job_states:{'stage:extract':{status:'failed', last_error:PAYLOAD}}});
      const toast = () => {};
      const clearInterval = () => {};
      let autoTimer = null;
      function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
      SHOWEXP_SOURCE
      showExp('EXP-TEST').then(() => console.log(JSON.stringify({
        html: nodes['#d-stages'].innerHTML,
        status: nodes['#d-id'].textContent,
      })));
    """.replace("PAYLOAD", json.dumps(payload)).replace("SHOWEXP_SOURCE", _showexp_src())
    r = _run_js(js)
    assert "<img" not in r["html"], r["html"]
    assert "&lt;img" in r["html"], "错误文本应继续可读，但必须转义"
    assert r["status"] == "EXP-TEST · running"


def test_mark_open_tag_escapes_both_attrs():
    """mark 开标签的 data-mid 与 title 都必须过 escHtml。"""
    block = _marking_block()
    build = block[block.index("function buildMarkedHtml"):block.index("function renderMarks")]
    assert 'data-mid="${escHtml(m.id)}"' in build, "buildMarkedHtml 的 data-mid 未走 escHtml"
    assert 'title="${escHtml(m.kind)}' in build, "buildMarkedHtml 的 title 未走 escHtml"
    dsrc = DIFF_JS.read_text(encoding="utf-8")
    compose = dsrc[dsrc.index("function composeHtml"):dsrc.index("global.DiffLite")]
    assert "+ escHtml(mk.m.id) +" in compose, "composeHtml 的 data-mid 未走 escHtml"
    assert "escHtml(mk.m.kind)" in compose, "composeHtml 的 title 未走 escHtml"


def test_done_list_id_cannot_inject_an_inline_handler():
    src = HTML.read_text(encoding="utf-8")
    block = src[src.index("function verdictChip("):src.index("// ── 实验", src.index("function verdictChip("))]
    assert 'onclick="rejudgeFromDone(' not in block
    payload = '\\"><img src=x onerror=alert(1)>'
    result = _run_js(f"""
      {_html_esc_src()}
      const esc = escHtml;
      const BATCH = 'sample';
      const nodes = Object.create(null);
      const $ = key => nodes[key] || (nodes[key] = {{innerHTML:'', textContent:'',
        querySelectorAll: () => []}});
      const api = async () => ({{done:1, items:[{{id:{json.dumps(payload)},
        winner:'human', reviewed_at:'2026-09-26T10:00', n_annotations:0}}]}});
      const rejudgeFromDone = () => {{}};
      {block}
      loadDoneList().then(() => console.log(JSON.stringify({{html:nodes['#rv-table'].innerHTML}})));
    """)
    assert "<img" not in result["html"]
    assert "onclick=" not in result["html"]
    assert "&quot;&gt;&lt;img" in result["html"]


def test_corpus_list_escapes_server_fields():
    src = HTML.read_text(encoding="utf-8")
    block = src[src.index("async function loadCorpus("):src.index("async function importInbox(")]
    payload = '\\"><img src=x onerror=alert(1)>'
    result = _run_js(f"""
      {_html_esc_src()}
      const nodes = Object.create(null);
      const $ = key => nodes[key] || (nodes[key] = {{innerHTML:'', textContent:''}});
      const api = async path => path === '/works' ? [{{id:{json.dumps(payload)},
        title:{json.dumps(payload)}, author:'author', source:'source', segments:1}}]
        : path.startsWith('/segments') ? [{{id:{json.dumps(payload)}, text:{json.dumps(payload)}}}]
        : {{n_segments:1}};
      {block}
      loadCorpus().then(() => console.log(JSON.stringify({{
        works:nodes['#work-table tbody'].innerHTML,
        options:nodes['#f-work'].innerHTML,
        segments:nodes['#seg-list'].innerHTML
      }})));
    """)
    assert all("<img" not in html for html in result.values())
    assert all("&lt;img" in html for html in result.values())
