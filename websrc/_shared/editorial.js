/* 研究台交互层：目录检索、数据重读和阅读状态。只消费既有只读接口。 */
(function () {
  'use strict';
  var originalInit = LG.init;
  var returnFocus = null;
  var matches = [];
  var cursor = 0;

  function esc(s) { return LG.esc(s); }
  function byId(id) { return document.getElementById(id); }
  function two(n) { return String(n).padStart(2, '0'); }

  function enhanceNav() {
    var nav = document.querySelector('.lg-nav');
    if (!nav || nav.querySelector('.lg-nav-rail')) return;
    var brand = nav.querySelector('.lg-brand');
    brand.innerHTML = '<div class="lg-brand-top"><span class="mark" aria-hidden="true">言</span><div class="t">语言基因组</div></div>' +
      '<div class="s">LANGUAGE GENOME / ARCHIVE</div>';
    var rail = document.createElement('div');
    rail.className = 'lg-nav-rail';
    nav.insertBefore(rail, nav.querySelector('.lg-nav-list'));
    nav.querySelectorAll('.lg-nav-list,.lg-nav-sep').forEach(function (node) { rail.appendChild(node); });
    var navLists = rail.querySelectorAll('.lg-nav-list');
    ['01 / 观察与材料', '02 / 判断与校准', '03 / 系统与交接'].forEach(function (label, i) {
      var target = navLists[i];
      if (!target) return;
      var section = document.createElement('div');
      section.className = 'lg-nav-section';
      section.textContent = label;
      target.prepend(section);
    });
    nav.querySelectorAll('.lg-nav-item').forEach(function (link, i) {
      var index = document.createElement('span');
      index.className = 'idx';
      index.textContent = two(i + 1);
      link.prepend(index);
    });
    var search = document.createElement('button');
    search.type = 'button';
    search.id = 'lg-search-btn';
    search.className = 'lg-nav-search';
    search.setAttribute('aria-label', '打开目录检索');
    search.innerHTML = '<span class="glyph" aria-hidden="true">⌕</span><span class="label">检索研究目录</span><kbd>Ctrl K</kbd>';
    nav.insertBefore(search, rail);
    search.addEventListener('click', openSearch);
    buildSearch();
  }

  function buildSearch() {
    var backdrop = document.createElement('div');
    backdrop.className = 'lg-search-backdrop';
    backdrop.id = 'lg-search';
    backdrop.innerHTML = '<div class="lg-search-panel" role="dialog" aria-modal="true" aria-label="检索研究目录">' +
      '<div class="lg-search-heading"><span>LANGUAGE GENOME / INDEX</span><button type="button" id="lg-search-close" aria-label="关闭目录">ESC</button></div>' +
      '<input class="lg-search-input" id="lg-search-input" type="search" autocomplete="off" placeholder="输入模块名称或英文关键词" aria-label="搜索模块">' +
      '<div class="lg-search-results" id="lg-search-results" role="listbox" aria-label="匹配模块"></div>' +
      '<div class="lg-search-help"><kbd>↑</kbd><kbd>↓</kbd> 选择　<kbd>Enter</kbd> 打开　<kbd>Esc</kbd> 关闭</div></div>';
    document.body.appendChild(backdrop);
    backdrop.addEventListener('mousedown', function (e) { if (e.target === backdrop) closeSearch(); });
    backdrop.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var focusable = Array.from(backdrop.querySelectorAll('button,input'));
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    byId('lg-search-close').addEventListener('click', closeSearch);
    byId('lg-search-input').addEventListener('input', function () { renderSearch(this.value); });
    byId('lg-search-input').addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!matches.length) return;
        cursor = (cursor + (e.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
        highlightSearch();
      } else if (e.key === 'Enter' && matches.length) {
        e.preventDefault();
        location.href = '/lab/' + matches[cursor].page;
      } else if (e.key === 'Escape') closeSearch();
    });
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        backdrop.classList.contains('open') ? closeSearch() : openSearch();
      } else if (e.key === 'Escape' && backdrop.classList.contains('open')) closeSearch();
    });
  }

  function renderSearch(query) {
    var q = String(query || '').trim().toLowerCase();
    matches = LG.MODULES.filter(function (m) {
      return !q || (m.zh + ' ' + m.en + ' ' + m.slug).toLowerCase().includes(q);
    });
    cursor = 0;
    var list = byId('lg-search-results');
    list.innerHTML = matches.length ? matches.map(function (m) {
      var i = LG.MODULES.indexOf(m);
      return '<button type="button" class="lg-search-result" data-result="' + i + '" role="option">' +
        '<span class="no">' + two(i + 1) + '</span><span class="name">' + esc(m.zh) +
        '</span><span class="en">' + esc(m.en) + '</span></button>';
    }).join('') : '<div class="lg-search-empty">没有匹配的目录。试试「语料」「实验」或「模型」。</div>';
    list.querySelectorAll('[data-result]').forEach(function (button) {
      button.addEventListener('click', function () { location.href = '/lab/' + LG.MODULES[Number(button.dataset.result)].page; });
    });
    highlightSearch();
  }

  function highlightSearch() {
    byId('lg-search-results').querySelectorAll('[data-result]').forEach(function (button, i) {
      button.classList.toggle('active', i === cursor);
      button.setAttribute('aria-selected', i === cursor ? 'true' : 'false');
      if (i === cursor) button.scrollIntoView({ block: 'nearest' });
    });
  }

  function openSearch() {
    returnFocus = document.activeElement;
    byId('lg-search').classList.add('open');
    document.body.style.overflow = 'hidden';
    byId('lg-search-input').value = '';
    renderSearch('');
    byId('lg-search-input').focus();
  }

  function closeSearch() {
    byId('lg-search').classList.remove('open');
    document.body.style.overflow = '';
    if (returnFocus && returnFocus.focus) returnFocus.focus();
  }

  function dataElements() {
    return {
      stats: byId('stats'), tbody: byId('tbody'), head: byId('thead') || byId('headRow'),
      genAt: byId('genAt') || byId('generatedAt') || byId('ts') || byId('meta')
    };
  }

  function enhancePage(slug, renderer) {
    var header = document.querySelector('.lg-head');
    if (!header) return;
    var index = LG.MODULES.findIndex(function (m) { return m.slug === slug; });
    var kicker = document.createElement('div');
    kicker.className = 'lg-kicker';
    // 目录里没有该 slug（findIndex == -1）时不渲染伪序号「00」，改用显式可见的「--」哨兵
    kicker.textContent = 'RESEARCH ARCHIVE  /  ' + (index >= 0 ? two(index + 1) : '--');
    header.prepend(kicker);
    var actions = document.createElement('div');
    actions.className = 'lg-head-actions';
    actions.innerHTML = '<button type="button" class="lg-refresh" id="lg-refresh-btn">重新读取数据 ↻</button>' +
      '<span class="lg-sync" id="lg-sync" role="status" aria-live="polite">读取中…</span>';
    header.appendChild(actions);
    var busy = false;
    function refresh() {
      if (busy) return;
      busy = true;
      byId('lg-refresh-btn').disabled = true;
      byId('lg-sync').textContent = '读取中…';
      LG.fetchModule(slug).then(function (data) {
        renderer(data, dataElements());
        LG.hideBanner();
        byId('lg-sync').textContent = '读取于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
      }).catch(function (err) {
        byId('lg-sync').textContent = '读取失败 · 上次数据保留';
        LG.showBanner('读取失败：' + (err && err.message ? err.message : err), true);
        var banner = byId('banner');
        // 去重：同一个 banner 上最多一个「重新读取」按钮，多次失败不得堆叠
        if (banner && !banner.querySelector('.lg-banner-retry')) {
          var retry = document.createElement('button');
          retry.type = 'button';
          retry.className = 'lg-banner-retry';
          retry.textContent = '重新读取';
          retry.addEventListener('click', refresh);
          banner.appendChild(retry);
        }
      }).finally(function () { busy = false; byId('lg-refresh-btn').disabled = false; });
    }
    byId('lg-refresh-btn').addEventListener('click', refresh);
    var banner = byId('banner');
    if (banner) new MutationObserver(function () {
      if (banner.classList.contains('err') && byId('lg-sync').textContent === '读取中…')
        byId('lg-sync').textContent = '初次读取失败';
    }).observe(banner, { attributes: true, attributeFilter: ['class'] });
  }

  LG.init = function (slug, renderer, opts) {
    var wrapped = function (data, elements) {
      renderer(data, elements);
      var status = byId('lg-sync');
      if (status) status.textContent = '读取于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
    };
    originalInit(slug, wrapped, opts);
    enhanceNav();
    enhancePage(slug, renderer);
  };
})();
