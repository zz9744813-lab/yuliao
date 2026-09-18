"""口径对比：整体偏好 vs 缺陷检测（2026-09-16）。

**背景**：本项目此前一直在调「整段哪边更好」这个整体偏好任务的口径（v3→v4），
但大样本诊断（n=99、少数类 36）给出 **AUC ≈ 0.50**——评委在该任务上没有可测信息。

于是换任务：让评委只做「指缺陷」，胜负由缺陷计数导出（规则预先固定）。
缺陷类型沿用集霸自己标注的 19 处噪点所用词表
（解释过度 11 / 用词 5 / 其他 3，其中 17/19 指向候选侧）。

**本脚本把三个口径放在同一批留出题上比**：
  v3（无 rubric 的整体偏好）／v4（有 rubric 的整体偏好）／defect（缺陷检测）

指标一律用 **κ + AUC**（不用 agreement——基础率偏斜下它不可比）。

用法：
    python scripts/defect_compare.py
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

VARIANTS = (("v3", "整体偏好·无rubric"), ("v4", "整体偏好·有rubric"),
            ("defect", "缺陷检测"))


def collect() -> dict:
    u25 = {x["cid"]: x["user"] for x in HE.load_items(True, "r25", HE.BATCH_EPOCH)}
    u30 = {x["cid"]: x["user"] for x in HE.load_items(True, "s30", None)}
    user = {**u25, **u30}
    return user


def judge_result(cid: str, model: str, pv: str, con) -> dict | None:
    """取一条判定：返回 {human: bool, score: float|None, resolved: str}。"""
    r = con.execute(
        """select verdict, abstain from judge_runs where subject_id=? and judge_kind='preference'
           and model=? and prompt_version=? order by created_at desc limit 1""",
        (cid, model, pv)).fetchone()
    if not r or not r["verdict"] or r["abstain"]:
        return None
    d = HE._as_dict(r["verdict"])
    if not d:
        return None
    w = d.get("winner_resolved")
    if w not in ("human", "candidate"):
        return None
    # 连续分：整体偏好用有向 confidence；缺陷口径用「对手缺陷数 − 己方缺陷数」当强度
    if "n_defects_a" in d:
        h = d.get("human_was_a")
        na, nb = d.get("n_defects_a") or 0, d.get("n_defects_b") or 0
        # ⚠ 方向必须算对：own = **人类段**的缺陷数，opp = 候选段的。
        #   分数 = opp − own（人类段缺陷越少 → 分越高 → 越该判人类胜）。
        #   我第一版把 own/opp 写反了，得到 AUC=0.069 这种荒谬低值才发现
        #   ——低到近乎"完美反向"，正是符号写反的典型征兆。
        own, opp = (na, nb) if h else (nb, na)
        score = float(opp - own)
    else:
        conf = d.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.5
        score = conf if w == "human" else -conf
    return {"human": w == "human", "score": score}


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


def auc(pairs):
    y = np.array([1 if u else 0 for u, _ in pairs])
    s = np.array([sc for _, sc in pairs], dtype=float)
    n1 = int((y == 1).sum()); n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    o = np.argsort(s); rk = np.empty(len(s)); rk[o] = np.arange(1, len(s) + 1)
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def boot_ci(vals, fn, B=4000, seed=9):
    if len(vals) < 4:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    k = len(vals)
    d = []
    for _ in range(B):
        i = rng.integers(0, k, k)
        v = fn([vals[x] for x in i])
        if np.isfinite(v):
            d.append(v)
    return (np.percentile(d, [2.5, 97.5]) if d else (float("nan"), float("nan")))


def main() -> None:
    user = collect()
    cids = list(user)
    n_min = sum(1 for c in cids if user[c] == "candidate")
    print(f"留出集 n={len(cids)}：human {len(cids)-n_min} / candidate {n_min}"
          f"（人胜率 {1-n_min/len(cids):.3f}）\n")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print(f"{'口径':20s}{'评委':10s}{'n':>4s}{'说human':>8s}{'κ':>9s}{'κ 95%CI':>17s}"
          f"{'AUC':>7s}{'AUC 95%CI':>16s}")
    for tag, lab in VARIANTS:
        pv = HE.PROMPT_VARIANTS[tag][1]
        for m in HE.JUDGES:
            res = [(user[c], judge_result(c, m, pv, con)) for c in cids]
            res = [(u, r) for u, r in res if r is not None]
            if not res:
                print(f"{lab:20s}{m.split('/')[-1][:9]:10s}   无样本")
                continue
            pairs = [(u == "human", r["human"]) for u, r in res]
            sc = [(u == "human", r["score"]) for u, r in res]
            phr = np.mean([1.0 if j else 0.0 for _, j in pairs])
            k = kappa(pairs)
            kl, kh = boot_ci(pairs, kappa)
            a = auc(sc)
            al, ah = boot_ci(sc, auc)
            print(f"{lab:20s}{m.split('/')[-1][:9]:10s}{len(pairs):4d}{phr:8.3f}"
                  f"{k:+9.3f}{f'[{kl:+.3f},{kh:+.3f}]':>17s}{a:7.3f}{f'[{al:.3f},{ah:.3f}]':>16s}")
    con.close()

    print("\n【怎么读】")
    print("  · 比较的是**同一批题**上的 κ 与 AUC；AUC 与阈值无关，最适合比'有没有信息'。")
    print("  · AUC 的 95%CI 下界 >0.5 才算有可测信息——少数类只有 "
          f"{n_min} 条，CI 会很宽，属正常。")
    print("  · 若 defect 的 AUC/κ 明显高于 v3/v4，则说明**瓶颈在任务而非措辞**，"
          "下一步应把 rubric 改成判别式清单、并据此重构 Gate。")


if __name__ == "__main__":
    main()
