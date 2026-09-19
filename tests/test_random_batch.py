"""随机抽样批回归（2026-09-14）。

核心不变量：
1. **抽样池必须是全部 ok 候选，不能是已有 review_items**。
   后者本身按优先级挑出来的（human_upset / judge_disagreement 过采样），
   在其中抽样仍是偏样本 —— 而随机批的全部意义就是给 Gate 一个**无偏**估计。
2. **抽样池只含平行文本候选**（`BLIND_REVIEW_PROMPT_VERSIONS`）。
   消融条件 `recon_ctxonly_v1` 是"只给上下文自由续写"，与人类段不是同一内容；
   混进盲评会让 A/B 变成"牛头不对马嘴"（2026-09-14 集霸实测撞到）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import make_random_batch as MRB
from make_random_batch import looks_watermarked
from app import db
from app.config import BLIND_REVIEW_PROMPT_VERSIONS
from app.models import Candidate, Experiment, Frame, ReviewItem, Segment, Work


def _seed(exp_id, n_cand, n_queued, prompt_version="reconstruct_v1"):
    """造 n_cand 个候选，其中只有前 n_queued 个有 review_item（模拟优先级队列）。"""
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
        for i in range(n_cand):
            c = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                          anon_label=f"X{i:04d}", model="m", prompt_version=prompt_version,
                          text=f"这是一段候选文本，编号 {i}，长度够长以便通过最小长度过滤。")
            s.add(c)
            s.flush()
            if i < n_queued:
                s.add(ReviewItem(experiment_id=exp_id, subject_type="candidate",
                                 subject_id=c.id, priority=float(n_cand - i),
                                 reasons=["human_upset"], status="pending"))
        s.commit()


def test_pool_is_all_candidates_not_just_queue():
    """抽样池 = 全部候选，不是只有 review_item 的那些。"""
    _seed("EXP-RB10", 40, 5)          # 40 候选，只有 5 条在队列里
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB10", 20, "t10", seed=1, dry_run=True)
    assert info["pool"] == 40, f"池子应含全部 40 条候选，实得 {info['pool']}"
    assert len(info["picked"]) == 20


def test_pool_excludes_non_parallel_ablation_candidates():
    """核心回归：非平行（自由续写）的消融候选不得进入盲评池。

    2026-09-14 事故：r25 混进 3 条 recon_ctxonly_v1，A 讲"献出妹妹换命"、
    B 讲"钟华弹俞小塘额头"——"哪边更好"这个问题根本不成立。
    """
    _seed("EXP-RB20", 30, 5, prompt_version="recon_ctxonly_v1")
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB20", 10, "t20", seed=1, dry_run=True)
    assert info["pool"] == 0, f"非平行候选必须被排除，实得池子 {info['pool']} 条"
    assert info["picked"] == []


def test_pool_keeps_all_whitelisted_versions():
    """白名单里的版本都要保留——不能只认 reconstruct_v1。"""
    assert "reconstruct_v1" in BLIND_REVIEW_PROMPT_VERSIONS
    assert "recon_ctx_v1" in BLIND_REVIEW_PROMPT_VERSIONS
    assert "recon_ctxonly_v1" not in BLIND_REVIEW_PROMPT_VERSIONS
    _seed("EXP-RB21", 12, 2, prompt_version="recon_ctx_v1")
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB21", 5, "t21", seed=1, dry_run=True)
    assert info["pool"] == 12


def test_creates_missing_review_items():
    """抽到的候选若没有 review_item，必须补建——否则前端取不到题。"""
    _seed("EXP-RB11", 30, 3)
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB11", 15, "t11", seed=7, dry_run=False)
        picked_ids = {c.id for c in info["picked"]}
        rows = {r.subject_id for r in
                s.query(ReviewItem).filter_by(experiment_id="EXP-RB11").all()}
    assert picked_ids <= rows, "抽中的候选必须都有 review_item"
    assert info["created"] >= 1, "应有补建"


def test_tagging_is_idempotent():
    """重复跑同一批次不应产生重复标签。"""
    _seed("EXP-RB12", 20, 2)
    with db.session() as s:
        MRB.build_batch(s, "EXP-RB12", 10, "t12", seed=3)
        MRB.build_batch(s, "EXP-RB12", 10, "t12", seed=3)
        rows = s.query(ReviewItem).filter_by(experiment_id="EXP-RB12").all()
        for r in rows:
            assert (r.reasons or []).count("batch_t12") <= 1, f"重复标签：{r.id}"


def test_sampling_is_reproducible():
    """同种子必须抽到同一批——否则前后对照无法复现。"""
    _seed("EXP-RB13", 25, 4)
    with db.session() as s:
        a = {c.id for c in MRB.build_batch(s, "EXP-RB13", 10, "a", seed=99, dry_run=True)["picked"]}
        b = {c.id for c in MRB.build_batch(s, "EXP-RB13", 10, "b", seed=99, dry_run=True)["picked"]}
    assert a == b


def test_dry_run_writes_nothing():
    _seed("EXP-RB14", 20, 2)
    with db.session() as s:
        before = s.query(ReviewItem).filter_by(experiment_id="EXP-RB14").count()
        MRB.build_batch(s, "EXP-RB14", 10, "t14", seed=5, dry_run=True)
        after = s.query(ReviewItem).filter_by(experiment_id="EXP-RB14").count()
    assert before == after, "dry-run 不应写库"


# ── unjudged 池模式（2026-09-16 加，h30 之后）──────────────────
def _mark_done(s, exp_id, k):
    """把队列里前 k 条标为已判。"""
    rows = (s.query(ReviewItem).filter_by(experiment_id=exp_id)
            .order_by(ReviewItem.id).limit(k).all())
    for r in rows:
        r.status = "done"
        r.human_verdict = {"winner_resolved": "human"}
    s.commit()


def test_unjudged_pool_excludes_done_candidates():
    """要 n 条**全新判定**时，已判过的候选不能占位（抽到它不产生新信息）。"""
    _seed("EXP-RB30", 40, 12)
    with db.session() as s:
        _mark_done(s, "EXP-RB30", 12)
        info = MRB.build_batch(s, "EXP-RB30", 10, "u30", seed=1,
                               dry_run=True, pool="unjudged")
    assert info["pool"] == 28, f"池应为 40-12=28，实得 {info['pool']}"
    assert info["p_inc"] == pytest.approx(10 / 28)


def test_unjudged_pool_keeps_pending_queued_items():
    """队列里挂着但**没判过**的题仍可抽——它们同样产生新判定。"""
    _seed("EXP-RB31", 20, 20)          # 20 条全在队列里，且全 pending
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB31", 5, "u31", seed=1,
                               dry_run=True, pool="unjudged")
    assert info["pool"] == 20


def test_unjudged_pool_still_excludes_ablation():
    """池模式不得绕过"只收平行文本"这条不变量。"""
    _seed("EXP-RB32", 30, 5, prompt_version="recon_ctxonly_v1")
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB32", 10, "u32", seed=1,
                               dry_run=True, pool="unjudged")
    assert info["pool"] == 0 and info["picked"] == []


def test_random_pool_writes_stratum_and_w():
    """等概率抽样也要留档入样概率与分层名（下游 IPW 工具靠它识别框）。"""
    _seed("EXP-RB33", 30, 4)
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB33", 10, "u33", seed=5, pool="unjudged")
        ids = {c.id for c in info["picked"]}
        rows = [r for r in s.query(ReviewItem).filter_by(experiment_id="EXP-RB33").all()
                if r.subject_id in ids]
    assert len(rows) == 10
    for r in rows:
        assert "stratum:RANDOM" in r.reasons
        assert any(x.startswith("w:") for x in r.reasons)
        assert r.reasons.count("batch_u33") == 1


def test_stratum_tags_are_idempotent():
    """同一种子重跑不得堆叠 stratum/w 标签。"""
    _seed("EXP-RB34", 20, 2)
    with db.session() as s:
        MRB.build_batch(s, "EXP-RB34", 10, "u34", seed=3, pool="unjudged")
        MRB.build_batch(s, "EXP-RB34", 10, "u34", seed=3, pool="unjudged")
        rows = s.query(ReviewItem).filter_by(experiment_id="EXP-RB34").all()
    for r in rows:
        assert sum(1 for x in r.reasons if x.startswith("stratum:")) <= 1
        assert sum(1 for x in r.reasons if x.startswith("w:")) <= 1


def test_default_pool_mode_unchanged():
    """默认仍是 all（抽到已判的沿用旧判定）——不能悄悄改掉老行为。"""
    _seed("EXP-RB35", 30, 10)
    with db.session() as s:
        _mark_done(s, "EXP-RB35", 10)
        info = MRB.build_batch(s, "EXP-RB35", 10, "d35", seed=1, dry_run=True)
    assert info["pool"] == 30 and info["pool_mode"] == "all"


# ── 水印伪影（2026-09-17）──────────────────────────────────────
def test_looks_watermarked_true_cases():
    """两个真阳性（来自实测语料）。"""
    assert MRB.looks_watermarked("韩立脸上笑容一敛，无数金sè拳影浮现")
    assert MRB.looks_watermarked("来自阳关著名学府én下的才子钟大俊")
    assert MRB.looks_watermarked("小-说-t-xt-天.堂")


def test_looks_watermarked_clean_cases():
    assert not MRB.looks_watermarked("他推门进来，屋里没人。")
    assert not MRB.looks_watermarked("")
    assert not MRB.looks_watermarked(None)


def test_dirty_segments_excluded_by_default():
    """带水印伪影的段落默认不进批 —— 它只污染人类侧，会造成不公平比较。"""
    wid = _seed_multi_seg("EXP-RB50", 6, 2)
    with db.session() as s:
        segs = s.query(Segment).filter(Segment.work_id == wid).all()
        for seg in segs[:2]:
            seg.text = seg.text + "敬畏之sè"
        s.commit()
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB50", 6, "d50", seed=1,
                               dry_run=True, by="segment")
    assert info["pool"] == 8, f"2 段 x 2 候选 = 4 条应被剔除，实得池 {info['pool']}"


def test_keep_dirty_text_opt_out():
    """显式 exclude_dirty=False 时才保留（给"专门研究伪影影响"留出口子）。"""
    wid = _seed_multi_seg("EXP-RB51", 6, 2)
    with db.session() as s:
        segs = s.query(Segment).filter(Segment.work_id == wid).all()
        for seg in segs[:2]:
            seg.text = seg.text + "敬畏之sè"
        s.commit()
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB51", 6, "d51", seed=1,
                               dry_run=True, by="segment", exclude_dirty=False)
    assert info["pool"] == 12


# ── 按全书位置分层（2026-09-17 集霸问"怎么都是靠后的内容"）──────
def test_stratify_position_covers_every_decile():
    """❗核心：分层抽必须覆盖全书的每个十分位。

    起因：前几批几乎没碰书的最后 10%（末位十分位仅 1%），剩下的可抽池在末尾富集，
    集霸抽到的新样本跟着偏后。纯随机抽每次都在赌——实测 n=20 的随机抽会留下
    整整 3 个十分位一条都没有。分层把这些变成构造性保证。
    """
    _seed_multi_seg("EXP-RB60", 30, 2)      # ordinal 0..29，共 30 段 → 每层 3 段
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB60", 10, "s60", seed=1, dry_run=True,
                               by="segment", stratify_position=True)
        ords = [s.get(Segment, c.segment_id).ordinal for c in info["picked"]]
    assert len(ords) == 10
    deciles = sorted({min(9, o * 10 // 30) for o in ords})
    assert deciles == list(range(10)), f"应覆盖全部 10 层，实得 {deciles}"


def test_stratify_position_takes_what_exists_when_pool_is_thin():
    """某些层段落不够时，多出来的名额由其它层补足，不报错也不虚构。"""
    _seed_multi_seg("EXP-RB61", 12, 2)      # 12 段，只有 0..9 层中的前 4 层非空
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB61", 8, "s61", seed=1, dry_run=True,
                               by="segment", stratify_position=True)
    assert len(info["picked"]) == 8


def test_stratify_off_by_default():
    """默认仍是纯随机（老行为不变）。"""
    _seed_multi_seg("EXP-RB62", 20, 2)
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB62", 5, "s62", seed=1, dry_run=True, by="segment")
    assert len(info["picked"]) == 5


# ── 番外区（2026-09-17 集霸定的策略：只排番外，其余照抽）──────
def test_extras_start_detects_boundary():
    """「番外」开头的标题段即番外区起点；没有则为 None。"""
    wid = _seed_multi_seg("EXP-RB70", 10, 2)
    with db.session() as s:
        seg = (s.query(Segment).filter(Segment.work_id == wid)
               .order_by(Segment.ordinal).all())[6]        # ordinal 6
        assert MRB.extras_start(s, seg.work_id, seg.seg_version) is None     # 改之前没有番外
        seg.text = "番外 多年之后1\n" + seg.text
        s.commit()
        assert MRB.extras_start(s, seg.work_id, seg.seg_version) == 6        # 边界 = 该段 ordinal


def test_extras_excluded_by_default():
    """番外区（含边界那一段）默认不进批。"""
    wid = _seed_multi_seg("EXP-RB71", 10, 2)
    with db.session() as s:
        seg = (s.query(Segment).filter(Segment.work_id == wid)
               .order_by(Segment.ordinal).all())[6]
        seg.text = "番外 多年之后1\n" + seg.text
        s.commit()
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB71", 20, "e71", seed=1, dry_run=True, by="segment")
    # ordinal 6..9 共 4 段（x2 候选 = 8 条）应被剔除 → 池 12
    assert info["pool"] == 12, f"番外区应被剔除，实得池 {info['pool']}"


def test_keep_extras_opt_out():
    wid = _seed_multi_seg("EXP-RB72", 10, 2)
    with db.session() as s:
        seg = (s.query(Segment).filter(Segment.work_id == wid)
               .order_by(Segment.ordinal).all())[6]
        seg.text = "番外 多年之后1\n" + seg.text
        s.commit()
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB72", 20, "e72", seed=1, dry_run=True,
                               by="segment", exclude_extras=False)
    assert info["pool"] == 20


# ── 按段落抽样（2026-09-16 加，r50 的教训）──────────────────────
def _seed_multi_seg(exp_id, n_seg, per_seg, prompt_version="reconstruct_v1"):
    """n_seg 个段落，每段 per_seg 个候选。"""
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
        w = Work(title="t-" + exp_id, source="test:seed")
        s.add(w)
        s.flush()
        for k in range(n_seg):
            seg = Segment(work_id=w.id, ordinal=k, text=f"第{k}段人类原文，长度足够通过过滤。",
                          n_sentences=1, n_chars=16)
            s.add(seg)
            s.flush()
            fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                       extractor_model="m", prompt_version="pv")
            s.add(fr)
            s.flush()
            for i in range(per_seg):
                s.add(Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                                anon_label=f"Y{k:03d}{i:02d}", model="m",
                                prompt_version=prompt_version,
                                text=f"第{k}段的第{i}个候选文本，长度足够长以便通过最小长度过滤。"))
        s.commit()
        return w.id


def test_by_segment_never_repeats_a_segment():
    """❗核心回归：按段落抽样时，n 题必须对应 n 个不同人类段落。

    r50 的实况：按候选抽 → 50 题只覆盖 25 段，同一段人类原文被端出 1–4 次，
    集霸当场发问"怎么段落没变"。这条断言让那种样本无法再产生。
    """
    _seed_multi_seg("EXP-RB40", 12, 4)
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB40", 8, "s40", seed=1, dry_run=True, by="segment")
    segs = [c.segment_id for c in info["picked"]]
    assert len(info["picked"]) == 8
    assert len(set(segs)) == 8, f"段落必须互不相同，实得 {len(set(segs))} 个"
    assert info["n_segments"] == 8


def test_by_candidate_still_can_repeat_segment():
    """对照组：默认模式仍按候选抽（同段多候选可被抽中多次）——行为不变。"""
    _seed_multi_seg("EXP-RB41", 3, 10)      # 3 段 × 10 候选
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB41", 10, "c41", seed=1, dry_run=True)
    assert info["n_segments"] <= 3, "按候选抽时 10 题挤在 3 段上是预期行为"


def test_by_segment_caps_at_available_segments():
    """池里段落不够时只能抽到全部段落数，不得虚构。"""
    _seed_multi_seg("EXP-RB42", 5, 3)
    with db.session() as s:
        info = MRB.build_batch(s, "EXP-RB42", 20, "s42", seed=1, dry_run=True, by="segment")
    assert len(info["picked"]) == 5
    assert info["p_inc"] == pytest.approx(1.0)


def test_by_segment_is_reproducible_and_one_per_segment():
    """同种子可复现，且每段恰好 1 个候选。"""
    _seed_multi_seg("EXP-RB43", 10, 4)
    with db.session() as s:
        a = MRB.build_batch(s, "EXP-RB43", 6, "a", seed=99, dry_run=True, by="segment")
        b = MRB.build_batch(s, "EXP-RB43", 6, "b", seed=99, dry_run=True, by="segment")
    assert [c.id for c in a["picked"]] == [c.id for c in b["picked"]]
    import collections
    assert collections.Counter(c.segment_id for c in a["picked"]).most_common(1)[0][1] == 1


def test_watermark_filter_catches_site_watermark_injected_midsentence():
    """站点水印会**插在句子中间**：`青年电~脑}访整理神色一狞`。

    2026-09-18 实测：一个这样的段污染了 26 条受控劣化变体（生成侧从人类原文改写，
    乱码被原样带过去，A/B 两侧都脏）。旧规则只查拉丁字母/带调拼音，漏掉这一类。
    """
    assert looks_watermarked("青年电~脑}访整理神色一狞的言道。")
    assert looks_watermarked("{乱入}的括号内容")
    # 合法中文标点不许误伤（成对引号、括号、破折号、省略号都常见）
    assert not looks_watermarked("他说：“你不必再来了。”")
    assert not looks_watermarked("（这一段是插叙）——就这样结束了……")
    assert looks_watermarked("朱红sè的大门")       # 旧规则（带调拼音）仍生效


# ── §14 隔离硬闸（2026-09-19）────────────────────────────────
def test_benchmark_role_candidates_never_enter_pool():
    """基准段上的候选端给集霸判一次，隐藏基准就不再 hidden——
    role='benchmark' 的候选必须在建批前被硬闸拦下（此前只靠自觉）。"""
    exp_id = "EXP-RB-BENCHGUARD"
    _seed(exp_id, 2, 0)                    # 2 条普通候选（role=None）
    with db.session() as s:
        seg = s.query(Segment).filter_by(work_id=s.query(Work).filter_by(
            title="t-" + exp_id).one().id).one()
        seg.role = "benchmark"             # 把同实验里的段标成基准段
        s.commit()
    with db.session() as s:
        out = MRB.build_batch(s, exp_id, 10, "benchguard", seed=1, dry_run=True)
    assert out["picked"] == [], "基准段候选混进了盲评池——§14 隔离被破坏"
