"""Preference-task Judge 重测（Phase 1.5 §十 v1 修正）。

问题：adversarial judge 判"哪边是 AI"，用户判"哪边更好"，口径错位使 agreement ≈ 抛硬币。
做法：换成本 Judge 做**与用户完全相同的任务**——匿名 A/B 直接问"哪边写得更好"，
      再与用户已判条目对齐算 agreement。

⚠ 上下文（2026-09-14 修正）：早期版本只把两段裸文本给 Judge，而前端取题是**带上文**的
（scene_context 回溯到场景起点）。这造成"用户带上下文判、Judge 空手判"的不对称比较，
而 calibration-report-v1 §三 早已立下**强制**规程：human vs candidate 必须供 ≥prev1 上下文
（segment_only 下 human 1/10，带 prev1 后 7/10）。本脚本现与前端共用同一个 scene_context。
`--no-context` 仅供做上下文消融对照，会写成独立 prompt_version。

幂等：已存在 (subject_id, model, prompt_version) 且 status=ok 的记录则跳过。
匿名：A/B 位置由 random.Random(f"pref:{candidate_id}") 决定，可复现；human_was_a 只进库。

用法：
    python scripts/pref_judge.py EXP-0911-B82D --batch r15
    python scripts/pref_judge.py EXP-0911-B82D --all --judges moonshotai/kimi-k3,deepseek/deepseek-v4.1-flash
    python scripts/pref_judge.py EXP-0911-B82D --batch r15 --no-context   # 消融对照
    python scripts/pref_judge.py EXP-0911-B82D --batch r15 --dry-run
"""
from __future__ import annotations

import argparse
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.context_ablation import scene_context
from app.judges import PREFERENCE_PROMPT_VERSION, judge_preference
from app.models import Candidate, JudgeRun, ReviewItem, Segment

DEFAULT_JUDGES = "moonshotai/kimi-k3,deepseek/deepseek-v4.1-flash"

_lock = threading.Lock()
_counter = {"ok": 0, "failed": 0, "skip": 0}


def _load_pairs(s, exp_id: str, batch: str | None, with_context: bool) -> list[dict]:
    """取"用户已判"的题目，还原成 (candidate, human_text) 对。

    with_context=True 时用 context_ablation.scene_context 取**与前端取题完全相同**的上文——
    评审规程（calibration-report-v1 §三）要求 human vs candidate 必须带 ≥prev1 上下文，
    否则是 segment_only 条件（实测 human 1/10 vs 带 prev1 时 7/10，严重仪器偏差）。
    """
    tag = f"batch_{batch}" if batch else None
    items = s.query(ReviewItem).filter_by(experiment_id=exp_id, status="done").all()
    out = []
    for r in items:
        if not r.human_verdict:
            continue
        if tag and tag not in (r.reasons or []):
            continue
        cand = s.get(Candidate, r.subject_id)
        if not cand or not cand.text:
            continue
        human = s.get(Segment, cand.segment_id)
        if not human or not human.text:
            continue
        context, ctx_mode = "", "segment_only"
        if with_context:
            texts, ctx_mode = scene_context(s, human)
            context = (chr(10) * 2).join(texts)
        out.append({"candidate_id": cand.id, "human_text": human.text,
                    "candidate_text": cand.text, "context": context or None,
                    "ctx_mode": ctx_mode,
                    "user_verdict": r.human_verdict.get("winner_resolved")})
    return out


def _already_done(s, exp_id: str, model: str, prompt_version: str) -> set[str]:
    """幂等键含 prompt_version（沿用对抗审查 P1-2 教训）：
    只按 (subject, model) 判重会让改版 prompt 后的旧产物被静默复用。"""
    rows = (s.query(JudgeRun)
            .filter_by(experiment_id=exp_id, judge_kind="preference", model=model,
                       prompt_version=prompt_version)
            .filter(JudgeRun.status == "ok").all())
    return {r.subject_id for r in rows}


def run_one(exp_id: str, model: str, pair: dict, prompt_version: str) -> dict:
    cid = pair["candidate_id"]
    with db.session() as s:
        if cid in _already_done(s, exp_id, model, prompt_version):
            with _lock:
                _counter["skip"] += 1
            return {"skip": True}
    rng = random.Random(f"pref:{cid}")          # 位置可复现，重跑不会换边
    out = judge_preference(human_text=pair["human_text"],
                           candidate_text=pair["candidate_text"],
                           model=model, rng=rng, context=pair.get("context"))
    # 与 adversarial 同一约定：human_was_a / winner_resolved 只进库做汇总，
    # prompt 中永远不出现（保证匿名）。ctx_mode 一并落库以便事后做上下文消融。
    payload = None
    if out.get("status") == "ok":
        payload = {**out["verdict"],
                   "human_was_a": out.get("human_was_a"),
                   "winner_resolved": out.get("winner_resolved"),
                   "ctx_mode": pair.get("ctx_mode"),
                   "ctx_chars": len(pair.get("context") or "")}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=exp_id, subject_type="candidate", subject_id=cid,
                       judge_kind="preference", model=model,
                       prompt_version=prompt_version,
                       verdict=payload,
                       confidence=out.get("confidence"),
                       abstain=bool(out.get("abstain", False)),
                       status=out["status"]))
        s.commit()
    with _lock:
        key = "ok" if out["status"] == "ok" else "failed"
        _counter[key] += 1
        n = _counter["ok"] + _counter["failed"]
        if n % 10 == 0:
            print(f"  progress: ok={_counter['ok']} failed={_counter['failed']}", flush=True)
    return {"ok": out["status"] == "ok"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("exp_id", nargs="?", default="EXP-0911-B82D")
    ap.add_argument("--batch", default=None, help="只看该批次的已判条目（如 r15）")
    ap.add_argument("--all", action="store_true", help="该实验全部已判条目")
    ap.add_argument("--judges", default=DEFAULT_JUDGES)
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--no-context", action="store_true",
                    help="消融用：不给上文（=segment_only）。协议要求必须给上下文，"
                         "只有做对照实验时才用；会写成独立 prompt_version 避免与正式口径混淆")
    ap.add_argument("--dry-run", action="store_true", help="只列出将判的条目，不调用 API")
    args = ap.parse_args()

    if not args.batch and not args.all:
        args.batch = "r15"      # 默认小批，避免误烧额度
    judges = [m.strip() for m in args.judges.split(",") if m.strip()]
    with_context = not args.no_context
    prompt_version = (PREFERENCE_PROMPT_VERSION if with_context
                      else PREFERENCE_PROMPT_VERSION + "_nocontext")

    with db.session() as s:
        pairs = _load_pairs(s, args.exp_id, args.batch, with_context)
        # 版本切换提醒：幂等键含 prompt_version，升版后会整批重判（要花额度），
        # 这里显式提示，避免"以为会跳过、结果全量重跑"。
        stale = {}
        for r in s.query(JudgeRun).filter_by(experiment_id=args.exp_id,
                                             judge_kind="preference").all():
            if r.prompt_version != prompt_version:
                stale[r.prompt_version] = stale.get(r.prompt_version, 0) + 1
    scope = f"batch_{args.batch}" if args.batch else "全部已判"
    ctx_desc = "带上文（协议口径）" if with_context else "无上文（消融）"
    print(f"[{args.exp_id} / {scope}] {ctx_desc} / {prompt_version}")
    print(f"  待判 {len(pairs)} 对 × {len(judges)} 评委 = {len(pairs) * len(judges)} 次调用")
    if pairs:
        modes = {}
        for p in pairs:
            modes[p["ctx_mode"]] = modes.get(p["ctx_mode"], 0) + 1
        chars = [len(p["context"] or "") for p in pairs]
        print(f"  上下文：{modes}，中位 {sorted(chars)[len(chars) // 2]} 字")
    if stale:
        print(f"  注意：库里已有其它版本记录 {stale}，本次版本 {prompt_version}；"
              f"本次会重判（旧记录保留，不删）。只想跳过就别跑。")
    if not pairs:
        print("没有可判条目（用户尚未盲评该批）")
        return
    if args.dry_run:
        for p in pairs:
            print(f"  {p['candidate_id']}  user={p['user_verdict']}  "
                  f"human={len(p['human_text'])}字 cand={len(p['candidate_text'])}字 "
                  f"ctx={p['ctx_mode']}/{len(p['context'] or '')}字")
        print("dry-run：未调用任何 API")
        return

    jobs = [(args.exp_id, m, p, prompt_version) for m in judges for p in pairs]
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: run_one(*j), jobs):
            pass
    print(f"完成：ok={_counter['ok']} failed={_counter['failed']} skip={_counter['skip']}")
    print(f"下一步：python scripts/judge_matrix.py {args.exp_id}"
          + (f" {args.batch}" if args.batch else ""))


if __name__ == "__main__":
    main()
