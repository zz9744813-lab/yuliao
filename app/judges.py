"""三个互相独立的 Judge。全部禁看模型名/来源，只收匿名文本。

- semantic   对 Frame+命题：事实/缺失/新增/矛盾/确定性/信息边界（不论文笔）
- naturalness 盲评单篇：像不像成熟作者写的（不给原文对照）
- adversarial 匿名 A/B（Human vs Candidate 随机位）：猜哪个是 AI + 暴露自身偏差
"""
from __future__ import annotations

import json
import random
from typing import Any

from .gateway import LLMError, chat
from .prompt_render import render
from .residual_sem import parse_llm_json

SEMANTIC_PROMPT_VERSION = "judge_semantic_v1"
NATURALNESS_PROMPT_VERSION = "judge_naturalness_v1"
ADVERSARIAL_PROMPT_VERSION = "judge_adversarial_v1"

SEMANTIC_PROMPT = """你是 SemanticVerifier。你只判断语义，绝不评论文笔/文采。

给你：语义骨架（Frame）、Human 原文被拆成的原子命题、一段候选文本。

逐条判定候选文本对每个原子命题的支持状态：
- hit：表达了
- missing：没有表达
- contradicted：与命题冲突
- altered：意思变形（记录怎么变了）

同时还要输出：
- additions：候选多出来的、命题之外的信息
- info_boundary_breach：候选是否说出了 Frame 里 must_not_state / reader_should_infer 里"不该直说"的信息
- confidence: 0~1
- abstain: 若文本太短/损坏/语言不通，true 并把 confidence 记 0

只输出 JSON：
{
  "per_proposition": [{"id": "P1", "verdict": "hit|missing|contradicted|altered", "note": ""}],
  "additions": [],
  "info_boundary_breach": false,
  "confidence": 0.0,
  "abstain": false
}

Frame：
«{frame_json}»

命题：
«{propositions_json}»

候选文本：
«{candidate}»"""

NATURALNESS_PROMPT = """你是一名中文小说编辑。下面给你一篇匿名文本，你只评"自然度"。

评分维度（合成 0~10 分）：
- 语感：像不像成熟中文小说作者的行文
- 句式：是否有机械解释、过度完整、模板腔、书面腔
- 节奏：句子长短安排是否自然
- 克制：是否有过度解释、修饰堆砌、情绪直接命名堆砌

只输出 JSON：
{"score": 0.0, "issues": ["问题标签"], "confidence": 0.0, "abstain": false}

拿不准就 abstain=true。不要评情节好坏，不评信息量。

文本：
«{text}»"""

ADVERSARIAL_PROMPT = """你是 AdversarialVerifier。下面有两段匿名文本，来自同一段中文小说的不同写法。

你的任务只有两个：
1. 猜哪一段更像 AI 生成（A / B / 无法判断）
2. 主动检查自己会不会犯错：列出这次判断可能依赖的偏差（长度偏好/华丽偏好/解释完整性偏好/对白偏好）

只输出 JSON：
{
  "guess_ai": "A|B|unknown",
  "why": "一句话理由",
  "bias_check": ["本次可能影响判断的偏差"],
  "confidence": 0.0,
  "abstain": false
}

文本 A：
«{text_a}»

文本 B：
«{text_b}»"""


def _judge_call(prompt: str, purpose: str, version: str, model: str) -> dict[str, Any]:
    try:
        r = chat(
            model=model,
            system="你是独立评审模块。只输出 JSON，解释留在 JSON 里。",
            user=prompt,
            purpose=purpose,
            prompt_version=version,
            temperature=0.2,
            max_tokens=2500,
        )
    except LLMError as e:
        return {"status": "failed", "error": str(e)}
    payload = parse_llm_json(r.text)
    if payload is None:
        return {"status": "failed_parse", "raw": r.text[:400]}
    return {"status": "ok", "payload": payload}


def judge_semantic(*, frame_json: str, propositions: list[dict], candidate_text: str,
                   model: str) -> dict:
    out = _judge_call(
        render(SEMANTIC_PROMPT, frame_json=frame_json,
               propositions_json=json.dumps(propositions, ensure_ascii=False),
               candidate=candidate_text),
        purpose="judge_semantic", version=SEMANTIC_PROMPT_VERSION, model=model,
    )
    if out.get("status") == "ok":
        p = out["payload"]
        return {**out, "verdict": p, "confidence": p.get("confidence"), "abstain": bool(p.get("abstain"))}
    return out


def judge_naturalness(*, text: str, model: str) -> dict:
    out = _judge_call(
        render(NATURALNESS_PROMPT, text=text),
        purpose="judge_naturalness", version=NATURALNESS_PROMPT_VERSION, model=model,
    )
    if out.get("status") == "ok":
        p = out["payload"]
        return {**out, "verdict": p, "confidence": p.get("confidence"),
                "score": p.get("score"), "abstain": bool(p.get("abstain"))}
    return out


NATURALNESS_V2_PROMPT_VERSION = "judge_naturalness_v2"

# 任务六：单一"自然度"拆成七维。over_polish 是反向轴（分越高越"AI 作文感"），
# 汇总时不得与其他六维平均，单独呈现。
NATURALNESS_V2_PROMPT = """你是中文小说文本质量评审。对下面这段文字按七个维度打分（1~10 整数），每维配一句证据。

维度定义：
- local_fluency 句内通顺：语法、搭配、标点顺畅，孤立可读
- novelistic_naturalness 小说感：像小说行文，不像说明文/梗概/剧本
- contextual_fit 语境贴合：称谓、语气、信息密度与场景匹配
- narrative_efficiency 叙事效率：字数花在刀刃上，不注水不跳步
- implicitness 含蓄度：情绪态度少贴标签，多靠动作与细节外显
- voice_authenticity 声腔真实：有作者/人物独特口吻，不是通用腔
- over_polish 打磨过度（反向轴）：分数越高越像"光滑均匀的 AI 作文"，正常文本应低分

只输出 JSON：
{"local_fluency": 7, "novelistic_naturalness": 7, "contextual_fit": 7,
 "narrative_efficiency": 7, "implicitness": 7, "voice_authenticity": 7,
 "over_polish": 3, "confidence": 0.8, "evidence": "一句话总证"}

待评文本：
«{text}»"""


def judge_naturalness_v2(*, text: str, model: str) -> dict:
    out = _judge_call(
        render(NATURALNESS_V2_PROMPT, text=text),
        purpose="judge_naturalness_v2", version=NATURALNESS_V2_PROMPT_VERSION, model=model,
    )
    if out.get("status") == "ok":
        p = out["payload"]
        return {**out, "verdict": p, "confidence": p.get("confidence"),
                "abstain": bool(p.get("abstain"))}
    return out


def judge_adversarial(*, human_text: str, candidate_text: str, model: str,
                      rng: random.Random) -> dict:
    """human_position / guess_hit_ai 只进库不上 prompt，保证匿名。"""
    human_a = rng.random() < 0.5
    a_text, b_text = (human_text, candidate_text) if human_a else (candidate_text, human_text)
    out = _judge_call(
        render(ADVERSARIAL_PROMPT, text_a=a_text, text_b=b_text),
        purpose="judge_adversarial", version=ADVERSARIAL_PROMPT_VERSION, model=model,
    )
    if out.get("status") == "ok":
        p = out["payload"]
        ai_slot = "B" if human_a else "A"
        guess = p.get("guess_ai")
        return {
            **out,
            "verdict": p,
            "confidence": p.get("confidence"),
            "abstain": bool(p.get("abstain")),
            "human_position": "A" if human_a else "B",
            "guess_hit_ai": (guess == ai_slot) if guess in ("A", "B") else None,
        }
    return out


# ── Preference-task Judge（Phase 1.5 §十 v1 修正）────────────────────────────
# 背景：adversarial judge 判"哪边是 AI"，用户判"哪边更好"——口径错位使 agreement ≈ 抛硬币。
# 本 Judge 让模型做**与用户完全相同的任务**（A/B 哪边更好），再算 agreement 才有意义。
PREFERENCE_PROMPT_VERSION = "judge_preference_v4"

PREFERENCE_PROMPT = """你是中文小说编辑。A、B 是同一场景下同一段语义的两种写法。

{context_block}

请判断哪一段**写得更好**。判据是"成熟人类小说作者的文本质量"：
- 语感自然，不像 AI 作文腔
- 情绪靠动作、细节、留白外显，而不是贴标签解释
- 叙事效率高：不注水、不过度解释、不把话说尽
- 节奏与句群呼吸自然，长短句安排有起伏
- 潜台词与信息显隐得当
- 与上文接得上：指代、引语、场景、语气是否延续（若给了上文）

**本项目的具体偏好（重要，来自作者本人的盲评统计）**：
作者偏好**紧凑而流畅**的文字，具体表现为——
- **句子少而长**：同样一段意思，宁可少几句、每句写长，而不是用一堆短句碎句堆砌
- **连接词少**：不要靠"于是/因此/然而/而"这类词把逻辑关系讲满，让意思自己接上
- **整体不啰嗦**：该收就收，不为了写满而加内容
- 长句占比高是**优点**，不是缺点；不要因为"句子短、读着轻松"就给高分

反向提醒（防止误判）：
- 不要因为"信息更全/更清楚"就判更好
- 不要因为"辞藻更华丽"就判更好
- 不要因为"更长"就判更好；也不要因为"更短"就判更好——看的是**句子的组织方式**，不是字数
- 不要因为 A/B 里某段"更自足、更像独立成篇"就判更好——
  两段都是小说中的**一段**，本就该依赖上文；能独立读懂不是优点
- 两段差不多就选 equal；都不行选 both_bad；确实无法判断选 cant_judge

reasons 字段规则（重要）：
- 每条必须**以 winner 或 loser 开头**指代是谁的问题，不要用 A/B 指代——
  否则事后无法判断你在说哪一段。
- 例：["winner:动作外显情绪", "loser:情绪贴标签", "loser:过度解释"]
- 用你前面维度表里的词，2~6 字一条，3~5 条即可。

只输出 JSON：
{"winner": "A|B|equal|both_bad|cant_judge", "confidence": 0.0,
 "reason": "一句话理由", "reasons": ["winner:…", "loser:…"]}

文本 A：
«{text_a}»

文本 B：
«{text_b}»"""

# 用户的判定词汇 → 本 Judge 的判定词汇（用于 agreement 对齐）
_DECISIVE = ("human", "candidate")

# ── 归档变体：留出集对照专用 ──────────────────────────────────
# `scripts/heldout_eval.py` 需要在**同一批留出题**上同时跑新旧口径，才能回答
# "rubric 到底有没有用"。而 v3 的 prompt 没有版本控制可回溯（仓库无 git），
# 故在此归档原文。**只读**——正常流程请用 PREFERENCE_PROMPT / PREFERENCE_PROMPT_VERSION。
PREFERENCE_PROMPT_V3_ARCHIVE = """你是中文小说编辑。A、B 是同一场景下同一段语义的两种写法。

{context_block}

请判断哪一段**写得更好**。判据是"成熟人类小说作者的文本质量"：
- 语感自然，不像 AI 作文腔
- 情绪靠动作、细节、留白外显，而不是贴标签解释
- 叙事效率高：不注水、不过度解释、不把话说尽
- 节奏与句群呼吸自然，长短句安排有起伏
- 潜台词与信息显隐得当
- 与上文接得上：指代、引语、场景、语气是否延续（若给了上文）

反向提醒（防止误判）：
- 不要因为"信息更全/更清楚"就判更好
- 不要因为"辞藻更华丽"就判更好
- 不要因为"更长"或"更短"就判更好
- 不要因为 A/B 里某段"更自足、更像独立成篇"就判更好——
  两段都是小说中的**一段**，本就该依赖上文；能独立读懂不是优点
- 两段差不多就选 equal；都不行选 both_bad；确实无法判断选 cant_judge

reasons 字段规则（重要）：
- 每条必须**以 winner 或 loser 开头**指代是谁的问题，不要用 A/B 指代——
  否则事后无法判断你在说哪一段。
- 例：["winner:动作外显情绪", "loser:情绪贴标签", "loser:过度解释"]
- 用你前面维度表里的词，2~6 字一条，3~5 条即可。

只输出 JSON：
{"winner": "A|B|equal|both_bad|cant_judge", "confidence": 0.0,
 "reason": "一句话理由", "reasons": ["winner:…", "loser:…"]}

文本 A：
«{text_a}»

文本 B：
«{text_b}»"""

# variant 名 → (prompt, 落库用的 prompt_version)
# ── 缺陷检测口径（judge_defect_v1，2026-09-16）────────────────────
# **为什么换任务**：留出与全量诊断给出的一致结论是——
#   · 「整段哪边更好」这个整体偏好任务上，评委的可判别性 AUC ≈ 0.50（n=99，少数类 36），
#     即**没有可测信息**；连 v4 在自身推导来源的数据上也只有 0.48–0.54。
#   · 而集霸**亲自标注的 19 处噪点**里，类型高度集中（解释过度 11 / 用词 5），
#     且 17/19 指向候选侧——说明"指出缺陷位置"是他能稳定完成的任务。
# → 于是把评委的任务从「整体偏好」改成「**指缺陷**」：给出具体片段 + 类型，
#   胜负由缺陷计数导出。计数规则**预先固定**（缺陷少者胜；相等为 equal），
#   不允许事后调参，否则又是过拟合。
# 缺陷类型沿用集霸自己的词表，避免口径漂移。
DEFECT_KINDS = ("解释过度", "用词", "情绪直给", "节奏", "逻辑", "意象", "其他")

PREFERENCE_PROMPT_DEFECT = """你是中文小说编辑。A、B 是同一场景下同一段语义的两种写法。

{context_block}

**不要判断哪一段整体更好。** 只做一件事：找出每一段里**具体的、可以指出位置的缺陷**。

缺陷类型（只能用这些词）：
- 解释过度：把本该读者自己体会的意思写出来（贴标签、把话说尽、替读者总结）
- 用词：词不达意、搭配生硬、用力过猛的词
- 情绪直给：直接陈述情绪（"她很愤怒"），而不是靠动作细节外显
- 节奏：句子长短安排失衡、拖沓或碎断
- 逻辑：因果不清、前后矛盾、动作链断裂
- 意象：比喻生硬、堆砌、与场景不搭
- 其他

对 A、B 分别列出缺陷。**每条必须引用原文片段**（10~30 字，从文中原样摘抄）。
没找到缺陷就留空数组——**不要为了凑数硬找**，也不要因为某段"更短/更长"就判它有缺陷。

只输出 JSON：
{"A": [{"kind": "解释过度", "quote": "原文片段", "why": "10字内理由"}],
 "B": [{"kind": "情绪直给", "quote": "原文片段", "why": "10字内理由"}]}

文本 A：
«{text_a}»

文本 B：
«{text_b}»"""


def _defect_winner(payload: dict) -> tuple[str | None, int, int]:
    """由缺陷计数导出胜负。规则**预先固定**：缺陷少者胜，相等为 equal。

    只统计合法 kind 且带非空 quote 的条目——防止模型用空条目凑数。
    返回 (winner_in_AB, nA, nB)；winner 为 "A"/"B"/"equal"。
    """
    valid = set(DEFECT_KINDS)

    def cnt(side: str) -> int:
        items = payload.get(side) or []
        if not isinstance(items, list):
            return 0
        return sum(1 for it in items
                   if isinstance(it, dict) and it.get("kind") in valid
                   and str(it.get("quote") or "").strip())

    na, nb = cnt("A"), cnt("B")
    if na < nb:
        return "A", na, nb
    if nb < na:
        return "B", na, nb
    return "equal", na, nb


PROMPT_VARIANTS = {
    "v3": (PREFERENCE_PROMPT_V3_ARCHIVE, "judge_preference_v3_heldout"),
    "v4": (PREFERENCE_PROMPT, "judge_preference_v4_heldout"),
    # 缺陷检测口径：换任务而非调措辞（依据见上方块注释）
    "defect": (PREFERENCE_PROMPT_DEFECT, "judge_defect_v1_heldout"),
}

_NO_CONTEXT = "（无上文，两段都是独立给出的片段）"


def judge_preference(*, human_text: str, candidate_text: str, model: str,
                     rng: random.Random, context: str | None = None,
                     variant: str | None = None,
                     force_human_a: bool | None = None) -> dict:
    """匿名 A/B 偏好判定，与用户盲评任务完全一致。

    context：**必须**传用户判该题时看到的同一份上文（走 context_ablation.scene_context）。
    不传 = 退化为 segment_only 条件，而 calibration-report-v1 §三 已证该条件有严重仪器偏差
    （human 1/10 vs 带 prev1 时 7/10）——会造成"用户带上下文判、Judge 空手判"的不对称比较。

    variant：留出集对照用，取 PROMPT_VARIANTS 的键（"v3"/"v4"）。None = 正式口径。

    force_human_a：强制人类段放 A 还是 B。**位置偏差校正专用**（2026-09-16）——
    实测评委选 A 率高达 0.74–0.80，同一题正反两序各跑一次再合并可把位置偏差抵消掉。
    None = 用 rng 随机（默认行为，保持匿名）。

    human_was_a / winner_resolved 只进库不上 prompt，保证匿名。
    winner_resolved ∈ {human, candidate, equal, both_bad, cant_judge}。
    """
    prompt, version = (PROMPT_VARIANTS[variant] if variant
                       else (PREFERENCE_PROMPT, PREFERENCE_PROMPT_VERSION))
    human_a = rng.random() < 0.5 if force_human_a is None else bool(force_human_a)
    a_text, b_text = (human_text, candidate_text) if human_a else (candidate_text, human_text)
    ctx_block = f"【上文】{context}" if context else _NO_CONTEXT
    out = _judge_call(
        render(prompt, context_block=ctx_block, text_a=a_text, text_b=b_text),
        # 必须用算出来的 version，不能写死正式口径——否则 v3 的调用会被记到 v4 名下，
        # 成本与审计按 prompt_version 聚合时会静默错账。
        purpose="judge_preference", version=version, model=model,
    )
    if out.get("status") == "ok":
        p = out["payload"]
        if variant == "defect":
            # 缺陷口径的输出结构不同：没有 winner 字段，胜负由缺陷计数导出。
            # 计数规则预先固定（见 _defect_winner），不做事后调参。
            w, na, nb = _defect_winner(p)
            p = {**p, "winner": w, "n_defects_a": na, "n_defects_b": nb}
            conf = None      # 该口径不产出整体置信度；用计数差当强度信号
        else:
            w = p.get("winner")
        if w == "A":
            resolved = "human" if human_a else "candidate"
        elif w == "B":
            resolved = "candidate" if human_a else "human"
        elif w in ("equal", "both_bad", "cant_judge"):
            resolved = w
        else:
            resolved = None
        return {
            **out,
            "verdict": p,
            "winner_resolved": resolved,
            "human_was_a": human_a,
            "confidence": p.get("confidence"),
            # abstain 只表示"评委拒绝表态"，equal/both_bad 是表态但非二选一，两者都不进 agreement 分母
            "abstain": bool(p.get("abstain")) or w in (None, "cant_judge"),
        }
    return out
