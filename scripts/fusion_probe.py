"""融合实验 v2：同子集严格对照（2026-09-16）。

**v1 的三个问题（已修）**：
1. `point_biserial` 返回 `(r, p)`，当标量用了 → TypeError。
2. **各路线的题集不同却直接比**：kimi_v3 覆盖 103 题、kimi_v4 覆盖 120 题，
   于是"v3 0.652 > v4 0.613"这个比较毫无意义（不同题、不同难度）。
   → 本版**强制所有对比落在同一子集**。
3. 融合时塞了 36 列而样本仅 ~110 → 过拟合导致融合反而不如单用。
   → 本版加"极简融合"（少量强特征 + 1~2 个评委平均分）。

**这不是"训练小模型"**：全部是纯 numpy 逻辑回归 / 简单平均，
110×8 规模下毫秒级，无 GPU。

用法：
    python scripts/fusion_probe.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config  # noqa: E402  # SIGNALS 用 DEFAULT_LLM_MODEL（P0 死 id 收敛）

import pref_drivers as PD  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
EXP = "EXP-0911-B82D"
CORE = ("n_sentences", "long_sent_ratio", "punct_！_per_k", "sent_len_mean")
# 只保留两模型都有的两个口径，且**要求所有列齐全**才纳入样本 → 保证同子集
SIGNALS = {
    "kimi_v4": ("judge_preference_v4", "moonshotai/kimi-k3"),
    "ds_v4": ("judge_preference_v4", config.DEFAULT_LLM_MODEL),
    "kimi_v3": ("judge_preference_v3", "moonshotai/kimi-k3"),
    "ds_v3": ("judge_preference_v3", config.DEFAULT_LLM_MODEL),
}


def as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
        except Exception:
            return None
        return d if isinstance(d, dict) else None
    return None


def load() -> tuple[list[str], np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    """要求 **det + 全部 4 个评委信号齐全** 才纳入 → 真正同子集。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    names = PD.load()[0]
    rows = con.execute(
        """select ri.subject_id cid, ri.human_verdict, rd.deltas
           from review_items ri
           join candidates c on c.id = ri.subject_id
           join residuals_det rd on rd.candidate_id = c.id
           where ri.experiment_id = ? and ri.status = 'done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""", (EXP,)).fetchall()
    cids, ys, xs = [], [], []
    jv: dict[str, list[float]] = {k: [] for k in SIGNALS}
    for r in rows:
        hv = as_dict(r["human_verdict"]) or {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        dl = as_dict(r["deltas"])
        if not dl:
            continue
        vals = {}
        for key, (pv, model) in SIGNALS.items():
            # model_any：历史判定写于改名前（model 列存旧 id），只查新名会
            # 静默清空 → 「所有列齐全才纳入」的口径会无声丢光样本（会审实测指出）
            any_ids = config.model_any(model)
            q = con.execute(
                f"""select verdict, abstain from judge_runs
                    where subject_id=? and judge_kind='preference'
                    and model in ({",".join("?" * len(any_ids))})
                    and prompt_version=? order by created_at desc limit 1""",
                (r["cid"], *any_ids, pv)).fetchone()
            if not q or not q["verdict"] or q["abstain"]:
                vals[key] = None
                continue
            d = as_dict(q["verdict"]) or {}
            wv, cf, h = d.get("winner"), d.get("confidence"), d.get("human_was_a")
            if wv not in ("A", "B") or h is None:
                vals[key] = None
                continue
            conf = float(cf) if isinstance(cf, (int, float)) else 0.5
            vals[key] = conf if ((wv == "A") == h) else -conf
        if any(v is None for v in vals.values()):
            continue                     # 缺任一信号 → 剔除，保证同子集
        cids.append(r["cid"])
        ys.append(1 if w == "human" else 0)
        xs.append(np.array([float(dl.get(k, 0.0)) for k in names], dtype=float))
        for k, v in vals.items():
            jv[k].append(v)
    con.close()
    if not cids:
        raise SystemExit("0 条样本——静默跑空比崩溃更糟，显式报错。")
    return cids, np.array(ys), np.array(xs), {k: np.array(v) for k, v in jv.items()}


def auc(y, s):
    n1 = int((y == 1).sum()); n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    o = np.argsort(s); rk = np.empty(len(s)); rk[o] = np.arange(1, len(s) + 1)
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def cv_auc(X, y, folds=5, seed=0, l2=1.0):
    """折内标准化 + L2 逻辑回归 → 折外 AUC。"""
    n = len(y)
    rng = np.random.default_rng(seed)
    parts = np.array_split(rng.permutation(n), folds)
    sc = np.full(n, np.nan)
    for i in range(folds):
        te = parts[i]
        tr = np.concatenate([parts[j] for j in range(folds) if j != i])
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd = np.where(sd < 1e-9, 1.0, sd)
        w, b = PD.fit_logreg((X[tr] - mu) / sd, y[tr], l2=l2)
        z = np.clip(((X[te] - mu) / sd) @ w + b, -30, 30)
        sc[te] = 1 / (1 + np.exp(-z))
    ok = np.isfinite(sc)
    return auc(y[ok], sc[ok])


def rep_multi(X, y, seeds=15, **kw):
    v = np.array([cv_auc(X, y, seed=s, **kw) for s in range(seeds)])
    v = v[np.isfinite(v)]
    return v.mean(), v.min(), v.max(), v.std()


def main() -> None:
    cids, y, Xdet, J = load()
    names = PD.load()[0]
    core = [names.index(k) for k in CORE if k in names]
    nh = int(y.sum())
    print(f"**同子集** n={len(y)}：human {nh} / candidate {len(y)-nh}（人胜率 {y.mean():.3f}）")
    print(f"（要求 det + 4 个评委信号齐全，故比单路线的样本少——但这样才可比）\n")

    print("=== ① 单路线基线（同一批题）===")
    for lab, X in (("确定性特征 32 维", Xdet), ("确定性核心 4 维", Xdet[:, core])):
        m, lo, hi, sd = rep_multi(X, y)
        print(f"  {lab:22s} AUC={m:.3f}  [{lo:.3f},{hi:.3f}] sd={sd:.3f}")
    Xj = np.column_stack([J[k] for k in SIGNALS])
    for i, k in enumerate(SIGNALS):
        print(f"  {'仅 ' + k:22s} AUC={auc(y, J[k]):.3f}   (单列, 无 CV)")

    km = (J["kimi_v4"] + J["ds_v4"]) / 2
    km3 = (J["kimi_v3"] + J["ds_v3"]) / 2
    print(f"  {'两模型平均 ' + 'v4':22s} AUC={auc(y, km):.3f}")
    print(f"  {'两模型平均 ' + 'v3':22s} AUC={auc(y, km3):.3f}")

    print("\n=== ② 融合（关键：特征要少，样本才 100 出头）===")
    cands = {
        "核心4 + 两模型平均v4 (5维)": np.column_stack([Xdet[:, core], km]),
        "核心4 + 4评委列 (8维)": np.column_stack([Xdet[:, core], Xj]),
        "32维 + 两模型平均v4 (33维)": np.column_stack([Xdet, km]),
        "32维 + 4评委列 (36维)": np.column_stack([Xdet, Xj]),
        "仅两模型平均v4 (1维)": km.reshape(-1, 1),
    }
    for lab, X in cands.items():
        m, lo, hi, sd = rep_multi(X, y)
        print(f"  {lab:26s} AUC={m:.3f}  [{lo:.3f},{hi:.3f}] sd={sd:.3f}")

    print("\n=== ③ 确定性特征与评委分的相关性（解释为什么融合不涨）===")
    for k in SIGNALS:
        rs = [abs(PD.point_biserial(Xdet[:, j], J[k])[0]) for j in core]
        print(f"  {k:9s} 与核心4维的最大 |r| = {max(rs):.3f}  (各: "
              + ", ".join(f"{r:.2f}" for r in rs) + ")")
    print(f"  两模型平均v4 与 用户标签 |r| = {abs(PD.point_biserial(km, y)[0]):.3f}")
    for j in core:
        print(f"  {names[j]:18s} 与 用户标签 |r| = "
              f"{abs(PD.point_biserial(Xdet[:, j], y)[0]):.3f}")

    print("\n【读法】")
    print("  · 若「核心4 + 两模型平均」明显高于任一单路线 → 真互补，可落地（无需训练）。")
    print("  · 若融合 ≤ 单用 → 两条路线信息重叠或样本不足以支撑更多维度，**不要硬融**。")
    print("  · 维度越多反而越差，是「样本量 < 维度」的典型征兆，不是「还没调好」。")


if __name__ == "__main__":
    main()
