"""前端注入面残留收口回归（2026-09-27，审计 P2 残留第二轮）。

钉住本轮收口的四类事实（任务 lg-frontend-injection-residual 必做 3）：

① 静态形态：被收口的出口不再出现「服务端字符串插值进 class/style 属性」；
   value / data-* 属性位置的拼接一律走 escAttr（escHtml 不许再出现在这些位置）；
② escHtml 两份实现（index.html / diff.js）都含 `"` `'` `` ` `` 转义，
   escAttr 组合 escHtml 并额外转义反斜杠，且只出现在属性位置；
③ loadExps 仍走 DOM 构造 + textContent（不许改回 innerHTML）；
④ 注入样本串（含 `"` `'` `` ` `` `<img onerror=…>`）经 showExp(#d-stages) /
   loadCorpus(#work-table、#f-work、#seg-list) / loadDoneList / buildMarkedHtml /
   DiffLite.composeHtml 渲染后不产生可执行标记（标签内无事件属性、
   属性值不挣脱引号、类名只余 CSS 标识符安全 token）。

口径约束（为什么不全部 DOM 化）：test_frontend_escape 用「函数源码切片 + 无
document 的 node stub」执行 showExp/loadCorpus/loadDoneList，且 composeHtml 有
「返回字符串、剥标签逐字等于原文」的偏移契约（test_diff_js 钉死）——这些出口必须
保持字符串拼接，收口方式按任务条款取「escAttr / escClass 白名单」；纯浏览器路径
（loadExps）才走 createElement/textContent。

node 不可用时动态用例 skip（口径同 test_frontend_escape），静态用例照跑。
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

PAY = '"><img src=x onerror=alert(1)>'          # 双引号挣脱 + 可执行标签
PAY_FULL = '"\'`\\<b onmouseover=alert(2)>'      # 双引号/单引号/反引号/反斜杠全谱
MARK_KINDS = ['用词', '解释过度', '情绪直给', '节奏', '逻辑', '意象', '其他']


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _diff_js() -> str:
    return DIFF_JS.read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _escapes_src() -> str:
    """index.html 里 escHtml + escAttr 的定义段（到 buildMarkedHtml 前）。"""
    return _slice(_html(), "function escHtml(", "\nfunction buildMarkedHtml")


def _marking_block() -> str:
    src = _html()
    return _slice(src, "// ── 噪点批注", "// 选中 / 点删的委托")


def _showexp_src() -> str:
    return _slice(_html(), "async function showExp(", "function mdLite(")


def _loadexps_src() -> str:
    return _slice(_html(), "async function loadExps()", "async function createExp()")


def _loadcorpus_src() -> str:
    return _slice(_html(), "async function loadCorpus(", "async function importInbox(")


def _initreview_src() -> str:
    return _slice(_html(), "async function initReview()", "function renderProgress()")


def _donelist_src() -> str:
    return _slice(_html(), "function verdictChip(", "// ── 实验")


def _node() -> str:
    for cand in ("node", r"F:\Geek\node.exe",
                 r"C:\Users\6\.workbuddy-ai\binaries\node\versions\22.22.2-2\node.exe"):
        p = shutil.which(cand) if not cand.startswith(("F:", "C:")) else (
            cand if Path(cand).exists() else None)
        if p:
            return p
    pytest.skip("找不到可用的 node，跳过动态渲染测试")


def _run_js(js: str) -> dict:
    proc = subprocess.run([_node(), "-e", js], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 失败:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# JS 侧共用检查器：真实浏览器解析标签时，属性值/事件只在 < ... > 内部才「可执行」。
# 注入串被转义后作为文本节点存在（在标签外）不算可执行标记。
TAG_SCANNER = r"""
function scanInert(html){
  const tags = html.match(/<[^>]*>/g) || [];
  const r = {};
  r.nTags = tags.length;
  // 1) 生成的标签内不允许出现事件处理器属性（onclick=/onerror=/onmouseover=…）。
  //    双引号属性值是 HTML 模型里的惰性文本位，先整体剥掉再查——
  //    查的是「挣脱出引号」的事件属性，不是「被引号关押」的同名字符串。
  r.eventInTag = tags.filter(t => /\son[a-z]+\s*=/i.test(t.replace(/"[^"]*"/g, '""')));
  // 2) class="…" 只许 CSS 标识符与空格（escClass 白名单口径），不许实体/引号/等号
  r.classDirty = [...html.matchAll(/class="([^"]*)"/g)]
    .map(m => m[1]).filter(c => !/^[A-Za-z0-9_ -]*$/.test(c));
  // 3) 每个标签的双引号必须成对（属性值没有挣脱出来的裸引号）
  r.unbalanced = tags.filter(t => (t.match(/"/g) || []).length % 2 !== 0);
  return r;
}
"""


# ── ① 静态形态：属性/类名/样式位置 ────────────────────────────

def test_no_escape_call_interpolation_in_class_attrs():
    """全文：class="…" 里不许插 esc / escHtml / escAttr（原始串或未收口转义都不行）。
    类名位置唯一允许的插值是 escClass 白名单（由专测钉在 showExp 内）。"""
    bad = re.findall(r'class="[^"]*\$\{(?:esc|escHtml|escAttr)\(', _html())
    assert bad == [], f"类名属性仍插值未收口的转义调用：{bad}"


def test_no_escape_call_interpolation_in_style_attrs():
    bad = re.findall(r'style="[^"]*\$\{(?:esc|escHtml|escAttr)\(', _html())
    assert bad == [], f"style 属性仍插值转义调用（应走 DOM style.*）：{bad}"


def test_value_attrs_use_only_escattr():
    src = _html()
    assert re.findall(r'value="\$\{escHtml\(', src) == [], \
        "option value 属性仍用 escHtml 拼接——属性位置一律 escAttr"
    assert src.count('value="${escAttr(') == 3, \
        "收口的 value 属性点应有 3 处（#rv-exp 两处 + #f-work 一处）"


def test_closed_data_attrs_use_escattr():
    src = _html()
    assert 'data-rejudge-id="${escAttr(it.id)}"' in src
    assert 'data-del="${escAttr(m.id)}"' in src
    # 这两个属性位不得再残留 escHtml 口径
    assert 'data-rejudge-id="${escHtml' not in src and 'data-del="${escHtml' not in src


def test_d_stages_class_position_uses_token_whitelist():
    sl = _showexp_src()
    assert 'class="tag ${escHtml(v.status)}"' not in sl, \
        "#d-stages 又退回「服务端 status 经 escHtml 进 class」的未收口形态"
    assert 'class="tag ${escClass(v.status)}"' in sl
    assert re.search(r"const escClass = s => String\(s == null \? '' : s\)"
                     r"\.replace\(/\[\^A-Za-z0-9_-\]/g, ''\);", sl), \
        "escClass 必须是「非 [A-Za-z0-9_-] 全丢」的白名单，不是转义"
    # showExp 切片内 class="…" 的插值只许 escClass
    others = [m for m in re.findall(r'class="[^"]*\$\{(\w+)\(', sl) if m != "escClass"]
    assert others == []
    assert re.findall(r'style="[^"]*\$\{', sl) == [], "showExp 里 style 属性不许有插值"


def test_verdict_chip_class_position_never_receives_server_value():
    sl = _donelist_src()
    assert "const [t,c] = m[w] || [w||'—','chip'];" not in sl, \
        "未知 winner 又被当类名插进 class=（verdictChip 回退必须是固定 'chip'）"
    assert "const c = hit ? hit[1] : 'chip';" in sl
    assert 'class="${c}"' in sl and re.findall(r'class="[^"]*\$\{(?:esc|w)\b', sl) == []


def test_loadcorpus_and_initreview_slices_no_class_style_interpolation():
    for sl, name in ((_loadcorpus_src(), "loadCorpus"), (_initreview_src(), "initReview"),
                     (_donelist_src(), "loadDoneList")):
        assert re.findall(r'style="[^"]*\$\{', sl) == [], f"{name} 的 style 属性出现插值"
    # loadCorpus/initReview 的 class="…" 全是静态词（verdictChip 的 ${c} 由专测钉白名单）
    assert re.findall(r'class="[^"]*\$\{', _loadcorpus_src()) == []
    assert re.findall(r'class="[^"]*\$\{', _initreview_src()) == []


# ── ② escHtml / escAttr 转义口径 ──────────────────────────────

_ESC_SPECS = [(r'\.replace\(/"/g,\s*\'&quot;\'\)', "双引号"),
              (r"\.replace\(/'/g,\s*'&#39;'\)", "单引号"),
              (r"\.replace\(/`/g,\s*'&#96;'\)", "反引号")]


@pytest.mark.parametrize("getter,name", [(lambda: _html(), "index.html"),
                                         (lambda: _diff_js(), "diff.js")])
def test_eschtml_implementations_escape_quotes_and_backticks(getter, name):
    src = getter()
    i = src.index("function escHtml")
    body = src[i:i + 400]
    for spec, label in _ESC_SPECS:
        assert re.search(spec, body), f"{name} 的 escHtml 丢了 {label} 转义：{spec}"


def test_escattr_defined_and_used_only_in_attribute_positions():
    src = _html()
    i = src.index("function escAttr")
    body = src[i:i + 120]
    assert "return escHtml(s)" in body, "escAttr 必须组合 escHtml（继承文本+引号口径）"
    assert ".replace(/\\\\/g,'&#92;')" in src[i:i + 200], "escAttr 必须额外转义反斜杠"
    for m in re.finditer(r"\$\{escAttr\(", src):
        assert src[:m.start()].endswith('="'), (
            f"escAttr 出现在非属性位置（行~{src[:m.start()].count(chr(10)) + 1}）")
    assert "escAttr" not in _diff_js(), "diff.js 的属性拼接形态被 test_frontend_escape 钉死，本轮不动"


# ── ③ loadExps 仍走 DOM 构造 ─────────────────────────────────

def test_loadexps_still_textcontent_dom_construction():
    sl = _loadexps_src()
    assert "innerHTML" not in sl, "loadExps 被改回 innerHTML 拼接——granularities 是用户可控串"
    assert "createElement" in sl and "textContent" in sl
    assert "textContent = (e.granularities || []).join(',')" in sl
    assert "tag.className = 'tag ' + (e.status || '');" in sl, \
        "status 进类名必须走 DOM className 赋值（无 HTML 解析），不许回拼字符串模板"


# ── ④ 动态行为：注入样本经渲染函数不产生可执行标记 ────────────

def test_escattr_neutralizes_full_injection_alphabet():
    r = _run_js(f"""
      {_escapes_src()}
      const out = {{ esc: escAttr({json.dumps(PAY_FULL)}),
                     html: escHtml({json.dumps(PAY_FULL)}) }};
      console.log(JSON.stringify(out));
    """)
    assert not re.search(r"""["'`\\<>]""", r["esc"]), f"escAttr 输出残留结构字符：{r['esc']}"
    assert "&quot;" in r["esc"] and "&#39;" in r["esc"] and "&#96;" in r["esc"] \
        and "&#92;" in r["esc"] and "&lt;" in r["esc"] and "&gt;" in r["esc"]
    # escHtml 与 escAttr 的差值只有反斜杠口径（文本位不需要它）
    assert r["esc"] == r["html"].replace("\\", "&#92;")


_SHOWEXP_HARNESS = r"""
      const nodes = Object.create(null);
      const $ = key => nodes[key] || (nodes[key] = {style:{}, textContent:'', innerHTML:''});
      const toast = m => { out.toast = String(m); };
      const clearInterval = () => {};
      let autoTimer = null;
      const api = async () => ({status:'running', stats:{stages:{}},
        job_states:{'stage:extract':{status:STATUS, last_error:ERR},
                    '`key`\\q':{status:'ok'}}});
"""


def test_d_stages_render_inert():
    r = _run_js(f"""
      {_escapes_src()}
      const out = {{}};
      {_SHOWEXP_HARNESS.replace('STATUS', json.dumps(PAY)).replace('ERR', json.dumps(PAY_FULL))}
      {TAG_SCANNER}
      {_showexp_src()}
      showExp('EXP-TEST').then(() => {{
        const html = nodes['#d-stages'].innerHTML;
        out.html = html;
        out.scan = scanInert(html);
        out.noImg = !html.includes('<img');
        out.readable = html.includes('&lt;img') && html.includes('&#96;') && html.includes('&#92;') === false;
        console.log(JSON.stringify(out));
      }});
    """)
    assert r["html"], f"showExp 没产出 #d-stages（可能抛错）：{r.get('toast')}"
    s = r["scan"]
    assert s["nTags"] >= 3 and s["eventInTag"] == [], f"生成的标签带事件属性：{s['eventInTag']}"
    assert s["classDirty"] == [], f"class 属性混入非白名单 token：{s['classDirty']}"
    assert s["unbalanced"] == []
    assert r["noImg"], f"payload 生成了真实标签：{r['html']}"
    assert r["readable"], f"转义后的错误文本应仍可读：{r['html']}"
    # 类名只剩安全 token：payload 的字母被并进一个合法 class（展示没被吞）
    assert "tag imgsrcxonerroralert1" in r["html"]


def test_corpus_lists_and_options_render_inert():
    payload = json.dumps(PAY_FULL)
    r = _run_js(f"""
      {_escapes_src()}
      const out = {{}};
      const nodes = Object.create(null);
      const $ = key => nodes[key] || (nodes[key] = {{innerHTML:'', textContent:''}});
      const api = async path => path === '/works'
        ? [{{id:{payload}, title:{payload}, author:'a', source:'s', segments:2, note:{payload}}}]
        : path.startsWith('/segments')
          ? [{{id:{payload}, text:{payload}}}]
          : {{n_segments:1}};
      {TAG_SCANNER}
      {_loadcorpus_src()}
      loadCorpus().then(() => {{
        const works = nodes['#work-table tbody'].innerHTML;
        const options = nodes['#f-work'].innerHTML;
        const segs = nodes['#seg-list'].innerHTML;
        out.scan = [works, options, segs].map(h => scanInert(h));
        out.noRawImg = [works, options, segs].every(h => !h.includes('<img') && !h.includes('<b '));
        out.escapedReadable = [works, options, segs].every(h => h.includes('&lt;'));
        out.values = [...options.matchAll(/value="([^"]*)"/g)].map(m => m[1]);
        console.log(JSON.stringify(out));
      }});
    """)
    for scan in r["scan"]:
        assert scan["eventInTag"] == [] and scan["classDirty"] == [] and scan["unbalanced"] == []
    assert r["noRawImg"], "payload 在语料列表生成了真实标签"
    assert r["escapedReadable"], "payload 文本应经转义后仍可读"
    assert r["values"], "option value 属性缺失"
    for v in r["values"]:
        assert not re.search(r"""["'`\\<>]""", v), f"option value 挣脱出结构字符：{v}"


def test_done_list_rejudge_button_attr_inert():
    payload = json.dumps(PAY)
    src = _html()
    esc_def = src[src.index("function esc(s){"):src.index("\n", src.index("function esc(s){")) + 1]
    r = _run_js(f"""
      {_escapes_src()}
      {esc_def}
      const out = {{}};
      const nodes = Object.create(null);
      const $ = key => nodes[key] || (nodes[key] = {{innerHTML:'', textContent:'',
        querySelectorAll: () => []}});
      const BATCH = 'sample';
      const api = async () => ({{done:1, items:[{{id:{payload},
        winner:'<img src=x onerror=alert(3)>', reviewed_at:'2026-09-26T10:00', n_annotations:0}}]}});
      const rejudgeFromDone = () => {{}};
      {TAG_SCANNER}
      {_donelist_src()}
      loadDoneList().then(() => {{
        const html = nodes['#rv-table'].innerHTML;
        out.html = html;
        out.scan = scanInert(html);
        out.attr = (html.match(/data-rejudge-id="([^"]*)"/) || [])[1];
        out.chipAttrs = [...html.matchAll(/class="([^"]*)"/g)].map(m => m[1]);
        console.log(JSON.stringify(out));
      }});
    """)
    s = r["scan"]
    assert s["eventInTag"] == [], f"未知 winner 挣脱进标签/类名：{s['eventInTag']}"
    assert s["classDirty"] == [], f"未知 winner 污染 class：{s['classDirty']}"
    assert s["unbalanced"] == []
    assert r["attr"] is not None and not re.search(r"""["'<>]""", r["attr"]), \
        f"data-rejudge-id 挣脱属性：{r['attr']}"
    assert "<img" not in r["html"] and "&quot;&gt;&lt;img" in r["html"]


def test_mark_and_compose_payload_no_executable_marks():
    payload = json.dumps(PAY_FULL)
    r = _run_js(f"""
      {_marking_block()}
      const DiffLite = require({json.dumps(str(DIFF_JS).replace(chr(92), '/'))});
      const out = {{}};
      const mk = [{{id:1, start:0, end:2, kind:{payload}}}];
      const h1 = buildMarkedHtml('正文文字', mk);
      const h2 = DiffLite.composeHtml('正文文字', mk, null, {json.dumps(MARK_KINDS)});
      out.same = h1 === h2;
      out.markOpen = h1.match(/<mark\\b[^>]*>/g) || [];
      out.plain = h1.replace(/<[^>]+>/g, '');
      out.escaped = h1.includes('&quot;') && h1.includes('&#39;') && h1.includes('&#96;');
      out.noEscape = !/<b\\b|<img\\b/i.test(h1);
      console.log(JSON.stringify(out));
    """)
    assert r["noEscape"], "payload 生成了真实标签"
    assert r["escaped"], "引号/反引号未按属性口径转义"
    assert len(r["markOpen"]) == 1 and r["markOpen"][0].count('"') % 2 == 0, \
        f"kind 挣脱 title：{r['markOpen']}"
    assert r["plain"] == "正文文字", "转义收口不得吞正文（textContent 完整性）"
    assert r["same"], "composeHtml 与 buildMarkedHtml 的收口口径漂移"
