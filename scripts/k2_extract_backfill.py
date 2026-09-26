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
    用来看「抽到的能不能进 K3」，不参与任何过滤决策。K3 可达预演是**两段口径**
    （2026-09-24 钉死：旧版只报来源闸上界，对信心 hypothesis/UNCERTAIN 的真库
    报出 33703/33703 的乐观假象，见 docs/K3_可达预演口径_20260924.md）：
    · `upper_bound`（=`would_reach_k3`，字段保留）：**来源闸可达上界**，明确
      「上界，未含 scope/status/预算闸」；
    · `would_pass`：**真判据预演**——对抽到的每对 (strategy, work, segment) 逐个
      走 `app.knowledge_query` 的真函数（`eligible_statuses`/`ELIGIBLE_OBSERVATION`/
      `_scope_matches`/`_condition_pipeline`；来源闸按 `_evidence_for` 单实例块
      同序同常量求 would-be 实例），报 would_pass_scope/status/all 与逐闸剔除
      分桶。**关键纪律：预演只引用 K3 真函数与常量（import 复用），绝不复制
      常量、绝不另写主流程判定**；K3 侧缺的可复用入口如实记录为缺口（见交付文档）。
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


def integrity_dict(seg_or_value) -> dict:
    """integrity 原文 → dict 的唯一安全解析层（纯函数，绝不抛异常）。

    带 .integrity 属性的对象取其属性；已是 dict 直接用；解析失败、空值、
    **合法 JSON 但非字典**（[1,2]/"ok"/42/null）一律 → {}。
    旧写法 `json.loads(raw).get(...)` 对非字典 JSON 抛 AttributeError——
    读取侧崩溃点的根因，本函数即其收口。"""
    raw = seg_or_value.integrity if hasattr(seg_or_value, "integrity") else seg_or_value
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:                                      # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def integrity_flag_state(seg_or_value, key: str) -> bool | None:
    """integrity[key] 的严格三态：JSON true→True，JSON false→False，
    其余（缺键/"false"/1/0/null/非字典/解析失败）→ None = **未校验**。"""
    v = integrity_dict(seg_or_value).get(key)
    return v if v is True or v is False else None


def src_ok_state(seg_or_value) -> bool | None:
    """src_ok 三态口径（与写入侧 source_check.parse_src_ok 同语义，读取镜像）。"""
    return integrity_flag_state(seg_or_value, "src_ok")


def src_ok_strict(seg_or_value) -> bool:
    """源检查闸唯一读取入口（**严格布尔**，fail-closed）：integrity.src_ok
    必须是 JSON 布尔 true 才过闸；未校验（缺键/类型不严/非字典/解析失败）
    一律不过，且绝不抛异常。"""
    return src_ok_state(seg_or_value) is True


def _src_ok(seg) -> bool:
    """兼容旧调用名：口径完全等价于 src_ok_strict(seg)。"""
    return src_ok_strict(seg)


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


def _k3_source_gate(segment, text_version, *, work_source=None) -> str | None:
    """would-be 实例的**来源闸**（返回 None=过闸，否则理由串）。

    K3 侧唯一按实例判来源闸的入口是 `app.knowledge_query._evidence_for`
    （行 234–288）：它只对**已落库**的 StrategyInstance 求值，对「抽到但还没
    落库」的 pair 没有按段/按假设实例的可复用入口（本文件模块文档「缺口」项
    里如实记录）。为钉死预演与 K3 同源，这里按 `_evidence_for` 单实例块的
    **同一顺序**（行 270–279）与**同一常量**（只 import，不复制）求 would-be
    实例的判定，理由串格式与 `_evidence_for.stripped` 逐字一致：
    no_registry → benchmark_source → excluded_source_type:.. → excluded_use
    → text_version:..。若 K3 日后把该块升为公开入口，改调它、删掉本镜像。"""
    from app import knowledge_query as KQ
    ws = work_source
    if ws is None:
        return "no_registry"
    if (segment.role or "") == BENCHMARK_ROLE:
        return "benchmark_source"
    if ws.source_type in KQ.DEFAULT_EXCLUDED_SOURCE_TYPES:
        return f"excluded_source_type:{ws.source_type}"
    if set(ws.license_purposes or []) & KQ.DEFAULT_EXCLUDED_USES:
        return "excluded_use"
    if text_version not in KQ.DEFAULT_ALLOWED_TEXT_VERSIONS:
        return f"text_version:{text_version}"
    return None


def k3_would_pass(s, strategy, segment, text_version, *,
                  work_source=None, gate_cache: dict | None = None) -> dict:
    """**真判据预演（单对）**：对 (strategy, work, segment) 走 `app.knowledge_query`
    的真函数求「抽到并落 verified 实例后，能不能进 K3 的候选」：

    - status 闸：`strategy.status in eligible_statuses(version)` 且
      `observation_status in ELIGIBLE_OBSERVATION`（即 query_knowledge 候选
      预筛，行 381–384）→ `would_pass_status`；
    - scope 闸：`_scope_matches(s, strategy, {"book_id": work_id})`（真函数，
      行 291–312）→ `would_pass_scope`；
    - condition 管道：`_condition_pipeline(s, strategy.id, {})`（真函数，
      行 315–344；预演无 semantic_requirements，空 dict 保守求值，记录在理由）
      → `would_pass_condition`；
    - 来源闸：`_k3_source_gate`（`_evidence_for` 单实例块同序同常量）→
      `would_pass_source`；
    - `would_pass_all` = 四闸 AND（该对抽到即可能被 K3 选中）。

    剔除原因逐闸分桶进 `reasons`（一对可进多桶；原因串与 `_evidence_for`/
    `_scope_matches`/`_condition_pipeline` 的一致，如 `excluded_scope_uncertain`、
    `status_not_eligible`、`benchmark_source`、`text_version:xxx`）。

    `gate_cache`：跨对复用的判定 memo（key 按 strategy.id / (strategy.id,
    work_id)，值按该策略/该对策略×作品求）——把同一策略逐对的 DB 查询压到
    每策略一次 / 每(策略,作品)一次，只应在同一次预览的相同策略循环内使用。
    """
    from app import knowledge_query as KQ
    cache = {} if gate_cache is None else gate_cache
    status_key = f"status:{strategy.id}"
    if status_key not in cache:
        cache[status_key] = (
            strategy.status in KQ.eligible_statuses(strategy.version)
            and strategy.observation_status in KQ.ELIGIBLE_OBSERVATION)
    status_ok = cache[status_key]
    scope_key = f"scope:{strategy.id}:{segment.work_id}"
    if scope_key not in cache:
        cache[scope_key] = KQ._scope_matches(
            s, strategy, {"book_id": segment.work_id})
    scope_verdict = cache[scope_key]
    cond_key = f"cond:{strategy.id}"
    if cond_key not in cache:
        cache[cond_key] = KQ._condition_pipeline(s, strategy.id, {})[0]
    cond_reason = cache[cond_key]
    src_reason = _k3_source_gate(segment, text_version,
                                 work_source=work_source)
    reasons = []
    if not status_ok:
        reasons.append("status_not_eligible")
    if scope_verdict != "pass":
        reasons.append(scope_verdict)
    if cond_reason:
        reasons.append(cond_reason)
    if src_reason:
        reasons.append(src_reason)
    return {"would_pass_status": status_ok,
            "would_pass_scope": scope_verdict == "pass",
            "would_pass_condition": cond_reason is None,
            "would_pass_source": src_reason is None,
            "would_pass_all": (status_ok and scope_verdict == "pass"
                               and cond_reason is None and src_reason is None),
            "reasons": reasons}


def k3_evidence_preview(s, queues: dict) -> dict:
    """**只读预演**：K3 可达口径（两段，2026-09-24 钉死——见
    docs/K3_可达预演口径_20260924.md：旧版只复刻来源闸，对真库报出
    「可达 33703」而 K3 真身 selected=0 的乐观假象）：

    ① `upper_bound`（**来源闸可达上界**，字段 `would_reach_k3` 保留兼容）：
    `_evidence_for` 同一顺序、同一常量判来源闸（benchmark 剔除 / source_type /
    license 用途 / text_version），**明确注记「上界，未含 scope/status/预算闸」**；
    ② `would_pass`（**真判据预演**）：对抽到的 (strategy, work, segment) 逐个走
    `app.knowledge_query` 的真函数（见 `k3_would_pass`）——报 would_pass_status /
    would_pass_scope / would_pass_all 并把逐闸剔除原因分桶（如
    `excluded_scope_uncertain`、`status_not_eligible`、`benchmark_source`、
    `text_version:xxx`）。

    两段是同一pair宇宙（都 = 队列里所有 pending 对，`pairs` 恒等），改前
    `would_reach_k3` 的数字仍然一样——变的只是它被正确标注为**上界**，且
    旁边多了会照实报 0 的 `would_pass`。不参与本驱动过滤决策，不改 K3 一行。"""
    from app import knowledge_query as KQ
    reg = _registry_by_work(s)
    buckets: dict[tuple[str, bool], int] = {}
    for q in queues.values():
        for it in q:
            seg = it["segment"]
            key = (seg.work_id, (seg.role or "") == BENCHMARK_ROLE)
            buckets[key] = buckets.get(key, 0) + 1
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
    n_pairs = sum(buckets.values())

    gate_cache: dict = {}
    wp = {"pairs": 0, "would_pass_status": 0, "would_pass_scope": 0,
          "would_pass_condition": 0, "would_pass_source": 0,
          "would_pass_all": 0, "reasons": {}}
    for q in queues.values():
        for it in q:
            seg = it["segment"]
            v = k3_would_pass(s, it["strategy"], seg, it["text_version"],
                              work_source=reg.get(seg.work_id),
                              gate_cache=gate_cache)
            wp["pairs"] += 1
            for k in ("would_pass_status", "would_pass_scope",
                      "would_pass_condition", "would_pass_source",
                      "would_pass_all"):
                wp[k] += int(v[k])
            for r in v["reasons"]:
                wp["reasons"][r] = wp["reasons"].get(r, 0) + 1
    wp["reasons"] = dict(sorted(wp["reasons"].items()))
    wp["note"] = ("真判据预演：逐对走 K3 真函数（eligible_statuses+"
                  "ELIGIBLE_OBSERVATION/_scope_matches/_condition_pipeline）与 "
                  "_evidence_for 单实例块同序同常量（would-be 实例，不落库）；"
                  "would_pass_all=status∧scope∧condition∧source 全过=该对抽到即"
                  "可能被 K3 选中。逐闸剔除原因分桶（一对可进多桶）。预算闸"
                  "（candidate_cap/context_items 等）与去重/排序是查询级聚合，"
                  "非单对可判，以 query_knowledge 为准。")
    upper_bound = {
        "label": "来源闸可达上界",
        "upper_bound": True,
        "pairs": n_pairs,
        "would_reach_k3": reach,
        "stripped": dict(sorted(stripped.items())),
        "note": ("上界：只复刻来源闸（benchmark 剔除/source_type/license/"
                 "text_version），未含 status 闸（eligible_statuses/"
                 "ELIGIBLE_OBSERVATION）、scope 闸（_scope_matches）、condition"
                 " 管道（_condition_pipeline）与预算闸——真判据见 would_pass。"),
    }
    return {"pairs": n_pairs, "would_reach_k3": reach,
            "stripped": dict(sorted(stripped.items())),
            "upper_bound": upper_bound, "would_pass": wp,
            "note": "只读预演，与 K3 同源（import 复用真函数/常量）；本驱动不改不放宽 K3 口径"}


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
        # 候选来源数 / 合格段数 / 可配对总数 + 每个被排除来源的原因 + K3
        # 可达预演（两段：upper_bound 来源闸上界 + would_pass 真判据预演）
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
        wp = k3["would_pass"]
        print(f"[k2_extract_backfill] scope={rep['source_scope']} "
              f"候选来源={rep['n_candidate_sources']} "
              f"合格段={rep['skips']['n_eligible_segments']} "
              f"可配对={rep['n_pending_pairs']} "
              f"排除来源={rep['skips']['n_sources_excluded']} "
              f"（明细见 skips.excluded_sources）"
              f" | K3 可达预演={k3['would_reach_k3']}/{k3['pairs']}"
              f"(来源闸上界，未含 scope/status/预算闸) 剔除={k3['stripped']}"
              f" | 真判据预演 would_pass_all={wp['would_pass_all']}/{wp['pairs']}"
              f" pos:scope={wp['would_pass_scope']} status={wp['would_pass_status']}"
              f" 否决={wp['reasons']}")



if __name__ == "__main__":
    main()
