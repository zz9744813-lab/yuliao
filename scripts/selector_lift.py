"""选择器评估：怎样最省人工地抽到"候选胜"的题（2026-09-16）。

**问题**：自然池里候选胜率只有 10–25%，而 κ/AUC 的可靠性主要由少数类决定。
靠纯随机要判上百题才够。之前的 det 特征分层**失败了**（⑥A：先验跨抽样框不可搬运）。

**本脚本回答**：换个选择器会不会更好？候选：
  A. 纯随机（基线）
  B. det 特征打分（之前的分层方案）
  C. **评委自身**：模型说 candidate 且置信度高 → 该题更可能是真候选胜
     （依据：deepseek v4 的 AUC=0.79，即模型的连续信号与用户判定同向）

**关键指标是 precision@K（命中率）**，不是 AUC——因为实际用法是
"拿选择器从待判池里挑 K 题去判"，我们关心的是挑出来那批里有多少真候选胜。

用法：
    python scripts/selector_lift.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as HE  # noqa: E402
import pref_drivers as PD  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
FEATURES = ("punct_！_per_k", "n_sentences", "sent_len_mean",
            "emotion_word_per_k", "sent_len_min", "connective_per_k")


def load_labeled() -> list[dict]:
    """所有"有 det 指标 + 已判 + 有评委记录"的题，带若干种候选打分。"""
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
        rec = {"cid": r["cid"], "y": 1 if w == "candidate" else 0,
               "deltas": json.loads(r["deltas"])}
        rs = json.loads(r["reasons"]) if r["reasons"] else []
        rec["batch"] = next((x[6:] for x in rs if x.startswith("batch_")), None)
        # 评委侧：两版都用，取"最像候选"的那个方向的置信度
        for tag, pv in (("v3", HE.PROMPT_VARIANTS["v3"][1]),
                        ("v4", HE.PROMPT_VARIANTS["v4"][1])):
            for mi, m in enumerate(HE.JUDGES):
                q = con.execute(
                    """select verdict from judge_runs where subject_id=? and judge_kind='preference'
                       and model=? and prompt_version=? order by created_at desc limit 1""",
                    (r["cid"], m, pv)).fetchone()
                d = HE._as_dict(q["verdict"]) if q and q["verdict"] else None
                key = f"j{mi}_{tag}"
                rec[key] = None
                if not d:
                    continue
                wv, cf, h = d.get("winner"), d.get("confidence"), d.get("human_was_a")
                if wv not in ("A", "B") or h is None:
                    continue
                resolved = "human" if ((wv == "A") == h) else "candidate"
                conf = float(cf) if isinstance(cf, (int, float)) else 0.5
                rec[key] = conf if resolved == "candidate" else -conf
        out.append(rec)          # ← 曾经漏掉这行：函数静默返回空 list
    con.close()
    if not out:
        raise SystemExit(
            "查询到 0 条可用样本——分析脚本在空数据上静默跑完比崩溃更糟，故显式报错。\n"
            "检查：库路径、review_items.status、candidates.prompt_version 白名单。")
    return out


def precision_at_k(scores: np.ndarray, ys: np.ndarray, ks: list[int]) -> dict:
    """按 scores 升序（越小越像候选）取前 K，算命中率。"""
    order = np.argsort(scores)
    ys = ys[order]
    out = {}
    for k in ks:
        kk = min(k, len(ys))
        out[k] = float(ys[:kk].mean()) if kk else float("nan")
    return out


def main() -> None:
    recs = load_labeled()
    ys = np.array([r["y"] for r in recs])
    n = len(recs)
    base = ys.mean()
    print(f"已判且有 det 指标：n={n}  候选胜 {int(ys.sum())} 条  基础率={base:.3f}\n")

    # 列 A：纯随机 —— precision@K 期望恒等于基础率
    ks = [10, 20, 30, 50]
    print("=== precision@K（取最像候选的 K 题，命中率越高越省人工）===")
    print(f"{'选择器':28s}" + "".join(f"{'K='+str(k):>9s}" for k in ks))
    print(f"{'A. 纯随机（基线）':28s}" + "".join(f"{base:9.3f}" for _ in ks))

    # 列 B：det 特征打分
    names, X, yu, _ = PD.load()
    idxs = [names.index(k) for k in FEATURES]
    Xs, mu, sd = PD.standardize(X[:, idxs])
    w, b = PD.fit_logreg(Xs, yu, l2=2.0)
    det = np.array([1 / (1 + np.exp(-np.clip(((np.array([r["deltas"][k] for k in FEATURES]) - mu) / sd) @ w + b, -30, 30)))
                    for r in recs])
    p = precision_at_k(-det, ys, ks)     # det 高=像候选 → 取负号使"小=像候选"
    print(f"{'B. det 特征打分（旧方案）':28s}" + "".join(f"{p[k]:9.3f}" for k in ks))

    # 列 C/D：评委方向 × 版本 × 模型
    for key, label in (("j0_v3", "C. kimi v3 的候选倾向"),
                       ("j0_v4", "D. kimi v4 的候选倾向"),
                       ("j1_v3", "E. deepseek v3 的候选倾向"),
                       ("j1_v4", "F. deepseek v4 的候选倾向")):
        vals = [r[key] for r in recs]
        mask = np.array([v is not None for v in vals], dtype=bool)
        if mask.sum() < 10:
            continue
        sc = np.array([v if v is not None else 1e9 for v in vals], dtype=float)
        p = precision_at_k(sc, ys, ks)
        # 只在"有评委记录"的子集上比，才对得起基线
        sub_base = ys[mask].mean()
        row = "".join(f"{p[k]:9.3f}" for k in ks)
        print(f"{label:28s}{row}   （该子集基线 {sub_base:.3f}, n={int(mask.sum())}）")

    print("\n【怎么用】precision@K 就是「判 K 题能拿到几成真候选胜」——直接换算人工成本。")
    print("  例：基线 0.15 时判 30 题得 ~4.5 条少数类；若某选择器能到 0.45，同样 30 题得 ~13.5 条，")
    print("  把达到可信 κ 所需的人工从上百题压到几十题。")

    # 组合选择器：det 与评委同向时更可信？
    print("\n=== 组合：评委说候选 且 det 也高 ===")
    for tag in ("v3", "v4"):
        jkey = f"j1_{tag}"
        vals = [r[jkey] for r in recs]
        mask = np.array([v is not None for v in vals], dtype=bool)
        if mask.sum() < 10:
            continue
        sc = np.array([(v if v is not None else 1e9) + 0.5 * (-d)
                       for v, d in zip(vals, det)], dtype=float)
        p = precision_at_k(sc, ys, [10, 20, 30])
        print(f"  deepseek {tag} + det: " + "  ".join(f"K={k}:{p[k]:.3f}" for k in (10, 20, 30)))


if __name__ == "__main__":
    main()
