"""缺陷口径校准的鲁棒性检验（2026-09-16）。

上一步得到 kimi 的「缺陷口径 + 折外校准阈值」κ = +0.484（固定规则 +0.050）。
但 n=46、少数类仅 5 条 —— **折外挑阈值在这种规模下方差极大**。

**在下结论前必须做三件事**：
1. **换种子重复**：若 κ 在不同折划分下剧烈波动，那 0.484 就是噪声。
2. **bootstrap CI**：给出区间，看下界是否 >0。
3. **置换零分布**：把标签打乱重跑同一流程，看 κ=0.484 是否超出"纯噪声也能刷出的水平"。
   ⚠ 这一步尤其重要——**折外挑阈值本身就会引入乐观偏差**（在训练折上优选），
   置换检验能量化这个偏差有多大。

用法：
    python scripts/defect_robustness.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as HE  # noqa: E402
from defect_threshold import kappa, load  # noqa: E402


def cv_pipeline(rows: list[dict], seed: int, folds: int = 5) -> tuple:
    """完整流程：折内挑阈值 → 折外评估。返回 (κ, 说human比例)。"""
    n = len(rows)
    if n < folds * 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    parts = np.array_split(order, folds)
    yv, pv = [], []
    for i in range(folds):
        te = parts[i]
        tr = np.concatenate([parts[j] for j in range(folds) if j != i])
        ys = np.array([rows[x]["y"] for x in tr])
        ss = np.array([rows[x]["score"] for x in tr])
        if len(set(ys)) < 2:
            continue
        best_t, best_k = 0.0, -9.9
        for t in np.unique(ss):
            kk = kappa(list(zip(ys == 1, ss >= t)))
            if np.isfinite(kk) and kk > best_k:
                best_k, best_t = kk, t
        for x in te:
            yv.append(rows[x]["y"] == 1)
            pv.append(rows[x]["score"] >= best_t)
    if not yv:
        return float("nan"), float("nan")
    return kappa(list(zip(yv, pv))), float(np.mean([1.0 if q else 0.0 for q in pv]))


def main() -> None:
    rows = load()
    for mi, ml in ((0, "kimi-k3"), (1, "deepseek")):
        sub = [r for r in rows if r["j"] == mi]
        if len(sub) < 20:
            continue
        nmin = sum(1 for r in sub if r["y"] == 0)
        print("=" * 74)
        print(f"### {ml}  n={len(sub)}  少数类={nmin}  "
              f"用户说human={np.mean([r['y'] for r in sub]):.3f}")
        print("=" * 74)

        # ① 换种子重复
        ks = [cv_pipeline(sub, s)[0] for s in range(30)]
        ks = np.array([k for k in ks if np.isfinite(k)])
        print(f"① 换 30 个折划分种子：κ 均值={ks.mean():+.3f}  中位={np.median(ks):+.3f}  "
              f"范围=[{ks.min():+.3f},{ks.max():+.3f}]  标准差={ks.std():.3f}")
        print(f"   → 波动{'很大（结论不可靠）' if ks.std() > 0.15 else '尚可'}")

        # ② 置换零分布：打乱 y 后重跑同一流程
        rng = np.random.default_rng(2026)
        null = []
        ys_all = [r["y"] for r in sub]
        for _ in range(200):
            perm = rng.permutation(len(sub))
            fake = [{"j": r["j"], "cid": r["cid"], "y": ys_all[perm[i]],
                     "score": r["score"]} for i, r in enumerate(sub)]
            v = cv_pipeline(fake, seed=int(rng.integers(1 << 30)))[0]
            if np.isfinite(v):
                null.append(v)
        null = np.array(null)
        obs = ks.mean()
        p = float((null >= obs).mean()) if len(null) else float("nan")
        print(f"② 置换零分布（200 次，标签打乱）：均值={null.mean():+.3f}  "
              f"95 分位={np.percentile(null,95):+.3f}  最大={null.max():+.3f}")
        print(f"   观测 κ={obs:+.3f} → 置换 p = {p:.4f}"
              f"  {'✓ 超出噪声' if p < 0.05 else '✗ 与噪声不可区分'}")
        print("   注：置换零分布的均值不为 0（折内挑阈值带来乐观偏差），"
              "故必须与它比而非与 0 比。\n")


if __name__ == "__main__":
    main()
