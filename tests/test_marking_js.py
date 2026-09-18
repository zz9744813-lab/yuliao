"""噪点批注前端逻辑回归（2026-09-14）。

不靠截图——截图只能证明"看起来对"，证明不了边界行为。这里把 `index.html` 里
**真实的**批注代码块抽出来，用 Node 跑断言，覆盖几个真会出错的地方：

1. 正常渲染：选中区间要被 `<mark>` 包住，其余原样保留；
2. **HTML 转义**：小说正文里有「」《》和可能的 `<`，不转义会把页面结构冲掉；
3. **重叠批注**：用户在同一处标两次时必须跳过后者，否则 mark 会嵌套、offset 语义崩坏；
4. 越界/空区间：不能抛异常，也不能生成空 mark。

需要 node 可执行（沿用 agent-browser 那套环境）。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "app" / "static" / "index.html"


def _node() -> str:
    for cand in ("node", r"F:\Geek\node.exe",
                 r"C:\Users\6\.workbuddy-ai\binaries\node\versions\22.22.2-2\node.exe"):
        p = shutil.which(cand) if not cand.startswith(("F:", "C:")) else (
            cand if Path(cand).exists() else None)
        if p:
            return p
    pytest.skip("找不到可用的 node，跳过前端逻辑测试")


def _extract_block() -> str:
    src = HTML.read_text(encoding="utf-8")
    start = src.index("// ── 噪点批注")
    end = src.index("// 选中 / 点删的委托")
    return src[start:end]


def _run_js(js: str) -> dict:
    """在 node 里跑一段脚本，返回其打印的 JSON。"""
    proc = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 失败:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _harness(assertions: str) -> dict:
    block = _extract_block()
    js = f"""
    {block}
    const out = {{}};
    {assertions}
    console.log(JSON.stringify(out));
    """
    return _run_js(js)


def test_marks_wrap_selected_span_only():
    r = _harness("""
      const t = '他推门进来，屋里没人。';
      const h = buildMarkedHtml(t, [{id:1, start:6, end:9, kind:'用词'}]);
      out.html = h;
      out.hasMark = h.includes('<mark class="k0"');
      out.plain = h.replace(/<[^>]+>/g, '');
    """)
    assert r["hasMark"], "选中区间必须被 mark 包住"
    assert r["plain"] == "他推门进来，屋里没人。", "mark 之外的原文必须一字不差"
    assert '>屋里没<' in r["html"], f"包住的应是选中的三个字，实得 {r['html']}"


def test_html_special_chars_escaped():
    r = _harness("""
      const t = '他说<这是标签>&「引号」';
      const h = buildMarkedHtml(t, [{id:1, start:2, end:6, kind:'用词'}]);
      out.html = h;
      // 剥掉 mark 标签后，正文里不该再有任何裸尖括号
      out.stripped = h.replace(/<mark[^>]*>/g, '').replace(/<\\/mark>/g, '');
      out.escLt = h.includes('&lt;');
      out.escGt = h.includes('&gt;');
      out.escAmp = h.includes('&amp;');
    """)
    assert r["escLt"] and r["escGt"] and r["escAmp"], f"转义不完整：{r['html']}"
    assert "<" not in r["stripped"] and ">" not in r["stripped"], (
        f"正文里仍有裸尖括号，会冲掉页面结构：{r['stripped']}")


def test_overlapping_marks_skipped():
    """同一处标两次：后者跳过，不能嵌套 mark。"""
    r = _harness("""
      const t = '0123456789';
      const h = buildMarkedHtml(t, [
        {id:1, start:1, end:5, kind:'用词'},
        {id:2, start:3, end:8, kind:'节奏'},
      ]);
      out.html = h;
      out.nMark = (h.match(/<mark/g) || []).length;
      out.plain = h.replace(/<[^>]+>/g, '');
    """)
    assert r["nMark"] == 1, f"重叠批注应只渲染第一个，实得 {r['nMark']} 个"
    assert r["plain"] == "0123456789", "跳过后原文仍须完整"


def test_non_overlapping_marks_both_rendered():
    r = _harness("""
      const t = '0123456789';
      const h = buildMarkedHtml(t, [
        {id:1, start:1, end:3, kind:'用词'},
        {id:2, start:5, end:8, kind:'节奏'},
      ]);
      out.nMark = (h.match(/<mark/g) || []).length;
      out.plain = h.replace(/<[^>]+>/g, '');
    """)
    assert r["nMark"] == 2
    assert r["plain"] == "0123456789"


def test_out_of_range_and_empty_do_not_throw():
    """越界或空区间不能抛异常、也不能生成空 mark（用户拖一下鼠标就会产生）。"""
    r = _harness("""
      const t = '短文本';
      const cases = [
        [{id:1, start:0, end:99, kind:'用词'}],
        [{id:2, start:2, end:2, kind:'用词'}],
        [{id:3, start:99, end:120, kind:'用词'}],
        [{id:4, start:-5, end:2, kind:'用词'}],
      ];
      out.results = cases.map(c => {
        try {
          const h = buildMarkedHtml(t, c);
          return {ok:true, plain:h.replace(/<[^>]+>/g,''), nMark:(h.match(/<mark/g)||[]).length};
        } catch(e) { return {ok:false, err:String(e)}; }
      });
    """)
    for res in r["results"]:
        assert res["ok"], f"越界输入抛异常了：{res}"
        assert res["plain"] == "短文本", f"越界输入破坏了原文：{res}"
    # 空区间与完全越界不应产生 mark
    assert r["results"][1]["nMark"] == 0
    assert r["results"][2]["nMark"] == 0
    # 起止都越界但夹住全文时，应正好包住全文
    assert r["results"][0]["nMark"] == 1
    assert r["results"][3]["nMark"] == 1


def test_kind_maps_to_stable_class():
    """每种 kind 必须映射到固定的 k 索引——否则颜色会串。"""
    r = _harness("""
      out.kinds = MARK_KINDS;
      out.classes = MARK_KINDS.map(k => {
        const h = buildMarkedHtml('abcdef', [{id:1, start:0, end:2, kind:k}]);
        return (h.match(/class="(k\\d)"/) || [])[1];
      });
      out.unknown = (buildMarkedHtml('abcdef', [{id:1,start:0,end:2,kind:'不存在'}])
                     .match(/class="(k\\d)"/) || [])[1];
    """)
    assert r["classes"] == [f"k{i}" for i in range(len(r["kinds"]))], r["classes"]
    assert r["unknown"] == "k0", "未知 kind 应回落到第一个而不是崩掉"
