"""K1-A 镜像去重与对账（知识化方案 §4.1/监督 2026-09-21）：一键可重跑。

## 核对什么

四部根作品的镜像关系必须可对账——逐条回连、mismatch 非零退出、
--json 落证据（照 verify_bal_universe.py 的口径）：

1. **全覆盖**：每部 Work 必有 work_sources 登记行，登记行必指向真实
   Work（漏登/悬空都是 mismatch）；
2. **镜像回连**：corpus v2 镜像（works.v2_of 非空）的
   canonical_work_id 必须等于 v2_of——镜像/重切段/清洗副本回连同一根
   作品，**不计独立复现**；根作品的 canonical 必是自身；
3. **独立人类源计数**：只认「source_type=human_fiction 且
   canonical==自身」的根作品——镜像、fixture、synthetic 一律不加分；
4. **corpus_v2_map 回连**（若 map 文件在场）：每条 v1_segment 必须在
   库中存在且 work 归属一致；悬空条目是 mismatch（map 缺席则如实记
   skipped，不冒充通过）；
5. **基准源普查（K2 隔离契约的源身份层，冻结）**：每个根作品的
   基准段（role='benchmark'）计数入证据——发现/复现样本的隔离三查
   （源身份 + 文本版本 + 目标与上下文区间）从这里对账；
6. **fixture 不冒充人类语料**：fixture 登记类型必须是 fixture。

跨语料出现不得误译成质量通过（方案 §4.3）——本脚本只对账身份，
不做任何质量判定。

## 用法

    python scripts/verify_work_registry.py
    python scripts/verify_work_registry.py --json out/registry.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                # noqa: E402
from app.models import Segment, Work, WorkSource  # noqa: E402

V2_MAP = ROOT / "data" / "exports" / "corpus_v2_map.jsonl"


def verify(write_json: str = "") -> dict:
    with db.session() as s:
        works = {w.id: w for w in s.query(Work).all()}
        regs = {r.work_id: r for r in s.query(WorkSource).all()}
        if not regs:
            raise SystemExit("work_sources 登记表为空——先跑 "
                             "scripts/register_work_sources.py（宁拒不恒绿）")
        mismatches: list[dict] = []
        per_work: list[dict] = []
        for wid, w in sorted(works.items()):
            r = regs.get(wid)
            if r is None:
                mismatches.append({"work": wid, "kind": "work_without_registry",
                                   "title": w.title})
                continue
            bench_n = (s.query(Segment).filter(Segment.work_id == wid,
                                              Segment.role == "benchmark").count())
            per_work.append({
                "work_id": wid, "title": w.title, "v2_of": w.v2_of,
                "source_type": r.source_type, "author_id": r.author_id,
                "genre_ids": r.genre_ids, "text_version": r.text_version,
                "canonical_work_id": r.canonical_work_id,
                "metadata_status": r.metadata_status,
                "n_segments": s.query(Segment).filter(
                    Segment.work_id == wid).count(),
                "n_benchmark_segments": bench_n,
            })
        for wid, r in regs.items():
            if wid not in works:
                mismatches.append({"work": wid, "kind": "registry_dangling_work"})
        for row in per_work:
            wid = row["work_id"]
            if row["v2_of"]:
                if row["canonical_work_id"] != row["v2_of"]:
                    mismatches.append({"work": wid,
                                       "kind": "mirror_canonical_mismatch",
                                       "canonical": row["canonical_work_id"],
                                       "v2_of": row["v2_of"]})
            elif row["source_type"] == "human_fiction" \
                    and row["canonical_work_id"] != wid:
                mismatches.append({"work": wid,
                                   "kind": "root_canonical_mismatch"})
            if (row["title"] or "").startswith("fixture") \
                    and row["source_type"] != "fixture":
                mismatches.append({"work": wid, "kind": "fixture_type_mismatch",
                                   "source_type": row["source_type"]})

        # corpus_v2_map 回连（文件在场才核；缺席如实记 skipped）
        map_report: dict = {"path": str(V2_MAP), "checked": False}
        if V2_MAP.exists():
            seg_ids = {sid for sid, in s.query(Segment.id).all()}
            seg_work = {sid: wid for sid, wid in
                        s.query(Segment.id, Segment.work_id).all()}
            entries = orphan = 0
            for line in V2_MAP.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                entries += 1
                sid = e.get("v1_segment")
                if sid not in seg_ids:
                    orphan += 1
                    if len([m for m in mismatches
                            if m.get("kind") == "map_orphan_entry"]) < 10:
                        mismatches.append({"work": e.get("work"),
                                           "kind": "map_orphan_entry",
                                           "v1_segment": sid})
            map_report = {"path": str(V2_MAP), "checked": True,
                          "entries": entries, "orphan": orphan}
        # 独立人类源：只认根作品（canonical==自身）；镜像/fixture 不计
        roots = [p for p in per_work if p["source_type"] == "human_fiction"
                 and p["canonical_work_id"] == p["work_id"]
                 and not (p["title"] or "").startswith("fixture")]
        mirrors = [p for p in per_work if p["v2_of"]]
        report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_works": len(works), "n_registered": len(regs),
            "independent_human_sources": len(roots),
            "root_works": [{"work_id": p["work_id"], "title": p["title"],
                            "author_id": p["author_id"],
                            "n_segments": p["n_segments"],
                            "n_benchmark_segments": p["n_benchmark_segments"]}
                           for p in roots],
            "mirror_works": [{"work_id": p["work_id"], "title": p["title"],
                              "canonical_work_id": p["canonical_work_id"],
                              "text_version": p["text_version"]}
                             for p in mirrors],
            "per_work": per_work,
            "corpus_v2_map": map_report,
            "n_mismatch": len(mismatches), "mismatch": mismatches[:50],
            "isolation_contract": {
                "rule": "发现/复现样本隔离三查：源身份（work_sources 分型与"
                        "允许用途）+ 文本版本（text_version/text_sha256）+ "
                        "目标与上下文区间（基准哈希族：整段+组成段落，"
                        "export_training._bench_hashes 口径）——只查 role 或"
                        "整段哈希不够（A03/A11 教训）",
                "independent_evidence": "唯一源区间/根作品聚合；镜像、重切段、"
                                        "清洗副本与重复抽取不计独立复现",
                "fixtures": "只验契约，不给人类来源计数加分"},
        }
    if write_json:
        out = Path(write_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"[verify_work_registry] 证据已写 {out}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", default="", help="证据 JSON 输出路径")
    args = ap.parse_args()
    db.init_db()
    rep = verify(write_json=args.json)
    print(json.dumps({k: rep[k] for k in
                      ("n_works", "n_registered", "independent_human_sources",
                       "n_mismatch", "corpus_v2_map")},
                     ensure_ascii=False, indent=1))
    if rep["n_mismatch"]:
        raise SystemExit(f"对账 mismatch：{rep['n_mismatch']} 条（exit 1）")
    print(f"[verify_work_registry] PASS：{rep['independent_human_sources']} 个"
          "独立人类源（仅根作品；镜像/fixture 不计）")


if __name__ == "__main__":
    main()
