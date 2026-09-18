"""差异高亮（diff.js）回归（2026-09-17）。

diff 视图最大的风险是**破坏批注偏移**：划选批注的 offsetOf() 基于 textContent，
所以 composer 只能输出内联标签，剥标签后必须与原文逐字相等。这里用 node 直接跑
真实的 diff.js，钉住以下不变量：

1. textContent 完整性：composeHtml 输出剥标签后与原文一字不差（marks 与 diff
   高亮任意重叠时也要成立）——这正是 offsetOf 对齐的前提；
2. 增删方向：A 标「被改掉的」、B 标「新写的」；
3. 小空隙桥接：相隔 ≤2 字的两处改动并成一处（否则中文逐字 diff 满屏单字红绿）；
4. 越界 / 空输入 / 超长文本不抛异常；
5. 与被测试钉住的批注块同跑：无 diff 高亮时 composeHtml 与 buildMarkedHtml
   **逐字相等**（零回归），有高亮时 mark 覆盖的字与老实现完全一致。

需要 node 可执行（沿用 test_marking_js 的查找方式）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIFF_JS = ROOT / "app" / "static" / "diff.js"
HTML = ROOT / "app" / "static" / "index.html"

# 真实小说风味的样例（同一段的两种写法）
A_TEXT = "他推门进来，屋里没人。桌上摆着两碗面，还冒着热气。"
B_TEXT = "他推门进来，屋子里空荡荡的。桌上摆着两碗面，还冒着热气。"


def _node() -> str:
    for cand in ("node", r"F:\Geek\node.exe",
                 r"C:\Users\6\.workbuddy-ai\binaries\node\versions\22.22.2-2\node.exe"):
        p = shutil.which(cand) if not cand.startswith(("F:", "C:")) else (
            cand if Path(cand).exists() else None)
        if p:
            return p
    pytest.skip("找不到可用的 node，跳过前端逻辑测试")


def _run_js(js: str) -> dict:
    proc = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 失败:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _req() -> str:
    """node require 路径（反斜杠在 JS 字符串里是转义，换成正斜杠）。"""
    return json.dumps(str(DIFF_JS).replace("\\", "/"))


def _harness(assertions: str, with_block: bool = False) -> dict:
    parts = [f"const DiffLite = require({_req()});"]
    if with_block:
        src = HTML.read_text(encoding="utf-8")
        start = src.index("// ── 噪点批注")
        end = src.index("// 选中 / 点删的委托")
        parts.append(src[start:end])
    parts.append("const out = {};")
    parts.append(assertions)
    parts.append("console.log(JSON.stringify(out));")
    return _run_js("\n".join(parts))


# ── 偏移与方向 ──────────────────────────────────────────

def test_identical_texts_produce_no_spans():
    r = _harness(f"""
      const s = DiffLite.highlightSpans({json.dumps(A_TEXT)}, {json.dumps(A_TEXT)});
      out.a = s.A; out.b = s.B;
    """)
    assert r["a"] == [], "两段相同时 A 栏不应有高亮"
    assert r["b"] == [], "两段相同时 B 栏不应有高亮"


def test_direction_del_on_a_and_ins_on_b():
    r = _harness(f"""
      const a = {json.dumps(A_TEXT)}, b = {json.dumps(B_TEXT)};
      const s = DiffLite.highlightSpans(a, b);
      out.aText = s.A.map(x => a.slice(x.start, x.end)).join('|');
      out.bText = s.B.map(x => b.slice(x.start, x.end)).join('|');
      out.aInBounds = s.A.every(x => x.start >= 0 && x.end <= a.length && x.end > x.start);
      out.bInBounds = s.B.every(x => x.start >= 0 && x.end <= b.length && x.end > x.start);
      out.nA = s.A.length; out.nB = s.B.length;
    """)
    assert r["aInBounds"] and r["bInBounds"], "高亮区间必须落在原文范围内"
    # LCS 会把「屋里没人 / 屋子里空荡荡的」对齐成 屋·里 共用、中间各自增删，
    # 所以只断言各自独有的字，不假设整词对齐
    assert "没人" in r["aText"], f"A 栏应标出 A 独有的「没人」，实得 {r['aText']}"
    assert "空荡荡" in r["bText"], f"B 栏应标出 B 独有的「空荡荡」，实得 {r['bText']}"
    assert r["nA"] >= 1 and r["nB"] >= 1
    # 相同的「他推门进来，」和「桌上摆着两碗面…」不应出现在任一侧高亮里
    assert "他推门进来" not in r["aText"] and "他推门进来" not in r["bText"]


def test_spans_reconstruct_original_text():
    """高亮区间的并集必须能拼回原文：不能吞字、不能错位。"""
    r = _harness(f"""
      function recon(text, spans){{
        let out = '', p = 0;
        for(const s of spans){{ out += text.slice(p, s.start); out += text.slice(s.start, s.end); p = s.end; }}
        return out + text.slice(p);
      }}
      const a = {json.dumps(A_TEXT)}, b = {json.dumps(B_TEXT)};
      const s = DiffLite.highlightSpans(a, b);
      out.aRecon = recon(a, s.A);
      out.bRecon = recon(b, s.B);
      out.aOverlap = s.A.some((x, i) => i && x.start < s.A[i-1].end);
      out.bOverlap = s.B.some((x, i) => i && x.start < s.B[i-1].end);
    """)
    assert r["aRecon"] == A_TEXT, f"A 侧高亮吞字或错位：{r['aRecon']!r}"
    assert r["bRecon"] == B_TEXT, f"B 侧高亮吞字或错位：{r['bRecon']!r}"
    assert not r["aOverlap"] and not r["bOverlap"], "高亮区间不该重叠"


def test_small_gap_is_bridged():
    """相隔 ≤2 字的两处改动并成一处：避免「改一字 · 隔一字 · 又改一字」的满屏单字高亮。"""
    r = _harness("""
      // a：甲X乙Y丙；b：甲乙丙 → 两处删除被 1 字的「乙」隔开，应并成一段
      const a = '甲X乙Y丙', b = '甲乙丙';
      const s = DiffLite.highlightSpans(a, b);
      out.n = s.A.length;
      out.covered = s.A.length === 1 ? a.slice(s.A[0].start, s.A[0].end) : null;
      // 隔 3 字（超过阈值）不应合并
      const a2 = '甲X乙丙丁Y戊', b2 = '甲乙丙丁戊';
      const s2 = DiffLite.highlightSpans(a2, b2);
      out.n2 = s2.A.length;
    """)
    assert r["n"] == 1, f"相隔 1 字的两处删除应合并，实得 {r['n']} 段"
    assert r["covered"] == "X乙Y", f"合并后应覆盖中间被桥接的字，实得 {r['covered']!r}"
    assert r["n2"] == 2, "相隔 3 字的改动不应被桥接"


def test_empty_and_huge_inputs_do_not_throw():
    r = _harness("""
      out.cases = [
        ['', ''],
        ['', '新增的全部'],
        ['删掉的全部', ''],
        ['相同', '相同'],
      ].map(([a, b]) => {
        try { return {ok: true, r: DiffLite.highlightSpans(a, b)}; }
        catch(e) { return {ok: false, err: String(e)}; }
      });
      // 超长保护：DP 单元数封顶，不能 OOM / 卡死
      const big = '字'.repeat(30000);
      try { out.huge = {ok: true, parts: DiffLite.diffChars(big, big + '尾巴').length}; }
      catch(e) { out.huge = {ok: false, err: String(e)}; }
    """)
    for c in r["cases"]:
        assert c["ok"], f"边界输入抛异常：{c}"
    assert r["huge"]["ok"], "超长文本 diff 不该抛异常"


def test_diffchars_basic_shape():
    r = _harness("""
      out.parts = DiffLite.diffChars('abc', 'abd');
      out.empty = DiffLite.diffChars('', '');
      out.onlyDel = DiffLite.diffChars('abc', '');
      out.onlyIns = DiffLite.diffChars('', 'abc');
    """)
    kinds = [p["type"] for p in r["parts"]]
    assert kinds == ["equal", "del", "ins"], f"基本 diff 序列形状应为 equal/del/ins，实得 {kinds}"
    assert r["empty"] == []
    assert [p["type"] for p in r["onlyDel"]] == ["del"]
    assert [p["type"] for p in r["onlyIns"]] == ["ins"]


# ── composer：偏移契约与零回归 ────────────────────────────

def test_compose_html_text_content_intact():
    """diff 高亮 + 批注任意重叠时，剥标签后仍与原文逐字相等 → offsetOf 不会错位。"""
    r = _harness(f"""
      const t = {json.dumps(A_TEXT)};
      const mk = [
        {{id:1, start:6, end:10, kind:'用词'}},   // 与 diff 高亮重叠
        {{id:2, start:0, end:3, kind:'节奏'}},    // 在高亮外
      ];
      const spans = [{{start:5, end:11, type:'del'}}];
      const h = DiffLite.composeHtml(t, mk, spans, ['用词','解释过度','情绪直给','节奏','逻辑','意象','其他']);
      out.plain = h.replace(/<[^>]+>/g, '');
      out.nMark = (h.match(/<mark/g) || []).length;
      out.nDhl = (h.match(/class="dhl/g) || []).length;
      out.midKept = h.includes('data-mid="1"') && h.includes('data-mid="2"');
    """)
    assert r["plain"] == A_TEXT, f"composer 破坏了 textContent，offsetOf 会错位：{r['plain']!r}"
    assert r["nMark"] == 2 and r["nDhl"] >= 1
    assert r["midKept"], "mark 的 data-mid 必须保留（点删委托靠它）"


def test_compose_html_matches_buildmarked_without_spans():
    """无 diff 高亮时，composer 与被测试钉住的 buildMarkedHtml 逐字相等（零回归）。"""
    r = _harness(f"""
      const t = {json.dumps(A_TEXT)};
      const mk = [
        {{id:1, start:6, end:10, kind:'用词'}},
        {{id:2, start:3, end:8, kind:'节奏'}},   // 与前者重叠，按老规则应被跳过
        {{id:3, start:20, end:24, kind:'意象'}},
      ];
      const K = ['用词','解释过度','情绪直给','节奏','逻辑','意象','其他'];
      out.same = DiffLite.composeHtml(t, mk, null, K) === buildMarkedHtml(t, mk);
      out.sameEmpty = DiffLite.composeHtml(t, [], [], K) === buildMarkedHtml(t, []);
    """, with_block=True)
    assert r["same"], "无高亮时 composer 必须与 buildMarkedHtml 完全一致"
    assert r["sameEmpty"]


def test_compose_html_mark_coverage_unchanged_under_diff():
    """有 diff 高亮时，mark 覆盖的字必须与老实现一致（重叠跳过规则相同）。"""
    r = _harness(f"""
      function markedText(h){{ return (h.match(/<mark[^>]*>([\\s\\S]*?)<\\/mark>/g) || []).join(''); }}
      const t = {json.dumps(A_TEXT)};
      const mk = [
        {{id:1, start:6, end:10, kind:'用词'}},
        {{id:2, start:7, end:9, kind:'节奏'}},    // 与 1 重叠，应跳过
        {{id:3, start:20, end:24, kind:'意象'}},
      ];
      const K = ['用词','解释过度','情绪直给','节奏','逻辑','意象','其他'];
      const h1 = buildMarkedHtml(t, mk);
      const h2 = DiffLite.composeHtml(t, mk, [{{start:5, end:12, type:'del'}}], K);
      out.marked1 = markedText(h1);
      out.marked2 = markedText(h2);
      out.nMark1 = (h1.match(/<mark/g) || []).length;
      out.nMark2 = (h2.match(/<mark/g) || []).length;
      out.kindIdx = (h2.match(/class="(k\\d)" data-mid="1"/) || [])[1];
    """, with_block=True)
    assert r["marked1"] == r["marked2"], "diff 高亮不应改变批注覆盖的字"
    assert r["nMark1"] == r["nMark2"] == 2, "重叠批注同样要跳过"
    assert r["kindIdx"] == "k0", "kind→k 索引必须与老实现一致"


def test_compose_html_escapes_and_clamps():
    r = _harness(f"""
      const t = '他说<这>&「那」';
      const K = ['用词'];
      const h = DiffLite.composeHtml(t, [{{id:1, start:2, end:5, kind:'用词'}}],
                                     [{{start:0, end:99, type:'ins'}}], K);
      // textContent 会反转义实体，剥标签后手动反转义再比（这就是 DOM 看到的文本）
      function unesc(s){{ return s.replace(/&lt;/g,'<').replace(/&gt;/g,'>')
        .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&'); }}
      out.plain = unesc(h.replace(/<[^>]+>/g, ''));
      out.escLt = h.includes('&lt;');
      out.escAmp = h.includes('&amp;');
      out.nMark = (h.match(/<mark/g) || []).length;
    """)
    assert r["plain"] == "他说<这>&「那」", "转义后剥标签仍须等于原文"
    assert r["escLt"] and r["escAmp"]
    assert r["nMark"] == 1, "越界高亮区间被夹回后仍应渲染 mark"
