"""K1-A 镜像去重与对账（知识化方案 §4.1/监督 2026-09-21）：一键可重跑。

## 核对什么

四部根作品的镜像关系必须可对账——逐条回连、mismatch 非零退出、
--json 落证据（照 verify_bal_universe.py 的口径）：

1. **全覆盖**：每部 Work 必有 work_sources 登记行，登记行必指向真实
   Work（漏登/悬空都是 mismatch）；
2. **悬空三查**（会审二轮 BLOCK 项：无 FK 的列必须对账）：author_id
   非空 → 必须在 authors 里存在；genre_ids 逐个 → 必须在 genres 里存在；
   canonical_work_id → 必须指向真实作品；
3. **镜像回连到根**：镜像的 canonical==其 v2_of 只是第一步——canonical
   链必须**走到一个真根**（v2_of 为空的作品）；A↔B 互指镜像、自指
   镜像（v2_of==自身）、canonical 指向另一条镜像链的中间节点，
   全部是 mismatch（root_chain、self_mirror）；
4. **独立人类源计数**：只认「source_type=human_fiction 且 canonical==自身
   且 v2_of 为空」的根作品——镜像、fixture、synthetic 一律不加分
   （自指镜像不许虚增计数，会审二轮）；
5. **镜像继承一致**：镜像行的 author_id/genre_ids 必须与其根作品的
   登记行一致（register 复制、verify 比对——不是口头承诺）；
6. **内容锚复核**：每个登记行的 text_sha256 按当前库内容**重算比对**
   ——锚被随手改写就失去漂移检测能力（会审二轮 BLOCK 项）；
   0 段作品的锚必须为 NULL（如实，不是缺失）；
7. **corpus_v2_map 回连**（map 在场时）：每条 v1_segment 必须在库中
   存在；悬空是 mismatch；map 缺席如实记 skipped，不冒充通过；
8. **基准源普查**（K2 隔离契约的源身份层）：每个根作品的基准段计数
   入证据。

## 隔离三查的执行分工（如实声明，不夸大）

①源身份 + ②文本版本：由本登记与对账承担；
③目标与上下文区间：由既有基准哈希族（export_training._bench_hashes
整段+组成段落）在 **K2 发现管线运行时**执行——本脚本不执行区间检查。
跨语料出现不得误译成质量通过（方案 §4.3）——本脚本只对账身份。

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
sys.path.insert(0, str(ROOT / "scripts"))

import register_work_sources as REG             # noqa: E402
from app import db                               # noqa: E402
from app.models import Author, Genre, Segment, Work, WorkSource  # noqa: E402

V2_MAP = ROOT / "data" / "exports" / "corpus_v2_map.jsonl"


def _work_sha256(s, work_id: str) -> tuple[str | None, int]:
    """与 register_work_sources._work_sha256 同一口径（锚复核用）。"""
    import hashlib
    h = hashlib.sha256()
    n = 0
    q = (s.query(Segment.text_clean, Segment.text)
         .filter(Segment.work_id == work_id)
         .order_by(Segment.ordinal)
         .yield_per(500))
    for text_clean, text in q:
        h.update(((text_clean or text or "") + "\n").encode("utf-8"))
        n += 1
    return (h.hexdigest() if n else None), n


def verify(write_json: str = "", check_anchors: bool = True) -> dict:
    with db.session() as s:
        works = {w.id: w for w in s.query(Work).all()}
        regs = {r.work_id: r for r in s.query(WorkSource).all()}
        author_ids = {row[0] for row in s.query(Author.id).all()}
        genre_ids_all = {row[0] for row in s.query(Genre.id).all()}
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
                "identity_purposes": r.identity_purposes,
                "license_purposes": r.license_purposes,
                "n_segments": s.query(Segment).filter(
                    Segment.work_id == wid).count(),
                "n_benchmark_segments": bench_n,
            })
        for wid, r in regs.items():
            if wid not in works:
                mismatches.append({"work": wid, "kind": "registry_dangling_work"})
        # 悬空三查：author / genre / canonical（无 FK 的列，对账承担完整性）
        for row in per_work:
            wid = row["work_id"]
            r = regs[wid]
            if r.author_id is not None and r.author_id not in author_ids:
                mismatches.append({"work": wid, "kind": "author_dangling",
                                   "author_id": r.author_id})
            for gid in r.genre_ids:
                if gid not in genre_ids_all:
                    mismatches.append({"work": wid, "kind": "genre_dangling",
                                       "genre_id": gid})
            if r.canonical_work_id not in works:
                mismatches.append({"work": wid, "kind": "canonical_dangling",
                                   "canonical": r.canonical_work_id})
        # 镜像回连到真根（链走 + 环检测）+ 继承一致 + 自指
        for row in per_work:
            wid = row["work_id"]
            r = regs[wid]
            w = works[wid]
            if w.v2_of:
                if w.v2_of == wid:
                    mismatches.append({"work": wid, "kind": "self_mirror"})
                elif r.canonical_work_id != w.v2_of:
                    mismatches.append({"work": wid,
                                       "kind": "mirror_canonical_mismatch",
                                       "canonical": r.canonical_work_id,
                                       "v2_of": w.v2_of})
                # canonical 链必须走到真根（A↔B 互指在此现形）
                cur, hops = w.v2_of, 0
                while cur is not None and hops <= len(works) + 1:
                    nxt = works.get(cur)
                    if nxt is None:
                        break
                    cur = nxt.v2_of
                    hops += 1
                if cur is not None:
                    mismatches.append({"work": wid,
                                       "kind": "mirror_root_chain",
                                       "note": "canonical/v2_of 链未终止于真根"
                                               "（环或悬空）"})
                root_reg = regs.get(w.v2_of)
                if root_reg is not None:
                    if (r.author_id or None) != (root_reg.author_id or None) \
                            or list(r.genre_ids or []) != list(root_reg.genre_ids or []):
                        mismatches.append({"work": wid,
                                           "kind": "mirror_inherit_mismatch",
                                           "root": w.v2_of})
                elif w.v2_of != wid:
                    mismatches.append({"work": wid,
                                       "kind": "root_registry_missing",
                                       "root": w.v2_of})
            else:
                if r.canonical_work_id != wid:
                    mismatches.append({"work": wid,
                                       "kind": "root_canonical_mismatch"})
            if REG._is_fixture(w) and r.source_type != "fixture":
                mismatches.append({"work": wid, "kind": "fixture_type_mismatch",
                                   "source_type": r.source_type})
            # 授权闸（会审二轮 BLOCK 项：署名可核对 ≠ 用途授权）——
            # license_purposes 非空必须带 license_basis（授权人/日期/范围）
            if r.license_purposes and not (r.license_basis or "").strip():
                mismatches.append({"work": wid, "kind": "license_without_basis",
                                   "license_purposes": list(r.license_purposes)})
            for p in r.license_purposes:
                if p not in ("training_source", "benchmark_source"):
                    mismatches.append({"work": wid,
                                       "kind": "license_purpose_unknown",
                                       "purpose": p})
        # 内容锚复核：锚=登记时的事实，库内容变了必须显式重锚
        if check_anchors:
            for row in per_work:
                wid = row["work_id"]
                r = regs[wid]
                sha, n = _work_sha256(s, wid)
                row["n_segments"] = n
                if (r.text_sha256 or None) != (sha or None):
                    mismatches.append({"work": wid, "kind": "anchor_drift",
                                       "registered": (r.text_sha256 or "")[:12],
                                       "current": (sha or "")[:12],
                                       "note": "库内容与登记锚不符——"
                                               "确认漂移无害后 "
                                               "register --reset-anchor"})
        # corpus_v2_map 回连（文件在场才核；缺席如实记 skipped）
        map_report: dict = {"path": str(V2_MAP), "checked": False}
        if V2_MAP.exists():
            seg_ids = {sid for sid, in s.query(Segment.id).all()}
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
        # 独立人类源：只认真根（human_fiction + canonical==自身 + v2_of 空）
        roots = [p for p in per_work if p["source_type"] == "human_fiction"
                 and p["canonical_work_id"] == p["work_id"]
                 and not p["v2_of"]]
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
                "rule": "发现/复现样本隔离三查：源身份+文本版本+目标与上下文区间",
                "division": "①源身份②文本版本由本登记与对账承担；③区间比对由"
                            "基准哈希族（export_training._bench_hashes 整段+"
                            "组成段落）在 K2 发现管线运行时执行——本脚本不执行"
                            "区间检查（如实分工，不夸大）",
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
    ap.add_argument("--skip-anchors", action="store_true",
                    help="跳过内容锚复核（大库提速用；默认必查）")
    args = ap.parse_args()
    db.init_db()
    rep = verify(write_json=args.json, check_anchors=not args.skip_anchors)
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
