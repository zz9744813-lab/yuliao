"""字表归一化修复 —— 盗版 txt 系统性错字（2026-09-19 集霸授权，不整体换源）。

只处理**频次自洽确认过**的单字系统性错字（source_check.KNOWN_TYPOS 同一方法）：
同一作品里正确写法压倒性多数 → 少数派即抄写错误。原文 `text` 一律不动，
修复写 `text_clean`（§7.6 纪律 1：可审计、可重跑）；修复后按规则重判 integrity，
让被错字冤枉的段回到可用池。

对照实验（--build-ab）：同一批修复段上建「原文字侧 vs 修复字侧」两套冻结诊断集，
同一重建候选、只差错字 → qoder detection 两读数对照，量出错字对判别读数的影响。
诊断集 kind=typo_ab，只作仪器不入正式基准（不入 §14 排行榜对比）。

用法：
    python scripts/normalize_typos.py --scan                 # 频次表 + 上下文样本
    python scripts/normalize_typos.py --apply                # 修复 text_clean + 重判 integrity
    python scripts/normalize_typos.py --report               # 影响面报告
    python scripts/normalize_typos.py --build-ab 30 --run    # 建 AB 诊断集（跑引擎+评委）
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db  # noqa: E402
from app.models import BenchmarkItem, BenchmarkSet, ControlledCorruption, Segment, Work  # noqa: E402
from app.ids import new_id  # noqa: E402
from scripts.source_check import rule_defects  # noqa: E402
from app.typo_map import RULES as _TYPO_RULES  # noqa: E402  单一事实源（T-CORPUS-V2）
from k2_extract_backfill import src_ok_strict  # noqa: E402  src_ok 读取侧唯一入口（同源消费）

# 频次自洽确认过的系统性错字（规则本体收敛到 app/typo_map.py；works=允许修复的作品）
TYPO_TABLE = [
    {**rule, "works": works}
    for rule, works in zip(_TYPO_RULES, (
        ("斗罗大陆",), ("斗罗大陆", "将夜"), ("斗罗大陆",)))
]


def _hit(text: str, bad: str, lookbehind: str | None) -> int:
    if not text:
        return 0
    pat = f"(?<!{lookbehind}){re.escape(bad)}" if lookbehind else re.escape(bad)
    return len(re.findall(pat, text))


def _work_title(work_id: str, cache: dict) -> str:
    if work_id not in cache:
        with db.session() as s:
            w = s.get(Work, work_id)
            cache[work_id] = (w.title if w else "") or ""
    return cache[work_id]


def scan() -> dict:
    """全量频次表：每对错字×作品的 bad/good 计数 + 上下文样本。"""
    out = []
    cache: dict[str, str] = {}
    with db.session() as s:
        rows = s.query(Segment).all()
    for rule in TYPO_TABLE:
        per_work: Counter = Counter()
        good_per_work: Counter = Counter()
        samples = []
        for seg in rows:
            text = seg.text_clean or seg.text or ""
            title = _work_title(seg.work_id, cache)
            n_bad = _hit(text, rule["bad"], rule.get("lookbehind"))
            n_good = _hit(text, rule["good"], None)
            if n_bad:
                per_work[title] += n_bad
                if len(samples) < 6:
                    i = re.search(f"(?<!{rule.get('lookbehind') or ''}){re.escape(rule['bad'])}", text)
                    if i:
                        samples.append({"work": title[:14], "segment": seg.id,
                                        "context": text[max(0, i.start() - 15):i.start() + 15]})
            if n_good:
                good_per_work[title] += n_good
        confirmed = []
        for title, n_bad in per_work.items():
            n_good = good_per_work.get(title, 0)
            allowed = any(k in title for k in rule["works"])
            ok = allowed and n_good >= 5 and n_bad <= 0.5 * n_good
            confirmed.append({"work": title[:20], "bad": n_bad, "good": n_good,
                              "confirmed": bool(ok), "work_allowed": allowed})
        out.append({**rule, "evidence": sorted(confirmed, key=lambda x: -x["bad"]),
                    "samples": samples})
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return {"rules": out}


def _repaired(text: str, work_title: str) -> tuple[str, int]:
    """对一段文本应用全部适用规则，返回（新文本, 替换次数）。"""
    n = 0
    for rule in TYPO_TABLE:
        if not any(k in work_title for k in rule["works"]):
            continue
        pat = (f"(?<!{rule['lookbehind']}){re.escape(rule['bad'])}"
               if rule.get("lookbehind") else re.escape(rule["bad"]))
        text, k = re.subn(pat, rule["good"], text)
        n += k
    return text, n


def apply_repairs() -> dict:
    """text_clean 修复 + 规则重判 integrity。原文 text 不动；幂等。"""
    cache: dict[str, str] = {}
    per_work: Counter = Counter()
    n_seg = n_replaced = 0
    with db.session() as s:
        segs = s.query(Segment).all()
        for seg in segs:
            title = _work_title(seg.work_id, cache)
            base = seg.text_clean if seg.text_clean is not None else seg.text
            new, k = _repaired(base or "", title)
            if k == 0:
                continue
            seg.text_clean = new
            n_seg += 1
            n_replaced += k
            per_work[title[:20]] += k
        s.commit()
    # ⚠ 军师退回（P0）：**只许改字，不许碰 src_ok / 缺陷字段**。规则只认识字表，
    # 修人名不该顺手放行 LLM 查出的缺句/截断。integrity 一律由 source_check
    # 的 LLM 校勘重判：`python scripts/source_check.py --run --scope used`。
    out = {"segments_touched": n_seg, "replacements": n_replaced,
           "by_work": dict(per_work),
           "integrity_note": "src_ok 不在本工具职责内；重判请跑 source_check --run"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def report() -> dict:
    cache: dict[str, str] = {}
    with db.session() as s:
        segs = s.query(Segment).all()
        n_total = len(segs)
        pending = 0
        for seg in segs:
            title = _work_title(seg.work_id, cache)
            base = seg.text_clean if seg.text_clean is not None else seg.text
            _, k = _repaired(base or "", title)
            pending += 1 if k else 0
        n_ok = sum(1 for (raw,) in s.query(Segment.integrity).all()
                   if _json_ok(raw))
        sets = [{"id": t.id, "name": t.name, "kind": t.kind, "n": t.n_items}
                for t in s.query(BenchmarkSet).all()]
    out = {"segments_total": n_total, "segments_pending_repair": pending,
           "segments_src_ok_true": n_ok, "benchmark_sets": sets,
           "frozen_note": "基准条目文本已冻结（含错字原样）——隔离保证历史读数可跨时间比较，"
                          "修复不影响已建集合；新集合一律用修复后的 text_clean"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def _json_ok(raw) -> bool:
    # 口径同源：k2_extract_backfill.src_ok_strict（严格布尔，非字典/解析失败=未校验→False）
    return src_ok_strict(raw)


def build_ab(n_seg: int, seed: int = 20260919, dry_run: bool = True) -> dict:
    """在修复段上建「原文侧 / 修复侧」两套冻结诊断集（同一重建候选，只差错字）。

    前置：--apply 已跑（text_clean 已修复）+ EXP-TYPO-AB 已跑过 extract+reconstruct
    （候选来自修复后文本的帧）。两套集合 human 侧分别为 `text`（含错字）与
    `text_clean`（修复），variant 同一条；位置同种子 → 读数差异只来自错字。
    """
    EXP = "EXP-TYPO-AB"
    with db.session() as s:
        from app.models import Candidate, Experiment, Frame
        exp = s.get(Experiment, EXP)
        if exp is None:
            return {"error": f"先建实验 {EXP}（bench_recon_setup 模式）并跑 extract+reconstruct"}
        ids = (exp.config or {}).get("segment_ids") or []
        cands = (s.query(Candidate)
                 .filter(Candidate.experiment_id == EXP, Candidate.status == "ok").all())
        by_seg: dict[str, list] = {}
        for c in cands:
            by_seg.setdefault(c.segment_id, []).append(c)
        rng_pick = random.Random(seed)
        picked = []
        for sid in sorted(rng_pick.sample(sorted(by_seg), min(n_seg * 2, len(by_seg)))):
            if len(picked) >= n_seg:
                break
            seg = s.get(Segment, sid)
            if seg is None:
                continue
            original = seg.text or ""
            repaired = seg.text_clean or original
            if original == repaired:
                continue                      # 该段没被修过，进不了对照
            picked.append((seg, by_seg[sid][0], original, repaired))
        if dry_run:
            return {"would_build_pairs": len(picked)}
        created = []
        for tag, human_pick in (("before", lambda seg, orig, rep: orig),
                                ("after", lambda seg, orig, rep: rep)):
            rng = random.Random(seed)      # 每套集合重置种子 → 两侧位置逐题一致
            st = BenchmarkSet(id=new_id("BS"), name=f"typo-{tag}-v1", version=1,
                              kind="typo_ab", n_items=len(picked),
                              spec={"source": "EXP-TYPO-AB candidates",
                                    "human_side": ("original text" if tag == "before"
                                                   else "repaired text_clean"),
                                    "position_seed": seed},
                              note=f"错字对照诊断集（{tag}）：human 侧={'含错字原文' if tag == 'before' else '修复后'}，"
                                   "variant 同源；诊断仪器，不进 §14 排行榜")
            s.add(st)
            s.flush()
            for seg, cand, orig, rep in picked:
                human = human_pick(seg, orig, rep)
                if rng.random() < 0.5:
                    a, b, ans = human, cand.text, "A"
                else:
                    a, b, ans = cand.text, human, "B"
                s.add(BenchmarkItem(set_id=st.id, segment_id=seg.id, kind="typo_ab",
                                    context="", text_a=a, text_b=b, answer=ans,
                                    meta={"repaired": tag == "after"}))
            created.append({"set_id": st.id, "name": st.name, "items": len(picked)})
        s.commit()
        return {"sets": created}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--build-ab", type=int, default=0, metavar="N")
    ap.add_argument("--run", action="store_true", help="配合 --build-ab：真写库")
    ap.add_argument("--seed", type=int, default=20260919)
    args = ap.parse_args()
    db.init_db()
    if args.scan:
        scan()
    elif args.apply:
        apply_repairs()
    elif args.report:
        report()
    elif args.build_ab:
        print(json.dumps(build_ab(args.build_ab, args.seed,
                                  dry_run=not args.run), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
