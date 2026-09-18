"""噪点批注汇总（2026-09-14）。

集霸的观察：**有些段落整体其实还可以，但总有几处坏的把整段拖垮。**
盲评里标出的批注是"亲手指认的缺陷位置"——比从指标反推相关性干净得多。

本脚本回答三个问题：
1. **哪类噪点最常见**（按 kind 统计，按 human/candidate 侧分开）；
2. **标了噪点的段落是不是更容易输**（噪点密度 vs 胜负）；
3. **人类段与候选段的噪点类型分布是否不同**（若不同，就是一条可写的 rubric 规则）。

⚠ 盲评翻译提醒：批注的 `target` 字段已由服务端用 `human_was_a` 翻译过。
若 `target` 为 None（服务端重启导致映射丢失），本脚本会**跳过该条**而不猜——
猜错会让"人类段噪点"统计成"候选段噪点"，整份结论就反了。

用法：
    python scripts/noise_report.py
    python scripts/noise_report.py --batch r25
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
EXP = "EXP-0911-B82D"


def load(batch: str | None, exp: str | None = None):
    """exp=None = 不限实验（跨语料混合批：同批题分属多个实验）。
    写死 B82D 会让跨语料批次**静默报「已判 0 条」**（2026-09-16 实测 mix30 就是这样）。"""
    tag = f"batch_{batch}" if batch else None
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    sql = ("""select ri.id rid, ri.subject_id cid, ri.reasons, ri.human_verdict
              from review_items ri
              join candidates c on c.id = ri.subject_id
              where ri.status = 'done'
                and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""")
    qargs: list = []
    if exp:
        sql += " and ri.experiment_id = ?"
        qargs.append(exp)
    rows = con.execute(sql, qargs).fetchall()
    con.close()
    out, skipped = [], 0
    for r in rows:
        if tag and tag not in (r["reasons"] or ""):
            continue
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        anns = hv.get("annotations") or []
        # 映射丢失的条目：target 为 None，宁可不统计也不猜
        usable = [a for a in anns if a.get("target") in ("human", "candidate")]
        skipped += len(anns) - len(usable)
        out.append({"rid": r["rid"], "winner": hv.get("winner_resolved"),
                    "n_ann": len(usable), "anns": usable})
    return out, skipped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None)
    ap.add_argument("--exp", default=None,
                    help="实验号；默认由批次反查（混合批则不限实验）")
    args = ap.parse_args()

    exp = args.exp
    if exp is None and args.batch:
        import heldout_eval as he
        exps = he.experiments_of_batch(args.batch)
        exp = exps[0] if len(exps) == 1 else None

    items, skipped = load(args.batch, exp)
    scope = f"batch_{args.batch}" if args.batch else "全部已判"
    n_with = sum(1 for x in items if x["n_ann"])
    total_ann = sum(x["n_ann"] for x in items)
    print(f"[{exp or '全部实验'} / {scope}] 已判 {len(items)} 条；其中 {n_with} 条带批注，"
          f"共 {total_ann} 处噪点")
    if skipped:
        print(f"  ⚠ 跳过 {skipped} 处（target 为 None，服务端重启导致映射丢失）——不猜")
    if not total_ann:
        print("\n还没有批注数据。在前端选中 A/B 里出问题的句子即可标注。")
        return

    # ① 按 kind × target
    by = defaultdict(Counter)
    for x in items:
        for a in x["anns"]:
            by[a["target"]][a.get("kind") or "other"] += 1
    print("\n=== ① 噪点类型分布（按被标的那一侧）===")
    kinds = sorted(set(list(by["human"]) + list(by["candidate"])),
                   key=lambda k: -(by["human"][k] + by["candidate"][k]))
    print(f"{'类型':12s}{'人类段':>8s}{'候选段':>8s}")
    for k in kinds:
        print(f"{k:12s}{by['human'][k]:8d}{by['candidate'][k]:8d}")

    # ② 批注是否指向了输的那一侧（"集霸的直觉对不对"）
    print("\n=== ② 批注指向与胜负是否一致 ===")
    print("（只统计二选一的题；tie/both_bad 不进）")
    only_loser = both_sides = only_winner = 0
    for x in items:
        w = x["winner"]
        if w not in ("human", "candidate") or not x["anns"]:
            continue
        marked = {a["target"] for a in x["anns"]}
        if marked == {w}:
            only_winner += 1          # 只标了赢的那侧（"虽然赢但也有毛病"）
        elif len(marked) == 1:
            only_loser += 1           # 只标了输的那侧（批注指认对了）
        else:
            both_sides += 1
    tot = only_loser + both_sides + only_winner
    if tot:
        print(f"  只标输的一侧：{only_loser}/{tot} = {only_loser/tot:.3f}   （批注定位到失败原因）")
        print(f"  只标赢的一侧：{only_winner}/{tot} = {only_winner/tot:.3f}   （赢了但仍有毛病）")
        print(f"  两侧都标：    {both_sides}/{tot} = {both_sides/tot:.3f}")
    else:
        print("  暂无可统计条目")


if __name__ == "__main__":
    main()
