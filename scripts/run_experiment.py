"""实验引擎 CLI —— 跑 / 续跑一个实验的阶段状态机（任务 12）。

    python scripts/run_experiment.py --exp EXP-XXX                     # 全部阶段
    python scripts/run_experiment.py --exp EXP-XXX --stages plan,extract
    python scripts/run_experiment.py --exp EXP-XXX --dry-run --json    # 只打印阶段计划，零调用
    python scripts/run_experiment.py --exp EXP-XXX --mock              # mock 端到端，不花额度

--dry-run 在实验不存在时也能出计划（只打印，不创建）；--mock 把 LLM 切到 mock 模式
端到端跑通全流程，token/计数照常落库但不花一分钱额度。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description="实验引擎：plan→source_check→extract→"
                                             "reconstruct→residual→judge→report")
    ap.add_argument("--exp", required=True, help="实验 id（experiments.id）")
    ap.add_argument("--stages", default=None,
                    help="逗号分隔的阶段子集（默认全部）；顺序恒按状态机")
    ap.add_argument("--dry-run", action="store_true", help="只打印阶段计划，不跑不发调用")
    ap.add_argument("--mock", action="store_true", help="LG_LLM_MODE=mock，端到端跑通不花额度")
    ap.add_argument("--force", action="store_true",
                    help="stats 里已 done 的阶段也强制重跑（产品级幂等，不会重复计数）")
    ap.add_argument("--release", action="store_true",
                    help="显式释放卡死的执行权（A07：status=running 但 runner 已死；"
                         "无自动 TTL 接管——长跑中途不许被误抢）")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    # --mock 必须在 import app 之前生效：config 在 import 时读环境变量
    if args.mock:
        os.environ["LG_LLM_MODE"] = "mock"

    from app import config, db, engine

    if args.mock and config.LLM_MODE != "mock":
        config.LLM_MODE = "mock"          # 双保险：环境变量没生效就直接改运行时配置

    db.init_db()

    if args.release:
        out = engine.release_run(args.exp)
        print(json.dumps({"experiment": args.exp, **out}, ensure_ascii=False))
        return

    if args.dry_run:
        plan = engine.plan_run(args.exp, args.stages)
        out = {"experiment": args.exp, "dry_run": True, **plan}
        if args.as_json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"实验 {args.exp}（存在={plan['exists']}）阶段计划：")
            for name, row in plan["stages"].items():
                extra = f"  ← {row['reason']}" if row.get("reason") else ""
                print(f"  {row['action']:8s} {name}{extra}")
        return

    try:
        result = engine.run(args.exp, args.stages, force=args.force)
    except engine.EngineError as e:
        if args.as_json:
            print(json.dumps({"experiment": args.exp, "ok": False,
                              "error": str(e)}, ensure_ascii=False))
        else:
            print(f"✗ {e}", file=sys.stderr)
        raise SystemExit(1)

    stages = result.get("stages") or {}
    if args.as_json:
        print(json.dumps({"experiment": args.exp, "ok": True,
                          "status": "done", "engine": result},
                         ensure_ascii=False, indent=2))
    else:
        print(f"实验 {args.exp} 跑完：")
        for name, rec in stages.items():
            print(f"  {rec['status']:6s} {name:13s} attempted={rec['attempted']} "
                  f"ok={rec['ok']} failed={rec['failed']} skipped={rec['skipped']} "
                  f"tokens={rec['tokens']} {rec['seconds']}s")


if __name__ == "__main__":
    main()
