"""Entity-normalized leakage：琼明 84 个 primary frame 全量跑（§六）。"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entity_leakage as el
from app.models import Experiment, Frame, LeakageScore, Segment

with db.session() as s:
    exp = s.get(Experiment, "EXP-0911-B82D")
    segs = {x.id: x for x in s.query(Segment).filter(
        Segment.id.in_(exp.config["segment_ids"])).all()}
    lex = el.build_entity_lexicon(s, "WK-6e5d2623", sample=400)
    print("实体词表:", len(lex), "个", lex[:12], flush=True)
    frames = s.query(Frame).filter_by(experiment_id=exp.id, is_primary=True)\
              .filter(Frame.status != "failed").all()
    done_keys = {(lk.frame_id, lk.layer) for lk in s.query(LeakageScore).all()
                 if lk.layer.startswith(("char6_norm", "word3_norm", "adversarial_entity"))}
    n = 0
    for f in frames:
        seg = segs.get(f.segment_id)
        if not seg or not f.payload:
            continue
        rep = el.entity_normalized_report(
            json.dumps(f.payload, ensure_ascii=False), seg.text, lex, adversarial_k=8)
        rows = [("char6_norm", rep["char6_norm"], {"score": rep["char6_norm"]}),
                ("word3_norm", rep["word3_norm"], {"score": rep["word3_norm"]}),
                ("adversarial_entity", rep["adversarial_norm"]["score"] or 0.0,
                 {**rep["adversarial_norm"], "entities": rep["entities_in_pair"][:8],
                  "n_entities": rep["n_entities_normalized"]})]
        for layer, score, detail in rows:
            if (f.id, layer) in done_keys:
                continue
            s.add(LeakageScore(frame_id=f.id, layer=layer, score=score or 0.0, detail=detail))
            s.commit(); n += 1
        if (n % 20) < 3:
            print(f"  {f.granularity} frame {f.id[:12]} adv_norm={rep['adversarial_norm']['score']}", flush=True)
    print("entity-normalized 完成:", n, "行")
