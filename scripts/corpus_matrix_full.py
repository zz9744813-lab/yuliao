"""四语料矩阵推进：gap-fill 抽取失败 → leakage_static → B0/C/D 候选。幂等。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from app import db, experiments
from app.models import Experiment, Frame

EIDS = ["EXP-0914-C812", "EXP-0914-FF7B", "EXP-0914-EE18", "EXP-0914-6DD3"]

for eid in EIDS:
    print("==", eid, flush=True)
    with db.session() as s:
        exp = s.get(Experiment, eid)
        experiments.stage_extract_frames(s, exp)   # failed 清除重抽（gap-fill）
        experiments.stage_leakage_static(s, exp)
    with db.session() as s:
        st = (s.get(Experiment, eid).stats or {}).get("stages", {}).get("extract_frames", {})
        print(f"  extract: ok={st.get('ok')} repaired={st.get('repaired')} failed={st.get('failed')}", flush=True)

from corpus_matrix_candidates import MODELS  # noqa: E402
import json  # noqa: E402
from app.context_ablation import neighbors  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import Candidate, Frame, Segment, Work  # noqa: E402
from app.prompt_render import render  # noqa: E402
from app.reconstruct import build_reconstruct_user, RECON_PROMPT_VERSION  # noqa: E402
from phase15_gen import RECON_CTX_USER, RECON_CTX_SYSTEM  # noqa: E402
from factorial_d import USER as CTXONLY_USER, SYS as CTXONLY_SYS  # noqa: E402


def gen_group(s, exp, frames, segs, kind, builder, purpose, pv):
    existing = {(c.frame_id, c.model, c.prompt_version) for c in
                s.query(Candidate).filter_by(experiment_id=exp.id)}
    n = 0
    for f in frames:
        seg = segs.get(f.segment_id)
        if not seg or not f.payload:
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


for eid in EIDS:
    print("== candidates", eid, flush=True)
    with db.session() as s:
        exp = s.get(Experiment, eid)
        frames = s.query(Frame).filter_by(experiment_id=eid, granularity="L", is_primary=True)\
                  .filter(Frame.status != "failed").all()
        segs = {x.id: x for x in s.query(Segment).filter(
            Segment.id.in_({f.segment_id for f in frames})).all()}
        print(f"  L-frames 可用: {len(frames)}", flush=True)
        gen_group(s, exp, frames, segs, "B0", lambda s, seg, f: build_reconstruct_user(f.payload),
                  "reconstruct", RECON_PROMPT_VERSION)
        gen_group(s, exp, frames, segs, "C", b_ctx, "reconstruct_ctx", "recon_ctx_v1")
        gen_group(s, exp, frames, segs, "D", b_ctxonly, "reconstruct_ctxonly", "recon_ctxonly_v1")
print("四语料矩阵数据全部就绪", flush=True)
