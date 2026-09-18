"""分层抽样盲评批（2026-09-14）。

**为什么不用纯随机**：r25 纯随机后人类胜率 76%，候选胜只有 24%。
κ 的方差主要由少数类决定——要拿到 10 条候选胜，纯随机需判约 42 条（若人胜率 93.8% 则需约 160 条）。
人工成本不可接受，所以改用**分层抽样：对"候选胜"过采样**。

**怎么分层**：用 `pref_drivers` 的确定性打分模型（6 个 det 特征，逻辑回归，嵌套 CV AUC 0.650）
给候选打分。`score = P(候选胜)`（**方向已断言，见 `_assert_orientation`**）。
实测分箱：

| score 区间 | 候选胜率 |
|---|---|
| ≥0.46 | **0.737** |
| 0.32–0.45 | 0.263 |
| 0.20–0.32 | 0.368 |
| 0.15–0.20 | 0.053 |
| <0.15 | 0.105 |

→ 从高分箱抽样可把候选胜率从 0.316 提到 0.74。

**⚠ 抽样偏差与还原**：本批是**分层抽样，不是无偏样本**，raw κ 有偏。
但每条的**入样概率已知**（n_s/N_s），故把 `stratum` 与 `w`（入样概率）写进 reasons，
分析时可按 w 做**逆概率加权**还原总体 κ。`heldout_eval.py` 支持读取。

用法：
    python scripts/make_stratified_batch.py --dry-run
    python scripts/make_stratified_batch.py --n 30 --tag s30
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db
from app.config import BLIND_REVIEW_PROMPT_VERSIONS
from app.models import Candidate, ReviewItem, Segment

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
EXP = "EXP-0911-B82D"

# 打分模型用的特征（与 scripts/pref_drivers.py 的 top-6 一致）
FEATURES = ("punct_！_per_k", "n_sentences", "sent_len_mean",
            "emotion_word_per_k", "sent_len_min", "connective_per_k")
# 分层边界（score = P(候选胜)）：高分箱候选胜率最高，用于过采样少数类
STRATA = (("S1", 0.45, 1.01), ("S2", 0.20, 0.45), ("S3", -0.01, 0.20))
# 每层分配比例（合计=1.0）：把配额压在候选胜率最高的 S1
ALLOC = {"S1": 0.50, "S2": 0.27, "S3": 0.23}


# ── 打分模型（纯 numpy，沿用 pref_drivers 的实现）────────────────
def _standardize(X):
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    return (X - mu) / sd, mu, sd


def _fit_logreg(X, y, l2=2.0, lr=0.3, iters=3000):
    n, d = X.shape
    w, b = np.zeros(d), 0.0
    for _ in range(iters):
        z = X @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        w -= lr * (X.T @ (p - y) / n + l2 * w / n)
        b -= lr * float((p - y).mean())
    return w, b


def _assert_orientation(scores, y) -> float:
    """断言 score 与"候选胜"同向。

    这里不是形式主义：上一轮我做探索分析时把分箱列名标反了，
    差点按"低分=候选胜"去抽样，会抽到完全相反的样本。
    故把方向做成启动即失败的断言。
    """
    r = float(np.corrcoef(scores, y)[0, 1])
    if r <= 0:
        raise SystemExit(f"打分方向错误：score 与候选胜相关系数 r={r:+.3f}（应为正）。"
                         f"检查 FEATURES 与标签定义。")
    return r


def build_model():
    """在"有 det 指标且已判"的候选上拟合，返回 (predict_fn, r, n_fit)。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select rd.deltas, ri.human_verdict from candidates c
           join residuals_det rd on rd.candidate_id = c.id
           join review_items ri on ri.subject_id = c.id and ri.status = 'done'
           where c.experiment_id = ? and c.status = 'ok'""", (EXP,)).fetchall()
    con.close()
    X, y = [], []
    for r in rows:
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        w_ = hv.get("winner_resolved")
        if w_ not in ("human", "candidate"):
            continue
        d = json.loads(r["deltas"])
        X.append([d[k] for k in FEATURES])
        y.append(1.0 if w_ == "candidate" else 0.0)
    X, y = np.asarray(X, float), np.asarray(y, float)
    Xs, mu, sd = _standardize(X)
    w, b = _fit_logreg(Xs, y)

    def predict(deltas: dict) -> float:
        v = np.array([deltas[k] for k in FEATURES])
        z = ((v - mu) / sd) @ w + b
        return float(1 / (1 + np.exp(-np.clip(z, -30, 30))))

    return predict, _assert_orientation(1 / (1 + np.exp(-(Xs @ w + b))), y), len(y)


def build_batch(s, n: int, tag: str, seed: int, dry_run: bool = False) -> dict:
    predict, r, n_fit = build_model()
    # 候选池：可盲评 + 有 det 指标 + 未判过
    done = {x.subject_id for x in
            s.query(ReviewItem).filter_by(experiment_id=EXP, status="done").all()}
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    pool_rows = con.execute(
        """select c.id cid, rd.deltas from candidates c
           join residuals_det rd on rd.candidate_id = c.id
           where c.experiment_id = ? and c.status = 'ok'""", (EXP,)).fetchall()
    con.close()
    pool = []
    for r_ in pool_rows:
        c = s.get(Candidate, r_["cid"])
        if not c or c.prompt_version not in BLIND_REVIEW_PROMPT_VERSIONS:
            continue
        if c.id in done or not c.text or len(c.text.strip()) < 20:
            continue
        pool.append((c.id, predict(json.loads(r_["deltas"]))))

    # 分层 + 配额
    rng = random.Random(seed)
    picked, plan = [], []
    for name, lo, hi in STRATA:
        members = sorted([c for c, sc in pool if lo <= sc < hi])
        want = max(1, round(n * ALLOC[name]))
        take = min(want, len(members))
        if take:
            picked += [(c, name, take / len(members)) for c in rng.sample(members, take)]
        plan.append({"stratum": name, "range": f"[{lo},{hi})", "pool": len(members),
                     "want": want, "take": take,
                     "inclusion_prob": round(take / len(members), 4) if members else 0.0})
    # 配额没填满（某层池子不够）时，用 S1 余量补齐
    if len(picked) < n:
        used = {c for c, _, _ in picked}
        extra = sorted([(c, sc) for c, sc in pool if c not in used and sc >= STRATA[0][1]],
                       key=lambda x: -x[1])
        need = n - len(picked)
        for c, _ in extra[:need]:
            picked.append((c, "S1", 1.0))
        if need > len(extra):
            print(f"  ⚠ 池子不足：想补 {need} 条，S1 只剩 {len(extra)} 条")

    if not dry_run:
        for cid, name, w in picked:
            ri = s.query(ReviewItem).filter_by(experiment_id=EXP, subject_id=cid).first()
            if ri is None:
                ri = ReviewItem(experiment_id=EXP, subject_type="candidate",
                                subject_id=cid, priority=0.0, status="pending",
                                reasons=["stratified"])
                s.add(ri)
            tags = [x for x in (ri.reasons or []) if not x.startswith("batch_")]
            ri.reasons = tags + [f"batch_{tag}", f"stratum:{name}", f"w:{w:.4f}"]
        s.commit()

    return {"n_fit": n_fit, "score_r": r, "pool": len(pool),
            "picked": picked, "plan": plan, "dry_run": dry_run}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--tag", default="s30")
    ap.add_argument("--seed", type=int, default=20260914)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    with db.session() as s:
        info = build_batch(s, args.n, args.tag, args.seed, args.dry_run)

    print(f"打分模型拟合集 n={info['n_fit']}；score↔候选胜 相关 r={info['score_r']:+.3f}（已断言为正）")
    print(f"候选池（可盲评 + 有 det 指标 + 未判） {info['pool']} 条\n")
    print(f"{'层':6s}{'score 区间':16s}{'池子':>6s}{'计划':>6s}{'实取':>6s}{'入样概率':>10s}")
    for p in info["plan"]:
        print(f"{p['stratum']:6s}{p['range']:16s}{p['pool']:6d}{p['want']:6d}"
              f"{p['take']:6d}{p['inclusion_prob']:10.4f}")
    print(f"\n合计选取 {len(info['picked'])} 条")
    # 预期候选胜数（用观测到的分箱率做粗略外推）
    exp_cw = {"S1": 0.737, "S2": 0.30, "S3": 0.08}
    e = sum(exp_cw[name] for _, name, _ in info["picked"])
    print(f"预期候选胜 ≈ {e:.1f} 条 / 人类胜 ≈ {len(info['picked']) - e:.1f} 条"
          f"（对比纯随机 n={len(info['picked'])} 只能拿约 {0.316 * len(info['picked']):.1f} 条候选胜）")

    if info["dry_run"]:
        print("\ndry-run：未写库")
        return
    print(f"\n已打标签 batch_{args.tag}（含 stratum / w 供逆概率加权还原总体 κ）")
    print(f"前端：http://127.0.0.1:8787/?batch={args.tag}")
    print(f"判完后：python scripts/heldout_eval.py --batch {args.tag}   # 含加权 κ")


if __name__ == "__main__":
    main()
