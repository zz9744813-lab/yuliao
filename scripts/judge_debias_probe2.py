"""T7 探路·第二轮（2026-09-20）：评委方向偏差校正 + 新增变换，用库内现有数据。

与第一轮（scripts/judge_debias_probe.py，结论"不采信过线"）的差别：

1. **新增三变换**：
   - `len` 长度基线：预言"短的一侧是人类原文"（军师 P1-3 在 nat-v1/hvai 上发现
     "只选较短"高达 0.944/0.872——它不是评委校正，而是 Gate 线本身的污染源，
     必须作为对照进表）；
   - `ens` 集成多数：四家评委逐题投票，2:1/2:0 多数决（平票丢弃）；
   - `ens_flip`：集成后再按控制臂 r̂ 反转方向。
2. **Gate 判定加对照**：任何变换要主张"过线"，agreement 不仅 ≥0.70，还必须
   **≥ 长度基线在同一子集上的 agreement**——否则过线只是捡了长度便宜
   （军师 P1-3 的教训：长度混淆曾把四个子基准 pass 全部打回）。
3. 数据仍是 corr24（唯一"集霸判定×评委判定"共存面），统计口径不变：
   排除制（集霸 tie/both_bad/cant_judge 剔除）为主，五分类严格口径（分母 24）
   为辅；必报 κ、置换 p、恒定多数类、位置基线；段聚类上限注明。

只读库、零 LLM 调用。用法：
    python scripts/judge_debias_probe2.py [--md]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.judge_debias_probe import (  # noqa: E402
    BATCH, CONTROL_TYPE, GATE, load_probe,
    agreement_bin, kappa_bin, perm_null, resolve_pick,
)

PERM_B = 20000
SEED = 20260920
MODELS = ("moonshotai/kimi-k3", "deepseek/deepseek-v4.1-flash",
          "qoder/Qwen3.8-Flash", "wb/hy4-preview-f", "agnes-3.0-flash")
PV_HINT = "heldout_near1"       # corr24 上实际有判定的口径前缀（装载时自动发现）


def _lengths(session_factory, items: dict) -> dict:
    """每题（候选 id）的 (人类侧字数, 劣化侧字数)。只读。"""
    from app.models import ControlledCorruption, Segment
    out = {}
    with session_factory() as s:
        ccs = {c.candidate_id: c for c in
               s.query(ControlledCorruption).filter(ControlledCorruption.candidate_id.isnot(None)).all()}
        for cid in items:
            cc = ccs.get(cid)
            if cc is None:
                continue
            seg = s.get(Segment, cc.segment_id)
            if seg is None:
                continue
            human = len((seg.text_clean or seg.text or "").strip())
            variant = len((cc.text or "").strip())
            out[cid] = (human, variant)
    return out


def _baseline_len_agreement(session_factory, items: dict) -> dict:
    """长度基线在同一批题上的 agreement（排除制 + 严格两种口径）。"""
    lens = _lengths(session_factory, items)
    agree_n = agree_k = strict_k = 0
    per: dict[str, list[int]] = {}
    for cid, it in items.items():
        hv, vv = lens.get(cid, (None, None))
        if hv is None or hv == vv:
            continue                       # 无长度差：基线弃权
        pred = "human" if hv < vv else "candidate"
        u = it.user
        strict_k += int(pred == u)
        if u in ("human", "candidate"):
            agree_n += 1
            k = 1 if pred == u else 0
            agree_k += k
            per.setdefault(it.kind, [0, 0])
            per[it.kind][0] += 1
            per[it.kind][1] += k
    return {"n_clear": agree_n, "agree": (agree_k / agree_n if agree_n else None),
            "by_kind": {k: {"n": v[0], "correct": v[1]} for k, v in sorted(per.items())},
            "strict_k_24": strict_k}


def _ensemble(session_factory, data) -> dict:
    """四家评委逐题集成多数：resolved 空间 {'human','candidate'}。"""
    lens = _lengths(session_factory, data.items)
    # 找 corr24 上判定数最多的口径（pv）作为集成面板
    pv_votes: Counter = Counter()
    for (model, pv), cells in data.judges.items():
        pv_votes[pv] += len([c for c in cells.values() if c.resolved in ("human", "candidate")])
    pv_best, _ = pv_votes.most_common(1)[0]
    rows = []
    for cid, it in sorted(data.items.items()):
        if it.user not in ("human", "candidate"):
            continue
        votes = []
        for (model, pv), cells in data.judges.items():
            if pv == pv_best and cid in cells:
                votes.append(cells[cid].resolved)
        if not votes:
            continue
        top, k = Counter(votes).most_common(1)[0]
        if k * 2 <= len(votes):
            continue                       # 平票/无多数：弃权
        hv, vv = lens.get(cid, (None, None))
        pred_len = ("human" if hv < vv else "candidate") if hv is not None and hv != vv else None
        rows.append({"cid": cid, "user": it.user, "ens": top, "k": k,
                     "nvotes": len(votes), "len_pred": pred_len})
    agree = sum(1 for r in rows if r["ens"] == r["user"])
    n = len(rows)
    ens_len = sum(1 for r in rows if r["len_pred"] == r["user"]) if rows else 0
    n_len = sum(1 for r in rows if r["len_pred"] is not None)
    return {"pv": pv_best, "n": n, "agree": (agree / n if n else None),
            "agree_k": agree, "len_agree": (ens_len / n_len if n_len else None),
            "len_n": n_len, "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args()
    from app import db
    data = load_probe(db.session)

    print(f"corr24 集霸判定 {len(data.items)} 题；评委口径 "
          f"{sorted({pv for _, pv in data.judges})}；"
          f"模型 {sorted({m for m, _ in data.judges})}")

    out_lines = []
    # ① 长度基线（Gate 对照物）
    bl = _baseline_len_agreement(db.session, data.items)
    print(f"\n[长度基线] 明确胜方子集 n={bl['n_clear']}，agreement="
          f"{bl['agree'] if bl['agree'] is None else round(bl['agree'], 3)}，"
          f"分型={bl['by_kind']}，严格口径(24)={bl['strict_k_24']}/24")

    # ② 各家原始/翻转在排除制子集上的 agreement（复用第一轮口径）
    print("\n[分模型] 变换=raw / flip（排除制 agreement）")
    best_rows = []
    for (model, pv), cells in sorted(data.judges.items()):
        pairs = []
        for cid, it in sorted(data.items.items()):
            if it.user not in ("human", "candidate") or cid not in cells:
                continue
            pred = cells[cid].resolved
            pairs.append((1 if it.user == "human" else 0,
                          1 if pred == "human" else 0))
        if not pairs:
            continue
        acc = agreement_bin(pairs)
        flipped = [(u, 1 - j) for u, j in pairs]
        acc_f = agreement_bin(flipped)
        agree = sum(1 for u, j in pairs if u == j)
        n = len(pairs)
        pn = perm_null([u for u, _ in pairs], [j for _, j in pairs], b=PERM_B)
        print(f"  {model:34s} {pv:26s} n={n:2d} acc={acc:.3f} "
              f"(flip {acc_f:.3f}, perm p={pn['p_agree']:.4f})")
        best_rows.append({"model": model, "pv": pv, "n": n, "acc": acc,
                          "acc_flip": acc_f, "perm_p": pn["p_agree"]})

    # ③ 集成
    ens = _ensemble(db.session, data)
    if ens["n"]:
        pairs = [(1 if r["user"] == "human" else 0,
                  1 if r["ens"] == "human" else 0) for r in ens["rows"]]
        acc = agreement_bin(pairs)
        pn = perm_null([u for u, _ in pairs], [j for _, j in pairs], b=PERM_B)
        print(f"\n[集成多数] 口径={ens['pv']} n={ens['n']} agreement={acc:.3f} "
              f"(perm p={pn['p_agree']:.4f})；同子集长度基线={ens['len_agree'] if ens['len_agree'] is None else round(ens['len_agree'], 3)}"
              f"（n={ens['len_n']}）")
        out_lines.append({"ensemble": {"n": ens["n"], "acc": acc,
                                       "perm_p": pn["p_agree"],
                                       "len_agree": ens["len_agree"]}})
        gate_pair = {"model": "ens(4家)", "pv": ens["pv"], "n": ens["n"],
                     "acc": acc, "acc_flip": None, "perm_p": pn["p_agree"]}
    else:
        gate_pair = None
        print("\n[集成多数] 无可用多数票")

    # ④ Gate 判定（含长度对照）
    # 过线须同时满足：acc≥Gate、≥长度基线、置换 p<0.05——n=5 时 acc=1.0
    # 的"过线"正是纪律①的陷阱（恰好越过 Gate 线的数字先找反证）
    print(f"\n[Gate {GATE}]（排除制；过线还须 ≥长度基线 且 置换 p<0.05）")
    for r in best_rows:
        beats = (r["acc"] >= GATE and r["perm_p"] < 0.05 and
                 (bl["agree"] is None or r["acc"] >= bl["agree"]))
        print(f"  {r['model']}: acc={r['acc']:.3f} acc≥线={r['acc'] >= GATE} "
              f"≥长度基线={bl['agree'] is None or r['acc'] >= bl['agree']} "
              f"p<0.05={r['perm_p'] < 0.05} → 过线={beats}")
    if gate_pair and gate_pair["acc"] is not None:
        beats = (gate_pair["acc"] >= GATE and gate_pair["perm_p"] < 0.05 and
                 (bl["agree"] is None or gate_pair["acc"] >= bl["agree"]))
        print(f"  集成: acc={gate_pair['acc']:.3f} acc≥线={gate_pair['acc'] >= GATE} "
              f"≥长度基线={bl['agree'] is None or gate_pair['acc'] >= bl['agree']} "
              f"p<0.05={gate_pair['perm_p'] < 0.05} → 过线={beats}")

    if args.md:
        rep = {"length_baseline": bl, "per_model": best_rows, "ensemble": ens,
               "gate": GATE}
        Path("data/_dbg/judge_debias_probe2_report.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        print("\n[已落盘] data/_dbg/judge_debias_probe2_report.json")


if __name__ == "__main__":
    main()
