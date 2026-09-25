"""v2 段 `n_sentences` 补数（口径修正的存量回收）。用法：
python scripts/backfill_v2_sentences.py               # 默认 dry-run：零写入、零 commit
python scripts/backfill_v2_sentences.py --apply       # 显式才写库（分批提交）
python scripts/backfill_v2_sentences.py --seg-version 2   # 限定只处理 seg_version=2 的段
python scripts/backfill_v2_sentences.py --verify      # 只读一致性校验：残留=0 才 exit 0

背景：`scripts/import_corpus_v2.py` 旧代码建段时硬写 `n_sentences=0`，v2 段
（seg_version=2）的句数在库里全是 0；v1 路径 `app/corpus.py add_work()` 写的
是 `len(_sentences(s))` ⇒ 两条导入路径口径不一致，`app/corpus.py segment_stats()`
的 `sentences_mean` 是被 0 拉平的假读数（受影响面是**存储列与统计口径**，
K2/K3 特征取自 `residuals_det.deltas` 现算，不读这一列）。

口径**单源**：句数只经 `scripts/import_corpus_v2.py` 的 `n_sents()` 计算，
它复用 `app.segmenter_v2._count_sents` → `app.metrics_det._sentences`——与 v1
路径同一个实现。本脚本不自写第二套切句器，也不另立常量：连分批提交的
`BATCH` 规模都从 `import_corpus_v2.py` 读现值。

版本作用域（审计 lg-review-round-20260925 §1.6 的声明-实现口径差收口）：
本脚本的背景数字是「v2 段 424,294 行」，但**默认扫描一直是全库口径**——
查询不含 `seg_version` 谓词，扫的是所有版本里 `n_sentences == 0` 的非空段
（功能上等价：v1 非空段本就 ≥1，命中行只会是那个 bug 造出来的）。默认值
保持该既有行为不变；要声明与实现严格吻合，用 `--seg-version 2` 显式限定
作用域。任何模式下安全过滤都不放宽：只碰 `n_sentences == 0` 且 `text` 非空
的段，只写 `n_sentences` 一列，`text`/`text_clean`/`n_chars` 一律不动。

范围与幂等：只处理 `n_sentences == 0` 且 `text` 非空（strip 后有内容）的段——
非空文本按该口径恒 ≥1，补完即不再命中过滤器，重跑不产生任何变化。数的是
`text` 原文（与 v1 一致），`text_clean` 不参与计数。已有非 0 值一律不动，
空文本段保持 0（如实反映「没有句子」）。

写入安全：默认 dry-run，只报 `would_update` 与抽样前后值，一次 commit 都不发；
`--apply` 才写，并按 BATCH 分批提交（42 万行不是一次 commit）。目标库路径每次
都打印，避免误指真库。

回填后一致性校验：`--verify` 为**纯只读**（零写入、零 commit），逐 `seg_version`
输出 `count(*) / sum(n_sentences>0) / avg(n_sentences) / 残留（n_sentences=0 且
文本非空）`；「非空文本但 `n_sentences == 0`」的残留行数必须为 0 才 exit 0，
否则 exit 1 并列出样本 id——回填「跑过了」从此可以用一条命令自证。"""
import argparse
import importlib.util
import sys
from pathlib import Path

from sqlalchemy import case, func

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db                                # noqa: E402
from app.models import Segment                            # noqa: E402

_ROOT = Path(__file__).resolve().parent


def _import_mod():
    """加载 scripts/import_corpus_v2.py，复用它的 BATCH 与 n_sents（口径单源）。"""
    spec = importlib.util.spec_from_file_location("ic_backfill",
                                                  _ROOT / "import_corpus_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IC = _import_mod()
BATCH = IC.BATCH          # 与导入脚本同一规模常量（不另立数字）
n_sents = IC.n_sents      # 与两条导入路径同一个句数口径


def _preview(text: str, width: int = 24) -> str:
    flat = " ".join(text.split())
    return flat[:width] + ("…" if len(flat) > width else "")


def _scope_sql(seg_versions: list[int] | None) -> str:
    return ("全库（查询不含 seg_version 谓词）" if seg_versions is None
            else "seg_version∈{" + "、".join(str(v) for v in seg_versions) + "}")


def _check_versions(seg_versions: list[int] | None) -> list[int] | None:
    """None=全库（保持既有默认行为）；给了就必须是非空、去重、≥1 的整数列表。"""
    if seg_versions is None:
        return None
    vs = sorted(set(seg_versions))
    if not vs:
        raise ValueError("seg_versions 不许是空列表：不限版本请传 None（全库），"
                         "否则至少给一个版本号")
    if any(not isinstance(v, int) or v < 1 for v in vs):
        raise ValueError(f"seg_versions 必须是 ≥1 的整数，收到 {vs}")
    return vs


def scan(*, batch: int = BATCH, sample: int = 5, apply: bool = False,
         work_ids: list[str] | None = None,
         seg_versions: list[int] | None = None) -> dict:
    """扫库补数。apply=False 只读（零写入零 commit）；True 分批 UPDATE + commit。

    seg_versions=None（默认）＝**全库**：查询不带 seg_version 谓词，扫所有版本里
    `n_sentences == 0` 的非空段——与脚本历史行为逐字一致（审计 §1.6 指出的
    「说只补 v2、实际全库」口径差由默认值如实保留，不再靠叙述掩盖）。
    指定如 [2] 时只处理该 seg_version 的行。两种模式下安全过滤完全相同：
    只碰 `n_sentences == 0` 且 text 非空的段，只写 n_sentences 一列。

    返回 {scanned, matched, blank, updated, batch, samples:[…], versions:{…}}：
    scanned=读到的行数，matched=n_sentences==0 的行数，blank=其中文本为空跳过的行数，
    updated=实际写库行数（dry-run 恒 0），samples=抽样前后值，
    versions=按 seg_version 分布的 {matched, blank, updated}（本次实际覆盖了哪些版本，
    一眼可辨；apply=False 时 updated 恒 0）。
    按主键 keyset 分页（`id > 上一批末 id`），42 万行不全表载入内存。
    work_ids 非空时只补这些 work 的段（真库可先拿一本试点书验证影响面）。"""
    vs = _check_versions(seg_versions)
    scanned = matched = blank = updated = 0
    versions: dict[int, dict] = {}
    samples: list[dict] = []
    last_id = ""
    while True:
        with db.session() as s:
            q = s.query(Segment).filter(Segment.n_sentences == 0, Segment.id > last_id)
            if vs is not None:
                q = q.filter(Segment.seg_version.in_(vs))
            if work_ids:
                q = q.filter(Segment.work_id.in_(work_ids))
            rows = q.order_by(Segment.id).limit(batch).all()
            if not rows:
                break
            last_id = rows[-1].id
            scanned += len(rows)
            pending = []
            for seg in rows:
                matched += 1
                vd = versions.setdefault(
                    seg.seg_version, {"matched": 0, "blank": 0, "updated": 0})
                vd["matched"] += 1
                text = seg.text or ""
                if not text.strip():        # 空文本：没有句子可数，保持 0
                    blank += 1
                    vd["blank"] += 1
                    continue
                n = n_sents(text)
                if len(samples) < sample:
                    samples.append({"id": seg.id, "work_id": seg.work_id,
                                    "ordinal": seg.ordinal, "seg_version": seg.seg_version,
                                    "before": seg.n_sentences,
                                    "after": n, "text": _preview(text)})
                if apply:
                    seg.n_sentences = n
                pending.append(seg.id)
            if apply and pending:
                s.commit()                  # 每 BATCH 行提交一次
                updated += len(pending)
                for seg in rows:
                    if seg.n_sentences:     # 已写入行（空文本行保持 0 不计）
                        versions[seg.seg_version]["updated"] += 1
            else:
                s.expunge_all()             # dry-run 分支根本不赋值；这里只丢弃身份映射
    return {"scanned": scanned, "matched": matched, "blank": blank,
            "updated": updated, "batch": batch, "samples": samples,
            "versions": versions, "seg_versions": vs}


def verify(*, batch: int = BATCH, sample: int = 5,
           seg_versions: list[int] | None = None) -> dict:
    """**只读**一致性校验（零写入、零 commit、零 flush）：回填后的自证出口。

    逐 seg_version 输出 count(*) / sum(n_sentences>0) / avg(n_sentences) /
    残留数（`n_sentences == 0` 且 text 非空——与 scan 的安全过滤**同一判据**，
    文本按 strip 判空，Python 侧复核，不走 SQL trim 近似）。
    seg_versions=None＝全库逐版本统计；指定时只看这些版本（残留门也只看它们）。

    返回 {stats: [{seg_version, count, positive, avg, residual}], residual_total,
    samples: [{id, seg_version, work_id}…前 sample 条]}；
    合格判据 = residual_total == 0。"""
    vs = _check_versions(seg_versions)
    stats: list[dict] = []
    with db.session() as s:
        q = (s.query(Segment.seg_version,
                     func.count().label("cnt"),
                     func.sum(case((Segment.n_sentences > 0, 1), else_=0)).label("pos"),
                     func.avg(Segment.n_sentences).label("avg"))
             .group_by(Segment.seg_version).order_by(Segment.seg_version))
        if vs is not None:
            q = q.filter(Segment.seg_version.in_(vs))
        for ver, cnt, pos, avg in q.all():
            stats.append({"seg_version": ver, "count": cnt, "positive": int(pos or 0),
                          "avg": (round(float(avg), 3) if avg is not None else None),
                          "residual": 0})
    by_ver = {x["seg_version"]: x for x in stats}
    residual_total = 0
    samples: list[dict] = []
    last_id = ""
    while True:
        with db.session() as s:
            q = (s.query(Segment.id, Segment.work_id, Segment.seg_version, Segment.text)
                 .filter(Segment.n_sentences == 0, Segment.id > last_id))
            if vs is not None:
                q = q.filter(Segment.seg_version.in_(vs))
            rows = q.order_by(Segment.id).limit(batch).all()
            if not rows:
                break
            last_id = rows[-1].id
            for rid, wid, rver, rtext in rows:
                if (rtext or "").strip():   # 与 scan 完全同一判据：非空文本仍是 0 ⇒ 残留
                    residual_total += 1
                    by_ver[rver]["residual"] += 1
                    if len(samples) < sample:
                        samples.append({"id": rid, "seg_version": rver, "work_id": wid})
    stats.sort(key=lambda x: x["seg_version"])
    return {"stats": stats, "residual_total": residual_total, "samples": samples,
            "seg_versions": vs}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    p = argparse.ArgumentParser(
        prog="backfill_v2_sentences.py",
        description="补 v2 段的 n_sentences（口径与 app.metrics_det._sentences 同源）。"
                    "默认 dry-run 零写入，只有显式 --apply 才写库。"
                    "默认扫描是全库口径（不限 seg_version）；--seg-version 显式限定版本作用域。"
                    "--verify 为只读一致性校验（残留=0 才 exit 0）。")
    p.add_argument("--apply", action="store_true",
                   help="实际写库（默认关＝dry-run，只报数与抽样，零 commit）")
    p.add_argument("--verify", action="store_true",
                   help="只读一致性校验：逐 seg_version 报 count/sum(>0)/avg/残留"
                        "（n_sentences=0 且文本非空）；残留=0 才 exit 0，零写入")
    p.add_argument("--seg-version", action="append", type=int, default=[], metavar="N",
                   help="只处理该 seg_version（可重复，如 --seg-version 2）；"
                        "默认不限＝全库口径（查询不含 seg_version 谓词，与既有行为一致）")
    p.add_argument("--batch", type=int, default=BATCH,
                   help=f"每批行数/提交粒度（默认沿用 import_corpus_v2.BATCH={BATCH}）")
    p.add_argument("--sample", type=int, default=5,
                   help="抽样打印前后值的行数（默认 5）")
    p.add_argument("--work", action="append", default=[], metavar="WORK_ID",
                   help="只补这些 work 的段（可重复；默认不限＝全库 n_sentences=0 的段）")
    a = p.parse_args(argv)
    if a.batch < 1:
        p.error("--batch 必须 ≥1")
    if a.sample < 0:
        p.error("--sample 不能为负")
    if a.verify and a.apply:
        p.error("--verify 是只读校验，不许与 --apply 同用")
    if a.verify and a.work:
        p.error("--verify 是全库逐 seg_version 的口径校验，不支持 --work 局部化")

    print(f"目标库: {config.DATABASE_URL}", flush=True)
    try:
        scope = _check_versions(a.seg_version or None)
    except ValueError as e:
        p.error(str(e))
    if a.verify:
        print(f"模式: VERIFY（只读一致性校验，零写入、零 commit）"
              f"｜范围 {_scope_sql(scope)}", flush=True)
        r = verify(batch=a.batch, sample=a.sample, seg_versions=scope)
        for st in r["stats"]:
            print(f"  seg_version={st['seg_version']}｜count={st['count']} "
                  f"｜n_sentences>0 {st['positive']} "
                  f"｜avg={st['avg']} "
                  f"｜残留(n_sentences=0 且文本非空) {st['residual']}", flush=True)
        if r["residual_total"]:
            print(f"验证明不合格：仍有 {r['residual_total']} 行非空文本 n_sentences=0"
                  f"（exit 1）。样本 id（前 {len(r['samples'])} 条）: "
                  f"{('、'.join(x['id'] for x in r['samples']))}", flush=True)
            return 1
        print("验证合格：非空文本且 n_sentences=0 的残留为 0 行 ⇒ 回填口径闭合"
              if scope is None else
              f"验证合格：{'、'.join(f'v{v}' for v in scope)} 内非空文本 n_sentences=0 "
              f"的残留为 0 行", flush=True)
        return 0

    print(f"模式: {'APPLY（写库）' if a.apply else 'DRY-RUN（零写入、零 commit）'}"
          f"｜批量 {a.batch}｜版本范围 {_scope_sql(scope)}"
          f"｜书目范围 {'全库（不限 work）' if not a.work else '、'.join(a.work)}"
          f"｜口径 import_corpus_v2.n_sents → "
          f"segmenter_v2._count_sents → metrics_det._sentences（与 v1 同源）", flush=True)
    r = scan(batch=a.batch, sample=a.sample, apply=a.apply, work_ids=list(a.work),
             seg_versions=scope)
    print(f"扫描 {r['scanned']} 行｜n_sentences=0 命中 {r['matched']} 行"
          f"（其中空文本跳过 {r['blank']} 行）"
          f"｜{('would_update' if not a.apply else 'updated')} "
          f"{r['matched'] - r['blank'] if not a.apply else r['updated']}", flush=True)
    verb = "would_update" if not a.apply else "updated"
    for ver, vd in sorted(r["versions"].items()):
        would = vd["matched"] - vd["blank"]
        print(f"  版本分布 seg_version={ver}｜命中 {vd['matched']} "
              f"（空文本跳过 {vd['blank']}）｜{verb} "
              f"{vd['updated'] if a.apply else would}", flush=True)
    for x in r["samples"]:
        print(f"  {x['id']} work={x['work_id']} ordinal={x['ordinal']} "
              f"seg_version={x['seg_version']} "
              f"n_sentences {x['before']} → {x['after']}｜{x['text']}", flush=True)
    if not r["matched"]:
        print(f"该作用域（{_scope_sql(scope)}）内没有 n_sentences=0 的非空段需要补"
              f"（已补齐或本就无需补）⇒ 幂等空转", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
