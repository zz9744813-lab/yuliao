"""Phase 1 Calibration 新模块回归测试：integrity / near_dup / ablation / review refresh。"""
from __future__ import annotations

import json

from app import corpus, db, experiments, near_dup, segment_integrity as si
from app.context_ablation import window_texts, render_context_block
from app.models import Experiment, ReviewItem, Segment
from app.review import refresh_review_queue

BOOK = (
    "翌日清晨，陈守拙推开祠堂的门。供桌上的香灰积了三指厚。他挽起袖子，一点一点擦干净，"
    "又把新拆的香束摆正。\n"
    "「三叔公，今年收成好，给您多上两炷。」他低声念叨着，退后三步，郑重磕了头。\n"
    "族里人陆续到了。陈守业站在廊下没动，只远远看着，手里的茶盏早凉透了。\n"
    "「守拙，账目拿出来罢。」陈守业终于开口，声音不高，祠堂里却人人听见了。\n"
) * 6


def _mk_work_and_exp(n_segments: int = 4, **cfg):
    db.init_db()
    with db.session() as s:
        work = corpus.add_work(s, title="phase1测试书", text=BOOK, source="test:phase1")
        exp = experiments.create_experiment(s, {"n_segments": n_segments,
                                                "granularities": ["S"],
                                                "work_ids": [work.id], **cfg})
        return work.id, exp.id


def test_integrity_orphan_quote_not_eligible():
    r = si.analyze("」她话语温柔而清冷。林玄言点了点头。", ordinal=3)
    assert r["quote_integrity"] < 1.0 and r["context_dependency"] >= 0.5
    assert not r["eligible"]


def test_integrity_clean_scene_start_eligible():
    text = ("翌日清晨，陈守拙推开祠堂的门。供桌上的香灰积了三指厚。他挽起袖子，"
            "一点一点擦干净。族里人陆续到了，谁也没有先开口。")
    r = si.analyze(text, ordinal=7)
    assert r["quote_integrity"] == 1.0 and r["truncation_risk"] <= 0.3
    assert r["eligible"]


def test_integrity_truncation_risk():
    r = si.analyze("他抬起头，看向远处的山，", ordinal=2)
    assert r["truncation_risk"] >= 0.5 and not r["eligible"]


def test_integrity_flag_schema():
    r = si.analyze("他推门进来。桌上茶还温着。", ordinal=5)
    assert {"quote_integrity", "antecedent_integrity", "dialogue_integrity",
            "scene_boundary", "context_dependency", "truncation_risk",
            "eligible"} <= set(r)
    assert isinstance(r["eligible"], bool)


def test_near_dup_self_and_far():
    a = "他站在廊下没有说话，手里的茶盏早凉透了，雨顺着檐角落成一条线。"
    self_r = near_dup.near_dup_report(a, a, use_embedding=False)
    far_r = near_dup.near_dup_report(a, "雪落在北岭的松林里，猎户收起铁夹，把今日第三只灰兔系上腰间。",
                                     use_embedding=False)
    assert self_r["is_near_dup"] and self_r["exact"] == 1.0
    assert not far_r["is_near_dup"]
    # simhash 汉明距离小、ngram 0
    assert far_r["simhash_hamming"] > 12 and far_r["ngram"] == 0.0


def test_benchmark_split_excludes_used_segments():
    work_id, exp_id = _mk_work_and_exp(4)
    with db.session() as s:
        exp = s.get(Experiment, exp_id)
        used = exp.config["segment_ids"]
        r = near_dup.split_benchmark(work_id=work_id, n=10, exclude_ids=used, seed=5)
        assert r["marked"] > 0
        roles = {x.id: x.role for x in s.query(Segment).filter_by(work_id=work_id).all()}
        assert all(roles[sid] is None for sid in used), "实验已用段不得标成 benchmark"
        assert sum(1 for v in roles.values() if v == "benchmark") >= 1
        # 训练采样域必须排除 benchmark
        pool = near_dup.train_sampling_pool(s, [work_id])
        assert all(x.role != "benchmark" for x in pool)


def test_context_ablation_windows():
    work_id, exp_id = _mk_work_and_exp(6)
    with db.session() as s:
        seg = (s.query(Segment).filter_by(work_id=work_id)
               .filter(Segment.ordinal >= 2, Segment.ordinal <= 10).first())
        only = window_texts(s, seg, "segment_only")
        p1 = window_texts(s, seg, "prev1_current")
        full = window_texts(s, seg, "prev2_current_next1")
        assert only == [seg.text]
        assert p1[-1] == seg.text and len(p1) == 2
        assert full[2] == seg.text and 3 <= len(full) <= 4
        assert "【当前段】" in render_context_block(p1)


def test_review_refresh_fills_to_target_and_idempotent():
    work_id, exp_id = _mk_work_and_exp(4, recon_models=["m1"], temperatures=[0.5],
                                       samples_per_pair=1)
    with db.session() as s:
        exp = s.get(Experiment, exp_id)
        experiments.stage_extract_frames(s, exp)
        experiments.stage_reconstruct(s, exp)
    with db.session() as s:
        r1 = refresh_review_queue(s, exp_id, target=300)
        r2 = refresh_review_queue(s, exp_id, target=300)
        total = s.query(ReviewItem).filter_by(experiment_id=exp_id).count()
    assert r1["added"] > 0
    assert r2["added"] == 0, "二次 refresh 不得重复入队"
    assert total == r1["total"]
    reasons_seen = {rr for item in s.query(ReviewItem).filter_by(experiment_id=exp_id).all()
                    for rr in (item.reasons or [])}
    assert "random_baseline" in reasons_seen or r1["added"] < 3
