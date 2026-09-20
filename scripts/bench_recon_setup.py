"""基准段自由重建 —— 解锁 §14 子基准「Human-vs-AI Discrimination」（2026-09-19）。

背景：T5 建子基准时查明，role='benchmark' 的 742 段上只有劣化变体（corrupt_*），
没有**自由重建**候选——hvai 子基准（人类原文 vs 重建候选，答案=人类侧）没有题源。

本脚本只做**装配**（幂等，可反复跑）：
  · 找出还没有 L 帧的基准段，**每次运行都刷新** exp.config["segment_ids"]
    （辅助实验的 segment_ids 只建一次 = 补抽帧时一个调用都不发的坑，交接 §9 实测）；
  · 实验配置压到最小额度：单抽取器、单重建模型、单温度、单采样、无对抗——
    human_vs_ai 每段 1 条候选就够建题，不烧额度。

跑批交给引擎（不在这里发调用）：
    python scripts/run_experiment.py --exp EXP-BENCH-RECON \
        --stages plan,source_check,extract,reconstruct

⚠ 隔离三件套（都已有闸/约定）：
  1. 训练导出：role='benchmark' 段不进导出（export_training 已实现）；
  2. 盲评池：基准段候选不进建批池（make_random_batch §14 硬闸，本日新增）；
  3. 本实验**永远不要**指给 make_random_batch / select_harvest_batch 建批。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db  # noqa: E402
from app.models import Experiment, Frame, Segment  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：模型名预检

EXP_ID = "EXP-BENCH-RECON"
MODEL = config.DEFAULT_LLM_MODEL   # 中转网关，非串行通道；够快且便宜


def setup(dry_run: bool = True) -> dict:
    with db.session() as s:
        have_l = {r[0] for r in s.query(Frame.segment_id).filter(
            Frame.granularity == "L", Frame.status == "ok").distinct()}
        segs = [x for x in s.query(Segment).filter(Segment.role == "benchmark").all()
                if x.id not in have_l]
        ids = [x.id for x in segs]
        exp = s.get(Experiment, EXP_ID)
        if exp is None:
            exp = Experiment(id=EXP_ID, name="benchmark_recon", status="created",
                             config={}, stats={})
            s.add(exp)
        cfg = dict(exp.config or {})
        cfg.update({
            "segment_ids": ids,              # 每次运行都刷新（交接 §9 的坑）
            "granularities": ["L"],
            "extractors": [MODEL],
            "recon_models": [MODEL],
            "judge_models": [MODEL],
            "temperatures": [0.7],
            "samples_per_pair": 1,
            "adversarial_k": 0,
            "concurrency": 4,
        })
        exp.config = cfg
        if dry_run:
            s.rollback()
            return {"segments": len(ids), "would_update_config": True}
        s.commit()
        return {"segments": len(ids), "experiment": EXP_ID,
                "already_done": exp.status}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="真写库（默认 dry-run）")
    args = ap.parse_args()
    db.init_db()
    if args.run:
        # 死 id 写进 exp.config，代价是后面整批抽帧静默 503（P0 事故形状）→ 真写库前先验名
        pf.require_models([MODEL], source="bench_recon_setup")
    out = setup(dry_run=not args.run)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
