"""实验编排：Calibration 全流程分 stage，每 stage 是一条 Job，可中断续跑。

stage 顺序：
  extract_frames → leakage_static → extract_propositions → reconstruct
  → adversarial_leakage → residual_det → residual_sem → judges
  → review_queue → report

粒度语义：
- frame 失败只影响该 segment 的该粒度下游
- 全部 stage 都学会"跳过自己已做过的产物"（幂等），重跑不会重复花钱
"""
from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

from . import config, jobs, limits, residual_sem
from .db import session
from .frames_prompts import EXTRACT_V2, PROMPT_VERSION
from .frames_schema import MODEL_BY_GRAN, frame_prompt_text
from .gateway import bind_experiment, chat, is_serial_model
from .ids import new_exp_id, new_id
from .judges import (ADVERSARIAL_PROMPT_VERSION, NATURALNESS_PROMPT_VERSION,
                     SEMANTIC_PROMPT_VERSION, judge_adversarial, judge_naturalness,
                     judge_semantic)
from .leakage import (ADVERSARIAL_PROMPT_VERSION as ADV_LEAK_VERSION,
                      RarePhraseIndex, adversarial_layer, char_layer, composite_score,
                      word_layer)
from .models import (Candidate, Experiment, Frame, JudgeRun, LeakageScore,
                     Proposition, ResidualDet, ResidualSem, Segment, LlmCall)
from .prompt_render import render
from .reconstruct import REPAIR_PROMPT, build_reconstruct_user, RECON_PROMPT_VERSION
from .residual_sem import (PROPOSITIONS_PROMPT_VERSION, extract_propositions,
                           parse_llm_json, semantic_residual)
from .metrics_det import det_residual
from .review import build_review_queue

STAGES = [
    "extract_frames",
    "leakage_static",
    "extract_propositions",
    "reconstruct",
    "adversarial_leakage",
    "residual_det",
    "residual_sem",
    "judges",
    "review_queue",
    "report",
]

DEFAULT_CONFIG = {
    "n_segments": 24,
    "granularities": ["S", "M", "L"],
    "extractors": None,              # None → config.EXTRACTOR_MODELS
    "recon_models": None,            # None → config.DEFAULT_RECON_MODELS
    "temperatures": [0.5, 0.9],
    "samples_per_pair": 2,           # 每 model×temp 采样数 → 默认 4模×2温×2样=16/frame
    "judge_models": None,            # None → 复用 recon_models[:1]
    "judge_human_naturalness": True, # Human 也做盲评（供 upset 检测+基线）
    "adversarial_k": 8,
    "leak_thresholds": {"char6": 0.30, "word3": 0.35, "rare": 0.25, "adversarial": 0.65},
    "segment_seed": 20260911,
    "concurrency": 4,
    "work_ids": None,                # None → 全语料采样；指定后只从这些 Work 抽血
    "writer_input_mode": "frame_only",  # 实验变量：frame_only|context_only|context_plus_frame（v2 定默认）
    "context_ladder": ["prev1", "prev3", "scene_context", "compressed_long"],
}


# ────────────────────────────────────────────────────────────
#
# 匿名标号：X#### 必须实验内唯一。旧实现用"stage 开始时的 count 快照 + 每线程局部
# made"相加，并发下不同 frame 的线程必然撞号。改为进程内锁 + DB 当前计数做种子；
# 单进程单 stage 前提下严格唯一，中断重跑后也从 DB 续号。
_ANON_LOCK = threading.Lock()


def _anon_allocator(s: Session, exp_id: str):
    cell = {"n": s.query(Candidate).filter_by(experiment_id=exp_id).count()}

    def next_label() -> str:
        with _ANON_LOCK:
            cell["n"] += 1
            return f"X{cell['n']:04d}"

    return next_label


def create_experiment(s: Session, overrides: dict | None = None) -> Experiment:
    cfg = {**DEFAULT_CONFIG, **(overrides or {})}
    # 基准隔离：role='benchmark' 的段永远不进训练采样（near_dup.train_sampling_pool）
    from .near_dup import train_sampling_pool
    q = train_sampling_pool(s, cfg.get("work_ids") or None,
                            seg_version=cfg.get("seg_version"),
                            eligible_only=bool(cfg.get("eligible_only")))
    segs = sorted(q, key=lambda x: x.id)
    if not segs:
        raise ValueError("没有语料：先导入 corpus（inbox 或 distiller），或检查 work_ids 过滤")
    rng = random.Random(cfg["segment_seed"])
    selected = rng.sample(segs, min(cfg["n_segments"], len(segs)))
    cfg["segment_ids"] = [x.id for x in selected]
    cfg["extractors"] = cfg["extractors"] or config.EXTRACTOR_MODELS
    cfg["recon_models"] = cfg["recon_models"] or config.DEFAULT_RECON_MODELS
    cfg["judge_models"] = cfg["judge_models"] or cfg["recon_models"][:1]

    exp = Experiment(id=new_exp_id(), name="calibration_v1", status="created", config=cfg, stats={})
    s.add(exp)
    for stage in STAGES:
        jobs.enqueue(s, f"stage:{stage}", {"experiment_id": exp.id})
    s.commit()
    return exp


def _exp(s: Session, exp_id: str) -> Experiment:
    e = s.get(Experiment, exp_id)
    if not e:
        raise KeyError(f"experiment 不存在: {exp_id}")
    return e


def _set_stage(s: Session, exp: Experiment, name: str, **info) -> None:
    st = dict(exp.stats or {})
    stages = dict(st.get("stages") or {})
    stages[name] = info
    st["stages"] = stages
    st["current_stage"] = name
    exp.stats = st
    s.commit()


def _segments(s: Session, exp: Experiment) -> list[Segment]:
    ids = exp.config["segment_ids"]
    return s.query(Segment).filter(Segment.id.in_(ids)).all()


def _pool_plan(exp: Experiment, models: list[str] | None = None) -> dict:
    """算出本 stage 线程池该开几个 worker，并把"为什么不是配置值"显式记进返回值。

    两条口径，都不静默（2026-09-25 实验并发上限收口）：

    1. **上限** workers ≤ `limits.MAX_CONCURRENCY`（与 HTTP 入参闸同一个真源）。
       越界**按上限截断**并在 `concurrency_clamped_from` 里记下配置原值，
       不抛异常：这里已经在 stage/job 执行中途，抛错会把整条 stage 打成
       failed、白白丢掉已花掉的调用；旁路脚本（scale_corpus / run_calibration）
       写的 config 又是历史既有实验，硬失败等于让老实验永远跑不动。
       HTTP 入参侧（`api.ExperimentIn`）仍是越界 422 直拒 —— 入参契约与运行
       护栏口径不同是刻意的，理由见 docs/实验并发上限_20260925.md。

    2. **串行纪律** 本 stage 用到的模型里只要有 `gateway.is_serial_model()` 为真
       （`agy/` `qoder/` `wb/` `zcode/` = 本机 CLI 单账号共享额度，
       gateway.py:133-143 明写「顺序调用，不要并发」）→ workers 强制 1，
       并记 `serial_forced` + 命中的模型名。判定一律复用 gateway 的函数，
       不在这里自己比字符串前缀（口径源只有一处）。
       混合池（并发组+串行组）按**最保守**处理：整段串行。分两组分别跑会改动
       stage 的执行结构，且串行组慢模型本来就该单独排班，不在本次收口范围。
    """
    requested = max(1, int(exp.config.get("concurrency", 4)))
    workers, clamped_from = requested, None
    if requested > limits.MAX_CONCURRENCY:
        workers, clamped_from = limits.MAX_CONCURRENCY, requested
    hits = [m for m in (models or []) if is_serial_model(m)]
    if hits:
        workers = 1
    return {"requested": requested, "workers": workers,
            "concurrency_clamped_from": clamped_from,
            "serial_forced": bool(hits), "serial_models": hits}


class _PoolRun(list):
    """`_pool_map` 的返回：item 结果列表（照旧可迭代/求和）+ 本次线程池口径 `plan`。"""


def _pool_map(exp: Experiment, items, fn, desc: str = "",
              models: list[str] | None = None) -> _PoolRun:
    """线程池执行 fn(item)；fn 自己开自己的 session。返回成功 item 数统计写在外层。

    `models` 是本 stage 实际发请求的模型列表，用于串行纪律判定（见 `_pool_plan`）；
    调用点必须传，不传等于放弃串行保护。
    """
    plan = _pool_plan(exp, models)
    if plan["serial_forced"] or plan["concurrency_clamped_from"] is not None:
        print(f"[pool] {exp.id} {desc or 'stage'} concurrency={plan['requested']} "
              f"→ workers={plan['workers']} serial_forced={plan['serial_forced']} "
              f"serial_models={plan['serial_models']} "
              f"concurrency_clamped_from={plan['concurrency_clamped_from']}",
              flush=True)
    results = _PoolRun()
    results.plan = plan
    with ThreadPoolExecutor(max_workers=plan["workers"]) as pool:
        futs = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:  # 单点失败不拖垮整个 stage
                results.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return results


# ── stage 1: 抽取 Frame（含 M/L 修复重试 + 双抽存证）────────

def stage_extract_frames(s: Session, exp: Experiment) -> None:
    segs = _segments(s, exp)
    grans = exp.config["granularities"]
    extractors = exp.config["extractors"]

    # failed 不是终态：重跑本 stage 前先清掉失败行，让幂等键只覆盖 ok/repaired —
    # 否则 timeout/解析失败的 frame 永远无法补救（审查补遗 P0-8）。
    s.query(Frame).filter_by(experiment_id=exp.id, status="failed").delete()
    s.commit()
    existing = {f.segment_id + ":" + f.granularity + ":" + f.extractor_model
                for f in s.query(Frame).filter_by(experiment_id=exp.id).all()}

    def work(seg: Segment) -> dict:
        bind_experiment(exp.id)
        with session() as ts:
            made = 0
            for gran in grans:
                for i, model in enumerate(extractors):
                    key = f"{seg.id}:{gran}:{model}"
                    if key in existing:
                        continue
                    prompt = render(EXTRACT_V2[gran], text=seg.text)
                    status, payload, raw = "ok", None, ""
                    try:
                        r = chat(model=model, system="你是语义骨架抽取器，只输出 JSON。",
                                 user=prompt, purpose=f"extract:{gran}",
                                 prompt_version=PROMPT_VERSION, temperature=0.2, max_tokens=4096)
                        raw = r.text
                        payload = parse_llm_json(raw)
                    except Exception as e:
                        status, payload = "failed", None
                        raw = f"{type(e).__name__}: {e}"
                    # 只产出了非 JSON（payload is None）也算校验失败，走同一条 repair 路径；
                    # 旧代码只在 payload 非 None 时校验，parse 失败会以无 payload 的 ok 落库。
                    _, verr = _validate(gran, payload) if payload is not None else (None, "not json")
                    if status == "ok" and verr:
                        try:
                            r2 = chat(model=model, system="只输出修正后的 JSON。",
                                      user=render(REPAIR_PROMPT, raw=raw, error=verr),
                                      purpose=f"extract:{gran}:repair",
                                      prompt_version=PROMPT_VERSION + ":repair",
                                      temperature=0.0, max_tokens=4096)
                            p2 = parse_llm_json(r2.text)
                            _, verr2 = _validate(gran, p2) if p2 else (None, "not json")
                            if verr2 is None:
                                payload, status, raw = p2, "repaired", r2.text
                            else:
                                status = "failed"
                        except Exception:
                            status = "failed"
                    ts.add(Frame(experiment_id=exp.id, segment_id=seg.id, granularity=gran,
                                 extractor_model=model, prompt_version=PROMPT_VERSION,
                                 payload=payload, raw_output=raw[:2000], status=status,
                                 is_primary=(i == 0)))
                    ts.commit()
                    made += 1
            return {"ok": True, "made": made}

    results = _pool_map(exp, segs, work, desc="extract_frames", models=extractors)
    ok_frames = s.query(Frame).filter_by(experiment_id=exp.id, status="ok").count()
    repaired = s.query(Frame).filter_by(experiment_id=exp.id, status="repaired").count()
    failed = s.query(Frame).filter_by(experiment_id=exp.id, status="failed").count()
    item_errors = sum(1 for r in results if not r.get("ok"))
    _set_stage(s, exp, "extract_frames", done=True, ok=ok_frames, repaired=repaired,
               failed=failed, item_errors=item_errors)


def _validate(gran: str, payload: dict):
    cls = MODEL_BY_GRAN.get(gran)
    if cls is None:
        return None, f"bad gran {gran}"
    try:
        cls.model_validate({**payload, "granularity": gran})
        return payload, None
    except Exception as e:
        return None, str(e)[:300]


# ── stage 2: 静态泄漏三层 ───────────────────────────────────

def stage_leakage_static(s: Session, exp: Experiment) -> None:
    frames = s.query(Frame).filter_by(experiment_id=exp.id).filter(Frame.status != "failed").all()
    seg_map = {x.id: x for x in _segments(s, exp)}
    texts = [seg.text for seg in seg_map.values()]
    rpi = RarePhraseIndex(texts)

    existing = {x.frame_id + ":" + x.layer for x in s.query(LeakageScore).all()}
    added = 0
    for f in frames:
        seg = seg_map.get(f.segment_id)
        if not seg or not f.payload:
            continue
        repr_text = frame_prompt_text(f.payload)
        for layer_name, scorer in (("char6", char_layer), ("word3", word_layer)):
            if f"{f.id}:{layer_name}" in existing:
                continue
            out = scorer(repr_text, seg.text)
            s.add(LeakageScore(frame_id=f.id, layer=layer_name, score=out["score"], detail=out))
            added += 1
        if f"{f.id}:rare" not in existing:
            out = rpi.weighted_containment(repr_text, seg.text)
            s.add(LeakageScore(frame_id=f.id, layer="rare", score=out["score"], detail=out))
            added += 1
    s.commit()
    _set_stage(s, exp, "leakage_static", done=True, scored=added)


# ── stage 3: 命题分解 ───────────────────────────────────────

def stage_extract_propositions(s: Session, exp: Experiment) -> None:
    segs = _segments(s, exp)
    # 版本感知复用：prompt 升级后旧命题不得静默沿用。
    # 失败行（propositions 为空）不算"已做"，否则一次超时永久丢段。
    existing = {p.segment_id for p in s.query(Proposition)
                .filter(Proposition.prompt_version == PROPOSITIONS_PROMPT_VERSION).all()
                if p.propositions}

    def work(seg: Segment) -> dict:
        bind_experiment(exp.id)
        if seg.id in existing:
            return {"ok": True, "skip": True}
        res = extract_propositions(seg.text)
        with session() as ts:
            ts.add(Proposition(segment_id=seg.id, model=config.STRONG_MODEL,
                               prompt_version=PROPOSITIONS_PROMPT_VERSION,
                               propositions=res.get("propositions", [])))
            ts.commit()
        return {"ok": res["status"] == "ok"}

    results = _pool_map(exp, segs, work, desc="extract_propositions",
                        models=[residual_sem.PROPOSITION_MODEL])
    n = s.query(Proposition).count()
    _set_stage(s, exp, "extract_propositions", done=True, total=n,
               item_errors=sum(1 for r in results if not r.get("ok")))


# ── stage 4: 多模型重建 ─────────────────────────────────────

def stage_reconstruct(s: Session, exp: Experiment) -> None:
    frames = s.query(Frame).filter_by(experiment_id=exp.id, is_primary=True)\
                           .filter(Frame.status != "failed").all()
    models, temps = exp.config["recon_models"], exp.config["temperatures"]
    n_samples = exp.config["samples_per_pair"]
    next_label = _anon_allocator(s, exp.id)

    # 幂等键含 prompt_version：RECON prompt 改了就让旧 candidate 作废重出，而不是静默复用
    existing = {(c.frame_id, c.model, c.temperature, c.seed, c.prompt_version) for c in
                s.query(Candidate).filter_by(experiment_id=exp.id)}

    def work(frame: Frame) -> dict:
        bind_experiment(exp.id)
        made = 0
        with session() as ts:
            for mi, model in enumerate(models):
                for ti, temp in enumerate(temps):
                    for sample_i in range(n_samples):
                        seed = (mi * 1000 + ti * 100 + sample_i) * 7919 + 17
                        if (frame.id, model, temp, seed, RECON_PROMPT_VERSION) in existing:
                            continue
                        user = build_reconstruct_user(frame.payload)
                        try:
                            r = chat(model=model,
                                     system="你是中文小说写作者，只输出正文。",
                                     user=user, purpose=f"reconstruct:{frame.granularity}",
                                     prompt_version=RECON_PROMPT_VERSION,
                                     temperature=temp, max_tokens=3000, seed=seed)
                            text, tok_in, tok_out, lat, status, err = (
                                r.text.strip(), r.tokens_in, r.tokens_out, r.latency_ms, "ok", None)
                        except Exception as e:
                            text, tok_in, tok_out, lat, status, err = "", 0, 0, 0, "failed", str(e)[:300]
                        ts.add(Candidate(
                            experiment_id=exp.id, frame_id=frame.id, segment_id=frame.segment_id,
                            anon_label=next_label(), model=model, temperature=temp,
                            seed=seed, prompt_version=RECON_PROMPT_VERSION, text=text,
                            tokens_in=tok_in, tokens_out=tok_out, latency_ms=lat,
                            status=status, error=err,
                        ))
                        ts.commit()
                        made += 1
        return {"ok": True, "made": made}

    results = _pool_map(exp, frames, work, desc="reconstruct",
                        models=exp.config["recon_models"])
    n_ok = s.query(Candidate).filter_by(experiment_id=exp.id, status="ok").count()
    n_fail = s.query(Candidate).filter_by(experiment_id=exp.id, status="failed").count()
    _set_stage(s, exp, "reconstruct", done=True, ok=n_ok, failed=n_fail,
               item_errors=sum(1 for r in results if not r.get("ok")))


# ── stage 5: 对抗还原泄漏 ───────────────────────────────────

def stage_adversarial_leakage(s: Session, exp: Experiment) -> None:
    frames = s.query(Frame).filter_by(experiment_id=exp.id, is_primary=True)\
                           .filter(Frame.status != "failed").all()
    seg_map = {x.id: x.text for x in _segments(s, exp)}
    k = exp.config["adversarial_k"]
    # 版本感知幂等：prompt 升版后旧分数作废重跑；status=failed 的不算"已做过"
    existing = {x.frame_id for x in s.query(LeakageScore).filter_by(layer="adversarial").all()
                if (x.detail or {}).get("prompt_version") == ADV_LEAK_VERSION
                and (x.detail or {}).get("status") == "ok"}

    def work(frame: Frame) -> dict:
        bind_experiment(exp.id)
        if frame.id in existing:
            return {"ok": True, "skip": True}
        src = seg_map.get(frame.segment_id, "")
        out = adversarial_layer(frame_prompt_text(frame.payload), src, k=k)
        out["prompt_version"] = ADV_LEAK_VERSION
        with session() as ts:
            ts.add(LeakageScore(frame_id=frame.id, layer="adversarial",
                                score=out.get("score", 0.0), detail=out))
            ts.commit()
        return {"ok": out.get("status") == "ok"}

    results = _pool_map(exp, frames, work, desc="adversarial_leakage",
                        models=[config.STRONG_MODEL])
    _set_stage(s, exp, "adversarial_leakage", done=True,
               scored=len(frames) if config.LLM_MODE != "mock" else "mock-skipped",
               item_errors=sum(1 for r in results if not r.get("ok")))


# ── stage 6: 确定性残差（无 LLM，直接算）────────────────────

def stage_residual_det(s: Session, exp: Experiment) -> None:
    cands = s.query(Candidate).filter_by(experiment_id=exp.id, status="ok").all()
    seg_text = {x.id: x.text for x in _segments(s, exp)}
    existing = {r.candidate_id for r in s.query(ResidualDet).all()}
    added = 0
    for c in cands:
        if c.id in existing:
            continue
        human = seg_text.get(c.segment_id, "")
        if not human or not c.text:
            continue
        out = det_residual(human, c.text)
        s.add(ResidualDet(candidate_id=c.id, metrics=out["candidate"], deltas=out["delta"]))
        added += 1
    s.commit()
    _set_stage(s, exp, "residual_det", done=True, added=added)


# ── stage 7: 语义残差 ───────────────────────────────────────

def stage_residual_sem(s: Session, exp: Experiment) -> None:
    cands = s.query(Candidate).filter_by(experiment_id=exp.id, status="ok").all()
    frame_map = {f.id: f for f in s.query(Frame).filter_by(experiment_id=exp.id).all()}
    seg_text = {x.id: x.text for x in _segments(s, exp)}
    existing = {r.candidate_id for r in s.query(ResidualSem).all() if r.status == "ok"}

    def work(c: Candidate) -> dict:
        bind_experiment(exp.id)
        if c.id in existing:
            return {"ok": True, "skip": True}
        frame = frame_map.get(c.frame_id)
        human = seg_text.get(c.segment_id, "")
        if not frame or not human:
            return {"ok": False, "error": "missing context"}
        res = semantic_residual(frame_json=frame_prompt_text(frame.payload),
                                human_text=human, candidate_text=c.text)
        with session() as ts:
            ts.add(ResidualSem(candidate_id=c.id, model=config.STRONG_MODEL,
                               prompt_version="sem_residual_v1",
                               payload=res.get("payload"), raw_output=res.get("raw"),
                               status=res["status"]))
            ts.commit()
        return {"ok": res["status"] == "ok"}

    _pool_map(exp, cands, work, desc="residual_sem",
              models=[residual_sem.PROPOSITION_MODEL])
    n = s.query(ResidualSem).count()
    _set_stage(s, exp, "residual_sem", done=True, total=n)


# ── stage 8: 三 Judge ───────────────────────────────────────

def stage_judges(s: Session, exp: Experiment) -> None:
    cands = s.query(Candidate).filter_by(experiment_id=exp.id, status="ok").all()
    frame_map = {f.id: f for f in s.query(Frame).filter_by(experiment_id=exp.id).all()}
    seg_map = {x.id: x for x in _segments(s, exp)}
    prop_map = {p.segment_id: p.propositions for p in s.query(Proposition)
                .filter(Proposition.prompt_version == PROPOSITIONS_PROMPT_VERSION).all()}
    judge_models = exp.config["judge_models"]

    _PV = {"semantic": SEMANTIC_PROMPT_VERSION,
           "naturalness": NATURALNESS_PROMPT_VERSION,
           "adversarial": ADVERSARIAL_PROMPT_VERSION}
    # 幂等键含 prompt_version：judge prompt 改版后旧判定作废重判；
    # status=failed 的判定不算"已做"，留着重跑时补。
    done_keys = {(j.subject_type, j.subject_id, j.judge_kind, j.model, j.prompt_version)
                 for j in s.query(JudgeRun).filter_by(experiment_id=exp.id).all()
                 if j.status == "ok"}

    # 先对 Human 原文做盲评 naturalness（upset 检测要用）
    if exp.config.get("judge_human_naturalness"):
        def work_h(seg: Segment) -> dict:
            bind_experiment(exp.id)
            for model in judge_models:
                if ("human_segment", seg.id, "naturalness", model,
                        NATURALNESS_PROMPT_VERSION) in done_keys:
                    continue
                out = judge_naturalness(text=seg.text, model=model)
                with session() as ts:
                    ts.add(JudgeRun(experiment_id=exp.id, subject_type="human_segment",
                                    subject_id=seg.id, judge_kind="naturalness", model=model,
                                    prompt_version=NATURALNESS_PROMPT_VERSION,
                                    verdict=out.get("payload"), confidence=out.get("confidence"),
                                    abstain=out.get("abstain", False), status=out["status"]))
                    ts.commit()
            return {"ok": True}
        _pool_map(exp, list(seg_map.values()), work_h, desc="judges:human",
                  models=judge_models)

    def work(c: Candidate) -> dict:
        bind_experiment(exp.id)
        seg = seg_map.get(c.segment_id)
        frame = frame_map.get(c.frame_id)
        if not seg or not frame:
            return {"ok": False, "error": "missing context"}
        props = prop_map.get(seg.id, [])
        # A/B 随机位必须逐候选独立播种：共享 RNG 的取数顺序依赖线程调度，
        # 同一实验配置重跑会得到不同排列，人肉核对无法复现。
        rng = random.Random(f"{exp.config['segment_seed']}:{c.id}")
        made = 0
        for model in judge_models:
            calls = [
                ("semantic", lambda: judge_semantic(frame_json=frame_prompt_text(frame.payload),
                                                    propositions=props, candidate_text=c.text,
                                                    model=model)),
                ("naturalness", lambda: judge_naturalness(text=c.text, model=model)),
                ("adversarial", lambda: judge_adversarial(human_text=seg.text,
                                                          candidate_text=c.text,
                                                          model=model, rng=rng)),
            ]
            for kind, fn in calls:
                if ("candidate", c.id, kind, model, _PV[kind]) in done_keys:
                    continue
                out = fn()
                verdict = out.get("payload")
                if verdict is not None and kind == "adversarial":
                    # human_position / guess_hit_ai 只进库做汇总，prompt 中永远不出现
                    verdict = {**verdict,
                               "human_position": out.get("human_position"),
                               "guess_hit_ai": out.get("guess_hit_ai")}
                with session() as ts:
                    ts.add(JudgeRun(
                        experiment_id=exp.id, subject_type="candidate", subject_id=c.id,
                        judge_kind=kind, model=model, prompt_version=_PV[kind],
                        verdict=verdict,
                        confidence=out.get("confidence"), abstain=out.get("abstain", False),
                        status=out["status"]))
                    ts.commit()
                made += 1
        return {"ok": True, "made": made}

    results = _pool_map(exp, cands, work, desc="judges", models=judge_models)
    n = s.query(JudgeRun).filter_by(experiment_id=exp.id).count()
    _set_stage(s, exp, "judges", done=True, total=n,
               item_errors=sum(1 for r in results if not r.get("ok")))


# ── stage 9/10 ─────────────────────────────────────────────

def stage_review_queue(s: Session, exp: Experiment) -> None:
    n = build_review_queue(s, exp.id)
    _set_stage(s, exp, "review_queue", done=True, enqueued=n)


def stage_report(s: Session, exp: Experiment) -> None:
    from . import report as report_mod
    out = report_mod.build_calibration_report(s, exp.id)
    _set_stage(s, exp, "report", done=True, **out)


# ── 主驱动 ──────────────────────────────────────────────────

STAGE_FUNCS = {
    "extract_frames": stage_extract_frames,
    "leakage_static": stage_leakage_static,
    "extract_propositions": stage_extract_propositions,
    "reconstruct": stage_reconstruct,
    "adversarial_leakage": stage_adversarial_leakage,
    "residual_det": stage_residual_det,
    "residual_sem": stage_residual_sem,
    "judges": stage_judges,
    "review_queue": stage_review_queue,
    "report": stage_report,
}


def run_experiment(exp_id: str) -> Experiment:
    with session() as s:
        exp = _exp(s, exp_id)
        if (exp.config or {}).get("frozen"):
            raise ValueError(f"{exp_id} 已冻结（Phase 1 定标实验），禁止重跑；复现在新实验进行")
        exp.status = "running"
        s.commit()

    # 上次进程中断遗留的 running job 永远回不到 pending（没有 reaper），
    # 实验是单进程串行跑的，重启时先把本实验的僵尸 running 复位。
    with session() as s:
        stale = s.query(jobs.Job).filter(
            jobs.Job.kind.like("stage:%"), jobs.Job.status == "running").all()
        for j in stale:
            if j.payload.get("experiment_id") == exp_id:
                j.status = "pending"
        s.commit()

    def handler(job):
        kind = job.kind
        if not kind.startswith("stage:"):
            raise ValueError(f"未知 job kind: {kind}")
        name = kind.split(":", 1)[1]
        if job.payload.get("experiment_id") != exp_id:
            return
        fn = STAGE_FUNCS[name]
        with session() as s:
            exp = _exp(s, exp_id)
            fn(s, exp)

    stats = jobs.pump(handler, limit=100, experiment_id=exp_id)
    with session() as s:
        exp = _exp(s, exp_id)
        pends = s.query(jobs.Job).filter(
            jobs.Job.kind.like("stage:%"),
            jobs.Job.payload["experiment_id"].as_string() == exp_id,
            jobs.Job.status.in_(["pending", "retry", "running"]),
        ).count()
        exp.status = "done" if stats["failed"] == 0 and pends == 0 else "failed"
        exp.stats = {**(exp.stats or {}), "pump": stats}
        s.commit()
        return exp


def run_experiment_background(exp_id: str) -> threading.Thread:
    t = threading.Thread(target=run_experiment, args=(exp_id,), daemon=True)
    t.start()
    return t
