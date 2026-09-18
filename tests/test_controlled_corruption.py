"""受控劣化（主工作流 B，§4.6/§7）回归测试。

锁定的不变量：

1. **主池隔离**：`corrupt_v1` 只能"按批次端出"，绝不能进随机抽样池。
   劣化版是"人类原文的劣化版"，胜率天然≈0；一旦混进 `BLIND_REVIEW_PROMPT_VERSIONS`，
   所有随机批的候选胜率/κ 都会被整体拽偏，而且是**静默**的偏。
2. **单变量承诺**：18 类劣化各改**一个**不同的变量（表本身不许退化成同义重复）。
3. **校验门**：事实不一致 / 漂移超阈 / 长度比越界 → 一律拒收，
   且拒收的**不写 candidate**（否则会以"平行文本"身份混进盲评）。
4. **匿名标签不泄漏类型**：anon_label 是给评审台看的，写成 CEXP（EXPLICITIZE）
   等于把谜底印在题面上。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import controlled_corruption as CC  # noqa: E402
import export_training as EX  # noqa: E402
from app import db  # noqa: E402
from app.config import BLIND_REVIEW_PROMPT_VERSIONS, SERVABLE_PROMPT_VERSIONS  # noqa: E402
from app.models import (Candidate, ControlledCorruption, Experiment, Frame,  # noqa: E402
                        Segment, Work)

# 方案 §4.6 原文列出的类型（一个都不能少；多出来的算新增，不算缺失）
SPEC_TYPES = ("EXPLICITIZE", "OVER_EXPLAIN", "EMOTION_LABEL", "PSYCHOLOGY_LABEL",
              "LITERARY_OVERWRITE", "ADJECTIVE_INFLATION", "ADVERB_INFLATION",
              "LOGIC_CONNECTOR_INFLATION", "REDUNDANCY", "PARALLELISM_OVERUSE",
              "ABSTRACT_SUMMARY", "MICRO_EXPRESSION_TEMPLATE", "DIALOGUE_EXPOSITION",
              "POV_DRIFT", "SEMANTIC_OVERCOMPLETION", "RHYTHM_FLATTEN", "SUBTEXT_ERASE")


# ── 1. 主池隔离 ────────────────────────────────────────────────

def test_corrupt_pv_never_enters_sampling_pool():
    """corrupt_v1 不在抽样池白名单里，但可以端出（SERVABLE）。"""
    assert "corrupt_v1" not in BLIND_REVIEW_PROMPT_VERSIONS, \
        "劣化候选混进抽样池会让所有随机批的胜率整体偏斜"
    assert "corrupt_v1" in SERVABLE_PROMPT_VERSIONS
    for pv in BLIND_REVIEW_PROMPT_VERSIONS:
        assert pv in SERVABLE_PROMPT_VERSIONS


def test_random_batch_never_picks_corruption():
    """造一批候选（含一条劣化），随机批的池子里不许出现劣化候选。"""
    import make_random_batch as MRB
    db.init_db()
    exp = "EXP-CC-POOL"
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w = Work(title="t-cc-pool", source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他推门进来，屋里没人，桌上的茶还温着。",
                      n_sentences=1, n_chars=20)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        for i in range(5):
            s.add(Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg.id,
                            anon_label=f"X{i:04d}", model="m",
                            prompt_version="reconstruct_v1",
                            text=f"他推开门走进去，屋里空无一人，只有桌上的茶还温着{i}。"))
        bad = Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg.id,
                        anon_label="XBAD", model="corrupt:EXPLICITIZE",
                        prompt_version="corrupt_v1",
                        text="他推门进来，显然是想看看屋里有没有人，屋里没人，桌上的茶还温着。")
        s.add(bad)
        s.commit()
        bad_id = bad.id
    with db.session() as s:
        info = MRB.build_batch(s, exp, 10, "ccpool", seed=3, dry_run=True)
        picked = info["picked"]
    assert bad_id not in {c.id for c in picked}, "劣化候选混进了随机批的池子"
    assert all(c.prompt_version in BLIND_REVIEW_PROMPT_VERSIONS for c in picked)


# ── 2. 类型表 ──────────────────────────────────────────────────

def test_spec_types_all_present():
    missing = [t for t in SPEC_TYPES if t not in CC.TYPES]
    assert not missing, f"方案 §4.6 要求的劣化类型缺失：{missing}"
    assert len(CC.TYPES) >= 15, "任务清单第 6 项验收：至少 15 类"


def test_each_type_targets_a_distinct_variable():
    """每类必须写清"唯一改变的变量"，且变量之间不重复（否则不是控制实验）。"""
    for code, t in CC.TYPES.items():
        assert t.get("zh") and t.get("variable") and t.get("directive"), f"{code} 定义不全"
    vars_ = [t["variable"] for t in CC.TYPES.values()]
    assert len(set(vars_)) == len(vars_), "有两个类型改的是同一个变量 —— 那就不是单变量对照"


# ── 3. 校验门 ──────────────────────────────────────────────────

def test_verify_gate_rejects_contradiction_even_with_low_drift():
    """v2 口径：只有「与原文明说的事实矛盾」是硬拒条件。"""
    ok, _, why = CC.judge_verify(
        {"contradicts_source": True, "drift": 0.05, "added_events": ["结果反转"]},
        ratio=1.0)
    assert not ok and why == "contradicts_source"


def test_verify_gate_honors_v1_output_for_backcompat():
    """旧数据是 v1 口径（fact_consistent），判定函数必须仍认得，不能静默放行。"""
    ok, _, why = CC.judge_verify({"fact_consistent": False, "drift": 0.05}, ratio=1.0)
    assert not ok and why == "contradicts_source"


def test_verify_gate_accepts_interpretive_addition():
    """§7 的核心：**增补解说就是那个变量**，不该被当成事实篡改拒收。

    v1 曾把它判成 fact_consistent=false，把 EMOTION_LABEL / PSYCHOLOGY_LABEL /
    EXPLICITIZE 的大批本该放行的样本拒了——门太严会把数据集清空。
    """
    ok, drift, why = CC.judge_verify(
        {"contradicts_source": False, "added_events": [],
         "added_non_event": ["他其实是在试探"], "lost": [], "drift": 0.2}, ratio=1.2)
    assert ok and why == "" and drift == 0.2


def test_verify_gate_rejects_high_drift_and_bad_length():
    ok, _, why = CC.judge_verify({"contradicts_source": False, "drift": 0.9}, ratio=1.0)
    assert not ok and why.startswith("drift=")
    ok, _, why = CC.judge_verify({"contradicts_source": False, "drift": 0.0}, ratio=3.0)
    assert not ok and why.startswith("len_ratio=")
    ok, _, why = CC.judge_verify({"contradicts_source": False, "drift": 0.0}, ratio=0.2)
    assert not ok and why.startswith("len_ratio=")


def test_verify_gate_passes_single_variable_change():
    ok, drift, why = CC.judge_verify(
        {"contradicts_source": False, "drift": 0.15,
         "added_non_event": ["意图"], "lost": []}, ratio=1.1)
    assert ok and why == "" and drift == 0.15


def test_verify_gate_rejects_unparsable_verdict():
    """校验器输出解析失败 → 拒收（宁可少一条数据，不能让未经校验的文本进数据集）。"""
    ok, drift, why = CC.judge_verify(None, ratio=1.0)
    assert not ok and why == "verify_parse_failed" and drift == 1.0


def test_parse_json_tolerates_fences_and_chatter():
    assert CC.parse_json('前言\n```json\n{"text": "a"}\n```\n后记') == {"text": "a"}
    assert CC.parse_json('{"text": "a"}') == {"text": "a"}
    assert CC.parse_json("这不是 JSON") is None
    assert CC.parse_json("") is None
    assert CC.parse_json('{"broken": ') is None


# ── 4. 落库 ────────────────────────────────────────────────────

def _seed_one(exp="EXP-CC-SAVE"):
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created",
                             config={"kind": "controlled_corruption"}, stats={}))
        w = Work(title="t-cc-save", source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他把茶喝完，才起身。",
                      n_sentences=1, n_chars=9)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.commit()
        return seg.id, fr.id


def test_save_ok_writes_record_and_candidate_with_neutral_label():
    seg_id, frame_id = _seed_one()
    rec = {"exp": "EXP-CC-SAVE", "segment_id": seg_id, "frame_id": frame_id,
           "corruption_type": "EXPLICITIZE", "gen_model": "g", "verify_model": "v",
           "text": "他虽然想离开，却不愿表现得慌张，于是把茶喝完才起身。",
           "gen_note": "把离意写成明话", "len_ratio": 1.6, "drift": {"drift": 0.1},
           "drift_score": 0.1, "fact_consistent": True, "drift_ok": True, "status": "ok"}
    CC._save(rec, human_len=9)
    with db.session() as s:
        cc = s.query(ControlledCorruption).filter_by(segment_id=seg_id).one()
        assert cc.status == "ok" and cc.candidate_id
        cand = s.get(Candidate, cc.candidate_id)
        assert cand.prompt_version == CC.GEN_PV, "候选口径必须与生成口径一致（可审计）"
        assert cand.model == "corrupt:EXPLICITIZE"       # 类型只存 DB 侧
        assert cand.anon_label != "CEXP"
        assert "EXP" not in cand.anon_label and ":" not in cand.anon_label
        assert not cand.anon_label.startswith("C"), \
            "anon_label 不许按类型首字母命名（会泄漏谜底）"


def test_save_rejected_writes_no_candidate():
    seg_id, frame_id = _seed_one("EXP-CC-SAVE2")
    rec = {"exp": "EXP-CC-SAVE2", "segment_id": seg_id, "frame_id": frame_id,
           "corruption_type": "OVER_EXPLAIN", "gen_model": "g", "verify_model": "v",
           "text": "改写失败的文本", "gen_note": "", "len_ratio": 1.0,
           "drift": {"fact_consistent": False}, "drift_score": 0.9,
           "fact_consistent": False, "drift_ok": False, "status": "rejected_drift",
           "reject_reason": "fact_inconsistent"}
    CC._save(rec, human_len=9)
    with db.session() as s:
        cc = s.query(ControlledCorruption).filter_by(segment_id=seg_id).one()
        assert cc.status == "rejected_drift" and cc.candidate_id is None
        assert s.query(Candidate).filter_by(segment_id=seg_id).count() == 0


# ── 5. 选段 ────────────────────────────────────────────────────

def test_pick_segments_excludes_short_and_fixture():
    exp = "EXP-CC-PICK"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        for title, texts in (("fixture_wuguan", ["短" * 80]),
                             ("真书", ["短" * 20, "够长的段落" * 12])):
            w = Work(title=title, source="test:seed")
            s.add(w)
            s.flush()
            for i, t in enumerate(texts):
                # 源完整性：2026-09-18 起，**没查过 src_ok 的段一律不可用**
                # （集霸看到「逃走的五名强。」当场发火 → 宁可少用，不许端脏文本）
                seg = Segment(work_id=w.id, ordinal=i, text=t, n_sentences=1, n_chars=len(t),
                              integrity='{"src_ok": true}')
                s.add(seg)
                s.flush()
                s.add(Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                            extractor_model="m", prompt_version="pv"))
        s.commit()
    with db.session() as s:
        picked = CC.pick_segments(s, 10, None, seed=1, min_chars=60)
        titles = {s.get(Work, seg.work_id).title for seg, _ in picked}
    assert "fixture_wuguan" not in titles, "测试夹具不该进数据集"
    assert titles == {"真书"}
    assert len(picked) == 1, "20 字的段不该被选中"


# ── 6. 基准隔离（§14）─────────────────────────────────────────

def test_split_benchmark_excludes_from_training_export(tmp_path, monkeypatch):
    """基准段必须被训练导出跳过，且基准段优先挑"没被别的实验用过"的段。

    §14 的硬要求：Hidden Benchmark 不得被训练读取。corruption 检测是基准必含项，
    所以它的对照对必须能划出一部分**只做基准**。
    """
    exp = "EXP-CC-BENCH"
    db.init_db()
    seg_ids = []
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w = Work(title="t-cc-bench", source="test:seed")
        s.add(w)
        s.flush()
        fr = None
        for i in range(4):
            seg = Segment(work_id=w.id, ordinal=i, text=f"第{i}段正文，够长以通过最小字数门槛。",
                          n_sentences=1, n_chars=20)
            s.add(seg)
            s.flush()
            seg_ids.append(seg.id)
            f = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                      extractor_model="m", prompt_version="pv")
            s.add(f)
            s.flush()
            fr = f
            cand = Candidate(experiment_id=exp, frame_id=f.id, segment_id=seg.id,
                             anon_label=f"X{i}", model=f"corrupt:EXPLICITIZE",
                             prompt_version=CC.GEN_PV,
                             text=f"第{i}段正文，显然够长以通过最小字数门槛。")
            s.add(cand)
            s.flush()
            s.add(ControlledCorruption(
                experiment_id=exp, segment_id=seg.id, frame_id=f.id, candidate_id=cand.id,
                corruption_type="EXPLICITIZE", variable="显式化", generator_model="g",
                prompt_version=CC.GEN_PV, text=cand.text, n_chars=len(cand.text),
                drift={}, drift_score=0.1, fact_consistent=True, drift_ok=True,
                verify_model="v", verify_pv=CC.VERIFY_PV, status="ok"))
        # 第 0 段额外挂一个"自然重建"候选 → 它是"已被别的实验用过"的段
        s.add(Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg_ids[0],
                        anon_label="XN", model="m", prompt_version="reconstruct_v1",
                        text="第0段正文，写得也不错的一段话。"))
        s.commit()

    out = CC.split_benchmark(n=2, dry_run=False, exp=exp)
    assert out["pool_fresh"] == 3, f"应只把未被别的实验用过的 3 段当候选池，实得 {out}"
    assert out["marked"] == 2
    with db.session() as s:
        marked = {x.id for x in s.query(Segment).filter(Segment.role == "benchmark").all()}
    assert seg_ids[0] not in marked, "已被其他实验用过的段不该进基准（会泄漏）"
    info = EX.export_corrupt_pairs("testbench", out_dir=tmp_path)
    # 相对断言：同一临时库里别的测试文件也造基准段（test_benchmark.py 会建 4 条），
    # 绝对计数会被牵动 —— 这里改成"和库里实际该被剔除的条数一致"。
    with db.session() as s:
        role_of = {x.id: (x.role or "") for x in s.query(Segment).all()}
        expect_bench = sum(1 for c in s.query(ControlledCorruption).all()
                           if c.status == "ok" and c.candidate_id
                           and role_of.get(c.segment_id) == "benchmark")
    assert info["n_benchmark_held_out"] == expect_bench,         f"基准段应从训练导出里被剔除（应 {expect_bench}）"
    assert expect_bench >= 2
    # 用**相对**断言：同一测试文件里别的用例也写了劣化行，绝对条数会互相牵动
    with db.session() as s:
        role = {x.id: x.role for x in s.query(Segment).all()}
        want = sum(1 for c in s.query(ControlledCorruption).all()
                   if c.status == "ok" and c.candidate_id and role.get(c.segment_id) != "benchmark")
    assert info["n"] == want, f"导出应剔除基准段后剩 {want} 对，实得 {info['n']}"


def test_control_arm_never_enters_dpo_export(tmp_path):
    """控制臂（中性改写）**不能**当 rejected 导出——那等于教模型「换个说法就是错的」。

    控制臂的用途是量评委偏差（实测：中性改写上评委仍偏向 AI 那一版 77%），
    它是**测量工具**，不是训练信号。
    """
    exp = "EXP-CC-CTRL"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w = Work(title="t-cc-ctrl", source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="他把茶喝完，才起身，屋里静得能听见风声。",
                      n_sentences=1, n_chars=18)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        for ct in ("EXPLICITIZE", "NEUTRAL_PARAPHRASE"):
            cand = Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg.id,
                             anon_label="X" + ct[:2], model=f"corrupt:{ct}",
                             prompt_version=CC.GEN_PV, text=f"他把茶喝完才起身（{ct}）。")
            s.add(cand)
            s.flush()
            s.add(ControlledCorruption(
                experiment_id=exp, segment_id=seg.id, frame_id=fr.id, candidate_id=cand.id,
                corruption_type=ct, variable="x", generator_model="g",
                prompt_version=CC.GEN_PV, text=cand.text, n_chars=len(cand.text),
                drift={}, drift_score=0.0, fact_consistent=True, drift_ok=True,
                verify_model="v", verify_pv=CC.VERIFY_PV, status="ok"))
        s.commit()
    info = EX.export_corrupt_pairs("ctrl_test", out_dir=tmp_path)
    assert info["n_control_excluded"] >= 1, "控制臂必须被剔除并计数"
    assert "NEUTRAL_PARAPHRASE" not in info["by_type"], "控制臂不该出现在导出的类型分布里"
    body = (tmp_path / "corrupt_dpo_ctrl_test.jsonl").read_text(encoding="utf-8")
    assert "NEUTRAL_PARAPHRASE" not in body


def test_pick_segments_requires_source_integrity():
    """源文本没查过 / 判坏的段，一律不进劣化数据集。

    起因：集霸 2026-09-18 看到「还有和千仞雪一起逃走的五名强。」（掉了「者」）
    与「千雪」（掉了「仞」）——原始 txt 掉字，他原话「字都不舍得搞完了？」。
    """
    exp = "EXP-CC-SRC"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        for title, integ in (("好源", '{"src_ok": true}'),
                             ("坏源", '{"src_ok": false}'),
                             ("没查过", None)):
            w = Work(title=title, source="test:seed")
            s.add(w)
            s.flush()
            seg = Segment(work_id=w.id, ordinal=0, text="够长的段落" * 12, n_sentences=1,
                          n_chars=60, integrity=integ)
            s.add(seg)
            s.flush()
            s.add(Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                        extractor_model="m", prompt_version="pv"))
        s.commit()
    with db.session() as s:
        picked = CC.pick_segments(s, 10, None, seed=1, min_chars=40)
        got = {s.get(Work, seg.work_id).title for seg, _ in picked}
    # 用**相对**断言：同文件的其它用例也会造段（共享临时库），绝对集合会被牵动
    assert "好源" in got, f"src_ok=true 的段必须可用，实得 {got}"
    assert "坏源" not in got, "src_ok=false 的段不许进数据集"
    assert "没查过" not in got, "没查过完整性的段不许进数据集（宁可少用）"


def test_current_gen_pv_is_servable():
    """**生成口径必须随时可端出**——这条能一招防住 2026-09-18 的实际事故。

    事故回放：类型定义修正时把 `corrupt_v1` 升成 `corrupt_v2`，但端出白名单
    是写死的两个字符串，没同步 → 批次里 12 道**待判**的题对取题接口"不存在"，
    集霸判到一半看到"已评完"。这类 bug 静默、且表现为"系统说没事"。

    断言的是**不变量**而不是某个具体版本号：劣化数据集当前用的 prompt_version，
    永远必须是可端出的。
    """
    from app.config import is_servable_pv
    assert is_servable_pv(CC.GEN_PV), f"当前生成口径 {CC.GEN_PV} 端不出来"
    assert is_servable_pv("corrupt_v1") and is_servable_pv("corrupt_v9")
    # 但抽样池不许被污染（corrupt_* 永远不进随机批）
    from app.config import BLIND_REVIEW_PROMPT_VERSIONS
    assert not any(p.startswith("corrupt_") for p in BLIND_REVIEW_PROMPT_VERSIONS)
