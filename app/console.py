"""只读控制台数据面（总方案 §18 导航的 16 个模块页，2026-09-19 任务清单 T2）。

每个模块一个聚合函数：吃一个 Session，吐一个 JSON-safe dict。铁律：

· **只读**：函数体里只许 `s.query(...)`，不许 add/commit/delete——控制台永远
  不能因为"看一眼"改变数据库（回归测试 test_console 钉住：全端点扫一遍前后
  全表行数必须逐表相等）。
· **走 ORM（db.session()）**，不许 `sqlite3.connect(he.DB)` 那种裸连接——
  那条路在测试里会静默读生产库（交接 §9 实测踩过）。
· **白名单分发**：未知模块名 404，不做动态 getattr（白名单优于黑名单，纪律⑤）。
· settings 模块**显式白名单**字段：令牌、网关密钥一律不出现在任何响应里。
· 数字全部现算现报，不落缓存——控制台是仪器读数，不是第二份真相。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config, db, observability
from .models import (BenchmarkItem, BenchmarkRun, BenchmarkSet, Candidate,
                     ControlledCorruption, Experiment, ExpressionStrategy,
                     Frame, HardCase, Job, JudgeRun, LeakageScore, LlmCall,
                     Proposition, ReportFile, ResidualDet, ResidualSem,
                     ReviewItem, Segment, Work)

# 总方案 §18 导航清单的 16 个模块（顺序即导航顺序，勿重排——T3 前端外壳按此渲染）。
MODULE_ORDER = [
    "dashboard", "corpus", "semantic-lab", "reconstruction-arena",
    "expression-residual", "strategy-atlas", "judge-arena", "preference-lab",
    "hard-cases", "benchmarks", "experiments", "models", "training-data",
    "workflow", "observability", "settings",
]

MODULE_TITLES = {
    "dashboard": "Dashboard",
    "corpus": "Corpus",
    "semantic-lab": "Semantic Lab",
    "reconstruction-arena": "Reconstruction Arena",
    "expression-residual": "Expression Residual",
    "strategy-atlas": "Strategy Atlas",
    "judge-arena": "Judge Arena",
    "preference-lab": "Preference Lab",
    "hard-cases": "Hard Cases",
    "benchmarks": "Benchmarks",
    "experiments": "Experiments",
    "models": "Models",
    "training-data": "Training Data",
    "workflow": "Workflow",
    "observability": "Observability",
    "settings": "Settings",
}

_TOP = 20          # 列表型读数的统一截断（控制台不是导出工具）
_HOURS = 24        # 观测窗口


def _count(s: Session, q) -> int:
    return q.count()


def _group_counts(s: Session, column) -> dict[str, int]:
    rows = s.query(column, func.count(column)).group_by(column).all()
    return {str(k or "∅"): v for k, v in rows}


def _head(s: Session, column, n: int = _TOP):
    return s.query(column).distinct().limit(n).all()


def _verdict_of_hv(hv) -> str:
    """从 human_verdict（dict 或 TEXT 双形态）解析 winner_resolved。"""
    if isinstance(hv, str):
        try:
            hv = json.loads(hv)
        except Exception:
            return "∅"
    if not isinstance(hv, dict):
        return "∅"
    return str(hv.get("winner_resolved") or "∅")


def _batch_tags(reasons) -> list[str]:
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except Exception:
            reasons = []
    return [r for r in (reasons or []) if isinstance(r, str) and r.startswith("batch_")]


# ── 1 dashboard ─────────────────────────────────────────────

def _dashboard(s: Session) -> dict:
    best = (s.query(BenchmarkRun.model, func.avg(BenchmarkRun.accuracy))
            .group_by(BenchmarkRun.model).order_by(func.avg(BenchmarkRun.accuracy).desc())
            .first())
    by_status = _group_counts(s, Experiment.status)
    # winner_resolved 埋在 JSON 列里，SQL 侧过滤不便（且 SQLite/ORM 双形态易错），
    # 行数是几百量级：拉出来在 Python 侧数，宁可慢一点也不要错。
    pairs = 0
    for (hv,) in s.query(ReviewItem.human_verdict).filter(ReviewItem.status == "done").all():
        if _verdict_of_hv(hv) in ("human", "candidate"):
            pairs += 1
    return {
        "human_anchors": _count(s, s.query(Segment.id).distinct()),
        "works": _count(s, s.query(Work)),
        "semantic_frames": _count(s, s.query(Frame.id).distinct()),
        "candidates_ok": _count(s, s.query(Candidate).filter(Candidate.status == "ok")),
        "human_judged": _count(s, s.query(ReviewItem).filter(ReviewItem.status == "done")),
        "preference_pairs": pairs,
        "hard_cases": _count(s, s.query(HardCase)),
        "benchmark_items": _count(s, s.query(BenchmarkItem)),
        "experiments": {"total": sum(by_status.values()), "by_status": by_status},
        "active_experiments": by_status.get("running", 0),
        "best_benchmark_model": {"model": best[0], "avg_accuracy": round(best[1], 4)} if best else None,
        "llm_calls_total": _count(s, s.query(LlmCall)),
    }


# ── 2 corpus ────────────────────────────────────────────────

def _corpus(s: Session) -> dict:
    total = _count(s, s.query(Segment))
    src = s.query(Segment.integrity).all()
    src_ok = src_bad = src_unverified = 0
    for (raw,) in src:
        try:
            d = json.loads(raw or "{}")
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        v = d.get("src_ok")
        if v is True:                       # 严格口径：只认 JSON true/false
            src_ok += 1
        elif v is False:
            src_bad += 1
        elif "src_ok" in d or d.get("src_ok_unverified"):
            src_unverified += 1            # 类型不严/显式未校验态：不算完好也不算判坏
    cleaned = _count(s, s.query(Segment).filter(Segment.text_clean.isnot(None)))
    chars = s.query(func.sum(Segment.n_chars)).scalar() or 0
    works = []
    for w in s.query(Work).order_by(Work.id.desc()).limit(_TOP).all():
        works.append({
            "id": w.id, "title": w.title, "author": w.author,
            "segments": _count(s, s.query(Segment).filter(Segment.work_id == w.id)),
        })
    return {
        "works": _count(s, s.query(Work)),
        "segments": total,
        "chars_total": int(chars),
        "text_cleaned": cleaned,
        "integrity": {"checked": src_ok + src_bad, "src_ok": src_ok, "src_bad": src_bad,
                      "src_unverified": src_unverified,
                      "unchecked": total - src_ok - src_bad - src_unverified},
        "roles": _group_counts(s, Segment.role),
        "seg_versions": _group_counts(s, Segment.seg_version),
        "recent_works": works,
    }


# ── 3 semantic-lab ──────────────────────────────────────────

def _semantic_lab(s: Session) -> dict:
    seg_with_frames = _count(s, s.query(Frame.segment_id).distinct())
    leak_by_layer: Counter = Counter()
    leak_over: Counter = Counter()
    for lay, score in s.query(LeakageScore.layer, LeakageScore.score).all():
        leak_by_layer[lay] += 1
        if score > 0.6:
            leak_over[lay] += 1
    return {
        "frames": _count(s, s.query(Frame)),
        "by_granularity": _group_counts(s, Frame.granularity),
        "by_status": _group_counts(s, Frame.status),
        "by_extractor": _group_counts(s, Frame.extractor_model),
        "segments_covered": seg_with_frames,
        "propositions": _count(s, s.query(Proposition)),
        "leakage_scores": {"total": sum(leak_by_layer.values()),
                           "by_layer": dict(leak_by_layer),
                           "over_0.6_by_layer": dict(leak_over)},
    }


# ── 4 reconstruction-arena ──────────────────────────────────

def _reconstruction(s: Session) -> dict:
    return {
        "candidates": _count(s, s.query(Candidate)),
        "by_status": _group_counts(s, Candidate.status),
        "by_model": _group_counts(s, Candidate.model),
        "by_prompt_version": _group_counts(s, Candidate.prompt_version),
        "by_temperature": {f"{t:.1f}": n for t, n in
                           s.query(Candidate.temperature, func.count(Candidate.temperature))
                           .group_by(Candidate.temperature).all()},
        "experiments_with_recon": _count(s, s.query(Candidate.experiment_id).distinct()),
        "tokens_total": int(s.query(func.sum(Candidate.tokens_in + Candidate.tokens_out)).scalar() or 0),
    }


# ── 5 expression-residual ───────────────────────────────────

def _residual(s: Session) -> dict:
    det_n = _count(s, s.query(ResidualDet))
    sem_rows = s.query(ResidualSem.payload).limit(2000).all()
    keys: Counter = Counter()
    subtext: list[float] = []
    for (payload,) in sem_rows:
        if not isinstance(payload, dict):
            continue
        for k in payload:
            keys[k] += 1
        v = payload.get("subtext_preserved")
        if isinstance(v, (int, float)):
            subtext.append(float(v))
    return {
        "residuals_det": det_n,
        "residuals_sem": _count(s, s.query(ResidualSem)),
        "by_status": _group_counts(s, ResidualSem.status),
        "by_model": _group_counts(s, ResidualSem.model),
        "sem_payload_keys": dict(keys.most_common(_TOP)),
        "subtext_preserved": {"n": len(subtext),
                              "mean": round(sum(subtext) / len(subtext), 4) if subtext else None},
    }


# ── 6 strategy-atlas ────────────────────────────────────────

def _strategies(s: Session) -> dict:
    rows = (s.query(ExpressionStrategy)
            .order_by(ExpressionStrategy.n_items.desc()).limit(_TOP).all())
    return {
        "strategies": _count(s, s.query(ExpressionStrategy)),
        "by_method": _group_counts(s, ExpressionStrategy.method),
        "cluster_items_total": int(s.query(func.sum(ExpressionStrategy.n_items)).scalar() or 0),
        "top_by_size": [{
            "id": r.id, "name": r.name, "n_items": r.n_items,
            "success_rate": round(r.success_rate, 4),
            "wilson": [round(r.rate_lo, 4), round(r.rate_hi, 4)],
            "method": r.method, "version": r.version,
        } for r in rows],
    }


# ── 7 judge-arena ───────────────────────────────────────────

def _judge_arena(s: Session) -> dict:
    by_model: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])  # calls, abstain, failed
    for model, pv, abstain, status in s.query(
            JudgeRun.model, JudgeRun.prompt_version, JudgeRun.abstain, JudgeRun.status).all():
        rec = by_model[(model, pv)]
        rec[0] += 1
        rec[1] += bool(abstain)
        rec[2] += status != "ok"
    board = [{
        "model": m, "prompt_version": pv, "calls": c,
        "abstain_rate": round(a / c, 4) if c else None,
        "failed_rate": round(f / c, 4) if c else None,
    } for (m, pv), (c, a, f) in sorted(by_model.items(), key=lambda kv: -kv[1][0])[:_TOP]]
    return {
        "judge_runs": _count(s, s.query(JudgeRun)),
        "by_kind": _group_counts(s, JudgeRun.judge_kind),
        "by_status": _group_counts(s, JudgeRun.status),
        "abstain_total": _count(s, s.query(JudgeRun).filter(JudgeRun.abstain.is_(True))),
        "leaderboard": board,
        "known_bias": "corr24 控制臂上评委约 77% 偏 AI 版（方向性偏差，读数必对照；见交接 §0.5③）",
    }


# ── 8 preference-lab ────────────────────────────────────────

def _preference_lab(s: Session) -> dict:
    items = s.query(ReviewItem.status, ReviewItem.reasons, ReviewItem.human_verdict).all()
    by_status: Counter = Counter()
    winners: Counter = Counter()
    batches: Counter = Counter()
    for status, reasons, hv in items:
        by_status[status] += 1
        if status == "done":
            winners[_verdict_of_hv(hv)] += 1
        for tag in _batch_tags(reasons):
            batches[tag] += 1
    return {
        "review_items": len(items),
        "by_status": dict(by_status),
        "winner_resolved": dict(winners),
        "batches": dict(batches.most_common(_TOP)),
        "gate": "agreement ≥ 0.70（未过；评委 κ 全部低于恒定答 human 基线，见终报 docs/final-report.md）",
    }


# ── 9 hard-cases ────────────────────────────────────────────

def _hard_cases(s: Session) -> dict:
    rows = (s.query(HardCase).order_by(HardCase.severity.desc()).limit(_TOP).all())
    return {
        "hard_cases": _count(s, s.query(HardCase)),
        "by_kind": _group_counts(s, HardCase.kind),
        "need_human": _count(s, s.query(HardCase).filter(HardCase.need_human.is_(True))),
        "need_ontology": _count(s, s.query(HardCase).filter(HardCase.need_ontology.is_(True))),
        "need_strategy": _count(s, s.query(HardCase).filter(HardCase.need_strategy.is_(True))),
        "top_by_severity": [{
            "id": r.id, "kind": r.kind, "severity": round(r.severity, 4),
            "experiment_id": r.experiment_id, "why": (r.why or "")[:200],
            "need_human": r.need_human,
        } for r in rows],
    }


# ── 10 benchmarks ───────────────────────────────────────────

def _benchmarks(s: Session) -> dict:
    sets = s.query(BenchmarkSet).all()
    runs = (s.query(BenchmarkRun).order_by(BenchmarkRun.created_at.desc())
            .limit(200).all())
    per_model: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        per_model[r.model].append(r.accuracy)
    board = sorted(({"model": m, "runs": len(v), "avg_accuracy": round(sum(v) / len(v), 4),
                     "best": round(max(v), 4)} for m, v in per_model.items()),
                   key=lambda d: -d["avg_accuracy"])
    detail_types: Counter = Counter()
    for r in runs[:_TOP]:
        for key, one in ((r.detail or {}).get("by_type") or {}).items():
            detail_types[key] += one.get("n", 0) if isinstance(one, dict) else 0
    return {
        "sets": [{"id": t.id, "name": t.name, "version": t.version, "kind": t.kind,
                  "n_items": t.n_items, "created_at": t.created_at} for t in sets],
        "items": _count(s, s.query(BenchmarkItem)),
        "runs": _count(s, s.query(BenchmarkRun)),
        "leaderboard": board,
        "latest_by_type": dict(detail_types),
        "hidden_rule": "role='benchmark' 的段不进训练导出；基准条目冻结文本（§14）",
    }


# ── 11 experiments ──────────────────────────────────────────

def _experiments(s: Session) -> dict:
    rows = (s.query(Experiment).order_by(Experiment.created_at.desc()).limit(_TOP).all())
    return {
        "total": _count(s, s.query(Experiment)),
        "by_status": _group_counts(s, Experiment.status),
        "recent": [{
            "id": e.id, "name": e.name, "status": e.status,
            "created_at": e.created_at, "updated_at": e.updated_at,
            "n_segments": len((e.config or {}).get("segment_ids") or []),
            "error": (e.error or "")[:200] or None,
        } for e in rows],
        "reports": _count(s, s.query(ReportFile)),
    }


# ── 12 models ───────────────────────────────────────────────

def _models(s: Session) -> dict:
    reg_path = Path(config.DATA_DIR) / "model_registry.json"
    registry = None
    if reg_path.exists():
        try:
            registry = json.loads(reg_path.read_text(encoding="utf-8"))
        except Exception:
            registry = {"_error": "model_registry.json 解析失败（纪律④：不许静默）"}
    rows = s.query(LlmCall.model, LlmCall.status, LlmCall.tokens_in, LlmCall.tokens_out).all()
    agg: dict[str, list] = defaultdict(lambda: [0, 0, 0, 0])   # calls, failed, tin, tout
    for model, status, ti, to in rows:
        rec = agg[model]
        rec[0] += 1
        rec[1] += status != "ok"
        rec[2] += ti or 0
        rec[3] += to or 0
    usage = [{"model": m, "calls": c, "failed": f, "tokens_in": ti, "tokens_out": to,
              "failed_rate": round(f / c, 4) if c else None}
             for m, (c, f, ti, to) in sorted(agg.items(), key=lambda kv: -kv[1][0])]
    return {
        "registry": registry,
        "usage_all_time": usage[:_TOP],
        "llm_calls_total": sum(v[0] for v in agg.values()),
        "serial_discipline": "agy/qoder/wb 前缀=本机 CLI 单账号 → 必须串行（gateway.is_serial_model）",
    }


# ── 13 training-data ────────────────────────────────────────

def _training_data(s: Session) -> dict:
    exports = Path(config.DATA_DIR) / "exports"
    files = []
    if exports.exists():
        for p in sorted(exports.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:_TOP]:
            files.append({"file": p.name, "bytes": p.stat().st_size})
    corr = _group_counts(s, ControlledCorruption.status)
    negatives = 0
    for status, drift_ok, fact in s.query(
            ControlledCorruption.status, ControlledCorruption.drift_ok,
            ControlledCorruption.fact_consistent).all():
        if status == "ok" and drift_ok and fact:
            negatives += 1
    return {
        "export_files": files,
        "corruptions": {"total": _count(s, s.query(ControlledCorruption)), "by_status": corr},
        "negative_pool_eligible": negatives,
        "sft_note": "SFT 目标=人类原文（--from-frames 零人工）；DPO 默认方向已被集霸裁定否掉，"
                    "只认 --strict / 负面库（交接 §0.5③）",
        "benchmark_isolation": "role='benchmark' 段不进任何导出",
    }


# ── 14 workflow ─────────────────────────────────────────────

_WORKFLOWS = [
    {"id": "A", "name": "Human Reconstruction", "steps": 10, "engine": "app/engine.py 状态机"},
    {"id": "B", "name": "Controlled Corruption",
     "steps": "18 类单变量 + 控制臂", "engine": "scripts/controlled_corruption.py"},
    {"id": "C", "name": "Minimal Pair", "steps": "实验矩阵", "engine": "app/engine.py（v2 报告待做）"},
    {"id": "D", "name": "Strategy Discovery", "steps": "聚类+归纳", "engine": "scripts/strategy_discovery.py"},
    {"id": "E", "name": "Hard Case Mining", "steps": "分诊入库", "engine": "scripts/hard_case_mining.py"},
]


def _workflow(s: Session) -> dict:
    jobs = s.query(Job.kind, Job.status, func.count(Job.id)).group_by(Job.kind, Job.status).all()
    by_kind: dict[str, dict[str, int]] = defaultdict(dict)
    for kind, status, n in jobs:
        by_kind[kind][status] = n
    return {
        "workflows": _WORKFLOWS,
        "jobs": {"by_kind": dict(by_kind),
                 "total": _count(s, s.query(Job)),
                 "pending": _count(s, s.query(Job).filter(Job.status == "pending")),
                 "failed": _count(s, s.query(Job).filter(Job.status == "failed"))},
        "engine_stages": ["plan", "source_check", "extract", "reconstruct",
                          "residual", "judge", "report"],
    }


# ── 15 observability ────────────────────────────────────────

def _observability(s: Session) -> dict:
    return {"window_hours": _HOURS, "snapshot": observability.snapshot(s, hours=_HOURS)}


# ── 16 settings ─────────────────────────────────────────────

def _settings(s: Session) -> dict:
    # ⚠ 显式白名单：这里出现什么，响应里就有什么。令牌 / API key / 隧道 URL 永不进。
    return {
        "llm_mode": config.LLM_MODE,
        "database_backend": config.DATABASE_URL.split(":", 1)[0],
        "data_dir": str(config.DATA_DIR),
        "blind_review_prompt_versions": list(config.BLIND_REVIEW_PROMPT_VERSIONS),
        "servable_prompt_versions": list(config.SERVABLE_PROMPT_VERSIONS),
        "extractor_models": list(config.EXTRACTOR_MODELS),
        "default_recon_models": list(config.DEFAULT_RECON_MODELS),
        "modules": MODULE_ORDER,
        "readonly": True,
        "secrets_policy": "令牌与密钥不出现在 /console/* 任何响应里",
    }


MODULE_FUNCS = {
    "dashboard": _dashboard,
    "corpus": _corpus,
    "semantic-lab": _semantic_lab,
    "reconstruction-arena": _reconstruction,
    "expression-residual": _residual,
    "strategy-atlas": _strategies,
    "judge-arena": _judge_arena,
    "preference-lab": _preference_lab,
    "hard-cases": _hard_cases,
    "benchmarks": _benchmarks,
    "experiments": _experiments,
    "models": _models,
    "training-data": _training_data,
    "workflow": _workflow,
    "observability": _observability,
    "settings": _settings,
}


def index() -> list[dict]:
    return [{"slug": slug, "title": MODULE_TITLES[slug]} for slug in MODULE_ORDER]


def module_data(module: str) -> dict:
    if module not in MODULE_FUNCS:      # 白名单分发：未知 → 调用方报 404
        raise KeyError(module)
    with db.session() as s:
        return {"module": module, "title": MODULE_TITLES[module],
                "data": MODULE_FUNCS[module](s)}
