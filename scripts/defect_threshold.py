"""缺陷口径的阈值校准（2026-09-16）。

**为什么做这个**：缺陷检测口径的 AUC = 0.897，CI[0.758,1.000]（kimi），
是本项目**第一个明确显著**的信号。但它的二值 κ 只有 +0.085——
因为「缺陷多者输」这个**预先固定**的规则太粗：模型说 candidate 的比例 0.76，
而用户只有 0.109。

AUC 高 + κ 低 = 典型的**阈值失准**。所以：把「缺陷数之差」当连续分，
在训练折上挑阈值，在测试折上评估。

⚠ 必须折外。在全部数据上挑最佳阈值 = 过拟合（本项目已有"特征选择要放进折内"的教训）。
⚠ 少数类只有 5 条：折外挑阈值的方差极大，**只看方向，别看小数位**。

用法：
    python scripts/defect_threshold.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as HE  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"


def kappa(pairs):
    n = len(pairs)
    if not n:
        return float("nan")
    a = sum(1 for u, j in pairs if u and j)
    b = sum(1 for u, j in pairs if u and not j)
    c = sum(1 for u, j in pairs if not u and j)
    d = sum(1 for u, j in pairs if not u and not j)
    po = (a + d) / n
    pu = (a + b) / n
    pj = (a + c) / n
    pe = pj * pu + (1 - pj) * (1 - pu)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def load() -> list[dict]:
    u25 = {x["cid"]: x["user"] for x in HE.load_items(True, "r25", HE.BATCH_EPOCH)}
    u30 = {x["cid"]: x["user"] for x in HE.load_items(True, "s30", None)}
    user = {**u25, **u30}
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for cid, uw in user.items():
        for mi, m in enumerate(HE.JUDGES):
            q = con.execute(
                """select verdict, abstain from judge_runs where subject_id=? and judge_kind='preference'
                   and model=? and prompt_version=? order by created_at desc limit 1""",
                (cid, m, HE.PROMPT_VARIANTS["defect"][1])).fetchone()
            if not q or not q["verdict"] or q["abstain"]:
                continue
            d = HE._as_dict(q["verdict"])
            if not d or "n_defects_a" not in d:
                continue
            h = d.get("human_was_a")
            na = d.get("n_defects_a") or 0
            nb = d.get("n_defects_b") or 0
            own, opp = (na, nb) if h else (nb, na)   # own=人类段缺陷数
            out.append({"j": mi, "cid": cid, "y": 1 if uw == "human" else 0,
                        "score": float(opp - own),
                        "total": na + nb})
    con.close()
    if not out:
        raise SystemExit("0 条样本——静默跑空比崩溃更糟，显式报错。")
    return out


def cv_threshold(rows: list[dict], seed: int = 4, folds: int = 5):
    """折外挑阈值：返回 (折外κ, 折外说human比例, 原始规则κ, 原始规则说human比例)。"""
    n = len(rows)
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
    # 原始固定规则：score > 0（缺陷少者胜）
    raw = [(r["y"] == 1, r["score"] > 0) for r in rows]
    return (kappa(list(zip(yv, pv))), np.mean([1.0 if q else 0.0 for q in pv]),
            kappa(raw), np.mean([1.0 if q else 0.0 for _, q in raw]))


def main() -> None:
    rows = load()
    nmin = sum(1 for r in rows if r["y"] == 0)
    print(f"样本 n={len(rows)}（human {len(rows)-nmin} / candidate {nmin}）\n")
    print(f"{'评委':12s}{'n':>5s}{'固定规则κ':>11s}{'固定说human':>12s}"
          f"{'折外校准κ':>11s}{'校准后说human':>13s}")
    for mi, ml in ((0, "kimi-k3"), (1, "deepseek")):
        sub = [r for r in rows if r["j"] == mi]
        if len(sub) < 10:
            continue
        k_cal, phr_cal, k_raw, phr_raw = cv_threshold(sub)
        print(f"{ml:12s}{len(sub):5d}{k_raw:+11.3f}{phr_raw:12.3f}{k_cal:+11.3f}{phr_cal:13.3f}")

    # 参照：用户比例
    print(f"\n  参照：用户说 human 的比例 = {np.mean([r['y'] for r in rows]):.3f}")
    print("\n【读法】")
    print("  · 若折外校准 κ 明显高于固定规则的 κ → **缺陷口径的用法应该是分数+阈值，")
    print("    而不是'缺陷多者输'这个硬规则**。这是可以直接落地的改动。")
    print("  · ⚠ 少数类极少，折外挑阈值方差大；要坐实需要更多候选胜的题。")
    print("  · ⚠ 循环性提示：缺陷**类型词表**来自集霸在这批题上的标注，")
    print("    故存在轻度循环（比 v4 用胜负标签推导要轻，但需明示）。")


if __name__ == "__main__":
    main()
