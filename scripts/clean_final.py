"""干净集上的三口径终审（2026-09-16）。

**为什么需要这一张表**：前面每一步都有一个可能的漏洞——
  · 留出集（46 题）：少数类只有 5 条，CI 宽到无法判别；
  · 大样本（99 题）：用标准口径 v3/v4，但 v4 对它有循环性；
  · 缺陷口径在 46 题上 AUC=0.897 —— 但它的**类型词表**来自集霸在该批的标注，
    实测效果**不迁移**到未标注集（0.897 → 0.569）。

→ 所以唯一干净、且样本够大的比较是：**未标注过的优先队列 62 题（候选胜 19 条）**，
  在其上同时评 v3 / v4 / defect 三个口径。

⚠ v4 的循环性说明：v4 的 rubric 是从 `first50/dual10/r15` 这 99 题推导的，
  所以 v4 在这一栏**仍是自我考试**（上界）。v3 与 defect 无此问题
  （defect 的词表来自另一批 r25/s30，与这里不重叠）。

用法：
    python scripts/clean_final.py
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

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
CLEAN_BATCHES = ("first50", "first15", "dual10", "r15")


def rows_for(cids: list[str], user: dict, key: str, model: str) -> list[dict]:
    """取某口径的 (y, score)。

    ⚠ 必须**同时找标准口径与 `_heldout` 口径**：优先队列是用标准口径
    （`judge_preference_v3/v4`）跑的，留出批次用的是 `_heldout` 版本。
    只查一个会得到 0 样本、然后被静默当成"样本不足"（本项目高频故障模式）。
    """
    pvs = [HE.PROMPT_VARIANTS[key][1]]
    if key == "defect":
        pass
    else:
        pvs.append(f"judge_preference_{key}")      # 标准口径，无 _heldout 后缀
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for cid in cids:
        d = None
        for pv in pvs:
            q = con.execute(
                """select verdict, abstain from judge_runs where subject_id=? and judge_kind='preference'
                   and model=? and prompt_version=? order by created_at desc limit 1""",
                (cid, model, pv)).fetchone()
            if q and q["verdict"] and not q["abstain"]:
                cand = HE._as_dict(q["verdict"])
                if cand and cand.get("winner_resolved") in ("human", "candidate"):
                    d = cand
                    break
        if not d:
            continue
        w = d.get("winner_resolved")
        y = 1 if user[cid] == "human" else 0
        h = d.get("human_was_a")
        if key == "defect":
            if "n_defects_a" not in d:
                continue
            na = d.get("n_defects_a") or 0
            nb = d.get("n_defects_b") or 0
            own, opp = (na, nb) if h else (nb, na)
            score = float(opp - own)
        else:
            conf = d.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.5
            score = conf if w == "human" else -conf
        out.append({"cid": cid, "y": y, "score": score, "human": w == "human"})
    con.close()
    return out


def auc(rows: list[dict]) -> float:
    y = np.array([r["y"] for r in rows]); s = np.array([r["score"] for r in rows])
    n1 = int((y == 1).sum()); n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    o = np.argsort(s); rk = np.empty(len(s)); rk[o] = np.arange(1, len(s) + 1)
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def boot_auc(rows: list[dict], B: int = 4000, seed: int = 2):
    if len(rows) < 6:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    k = len(rows)
    d = []
    for _ in range(B):
        i = rng.integers(0, k, k)
        v = auc([rows[x] for x in i])
        if np.isfinite(v):
            d.append(v)
    return (np.percentile(d, [2.5, 97.5]) if d else (float("nan"), float("nan")))


def main() -> None:
    # 干净集 = 优先队列，且去掉集霸标注过噪点的题
    ann = {x["cid"] for x in
           list(HE.load_items(True, "r25", HE.BATCH_EPOCH)) + list(HE.load_items(True, "s30", None))}
    pr = HE.load_items(False, "", None, batches=CLEAN_BATCHES)
    user = {x["cid"]: x["user"] for x in pr if x["cid"] not in ann}
    cids = list(user)
    nmin = sum(1 for c in cids if user[c] == "candidate")
    print(f"干净集（未标注过的优先队列）n={len(cids)}：human {len(cids)-nmin} / "
          f"candidate {nmin}（人胜率 {1-nmin/len(cids):.3f}）\n")

    print(f"{'口径':22s}{'评委':11s}{'n':>4s}{'AUC':>7s}{'AUC 95%CI':>17s}{'固定规则κ':>11s}")
    for key, lab in (("v3", "整体偏好·无rubric"), ("v4", "整体偏好·有rubric"),
                     ("defect", "缺陷检测")):
        for m, ml in ((HE.JUDGES[0], "kimi"), (HE.JUDGES[1], "deepseek")):
            rows = rows_for(cids, user, key, m)
            if len(rows) < 6 or sum(1 for r in rows if r["y"] == 0) == 0:
                print(f"{lab:22s}{ml:11s}{len(rows):4d}   样本不足")
                continue
            a = auc(rows)
            lo, hi = boot_auc(rows)
            kf = kappa([(r["y"] == 1, r["score"] > 0) for r in rows])
            print(f"{lab:22s}{ml:11s}{len(rows):4d}{a:7.3f}{f'[{lo:.3f},{hi:.3f}]':>17s}{kf:+11.3f}")

    print("\n【终审读法】")
    print("  · AUC 的 95%CI 下界 >0.5 ⇒ 有可测信息。这一栏是**唯一样本量与循环性都过关**的比较。")
    print("  · v4 这一行仍有循环性（rubric 从这 99 题推导）→ 它的数字是**上界**；")
    print("    v3 与 defect 无循环性（defect 的词表来自另一批 r25/s30，不重叠）。")
    print("  · 若三个口径在这一栏 AUC 都在 0.5 附近 → **LLM 评委这条路在本任务上到顶**，")
    print("    应转向训小 RM（§43 Personal Preference）或降低任务难度。")


if __name__ == "__main__":
    main()
