"""K2-A 实例抽取放量驱动（主控派工 2026-09-23：docs/K4_首轮真跑_证据_20260923.md §3.1
——app/knowledge_extract.py 三道门全仓无调用方，A 臂知识包恒空的根因，本驱动即其调用方）。

口径：
- 策略宇宙：expression_strategies_v2 里 status ∈ {hypothesis, active}
  （retired/superseded 不抽）；UNCERTAIN/空 scope_ids 的假设按**试点宇宙**抽
  ——实例用于之后建立 scope，不自动授予。
- 段宇宙（试点口径，绝不直接全库放量 40 万段）：role='benchmark' 且
  integrity.src_ok 为 True 且 text_clean 非空（来源合格纪律：没查过 src_ok=
  不可用）；按 (work_id, ordinal) 确定性排序；WORK/AUTHOR/GENRE 经 work_sources
  登记解析到所属作品的段，GLOBAL/UNCERTAIN 空身份=试点全集（仍受 limit/预算闸约束）。
- text_version 取 work_sources 登记行（K1-A 契约）；缺登记 → skip_no_text_version。
- **幂等**：已存在 StrategyInstance 行的 (strategy_id, strategy_version,
  segment_id) 对跳过（verified 与 rejected_evidence 都算已处理——同模型重试
  只烧预算不加证据）；无行可落的失败（invalid json / 输出门缺字段）只进 run
  报告不占幂等位，重跑会再试（预算闸兜底）。
- **预算闸**：ExtractBudget（--max-calls/--max-tokens）超限 → 显式
  blocked_budget 收尾，已抽候选照实提交保留（不回滚不丢弃）。
- 拦截顺序：策略间轮转取对（round-robin）直至 --limit——单策略不垄断预算，
  跨作品复现（≥2 根作品）才有机会成立。
- **双闸**：--live + 环境变量 K2_ALLOW_LIVE=1 才真跑（live client 走
  app.gateway.chat：A05 逐次记账/A06 完成原因门自动生效）；--dry-run 零调用
  零库写预演；两者都不带 → 拒（不默认猜模式）。

用法：
    python scripts/k2_extract_backfill.py --dry-run                # 预演
    K2_ALLOW_LIVE=1 python scripts/k2_extract_backfill.py --live \
        --extractor-model deepseek-v4.1-flash --limit 1            # 单段 live 探针
    K2_ALLOW_LIVE=1 python scripts/k2_extract_backfill.py --live \
        --extractor-model deepseek-v4.1-flash --limit 48            # 放量
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db, knowledge_extract as KE                    # noqa: E402
from app.config import LLM_MODE                                # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,         # noqa: E402
                        StrategyInstance, WorkSource)

DEFAULT_LIMIT = 48


def eligible_segments(s) -> list:
    """试点段宇宙：benchmark + src_ok + text_clean 非空。
    排序=(ordinal, work_id) 跨作品交错——轮转在策略间公平、段序在作品间
    交错，限量抽取才能尽早覆盖多部作品（跨作品复现证据，2026-09-23 首轮
    实测教训：按 (work_id, ordinal) 排序时 48 对全落在第一部作品，
    root_works 恒 1，复现证据出不来）。确定性排序，重跑同序。"""
    rows = []
    for seg in (s.query(Segment).filter(Segment.role == "benchmark")
                .order_by(Segment.ordinal, Segment.work_id).all()):
        try:
            integ = json.loads(seg.integrity or "{}")
        except Exception:                                      # noqa: BLE001
            integ = {}
        if integ.get("src_ok") is not True or not (seg.text_clean or "").strip():
            continue
        rows.append(seg)
    return rows


def _works_for_scope(s, strategy) -> list[str] | None:
    """None=试点全集（UNCERTAIN/GLOBAL 或空 ids）；否则按登记解析作品列表。"""
    ids = list(strategy.scope_ids or [])
    if not ids:
        return None
    if strategy.scope == "WORK":
        return ids
    if strategy.scope in ("AUTHOR", "GENRE"):
        out = []
        for ws in s.query(WorkSource).all():
            if strategy.scope == "AUTHOR":
                if (ws.author_id or "") in ids:
                    out.append(ws.work_id)
            elif set(ids) & set(ws.genre_ids or []):
                out.append(ws.work_id)
        return out
    return None                                             # GLOBAL 空身份同全集


def build_queues(s, *, strategy_keys: tuple | None = None) -> tuple[dict, dict]:
    """确定性 per-strategy 工作队列（round-robin 的原料）+ 跳过统计。
    strategy_keys：None=全部合格策略（生产口径）；测试传本测试的键隔离
    共享测试库（driver 不感知测试存在，只是个确定性过滤器）。"""
    strategies = (s.query(ExpressionStrategyV2)
                  .filter(ExpressionStrategyV2.status.in_(("hypothesis", "active")))
                  .order_by(ExpressionStrategyV2.strategy_key,
                            ExpressionStrategyV2.version).all())
    if strategy_keys is not None:
        strategies = [st for st in strategies
                     if st.strategy_key in strategy_keys]
    segs = eligible_segments(s)
    by_work: dict[str, list] = {}
    for seg in segs:
        by_work.setdefault(seg.work_id, []).append(seg)
    tv_cache: dict[str, str] = {}
    done = {(r.strategy_id, r.strategy_version, r.segment_id)
            for r in s.query(StrategyInstance).all()}
    queues, stats = {}, {"n_strategies": len(strategies), "n_eligible_segments":
                         len(segs), "skipped_done": 0, "skipped_scope_no_segments": 0,
                         "skipped_no_text_version": 0}
    for st in strategies:
        works = _works_for_scope(s, st)
        pool = segs if works is None else \
            [x for w in works for x in by_work.get(w, [])]
        if not pool:
            stats["skipped_scope_no_segments"] += 1
            continue
        q = []
        for seg in pool:
            if (st.id, st.version, seg.id) in done:
                stats["skipped_done"] += 1
                continue
            if seg.work_id not in tv_cache:
                ws = s.query(WorkSource).filter_by(work_id=seg.work_id).first()
                tv_cache[seg.work_id] = (ws.text_version or "") if ws else ""
            if not tv_cache[seg.work_id]:
                stats["skipped_no_text_version"] += 1
                continue
            q.append({"strategy": st, "segment": seg, "text": seg.text_clean,
                      "text_version": tv_cache[seg.work_id]})
        if q:
            queues[st.id] = q
    return queues, stats


def _round_robin(queues: dict, limit: int) -> list:
    """策略间轮转取对直至 limit（单策略不垄断预算，跨作品复现有机会）。"""
    out, keys = [], sorted(queues)
    while len(out) < limit:
        added = False
        for k in keys:
            if queues[k]:
                out.append(queues[k].pop(0))
                added = True
                if len(out) >= limit:
                    break
        if not added:
            break
    return out


def _persist(s, item: dict, r: dict, status: str) -> None:
    st, seg = item["strategy"], item["segment"]
    s.add(StrategyInstance(
        strategy_id=st.id, strategy_version=st.version, work_id=seg.work_id,
        segment_id=seg.id, text_version=item["text_version"],
        span_start=r["span_start"], span_end=r["span_end"],
        evidence_text=r["evidence_text"], evidence_sha256=r["evidence_sha256"],
        conditions_observed={}, observed_content=r["observed_content"],
        extractor_model=r["extractor_model"], status=status))


def run_backfill(s, client, *, limit: int = DEFAULT_LIMIT, max_calls: int = 20,
                 max_tokens: int = 50_000, dry_run: bool = False,
                 live: bool = False, strategy_keys: tuple | None = None) -> dict:
    """一次 run：建队列→（dry-run 即回）→轮转取对→抽→落行→统一提交。"""
    queues, stats = build_queues(s, strategy_keys=strategy_keys)
    n_pairs = sum(len(q) for q in queues.values())
    report = {"mode": "dry_run" if dry_run else ("live" if live else "run"),
              "limit": limit, "n_pending_pairs": n_pairs, "skips": stats,
              "attempted": 0, "verified": 0, "rejected_evidence": 0,
              "unverified": 0, "written": 0, "blocked_budget": False}
    picked = _round_robin(queues, limit)
    if dry_run:
        report["would_attempt"] = len(picked)
        return report
    budget = KE.ExtractBudget(max_calls=max_calls, max_tokens=max_tokens)
    unv = []
    for item in picked:
        st, seg = item["strategy"], item["segment"]
        try:
            r = KE.extract_segment(
                client, strategy_id=st.id, strategy_version=st.version,
                work_id=seg.work_id, segment_id=seg.id, text=item["text"],
                text_version=item["text_version"], budget=budget, live=live)
        except KE.ExtractBudgetExceeded:
            report["blocked_budget"] = True
            report["blocked_at"] = {"strategy": st.strategy_key,
                                    "segment_id": seg.id}
            break                       # 预算断点：已抽候选保留（下方统一提交）
        report["attempted"] += 1
        status = r.get("status")
        if status == "verified":
            _persist(s, item, r, "verified")
            report["verified"] += 1
        elif status == "rejected_evidence":
            # 落库枚举=INSTANCE_STATUS{proposed,verified,rejected}（K 契约）；
            # 模块报告口径保留 rejected_evidence（gate 词汇），行状态写枚举值
            _persist(s, item, r, "rejected")
            report["rejected_evidence"] += 1
        else:                           # unverified：只入报告（span 字段不全，无行可落）
            report["unverified"] += 1
            raw = r.get("raw")
            raw = raw if isinstance(raw, dict) else {}
            reason = ("no_instance_claimed" if raw.get("none") is True
                      else (r.get("reason") or status))
            unv.append({"strategy": st.strategy_key, "segment_id": seg.id,
                        "reason": reason,
                        "_raw_head": str(raw)[:200]})   # 模型原文截断入报告，不静默吞
    s.commit()                           # 断点/完成统一提交——候选不丢弃
    report["written"] = report["verified"] + report["rejected_evidence"]
    report["budget"] = {"calls": budget.calls, "tokens": budget.tokens,
                        "max_calls": max_calls, "max_tokens": max_tokens}
    if unv:
        report["unverified_details"] = unv[:20]     # 明细截断，报告可读
    return report


class _GatewayAdapter:
    """invoke 契约 → app.gateway.chat（A05 逐次记账/A06 完成原因门自动生效；
    gateway 隐藏真实模型——extractor_model 记请求名，实际名以网关账为准）。"""
    def __init__(self, model: str):
        self.model = model

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        from app import gateway
        r = gateway.chat(model=self.model, system=system,
                         user=json.dumps(payload, ensure_ascii=False),
                         purpose="k2_extract_backfill", max_tokens=max_tokens)
        return {"text": r.text, "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out, "actual_model": self.model}


def main() -> None:
    ap = argparse.ArgumentParser(description="K2-A 实例抽取放量驱动（试点口径）")
    ap.add_argument("--dry-run", action="store_true", help="预演：零调用零库写")
    ap.add_argument("--live", action="store_true",
                    help="真跑（拍板后）：需环境变量 K2_ALLOW_LIVE=1")
    ap.add_argument("--extractor-model", default="")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"本轮尝试对数上限（默认 {DEFAULT_LIMIT}，试点口径）")
    ap.add_argument("--max-calls", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=50_000)
    a = ap.parse_args()
    if bool(a.dry_run) == bool(a.live):
        raise SystemExit("须显式二选一：--dry-run（预演，零调用零库写）"
                         "或 --live（真跑，需 K2_ALLOW_LIVE=1）——不默认猜模式")
    if a.limit < 1:
        raise SystemExit("--limit 须为 ≥1 的整数")
    client = None
    if a.live:
        if os.environ.get("K2_ALLOW_LIVE") != "1":
            raise SystemExit("--live 需要环境变量 K2_ALLOW_LIVE=1（双闸："
                             "未拍板防误跑烧钱）")
        if not a.extractor_model:
            raise SystemExit("--live 需要 --extractor-model")
        if LLM_MODE != "real":
            raise SystemExit(f"--live 需要 LG_LLM_MODE=real（当前 {LLM_MODE}）")
        from preflight_models import require_models   # 预检门：池外名字=整批白跑（A01 纪律）
        require_models((a.extractor_model,), source="k2_extract_backfill")
        client = _GatewayAdapter(a.extractor_model)
    db.init_db()
    import contextlib
    from app.live_guard import live_lock
    # R6 守卫：live 实跑与全量 pytest/live 互斥（锁文件 O_EXCL 原子创建）
    with (live_lock("k2_extract_backfill") if a.live
          else contextlib.nullcontext()):
        with db.session() as s:
            rep = run_backfill(s, client, limit=a.limit, max_calls=a.max_calls,
                               max_tokens=a.max_tokens, dry_run=a.dry_run,
                               live=a.live)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
