"""基准集构建 —— 总方案 §14（Hidden Benchmark / Hidden Set）。

## 为什么需要它

§53 的六条成功标准（Human Preference↑ / Implicitness↑ / Hidden Benchmark↑ …）里，
**没有一条现在测得了**，因为没有固定的、带答案的、不进训练的题集。
此前所有数字都来自"当时那批候选"，换了语料/换了题就不可比。

§14 明确把 **Controlled Corruption Detection** 列为基准的必含项——而劣化数据集
**自带答案**（哪边是人类原文），是整套体系里唯一不需要集霸再判一次的题源。

## 冻结与隔离（两条都是硬要求）

1. **冻结文本**：条目里存 A/B 原文，不只是 segment_id。语料清洗、切分器升级都会
   改变 segment 的内容；冻结后同一版基准的分数才能跨时间比较（§14 Regression）。
2. **隔离**：只取 `Segment.role='benchmark'` 的段，而这些段的文本已经
   被 `export_training.py` 排除在训练导出之外（§14：不得被训练读取）。

## 用法

    python scripts/benchmark_build.py --name cc-v1 --kind corruption_detection
    python scripts/benchmark_build.py --scan
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.models import BenchmarkItem, BenchmarkSet, ControlledCorruption, Segment  # noqa: E402
from app.ids import new_id  # noqa: E402


def build_corruption_detection(name: str, version: int = 1, seed: int = 20260918,
                               dry_run: bool = False) -> dict:
    """把 `role='benchmark'` 段上的劣化对做成"哪边是原文"的判别题。

    位置用固定种子随机化（可复现），答案记在 item 上。
    只收：源文本完好（src_ok）+ 变体未被判病句/机械劣化的。
    """
    with db.session() as s:
        bm = {x.id: x for x in s.query(Segment).filter(Segment.role == "benchmark").all()}
        rows = []
        for cc in (s.query(ControlledCorruption)
                   .filter(ControlledCorruption.status == "ok")
                   .filter(ControlledCorruption.candidate_id.isnot(None)).all()):
            seg = bm.get(cc.segment_id)
            if seg is None:                       # 只收基准段
                continue
            try:
                integ = json.loads(seg.integrity or "{}")
            except Exception:
                integ = {}
            if integ.get("src_ok") is not True:   # 源文本必须干净
                continue
            dr = cc.drift or {}
            if isinstance(dr, str):
                try:
                    dr = json.loads(dr)
                except Exception:
                    dr = {}
            if dr.get("ungrammatical"):
                continue
            rows.append((cc, seg))
        if dry_run:
            return {"would_build": len(rows), "segments": len({x[1].id for x in rows})}

        st = BenchmarkSet(id=new_id("BS"), name=name, version=version,
                          kind="corruption_detection", n_items=len(rows),
                          spec={"source": "controlled_corruptions",
                                "segment_role": "benchmark",
                                "require_src_ok": True,
                                "require_not_ungrammatical": True,
                                "position_seed": seed,
                                "ctx": "near1"},
                          note="判别题：A/B 哪一边是**人类原文**（另一边是按单一变量劣化的版本）")
        s.add(st)
        s.flush()
        rng = random.Random(seed)
        for cc, seg in rows:
            human = seg.text_clean or seg.text
            if rng.random() < 0.5:
                a, b, ans = human, cc.text, "A"
            else:
                a, b, ans = cc.text, human, "B"
            # 上文与评审台默认口径一致（near1）：基准测的必须是"人判/机判"同一个任务
            from app.context_ablation import scene_context
            ctxs, _ = scene_context(s, seg)
            s.add(BenchmarkItem(set_id=st.id, segment_id=seg.id,
                                kind="corruption_detection",
                                context=(ctxs or [""])[-1] if ctxs else "",
                                text_a=a, text_b=b, answer=ans,
                                meta={"corruption_type": cc.corruption_type,
                                      "variable": cc.variable, "drift": cc.drift_score}))
        s.commit()
        return {"set_id": st.id, "items": len(rows),
                "segments": len({x[1].id for x in rows})}


def scan() -> dict:
    with db.session() as s:
        sets = s.query(BenchmarkSet).all()
        out = []
        for st in sets:
            n = s.query(BenchmarkItem).filter_by(set_id=st.id).count()
            out.append((st.id, st.name, st.version, st.kind, st.n_items, n))
    con = sqlite3.connect(str(ROOT / "data" / "language_genome.db"))
    runs = {}
    for r in con.execute("select set_id, model, n, accuracy from benchmark_runs"):
        runs.setdefault(r[0], []).append((r[1], r[2], r[3]))
    con.close()
    print(f"{'集合':<34}{'名称':<16}{'版本':>4}{'口径':<24}{'条目':>6}{'实存':>6}")
    for sid, name, ver, kind, ni, n in out:
        print(f"{sid:<34}{name:<16}{ver:>4}{kind:<24}{ni:>6}{n:>6}")
    if runs:
        print("\n已有评测:")
        for sid, rs in runs.items():
            for model, n, acc in rs:
                print(f"   {sid}  {model[:30]:32} n={n} acc={acc:.3f}")
    return {"sets": len(out)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="cc-v1")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--kind", default="corruption_detection")
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    if args.scan:
        scan()
        return
    if args.kind != "corruption_detection":
        raise SystemExit("目前只实现了 corruption_detection")
    out = build_corruption_detection(args.name, args.version, args.seed, args.dry_run)
    print(json.dumps(out, ensure_ascii=False))
    scan()


if __name__ == "__main__":
    main()
