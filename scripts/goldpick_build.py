"""gold standard 路线 (a)：待集霸指认的「合格下限」片段清单（2026-09-19，P2-7）。

军师方向修正：gold standard 主线改为 **(a) 指认可文本定合格下限**——
(b) AI 相对排序降级为弱标签试验，(c) 负面库转辅助。本脚本产出一份
**分层抽样的候选片段清单**（12~20 段）供集霸指认，零 LLM 成本：

· 跨作品 × 全书位置三分位 × 「AI 味词表命中密度」双桶（高/低）——
  高命中桶是**正常反例**：修辞浓但不该被一票否决的人类原文，
  用来钉住"合格下限"不被词表过拟合拽走（§7 表层词表已证只分网文味）；
· 只收 src_ok=True、非基准段、非番外、无水印、≥60 字；
· 位置分层用 make_random_batch.extras_start 的三分位近似。

指认入口：生成的 docs/goldpick-候选清单-<ver>.md，每段编号，
集霸直接回复「认可：编号列表；不认可：编号列表（可带一句理由）」。
认可段将构成 gold_positives，用于校准后续所有仪器与训练目标。

用法：
    python scripts/goldpick_build.py --ver v1            # 默认 18 段
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.ai_flavor import analyze  # noqa: E402
from app.models import Segment, Work  # noqa: E402
from make_random_batch import extras_start, looks_watermarked  # noqa: E402

N_DEFAULT = 18


def _eligible_segments(s) -> list:
    """可抽样段：src_ok=True、非基准、非番外、无水印、≥60 字。"""
    out = []
    works = {w.id: w for w in s.query(Work).all()}
    starts: dict = {}
    for seg in s.query(Segment).filter(Segment.role.is_(None) | (Segment.role == "train")).all():
        try:
            integ = json.loads(seg.integrity or "{}")
        except Exception:
            integ = {}
        if integ.get("src_ok") is not True:
            continue
        text = seg.text_clean or seg.text or ""
        if len(text.strip()) < 60 or looks_watermarked(seg.text or ""):
            continue
        w = works.get(seg.work_id)
        if w is None:
            continue
        title = w.title or ""
        if title.startswith("fixture") or title.startswith("t-") or title.startswith("test"):
            # 历史测试夹具作品（生产库里的遗留垃圾）不进指认清单
            continue
        key = (w.id, seg.seg_version)
        if key not in starts:
            starts[key] = extras_start(s, w.id, seg.seg_version)
        st = starts[key]
        if st is not None and seg.ordinal >= st:
            continue
        out.append((w, seg))
    return out


def build(ver: str, n: int = N_DEFAULT, seed: int = 20260919,
          out_dir: Path | None = None) -> dict:
    dest = out_dir or (ROOT / "data" / "exports")
    dest.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        pool = _eligible_segments(s)
        # 命中密度分桶（正常反例=高命中人类原文）
        buckets = defaultdict(list)
        for w, seg in pool:
            rep = analyze(seg.text_clean or seg.text or "")
            hi = 1 if rep.hits else 0        # FlavorReport.hits：命中的词表条目
            buckets[(w.title[:12], hi)].append((w, seg, len(rep.hits)))
        # 目标配额：每作品至少 2；高/低命中桶约 1:2；位置三分位覆盖
        rng = random.Random(seed)
        picked = []
        work_names = sorted({k[0] for k in buckets})
        per_work = max(2, n // max(1, len(work_names)))
        n_high_target = max(4, n // 3)       # 正常反例占比 ~1/3
        n_high = 0
        keys = sorted(buckets)
        rng.shuffle(keys)
        for key in keys:
            if len(picked) >= n:
                break
            title, hi = key
            if title not in work_names:
                continue
            taken = sum(1 for p in picked if p["work"][:12] == title)
            if taken >= per_work and len(picked) + 1 <= n:
                continue
            cand_list = buckets[key]
            rng.shuffle(cand_list)
            for w, seg, hits in cand_list:
                if any(p["segment_id"] == seg.id for p in picked):
                    continue
                if hi == 1 and n_high >= n_high_target:
                    break
                pos_t = "前" if seg.ordinal < 100 else ("中" if seg.ordinal < 1000 else "后")
                picked.append({
                    "segment_id": seg.id, "work": w.title, "ordinal": seg.ordinal,
                    "position_bucket": pos_t, "flavor_hits": hits,
                    "rhetoric_bucket": "high" if hi else "low",
                    "text": seg.text_clean or seg.text,
                    "why": ("正常反例候选：AI 味词表高命中的人类原文——指认它=承认修辞不下限"
                            if hi else "常规叙事段"),
                })
                n_high += hi
                break
        # 不足则回填
        for key in keys:
            if len(picked) >= n:
                break
            for w, seg, hits in buckets[key]:
                if len(picked) >= n:
                    break
                if any(p["segment_id"] == seg.id for p in picked):
                    continue
                picked.append({
                    "segment_id": seg.id, "work": w.title, "ordinal": seg.ordinal,
                    "position_bucket": "中", "flavor_hits": hits,
                    "rhetoric_bucket": "low", "text": seg.text_clean or seg.text,
                    "why": "回填", })
                break

    path = dest / f"goldpick_{ver}.jsonl"
    sum_path = dest / f"goldpick_{ver}_summary.json"
    with path.open("w", encoding="utf-8") as f:
        for i, r in enumerate(picked, 1):
            r["pick_number"] = i
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = {"path": str(path), "n": len(picked), "seed": seed,
               "works": sorted({p["work"] for p in picked}),
               "high_rhetoric_n": sum(1 for p in picked if p["rhetoric_bucket"] == "high"),
               "note": "gold standard 路线 (a)：集霸指认可文本 = 合格下限；认可集将落 gold_positives"}
    sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    # 可读清单（指认入口）
    md = [f"# gold standard 待指认清单（goldpick {ver}，{len(picked)} 段）",
          "",
          f"> 集霸请直接回复：**认可：编号**（如 3,7,12）；不认可的编号可带一句理由。",
          "> 认可段 = 「这样写是合格的」正面下限样本，将作为 gold_positives 校准仪器与训练。",
          "", "| # | 作品 | 位置 | 修辞桶 | 选入原因 |", "|---|---|---|---|---|"]
    for r in picked:
        md.append(f"| {r['pick_number']} | {r['work'][:14]} | #{r['ordinal']} | "
                  f"{r['rhetoric_bucket']} | {r['why'][:24]} |")
    md.append("")
    for r in picked:
        md.append(f"## #{r['pick_number']}  {r['work']}  (ordinal {r['ordinal']}, "
                  f"词表命中 {r['flavor_hits']})")
        md.append("")
        md.append(r["text"])
        md.append("")
    md_path = ROOT / "docs" / f"goldpick-候选清单-{ver}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    summary["md_path"] = str(md_path)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ver", default="v1")
    ap.add_argument("--n", type=int, default=N_DEFAULT)
    ap.add_argument("--seed", type=int, default=20260919)
    args = ap.parse_args()
    db.init_db()
    out = build(args.ver, args.n, args.seed)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
