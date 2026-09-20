"""补齐 6 个 M-frame（用户指令：补齐 M + 确认根因；根因=旧超时误杀，已修 300s）。
只针对 failed 行，不触碰实验其他产物；下游用幂等 stage 补齐。"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from app import config, db, experiments
from app.frames_prompts import EXTRACT_V1, PROMPT_VERSION
from app.models import Experiment, Frame, Segment
from app.prompt_render import render
from app.residual_sem import parse_llm_json
from app.gateway import chat
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内

EXP = "EXP-0911-B82D"
# 批量防呆①（P0 死 id 事故）：这里连抽带下游 stage，名字错了会一路"补齐 0 个"
pf.require_models([*config.EXTRACTOR_MODELS, "moonshotai/kimi-k3"], source="m_heal")
with db.session() as s:
    exp = s.get(Experiment, EXP)
    segs = {x.id: x for x in s.query(Segment).filter(
        Segment.id.in_(exp.config["segment_ids"])).all()}
    bad = s.query(Frame).filter_by(experiment_id=EXP, granularity="M",
                                   extractor_model="moonshotai/kimi-k3",
                                   status="failed").all()
    healed = 0
    for f in bad:
        seg = segs[f.segment_id]
        prompt = render(EXTRACT_V1["M"], text=seg.text)
        try:
            r = chat(model="moonshotai/kimi-k3", system="你是语义骨架抽取器，只输出 JSON。",
                     user=prompt, purpose="extract:M", prompt_version=PROMPT_VERSION,
                     temperature=0.2, max_tokens=4096)
            payload = parse_llm_json(r.text)
            if payload is None:
                continue
            f.payload, f.status, f.raw_output = payload, "ok", r.text[:2000]
            s.commit()
            healed += 1
            print("healed", f.id, flush=True)
        except Exception as e:
            print("still failed", f.id, str(e)[:80], flush=True)
    print("healed:", healed)
    # 下游幂等补齐（不经过冻结门，直接调 stage 函数，只填缺口）
    experiments.stage_leakage_static(s, exp)
    experiments.stage_reconstruct(s, exp)
    experiments.stage_judges(s, exp)
print("M 补齐 + 下游补齐完成")
