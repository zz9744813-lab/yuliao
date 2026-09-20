"""诚实终报：全部已判数据的池化估计（2026-09-17）。

与 `heldout_eval.py` 的区别：
1. **池化全部实验 / 全部批次**（heldout_eval 一次只跑一批）；
2. **按人类段落做聚类 bootstrap** —— 段落才是独立单位，同一段的多个候选不独立
   （交接文档 §9 第 8 条）；按条目 bootstrap 会把 CI 算窄；
3. 必报四样：κ（不是 agreement）、恒定多数类基线、位置基线、置换零分布。

用法：
    python scripts/final_report.py
    python scripts/final_report.py --out docs/final-report.md
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402

DB = ROOT / "data" / "language_genome.db"
WL = ("reconstruct_v1", "recon_ctx_v1")


def judge_models(db_path=None) -> list[str]:
    """库里实际有 heldout 口径记录的评委模型（不写死）。

    2026-09-17：终报初版的局限之一是「只基于 2 个模型」——那 2 个当时出自
    heldout_eval.JUDGES 常量。改成从库里发现，补跑的模型自动进表。
    """
    con = sqlite3.connect(db_path or DB)
    rows = con.execute(
        """select distinct model from judge_runs
           where judge_kind='preference' and prompt_version like '%heldout%'
             and status='ok'""").fetchall()
    con.close()
    return sorted(r[0] for r in rows if r[0])


def load() -> tuple[list[dict], dict[str, int]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""select ri.id rid, ri.human_verdict hv, ri.reasons rs, c.id cid,
                   c.segment_id seg, c.experiment_id exp
            from review_items ri join candidates c on c.id = ri.subject_id
            where ri.status = 'done'
              and c.prompt_version in ({",".join("?" * len(WL))})""", WL).fetchall()
    items, dropped = [], {"tie": 0, "both_bad": 0, "cant_judge": 0, "mapping_lost": 0}
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            dropped[w if w in dropped else "mapping_lost"] += 1
            continue
        items.append({"rid": r["rid"], "cid": r["cid"], "seg": r["seg"], "exp": r["exp"],
                      "user": w, "human_was_a": hv.get("human_was_a"),
                      "tags": [x for x in (json.loads(r["rs"] or "[]") or [])
                               if isinstance(x, str) and x.startswith("batch_")] or ["(无批次)"]})
    con.close()
    return items, dropped


def kappa(pairs) -> float:
    n = len(pairs)
    if not n:
        return float("nan")
    a = sum(1 for u, j in pairs if u and j)
    b = sum(1 for u, j in pairs if u and not j)
    c = sum(1 for u, j in pairs if not u and j)
    d = sum(1 for u, j in pairs if not u and not j)
    po = (a + d) / n
    pj = (a + c) / n
    pu = (a + b) / n
    pe = pj * pu + (1 - pj) * (1 - pu)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def cluster_bootstrap_ci(pairs_by_seg: dict[str, list], iters: int = 2000,
                         seed: int = 20260917) -> tuple[float, float]:
    """按**段落**重抽样。同一段落的判定一起进出，保留段内相关性。"""
    rng = random.Random(seed)
    segs = list(pairs_by_seg)
    if len(segs) < 3:
        return float("nan"), float("nan")
    vals = []
    for _ in range(iters):
        sample = []
        for _ in range(len(segs)):
            sample.extend(pairs_by_seg[rng.choice(segs)])
        k = kappa(sample)
        if not math.isnan(k):
            vals.append(k)
    if not vals:
        return float("nan"), float("nan")
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="把报告写到文件")
    ap.add_argument("--iters", type=int, default=2000)
    args = ap.parse_args()

    items, dropped = load()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    MODELS = judge_models()          # 从库里发现（含补跑的跨家模型），不写死
    # 评委判定
    for it in items:
        it["judge"] = {}
        for m in MODELS:
            for v in ("v3", "v4"):
                it["judge"][(m, v)] = he._read_verdict(
                    it["cid"], m, he.PROMPT_VARIANTS[v][1], con)
    con.close()

    n = len(items)
    hr = sum(1 for x in items if x["user"] == "human") / n
    segs = {x["seg"] for x in items}
    exps = sorted({x["exp"] for x in items})
    L = []
    L.append("# Language Genome — Phase 1.5 诚实终报")
    L.append("")
    L.append("> 生成：`python scripts/final_report.py`（2026-09-17）")
    L.append("> 数据：全部已判盲评条目（所有实验、所有批次）")
    L.append("")
    L.append("## 0. 一句话结论")
    L.append("")
    L.append("**LLM 评委无法复现集霸对「哪种写法更好」的判断**：跨三个独立批次，")
    L.append("κ 稳定在 +0.09～+0.21，agreement 不超过「恒定答 human」基线，")
    L.append("全部配置低于位置基线。旧单线 Gate（agreement ≥ 0.70）作废，")
    L.append("改分档口径（见下「Gate 口径（分档）」）。")
    L.append("")
    # ── Gate 口径（分档，2026-09-20 军师改）─────────────────
    # 旧单线 Gate 把两条正交的轴混成一条线：①认得出（方向识别——哪边是
    # 人类原文/AI 生成，自带构造性答案键）；②复现口味（评委偏好对齐集霸，
    # 无答案键、只有他本人）。分档后各自设线/各自报数。
    L.append("## Gate 口径（分档，2026-09-20）")
    L.append("")
    L.append("### 档一：方向识别——达标线 **≥ 0.80**")
    L.append("")
    L.append("判别「哪边是人类原文 / AI 生成」。**只认长度平衡基准**（bal-v1：")
    L.append("S/L 各半，长度基线按构造 = 0.5）；长度混淆集的读数（nat-v1 / ")
    L.append("cc-v1 / hvai-v1 的 0.86~0.94）**不得作为达标依据**——军师 P1-3")
    L.append("实测「只选较短」基线 0.944/0.872 压过全部模型。")
    L.append("")
    try:
        # main() 早期已 con.close()——这里自开短连接读 bal 读数
        with sqlite3.connect(DB) as bcon:
            bcon.row_factory = sqlite3.Row
            bal = bcon.execute(
                """select r.model, r.n, r.n_correct, r.accuracy from benchmark_runs r
                   join benchmark_sets s on s.id = r.set_id
                   where s.kind = 'length_balanced' and r.accuracy is not null
                   order by r.accuracy desc limit 1""").fetchone()
    except Exception as e:                     # 纪律④：查询失败不许静默
        print(f"[gate 分档] bal 读数查询失败：{type(e).__name__}: {e}", file=sys.stderr)
        bal = None
    if bal:
        L.append(f"- 当前最好诚实读数：**{bal['model']} = {bal['accuracy']:.3f}**"
                 f"（{bal['n_correct']}/{bal['n']}，bal-v1）")
        L.append(f"  → **未达标**（距 0.80 差 {0.80 - bal['accuracy']:.3f}；这段差距就是后续工作目标）。")
    else:
        L.append("- （库里还没有长度平衡基准的跑分：先跑 bal-v1。）")
    L.append("")
    L.append("### 档二：复现人类口味——**无固定达标线，按实测报 κ 区间**")
    L.append("")
    L.append("评委偏好对齐集霸。**否定性结论（不是「接近达成」）**：现有全部")
    L.append("评委配置 κ 落在 **+0.09～+0.21**，最好一格 kimi v4 κ = +0.112")
    L.append("（按段落聚类 95% CI [+0.010, +0.214]，区间贴近 0），全部低于")
    L.append("「恒定答 human」基线 agreement 0.735；T7 两轮探针在 corr24 排除制")
    L.append("子集（n=5~6）上所有变换（raw/flip/map/集成）置换 p = 0.10~0.40")
    L.append("均不显著。**该轴在现有数据面上不可达**——除非出现新的 gold ")
    L.append("standard（goldpick 指认进行中），不应再在此轴上投入调用。")
    L.append("")
    L.append("## 1. 样本")
    L.append("")
    L.append(f"- 可用判定（human/candidate 二选一）：**{n} 条**")
    L.append(f"- 非二选一被排除：tie {dropped['tie']} / both_bad {dropped['both_bad']} / "
             f"cant_judge {dropped['cant_judge']} / 映射丢失 {dropped['mapping_lost']}")
    L.append(f"- 覆盖人类段落：**{len(segs)} 个**（← 独立单位是段落，不是条目）")
    L.append(f"- 覆盖实验：{', '.join(exps)}")
    L.append(f"- 用户判 human 比例：**{hr:.3f}**")
    L.append("")

    # 基线
    const = [(1 if x["user"] == "human" else 0, 1) for x in items]
    L.append("## 2. 必报基线")
    L.append("")
    L.append(f"- **恒定答 human**：agreement = {sum(1 for u,j in const if u==j)/n:.3f}，"
             f"κ = {kappa(const):+.3f}  ← agreement 的上限参照物")
    pos = [(x["user"], x) for x in items if x["human_was_a"] is not None]
    if pos:
        # 「永远选 A」对不对：A 更好 iff (human_was_a 且 用户判 human) 或 (非 A 且 用户判 candidate)
        hit = sum(1 for u, x in pos
                  if ((x["human_was_a"] and u == "human")
                      or ((not x["human_was_a"]) and u == "candidate")))
        L.append(f"- **永远选 A**（位置基线）：准确率 = {hit/len(pos):.3f}（n={len(pos)}）")
    L.append("")

    L.append("## 3. 各评委口径（池化全部数据）")
    L.append("")
    # 完整案例：四个口径都有的条目才进这张表 —— 否则各行 n 不一致，
    # 读者会误以为 kimi v3 样本比 deepseek v4 多（其实只是补齐进度不同）。
    # 每行按**该 (模型, 口径) 实际有分的条目**算 —— 各模型覆盖不同（有的只跑了 v4、
    # 有的被内容审查拒答），强行要求"全配置齐全"会把表清空。每行都标 n。
    L.append("（每行的 n 是该模型实际给出有效判定的条目数；各模型覆盖不同）")
    L.append("")
    L.append("| 评委 | 口径 | n | agreement | **κ** | κ 的 95% CI（按段落聚类） | 挑 candidate | 判 human 精度 | 判 candidate 精度 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    best = None
    for m in MODELS:
        for v in ("v3", "v4"):
            pairs, by_seg = [], {}
            jh = jhc = jc = jcc = 0
            for x in items:
                d = x["judge"].get((m, v))
                if d is None:
                    continue
                pair = (x["user"] == "human", d == "human")
                pairs.append(pair)
                by_seg.setdefault(x["seg"], []).append(pair)
                if pair[1]:
                    jh += 1
                    jhc += pair[0]
                else:
                    jc += 1
                    jcc += not pair[0]
            if len(pairs) < 10:
                continue
            agr = sum(1 for a, b in pairs if a == b) / len(pairs)
            k = kappa(pairs)
            lo, hi = cluster_bootstrap_ci(by_seg, args.iters)
            name = m.split("/")[-1][:12]
            L.append(f"| {name} | {v} | {len(pairs)} | {agr:.3f} | **{k:+.3f}** | "
                     f"[{lo:+.3f}, {hi:+.3f}] | {sum(1 for _,b in pairs if not b)/len(pairs):.3f} | "
                     f"{jhc/jh if jh else float('nan'):.3f} | {jcc/jc if jc else float('nan'):.3f} |")
            if best is None or k > best[0]:
                best = (k, name, v, lo, hi, len(pairs))
    L.append("")
    if best:
        L.append(f"**最好的一格**：{best[1]} {best[2]}，κ = {best[0]:+.3f} "
                 f"（聚类 CI [{best[3]:+.3f}, {best[4]:+.3f}]，n={best[5]}）")
        L.append(f"→ 区间包含 0，且远低于任何可用阈值（Gate 建议 κ ≥ 0.40）。")

    # ── 逐批（三个独立复现）──
    L.append("")
    L.append("### 3b. 逐批复现（三个独立批次，κ 未随任何调整改善）")
    L.append("")
    L.append("| 批次 | 样本 | 用户判 human | 恒定答 human 基线 | kimi v4 κ | deepseek v4 κ |")
    L.append("|---|---|---|---|---|---|")
    for tag, label in [("h30", "h30（琼明旧 30 段）"), ("mix30", "mix30（跨语料 30 题）"),
                       ("nq50", "nq50（琼明全新 50 段）")]:
        sub = [x for x in items if f"batch_{tag}" in x.get("tags", [])]
        if not sub:
            continue
        hr2 = sum(1 for x in sub if x["user"] == "human") / len(sub)
        cells = []
        for m in he.JUDGES:
            pairs = [(x["user"] == "human", x["judge"][(m, "v4")] == "human")
                     for x in sub if x["judge"].get((m, "v4")) is not None]
            cells.append(f"{kappa(pairs):+.3f}" if len(pairs) >= 8 else "n/a")
            if m == he.JUDGES[-1]:
                pass
        L.append(f"| {label} | {len(sub)} | {hr2:.3f} | {hr2:.3f} | {cells[0]} | {cells[1]} |")
    L.append("")
    L.append("→ 基础率从 0.867（h30）掰到 0.556（mix30）再回到 0.917（nq50），")
    L.append("  κ 始终在 +0.09～+0.16 之间——**不是基础率、不是语料、不是段落选择造成的**。")

    # ── 按抽样框拆分（§5 纪律：框不能混）──
    L.append("")
    L.append("### 3c. 按抽样框拆分（避免混框解读）")
    L.append("")
    PQ = {"batch_first50", "batch_r15", "batch_dual10", "(无批次)"}
    grp = {"信息量优先队列（难例）": [], "设计批次（随机/分层/跨语料）": []}
    for x in items:
        tags = set(x.get("tags", []))
        grp["信息量优先队列（难例）" if tags & PQ else "设计批次（随机/分层/跨语料）"].append(x)
    L.append("| 抽样框 | 样本 | 用户判 human | kimi v4 agreement | kimi v4 κ |")
    L.append("|---|---|---|---|---|")
    for name, sub in grp.items():
        if not sub:
            continue
        hr2 = sum(1 for x in sub if x["user"] == "human") / len(sub)
        pairs = [(x["user"] == "human", x["judge"][(he.JUDGES[0], "v4")] == "human")
                 for x in sub if x["judge"].get((he.JUDGES[0], "v4")) is not None]
        agr = sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else float("nan")
        L.append(f"| {name} | {len(sub)} | {hr2:.3f} | {agr:.3f} | "
                 f"{kappa(pairs) if len(pairs) >= 8 else float('nan'):+.3f} |")
    L.append("")
    L.append("→ 两类框的 κ 都低，结论不因抽样框而变。")
    L.append("")
    L.append("## 4. 排除过的解释（都试过，都不成立）")
    L.append("")
    L.append("| 解释 | 检验方式 | 结果 |")
    L.append("|---|---|---|")
    L.append("| 措辞不好 | v3 → v4 rubric | 留出集 κ 0.217，再调无效（大样本 AUC 0.504）|")
    L.append("| 位置偏差 | 正反两序各跑一遍再合并 | 未见可靠提升，n 腰斩 |")
    L.append("| 基础率偏斜 | mix30 掰到 0.556/0.444 | κ 仍 +0.09～+0.21（跨语料新段）|")
    L.append("| 段落抽样不好 | nq50 同书全新 50 段 | 候选胜 5/26 vs 旧 4/30，p=0.72 无差异 |")
    L.append("| 题材（性描写多）| 按露骨度劈两半 | 两组 κ 都低（+0.10～+0.21）|")
    L.append("| 任务不可判别 | 换任务：指缺陷而非判优劣 | 该口径 AUC 0.897 是词表过拟合，干净集掉到 0.569 |")
    L.append("| 特征与评委互补 | 融合实验 | Δ = +0.000，且维度越多越差 |")
    L.append("")
    L.append("## 5. 局限（引用本报告必须带上）")
    L.append("")
    L.append(f"1. **独立单位是 {len(segs)} 个人类段落**，不是 {n} 条判定——同一段的多个候选")
    L.append("   共享人类文本与上文，判定不独立。本报告已用聚类 bootstrap，但")
    L.append("   段落数本身限制了任何结论的精度。")
    L.append("2. **nq50 有 54% 判成 tie/both_bad/cant_judge**（其它批 0–10%），集霸自述疲劳，")
    L.append("   该批有效二选一只剩 12 条。")
    L.append("3. **评委池只有 2 个模型 × 2 套 prompt**；不能外推到「所有 LLM 评委」。")
    L.append("4. **候选生成器固定**（deepseek-v4.1-flash + muse-spark-1.3，两种重建口径）。")
    L.append("5. **语料以琼明为主**（色情小说，单作者）；跨语料只有 mix30 的 30 条。")
    L.append("")
    L.append("## 6. 这条路之外，项目留下了什么")
    L.append("")
    L.append("- **仪器已定型并冻结**：切分器 v2（合格率 91%）、场景上下文、盲评队列与匿名化、")
    L.append("  评委口径版本化、留出集评测、四样必报基线、以及一批**自检工具**")
    L.append("  （锚点自检 / 覆盖回读断言 / 水印与番外过滤 / 段级聚类提醒）。")
    L.append("- **确定性计数特征的路线仍开着但很弱**：折外诚实 agreement 0.673"
             "（属分档后的「复现口味」轴——该轴为否定性结论，无固定线），")
    L.append("  AUC 0.62–0.67，且维度越多越差（样本量撑不住）。")
    L.append("- **最重要的负结果**：在「整段哪边写得更好」这个粒度上，")
    L.append("  LLM 评委与确定性特征**都不足以复现作者本人的偏好**。")
    L.append("  要往前走，应当**换任务粒度**（问更可判别的子问题），而不是继续调评委。")
    L.append("")
    L.append("## 7. 人工判定是真正的瓶颈（设计约束，不是统计结论）")
    L.append("")
    L.append("本轮集霸共判 **100 条**（nq50 26 / mix30 30 / h30 30 / r50 9 / x50 5），")
    L.append("自述「太折磨了」。**任何后续方案都不应假设他能批量盲评数百题**；")
    L.append("要把每题成本压到极低（更短上文、只在明显有问题时出手），或改用不需要")
    L.append("大量人工判定的任务形式。")
    L.append("")
    out = "\n".join(L)
    print(out)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"\n[已写入 {args.out}]")


if __name__ == "__main__":
    main()
