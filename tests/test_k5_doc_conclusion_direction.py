"""结论词方向门：把 docs/K5供给口径真计数_20260925.md 的结论词绑到它自己的数字。

2026-09-25 第二轮独立复核（仓库外 REVIEW_audit_residuals_round2_20260925.md）判 BLOCK，
唯一依据是：把该文档的结论词 `不成立` 手工改成 `成立`，本仓现有验收门 100% 全绿。
根因是结构性错位——`tests/test_k5_supply_recount.py` 钉的是**当场生成**的临时 markdown，
没有任何测试读取**已提交**的文档；复跑生成器还会覆写手改，形成「自愈式无红」。

本门零依赖、零真库、纯文本：只取 §2.1「口径 A」那一节的逐作品表，按生成器同一谓词复算
n_ge（≥10,000 段的作品数），达标条件 n_ge ≥ 2，再要求文档结论词与复算一致。
⇒ 只改结论词 → 复算不变而词不符 → 红；连数字一起改 → 表与结论不自洽 → 红。

陷阱（务必保留本注释所述约束）：文档里 §2.1/2.2/2.3/2.4 共四张表，口径 B/C 的最大单作品
154,674/154,809、≥10,000 段的作品有 6~7 个——四张表一起解析得到的复算结论恰好是「成立」，
本门会**反向失效**。故 §2.1 小节严格锚定、到下一个 ### 为止。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "K5供给口径真计数_20260925.md"

MIN_PER_WORK = 10_000   # 单作品段数下限
NEED_WORKS = 2          # 达标所需作品数
OK_WORD = "成立"
NOT_OK_WORD = "不成立"

SECTION_A = re.compile(r"^###\s*2\.1\s")  # 口径 A 小节标题
ROW = re.compile(r"^\|\s*(WK-[0-9A-Za-z]+)\s*\|[^|]*\|\s*([\d,]+)\s*\|")


def _doc_text() -> str:
    assert DOC.is_file(), f"K5 真计数文档不在树内：{DOC}（门不得因缺文档而静默放行）"
    return DOC.read_text(encoding="utf-8")


def _section_a(text: str) -> str:
    """只取 §2.1（口径 A）小节正文，到下一个 ### 为止。"""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if SECTION_A.match(ln)), None)
    assert start is not None, "文档里找不到 §2.1 口径 A 小节 ⇒ 本门失去解析对象"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("###")), len(lines))
    return "\n".join(lines[start:end])


def _parse_works(section: str) -> list[tuple[str, int]]:
    return [(m.group(1), int(m.group(2).replace(",", "")))
            for m in (ROW.match(ln) for ln in section.splitlines()) if m]


def _conclusion_words(text: str) -> set[str]:
    """文档里出现的结论词。`不成立` 含子串 `成立`，必须先剥掉再判 `成立`，
    否则「只查肯定词」会让正反两个方向都判绿。"""
    words = set()
    if NOT_OK_WORD in text:
        words.add(NOT_OK_WORD)
    if OK_WORD in text.replace(NOT_OK_WORD, ""):
        words.add(OK_WORD)
    return words


def _recompute(section: str) -> tuple[str, int, int]:
    """按生成器同一谓词复算，返回（应有结论词, 逐作品数, n_ge）。"""
    works = _parse_works(section)
    assert works, "§2.1 口径 A 表解析不到任何 WK- 行 ⇒ 本门失去解析对象"
    n_ge = sum(1 for _, n in works if n >= MIN_PER_WORK)
    return (OK_WORD if n_ge >= NEED_WORKS else NOT_OK_WORD), len(works), n_ge


def test_document_has_conclusion_word_and_parsable_section_a():
    text = _doc_text()
    assert _conclusion_words(text), f"文档里找不到任何结论词（{OK_WORD}/{NOT_OK_WORD}）"
    section = _section_a(text)
    assert "口径 A" in section.splitlines()[0], "§2.1 小节标题不再声明口径 A ⇒ 解析对象已漂移"
    works = _parse_works(section)
    assert works, "§2.1 口径 A 表解析不到任何 WK- 行"
    # 混解析四张表会让复算反向失效（口径 B/C 有 6~7 个 ≥10,000 段作品）⇒ 钉住解析范围
    assert max(n for _, n in works) < MIN_PER_WORK, (
        f"§2.1 里出现 >= {MIN_PER_WORK} 段的行 ⇒ 小节锚定失效、疑似混入口径 B/C 的表")


def test_conclusion_word_matches_the_numbers_in_the_document():
    """核心判据（独立覆盖）：结论词必须与文档自身 §2.1 表格的复算结果一致。"""
    text = _doc_text()
    expected, n_works, n_ge = _recompute(_section_a(text))
    words = _conclusion_words(text)
    assert expected in words, (
        "结论词与文档数字不符：§2.1 口径 A 复算 %d 部作品、其中 >=%d 段的 %d 部，"
        "故结论词应为 %r，但文档里的结论词是 %r ⇒ 结论被手工改动，或表格与结论不自洽"
        % (n_works, MIN_PER_WORK, n_ge, expected, sorted(words)))


def test_document_does_not_assert_both_directions():
    """防自相矛盾：同一份文档不得同时主张 `不成立` 与独立的 `成立`。"""
    words = _conclusion_words(_doc_text())
    assert not {OK_WORD, NOT_OK_WORD} <= words, (
        f"文档同时出现 {OK_WORD!r} 与 {NOT_OK_WORD!r} 两种结论词 ⇒ 结论自相矛盾")


def test_reverse_mutation_of_the_conclusion_word_is_caught():
    """反向自检（门「有方向」的机械证明）：在内存里把结论词翻转，核心判据必须转红。

    不落盘、不改仓库文件；断言的是核心判据本身（expected not in words），
    而不是另写一份近似逻辑。
    """
    text = _doc_text()
    assert NOT_OK_WORD in text, f"原文不含 {NOT_OK_WORD!r}，无法做翻转自检"
    mutated = text.replace(NOT_OK_WORD, OK_WORD)
    expected, n_works, n_ge = _recompute(_section_a(mutated))
    words = _conclusion_words(mutated)
    assert expected not in words, (
        "结论词翻转后本门仍判绿（复算应为 %r，文档词 %r）⇒ 门钉不住方向"
        % (expected, sorted(words)))
