"""Segment 完整性六指标（任务一）——全部确定性 Python 启发式，零 LLM。

背景：琼明首测发现若干 human 段以孤悬的「」开头/结尾（跨段引语被切断），
起手就在自然度盲评里丢分。切分器只保证"不在句中切"，不保证"不在场景/对话/指代中切"。

六指标（越大越好，除 truncation_risk / context_dependency 是风险项）：
  quote_integrity     引号配平：无孤悬开/闭引号，引号内非空
  antecedent_integrity 首句指代可自解：不以裸代词/零主语/连接词开头
  dialogue_integrity   对话完整：有引号必有成对闭合，独白不悬空
  scene_boundary       场景起点信号：时间/地点/状态开场，或全书首段
  context_dependency   上下文依赖（风险项，越低越好）：连接词开场/半句开场/指代开场
  truncation_risk      截断风险（风险项，越低越好）：无终止标点收尾/逗号悬尾/末句异常短

is_naturalness_eligible()：自然度校准只允许自足性强的段参与，
避免"仪器伪影压低 human 分"（2026-09-12 窗口评委实验已证实此效应存在）。
"""
from __future__ import annotations

import re

# 引号对（开, 闭）
_QUOTE_PAIRS = [("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’")]
_OPENERS = {o for o, _ in _QUOTE_PAIRS}
_CLOSERS = {c for _, c in _QUOTE_PAIRS}
_ALL_QUOTES = _OPENERS | _CLOSERS

# 裸代词/泛指开场：指代大概率挂在前文
_PRONOUN_START = re.compile(r"^(他|她|它|他俩|她们|两人|二人|三人|众人|那人|此人|对方|两人之间)")
# 连接词/承接语开场：语义挂在前句
_CONNECTIVE_START = re.compile(r"^(但|可是|然而|而|于是|接着|随即|顿时|随即|话音未落|闻言|见状|说罢|半晌|片刻后|这时|此时)")
# 场景起点信号：时间/地点/天象开场
_SCENE_START = re.compile(r"^(第[一二三四五六七八九十百千0-9]+[章回节]|翌日|次日|次日清晨|清晨|黄昏|傍晚|入夜|深夜|午后|正午|夜里|天亮|雨|风|雪|月|日|天岭池|大山|城|府|院|殿|山门)")
# 半句开场（上句延续）：小写逗号/助词开头
_MID_SENTENCE_START = re.compile(r"^[，、。；：…—）]")

_TERMINAL = "。！？…”」』"
_SENT_SPLIT = re.compile(r"[。！？…]+")

RISK_HIGH, RISK_MID, RISK_LOW = "high", "mid", "low"


def _quote_stats(text: str) -> dict:
    opens = closes = 0
    unclosed = 0
    empty_quotes = 0
    stack: list[str] = []
    for ch in text:
        if ch in _OPENERS:
            stack.append(ch)
            opens += 1
        elif ch in _CLOSERS:
            closes += 1
            if not stack:
                unclosed += 1  # 孤悬闭引号
            else:
                o = stack.pop()
                # 引号内空（「。」相邻处理粗糙但可接受）
    empty_quotes = text.count("「」") + text.count("『』") + text.count("“”")
    return {"opens": opens, "closes": closes, "unclosed_open": len(stack),
            "orphan_close": unclosed, "empty": empty_quotes}


def analyze(text: str, *, ordinal: int = 0) -> dict:
    text = (text or "").strip()
    if not text:
        return {k: 0.0 for k in (
            "quote_integrity", "antecedent_integrity", "dialogue_integrity",
            "scene_boundary", "context_dependency", "truncation_risk")} | {"eligible": False}

    first = text[0]
    sentences = [x for x in _SENT_SPLIT.split(text) if x.strip()]
    last = text[-1]

    # 1) quote_integrity：配平 + 无孤悬
    qs = _quote_stats(text)
    quote_ok = (qs["unclosed_open"] == 0 and qs["orphan_close"] == 0
                and qs["opens"] == qs["closes"] and qs["empty"] == 0)
    # 首字符是闭引号 = 从上段对话中间切进来，直接判 0
    if first in _CLOSERS:
        quote_ok = False
    quote_score = 1.0 if quote_ok else (0.5 if qs["orphan_close"] + qs["unclosed_open"] <= 1 else 0.0)

    # 2) antecedent_integrity：首句指代自足
    antecedent_bad = bool(_PRONOUN_START.match(text)) or bool(_MID_SENTENCE_START.match(text))
    antecedent_score = 0.0 if antecedent_bad else 1.0

    # 3) dialogue_integrity：有对话则必须闭合；对话占比过高且无叙述锚 → 悬空风险
    n_quote_chars = sum(text.count(q) for q in _ALL_QUOTES)
    dialogue_ratio = 0.0
    if n_quote_chars:
        dialogue_chars = 0
        inside = False
        for ch in text:
            if ch in _OPENERS:
                inside = True
            elif ch in _CLOSERS:
                inside = False
            elif inside:
                dialogue_chars += 1
        dialogue_ratio = dialogue_chars / max(1, len(text))
    dialogue_ok = (qs["unclosed_open"] == 0 and qs["orphan_close"] == 0)
    dialogue_score = 1.0 if (not n_quote_chars or dialogue_ok) else 0.0
    if n_quote_chars and dialogue_ratio > 0.9 and not sentences:
        dialogue_score = 0.5

    # 4) scene_boundary：场景起点信号
    scene = 1.0 if (ordinal == 0 or bool(_SCENE_START.match(text))) else 0.0

    # 5) context_dependency（风险项 0~1，越低越好）
    dep = 0.0
    if _CONNECTIVE_START.match(text):
        dep += 0.4
    if _PRONOUN_START.match(text):
        dep += 0.3
    if _MID_SENTENCE_START.match(text):
        dep += 0.5
    if first in _CLOSERS:
        dep += 0.5
    context_dependency = min(1.0, dep)

    # 6) truncation_risk（风险项 0~1，越低越好）
    risk = 0.0
    if last not in _TERMINAL:
        risk += 0.5 if last in "，、；：…—" else 0.3
    if sentences and len(sentences[-1]) < 4 and len(text) > 40:
        risk += 0.2
    # 末句远短于中位句长 → 疑似被拦腰
    if len(sentences) >= 3:
        lens = sorted(len(x) for x in sentences)
        median = lens[len(lens) // 2]
        if median > 10 and len(sentences[-1]) < median * 0.25:
            risk += 0.3
    truncation_risk = min(1.0, risk)

    eligible = (quote_score >= 1.0 and antecedent_score >= 1.0
                and dialogue_score >= 1.0 and truncation_risk <= 0.3
                and context_dependency <= 0.4)
    return {
        "quote_integrity": quote_score,
        "antecedent_integrity": antecedent_score,
        "dialogue_integrity": dialogue_score,
        "scene_boundary": scene,
        "context_dependency": context_dependency,
        "truncation_risk": truncation_risk,
        "dialogue_ratio": round(dialogue_ratio, 3),
        "eligible": bool(eligible),
    }


def is_naturalness_eligible(integrity: dict | None) -> bool:
    """报告/校准抽样用它过滤；未回填的旧数据按不合格处理（宁缺毋滥）。"""
    return bool(integrity and integrity.get("eligible"))
