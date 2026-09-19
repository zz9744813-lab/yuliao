"""四语料矩阵第二步：对 4 个 50 段实验生成 B0/C/D 候选（L-frame，temp0.7，双模型）。
前置：corpus_matrix_extract.py 已跑完。幂等，可重复执行。"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db, experiments
from app.context_ablation import neighbors
from app.gateway import chat
from app.models import Candidate, Experiment, Frame, Segment, Work
from app.prompt_render import render
from app.reconstruct import build_reconstruct_user, RECON_PROMPT_VERSION
from scripts.phase15_gen import RECON_CTX_USER, RECON_CTX_SYSTEM
from scripts.factorial_d import USER as CTXONLY_USER, SYS as CTXONLY_SYS

MODELS = [config.DEFAULT_LLM_MODEL, "z-ai/glm-5.3"]

def gen_group(s, exp, frames, segs, kind, builder, purpose, pv):
    existing = {(c.frame_id, c.model, c.prompt_version) for c in
                s.query(Candidate).filter_by(experiment_id=exp.id)}
    n = 0
    for f in frames:
        seg = segs.get(f.segment_id)
        if not seg:
            continue
        for model in MODELS:
            if (f.id, model, pv) in existing:
                continue
            try:
                r = chat(model=model, system="你是中文小说写作者，只输出正文。",
                         user=builder(s, seg, f), purpose=f"{purpose}:{f.granularity}",
                         prompt_version=pv, temperature=0.7, max_tokens=3000)
                text, st = r.text.strip(), ("ok" if r.text.strip() else "failed")
                ti, to, lat, err = r.tokens_in, r.tokens_out, r.latency_ms, None
            except Exception as e:
                text, st, ti, to, lat, err = "", "failed", 0, 0, 0, str(e)[:200]
            s.add(Candidate(experiment_id=exp.id, frame_id=f.id, segment_id=f.segment_id,
                            anon_label=f"{kind[0].upper()}X{n:03d}", model=model, temperature=0.7,
                            seed=None, prompt_version=pv, text=text, tokens_in=ti, tokens_out=to,
                            latency_ms=lat, status=st, error=err))
            s.commit(); n += 1
    print(f"  {kind}: {n}", flush=True)

def b_ctx(s, seg, f):
    nb = neighbors(s, seg)
    return render(RECON_CTX_USER, prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                  prev1=(nb["prev1"].text if nb["prev1"] else "（无）"),
                  frame_json=json.dumps(f.payload, ensure_ascii=False))

def b_ctxonly(s, seg, f):
    nb = neighbors(s, seg)
    return render(CTXONLY_USER, prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                  prev1=(nb["prev1"].text if nb["prev1"] else "（无）"))

def main():
    with db.session() as s:
        eids = [r[0] for r in s.query(Experiment.id).filter(
            Experiment.config.like('%"seg_version": 2%'),
            Experiment.config.like('%"n_segments": 50%')).all()]
    print("目标实验:", eids, flush=True)

    for eid in eids:
        print("==", eid, flush=True)
        with db.session() as s:
            exp = s.get(Experiment, eid)
            frames = s.query(Frame).filter_by(experiment_id=eid, granularity="L", is_primary=True)\
                      .filter(Frame.status != "failed").all()
            segs = {x.id: x for x in s.query(Segment).filter(
                Segment.id.in_({f.segment_id for f in frames})).all()}
            gen_group(s, exp, frames, segs, "B0", lambda s, seg, f: build_reconstruct_user(f.payload),
                      "reconstruct", RECON_PROMPT_VERSION)
            gen_group(s, exp, frames, segs, "C", b_ctx, "reconstruct_ctx", "recon_ctx_v1")
            gen_group(s, exp, frames, segs, "D", b_ctxonly, "reconstruct_ctxonly", "recon_ctxonly_v1")
    print("四语料候选全部完成")


if __name__ == "__main__":
    main()
