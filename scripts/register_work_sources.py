"""K1-A 来源登记（知识化方案 §4.1）：为既有 Work 增量登记来源身份。

## 登记什么（§4.1 字段清单）

canonical_work_id / 已核对的 author_id / genre_ids / 来源类型 /
文本版本·哈希 / 用途依据 / 允许用途 / 元数据核对状态与依据。
落在 **work_sources 扩展关联表**（authors / genres 词表配套）——
迁移不改旧 ID，works 表零改动。

## 纪律（方案原文）

- corpus v2 镜像 / 重切段 / 清洗副本回连**同一根作品**（canonical_work_id
  = 根的 work_id），不计作独立复现；镜像行显式复制根的 author/genre，
  verify_work_registry 逐行比对继承一致性；
- fixture / synthetic / commentary / human_fiction 分型——测试材料可验
  契约，不给人类来源计数加分；
- src_ok=true 只证源检查通过，不替代作者身份、用途或质量证明；
- 作者/题材**可考据才填**：核对依据写进 authors.verified_basis 与
  metadata_basis；无可靠考据（如《琼明神女录》的作者）显式留空 +
  metadata_status=partial，不许猜。

## 内容锚的漂移语义（会审 09-21 二轮 BLOCK 项）

text_sha256 是**登记时的内容指纹**：重跑发现库内容与登记不符 → 响亮
报错（内容漂移必须显式处理，不许静默重锚把漂移洗掉）；确需重锚
（如整个语料重新清洗）用 --reset-anchor 显式声明。0 段作品的锚为
NULL——不是缺失，是如实（空作品）。

## 隔离规则（K2 发现/复现样本的执行契约）

发现与复现样本的基准隔离按「**源身份 + 文本版本 + 目标与上下文区间**」
三查执行：①源身份与②文本版本由本登记与 verify_work_registry 承担
（分型/允许用途/text_version/内容锚）；③区间比对由既有基准哈希族
（export_training._bench_hashes：整段+组成段落）承担——**本脚本只
登记与对账，不在此执行区间检查**（那是 K2 发现管线的运行时检查）。

## 用法

    python scripts/register_work_sources.py --dry-run        # 只报告（含内容锚）
    python scripts/register_work_sources.py                  # 幂等登记（漂移即报）
    python scripts/register_work_sources.py --only WK-a,WK-b # 作用域登记
    python scripts/register_work_sources.py --reset-anchor   # 显式重锚（漂移时）
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
    "覆汉（榴弹怕水）": {"author": "榴弹怕水", "genres": ["架空历史"],
                    "basis": "源文件名自带署名（作者：榴弹怕水）且为公开通行"
                             "署名/分类的历史类网文，人工核对 2026-09-23"},
}
# 题材词表从根作品元数据派生（会审二轮：两处清单不许漂移）
_GENRES = {g for meta in _ROOT_META.values() for g in meta["genres"]}


def _is_fixture(w) -> bool:
    """fixture 判定唯一口径（register 与 verify 共用，会审二轮：不许两套）。"""
    return (w.source or "").startswith("inbox:fixture") \
        or (w.title or "").startswith("fixture")


def register(dry_run: bool = False, only: set[str] | None = None,
             reset_anchor: bool = False) -> list[dict]:
    """only：只处理这些 work_id（测试/局部补登）；None=全部（生产口径——
    全覆盖是契约，未知根作品标题响亮失败）。reset_anchor：内容漂移时
    显式重锚（默认漂移即报错——锚被随手改写就失去检测漂移的能力）。"""
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
        for gname in sorted(_GENRES):
            g = s.query(Genre).filter_by(name=gname).first()
            if g is None:
                g = Genre(name=gname)
                s.add(g)
                s.flush()
            name2genre[gname] = g.id

        works = s.query(Work).all()
        if only is not None:
            works = [w for w in works if w.id in only]
        # 根先于镜像（镜像行要复制根的 author/genre——继承是登记数据，
        # verify 逐行比对，不是口头承诺）
        works.sort(key=lambda w: 1 if w.v2_of else 0)

        root_meta_cache: dict[str, dict] = {}
        rows: list[dict] = []
        for w in works:
            if _is_fixture(w):
                row = dict(
                    work_id=w.id, canonical_work_id=w.id, author_id=None,
                    genre_ids=[], source_type="fixture",
                    text_version="test-fixture",
                    purpose_basis="测试夹具：只验管线契约，不给人类来源计数加分",
                    identity_purposes=["test_contract"], license_purposes=[],
                    metadata_status="verified",
                    metadata_basis=f"夹具身份明确（source={w.source}），非人类语料")
            elif w.v2_of:
                if w.v2_of == w.id:
                    raise SystemExit(f"自指镜像（v2_of==自身）：{w.id}——"
                                    "血缘键坏了，这行会虚增独立人类源")
                root = s.get(Work, w.v2_of)
                if root is None:
                    raise SystemExit(f"镜像 {w.id} 的 v2_of 指向不存在的作品 "
                                     f"{w.v2_of}——先登记根作品")
                # 继承源：本轮已处理的根行优先，否则回读根的**已登记行**
                # （--only 圈镜像不圈根时，静默拿 {} 抹空继承是会审三轮
                # 一般项——必须回库或响亮失败，不许产出继承字段被抹空的行）
                rmeta = root_meta_cache.get(w.v2_of)
                if rmeta is None:
                    root_src = s.query(WorkSource).filter_by(
                        work_id=w.v2_of).first()
                    if root_src is None:
                        raise SystemExit(
                            f"镜像 {w.id} 的根作品 {w.v2_of} 未登记——"
                            "--only 局部补登必须先圈入根（或全量重跑），"
                            "不许静默抹空继承字段")
                    rmeta = {"author_id": root_src.author_id,
                             "genre_ids": list(root_src.genre_ids or [])}
                row = dict(
                    work_id=w.id, canonical_work_id=w.v2_of,
                    # 继承是登记数据（会审二轮：不许留指向不存在检查的说明）
                    author_id=rmeta.get("author_id"),
                    genre_ids=list(rmeta.get("genre_ids") or []),
                    source_type="human_fiction",
                    text_version="corpus-v2-mirror",
                    purpose_basis="corpus v2 校勘镜像：回连根作品"
                                  f"（{root.title}）与内容区间，"
                                  "不计独立复现；署名继承根作品",
                    identity_purposes=["research_reference"], license_purposes=[],
                    metadata_status="partial",
                    metadata_basis="镜像身份由 works.v2_of 血缘键确立；"
                                   "author/genre 从根作品行复制（verify 逐行比对继承一致）")
            else:
                meta = _ROOT_META.get(w.title)
                if meta is None:
                    raise SystemExit(f"未登记元数据的根作品：{w.title!r}——"
                                    "先在 _ROOT_META 里补齐核对依据（可考据才填）；"
                                    "局部补登用 --only")
                row = dict(
                    work_id=w.id, canonical_work_id=w.id,
                    author_id=name2author.get(meta["author"]),
                    genre_ids=[name2genre[g] for g in meta["genres"]],
                    source_type="human_fiction", text_version="corpus-v1",
                    purpose_basis="署名可核对（作者/题材核对依据见上）——"
                                  "**不构成训练/基准用途授权**",
                    # 身份可核对面；授权面留空——训练/基准用途授权是集霸的
                    # 决策位（license_basis 须记授权人/日期/范围），未授权
                    # 前一律不得进训练/基准用途（会审二轮 BLOCK 项）
                    identity_purposes=["research"], license_purposes=[],
                    metadata_status="verified" if meta["author"] else "partial",
                    metadata_basis=meta["basis"])
                root_meta_cache[w.id] = row
            rows.append(row)

        if dry_run:
            out = []
            for row in rows:
                sha, n = _work_sha256(s, row["work_id"])
                out.append({**row, "text_sha256": sha, "n_segments": n})
            # 只读承诺的机制依据（会审三轮一般项）：db.session() 返回
            # SessionLocal()，`with` 退出调用 close()——Session close
            # **不提交**，未 commit 的 flush（词表行）随之回滚；
            # tests/test_work_registry.py::test_dry_run_shape_matches_
            # real_and_reads_only 用真实词表写入前后对比钉死这一语义。
            return out

        for row in rows:
            sha, n_segs = _work_sha256(s, row["work_id"])
            src = s.query(WorkSource).filter_by(work_id=row["work_id"]).first()
            if src is not None and src.text_sha256 is not None \
                    and src.text_sha256 != sha and not reset_anchor:
                raise SystemExit(
                    f"内容漂移：{row['work_id']} 的库内容与登记锚不符"
                    f"（登记 {src.text_sha256[:12]}… vs 现算 {sha[:12]}…）——"
                    "锚是登记时的事实，不许静默重写；确认漂移无害用 "
                    "--reset-anchor 显式重锚")
            # 会审三轮严重项：license_purposes/license_basis 由 register
            # 之外（集霸授权位）写入——幂等重登**只许在新建行时**携带，
            # 更新分支整体剔除，否则重登把外部授予的授权静默擦空
            # （basis 不在 row 里被留、purposes 被擦=孤儿态，且擦空后
            # verify 的授权闸静默失效——不可逆授权丢失）。
            if src is None:
                s.add(WorkSource(**{**row, "text_sha256": sha}))
            else:
                for k, v in row.items():
                    if k in ("license_purposes", "license_basis"):
                        continue        # 授权面字段：只由外部授权位修改
                    setattr(src, k, v)
                src.text_sha256 = sha
            row["text_sha256"] = sha
            row["n_segments"] = n_segs
        s.commit()
    return rows


def _work_sha256(s, work_id: str) -> tuple[str | None, int]:
    """内容锚：按 ordinal 序流式拼接段 text_clean（缺失用 text）的 sha256。

    在调用方 session 内读（会审二轮：另开 session 读同一 SQLite 有
    BUSY/快照风险）；yield_per 流式——10 万段级不整载。"""
    h = hashlib.sha256()
    n = 0
    q = (s.query(Segment.text_clean, Segment.text)
         .filter(Segment.work_id == work_id)
         .order_by(Segment.ordinal)
         .yield_per(500))
    for text_clean, text in q:
        h.update(((text_clean or text or "") + "\n").encode("utf-8"))
        n += 1
    return (h.hexdigest() if n else None), n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="只报告（含内容锚）")
    ap.add_argument("--only", default="",
                    help="逗号分隔 work_id——只登记这些（生产默认全部）")
    ap.add_argument("--reset-anchor", action="store_true",
                    help="内容漂移时显式重锚（默认漂移即报错）")
    args = ap.parse_args()
    db.init_db()
    only = {x for x in args.only.split(",") if x} or None
    rows = register(dry_run=args.dry_run, only=only,
                    reset_anchor=args.reset_anchor)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    n_human_roots = sum(1 for r in rows
                        if r["source_type"] == "human_fiction"
                        and r["canonical_work_id"] == r["work_id"])
    print(f"[register_work_sources] 处理 {len(rows)} 部 Work；"
          f"人类源根作品（镜像/fixture 不计）= {n_human_roots}")


if __name__ == "__main__":
    main()
