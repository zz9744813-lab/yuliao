"""编辑台会审残留收口回归（会审 87bd95235d 一般级 4 条，任务单 gui-editorial-residual）。

逐条钉住：
(1) enhanceNav 硬编码 3 组 .lg-nav-list —— bc23a8f 已加存在性守卫；本文件用
    lists=0 / lists=5 两端钉住（互补于 test_editorial_js 的 lists=1 用例，只增不减）。
(2) refresh 失败分支「重新读取」按钮堆叠 —— bc23a8f 已去重；本文件钉
    「初载 + 2 次点击重试共 3 连败，banner 上按钮数恒 1，fetch 恰好 3 次」。
(3) index.html showDone/openDoneRecords 用内联 style 判可见性（class/CSS 隐藏时
    恒 false，点「查看已判记录」可能反向收起队列）—— 本次改 getComputedStyle，
    真跑抽取出的函数源码验证三个场景（内联隐藏 / class 隐藏 / 可见）+ 双向开合。
(4) 两份 editorial.css 同名不同口径 —— 本次把语义层结构化为「刻度+别名」，
    并以两条机械对账钉住：刻度逐值一致 + 语义变量必须 var() 指向刻度（禁止裸色值）。

C2 漂移可抓性：改任一侧任一刻度 hex、改语义→刻度的映射、或重新引入裸色值，
必有一条用例转红（反向验证见交付文档 docs/编辑台会审残留收口_20260926.md）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_editorial_js import EDITORIAL_JS, PRELUDE, _node  # noqa: E402

INDEX_HTML = ROOT / "app" / "static" / "index.html"
APP_CSS = ROOT / "app" / "static" / "editorial.css"
WEBSRC_CSS = ROOT / "websrc" / "_shared" / "editorial.css"

# 语义层 → 刻度 的权威映射（与 tokens.css 的两层令牌口径一致）
EXPECTED_MAPPING = {
    "--bg": "--n-100",
    "--surface": "--n-0",
    "--surface-2": "--n-50",
    "--line": "--n-300",
    "--line-2": "--n-400",
    "--fg": "--n-900",
    "--fg-2": "--n-700",
    "--dim": "--n-600",
    "--accent": "--acc-500",
    "--accent-soft": "--acc-100",
    "--accent-strong": "--acc-600",
    "--paper-bg": "--n-0",
    "--ctx-fg": "--n-600",
}


def _run_node(body: str, **subs: str) -> dict:
    """与 test_editorial_js._run_js 同款装载器，额外支持 %KEY% 替换（含 body）。"""
    script = "\n".join([
        PRELUDE,
        "const out = {};",
        "(async () => {",
        body,
        "})().then(() => { console.log(JSON.stringify(out)); },",
        "  e => { console.error('HARNESS_ERROR: ' + (e && e.stack || e)); process.exit(1); });",
    ])
    script = script.replace(
        "%EDITORIAL_PATH%", json.dumps(str(EDITORIAL_JS).replace("\\", "/"))
    )
    for key, val in subs.items():
        script = script.replace("%" + key + "%", json.dumps(val))
    import subprocess

    proc = subprocess.run([_node(), "-e", script], capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 失败:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _extract_fn(src: str, name: str) -> str:
    """从 index.html 抽出 function name(){...} 源码（花括号配平）。"""
    marker = "function " + name + "("
    i = src.index(marker)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"index.html 里 {name} 函数体未闭合")


# ── (1) enhanceNav：0 组 / 5 组导航列表都不得中断初始化（bc23a8f 守卫的现状钉） ──

def test_enhance_nav_survives_zero_and_extra_nav_lists():
    out = _run_node("""
  const p0 = setupPage({ lists: 0, items: 0 });
  loadEditorial();
  out.threw0 = null;
  try { LG.init('m1', function () {}); }
  catch (e) { out.threw0 = String(e); }
  out.rail0 = !!p0.nav.querySelector('.lg-nav-rail');
  const p5 = setupPage({ lists: 5, items: 3 });
  loadEditorial();
  out.threw5 = null;
  try { LG.init('m1', function () {}); }
  catch (e) { out.threw5 = String(e); }
  out.sections5 = p5.nav.querySelectorAll('.lg-nav-section').length;
  out.rail5 = !!p5.nav.querySelector('.lg-nav-rail');
  out.items5 = p5.nav.querySelectorAll('.lg-nav-item').length;
""")
    assert out["threw0"] is None, f"0 组导航列表时初始化抛错：{out['threw0']}"
    assert out["rail0"] is True, "0 组导航列表时侧栏未构建"
    assert out["threw5"] is None, f"5 组导航列表时初始化抛错：{out['threw5']}"
    assert out["rail5"] is True
    # 硬编码的 3 个分组标题只发给存在的前 3 组，绝不越界
    assert out["sections5"] == 3, f"分组标题数={out['sections5']}，应为 3"
    assert out["items5"] == 3, "导航项在多组场景下丢失"


# ── (2) banner「重新读取」按钮：3 连败（初载 + 2 次点击重试）恒 1 个 ──

def test_banner_retry_button_dedup_across_repeated_failures():
    out = _run_node("""
  const pages = setupPage({
    lists: 3, items: 3,
    fetch: function () { out.fetchCalls = (out.fetchCalls || 0) + 1;
                          return Promise.reject(new Error('模拟读取失败')); },
  });
  loadEditorial();
  LG.init('m1', function () {});
  const tick = () => new Promise(r => setTimeout(r, 5));
  const banner = pages.banner;
  const retryCount = () => banner.querySelectorAll('.lg-banner-retry').length;
  const refreshBtn = document.getElementById('lg-refresh-btn');
  refreshBtn._fire('click'); await tick(); await tick(); await tick();
  out.afterFirst = retryCount();
  banner.querySelector('.lg-banner-retry')._fire('click'); await tick(); await tick(); await tick();
  out.afterSecond = retryCount();
  banner.querySelector('.lg-banner-retry')._fire('click'); await tick(); await tick(); await tick();
  out.afterThird = retryCount();
""")
    assert out["afterFirst"] == 1, f"初次失败后按钮数={out['afterFirst']}"
    assert out["afterSecond"] == 1, f"第 2 次失败后按钮堆叠为 {out['afterSecond']}（去重失效）"
    assert out["afterThird"] == 1, f"第 3 次失败后按钮堆叠为 {out['afterThird']}（去重失效）"
    assert out["fetchCalls"] == 3, f"fetch 调用数={out['fetchCalls']}，重试按钮未真正触发刷新"


# ── (3) toggleQueue / openDoneRecords：可见性判定走 getComputedStyle ──

def _queue_js_src() -> tuple[str, str]:
    src = INDEX_HTML.read_text(encoding="utf-8")
    toggle = _extract_fn(src, "toggleQueue")
    open_fn = _extract_fn(src, "openDoneRecords")
    return toggle, open_fn


def test_toggle_queue_both_directions_use_computed_style():
    toggle_src, open_src = _queue_js_src()
    # 静态钉：判定必须是 getComputedStyle，不得再回退到内联 style
    assert "getComputedStyle(" in toggle_src, "toggleQueue 未使用 getComputedStyle"
    assert "style.display === 'none'" not in toggle_src, "toggleQueue 仍用内联 style 判可见性"
    assert "getComputedStyle(" in open_src, "openDoneRecords 未使用 getComputedStyle"
    assert "style.display === 'none'" not in open_src, "openDoneRecords 仍用内联 style 判可见性"

    out = _run_node("""
  eval(%TOGGLE_SRC%);
  // 真实语义：computed display 由「样式表/class 规则」与内联样式共同决定——
  // 内联非空时内联胜出；内联为空则回落到样式表规则（_sheetDisplay）。
  globalThis.getComputedStyle = el => ({
    display: el.style.display || el._sheetDisplay || 'block',
  });
  let tabs = [];
  globalThis.queueTab = m => { tabs.push(m); };
  globalThis.queueMode = 'done';
  // D) 可见（inline ''，computed block）→ 收起
  const wD = new El('div'); wD.id = 'rv-q-wrap'; wD.style.display = '';
  const bD = new El('button'); bD.id = 'rv-q-btn'; bD.textContent = '收起';
  globalThis.$ = s => s === '#rv-q-wrap' ? wD : (s === '#rv-q-btn' ? bD : null);
  toggleQueue();
  out.D = { display: wD.style.display, label: bD.textContent };
  // E) 隐藏（inline none）→ 展开
  const wE = new El('div'); wE.id = 'rv-q-wrap'; wE.style.display = 'none';
  const bE = new El('button'); bE.id = 'rv-q-btn';
  globalThis.$ = s => s === '#rv-q-wrap' ? wE : (s === '#rv-q-btn' ? bE : null);
  toggleQueue();
  out.E = { display: wE.style.display, label: bE.textContent,
            computed: getComputedStyle(wE).display };
""", TOGGLE_SRC=toggle_src, OPEN_SRC=open_src)
    assert out["D"] == {"display": "none", "label": "展开"}, out["D"]
    assert out["E"]["label"] == "收起", out["E"]
    # 关键：展开后必须真的可见——'none' 或空串在样式表压制下都可能仍隐藏
    assert out["E"]["computed"] != "none", f"展开后元素仍不可见：{out['E']}"
    assert out["E"]["display"] == "block", f"展开必须显式 block：{out['E']}"


def test_open_done_records_expands_hidden_queue_and_never_collapses():
    toggle_src, open_src = _queue_js_src()
    out = _run_node("""
  eval(%TOGGLE_SRC%);
  eval(%OPEN_SRC%);
  // 真实语义：computed display 由「样式表/class 规则」与内联样式共同决定——
  // 内联非空时内联胜出；内联为空则回落到样式表规则（_sheetDisplay）。
  globalThis.getComputedStyle = el => ({
    display: el.style.display || el._sheetDisplay || 'block',
  });
  let tabs = [];
  globalThis.queueTab = m => { tabs.push(m); };
  globalThis.queueMode = 'done';
  // A) 初始态：内联 display:none → 必须展开队列
  const wA = new El('div'); wA.id = 'rv-q-wrap'; wA.style.display = 'none';
  const bA = new El('button'); bA.id = 'rv-q-btn';
  globalThis.$ = s => s === '#rv-q-wrap' ? wA : (s === '#rv-q-btn' ? bA : null);
  openDoneRecords();
  out.A = { display: wA.style.display, label: bA.textContent, tabs: tabs.length,
            computed: getComputedStyle(wA).display };
  // B) class/CSS 隐藏（inline '' 但 computed none）——旧代码在此不作为（残洞）
  tabs = [];
  // B) class/样式表隐藏（内联为空，_sheetDisplay='none'）——展开必须显式写内联
  const wB = new El('div'); wB.id = 'rv-q-wrap'; wB.style.display = ''; wB._sheetDisplay = 'none';
  const bB = new El('button'); bB.id = 'rv-q-btn'; bB.textContent = '展开';
  globalThis.$ = s => s === '#rv-q-wrap' ? wB : (s === '#rv-q-btn' ? bB : null);
  openDoneRecords();
  out.B = { display: wB.style.display, label: bB.textContent, tabs: tabs.length,
            computed: getComputedStyle(wB).display };
  // C) 已可见 → 绝不反向收起（残洞症状：点「查看已判记录」反而把队列收起）
  tabs = [];
  const wC = new El('div'); wC.id = 'rv-q-wrap'; wC.style.display = '';
  const bC = new El('button'); bC.id = 'rv-q-btn'; bC.textContent = '收起';
  globalThis.$ = s => s === '#rv-q-wrap' ? wC : (s === '#rv-q-btn' ? bC : null);
  openDoneRecords();
  out.C = { display: wC.style.display, label: bC.textContent, tabs: tabs.length,
            computed: getComputedStyle(wC).display };
""", TOGGLE_SRC=toggle_src, OPEN_SRC=open_src)
    assert out["A"]["label"] == "收起", out["A"]
    assert out["A"]["computed"] != "none", f"初始隐藏态点开仍不可见：{out['A']}"
    assert out["A"]["display"] == "block", out["A"]
    assert out["A"]["tabs"] == 2, out["A"]  # queueTab('done') + toggleQueue 内 queueTab(queueMode)
    # 修复点：class 隐藏也必须触发展开
    assert out["B"]["label"] == "收起" and out["B"]["tabs"] == 2, out["B"]
    assert out["B"]["computed"] != "none", f"class 隐藏下点了展开但元素仍不可见：{out['B']}"
    assert out["B"]["display"] == "block", f"展开必须显式 block（'' 压不过 class）：{out['B']}"
    # 修复点：可见时绝不收起
    assert out["C"]["computed"] != "none", f"队列被反向收起：{out['C']}"
    assert out["C"]["label"] == "收起" and out["C"]["tabs"] == 1, out["C"]


# ── (4) 两份 editorial.css：刻度逐值一致 + 语义层必须是刻度别名（C2 机械对账） ──

def _css_theme_blocks(text: str) -> tuple[str, str]:
    light = re.search(r":root\s*\{([^}]*)\}", text)
    dark = re.search(r'html\[data-theme="dark"\]\s*\{([^}]*)\}', text)
    assert light and dark, "CSS 主题块缺失"
    return light.group(1), dark.group(1)


def _var_map(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+)", block)}


def _scale_vars(block_map: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in block_map.items() if re.fullmatch(r"--(?:n|acc)-\d+", k)}


def test_editorial_css_palette_scale_identical_across_two_files():
    web_light, web_dark = _css_theme_blocks(WEBSRC_CSS.read_text(encoding="utf-8"))
    app_light, app_dark = _css_theme_blocks(APP_CSS.read_text(encoding="utf-8"))
    for theme, web_block, app_block in (("light", web_light, app_light), ("dark", web_dark, app_dark)):
        web_scale = _scale_vars(_var_map(web_block))
        app_scale = _scale_vars(_var_map(app_block))
        assert set(web_scale) <= set(app_scale), (
            f"{theme}：app 侧刻度缺 {sorted(set(web_scale) - set(app_scale))}"
        )
        drift = {k: (web_scale[k], app_scale.get(k)) for k in web_scale if app_scale.get(k) != web_scale[k]}
        assert not drift, f"{theme}：两份 CSS 刻度漂移（websrc 值, app 值）→ {drift}"


def test_app_css_semantic_vars_are_scale_aliases():
    app_text = APP_CSS.read_text(encoding="utf-8")
    web_light, web_dark = _css_theme_blocks(WEBSRC_CSS.read_text(encoding="utf-8"))
    app_light, _ = _css_theme_blocks(app_text)
    app_map = _var_map(app_light)

    for name, target in EXPECTED_MAPPING.items():
        hits = re.findall(rf"{re.escape(name)}\s*:\s*([^;]+)", app_text)
        assert hits, f"app 侧丢失语义变量 {name}"
        assert len(hits) == 1, f"语义变量 {name} 被声明 {len(hits)} 次（只允许 :root 一次）"
        val = hits[0].strip()
        m = re.fullmatch(r"var\((--(?:n|acc)-\d+)\)", val)
        assert m, f"语义变量 {name} 必须是 var() 刻度别名，实际为裸值 {val!r}"
        assert m.group(1) == target, f"{name} 指向 {m.group(1)}，权威映射应为 {target}"
        # 别名指向的刻度名必须真实存在
        assert target in app_map, f"{name} 指向的 {target} 不在 app 刻度里"

    # websrc 暗色块的语义覆盖也必须是别名，且映射同表（漂移钉）
    web_dark_map = _var_map(web_dark)
    for name in ("--bg", "--surface", "--surface-2", "--line", "--line-2",
                 "--fg", "--fg-2", "--dim"):
        val = web_dark_map.get(name)
        assert val is not None, f"websrc 暗色块丢失 {name}"
        m = re.fullmatch(r"var\((--(?:n|acc)-\d+)\)", val)
        assert m, f"websrc 暗色 {name} 仍是裸值 {val!r}（口径未对齐）"
        assert m.group(1) == EXPECTED_MAPPING[name], (
            f"websrc 暗色 {name} 映射 {m.group(1)} 与权威表 {EXPECTED_MAPPING[name]} 不一致"
        )
