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

from sqlalchemy import or_
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
    checked = ok_true = ok_false = 0
    for x in rows:
        d = _as_dict(x.integrity)
        if "src_ok" in d:
            checked += 1
            ok_true += bool(d["src_ok"])
            ok_false += not d["src_ok"]
    return {"checked": checked, "src_ok": ok_true, "src_bad": ok_false}


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
    sc._stat.update(ok=0, failed=0, skip=0, bad=0)   # 模块全局计数是跨趟累计的，先归零
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


def _claim_run(exp_id: str, token: str) -> bool:
    """A07：**原子领取执行权**——条件 UPDATE，只有把 status 从非 running
    翻成 running 的那一个赢；owner/lease（领取凭据）随领取一次写入。

    为什么必须是领取事务：「检查 running 再启动」不是原子操作——API 先查
    后启线程、CLI 与 API 同时启动、双请求竞争，都读到「阶段尚未完成」
    （审查实测：屏障控制顺序后，阶段体执行 2 次、两边都返回成功；真实
    阶段则重复生成与计费）。UI 禁用按钮、单纯读 status，都不能替代本事务。
    无自动 TTL 接管：合法实验一跑数小时（PROD 290 变体），固定 TTL 会在
    长跑中途被误抢——A07 换姿势重演。卡死恢复走 release_run 显式释放。"""
    with session() as s:
        n = (s.query(Experiment)
             .filter(Experiment.id == exp_id,
                     Experiment.status != "running")
             .update({Experiment.status: "running",
                      Experiment.run_owner: token,
                      Experiment.run_claimed_at: _now()},
                     synchronize_session=False))
        s.commit()
        return bool(n)


def release_run(exp_id: str) -> dict:
    """显式释放卡死的执行权（A07 运维口径，CLI --release）。

    只对 running 态生效：release 后 status=failed，下次 run 可重新领取。"""
    with session() as s:
        exp = _exp(s, exp_id)
        if exp.status != "running":
            return {"released": False, "status": exp.status,
                    "note": "不在 running 态，无需释放"}
        owner = exp.run_owner
        exp.status = "failed"
        exp.error = ((exp.error or "") + "；执行权被显式释放（release_run）").lstrip("；")
        exp.updated_at = _now()
        s.commit()
        return {"released": True, "run_owner": owner}


def run(exp_id: str, stages=None, *, force: bool = False) -> dict:
    """主驱动：按状态机顺序执行 --stages 子集，返回 stats["engine"] 块。

    - **A07 执行权**：开头原子领取（_claim_run）；竞争失败方直接返回
      already_running，绝不触碰阶段体——阶段执行 2 次=重复生成重复计费。
      每个阶段提交前校验持有权（run_owner 仍是自己的 token）：中途失去
      执行权的 runner 立即停止提交——它手里的结果可能已过期。
    - 阶段级幂等：stats 里 status=done 的阶段直接跳过（force=True 可强制重跑，
      产品级幂等保证不重复计数）。
    - 失败：_execute 已把失败落库并上抛，这里 finally 只负责把还停在 running 的
      实验状态收尾——且仅当持有权还在自己手里（别人的 run 不许被我收尾）。
    - status 语义与旧管线一致：done = 本趟请求的阶段全部成功；精确进度看 /stages。
    """
    names = _resolve(stages)
    with session() as s:
        exp = _exp(s, exp_id)
        if (exp.config or {}).get("frozen"):
            raise EngineError(f"{exp_id} 已冻结（Phase 1 定标实验），禁止重跑；复现在新实验进行")
    token = new_id("RUN")
    if not _claim_run(exp_id, token):
        with session() as s:
            exp = _exp(s, exp_id)
            return {"status": "already_running", "run_owner": exp.run_owner,
                    "stages": {},
                    "note": "执行权已被领取（原子 UPDATE 竞争失败）——阶段体不重复执行"}

    clean = True
    try:
        for name in names:
            if _stage_record(exp_id, name).get("status") == "done" and not force:
                continue                # 从 stats 续跑：已 done 的阶段不再执行
            try:
                with session() as s:
                    exp = _exp(s, exp_id)
                    if exp.run_owner != token:
                        # A07：持有权被显式 release 后被别人领走——本 runner
                        # 的结果可能已过期，停止提交，绝不覆盖新主的进度
                        raise EngineError(f"失去运行持有权（owner={exp.run_owner}≠{token}）"
                                          "——停止提交阶段结果")
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

    with session() as s:
        exp = _exp(s, exp_id)
        return dict((exp.stats or {}).get("engine") or {})


def run_experiment_background(exp_id: str, stages=None) -> threading.Thread:
    """给 API 用：后台线程跑引擎。失败已落库（stats + status），线程异常无需上报。"""
    t = threading.Thread(target=run, args=(exp_id, stages), daemon=True)
    t.start()
    return t
