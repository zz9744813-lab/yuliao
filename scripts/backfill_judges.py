"""补齐评委分：让**每一条已判条目**都有 2 评委 × 2 口径的分。

为什么需要：终报要池化全部已判数据，但早期批次（`_heldout` 口径出现之前——
主要是 B82D 的 first50 / r15 / dual10 / 无批次）没有对应口径的评委分，
覆盖率只有 126/230，池化估计只代表一半数据。

**按候选**（不是按批次）补齐，因此也覆盖「无批次标签」的早期条目。
幂等：已有 ok 记录的 (cid, model, pv) 直接跳过（复用 heldout_eval.run_one）。

用法：
    python scripts/backfill_judges.py --dry-run
    python scripts/backfill_judges.py --conc 6
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.context_ablation import scene_context  # noqa: E402
from app.models import Candidate, Segment  # noqa: E402

WL = ("reconstruct_v1", "recon_ctx_v1")


def missing(db_path=None) -> list[str]:
    """已判（二选一）但缺任一 (评委, 口径) 的候选 id。"""
    con = sqlite3.connect(db_path or he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""select ri.human_verdict hv, c.id cid
            from review_items ri join candidates c on c.id = ri.subject_id
            where ri.status = 'done'
              and c.prompt_version in ({",".join("?" * len(WL))})""", WL).fetchall()
    out = []
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        if hv.get("winner_resolved") not in ("human", "candidate"):
            continue
        if any(he._read_verdict(r["cid"], m, he.PROMPT_VARIANTS[v][1], con) is None
               for m in he.JUDGES for v in ("v3", "v4")):
            out.append(r["cid"])
    con.close()
    return out


def verify(cids, db_path=None) -> list[str]:
    con = sqlite3.connect(db_path or he.DB)
    con.row_factory = sqlite3.Row      # 少了这行 _read_verdict 内部会按元组取键而崩
    gaps = []
    for cid in cids:
        for m in he.JUDGES:
            for v in ("v3", "v4"):
                if he._read_verdict(cid, m, he.PROMPT_VARIANTS[v][1], con) is None:
                    gaps.append((cid[:14], m.split("/")[-1][:12], v))
    con.close()
    return gaps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conc", type=int, default=6)
    ap.add_argument("--variants", default="v3,v4")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    db.init_db()
    cids = missing()
    need = len(cids) * len(he.JUDGES) * len(variants)
    print(f"缺评委分的候选 = {len(cids)} → 需 {need} 次调用"
          f"（{len(cids)} × {len(he.JUDGES)} 评委 × {len(variants)} 口径）")
    if not cids:
        print("无缺口，结束")
        return
    if args.dry_run:
        print("dry-run：未发起")
        return

    ctx_by_cid: dict[str, str] = {}
    with db.session() as s:
        for cid in cids:
            cand = s.get(Candidate, cid)
            human = s.get(Segment, cand.segment_id)
            texts, _ = scene_context(s, human)
            ctx_by_cid[cid] = ("\n\n").join(texts)
    med = sorted(len(v) for v in ctx_by_cid.values())[len(ctx_by_cid) // 2]
    print(f"上文中位 {med} 字")

    jobs = [(cid, ctx_by_cid[cid], m, v)
            for m in he.JUDGES for v in variants for cid in cids]
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: he.run_one(*j), jobs):
            pass
    print(f"完成：ok={he._counter['ok']} failed={he._counter['failed']} "
          f"skip={he._counter['skip']}")

    gaps = verify(cids)
    if gaps:
        print(f"\n仍有缺口 {len(gaps)} 项（多为 deepseek 解析失败，重跑本脚本即可）：")
        for g in gaps[:10]:
            print("  ", g)
        raise SystemExit(1)
    print("覆盖核对通过：全部候选的 2 评委 × 2 口径 均已就绪")


if __name__ == "__main__":
    main()
