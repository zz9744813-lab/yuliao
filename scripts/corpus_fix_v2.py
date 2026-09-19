"""corpus v2 产出器 —— 源文本缺字修复的版本化方案（T-CORPUS-V2，2026-09-20）。

背景：扩产池耗尽后盘点源文本，斗罗/将夜的系统性错字（app/typo_map 三条
频次自洽规则）在 v1 里只修了 text_clean 层。本脚本产出 **corpus v2**：
受影响作品各建一个新 Work，段 1:1 镜像，text = TYPO_MAP 修复后的清洗文本，
**v1 一律不覆盖**（原文、text_clean、帧、标注、基准冻结全部原样）。

产出物：
· 新 Work（title = 原题 + "（corpus v2）"，**v2_of = v1 Work.id**），note 记录来源与替换统计；
· v2 段的 integrity JSON 里带 "corpus_v2_source": <v1 segment_id>（重绑帧/标注
  时的映射锚点），同时落映射文件 data/exports/corpus_v2_map.jsonl；
· 幂等键 = Work.v2_of（由 v1 work.id 派生的稳定血缘键），**不认标题**：
  同名多 Work 各自镜像自己的 v2，不会被标题键静默丢弃；
· v2 段 role 恒为 None（不继承 v1 的 benchmark/gold——同一内容双份入池/入 gold）。

⚠ 存量口径：本脚本只保证"以后产出的 v2 干净"，历史上已入库的 v2 行要靠
`--backfill` 收口（回填 v2_of 稳定键 + 把 v2 段继承来的 role 显式清零）。
默认 dry-run 只报告，`--backfill --apply` 才写库。

⚠ 抽样/建批口径：v2 段**永不参与**基准评选与训练采样，基准只在 v1 侧维护。
排除逻辑已实际接上（判定统一走 app.models.exclude_corpus_v2_segments /
is_corpus_v2_work）：app/near_dup 的训练采样域与基准切分、scripts/scale_corpus.pick、
scripts/goldpick_build、scripts/export_training、scripts/benchmark_build。

⚠ text / text_clean 口径：v2 的 text = v1.text_clean 经 TYPO_MAP 后的文本，
v2 的 text_clean 随行携带 v1 的 text_clean——两者内容一致（v1 的 text_clean
已先被 normalize_typos 修复过），但请下游统一读 `text`，避免口径漂移。
未来 v2 上重跑 clean_text/normalize 时 text_clean 才会与 text 再次分层。

用法：
    python scripts/corpus_fix_v2.py --scan                # 受影响作品的命中统计
    python scripts/corpus_fix_v2.py --build               # 产出 corpus v2（幂等）
    python scripts/corpus_fix_v2.py --build --works 斗罗大陆  # 只做指定作品
    python scripts/corpus_fix_v2.py --backfill            # 存量收口：只报告
    python scripts/corpus_fix_v2.py --backfill --apply    # 存量收口：写库
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.models import Segment, Work, is_corpus_v2_work  # noqa: E402
from app.typo_map import V2_TITLE_SUFFIX as V2_SUFFIX  # noqa: E402
from app.typo_map import apply as typo_apply  # noqa: E402
from app.typo_map import hits as typo_hits  # noqa: E402

# 频次自洽确认过错字的作品（typo_map 的证据来源：docs/typo-normalization-20260919.md）
AFFECTED = ("斗罗大陆", "将夜")
MAP_PATH = ROOT / "data" / "exports" / "corpus_v2_map.jsonl"
V2_ANCHOR_KEY = "corpus_v2_source"       # v2 段 integrity 里指向 v1 segment_id 的锚
_ID_CHUNK = 500                          # SQLite 变量数上限下的安全批


def _works(s, only: tuple[str, ...] | None) -> list[Work]:
    out = []
    for w in s.query(Work).all():
        if is_corpus_v2_work(w):
            continue                      # v2 的 v2 不存在
        title = w.title or ""
        if only and not any(k in title for k in only):
            continue
        if not any(k in title for k in AFFECTED):
            continue                      # 未受错字影响的作品不镜像
        out.append(w)
    return out


def _v1_of_v2_work(s, v2: Work) -> str | None:
    """靠段上的 corpus_v2_source 锚反查这本 v2 的 v1 Work.id（不靠标题，抗同名多 Work）。

    锚点解析不到、或指向多个 v1 Work（血缘不唯一）时返回 None：宁缺不猜。
    """
    src_ids: set[str] = set()
    for (integ,) in (s.query(Segment.integrity)
                     .filter(Segment.work_id == v2.id,
                             Segment.integrity.like(f'%"{V2_ANCHOR_KEY}"%')).all()):
        try:
            src = json.loads(integ or "{}").get(V2_ANCHOR_KEY)
        except Exception:
            continue
        if src:
            src_ids.add(src)
    if not src_ids:
        return None
    v1_works: set[str] = set()
    ids = sorted(src_ids)
    for i in range(0, len(ids), _ID_CHUNK):
        for (wid,) in (s.query(Segment.work_id)
                       .filter(Segment.id.in_(ids[i:i + _ID_CHUNK])).all()):
            if wid != v2.id:
                v1_works.add(wid)
        if len(v1_works) > 1:             # 已经歧义，不必再查
            return None
    return next(iter(v1_works), None)


def _find_existing_v2(s, v1: Work) -> Work | None:
    """幂等查找：先认 v2_of 稳定键；历史行（回填前）用「标题 + 锚点反查」双重确认血缘后采纳。

    只按标题命中是危险的（同名多 Work 会互相冒认），所以标题候选必须再经
    `_v1_of_v2_work` 证明"这本 v2 确实镜像自 v1"才采纳；证明不了就不认定、照常新建。
    """
    hit = s.query(Work).filter(Work.v2_of == v1.id).first()
    if hit:
        return hit
    for c in (s.query(Work).filter(Work.title == f"{v1.title}{V2_SUFFIX}").all()):
        if c.v2_of == v1.id:
            return c
        if c.v2_of is None and _v1_of_v2_work(s, c) == v1.id:
            c.v2_of = v1.id               # 采纳历史行并补齐稳定键
            return c
    return None


def scan(only: tuple[str, ...] | None = None) -> dict:
    """受影响作品的错字命中统计（不改动）。"""
    out = []
    with db.session() as s:
        for w in _works(s, only):
            n_seg = n_hit = 0
            by_rule: dict[str, int] = {}
            for seg in s.query(Segment).filter(Segment.work_id == w.id).all():
                n_seg += 1
                h = typo_hits(seg.text_clean or seg.text)
                if h:
                    n_hit += 1
                    for k, v in h.items():
                        by_rule[k] = by_rule.get(k, 0) + v
            if n_hit:
                out.append({"work": w.title, "segments": n_seg,
                            "segments_with_hits": n_hit, "by_rule": by_rule})
    print(json.dumps({"affected": out}, ensure_ascii=False, indent=1))
    return {"affected": out}


def build(only: tuple[str, ...] | None = None,
          map_path: Path | None = None) -> dict:
    map_file = map_path or MAP_PATH
    with db.session() as s:
        works = _works(s, only)
        created = []
        skipped = []
        map_rows: list[dict] = []
        # 同名多 Work 是可核对的现场证据：稳定键下它们各自镜像，不再互相顶掉
        dup_titles = {t: n for t, n in
                      Counter(w.title for w in works).items() if n > 1}
        for w in works:
            v2_title = f"{w.title}{V2_SUFFIX}"
            exists = _find_existing_v2(s, w)
            if exists:
                skipped.append({"work": w.title, "v1_work": w.id,
                                "v2": exists.title, "v2_work": exists.id,
                                "reason": "v2 已存在（按 v2_of 稳定键幂等跳过）"})
                continue
            n_seg = n_repl = n_integ_unparsed = 0
            w2 = Work(title=v2_title, author=w.author, source=w.source,
                      v2_of=w.id,           # 幂等键：由 v1 work.id 派生，不用标题
                      note=f"corpus v2 of「{w.title}」：TYPO_MAP 修复版"
                           f"（app/typo_map；v1 原样保留）；v1_work={w.id}")
            s.add(w2)
            s.flush()
            for seg in s.query(Segment).filter(Segment.work_id == w.id).all():
                base = seg.text_clean or seg.text
                new_text, k = typo_apply(base)
                integ = None
                if seg.integrity:
                    try:
                        obj = json.loads(seg.integrity)
                        obj[V2_ANCHOR_KEY] = seg.id
                        integ = json.dumps(obj, ensure_ascii=False)
                    except Exception:
                        # 会审要求：不许静默丢锚——保留原值但计数告警，
                        # 该段 v2 重绑帧/标注时需人工补映射
                        integ = seg.integrity
                        n_integ_unparsed += 1
                        print(f"[warn] integrity 非 JSON，未写 {V2_ANCHOR_KEY}："
                              f"v1={seg.id}", flush=True)
                s.add(Segment(
                    work_id=w2.id, chapter=seg.chapter, ordinal=seg.ordinal,
                    text=new_text,                     # v2 正文 = 修复后清洗文本
                    text_clean=seg.text_clean,          # 清洗层随行携带（审计可对照）
                    n_sentences=seg.n_sentences, n_chars=len(new_text),
                    integrity=integ,
                    role=None,                          # 会审①：不继承 role——v1 的
                    # benchmark/gold 段若被 v2 原样继承，同一内容会双份入池/入 gold
                    seg_version=seg.seg_version,
                ))
                map_rows.append({"work": w.title, "v1_segment": seg.id,
                                 "ordinal": seg.ordinal})
                n_seg += 1
                n_repl += k
            w2.note = (w2.note or "") + f"；镜像 {n_seg} 段，替换 {n_repl} 处"
            created.append({"work": w.title, "v1_work": w.id, "v2": v2_title,
                            "v2_work": w2.id,
                            "segments": n_seg, "replacements": n_repl,
                            "integrity_unparsed": n_integ_unparsed})
        s.commit()
        if map_rows:
            map_file.parent.mkdir(parents=True, exist_ok=True)
            with map_file.open("a", encoding="utf-8") as f:
                for r in map_rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    out = {"created": created, "skipped": skipped,
           "same_title_v1_works": dup_titles,
           "map_rows": len(map_rows), "map_path": str(map_file)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def backfill(apply: bool = False) -> dict:
    """存量收口（会审①严重项）：给已入库的 v2 行补稳定键、清继承 role。

    ① v2_of：历史 v2 Work 的锚点（corpus_v2_source）反查出 v1 Work.id 后回填；
       血缘不唯一/查不到的一律列入 unresolved_lineage，不猜。
    ② role：v2 段上任何非空 role 显式清零（v2 永不参与基准评选，基准只在 v1 侧维护）。
       清零前逐本统计原有 role 分布，作为可核对的前后差异。
    默认 dry-run；`--apply` 才写库。
    """
    v2_works, cleared = [], Counter()
    with db.session() as s:
        for w in s.query(Work).all():
            if not is_corpus_v2_work(w):
                continue
            entry = {"v2_work": w.id, "v2": w.title,
                     "v2_of_before": w.v2_of, "v2_of_after": w.v2_of,
                     "segments": s.query(Segment).filter(Segment.work_id == w.id).count(),
                     "role_before": {}, "role_after": {}, "lineage": "resolved"}
            resolved = w.v2_of or _v1_of_v2_work(s, w)
            entry["v2_of_after"] = resolved
            if not resolved:
                entry["lineage"] = "unresolved"      # 血缘查不到/不唯一：只报不猜
            elif apply and not w.v2_of:
                w.v2_of = resolved
            prior = Counter(r[0] for r in s.query(Segment.role)
                            .filter(Segment.work_id == w.id,
                                    Segment.role.isnot(None)).all())
            entry["role_before"] = dict(prior)
            if prior:
                cleared.update(prior)
                if apply:
                    (s.query(Segment).filter(Segment.work_id == w.id)
                     .filter(Segment.role.isnot(None))
                     .update({Segment.role: None}, synchronize_session=False))
                    entry["role_after"] = {}
                    w.note = ((w.note or "") +
                              f"；[U0c backfill] v2 段 role 清零 {sum(prior.values())} 段"
                              f"（原 {dict(prior)}）——v2 永不参与基准评选")
                else:
                    entry["role_after"] = dict(prior)
            else:
                entry["role_after"] = {}
            v2_works.append(entry)
        if apply:
            s.commit()
    out = {"apply": apply, "v2_works": v2_works,
           "role_cleared_total": dict(cleared),
           "unresolved_lineage": [e["v2"] for e in v2_works
                                  if e["lineage"] != "resolved"]}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="corpus v2：TYPO_MAP 修复的版本化产出")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="存量 v2 行收口（补 v2_of 稳定键 + 清继承 role）；默认 dry-run")
    ap.add_argument("--apply", action="store_true", help="与 --backfill 同用才写库")
    ap.add_argument("--works", default="", help="逗号分隔的作品名过滤（默认全部受影响作品）")
    args = ap.parse_args()
    db.init_db()
    only = tuple(k for k in args.works.split(",") if k) or None
    if args.scan:
        scan(only)
    elif args.build:
        build(only)
    elif args.backfill:
        backfill(apply=args.apply)
    else:
        print("用 --scan / --build / --backfill（见 --help）")


if __name__ == "__main__":
    main()
