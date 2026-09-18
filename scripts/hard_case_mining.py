"""硬例挖掘 —— 总方案 §10 工作流 E（2026-09-17 补建）。

## 进入条件（照方案原文）

```
Judge disagreement > threshold          → 评委之间分歧大
Human preference ≠ Reward Model         → （本库暂无 RM，跳过）
Human original 被 Judge 大量判输         → **集霸判人类胜，评委却多数站候选**
多模型结果分布异常                        → 各评委站边率极端不一致
语义评分高但自然度极低                     → residuals_sem / naturalness 口径
自然度高但语义发生漂移                     → 同上反向
模型反复失败                             → candidates.status=failed / judge status!=ok
```

## 为什么要做

今天确立了：**评委的审美与集霸方向相反**（集霸判候选胜 26.5%，评委挑候选 53~74%）——
所以"人类原文被评委大量判输"这一类硬例，正是**最该进策略库/训练集**的样本：
它们标出了模型与作者分歧最大的地方，也就是 §53 成功标准里"要改掉的 AI 习惯"。

## 产出

`hard_cases` 表（方案规定字段：失败原因 / 争议点 / 可能缺失的特征 / 是否需要人工判断 /
是否需要新增 Ontology / 是否需要新增 ExpressionStrategy），并报告分布。

用法：
    python scripts/hard_case_mining.py --dry-run
    python scripts/hard_case_mining.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.models import HardCase  # noqa: E402

# 参与"站边"统计的评委（heldout 口径、按模型取一排）
PV = he.PROMPT_VARIANTS["v4"][1]


def load_items() -> list[dict]:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, c.id cid, c.experiment_id exp, s.text stext, c.text ctext
           from review_items ri
           join candidates c on c.id = ri.subject_id
           join segments s on s.id = c.segment_id
           where ri.status='done' and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')"""
    ).fetchall()
    models = [r[0] for r in con.execute(
        """select distinct model from judge_runs where judge_kind='preference'
           and prompt_version=? and status='ok'""", (PV,))]
    out = []
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        votes = {}
        for m in models:
            d = he._read_verdict(r["cid"], m, PV, con)
            if d in ("human", "candidate"):
                votes[m] = d
        if votes:
            out.append({"cid": r["cid"], "exp": r["exp"], "user": w, "votes": votes,
                        "stext": r["stext"], "ctext": r["ctext"]})
    con.close()
    return out


def classify(it: dict) -> dict | None:
    """按方案条件判定；返回 None = 不是硬例。"""
    votes = it["votes"]
    n = len(votes)
    n_cand = sum(1 for v in votes.values() if v == "candidate")
    n_human = n - n_cand
    kinds, why, dispute = [], [], []

    # ① 人类原文被评委大量判输（集霸判 human，评委多数站 candidate）
    if it["user"] == "human" and n >= 3 and n_cand / n >= 0.75:
        kinds.append("human_lost_to_judges")
        why.append(f"{n_cand}/{n} 个评委站候选，但集霸判人类胜")
        dispute.append("评委偏好铺陈，集霸偏好克制——方向相反")

    # ② 评委之间分歧大（要求**真正对半**，不是"有一个不同"）
    #    ⚠ 第一版把 model_outlier（某评委与其余相反）也算硬例，结果 5 个模型 +
    #    基础率偏斜下 184/230 都中——等于没区分度。判据必须收紧到「系统真的拿不准」。
    if n >= 4 and 0.4 <= n_cand / n <= 0.6:
        kinds.append("judge_disagreement")
        why.append(f"评委接近对半：{n_cand} 站候选 / {n_human} 站人类（n={n}）")

    # ③ 全票一致但与被判方相反（5 个评委一个方向、集霸另一个方向）
    if n >= 4 and (n_cand == n or n_cand == 0) and it["user"] != ("candidate" if n_cand else "human"):
        kinds.append("unanimous_vs_user")
        why.append(f"评委 {n} 票一致站{'候选' if n_cand else '人类'}，集霸判相反")

    if not kinds:
        return None
    sev = abs(n_cand / n - (1.0 if it["user"] == "human" else 0.0))
    return {"kind": ",".join(kinds), "severity": round(sev, 3),
            "why": "；".join(why), "dispute": "；".join(dispute) or "（待人工判定争议点）",
            "n_cand": n_cand, "n": n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    items = load_items()
    print(f"可用条目 = {len(items)}（有 heldout v4 评委记录）")
    cases = []
    for it in items:
        c = classify(it)
        if c:
            cases.append({**it, **c})
    print(f"命中硬例 = {len(cases)} / {len(items)}")
    from collections import Counter
    print("  按条件:", dict(Counter(k for c in cases for k in c["kind"].split(","))))
    print("  按集霸判定:", dict(Counter(c["user"] for c in cases)))
    print()
    print("  最严重的 5 条（severity 降序）：")
    for c in sorted(cases, key=lambda x: -x["severity"])[:5]:
        print(f"    {c['cid'][:14]} sev={c['severity']:.2f} 集霸={c['user']} "
              f"评委 {c['n_cand']}/{c['n']} 站候选  人类原文：{c['stext'][:28]}")

    if args.dry_run:
        print("\ndry-run：未写库")
        return
    with db.session() as s:
        s.query(HardCase).delete()
        for c in cases:
            s.add(HardCase(
                candidate_id=c["cid"], experiment_id=c["exp"], kind=c["kind"],
                severity=c["severity"], user_verdict=c["user"],
                judge_votes={m.split("/")[-1]: v for m, v in c["votes"].items()},
                n_judges=c["n"], why=c["why"], dispute=c["dispute"],
                missing_features=["implicitness", "克制/留白（方案 §53 明列）"],
                # 争议点待人工确认的（评委方向相反）标记为需要人看
                need_human=("model_outlier" in c["kind"] or "judge_disagreement" in c["kind"]),
                need_ontology=False, need_strategy=("human_lost_to_judges" in c["kind"]),
                strategy_id=None, version=1,
                source=f"{len(items)} 条已判条目；评委口径 v4_heldout"))
        s.commit()
    print(f"\n已写入 hard_cases：{len(cases)} 条")



if __name__ == "__main__":
    main()
