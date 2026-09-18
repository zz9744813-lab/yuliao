"""Active Human Review 队列（任务八 v2）。

优先级构成（信息价值高 → 先评）：
  human_upset        对抗评委把 Human 误判成 AI（+30）
  judge_disagreement 三 Judge 方向不一致（×45）
  judge_uncertainty  评委置信度均值低（×20；v1 low_conf 合并进来）
  novel_pattern      det 指标 |z| 异常（×10）
  over_polish        七维自然度 v2 的反向轴高分（+15，v2 存在时）
  judges_abstain     弃权多（+10）
  random_baseline    种子随机抽样保底（priority=25），防止队列全是极端样本

refresh_review_queue()：幂等——已入队的不重复；pending 项重算优先级；
不足 target 时按分数补齐 + 注入 random_baseline。
"""
from __future__ import annotations

import json
import random
import statistics

from sqlalchemy.orm import Session

from .ids import new_id
from .metrics_det import KEY_METRICS
from .models import (Candidate, JudgeRun, ResidualDet, ResidualSem, ReviewItem,
                     Segment)

_DIMS = [k for k in KEY_METRICS if not k.startswith("n_")]


def _norm(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(1.0, (x - lo) / max(hi - lo, 1e-6)))


def _score_candidate(c: Candidate, judges: list[JudgeRun],
                     det_map: dict[str, dict], stats: dict) -> tuple[float, list[str]]:
    """返回 (priority, reasons)。judges = 该 candidate 的全部 JudgeRun。"""
    scores: list[float] = []
    confs: list[float] = []
    abstains = 0
    upset = False
    for jr in judges:
        if jr.abstain:
            abstains += 1
        if jr.confidence is not None:
            confs.append(jr.confidence)
        v = jr.verdict or {}
        if jr.judge_kind == "semantic":
            per = v.get("per_proposition") or []
            if per:
                hit = sum(1 for x in per if x.get("verdict") == "hit")
                scores.append(hit / len(per))
        elif jr.judge_kind == "naturalness":
            s = v.get("score")
            if s is not None:
                scores.append(float(s) / 10.0)
        elif jr.judge_kind == "adversarial":
            hit = v.get("guess_hit_ai")
            if hit is True:
                scores.append(0.3)
            elif hit is False:
                scores.append(0.8)
                upset = True
        elif jr.judge_kind == "naturalness_v2":
            # v2 不进方向分数，但贡献置信度 + over_polish 信号
            op = v.get("over_polish")
            if op is not None and op >= 7:
                pass  # reason 在下面加，分数另算

    disagreement = statistics.pstdev(scores) if len(scores) >= 2 else 0.0
    uncertainty = 1.0 - (sum(confs) / len(confs)) if confs else 0.5

    novelty = 0.0
    m = det_map.get(c.id, {})
    for d in _DIMS:
        if d not in stats:
            continue
        mu, sd = stats[d]
        novelty = max(novelty, abs(m.get(d, 0.0) - mu) / sd)
    novelty = _norm(novelty, 0.0, 3.0)

    over_polish = None
    v2 = next((j for j in judges if j.judge_kind == "naturalness_v2" and j.verdict), None)
    if v2:
        op = (v2.verdict or {}).get("over_polish")
        if op is not None:
            over_polish = float(op)

    reasons: list[str] = []
    priority = 0.0
    if disagreement > 0.25:
        reasons.append(f"judge_disagreement={disagreement:.2f}")
        priority += 45 * _norm(disagreement, 0.0, 0.5)
    if upset:
        reasons.append("human_upset:adversarial 把 Human 误判为 AI")
        priority += 30
    if uncertainty > 0.40:
        reasons.append(f"judge_uncertainty={uncertainty:.2f}")
        priority += 20 * uncertainty
    if novelty > 0.6:
        reasons.append(f"novel_pattern z>{novelty:.2f}")
        priority += 10 * novelty
    if over_polish is not None and over_polish >= 7:
        reasons.append(f"over_polish={over_polish:.0f}")
        priority += 15 * _norm(over_polish, 7.0, 10.0)
    if abstains >= 2:
        reasons.append("judges_abstain>=2")
        priority += 10
    return round(priority, 2), reasons


def _det_stats(det_map: dict[str, dict]) -> dict:
    stats = {}
    for d in _DIMS:
        vals = [m.get(d, 0.0) for m in det_map.values()]
        if vals:
            stats[d] = (statistics.mean(vals), statistics.pstdev(vals) or 1e-6)
    return stats


def refresh_review_queue(s: Session, experiment_id: str, target: int = 300,
                         random_ratio: float = 0.08, seed: int = 20260912) -> dict:
    cands = s.query(Candidate).filter_by(experiment_id=experiment_id, status="ok").all()
    if not cands:
        return {"total": 0}

    det_map: dict[str, dict] = {}
    cand_ids = [c.id for c in cands]
    for rd in s.query(ResidualDet).filter(ResidualDet.candidate_id.in_(cand_ids)).all():
        det_map[rd.candidate_id] = rd.metrics or {}
    stats = _det_stats(det_map)

    judges_by_cand: dict[str, list[JudgeRun]] = {}
    for jr in s.query(JudgeRun).filter_by(experiment_id=experiment_id).all():
        if jr.subject_type == "candidate":
            judges_by_cand.setdefault(jr.subject_id, []).append(jr)

    items = {r.subject_id: r for r in
             s.query(ReviewItem).filter_by(experiment_id=experiment_id).all()}

    # 1) 已入队且 pending 的：重算优先级/理由（done 的不动，保留判定历史）
    refreshed = 0
    scored: dict[str, tuple[float, list[str]]] = {}
    for c in cands:
        scored[c.id] = _score_candidate(c, judges_by_cand.get(c.id, []), det_map, stats)
    for cid, item in items.items():
        if item.status == "pending" and cid in scored:
            p, reasons = scored[cid]
            # random_baseline 是入队时打的采样标签，重算不得弄丢（幂等性 bug）
            if "random_baseline" in (item.reasons or []):
                reasons = (reasons or []) + ["random_baseline"]
                p = max(p, 25.0)
            item.priority = p
            item.reasons = reasons
            refreshed += 1

    # 2) 补齐到 target：按分数降序入队（阈值不再是硬门槛——目标量优先）
    added = 0
    missing = [c for c in cands if c.id not in items]
    missing.sort(key=lambda c: scored[c.id][0], reverse=True)
    rng = random.Random(seed)
    n_random = int(round(target * random_ratio))
    random_picked = {c.id for c in rng.sample(missing, min(n_random, len(missing)))}
    for c in missing:
        if len(items) >= target:
            break
        p, reasons = scored[c.id]
        if c.id in random_picked:
            reasons = (reasons or []) + ["random_baseline"]
            p = max(p, 25.0)
        item = ReviewItem(id=new_id("RV"), experiment_id=experiment_id,
                          subject_type="candidate", subject_id=c.id,
                          priority=p, reasons=reasons, status="pending")
        s.add(item)
        items[c.id] = item
        added += 1
    s.commit()
    pending = sum(1 for i in items.values() if i.status == "pending")
    return {"total": len(items), "pending": pending, "refreshed": refreshed, "added": added}


def build_review_queue(session: Session, experiment_id: str) -> int:
    """旧入口（stage 用）：等价于 refresh 到 300。"""
    return refresh_review_queue(session, experiment_id, target=300)["total"]
