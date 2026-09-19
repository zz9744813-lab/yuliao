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
            w, segs = _work_seg(s, "t-rm-fields", [(0, TEXT, None, None)])
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
                            [(i, TEXT + str(i), None, None) for i in range(5)])
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
                            [(0, TEXT, None, None), (1, TEXT + "二", None, None)])
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
                            [(0, TEXT, None, None), (1, TEXT + "二", None, None)])
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
        w, segs = _work_seg(s, "t-rm-ctrl", [(0, TEXT, None, None)])
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
        w, segs = _work_seg(s, "t-rm-iso", [(0, TEXT, None, None)])
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
        w3, cs = _work_seg(s, "t-rm-iso-ctrl", [(0, TEXT + "控制段。", None, None)])
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
        w, segs = _work_seg(s, "t-rw-ok", [(0, TEXT, None, None), (1, TEXT + "续。", None, None)])
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
        w, segs = _work_seg(s, "t-rw-iso", [(0, TEXT, None, None)])
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
