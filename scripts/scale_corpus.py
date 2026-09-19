"""语料扩产 —— 把"人类锚点"从几百段推到几千段（总方案 §42 的数据前提）。

## 为什么现在做这个

项目最硬的缺口不是算法，是**数据量**：L 语义帧只有 325 个。
§42 要训的 Writer 是 "SemanticFrame + 策略 + 上下文 → 文本"，
而这条链的第一环（帧）只有几百条，撑不起训练。

**关键：扩产不需要集霸判题。** SFT 的目标文本就是**人类原文**本身
（frame → 人怎么写的），标签天然存在。集霸的判定只用于**偏好**数据
（两个候选哪个好），那是另一条线。所以扩产是纯自动的，可以放手跑。

## 流水线（每一步都设闸，脏数据不许往下走）

```
挑段 → 规则清洗+水印过滤 → LLM 源校勘（src_ok） → 抽 L 帧 → （可选）生成劣化对
```

## 用法

    python scripts/scale_corpus.py --plan 200            # 只看会挑到哪些段
    python scripts/scale_corpus.py --run 200 --conc 8    # 走完整条流水线
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.models import (Candidate, ControlledCorruption, Experiment, Frame, Segment,  # noqa: E402
                        Work, exclude_corpus_v2_segments)
from app.experiments import stage_extract_frames  # noqa: E402
from make_random_batch import extras_start, looks_watermarked  # noqa: E402
from clean_text import clean_rules, looks_broken, needs_llm  # noqa: E402


def pick(n: int, seed: int, min_chars: int = 60, works: list[str] | None = None) -> list[str]:
    """挑"从没被任何实验用过"的干净段。

    五条排除：已有候选/劣化记录、fixture、番外、水印或拼音伪影、
    corpus v2 镜像段（v1 的错字修复副本，同文双份入池=重复计数）。
    （源校勘是下一步，不在这里做——那要花 LLM 调用。）
    """
    with db.session() as s:
        # corpus v2 段 role=None，不排就会和 v1 同文双份进扩产池（会审①收口）
        segs = s.query(Segment).filter(Segment.role.is_(None),
                                       exclude_corpus_v2_segments()).all()
        used = {r[0] for r in s.query(Candidate.segment_id).distinct()}
        used |= {r[0] for r in s.query(ControlledCorruption.segment_id).distinct()}
        have_frame = {r[0] for r in s.query(Frame.segment_id).distinct()}
        titles = {w.id: (w.title or "") for w in s.query(Work).all()}
        starts: dict[tuple, int | None] = {}
        pool = []
        for seg in segs:
            if seg.id in used or seg.id in have_frame:
                continue
            if titles.get(seg.work_id, "").startswith("fixture"):
                continue
            t = clean_rules(seg.text or "")
            if not t or len(t.strip()) < min_chars:
                continue
            if looks_watermarked(seg.text) or needs_llm(t) or looks_broken(t):
                continue
            key = (seg.work_id, seg.seg_version)
            if key not in starts:
                starts[key] = extras_start(s, seg.work_id, seg.seg_version)
            st = starts[key]
            if st is not None and seg.ordinal >= st:
                continue
            pool.append(seg.id)
        pool.sort()
        rng = random.Random(seed)
        # 跨作品均匀取样（单一作品的文风会让数据集偏）
        by_work: dict[str, list] = {}
        for sid in pool:
            w = s.get(Segment, sid).work_id
            by_work.setdefault(w, []).append(sid)
        picked, keys = [], sorted(by_work)
        while len(picked) < min(n, len(pool)):
            progressed = False
            for k in keys:
                if len(picked) >= min(n, len(pool)):
                    break
                if by_work[k]:
                    picked.append(by_work[k].pop(rng.randrange(len(by_work[k]))))
                    progressed = True
            if not progressed:
                break
        return picked


def run(n: int, seed: int, conc: int, exp_id: str, min_chars: int = 60) -> dict:
    ids = pick(n, seed, min_chars)
    print(f"挑到候选段 {len(ids)}（从没被任何实验用过、无水印无拼音、够长）")
    if not ids:
        return {"picked": 0}
    from app import config
    with db.session() as s:
        e = s.get(Experiment, exp_id)
        if e is None:
            e = Experiment(id=exp_id, name=f"corpus_scale {time.strftime('%Y-%m-%d')}",
                           status="created",
                           config={"segment_ids": ids, "granularities": ["L"],
                                   "extractors": ["deepseek/deepseek-v4.1-flash"],
                                   "concurrency": conc,
                                   "kind": "corpus_scale"})
            s.add(e)
            s.commit()
        else:
            print(f"复用已有扩产实验 {exp_id}")
    # 1) 源校勘（确定性规则先跑，命中直接判坏）
    from source_check import rule_defects
    bad = []
    with db.session() as s:
        for sid in ids:
            seg = s.get(Segment, sid)
            hits = rule_defects(seg.text_clean or seg.text)
            if hits:
                bad.append(sid)
                seg.integrity = json.dumps({"src_ok": False, "severity": "high",
                                            "defects": hits, "checked_pv": "rules"},
                                           ensure_ascii=False)
        s.commit()
    if bad:
        ids = [i for i in ids if i not in set(bad)]
        print(f"规则判坏 {len(bad)} 段，剩 {len(ids)}")
    # 1b) LLM 源校勘（规则抓不到掉字/错字；训练数据不能用坏源）
    import source_check
    source_check.run(conc=conc, ids=list(ids))
    with db.session() as s:
        ok_ids = []
        for sid in ids:
            seg = s.get(Segment, sid)
            try:
                d = json.loads(seg.integrity or "{}")
            except Exception:
                d = {}
            if d.get("src_ok") is True:
                ok_ids.append(sid)
    print(f"源校勘通过 {len(ok_ids)}/{len(ids)}")
    ids = ok_ids
    if not ids:
        return {"picked": 0, "reason": "源校勘无一通过"}
    # 2) 抽 L 帧
    with db.session() as s:
        e = s.get(Experiment, exp_id)
        e.config = {**e.config, "segment_ids": ids}
        s.commit()
        stage_extract_frames(s, e)
        n_frames = s.query(Frame).filter(Frame.experiment_id == exp_id,
                                         Frame.granularity == "L",
                                         Frame.status != "failed").count()
    print(f"抽到 L 帧 {n_frames}/{len(ids)}（实验 {exp_id}）")
    return {"picked": len(ids), "frames": n_frames, "exp": exp_id}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=int, default=0)
    ap.add_argument("--run", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--min-chars", type=int, default=60)
    ap.add_argument("--exp", default="EXP-0918-SCALE")
    args = ap.parse_args()
    db.init_db()
    if args.plan:
        ids = pick(args.plan, args.seed, args.min_chars)
        print(f"计划挑 {len(ids)} 段；样例：")
        with db.session() as s:
            for sid in ids[:8]:
                seg = s.get(Segment, sid)
                print(f'   {sid} {len(seg.text)}字 :: {seg.text[:44]}')
        return
    if args.run:
        print(json.dumps(run(args.run, args.seed, args.conc, args.exp,
                             args.min_chars), ensure_ascii=False))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
