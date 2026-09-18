"""Context Ablation（任务二）：同一 Human/Candidate pair 在三种上下文恢复度下的偏好变化。

mode（上下文供给量递增）：
  segment_only            只有当前段（ = 原实验的评审条件）
  prev1_current           + 前一段
  prev2_current_next1     + 前两段 + 后一段

用途：检验"孤立切段压低 human 自然度分"的仪器伪影假说——
若 human 偏好随上下文恢复而上升、candidate 不升，则伪影成立。
上下文取自原书相邻段（work_id + ordinal），对两边等量供给，盲评 A/B 次序随机化。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .models import Segment

MODES = ("segment_only", "prev1_current", "prev2_current_next1",
         "prev3_current", "scene_context", "compressed_long")


def neighbors(s: Session, seg: Segment) -> dict[str, Segment | None]:
    """同作品按 ordinal 取邻段。"""
    # 必须按切分版本过滤：同库混存 v1/v2 时 ordinal 是两套坐标系（实测串上下文 bug）
    q = s.query(Segment).filter_by(work_id=seg.work_id, seg_version=seg.seg_version or 1)
    by_ord = {x.ordinal: x for x in q.filter(
        Segment.ordinal.in_([seg.ordinal - 2, seg.ordinal - 1, seg.ordinal + 1])).all()}
    return {"prev2": by_ord.get(seg.ordinal - 2), "prev1": by_ord.get(seg.ordinal - 1),
            "next1": by_ord.get(seg.ordinal + 1)}


def window_texts(s: Session, seg: Segment, mode: str) -> list[str]:
    """返回按阅读顺序排列的窗口文本；当前段始终在列。邻段缺失时窗口自动缩短。"""
    nb = neighbors(s, seg)
    if mode == "segment_only":
        seq = [seg]
    elif mode == "prev1_current":
        seq = [nb["prev1"], seg]
    elif mode == "prev2_current_next1":
        seq = [nb["prev2"], nb["prev1"], seg, nb["next1"]]
    else:
        raise ValueError(f"unknown mode: {mode}")
    return [x.text for x in seq if x is not None]


def render_context_block(texts: list[str]) -> str:
    """窗口文本拼成带标注的上下文块。约定：末位是后文仅当窗口长度为 4。"""
    if len(texts) <= 1:
        return texts[0] if texts else ""
    parts = []
    n = len(texts)
    for i, t in enumerate(texts):
        if n >= 4 and i == n - 1:
            tag = "后文"
        elif i == n - 1:
            tag = "当前段"
        else:
            tag = f"上文-{n - 1 - i}"
        parts.append(f"【{tag}】{t}")
    return "\n\n".join(parts)


def scene_context(s: Session, seg: Segment, limit: int = 60) -> tuple[list[str], str]:
    """回溯到场景起点（integrity.scene_boundary==1）的上文序列。

    这是评审规程（calibration-report-v1 §三：human vs candidate 必须供 ≥prev1 上下文）
    的**唯一**实现——前端取题与 Judge 取上下文必须走同一个函数，
    否则"用户带上下文判、Judge 不带"会制造不对称比较（2026-09-14 实测踩到）。

    返回 (texts, mode_label)；mode_label 形如 "scene7"。按 seg_version 过滤，
    避免 v1/v2 两套 ordinal 坐标系串线。
    """
    ver = seg.seg_version or 1
    prevs = (s.query(Segment)
             .filter_by(work_id=seg.work_id, seg_version=ver)
             .filter(Segment.ordinal < seg.ordinal)
             .order_by(Segment.ordinal.desc()).limit(limit).all())
    chosen: list[Segment] = []
    for x in prevs:                      # 从最近一段往回走
        chosen.append(x)
        try:
            if json.loads(x.integrity or "{}").get("scene_boundary") == 1.0:
                break                    # 已到场景开头
        except Exception:
            pass
    chosen.reverse()
    return [x.text for x in chosen], f"scene{len(chosen)}"
