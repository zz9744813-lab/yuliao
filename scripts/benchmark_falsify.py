"""子基准跑分结果的否掉检验（纪律①：过线读数采信前，先设计最可能否掉它的检验）。

2026-09-19 起 nat-v1 / hvai-v1 出现 ~0.9 的高读数——正是交接 §0.7 附的那条教训
「显著优于此前瓶颈的结果，先设计一个最可能否掉它的检验」的适用场景。本工具对
benchmark_runs 里的已有跑分做六项检验，**只读库、零 LLM 成本**：

· N0 聚类置换检验：题目聚在少量段落上（nat-v1 201 题只来自 15 段），n 不是独立
  样本数（交接 §9 统计 8）。零假设下对**每段**做符号翻转（段内所有题的 A/B 对调
  ——相当于该段的方向随机），重排 B 次答对率的零分布。位置已随机化，0.5 是正确的
  无信号基准。段级翻转保守于按题翻转（段效应共享）。
· N1 位置偏差：报 pick-A 率 vs 答案-A 率。恒选 A 的模型在均衡答案集上应只有 0.5，
  偏离说明读数里有位置成分（评审台 §5 的老坑）。
· N2 留一类型敏感性：剔除任一 corruption_type 后答对率的最大波动。单一类型撑起
  的"过线"不算过线。
· N3 未答率：n / 集合条目。failed_parse 偏多（deepseek 实测 ~9%）会静默缩水 n，
  必须显形（纪律④）。
· N4 长度基线（军师 P1-3）：与"只选较短文本"的简单规则配对比较——位置随机化了
  但长度没有，不显著优于它就不算真信号。
· N5 长度分层正确率（指标硬化）：把已答题按「人类答案在更短侧 / 更长侧」分两层
  （等长的单列）。两层各 ≥3 题时，"人类侧更长"层 acc < 0.5 ⇒ 模型在那半边接近
  瞎猜，读数是纯长度驱动——不许 pass（只报总 acc 掩盖这个混淆）。

结论措辞是**分级的**（provisional 纪律）：
  pass  = N0 p<0.05 且 N2 波动 < 0.05 且 N1 |偏差| < 0.2 且 N3 >= 0.9
          且显著优于"只选较短"基线，且（两层各 ≥3 题时）"人类侧更长"层 acc >= 0.5
  weak  = N0 过但其它有一项存疑
  fail  = N0 未过
用法：
    python scripts/benchmark_falsify.py --set BS-9fb5d1ac1134
    python scripts/benchmark_falsify.py --set BS-9fb5d1ac1134 --md
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.models import BenchmarkItem, BenchmarkRun  # noqa: E402

FLIP_B = 20000          # 聚类置换重排次数
CLUST_B = 5000          # 段级 bootstrap 次数
SEED = 20260919         # 检验本身也要可复现
GATE = {"perm_p": 0.05, "sensitivity": 0.05, "pos_bias": 0.2, "answered": 0.9,
        "long_layer_acc": 0.5, "long_layer_min_n": 3}   # N5：长层下限与参与判定的最小层规模


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _load(set_id: str):
    """读集合条目（段/答案/类型/AB 长度）+ 各 run 的逐题 pick。只读。"""
    with db.session() as s:
        items = s.query(BenchmarkItem).filter_by(set_id=set_id).all()
        if not items:
            raise SystemExit(f"集合 {set_id} 没有条目")
        meta = {x.id: (x.segment_id, x.answer, (x.meta or {}).get("corruption_type", ""))
                for x in items}
        # 军师 P1-3：长度基线必检——"只选较短文本"的简单规则曾是两个集合上的
        # 最高分（nat-v1 规则 0.940 vs 模型 0.915；hvai 0.867 vs 0.827）。
        lengths = {x.id: (len(x.text_a or ""), len(x.text_b or "")) for x in items}
        n_items = len(items)
        runs = []
        for r in s.query(BenchmarkRun).filter_by(set_id=set_id).all():
            picks = (r.detail or {}).get("picks") or {}
            runs.append({"model": r.model, "n": r.n, "correct": r.n_correct,
                         "accuracy": r.accuracy, "picks": picks,
                         "created_at": r.created_at})
    return meta, n_items, runs, lengths


def _binom_two_sided(k: int, n: int) -> float:
    from math import comb
    if n == 0:
        return 1.0
    def pm(x):
        return comb(n, x) * (0.5 ** n)
    p0 = pm(k)
    return min(1.0, sum(pm(x) for x in range(n + 1) if pm(x) <= p0 + 1e-12))


def falsify_run(meta: dict, run: dict, n_items: int | None = None,
                lengths: dict | None = None) -> dict:
    """对单个 run 做六项检验。picks 缺的题按未答处理（不进分母，但进 N3）。

    N3 的分母是**集合条目数**（n_items）——DB 里 run.n 是已答数，用它当分母
    恒为 1，failed_parse 缩水 n 会被静默放过（2026-09-19 deepseek nat-v1 实测 19 题未答）。
    """
    answered = {iid: pick for iid, pick in run["picks"].items() if pick in ("A", "B")}
    n_ans = len(answered)
    if n_ans == 0:
        return {"model": run["model"], "verdict": "fail", "reason": "无可用答案"}
    acc = sum(1 for iid, p in answered.items()
              if meta[iid][1] == p) / n_ans
    ans_a_rate = sum(1 for iid in answered if meta[iid][1] == "A") / n_ans
    pick_a_rate = sum(1 for p in answered.values() if p == "A") / n_ans

    # N0：段级符号翻转置换。段分组只看**已答**的题；每组按伯努利翻转该组全部
    # 题的 pick（A↔B），重算 acc。组内题共享段效应，翻转必须整组进行。
    segs: dict[str, list[str]] = {}
    for iid, pick in answered.items():
        segs.setdefault(meta[iid][0], []).append(iid)
    base_segs = {g: [(meta[i][1], answered[i]) for i in ids] for g, ids in segs.items()}
    rng = random.Random(SEED)
    hits = 0
    for _ in range(FLIP_B):
        k = 0
        for ids in base_segs.values():
            flip = rng.random() < 0.5
            for ans, pick in ids:
                eff = pick if not flip else ("A" if pick == "B" else "B")
                k += (eff == ans)
        if k / n_ans >= acc - 1e-12:
            hits += 1
    perm_p = hits / FLIP_B

    # 段级 cluster bootstrap CI（重采样段，不是题）
    accs = []
    gs = list(base_segs.values())
    for _ in range(CLUST_B):
        num = den = 0
        for g in (rng.choice(gs) for _ in range(len(gs))):
            for ans, pick in g:
                num += (pick == ans)
                den += 1
        if den:
            accs.append(num / den)
    accs.sort()
    lo = accs[int(0.025 * len(accs))]
    hi = accs[int(0.975 * len(accs)) - 1]

    # N1 位置偏差
    pos_bias = abs(pick_a_rate - 0.5) if abs(ans_a_rate - 0.5) < 0.1 else None

    # N2 留一类型敏感性
    types = {meta[iid][2] or "∅" for iid in answered}
    worst = acc
    for drop in types:
        keep = [(iid, p) for iid, p in answered.items() if (meta[iid][2] or "∅") != drop]
        if keep:
            worst = min(worst, sum(1 for iid, p in keep if meta[iid][1] == p) / len(keep))
    sensitivity = acc - worst

    # N3 未答率：分母 = 集合条目数（不是 run.n，那已经是已答数）
    answered_rate = n_ans / n_items if n_items else n_ans / (run["n"] or 1)
    # P1-4：全题有效成功率——漏答按错算（只报已答 acc 是选择性汇报）
    effective_acc = run["correct"] / n_items if n_items else None

    # N4（军师 P1-3）长度基线："只选较短文本"的简单规则读数 + 配对比较。
    # 位置已随机化但**长度没有**——若劣化侧普遍更长，"选短"就是免费高分，
    # 模型读数必须显著超过它才算真信号。
    len_correct = model_beat = baseline_beat = 0
    n_both = 0
    for iid, pick in answered.items():
        la, lb = (lengths or {}).get(iid, (0, 0))
        pred = "A" if la < lb else ("B" if lb < la else None)
        if pred is None:
            continue                      # 等长：规则无答案，算它错，跳过配对
        n_both += 1
        b_ok = (pred == meta[iid][1])
        m_ok = (pick == meta[iid][1])
        len_correct += b_ok
        if m_ok and not b_ok:
            model_beat += 1
        if b_ok and not m_ok:
            baseline_beat += 1
    len_acc = len_correct / n_both if n_both else None
    disc = model_beat + baseline_beat
    beat_p = _binom_two_sided(min(model_beat, baseline_beat), disc) if disc else 1.0

    # N5（指标硬化）长度分层正确率：把已答题按「人类答案在更短侧 / 更长侧」分两层
    # （等长的单列 equal）。总 acc 一高就把长度混淆盖住了——模型完全可能只在
    # "人类侧更短"的题上对（等价于免费"选短"规则）。分母 = 该层已答数。
    strat_n = {"short": 0, "long": 0, "equal": 0}
    strat_hit = {"short": 0, "long": 0, "equal": 0}
    length_strat = None
    if lengths:
        for iid, pick in answered.items():
            la, lb = lengths.get(iid, (0, 0))
            if la == lb:
                layer = "equal"
            elif (la < lb) == (meta[iid][1] == "A"):
                layer = "short"                  # 人类答案在更短侧
            else:
                layer = "long"                   # 人类答案在更长侧
            strat_n[layer] += 1
            strat_hit[layer] += (pick == meta[iid][1])
        length_strat = {g: {"n": strat_n[g],
                            "acc": round(strat_hit[g] / strat_n[g], 4) if strat_n[g] else None}
                        for g in ("short", "long", "equal")}

    # 两层都 ≥ long_layer_min_n 时参与判定：较长层（人类侧更长）acc < 0.5 ⇒
    # 模型在那半边接近瞎猜 = 纯长度驱动，不许 pass。任一层不足 ⇒ None 不参与。
    if length_strat and strat_n["short"] >= GATE["long_layer_min_n"] \
            and strat_n["long"] >= GATE["long_layer_min_n"]:
        ls_check = strat_hit["long"] / strat_n["long"] >= GATE["long_layer_acc"]
    else:
        ls_check = None

    checks = {
        "perm_p": perm_p < GATE["perm_p"],
        "sensitivity": sensitivity < GATE["sensitivity"],
        "pos_bias": pos_bias is None or pos_bias < GATE["pos_bias"],
        "answered": answered_rate >= GATE["answered"],
        "beats_length": (len_acc is not None and acc > len_acc
                         and beat_p < 0.05),   # P1-3：不显著优于"只选较短"不许 pass
        "length_stratified": ls_check,         # N5：None=层太小不参与判定
    }
    gate_ok = all(v for v in checks.values() if v is not None)   # None 不阻塞
    verdict = ("pass" if gate_ok
               else "fail" if not checks["perm_p"]
               else "weak")
    return {"model": run["model"], "n_answered": n_ans, "acc": round(acc, 4),
            "wilson": [round(v, 4) for v in wilson(run["correct"], run["n"])],
            "cluster_ci": [round(lo, 4), round(hi, 4)],
            "perm_p": round(perm_p, 4), "flip_groups": len(segs),
            "pick_a_rate": round(pick_a_rate, 4), "answer_a_rate": round(ans_a_rate, 4),
            "pos_bias": None if pos_bias is None else round(pos_bias, 4),
            "sensitivity": round(sensitivity, 4), "answered_rate": round(answered_rate, 4),
            "effective_acc": round(effective_acc, 4) if effective_acc is not None else None,
            "length_baseline": {"acc": round(len_acc, 4) if len_acc is not None else None,
                                 "n_pairs": n_both,
                                 "model_beat": model_beat, "baseline_beat": baseline_beat,
                                 "sign_p": round(beat_p, 4)},
            "length_strat": length_strat,
            "checks": checks, "verdict": verdict, "n_answered": run["n"]}


def falsify_set(set_id: str) -> dict:
    meta, n_items, runs, lengths = _load(set_id)
    return {"set_id": set_id, "n_items": n_items,
            "gates": GATE,
            "runs": [falsify_run(meta, r, n_items=n_items, lengths=lengths)
                     for r in runs]}


def _fmt_length_strat(strat: dict | None) -> str:
    """主表"分层acc(短/长)"单元格："0.95/0.60 (n=8/12)"；无长度数据或任一层
    为空（acc 不可算）显示 —。"""
    if not strat:
        return "—"
    s, l = strat["short"], strat["long"]
    if s["acc"] is None or l["acc"] is None:
        return "—"
    return f"{s['acc']:.2f}/{l['acc']:.2f} (n={s['n']}/{l['n']})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args()
    out = falsify_set(args.set)
    if args.md:
        print("| 模型 | 已答acc | 全题成功率 | 段bootstrap CI | 置换p(段翻转) | 长度基线(超它?) | 分层acc(短/长) | pickA/ansA | 留一波动 | 未答率 | 判定 |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in out["runs"]:
            if "acc" not in r:
                print(f"| {r['model']} | — | — | — | — | — | — | — | — | — | fail（{r.get('reason','')}）|")
                continue
            lb = r["length_baseline"]
            pb = r["pos_bias"]
            print(f"| {r['model']} | {r['acc']:.3f} | {r['effective_acc']:.3f} "
                  f"| [{r['cluster_ci'][0]:.3f},{r['cluster_ci'][1]:.3f}] "
                  f"| {r['perm_p']:.4f}（{r['flip_groups']}段）"
                  f"| {lb['acc']:.3f}（{lb['model_beat']}:{lb['baseline_beat']} p={lb['sign_p']:.3f}）"
                  f"| {_fmt_length_strat(r['length_strat'])} "
                  f"| {r['pick_a_rate']:.2f}/{r['answer_a_rate']:.2f} "
                  f"| {r['sensitivity']:.3f} | {r['answered_rate']:.2f} | **{r['verdict']}** |")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
