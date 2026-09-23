"""实验引擎（总方案 §50 任务 12）：把零散脚本收进一条**阶段状态机**。

    plan → source_check → extract → reconstruct → residual → judge → report

## 与 app/experiments.py 的关系

那个模块是 10-stage 校准管线的**实体实现**（stage_extract_frames / stage_reconstruct /
stage_judges…），本模块只做**编排**：不重写任何已工作的 stage，而是按上面的顺序调用它们，
再补上原来游离在管线外的 source_check（源校勘，见 scripts/source_check.py）。

## 四条硬要求（任务 12 验收口径）

1. **可从任意阶段续跑**：每阶段的状态与计数写进 `experiments.stats["engine"]["stages"]`；
   status=done 的阶段重跑时直接跳过（阶段级幂等），stage 函数本身又各自跳过已有产物
   （产品级幂等）——两层幂等，跑两遍不重复计数、不重复花钱。
2. **每阶段产出写 experiments.stats（JSON）**：attempted / ok / failed / skipped /
   seconds（耗时）/ tokens。
3. **token 与失败率一律从 llm_calls 按实验聚合**（PURPOSE_BY_STAGE 把 purpose 前缀
   映射回阶段），引擎自己不另记一套账——llm_calls 是唯一账本。
4. **失败要抛错**：阶段异常先落 stats（status=failed + error）再原样上抛；
   前置产物缺失（比如没 Frame 就要 reconstruct）也直接抛 EngineError，
   绝不让阶段静默空转、绝不带着空数据往下走。

## CLI

    python scripts/run_experiment.py --exp EXP-XXX              # 全部阶段
    python scripts/run_experiment.py --exp EXP-XXX --stages plan,extract
    python scripts/run_experiment.py --exp EXP-XXX --dry-run --json   # 只打印计划，零调用
    python scripts/run_experiment.py --exp EXP-XXX --mock       # LG_LLM_MODE=mock 端到端
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from sqlalchemy import and_, case, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import config, experiments
from .db import session
from .ids import new_id
from .models import (Candidate, Experiment, Frame, JudgeRun, LlmCall,
                     ReportFile, ResidualDet, ResidualSem, Segment)

ENGINE_STAGES = ["plan", "source_check", "extract", "reconstruct",
                 "residual", "judge", "report"]

# 每阶段归属的 llm_calls.purpose 前缀。口径对齐各调用点的 purpose 实串：
#   extract 阶段 = 帧（"extract:S"、修复 "extract:S:repair"）+ 命题分解（"propositions"）
#   judge   阶段 = judge_semantic / judge_naturalness(_v2) / judge_adversarial / judge_preference
# 没有列出的阶段（plan / report）按定义不发生 LLM 调用。
PURPOSE_BY_STAGE = {
    "plan": (),
    "source_check": ("source_integrity",),
    "extract": ("extract", "propositions"),
    "reconstruct": ("reconstruct",),
    "residual": ("sem_residual",),
    "judge": ("judge",),
    "report": (),
}

# 需要存在的 ReportFile kinds（report 阶段的"已完成"判据）
REPORT_KINDS = ("calibration_md", "calibration_json")


class EngineError(Exception):
    """引擎级错误：实验不存在 / 已冻结 / 阶段名未知 / 前置产物缺失——宁可报错不静默空转。"""


class EngineStageError(EngineError):
    """某个阶段执行失败（原始异常挂在 __cause__ 上）。"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _as_dict(v) -> dict:
    """JSON 列双形态容错（铁律⑤）：ORM 已解析成 dict，裸串则 json.loads。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}
    return {}


def _exp(s: Session, exp_id: str) -> Experiment:
    e = s.get(Experiment, exp_id)
    if e is None:
        raise EngineError(f"experiment 不存在: {exp_id}")
    return e


def _resolve(stages) -> list[str]:
    """把 --stages 参数解析成**状态机顺序**的阶段列表（子集内的顺序也恒为 canonical）。"""
    if stages is None:
        return list(ENGINE_STAGES)
    if isinstance(stages, str):
        names = [x.strip() for x in stages.split(",") if x.strip()]
    else:
        names = [str(x).strip() for x in stages if str(x).strip()]
    unknown = [x for x in names if x not in ENGINE_STAGES]
    if unknown:
        raise EngineError(f"未知阶段 {unknown}；可选 {ENGINE_STAGES}")
    return [x for x in ENGINE_STAGES if x in names]


def _llm_usage(s: Session, exp_id: str, prefixes) -> dict:
    """从 llm_calls 按实验聚合（要求③：引擎不另记账）。

    prefixes=None → 该实验全部 purpose（引擎总量账）；prefixes=() → 该阶段按定义
    不发生调用（plan/report），必须记 0——两种空不能混；列表 → 前缀任一命中即算。
    failure_rate = 非 ok 调用 / 总调用；无调用时记 0.0（不造 NaN）。
    """
    q = s.query(LlmCall).filter(LlmCall.experiment_id == exp_id)
    if prefixes is None:
        rows = q.all()
    elif prefixes:
        rows = q.filter(or_(*[LlmCall.purpose.like(p + "%") for p in prefixes])).all()
    else:
        rows = []
    calls = len(rows)
    tokens = sum(r.tokens_in + r.tokens_out for r in rows)
    failed = sum(1 for r in rows if r.status != "ok")
    return {"tokens": tokens, "llm_calls": calls, "llm_failed": failed,
            "failure_rate": round(failed / calls, 4) if calls else 0.0}


# ── 前置检查：产物缺失就直接报错，不让阶段静默空转（纪律④）──────

def _prereq_missing(s: Session, exp_id: str, name: str) -> str | None:
    if name in ("plan",):
        return None
    if name in ("source_check", "extract"):
        ids = (s.get(Experiment, exp_id).config or {}).get("segment_ids") or []
        if not ids or not s.query(Segment).filter(Segment.id.in_(ids)).count():
            return "config.segment_ids 里没有可用的 Segment（实验没挂上语料）"
        return None
    if name == "reconstruct":
        n = s.query(Frame).filter_by(experiment_id=exp_id, is_primary=True)\
            .filter(Frame.status != "failed").count()
        if not n:
            return "还没有 primary Frame（先跑 extract）"
        return None
    if name in ("residual", "judge", "report"):
        n = s.query(Candidate).filter_by(experiment_id=exp_id, status="ok").count()
        if not n:
            return "还没有 status=ok 的候选（先跑 reconstruct）"
        return None
    return None


# ── 各阶段：body（干活，返回 detail）+ before/collect（统计口径）──

def _stage_plan(s: Session, exp: Experiment) -> dict:
    """计划阶段：只补缺配置、解析默认模型池、验段，不发生任何 LLM 调用。

    兼容两类实验：calibration 管线建的（config 已全）和 scale_corpus 等脚本
    手工建的（config 只有 segment_ids）——只填缺失键，绝不覆盖既有配置。
    """
    cfg = exp.config or {}
    filled = sorted(k for k in experiments.DEFAULT_CONFIG if k not in cfg)
    merged = {**experiments.DEFAULT_CONFIG, **cfg}
    merged["extractors"] = merged.get("extractors") or config.EXTRACTOR_MODELS
    merged["recon_models"] = merged.get("recon_models") or config.DEFAULT_RECON_MODELS
    merged["judge_models"] = merged.get("judge_models") or merged["recon_models"][:1]
    exp.config = merged
    s.commit()
    ids = merged.get("segment_ids") or []
    segs = s.query(Segment).filter(Segment.id.in_(ids)).all()
    if not segs:
        raise EngineError(f"{exp.id} 的 config.segment_ids 在 segments 表里一条都查不到")
    return {"segments": len(segs), "works": len({x.work_id for x in segs}),
            "granularities": merged["granularities"],
            "recon_models": merged["recon_models"],
            "judge_models": merged["judge_models"],
            "config_filled": filled}


def _integrity_state(rows: list[Segment]) -> dict:
    """统计口径严格化：只 `is True` 计完好、`is False` 计判坏，
    其余（字符串/数字/null/缺字段）计未校验。旧键全保留，新增 src_unverified。"""
    checked = ok_true = ok_false = unverified = 0
    for x in rows:
        d = _as_dict(x.integrity)
        v = d.get("src_ok")
        if v is True:
            checked += 1
            ok_true += 1
        elif v is False:
            checked += 1
            ok_false += 1
        elif "src_ok" in d or d.get("src_ok_unverified"):
            unverified += 1
    return {"checked": checked, "src_ok": ok_true, "src_bad": ok_false,
            "src_unverified": unverified}


def _stage_source_check(s: Session, exp: Experiment) -> dict:
    """源校勘（scripts/source_check.py）：只判文本完整性，不评文笔。

    复用脚本自己的 run()：规则命中直接判坏不花 LLM 的钱、已有 integrity 的段幂等跳过、
    每段一个 session 落 segments.integrity——这些都不要重写。exp_id 用来把
    本阶段的 llm_calls 归账到本实验（要求③）。
    脚本的进度 print 收进 detail.script_log：引擎在 API 后台线程里跑，stdout 不能喷。
    """
    import io
    from contextlib import redirect_stdout

    import scripts.source_check as sc

    ids = (exp.config or {}).get("segment_ids") or []
    sc._stat.update(ok=0, failed=0, skip=0, bad=0, unverified=0)   # 模块全局计数是跨趟累计的，先归零
    buf = io.StringIO()
    with redirect_stdout(buf):
        sc.run(ids=ids, exp_id=exp.id, conc=int(exp.config.get("concurrency") or 4))
    after = _integrity_state(s.query(Segment).filter(Segment.id.in_(ids)).all())
    if ids and after["checked"] == 0:
        # 全军覆没不往下走：extract 拿到的将全是没校勘过的段（静默劣化，最忌讳）
        raise RuntimeError(f"source_integrity 一个段都没检查成（共 {len(ids)} 段）——"
                           f"查 llm_calls(purpose=source_integrity) 看失败原因")
    return {"src_ok": after["src_ok"], "src_bad": after["src_bad"],
            "new_checked": after["checked"],
            "script_log": buf.getvalue().strip()[-500:]}


def _stage_extract(s: Session, exp: Experiment) -> dict:
    """抽取 = 帧（双抽+修复）+ 命题分解 + 静态泄漏三层。

    命题与泄漏本来是校准管线的独立 stage，这里并进来是因为下游 judge_semantic
    与报告直接读它们的产物；且命题分解幂等（按 prompt_version 复用）、
    leakage_static 零 LLM 成本——并进来不改变幂等性质，少两个可忘跑的阶段。
    计数（含命题覆盖数）统一在 collect 里从 DB 现算。
    """
    experiments.stage_extract_frames(s, exp)
    experiments.stage_extract_propositions(s, exp)
    experiments.stage_leakage_static(s, exp)
    return {}


def _stage_reconstruct(s: Session, exp: Experiment) -> dict:
    experiments.stage_reconstruct(s, exp)
    return {}


def _stage_residual(s: Session, exp: Experiment) -> dict:
    experiments.stage_residual_det(s, exp)
    experiments.stage_residual_sem(s, exp)
    return {}


def _stage_judge(s: Session, exp: Experiment) -> dict:
    experiments.stage_judges(s, exp)
    from .review import build_review_queue
    n = build_review_queue(s, exp.id)   # 队列依赖 judge 分数，幂等（refresh 不重复建条）
    return {"review_enqueued": n}


def _stage_report(s: Session, exp: Experiment) -> dict:
    from . import report as report_mod
    out = report_mod.build_calibration_report(s, exp.id)
    return {"md": out.get("md"), "json": out.get("json"), "recommended": out.get("recommended")}


# ── 统计口径：attempted=本阶段职责内条目总数（累计目标量）；
#    ok=已完成且有效；failed=职责内尚未完成的残留；
#    skipped=本趟进来时就已经完成的条数（阶段级幂等跳过的部分）。
#    全部从 DB 产品行现查现算——重跑两遍数字只会重合，不会累加。


def _ok_cand_ids(s: Session, exp_id: str) -> set[str]:
    return {c.id for c in s.query(Candidate)
            .filter_by(experiment_id=exp_id, status="ok").all()}


def _before_plan(s: Session, exp: Experiment) -> int:
    return 0


def _collect_plan(s: Session, exp: Experiment, before: int) -> dict:
    return {"attempted": len(ENGINE_STAGES), "ok": len(ENGINE_STAGES),
            "failed": 0, "skipped": before}


def _before_source_check(s: Session, exp: Experiment) -> int:
    ids = (exp.config or {}).get("segment_ids") or []
    return _integrity_state(s.query(Segment).filter(Segment.id.in_(ids)).all())["checked"]


def _collect_source_check(s: Session, exp: Experiment, before: int) -> dict:
    ids = (exp.config or {}).get("segment_ids") or []
    st = _integrity_state(s.query(Segment).filter(Segment.id.in_(ids)).all())
    return {"attempted": len(ids), "ok": st["checked"],
            "failed": len(ids) - st["checked"], "skipped": before}


def _before_extract(s: Session, exp: Experiment) -> int:
    return s.query(Frame).filter_by(experiment_id=exp.id).count()


def _collect_extract(s: Session, exp: Experiment, before: int) -> dict:
    cfg = exp.config or {}
    frames = s.query(Frame).filter_by(experiment_id=exp.id).all()
    ok = sum(1 for f in frames if f.status in ("ok", "repaired"))
    failed = sum(1 for f in frames if f.status == "failed")
    attempted = (len(cfg.get("segment_ids") or []) * len(cfg.get("granularities") or [])
                 * len(cfg.get("extractors") or []))
    from .models import Proposition
    from .residual_sem import PROPOSITIONS_PROMPT_VERSION
    ids = cfg.get("segment_ids") or []
    prop_covered = len({p.segment_id for p in s.query(Proposition).filter(
        Proposition.segment_id.in_(ids),
        Proposition.prompt_version == PROPOSITIONS_PROMPT_VERSION).all() if p.propositions})
    return {"attempted": attempted, "ok": ok, "failed": failed, "skipped": before,
            "detail_extra": {"proposition_segments": prop_covered,
                             "granularities": len(cfg.get("granularities") or []),
                             "extractors": len(cfg.get("extractors") or [])}}


def _before_reconstruct(s: Session, exp: Experiment) -> int:
    return s.query(Candidate).filter_by(experiment_id=exp.id).count()


def _collect_reconstruct(s: Session, exp: Experiment, before: int) -> dict:
    cfg = exp.config or {}
    n_frames = s.query(Frame).filter_by(experiment_id=exp.id, is_primary=True)\
        .filter(Frame.status != "failed").count()
    attempted = (n_frames * len(cfg.get("recon_models") or [])
                 * len(cfg.get("temperatures") or []) * int(cfg.get("samples_per_pair") or 0))
    rows = s.query(Candidate).filter_by(experiment_id=exp.id).all()
    ok = sum(1 for c in rows if c.status == "ok")
    return {"attempted": attempted, "ok": ok, "failed": len(rows) - ok, "skipped": before}


def _before_residual(s: Session, exp: Experiment) -> int:
    cand_ids = _ok_cand_ids(s, exp.id)
    if not cand_ids:
        return 0
    n_det = s.query(ResidualDet).filter(ResidualDet.candidate_id.in_(cand_ids)).count()
    n_sem = s.query(ResidualSem).filter(ResidualSem.candidate_id.in_(cand_ids)).count()
    return n_det + n_sem


def _collect_residual(s: Session, exp: Experiment, before: int) -> dict:
    cand_ids = _ok_cand_ids(s, exp.id)
    if cand_ids:
        det_ids = {r.candidate_id for r in s.query(ResidualDet)
                   .filter(ResidualDet.candidate_id.in_(cand_ids)).all()}
        sem_ok = {r.candidate_id for r in s.query(ResidualSem)
                  .filter(ResidualSem.candidate_id.in_(cand_ids),
                          ResidualSem.status == "ok").all()}
    else:
        det_ids, sem_ok = set(), set()
    return {"attempted": len(cand_ids) * 2, "ok": len(det_ids) + len(sem_ok),
            "failed": len(cand_ids) * 2 - len(det_ids) - len(sem_ok), "skipped": before,
            "detail_extra": {"det_covered": len(det_ids), "sem_ok_covered": len(sem_ok)}}


def _before_judge(s: Session, exp: Experiment) -> int:
    return s.query(JudgeRun).filter_by(experiment_id=exp.id).count()


def _collect_judge(s: Session, exp: Experiment, before: int) -> dict:
    cfg = exp.config or {}
    n_cands = len(_ok_cand_ids(s, exp.id))
    n_models = len(cfg.get("judge_models") or [])
    attempted = n_cands * n_models * 3
    if cfg.get("judge_human_naturalness", True):
        attempted += len(cfg.get("segment_ids") or []) * n_models
    rows = s.query(JudgeRun).filter_by(experiment_id=exp.id).all()
    ok = sum(1 for r in rows if r.status == "ok")
    return {"attempted": attempted, "ok": ok, "failed": len(rows) - ok, "skipped": before}


def _before_report(s: Session, exp: Experiment) -> int:
    return s.query(ReportFile).filter_by(experiment_id=exp.id).count()


def _collect_report(s: Session, exp: Experiment, before: int) -> dict:
    kinds = {r.kind for r in s.query(ReportFile).filter_by(experiment_id=exp.id).all()}
    ok = len(kinds & set(REPORT_KINDS))
    return {"attempted": len(REPORT_KINDS), "ok": ok,
            "failed": len(REPORT_KINDS) - ok, "skipped": before}


# 阶段注册表：body 干活 / before 记入口存量（供 skipped）/ collect 从 DB 现算计数。
# 测试通过 setitem(STAGE_IMPL[...], "body", ...) 注入失败来验证失败传播。
STAGE_IMPL = {
    "plan": {"body": _stage_plan, "before": _before_plan, "collect": _collect_plan},
    "source_check": {"body": _stage_source_check, "before": _before_source_check,
                     "collect": _collect_source_check},
    "extract": {"body": _stage_extract, "before": _before_extract,
                "collect": _collect_extract},
    "reconstruct": {"body": _stage_reconstruct, "before": _before_reconstruct,
                    "collect": _collect_reconstruct},
    "residual": {"body": _stage_residual, "before": _before_residual,
                 "collect": _collect_residual},
    "judge": {"body": _stage_judge, "before": _before_judge, "collect": _collect_judge},
    "report": {"body": _stage_report, "before": _before_report, "collect": _collect_report},
}


def _mark(s: Session, exp: Experiment, name: str, status: str, *, t0: float,
          counters: dict, detail: dict | None = None, error: Exception | None = None) -> None:
    """把本趟的阶段结果写进 stats["engine"]（要求②），并镜像顶层 current_stage。"""
    st = dict(exp.stats or {})
    eng = dict(st.get("engine") or {})
    stages = dict(eng.get("stages") or {})
    prev = dict(stages.get(name) or {})
    rec = {
        "status": status,
        "attempted": counters.get("attempted", 0),
        "ok": counters.get("ok", 0),
        "failed": counters.get("failed", 0),
        "skipped": counters.get("skipped", 0),
        "seconds": round(time.perf_counter() - t0, 3),
        **_llm_usage(s, exp.id, PURPOSE_BY_STAGE.get(name) or ()),
        "runs": int(prev.get("runs") or 0) + 1,
        "finished_at": _now(),
    }
    detail = {**(detail or {}), **(counters.get("detail_extra") or {})}
    if detail:
        rec["detail"] = detail
    if error is not None:
        rec["error"] = f"{type(error).__name__}: {error}"[:400]
    stages[name] = rec
    eng["stages"] = stages
    eng["stage_order"] = list(ENGINE_STAGES)
    eng["current_stage"] = name
    eng["llm_totals"] = _llm_usage(s, exp.id, None)   # None = 该实验全部 purpose
    st["engine"] = eng
    st["current_stage"] = name   # 顶层镜像：旧列表接口 / 前端直接读它
    exp.stats = st
    exp.updated_at = _now()
    s.commit()


def _execute(s: Session, exp: Experiment, name: str) -> None:
    """跑一个阶段：成功落 done；失败先落 stats 再原样上抛（要求④，不静默继续）。"""
    impl = STAGE_IMPL[name]
    t0 = time.perf_counter()
    before = impl["before"](s, exp)
    try:
        detail = impl["body"](s, exp) or {}
        counters = impl["collect"](s, exp, before)
        _mark(s, exp, name, "done", t0=t0, counters=counters, detail=detail)
    except Exception as e:                      # noqa: BLE001
        try:
            counters = impl["collect"](s, exp, before)
        except Exception:                       # noqa: BLE001  统计器自己坏了也别挡住报错
            counters = {"attempted": 0, "ok": 0, "failed": 0, "skipped": before}
        _mark(s, exp, name, "failed", t0=t0, counters=counters, error=e)
        exp.status = "failed"
        exp.error = f"stage {name}: {type(e).__name__}: {e}"[:500]
        s.commit()
        raise EngineStageError(f"阶段 {name} 失败：{type(e).__name__}: {e}") from e


def stage_states(exp: Experiment) -> dict:
    """各阶段当前状态（GET /experiments/{id}/stages 用）；没跑过的阶段记 pending。"""
    recs = (((exp.stats or {}).get("engine") or {}).get("stages") or {})
    return {name: recs.get(name) or {"status": "pending"} for name in ENGINE_STAGES}


def plan_run(exp_id: str, stages=None) -> dict:
    """dry-run：只算计划，不建配置、不写 stats、不发调用（零副作用）。

    实验不存在也能出计划（CLI 首次验收就打在没建过的 EXP-DEMO 上），
    诚实标注 exists=false；已存在则逐阶段给 action：skip（stats 已 done）/
    run / blocked（前置缺失，附原因）。
    """
    names = _resolve(stages)
    with session() as s:
        exp = s.get(Experiment, exp_id)
        if exp is None:
            return {"exists": False, "stage_order": list(ENGINE_STAGES),
                    "stages": {n: {"action": "run"} for n in names},
                    "note": "实验不存在：dry-run 只打印计划，不创建不修改任何数据"}
        rows = {}
        eng = ((exp.stats or {}).get("engine") or {}).get("stages") or {}
        for name in names:
            if (eng.get(name) or {}).get("status") == "done":
                rows[name] = {"action": "skip", "reason": "stats.engine 里已 done（续跑幂等）"}
                continue
            miss = _prereq_missing(s, exp.id, name)
            rows[name] = ({"action": "blocked", "reason": miss} if miss
                          else {"action": "run"})
        return {"exists": True, "status": exp.status, "stage_order": list(ENGINE_STAGES),
                "stages": rows}


def _stage_record(exp_id: str, name: str) -> dict:
    """读某阶段在 stats["engine"] 里的当前记录（独立短会话，读完即关）。"""
    with session() as s:
        exp = _exp(s, exp_id)
        return (((exp.stats or {}).get("engine") or {}).get("stages") or {})\
            .get(name) or {}


def claim_run(exp_id: str, token: str) -> bool:
    """A07：**原子领取执行权**——条件 UPDATE，只有赢得条件竞争的那一个能跑。

    领取条件（命中即可翻 running）：
    ① status NOT IN (running, releasing)——正常路径 created/done/failed 可领；
      **releasing 不可领**（会审四轮严重项）：release 生效时原 runner 可能
      还在阶段体里（PROD 级数小时）——立刻放行新领取=A/B 双跑双计费
      +stats 互踩，正是本修复要消灭的事故；releasing 由原 runner 的
      收尾（或再次 release）翻成 failed 后才重新可领。
    ② status == running 且 run_owner IS NULL——**存量对账**（三轮严重项）：
      迁移前卡在 running 的老行没有 owner，不放开则永久领不到执行权。
      侧门收窄（四轮一般项）：NULL-owner 只在 status==running 时可领——
      release 产生的是 releasing+NULL，走不进本条；今后任何「写 running
      不写 owner」的新路径仍会变成可抢行，靠本条注释 + 审查盯住。
    凭据（owner/领取时间戳）随领取一次写入；时间戳只做审计追溯，**无
    TTL 判定**——自动过期会在数小时长跑中途误抢；接管必须显式（release_run）。
    SQLITE_BUSY 防御：只对「database is locked / busy」退避重试（四轮
    一般项：no such column 等真错不许被退避掩盖）。"""
    for i in range(3):
        try:
            with session() as s:
                n = (s.query(Experiment)
                     .filter(Experiment.id == exp_id,
                             or_(Experiment.status.notin_(["running", "releasing"]),
                                 and_(Experiment.status == "running",
                                      Experiment.run_owner.is_(None))))
                     .update({Experiment.status: "running",
                              Experiment.run_owner: token,
                              Experiment.run_claimed_at: _now(),
                              Experiment.error: None,
                              Experiment.updated_at: _now()},
                             synchronize_session=False))
                s.commit()
                return n > 0    # rowcount 未知(-1) 的 DBAPI 不许误判成赢
        except OperationalError as e:
            if ("locked" not in str(e).lower() and "busy" not in str(e).lower()) \
                    or i == 2:
                raise
            time.sleep(0.5 * (i + 1))
    return False       # 不可达兜底：控制流不许靠异常穿透的偶然性


def release_run(exp_id: str) -> dict:
    """显式释放执行权（A07 运维口径，CLI --release）——**立即夺权，
    延迟让位**。

    状态机：running --release--> **releasing**（不可领取）--原 runner 收尾
    或再次 release--> failed。为什么中间态（会审四轮严重项）：release 时
    原 runner 可能还在阶段体里（数小时级）——若直接翻 failed，新 run
    立刻可领，A 还在跑同一阶段=A/B 双跑双计费 + A 的 stats 整体替换
    踩掉 B 的进度；releasing 把「夺权」与「让位」分开，新主必须等
    原主停笔。
    - CAS 条件 UPDATE（与领取同一原子语义）：只动「status 仍是观察值
      且 owner 仍是观察值」的行，观察与提交之间易主则如实报告不误杀。
    - owner 清空 + 翻 releasing：持有权校验（owner+status 双匹配）让
      活 runner 在下一道校验即停。
    - 留痕：释放是打掉别人长跑的生产操作，error 必须可持久化记录
      （四轮一般项），幂等性由「只对 running/releasing 生效」保证——
      非 running 态的重复 release 是无操作，不会追加文本。
    - 二次 release 打到 releasing（原 runner 死透、没人收尾）：翻 failed，
      卡死恢复的最后一格。"""
    with session() as s:
        exp = _exp(s, exp_id)
        if exp.status not in ("running", "releasing"):
            return {"released": False, "status": exp.status,
                    "note": "不在 running/releasing 态，无需释放"}
        observed_status, observed_owner = exp.status, exp.run_owner
    with session() as s:
        # 五轮：留痕折进 CAS 同一条 UPDATE——第三段 session 的读改写在
        # 「原 runner 火速让位 → 新主领取清空 error」与留痕提交之间有
        # 张冠李戴窗口（审计文本落到新主的行上）。owner 只留前缀：token
        # 是写权限凭据，全串落用户可见字段=给未来的带凭据接口留劫持面。
        owner_ref = (observed_owner or "?")
        owner_ref = owner_ref[:12] + ("…" if len(owner_ref) > 12 else "")
        mark = f"执行权被显式释放（原 owner={owner_ref}, at={_now()}）"
        n = (s.query(Experiment)
             .filter(Experiment.id == exp_id,
                     Experiment.status == observed_status,
                     Experiment.run_owner == observed_owner)
             .update({Experiment.status: "releasing" if observed_status == "running"
                      else "failed",
                      Experiment.run_owner: None,
                      Experiment.error: case(
                          (Experiment.error.is_(None), mark),
                          else_=Experiment.error + "；" + mark),
                      Experiment.updated_at: _now()},
                     synchronize_session=False))
        s.commit()
    if not n:      # 观察与提交之间状态已变——如实报告，不误杀
        with session() as s:
            exp = _exp(s, exp_id)
            return {"released": False, "status": exp.status,
                    "run_owner": exp.run_owner,
                    "note": "观察期间状态已变，本次未释放"}
    return {"released": True, "status": "releasing" if observed_status == "running"
            else "failed", "run_owner": observed_owner}


def list_stuck() -> list[dict]:
    """列出卡在 running/releasing 的实验（A07 运维口径）——存量对账与
    卡死排查的发现手段（--release 需要先知道是谁卡了）。releasing 是
    「已被释放、等原 runner 让位」的形状：原 runner 死透时需二次
    release 兜底，所以必须可见。"""
    with session() as s:
        rows = (s.query(Experiment)
                .filter(Experiment.status.in_(["running", "releasing"]))
                .order_by(Experiment.updated_at.desc()).all())
        return [{"id": r.id, "name": r.name, "status": r.status,
                 "run_owner": r.run_owner,
                 "run_claimed_at": r.run_claimed_at,
                 "updated_at": r.updated_at} for r in rows]


def _report_already_running(exp_id: str, note: str) -> None:
    """后台线程竞争输家的回显（四轮：独立成函数才测得了「线程连 note
    都不看」的假象）。"""
    print(f"[engine] {exp_id}: {note}", flush=True)


def run(exp_id: str, stages=None, *, force: bool = False, token: str | None = None) -> dict:
    """主驱动：按状态机顺序执行 --stages 子集，返回 stats["engine"] 块。

    - **A07 执行权**：开头原子领取（claim_run）；竞争失败方直接返回
      already_running，绝不触碰阶段体——阶段执行 2 次=重复生成重复计费。
      每个阶段提交前校验持有权（run_owner 仍是自己的 token）：中途失去
      执行权的 runner 立即停止提交——它手里的结果可能已过期。
    - token：API 侧在请求里**代领**后传入（响应才能如实回 409/started，
      不发「已启动」的假响应）；自领路径（CLI/直调）不传即自动领取。
      冻结/领取/预领校验都在 try 内：**领取之后任何一步抛错，finally
      都会把自己的行收尾掉**（五轮：API 领取后 engine 抛错会把行卡在
      running+token——那种行谁也领不动，只能靠二次 release 救）。
    - 阶段级幂等：stats 里 status=done 的阶段直接跳过（force=True 可强制重跑，
      产品级幂等保证不重复计数）。
    - 失败：_execute 已把失败落库并上抛，这里 finally 只负责把还停在
      running 的实验状态收尾——且仅当持有权还在自己手里（别人的 run
      不许被我收尾）；被 release 的（releasing）由这里翻 failed——
      释放→让位的最后一格（四轮：releasing 不可领取，新主必须等停笔）。
    - status 语义与旧管线一致：done = 本趟请求的阶段全部成功；精确进度看 /stages。
    """
    names = _resolve(stages)
    clean = False       # 领取与校验全过、阶段全部走完才置 True
    held = False        # 本 runner 是否真正持有过执行权——只有持过权的，
                        # finally 才有资格翻 releasing（五轮：竞争输家的
                        # finally 也会路过 elif，不许替别人让位）
    try:
        with session() as s:
            exp = _exp(s, exp_id)
            if (exp.config or {}).get("frozen"):
                # API 侧已在领取前拒（五轮）；自领路径到这里=还没领，
                # finally 的持有权校验天然不碰别人的行
                raise EngineError(f"{exp_id} 已冻结（Phase 1 定标实验），禁止重跑；复现在新实验进行")
        if token is None:
            token = new_id("RUN")
            if not claim_run(exp_id, token):
                # 输家不再回读 owner（会审三轮：赢家可能已跑完，回显陈旧/None
                # 只会误导）——note 说清是领取竞争失败即可。finally 的持有权
                # 校验对输家天然 no-op（行不是我们的）。
                return {"status": "already_running",
                        "stages": {},
                        "note": "执行权已被领取（原子 UPDATE 竞争失败）——阶段体不重复执行；"
                                "卡死排查用 engine.list_stuck() / CLI --list-stuck"}
            held = True
        else:
            # 外部（API）已代领：校验凭据确属自己，防错传/过期
            with session() as s:
                exp = _exp(s, exp_id)
                if exp.run_owner != token or exp.status != "running":
                    raise EngineError("预领凭据无效（owner/status 不匹配）——拒绝执行")
            held = True
        clean = True
        for name in names:
            if _stage_record(exp_id, name).get("status") == "done" and not force:
                continue                # 从 stats 续跑：已 done 的阶段不再执行
            try:
                with session() as s:
                    exp = _exp(s, exp_id)
                    if exp.run_owner != token or exp.status != "running":
                        # A07 三轮：持有权校验必须 owner+status **双匹配**——
                        # release 立即夺权（owner 清空+status 翻 failed），活
                        # runner 误被打中时在这里停下，不许继续烧钱；被夺权
                        # runner 的结果可能已过期，停止提交，绝不覆盖新主进度
                        raise EngineError(
                            f"失去运行持有权（owner={exp.run_owner}≠{token} 或 "
                            f"status={exp.status}≠running）——停止提交阶段结果")
                    miss = _prereq_missing(s, exp.id, name)
                    if miss:
                        raise EngineError(f"阶段 {name} 前置未满足：{miss}")
                    _execute(s, exp, name)
            except Exception:
                clean = False           # 任何中断都不许把 running 收尾成 done
                raise
    finally:
        with session() as s:
            exp = _exp(s, exp_id)
            if exp.status == "running" and exp.run_owner == token:
                exp.status = "done" if clean else "failed"
                if not clean:
                    exp.error = "引擎中断（各阶段 error 见 stats.engine.stages）"
                exp.updated_at = _now()
                s.commit()
            elif exp.status == "releasing" and held:
                # 四轮：被 release 的原 runner 在这里让位——releasing 不可
                # 领取，翻 failed 后新主才可领；本分支是「释放→让位」的
                # 最后一格（原 runner 死透则由二次 release 兜底）。
                # 五轮 held 门：竞争输家没持过权，不许替别人让位。
                exp.status = "failed"
                exp.updated_at = _now()
                s.commit()

    with session() as s:
        exp = _exp(s, exp_id)
        return dict((exp.stats or {}).get("engine") or {})


def run_experiment_background(exp_id: str, stages=None,
                              token: str | None = None) -> threading.Thread:
    """给 API 用：后台线程跑引擎。失败已落库（stats + status），线程异常无需上报。

    token：API 端点在请求里已代领时传入（响应才能如实 409/started）；
    竞争输家（无 token 自领路径）在服务端日志回显 note——「线程连
    note 都不看」= 已启动的假象（会审三轮点名）。"""
    def _bg():
        result = run(exp_id, stages, token=token)
        if result.get("status") == "already_running":
            _report_already_running(exp_id, result.get("note") or "")
    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return t
