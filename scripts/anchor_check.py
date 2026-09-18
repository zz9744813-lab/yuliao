"""锚点自检（2026-09-16，h30 判完后修）：核对本批的**选择器承诺是否成立**。

## 为什么必须有这个脚本

`select_harvest_batch.py` 每批掺 30% 锚点，就是为了让**每批自己算出池子的真实
命中率**，不再依赖外推先验（s30 的翻车教训：先验跨抽样框搬运，判完才发现）。
但挑题脚本只把 `stratum:` / `w:` 写进 `review_items.reasons`，**没有任何脚本读回来**：
`heldout_eval` / `noise_report` / `large_sample_diag` 里 stratum 出现 0 次。
设计的自检没有工具，等于没有自检。

## ⚠ 第一版写错过什么（h30 实测）

第一版只做了「锚点实测 vs 建批外推池基准率」这一侧的比对，并且据此打印
「✓ 框一致，挑题策略可继续」。h30 判完后它**放过了真问题**：

| | 承诺 | 实测 | 检验 |
|---|---|---|---|
| 收割段 | 0.696 | **3/21 = 0.143** | P(≤3)=2.4e-07 ✗ 被推翻 |
| 锚点段 | 0.271 | 1/9 = 0.111 | P(≤1)=0.253，**判不出来** |

根因是**功效不对称**：锚点只有 9 条，CI 宽到 [0.02, 0.44]，几乎放行一切；
真正有功效的是收割段那 21 条（它承载了建批时承诺的提升）。
**只测锚点 = 只测了没有功效的那一侧。**

因此本版把三个检验都做，且**判定以收割段承诺为主**：
1. **选择器承诺**（主检验）：P(收割实测 ≤ k | n_harvest, p=建批承诺率) —— 有功效
2. **提升**：收割率 vs 锚点率（承诺的提升是否真的出现）
3. **框漂移**：锚点率 vs 建批池基准率 —— 功效低，只用于排除灾难性漂移

## 报的数（口径各不相同，别混用）

1. **ANCHOR 实测命中率** —— 池内随机抽，无偏估计「未收割部分」的候选胜率。
   ⚠ 它抽自 `rest = 池 − top-K`，估计的是**剩余池**，池整体率略高。
2. **IPW 反推的池整体命中率** —— 按入样概率 `w` 的 Hájek 估计（Σw·y/Σw，w=1/入样概率），
   把收割段与锚点段合起来还原池子。这是本批能给出的**最强无偏读数**。
   ⚠ 成立前提：各层的 `w` 记录正确、且各层合起来覆盖目标池。
3. **HARVEST 实测命中率** —— 与建批承诺直接对照的那一段。

用法：
    python scripts/anchor_check.py --batch h31                      # 自动读建批时落盘的承诺
    python scripts/anchor_check.py --batch h30 --expect 0.271 --expect-harvest 0.696

可测试性：`analyze(..., db_path=...)` **显式收库路径**。不要靠 monkeypatch 模块全局
`DB` —— 见 `heldout_eval._position_diagnostic` 的同款注释（模块双实例会导致静默 None）。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from select_harvest_batch import wilson  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
EXPECTATIONS = Path(__file__).resolve().parent.parent / "data" / "harvest_expectations.json"
EXP = "EXP-0911-B82D"
ALPHA = 0.05


def _tag(reasons: list, prefix: str) -> str | None:
    for t in reasons:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix):]
    return None


def _binom_tail_le(k: int, n: int, p: float) -> float:
    """P(X ≤ k)，X ~ Binomial(n, p)。单尾：只问「是不是低得不像承诺的率」。"""
    if n == 0:
        return float("nan")
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def load_expectations(batch: str, path: Path | str | None = None) -> dict:
    """读建批时落盘的承诺值（`select_harvest_batch.py` 写入）。缺失则返回空 dict。"""
    p = Path(path or EXPECTATIONS)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(batch) or {}
    except Exception:
        return {}


def collect(batch: str, db_path: Path | str = DB, exp: str | None = None) -> dict:
    """取该批已判条目的（是否候选胜, 层, 入样概率）。不静默：空/全弃权都返回可判定的计数。

    exp：实验号。None = 不限实验（跨语料混合批：同批题分属多个实验）。
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sql = ("""select ri.human_verdict, ri.reasons
              from review_items ri
              join candidates c on c.id = ri.subject_id
              where ri.reasons like ? and ri.status = 'done'
                and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""")
    args: list = [f"%batch_{batch}%"]
    if exp:
        sql += " and ri.experiment_id = ?"
        args.append(exp)
    rows = con.execute(sql, args).fetchall()
    con.close()

    items, dropped = [], 0
    for r in rows:
        hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            dropped += 1            # both_bad / equal / cant_judge：不进命中率分母
            continue
        reasons = json.loads(r["reasons"] or "[]")
        p = _tag(reasons, "w:")
        items.append({"cand_win": w == "candidate",
                      "stratum": _tag(reasons, "stratum:"),
                      "p": float(p) if p else None})
    return {"n_done": len(rows), "n_dropped": dropped, "items": items}


def _stratum_stats(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for it in items:
        st = it["stratum"] or "(未标 stratum)"
        s = out.setdefault(st, {"n": 0, "k": 0})
        s["n"] += 1
        s["k"] += int(it["cand_win"])
    for s in out.values():
        s["rate"] = s["k"] / s["n"]
        s["ci"] = wilson(s["k"], s["n"])
    return out


def ipw_overall(items: list[dict]) -> dict | None:
    """Hájek 估计：Σ(w·y)/Σw，w = 1/入样概率。缺 w 则返回 None（由调用方明说，不静默）。"""
    if not items or any(it["p"] is None for it in items):
        return None
    ws = [1.0 / it["p"] for it in items]
    W = sum(ws)
    rate = sum(w * int(it["cand_win"]) for it, w in zip(items, ws)) / W
    n_eff = W * W / sum(w * w for w in ws)
    k_eff, n_eff_i = round(rate * n_eff), max(1, round(n_eff))
    return {"rate": rate, "n_eff": n_eff, "ci": wilson(k_eff, n_eff_i)}


def analyze(batch: str, expect: float | None = None,
            expect_harvest: float | None = None,
            db_path: Path | str = DB,
            expectations_path: Path | str | None = None,
            exp: str | None = None) -> dict:
    """算数并打印。返回 dict 供测试断言（含 verdict 字段）。"""
    exp_meta = load_expectations(batch, expectations_path)
    if expect is None:
        expect = exp_meta.get("pool_base_rate")
    if expect_harvest is None:
        expect_harvest = exp_meta.get("harvest_rate")

    res = collect(batch, db_path, exp)
    items = res["items"]
    print(f"批次 {batch}：已判 {res['n_done']} 条，其中可用（human/candidate）"
          f"{len(items)} 条，弃权或 both_bad {res['n_dropped']} 条")
    if not items:
        raise SystemExit(
            f"批次 {batch} 没有可用的 human/candidate 判定 —— 空数据不继续"
            f"（判定前后都别把空当成功）。")

    stats = _stratum_stats(items)
    print()
    for st in sorted(stats):
        s = stats[st]
        print(f"  {st:12s} n={s['n']:3d}  候选胜={s['k']:3d}  实测命中率={s['rate']:.3f}"
              f"  Wilson 95% CI [{s['ci'][0]:.3f}, {s['ci'][1]:.3f}]")

    ipw = ipw_overall(items)
    print()
    if ipw:
        print(f"  {'IPW 池整体':12s} n_eff={ipw['n_eff']:5.1f}  命中率={ipw['rate']:.3f}"
              f"  （n_eff 上的近似 CI [{ipw['ci'][0]:.3f}, {ipw['ci'][1]:.3f}]）")
        print("  ↑ 本批对「池整体候选胜率」的最强无偏读数（Σ(w·y)/Σw，w=1/入样概率）")
    else:
        print("  ⚠ 本批有条目缺 `w:`（入样概率），IPW 无法计算 —— 不静默跳过，明说。")

    out = {"batch": batch, "n_done": res["n_done"], "n_used": len(items),
           "strata": stats, "ipw": ipw, "verdict": "no_expect",
           "expect": expect, "expect_harvest": expect_harvest,
           "p_harvest": None, "lift": None}

    anchor, harvest = stats.get("ANCHOR"), stats.get("HARVEST")

    # ── 主检验：收割段是否兑现建批承诺（有功效的一侧）──
    if harvest and expect_harvest is not None:
        p = _binom_tail_le(harvest["k"], harvest["n"], expect_harvest)
        out["p_harvest"] = p
        print()
        print("=== 选择器承诺核验（主检验）===")
        print(f"  收割段承诺 {expect_harvest:.3f} → 期望 {expect_harvest*harvest['n']:.1f}/"
              f"{harvest['n']}，实测 {harvest['k']}/{harvest['n']} = {harvest['rate']:.3f}")
        print(f"  P(实测 ≤ {harvest['k']} | n={harvest['n']}, p={expect_harvest:.3f})"
              f" = {p:.2e}")
        if p < ALPHA:
            out["verdict"] = "selector_falsified"
            print("  ✗ 承诺被推翻：收割段远低于建批外推 → **不要再用当前 score 挑题**。")
            print("    这不是运气差（p 已算），而是该模型对这个池子没有判别力 / 外推未迁移。")
        else:
            print("  ✓ 未推翻承诺（注意：这只是「没被抓到」，不等于证明有效）。")
    elif harvest and expect_harvest is None:
        print()
        print("  ⚠ 没有建批时的收割段承诺值 → 无法做主检验。")
        print("    （旧批次未落盘承诺；`select_harvest_batch.py` 现已写入 "
              f"{EXPECTATIONS.name}，用 --expect-harvest 也可手动给。）")

    # ── 提升：收割是否真的高于锚点 ──
    if harvest and anchor:
        lift = harvest["rate"] / anchor["rate"] if anchor["rate"] > 0 else float("inf")
        out["lift"] = lift
        print()
        print("=== 提升核验 ===")
        print(f"  收割 {harvest['k']}/{harvest['n']} = {harvest['rate']:.3f}  vs  "
              f"锚点 {anchor['k']}/{anchor['n']} = {anchor['rate']:.3f}  → 提升 {lift:.2f}x")
        if harvest["rate"] <= anchor["rate"]:
            if out["verdict"] in ("no_expect",):
                out["verdict"] = "no_lift"
            print("  ✗ 收割段**不高于**锚点 → 本批没有任何可证实的提升"
                  "（锚点 n 小，只能说明「没测出提升」，不能说「一定没有」）。")

    # ── 框漂移：功效低，只用于排除灾难性漂移 ──
    if expect is not None:
        print()
        if not anchor:
            print("  ⚠ 本批没有 ANCHOR 段，无法做框漂移核验。")
            if out["verdict"] == "no_expect":
                out["verdict"] = "no_anchor"
        else:
            lo, hi = anchor["ci"]
            print(f"=== 框漂移核验（低功效）===")
            print(f"  ANCHOR 实测 {anchor['rate']:.3f} [{lo:.3f}, {hi:.3f}] vs 建批外推 {expect:.3f}")
            if not (lo <= expect <= hi):
                out["verdict"] = "frame_drift"
                print("  ✗ 外推值落在 CI 外 → 框可能漂移（s30 的形态）。")
            else:
                if out["verdict"] == "no_expect":
                    out["verdict"] = "consistent"
                print(f"  · 落在 CI 内。⚠ n={anchor['n']} 的 CI 这么宽，**判不出**中等偏离——")
                print("    这一侧几乎放行一切，**不能**据此说「框一致、可继续」。")
                print("    真正有功效的是上面的主检验（收割段 21 条）。")

    if out["verdict"] == "no_expect":
        print()
        print("  未给承诺值：只报实测，不做判定。")
    print()
    print(f"判定 = {out['verdict']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--expect", type=float, default=None,
                    help="建批时外推的池基准率（锚点期望）；默认读 data/harvest_expectations.json")
    ap.add_argument("--expect-harvest", type=float, default=None,
                    help="建批时外推的收割段命中率（主检验用）；默认读同一个 json")
    ap.add_argument("--db", default=None, help="库路径（默认生产库；测试传临时库）")
    ap.add_argument("--expectations", default=None, help="承诺值 JSON 路径")
    ap.add_argument("--exp", default=None, help="实验号；默认由批次标签反查")
    args = ap.parse_args()
    exp = args.exp
    if exp is None:
        import heldout_eval as he
        exps = he.experiments_of_batch(args.batch, args.db or DB)
        if len(exps) == 1:
            exp = exps[0]                     # 单实验批：加上过滤器（批次打错字时更快报错）
        elif len(exps) > 1:
            exp = None                        # 跨语料混合批：不限实验，池化统计
            print(f"批次 {args.batch} 跨 {len(exps)} 个实验 → 池化统计（逐语料用 --exp）")
    analyze(args.batch, args.expect, args.expect_harvest,
            args.db or DB, args.expectations, exp)


if __name__ == "__main__":
    main()
