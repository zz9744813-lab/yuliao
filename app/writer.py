"""Writer 接口骨架（Phase 2 地基）：Previous Prose + SemanticFrame → Next Prose。

设计口径（与 Calibration Report v2 / Phase 1.5 指令一致）：
- 输入形态显式建模 2×2 因子，不设隐式默认值：
    frame_only        → B 形态（reconstruct_v1）
    context_only      → D 形态（recon_ctxonly_v1）
    context_and_frame → C 形态（recon_ctx_v1）
- writer_input_mode 是实验变量：本模块只提供接口，不改任何默认工程行为，
  不接入 api.py（是否成为产品默认由跨语料 2×2 数据 + 集霸拍板决定）。
- prompt 版本沿用已标定的三个 prompt_version，杜绝另起炉灶造成口径漂移。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from . import config
from . import context_ablation
from .context_ablation import MODES as CONTEXT_MODES  # noqa: F401  （口径引用）
from .gateway import chat
from .prompts_ctx import CTXONLY_SYSTEM, CTXONLY_USER, RECON_CTX_SYSTEM, RECON_CTX_USER
from .prompt_render import render
from .reconstruct import RECON_PROMPT_VERSION, build_reconstruct_user

WriterMode = Literal["frame_only", "context_only", "context_and_frame"]

PV_C, PV_D = "recon_ctx_v1", "recon_ctxonly_v1"


@dataclass
class WriterRequest:
    mode: WriterMode
    prev2: str | None = None       # 前文-2（更远一段）
    prev1: str | None = None       # 前文-1（紧邻待写位置的一段）
    frame: dict[str, Any] | None = None   # SemanticFrame payload（M/L）

    def validate(self) -> None:
        if self.mode == "frame_only" and not self.frame:
            raise ValueError("frame_only 需要 frame")
        if self.mode == "context_only" and not (self.prev1 or self.prev2):
            raise ValueError("context_only 需要 prev1/prev2 至少一段")
        if self.mode == "context_and_frame" and not (self.frame and (self.prev1 or self.prev2)):
            raise ValueError("context_and_frame 需要 frame 和 prev1/prev2")


@dataclass
class WriterResult:
    text: str
    mode: WriterMode
    model: str
    prompt_version: str
    meta: dict[str, Any] = field(default_factory=dict)


def build_prompt(req: WriterRequest) -> tuple[str, str]:
    """(system, user)。C/D 形态与 2×2 实验生成路径共用 app/prompts_ctx 正典模板。"""
    if req.mode == "frame_only":
        return RECON_CTX_SYSTEM, build_reconstruct_user(req.frame)
    prev2 = req.prev2 or "（无）"
    prev1 = req.prev1 or "（无）"
    if req.mode == "context_only":
        return CTXONLY_SYSTEM, render(CTXONLY_USER, prev2=prev2, prev1=prev1)
    return RECON_CTX_SYSTEM, render(RECON_CTX_USER, prev2=prev2, prev1=prev1,
                                    frame_json=json.dumps(req.frame, ensure_ascii=False, indent=2))


PROMPT_VERSION_BY_MODE: dict[WriterMode, str] = {
    "frame_only": RECON_PROMPT_VERSION,
    "context_only": PV_D,
    "context_and_frame": PV_C,
}


def write(req: WriterRequest, model: str = config.DEFAULT_LLM_MODEL,
          temperature: float = 0.7, max_tokens: int = 3000) -> WriterResult:
    """调 gateway 生成一段。异常向上抛，调用方决定重试策略。"""
    req.validate()
    system, user = build_prompt(req)
    pv = PROMPT_VERSION_BY_MODE[req.mode]
    r = chat(model=model, system=system, user=user,
             purpose=f"writer:{req.mode}", prompt_version=pv,
             temperature=temperature, max_tokens=max_tokens)
    return WriterResult(text=r.text.strip(), mode=req.mode, model=model,
                        prompt_version=pv,
                        meta={"tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                              "latency_ms": r.latency_ms})
