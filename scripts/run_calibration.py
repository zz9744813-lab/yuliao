#!/usr/bin/env python
"""一键跑通校准实验。

用法：
  python scripts/run_calibration.py --mock                 # 假数据全流程冒烟（不联网）
  python scripts/run_calibration.py --segments 60          # 真实跑 60 段
  python scripts/run_calibration.py --segments 200 --concurrency 8

默认从 data/corpus_inbox/ 读语料；--use-distiller 也可把 novel-distiller 的库拉进来
（注意：distiller 当前只有验收测试书，不是好的 Human Anchor）。

导入路径守卫（审计 P1）只接受**绝对路径**且 realpath 必须落在允许根内，所以本脚本
把 CLI 给的路径按当前工作目录解析成绝对路径再交给 corpus；需要放行别的目录时用
--import-root（可重复），它只并入本次进程的 LG_IMPORT_ROOTS，不改 app 的默认口径。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, corpus, db, experiments  # noqa: E402

# 导入阶段的退出码：守卫拒绝 ≠ 文件不存在（后者沿用既有行为，打印后继续跑）
EXIT_IMPORT_REFUSED = 3
# 守卫缺位时（本文件可跑在未合入守卫的基线上）用来识别错误口径的兜底名
_GUARD_RULES_FALLBACK = (
    "import_not_absolute", "import_root_not_allowed",
    "import_ext_not_allowed", "import_too_large",
)
IMPORT_ROOTS_ENV = getattr(corpus, "IMPORT_ROOTS_ENV", "LG_IMPORT_ROOTS")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--segments", type=int, default=24)
    p.add_argument("--granularities", default="S,M,L")
    p.add_argument("--models", default=None, help="逗号分隔；默认读 LG_RECON_MODELS")
    p.add_argument("--judge-models", default=None, help="逗号分隔；评委应与重建模型不同家族")
    p.add_argument("--extract-models", default=None, help="逗号分隔；默认 kimi+deepseek 双抽")
    p.add_argument("--work", default=None, help="作品 id 或标题子串；只从这本书采样")
    p.add_argument("--file", default=None,
                   help="先导入一本书再跑（相对路径按当前工作目录解析成绝对路径）")
    p.add_argument("--import-root", action="append", default=None, metavar="DIR",
                   help=f"本次调用把该目录并入允许根（{IMPORT_ROOTS_ENV}），可重复")
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


def _abs(raw: str) -> str:
    """CLI 给的路径 → 绝对路径（相对路径按当前工作目录解析，符合命令行直觉）。

    守卫只接受绝对路径，不解析就是既有 `--file 相对路径` 用法的行为回归。
    """
    return str(Path(str(raw).strip()).expanduser().resolve())


def _default_import_roots() -> list[str]:
    """守卫自己的默认根（有守卫就问守卫，没有则同口径回退）——不改其口径。"""
    getter = getattr(corpus, "_default_import_roots", None)
    if callable(getter):
        return [str(p) for p in getter()]
    return [str(config.DATA_DIR), str(config.ROOT / "inbox")]


def _apply_import_roots(extra: list[str] | None) -> None:
    """--import-root：本次调用把目录并入 LG_IMPORT_ROOTS，**保留**原有默认根。"""
    if not extra:
        return
    raw = os.environ.get(IMPORT_ROOTS_ENV, "")
    parts = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    if not parts:
        parts = _default_import_roots()
    for p in extra:
        ap = _abs(p)
        if all(os.path.normcase(ap) != os.path.normcase(q) for q in parts):
            parts.append(ap)
    os.environ[IMPORT_ROOTS_ENV] = os.pathsep.join(parts)


def _guard_rules() -> tuple[str, ...]:
    """守卫的错误码前缀（app 侧常量是唯一口径源）。"""
    found = tuple(v for k, v in vars(corpus).items()
                  if k.startswith("IMPORT_ERR_") and isinstance(v, str))
    return found or _GUARD_RULES_FALLBACK


def _is_guard_refusal(err: str) -> bool:
    return any(rule in err for rule in _guard_rules())


def _print_refusal_hint(err: str, target: str) -> None:
    print(f"提示: {target} 被导入路径守卫拒绝 ⇒ {err}")
    print(f"      可选做法：① 加 --import-root <该文件所在目录>（只影响本次调用）；"
          f"② 设环境变量 {IMPORT_ROOTS_ENV}（{os.pathsep} 分隔多个根）长期放行；"
          f"③ 把文件放进默认根 {config.DATA_DIR} 或 {config.ROOT / 'inbox'} 内。")
    print("      当前允许根: "
          f"{os.environ.get(IMPORT_ROOTS_ENV, '').strip() or os.pathsep.join(_default_import_roots())}")


def _report_import(res, target: str) -> int:
    """导入返回值的口径：守卫拒绝 ⇒ 可行动提示 + 专用退出码；其余沿用既有「打印后继续」。"""
    err = res.get("error") if isinstance(res, dict) else None
    if not err:
        return 0
    if _is_guard_refusal(str(err)):
        _print_refusal_hint(str(err), target)
        return EXIT_IMPORT_REFUSED
    print(f"提示: {target} 未导入（{err}），继续用现有语料")
    return 0


def run_imports(s, args) -> int:
    """导入阶段（不动其它行为）。返回 0 = 继续；非 0 = 直接作为脚本退出码。"""
    _apply_import_roots(args.import_root)
    if args.file:
        target = _abs(args.file)
        res = corpus.import_file(s, target)
        print(res)
        rc = _report_import(res, f"--file {target}")
        if rc:
            return rc
    if args.use_fixtures:
        print(corpus.import_inbox(s, Path(config.DATA_DIR) / "fixtures"))
    print(corpus.import_inbox(s))
    if args.use_distiller:
        distiller_db, distiller_root = _abs(args.distiller_db), _abs(args.distiller_root)
        res = corpus.import_distiller(s, distiller_db, distiller_root)
        print(res)
        rc = _report_import(res, f"--use-distiller（{distiller_db}）")
        if rc:
            return rc
    return 0


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
        rc = run_imports(s, args)
        if rc:
            return rc
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
