"""多模型多采样重建。Generator 只看 Frame + 表达约束，绝不看 Human 原文。"""
from __future__ import annotations

from .frames_schema import frame_prompt_text

RECON_PROMPT_VERSION = "reconstruct_v1"

RECON_PROMPT = """你是一名中文小说写作者。下面给你一个语义骨架（SemanticFrame）和表达约束，请你据此写一段小说文本。

要求：
- 必须表达骨架中的全部事件与意图
- 不得超过 skeleton 给出的信息表演（读者应当推断的，你不要写明）
- 遵守 expression_constraints（explicitness / psychological_explanation_allowed / dialogue_allowed / rhythm_target）
- 不要提纲挈领，不要解释自己
- 直接写正文，长度 2~6 句，不要分段不要标题

骨架：
«{frame_json}»

直接输出正文。"""

REPAIR_PROMPT = """你上一次输出的内容是：
«{raw}»

它不是合法 JSON，或不符合 schema：{error}

请修正为**只含 JSON** 的输出（不要解释、不要代码块标记）。"""


def build_reconstruct_user(frame_payload: dict) -> str:
    from .prompt_render import render
    return render(RECON_PROMPT, frame_json=frame_prompt_text(frame_payload))
