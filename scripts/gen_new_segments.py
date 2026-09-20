"""为新段落建实验（「加段落」链路）：抽段 → 抽 primary L 帧 → 生成 B0/C 候选。

## 为什么必须单独一个脚本

`experiments.create_experiment` 从**全池**按 `segment_seed` 随机抽段，
**不会避开已被别的实验用过的段落** —— 直接复用会把旧段又抽回来
（B82D 那 30 段就是这么来的，全项目在它上面判了 192 次才发现是同一批段落）。
本脚本显式写死 `segment_ids`，并保证与所有既有实验**零重叠**。

## 速度取舍（每次调用都是钱）

- 只抽 **L** 粒度、只用**第一个抽取器**：`is_primary=(i==0)`，而候选只认 primary 帧 →
  双抽取器/三粒度是纯浪费。
- 只生成 **B0(`reconstruct_v1`) + C(`recon_ctx_v1`)**：D(`recon_ctxonly_v1`) 是
  "只给前文自由续写"的消融条件，与人类段不是同一内容，**不允许进盲评**。
- → **每段 1 次抽帧 + 4 次重建 = 5 次调用**。

用法：
    python scripts/gen_new_segments.py --work 琼明 --n 50 --tag nq50 --dry-run
    python scripts/gen_new_segments.py --work 琼明 --n 50 --tag nq50
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import config, db, experiments                    # noqa: E402
from app.ids import new_exp_id                             # noqa: E402
from app.models import Candidate, Experiment, Frame, Segment, Work  # noqa: E402
from app.near_dup import train_sampling_pool               # noqa: E402
from app.reconstruct import RECON_PROMPT_VERSION, build_reconstruct_user  # noqa: E402
from corpus_matrix_candidates import MODELS, b_ctx, gen_group  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内

MIN_HUMAN_CHARS = 40        # 与既有盲评批一致（B82D 段落全部 ≥40 字）


def pick_segments(s, work_id: str, n: int, seed: int,
                  min_chars: int = MIN_HUMAN_CHARS) -> list[Segment]:
    """从该作品的合格 v2 段里抽 n 段，**排除所有已被任何实验用过的段**。"""
    pool = train_sampling_pool(s, [work_id], seg_version=2, eligible_only=True)
    used = {r[0] for r in s.query(Frame.segment_id).distinct()}
    cands = [x for x in pool
             if x.id not in used and len((x.text or "").strip()) >= min_chars]
    cands.sort(key=lambda x: x.id)                  # 固定顺序 → 种子可复现
    if len(cands) < n:
        raise SystemExit(f"合格且未用过的段落只有 {len(cands)} 条（<{n}），无法抽 {n} 段")
    return random.Random(seed).sample(cands, n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="作品标题关键字（如 琼明）")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--tag", required=True, help="实验名后缀 / 便于识别")
    ap.add_argument("--seed", type=int, default=20260916)
    ap.add_argument("--min-human-chars", type=int, default=MIN_HUMAN_CHARS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-candidates", action="store_true",
                    help="只抽帧、不生成候选（用于改用并行驱动 gen_cand_one.py 分模型跑）")
    args = ap.parse_args()

    db.init_db()
    with db.session() as s:
        w = s.query(Work).filter(Work.title.like(f"%{args.work}%")).first()
        if not w:
            raise SystemExit(f"找不到作品：{args.work}")
        segs = pick_segments(s, w.id, args.n, args.seed, args.min_human_chars)
        lens = sorted(len(x.text.strip()) for x in segs)
        print(f"作品 {w.title}")
        print(f"抽取 {len(segs)} 段（均未被任何实验用过，人类段长度 "
              f"中位 {lens[len(lens)//2]} / 最小 {lens[0]} / 最大 {lens[-1]}）")
        print(f"预计调用：抽帧 {len(segs)} 次 + 重建 {len(segs)}×2模型×2口径 = "
              f"{len(segs) * 5} 次")
        if args.dry_run:
            print("\ndry-run：未写库")
            return
        # 批量防呆①（P0 死 id 事故）：这个脚本会新建成一条实验配置，
        # 名字写进 cfg 后就再没人检查——池外抽取器=整批 failed 帧 + 空候选池。
        pf.require_models([config.EXTRACTOR_MODELS[0], *MODELS], source="gen_new_segments")

        cfg = {
            "n_segments": len(segs),
            "granularities": ["L"],                       # 候选只认 primary L 帧
            "extractors": [config.EXTRACTOR_MODELS[0]],   # 第一个 = is_primary
            "recon_models": MODELS,
            "temperatures": [0.7],
            "samples_per_pair": 1,
            "judge_models": MODELS[:1],
            "judge_human_naturalness": False,
            "adversarial_k": 8,
            "leak_thresholds": {"char6": 0.30, "word3": 0.35, "rare": 0.25,
                                "adversarial": 0.65},
            "segment_seed": args.seed,
            "concurrency": 4,
            "work_ids": [w.id],
            "seg_version": 2,
            "eligible_only": True,
            "segment_ids": [x.id for x in segs],          # ← 显式写死，绕开随机抽样
            "writer_input_mode": "frame_only",
            "context_ladder": ["prev1", "prev3", "scene_context", "compressed_long"],
        }
        exp = Experiment(id=new_exp_id(), name=f"new_segments_{args.tag}",
                         status="created", config=cfg, stats={})
        s.add(exp)
        s.commit()
        eid = exp.id
        print(f"\n已建实验 {eid}（{len(segs)} 段）")

    # ① 抽 primary L 帧
    with db.session() as s:
        exp = s.get(Experiment, eid)
        experiments.stage_extract_frames(s, exp)
    with db.session() as s:
        st = (s.get(Experiment, eid).stats or {}).get("stages", {}).get("extract_frames", {})
        nfr = s.query(Frame).filter_by(experiment_id=eid, granularity="L",
                                       is_primary=True).count()
        print(f"① 抽帧：ok={st.get('ok')} failed={st.get('failed')}；"
              f"primary L 帧 = {nfr}/{len(segs)}")

    # ② 生成 B0 + C 候选
    if args.skip_candidates:
        print(f"\n--skip-candidates：只抽帧。下一步用并行驱动分模型跑：")
        print(f"  python scripts/gen_cand_one.py {eid} --models {config.DEFAULT_LLM_MODEL} --groups B0,C")
        print(f"  python scripts/gen_cand_one.py {eid} --models z-ai/glm-5.3 --groups B0,C")
        return

    with db.session() as s:
        exp = s.get(Experiment, eid)
        frames = (s.query(Frame).filter_by(experiment_id=eid, granularity="L", is_primary=True)
                  .filter(Frame.status != "failed").all())
        segs_map = {x.id: x for x in s.query(Segment).filter(
            Segment.id.in_({f.segment_id for f in frames})).all()}
        print("② 生成候选（B0 无上文重建 / C 带上文重建）")
        gen_group(s, exp, frames, segs_map, "B0",
                  lambda s_, seg, f: build_reconstruct_user(f.payload),
                  "reconstruct", RECON_PROMPT_VERSION)
        gen_group(s, exp, frames, segs_map, "C", b_ctx, "reconstruct_ctx", "recon_ctx_v1")

    with db.session() as s:
        ok = s.query(Candidate).filter_by(experiment_id=eid, status="ok").count()
        nseg = len({c.segment_id for c in s.query(Candidate).filter_by(
            experiment_id=eid, status="ok").all()
            if c.prompt_version in config.BLIND_REVIEW_PROMPT_VERSIONS})
    print(f"\n完成：实验 {eid}，候选 ok={ok}，可盲评覆盖段落 {nseg}")
    print(f"下一步：python scripts/make_random_batch.py --exp {eid} --n {nseg} "
          f"--tag <批标签> --by segment --min-human-chars {args.min_human_chars}")


if __name__ == "__main__":
    main()
