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

from app import config, db, limits  # noqa: E402
from app.models import (Candidate, ControlledCorruption, Experiment, Frame, Segment,  # noqa: E402
                        Work, exclude_corpus_v2_segments)
from app.experiments import stage_extract_frames  # noqa: E402
from app.gateway import is_serial_model  # noqa: E402
from _conc_guard import check_conc as _check_conc, pool_workers as _pool_workers  # noqa: E402  # 并发闸唯一实现（2026-09-25 去重）
from make_random_batch import extras_start, looks_watermarked  # noqa: E402
from clean_text import clean_rules, looks_broken, needs_llm  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内
import source_check  # noqa: E402  # 扩产的源校勘闸门（MODEL 也在这里定义）


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=int, default=0)
    ap.add_argument("--run", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--conc", type=int, default=8,
                    help=f"并发（上界 app/limits.MAX_CONCURRENCY="
                         f"{limits.MAX_CONCURRENCY}，越界报错退出）")
    ap.add_argument("--min-chars", type=int, default=60)
    ap.add_argument("--exp", default="EXP-0918-SCALE")
    return ap


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = build_parser()
    args = ap.parse_args(argv)
    _check_conc(ap, args.conc, "--conc")
    return args


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
    # 写入/下传之前先夹紧（2026-09-25 收口）：下面三处消费 conc——
    # exp.config["concurrency"]（落库）、source_check.run(conc=)（它自建线程池，
    # 不在本脚本改动范围）、stage_extract_frames→_pool_map（执行侧已有同闸）。
    # 入口兜底保证任何一条路径都拿不到越界值；串行模型命中则整链压成 1。
    conc = _pool_workers(conc, [config.DEFAULT_LLM_MODEL, source_check.MODEL],
                         serial_check=is_serial_model)
    # 批量防呆①（P0 死 id 事故）：开跑前先把要用的模型名问一遍网关。
    # 死 id 的表现是"源校勘通过 0/300 → 抽到 0 帧"，看上去像池子耗尽，实际全在 503。
    blocked = pf.preflight_block([config.DEFAULT_LLM_MODEL, source_check.MODEL],
                                 source="scale_corpus")
    if blocked:
        print(f"[预检失败] 未开跑：{blocked}")
        return {"picked": 0, "aborted": True, "reason": blocked}
    ids = pick(n, seed, min_chars)
    print(f"挑到候选段 {len(ids)}（从没被任何实验用过、无水印无拼音、够长）")
    if not ids:
        return {"picked": 0}
    with db.session() as s:
        e = s.get(Experiment, exp_id)
        if e is None:
            e = Experiment(id=exp_id, name=f"corpus_scale {time.strftime('%Y-%m-%d')}",
                           status="created",
                           config={"segment_ids": ids, "granularities": ["L"],
                                   "extractors": [config.DEFAULT_LLM_MODEL],
                                   "concurrency": conc,
                                   "kind": "corpus_scale"})
            s.add(e)
            s.commit()
        else:
            print(f"复用已有扩产实验 {exp_id}")
            # 预检对象 = 实际要用的对象（会审意见）：复用实验真正跑的是
            # e.config["extractors"]，存量配置里的死 id 必须拦在开跑前——
            # 这正是「源校勘通过 0/300」那条路径的残留入口。
            stored = list((e.config or {}).get("extractors") or [])
            if stored:
                blocked = pf.preflight_block(stored, source=f"scale_corpus:{exp_id}")
                if blocked:
                    print(f"[预检失败] 复用实验的存量 extractors 有死 id，未开跑：{blocked}")
                    return {"picked": 0, "aborted": True, "reason": blocked}
    # 1) 源校勘（确定性规则先跑，命中直接判坏）
    bad = []
    with db.session() as s:
        for sid in ids:
            seg = s.get(Segment, sid)
            hits = source_check.rule_defects(seg.text_clean or seg.text)
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
    sc_res = source_check.run(conc=conc, ids=list(ids))
    if sc_res.get("aborted"):
        # 源校勘自己没跑完（预检拦下 / 失败率熔断）：原因原样上抛。
        # 绝不能继续往下算 ok_ids——那会把它伪装成"源校勘无一通过"（=池子耗尽的假象）。
        reason = sc_res.get("first_error") or "源校勘中途熔断（失败率超线，原文见上方输出）"
        print(f"[扩产中止] 源校勘未跑完，不进入抽帧：{reason}")
        return {"picked": 0, "aborted": True, "reason": reason,
                "source_check": {k: sc_res.get(k) for k in ("ok", "failed", "skip", "bad")}}
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
        return {"picked": 0, "reason": "源校勘无一通过（LLM 侧失败 "
                                       f"{sc_res.get('failed', 0)} 条）"}
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
    out = {"picked": len(ids), "frames": n_frames, "exp": exp_id}
    if ids and n_frames == 0:
        # 全灭不是"池子小"，通常是抽帧模型整批报错：把首条原文就地打出来
        with db.session() as s:
            fr = (s.query(Frame)
                  .filter(Frame.experiment_id == exp_id, Frame.granularity == "L",
                          Frame.status == "failed").first())
        hint = pf.redact(fr.raw_output, 200) if fr else "（库里没有 failed 帧行可引）"
        print(f"[抽帧全灭] 0/{len(ids)}；首条原文：{hint or '（帧行没留 raw_output）'}"
              f"；明细看 llm_calls（experiment_id={exp_id}）的 error 字段")
        out.update(aborted=True, reason=f"L 帧全灭 0/{len(ids)}，首条原文：{hint}")
    return out


def main() -> None:
    args = parse_args()
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
        res = run(args.run, args.seed, args.conc, args.exp, args.min_chars)
        print(json.dumps(res, ensure_ascii=False))
        if res.get("aborted"):
            sys.exit(2)     # 预检没过 / 源校勘熔断 / 抽帧全灭：非零退出，别静默空转
        return
    build_parser().print_help()


if __name__ == "__main__":
    main()
