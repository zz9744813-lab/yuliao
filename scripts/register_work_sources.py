"""K1-A 来源登记（知识化方案 §4.1）：为既有 Work 增量登记来源身份。

## 登记什么（§4.1 字段清单）

canonical_work_id / 已核对的 author_id / genre_ids / 来源类型 /
文本版本·哈希 / 用途依据 / 允许用途 / 元数据核对状态与依据。
落在 **work_sources 扩展关联表**（authors / genres 词表配套）——
迁移不改旧 ID，works 表零改动。

## 纪律（方案原文）

- corpus v2 镜像 / 重切段 / 清洗副本回连**同一根作品**（canonical_work_id
  = 根的 work_id），不计作独立复现；
- fixture / synthetic / commentary / human_fiction 分型——测试材料可验
  契约，不给人类来源计数加分；
- src_ok=true 只证源检查通过，不替代作者身份、用途或质量证明；
- 作者/题材**可考据才填**：核对依据写进 authors.verified_basis 与
  metadata_basis；无可靠考据（如《琼明神女录》的作者）显式留空 +
  metadata_status=partial，不许猜。

## 隔离规则（K2 发现/复现样本的前置契约，一并冻结于此）

发现与复现样本的基准隔离按「**源身份 + 文本版本 + 目标与上下文区间**」
三查执行，缺一不可：
①源身份：样本的根作品在 work_sources 里分型且用途允许（镜像/fixture 不
  计入人类证据）；
②文本版本：样本引用的段属 text_version 一致的行（v1 样本不许冒 v2）；
③区间：目标与上下文文本对全库冻结基准哈希族（整段 + 组成段落，
  scripts/export_training.py::_bench_hashes / verify_bal_universe.py 口径）
  比对——只查 role 或整段哈希不够（A03/A11 教训）。

## 用法

    python scripts/register_work_sources.py --dry-run   # 只报告将登记什么
    python scripts/register_work_sources.py             # 幂等登记（重跑更新）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                # noqa: E402
from app.models import Author, Genre, Segment, Work, WorkSource  # noqa: E402

# 已核对作者（核对依据：作品标题与来源文件名中的署名一致 + 公开通行署名，
# 人工核对 2026-09-21）。不可考据的作者**不在此列**——登记时留空。
_AUTHORS = {
    "猫腻": "标题与来源文件名署名一致（将夜_猫腻），公开通行署名，人工核对 2026-09-21",
    "忘语": "标题与来源文件名署名一致（凡人修仙传_忘语），公开通行署名，人工核对 2026-09-21",
    "唐家三少": "标题与来源文件名署名一致（斗罗大陆_唐家三少），公开通行署名，人工核对 2026-09-21",
}
_GENRES = {"玄幻", "仙侠"}

# 根作品登记（key=作品标题）。genre 分配依据：公开书目的通行分类（人工核对）。
_ROOT_META = {
    "将夜（猫腻）": {"author": "猫腻", "genres": ["玄幻"],
                 "basis": "作者与题材均有公开通行署名/分类，人工核对 2026-09-21"},
    "凡人修仙传（忘语）": {"author": "忘语", "genres": ["仙侠"],
                     "basis": "作者与题材均有公开通行署名/分类，人工核对 2026-09-21"},
    "斗罗大陆（唐家三少）": {"author": "唐家三少", "genres": ["玄幻"],
                      "basis": "作者与题材均有公开通行署名/分类，人工核对 2026-09-21"},
    "琼明神女录（精校）": {"author": None, "genres": [],
                     "basis": "作者与题材无可靠考据来源——显式留空待补（不许猜），"
                              "人工核对 2026-09-21"},
}


def _is_fixture(w: Work) -> bool:
    return (w.source or "").startswith("inbox:fixture") \
        or (w.title or "").startswith("fixture")


def _work_sha256(work_id: str) -> tuple[str | None, int]:
    """内容锚：按 ordinal 序拼接段 text_clean（缺失用 text）的 sha256。"""
    h = hashlib.sha256()
    n = 0
    with db.session() as s:
        segs = (s.query(Segment).filter(Segment.work_id == work_id)
                .order_by(Segment.ordinal).all())
        for seg in segs:
            h.update(((seg.text_clean or seg.text or "") + "\n").encode("utf-8"))
            n += 1
    return (h.hexdigest() if n else None), n


def register(dry_run: bool = False, only: set[str] | None = None) -> list[dict]:
    """only：只处理这些 work_id（测试用作用域）；None=全部（生产口径——
    全覆盖是登记的契约，未知根作品标题必须响亮失败）。"""
    out: list[dict] = []
    with db.session() as s:
        # 词表（幂等）
        name2author: dict[str, str] = {}
        for name, basis in _AUTHORS.items():
            a = s.query(Author).filter_by(name=name).first()
            if a is None:
                a = Author(name=name, verified_basis=basis)
                s.add(a)
                s.flush()
            name2author[name] = a.id
        name2genre: dict[str, str] = {}
        for gname in _GENRES:
            g = s.query(Genre).filter_by(name=gname).first()
            if g is None:
                g = Genre(name=gname)
                s.add(g)
                s.flush()
            name2genre[gname] = g.id

        works = s.query(Work).all()
        if only is not None:
            works = [w for w in works if w.id in only]
        rows = []
        for w in works:
            if _is_fixture(w):
                row = dict(
                    work_id=w.id, canonical_work_id=w.id, author_id=None,
                    genre_ids=[], source_type="fixture",
                    text_version="test-fixture",
                    purpose_basis="测试夹具：只验管线契约，不给人类来源计数加分",
                    allowed_purposes=["test_contract"],
                    metadata_status="verified",
                    metadata_basis=f"夹具身份明确（source={w.source}），非人类语料")
            elif w.v2_of:
                root = s.get(Work, w.v2_of)
                row = dict(
                    work_id=w.id, canonical_work_id=w.v2_of, author_id=None,
                    genre_ids=[], source_type="human_fiction",
                    text_version="corpus-v2-mirror",
                    purpose_basis="corpus v2 校勘镜像：回连根作品"
                                  f"（{root.title if root else w.v2_of}）与内容区间，"
                                  "不计独立复现；用途限研究对照",
                    allowed_purposes=["research_reference"],
                    metadata_status="partial",
                    metadata_basis="镜像身份由 works.v2_of 血缘键确立；"
                                   "作者/题材继承根作品（对账见 verify_work_registry.py）")
            else:
                meta = _ROOT_META.get(w.title)
                if meta is None:
                    raise SystemExit(f"未登记元数据的根作品：{w.title!r}——"
                                    "先在 _ROOT_META 里补齐核对依据（可考据才填）")
                row = dict(
                    work_id=w.id, canonical_work_id=w.id,
                    author_id=name2author.get(meta["author"]),
                    genre_ids=[name2genre[g] for g in meta["genres"]],
                    source_type="human_fiction", text_version="corpus-v1",
                    purpose_basis="人类长篇小说语料：表达研究 / 训练源段 / "
                                  "基准源段的根作品（role 闸与内容级隔离由 "
                                  "export_training / verify 链承担）",
                    allowed_purposes=["research", "training_source",
                                      "benchmark_source"],
                    metadata_status="verified" if meta["author"] else "partial",
                    metadata_basis=meta["basis"])
            rows.append(row)

        if dry_run:
            return rows

        for row in rows:
            sha, n_segs = _work_sha256(row["work_id"])
            row = {**row, "text_sha256": sha, "n_segments": n_segs}
            src = s.query(WorkSource).filter_by(work_id=row["work_id"]).first()
            if src is None:
                s.add(WorkSource(**{k: v for k, v in row.items()
                                   if k != "n_segments"}))
            else:
                for k, v in row.items():
                    if k != "n_segments":
                        setattr(src, k, v)
            out.append(row)
        s.commit()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="只报告将登记什么")
    args = ap.parse_args()
    db.init_db()
    rows = register(dry_run=args.dry_run)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    n_human_roots = sum(1 for r in rows
                        if r["source_type"] == "human_fiction"
                        and r["canonical_work_id"] == r["work_id"])
    print(f"[register_work_sources] 登记 {len(rows)} 部 Work；"
          f"独立人类源（仅根作品，镜像/fixture 不计）= {n_human_roots}")


if __name__ == "__main__":
    main()
