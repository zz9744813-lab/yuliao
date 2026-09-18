"""确定性特征层：能算的先算，不花 token。口径与 novel-hub style/metrics 对齐并扩到本次需要的维度。

所有密度类指标统一为"每千字"，便于跨长度比较。
"""
from __future__ import annotations

import re
from collections import Counter
from statistics import mean, pstdev

try:
    import jieba  # type: ignore

    _JIEBA = True
except Exception:  # pragma: no cover
    jieba = None
    _JIEBA = False

SENT_SPLIT = re.compile(r"(?<=[。！？!?…；;])")
_DIALOGUE_RE = re.compile(r"[「」“”\"'‘’][^「」“”\"'‘’\n]{1,200}[「」“”\"'‘’]")
_PARA_SPLIT = re.compile(r"\n+")

# AI 高频修饰副词（校准清单，可由实验数据修订）
ADVERBS_FLAVOR = (
    "微微 缓缓 淡淡 轻轻 静静 默默 悄悄 渐渐 缓缓 不由 不禁 不由得 忍不住 "
    "似乎 仿佛 宛如 犹如 好像 蓦地 蓦然 陡然 骤然 倏然 赫然 暗自 暗暗 "
    "下意识 下意识 本能 不自觉 不知不觉 下意识"
).split()

# 显式心理标记
PSYCH_MARKERS = (
    "心想 暗想 心道 暗道 心中 心里 心底 心头 心知 心下 "
    "意识到 察觉到 感觉到 觉得 感到 想到 想起 明白 知道 懂得 "
    "思忖 寻思 忖道 琢磨 狐疑 纳闷 恍然 醒悟 顿悟 "
    "松了口气 舒了口气 倒吸一口凉气 屏住呼吸 提起心来"
).split()

# 直接情绪命名
EMOTION_WORDS = (
    "愤怒 悲伤 喜悦 恐惧 惊讶 痛苦 快乐 忧愁 焦虑 兴奋 愧疚 羞耻 嫉妒 憎恨 "
    "慌乱 激动 委屈 绝望 欣喜 不安 烦躁 感动 苦涩 狂喜 惊惧 悲恸 忐忑 "
    "心酸 欣慰 厌倦 迷茫 陶醉 怜惜 震怒 惊恐 哀伤 恼火 郁闷 彷徨 怅然"
).split()

# 逻辑连接词
CONNECTIVES = (
    "因为 所以 但是 然而 于是 然后 接着 因此 虽然 可是 不过 只是 随后 "
    "与此同时 紧接着 换句话说 换言之 事实上 实际上 毕竟 显然 果然 竟然"
).split()

# 语气/强势程度副词（确定性标记）
INTENSIFIERS = "很 太 极 极其 非常 十分 格外 分外 异常 颇 相当 特别 尤其".split()

_PUNCTS = "，。！？；：、…—“”\"''「」·"


def _sentences(text: str) -> list[str]:
    raw = SENT_SPLIT.split(text.strip())
    return [s.strip() for s in raw if s and s.strip()]


def _per_k(count: float, n_chars: int) -> float:
    return round(count / max(n_chars, 1) * 1000, 3)


def _lexicon_density(text: str, lexicon: list[str], n_chars: int) -> float:
    c = sum(text.count(w) for w in lexicon)
    return _per_k(c, n_chars)


def _ttr(text: str) -> float:
    if _JIEBA:
        words = [w for w in jieba.cut(text) if w.strip()]
        if not words:
            return 0.0
        return round(len(set(words)) / len(words), 4)
    bigrams = [text[i : i + 2] for i in range(len(text) - 1)]
    if not bigrams:
        return 0.0
    return round(len(set(bigrams)) / len(bigrams), 4)


def compute_metrics(text: str) -> dict[str, float]:
    text = text.strip()
    n_chars = len(text)
    sents = _sentences(text)
    paras = [p for p in _PARA_SPLIT.split(text) if p.strip()]
    sent_lens = [len(s) for s in sents] or [0]

    m: dict[str, float] = {
        "n_chars": float(n_chars),
        "n_sentences": float(len(sents)),
        "sent_len_mean": round(mean(sent_lens), 2),
        "sent_len_std": round(pstdev(sent_lens), 2) if len(sent_lens) > 1 else 0.0,
        "sent_len_max": float(max(sent_lens)),
        "sent_len_min": float(min(sent_lens)),
        "short_sent_ratio": round(sum(1 for L in sent_lens if L <= 8) / len(sent_lens), 4),
        "long_sent_ratio": round(sum(1 for L in sent_lens if L >= 40) / len(sent_lens), 4),
        "n_paragraphs": float(len(paras)),
        "para_len_mean": round(mean([len(p) for p in paras]) if paras else 0.0, 2),
    }

    # 标点密度（每千字）
    for ch in "，。！？；：、…":
        m[f"punct_{ch}_per_k"] = _per_k(text.count(ch), n_chars)
    m["comma_period_ratio"] = round(
        text.count("，") / max(text.count("。"), 1), 3
    )

    # 对话
    dlg = _DIALOGUE_RE.findall(text)
    dlg_chars = sum(len(d) for d in dlg)
    m["dialogue_turns"] = float(len(dlg))
    m["dialogue_ratio"] = round(dlg_chars / max(n_chars, 1), 4)

    # 词法密度
    m["adv_flavor_per_k"] = _lexicon_density(text, ADVERBS_FLAVOR, n_chars)
    m["psych_marker_per_k"] = _lexicon_density(text, PSYCH_MARKERS, n_chars)
    m["emotion_word_per_k"] = _lexicon_density(text, EMOTION_WORDS, n_chars)
    m["connective_per_k"] = _lexicon_density(text, CONNECTIVES, n_chars)
    m["intensifier_per_k"] = _lexicon_density(text, INTENSIFIERS, n_chars)
    m["de_per_k"] = _per_k(text.count("的"), n_chars)

    # 叠词（XX 重复，如"慢慢""怔怔"）
    redu = len(re.findall(r"([一-鿿])\1", text))
    m["reduplication_per_k"] = _per_k(redu, n_chars)

    m["ttr"] = _ttr(text)

    # T5 补充（对话体语料关键）：感叹/疑问句占比、省略号密度
    m["exclaim_ratio"] = round(text.count("！") / max(len(sents), 1), 4)
    m["question_ratio"] = round(text.count("？") / max(len(sents), 1), 4)
    m["ellipsis_per_k"] = _per_k(text.count("…"), n_chars)
    return m


def det_residual(human_text: str, cand_text: str) -> dict[str, dict[str, float]]:
    """返回 {candidate: {...}, delta: {...}}；delta = candidate - human。"""
    h = compute_metrics(human_text)
    c = compute_metrics(cand_text)
    delta = {k: round(c.get(k, 0.0) - h.get(k, 0.0), 4) for k in c}
    return {"candidate": c, "human": h, "delta": delta}


# 报告里展示用的核心指标子集（保持报告可读）
KEY_METRICS = [
    "sent_len_mean", "sent_len_std", "short_sent_ratio", "long_sent_ratio",
    "comma_period_ratio", "dialogue_ratio", "adv_flavor_per_k",
    "psych_marker_per_k", "emotion_word_per_k", "connective_per_k",
    "intensifier_per_k", "reduplication_per_k", "de_per_k", "ttr",
    "exclaim_ratio", "question_ratio", "ellipsis_per_k",
]
