"""语义级 Residual（LLM 结构分析）+ 原子命题分解。

命题分解（propositions）先行：Human 原文拆成 P1..Pn 原子语义点，
之后 SemanticVerifier / residual 都以它判定 missing / added / contradicted。
"""
from __future__ import annotations

import json
import re

from . import config
from .gateway import LLMError, chat
from .prompt_render import render

PROPOSITIONS_PROMPT_VERSION = "propositions_v1"

PROPOSITIONS_PROMPT = """把下面这段中文小说拆成原子级语义命题（atomic propositions）。

要求：
- 每条不可再分（一个事实/一个状态/一个推断）
- 用中性语言表述，不复制原文措辞
- 目标 5~14 条：每个事件、动作、状态变化、可推断的心理/关系信息都应单独成条
- 标注类型：fact（明说了）/ infer（读者可推）/ atmosphere（氛围/情绪基调）

只输出 JSON：
{"propositions": [{"id": "P1", "type": "fact", "text": "..."}]}

原文：
«{text}»"""

SEM_RESIDUAL_PROMPT_VERSION = "sem_residual_v1"

SEM_RESIDUAL_PROMPT = """你是中文小说语义对照分析员。给你三个输入：骨架（Frame）、原文（Human）、候选文（Candidate）。
任务：找出 Candidate 相对 Human 的**语义差异**（不评好坏，只列事实）。

输出 JSON：
{
  "missing": ["命题 id 或描述：Candidate 丢掉的骨架/原文信息"],
  "added": ["Candidate 多出的信息"],
  "contradicted": ["与骨架/原文冲突处"],
  "certainty_shift": 0,
  "explicitness_delta": 0,
  "subtext_preserved": 0.5,
  "pov_consistent": true,
  "info_boundary_breach": false,
  "tags": ["差异类型，从候选清单选：显式心理/情绪命名/过度解释/冗余/修饰膨胀/连接词堆叠/对话解释/时序挤压/留白丢失/节奏扁平/POV漂移/具身动作缺失/环境代理缺失/口头说明替换动作/其他"],
  "notes": "一句话最重要观察"
}

说明：
- certainty_shift: -2(更含蓄) ~ +2(更武断)，整数
- explicitness_delta: -2(更隐) ~ +2(更显)
- subtext_preserved: 0~1
- info_boundary_breach: Candidate 是否说出了原文不许说的信息

骨架：
«{frame_json}»

原文：
«{human}»

候选：
«{candidate}»"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_llm_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text.strip())
    m = _JSON_RE.search(text)
    if not m:
        try:
            return json.loads(text)
        except Exception:
            return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def extract_propositions(text: str) -> dict:
    try:
        r = chat(
            model=config.STRONG_MODEL,
            system="你只输出 strict JSON。",
            user=render(PROPOSITIONS_PROMPT, text=text),
            purpose="propositions",
            prompt_version=PROPOSITIONS_PROMPT_VERSION,
            temperature=0.2,
            max_tokens=2500,
        )
    except LLMError:
        return {"status": "failed", "propositions": []}
    payload = parse_llm_json(r.text)
    if not payload or "propositions" not in payload:
        return {"status": "failed_parse", "propositions": [], "raw": r.text[:400]}
    props = payload.get("propositions") or []
    return {"status": "ok", "propositions": props, "raw": r.text[:400]}


def semantic_residual(*, frame_json: str, human_text: str, candidate_text: str) -> dict:
    try:
        r = chat(
            model=config.STRONG_MODEL,
            system="你是严谨的语义核对员，只输出 JSON。",
            user=render(SEM_RESIDUAL_PROMPT,
                        frame_json=frame_json, human=human_text, candidate=candidate_text),
            purpose="sem_residual",
            prompt_version=SEM_RESIDUAL_PROMPT_VERSION,
            temperature=0.2,
            max_tokens=2000,
        )
    except LLMError:
        return {"status": "failed", "payload": None}
    payload = parse_llm_json(r.text)
    if payload is None:
        return {"status": "failed_parse", "payload": None, "raw": r.text[:400]}
    # 兜底补键，避免下游 KeyError
    payload.setdefault("missing", [])
    payload.setdefault("added", [])
    payload.setdefault("contradicted", [])
    payload.setdefault("tags", [])
    payload.setdefault("certainty_shift", 0)
    payload.setdefault("explicitness_delta", 0)
    payload.setdefault("subtext_preserved", None)
    payload.setdefault("pov_consistent", None)
    payload.setdefault("info_boundary_breach", None)
    payload.setdefault("notes", "")
    return {"status": "ok", "payload": payload}
