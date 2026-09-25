"""v2 段 `n_sentences` 补数（口径修正的存量回收）。用法：
python scripts/backfill_v2_sentences.py               # 默认 dry-run：零写入、零 commit
python scripts/backfill_v2_sentences.py --apply       # 显式才写库（分批提交）

背景：`scripts/import_corpus_v2.py` 旧代码建段时硬写 `n_sentences=0`，v2 段
（seg_version=2）的句数在库里全是 0；v1 路径 `app/corpus.py add_work()` 写的
是 `len(_sentences(s))` ⇒ 两条导入路径口径不一致，`app/corpus.py segment_stats()`
的 `sentences_mean` 是被 0 拉平的假读数（受影响面是**存储列与统计口径**，
K2/K3 特征取自 `residuals_det.deltas` 现算，不读这一列）。

口径**单源**：句数只经 `scripts/import_corpus_v2.py` 的 `n_sents()` 计算，
它复用 `app.segmenter_v2._count_sents` → `app.metrics_det._sentences`——与 v1
路径同一个实现。本脚本不自写第二套切句器，也不另立常量：连分批提交的
`BATCH` 规模都从 `import_corpus_v2.py` 读现值。

范围与幂等：只处理 `n_sentences == 0` 且 `text` 非空（strip 后有内容）的段——
非空文本按该口径恒 ≥1，补完即不再命中过滤器，重跑不产生任何变化。数的是
`text` 原文（与 v1 一致），`text_clean` 不参与计数。已有非 0 值一律不动，
空文本段保持 0（如实反映「没有句子」）。

写入安全：默认 dry-run，只报 `would_update` 与抽样前后值，一次 commit 都不发；
`--apply` 才写，并按 BATCH 分批提交（42 万行不是一次 commit）。目标库路径每次
都打印，避免误指真库。"""
import argparse
import importlib.util
import sys
from pathlib import Path

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


def scan(*, batch: int = BATCH, sample: int = 5, apply: bool = False,
         work_ids: list[str] | None = None) -> dict:
    """扫库补数。apply=False 只读（零写入零 commit）；True 分批 UPDATE + commit。

    返回 {scanned, matched, blank, updated, batch, samples:[…]}：
    scanned=读到的行数，matched=n_sentences==0 的行数，blank=其中文本为空跳过的行数，
    updated=实际写库行数（dry-run 恒 0），samples=抽样前后值。
    按主键 keyset 分页（`id > 上一批末 id`），42 万行不全表载入内存。
    work_ids 非空时只补这些 work 的段（真库可先拿一本试点书验证影响面）。"""
    scanned = matched = blank = updated = 0
    samples: list[dict] = []
    last_id = ""
    while True:
        with db.session() as s:
            q = s.query(Segment).filter(Segment.n_sentences == 0, Segment.id > last_id)
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
                text = seg.text or ""
                if not text.strip():        # 空文本：没有句子可数，保持 0
                    blank += 1
                    continue
                n = n_sents(text)
                if len(samples) < sample:
                    samples.append({"id": seg.id, "work_id": seg.work_id,
                                    "ordinal": seg.ordinal, "before": seg.n_sentences,
                                    "after": n, "text": _preview(text)})
                if apply:
                    seg.n_sentences = n
                pending.append(seg.id)
            if apply and pending:
                s.commit()                  # 每 BATCH 行提交一次
                updated += len(pending)
            else:
                s.expunge_all()             # dry-run 分支根本不赋值；这里只丢弃身份映射
    return {"scanned": scanned, "matched": matched, "blank": blank,
            "updated": updated, "batch": batch, "samples": samples}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    p = argparse.ArgumentParser(
        prog="backfill_v2_sentences.py",
        description="补 v2 段的 n_sentences（口径与 app.metrics_det._sentences 同源）。"
                    "默认 dry-run 零写入，只有显式 --apply 才写库。")
    p.add_argument("--apply", action="store_true",
                   help="实际写库（默认关＝dry-run，只报数与抽样，零 commit）")
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

    print(f"目标库: {config.DATABASE_URL}", flush=True)
    print(f"模式: {'APPLY（写库）' if a.apply else 'DRY-RUN（零写入、零 commit）'}"
          f"｜批量 {a.batch}｜范围 {'、'.join(a.work) if a.work else '全库'}"
          f"｜口径 import_corpus_v2.n_sents → "
          f"segmenter_v2._count_sents → metrics_det._sentences（与 v1 同源）", flush=True)
    r = scan(batch=a.batch, sample=a.sample, apply=a.apply, work_ids=list(a.work))
    print(f"扫描 {r['scanned']} 行｜n_sentences=0 命中 {r['matched']} 行"
          f"（其中空文本跳过 {r['blank']} 行）"
          f"｜{('would_update' if not a.apply else 'updated')} "
          f"{r['matched'] - r['blank'] if not a.apply else r['updated']}", flush=True)
    for x in r["samples"]:
        print(f"  {x['id']} work={x['work_id']} ordinal={x['ordinal']} "
              f"n_sentences {x['before']} → {x['after']}｜{x['text']}", flush=True)
    if not r["matched"]:
        print("没有 n_sentences=0 的非空段需要补（已补齐或本就无需补）⇒ 幂等空转", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
