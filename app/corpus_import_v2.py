"""《覆汉》入库 caveat 硬化（独立核查席 IMPORT_CHECK.md 三条实锤，opt-in）。

1. 卷首元数据（书名行 / `作者：X` / `内容简介：` 起头的头块）不作为正文段入库：
   切分前剥离，原文逐行记账进 `Work.anchors["front_matter"]`（不静默丢弃），
   解析到的作者回填 `Work.author`（仅当原值为空）；
2. 末段截断要标记而非静默：全篇最后一段段尾无句读 ⇒ 该段 integrity JSON 加
   `"truncated": true` 显式标记——复用既有 JSON 字段体系，**不加列**（建表史
   NOT NULL 无默认的老坑），下游按键取值不受多余键影响；同时记入
   `anchors["last_truncated"]`；
3. chapter 回填：复用 `segmenter_v2._CHAPTER_RE`（不重写标题正则），按与
   `_preprocess` 相同的行扫描口径在剥离标题行前追踪「当前章节」，再用
   「段首行是对应段落行的子串」把 v2 段前向单指针回对到段落；解析不到留 None。

所有新行为只在显式开关下生效（`import_work(..., caveats=True)` / CLI `--caveats`）；
默认导入路径逐字不变（既有 tests/test_import_corpus_v2_resume.py 全绿即证）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import segmenter_v2
from .segmenter_v2 import _CHAPTER_RE, _NOISE_RE, _SCENE_BREAK_RE, _TERMINAL

# 头部块各行的识别口径（只在文件开头、遇到第一段正文前生效）
_AUTHOR_RE = re.compile(r"^作\s*者[:：]\s*(.*)$")
_INTRO_RE = re.compile(r"^(?:内容|作品|书籍)?(?:简[介]|介绍|提要)\s*[:：]?\s*(.*)$")
_LABEL_RE = re.compile(
    r"^(?:书名|作品名|标签|关键词|关键字|分类|类型|状态|连载状态|更新时间|字数)\s*[:：]\s*(.*)$")
_TITLE_LINE_MAX = 60      # 裸书名行长度上限，防把正文短句当书名
_INTRO_MAX_LINES = 20     # 内容简介头块消费行数上限（有界，防吞正文）
_INTRO_LINE_MAX = 40      # 内容简介续行长度上限：≥此长视作正文（正文段按切分器
                          # min_chars=40 口径起步；简介/引子都是短行）
_CHAPTER_MAX = 200        # Segment.chapter 列宽 String(200)


def _classify_meta(s: str, title: str | None, allow_title: bool) -> str | None:
    """一行（已 strip）像不像卷首元数据；返回种类或 None。"""
    if not s:
        return None
    if _AUTHOR_RE.match(s):
        return "author"
    if _INTRO_RE.match(s):
        return "intro"
    if _LABEL_RE.match(s):
        return "label"
    if allow_title and title and len(s) <= _TITLE_LINE_MAX \
            and s.strip("《》").strip() == title.strip():
        return "title"
    return None


def strip_front_matter(text: str, *, title: str | None = None) -> tuple[str, dict]:
    """剥离卷首元数据头块，返回 (正文文本, 记账 dict)。

    记账 dict 的 `lines` 保存被剥离的**原始行**（逐字），author/book_title/intro/
    labels 为结构化摘要；文件不含元数据头时原样返回 (text, {})——零改动。
    停止条件：第一个不像元数据且非空行的正文行；`内容简介` 头块至多再吃
    `_INTRO_MAX_LINES` 行、遇空行/章节标题行/超长行（≥`_INTRO_LINE_MAX` 字，
    视作正文）即止（有界，防吞正文）。"""
    lines = text.replace("\r\n", "\n").split("\n")
    meta: dict = {"lines": [], "author": None, "book_title": None,
                  "intro": [], "labels": []}
    i = 0
    consumed_any = False
    in_intro = False
    intro_left = _INTRO_MAX_LINES
    while i < len(lines):
        s = lines[i].strip()
        if in_intro:
            stop = (not s) or _CHAPTER_RE.match(s) or intro_left <= 0 \
                or len(s) >= _INTRO_LINE_MAX \
                or _classify_meta(s, title, False) is not None
            if not stop:
                meta["intro"].append(s)
                meta["lines"].append(lines[i])
                intro_left -= 1
                i += 1
                continue
            in_intro = False   # 交回主循环按元数据/正文/标题行重新判定
            if not s:
                continue       # 空行不入账；正文从主循环 break 处原样保留
        kind = _classify_meta(s, title, allow_title=not consumed_any)
        if not s:
            # 空行只有「下一非空行仍是元数据」时才算头块内部排版被跳过（空白不入账）
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            nxt = lines[j].strip() if j < len(lines) else ""
            if j < len(lines) and _classify_meta(nxt, title, allow_title=not consumed_any):
                i = j
                continue
            break
        if kind is None or _CHAPTER_RE.match(s):
            break
        if kind == "author":
            val = _AUTHOR_RE.match(s).group(1).strip()
            meta["author"] = meta["author"] or val or None
        elif kind == "title":
            meta["book_title"] = s
        elif kind == "label":
            meta["labels"].append(s)
        elif kind == "intro":
            rest = _INTRO_RE.match(s).group(1).strip()
            if rest:
                meta["intro"].append(rest)
                intro_left -= 1
            in_intro = True
        meta["lines"].append(lines[i])
        consumed_any = True
        i += 1
    if not consumed_any:
        return text, {}
    return "\n".join(lines[i:]), meta


def paras_with_chapter(text: str) -> list[tuple[str, str | None]]:
    """行序列上与 `segmenter_v2._preprocess` 同口径的段落列表，附「所属章节标题行」。

    复用 `_CHAPTER_RE`/`_NOISE_RE`/`_SCENE_BREAK_RE`：噪声/场景线同样跳过，
    章节标题行不产段但更新当前章——这是 chapter 回填的唯一真相源。"""
    out: list[tuple[str, str | None]] = []
    chapter: str | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or _NOISE_RE.search(line):
            continue
        if _CHAPTER_RE.match(line):
            chapter = line[:_CHAPTER_MAX]
            continue
        if _SCENE_BREAK_RE.match(line):
            continue
        out.append((line, chapter))
    return out


def map_chapters(chunks: list[str], paras: list[tuple[str, str | None]]) -> list[str | None]:
    """把 v2 段回对到段落并取章节：前向单指针 + 「段首行 ⊂ 段落行」判定。

    v2 段要么以完整段落行开头（合并/回吸用 \\n 拼接），要么是长段的按句切片
    （`_sentences` 为精确子串切分），两种情形段首行都是对应段落行的子串。
    匹配不到就留 None——宁缺毋滥，不猜。"""
    out: list[str | None] = []
    p = 0
    for seg in chunks:
        probe = seg.split("\n", 1)[0].strip()
        found: int | None = None
        if probe:
            for j in range(p, len(paras)):
                if probe in paras[j][0]:
                    found = j
                    break
            if found is None and len(probe) > 24:   # 切片首句过长时退化为前缀判定
                head = probe[:24]
                for j in range(p, len(paras)):
                    if head in paras[j][0]:
                        found = j
                        break
        if found is None:
            out.append(None)
            continue
        p = found          # 同段多切片：指针停在当前段，后续切片继续命中它
        out.append(paras[found][1])
    return out


def is_truncated_tail(chunks: list[str]) -> bool:
    """全篇最后一段段尾无句读（复用切分器的终止标点口径 `_TERMINAL`）。"""
    return bool(chunks) and not chunks[-1].rstrip().endswith(tuple(_TERMINAL))


@dataclass
class Prepared:
    chunks: list[str]
    chapters: list[str | None]
    front_matter: dict = field(default_factory=dict)
    last_truncated: bool = False


def prepare_import(text: str, *, title: str | None = None) -> Prepared:
    """caveats 口径的「切分 + 三条修复」产物（纯函数，不碰库）。"""
    body, fm = strip_front_matter(text, title=title)
    chunks = segmenter_v2.make_segments_v2(body)
    chapters = map_chapters(chunks, paras_with_chapter(body))
    return Prepared(chunks=chunks, chapters=chapters,
                    front_matter=fm, last_truncated=is_truncated_tail(chunks))
