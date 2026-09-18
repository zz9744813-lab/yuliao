"""大样本诊断：κ 低是「少数类太少」还是「真的到顶」？（2026-09-16）

**背景**：留出集上 v3 的 κ 只有 +0.07，但**少数类只有 5 条**。5 条下 κ 的方差极大，
既可能真这么低，也可能是噪声。不能只凭它下结论。

**做法**：改用标准口径（`judge_preference_v3` / `v4`）已有的 99–120 题记录，
少数类约 40 条 —— 样本量足够谈方向。

⚠ **必须标注循环性**：v4 的 rubric 是从这 99 题反推的，所以 v4 在这批数据上是
**自我考试**，数字只能当上界。**v3 没有被这批数据调过，故 v3 的数字是可用的基线。**
本脚本会把两者分开陈述，并显式打印警示。

用法：
    python scripts/large_sample_diag.py
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
MINORITY_BATCHES = ("r25", "s30")


def load(pv: str) -> dict[str, int | None]:
    """cid → 该口径下的 winner_resolved（human/candidate），无记录为 None。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out: dict[str, int | None] = {}
    for r in con.execute(
            """select ri.subject_id cid, ri.reasons, ri.human_verdict
               from review_items ri join candidates c on c.id = ri.subject_id
               where ri.experiment_id = 'EXP-0911-B82D' and ri.status = 'done'
                 and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')"""):
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        rs = json.loads(r["reasons"]) if r["reasons"] else []
        batch = next((x[6:] for x in rs if x.startswith("batch_")), None)
        out[r["cid"]] = None      # 占位：用户标签另存
        for mi, m in enumerate(HE.JUDGES):
            q = con.execute(
                """select verdict from judge_runs where subject_id=? and judge_kind='preference'
                   and model=? and prompt_version=? order by created_at desc limit 1""",
                (r["cid"], m, pv)).fetchone()
            d = HE._as_dict(q["verdict"]) if q and q["verdict"] else None
            if d and d.get("winner_resolved") in ("human", "candidate"):
                out[f"{r['cid']}|j{mi}"] = d["winner_resolved"]
        out[f"{r['cid']}|user"] = w
        out[f"{r['cid']}|nat"] = batch in MINORITY_BATCHES
    con.close()
    return out


def pairs_for(data: dict, judge_key: str, only_nat: bool | None = None):
    cids = {k.split("|")[0] for k in data if k.endswith("|user")}
    ps = []
    for c in cids:
        if only_nat is not None and bool(data.get(f"{c}|nat")) != only_nat:
            continue
        u = data.get(f"{c}|user")
        j = data.get(f"{c}|{judge_key}")
        if u in ("human", "candidate") and j in ("human", "candidate"):
            ps.append((u == "human", j == "human"))
    return ps


def boot_kappa(ps, B=4000, seed=3):
    if len(ps) < 4:
        return float("nan"), (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    k = len(ps)
    d = []
    for _ in range(B):
        i = rng.integers(0, k, k)
        v = HE._kappa([ps[x] for x in i])
        if np.isfinite(v):
            d.append(v)
    return HE._kappa(ps), (np.percentile(d, [2.5, 97.5]) if d else (float("nan"),) * 2)


def auc_of(ps):
    y = np.array([1 if u else 0 for u, _ in ps])
    s = np.array([1.0 if j else 0.0 for _, j in ps])
    n1 = int((y == 1).sum()); n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    o = np.argsort(s); rk = np.empty(len(s)); rk[o] = np.arange(1, len(s) + 1)
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def main() -> None:
    print("=" * 78)
    print("大样本诊断：κ 低是「少数类太少」还是「真的到顶」？")
    print("=" * 78)
    # ⚠ 用**标准口径**（judge_preference_v3 / v4，各 99–120 题），
    #   不是 `PROMPT_VARIANTS` 里的 `_heldout` 版本（那个只有 46 题）。
    #   用 heldout 版就等于回到原问题（少数类 5 条），跑这个脚本就白跑了。
    for tag, pv in (("v3", "judge_preference_v3"), ("v4", "judge_preference_v4")):
        data = load(pv)
        cids = {k.split("|")[0] for k in data if k.endswith("|user")}
        print(f"\n### 口径 {tag}（{pv}）  已判题 {len(cids)}")
        if tag == "v4":
            print("  ⚠ **循环性警示**：v4 的 rubric 就是从这批数据（99 题）反推的 → "
                  "下列 v4 数字是自我考试，只能当**上界**。")
            print("    （要无偏比较 v3/v4 请看 heldout_eval.py 的 46 题留出集结果。）")
        else:
            print("  ✓ v3 未被这批数据调过 → 其数字是**可用基线**。")
        print(f"  {'子集':22s}{'评委':12s}{'n':>5s}{'少数类':>7s}{'agreement':>11s}"
              f"{'κ':>9s}{'κ 95%CI':>18s}{'AUC':>7s}")
        for label, only_nat in (("全部（含优先队列）", None),
                                ("仅自然池（无偏框）", True)):
            for jk, jl in (("j0", "kimi"), ("j1", "deepseek")):
                ps = pairs_for(data, jk, only_nat)
                if len(ps) < 4:
                    continue
                nmin = sum(1 for u, _ in ps if not u)
                ag = np.mean([u == j for u, j in ps])
                k, (lo, hi) = boot_kappa(ps)
                print(f"  {label:22s}{jl:12s}{len(ps):5d}{nmin:7d}{ag:11.3f}{k:+9.3f}"
                      f"{f'[{lo:+.3f},{hi:+.3f}]':>18s}{auc_of(ps):7.3f}")

    print("\n" + "=" * 78)
    print("【结论怎么读】")
    print("  · 若 v3 在 n≈100 / 少数类≈40 上 κ 仍只有 0.1 量级 →")
    print("    **不是样本量问题，是信号真的弱** → 继续攒数据没意义。")
    print("  · 若 v3 的 κ 明显高于留出集的 +0.07 → 之前的低值主要是少数类太少的噪声。")
    print("  · 注意「仅自然池」一栏 n 很小（约 46），其 CI 宽属正常。")
    print("=" * 78)


if __name__ == "__main__":
    main()
