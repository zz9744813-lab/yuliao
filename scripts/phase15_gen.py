"""Phase 1.5：① M 缺口根因诊断（raw 直调，存证 finish_reason/latency/tokens）
② Context-conditioned Reconstruction：为 30 个 L-frame 生成 C 组候选（prev2+prev1+Frame）。

C 组候选 prompt_version='recon_ctx_v1'，与 B 组（Frame-only）同模型同温度便于配对比较。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db
from app.context_ablation import neighbors
from app.frames_prompts import EXTRACT_V1, PROMPT_VERSION
from app.ids import new_id
from app.models import Candidate, Experiment, Frame, Segment
from app.prompt_render import render
from app.reconstruct import build_reconstruct_user
import httpx

EXP = "EXP-0911-B82D"
CTX_MODELS = [config.DEFAULT_LLM_MODEL, "meta/muse-spark-1.3-contributor"]

RECON_CTX_SYSTEM = "你是中文小说写作者，只输出正文。"
RECON_CTX_USER = """你在续写一部长篇小说。下面给出：前文（最近两段）、以及接下来这一段必须完成的骨架（Frame JSON）。

要求：
- 只输出"当前段"的正文，2~6 句，不重复前文
- 与前文的语流、指代、场景无缝衔接（不要重新起景、不要重复交代已知信息）
- 覆盖骨架的全部信息点；骨架中没有的信息不要添加
- 禁止复制骨架或前文中超过 6 字的原文连续字串

【前文-2】{prev2}

【前文-1】{prev1}

【当前段骨架】
{frame_json}

现在写"当前段"："""


def diag_m_frames():
    """对失败的 M-frame 直调一次，完整存证。"""
    out = []
    with db.session() as s:
        exp = s.get(Experiment, EXP)
        seg_map = {x.id: x for x in s.query(Segment).filter(
            Segment.id.in_(exp.config["segment_ids"])).all()}
        bad = s.query(Frame).filter_by(experiment_id=EXP, granularity="M",
                                       extractor_model="moonshotai/kimi-k3",
                                       status="failed").all()
        for f in bad[:6]:
            seg = seg_map.get(f.segment_id)
            if not seg:
                continue
            prompt = render(EXTRACT_V1["M"], text=seg.text)
            t0 = time.time()
            rec = {"frame_id": f.id, "segment_id": f.segment_id}
            try:
                with httpx.Client(timeout=300) as cli:
                    r = cli.post(f"{config.GATEWAY_BASE_URL}/chat/completions",
                                 headers={"Authorization": f"Bearer {config.GATEWAY_API_KEY}"},
                                 json={"model": "moonshotai/kimi-k3",
                                       "messages": [{"role": "system", "content": "你是语义骨架抽取器，只输出 JSON。"},
                                                    {"role": "user", "content": prompt}],
                                       "temperature": 0.2, "max_tokens": 4096})
                rec["http"] = r.status_code
                if r.status_code == 200:
                    d = r.json()
                    ch0 = d["choices"][0]
                    rec.update({"finish_reason": ch0.get("finish_reason"),
                                "content_len": len(ch0.get("message", {}).get("content") or ""),
                                "has_reasoning": bool((ch0.get("message") or {}).get("reasoning_content")),
                                "tokens": d.get("usage")})
                    rec["content_head"] = (ch0.get("message", {}).get("content") or "")[:120]
                else:
                    rec["body"] = r.text[:200]
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
            rec["latency_s"] = round(time.time() - t0, 1)
            out.append(rec)
    Path("data/m_gap_diagnosis.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("M 诊断:", json.dumps(out, ensure_ascii=False)[:600])


def gen_ctx_candidates():
    """C 组：prev2+prev1 + Frame → 当前段。"""
    n = 0
    with db.session() as s:
        exp = s.get(Experiment, EXP)
        seg_map = {x.id: x for x in s.query(Segment).filter(
            Segment.id.in_(exp.config["segment_ids"])).all()}
        frames = s.query(Frame).filter_by(experiment_id=EXP, granularity="L",
                                          is_primary=True).filter(
            Frame.status != "failed").all()
        existing = {(c.frame_id, c.model, c.prompt_version) for c in
                    s.query(Candidate).filter_by(experiment_id=EXP)}
        for f in frames:
            seg = seg_map.get(f.segment_id)
            if not seg:
                continue
            nb = neighbors(s, seg)
            for model in CTX_MODELS:
                key = (f.id, model, "recon_ctx_v1")
                if key in existing:
                    continue
                user = render(RECON_CTX_USER,
                              prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                              prev1=(nb["prev1"].text if nb["prev1"] else "（无）"),
                              frame_json=json.dumps(f.payload, ensure_ascii=False))
                t0 = time.time()
                try:
                    with httpx.Client(timeout=300) as cli:
                        r = cli.post(f"{config.GATEWAY_BASE_URL}/chat/completions",
                                     headers={"Authorization": f"Bearer {config.GATEWAY_API_KEY}"},
                                     json={"model": model,
                                           "messages": [{"role": "system", "content": RECON_CTX_SYSTEM},
                                                        {"role": "user", "content": user}],
                                           "temperature": 0.7, "max_tokens": 3000})
                    d = r.json()
                    text = ((d["choices"][0].get("message") or {}).get("content") or "").strip()
                    usage = d.get("usage") or {}
                    s.add(Candidate(
                        experiment_id=EXP, frame_id=f.id, segment_id=f.segment_id,
                        anon_label=f"CX{n:03d}", model=model, temperature=0.7,
                        seed=None, prompt_version="recon_ctx_v1", text=text,
                        tokens_in=int(usage.get("prompt_tokens") or 0),
                        tokens_out=int(usage.get("completion_tokens") or 0),
                        latency_ms=int((time.time() - t0) * 1000),
                        status="ok" if text else "failed",
                        error=None if text else "empty content",
                    ))
                    s.commit()
                    n += 1
                except Exception as e:
                    s.add(Candidate(
                        experiment_id=EXP, frame_id=f.id, segment_id=f.segment_id,
                        anon_label=f"CX{n:03d}", model=model, temperature=0.7,
                        seed=None, prompt_version="recon_ctx_v1", text="",
                        tokens_in=0, tokens_out=0,
                        latency_ms=int((time.time() - t0) * 1000),
                        status="failed", error=str(e)[:300]))
                    s.commit()
                    n += 1
    print("C 组候选:", n)


if __name__ == "__main__":
    diag_m_frames()
    gen_ctx_candidates()
