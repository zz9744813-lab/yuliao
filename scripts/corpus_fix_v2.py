"""corpus v2 产出器 —— 源文本缺字修复的版本化方案（T-CORPUS-V2，2026-09-20）。

背景：扩产池耗尽后盘点源文本，斗罗/将夜的系统性错字（app/typo_map 三条
频次自洽规则）在 v1 里只修了 text_clean 层。本脚本产出 **corpus v2**：
受影响作品各建一个新 Work，段 1:1 镜像，text = TYPO_MAP 修复后的清洗文本，
**v1 一律不覆盖**（原文、text_clean、帧、标注、基准冻结全部原样）。

产出物：
· 新 Work（title = 原题 + "（corpus v2）"），note 记录来源与替换统计；
· v2 段的 integrity JSON 里带 "corpus_v2_source": <v1 segment_id>（重绑帧/标注
  时的映射锚点），同时落映射文件 data/exports/corpus_v2_map.jsonl；
· 幂等：v2 Work 已存在时跳过（--force 也不重跑，防重复镜像）。

⚠ 抽样注意：v2 段 role=None，会进训练采样池——v1/v2 同文并存，实验建批时
必须用 work_ids 明确选边（否则同一内容双份入池）。goldpick/建批工具按
标题含"（corpus v2）"排除 v2 的默认行为另行决定。

⚠ text / text_clean 口径：v2 的 text = v1.text_clean 经 TYPO_MAP 后的文本，
v2 的 text_clean 随行携带 v1 的 text_clean——两者内容一致（v1 的 text_clean
已先被 normalize_typos 修复过），但请下游统一读 `text`，避免口径漂移。
未来 v2 上重跑 clean_text/normalize 时 text_clean 才会与 text 再次分层。

用法：
    python scripts/corpus_fix_v2.py --scan                # 受影响作品的命中统计
    python scripts/corpus_fix_v2.py --build               # 产出 corpus v2（幂等）
    python scripts/corpus_fix_v2.py --build --works 斗罗大陆  # 只做指定作品
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.models import Segment, Work  # noqa: E402
from app.typo_map import V2_TITLE_SUFFIX as V2_SUFFIX  # noqa: E402
from app.typo_map import apply as typo_apply  # noqa: E402
from app.typo_map import hits as typo_hits  # noqa: E402
# 频次自洽确认过错字的作品（typo_map 的证据来源：docs/typo-normalization-20260919.md）
AFFECTED = ("斗罗大陆", "将夜")
MAP_PATH = ROOT / "data" / "exports" / "corpus_v2_map.jsonl"


def _works(s, only: tuple[str, ...] | None) -> list[Work]:
    out = []
    for w in s.query(Work).all():
        if V2_SUFFIX in (w.title or ""):
            continue                      # v2 的 v2 不存在
        title = w.title or ""
        if only and not any(k in title for k in only):
            continue
        if not any(k in title for k in AFFECTED):
            continue                      # 未受错字影响的作品不镜像
        out.append(w)
    return out


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
        for w in works:
            v2_title = f"{w.title}{V2_SUFFIX}"
            exists = (s.query(Work)
                      .filter(Work.title == v2_title).first())
            if exists:
                skipped.append({"work": w.title, "v2": v2_title,
                                "reason": "v2 已存在（幂等跳过）"})
                continue
            n_seg = n_repl = n_integ_unparsed = 0
            w2 = Work(title=v2_title, author=w.author, source=w.source,
                      note=f"corpus v2 of「{w.title}」：TYPO_MAP 修复版"
                           f"（app/typo_map；v1 原样保留）")
            s.add(w2)
            s.flush()
            for seg in s.query(Segment).filter(Segment.work_id == w.id).all():
                base = seg.text_clean or seg.text
                new_text, k = typo_apply(base)
                integ = None
                if seg.integrity:
                    try:
                        integ = json.loads(seg.integrity)
                        integ["corpus_v2_source"] = seg.id
                        integ = json.dumps(integ, ensure_ascii=False)
                    except Exception:
                        # 会审要求：不许静默丢锚——保留原值但计数告警，
                        # 该段 v2 重绑帧/标注时需人工补映射
                        integ = seg.integrity
                        n_integ_unparsed += 1
                        print(f"[warn] integrity 非 JSON，未写 corpus_v2_source："
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
            created.append({"work": w.title, "v2": v2_title,
                            "segments": n_seg, "replacements": n_repl,
                            "integrity_unparsed": n_integ_unparsed})
        s.commit()
        if map_rows:
            map_file.parent.mkdir(parents=True, exist_ok=True)
            with map_file.open("a", encoding="utf-8") as f:
                for r in map_rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    out = {"created": created, "skipped": skipped,
           "map_rows": len(map_rows), "map_path": str(map_file)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="corpus v2：TYPO_MAP 修复的版本化产出")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--works", default="", help="逗号分隔的作品名过滤（默认全部受影响作品）")
    args = ap.parse_args()
    db.init_db()
    only = tuple(k for k in args.works.split(",") if k) or None
    if args.scan:
        scan(only)
    elif args.build:
        build(only)
    else:
        print("用 --scan 或 --build（见 --help）")


if __name__ == "__main__":
    main()
