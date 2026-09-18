"""窗口评委工具：让当前会话里的模型（不调 LLM API）做盲评。

设计要点：盲评纪律由文件隔离保证——
  prepare  把 top-N 待评项做成匿名 A/B 批次（映射写进 .map.json，评委不看）
  commit   评委给出 A/B 判定后，用映射翻译回 human/candidate 写库

用法：
  python scripts/window_judge.py prepare --exp EXP-0911-B82D --n 24
  # 评委读 data/blind/batch_xxx.json，按顺序给出判定列表
  python scripts/window_judge.py commit --batch data/blind/batch_xxx.json \
      --verdicts '["A","B","tie",...]'

自然度校准（不进 DB，出对照数字）：
  python scripts/window_judge.py prepare-nat --exp EXP-0911-B82D --n 10
  python scripts/window_judge.py commit-nat --batch data/blind/nat_xxx.json \
      --scores '[7,8,...]'
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.models import Candidate, ReviewItem, Segment

BLIND_DIR = Path(__file__).resolve().parent.parent / "data" / "blind"
# 会话内窗口评委（不调 LLM API，由会话本人充当）。
# 2026-09-14：glm 系模型已不可用，评委标识由 "glm-5.3-flash-window" 改为中性名；
# 该日期之前的落库记录仍带旧标识，做 Matrix 时两者都算"窗口评委"。
JUDGE_ID = "session-window-judge"


def _pair_texts(s, item: ReviewItem):
    cand = s.get(Candidate, item.subject_id)
    if not cand or not cand.text:
        return None
    human = s.get(Segment, cand.segment_id)
    if not human:
        return None
    return human.text, cand.text


def cmd_prepare(exp_id: str, n: int) -> None:
    BLIND_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%H%M%S")
    with db.session() as s:
        items = (s.query(ReviewItem)
                 .filter_by(experiment_id=exp_id, status="pending")
                 .order_by(ReviewItem.priority.desc()).limit(n).all())
        batch, mapping = [], {}
        for it in items:
            pair = _pair_texts(s, it)
            if not pair:
                continue
            human_text, cand_text = pair
            human_first = random.random() < 0.5
            a, b = (human_text, cand_text) if human_first else (cand_text, human_text)
            mapping[it.id] = {"human_is_a": human_first}
            batch.append({"review_id": it.id, "priority": it.priority,
                          "reasons": it.reasons, "text_a": a, "text_b": b})
    bpath = BLIND_DIR / f"batch_{stamp}.json"
    mpath = BLIND_DIR / f"batch_{stamp}.map.json"
    bpath.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    mpath.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"批次: {bpath}（{len(batch)} 题）")
    print(f"映射（评委禁读）: {mpath}")


def cmd_commit(batch_path: str, verdicts_json: str) -> None:
    batch = json.loads(Path(batch_path).read_text(encoding="utf-8"))
    verdicts = json.loads(verdicts_json)
    assert len(verdicts) == len(batch), f"判定数 {len(verdicts)} != 题数 {len(batch)}"
    mpath = Path(batch_path).with_suffix("").with_suffix("")
    mpath = Path(str(batch_path).replace(".json", ".map.json"))
    mapping = json.loads(mpath.read_text(encoding="utf-8"))
    from datetime import datetime as dt
    with db.session() as s:
        for item, verdict in zip(batch, verdicts):
            v = str(verdict).upper()
            if v not in ("A", "B", "TIE", "BOTH_BAD", "CANT_JUDGE"):
                raise SystemExit(f"非法判定: {verdict}")
            info = mapping[item["review_id"]]
            if v in ("A", "B"):
                resolved = "human" if (v == "A") == info["human_is_a"] else "candidate"
            else:
                resolved = v.lower()
            r = s.get(ReviewItem, item["review_id"])
            r.status = "done"
            r.human_verdict = {
                "judge": JUDGE_ID,
                "winner_raw": v,
                "winner_resolved": resolved,
                "human_was_a": info["human_is_a"],
                "reviewed_at": dt.utcnow().isoformat(timespec="seconds") + "Z",
            }
        s.commit()
    wins = {}
    for item, verdict in zip(batch, verdicts):
        v = str(verdict).upper()
        if v in ("A", "B"):
            resolved = "human" if (v == "A") == mapping[item["review_id"]]["human_is_a"] else "candidate"
            wins[resolved] = wins.get(resolved, 0) + 1
    print("已写库。解盲统计:", wins,
          f"（human 胜率 {wins.get('human', 0) / max(1, sum(wins.values())):.0%}）")


def cmd_prepare_nat(exp_id: str, n: int, seed: int = 7, exclude_batch: str | None = None) -> None:
    """自然度校准：n 段 Human + n 段候选，混洗去标签，评委逐条 1~10 打分。
    qid 用中性 T{i}-0/1，组别只进映射文件——qid 不得泄露 H/C（首发版本犯过这个错）。"""
    BLIND_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%H%M%S")
    prev_texts: set[str] = set()
    if exclude_batch:
        prev = json.loads(Path(exclude_batch).read_text(encoding="utf-8"))
        prev_texts = {e["text"][:40] for e in prev}
    with db.session() as s:
        from app.models import Experiment
        exp = s.get(Experiment, exp_id)
        seg_rows = s.query(Segment).filter(Segment.id.in_(exp.config["segment_ids"])).all()
        if prev_texts:
            seg_rows = [x for x in seg_rows if x.text[:40] not in prev_texts]
        segs = random.Random(seed).sample(seg_rows, min(n, len(seg_rows)))
        pool = []
        for seg in segs:
            cands = (s.query(Candidate)
                     .filter_by(experiment_id=exp_id, segment_id=seg.id, status="ok")
                     .all())
            if cands:
                pool.append((seg, random.Random(seg.id).choice(cands)))
        flat, mapping = [], {}
        for i, (seg, cand) in enumerate(pool):
            human_first = random.Random(f"nat:{seg.id}").random() < 0.5
            first_text, second_text = ((seg.text, cand.text) if human_first
                                       else (cand.text, seg.text))
            flat.append({"qid": f"T{i}-0", "text": first_text})
            flat.append({"qid": f"T{i}-1", "text": second_text})
            mapping[f"T{i}-0"] = "human" if human_first else "candidate"
            mapping[f"T{i}-1"] = "candidate" if human_first else "human"
        random.Random(seed + 1).shuffle(flat)
    bpath = BLIND_DIR / f"nat_{stamp}.json"
    mpath = BLIND_DIR / f"nat_{stamp}.map.json"
    bpath.write_text(json.dumps(flat, ensure_ascii=False, indent=1), encoding="utf-8")
    mpath.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"校准批次: {bpath}（{len(flat)} 条）")
    print(f"映射（评委禁读）: {mpath}")


def cmd_commit_nat(batch_path: str, scores_json: str) -> None:
    entries = json.loads(Path(batch_path).read_text(encoding="utf-8"))
    scores = json.loads(scores_json)
    assert len(scores) == len(entries), f"分数数 {len(scores)} != 条数 {len(entries)}"
    mpath = Path(str(batch_path).replace(".json", ".map.json"))
    mapping = json.loads(mpath.read_text(encoding="utf-8"))
    import statistics
    by = {"human": [], "candidate": []}
    rows = []
    for e, sc in zip(entries, scores):
        g = mapping[e["qid"]]
        by[g].append(sc)
        rows.append((e["qid"], g, sc))
    for g, vals in by.items():
        print(f"{g}: n={len(vals)} mean={statistics.mean(vals):.2f} "
              f"stdev={statistics.pstdev(vals):.2f} min={min(vals)} max={max(vals)}")
    out = Path(str(batch_path).replace(".json", "_result.json"))
    out.write_text(json.dumps({"rows": rows,
                               "human_mean": statistics.mean(by["human"]),
                               "candidate_mean": statistics.mean(by["candidate"])},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果存:", out)


# ── 任务二：Context Ablation 盲评 ──────────────────────────

def cmd_prepare_ablation(exp_id: str, n: int, seed: int,
                         exclude_batches: list[str]) -> None:
    """n 个段 × 3 种上下文模式，每题 = 同一段的 human vs candidate 匿名 A/B。

    上下文块只含邻段（当前段文本进 A/B）；段是否 integrity-eligible 藏在映射里，
    解盲后再做"伪影组 vs 干净组"的偏好对比。排除已见文本防记忆污染。
    """
    from app.context_ablation import MODES, neighbors
    from app.models import Experiment
    BLIND_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%H%M%S")
    seen_prefixes: set[str] = set()
    for f in exclude_batches:
        for e in json.loads(Path(f).read_text(encoding="utf-8")):
            seen_prefixes.add(e.get("text", "")[:40])
            seen_prefixes.add(e.get("text_a", "")[:40])
    with db.session() as s:
        exp = s.get(Experiment, exp_id)
        segs = [s.get(Segment, sid) for sid in exp.config["segment_ids"]]
        segs = [x for x in segs if x and x.text[:40] not in seen_prefixes]
        rng = random.Random(seed)
        segs = rng.sample(segs, min(n, len(segs)))
        items, mapping = [], {}
        k = 0
        for seg in segs:
            cands = (s.query(Candidate).filter_by(experiment_id=exp_id,
                                                  segment_id=seg.id, status="ok").all())
            if not cands:
                continue
            cand = rng.choice(cands)
            nb = neighbors(s, seg)
            integrity = json.loads(seg.integrity or "{}")
            eligible = bool(integrity.get("eligible"))
            for mode in MODES:
                ctx = []
                if mode in ("prev1_current", "prev2_current_next1") and nb["prev1"]:
                    ctx.append(nb["prev1"].text)
                if mode == "prev2_current_next1":
                    if nb["prev2"]:
                        ctx.insert(0, nb["prev2"].text)
                    if nb["next1"]:
                        ctx.append(nb["next1"].text)
                ctx_block = "\n\n".join(
                    f"【后文】{t}" if (mode == "prev2_current_next1" and i == len(ctx) - 1
                                       and nb["next1"] and t == nb["next1"].text)
                    else f"【上文】{t}"
                    for i, t in enumerate(ctx))
                human_first = rng.random() < 0.5
                a, b = ((seg.text, cand.text) if human_first else (cand.text, seg.text))
                iid = f"A{k}"
                items.append({"item_id": iid, "mode": mode, "context": ctx_block,
                              "text_a": a, "text_b": b})
                mapping[iid] = {"human_is_a": human_first, "eligible": eligible}
                k += 1
    bpath = BLIND_DIR / f"abl_{stamp}.json"
    mpath = BLIND_DIR / f"abl_{stamp}.map.json"
    bpath.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    mpath.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"消融批次: {bpath}（{len(items)} 题）")
    print(f"映射（评委禁读）: {mpath}")


def cmd_commit_ablation(batch_path: str, verdicts_json: str) -> None:
    items = json.loads(Path(batch_path).read_text(encoding="utf-8"))
    verdicts = json.loads(verdicts_json)
    assert len(verdicts) == len(items)
    mapping = json.loads(Path(str(batch_path).replace(".json", ".map.json"))
                         .read_text(encoding="utf-8"))
    import statistics
    by_mode = {}
    by_mode_split = {}
    for it, v in zip(items, verdicts):
        v = str(v).upper()
        info = mapping[it["item_id"]]
        if v in ("A", "B"):
            winner = "human" if (v == "A") == info["human_is_a"] else "candidate"
        else:
            winner = v.lower()
        by_mode.setdefault(it["mode"], []).append(winner)
        grp = "eligible" if info["eligible"] else "artifact_risk"
        by_mode_split.setdefault(f"{it['mode']}|{grp}", []).append(winner)

    def summarize(d):
        return {k: {"human": sum(1 for x in v if x == "human"),
                    "candidate": sum(1 for x in v if x == "candidate"),
                    "tie_or_other": sum(1 for x in v if x not in ("human", "candidate"))}
                for k, v in d.items()}

    out = Path(str(batch_path).replace(".json", "_result.json"))
    result = {"by_mode": summarize(by_mode), "by_mode_split": summarize(by_mode_split)}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    print("结果存:", out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--exp", required=True); p.add_argument("--n", type=int, default=24)
    p = sub.add_parser("commit"); p.add_argument("--batch", required=True); p.add_argument("--verdicts", required=True)
    p = sub.add_parser("prepare-nat"); p.add_argument("--exp", required=True); p.add_argument("--n", type=int, default=10)
    p.add_argument("--seed", type=int, default=7); p.add_argument("--exclude-batch", default=None)
    p = sub.add_parser("commit-nat"); p.add_argument("--batch", required=True); p.add_argument("--scores", required=True)
    p = sub.add_parser("prepare-ablation"); p.add_argument("--exp", required=True)
    p.add_argument("--n", type=int, default=10); p.add_argument("--seed", type=int, default=11)
    p.add_argument("--exclude-batch", action="append", default=[])
    p = sub.add_parser("commit-ablation"); p.add_argument("--batch", required=True); p.add_argument("--verdicts", required=True)
    a = ap.parse_args()
    {"prepare": lambda: cmd_prepare(a.exp, a.n),
     "commit": lambda: cmd_commit(a.batch, a.verdicts),
     "prepare-nat": lambda: cmd_prepare_nat(a.exp, a.n, a.seed, a.exclude_batch),
     "commit-nat": lambda: cmd_commit_nat(a.batch, a.scores),
     "prepare-ablation": lambda: cmd_prepare_ablation(a.exp, a.n, a.seed, a.exclude_batch),
     "commit-ablation": lambda: cmd_commit_ablation(a.batch, a.verdicts)}[a.cmd]()


if __name__ == "__main__":
    main()
