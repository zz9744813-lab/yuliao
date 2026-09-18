"""项目内所有主键统一为带前缀的短 ID（沿用 novel-distiller 风格）。"""
from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"  # 12hex：8hex 在 16万行规模会撞 ID


def new_exp_id() -> str:
    # 2026-09-18：hex 从 2 字节提到 4 字节。原来 65536 的号池在一次测试套件
    # （~15 个 create_experiment）里就有 ~0.16% 的生日碰撞率，实测撞过一次
    # （UNIQUE constraint failed: experiments.id）——偶发红一次最坑排查。
    return f"EXP-{time.strftime('%m%d')}-{secrets.token_hex(4)}".upper()
