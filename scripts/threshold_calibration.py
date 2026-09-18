"""阈值校准评估（2026-09-16）。

**动机**：留出集上暴露一个尖锐矛盾——
- deepseek v4 的 AUC=0.790，CI[0.597,0.922]，**显著 > 0.5**（有信息）
- 但它的二值判定 κ 只有 0.130，且**说 candidate 的比例 0.50 而用户只有 0.109**

→ 模型并非没能力，而是**操作点（阈值）严重偏了**：它太愿意判"候选赢"。
→ 那么：**改用它的连续分数 + 校准过的阈值**，κ 能到什么程度？

⚠ 必须折外做。在全部数据上挑最佳阈值 = 过拟合，会给出虚高估计
（本项目已有"特征选择必须放进 CV 折内，否则 AUC 虚高 0.06"的教训）。

用法：
    python scripts/threshold_calibration.py
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


def load_scores() -> list[dict]:
    """每个 (cid, judge, variant) 的连续分数：越大越像 human。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    recs = []
    for r in con.execute(
            """select ri.subject_id cid, ri.human_verdict,
                      c.prompt_version pv_cand
               from review_items ri join candidates c on c.id = ri.subject_id
               where ri.experiment_id = 'EXP-0911-B82D' and ri.status = 'done'
                 and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')"""):
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        rec = {"cid": r["cid"], "y": 1 if w == "human" else 0}
        for mi, m in enumerate(HE.JUDGES):
            for tag in ("v3", "v4"):
                pv = HE.PROMPT_VARIANTS[tag][1]
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
                resolved_human = (wv == "A") == h
                conf = float(cf) if isinstance(cf, (int, float)) else 0.5
                # 有向分：说 human 取 +conf，说 candidate 取 -conf
                rec[key] = conf if resolved_human else -conf
        recs.append(rec)
    con.close()
    if not recs:
        raise SystemExit("0 条样本——静默跑空比崩溃更糟，显式报错。")
    return recs


def main() -> None:
    recs = load_scores()
    ys = np.array([r["y"] for r in recs])
    print(f"已判样本 n={len(recs)}；human={int(ys.sum())} / candidate={int((1-ys).sum())}")
    print(f"用户 human 比例 = {ys.mean():.3f}\n")

    keys = [("j0_v3", "kimi v3"), ("j0_v4", "kimi v4"),
            ("j1_v3", "deepseek v3"), ("j1_v4", "deepseek v4")]

    print("=== ① 原始二值判定（现状）===")
    print(f"{'评委':14s}{'n':>5s}{'说human比例':>12s}{'agreement':>11s}{'κ':>9s}")
    for key, lab in keys:
        sub = [(r["y"], r[key]) for r in recs if np.isfinite(r[key])]
        if not sub:
            continue
        phr = np.mean([1.0 if s > 0 else 0.0 for _, s in sub])
        pairs = [(u == 1, s > 0) for u, s in sub]
        ag = np.mean([u == j for u, j in pairs])
        print(f"{lab:14s}{len(sub):5d}{phr:12.3f}{ag:11.3f}{kappa(pairs):+9.3f}")

    print("\n=== ② 折外阈值校准：只用分数，阈值在训练折上挑 ===")
    print("（阈值候选 = 训练折上所有分数的分位点；在测试折上评估）")
    print(f"{'评委':14s}{'n':>5s}{'折外κ':>9s}{'说human比例':>12s}{'对比原始κ':>11s}")
    rng = np.random.default_rng(11)
    for key, lab in keys:
        sub = [(r["y"], r[key]) for r in recs if np.isfinite(r[key])]
        if len(sub) < 20:
            continue
        idx = rng.permutation(len(sub))
        parts = np.array_split(idx, 5)
        y_out, pred_out = [], []
        for i in range(5):
            te = parts[i]
            tr = np.concatenate([parts[j] for j in range(5) if j != i])
            tr_s = np.array([sub[x][1] for x in tr]); tr_y = np.array([sub[x][0] for x in tr])
            if len(set(tr_y)) < 2:
                continue
            best_t, best_k = 0.0, -9.9
            for t in np.unique(tr_s):
                kk = kappa(list(zip(tr_y == 1, tr_s >= t)))
                if np.isfinite(kk) and kk > best_k:
                    best_k, best_t = kk, t
            for x in te:
                y_out.append(sub[x][0] == 1)
                pred_out.append(sub[x][1] >= best_t)
        pairs = list(zip(y_out, pred_out))
        phr = np.mean([1.0 if p else 0.0 for p in pred_out])
        # 原始二值 κ（同一子集）
        raw = kappa([(sub[x][0] == 1, sub[x][1] > 0) for x in range(len(sub))])
        print(f"{lab:14s}{len(pairs):5d}{kappa(pairs):+9.3f}{phr:12.3f}{raw:+11.3f}")

    print("\n【读法】")
    print("  · 若折外校准 κ 明显高于原始二值 κ → 这是**最便宜的一个改进**：")
    print("    不用改 prompt、不用换模型，只把'二选一'换成'分数+校准阈值'。")
    print("  · 若校准后仍低 → 分数里的信息不足以支撑判别，需换更根本的方案。")
    print("  ⚠ n 小、少数类少，折外挑阈值的方差很大：这里只能看**方向**，别看小数位。")


if __name__ == "__main__":
    main()
