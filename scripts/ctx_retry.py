"""C 组失败候选重发：走 gateway.chat（自带空文本重试/max_tokens 加倍）。"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from app import db
from app.context_ablation import neighbors
from app.gateway import chat
from app.models import Candidate, Experiment, Frame, Segment
from app.prompt_render import render
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内
from scripts.phase15_gen import RECON_CTX_USER, RECON_CTX_SYSTEM, CTX_MODELS

with db.session() as s:
    exp = s.get(Experiment, "EXP-0911-B82D")
    segs = {x.id: x for x in s.query(Segment).filter(
        Segment.id.in_(exp.config["segment_ids"])).all()}
    failed = s.query(Candidate).filter_by(experiment_id=exp.id,
                                          prompt_version="recon_ctx_v1", status="failed").all()
    # 批量防呆①：重发用的就是这批候选自己记着的 model，池外名字=再failed一遍
    pf.require_models(sorted({c.model for c in failed}), source="ctx_retry")
    n = 0
    for c in failed:
        frame = s.get(Frame, c.frame_id)
        seg = segs.get(c.segment_id)
        if not frame or not seg:
            continue
        nb = neighbors(s, seg)
        user = render(RECON_CTX_USER,
                      prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                      prev1=(nb["prev1"].text if nb["prev1"] else "（无）"),
                      frame_json=json.dumps(frame.payload, ensure_ascii=False))
        try:
            r = chat(model=c.model, system=RECON_CTX_SYSTEM, user=user,
                     purpose="reconstruct_ctx", prompt_version="recon_ctx_v1",
                     temperature=0.7, max_tokens=3000)
            c.text, c.status, c.error = r.text.strip(), "ok" if r.text.strip() else "failed", None
            c.tokens_in, c.tokens_out, c.latency_ms = r.tokens_in, r.tokens_out, r.latency_ms
            s.commit()
            n += 1
        except Exception as e:
            c.error = str(e)[:200]
            s.commit()
        print(n, c.model, c.status, flush=True)
print("ctx retry done")
