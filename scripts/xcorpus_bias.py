"""跨语料 Judge 偏差探针（2026-09-14）。

问题：preference judge 在琼明（对照语料，"糙而连续"）上把候选挑走 68–71%。
这到底是 ① 评委有家养腔偏好，还是 ② 该语料人类文本确实弱？

做法：**不需要用户盲评**——直接测"评委挑 candidate 的比例"这个响应偏差指标，
在四个不同定位的语料上各跑同一批（human 段 vs 其候选，带上文，v3 口径），横向比较。

预期读数：
- 若各语料候选被挑率都 ≈0.7 → 解释①（评委恒定偏好 AI 文本，与人类文本质量无关）
- 若质量锚（将夜）显著低于对照（琼明） → 解释②（评委是可用的，只是琼明不配当质量锚）

这是"两种解释"判定实验的**无用户版**：拿不到 agreement（那需要用户判定），
但拿得到响应偏差，而响应偏差正是两个解释分歧最大的观测量。

用法：
    python scripts/xcorpus_bias.py --per-corpus 20
    python scripts/xcorpus_bias.py --per-corpus 20 --dry-run
"""
from __future__ import annotations

import argparse
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config, db
from app.context_ablation import scene_context
from app.judges import PREFERENCE_PROMPT_VERSION, judge_preference
from app.models import Candidate, JudgeRun, Segment, Work
import preflight_models as pf

# 语料 → 实验（各语料 v2 段 + S/M/L 双抽取器产物）
CORPORA = [
    ("琼明（对照·糙而连续）", "EXP-0914-C812"),
    ("将夜（质量锚·猫腻）", "EXP-0914-FF7B"),
    ("凡人（叙事效率锚·忘语）", "EXP-0914-EE18"),
    ("斗罗（目标风格锚·三少）", "EXP-0914-6DD3"),
]
JUDGE_KIND = "preference_xcorpus"      # 独立 kind，不污染正式 preference 记录
DEFAULT_JUDGES = "moonshotai/kimi-k3," + config.DEFAULT_LLM_MODEL

_lock = threading.Lock()
# first_error：本轮首条失败**原文**。judge_runs 只存 status、不存错误串，
# 而"failed=全部"和"池子没货"在计数上长得一模一样（2026-09-20 P0 事故）→ 就地留一份。
_counter = {"ok": 0, "failed": 0, "skip": 0, "first_error": ""}


def _sample_pairs(s, exp_id: str, n: int, seed: int) -> list[dict]:
    """每个段只取一个候选（优先 L 粒度），带上文。"""
    cands = s.query(Candidate).filter_by(experiment_id=exp_id, status="ok").all()
    by_seg: dict[str, Candidate] = {}
    for c in cands:
        if c.segment_id not in by_seg:
            by_seg[c.segment_id] = c
    seg_ids = sorted(by_seg)
    rng = random.Random(seed)
    picked = rng.sample(seg_ids, min(n, len(seg_ids)))
    out = []
    for sid in picked:
        cand = by_seg[sid]
        human = s.get(Segment, sid)
        if not human or not human.text or not cand.text:
            continue
        texts, ctx_mode = scene_context(s, human)
        out.append({"candidate_id": cand.id, "human_text": human.text,
                    "candidate_text": cand.text,
                    "context": (chr(10) * 2).join(texts) or None,
                    "ctx_mode": ctx_mode})
    return out


def _already(s, exp_id: str, model: str) -> set[str]:
    rows = (s.query(JudgeRun)
            .filter_by(experiment_id=exp_id, judge_kind=JUDGE_KIND,
                       model__in=config.model_any(model),
                       prompt_version=PREFERENCE_PROMPT_VERSION)
            .filter(JudgeRun.status == "ok").all())
    return {r.subject_id for r in rows}


def run_one(exp_id: str, model: str, pair: dict) -> dict:
    cid = pair["candidate_id"]
    with db.session() as s:
        if cid in _already(s, exp_id, model):
            with _lock:
                _counter["skip"] += 1
            return {"skip": True}
    rng = random.Random(f"xcorpus:{cid}")
    out = judge_preference(human_text=pair["human_text"], candidate_text=pair["candidate_text"],
                           model=model, rng=rng, context=pair.get("context"))
    payload = None
    if out.get("status") == "ok":
        payload = {**out["verdict"], "human_was_a": out.get("human_was_a"),
                   "winner_resolved": out.get("winner_resolved"),
                   "ctx_mode": pair.get("ctx_mode"),
                   "ctx_chars": len(pair.get("context") or "")}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=exp_id, subject_type="candidate", subject_id=cid,
                       judge_kind=JUDGE_KIND, model=model,
                       prompt_version=PREFERENCE_PROMPT_VERSION,
                       verdict=payload, confidence=out.get("confidence"),
                       abstain=bool(out.get("abstain", False)), status=out["status"]))
        s.commit()
    with _lock:
        key = "ok" if out["status"] == "ok" else "failed"
        _counter[key] += 1
        if key == "failed" and not _counter["first_error"]:
            _counter["first_error"] = pf.redact(
                out.get("error") or out.get("raw") or f'status={out["status"]}')
    return {"ok": out["status"] == "ok"}


def _report() -> None:
    """打印各语料「评委挑 candidate 比例」。"""
    print(f"\n{'语料':24s}{'评委':14s}{'n':>4s}{'挑candidate':>13s}{'挑human':>9s}{'弃权/非二选一':>14s}")
    with db.session() as s:
        for label, exp_id in CORPORA:
            for m in ("moonshotai/kimi-k3", config.DEFAULT_LLM_MODEL):
                rows = (s.query(JudgeRun)
                        .filter_by(experiment_id=exp_id, judge_kind=JUDGE_KIND,
                                   model__in=config.model_any(m),
                                   prompt_version=PREFERENCE_PROMPT_VERSION)
                        .filter(JudgeRun.status == "ok").all())
                pc = ph = other = 0
                for r in rows:
                    w = (r.verdict or {}).get("winner_resolved")
                    if w == "candidate":
                        pc += 1
                    elif w == "human":
                        ph += 1
                    else:
                        other += 1
                n = pc + ph
                rate = f"{pc / n:.3f}" if n else "—"
                print(f"{label:24s}{m.split('/')[-1]:14s}{n:4d}{rate:>13s}{ph:9d}{other:>14d}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-corpus", type=int, default=20)
    ap.add_argument("--judges", default=DEFAULT_JUDGES)
    ap.add_argument("--conc", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260914)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    judges = [m.strip() for m in args.judges.split(",") if m.strip()]
    print(f"跨语料偏差探针 / prompt={PREFERENCE_PROMPT_VERSION} / 每语料 {args.per_corpus} 对 "
          f"× {len(judges)} 评委")
    jobs = []
    for label, exp_id in CORPORA:
        with db.session() as s:
            pairs = _sample_pairs(s, exp_id, args.per_corpus, args.seed)
        chars = [len(p["context"] or "") for p in pairs]
        med = sorted(chars)[len(chars) // 2] if chars else 0
        print(f"  {label:24s} {exp_id:18s} 取 {len(pairs):3d} 对，上文中位 {med} 字")
        if args.dry_run:
            continue
        jobs += [(exp_id, m, p) for m in judges for p in pairs]

    if args.dry_run:
        print(f"\ndry-run：预计 {len(CORPORA) * args.per_corpus * len(judges)} 次调用，未发起")
        return

    print(f"\n共 {len(jobs)} 次调用…")
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: run_one(*j), jobs):
            pass
    print(f"完成：ok={_counter['ok']} failed={_counter['failed']} skip={_counter['skip']}")
    _report()


if __name__ == "__main__":
    main()
