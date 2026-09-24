"""K2-A 实例抽取放量驱动（主控派工 2026-09-23：docs/K4_首轮真跑_证据_20260923.md §3.1
——app/knowledge_extract.py 三道门全仓无调用方，A 臂知识包恒空的根因，本驱动即其调用方）。

口径：
- 策略宇宙：expression_strategies_v2 里 status ∈ {hypothesis, active}
  （retired/superseded 不抽）；UNCERTAIN/空 scope_ids 的假设按**试点宇宙**抽
  ——实例用于之后建立 scope，不自动授予。
- 段宇宙（**显式来源口径 `--source-scope`**，两值语义写死在 help 与
  docs/K2_非benchmark试点_说明_20260923.md；试点口径，绝不直接全库放量 40 万段）：
  · `benchmark`（默认，现状不变）：role='benchmark' 且 integrity.src_ok 为 True
    且 text_clean 非空（来源合格纪律：没查过 src_ok= 不可用）；
  · `nonbenchmark`（审计 P0 主线 1 的试点供给通道，默认关闭）：**只收**
    work_sources.source_type 属合规人类语料（显式白名单：精确值 `human_fiction`
    或前缀 `production_nonbenchmark_`）**且**段 role 显式判定为「不是 benchmark」
    的来源——判据是 `role != 'benchmark'`（NULL/train 都不等于 benchmark），
    **不是**「把 role 当 null」的模糊口径；两条件都要留痕（被排除的来源与
    段数、入池段的 role 分布写进汇总 skips）。
    K3 侧（app/knowledge_query.py）排除 benchmark 段是**硬口径**：本驱动一行
    不改、不放宽，只加抽取侧供给；报告里的 `k3_preview` 是同源的**只读预演**，
    用来看「抽到的能不能进 K3」，不参与任何过滤决策。
  排序=(ordinal, work_id) 跨作品交错；WORK/AUTHOR/GENRE 经 work_sources
  登记解析到所属作品的段，GLOBAL/UNCERTAIN 空身份=**该口径下**的试点全集
  （仍受 limit/预算闸约束）。
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
  零库写预演；两者都不带 → 拒（不默认猜模式）。R6 互斥守卫前置于预检/
  客户端构造/init_db（锁在 ⇒ 立刻拒，与环境配置无关）。

用法：
    python scripts/k2_extract_backfill.py --dry-run                # 预演（默认 benchmark 口径）
    python scripts/k2_extract_backfill.py --dry-run --source-scope nonbenchmark   # 非基准试点预演（零调用零库写）
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

from sqlalchemy import func, or_                         # noqa: E402

from app import db, knowledge_extract as KE                    # noqa: E402
from app.config import LLM_MODE                                # noqa: E402
from app.models import (ExpressionStrategyV2, Segment, Work,   # noqa: E402
                        StrategyInstance, WorkSource)

DEFAULT_LIMIT = 48

# ── 来源口径（审计 P0 主线 1：K2 只抽 benchmark ⇒ 可进 K3 的证据恒 0）────
# 两值**都**是显式口径，默认 benchmark=现状逐字不变；nonbenchmark 是新增的
# 试点供给通道，默认关闭（不开 = 与今天同）。
SOURCE_SCOPES = ("benchmark", "nonbenchmark")
DEFAULT_SOURCE_SCOPE = "benchmark"
BENCHMARK_ROLE = "benchmark"
# 合规非 benchmark 人类语料的**显式白名单**（K3 侧 DEFAULT_EXCLUDED_SOURCE_TYPES
# 的 fixture/synthetic/commentary 天然不在这里——抽取侧只加严，不与之竞争）：
# · 精确值：human_fiction（K1-A 登记的根/镜像人类源）
# · 前缀：  production_nonbenchmark_*（真库试点源，如 production_nonbenchmark_k2v2）
NONBENCHMARK_SOURCE_TYPES = frozenset({"human_fiction"})
NONBENCHMARK_SOURCE_TYPE_PREFIX = "production_nonbenchmark_"

MAX_TRACE_SOURCES = 500       # 排除/合格来源明细上限（真库来源数十级，防刷屏；
                              # 超出部分计入 *_truncated，局部核对用 work_filter）


def nonbenchmark_compliant_source(source_type) -> bool:
    """来源类型是否属合规非 benchmark 人类语料（唯一判定入口，留痕用）。"""
    st = (source_type or "").strip()
    if not st:
        return False
    return st in NONBENCHMARK_SOURCE_TYPES or st.startswith(
        NONBENCHMARK_SOURCE_TYPE_PREFIX)


def _src_ok(seg) -> bool:
    """源检查闸（口径与旧实现逐字一致）：integrity.src_ok 必须是 True。"""
    try:
        integ = json.loads(seg.integrity or "{}")
    except Exception:                                      # noqa: BLE001
        integ = {}
    return integ.get("src_ok") is True


def _registry_by_work(s) -> dict:
    return {ws.work_id: ws for ws in s.query(WorkSource).all()}


def _segment_roles_by_work(s) -> dict:
    """work_id -> {role 值: 段数}（role NULL 记成字面 "NULL"）。
    聚合一趟，不载段文——排除留痕的原料，不参与入池判定。"""
    out = {}
    rows = (s.query(Segment.work_id, Segment.role, func.count(Segment.id))
            .group_by(Segment.work_id, Segment.role).all())
    for wid, role, n in rows:
        out.setdefault(wid, {})[role if role is not None else "NULL"] = n
    return out


def _role_passes_scope(source_scope: str, role_key: str) -> bool:
    """段 role 是否属该口径（role_key 里 NULL 用字面 "NULL" 表示）。
    显式一支笔：benchmark 口径只收 role=='benchmark'；nonbenchmark 口径收
    一切**不等于** 'benchmark' 的 role（含 NULL/train）——不是「role 为 null
    就算非基准」的模糊口径。"""
    if role_key == BENCHMARK_ROLE:
        return source_scope == "benchmark"
    return source_scope == "nonbenchmark"


def _exclusion_reason(source_scope: str, ws, role_map: dict, n_in_scope: int,
                      n_kept: int):
    """来源级排除原因（合格=None）。判定顺序即留痕顺序，两口径共用一支笔。"""
    if n_kept:
        return None
    if not role_map:
        return "no_segments"
    if source_scope == "nonbenchmark":
        if ws is None:
            return "no_registry"
        st = (ws.source_type or "").strip()
        if not nonbenchmark_compliant_source(st):
            return f"source_type_not_compliant:{st or 'EMPTY'}"
        if not n_in_scope:
            return "no_nonbenchmark_role_segment"
        return "source_gate_failed:src_ok_or_text_clean"
    if not n_in_scope:
        return "no_benchmark_role_segment"
    return "source_gate_failed:src_ok_or_text_clean"


def segment_universe(s, *, source_scope: str = DEFAULT_SOURCE_SCOPE,
                     work_filter: tuple | None = None) -> tuple[list, dict]:
    """该口径下的合格段宇宙 + **排除留痕**（trace）。

    合格段集合与排序对默认口径与旧实现逐字一致；nonbenchmark 另加两条件
    的显式判定（来源白名单 + role != benchmark）。
    work_filter：None=全库（生产口径）；给定 work_id 集合时只在这些作品内
    取段并出留痕——纯确定性过滤器（审计/测试局部核对用，与 strategy_keys
    同性质，不改变任何判定口径）。"""
    if source_scope not in SOURCE_SCOPES:
        raise SystemExit(f"source_scope 只接受 {SOURCE_SCOPES}，收到 "
                         f"{source_scope!r}")
    reg = _registry_by_work(s)
    titles = {wid: (t or "") for wid, t in s.query(Work.id, Work.title).all()}
    roles = _segment_roles_by_work(s)
    if work_filter is not None:
        wf = set(work_filter)
        reg = {k: v for k, v in reg.items() if k in wf}
        roles = {k: v for k, v in roles.items() if k in wf}
        titles = {k: v for k, v in titles.items() if k in wf}
    if source_scope == "benchmark":
        q = s.query(Segment).filter(Segment.role == BENCHMARK_ROLE)
    else:
        keep_works = sorted(wid for wid, ws in reg.items()
                            if nonbenchmark_compliant_source(ws.source_type))
        q = (s.query(Segment)
             .filter(Segment.work_id.in_(keep_works),
                     or_(Segment.role.is_(None),
                         Segment.role != BENCHMARK_ROLE)))
    if work_filter is not None:
        # SQL 与 Python 两侧同过滤器：留痕范围与取段范围必须一致
        q = q.filter(Segment.work_id.in_(sorted(set(work_filter))))
    # SQL 与 Python 同判据：role 不等于 benchmark（NULL 亦不等于）——用
    # or_(is_(None), !=) 明写，避免 `!=` 在 SQL 里把 NULL 吞掉的口径漂移
    rows = q.order_by(Segment.ordinal, Segment.work_id).all()
    segs: list = []
    kept_by_work: dict[str, int] = {}
    seg_excl: dict[str, int] = {}
    role_kept: dict[str, int] = {}

    def _bump(key: str) -> None:
        seg_excl[key] = seg_excl.get(key, 0) + 1

    for seg in rows:
        if source_scope == "nonbenchmark" and (seg.role or "") == BENCHMARK_ROLE:
            _bump("role_benchmark")            # 双保险（SQL 已排，仍显式复核留痕）
            continue
        if not _src_ok(seg):
            _bump("src_ok_not_true")
            continue
        if not (seg.text_clean or "").strip():
            _bump("text_clean_empty")
            continue
        segs.append(seg)
        kept_by_work[seg.work_id] = kept_by_work.get(seg.work_id, 0) + 1
        rk = seg.role if seg.role is not None else "NULL"
        role_kept[rk] = role_kept.get(rk, 0) + 1
    excluded, qualified = [], []
    dropped_by_scope = 0
    for wid in sorted(set(roles) | set(reg) | set(kept_by_work)):
        role_map = roles.get(wid, {})
        n_in_scope = sum(n for r, n in role_map.items()
                         if _role_passes_scope(source_scope, r))
        n_out_scope = sum(role_map.values()) - n_in_scope
        dropped_by_scope += n_out_scope
        n_kept = kept_by_work.get(wid, 0)
        ws = reg.get(wid)
        line = {"work_id": wid, "title": titles.get(wid, ""),
                "source_type": ws.source_type if ws is not None else None,
                "text_version": ws.text_version if ws is not None else None,
                "n_segments": sum(role_map.values()),
                "n_segments_in_scope": n_in_scope,
                "n_segments_dropped_by_scope": n_out_scope,
                "n_segments_dropped_by_source_gate": n_in_scope - n_kept,
                "n_segments_kept": n_kept,
                "segments_by_role": dict(sorted(role_map.items()))}
        reason = _exclusion_reason(source_scope, ws, role_map, n_in_scope,
                                   n_kept)
        if reason:
            line["reason"] = reason
            excluded.append(line)
        else:
            qualified.append(line)
    n_excluded = len(excluded)
    trace = {
        "source_scope": source_scope,
        "n_sources_seen": len(set(roles) | set(reg) | set(kept_by_work)),
        "n_sources_qualified": len(qualified),
        "n_sources_excluded": n_excluded,
        "excluded_sources": excluded[:MAX_TRACE_SOURCES],
        "excluded_sources_truncated": max(0, n_excluded - MAX_TRACE_SOURCES),
        "qualified_sources": qualified[:MAX_TRACE_SOURCES],
        "qualified_sources_truncated":
            max(0, len(qualified) - MAX_TRACE_SOURCES),
        "segments_dropped_by_scope": dropped_by_scope,
        "excluded_segments": dict(sorted(seg_excl.items())),
        "segments_by_role": dict(sorted(role_kept.items())),
    }
    return segs, trace


def eligible_segments(s, *, source_scope: str = DEFAULT_SOURCE_SCOPE) -> list:
    """试点段宇宙：见 segment_universe（默认 benchmark 口径与历史逐字一致）。
    排序=(ordinal, work_id) 跨作品交错——轮转在策略间公平、段序在作品间
    交错，限量抽取才能尽早覆盖多部作品（跨作品复现证据，2026-09-23 首轮
    实测教训：按 (work_id, ordinal) 排序时 48 对全落在第一部作品，
    root_works 恒 1，复现证据出不来）。确定性排序，重跑同序。"""
    return segment_universe(s, source_scope=source_scope)[0]


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


def build_queues(s, *, strategy_keys: tuple | None = None,
                 source_scope: str = DEFAULT_SOURCE_SCOPE) -> tuple[dict, dict]:
    """确定性 per-strategy 工作队列（round-robin 的原料）+ 跳过统计。
    strategy_keys：None=全部合格策略（生产口径）；测试传本测试的键隔离
    共享测试库（driver 不感知测试存在，只是个确定性过滤器）。
    source_scope：段宇宙口径（见 segment_universe）；排除留痕并进 stats，
    因此同时出现在报告的 skips 里。"""
    strategies = (s.query(ExpressionStrategyV2)
                  .filter(ExpressionStrategyV2.status.in_(("hypothesis", "active")))
                  .order_by(ExpressionStrategyV2.strategy_key,
                            ExpressionStrategyV2.version).all())
    if strategy_keys is not None:
        strategies = [st for st in strategies
                     if st.strategy_key in strategy_keys]
    segs, trace = segment_universe(s, source_scope=source_scope)
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
    stats.update(trace)
    return queues, stats


def k3_evidence_preview(s, queues: dict) -> dict:
    """**只读预演** K3 的来源闸（app/knowledge_query._evidence_for 的同一顺序、
    同一常量）：回答「抽到的实例能不能进 K3」，不参与本驱动任何过滤决策，
    也不改 K3 一行、不放宽任何门槛（K3 排除 benchmark 段是硬口径）。

    桶键 = (work_id, 段 role 是否 benchmark)；镜像去重/区间合并属 K3 统计层，
    本预演不复制（只报来源闸命中数，宁少猜不多口径）。"""
    from app import knowledge_query as KQ
    buckets: dict[tuple[str, bool], int] = {}
    for q in queues.values():
        for it in q:
            seg = it["segment"]
            key = (seg.work_id, (seg.role or "") == BENCHMARK_ROLE)
            buckets[key] = buckets.get(key, 0) + 1
    reg = _registry_by_work(s)
    stripped: dict[str, int] = {}
    reach = 0
    for (wid, is_bench), n in sorted(buckets.items()):
        ws = reg.get(wid)
        if ws is None:
            reason = "no_registry"
        elif is_bench:
            reason = "benchmark_source"
        elif ws.source_type in KQ.DEFAULT_EXCLUDED_SOURCE_TYPES:
            reason = f"excluded_source_type:{ws.source_type}"
        elif set(ws.license_purposes or []) & KQ.DEFAULT_EXCLUDED_USES:
            reason = "excluded_use"
        elif ws.text_version not in KQ.DEFAULT_ALLOWED_TEXT_VERSIONS:
            reason = f"text_version:{ws.text_version}"
        else:
            reach += n
            continue
        stripped[reason] = stripped.get(reason, 0) + n
    return {"pairs": sum(buckets.values()), "would_reach_k3": reach,
            "stripped": dict(sorted(stripped.items())),
            "note": "只读预演，与 K3 同源常量；本驱动不改不放宽 K3 口径"}


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
                 live: bool = False, strategy_keys: tuple | None = None,
                 source_scope: str = DEFAULT_SOURCE_SCOPE) -> dict:
    """一次 run：建队列→（dry-run 即回）→轮转取对→抽→落行→统一提交。"""
    queues, stats = build_queues(s, strategy_keys=strategy_keys,
                                 source_scope=source_scope)
    n_pairs = sum(len(q) for q in queues.values())
    # 预演口径在轮转取对**之前**算——_round_robin 会 pop 掉队头 limit 项，
    # 事后再数会把「本轮要试的」从总盘里抹掉（对账必漂）
    k3_preview = k3_evidence_preview(s, queues) if dry_run else None
    report = {"mode": "dry_run" if dry_run else ("live" if live else "run"),
              "source_scope": source_scope,
              "limit": limit, "n_pending_pairs": n_pairs, "skips": stats,
              "n_candidate_sources": stats["n_sources_seen"],
              "attempted": 0, "verified": 0, "rejected_evidence": 0,
              "unverified": 0, "written": 0, "blocked_budget": False}
    picked = _round_robin(queues, limit)
    if dry_run:
        # 预演三查（主控核对「为什么 82 条全是 benchmark」的口径依据）：
        # 候选来源数 / 合格段数 / 可配对总数 + 每个被排除来源的原因 + K3 可达预演
        report["would_attempt"] = len(picked)
        report["k3_preview"] = k3_preview
        return report

    budget = KE.ExtractBudget(max_calls=max_calls, max_tokens=max_tokens)
    unv = []
    for item in picked:
        st, seg = item["strategy"], item["segment"]
        try:
            r = KE.extract_segment(
                client, strategy_id=st.id, strategy_version=st.version,
                work_id=seg.work_id, segment_id=seg.id, text=item["text"],
                text_version=item["text_version"], budget=budget, live=live,
                strategy_def={
                    "abstract_operation": st.abstract_operation,
                    "invariants": list(st.invariants or []),
                    "effect_hypothesis": st.effect_hypothesis,
                    "failure_modes": list(st.failure_modes or []),
                })
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
    ap.add_argument("--source-scope", default=DEFAULT_SOURCE_SCOPE,
                    choices=list(SOURCE_SCOPES),
                    help=("段宇宙的来源口径（默认 %(default)s=现状逐字不变）。"
                          "benchmark：只收 role='benchmark' 且 src_ok=True 且 "
                          "text_clean 非空的段——即真库 82 条实例的唯一来源，"
                          "K3 侧按硬口径全部剔除，可进 K3 的证据恒 0。"
                          "nonbenchmark（试点通道，默认关闭）：只收 "
                          "work_sources.source_type ∈ {human_fiction} ∪ "
                          "前缀 production_nonbenchmark_ 的合规人类语料，"
                          "**且**段 role 显式判定不等于 'benchmark'（NULL/train "
                          "都算非基准，判据是「不等于 benchmark」，不是把 role "
                          "当 null）；被排除的来源与段数写进报告 skips。"
                          "两值都不改 K3 的 benchmark 排除口径，也不放宽既有"
                          "幂等/预算/双闸门。"))
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
    import contextlib
    from app.live_guard import live_lock
    # R6 守卫：live 实跑与全量 pytest/live 互斥（锁文件 O_EXCL 原子创建）。
    # 顺序钉（2026-09-23 回归，与 k4 同口径）：互斥检查必须前置于预检
    # （require_models 走网络取池）、客户端构造与 init_db——旧顺序里这些
    # 副作用先跑，其异常会抢掉守卫的 SystemExit，互斥成环境依赖巧合。
    with (live_lock("k2_extract_backfill") if a.live
          else contextlib.nullcontext()):
        if a.live:
            from preflight_models import require_models   # 预检门：池外名字=整批白跑（A01 纪律）
            require_models((a.extractor_model,), source="k2_extract_backfill")
            client = _GatewayAdapter(a.extractor_model)
        db.init_db()
        with db.session() as s:
            rep = run_backfill(s, client, limit=a.limit, max_calls=a.max_calls,
                               max_tokens=a.max_tokens, dry_run=a.dry_run,
                               live=a.live, source_scope=a.source_scope)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if rep.get("mode") == "dry_run":
        k3 = rep["k3_preview"]
        print(f"[k2_extract_backfill] scope={rep['source_scope']} "
              f"候选来源={rep['n_candidate_sources']} "
              f"合格段={rep['skips']['n_eligible_segments']} "
              f"可配对={rep['n_pending_pairs']} "
              f"排除来源={rep['skips']['n_sources_excluded']} "
              f"（明细见 skips.excluded_sources）"
              f" | K3 可达预演={k3['would_reach_k3']}/{k3['pairs']} "
              f"剔除={k3['stripped']}")



if __name__ == "__main__":
    main()
