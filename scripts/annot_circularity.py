"""缺陷口径的循环性检验（2026-09-16）。

**要排除的怀疑**：缺陷口径用的**缺陷类型词表**（解释过度/用词/…）来自集霸在
`r25`+`s30` 这 46 题上标注的 19 处噪点。那么缺陷口径在**同一批 46 题**上表现好，
可能只是因为词表"背过"了这批数据 —— 这是**词表循环性**。

**干净的检验**：在集霸**没有标注过**的优先队列（`first50`/`first15`/`dual10`/`r15`，
共 99 题）上跑缺陷口径，看 AUC/κ 是否仍成立。

⚠ 注意这里的循环性比 v4 的**轻得多**，值得说清：
  · v4：rubric 是从这 99 题的**胜负标签**直接推导 → 在同批数据上评估是自我考试。
  · defect：词表只是**类型名**（"解释过度"这类概念词），并非从标签拟合出的权重；
    且若它真能迁移到 99 题上，反而说明词表抓住了通用缺陷模式、而非过拟合。

用法：
    python scripts/annot_circularity.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as HE  # noqa: E402
from defect_threshold import kappa  # noqa: E402
from defect_robustness import cv_pipeline  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
PRIORITY_BATCHES = ("first50", "first15", "dual10", "r15")


def case_rows(cids: list[str], user: dict, key: str, model: str) -> list[dict]:
    """取某口径下、指定 cid 集合的 (y, score)。key 为 'defect' 或 'v4'。"""
    pv = HE.PROMPT_VARIANTS[key][1]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for cid in cids:
        q = con.execute(
            """select verdict, abstain from judge_runs where subject_id=? and judge_kind='preference'
               and model=? and prompt_version=? order by created_at desc limit 1""",
            (cid, model, pv)).fetchone()
        if not q or not q["verdict"] or q["abstain"]:
            continue
        d = HE._as_dict(q["verdict"])
        if not d:
            continue
        w = d.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        y = 1 if user[cid] == "human" else 0
        if key == "defect":
            if "n_defects_a" not in d:
                continue
            h = d.get("human_was_a")
            na, nb = d.get("n_defects_a") or 0, d.get("n_defects_b") or 0
            own, opp = (na, nb) if h else (nb, na)
            score = float(opp - own)
        else:
            conf = d.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.5
            score = conf if w == "human" else -conf
        out.append({"cid": cid, "y": y, "score": score})
    con.close()
    return out


def auc(rows: list[dict]) -> float:
    y = np.array([r["y"] for r in rows]); s = np.array([r["score"] for r in rows])
    n1 = int((y == 1).sum()); n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    o = np.argsort(s); rk = np.empty(len(s)); rk[o] = np.arange(1, len(s) + 1)
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main() -> None:
    ann = {x["cid"]: x["user"] for x in
           list(HE.load_items(True, "r25", HE.BATCH_EPOCH)) + list(HE.load_items(True, "s30", None))}
    pri = {x["cid"]: x["user"] for x in
           HE.load_items(False, "", None, batches=PRIORITY_BATCHES)}
    pri = {c: u for c, u in pri.items() if c not in ann}
    print(f"集霸标注过噪点的题（r25+s30）：{len(ann)}")
    print(f"未标注过的优先队列题：{len(pri)}"
          f"（候选胜 {sum(1 for u in pri.values() if u=='candidate')} 条）\n")

    print("=== 缺陷口径：标注过 vs 未标注过（词表循环性检验）===")
    print(f"{'子集':22s}{'评委':11s}{'n':>4s}{'AUC':>7s}{'折外校准κ':>11s}{'固定规则κ':>11s}")
    for label, pool in (("标注过（有循环风险）", ann), ("未标注过（干净）", pri)):
        cids = list(pool)
        for m, ml in ((HE.JUDGES[0], "kimi"), (HE.JUDGES[1], "deepseek")):
            rows = case_rows(cids, pool, "defect", m)
            if len(rows) < 8:
                print(f"{label:22s}{ml:11s}{len(rows):4d}   样本不足")
                continue
            nmin = sum(1 for r in rows if r["y"] == 0)
            if nmin == 0:
                print(f"{label:22s}{ml:11s}{len(rows):4d}   无候选胜样本")
                continue
            k_cal, _ = cv_pipeline(rows, seed=5)
            k_fix = kappa([(r["y"] == 1, r["score"] > 0) for r in rows])
            print(f"{label:22s}{ml:11s}{len(rows):4d}{auc(rows):7.3f}{k_cal:+11.3f}{k_fix:+11.3f}")

    print("\n=== 对照：整体偏好 v4（同样两批）===")
    print(f"{'子集':22s}{'评委':11s}{'n':>4s}{'AUC':>7s}")
    for label, pool in (("标注过", ann), ("未标注过", pri)):
        for m, ml in ((HE.JUDGES[0], "kimi"), (HE.JUDGES[1], "deepseek")):
            rows = case_rows(list(pool), pool, "v4", m)
            if len(rows) < 8 or sum(1 for r in rows if r["y"] == 0) == 0:
                continue
            print(f"{label:22s}{ml:11s}{len(rows):4d}{auc(rows):7.3f}")

    print("\n【读法】")
    print("  · 若 defect 在**未标注过**的 99 题上 AUC 仍明显 >0.5 → 词表抓的是通用缺陷模式，")
    print("    循环性可以排除，结论可用。")
    print("  · 若只在标注过的 46 题上好 → 是词表过拟合，不能用。")
    print("  · 注意 99 题的候选胜有 30+ 条（比留出集多得多），这里 AUC 的 CI 会窄很多，")
    print("    **是比 46 题更可信的估计**。")


if __name__ == "__main__":
    main()
