"""Human Prediction Matrix 口径回归（2026-09-14）。

锁死 adversarial agreement 的**方向**：评委认出 AI ⇒ 认为另一边（human）更像人 ⇒ 评委站 human。

原实现写的是 `judge_says_human_won = (guess_hit_ai is False)`，方向反了。
反证：一个 100% 认出 AI 的完美评委，在用户 93% 判 human 胜的数据上，
原口径只给 7% agreement（显然错误），正确口径给 93%。

另锁一条（P0 死模型 id · 2026-09-20）：评委改过名，历史判定存的是旧 id——
矩阵按在册名查**也必须**把它们算到这位评委头上，不许少一列。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import judge_matrix as jm
from app import config, db
from app.models import Candidate, Experiment, Frame, JudgeRun, ReviewItem, Segment, Work

JUDGE = "moonshotai/kimi-k3"


def _seed_case(exp_id, batch, *, hit_ai, user_won):
    """造 1 组：候选 + adversarial judge 判定 + 用户判定。"""
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
        w = Work(title="t-" + exp_id, source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他推门进来，屋里没人。",
                      n_sentences=1, n_chars=12)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                      anon_label="X0001", model="m", prompt_version="pv", text="候选文本")
        s.add(c)
        s.flush()
        s.add(JudgeRun(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                       judge_kind="adversarial", model=JUDGE, prompt_version="pv",
                       verdict={"guess_hit_ai": hit_ai, "guess_ai": "A"},
                       abstain=False))
        s.add(ReviewItem(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                         priority=1.0, reasons=[f"batch_{batch}"], status="done",
                         human_verdict={"winner_resolved": user_won,
                                        "winner_raw": "A"}))
        s.commit()


def _agreement(exp_id, batch):
    rows = jm.main(exp_id, batch)
    return {r["judge"]: r["agreement"] for r in rows}


def test_judge_that_spots_ai_agrees_with_user_preferring_human(capsys):
    """核心口径：认出 AI + 用户判 human 胜 → agreement = 1.0。"""
    _seed_case("EXP-JM1", "jm1", hit_ai=True, user_won="human")
    assert _agreement("EXP-JM1", "jm1")[JUDGE] == 1.0


def test_judge_that_misses_ai_disagrees_with_user_preferring_human(capsys):
    """评委把 human 当成 AI → 评委站 candidate，与用户相反 → agreement = 0.0。"""
    _seed_case("EXP-JM2", "jm2", hit_ai=False, user_won="human")
    assert _agreement("EXP-JM2", "jm2")[JUDGE] == 0.0


def test_perfect_judge_scores_human_win_rate(capsys):
    """反证用例：完美评委（永远认出 AI）的 agreement 应等于用户判 human 的胜率。

    用 3:1 的不对称胜率——若用 2:2，正确/反向两种实现都会得 0.5，测试就失去鉴别力。
    正确口径 → 0.75；反向口径 → 0.25。
    """
    for user_won in ("human", "human", "human", "candidate"):
        _seed_case("EXP-JM3", "jm3", hit_ai=True, user_won=user_won)
    assert _agreement("EXP-JM3", "jm3")[JUDGE] == 0.75


def test_both_bad_excluded_from_denominator(capsys):
    """both_bad / cant_judge 不计入 agreement 分母。"""
    _seed_case("EXP-JM4", "jm4", hit_ai=True, user_won="human")
    _seed_case("EXP-JM4", "jm4", hit_ai=False, user_won="both_bad")
    rows = jm.main("EXP-JM4", "jm4")
    assert rows and rows[0]["n"] == 1, "both_bad 不应进入分母"
    assert rows[0]["agreement"] == 1.0


# ── κ / 响应偏差（2026-09-14：agreement 会被基础率主导，必须看 κ）─────────────

def test_metrics_perfect_agreement_kappa_one():
    # (user_human, judge_human)：两次都一致
    m = jm._metrics([(True, True), (False, False)])
    assert m["agreement"] == 1.0
    assert m["kappa"] == 1.0


def test_metrics_constant_judge_gives_zero_kappa_despite_half_agreement():
    """关键：评委永远挑 candidate 时 agreement 可以正好 0.5，但 κ = 0（零信息）。

    这正是本项目 Judge Gate 的陷阱——agreement 0.5 看起来像"接近随机"，
    实际是"完全没有信息"，因为完全由用户基础率决定。
    """
    pairs = [(True, False), (True, False), (False, False), (False, False)]
    m = jm._metrics(pairs)
    assert m["agreement"] == 0.5
    assert m["kappa"] == 0.0
    assert m["judge_picks_candidate"] == 1.0


def test_metrics_reports_response_bias():
    pairs = [(True, False)] * 9 + [(False, False)] * 1
    m = jm._metrics(pairs)
    assert m["judge_picks_candidate"] == 1.0
    assert m["user_picks_human"] == 0.9
    assert m["kappa"] == 0.0


# ── preference 口径（口径 B）────────────────────────────────────────────────

def _seed_pref(exp_id, batch, *, judge_pick, user_won, resolved=None):
    """judge_pick: 'human'|'candidate'|'equal'；写一条 preference JudgeRun。"""
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
        w = Work(title="t-" + exp_id, source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他推门进来，屋里没人。",
                      n_sentences=1, n_chars=12)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                      anon_label="X0001", model="m", prompt_version="pv", text="候选文本")
        s.add(c)
        s.flush()
        s.add(JudgeRun(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                       judge_kind="preference", model=JUDGE, prompt_version="judge_preference_v1",
                       verdict={"winner": "A", "winner_resolved": resolved or judge_pick,
                                "human_was_a": True},
                       abstain=False))
        s.add(ReviewItem(experiment_id=exp_id, subject_type="candidate", subject_id=c.id,
                         priority=1.0, reasons=[f"batch_{batch}"], status="done",
                         human_verdict={"winner_resolved": user_won, "winner_raw": "A"}))
        s.commit()


def test_preference_rows_agreement_and_bias(capsys):
    """preference 口径：评委与用户都表态才进分母；agree/κ/偏差都要对。"""
    _seed_pref("EXP-JM5", "jm5", judge_pick="candidate", user_won="human")
    _seed_pref("EXP-JM5", "jm5", judge_pick="candidate", user_won="candidate")
    with db.session() as s:
        from app.models import ReviewItem as RI
        done = s.query(RI).filter_by(experiment_id="EXP-JM5", status="done").all()
        user_call = {r.subject_id: r.human_verdict["winner_resolved"] == "human" for r in done}
        rows = jm.preference_rows(s, "EXP-JM5", user_call, [JUDGE])
    r = rows[0]
    assert r["n"] == 2
    assert r["agreement"] == 0.5          # 一次对一次错
    assert r["judge_picks_candidate"] == 1.0   # 评委全挑 candidate
    assert r["kappa"] == 0.0              # 但零信息
    assert r["non_decisive"] == 0 and r["missing"] == 0


def test_preference_rows_non_decisive_excluded(capsys):
    """评委答 equal → 不进分母，计 non_decisive。"""
    _seed_pref("EXP-JM6", "jm6", judge_pick="equal", user_won="human")
    _seed_pref("EXP-JM6", "jm6", judge_pick="human", user_won="human")
    with db.session() as s:
        from app.models import ReviewItem as RI
        done = s.query(RI).filter_by(experiment_id="EXP-JM6", status="done").all()
        user_call = {r.subject_id: r.human_verdict["winner_resolved"] == "human" for r in done}
        rows = jm.preference_rows(s, "EXP-JM6", user_call, [JUDGE])
    r = rows[0]
    assert r["n"] == 1 and r["non_decisive"] == 1
    assert r["agreement"] == 1.0


def test_judgements_written_under_the_dead_id_still_count():
    """改名前落库的判定，按在册名查也要查得到（P0 死 id 事故的**查询侧**）。

    不修的形状不是报错，是**少一列**：库里 3.4k 行 deepseek 判定写于改名前，
    `model=新名` 精确匹配会把整位评委读成"没有判定"，矩阵与 κ 就此缺一家而无声。
    """
    dead = next(iter(config.DEAD_MODEL_ALIASES))
    live = config.DEAD_MODEL_ALIASES[dead]
    exp = "EXP-JM-DEADID"
    _seed_case(exp, "deadid", hit_ai=True, user_won="human")
    with db.session() as s:
        cid = s.query(Candidate).filter_by(experiment_id=exp).first().id
        s.add(JudgeRun(experiment_id=exp, subject_type="candidate", subject_id=cid,
                       judge_kind="adversarial", model=dead, prompt_version="pv",
                       verdict={"guess_hit_ai": True, "guess_ai": "A"}, abstain=False))
        s.add(JudgeRun(experiment_id=exp, subject_type="candidate", subject_id=cid,
                       judge_kind="preference", model=dead, prompt_version="pv",
                       verdict={"winner_resolved": "human"}, abstain=False))
        s.commit()
        adv = jm._adversarial_rows(s, exp, {cid: True}, (live,))
        pref = jm.preference_rows(s, exp, {cid: True}, (live,))
    assert [r["judge"] for r in adv] == [live] and adv[0]["n"] == 1, \
        "旧 id 下的 adversarial 判定没算到这位评委头上"
    assert pref[0]["n"] == 1 and pref[0]["missing"] == 0, \
        "旧 id 下的 preference 判定被读成 missing → 这一位在矩阵里静默蒸发"
