"""SemanticFrame 三种粒度的 Pydantic 契约。S ⊂ M ⊂ L。

设计原则（与 SemanticFrame Calibration Lab 目标一致）：
- 模块级字段全部可选，但校验记录缺失数，供抽取质量统计
- Frame 是用来"约束语义而不约束语言"的：字段只允许描述意思/读者/禁区，不允许写句子
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Granularity = Literal["S", "M", "L"]


class Fact(BaseModel):
    statement: str
    certainty: Literal["certain", "likely", "speculative"] = "certain"
    source: str | None = None  # 谁认为/谁说的，方便检查“谁可以知道”


class CharacterState(BaseModel):
    visible_emotion: str | None = None
    hidden_emotion: str | None = None
    goal: str | None = None
    intention: str | None = None      # 此刻想干什么
    knows: list[str] = Field(default_factory=list)      # 知道的事
    does_not_know: list[str] = Field(default_factory=list)  # 此时还不知道的事


class ExpressionConstraints(BaseModel):
    explicitness: Literal["low", "mid", "high"] = "low"
    psychological_explanation_allowed: bool = False
    dialogue_allowed: bool = True
    narration_distance: str | None = None  # close / mid / far / 自由
    rhythm_target: str | None = None


class FrameS(BaseModel):
    granularity: Literal["S"] = "S"
    event: str                       # 一句话客观主线
    intention: str | None = None     # 说话/动作意图
    reader_effect: str | None = None # 希望读者感受到什么


class FrameM(FrameS):
    granularity: Literal["M"] = "M"
    facts: list[Fact] = Field(default_factory=list)
    character_state: CharacterState = Field(default_factory=CharacterState)
    reader_should_infer: list[str] = Field(default_factory=list)
    must_not_state: list[str] = Field(default_factory=list)   # 不允许直说
    expression_constraints: ExpressionConstraints = Field(default_factory=ExpressionConstraints)


class FrameL(FrameM):
    granularity: Literal["L"] = "L"
    beats: list[str] = Field(default_factory=list)         # 动作/事件顺序
    pauses: list[str] = Field(default_factory=list)        # 停在哪
    emotion_intensity: float | None = None                 # 0~1
    observation_focus: str | None = None                   # 视线/感知集中在哪
    information_focus: str | None = None                   # 信息焦点放在哪个细节上
    dialogue_intent: str | None = None
    pov: str | None = None
    rhythm: str | None = None                              # 呼吸/节奏提示（描述，不给句子）


MODEL_BY_GRAN: dict[str, type[BaseModel]] = {"S": FrameS, "M": FrameM, "L": FrameL}


_NULL_LIST_FIELDS = ("facts", "reader_should_infer", "must_not_state", "beats", "pauses")
_NULL_CS_LISTS = ("knows", "does_not_know")


def _as_list(v):
    """观测到的"字符串冒充列表"收敛：分号/换行拼接 → 数组；单句 → 单元素数组。

    样本里模型常把 reader_should_infer 写成 "A；B；C" 一整句，语义就是列表，
    按 ；/;/换行 切开是无损的；没有分隔符就当单元素。
    """
    if isinstance(v, str):
        parts = [p.strip(" ；;\t") for p in v.replace("\r", "\n").split("\n")]
        parts = [q for p in parts for q in p.split("；") if q.strip()]
        parts = [q for p in parts for q in p.split(";") if q.strip()]
        return [p.strip() for p in parts if p.strip()]
    return v


def _unwrap_wrapper(payload: dict) -> dict:
    """观测到的单键外壳：{"frame_m": {...}} → {...}。仅当外壳内是含 event 的 dict。"""
    if len(payload) == 1:
        (k, v), = payload.items()
        if isinstance(v, dict) and "event" in v:
            return v
    return payload


def normalize_frame_payload(granularity: str, payload: dict) -> dict:
    """契约宽容层：只做观测到的、语义保持的确定性归一。

    依据（2026-09-14 M-gap 根因，四语料失败 M/L 实测）：
    1) facts[] 字段别名 fact(43)/content(14)：内容合格，只是没按 schema 命名；
    2) 显式 null 代替缺省列表（does_not_know: null 等）：prompt 教模型
       「拿不准就留 null」，但 pydantic 对显式 null 不走 default_factory；
    3) 字符串冒充列表（knows/reader_should_infer/must_not_state 等 22 行）：
       分号拼接的整句 → 切成数组；
    4) 单键外壳 frame_m: {...}（4 行）→ 拆壳。
    不做有损改写（如 character_state 按角色拆成数组的 6 行，取谁的态都算编造，
    留给 V2 prompt 的单角色 schema 约束去预防）。别名清单以失败样本实测为准。
    """
    if not isinstance(payload, dict):
        return payload
    payload = _unwrap_wrapper(payload)
    if granularity in ("M", "L"):
        fixed_lists = {k: [] for k in _NULL_LIST_FIELDS if payload.get(k) is None}
        if fixed_lists:
            payload = {**payload, **fixed_lists}
        for k in _NULL_LIST_FIELDS:
            v = payload.get(k)
            if isinstance(v, str):
                payload = {**payload, k: _as_list(v)}
        cs = payload.get("character_state")
        if cs is None:
            payload = {**payload, "character_state": {}}
        elif isinstance(cs, dict):
            cs_fixed = {}
            for k in _NULL_CS_LISTS:
                v = cs.get(k)
                if v is None:
                    cs_fixed[k] = []
                elif isinstance(v, str):
                    cs_fixed[k] = _as_list(v)
            if cs_fixed:
                payload = {**payload, "character_state": {**cs, **cs_fixed}}
    if granularity in ("M", "L") and isinstance(payload.get("facts"), list):
        fixed = []
        for x in payload["facts"]:
            if isinstance(x, dict) and "statement" not in x:
                for alias in ("content", "fact"):
                    if isinstance(x.get(alias), str):
                        x = {**x, "statement": x[alias]}
                        break
            fixed.append(x)
        payload = {**payload, "facts": fixed}
    return payload


def validate_frame(granularity: str, payload: dict) -> tuple[BaseModel | None, str | None]:
    """(model_or_none, error_or_none)。供抽取层判 pass / repair。

    校验前先过 normalize_frame_payload（已知别名归一），失败样本里
    大部分是字段改名而非内容问题，不该为此烧一次 LLM repair。
    """
    cls = MODEL_BY_GRAN.get(granularity)
    if cls is None:
        return None, f"unknown granularity {granularity!r}"
    payload = {**normalize_frame_payload(granularity, payload), "granularity": granularity}
    try:
        return cls.model_validate(payload), None
    except ValidationError as e:
        return None, e.errors().__repr__()[:500]


def frame_prompt_text(payload: dict) -> str:
    """给 Generator 看的 frame 表示。统一 JSON，模型输入与入库一致，杜绝二次解释。"""
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)
