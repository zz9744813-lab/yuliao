#!/usr/bin/env python
"""一键跑通校准实验。

用法：
  python scripts/run_calibration.py --mock                 # 假数据全流程冒烟（不联网）
  python scripts/run_calibration.py --segments 60          # 真实跑 60 段
  python scripts/run_calibration.py --segments 200 --concurrency 8

默认从 data/corpus_inbox/ 读语料；--use-distiller 也可把 novel-distiller 的库拉进来
（注意：distiller 当前只有验收测试书，不是好的 Human Anchor）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, corpus, db, experiments  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--segments", type=int, default=24)
    p.add_argument("--granularities", default="S,M,L")
    p.add_argument("--models", default=None, help="逗号分隔；默认读 LG_RECON_MODELS")
    p.add_argument("--judge-models", default=None, help="逗号分隔；评委应与重建模型不同家族")
    p.add_argument("--extract-models", default=None, help="逗号分隔；默认 kimi+deepseek 双抽")
    p.add_argument("--work", default=None, help="作品 id 或标题子串；只从这本书采样")
    p.add_argument("--file", default=None, help="先按绝对路径导入一本书再跑")
    p.add_argument("--temps", default="0.5,0.9")
    p.add_argument("--samples", type=int, default=2)
    p.add_argument("--adversarial-k", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--mock", action="store_true", help="不发请求：用确定性假模型过全流程")
    p.add_argument("--use-fixtures", action="store_true", help="把 data/fixtures 当 inbox 导一次")
    p.add_argument("--use-distiller", action="store_true")
    p.add_argument("--distiller-db", default=r"F:\agi\novel-distiller\data\app.sqlite3")
    p.add_argument("--distiller-root", default=r"F:\agi\novel-distiller")
    return p.parse_args()


def _resolve_work(s, wanted: str | None) -> list[str] | None:
    if not wanted:
        return None
    from app.models import Work
    hits = [w for w in s.query(Work).all()
            if w.id == wanted or wanted in w.title]
    if not hits:
        raise SystemExit(f"找不到作品: {wanted}（用 /works 或 --file 先导入）")
    ids = [w.id for w in hits]
    print(f"work filter: {[w.title for w in hits]} → {ids}")
    return ids


def main() -> int:
    args = parse_args()
    if args.mock:
        config.LLM_MODE = "mock"

    db.init_db()
    with db.session() as s:
        if args.file:
            print(corpus.import_file(s, args.file))
        if args.use_fixtures:
            print(corpus.import_inbox(s, Path(config.DATA_DIR) / "fixtures"))
        print(corpus.import_inbox(s))
        if args.use_distiller:
            print(corpus.import_distiller(s, args.distiller_db, args.distiller_root))
        print("corpus:", corpus.segment_stats(s))

        overrides = {
            "n_segments": args.segments,
            "granularities": args.granularities.split(","),
            "temperatures": [float(x) for x in args.temps.split(",")],
            "samples_per_pair": args.samples,
            "adversarial_k": args.adversarial_k,
            "concurrency": args.concurrency,
        }
        if args.models:
            overrides["recon_models"] = args.models.split(",")
        if args.judge_models:
            overrides["judge_models"] = args.judge_models.split(",")
        if args.extract_models:
            overrides["extractors"] = args.extract_models.split(",")
        work_ids = _resolve_work(s, args.work)
        if work_ids:
            overrides["work_ids"] = work_ids
        exp = experiments.create_experiment(s, overrides)
        print(f"experiment: {exp.id}")
        print(f"  segments={len(exp.config['segment_ids'])}  "
              f"grans={exp.config['granularities']}  models={exp.config['recon_models']}")

    print("running…（可在另一个终端 GET /experiments/<id> 看状态）")
    result = experiments.run_experiment(exp.id)
    print(f"done: status={result.status}")
    from app.models import ReportFile
    with db.session() as s:
        for rf in s.query(ReportFile).filter_by(experiment_id=result.id).all():
            print(f"  {rf.kind}: {rf.path}")
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
