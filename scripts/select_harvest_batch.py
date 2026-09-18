"""自动挑题：「收割 + 校准锚点」混合批（2026-09-16）。

## 要解决什么问题

少数类（候选胜）太少 → κ 与 AUC 的方差压不住（实测少数类 34 条时，
AUC 标准误 0.083、阈值在 30 个折划分下 agreement 波动 0.47–0.74）。
补标注是唯一出路，但纯随机效率低。本脚本用确定性打分模型挑题，提高命中率。

## 为什么不能只挑高分（s30 的教训）

`make_stratified_batch.py` 造 s30 时**只挑高分**，且用了在「优先队列」上估的层先验。
结果：s30 的 score 最高（均值 0.328）但候选胜率最低（**0.133**），
而已判的其它池是 0.31 左右 —— **先验跨抽样框搬运，彻底失效**。

根因：只挑高分 ⇒ 该批**不能代表池子**，每批都在新的框上重新猜，
一旦猜错就污染整批数据，且**事后才发现**。

## 本脚本的做法：「收割 + 锚点」

每批 = **70% 按 score 收割** + **30% 从池内随机抽（锚点）**。

- **收割** 提高少数类命中率（实测 top-20% 的候选胜率 ≈ 0.68 vs 池均值 0.273，**2.5x**）
- **锚点** 是无偏样本 ⇒ 每批都能**自己算出池子的真实基准率**，不再依赖外推先验

锚点让这个流程**有自纠错能力**：若模型对当前池失效，锚点的实测率会立刻暴露，
而不是等几十题判完才发现。

## 校准只用「未被 score 筛过」的数据

`s30` 是按 score 分层的，拿它拟合校准曲线会自洽性偏差（自证），故**显式排除**。
其余已判批次（r25 / none / first50 / dual10 / r15）未被 score 筛选，可用。

## 统计纪律（本项目反复踩过的）

- 打分方向必须**启动即断言**（`score` 与候选胜正相关），否则抽到反向样本
- 预期命中率必须给 **CI**（Wilson），不给点估计当承诺
- 池子不足 / 无可用信号时**显式报错**，不静默产出空批
- 阈值**不做折外调优**——本脚本用的是"按 score 排序取前 K"，规则简单、无超参，
  故不存在阈值过拟合（对比：⑨ 那次 agreement 0.725 就是阈值过拟合）

用法：
    python scripts/select_harvest_batch.py --dry-run
    python scripts/select_harvest_batch.py --n 30 --tag h30
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db
from app.config import BLIND_REVIEW_PROMPT_VERSIONS
from app.models import Candidate, ReviewItem

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
# 建批承诺落盘处：`anchor_check.py` 判完后要拿它做**主检验**（h30 的教训——
# 承诺只打印在终端、没留档，判完就只能靠重建才能对照）。
EXPECTATIONS = Path(__file__).resolve().parent.parent / "data" / "harvest_expectations.json"
EXP = "EXP-0911-B82D"

# 打分模型特征（与 pref_drivers 的稳定入选集一致，纯 numpy）
FEATURES = ("punct_！_per_k", "n_sentences", "sent_len_mean",
            "emotion_word_per_k", "sent_len_min", "connective_per_k")

# 按 score 分层的批次 **不能** 用于校准（自洽性偏差）：拿"按分挑出来的样本"
# 去拟合打分模型，等于自己考自己。h30 同样是按分挑的（21/30 为收割段），
# 必须与 s30 同等对待 —— 否则未来建批时它会悄悄进入校准集。
SCORE_SCREENED_BATCHES = ("s30", "h30")

HARVEST_FRAC = 0.70          # 收割比例
MIN_POOL = 20                # 池子小于此值直接报错
FRAME_TOL = 0.06             # 待判池与校准集的 score 均值差上限；超出即拒绝建批（硬 Gate）


# ── 纯 numpy 逻辑回归（与 pref_drivers / make_stratified_batch 同实现）──
def _standardize(X):
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    return (X - mu) / sd, mu, sd


def _fit_logreg(X, y, l2=2.0, lr=0.3, iters=3000):
    n, d = X.shape
    w, b = np.zeros(d), 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w + b, -30, 30)))
        w -= lr * (X.T @ (p - y) / n + l2 * w / n)
        b -= lr * float((p - y).mean())
    return w, b


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间——小样本下比正态近似稳，且不会越界到 [0,1] 之外。

    末尾显式 clamp：k=0 或 k=n 时公式会有 ~1e-17 的浮点残留，
    使下界变成 -3e-17。作为**概率**，负值是无意义的，故夹紧。
    """
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def _rows() -> list[sqlite3.Row]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = con.execute(
        """select c.id cid, c.prompt_version pv, c.text,
                  rd.deltas,
                  ri.status ri_status, ri.reasons ri_reasons, ri.human_verdict hv
           from candidates c
           join residuals_det rd on rd.candidate_id = c.id
           left join review_items ri
                  on ri.subject_id = c.id and ri.experiment_id = c.experiment_id
           where c.experiment_id = ? and c.status = 'ok'""", (EXP,)).fetchall()
    con.close()
    return out


def build() -> dict:
    """返回打分函数 + 校准集 + 待判池 + 诊断信息。"""
    rows = _rows()
    rows = [r for r in rows if r["pv"] in BLIND_REVIEW_PROMPT_VERSIONS]
    if not rows:
        raise SystemExit("白名单内没有任何带 det 指标的候选——检查 "
                         "BLIND_REVIEW_PROMPT_VERSIONS 与 residuals_det。")

    cal, skip_screened, pool_short = [], 0, 0
    pool: list[tuple[str, float]] = []
    for r in rows:
        d = json.loads(r["deltas"])
        vec = [d[k] for k in FEATURES]
        done = r["ri_status"] == "done"
        if done:
            rs = json.loads(r["ri_reasons"]) if r["ri_reasons"] else []
            bt = next((x[6:] for x in rs if x.startswith("batch_")), "none")
            if bt in SCORE_SCREENED_BATCHES:
                skip_screened += 1
                continue
            hv = json.loads(r["hv"]) if r["hv"] else {}
            w = hv.get("winner_resolved")
            if w in ("human", "candidate"):
                cal.append((vec, 1.0 if w == "candidate" else 0.0))
        else:
            if len((r["text"] or "").strip()) < 20:
                pool_short += 1
                continue
            pool.append((r["cid"], vec))

    if len(cal) < 30:
        raise SystemExit(f"可用于校准的已判样本只有 {len(cal)} 条（<30），"
                         f"无法拟合可靠的打分模型。")
    if len(pool) < MIN_POOL:
        raise SystemExit(f"待判池只有 {len(pool)} 条（<{MIN_POOL}）——池子已空，"
                         f"需要先生成新候选。这是显式报错，不是静默空批。")

    X = np.array([v for v, _ in cal], dtype=float)
    y = np.array([lb for _, lb in cal], dtype=float)
    Xs, mu, sd = _standardize(X)
    w, b = _fit_logreg(Xs, y)

    def score(vec) -> float:
        z = ((np.array(vec) - mu) / sd) @ w + b
        return float(1 / (1 + np.exp(-np.clip(z, -30, 30))))

    # **方向断言**：方向写反会抽到完全相反的样本（本项目真实踩过）
    rho = float(np.corrcoef(1 / (1 + np.exp(-(Xs @ w + b))), y)[0, 1])
    if rho <= 0:
        raise SystemExit(f"打分方向错误：score 与候选胜相关 r={rho:+.3f}（应为正）。"
                         f"检查 FEATURES 顺序是否与 deltas 的键一致。")

    pool_sc = [(cid, score(v)) for cid, v in pool]
    cal_sc = np.array([score(v) for v, _ in cal])
    return {"score": score, "cal": cal, "cal_sc": cal_sc, "cal_y": y,
            "pool": pool_sc, "rho": rho, "n_cal": len(cal),
            "n_skip_screened": skip_screened, "n_pool_short": pool_short}


def cumulative_rate(cal_sc: np.ndarray, cal_y: np.ndarray,
                    t: float) -> tuple[float, int, int]:
    """P(候选胜 | score ≥ t) 及 Wilson CI。用累积而非分箱，避免非单调假象。"""
    m = cal_sc >= t
    n = int(m.sum())
    if n == 0:
        return (float("nan"), 0, 0)
    k = int(cal_y[m].sum())
    return (k / n, k, n)


def pool_base_rate(pool_scores) -> float:
    """待判池基准率的估计 = **模型预测概率的均值**。

    ⚠ 这是本项目踩过的一个陷阱，故单独成函数并加测试：
    不能用「累积上尾率」去平均整池 —— 那会把高分段接近 1 的上尾率摊到全池，
    算出 0.49 这种与 score 均值 0.27 **自相矛盾**的数（我第一版就这么写错了）。
    逻辑回归带截距、在拟合集上均值即基准率，故 mean(score) 才是标准估计。
    """
    arr = np.asarray(list(pool_scores), dtype=float)
    if arr.size == 0:
        raise ValueError("空池无法估计基准率——静默返回 0 会让下游误判。")
    return float(arr.mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="本批题数")
    ap.add_argument("--tag", default=None, help="批次标签；默认 h<n>")
    ap.add_argument("--seed", type=int, default=20260916)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="帧检查失败时仍要建批（默认拒绝：外推不可信，s30/h30 都栽在这里）")
    args = ap.parse_args()
    tag = args.tag or f"h{args.n}"

    db.init_db()
    info = build()
    pool = sorted(info["pool"], key=lambda x: -x[1])

    print("=" * 78)
    print("自动挑题：收割 + 校准锚点")
    print("=" * 78)
    print(f"校准集 {info['n_cal']} 条（已排除按 score 分层的批次 "
          f"{info['n_skip_screened']} 条：{SCORE_SCREENED_BATCHES}）")
    print(f"  方向断言：score 与候选胜 r={info['rho']:+.3f} ✓")
    print(f"待判池 {len(pool)} 条（另有 {info['n_pool_short']} 条文本过短已跳过）")
    print()

    n_harvest = max(1, round(args.n * HARVEST_FRAC))
    n_anchor = args.n - n_harvest
    if len(pool) < args.n:
        raise SystemExit(f"池子只有 {len(pool)} 条，不足以取 {args.n} 条。")

    # 锚点：从**未被收割占用**的部分随机抽，保证与收割不重叠
    rng = random.Random(args.seed)
    harvest = pool[:n_harvest]
    taken = {cid for cid, _ in harvest}
    rest = [x for x in pool if x[0] not in taken]
    anchor = rng.sample(rest, n_anchor)

    # 预期命中（用累积校准率，给 CI）
    thresh = harvest[-1][1]
    rate, k, n = cumulative_rate(info["cal_sc"], info["cal_y"], thresh)
    lo, hi = wilson(k, n)
    # 池基准率：见 pool_base_rate 的注释（不要用累积上尾率平均整池）
    pool_mean = pool_base_rate([s for _, s in pool])
    print(f"收割 {n_harvest} 条（score ≥ {thresh:.3f}）+ 锚点 {n_anchor} 条（池内随机）")
    print(f"  收割段校准命中率 = {rate:.3f}  [{lo:.3f},{hi:.3f}]  (Wilson 95%, 基于校准集 n={n})")
    print(f"  待判池基准率（模型外推，非实测）= {pool_mean:.3f}")
    print(f"  锚点段预期命中率 ≈ {pool_mean:.3f}（锚点为池内随机，期望即池基准率）")
    exp_total = rate * n_harvest + pool_mean * n_anchor
    print()
    print(f"  预期候选胜 ≈ {exp_total:.1f} / {args.n} 条（命中率 {exp_total/args.n:.3f}）")
    print(f"  对照：纯随机 {args.n} 条 ≈ {pool_mean*args.n:.1f} 条"
          f"  → 提升 {exp_total/(pool_mean*args.n):.2f}x")
    print(f"  ⚠ CI 只覆盖收割段；区间宽是因为校准集小，不是模型不稳。")
    print()

    print(f"{'序':>4s}{'来源':8s}{'score':>9s}{'候选胜期望':>11s}")
    rows_out = []
    for i, (cid, sc) in enumerate(harvest, 1):
        rows_out.append((cid, "harvest", sc, rate))
        if i <= 5 or i == len(harvest):
            print(f"{i:4d}{'收割':8s}{sc:9.3f}{rate:11.3f}")
        elif i == 6:
            print(f"{'…':>4s}{'':8s}{'':9s}{'':11s}")
    for i, (cid, sc) in enumerate(anchor, 1):
        r_, _, _ = cumulative_rate(info["cal_sc"], info["cal_y"], sc)
        rows_out.append((cid, "anchor", sc, r_))
    print(f"（锚点 {n_anchor} 条已隐藏 score，避免事后按分数挑队）")

    print()
    print("=== 帧检查：待判池 vs 校准集 的 score 分布 ===")
    ps = np.array([s for _, s in pool])
    cs = info["cal_sc"]
    print(f"{'':10s}{'n':>5s}{'均值':>9s}{'中位':>9s}{'p90':>9s}{'max':>9s}")
    print(f"{'校准集':10s}{len(cs):5d}{cs.mean():9.3f}{np.median(cs):9.3f}"
          f"{np.percentile(cs,90):9.3f}{cs.max():9.3f}")
    print(f"{'待判池':10s}{len(ps):5d}{ps.mean():9.3f}{np.median(ps):9.3f}"
          f"{np.percentile(ps,90):9.3f}{ps.max():9.3f}")
    frame_ok = abs(ps.mean() - cs.mean()) <= FRAME_TOL
    if frame_ok:
        print("  score 分布接近 → 框一致性尚可，外推风险较低。")
    else:
        # 硬 Gate（2026-09-16 h30 之后）：原来这里只打印警告，批次照建 ——
        # 而 h30 正是"警告已打印、然后按这个外推建批、随后外推被推翻"（承诺 0.696 → 实测 3/21）。
        # **不拦人的警告等于没有 Gate。** 故改为默认拒绝，要建得显式 --force。
        print("  ✗ 两池 score 均值差 >"
              f"{FRAME_TOL} → 抽样框不同，**外推不可信**（s30 与 h30 都栽在这里）。")
        print(f"    实况：待判池 max score = {ps.max():.3f}，校准集 max = {cs.max():.3f}"
              " —— 收割批把高分题一批批抽走，池子被撇脂，模型的外推率随之失真。")
        if not args.force:
            print()
            print("  已拒绝建批（不是警告，是 Gate）。两条出路：")
            print("    ① 抽纯随机批（无偏，不需要外推率）：python scripts/make_random_batch.py")
            print("    ② 确认要用打分挑题 → 加 --force，并**把锚点段当成唯一的率估计来源**。")
            raise SystemExit(2)
        print("  --force 已给：继续，但判完后一切以锚点/IPW 实测为准，不要引用上面的外推率。")

    if args.dry_run:
        print("\ndry-run：未写库")
        return

    written = 0
    with db.session() as s:
        for cid, src, sc, exp in rows_out:
            ri = s.query(ReviewItem).filter_by(experiment_id=EXP, subject_id=cid).first()
            if ri is None:
                ri = ReviewItem(experiment_id=EXP, subject_type="candidate",
                                subject_id=cid, priority=0.0, status="pending",
                                reasons=["harvest_anchor"])
                s.add(ri)
            keep = [x for x in (ri.reasons or []) if not x.startswith("batch_")]
            # stratum 与 w 供逆概率加权；锚点是池内随机、收割是按分位，两者入样概率不同
            keep = [x for x in keep if not x.startswith(("stratum:", "w:"))]
            stratum = "HARVEST" if src == "harvest" else "ANCHOR"
            w_inc = (n_harvest / len(pool)) if src == "harvest" else (n_anchor / len(rest))
            ri.reasons = keep + [f"batch_{tag}", f"stratum:{stratum}",
                                 f"w:{max(w_inc, 1e-6):.4f}", f"score:{sc:.4f}"]
            ri.status = "pending"
            written += 1
        s.commit()

    # 承诺落盘。半途改过主检验：锚点只有 9 条、功效极低（h30 实测 P(≤1)=0.253 放过了
    # 真问题），真正有功效的是收割段那 21 条 → 收割段承诺必须留档，否则判完无从对照。
    try:
        all_exp = (json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
                   if EXPECTATIONS.exists() else {})
    except Exception:
        all_exp = {}
    all_exp[tag] = {"harvest_rate": rate, "harvest_ci": [lo, hi], "harvest_cal_n": n,
                    "pool_base_rate": pool_mean, "n_harvest": n_harvest, "n_anchor": n_anchor,
                    "expect_overall": exp_total / args.n,
                    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    EXPECTATIONS.write_text(json.dumps(all_exp, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写库 {written} 条，标签 batch_{tag}")
    print(f"建批承诺已落盘 → {EXPECTATIONS.name}（anchor_check 主检验要用）")
    print(f"前端：http://127.0.0.1:8787/?batch={tag}")
    print(f"判完：python scripts/heldout_eval.py --batch {tag}   # 含逆概率加权 κ")
    print()
    print("【判完之后必看】python scripts/anchor_check.py --batch " + tag)
    print("  主检验 = 收割段是否兑现上面的外推率（这一侧才有功效）")
    print("  次检验 = ANCHOR 段 vs 池基准率（n 小、功效低，只能排除灾难性漂移）")
    print("  ⚠ h30 教训：只测锚点会放过真问题——承诺 0.696 的收割段只打出 3/21。")


if __name__ == "__main__":
    main()
