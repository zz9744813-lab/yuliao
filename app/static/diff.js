/* diff.js — 极简字符级 diff（盲评 A/B 差异高亮用）
 *
 * 自写、零依赖。两件事：
 *   1. diffChars(a,b)  → LCS 动态规划，产出 {type:'equal'|'del'|'ins', value} 序列；
 *   2. highlightSpans(a,b) → 两侧各自的**绝对偏移高亮区间**：A 侧标「被改掉的」(del)，
 *      B 侧标「新写的」(ins)。带小空隙桥接（≤2 字的相同片段并入相邻改动），
 *      效果等价于 diff-match-patch 的 cleanupSemantic——否则中文逐字 diff 会满屏单字红绿。
 *
 * 为什么不用 CDN/npm：项目通过公网隧道暴露，审计明确要求不引外部依赖（2026-09-16）。
 *
 * 偏移契约（关键）：highlightSpans 返回的区间是相对各自原文的字符偏移，
 * 与批注 mark 的偏移体系（offsetOf / curRaw）同源；composeHtml 只渲染**内联**标签，
 * 剥掉标签后与原文逐字相等，因此划选批注的偏移计算完全不受影响。
 */
(function (global) {
  'use strict';

  var MAX_CELLS = 4000000; // DP 单元数封顶（约 2000×2000 字），超了退化为「整体不同」

  function isStr(x) { return typeof x === 'string'; }

  /** 字符级 LCS diff。返回 parts 数组：{type:'equal'|'del'|'ins', value}。 */
  function diffChars(a, b) {
    a = isStr(a) ? a : String(a == null ? '' : a);
    b = isStr(b) ? b : String(b == null ? '' : b);
    var n = a.length, m = b.length;
    if (n === 0) return m ? [{ type: 'ins', value: b }] : [];
    if (m === 0) return [{ type: 'del', value: a }];
    if (n * m > MAX_CELLS) {
      // 超长文本：LCS 代价太高，整体标为改动（两侧各自高亮全文）
      return [{ type: 'del', value: a }, { type: 'ins', value: b }];
    }

    // 反向填表：dp[i][j] = a[i:] 与 b[j:] 的 LCS 长度
    var dp = new Uint16Array((n + 1) * (m + 1));
    var at = function (i, j) { return i * (m + 1) + j; };
    for (var i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        if (a.charCodeAt(i) === b.charCodeAt(j)) dp[at(i, j)] = dp[at(i + 1, j + 1)] + 1;
        else dp[at(i, j)] = dp[at(i + 1, j)] > dp[at(i, j + 1)] ? dp[at(i + 1, j)] : dp[at(i, j + 1)];
      }
    }

    // 正向回溯，顺带把同类相邻片段合并成一段
    var parts = [];
    var i = 0, j = 0;
    var buf = '', bufType = null;
    var flush = function () {
      if (buf) { parts.push({ type: bufType, value: buf }); buf = ''; }
      bufType = null;
    };
    while (i < n && j < m) {
      if (a.charCodeAt(i) === b.charCodeAt(j)) {
        if (bufType !== 'equal') { flush(); bufType = 'equal'; }
        buf += a[i]; i++; j++;
      } else if (dp[at(i + 1, j)] >= dp[at(i, j + 1)]) {
        if (bufType !== 'del') { flush(); bufType = 'del'; }
        buf += a[i]; i++;
      } else {
        if (bufType !== 'ins') { flush(); bufType = 'ins'; }
        buf += b[j]; j++;
      }
    }
    while (i < n) { if (bufType !== 'del') { flush(); bufType = 'del'; } buf += a[i++]; }
    while (j < m) { if (bufType !== 'ins') { flush(); bufType = 'ins'; } buf += b[j++]; }
    flush();
    return parts;
  }

  /** 把相隔 ≤ gap 字的两段高亮并入一段（「改一字 · 隔一字 · 又改一字」→ 一处）。 */
  function mergeNear(spans, gap) {
    if (!spans || spans.length < 2) return spans || [];
    var out = [{ start: spans[0].start, end: spans[0].end, type: spans[0].type }];
    for (var i = 1; i < spans.length; i++) {
      var last = out[out.length - 1], cur = spans[i];
      if (cur.type === last.type && cur.start - last.end <= gap) {
        last.end = Math.max(last.end, cur.end);
      } else {
        out.push({ start: cur.start, end: cur.end, type: cur.type });
      }
    }
    return out;
  }

  /** 两侧高亮区间：A 侧 del（被改掉的）/ B 侧 ins（新写的）。 */
  function highlightSpans(a, b) {
    var parts = diffChars(a, b);
    var A = [], B = [];
    var pa = 0, pb = 0;
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (p.type === 'equal') { pa += p.value.length; pb += p.value.length; }
      else if (p.type === 'del') { A.push({ start: pa, end: pa + p.value.length, type: 'del' }); pa += p.value.length; }
      else { B.push({ start: pb, end: pb + p.value.length, type: 'ins' }); pb += p.value.length; }
    }
    return { A: mergeNear(A, 2), B: mergeNear(B, 2) };
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /** 把 diff 高亮与噪点批注 mark 复合渲染成一段 HTML。
   *  - 区间全部按原文偏移夹回并去重叠；
   *  - 只产出内联标签 <i class="dhl …"> 与 <mark class="kN">，剥标签后与原文逐字相等
   *    → 选区偏移（offsetOf / textContent 体系）不受影响，划选批注照常可用；
   *  - mark 的 data-mid / title 保留，点删委托不需要改。
   *  kinds 为批注类别表（MARK_KINDS），未知类别落 k0，与 buildMarkedHtml 一致。 */
  function composeHtml(text, list, spans, kinds) {
    text = String(text == null ? '' : text);
    kinds = kinds || [];
    var n = text.length;

    // 批注：与 buildMarkedHtml 同一套夹回 / 去重叠规则
    var ms = [], pos = 0;
    var sorted = (list || []).slice().sort(function (x, y) { return (x.start || 0) - (y.start || 0); });
    for (var si = 0; si < sorted.length; si++) {
      var m = sorted[si];
      var s = Math.max(0, Math.min(m.start || 0, n));
      var e = Math.max(s, Math.min(m.end || 0, n));
      if (e <= s || s < pos) continue;
      ms.push({ s: s, e: e, m: m });
      pos = e;
    }
    // diff 区间：构造时已保证不重叠且在界内，仍夹一道保险
    var sp = [];
    for (var di = 0; di < (spans || []).length; di++) {
      var x = spans[di];
      var xs = Math.max(0, Math.min(x.start || 0, n));
      var xe = Math.max(xs, Math.min(x.end || 0, n));
      if (xe > xs) sp.push({ s: xs, e: xe, type: x.type });
    }

    // 所有边界排序，逐段决定是否包在 diff 高亮 / mark 内
    var pts = [0, n];
    for (var mi = 0; mi < ms.length; mi++) { pts.push(ms[mi].s, ms[mi].e); }
    for (var pi = 0; pi < sp.length; pi++) { pts.push(sp[pi].s, sp[pi].e); }
    pts.sort(function (x, y) { return x - y; });
    var cuts = [];
    for (var ci = 0; ci < pts.length; ci++) {
      if (!cuts.length || pts[ci] !== cuts[cuts.length - 1]) cuts.push(pts[ci]);
    }

    var out = '';
    for (var k = 0; k < cuts.length - 1; k++) {
      var ca = cuts[k], cb = cuts[k + 1];
      if (cb <= ca) continue;
      var mk = null, sn = null;
      for (var mi2 = 0; mi2 < ms.length && !mk; mi2++) if (ms[mi2].s <= ca && ms[mi2].e >= cb) mk = ms[mi2];
      for (var pi2 = 0; pi2 < sp.length && !sn; pi2++) if (sp[pi2].s <= ca && sp[pi2].e >= cb) sn = sp[pi2];
      var o = '';
      if (sn) o += '<i class="dhl ' + sn.type + '">';
      if (mk) {
        var ki = Math.max(0, kinds.indexOf(mk.m.kind));
        o += '<mark class="k' + ki + '" data-mid="' + mk.m.id + '" title="'
           + escHtml(mk.m.kind) + '（点击删除）">';
      }
      o += escHtml(text.slice(ca, cb));
      if (mk) o += '</mark>';
      if (sn) o += '</i>';
      out += o;
    }
    return out;
  }

  global.DiffLite = { diffChars: diffChars, highlightSpans: highlightSpans, composeHtml: composeHtml };
  if (typeof module !== 'undefined' && module.exports) module.exports = global.DiffLite;
})(typeof window !== 'undefined' ? window : this);
