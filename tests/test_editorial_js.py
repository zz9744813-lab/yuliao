"""编辑台脚本（websrc/_shared/editorial.js）回归 —— 会审 87bd95235d 一般级 3 条。

用 node 直接跑真实的 editorial.js（不引 jsdom，自备最小 DOM stub），钉住：

(a) enhanceNav：导航里只有 1 个 .lg-nav-list 时不得抛 TypeError，
    且后续导航项编号（.idx = 01/02/03）与检索按钮仍完成构建；
(b) refresh 失败分支：连续 2 次读取失败后 #banner 内「重新读取」按钮数量 == 1（去重）；
(c) enhancePage：LG.MODULES 查不到 slug（findIndex == -1）时 kicker 渲染
    「RESEARCH ARCHIVE / --」哨兵，绝不出现静默降级的「00」。

node 查找方式沿用 tests/test_diff_js.py。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EDITORIAL_JS = ROOT / "websrc" / "_shared" / "editorial.js"


def _node() -> str:
    for cand in ("node", r"F:\Geek\node.exe",
                 r"C:\Users\6\.workbuddy-ai\binaries\node\versions\22.22.2-2\node.exe"):
        p = shutil.which(cand) if not cand.startswith(("F:", "C:")) else (
            cand if Path(cand).exists() else None)
        if p:
            return p
    pytest.skip("找不到可用的 node，跳过前端逻辑测试")


# ── 最小 DOM stub + editorial.js 装载器 ─────────────────────
# setupPage(opts)：opts = {lists, items, modules, fetch(slug)->Promise}
# loadEditorial()：把真实 editorial.js 包进全局 LG 的 init（其 IIFE 在装载时抓取 LG.init）。
PRELUDE = r"""
const fs = require('fs');
const EDITORIAL_PATH = %EDITORIAL_PATH%;
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this._children = []; this._parent = null; this._text = ''; this._raw = '';
    this._listeners = {}; this.id = ''; this.className = ''; this.type = '';
    this.disabled = false; this.style = {}; this.dataset = {};
  }
  get children() { return this._children; }
  get textContent() {
    return this._children.length ? this._children.map(c => c.textContent).join('') : this._text;
  }
  set textContent(v) { this._children = []; this._text = String(v); }
  get innerHTML() { return this._raw; }
  set innerHTML(v) {
    this._raw = String(v); this._children = []; this._text = '';
    const re = /<(\w+)([^>]*)>/g; let m;
    while ((m = re.exec(this._raw))) {
      const el = new El(m[1]);
      const idm = /id="([^"]*)"/.exec(m[2]); if (idm) el.id = idm[1];
      const cm = /class="([^"]*)"/.exec(m[2]); if (cm) el.className = cm[1];
      el._parent = this; this._children.push(el);
    }
  }
  get classList() {
    const self = this;
    return {
      add(c) { const s = new Set(self.className.split(/\s+/).filter(Boolean)); s.add(c); self.className = [...s].join(' '); },
      remove(c) { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(' '); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
      toggle(c, on) { if (on) this.add(c); else this.remove(c); },
    };
  }
  _detach() { if (this._parent) { const i = this._parent._children.indexOf(this); if (i >= 0) this._parent._children.splice(i, 1); this._parent = null; } }
  appendChild(node) { node._detach(); node._parent = this; this._children.push(node); return node; }
  prepend(node) { node._detach(); node._parent = this; this._children.unshift(node); return node; }
  insertBefore(node, ref) {
    node._detach();
    const i = ref ? this._children.indexOf(ref) : -1;
    if (i < 0) { node._parent = this; this._children.push(node); return node; }
    node._parent = this; this._children.splice(i, 0, node); return node;
  }
  setAttribute(k, v) { if (k === 'id') this.id = String(v); }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  _fire(t, ev) { (this._listeners[t] || []).forEach(fn => fn.call(this, ev || { target: this, preventDefault() {} })); }
  focus() {}
  scrollIntoView() {}
  _walk() { const acc = []; (function rec(n) { for (const c of n._children) { acc.push(c); rec(c); } })(this); return acc; }
  _parts(sel) { return sel.split(',').map(s => s.trim()); }
  _matches(part) {
    if (part.startsWith('.')) return this.className.split(/\s+/).includes(part.slice(1));
    if (part.startsWith('#')) return this.id === part.slice(1);
    return this.tagName === part.toUpperCase();
  }
  _matchesAny(sel) { return this._parts(sel).some(p => this._matches(p)); }
  querySelector(sel) { return this._walk().find(n => n._matchesAny(sel)) || null; }
  querySelectorAll(sel) { return this._walk().filter(n => n._matchesAny(sel)); }
}
function setupPage(opts) {
  opts = opts || {};
  globalThis.MutationObserver = class { constructor() {} observe() {} disconnect() {} };
  globalThis.location = { href: '' };
  const body = new El('body');
  const doc = {
    body, activeElement: null,
    createElement: t => new El(t),
    _listeners: {},
    addEventListener(t, fn) { (doc._listeners[t] = doc._listeners[t] || []).push(fn); },
    getElementById(id) { return body.id === id ? body : (body._walk().find(n => n.id === id) || null); },
    querySelector(sel) { return body._matchesAny(sel) ? body : body.querySelector(sel); },
    querySelectorAll(sel) {
      const r = body._matchesAny(sel) ? [body].concat(body.querySelectorAll(sel)) : body.querySelectorAll(sel);
      return r;
    },
  };
  globalThis.document = doc;
  const nav = new El('nav'); nav.className = 'lg-nav';
  const brand = new El('div'); brand.className = 'lg-brand'; nav.appendChild(brand);
  const lists = [];
  for (let l = 0; l < (opts.lists === undefined ? 3 : opts.lists); l++) {
    const list = new El('div'); list.className = 'lg-nav-list'; nav.appendChild(list); lists.push(list);
    if (l < (opts.lists === undefined ? 3 : opts.lists) - 1) {
      const sep = new El('div'); sep.className = 'lg-nav-sep'; nav.appendChild(sep);
    }
  }
  for (let i = 0; i < (opts.items === undefined ? 3 : opts.items); i++) {
    const a = new El('a'); a.className = 'lg-nav-item'; a.textContent = '模块' + (i + 1);
    lists[0].appendChild(a);
  }
  body.appendChild(nav);
  const head = new El('div'); head.className = 'lg-head'; body.appendChild(head);
  const banner = new El('div'); banner.id = 'banner'; banner.className = 'lg-banner'; body.appendChild(banner);
  globalThis.LG = {
    init(slug, renderer, o) { doc._origInit = (doc._origInit || 0) + 1; },
    esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    MODULES: opts.modules || [
      { slug: 'm1', zh: '甲', en: 'alpha', page: 'm1.html' },
      { slug: 'm2', zh: '乙', en: 'beta', page: 'm2.html' },
    ],
    fetchModule: slug => (opts.fetch || (() => Promise.resolve({})))(slug),
    showBanner(msg, retryable) { banner.classList.add('err'); },
    hideBanner() { banner.classList.remove('err'); },
  };
  return { doc, nav, head, banner, lists };
}
function loadEditorial() { new Function(fs.readFileSync(EDITORIAL_PATH, 'utf8'))(); }
"""


def _run_js(body: str) -> dict:
    script = "\n".join([
        PRELUDE.replace("%EDITORIAL_PATH%", json.dumps(str(EDITORIAL_JS).replace("\\", "/"))),
        "const out = {};",
        "(async () => {",
        body,
        "})().then(() => { console.log(JSON.stringify(out)); },",
        "  e => { console.error('HARNESS_ERROR: ' + (e && e.stack || e)); process.exit(1); });",
    ])
    proc = subprocess.run([_node(), "-e", script], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node 失败:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── (a) enhanceNav：1 个 .lg-nav-list 不得抛 TypeError，编号仍完成 ──

def test_enhance_nav_survives_missing_nav_lists():
    r = _run_js("""
  const pages = setupPage({ lists: 1, items: 3 });
  loadEditorial();
  out.threw = null;
  try { LG.init('m1', function () {}); }
  catch (e) { out.threw = (e && e.name ? e.name : 'Error') + ': ' + (e && e.message ? e.message : String(e)); }
  out.idxs = pages.nav.querySelectorAll('.lg-nav-item').map(function (link) {
    const first = link.children[0];
    return first && first.className === 'idx' ? first.textContent : null;
  });
  out.sections = pages.nav.querySelectorAll('.lg-nav-section').map(s => s.textContent);
  out.hasRail = !!pages.nav.querySelector('.lg-nav-rail');
  out.hasSearchBtn = !!document.getElementById('lg-search-btn');
""")
    assert r["threw"] is None, f"导航列表不足 3 个时 enhanceNav 抛异常（原 TypeError 缺陷）：{r['threw']}"
    assert r["idxs"] == ["01", "02", "03"], f"守卫不得中断后续导航项编号，实得 {r['idxs']}"
    assert r["hasRail"], ".lg-nav-rail 未构建"
    assert r["hasSearchBtn"], "检索按钮未构建（enhanceNav 被打断）"
    assert r["sections"] == ["01 / 观察与材料"], (
        f"只有 1 个 .lg-nav-list 时应只挂第 1 个分组标题，跳过其余，实得 {r['sections']}"
    )


def test_enhance_nav_three_lists_regression_control():
    """对照组：3 个 .lg-nav-list 的常规页面，三个分组标题齐全（守卫不误伤正常路径）。"""
    r = _run_js("""
  const pages = setupPage({ lists: 3, items: 2 });
  loadEditorial();
  LG.init('m1', function () {});
  out.sections = pages.nav.querySelectorAll('.lg-nav-section').map(s => s.textContent);
  out.idxs = pages.nav.querySelectorAll('.lg-nav-item').map(link => link.children[0].textContent);
""")
    assert r["sections"] == ["01 / 观察与材料", "02 / 判断与校准", "03 / 系统与交接"]
    assert r["idxs"] == ["01", "02"]


# ── (b) 失败分支：连续 2 次读取失败后「重新读取」按钮不堆叠 ──

def test_retry_button_deduplicated_after_two_failures():
    r = _run_js("""
  const pages = setupPage({ lists: 3, items: 1, fetch: () => Promise.reject(new Error('network down')) });
  loadEditorial();
  LG.init('m1', function () {});
  const btn = document.getElementById('lg-refresh-btn');
  async function failOnce() { btn._fire('click'); await new Promise(res => setTimeout(res, 30)); }
  await failOnce();
  await failOnce();
  out.failures = 2;
  out.bannerErr = pages.banner.classList.contains('err');
  out.retryTexts = pages.banner._walk()
    .filter(n => n.tagName === 'BUTTON' && n.textContent === '重新读取').length;
  out.retryClassed = pages.banner.querySelectorAll('.lg-banner-retry').length;
  out.syncText = document.getElementById('lg-sync').textContent;
""")
    assert r["bannerErr"], "两次失败后 banner 应处于 err 态"
    assert r["retryTexts"] == 1, (
        f"连续 2 次读取失败后「重新读取」按钮应恰为 1 个（去重），实得 {r['retryTexts']} 个"
    )
    assert r["retryClassed"] == 1
    assert "读取失败" in r["syncText"]


# ── (c) findIndex == -1：kicker 显式渲染「--」，不得静默降级成「00」 ──

def test_unknown_slug_kicker_uses_visible_sentinel_not_zero():
    r = _run_js("""
  const pages = setupPage({ lists: 3, items: 1 });
  loadEditorial();
  LG.init('ghost-slug', function () {});
  const kicker = pages.head.children.find(c => c.className === 'lg-kicker');
  out.kicker = kicker ? kicker.textContent : null;
  // 对照：已知 slug 'm2'（MODULES[1]）正常渲染 02
  const pages2 = setupPage({ lists: 3, items: 1 });
  loadEditorial();
  LG.init('m2', function () {});
  const kicker2 = pages2.head.children.find(c => c.className === 'lg-kicker');
  out.control = kicker2 ? kicker2.textContent : null;
""")
    assert r["kicker"] is not None, "unknown slug 页面 kicker 未渲染"
    assert "00" not in r["kicker"], f"findIndex==-1 不得静默渲染成 00，实得 {r['kicker']!r}"
    assert r["kicker"] == "RESEARCH ARCHIVE  /  --", (
        f"可判定行为：目录缺失 slug 时 kicker 固定渲染 '--' 哨兵，实得 {r['kicker']!r}"
    )
    assert r["control"] == "RESEARCH ARCHIVE  /  02", f"已知 slug 的编号渲染被改坏：{r['control']!r}"
