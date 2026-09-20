"""跨语料矩阵第一步：4 语料 × 50 合格段（v2）× S/M/L 抽取（双抽取器）。只跑 extract，
judges/candidates 等 Matrix 定稿后由专门脚本驱动。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db, experiments
from app.models import Experiment, Work
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内

# 批量防呆①（P0 死 id 事故）：整轮只有抽帧一类调用，池外名字=四语料全灭
pf.require_models(config.EXTRACTOR_MODELS, source="corpus_matrix_extract")

CORPORA = [("琼明神女录（精校）", "corpus01"),
           ("将夜（猫腻）", "corpus02"),
           ("凡人修仙传（忘语）", "corpus03"),
           ("斗罗大陆（唐家三少）", "corpus04")]

for title, tag in CORPORA:
    with db.session() as s:
        w = s.query(Work).filter_by(title=title).first()
        exp = experiments.create_experiment(s, {
            "n_segments": 50, "granularities": ["S", "M", "L"],
            "work_ids": [w.id], "seg_version": 2, "eligible_only": True,
            "temperatures": [0.5, 0.9], "samples_per_pair": 1,
            "judge_human_naturalness": False})
        eid = exp.id
        print(f"[{tag}] {title} -> {eid}", flush=True)
    with db.session() as s:
        exp = s.get(Experiment, eid)
        experiments.stage_extract_frames(s, exp)
    with db.session() as s:
        exp = s.get(Experiment, eid)
        st = (exp.stats or {}).get("stages", {}).get("extract_frames", {})
        print(f"[{tag}] extract: ok={st.get('ok')} repaired={st.get('repaired')} failed={st.get('failed')}", flush=True)
print("四语料抽取全部完成")
