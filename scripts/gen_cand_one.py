"""单实验候选生成器（并行切片版，2026-09-14）。

用法: python scripts/gen_cand_one.py EXP-0914-FF7B [--limit N] [--models m1,m2]

与 corpus_matrix_full.py 的 gen_group 逻辑一致，但：
  - 只跑命令行指定的一个实验（多进程并行切片，互不重叠）；
  - 帧内顺序 model → [B0, C, D]，尽早凑齐单模型三组，盲评池可增量出现；
  - 每次调用前重查 (frame_id, model, prompt_version) 幂等键，防与主进程撞车重复生成；
  - anon_label 加 4 位 run 标签，避免跨进程同号。
禁止 import corpus_matrix_candidates / corpus_matrix_full（两者顶层直接执行）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import config, db
from app.context_ablation import neighbors
from app.gateway import chat
from app.models import Candidate, Experiment, Frame, Segment
from app.prompt_render import render
from app.reconstruct import build_reconstruct_user, RECON_PROMPT_VERSION
from phase15_gen import RECON_CTX_USER
from factorial_d import USER as CTXONLY_USER

MODELS = [config.DEFAULT_LLM_MODEL, "meta/muse-spark-1.3-contributor"]
PV_B0 = RECON_PROMPT_VERSION          # reconstruct_v1
PV_C, PV_D = "recon_ctx_v1", "recon_ctxonly_v1"
RUN_TAG = format(int(time.time()) & 0xFFFF, "04x")


def b_ctx(s, seg, f):
    nb = neighbors(s, seg)
    return render(RECON_CTX_USER, prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                  prev1=(nb["prev1"].text if nb["prev1"] else "（无）"),
                  frame_json=json.dumps(f.payload, ensure_ascii=False))


def b_ctxonly(s, seg, f):
    nb = neighbors(s, seg)
    return render(CTXONLY_USER, prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                  prev1=(nb["prev1"].text if nb["prev1"] else "（无）"))


GROUPS = [
    ("B0", lambda s, seg, f: build_reconstruct_user(f.payload), "reconstruct", PV_B0),
    ("C", b_ctx, "reconstruct_ctx", PV_C),
    ("D", b_ctxonly, "reconstruct_ctxonly", PV_D),
]


def exists(s, eid, frame_id, model, pv):
    # model.in_(model_any)：改名前生成的候选存的是旧 id（库里上千行），
    # 精确匹配会把"已经做过"看成"没做过"→ 同一 (帧,模型,口径) 再生成一份，
    # 盲评里出现同文双份、成本也白烧一遍。
    q = (s.query(Candidate.id)
          .filter_by(experiment_id=eid, frame_id=frame_id, prompt_version=pv)
          .filter(Candidate.model.in_(config.model_any(model))))
    return q.first() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("eid")
    ap.add_argument("--limit", type=int, default=0, help="最多生成条数，0=不限")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--reverse", action="store_true", help="帧序反转（与主进程相向而行防撞车）")
    ap.add_argument("--groups", default=",".join(g[0] for g in GROUPS),
                    help="要生成的组，逗号分隔（默认全部）。D(recon_ctxonly_v1) 是非平行"
                         "消融、进不了盲评，只做盲评语料时可 --groups B0,C 省钱。")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    want = {x.strip() for x in a.groups.split(",") if x.strip()}
    groups = [g for g in GROUPS if g[0] in want]
    if not groups:
        raise SystemExit(f"--groups {a.groups} 无匹配；可选 {[g[0] for g in GROUPS]}")

    with db.session() as s:
        exp = s.get(Experiment, a.eid)
        if exp is None:
            raise SystemExit(f"experiment {a.eid} 不存在")
        frames = (s.query(Frame)
                  .filter_by(experiment_id=a.eid, granularity="L", is_primary=True)
                  .filter(Frame.status != "failed").all())
        segs = {x.id: x for x in s.query(Segment).filter(
            Segment.id.in_({f.segment_id for f in frames})).all()}
        print(f"[{a.eid}] run_tag={RUN_TAG} L-frames={len(frames)} models={models}", flush=True)

    n_total = 0
    t0 = time.time()
    if a.reverse:
        frames = list(reversed(frames))
    for f in frames:
        with db.session() as s:
            seg = segs.get(f.segment_id)
            if not seg or not f.payload:
                continue
            for model in models:
                for kind, builder, purpose, pv in groups:
                    if exists(s, a.eid, f.id, model, pv):
                        continue
                    if a.limit and n_total >= a.limit:
                        print(f"[{a.eid}] 达到 --limit={a.limit}，停止", flush=True)
                        print(f"[{a.eid}] 完成：本轮生成 {n_total} 条，"
                              f"耗时 {(time.time()-t0)/60:.1f} min", flush=True)
                        return
                    try:
                        r = chat(model=model, system="你是中文小说写作者，只输出正文。",
                                 user=builder(s, seg, f), purpose=f"{purpose}:{f.granularity}",
                                 prompt_version=pv, temperature=0.7, max_tokens=3000)
                        text, st = r.text.strip(), ("ok" if r.text.strip() else "failed")
                        ti, to, lat, err = r.tokens_in, r.tokens_out, r.latency_ms, None
                    except Exception as e:  # noqa: BLE001 —— 记录后继续，不吞帧
                        text, st, ti, to, lat, err = "", "failed", 0, 0, 0, str(e)[:200]
                    s.add(Candidate(experiment_id=a.eid, frame_id=f.id, segment_id=f.segment_id,
                                    anon_label=f"{kind[0]}X{RUN_TAG}{n_total % 1000:03d}",
                                    model=model, temperature=0.7, seed=None,
                                    prompt_version=pv, text=text, tokens_in=ti, tokens_out=to,
                                    latency_ms=lat, status=st, error=err))
                    s.commit()
                    n_total += 1
                    print(f"  {kind} {f.id[:8]} {model.split('/')[-1][:12]} → {st} "
                          f"lat={lat/1000:.0f}s 累计={n_total}", flush=True)
    print(f"[{a.eid}] 全部帧处理完毕：本轮生成 {n_total} 条，"
          f"耗时 {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
