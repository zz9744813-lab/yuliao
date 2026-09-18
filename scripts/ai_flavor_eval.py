"""AI 味检测器的验证（拿两套现成真值，不需要集霸再判任何题）。

## 两套真值

1. **构造性真值**：劣化数据集。每对都是"人类原文 vs 按某一类 AI 味改坏的版本"，
   方向**已知**。指标 = 检测器给"改坏版"的分数是否高于原文（AUC / 方向一致率），
   以及逐类型的检出率。
2. **人工真值**：集霸的 63 条划词批注（带 `target`：他标的是候选侧还是人类侧、带字符偏移）。
   指标 = 他标出的片段，检测器有没有在同一位置命中（recall）。

为什么不拿"和你判定的一致性"当指标：§⑱ 已证那是没有对照组的糊问题；
而且你刚说过"降低要求，只拦明显的 AI 味"——所以这里的指标是**检测**，
不是"预测你的偏好"。

用法：
    python scripts/ai_flavor_eval.py --pairs          # 构造性真值
    python scripts/ai_flavor_eval.py --annotations    # 人工批注
    python scripts/ai_flavor_eval.py --sweep          # 阈值扫描
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

from app import config  # noqa: E402
from app.ai_flavor import analyze  # noqa: E402


def _con():
    con = sqlite3.connect(str(ROOT / "data" / "language_genome.db"))
    con.row_factory = sqlite3.Row
    return con


def _usable_pairs() -> list[tuple[str, str, str]]:
    """(human, variant, type)：只取源文本完好的。"""
    con = _con()
    rows = con.execute("""select cc.corruption_type ct, cc.text v, s.text h, s.text_clean hc,
                                s.role role, s.integrity ig
                         from controlled_corruptions cc join segments s on s.id=cc.segment_id
                         where cc.status='ok' and cc.candidate_id is not null""").fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["ig"] or "{}")
        except Exception:
            d = {}
        if r["role"] == "benchmark" or d.get("src_ok") is False:
            continue
        out.append((r["hc"] or r["h"], r["v"], r["ct"]))
    return out


def eval_pairs() -> dict:
    pairs = _usable_pairs()
    if not pairs:
        print("没有可用对照（都进了基准或被源校勘剔除）")
        return {}
    by_type: dict[str, list[int]] = {}
    higher = tie = lower = 0
    sh = sv = 0.0
    for h, v, ct in pairs:
        a, b = analyze(h).score, analyze(v).score
        sh += a
        sv += b
        a2 = by_type.setdefault(ct, [0, 0, 0])
        a2[0] += 1
        if b > a:
            higher += 1
            a2[1] += 1
        elif b < a:
            lower += 1
            a2[2] += 1
        else:
            tie += 1
    n = len(pairs)
    print(f"对照 {n} 对（源文本完好，非基准段）")
    print(f"  检测器给**劣化版**更高分：{higher} ({higher / n:.1%})")
    print(f"  给原文更高分：{lower} ({lower / n:.1%})   持平：{tie}")
    print(f"  平均分：原文 {sh / n:.3f} / 劣化版 {sv / n:.3f}")
    print(f"\n{'类型':<28}{'n':>4}{'劣化版更高':>10}{'原文更高':>9}")
    for ct, (m, hi, lo) in sorted(by_type.items(), key=lambda kv: -(kv[1][1] / max(1, kv[1][0]))):
        print(f"   {ct:<26}{m:>4}{hi / m:>10.2f}{lo / m:>9.2f}")
    return {"n": n, "higher": higher, "lower": lower, "tie": tie,
            "mean_human": sh / n, "mean_variant": sv / n, "by_type": by_type}


def eval_annotations() -> dict:
    """集霸批注的召回：他标出 AI 味的位置，检测器有没有命中。"""
    con = _con()
    rows = con.execute("""select ri.id rid, ri.human_verdict hv, c.text ctext,
                                s.text stext
                         from review_items ri
                         join candidates c on c.id = ri.subject_id
                         join segments s on s.id = c.segment_id
                         where ri.status='done'""").fetchall()
    con.close()
    hit = miss = 0
    by_kind: dict[str, list[int]] = {}
    for r in rows:
        hv = r["hv"]
        if isinstance(hv, str):
            try:
                hv = json.loads(hv)
            except Exception:
                hv = {}
        text = r["ctext"] if False else None
        for a in ((hv or {}).get("annotations") or []):
            side = (a.get("target") or "").lower()
            src = r["ctext"] if side == "candidate" else r["stext"]
            if not src:
                continue
            s0, s1 = int(a.get("start") or 0), int(a.get("end") or 0)
            rep = analyze(src)
            got = any(not (h.end <= s0 or h.start >= s1) for h in rep.hits)
            k = a.get("kind") or "?"
            d = by_kind.setdefault(k, [0, 0])
            d[0] += 1
            if got:
                hit += 1
                d[1] += 1
            else:
                miss += 1
    n = hit + miss
    print(f"集霸批注 {n} 条：检测器命中 {hit} ({hit / max(1, n):.1%})，漏 {miss}")
    for k, (m, h) in by_kind.items():
        print(f"   [{k}] {h}/{m} = {h / max(1, m):.1%}")
    return {"n": n, "hit": hit, "by_kind": by_kind}


def sweep() -> None:
    """阈值扫描：给下游一个"拦多少"的选择（集霸说只拦明显的）。"""
    pairs = _usable_pairs()
    if not pairs:
        return
    scored = [(analyze(h).score, analyze(v).score) for h, v, _ in pairs]
    print(f"{'阈值':>6}{'劣化版被拦':>12}{'原文被误拦':>12}")
    for th in (0.2, 0.3, 0.4, 0.5, 0.6, 0.8):
        a = sum(1 for h, v in scored if v >= th) / len(scored)
        b = sum(1 for h, v in scored if h >= th) / len(scored)
        print(f"{th:>6.1f}{a:>12.1%}{b:>12.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--annotations", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    if args.pairs:
        eval_pairs()
    if args.annotations:
        eval_annotations()
    if args.sweep:
        sweep()
    if not (args.pairs or args.annotations or args.sweep):
        eval_pairs()
        print()
        eval_annotations()
        print()
        sweep()


if __name__ == "__main__":
    main()
