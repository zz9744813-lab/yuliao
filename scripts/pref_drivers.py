"""集霸偏好驱动因子分析（2026-09-14）。

问题：集霸凭什么判"哪边更好"？能不能用**确定性指标**（零 LLM、可复现）解释甚至预测？

做法：
1. 单变量筛查：29 个 `residuals_det` 指标的 delta（候选 − 人类）与"集霸是否挑候选"
   做点二列相关，用 Benjamini-Hochberg 控制 FDR（29 个指标必然出假阳性）。
2. 多变量：对 top-k 特征拟合 L2 逻辑回归（纯 numpy 实现，不引入 sklearn），
   5 折交叉验证算 AUC。
3. 对照组：同一套流程也用于预测"评委是否挑候选"，看两者可预测性差多少。
4. 基线：只用"长度"能不能预测？排除"其实只是长度效应"的解释。

用法：
    python scripts/pref_drivers.py
    python scripts/pref_drivers.py --topk 5
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
EXP = "EXP-0911-B82D"
JUDGE_PV = "judge_preference_v3"
JUDGE = "moonshotai/kimi-k3"


# ── 数据装载 ────────────────────────────────────────────────
def load(exp: str | None = None) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray]:
    """返回 (指标名, X_delta, y_user, y_judge)。

    exp=None = **全部实验**。2026-09-17 之前这里写死 B82D，于是新增的跨语料判定
    （mix30/nq50/x50/qfresh）全被挡在门外——而那正是少数类从 47 涨到 60 的来源。
    """
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if exp:
        rows = con.execute(
            """select c.id cid, rd.deltas, ri.human_verdict
               from candidates c
               join residuals_det rd on rd.candidate_id = c.id
               join review_items ri on ri.subject_id = c.id and ri.status='done'
               where c.experiment_id = ?""", (exp,)).fetchall()
    else:
        rows = con.execute(
            """select c.id cid, rd.deltas, ri.human_verdict
               from candidates c
               join residuals_det rd on rd.candidate_id = c.id
               join review_items ri on ri.subject_id = c.id and ri.status='done'""").fetchall()
    X, yu, yj, names = [], [], [], None
    for r in rows:
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        u = hv.get("winner_resolved")
        if u not in ("human", "candidate"):
            continue
        j = con.execute(
            """select verdict from judge_runs where subject_id=? and judge_kind='preference'
               and prompt_version=? and model=? order by created_at desc limit 1""",
            (r["cid"], JUDGE_PV, JUDGE)).fetchone()
        jr = None
        if j and j["verdict"]:
            try:
                v = json.loads(j["verdict"])
                if isinstance(v, dict):
                    jr = v.get("winner_resolved")
            except Exception:
                pass
        d = json.loads(r["deltas"])
        if names is None:
            names = list(d.keys())
        X.append([d[k] for k in names])
        yu.append(1.0 if u == "candidate" else 0.0)
        yj.append(np.nan if jr not in ("human", "candidate")
                  else (1.0 if jr == "candidate" else 0.0))
    con.close()
    return names, np.asarray(X, float), np.asarray(yu, float), np.asarray(yj, float)


# ── 统计工具（纯 numpy）─────────────────────────────────────
def point_biserial(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """点二列相关 + 双尾 p（用 t 近似）。y 为 0/1。"""
    ok = ~np.isnan(x)
    x, y = x[ok], y[ok]
    n = len(x)
    if n < 5 or len(set(y.tolist())) < 2:
        return 0.0, 1.0
    r = float(np.corrcoef(x, y)[0, 1])
    if not np.isfinite(r) or abs(r) >= 1.0:
        return r, 1.0
    t = r * np.sqrt((n - 2) / (1 - r * r))
    # 双尾 p，正态近似（n 足够大时够用）
    p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))))
    return r, p


def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg 校正后的 q 值。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = min(prev, pvals[i] * m / (rank + 1))
        q[i] = val
        prev = val
    return q


def standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    return (X - mu) / sd, mu, sd


def fit_logreg(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
               lr: float = 0.3, iters: int = 3000) -> tuple[np.ndarray, float]:
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(iters):
        z = X @ w + b
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        w -= lr * (X.T @ (p - y) / n + l2 * w / n)
        b -= lr * float((p - y).mean())
    return w, b


def auc(y: np.ndarray, s: np.ndarray) -> float:
    """秩和法算 AUC。"""
    ok = ~np.isnan(s)
    y, s = y[ok], s[ok]
    n1 = int((y == 1).sum())
    n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s))
    ranks[order] = np.arange(1, len(s) + 1)
    # 处理并列：平均秩
    s_sorted = s[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def cv_auc(X: np.ndarray, y: np.ndarray, k: int = 5, seed: int = 20260914,
           l2: float = 1.0) -> float:
    ok = ~np.isnan(y)
    X, y = X[ok], y[ok]
    n = len(y)
    if n < 20 or len(set(y.tolist())) < 2:
        return float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.permutation(n)
    folds = np.array_split(idx, k)
    scores = np.full(n, np.nan)
    for i in range(k):
        te = folds[i]
        tr = np.concatenate([folds[j] for j in range(k) if j != i])
        Xtr, mu, sd = standardize(X[tr])
        w, b = fit_logreg(Xtr, y[tr], l2=l2)
        scores[te] = ((X[te] - mu) / sd) @ w + b
    return auc(y, scores)


def nested_cv_auc(X: np.ndarray, y: np.ndarray, names: list[str], k: int = 5,
                  topk: int = 5, l2: float = 2.0, seed: int = 20260914
                  ) -> tuple[float, dict[str, int]]:
    """外层 CV 内部做特征选择——**必须这样**。

    先在全量数据上按 |r| 选 top-k、再对同一份数据做 CV，是典型的选择泄漏：
    实测把集霸侧 AUC 从 0.650 抬到 0.713（虚高 0.06）。折内选特征才是无偏估计。
    返回 (auc, {特征: 被选中的折数})。
    """
    ok = ~np.isnan(y)
    X, y = X[ok], y[ok]
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(y))
    folds = np.array_split(idx, k)
    scores = np.full(len(y), np.nan)
    cnt: dict[str, int] = {}
    for i in range(k):
        te = folds[i]
        tr = np.concatenate([folds[j] for j in range(k) if j != i])
        rs = [(m,) + point_biserial(X[tr, j], y[tr]) for j, m in enumerate(names)]
        rs.sort(key=lambda x: -abs(x[1]))
        sel = [names.index(r[0]) for r in rs[:topk]]
        for s in sel:
            cnt[names[s]] = cnt.get(names[s], 0) + 1
        Xtr, mu, sd = standardize(X[tr][:, sel])
        w, b = fit_logreg(Xtr, y[tr], l2=l2)
        scores[te] = ((X[te][:, sel] - mu) / sd) @ w + b
    return auc(y, scores), cnt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--l2", type=float, default=2.0)
    ap.add_argument("--exp", default=None,
                    help="只跑某实验；默认**全部实验**（2026-09-17 起，之前写死 B82D）")
    args = ap.parse_args()

    names, X, yu, yj = load(args.exp)
    n = len(yu)
    print(f"样本 n={n}（有 det 残差 + 集霸判定）；评委侧有效 {int((~np.isnan(yj)).sum())}")
    print(f"集霸挑 candidate 比例 = {yu.mean():.3f}")
    print(f"指标数 = {len(names)}\n")

    print("=== ① 单变量筛查：delta(候选−人类) vs 集霸挑候选（BH-FDR 校正）===")
    res = []
    for i, k in enumerate(names):
        r, p = point_biserial(X[:, i], yu)
        res.append((k, r, p))
    qs = bh_fdr([x[2] for x in res])
    res = [(k, r, p, q) for (k, r, p), q in zip(res, qs)]
    res.sort(key=lambda x: -abs(x[1]))
    print(f"{'指标':24s}{'r':>8s}{'p':>9s}{'q(FDR)':>10s}{'':>4s}")
    for k, r, p, q in res[:12]:
        flag = "  ★" if q < 0.10 else ""
        print(f"{k:24s}{r:+8.3f}{p:9.4f}{q:10.4f}{flag}")
    sig = [x for x in res if x[3] < 0.10]
    print(f"\n  FDR q<0.10 的指标数：{len(sig)} / {len(names)}")

    print("\n=== ② 多变量：折内选特征 + L2 逻辑回归，嵌套 5 折 CV ===")
    print("（注意：先全量选特征再 CV 是选择泄漏，实测会把 AUC 虚高 ~0.06，故用嵌套 CV）")
    auc_user, cnt_u = nested_cv_auc(X, yu, names, topk=args.topk, l2=args.l2)
    print(f"\n  预测「集霸挑候选」：AUC = {auc_user:.3f}")
    print(f"  预测「评委挑候选」：AUC = "
          f"{nested_cv_auc(X, yj, names, topk=args.topk, l2=args.l2)[0]:.3f}")
    print("\n  各折稳定入选的特征（被选中折数 / 5）：")
    for k_, v in sorted(cnt_u.items(), key=lambda x: -x[1]):
        print(f"    {v}/5  {k_}")

    print("\n=== ③ 置换检验（真实 AUC 是否高于打乱标签的零分布）===")
    rng = np.random.default_rng(999)
    null = np.array([nested_cv_auc(X, yu[rng.permutation(len(yu))], names,
                                   topk=args.topk, l2=args.l2)[0] for _ in range(20)])
    null = null[np.isfinite(null)]
    p_perm = float((null >= auc_user).mean())
    print(f"  零分布（20 次打乱）：mean={null.mean():.3f} sd={null.std():.3f} max={null.max():.3f}")
    print(f"  真实 {auc_user:.3f} → p={p_perm:.3f}，距零均值 "
          f"{(auc_user - null.mean()) / null.std():.2f} sd"
          f"  → {'显著' if p_perm < 0.05 else '不显著'}")

    print("\n=== ④ 基线对照（无特征选择，普通 CV）===")
    for label, cols in (("仅长度 n_chars", ["n_chars"]),
                        ("仅句长 sent_len_mean", ["sent_len_mean"]),
                        ("长度+句长", ["n_chars", "sent_len_mean"])):
        ii = [names.index(c) for c in cols]
        a_u = cv_auc(X[:, ii], yu, l2=args.l2)
        a_j = cv_auc(X[:, ii], yj, l2=args.l2)
        print(f"  {label:22s} 集霸 AUC={a_u:.3f}   评委 AUC={a_j:.3f}")

    print("\n判读：集霸侧 AUC 显著高于零分布 ⇒ 集霸的偏好**有稳定的可测结构**，廉价 RM 可行；"
          "\n      评委侧接近零分布 ⇒ 评委的取舍无法用表面指标解释（它另有判据，或含较多噪声）。")


if __name__ == "__main__":
    main()
