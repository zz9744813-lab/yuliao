"""K2 成对对照抽取器（paired_contrast_v2 前置机制，主控派工 2026-09-23）。

背景：8 条策略卡的"逐字证据"是单段描述，独立判断 24/24 判「证据不支持抽象
操作」0 条升格——根因：证据只说明"这一段在写什么"，与"人 X / AI Y 的对比"
无关（docs/策略语义审查_判定模型版_20260923.md）。合并方案定稿两条新卡
（docs/策略卡合并方案_20260923.md）：S1 留白摊开=语义增量、S2 节拍注水=
语义不变密度下降，互斥判据=主改动形态。

本模块换抽取协议：抽**成对对照**（同一场景的人类原文片段 ↔ 模型铺陈片段）。
独立审查席对合并方案给出 REVISE（F:/agi/_scratch/worktrees/mergeplan/
REVIEW_merge_plan.md）：靠"事后看文本归类"判 S1/S2（delta_new / delta_dil）
没有可执行的计算程序 ⇒ 分类器不可机械判定。修法：**按构造标注**——配对时
显式声明用了哪种操作（`op`，每对只许一个），S1/S2 归属由 op 经唯一映射表
**直接决定**（可机械），门只验证"这个构造真的成立"。六道**可机械判定**的门：

- 门0 gate_op_construction（按 op 验证构造，每类 op 有自己的断言）：
  · OP_ADD_INTERPRETATION / OP_ADD_PSYCH_NARRATION（→S1）：AI 侧**句数增加**，
    命中解释/心理标记词表的句必须是**新增**（不在人类侧），且人类侧不命中词表；
    新增命中句还须**自身可锚定到场景**（句内含 scene_keys 指称——_scene_refs
    字符集口径——或人称回指字 她/他/它/自己；REVISE_k2_contrast_v2：原门0 只验
    形态不验所指，"逐字引用后接跑题句"能过全门）。机械口径的**已知残余**：
    跑题句复述人名即可锚定，拦不住（回归 xfail 钉住）；
  · OP_SPLIT_BEATS / OP_DILUTE_MODIFIERS（→S2）：**实词集合 Jaccard ≥ 阈值**
    （命题/实词集合基本不变）且节拍/修饰标记数上升（密度变化可测）；
  · 并拒**混合形态**：跨标签的另一操作证据混入同一对 ⇒ 无效——不让混合形态
    进数据，就不需要在分类器里解决它。
- 门1 gate_keyword_cooccurrence：策略特征词表（S1=摊开/解释类信号词；
  S2=节拍/密度/修饰类信号词），要求 ai_text 命中**且** human_text 不命中；
  双向命中 / 双向未命中 / 反向命中 ⇒ 拒（理由可读，如 `S1 双向命中: [...]`）。
- 门2 gate_scene_reference：scene_keys 非空，且两侧的场景指称交集非空
  （人名/地名的字符集口径：指称的每个字符都出现在该侧文本中即视为该侧
  引用了此指称），证明两段写的是同一个场景；空集 ⇒ 拒。
- 门3 gate_length_ratio：len(ai)/len(human) ∈ [1.2, 6.0]，出界 ⇒ 拒
  （S2 的"注水"必须有可测的密度变化；S1 的"摊开"同理）。
- 门4 gate_anti_copy（anti-copy 反抄写，独立审查 REVISE 反例1/2）：S1 类对
  若 human 侧全文被 ai 侧**连续包含** ⇒ 拒（"照抄+贴标签"式假对照）。
  口径是**连续包含**而非模糊相似（合法扩写本就共享大量子串，LCS 相似度
  阈值必然误杀）：
  ① ai 侧以 human 侧全文为前缀开头（其后直接接续标签句）⇒ 拒；
  ② human 侧剔标点后全文作为连续子串出现在 ai 侧、且命中长度 ≥
  MIN_COPY_LEN（默认 40 字，模块常量，测试可覆写——短于该长度的偶合不判
  照抄）⇒ 拒。理由含 `anti-copy` 与命中片段长度。S2（拆拍/注水）按构造
  **保留原句**，human 侧本就整段出现在 ai 侧，不适用本门——该形态的双卡
  风险由门5 拦。
- 门5 gate_cross_strategy（cross-strategy 跨策略互斥，独立审查 REVISE
  反例1 根因）：一对必须**只**在声明的那条策略下放行——用**对方策略**的
  特征词表把 ai 侧再评一遍：本方词表与对方词表**同时**命中 ⇒ 拒（理由带
  双方命中的词），从根上堵住"同一对以 S1/S2 双卡双计"。
- 旁路账本（AI 侧可复核）：live 落库时把**完整配对**（含 AI 侧原文、
  pair_id、op 与 S1/S2 两个标签、六道门结果与拒绝理由、落库结果）追加
  写入 JSONL 账本（--pairs-ledger，默认 k2_pairs.jsonl）。不改任何既有
  表结构（真库建表史有 NOT NULL 无默认的坑，故走旁路文件）。

纪律：
- **纯离线**：AI 侧片段由调用方注入（--pairs-file / run_contrast 入参），
  本工具不联网、不生成、不调模型。
- **fail-closed**：--dry-run（默认）零库写只出统计与拒绝样本；真落库需
  --live **且** 环境变量 K2CONTRAST_ALLOW_LIVE=1，缺一即拒；写库前照用既有
  R6 互斥锁（app.live_guard.live_lock），锁被持有即拒绝，不与 live/pytest 重叠。
- **落库走既有 strategy_instances 表结构**：不改任何表结构、不放宽既有
  门禁（verify_instance_span 照用——human 侧 span 对登记段逐字可核，
  核不上不落）；新实例 status=proposed（状态纪律：判定≠升格）。
- **幂等**：同 (strategy_id, strategy_version, evidence_sha256) 已有行
  即跳过——同输入重跑不重复落库。

用法：
    python scripts/k2_contrast_extract.py --dry-run --pairs-file pairs.json
    K2CONTRAST_ALLOW_LIVE=1 python scripts/k2_contrast_extract.py --live \
        --pairs-file pairs.json --extractor-model paired_contrast_v2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PROTOCOL = "paired_contrast_v2"

# 合并方案 §1 定稿的两条新卡（strategy_key 照抄方案文档）
S1_KEY = "v2:留白摊开"
S2_KEY = "v2:节拍注水"

# 门1 特征词表（合并方案 §4 门A：S1 人类侧无解释类标记且 AI 侧有；
# S2 侧 AI 节拍/修饰标记成立且人类侧无）。机械 substring 匹配，离线确定。
S1_SPREAD_WORDS = (        # 摊开/解释类：AI 把潜台词摊成明文的信号
    "因为", "其实", "意味着", "心里明白", "说到底", "是由于",
    "换句话说", "之所以", "说白了", "归根结底",
)
S2_BEAT_WORDS = (          # 节拍/密度/修饰类：AI 拆拍注水的信号
    "忽然", "就在", "最后", "接着", "然后", "慢慢", "缓缓",
    "渐渐", "仿佛", "像是", "一连", "似乎", "轻轻", "一般",
)
FEATURE_WORDS: dict[str, tuple[str, ...]] = {
    S1_KEY: S1_SPREAD_WORDS,
    S2_KEY: S2_BEAT_WORDS,
}
_STRATEGY_LABEL = {S1_KEY: "S1", S2_KEY: "S2"}

# 门3 长度比边界（任务口径：S2 注水/S1 摊开都必须有可测的长度变化）
MIN_LEN_RATIO = 1.2
MAX_LEN_RATIO = 6.0

# 门4 反抄写口径（独立审查 REVISE 反例1：ai 侧 = human 侧逐字全文 + 尾缀
# 标签句）。判据是**连续包含**而非模糊相似——LCS 相似度阈值会误杀合法扩写
# （同一场景的人名、动作词本就共享大量子串）。两条判据并列，任一命中即拒：
#   ① ai 侧以 human 侧全文为前缀开头（其后直接接续标签句的典型照抄形态；
#     反例1 的 human 全文 36 字 < 40，仍由此条判拒——前缀照抄不设长度容错）；
#   ② human 侧剔标点后全文作为连续子串出现在 ai 侧，且命中长度 ≥
#     MIN_COPY_LEN（短于该长度的偶合不判照抄）。
# 模块常量，测试可整体覆写（monkeypatch 模块属性）。
MIN_COPY_LEN = 40

# 剔标点字符集（中英常用标点与空白；只影响门4 的包含判定，别处不用）
_PUNCT_CHARS = set(
    "，。！？；：、…—·～（）《》〈〉「」『』“”‘’"
    ",.;:!?()<>[]{}'\"`~^%|\\"
    " \t\n\r")

# ---------------------------------------------------------- 按构造标注
# 独立审查 REVISE（F:/agi/_scratch/worktrees/mergeplan/REVIEW_merge_plan.md）：
# "判 S1 不判 S2"若靠事后看文本归类（delta_new / delta_dil）没有可执行的计算
# 程序 ⇒ 分类器不可机械判定。改法：**按构造标注**——每对配对时显式声明唯一
# 的操作 op，S1/S2 归属由 op 直接决定（可机械）；门只验证构造成立。
OP_ADD_INTERPRETATION = "add_interpretation"    # 新增解释性陈述（人类侧无对应来源）→ S1
OP_ADD_PSYCH_NARRATION = "add_psych_narration"  # 补心理旁白/内心活动 → S1
OP_SPLIT_BEATS = "split_beats"                  # 拆一句动作/台词为多节拍（命题不增）→ S2
OP_DILUTE_MODIFIERS = "dilute_modifiers"        # 提高修饰密度/插入比喻（命题不增）→ S2

OPS = (OP_ADD_INTERPRETATION, OP_ADD_PSYCH_NARRATION,
       OP_SPLIT_BEATS, OP_DILUTE_MODIFIERS)

# 唯一映射表：op → S1/S2（查询入口 label_of；非法 op fail-closed 抛错）
OP_LABEL = {OP_ADD_INTERPRETATION: "S1", OP_ADD_PSYCH_NARRATION: "S1",
            OP_SPLIT_BEATS: "S2", OP_DILUTE_MODIFIERS: "S2"}
LABEL_STRATEGY = {"S1": S1_KEY, "S2": S2_KEY}


def label_of(op: str) -> str:
    """op → "S1" | "S2"。唯一映射、判定确定（同 op 必得同标签）；
    非法 op 抛 ValueError（fail-closed：不在四类操作里就不许进门）。"""
    try:
        return OP_LABEL[op]
    except (KeyError, TypeError):
        raise ValueError(f"非法操作标签 op={op!r}"
                         f"（必须在四类操作之一，每对只许声明一个 op）") from None


# 门0（构造验证）标记词表：模块常量，测试可整体覆写（monkeypatch 模块属性）。
INTERPRET_MARKERS = (      # 解释类：把潜台词摊成明文的信号
    "因为", "其实", "意味着", "说到底", "换句话说", "之所以",
    "说白了", "归根结底", "是由于", "总之",
)
PSYCH_MARKERS = (          # 心理类：内心活动旁白的信号
    "心里", "心想", "暗想", "暗自", "腹诽", "嘀咕",
    "寻思", "琢磨", "心头", "心底", "暗暗", "默默",
)
BEAT_MARKERS = (           # 节拍类：拆拍的时间/动作推进信号
    "忽然", "就在", "接着", "然后", "慢慢", "缓缓",
    "渐渐", "一连", "一步一步",
)
MODIFIER_MARKERS = (       # 修饰类：比喻/情态修饰信号
    "仿佛", "像是", "似乎", "宛如", "轻轻", "淡淡", "一般",
)

# 实词口径：CJK 字符剔除高频虚词/代词/数词等封闭类后的字符集合
# （离线、确定、无分词依赖；Jaccard 只作"命题/实词集合基本不变"的机械代理）。
FUNCTION_CHARS = set(
    "的了着呢吗吧啊呀哦嘛么把被给和与或也很就很都只才又再还就便之乎者哉"
    "她他您我咱你它是在一没不"
)
JACCARD_MIN = 0.3          # S2 门：实词集合 Jaccard 下限（命题集合基本不变）

_SENT_SPLIT = re.compile(r"[。！？；\n]+")


def _sentences(text: str) -> list[str]:
    """机械分句：按 。！？；\\n 切，丢空段。离线、确定。"""
    return [s for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _hit_count(text: str, words) -> int:
    """词表命中总次数（substring 口径，离线确定）。"""
    t = text or ""
    return sum(t.count(w) for w in words)


def _strip_punct(text: str) -> str:
    """剔除标点与空白（门4 口径：只比内容字，逗号差异不算改写）。"""
    return "".join(ch for ch in (text or "") if ch not in _PUNCT_CHARS)


def content_tokens(text: str) -> set:
    """实词集合（机械代理）：剔除虚词/代词/数词等封闭类字符后的 CJK 字符集。"""
    return {ch for ch in (text or "")
            if "\u4e00" <= ch <= "\u9fff" and ch not in FUNCTION_CHARS}


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class ContrastPair:
    """一条成对对照：同一场景的人类原文片段 ↔ AI 铺陈片段。

    **op 必填**（按构造标注）：声明这一对用了哪种操作（四类之一），
    S1/S2 归属由 label_of(op) 直接决定；strategy_key 是派生字段——留空
    按 op 推导，显式给了但与推导不一致 ⇒ ValueError（归属唯一，不许漂）。
    每对只许声明**一个** op；若构造过程用了多个操作，证据形态会被
    gate_op_construction 的混合检查判无效（不让混合形态进数据）。

    span_start/span_end 是**人类侧**片段在其登记段原文里的区间
    （落库后由 app.knowledge.verify_instance_span 机械核对）；
    human_sha256 缺省时按 human_text 现算（幂等键的原料）；
    meta 携带落库定位（segment_id / text_version），不入门判。"""
    human_text: str
    ai_text: str
    scene_keys: set
    op: str
    span_start: int
    span_end: int
    strategy_key: str = ""      # 派生字段：由 op 唯一决定
    human_sha256: str = ""
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.scene_keys = set(self.scene_keys or ())
        label = label_of(self.op)          # 非法 op：这里直接抛（fail-closed）
        derived = LABEL_STRATEGY[label]
        if self.strategy_key and self.strategy_key != derived:
            raise ValueError(
                f"strategy_key {self.strategy_key!r} 与 op={self.op!r}"
                f"（{label}）推导的 {derived!r} 不符——S1/S2 归属由 op 决定")
        self.strategy_key = derived
        if not self.human_sha256:
            self.human_sha256 = _sha256(self.human_text)


# ------------------------------------------------ 门 0：构造验证（按 op）
# 门0 场景锚定口径（REVISE_k2_contrast_v2 修法）：新增的命中句必须**自身**
# 可锚定到场景——句内含 scene_keys 的指称（复用门2 的 _scene_refs 字符集
# 口径）或含人称回指字（她/他/它/自己）。纯 substring/字符集运算，可机械
# 判定；复述人名的跑题句在此口径下仍可锚定 ⇒ 已知残余（回归 xfail 钉住）。
_ANAPHORA_CHARS = ("她", "他", "它", "自己")


def _anchored_to_scene(scene_keys: set, sent: str) -> bool:
    """句级场景锚定（纯机械）：句内含场景指称（_scene_refs 字符集口径）
    或人称回指字（她/他/它/自己）即视为可锚定。离线、确定。"""
    if _scene_refs(scene_keys, sent):
        return True
    return any(c in (sent or "") for c in _ANAPHORA_CHARS)


def _verify_s1_addition(pair: ContrastPair, op: str) -> list[str]:
    """S1 类 op 的构造断言：AI 侧句数增加；命中解释/心理标记词表的句必须是
    新增（不在人类侧）；人类侧不命中词表；新增命中句还须**锚定到场景**
    （_anchored_to_scene：句内含 scene_keys 指称或她/他/它/自己回指——
    堵"首句改写+跑题尾缀"形态）。词表是模块常量，可被测试覆写。"""
    markers = INTERPRET_MARKERS if op == OP_ADD_INTERPRETATION else PSYCH_MARKERS
    vocab_name = "解释" if op == OP_ADD_INTERPRETATION else "心理"
    h_text = pair.human_text or ""
    h_sents, a_sents = _sentences(h_text), _sentences(pair.ai_text)
    reasons = []
    if len(a_sents) <= len(h_sents):
        reasons.append(
            f"{op} 构造不符: AI 侧句数未增（{len(a_sents)}≤{len(h_sents)}），"
            f"不构成新增陈述")
    hit_sents = [s for s in a_sents if any(m in s for m in markers)]
    if not hit_sents:
        reasons.append(
            f"{op} 构造不符: 新增句未命中{vocab_name}标记词表"
            f"（AI 侧无可指认的新增{vocab_name}句）")
    else:
        old = [s for s in hit_sents if s in h_sents or s in h_text]
        if old:
            reasons.append(
                f"{op} 构造不符: 命中{vocab_name}标记的句在人类侧已存在"
                f"（非新增）: {old[:1]}")
        loose = [s for s in hit_sents
                 if s not in h_sents and s not in h_text
                 and not _anchored_to_scene(pair.scene_keys, s)]
        if loose:
            reasons.append(
                f"{op} 构造不符: 新增{vocab_name}句未锚定到场景"
                f"（句内无 scene_keys 指称也无她/他/它/自己回指——"
                f"所指不落本场景，疑似跑题尾缀）: {loose[:1]}")
    if any(m in h_text for m in markers):
        reasons.append(
            f"{op} 构造不符: 人类侧命中{vocab_name}标记词表（不构成对照）")
    return reasons


def _verify_s2_rewrite(pair: ContrastPair, op: str) -> list[str]:
    """S2 类 op 的构造断言：实词集合 Jaccard ≥ 阈值（命题/实词集合基本不变）
    且节拍/修饰标记数上升（密度变化可测）。"""
    reasons = []
    h_tok, a_tok = content_tokens(pair.human_text), content_tokens(pair.ai_text)
    if not h_tok:
        return [f"{op} 构造不符: human_text 无实词，Jaccard 不可判"]
    jac = len(h_tok & a_tok) / len(h_tok | a_tok)
    if jac < JACCARD_MIN:
        reasons.append(
            f"{op} 构造不符: 实词集合 Jaccard={jac:.2f}<{JACCARD_MIN}"
            f"（命题/实词集合变动过大，不是拆拍/注水）")
    if op == OP_SPLIT_BEATS:
        vocab, name = BEAT_MARKERS, "节拍"
    else:
        vocab, name = MODIFIER_MARKERS, "修饰"
    n_a, n_h = _hit_count(pair.ai_text, vocab), _hit_count(pair.human_text, vocab)
    if n_a <= n_h:
        reasons.append(
            f"{op} 构造不符: {name}标记数未上升（AI {n_a} ≤ human {n_h}），"
            f"密度/节拍变化不可测")
    return reasons


def _verify_mixed(pair: ContrastPair) -> list[str]:
    """混合形态检查（每对一个 op）：跨标签的另一操作证据混入同一对 ⇒ 无效。
    S1 对不许混入 S2 形态（节拍/修饰标记上升）；S2 对不许混入 S1 形态
    （新增句命中解释/心理标记）。不让混合形态进数据，分类器就无需解决它。"""
    if OP_LABEL[pair.op] == "S1":
        for name, vocab in (("节拍", BEAT_MARKERS), ("修饰", MODIFIER_MARKERS)):
            if _hit_count(pair.ai_text, vocab) > _hit_count(pair.human_text, vocab):
                return [f"混合操作: {pair.op} 对混入{name}形态"
                        f"（{name}标记数在 AI 侧上升）——拆成两对，每对一个 op"]
        return []
    h_sents = _sentences(pair.human_text)
    for s in _sentences(pair.ai_text):
        if (s not in h_sents and s not in (pair.human_text or "")
                and (any(m in s for m in INTERPRET_MARKERS)
                     or any(m in s for m in PSYCH_MARKERS))):
            return [f"混合操作: {pair.op} 对混入解释/心理新增句"
                    f"——拆成两对，每对一个 op"]
    return []


def gate_op_construction(pair: ContrastPair) -> tuple[bool, list[str]]:
    """按 op 验证构造：每类 op 有**对着自己**的断言（不能一条门通吃所有 op），
    并检查没有混入跨标签的另一操作形态。"""
    reasons = (_verify_s1_addition(pair, pair.op)
               if OP_LABEL[pair.op] == "S1"
               else _verify_s2_rewrite(pair, pair.op))
    reasons += _verify_mixed(pair)
    return (not reasons, reasons)


# ---------------------------------------------------------------- 门 1
def gate_keyword_cooccurrence(pair: ContrastPair) -> tuple[bool, list[str]]:
    """ai_text 命中特征词 且 human_text 不命中才放行；三种破形全拒。"""
    words = FEATURE_WORDS.get(pair.strategy_key)
    if not words:
        return False, [f"未知策略 {pair.strategy_key}: 无特征词表"]
    label = _STRATEGY_LABEL.get(pair.strategy_key, pair.strategy_key)
    ai_hits = sorted(w for w in words if w in pair.ai_text)
    human_hits = sorted(w for w in words if w in pair.human_text)
    if ai_hits and not human_hits:
        return True, []
    if ai_hits and human_hits:
        return False, [f"{label} 双向命中: {human_hits}"]
    if not ai_hits and not human_hits:
        return False, [f"{label} 双向未命中: 特征词 {len(words)} 个全缺"
                       f"（AI 侧未呈现该策略改动形态）"]
    return False, [f"{label} 反向命中: human 命中 {human_hits} 而 AI 侧未命中"
                   f"（人类原文已含解释/节拍信号，不构成对照）"]


# ---------------------------------------------------------------- 门 2
def _scene_refs(scene_keys: set, text: str) -> set:
    """场景指称匹配（字符集口径）：指称的每个字符都出现在该侧文本中，
    即视为该侧引用了此指称。离线、确定、机械可核——不做分词猜名。"""
    chars = set(text or "")
    return {k for k in scene_keys if k and set(k) <= chars}


def gate_scene_reference(pair: ContrastPair) -> tuple[bool, list[str]]:
    """两侧场景指称交集非空才放行——证明两段写的是同一个场景。"""
    if not pair.scene_keys:
        return False, ["场景指称缺失: scene_keys 为空"]
    h = _scene_refs(pair.scene_keys, pair.human_text)
    a = _scene_refs(pair.scene_keys, pair.ai_text)
    if not (h & a):
        return False, [f"场景指称交集为空: human={sorted(h)} ai={sorted(a)}"
                       f"（两侧疑似不是同一场景）"]
    return True, []


# ---------------------------------------------------------------- 门 3
def gate_length_ratio(pair: ContrastPair) -> tuple[bool, list[str]]:
    """len(ai)/len(human) 必须落在 [1.2, 6.0]——两侧等长说明没有摊开/注水，
    超长说明已不是同一片段的铺陈化。"""
    n_h = len(pair.human_text or "")
    if n_h == 0:
        return False, ["长度比不可判: human_text 为空"]
    ratio = len(pair.ai_text or "") / n_h
    if not (MIN_LEN_RATIO <= ratio <= MAX_LEN_RATIO):
        return False, [f"长度比超界: {ratio:.2f} ∉ "
                       f"[{MIN_LEN_RATIO}, {MAX_LEN_RATIO}]"]
    return True, []


# ---------------------------------------------------------------- 门 4
def gate_anti_copy(pair: ContrastPair) -> tuple[bool, list[str]]:
    """门4 反抄写（anti-copy）：human 侧全文被 ai 侧**连续包含** ⇒ 拒。

    只对 S1 类对生效：S2（拆拍/注水）按构造**保留原句**、命题不增，human
    侧整段出现在 ai 侧正是其合法形态，本门若套用会连正例一起拒——S2 形态
    的"照抄+贴标签"双卡风险由门5（cross-strategy）拦。

    两条判据并列（任一命中即拒，理由含 `anti-copy` 与命中片段长度）：
    ① ai 侧以 human 侧全文为前缀开头（`ai.startswith(human)`，照抄+贴标签
      的典型形态，不设长度容错——审查反例1 的 human 全文 36 字 < 40 仍判拒）；
    ② human 侧剔标点后全文作为连续子串出现在 ai 侧（`h_norm in a_norm`，
      去掉首尾空白/标点后仍如此），且命中长度 ≥ MIN_COPY_LEN——短于该
      长度的偶合不判照抄。"""
    if OP_LABEL[pair.op] != "S1":
        return True, []
    h, a = pair.human_text or "", pair.ai_text or ""
    h_norm = _strip_punct(h)
    if not h_norm:
        return False, ["anti-copy 不可判: human_text 剔标后无内容（fail-closed）"]
    if a.startswith(h):
        return False, [f"anti-copy 照抄+贴标签: ai 侧以 human 全文为前缀开头"
                       f"（命中片段 {len(h)} 字，其后直接接续标签句）——"
                       f"逐字照抄只贴标签，不构成对照"]
    if h_norm in _strip_punct(a):
        n = len(h_norm)
        if n >= MIN_COPY_LEN:
            return False, [f"anti-copy 照抄+贴标签: human 侧全文作为连续子串"
                           f"出现在 ai 侧（剔标点后命中片段 {n} 字 ≥ "
                           f"min_copy_len={MIN_COPY_LEN}）——"
                           f"逐字照抄只贴标签，不构成对照"]
    return True, []


# ---------------------------------------------------------------- 门 5
def gate_cross_strategy(pair: ContrastPair) -> tuple[bool, list[str]]:
    """门5 跨策略互斥（cross-strategy）：一对必须**只**在声明的那条策略下
    放行。用**对方策略**的特征词表把 ai 侧再评一遍：本方词表与对方词表
    **同时**命中 ⇒ 拒（理由带双方命中的词）——同一对不许以 S1/S2 双卡双计
    （审查反例1 的根因：门1 只查自己策略的词表，从不查对方词表）。"""
    label = _STRATEGY_LABEL.get(pair.strategy_key, pair.strategy_key)
    other_label = "S2" if label == "S1" else "S1"
    other_key = LABEL_STRATEGY[other_label]
    own_hits = sorted(w for w in FEATURE_WORDS.get(pair.strategy_key, ())
                      if w in (pair.ai_text or ""))
    other_hits = sorted(w for w in FEATURE_WORDS.get(other_key, ())
                        if w in (pair.ai_text or ""))
    if own_hits and other_hits:
        return False, [f"cross-strategy 互斥破形: {label} 对的 ai 侧同时命中"
                       f"对方 {other_label} 词表 {other_hits}"
                       f"（本方命中 {own_hits}）——同一对不许以两条策略双卡双计"]
    return True, []


GATES = (("op_construction", gate_op_construction),
         ("keyword_cooccurrence", gate_keyword_cooccurrence),
         ("scene_reference", gate_scene_reference),
         ("length_ratio", gate_length_ratio),
         ("anti_copy", gate_anti_copy),
         ("cross_strategy", gate_cross_strategy))


def _run_gates(pair: ContrastPair) -> tuple[bool, list[str], dict]:
    """跑全部六道门：返回 (是否全过, 汇总理由, 逐门结果明细)。"""
    ok, reasons, detail = True, [], {}
    for name, gate in GATES:
        g_ok, g_reasons = gate(pair)
        detail[name] = "pass" if g_ok else "fail"
        if not g_ok:
            ok = False
            reasons.extend(g_reasons)
    return ok, reasons, detail


def gate_pair(pair: ContrastPair) -> tuple[bool, list[str]]:
    """六道门全过才放行；任一破形收集全部理由（不短路，拒绝样本可读）。"""
    ok, reasons, _detail = _run_gates(pair)
    return ok, reasons


# ------------------------------------------------------- 构建 / 汇总
def build_pairs(specs: list[dict]) -> list[ContrastPair]:
    """把调用方注入的成对片段规格转成 ContrastPair 列表。

    每项规格必填：human_text / ai_text / **op**（按构造标注，S1/S2 归属由它
    决定）/ scene_keys / span_start / span_end；strategy_key 可选（须与 op
    推导一致，否则 ValueError）；segment_id / text_version（落库定位，进
    meta）可选。AI 侧片段**只能**从这里来。"""
    pairs = []
    for spec in specs:
        op = spec.get("op")
        if not op:
            raise ValueError("每条规格须有 op（按构造标注：S1/S2 归属由 op "
                             "决定，不再事后归类）")
        pairs.append(ContrastPair(
            human_text=spec["human_text"], ai_text=spec["ai_text"],
            scene_keys=set(spec.get("scene_keys") or ()),
            op=op, strategy_key=spec.get("strategy_key") or "",
            span_start=int(spec["span_start"]), span_end=int(spec["span_end"]),
            human_sha256=spec.get("human_sha256") or "",
            meta={"segment_id": spec.get("segment_id"),
                  "text_version": spec.get("text_version"),
                  **(spec.get("meta") or {})},
        ))
    return pairs


def load_pairs_file(path: str) -> list[ContrastPair]:
    """读取调用方注入的成对片段 JSON：{pairs: [...]}（每项须带 op）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return build_pairs(raw.get("pairs") or [])


def summarize(pairs: list[ContrastPair]) -> dict:
    """通过/拒绝计数（含**按 op** 的通过/拒绝）+ 拒绝理由分类
    （冒号前的门+形态即类目）+ 拒绝样本。"""
    passed, reason_classes, samples = 0, {}, []
    by_op = {op: {"passed": 0, "rejected": 0} for op in OPS}
    for p in pairs:
        bucket = by_op[p.op]
        ok, reasons = gate_pair(p)
        if ok:
            passed += 1
            bucket["passed"] += 1
            continue
        bucket["rejected"] += 1
        for r in reasons:
            cls = r.split(":", 1)[0]
            reason_classes[cls] = reason_classes.get(cls, 0) + 1
        if len(samples) < 5:
            samples.append({"op": p.op, "strategy_key": p.strategy_key,
                            "reasons": reasons})
    return {"n_pairs": len(pairs), "passed": passed,
            "rejected": len(pairs) - passed, "by_op": by_op,
            "reject_reason_classes": reason_classes,
            "reject_samples": samples}


# ------------------------------------------------------------- 落库
def resolve_strategy(s, strategy_key: str):
    """既有 expression_strategies_v2 表里按 key 取最新可用版本
    （hypothesis/active）；没有 → None（跳过，不自动建卡）。
    同版本并列时以 id 收尾：两次 run 必须解到**同一行**，否则幂等键
    (strategy_id, version, sha) 在跨会话重跑时会漂到别的行上、重复落库。"""
    from app.models import ExpressionStrategyV2
    return (s.query(ExpressionStrategyV2)
            .filter(ExpressionStrategyV2.strategy_key == strategy_key,
                    ExpressionStrategyV2.status.in_(("hypothesis", "active")))
            .order_by(ExpressionStrategyV2.version.desc(),
                      ExpressionStrategyV2.id.desc())
            .first())


def _persist_one(s, pair: ContrastPair, *, extractor_model: str) -> tuple[str, str]:
    """单对落库（门已过的对才进这里）。返回 (结果, 理由)：
    written / skip_no_strategy / skip_no_segment / skip_no_text_version /
    skip_span_mismatch / skip_dup_sha。"""
    from app.knowledge import verify_instance_span
    from app.models import Segment, StrategyInstance

    st = resolve_strategy(s, pair.strategy_key)
    if st is None:
        return "skip_no_strategy", f"库内无可用策略卡 {pair.strategy_key}"
    seg_id = (pair.meta or {}).get("segment_id")
    if not seg_id:
        return "skip_no_segment", "meta 缺 segment_id（人类侧须对登记段可核）"
    seg = s.query(Segment).filter_by(id=seg_id).first()
    if seg is None:
        return "skip_no_segment", f"段 {seg_id} 不存在"
    text_version = (pair.meta or {}).get("text_version") or ""
    if not text_version:
        return "skip_no_text_version", "meta 缺 text_version（K1-A 来源契约）"
    # 既有门禁照用：human 侧 span 必须对登记段逐字可核——核不上不落
    if not verify_instance_span(seg.text_clean, pair.span_start,
                                pair.span_end, pair.human_text):
        return "skip_span_mismatch", (
            f"span [{pair.span_start},{pair.span_end}) 与段 {seg_id} 原文不符")
    dup = (s.query(StrategyInstance)
           .filter(StrategyInstance.strategy_id == st.id,
                   StrategyInstance.strategy_version == st.version,
                   StrategyInstance.evidence_sha256 == pair.human_sha256)
           .first())
    if dup is not None:
        return "skip_dup_sha", "同 (策略, 版本, human_sha256) 已落行——幂等跳过"
    s.add(StrategyInstance(
        strategy_id=st.id, strategy_version=st.version, work_id=seg.work_id,
        segment_id=seg.id, text_version=text_version,
        span_start=pair.span_start, span_end=pair.span_end,
        evidence_text=pair.human_text, evidence_sha256=pair.human_sha256,
        conditions_observed={
            "protocol": PROTOCOL,
            "strategy_key": pair.strategy_key,
            "op": pair.op,
            "op_label": label_of(pair.op),
            "ai_side_sha256": _sha256(pair.ai_text),
            "ai_side_chars": len(pair.ai_text),
            "scene_keys": sorted(pair.scene_keys),
            "gates": {name: "pass" for name, _ in GATES},
        },
        observed_content=f"{PROTOCOL}:{pair.strategy_key}",
        extractor_model=extractor_model,
        status="proposed"))           # 状态纪律：判定≠升格，落库即 proposed
    return "written", ""


# ------------------------------------------------------------- 旁路账本
def pair_id(pair: ContrastPair) -> str:
    """确定性 pair_id：由协议、策略键与两侧 sha 派生——同输入重跑必得同 id，
    账本可跨 run 对账（同 id 重复出现即同对重交）。"""
    return "k2pair-" + _sha256(
        f"{PROTOCOL}|{pair.strategy_key}|{pair.human_sha256}"
        f"|{_sha256(pair.ai_text)}")[:16]


def ledger_entry(pair: ContrastPair, *, gates_ok: bool, gate_results: dict,
                 reasons: list[str], outcome: str) -> dict:
    """旁路账本一行：完整配对可复核（AI 侧原文在库外留档），不改任何表结构。"""
    return {
        "pair_id": pair_id(pair),
        "protocol": PROTOCOL,
        "strategy_key": pair.strategy_key,
        "op": pair.op,
        "op_label": label_of(pair.op),
        "scene_keys": sorted(pair.scene_keys),
        "span_start": pair.span_start,
        "span_end": pair.span_end,
        "human_text": pair.human_text,
        "human_sha256": pair.human_sha256,
        "ai_text": pair.ai_text,
        "ai_sha256": _sha256(pair.ai_text),
        "ai_side_chars": len(pair.ai_text),
        "gates_ok": gates_ok,
        "gate_results": gate_results,
        "reject_reasons": reasons,
        "persist_outcome": outcome,
    }


def write_pairs_ledger(path: str, entries: list[dict]) -> int:
    """追加写 JSONL 旁路账本（一行一对）。entries 为空不落文件。返回行数。"""
    if not entries:
        return 0
    with Path(path).open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    return len(entries)


def run_contrast(s, pairs: list[ContrastPair], *, live: bool,
                 extractor_model: str = PROTOCOL,
                 ledger_path: str = "k2_pairs.jsonl") -> dict:
    """一次 run：六道门 →（非 live 即回，零库写）→ 过门对逐条落库。

    live 只表示"允许写库"（CLI --live 已过环境变量双闸才到这）；
    本模块自身不联网、不生成 AI 侧——pairs 全部来自调用方注入。
    live 路径同时把每对（含被门拒掉的）完整配对写入 JSONL 旁路账本
    （路径 ledger_path，默认 k2_pairs.jsonl）：AI 侧原文、pair_id、op 与
    S1/S2 两个标签、逐门结果、拒绝理由、落库结果全留档，事后可复核。"""
    rep = {"mode": "live" if live else "dry_run",
           **summarize(pairs), "written": 0, "skipped": {}}
    if not live:
        return rep                    # dry-run：零库写承诺（账本也不写）
    skipped: dict[str, int] = {}
    entries = []
    for p in pairs:
        ok, reasons, detail = _run_gates(p)
        if not ok:
            entries.append(ledger_entry(p, gates_ok=False, gate_results=detail,
                                        reasons=reasons, outcome="gated_out"))
            continue                  # 破形对不落库（理由随账本留档）
        outcome, _why = _persist_one(s, p, extractor_model=extractor_model)
        entries.append(ledger_entry(p, gates_ok=True, gate_results=detail,
                                    reasons=[], outcome=outcome))
        if outcome == "written":
            rep["written"] += 1
        else:
            skipped[outcome] = skipped.get(outcome, 0) + 1
    s.commit()
    rep["skipped"] = skipped
    n = write_pairs_ledger(ledger_path, entries)
    rep["ledger"] = {"path": str(ledger_path), "entries": n}
    return rep


# --------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(
        description="K2 成对对照抽取器（paired_contrast_v2：按构造标注+六道机械门+旁路账本+落库）")
    ap.add_argument("--dry-run", dest="mode", action="store_const",
                    const="dry_run", help="预演（默认）：零库写，只出统计与拒绝样本")
    ap.add_argument("--live", dest="mode", action="store_const", const="live",
                    help="真落库：需环境变量 K2CONTRAST_ALLOW_LIVE=1")
    ap.set_defaults(mode="dry_run")
    ap.add_argument("--pairs-file", default="",
                    help="成对片段 JSON（AI 侧由调用方注入；--live 必填）")
    ap.add_argument("--pairs-ledger", default="k2_pairs.jsonl",
                    help="旁路账本 JSONL 路径（--live 落库时逐对追加完整配对，"
                         "默认 k2_pairs.jsonl）")
    ap.add_argument("--extractor-model", default=PROTOCOL)
    a = ap.parse_args()
    live = a.mode == "live"

    if live and os.environ.get("K2CONTRAST_ALLOW_LIVE") != "1":
        raise SystemExit(
            "--live 需要环境变量 K2CONTRAST_ALLOW_LIVE=1"
            "（fail-closed：未拍板不落库）")
    if live and not a.pairs_file:
        raise SystemExit(
            "--live 需要 --pairs-file（AI 侧片段由调用方注入，本工具不联网生成）")

    pairs = load_pairs_file(a.pairs_file) if a.pairs_file else []
    if not live:
        print(json.dumps({"mode": "dry_run", **summarize(pairs)},
                         ensure_ascii=False, indent=1))
        return
    # 既有 R6 互斥纪律照用（与 k2_extract_backfill 同口径）：写库前持锁，
    # 锁被持有（live 实跑 / pytest 整轮）即 SystemExit，不排队、不重叠。
    from app import db
    from app.live_guard import live_lock
    with live_lock("k2_contrast_extract"):
        db.init_db()
        with db.session() as s:
            rep = run_contrast(s, pairs, live=True,
                               extractor_model=a.extractor_model,
                               ledger_path=a.pairs_ledger)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
