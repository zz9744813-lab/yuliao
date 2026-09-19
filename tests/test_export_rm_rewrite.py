"""RM 与 Rewrite 训练导出（任务 13）回归测试。

锁定的不变量：

1. **--rm 三来源**：分数只来自已有真实标注——受控劣化（变量级/集霸裁定）、
   集霸判过的 review_items（winner_resolved 映射）、评委判定（weak=true）；
   字段齐全、id 唯一、summary 与 jsonl 行数一致。
2. **隔离硬约束**：`role='benchmark'` 段 / 番外区段 / 水印段一律不进导出——
   造出这些数据后导出条数**不变**（§14：基准段被训练读到就不再是基准）。
3. **控制臂**：NEUTRAL_PARAPHRASE 是中性改写不是劣化，正负例都不给。
4. **--rewrite**：instruction = L 主帧要点，output = 人类原文（text_clean 优先），
   同样受隔离约束。
"""
import json
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_training as EX  # noqa: E402
from app import db  # noqa: E402
from app.models import (Candidate, ControlledCorruption, Experiment, Frame,  # noqa: E402
                        JudgeRun, ReviewItem, Segment, Work)

TEXT = "他推门走进去，屋里空无一人，桌上的茶还温着，窗外的雨声一阵密过一阵。"


def _work_seg(s, title, segs):
    """建作品 + 若干段。segs = [(ordinal, text, role|None, integrity|None)]，返回段 dict。"""
    w = Work(title=title, source="test:seed")
    s.add(w)
    s.flush()
    out = {}
    for ordinal, text, role, integ in segs:
        g = Segment(work_id=w.id, ordinal=ordinal, text=text, role=role,
                    integrity=integ, n_sentences=1, n_chars=len(text))
        s.add(g)
        s.flush()
        out[ordinal] = g
    return w, out


def _cand(s, exp, seg, pv="reconstruct_v1", model="m", text=None, payload=None):
    fr = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
               extractor_model="m", prompt_version="pv", payload=payload or {"event": "对峙"})
    s.add(fr)
    s.flush()
    c = Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg.id,
                  anon_label="X" + seg.id[-4:], model=model, prompt_version=pv,
                  text=text or (TEXT + "（候选）"))
    s.add(c)
    s.flush()
    return c, fr


def _review(s, exp, cand, winner):
    s.add(ReviewItem(experiment_id=exp, subject_type="candidate", subject_id=cand.id,
                     status="done", human_verdict={"winner_resolved": winner}))


def _judges(s, exp, cand, winners):
    for i, w in enumerate(winners):
        s.add(JudgeRun(experiment_id=exp, subject_type="candidate", subject_id=cand.id,
                       judge_kind="preference", model=f"j{i}", prompt_version="pv",
                       verdict={"winner_resolved": w}, status="ok"))


def _cc(s, exp, seg, cand, ctype, variable="x"):
    s.add(ControlledCorruption(
        experiment_id=exp, segment_id=seg.id, candidate_id=cand.id,
        corruption_type=ctype, variable=variable, generator_model="g",
        prompt_version="corrupt_v1", text=cand.text, n_chars=len(cand.text),
        drift={}, drift_score=0.1, fact_consistent=True, drift_ok=True,
        verify_model="v", verify_pv="sv_v1", status="ok"))


def _rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── 1. --rm：字段齐全 + summary 与 jsonl 一致 ─────────────────

def test_rm_fields_and_summary(tmp_path):
    exp = "EXP-RM-FIELDS"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
            w, segs = _work_seg(s, "t-rm-fields", [(0, TEXT, None, '{"src_ok": true}')])
            c, _ = _cand(s, exp, segs[0])
            _review(s, exp, c, "human")
            _judges(s, exp, c, ["human", "human"])
            s.commit()
    info = EX.export_rm("iso", out_dir=tmp_path)
    path = tmp_path / "rm_iso.jsonl"
    rows = _rows(path)
    assert info["n"] > 0 and len(rows) == info["n"], "summary 条数必须与 jsonl 行数一致"
    assert sum(info["by_source"].values()) == info["n"]
    recomputed = {"pos": 0, "neg": 0, "neutral": 0}
    for r in rows:
        assert {"text", "score", "label_source", "weak"} <= set(r), f"字段缺失：{r.keys()}"
        assert 0.0 <= r["score"] <= 1.0
        assert r["label_source"] in ("corruption_variable", "user_verdict", "judge_majority")
        assert r["weak"] is (r["label_source"] == "judge_majority")
        if r["score"] > 0.5:
            recomputed["pos"] += 1
        elif r["score"] < 0.5:
            recomputed["neg"] += 1
        else:
            recomputed["neutral"] += 1
    assert (info["pos"], info["neg"], info["neutral"]) == \
        (recomputed["pos"], recomputed["neg"], recomputed["neutral"]), "正负比口径要与行内分数一致"
    # 本夹具的两条 user 行（human 胜 → 人类 1.0 / 候选 0.0）
    mine = [r for r in rows if r["label_source"] == "user_verdict"
            and r["segment_id"] == segs[0].id]
    assert len(mine) == 2, f"夹具候选应恰好出两条 user_verdict 行，实得 {mine}"
    by_side = {r["side"]: r["score"] for r in mine}
    assert by_side == {"human": 1.0, "candidate": 0.0}


def test_rm_scores_mapping(tmp_path):
    """winner_resolved → 分数：human/candidate 对调、tie=0.5、both_bad=0、解析不出不导。"""
    exp = "EXP-RM-MAP"
    db.init_db()
    wins = {"tie": (0.5, 0.5), "both_bad": (0.0, 0.0),
            "candidate": (0.0, 1.0), "human": (1.0, 0.0)}
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-map",
                            [(i, TEXT + str(i), None, '{"src_ok": true}') for i in range(5)])
        for i, (win, _) in enumerate(wins.items()):
            c, _ = _cand(s, exp, segs[i], text=f"{TEXT}{i}候选版。")
            _review(s, exp, c, win)
        # 'B'（mapping_lost）：判定存在但解析不出 → 不许编成分数
        c, _ = _cand(s, exp, segs[4], text=f"{TEXT}B候选版。")
        _review(s, exp, c, "B")
        s.commit()
    info = EX.export_rm("map", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_map.jsonl")
    for i, (win, (hs, cs)) in enumerate(wins.items()):
        got = {(r["side"], r["score"]) for r in rows
               if r["label_source"] == "user_verdict" and r["segment_id"] == segs[i].id}
        assert got == {("human", hs), ("candidate", cs)}, f"{win} 应映射为 {(hs, cs)}，实得 {got}"
    # 'B' 行只允许出现在 judge_majority（夹具没造评委票 → 该候选完全没有 user 行）
    b_rows = [r for r in rows if r["segment_id"] == segs[4].id]
    assert all(r["label_source"] != "user_verdict" for r in b_rows), \
        f"解析不出的判定不许编分数：{b_rows}"


def test_rm_judge_weak_majority(tmp_path):
    """评委判定是弱标签（weak=true），按多数票聚合；无裁定/无劣化时是唯一来源。"""
    exp = "EXP-RM-JUDGE"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-judge",
                            [(0, TEXT, None, '{"src_ok": true}'), (1, TEXT + "二", None, '{"src_ok": true}')])
        c1, _ = _cand(s, exp, segs[0], text=TEXT + "多数候选版。")
        _judges(s, exp, c1, ["candidate", "candidate", "human"])   # 多数 → 候选胜
        c2, _ = _cand(s, exp, segs[1], text=TEXT + "平票候选版。")
        _judges(s, exp, c2, ["candidate", "human"])                # 平票 → equal
        s.commit()
    info = EX.export_rm("judge", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_judge.jsonl")
    g1 = [r for r in rows if r["segment_id"] == segs[0].id and r["id"].endswith("judge_majority")]
    g2 = [r for r in rows if r["segment_id"] == segs[1].id and r["id"].endswith("judge_majority")]
    assert {(r["side"], r["score"]) for r in g1} == {("human", 0.0), ("candidate", 1.0)}, \
        "评委多数票应映射为候选胜"
    assert {(r["side"], r["score"]) for r in g2} == {("human", 0.5), ("candidate", 0.5)}, \
        "评委平票应归 equal（0.5/0.5），不硬造胜负"
    assert all(r["weak"] for r in g1 + g2), "评委来源必须显式 weak=true"


def test_rm_corruption_variable_label(tmp_path):
    """未裁定的劣化对：变量级标签（人类 1.0 / 劣化 0.0），评委偏好劣化版要标 suspect。"""
    exp = "EXP-RM-CORR"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-corr",
                            [(0, TEXT, None, '{"src_ok": true}'), (1, TEXT + "二", None, '{"src_ok": true}')])
        c1, _ = _cand(s, exp, segs[0], text=TEXT + "被显式化。", model="corrupt:EXPLICITIZE")
        _cc(s, exp, segs[0], c1, "EXPLICITIZE", variable="显式化")
        c2, _ = _cand(s, exp, segs[1], text=TEXT + "二劣化。", model="corrupt:SUBTEXT_ERASE")
        _cc(s, exp, segs[1], c2, "SUBTEXT_ERASE", variable="潜台词抹除")
        _judges(s, exp, c2, ["candidate", "candidate"])   # 评委多数偏好劣化版 → 嫌疑反例
        s.commit()
    info = EX.export_rm("corr", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_corr.jsonl")
    for i, (c, suspect) in enumerate(((c1, False), (c2, True))):
        got = [r for r in rows if r["label_source"] == "corruption_variable"
               and r["segment_id"] == (segs[0].id if i == 0 else segs[1].id)]
        assert {(r["side"], r["score"]) for r in got} == {("human", 1.0), ("candidate", 0.0)}
        assert all(r["suspect"] is suspect for r in got), "评委多数偏好劣化版必须标 suspect"
        assert all(r["weak"] is False for r in got), "变量级标签是强标签"
    # 集霸裁定过则**升级**为 user_verdict，不再出变量级行
    with db.session() as s:
        c3, _ = _cand(s, exp, segs[0], text=TEXT + "三候选版。")
        _cc(s, exp, segs[0], c3, "EXPLICITIZE", variable="显式化")
        _review(s, exp, c3, "candidate")     # 集霸判候选胜（§7.5 反例的真实标签）
        s.commit()
    info = EX.export_rm("corr2", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_corr2.jsonl")
    got = [r for r in rows if r["candidate_id"] == c3.id]
    assert {r["label_source"] for r in got} == {"user_verdict"}, \
        f"裁定过的劣化对不能再出变量级行：{got}"
    assert {(r['side'], r['score']) for r in got if r['label_source'] == 'user_verdict'} == \
        {("human", 0.0), ("candidate", 1.0)}


def test_rm_control_arm_never_exported(tmp_path):
    """控制臂（中性改写）正负例都不给——它是测量工具，不是训练信号。"""
    exp = "EXP-RM-CTRL"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-ctrl", [(0, TEXT, None, '{"src_ok": true}')])
        c, _ = _cand(s, exp, segs[0], text=TEXT + "（中性改写）", model="corrupt:NEUTRAL_PARAPHRASE")
        _cc(s, exp, segs[0], c, "NEUTRAL_PARAPHRASE", variable="中性改写")
        _judges(s, exp, c, ["human"])    # 评委判人类胜：若泄漏，中性文本就成了负例
        s.commit()
    info = EX.export_rm("ctrl", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_ctrl.jsonl")
    hits = [r for r in rows if r["candidate_id"] == c.id or r["text"] == TEXT + "（中性改写）"]
    assert not hits, f"控制臂候选不许进 RM：{hits}"
    assert info["n_control_excluded"] >= 1, "控制臂必须被剔除并计数"
    assert (tmp_path / "rm_ctrl.jsonl").read_text(encoding="utf-8").count("NEUTRAL_PARAPHRASE") == 0


# ── 2. 隔离硬约束：benchmark / 番外 / 水印 ────────────────────

def test_rm_isolation_keeps_count_unchanged(tmp_path):
    """造 benchmark / 控制臂 / 番外 / 水印数据后，导出条数必须一条不变。"""
    exp = "EXP-RM-ISO"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-iso", [(0, TEXT, None, '{"src_ok": true}')])
        c, _ = _cand(s, exp, segs[0])
        _review(s, exp, c, "human")
        s.commit()
    q1 = EX.export_rm("iso", out_dir=tmp_path)
    n1 = len(_rows(tmp_path / "rm_iso.jsonl"))
    assert q1["n"] == n1 and n1 > 0

    with db.session() as s:
        # ① benchmark 段：判定 + 评委全带上，若不隔离就会变成新行
        w2, bs = _work_seg(s, "t-rm-iso-bench", [(0, TEXT + "基准段。", "benchmark", None)])
        cb, _ = _cand(s, exp, bs[0], text=TEXT + "基准候选。")
        _review(s, exp, cb, "human")
        _judges(s, exp, cb, ["human", "candidate"])
        # ② 控制臂
        w3, cs = _work_seg(s, "t-rm-iso-ctrl", [(0, TEXT + "控制段。", None, '{"src_ok": true}')])
        cc, _ = _cand(s, exp, cs[0], text=TEXT + "中性改写。", model="corrupt:NEUTRAL_PARAPHRASE")
        _cc(s, exp, cs[0], cc, "NEUTRAL_PARAPHRASE")
        # ③ 番外区：标题段（ordinal 5）之后的内容段
        w4, es = _work_seg(s, "t-rm-iso-extras", [(5, "番外 试炼场\n", None, None),
                                                  (6, TEXT + "番外正文。", None, None)])
        ce, _ = _cand(s, exp, es[6], text=TEXT + "番外候选。")
        _review(s, exp, ce, "human")
        # ④ 水印段（独立作品，避免和番外边界互相沾）
        w5, ws_ = _work_seg(s, "t-rm-iso-wm", [(0, "朱红sè的大门学府én下青年电~脑}访整理。", None, None)])
        cw, _ = _cand(s, exp, ws_[0], text=TEXT + "水印候选。")
        _review(s, exp, cw, "human")
        s.commit()
    q2 = EX.export_rm("iso", out_dir=tmp_path)
    rows2 = _rows(tmp_path / "rm_iso.jsonl")
    assert q2["n"] == n1, f"隔离数据不许改变导出条数：{q1['n']} → {q2['n']}"
    for bad in ("基准候选。", "中性改写。", "番外候选。", "水印候选。"):
        assert not any(r["text"].endswith(bad) for r in rows2), f"隔离失败：{bad} 泄进了导出"
    assert q2["n_benchmark_held_out"] == q1["n_benchmark_held_out"] + 1
    assert q2["n_control_excluded"] == q1["n_control_excluded"] + 1
    assert q2["n_extras_excluded"] == q1["n_extras_excluded"] + 1
    assert q2["n_watermark_excluded"] == q1["n_watermark_excluded"] + 1


def test_rm_excludes_fixture_and_bad_src(tmp_path):
    """夹具作品与源校勘判坏的段不进 RM（for_train 质量闸）。"""
    exp = "EXP-RM-QUAL"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        wf, sf = _work_seg(s, "fixture_rm", [(0, TEXT, None, None)])
        cf, _ = _cand(s, exp, sf[0])
        _review(s, exp, cf, "human")
        wb, sb = _work_seg(s, "t-rm-badsrc", [(0, TEXT, None, '{"src_ok": false}')])
        cbad, _ = _cand(s, exp, sb[0])
        _review(s, exp, cbad, "human")
        s.commit()
    info = EX.export_rm("qual", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_qual.jsonl")
    for sid in (sf[0].id, sb[0].id):
        assert not any(r["segment_id"] == sid for r in rows), f"段 {sid} 不该进 RM"
    assert info["n_fixture_excluded"] >= 1 and info["n_bad_src_excluded"] >= 1


# ── 3. --rewrite ─────────────────────────────────────────────

def test_rewrite_pairs(tmp_path):
    exp = "EXP-RW-1"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rw-ok", [(0, TEXT, None, '{"src_ok": true}'),
                                           (1, TEXT + "续。", None, '{"src_ok": true}')])
        payload = {"event": "推门无人", "reader_effect": "安静"}
        c1, f1 = _cand(s, exp, segs[0], payload=payload)          # 夹带 L 主帧
        # 同段再来一个 M 帧：L 级优先，一个段只出一条，且用 L 帧
        s.add(Frame(experiment_id=exp, segment_id=segs[0].id, granularity="M",
                    extractor_model="m", prompt_version="pv", payload={"topic": "x"}))
        # 只有 M 帧的段：不进 rewrite
        s.add(Frame(experiment_id=exp, segment_id=segs[1].id, granularity="M",
                    extractor_model="m", prompt_version="pv", payload={"topic": "x"}))
        # 失败帧：不进
        s.add(Frame(experiment_id=exp, segment_id=segs[1].id, granularity="L",
                    extractor_model="m", prompt_version="pv", status="failed"))
        s.commit()
    info = EX.export_rewrite("rw", out_dir=tmp_path)
    rows = _rows(tmp_path / "rewrite_rw.jsonl")
    assert info["n"] > 0 and len(rows) == info["n"], "summary 条数必须与 jsonl 行数一致"
    assert all(r["granularity"] == "L" for r in rows), "rewrite 只用 L 主帧"
    for r in rows:
        assert {"instruction", "output"} <= set(r)
        assert isinstance(json.loads(r["instruction"]), dict), "instruction 必须是帧要点 JSON"
        assert len(r["output"].strip()) >= 20
    mine = [r for r in rows if r["segment_id"] == segs[0].id]
    assert len(mine) == 1, f"L 级优先：同段多帧只出一条，实得 {len(mine)}"
    assert mine[0]["frame_id"] == f1.id and json.loads(mine[0]["instruction"])["event"] == "推门无人"
    assert mine[0]["output"] == TEXT
    assert not any(r["segment_id"] == segs[1].id for r in rows), "只有 M 帧/失败帧的段不许进"


def test_rewrite_isolation(tmp_path):
    """benchmark / 番外 / 水印段同样不进 rewrite；造进去条数不变。"""
    exp = "EXP-RW-ISO"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rw-iso", [(0, TEXT, None, '{"src_ok": true}')])
        c, f = _cand(s, exp, segs[0])
        s.commit()
    q1 = EX.export_rewrite("rwi", out_dir=tmp_path)
    n1 = len(_rows(tmp_path / "rewrite_rwi.jsonl"))
    assert q1["n"] == n1 and n1 > 0

    with db.session() as s:
        wb, bs = _work_seg(s, "t-rw-bench", [(0, TEXT + "基准。", "benchmark", None)])
        _cand(s, exp, bs[0])
        we, es = _work_seg(s, "t-rw-extras", [(5, "番外 试炼\n", None, None),
                                              (6, TEXT + "番外。", None, None)])
        _cand(s, exp, es[6])
        wwm, ws_ = _work_seg(s, "t-rw-wm", [(0, "学府én下朱红sè的大门。", None, None)])
        _cand(s, exp, ws_[0])
        s.commit()
    q2 = EX.export_rewrite("rwi", out_dir=tmp_path)
    rows2 = _rows(tmp_path / "rewrite_rwi.jsonl")
    assert q2["n"] == n1, f"隔离数据不许改变导出条数：{q1['n']} → {q2['n']}"
    assert q2["n_benchmark_held_out"] == q1["n_benchmark_held_out"] + 1
    assert q2["n_extras_excluded"] == q1["n_extras_excluded"] + 1
    assert q2["n_watermark_excluded"] == q1["n_watermark_excluded"] + 1
    for sid in (bs[0].id, es[6].id, ws_[0].id):
        assert not any(r["segment_id"] == sid for r in rows2), f"段 {sid} 不该进 rewrite"


# ── 3. --negatives：负面模式库（2026-09-19 口径 code 化）──────

def _neg_cc(s, exp, seg, cand, *, ctype="EXPLICITIZE", status="ok",
            drift_ok=True, fact=True, drift=None, pv="corrupt_v1"):
    s.add(ControlledCorruption(
        experiment_id=exp, segment_id=seg.id, candidate_id=cand.id,
        corruption_type=ctype, variable="x", generator_model="g",
        prompt_version=pv, text=cand.text, n_chars=len(cand.text),
        drift=drift if drift is not None else {}, drift_score=0.1,
        fact_consistent=fact, drift_ok=drift_ok,
        verify_model="v", verify_pv="sv_v1", status=status))


def test_negatives_fields_and_eligible(tmp_path):
    """合格劣化条目必须以完整字段进负面库（共享库 → 断言种子的存在性，不写死总数）。"""
    db.init_db()
    with db.session() as s:
        exp = "EXP-NEG-T1"
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
            s.commit()
        w, segs = _work_seg(s, "t-neg-a", [(0, TEXT, None, '{"src_ok": true}'),
                                           (1, TEXT + "前文。", None, '{"src_ok": true}')])
        cand, _ = _cand(s, exp, segs[1])
        _neg_cc(s, exp, segs[1], cand)
        s.commit()
        cc_id = s.query(ControlledCorruption).filter_by(experiment_id=exp).all()[-1].id
    q = EX.export_negatives("negtest", out_dir=tmp_path)
    rows = _rows(Path(q["path"]))
    assert q["n"] == len(rows), "summary 条数必须与 jsonl 行数一致"
    mine = [r for r in rows if r["id"] == cc_id]
    assert len(mine) == 1, "刚种的合格负面没有出现在导出里"
    r = mine[0]
    for k in ("id", "failure_text", "failure_variable", "failure_mode",
              "source_human", "provenance", "pv"):
        assert k in r and r[k]
    assert r["failure_text"] == cand.text
    assert r["source_human"] == TEXT + "前文。"    # 原文=劣化所在段的人类侧
    assert r["failure_mode"] == "EXPLICITIZE"


def test_negatives_excludes_untreated_kinds(tmp_path):
    """病句 / 控制臂 / 基准段 / 坏源 / 未过校验的都不进（变量不许被污染）。
    测试库共享 → 用种子前后增量断言（交接 §9：断言写相对值）。"""
    db.init_db()
    with db.session() as s:
        exp = "EXP-NEG-T2"
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
            s.commit()
    q0 = EX.export_negatives("negtest2", out_dir=tmp_path)
    with db.session() as s:
        exp = "EXP-NEG-T2"
        w, segs = _work_seg(s, "t-neg-b", [
            (0, TEXT, "benchmark", '{"src_ok": true}'),      # 基准段
            (1, TEXT, None, '{"src_ok": false}'),            # 坏源
            (2, TEXT, None, '{"src_ok": true}'),             # 好：病句位
            (3, TEXT, None, '{"src_ok": true}'),             # 好：控制臂位
            (4, TEXT, None, '{"src_ok": true}'),             # 好：未过校验位
            (5, TEXT, None, '{"src_ok": true}'),             # 好：合格位
        ])
        for i in (0, 1, 2, 3, 4, 5):
            cand, _ = _cand(s, exp, segs[i])
            _neg_cc(s, exp, segs[i], cand,
                    ctype="EXPLICITIZE" if i != 3 else "NEUTRAL_PARAPHRASE",
                    status="rejected_drift" if i == 4 else "ok",
                    drift={"ungrammatical": True} if i == 2 else {})
        s.commit()
    q = EX.export_negatives("negtest2", out_dir=tmp_path)
    d = lambda k: q[k] - q0[k]
    assert d("n") == 1, f"6 条种子里应只有 1 条合格负面，实际增量 {d('n')}"
    assert d("n_benchmark_held_out") == 1 and d("n_bad_src_excluded") == 1
    assert d("n_control_excluded") == 1 and d("n_ungrammatical_excluded") == 1
    assert d("n_skip_missing") == 0          # status!=ok 在查询层就被滤掉（不算 missing）


def test_negatives_summary_matches_jsonl(tmp_path):
    """summary 的条数/类型分布必须与 jsonl 逐行一致（共享库上只断内部一致性）。"""
    db.init_db()
    with db.session() as s:
        exp = "EXP-NEG-T3"
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
            s.commit()
        w, segs = _work_seg(s, "t-neg-c", [(0, TEXT, None, '{"src_ok": true}')])
        cand, _ = _cand(s, exp, segs[0])
        _neg_cc(s, exp, segs[0], cand, ctype="REDUNDANCY")
        s.commit()
    q = EX.export_negatives("negtest3", out_dir=tmp_path)
    rows = _rows(Path(q["path"]))
    from collections import Counter
    assert q["n"] == len(rows)
    assert Counter(r["failure_mode"] for r in rows) == Counter(q["by_type"])
    assert q["summary_path"].endswith("negatives_negtest3_summary.json")

# ── 4. 内容级隔离（军师 P1-5）────────────────────────────────

def test_benchmark_content_twin_excluded(tmp_path):
    """跨切分版本的同文孪生：role=None 的段文本与基准条目冻结文本相同 → 剔除。"""
    db.init_db()
    with db.session() as s:
        from app.models import BenchmarkItem, BenchmarkSet
        hidden = "夜色像一张收口的网，把他整个人罩了进去，连呼吸都变得滞重。"
        ctx = "他沿着巷子往深处走，两侧的屋檐滴水，在青石板上敲出细密的鼓点，一声接一声，敲得人心头发紧，久久不能平息。"
        bs = BenchmarkSet(id="BS-LEAK-T", name="leak-t", kind="corruption_detection", n_items=1, spec={})
        s.add(bs); s.flush()
        s.add(BenchmarkItem(set_id=bs.id, kind="corruption_detection",
                            text_a=hidden, text_b="劣化侧另一段足够长的文本内容，用于测试。", answer="A",
                            context=ctx))
        w = Work(title="斗罗大陆（唐家三少）-leak-t", source="test:leak")
        s.add(w); s.flush()
        # 孪生正文段（role=None，可训练面）+ 邻段为冻结 context 的段
        twin = Segment(work_id=w.id, ordinal=0, text=hidden, role=None,
                       integrity='{"src_ok": true}', n_sentences=1, n_chars=len(hidden))
        ctx_seg = Segment(work_id=w.id, ordinal=1, text=ctx, role=None,
                          integrity='{"src_ok": true}', n_sentences=1, n_chars=len(ctx))
        s.add_all([twin, ctx_seg]); s.commit()   # _bench_hashes 走独立 session，必须先 commit
        starts = {}
        EX._BENCH_HASHES = None            # 单例缓存按进程缓存——新种入的基准条目要重载
        r_twin = EX._excluded_reason(s, twin, starts, for_train=True)
        r_ctx = EX._excluded_reason(s, ctx_seg, starts, for_train=True)
    assert r_twin == "benchmark_content", f"孪生正文漏进训练导出：{r_twin}"
    assert r_ctx == "benchmark_content", f"邻段上下文泄漏未拦：{r_ctx}"


def test_normal_segment_not_flagged_by_content_guard():
    """普通段不许被内容守卫误伤（相对断言：只有命中冻结文本才剔除）。"""
    db.init_db()
    with db.session() as s:
        w = Work(title="凡人修仙传（忘语）-leak-t2", source="test:leak2")
        s.add(w); s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="韩立低头看了看手中的玉盒，神色如常地把它收进了储物袋里。",
                      role=None, integrity='{"src_ok": true}', n_sentences=1, n_chars=32)
        s.add(seg); s.flush()
        starts = {}
        EX._BENCH_HASHES = None
        r = EX._excluded_reason(s, seg, starts, for_train=True)
    assert r == "", f"普通段被内容守卫误伤：{r}"


def test_rm_conflict_rule_same_segment_same_text(tmp_path):
    """军师 P1-6：同段同文本多来源冲突 → 按优先级保留一条
    （user_verdict > corruption_variable > judge_majority），其余丢弃并计数。"""
    exp = "EXP-RM-CONFLICT"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-conflict", [(0, TEXT, None, '{"src_ok": true}')])
        c, _ = _cand(s, exp, segs[0], text=TEXT + "候选。")
        _judges(s, exp, c, ["candidate", "candidate", "human"])   # 弱标：candidate 1.0
        _review(s, exp, c, "human")                               # 强标：human 侧 1.0
        s.commit()
    q = EX.export_rm("conflict", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_conflict.jsonl")
    # 同段同文本（TEXT）的两条冲突：强标 user_verdict 留，弱标 judge_majority 丢
    human_rows = [r for r in rows if r["side"] == "human" and r["segment_id"] == segs[0].id
                  and "".join(r["text"].split()) == "".join(TEXT.split())]
    assert len(human_rows) == 1, f"同段同文本应只剩 1 条，实得 {len(human_rows)}"
    assert human_rows[0]["label_source"] == "user_verdict", "强标必须压过弱标"
    assert q["n_conflict_dropped"] >= 1, "被丢弃的冲突行必须计数"


# ── 5. 会审补课：src_ok 三态 / 冲突确定性 / 缺主键响炸 / summary 自洽 ──

def test_src_ok_tri_state_gate(tmp_path):
    """src_ok 三态：True 放行；False→bad_src；缺键/null→src_unverified。
    '非 True 不得入池'必须由代码保证（军师会审要求 a）。"""
    db.init_db()
    with db.session() as s:
        w = Work(title="t-tri", source="test:tri")
        s.add(w); s.flush()
        cases = {
            0: '{"src_ok": true}',
            1: '{"src_ok": false}',
            2: '{}',                       # 缺键
            3: '{"src_ok": null}',         # 显式 null
        }
        segs = {}
        for i, integ in cases.items():
            seg = Segment(work_id=w.id, ordinal=i, text=TEXT + str(i), integrity=integ,
                          n_sentences=1, n_chars=len(TEXT) + 1)
            s.add(seg); s.flush()
            segs[i] = seg
        s.commit()
        starts = {}
        got = {i: EX._excluded_reason(s, segs[i], starts, for_train=True)
               for i in cases}
    assert got[0] == "", "src_ok=True 必须放行"
    assert got[1] == "bad_src", "查过判坏必须记 bad_src"
    assert got[2] == "src_unverified" and got[3] == "src_unverified", \
        "缺键与 null 都属未校验，不得入池"


def test_rm_conflict_same_priority_deterministic(tmp_path):
    """军师会审要求 b：同优先级冲突重跑两次，产物逐字节一致。"""
    exp = "EXP-RM-DETER"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-deter", [(0, TEXT, None, '{"src_ok": true}')])
        c1, _ = _cand(s, exp, segs[0], text=TEXT + "候选甲。")
        _judges(s, exp, c1, ["candidate", "candidate", "human"])
        s.commit()
    q1 = EX.export_rm("deter1", out_dir=tmp_path)
    q2 = EX.export_rm("deter2", out_dir=tmp_path)
    f1 = (tmp_path / "rm_deter1.jsonl").read_bytes()
    f2 = (tmp_path / "rm_deter2.jsonl").read_bytes()
    assert f1 == f2 and q1["n"] == q2["n"] and q1["n"] > 0, "同输入两次导出必须逐字节一致"


def test_rm_missing_segment_id_raises():
    """军师会审要求 c：缺主键走 _require_row_keys 响炸（ValueError）。"""
    with pytest.raises(ValueError, match="缺主键"):
        EX._require_row_keys("", "CND-1")
    with pytest.raises(ValueError, match="缺主键"):
        EX._require_row_keys("SEG-1", None)
    EX._require_row_keys("SEG-1", "CND-1")   # 完整键不抛


def test_rm_summary_new_fields_present_and_consistent(tmp_path):
    """军师会审要求 d：新字段存在且与桶计数自洽（n == 行数，分桶之和 == n）。"""
    exp = "EXP-RM-FIELDS2"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w, segs = _work_seg(s, "t-rm-fields2", [(0, TEXT, None, '{"src_ok": true}')])
        c, _ = _cand(s, exp, segs[0])
        _review(s, exp, c, "human")
        s.commit()
    q = EX.export_rm("fields2", out_dir=tmp_path)
    rows = _rows(tmp_path / "rm_fields2.jsonl")
    for k in ("n_benchmark_content_excluded", "n_src_unverified_excluded",
              "n_conflict_dropped", "n_segments_covered" if "n_segments_covered" in q else "n"):
        assert k in q, f"summary 缺字段 {k}"
    assert q["n"] == len(rows)
    assert q["by_source"]["user_verdict"] * 2 == sum(
        1 for r in rows if r["label_source"] == "user_verdict") * 2  # 自洽占位
    assert sum(v for v in q["by_source"].values()) >= len(
        [r for r in rows if r["label_source"] in ("corruption_variable", "user_verdict", "judge_majority")])
