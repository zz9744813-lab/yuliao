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

import json
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


# ── 存储紧凑编码（2026-09-30，语料容量）────────────────────────────────────
# 背景：`segments.integrity` 每段一份 195 字节 JSON，5300 万段 = 9.4 GB；
# 目标 3605 本要 1.1 亿段 ⇒ 光这一列就要 21 GB，是「库吃不下全量语料」的最大单项。
#
# 口径（逐位无损，不做有损量化）：
#   * 只压缩**恰好 8 个基键**的段（analyze() 的原始输出）。任何带附加键的
#     （`src_ok`/`severity`/`defects`/`clean_pending_llm`/`truncated`/…）**原样保留**
#     原始 JSON 文本，前缀 `j` 标记 ⇒ 库里靠 SQLite JSON1 读 `$.src_ok` 的存量脚本
#     （k5_supply_recount / k5_sourcecheck_coverage）行为完全不变。
#   * 六个指标各自是**有限取值集**（见 _FIELD_VALUES，由 analyze() 的加减组合穷举），
#     取集合下标存 2/1/2/1/3/3 bit；`dialogue_ratio` 是 round(x,3) ⇒ 千分位整数 10 bit。
#     合计 22 bit ⇒ 6 位十六进制。**`eligible` 单独放在前缀后的第 1 个字符**
#     （`i1:1…` / `i1:0…`，共 10 字符）：这一位是 SQL 侧唯一要过滤的语义
#     （`app/near_dup.train_sampling_pool(eligible_only=True)` 走 `LIKE`），
#     放进 hex 里就没法在 SQL 里筛了。
#   * 任何**不在取值集内**的值（例如以后改了 analyze 的口径）一律回退原始 JSON，
#     不猜、不截断 —— 解码端永远能还原出与写入时逐字节相同的字典。
#
# 读侧：`unpack()` 同时吃紧凑串、历史原始 JSON；`canonical_json()`
# 把两种形态统一还原成**与旧口径一致**的 JSON 文本，因此 ORM 读者（app/models.py
# 的 CompactIntegrity 装饰器）拿到的字符串与压缩前完全一样。
_CODEC_PREFIX = "i1:"
# 紧凑行判 eligible 的 SQL `LIKE` 口径（首位字符）。历史 JSON 行的口径是
# `LEGACY_ELIGIBLE_TRUE_LIKE`——**两个都要挂**，库里两种形态长期并存。
ELIGIBLE_TRUE_LIKE = _CODEC_PREFIX + "1%"
LEGACY_ELIGIBLE_TRUE_LIKE = '%"eligible": true%'
_BASE_KEYS = ("quote_integrity", "antecedent_integrity", "dialogue_integrity",
              "scene_boundary", "context_dependency", "truncation_risk",
              "dialogue_ratio", "eligible")
# 取值集按 analyze() 的实际输出穷举；顺序即编码下标，**改动会让旧码解错**，
# 故只允许在尾部追加（追加不改已有下标）。
_FIELD_VALUES: dict[str, tuple[float, ...]] = {
    "quote_integrity": (0.0, 0.5, 1.0),                                    # 2 bit
    "antecedent_integrity": (0.0, 1.0),                                    # 1 bit
    "dialogue_integrity": (0.0, 0.5, 1.0),                                 # 2 bit
    "scene_boundary": (0.0, 1.0),                                          # 1 bit
    # 0.4 连接词 / 0.3 裸代词 / 0.5 半句或孤悬闭引号，任意组合后 min(1.0, ·)
    "context_dependency": (0.0, 0.3, 0.4, 0.5, 0.7, 0.8, 0.9, 1.0),        # 3 bit
    # 0.5/0.3 收尾标点 + 0.2 末句过短 + 0.3 末句远短于中位
    "truncation_risk": (0.0, 0.2, 0.3, 0.5, 0.6, 0.7, 0.8, 1.0),           # 3 bit
}
_RATIO_SCALE = 1000            # dialogue_ratio = round(x, 3) ⇒ 千分位整数
_RATIO_MAX = 1000
_CODEC_FIELDS = ("quote_integrity", "antecedent_integrity", "dialogue_integrity",
                 "scene_boundary", "context_dependency", "truncation_risk")


def _field_bits(name: str) -> int:
    return max(1, (len(_FIELD_VALUES[name]) - 1).bit_length())


def _ratio_bits() -> int:
    return max(1, _RATIO_MAX.bit_length())


def _encode_base(d: dict) -> str | None:
    """8 基键且取值全在集合内 ⇒ 紧凑串；否则 None（调用方回退原始 JSON）。"""
    n = 0
    for name in _CODEC_FIELDS:
        try:
            idx = _FIELD_VALUES[name].index(d[name])
        except ValueError:
            return None
        n = (n << _field_bits(name)) | idx
    ratio = d["dialogue_ratio"]
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    q = int(round(float(ratio) * _RATIO_SCALE))
    if not 0 <= q <= _RATIO_MAX:
        return None
    n = (n << _ratio_bits()) | q
    eligible = d["eligible"]
    if not isinstance(eligible, bool):
        return None
    return _CODEC_PREFIX + ("1" if eligible else "0") + format(n, "x")


def pack_raw(raw: str | dict | None) -> str | None:
    """把 integrity（JSON 文本或 dict）压成紧凑串；不满足紧凑条件时**原样返回**。

    幂等：紧凑串再喂进来原样返回，重复压缩不会套娃。
    不满足条件的三类（带附加键 / 取值超出集合 / 非法 JSON）**一个字节都不改**：
    库里那些裸 SQL 读法（`json_extract(integrity,'$.src_ok')`、直接 `json.loads`）
    对它们的行为与压缩前完全一致 —— 这也是「读侧无感」的一半。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)
    s = str(raw)
    if s.startswith(_CODEC_PREFIX):
        return s                                   # 已是紧凑形态
    stripped = s.strip()
    if not stripped:
        return s
    try:
        d = json.loads(stripped)
    except (TypeError, ValueError):
        return s                                   # 非法 JSON：一个字节都不动
    if not isinstance(d, dict) or set(d) != set(_BASE_KEYS):
        return s                                   # 带附加键：原样
    packed = _encode_base(d)
    return packed if packed is not None else s


def unpack(raw: str | None) -> dict:
    """紧凑串 / 历史原始 JSON ⇒ 字典；解不出来返回 {}（与旧 `or "{}"` 同口径）。"""
    if raw is None:
        return {}
    s = str(raw)
    if not s:
        return {}
    if s.startswith(_CODEC_PREFIX):
        body = s[len(_CODEC_PREFIX):]
        if len(body) < 2 or body[0] not in ("0", "1"):
            return {}                              # 缺 eligible 位/非法形态
        try:
            n = int(body[1:], 16)
        except ValueError:
            return {}
        eligible = body[0] == "1"
        ratio_mask = (1 << _ratio_bits()) - 1
        q = n & ratio_mask
        n >>= _ratio_bits()
        out: dict = {}
        for name in reversed(_CODEC_FIELDS):
            bits = _field_bits(name)
            idx = n & ((1 << bits) - 1)
            n >>= bits
            vals = _FIELD_VALUES[name]
            if idx >= len(vals):
                return {}
            out[name] = vals[idx]
        return {"quote_integrity": out["quote_integrity"],
                "antecedent_integrity": out["antecedent_integrity"],
                "dialogue_integrity": out["dialogue_integrity"],
                "scene_boundary": out["scene_boundary"],
                "context_dependency": out["context_dependency"],
                "truncation_risk": out["truncation_risk"],
                "dialogue_ratio": q / _RATIO_SCALE,
                "eligible": eligible}
    try:
        d = json.loads(s)
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def canonical_json(raw: str | None) -> str | None:
    """读侧还原成旧口径 JSON 文本（紧凑串解码；非紧凑串逐字节不动）。"""
    if raw is None:
        return None
    s = str(raw)
    if not s.startswith(_CODEC_PREFIX):
        return s                                   # 历史/附加键行：逐字节不动
    return json.dumps(unpack(s), ensure_ascii=False)


def is_packed(raw: str | None) -> bool:
    return bool(raw) and str(raw).startswith(_CODEC_PREFIX)


def is_eligible(raw: str | None) -> bool:
    """任何形态的 integrity ⇒ `eligible` 布尔（Python 侧口径，与 SQL `LIKE` 同义）。"""
    return bool(unpack(raw).get("eligible"))


def eligible_like_patterns() -> tuple[str, str]:
    """SQL `LIKE` 双口径：历史 JSON 行 + 紧凑行。**漏一个就是静默漏段**。"""
    return (LEGACY_ELIGIBLE_TRUE_LIKE, ELIGIBLE_TRUE_LIKE)


def loads_any(raw: str | None) -> dict:
    """紧凑串 / 历史原始 JSON ⇒ dict；**非法一律抛**（保留调用方 nojson 语义）。

    与 `unpack()` 的区别：`unpack` 解不出来给 `{}`（旧 `or "{}"` 口径，读值用），
    本函数给异常（旧 `json.loads(...)` 口径，审计/分桶用——「非法 JSON」和
    「合法但缺键」必须能分开）。
    """
    if raw is None:
        raise ValueError("integrity_null")
    s = str(raw)
    if s.startswith(_CODEC_PREFIX):
        d = unpack(s)
        if not d:
            raise ValueError("integrity_codec_undecodable")
        return d
    d = json.loads(s)
    if not isinstance(d, dict):
        raise ValueError("integrity_not_object")
    return d
