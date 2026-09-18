"""D 组（Context-only，无 Frame）：prev2+prev1 → 续写下一段。prompt_version=recon_ctxonly_v1。
与 C 组同模型同温度，构成 2×2 的第四格。"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db
from app.context_ablation import neighbors
from app.gateway import chat
from app.ids import new_id
from app.models import Candidate, Experiment, Segment
from app.prompt_render import render

USER = """你在续写一部长篇小说。下面是最近两段前文。

要求：
- 只输出"下一段"的正文，2~6 句，不重复前文
- 与前文的语流、指代、场景无缝衔接，自然推进剧情
- 不确定剧情走向时按最自然的方式推进；禁止复制前文超过 6 字的连续字串

【前文-2】{prev2}

【前文-1】{prev1}

写"下一段"："""
SYS = "你是中文小说写作者，只输出正文。"
MODELS = ["deepseek/deepseek-v4.1-flash", "meta/muse-spark-1.3-contributor"]

def main():
  with db.session() as s:
    exp = s.get(Experiment, "EXP-0911-B82D")
    segs = {x.id: x for x in s.query(Segment).filter(
        Segment.id.in_(exp.config["segment_ids"])).all()}
    existing = {(c.frame_id, c.model, c.prompt_version) for c in
                s.query(Candidate).filter_by(experiment_id=exp.id)}
    from app.models import Frame
    l_frames = s.query(Frame).filter_by(experiment_id=exp.id, granularity="L",
                                        is_primary=True).filter(Frame.status != "failed").all()
    n = 0
    for f in l_frames:
        seg = segs.get(f.segment_id)
        if not seg: continue
        nb = neighbors(s, seg)
        for model in MODELS:
            key = (f.id, model, "recon_ctxonly_v1")
            if key in existing: continue
            user = render(USER, prev2=(nb["prev2"].text if nb["prev2"] else "（无）"),
                          prev1=(nb["prev1"].text if nb["prev1"] else "（无）"))
            try:
                r = chat(model=model, system=SYS, user=user, purpose="reconstruct_ctxonly",
                         prompt_version="recon_ctxonly_v1", temperature=0.7, max_tokens=3000)
                text, st, err, ti, to, lat = r.text.strip(), ("ok" if r.text.strip() else "failed"), None, r.tokens_in, r.tokens_out, r.latency_ms
            except Exception as e:
                text, st, err, ti, to, lat = "", "failed", str(e)[:200], 0, 0, 0
            s.add(Candidate(experiment_id=exp.id, frame_id=f.id, segment_id=f.segment_id,
                            anon_label=f"DX{n:03d}", model=model, temperature=0.7, seed=None,
                            prompt_version="recon_ctxonly_v1", text=text,
                            tokens_in=ti, tokens_out=to, latency_ms=lat, status=st, error=err))
            s.commit(); n += 1
            print(n, model, st, flush=True)
  print("D 组完成")


if __name__ == "__main__":
    main()
