"""strategy_stats 重建器（K1-B 投影的缺失写入方，2026-09-23）：按当前
strategy_instances 快照**全量重算**——不是第二份真值，重建即替换
（最新快照为准；snapshot_at + data_fingerprint 标明数据时点，可核验）。

口径（models.StrategyStats 契约 + 方案 §4.2/§4.3「独立证据按来源区间/
根作品聚合；镜像/重切段/重复抽取不计独立复现」）：
- 独立证据只数 **verified** 实例；作品经 work_sources.canonical_work_id
  回连根作品——镜像/派生聚合到同一根，**root_works 只认根**；
- unique_source_intervals = 去重 (根作品, evidence_sha256) 数——同一根下
  同一段原文（重切段/重复抽取）不重复计；段切分版本不影响聚合；
- known_authors / genres = verified 涉及根作品的登记 author_id 非空数 /
  登记 genre_ids 并集（登记缺失不计，不猜）；
- attempts = 该策略实例行总数（任何 status）；valid = verified；
  rejected = status∈{rejected, rejected_evidence}（旧词汇兼容）；
  missing = 其余（proposed 等——当前写入流不产 proposed，恒 0 起步）；
- counter_examples = contradicts 知识边数（knowledge_links 尚无写入流，
  0 起步，不虚构）；
- extras.by_root_work = 每根作品 verified 数（集中度的原始事实）；
- extras.usable_evidence = 按**服务端封底 policy**（空 policy）的 K3 可用
  证据数（调用方收窄不含）——compute() 直接调 kq._evidence_for(s, id, {})
  复用 K3 全部剔除链（无登记/基准段/来源类型/用途/文本版本/镜像去重），
  **不本地复刻**（复审实跑：本地只复刻 benchmark 一道时 SSR 6 vs K3 真实
  ev_count 2，虚高 3 倍）；计数单位 = 唯一 (根作品, span) 区间，与 K3
  evidence_count 同口径（非实例数）；
- extras.benchmark_stripped = 该策略实例中被 K3 以基准段来源
  （:benchmark_source）剔除的实例数——valid 仍按既有式计（含基准段
  实例），usable_evidence 与 valid 的差即两套口径的如实差距；
- 指纹边界（会审 2026-09-27 指出）：data_fingerprint 只覆盖**实例行**
  （id:status:span:evidence_sha256），**不含** extras 的任何键——分档键
  不在指纹内，故指纹相同不代表分档键已写入。当前 run() **无「指纹未变则
  跳过」短路**（apply=True 一律先 delete 再全量 add），老行不会因指纹而
  拿不到新键；若将来引入短路，必须同时把 extras 纳入指纹或显式绕过。
- extras 写入方式：整字典覆盖（重建即替换，与「不是第二份真值」一致）——
  **不允许**第三方往 extras 手填键：apply=True 会静默丢弃。新增键一律
  在此函数内追加。
- extras.k3_eligible_instances / extras.k3_eligible_root_works = K3
  可用证据的**实例级 / 根作品级**分档（分档派工 2026-09-26：审计发现
  82 条 benchmark 实例按观察口径报数，被误读成 K3 可用数）——与
  usable_evidence 取**同一次** kq._evidence_for 调用：前者=len(refs)
  （K3 实际保留并出具引用的证据条目数，基准段/无登记/来源类型/用途/
  文本版本/镜像去重全部已生效），后者=refs 中 distinct
  canonical_work 数。两者从准入判据链的返回值直接派生，**不本地复刻
  判据**（与 usable_evidence 同一纪律）；既有字段（valid/root_works/
  unique_source_intervals/by_root_work）保持观察证据口径逐字不动，
  分档只旁路新增，读表方无静默语义漂移。

用法：
    python scripts/strategy_stats_rebuild.py            # dry-run（零数据写）
    python scripts/strategy_stats_rebuild.py --apply    # 重建（写库）

注意「零数据写」≠ 零 DDL：main() 会调 db.init_db()，对旧 schema 真库
可能执行 DDL（建缺失表/列）；dry-run 本身不插/不删/不改任何数据行。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                    # noqa: E402
from app import knowledge_query as kq                 # noqa: E402
from app.models import (ExpressionStrategyV2, StrategyInstance,  # noqa: E402
                        StrategyStats, WorkSource)


def _root_of(s, work_id: str, cache: dict) -> str:
    if work_id not in cache:
        ws = s.query(WorkSource).filter_by(work_id=work_id).first()
        cache[work_id] = (ws.canonical_work_id if ws else None) or work_id
    return cache[work_id]


def compute(s) -> list[dict]:
    """确定性投影：策略按 (strategy_key, version) 序，实例按 id 序。"""
    root_cache: dict[str, str] = {}
    out = []
    for st in (s.query(ExpressionStrategyV2)
               .order_by(ExpressionStrategyV2.strategy_key,
                         ExpressionStrategyV2.version).all()):
        rows = (s.query(StrategyInstance)
                .filter_by(strategy_id=st.id)
                .order_by(StrategyInstance.id).all())
        intervals: set[tuple[str, str]] = set()
        by_root: dict[str, int] = {}
        n_valid = n_rej = 0
        for r in rows:
            if r.status == "verified":
                n_valid += 1
                root = _root_of(s, r.work_id, root_cache)
                intervals.add((root, r.evidence_sha256))
                by_root[root] = by_root.get(root, 0) + 1
            elif r.status in ("rejected", "rejected_evidence"):
                n_rej += 1
        authors: set[str] = set()
        genres: set[str] = set()
        for w in by_root:
            ws = s.query(WorkSource).filter_by(work_id=w).first()
            if ws is not None:
                if ws.author_id:
                    authors.add(ws.author_id)
                genres.update(ws.genre_ids or [])
        fp_src = "|".join(
            f"{r.id}:{r.status}:{r.span_start}-{r.span_end}:{r.evidence_sha256}"
            for r in rows)
        # usable_evidence：直接复用 K3 唯一口径（只读查询，不本地复刻剔除
        # 链）；取第 2 返回值 ev_count——计数单位 = 唯一 (根作品, span) 区间，
        # 与 K3 evidence_count 同口径。空 policy = 服务端封底默认
        # （excluded_source_types 并集封底 / allowed_text_versions 交集封顶
        # 全按 kq.DEFAULT_*），调用方收窄不含——这里是统计投影，如实按
        # 封底口径报告。benchmark_stripped 从 stripped 的 :benchmark_source
        # 后缀条目计数。k3_eligible_instances / k3_eligible_root_works 从
        # **同一次调用**的第 1 返回值 refs（K3 实际保留的证据条目，含
        # instance_id 与 canonical_work）派生——与准入判据同判据，禁止
        # 在此另写一套过滤（分档派工 2026-09-26）。
        refs, ev_count, stripped = kq._evidence_for(s, st.id, {})
        out.append({
            "strategy_id": st.id, "strategy_key": st.strategy_key,
            "strategy_version": st.version,
            "unique_source_intervals": len(intervals),
            "root_works": len(by_root),
            "known_authors": len(authors), "genres": len(genres),
            "attempts": len(rows), "valid": n_valid, "rejected": n_rej,
            "missing": len(rows) - n_valid - n_rej, "counter_examples": 0,
            "by_root_work": by_root,
            "usable_evidence": ev_count,
            "benchmark_stripped": sum(
                1 for t in stripped if t.endswith(":benchmark_source")),
            "k3_eligible_instances": len(refs),
            "k3_eligible_root_works": len(
                {ref["canonical_work"] for ref in refs}),
            "data_fingerprint": hashlib.sha256(
                fp_src.encode("utf-8")).hexdigest(),
        })
    return out


def run(apply: bool) -> dict:
    with db.session() as s:
        stats = compute(s)
        out = {"mode": "apply" if apply else "dry_run",
               "n_strategies": len(stats), "rows": stats}
        if apply:
            s.query(StrategyStats).delete()   # 投影：重建即替换，无第二真值
            # 注意：extras 为整字典覆盖写，且**无「指纹未变则跳过」短路**
            # （会审 2026-09-27 要求显式声明）——见模块 docstring 指纹边界。
            now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            for r in stats:
                s.add(StrategyStats(
                    strategy_id=r["strategy_id"],
                    strategy_version=r["strategy_version"], snapshot_at=now,
                    data_fingerprint=r["data_fingerprint"],
                    unique_source_intervals=r["unique_source_intervals"],
                    root_works=r["root_works"], known_authors=r["known_authors"],
                    genres=r["genres"], attempts=r["attempts"],
                    valid=r["valid"], rejected=r["rejected"],
                    missing=r["missing"], counter_examples=0,
                    extras={"by_root_work": r["by_root_work"],
                            "usable_evidence": r["usable_evidence"],
                            "benchmark_stripped": r["benchmark_stripped"],
                            "k3_eligible_instances":
                                r["k3_eligible_instances"],
                            "k3_eligible_root_works":
                                r["k3_eligible_root_works"]}))
            s.commit()
            out["applied"] = len(stats)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="strategy_stats 投影重建"
                                  "（dry-run 默认；--apply 写库）")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db.init_db()
    print(json.dumps(run(a.apply), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
