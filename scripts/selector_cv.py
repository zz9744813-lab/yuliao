"""选择器评估（2026-09-16，交叉验证版）。

**上一版有两个致命问题，这里修掉**：
1. **循环**：det 打分模型在全部已判数据上拟合、又在同一批数据上评估 →
   precision@10 虚高到 0.80。必须**折外评估**（沿用 MEMORY.md 的纪律：
   特征/模型选择必须放进 CV 折内）。
2. **抽样框混淆**：det 选择器在 144 行（含优先队列）上评，评委选择器在 41 行上评，
   **两者不是同一个总体**，数字不可比。

**真正要回答的问题**：下一批要从未判的**自然池**里抽，用哪个选择器命中率最高？
→ 所以必须在**自然池**上、**折外**评估。

用法：
    python scripts/selector_cv.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as HE  # noqa: E402
import pref_drivers as PD  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
FEATURES = ("punct_！_per_k", "n_sentences", "sent_len_mean",
            "emotion_word_per_k", "sent_len_min", "connective_per_k")
NATURAL_BATCHES = ("r25", "s30")


def load() -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.subject_id cid, ri.reasons, ri.human_verdict, rd.deltas
           from review_items ri
           join candidates c on c.id = ri.subject_id
           join residuals_det rd on rd.candidate_id = c.id
           where ri.experiment_id = 'EXP-0911-B82D' and ri.status = 'done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""").fetchall()
    out = []
    for r in rows:
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        dl = json.loads(r["deltas"])
        rs = json.loads(r["reasons"]) if r["reasons"] else []
        batch = next((x[6:] for x in rs if x.startswith("batch_")), None)
        rec = {"cid": r["cid"], "y": 1 if w == "candidate" else 0,
               "nat": batch in NATURAL_BATCHES,
               "det": np.array([dl[k] for k in FEATURES], dtype=float)}
        for tag, pv in (("v3", HE.PROMPT_VARIANTS["v3"][1]),
                        ("v4", HE.PROMPT_VARIANTS["v4"][1])):
            for mi, m in enumerate(HE.JUDGES):
                q = con.execute(
                    """select verdict from judge_runs where subject_id=? and judge_kind='preference'
                       and model=? and prompt_version=? order by created_at desc limit 1""",
                    (r["cid"], m, pv)).fetchone()
                d = HE._as_dict(q["verdict"]) if q and q["verdict"] else None
                key = f"j{mi}_{tag}"
                rec[key] = np.nan
                if not d:
                    continue
                wv, cf, h = d.get("winner"), d.get("confidence"), d.get("human_was_a")
                if wv not in ("A", "B") or h is None:
                    continue
                ok = ((wv == "A") == h)
                conf = float(cf) if isinstance(cf, (int, float)) else 0.5
                rec[key] = -conf if ok else conf     # 越小 = 越像候选胜
        out.append(rec)
    con.close()
    if not out:
        raise SystemExit("0 条样本——分析脚本静默跑空比崩溃更糟，故显式报错。")
    return out


def cv_precision(recs: list[dict], score_fn, ks: list[int], seed: int = 0,
                 folds: int = 5) -> dict:
    """折外 precision@K：每折用其余折拟合打分函数，在该折内取 top-K。

    score 约定：**越小越像候选胜**（与"取 top-K"一致）。
    """
    n = len(recs)
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    parts = np.array_split(order, folds)
    hits = {k: 0 for k in ks}
    tot = {k: 0 for k in ks}
    for i in range(folds):
        te = parts[i]
        tr = np.concatenate([parts[j] for j in range(folds) if j != i])
        sc = score_fn([recs[x] for x in tr], [recs[x] for x in te])
        y = np.array([recs[x]["y"] for x in te])
        s = np.asarray(sc, dtype=float)
        good = np.isfinite(s)
        s, y = s[good], y[good]
        o = np.argsort(s)
        y = y[o]
        for k in ks:
            kk = min(k, len(y))
            if kk:
                hits[k] += int(y[:kk].sum())
                tot[k] += kk
    return {k: (hits[k] / tot[k] if tot[k] else float("nan")) for k in ks}


def det_scorer(tr, te):
    """在 tr 上拟合 det 逻辑回归，返回 te 的"越像候选越小"的分数。"""
    Xtr = np.array([r["det"] for r in tr]); ytr = np.array([r["y"] for r in tr])
    if len(set(ytr)) < 2:
        return np.zeros(len(te))
    Xs, mu, sd = PD.standardize(Xtr)
    w, b = PD.fit_logreg(Xs, ytr, l2=2.0)
    Xte = (np.array([r["det"] for r in te]) - mu) / sd
    p = 1 / (1 + np.exp(-np.clip(Xte @ w + b, -30, 30)))
    return -p          # 候选倾向高 → 分数小


def judge_scorer(key: str):
    def f(tr, te):
        return np.array([r[key] for r in te], dtype=float)
    return f


def main() -> None:
    recs = load()
    nat = [r for r in recs if r["nat"]]
    pri = [r for r in recs if not r["nat"]]
    ks = [10, 20, 30]

    print("=== 样本构成（重要：两个框不可混评）===")
    print(f"  优先队列 n={len(pri)}  候选胜率={np.mean([r['y'] for r in pri]):.3f}")
    print(f"  自然池   n={len(nat)}  候选胜率={np.mean([r['y'] for r in nat]):.3f}")
    print("\n  → 下一批要从未判的**自然池**抽，故选择器必须在自然池上评估。\n")

    for name, pool in (("自然池（决定下一批用哪个选择器）", nat),
                       ("优先队列（仅供对照）", pri)):
        ys = np.array([r["y"] for r in pool])
        base = ys.mean()
        print(f"=== {name} n={len(pool)} 基线={base:.3f} ===")
        print(f"{'选择器':30s}" + "".join(f"{'K='+str(k):>10s}" for k in ks))
        print(f"{'A. 纯随机':30s}" + "".join(f"{base:10.3f}" for _ in ks))
        r = cv_precision(pool, det_scorer, ks, seed=1)
        print(f"{'B. det 特征（折外）':30s}" + "".join(f"{r[k]:10.3f}" for k in ks))
        for key, lab in (("j0_v3", "C. kimi v3"), ("j0_v4", "D. kimi v4"),
                         ("j1_v3", "E. deepseek v3"), ("j1_v4", "F. deepseek v4")):
            sub = [x for x in pool if np.isfinite(x[key])]
            if len(sub) < 15:
                continue
            sb = np.mean([x["y"] for x in sub])
            r = cv_precision(sub, judge_scorer(key), ks, seed=1)
            print(f"{lab:30s}" + "".join(f"{r[k]:10.3f}" for k in ks)
                  + f"   (子集基线 {sb:.3f}, n={len(sub)})")
        print()

    print("【读法】折外 precision@K 与基线之比 = 省人工的倍数。")
    print("  ⚠ 自然池只有少数类和少量样本，@K 的方差很大；这里只做**方向性**判断，")
    print("    不要把某一行的小数点当真值——要选就选「明显且多 K 一致」的那一列。")


if __name__ == "__main__":
    main()
