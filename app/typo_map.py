"""字表（TYPO_MAP）—— 盗版 txt 系统性错字的单一事实源（T-CORPUS-V2）。

频次自洽确认（同一作品内正确写法压倒性多数，见 docs/typo-normalization-20260919.md
与 scripts/normalize_typos.py 的 --scan 证据）：

- 千雪 → 千仞雪（斗罗大陆：千仞雪 ×1060 vs 千雪 ×390；lookbehind 防"千仞雪"子串误伤）
- 吴天 → 昊天（斗罗大陆 20:1727、将夜 27:2760——两书盗版源同款错字，上下文已人工核验）
- 了天斗罗 → 昊天斗罗（斗罗大陆 1:48）

规则顺序：**最长/最特异的先应用**（了天斗罗 先于 吴天/千雪 无实际重叠，
但保持特异性优先的稳定顺序）。历史出处：scripts/source_check.py 的
KNOWN_TYPOS（频次自洽法的原始版本）。
"""
from __future__ import annotations

import re

# corpus v2 的标题标记（T-CORPUS-V2）：出现在 Work.title 里代表「TYPO_MAP 修复的
# 镜像版本」。权威键是 Work.v2_of（存 v1 Work.id），标题只是人类可读的兼容通道
# （回填前的历史行只有它）。判定统一走 app.models.is_corpus_v2_work /
# exclude_corpus_v2_segments，消费方：scripts/corpus_fix_v2（产出+回填）、
# app/near_dup（训练采样域 + 基准切分）、scripts/scale_corpus.pick、
# scripts/goldpick_build、scripts/export_training、scripts/benchmark_build。
V2_TITLE_SUFFIX = "（corpus v2）"

RULES: tuple[dict, ...] = (
    {"bad": "了天斗罗", "good": "昊天斗罗", "lookbehind": None},
    {"bad": "吴天", "good": "昊天", "lookbehind": None},
    {"bad": "千雪", "good": "千仞雪", "lookbehind": "仞"},
)


def hits(text: str | None) -> dict[str, int]:
    """按规则统计命中次数（lookbehind 生效），返回 {错字标签: 次数}（仅命中项）。"""
    out: dict[str, int] = {}
    if not text:
        return out
    for rule in RULES:
        pat = (f"(?<!{rule['lookbehind']}){re.escape(rule['bad'])}"
               if rule.get("lookbehind") else re.escape(rule["bad"]))
        n = len(re.findall(pat, text))
        if n:
            out[rule["bad"]] = n
    return out


def total(text: str | None) -> int:
    return sum(hits(text).values())


def apply(text: str | None) -> tuple[str, int]:
    """应用全部修复，返回（新文本, 替换次数）。原文由调用方保证可弃。"""
    if not text:
        return text or "", 0
    n = 0
    for rule in RULES:
        pat = (f"(?<!{rule['lookbehind']}){re.escape(rule['bad'])}"
               if rule.get("lookbehind") else re.escape(rule["bad"]))
        text, k = re.subn(pat, rule["good"], text)
        n += k
    return text, n
