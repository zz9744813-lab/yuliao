"""prompt 模板渲染：占位符 {name} 朴素替换，不解析其他花括号。

为什么不用 str.format：中文 JSON 示例里大量 { } 会让 format 抛 KeyError。
规则：模板作者只须保证**占位符名字唯一**（如 {text} / {frame_json}），其余花括号随便用。
"""
from __future__ import annotations


def render(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items():
        out = out.replace("{" + k + "}", str(v))
    return out
