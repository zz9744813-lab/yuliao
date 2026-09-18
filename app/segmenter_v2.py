"""切分器 v2（Phase 1.5 P0）：修复 v1 的仪器伪影来源。

v1 的问题（Phase 1 实测）：跨段打包导致引语切断（1,456 段孤悬闭引号开场）、
章节标题黏连、连接词/裸代词开场。合格率仅 58%。

v2 规则（按优先级）：
  0. 预清洗：剥离章节标题行（第X章/回/节 独行）、场景分隔线（＊/—/===）转硬边界
  1. 段落是原子单位：默认一段一个 Segment（长段按句切，短段不强并）
  2. 引号闭合驱动合并：段末有未闭合引号 → 必须并入下一段直到闭合（跨段对话是作者意图）
  3. 回吸：本段以孤悬闭引号/连接词/裸代词开场 且 上一产物 < 2 句 → 并回上一产物
  4. 长段内部切分：> max_chars 时按句打包，但避免以闭引号开场
  5. 悬尾禁止：产物必须以终止标点收尾，否则并入下一句

不做的事：不为合格率把段落无限合并——天然依赖上下文的段打 context_required 标，
由评审协议供上下文，而不是扭曲切分。
"""
from __future__ import annotations

import re

from .metrics_det import _sentences
from .segment_integrity import _CLOSERS, _OPENERS

SEGMENTER_VERSION = 2
DEFAULTS2 = dict(min_chars=40, max_chars=300, target_sentences=4, max_sentences=10)

_CHAPTER_RE = re.compile(r"^\s*第[一二三四五六七八九十百千两零0-9]+[章回节卷][^\n]{0,30}$")
_NOISE_RE = re.compile(r"TXT小说天堂|xiaoshuotxt|www\.|http://|https://|更新最快|最新章节")
_SCENE_BREAK_RE = re.compile(r"^[\s＊*\-—=＝·⋯…]{3,}$")
_TERMINAL = "。！？…”」』"

_RISK_START = re.compile(r"^(」|』|”|’|但|可是|然而|而|于是|接着|随即|顿时|话音未落|闻言|见状|说罢|半晌|片刻后|这时|此时|他|她|它|两人|二人|那人|此人|对方)")


def _ends_inside_quote(text: str) -> bool:
    """栈式扫描：文本末尾是否仍处于未闭合的引号内。
    （净配平不够用：段内一开一闭会把末尾的悬空开引号抵消掉）"""
    stack: list[str] = []
    for ch in text:
        if ch in _OPENERS:
            stack.append(ch)
        elif ch in _CLOSERS and stack:
            stack.pop()
    return bool(stack)


def _quote_stack(text: str) -> list[str]:
    stack: list[str] = []
    for ch in text:
        if ch in _OPENERS:
            stack.append(ch)
        elif ch in _CLOSERS and stack:
            stack.pop()
    return stack


# 规则 2 的保险丝：源文本可能存在永不闭合的引号（错排/缺字），
# 不设上限会把整章吞成一个巨型 buffer，打包阶段退化为 O(n²)。
_MERGE_MAX_PARAS = 8
_MERGE_MAX_CHARS = 1200


def _preprocess(text: str) -> tuple[list[str], list[bool]]:
    """返回 (段落列表, 硬边界标记列表)；边界标记[i]=第 i 段之前是否有场景分隔。"""
    paras, hard = [], []
    pending_hard = True   # 文首视作硬边界
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _NOISE_RE.search(line):
            continue
        if _CHAPTER_RE.match(line) or _SCENE_BREAK_RE.match(line):
            pending_hard = True     # 下一个真实段落之前是硬边界
            continue
        paras.append(line)
        hard.append(pending_hard)
        pending_hard = False
    return paras, hard


def make_segments_v2(text: str, **kw) -> list[str]:
    cfg = {**DEFAULTS2, **kw}
    paras, hard_flags = _preprocess(text)
    units: list[tuple[str, bool]] = []   # (text, starts_hard/scene_start)

    i = 0
    while i < len(paras):
        buf = paras[i]
        buf_hard = hard_flags[i]
        # 规则 2：段末仍处于引号内 → 吞下一段，直到闭合或结尾（跨段对话是作者意图）。
        # 保险丝：最多并 8 段 / 1200 字，防止源文本引号错排导致吞章。
        merged_n = 0
        while _ends_inside_quote(buf) and i + 1 < len(paras) \
                and merged_n < _MERGE_MAX_PARAS and len(buf) < _MERGE_MAX_CHARS:
            i += 1
            buf += "\n" + paras[i]
            merged_n += 1
        # 规则 5：悬尾（无终止标点）→ 吞下一句/下一段
        while not buf.rstrip().endswith(tuple(_TERMINAL)) and i + 1 < len(paras):
            i += 1
            buf += "\n" + paras[i]
        units.append((buf, buf_hard))
        i += 1

    # 规则 1+3：产物成型 + 回吸
    segs: list[tuple[str, bool]] = []
    for buf, buf_hard in units:
        if len(buf) <= cfg["max_chars"] and _count_sents(buf) <= cfg["max_sentences"]:
            segs.append((buf, buf_hard))
            continue
        # 长段：按句打包（段内进行；不得切在引号内）。
        # 增量栈：预先算出每个句尾的引号深度，深度 0 处才允许断开，整体 O(n)。
        sents = _sentences(buf)
        depth = []
        st: list[str] = []
        for sent in sents:
            for ch in sent:
                if ch in _OPENERS:
                    st.append(ch)
                elif ch in _CLOSERS and st:
                    st.pop()
            depth.append(len(st))
        cur, cur_n = "", 0
        for idx, sent in enumerate(sents):
            if cur and (len(cur) + len(sent) > cfg["max_chars"]
                        or cur_n >= cfg["target_sentences"]
                        or len(cur) >= cfg["min_chars"] and cur_n >= 2) \
                    and depth[idx - 1] == 0:
                segs.append((cur, buf_hard))
                cur, cur_n = "", 0
                buf_hard = False  # 只有第一片保留场景边界
            cur += sent
            cur_n += 1
        if cur:
            # 尾片太短并回前片
            if segs and len(cur) < cfg["min_chars"] and len(segs[-1][0]) + len(cur) <= cfg["max_chars"]:
                prev, h = segs.pop()
                segs.append((prev + cur, h))
            else:
                segs.append((cur, buf_hard))

    # 规则 3：回吸——孤悬闭引号/连接词/裸代词开场且上一产物很短
    merged: list[tuple[str, bool]] = []
    for text_i, (buf, buf_hard) in enumerate(segs):
        if (merged and not buf_hard and len(buf) < cfg["min_chars"] * 1.5
                and _RISK_START.match(buf.lstrip("“「『'"))):
            prev, h = merged.pop()
            if len(prev) + len(buf) <= cfg["max_chars"] * 1.4:
                merged.append((prev + "\n" + buf, h))
                continue
            merged.append((prev, h))
        merged.append((buf, buf_hard))

    return [t for t, _ in merged]


def _count_sents(text: str) -> int:
    return len([s for s in _sentences(text) if s.strip()])
