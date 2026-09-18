"""项目内所有主键统一为带前缀的短 ID（沿用 novel-distiller 风格）。"""
from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"  # 12hex：8hex 在 16万行规模会撞 ID


def new_exp_id() -> str:
    return f"EXP-{time.strftime('%m%d')}-{secrets.token_hex(2)}".upper()
