"""端到端 mock 冒烟：全流程十个 stage 一条链跑通，产物齐全。"""
from app import corpus, db, experiments
from app.models import (Candidate, Frame, JudgeRun, LeakageScore, ReportFile,
                        ResidualDet, ResidualSem, ReviewItem, Proposition)

SEED_TEXT = (
    "天擦黑的时候他进了院子。院门没闩，他一推就开。屋里点着灯，人影晃了一下。\n"
    "\"回来了？\"她问。他没有答，把斗笠摘下来挂在门后。\n"
    "桌上摆着饭菜，还冒着热气。他坐下来，先喝了一口汤。汤是温的，早就放了好一阵了。\n"
    "她坐在对面看他的动作，始终没有再开口。窗外有虫鸣，一声长一声短。\n"
    "他放下碗，说我明早走。她点了点头，转身去收拾灶膛里的火。\n"
    "火光映着她的侧脸，一闪一闪。他忽然觉得喉咙有些发紧，却什么也没说。\n"
)


def test_full_pipeline_mock():
    db.init_db()
    with db.session() as s:
        corpus.add_work(s, title="测试书", text=SEED_TEXT * 3, source="test:seed")
        s.commit()

        exp = experiments.create_experiment(s, {
            "n_segments": 3,
            "granularities": ["S", "M"],
            "recon_models": ["mA", "mB"],
            "temperatures": [0.5],
            "samples_per_pair": 1,
            "adversarial_k": 2,
            "concurrency": 2,
        })
        exp_id = exp.id

    result = experiments.run_experiment(exp_id)
    assert result.status == "done", result.error

    with db.session() as s:
        segs = s.query(Proposition).all()
        assert len(segs) >= 1  # 命题分解发生了

        frames = s.query(Frame).filter_by(experiment_id=exp_id).all()
        # 3 段 × 2 粒度 × 2 抽取器（默认双抽）
        assert len(frames) == 3 * 2 * len(exp.config["extractors"])
        primary = [f for f in frames if f.is_primary]
        assert len(primary) == 6

        # 每 primary frame：char6/word3/rare + adversarial
        for f in primary:
            layers = {lk.layer for lk in s.query(LeakageScore).filter_by(frame_id=f.id).all()}
            assert {"char6", "word3", "rare", "adversarial"} <= layers

        cands = s.query(Candidate).filter_by(experiment_id=exp_id).all()
        assert len(cands) == 6 * 2 * 1 * 1  # 6 frames × 2 models × 1 temp × 1 sample
        labels = [c.anon_label for c in cands]
        assert all(x.startswith("X") for x in labels)
        assert len(labels) == len(set(labels)), "P0-3 回归：匿名标号必须唯一"

        cand_ids = {c.id for c in cands}
        assert s.query(ResidualDet).filter(ResidualDet.candidate_id.in_(cand_ids)).count() == len(cands)
        assert s.query(ResidualSem).filter(ResidualSem.candidate_id.in_(cand_ids)).count() == len(cands)

        # 每 candidate：semantic/naturalness/adversarial；human 侧：naturalness × 每段
        n_cand_judges = s.query(JudgeRun).filter_by(
            experiment_id=exp_id, subject_type="candidate").count()
        n_hum_judges = s.query(JudgeRun).filter_by(
            experiment_id=exp_id, subject_type="human_segment").count()
        assert n_cand_judges == len(cands) * 3
        assert n_hum_judges >= 1

        # active review 队列生成（mock 评分下可能低优先级；不强求非空，但表要可查）
        s.query(ReviewItem).all()

        reports = s.query(ReportFile).filter_by(experiment_id=exp_id).all()
        kinds = {r.kind for r in reports}
        assert {"calibration_md", "calibration_json"} <= kinds
