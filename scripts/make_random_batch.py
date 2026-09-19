"""建随机抽样盲评批（Phase 1.5 Gate 用）。

为什么需要它：现有 99 条判定全部来自**信息量优先队列**（human_upset / judge_disagreement
过采样），是"最难子集"，因此所有 agreement / κ 数字都是**下界**，不能当 Gate 数。
要拿无偏估计，必须从**全体候选**里随机抽。

关键实现点：**不能只在已有 review_items 里抽**——那 309 条本身就是按优先级挑出来的，
在里面抽样仍是偏的。正确做法是从全部 ok 候选里随机抽，缺 review_item 的就补建一条。

用法：
    python scripts/make_random_batch.py --n 25            # 抽 25 条，打 batch_r25
    python scripts/make_random_batch.py --n 25 --dry-run  # 只看抽到什么，不写库
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import BLIND_REVIEW_PROMPT_VERSIONS
from app.models import Candidate, ReviewItem, Segment


# ── 语料水印伪影检测（2026-09-17）──────────────────────────────
# 盗版 txt 会把随机几个汉字换成拼音（"此nv"=此女、"敬畏之sè"=之色），
# 有的还夹站点水印（"小-说-t-xt-天.堂"）。**受影响的只有人类那一侧**——
# 候选是从语义帧重建的，乱码自然被消掉 → 这类题是不公平比较（人类侧带乱码、
# 候选侧干净），集霸会因乱码判候选赢，与文笔无关。
# 实测占比：琼明 0/130（精校版干净）、将夜 3/50、凡人 3/50、斗罗 2/50。
_LATIN = re.compile(r"[A-Za-z]")
_ACCENT = re.compile(r"[āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]")
# 站点水印会**插在句子中间**，用非汉字符号/书名号断字：`青年电~脑}访整理神色一狞`
# （2026-09-18 实测：一个这样的段污染了 26 条劣化变体）。合法中文里 `~ } {` 与汉字
# 相邻几乎不出现（引号、括号、破折号是成对合法符号，故不入列）。
_WM_PUNCT = re.compile(r"[一-鿿][~{}]|[~{}][一-鿿]")


def looks_watermarked(text: str) -> bool:
    """人类段是否带 txt 水印伪影。真阳性示例：`朱红sè的大门`、`学府én下`、`青年电~脑}访整理`。"""
    t = text or ""
    return (bool(_ACCENT.search(t)) or len(_LATIN.findall(t)) >= 2
            or bool(_WM_PUNCT.search(t)))


def extras_start(s, work_id: str, seg_version: int | None = None) -> int | None:
    """该作品「番外区」的起始 ordinal；没有番外则 None。

    识别依据：**以「番外」二字开头的标题段**（标题通常独立成段）。
    琼明实测 15 个这样的段，最早 ordinal 18308 / 全文 20533（末 10.8%，与作者
    自己在 18299 处写的「应该没有番外了」一致）。将夜/凡人/斗罗均无。
    集霸 2026-09-17 定的策略：**只排番外，其余照抽**。

    seg_version 按调用方给的版本过滤（None 即匹配无版本号的段）——
    同一作品混存 v1/v2 时两套 ordinal 不可比。
    """
    from sqlalchemy import func
    row = (s.query(func.min(Segment.ordinal))
           .filter(Segment.work_id == work_id, Segment.seg_version == seg_version,
                   func.ltrim(Segment.text).like("番外%")).first())
    return row[0] if row and row[0] is not None else None

EXP = "EXP-0911-B82D"


def build_batch(s, exp: str, n: int, tag: str, seed: int,
                dry_run: bool = False, pool: str = "all",
                by: str = "candidate", min_human_chars: int = 0,
                exclude_batches: tuple[str, ...] | None = None,
                exclude_dirty: bool = True,
                stratify_position: bool = False,
                exclude_extras: bool = True) -> dict:
    """从可盲评的 ok 候选里随机抽 n 条并打 batch 标签；缺 review_item 的补建。

    两条核心不变量（回归测试锁定）：
    1. 抽样池 = 全部 ok 候选，**不是**已有 review_items。
       后者本身按优先级挑出来的（human_upset 过采样），在其中抽样仍是偏样本。
    2. 抽样池只含**平行文本**候选（`BLIND_REVIEW_PROMPT_VERSIONS`）。
       消融条件 `recon_ctxonly_v1` 是自由续写，与人类段不是同一内容，
       混进来会让盲评变成"牛头不对马嘴"（2026-09-14 实测事故）。

    pool（2026-09-16 加）：
      - "all"（默认，行为不变）：池 = 全部可盲评 ok 候选。抽到的题若**已判过**，
        沿用旧判定不重判（故实际新增判定数 < n）——这是既有设计。
      - "unjudged"：池 = 尚未判定的候选。用途：**要 n 条全新判定**补少数类时，
        抽已判过的题不产生新信息。⚠ 这一框是"被历次挑题筛剩的部分"，
        解释时**不要**当成全体候选的无偏样本（h30 已证收割批会把高分题抽走 → 撇脂）。

    by（2026-09-16 加，r50 的教训）：
      - "candidate"（默认，行为不变）：按**候选**抽样。同一个人类段落有多个候选时
        会被抽中多次 → 集霸会反复看到同一段人类原文（r50 实测：50 题只覆盖 25 段，
        他当场发问"怎么段落没变"）。
      - "segment"：按**段落**抽样，每段只端 1 个候选 → n 条题 = n 个不同人类段落。
        需要≥n 个不同段落才够。**要"更多不同的人类文本"时用这个**；
        想在固定段落上多测几个候选时用 "candidate"。
    """
    cands = s.query(Candidate).filter_by(experiment_id=exp, status="ok").all()
    cands = [c for c in cands
             if c.prompt_version in BLIND_REVIEW_PROMPT_VERSIONS
             and c.text and len(c.text.strip()) >= 20]
    # §14 隔离硬闸（2026-09-19）：role='benchmark' 段上的候选**永不进盲评池**——
    # 端给集霸判一次，隐藏基准就不再是 hidden。此前靠"别把基准实验指给本脚本"
    # 的自觉，按白名单纪律落成硬闸；被拦下的量显式打印，不许静默（纪律④）。
    if cands:
        _roles = {r.id: r.role for r in
                  s.query(Segment).filter(
                      Segment.id.in_({c.segment_id for c in cands})).all()}
        _blocked = [c for c in cands if _roles.get(c.segment_id) == "benchmark"]
        if _blocked:
            print(f"[§14 隔离] 拦下 {len(_blocked)} 条基准段候选，不进盲评池"
                  f"（experiment={exp}）")
            cands = [c for c in cands if _roles.get(c.segment_id) != "benchmark"]
    if pool == "unjudged":
        done_ids = {r.subject_id for r in
                    s.query(ReviewItem).filter_by(experiment_id=exp).all()
                    if r.status == "done"}
        cands = [c for c in cands if c.id not in done_ids]
    if exclude_batches:
        # 排除「**段落**已出现在这些批次里」的候选。`--by segment` 只在候选池里
        # 一段取一条，**不排除这段已经出过**——2026-09-16 实测：qmr12 的 12 段
        # 全部落在已被用过的段落上，其中 6 段就是集霸 40 分钟前刚判过的。
        want_all = "*" in exclude_batches
        used_ids = set()
        for r in s.query(ReviewItem).all():
            rs = r.reasons or []
            hit = (any(x.startswith("batch_") for x in rs) if want_all
                   else any(f"batch_{t}" in rs for t in exclude_batches))
            if hit and r.subject_type == "candidate":
                used_ids.add(r.subject_id)
        if used_ids:
            used_segs = {c.segment_id for c in
                         s.query(Candidate).filter(Candidate.id.in_(used_ids)).all()}
            cands = [c for c in cands if c.segment_id not in used_segs]
    if min_human_chars > 0 or exclude_dirty or stratify_position or exclude_extras:
        seg_ids = {c.segment_id for c in cands}
        seg_obj = {r.id: r for r in
                   s.query(Segment).filter(Segment.id.in_(seg_ids)).all()}
        if exclude_extras:
            # 排除番外区（集霸 2026-09-17：番外篇价值不大）。边界按作品各自算。
            starts: dict[str, int | None] = {}
            for seg in seg_obj.values():
                if seg.work_id not in starts:
                    starts[seg.work_id] = extras_start(s, seg.work_id, seg.seg_version)
            cands = [c for c in cands
                     if not (starts.get(seg_obj[c.segment_id].work_id) is not None
                             and seg_obj[c.segment_id].ordinal
                             >= starts[seg_obj[c.segment_id].work_id])]
        if min_human_chars > 0:
            # 人类段太短时"哪边写得更好"会退化成荒谬比较（实测 0914 四语料里
            # 将夜有 17/50 段不足 30 字、最短 6 字，而候选普遍 90 字上下）。
            # B82D 的段落全部 ≥40 字 —— 用 40 作门槛可让跨语料数据与既有数据可比。
            cands = [c for c in cands
                     if len(seg_obj[c.segment_id].text.strip()) >= min_human_chars]
        if exclude_dirty:
            # 水印伪影只污染人类侧（候选从帧重建、乱码被消掉）→ 会制造
            # "人类带乱码 vs 候选干净"的不公平比较。默认排除。
            cands = [c for c in cands
                     if not looks_watermarked(seg_obj[c.segment_id].text)]
    cands.sort(key=lambda c: c.id)          # 固定顺序，保证种子可复现
    rng = random.Random(seed)

    if by == "segment":
        by_seg: dict[str, list] = {}
        for c in cands:
            by_seg.setdefault(c.segment_id, []).append(c)
        segs = sorted(by_seg)               # 固定顺序，保证种子可复现
        if stratify_position:
            # 按**全书位置**的十分位分层轮流取（不按比例），保证覆盖全书。
            # 起因（2026-09-17 集霸发问"怎么都是靠后的内容"）：之前几批几乎没碰
            # 最后 10%（末位十分位仅 1%，预期 10%），剩下的可抽池在末尾富集，
            # 于是新抽的样本跟着偏后（末两位十分位各 14%）。纯随机抽每次都在赌运气，
            # 分层抽则把"覆盖全书"变成构造性保证。
            tot = {}
            for seg in seg_obj.values():
                if seg.work_id not in tot:
                    tot[seg.work_id] = s.query(Segment).filter_by(
                        work_id=seg.work_id, seg_version=seg.seg_version).count()
            buckets: dict[int, list] = {}
            for sid in segs:
                seg = seg_obj[sid]
                rel = (seg.ordinal / tot[seg.work_id]) if tot.get(seg.work_id) else 0.0
                buckets.setdefault(min(9, int(rel * 10)), []).append(sid)
            picked_segs, keys, exhausted = [], sorted(buckets), set()
            while len(picked_segs) < min(n, len(segs)) and len(exhausted) < len(keys):
                for k in keys:
                    if len(picked_segs) >= min(n, len(segs)):
                        break
                    if buckets[k]:
                        picked_segs.append(buckets[k].pop(rng.randrange(len(buckets[k]))))
                    else:
                        exhausted.add(k)
        else:
            picked_segs = rng.sample(segs, min(n, len(segs)))
        picked = [rng.choice(sorted(by_seg[s], key=lambda c: c.id))
                  for s in picked_segs]     # 每段恰好 1 个候选
        n_units = len(segs)
    else:
        picked = rng.sample(cands, min(n, len(cands)))
        n_units = len(cands)

    # 入样概率：等概率抽样 → 权重全同（自加权）。写进 reasons 只为留档，
    # 顺带让 IPW 工具不缺字段（等权时加权结果 = 原始结果，不会改变读数）。
    # by="segment" 时单位是段落：w = 抽中段落数 / 池内段落数。
    p_inc = (len(picked) / n_units) if n_units else 0.0

    existing = {r.subject_id: r for r in
                s.query(ReviewItem).filter_by(experiment_id=exp).all()}
    already = sum(1 for c in picked if c.id in existing)
    made = 0
    if not dry_run:
        for c in picked:
            r = existing.get(c.id)
            if r is None:
                r = ReviewItem(experiment_id=exp, subject_type="candidate",
                               subject_id=c.id, priority=0.0,
                               reasons=["random_sample"], status="pending")
                s.add(r)
                made += 1
            keep = [x for x in (r.reasons or []) if not x.startswith(("stratum:", "w:"))]
            if f"batch_{tag}" not in keep:
                keep = keep + [f"batch_{tag}"]
            r.reasons = keep + ["stratum:RANDOM", f"w:{max(p_inc, 1e-6):.4f}"]
        s.commit()
    return {"pool": len(cands), "picked": picked, "already_queued": already,
            "created": made, "dry_run": dry_run, "p_inc": p_inc, "pool_mode": pool,
            "by": by, "n_units": n_units,
            "n_segments": len({c.segment_id for c in picked})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default=EXP)
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--tag", default="r25")
    ap.add_argument("--seed", type=int, default=20260914)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pool", choices=("all", "unjudged"), default="all",
                    help="all=全部候选（默认，抽到已判的沿用旧判定）；"
                         "unjudged=只抽没判过的（要 n 条全新判定时用）")
    ap.add_argument("--by", choices=("candidate", "segment"), default="candidate",
                    help="candidate=按候选抽（默认；同段多候选会重复端出同一段人类原文）；"
                         "segment=按段落抽，每段只出 1 题（要 n 个不同人类段落时用这个）")
    ap.add_argument("--min-human-chars", type=int, default=0,
                    help="人类段最小字数；0=不过滤。跨语料建议 40 —— "
                         "太短的段（0914 四语料里最短 6 字）会让偏好比较退化，"
                         "且 B82D 既有段落全部 ≥40 字，用 40 才与既有数据可比")
    ap.add_argument("--exclude-batch", default=None,
                    help="排除「段落已出现在这些批次里」的候选，逗号分隔；"
                         "用 * 表示任何批次（= 只要从未出过的新段落）")
    ap.add_argument("--keep-dirty-text", action="store_true",
                    help="保留带 txt 水印伪影的人类段（默认排除，见 looks_watermarked）")
    ap.add_argument("--stratify-position", action="store_true",
                    help="按全书位置的十分位分层抽段（保证覆盖全书；默认纯随机）")
    ap.add_argument("--keep-extras", action="store_true",
                    help="保留番外区段落（默认排除；见 extras_start）")
    args = ap.parse_args()
    exclude = tuple(t.strip() for t in (args.exclude_batch or "").split(",") if t.strip())

    db.init_db()
    with db.session() as s:
        info = build_batch(s, args.exp, args.n, args.tag, args.seed, args.dry_run,
                           pool=args.pool, by=args.by,
                           min_human_chars=args.min_human_chars,
                           exclude_batches=exclude or None,
                           exclude_dirty=not args.keep_dirty_text,
                           stratify_position=args.stratify_position,
                           exclude_extras=not args.keep_extras)
        if not args.dry_run:
            picked = info["picked"]
            judged = sum(1 for c in picked
                         if (existing := s.query(ReviewItem).filter_by(
                             experiment_id=args.exp, subject_id=c.id).first())
                         and existing.human_verdict)

    unit = "段落" if info["by"] == "segment" else "候选"
    print(f"[{args.exp}] 抽样池：{info['pool']} 条合格候选 / {info['n_units']} 个{unit}"
          f"（pool={info['pool_mode']}，by={info['by']}）")
    print(f"  抽 {len(info['picked'])} 题（seed={args.seed}）"
          f"→ 覆盖 {info['n_segments']} 个不同人类段落"
          f"{'（每段 1 题）' if info['by']=='segment' else ''}")
    print(f"  入样概率 w = {info['p_inc']:.4f}"
          f"{'（= 全部段落都抽到，本批即普查）' if info['p_inc'] >= 0.999 else '（等概率 → 自加权）'}")
    print(f"  其中已有 review_item：{info['already_queued']} 条（若已判过则直接计入，无需重判）")
    print(f"  需补建 review_item：{len(info['picked']) - info['already_queued']} 条")
    if args.dry_run:
        for c in info["picked"]:
            print(f"    {c.id}  {len(c.text):4d}字  {c.segment_id}")
        print("\ndry-run：未写库")
        return
    print(f"  已补建 {info['created']} 条；已为 {len(info['picked'])} 条打上 batch_{args.tag}")
    print(f"  该批已判 {judged} 条")
    print(f"\n前端：http://127.0.0.1:8787/?batch={args.tag}  （或改 index.html 默认值）")
    print()
    print("随机批**没有外推承诺**可对照（这是它的优点）：判完后")
    print(f"  python scripts/heldout_eval.py --batch {args.tag}      # 评委 κ + 必报基线")
    print(f"  python scripts/anchor_check.py --batch {args.tag}      # 实测池率 + Wilson CI")
    print("  实测率本身就是池估计（自加权），不需要 IPW 还原。")


if __name__ == "__main__":
    main()
