/* Language Genome · 研究台共享脚本（2026-09-19）
   ─────────────────────────────────────────────────────────────
   一份脚本干四件事：
     1) 注入侧栏导航（16 模块，当前页高亮）—— 页面不必各自手写导航
     2) 主题切换（默认石墨深色，localStorage 键 lg_theme）
     3) fetch /console/<slug> 并解开 {module,title,data} 信封 + 统一错误态
     4) 一小组渲染件：指标卡 / 分布条 / 表格（可排序）/ 徽章 / 空态

   命名空间纪律：本地存储一律 lg_* 前缀，绝不碰盲评台的 rv_* 键
   （2026-09-18 教训：跨标签页持久化会把使用者钉在旧偏好上）。

   用法（每页只需一次调用）：
     LG.init('dashboard', function(data, el){ ... 渲染 ... });
*/

window.LG = (function () {
  'use strict';

  /* ── 模块表：顺序 = app/console.py::MODULE_ORDER，不得私改顺序 ── */
  var MODULES = [
    { slug: 'dashboard',            page: 'overview.html',      zh: '总览总控台', en: 'Dashboard' },
    { slug: 'corpus',               page: 'corpus.html',        zh: '语料库',     en: 'Corpus' },
    { slug: 'semantic-lab',         page: 'frames.html',        zh: '语义实验室', en: 'Semantic Lab' },
    { slug: 'reconstruction-arena', page: 'arena.html',         zh: '重构竞技场', en: 'Arena' },
    { slug: 'expression-residual',  page: 'residual.html',      zh: '表达残差',   en: 'Residual' },
    { slug: 'strategy-atlas',       page: 'strategy.html',      zh: '策略图谱',   en: 'Strategy' },
    { slug: 'judge-arena',          page: 'judges.html',        zh: '评委竞技场', en: 'Judges' },
    { slug: 'preference-lab',       page: 'preference.html',    zh: '偏好实验室', en: 'Preference' },
    { slug: 'hard-cases',           page: 'hardcase.html',      zh: '硬样本库',   en: 'Hard Cases' },
    { slug: 'benchmarks',           page: 'benchmark.html',     zh: '基准与回归', en: 'Benchmarks' },
    { slug: 'experiments',          page: 'experiments.html',   zh: '实验引擎',   en: 'Experiments' },
    { slug: 'models',               page: 'models.html',        zh: '模型管理',   en: 'Models' },
    { slug: 'training-data',        page: 'training.html',      zh: '训练数据',   en: 'Training' },
    { slug: 'workflow',             page: 'workflow.html',      zh: '工作流',     en: 'Workflow' },
    { slug: 'observability',        page: 'observability.html', zh: '可观测',     en: 'Observability' },
    { slug: 'settings',             page: 'settings.html',      zh: '设置与交接', en: 'Settings' }
  ];

  var THEME_KEY = 'lg_theme';

  /* ── 小工具 ──────────────────────────────────────────────── */

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtNum(n) {
    if (typeof n === 'number' && isFinite(n)) return n.toLocaleString('zh-CN');
    if (typeof n === 'string' && n !== '' && isFinite(Number(n))) return Number(n).toLocaleString('zh-CN');
    return '—';
  }

  function fmtPct(v, digits) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    var d = digits == null ? 1 : digits;
    return (v * 100).toFixed(d) + '%';
  }

  function fmtTime(s) {
    if (!s) return '—';
    return String(s).replace('T', ' ').replace(/(\+00:00|Z)$/, '').slice(0, 19);
  }

  function fmtBytes(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '—';
    var u = ['B', 'KB', 'MB', 'GB'], i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
  }

  /* ── 主题 ────────────────────────────────────────────────── */

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }

  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* 隐私模式忽略 */ }
    var btn = $('lg-theme-btn');
    if (btn) btn.textContent = t === 'dark' ? '☀ 浅色' : '☾ 深色';
  }

  function toggleTheme() {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  }

  /* ── 导航 ────────────────────────────────────────────────── */

  function currentPage() {
    var p = location.pathname.split('/').pop() || '';
    return p === '' ? 'overview.html' : p;
  }

  function buildNav() {
    var page = currentPage();
    var shell = document.querySelector('.lg-shell') || document.body;
    var nav = document.createElement('nav');
    nav.className = 'lg-nav';
    nav.setAttribute('aria-label', '研究台模块导航');

    var html = '' +
      '<div class="lg-brand">' +
        '<div class="t"><span class="mark"></span>中文文风研究台</div>' +
        '<div class="s">Language Genome</div>' +
      '</div>' +
      '<div class="lg-nav-list">';
    MODULES.forEach(function (m, i) {
      if (i === 4 || i === 12) html += '</div><div class="lg-nav-sep"></div><div class="lg-nav-list">';
      html += '<a class="lg-nav-item' + (m.page === page ? ' on' : '') + '" href="/lab/' + m.page + '"' +
              (m.page === page ? ' aria-current="page"' : '') + '>' +
              '<span class="zh">' + esc(m.zh) + '</span>' +
              '<span class="en">' + esc(m.en) + '</span></a>';
    });
    html += '</div>' +
      '<div class="lg-nav-foot">' +
        '<button type="button" class="tg" id="lg-theme-btn" title="切换深浅主题">' + (currentTheme() === 'dark' ? '☀ 浅色' : '☾ 深色') + '</button>' +
        '<a class="lg-back" href="/" title="回到盲评台（第一界面，判题写入口）">盲评台 ↗</a>' +
      '</div>';
    nav.innerHTML = html;

    if (shell === document.body) document.body.insertBefore(nav, document.body.firstChild);
    else shell.insertBefore(nav, shell.firstChild);

    var btn = $('lg-theme-btn');
    if (btn) btn.addEventListener('click', toggleTheme);
  }

  /* ── 渲染件 ──────────────────────────────────────────────── */

  /* 指标卡：items = [{value,label,hint,tone}] */
  function statCards(el, items) {
    if (!el) return;
    var list = (Array.isArray(items) ? items : []).filter(Boolean);
    el.innerHTML = '';
    if (!list.length) { el.innerHTML = '<div class="stat-card"><div class="value">—</div><div class="label">暂无指标</div></div>'; return; }
    var frag = document.createDocumentFragment();
    list.forEach(function (it) {
      var d = document.createElement('div');
      d.className = 'stat-card';
      var v = it.value === null || it.value === undefined || it.value === '' ? '—' : it.value;
      var isNum = (typeof v === 'number');
      // 文本型读数走小字号样式，避免长模型名被 23px 等宽硬断成碎片
      var cls = 'value' + (isNum ? '' : ' text') + (it.tone ? ' ' + it.tone : '');
      var shown = (typeof v === 'number') ? fmtNum(v) : esc(v);
      d.innerHTML = '<div class="' + cls + '">' + shown + '</div>' +
                    '<div class="label">' + esc(it.label || '') + '</div>' +
                    (it.hint ? '<div class="hint">' + esc(it.hint) + '</div>' : '');
      frag.appendChild(d);
    });
    el.appendChild(frag);
  }

  /* 分布条：dict = {key: count} */
  function bars(el, dict, opts) {
    if (!el) return;
    opts = opts || {};
    var entries = Object.keys(dict || {}).map(function (k) { return [k, dict[k]]; })
      .sort(function (a, b) { return (b[1] || 0) - (a[1] || 0); });
    if (opts.limit) entries = entries.slice(0, opts.limit);
    el.innerHTML = '';
    if (!entries.length) { el.innerHTML = '<div class="empty">暂无分布数据</div>'; return; }
    var max = entries.reduce(function (m, e) { return Math.max(m, Number(e[1]) || 0); }, 0) || 1;
    var frag = document.createDocumentFragment();
    entries.forEach(function (e) {
      var pct = Math.max(2, Math.round((Number(e[1]) || 0) / max * 100));
      var row = document.createElement('div');
      row.className = 'bar-row';
      row.innerHTML = '<div class="k" title="' + esc(e[0]) + '">' + esc(e[0] === '' || e[0] === 'null' ? '（空）' : e[0]) + '</div>' +
                      '<div class="track"><div class="fill" style="width:' + pct + '%"></div></div>' +
                      '<div class="v">' + fmtNum(e[1]) + '</div>';
      frag.appendChild(row);
    });
    el.appendChild(frag);
  }

  /* 表格：cols = [{key,label,num,fmt,render}]，render(row) 返回 HTML 字符串 */
  function table(tbodyEl, rows, cols, opts) {
    if (!tbodyEl) return;
    var thead = opts && opts.head ? opts.head : null;
    var list = Array.isArray(rows) ? rows.slice() : [];

    if (opts && opts.sortKey) {
      var k = opts.sortKey, dir = opts.sortDir === 'asc' ? 1 : -1;
      list.sort(function (a, b) {
        var x = a[k], y = b[k];
        if (typeof x === 'number' && typeof y === 'number') return (x - y) * dir;
        return String(x == null ? '' : x).localeCompare(String(y == null ? '' : y)) * dir;
      });
    }

    if (thead) {
      thead.innerHTML = '<tr>' + cols.map(function (c) {
        return '<th' + (c.num ? ' class="num"' : '') + '>' + esc(c.label) + '</th>';
      }).join('') + '</tr>';
    }

    tbodyEl.innerHTML = '';
    if (!list.length) {
      tbodyEl.innerHTML = '<tr><td class="empty" colspan="' + cols.length + '">暂无数据</td></tr>';
      return;
    }
    var frag = document.createDocumentFragment();
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      tr.innerHTML = cols.map(function (c) {
        var html;
        if (typeof c.render === 'function') html = c.render(r);
        else {
          var v = r[c.key];
          if (c.fmt === 'pct') html = fmtPct(v);
          else if (c.fmt === 'time') html = esc(fmtTime(v));
          else if (c.fmt === 'bytes') html = esc(fmtBytes(v));
          else if (typeof v === 'number') html = fmtNum(v);
          else html = esc(v == null || v === '' ? '—' : v);
        }
        var cls = [];
        if (c.num) cls.push('num');
        if (c.cls) cls.push(c.cls);
        return '<td' + (cls.length ? ' class="' + cls.join(' ') + '"' : '') + '>' + html + '</td>';
      }).join('');
      if (typeof opts?.onRow === 'function') {   // 可选行点击（只读展开）
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', function () { opts.onRow(r, tr); });
      }
      frag.appendChild(tr);
    });
    tbodyEl.appendChild(frag);
  }

  /* 徽章：按关键字猜语义色（状态/成功/失败/告警） */
  var BAD_OK = /^(ok|done|pass|passed|success|succeeded|完成|通过|已验证|verified|active)$/i;
  var BAD_BAD = /^(fail|failed|error|bad|reject|rejected|失败|错误|否决)$/i;
  var BAD_WARN = /^(pending|queued|running|partial|warn|warning|待|运行中|排队|部分)$/i;

  function badge(v, tone) {
    var s = String(v == null ? '' : v);
    var t = tone || (BAD_OK.test(s) ? 'ok' : BAD_BAD.test(s) ? 'bad' : BAD_WARN.test(s) ? 'warn' : '');
    return '<span class="badge' + (t ? ' ' + t : '') + '">' + esc(s) + '</span>';
  }

  function emptyRow(tbodyEl, cols, msg) {
    if (tbodyEl) tbodyEl.innerHTML = '<tr><td class="empty" colspan="' + cols + '">' + esc(msg || '暂无数据') + '</td></tr>';
  }

  /* ── 数据加载：fetch + 解信封 + 统一错误态 ──────────────── */

  function bannerEl() { return $('banner'); }

  function showBanner(msg, isErr) {
    var b = bannerEl();
    if (!b) return;
    b.textContent = msg || '数据接口不可用';
    b.className = 'banner show' + (isErr ? ' err' : '');
  }

  function hideBanner() {
    var b = bannerEl();
    if (b) b.className = 'banner';
  }

  /* fetchModule(slug) -> Promise<data>；解 {module,title,data} 信封 */
  function fetchModule(slug) {
    return fetch('/console/' + slug, { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (res.status === 401) throw new Error('令牌已失效或缺失 —— 请回首页重新用 ?t= 链接进入');
        if (!res.ok) throw new Error('接口返回 HTTP ' + res.status);
        var ct = res.headers.get('content-type') || '';
        if (ct.indexOf('application/json') === -1) throw new Error('接口未返回 JSON（可能被访问门拦截）');
        return res.json();
      })
      .then(function (env) {
        if (env && typeof env === 'object' && 'data' in env) return env.data;
        return env;   // 容错：无信封时按裸数据用
      });
  }

  /* ── 页面引导 ────────────────────────────────────────────── */

  function init(slug, renderer, opts) {
    opts = opts || {};
    document.documentElement.setAttribute('data-page', slug);
    buildNav();

    var foot = document.querySelector('footer.lg-foot');
    if (foot && opts.footNote !== false) {
      foot.innerHTML = '数据来源：<span class="num">/console/' + esc(slug) + '</span>';
    }

    var stats = $('stats'), tbody = $('tbody');
    if (stats) stats.innerHTML = '<div class="skel skel-card"></div><div class="skel skel-card"></div><div class="skel skel-card"></div>';
    if (tbody) tbody.innerHTML = '<tr><td class="empty" colspan="8">加载中…</td></tr>';

    fetchModule(slug)
      .then(function (data) {
        hideBanner();
        try {
          renderer(data, {
            stats: stats, tbody: tbody, head: $('thead') || $('headRow'),
            genAt: $('genAt') || $('generatedAt') || $('ts') || $('meta')
          });
        } catch (err) {
          showBanner('渲染出错：' + (err && err.message ? err.message : err), true);
          if (window.console) console.error(err);
        }
      })
      .catch(function (err) {
        showBanner('数据接口不可用：' + (err && err.message ? err.message : err), true);
        if (stats) statCards(stats, []);
        if (tbody) emptyRow(tbody, 8, '数据不可用');
      });
  }

  return {
    MODULES: MODULES,
    init: init, fetchModule: fetchModule,
    statCards: statCards, bars: bars, table: table, badge: badge, emptyRow: emptyRow,
    showBanner: showBanner, hideBanner: hideBanner,
    esc: esc, fmtNum: fmtNum, fmtPct: fmtPct, fmtTime: fmtTime, fmtBytes: fmtBytes,
    applyTheme: applyTheme, toggleTheme: toggleTheme, currentTheme: currentTheme,
    $: $
  };
})();
