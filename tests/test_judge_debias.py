"""评委偏差校正探针的回归测试（2026-09-19）。

背景：scripts/judge_debias_probe.py 回答「评委的方向偏差（控制臂上偏
AI 版、集霸 0/8）经方向反转 / 偏移校正后，agreement 能否过 Gate 0.70」。
本文件锁死三件事：

1. **空数据必须报错**而不是静默出空表（本项目纪律④——静默空跑出
   「漂亮报告」是本项目反复踩的坑）；
2. **反转投票对 κ 的影响方向**——符号搞反不会抛异常，只会给出反的
   结论（本项目反复踩的坑），所以方向性断言先于数值断言；
3. map 偏移校正的**非胜方桶不得折算**：把「答无胜方」折算成 candidate
   会与集霸答 candidate 的题碰巧一致而虚高（开发当天真踩过）。

装载函数 load_probe 只在 conftest 的**临时库**上验证（LG_DATABASE_URL
已指向 pytest 临时目录，不碰生产库；TestClient 的 lifespan 不会替它
建表，必须显式 init_db——见记忆文件 zcode-testclient-quirks）。
"""
import math
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.judge_debias_probe import (  # noqa: E402
    Item,
    JudgeCell,
    ProbeData,
    agreement_bin,
    decide,
    flip_resolved,
    ipw_kappa,
    kappa_bin,
    load_probe,
    map_recal_table,
    resolve_pick,
    run_probe,
    stress_tests,
    wilson,
    _exclude_pairs,
    _stress_verdict,
)

MODEL = "moonshotai/kimi-k3"
MODEL2 = "deepseek/deepseek-v4.1-flash"
PV = "judge_preference_v4_heldout_near1"


def _cell(resolved: str, pick: str = "A", hwa: bool | None = True) -> JudgeCell:
    return JudgeCell(resolved=resolved, pick=pick, human_was_a=hwa)


def _items(specs) -> dict[str, Item]:
    """specs: (user, kind, cctype) 列表 → {CNDi: Item}。"""
    return {f"CND{i}": Item(cid=f"CND{i}", kind=kind, cctype=ct, user=u)
            for i, (u, kind, ct) in enumerate(specs)}


def _judges(cells: dict[str, JudgeCell]) -> dict[tuple[str, str], dict[str, JudgeCell]]:
    return {(MODEL, PV): cells}


# ── ① 空数据必须报错，不许静默返回 ──────────────────────────

def test_empty_items_raises():
    with pytest.raises(ValueError):
        run_probe(ProbeData(items={}, judges={})) 


def test_empty_judges_raises():
    items = {f"CND{i}": Item(cid=f"CND{i}", kind="corrupt", cctype="EXPLICITIZE",
                             user="human") for i in range(3)}
    with pytest.raises(ValueError):
        run_probe(ProbeData(items=items, judges={}))


def test_map_recal_empty_calibration_raises():
    with pytest.raises(ValueError):
        map_recal_table([], {u: 0.25 for u in
                             ("human", "candidate", "tie", "both_bad")})


# ── ② 反转投票对 κ 的影响方向（小样本手算）───────────────────

def test_flip_turns_antagonist_into_perfect_agreement():
    """与集霸**完全相反**的评委：反转后 κ 从 −0.8 → +1.0，agreement 0 → 1。

    手算（6 题，集霸 4 human / 2 candidate，评委全部答反）：
      po = 0；pj = 2/6；pu = 4/6；pe = (2/6)(4/6) + (4/6)(2/6) = 0.444；
      κ_raw = (0 − 0.444) / 0.556 = −0.8。
      反转后评委与集霸完全一致：po = 1；pj = 4/6；pu = 4/6；
      pe = (4/6)(4/6) + (2/6)(2/6) = 0.556；κ = 0.444/0.444 = +1.0。
    """
    user = [1, 1, 1, 1, 0, 0]
    judge = [0, 0, 0, 0, 1, 1]
    raw = kappa_bin(list(zip(user, judge)))
    flipped = kappa_bin(list(zip(user, [1 - j for j in judge])))
    assert raw == pytest.approx(-0.8, abs=1e-9)
    assert flipped == pytest.approx(1.0, abs=1e-9)
    assert agreement_bin(list(zip(user, judge))) == pytest.approx(0.0)
    assert agreement_bin(list(zip(user, [1 - j for j in judge]))) == pytest.approx(1.0)


def test_flip_turns_perfect_agreement_into_antagonist():
    """与集霸**完全一致**的评委：反转后 κ 从 +1.0 → −0.8（方向必须对称）。"""
    user = [1, 1, 1, 1, 0, 0]
    judge = [1, 1, 1, 1, 0, 0]
    raw = kappa_bin(list(zip(user, judge)))
    flipped = kappa_bin(list(zip(user, [1 - j for j in judge])))
    assert raw == pytest.approx(1.0, abs=1e-9)
    assert flipped == pytest.approx(-0.8, abs=1e-9)


def test_flip_resolved_keeps_nonbinary_buckets():
    """tie/both_bad 没有方向，反转必须原样保留（不能折成 human/candidate）。"""
    assert flip_resolved("human") == "candidate"
    assert flip_resolved("candidate") == "human"
    assert flip_resolved("both_bad") == "both_bad"
    assert flip_resolved("tie") == "tie"


# ── ③ 基线数值（构造验证）────────────────────────────────────

def test_position_baseline_constant_pick_a():
    """「永远选 A」的基线：方向由每题 human_was_a 决定，与评委怎么选无关。"""
    # human_was_a 交替；集霸胜方与之配合：A=human 时集霸答 human、A=candidate 时答 candidate
    # → 恒选 A 撞对 3/4？逐题：resolve_pick("A", hwa) == "human" iff hwa。
    items = {
        "C0": Item(cid="C0", kind="corrupt", cctype="EXPLICITIZE", user="human"),
        "C1": Item(cid="C1", kind="corrupt", cctype="EXPLICITIZE", user="candidate"),
        "C2": Item(cid="C2", kind="corrupt", cctype="EXPLICITIZE", user="human"),
        "C3": Item(cid="C3", kind="corrupt", cctype="EXPLICITIZE", user="candidate"),
    }
    judges = {(MODEL, PV): {
        "C0": _cell("human", "A", True),    # A 位是 human，恒选 A → human ✓
        "C1": _cell("candidate", "B", True),  # A 位是 human，恒选 A → human ≠ candidate ✗
        "C2": _cell("candidate", "B", False),  # A 位是 candidate，恒选 A → candidate ✗
        "C3": _cell("human", "A", False),   # A 位是 candidate，恒选 A → candidate ✓
    }}
    data = ProbeData(items=items, judges=judges)
    user, judge, _ = _exclude_pairs(data, MODEL, PV, lambda c: resolve_pick("A", c.human_was_a))
    assert agreement_bin(list(zip(user, judge))) == pytest.approx(0.5)
    # 位置基线只撞 A 位恰为集霸胜方的题（2/4），与评委实际选择无关
    assert resolve_pick("A", True) == "human"
    assert resolve_pick("A", False) == "candidate"


def test_majority_baseline_equals_human_share():
    """恒定答 human 的基线 agreement = 集霸答 human 的占比（排除制子集上）。"""
    specs = [("human", "corrupt", "A"), ("human", "corrupt", "B"),
             ("candidate", "corrupt", "C"), ("human", "corrupt", "D")]
    items = {f"CND{i}": Item(cid=f"CND{i}", kind=k, cctype=t, user=u)
             for i, (u, k, t) in enumerate(specs)}
    cells = {cid: _cell("candidate") for cid in items}   # 评委恒答 candidate
    data = ProbeData(items=items, judges=_judges(cells))
    rep = run_probe(data)
    rows = [r for r in rep["rows"] if r["transform"] == "maj"]
    assert rows and rows[0]["n"] == 4
    assert rows[0]["agree"] == pytest.approx(0.75)   # = 集霸 human 占比 3/4


# ── ④ map 校准与排除制的口径约束 ────────────────────────────

def test_map_recal_table_laplace_and_marginal_tiebreak():
    """拉普拉斯平滑 + argmax：candidate 方向的众数桶是 both_bad；human 方向是 human。"""
    pairs = [("candidate", "both_bad"), ("candidate", "both_bad"),
             ("candidate", "human"), ("human", "human"), ("human", "human")]
    marg = {"human": 0.4, "candidate": 0.3, "tie": 0.1, "both_bad": 0.2}
    m = map_recal_table(pairs, marg, alpha=1.0)
    # dir=candidate：c={both_bad:2, human:1} → p(both_bad)=3/7 最大 → both_bad
    # dir=human：c={human:2} → p(human)=3/6=0.5 最高 → human
    assert m == {"human": "human", "candidate": "both_bad"}


def test_map_recal_marginal_tiebreak_on_equal_counts():
    """两个桶计数相等时取 corr24 集霸边缘分布更大的一方。"""
    # candidate 方向 both_bad 与 human 各 1（α=1 平滑后同分 2/6），边缘 human 更大 → human
    pairs = [("candidate", "both_bad"), ("candidate", "human")]
    marg = {"human": 0.5, "candidate": 0.2, "tie": 0.1, "both_bad": 0.2}
    m = map_recal_table(pairs, marg, alpha=1.0)
    assert m["candidate"] == "human"


def test_exclude_pairs_drops_nonbinary_answers():
    """map 输出 tie/both_bad 的题必须剔除，不许折算成 candidate 碰巧算对。"""
    items = {
        "C0": Item(cid="C0", kind="corrupt", cctype="EXPLICITIZE", user="human"),
        "C1": Item(cid="C1", kind="corrupt", cctype="EXPLICITIZE", user="candidate"),
    }
    # 映射把两个方向都打到 both_bad：若错误折算，C1 会假性「对上」
    mapping = {"human": "both_bad", "candidate": "both_bad"}
    data = ProbeData(items=items, judges=_judges(
        {"C0": _cell("human"), "C1": _cell("candidate")}))
    user, judge, _ = _exclude_pairs(data, MODEL, PV, lambda c: mapping.get(c.resolved))
    assert user == [] and judge == []      # 全部剔除，n=0（渲染显示不适用）


# ── ⑤ 控制臂面读数（变换是否把偏差压下去）────────────────────

def test_ctrl_face_flip_reduces_ai_picks_without_breaking_corrupt_side():
    """控制臂上评委 4/4 选 AI 版：flip 后选 AI 版必须 < 4（偏差被压）。

    同时真劣化题（集霸判 candidate 胜）在 flip 后被翻错——这正是
    「反转救控制臂、毁判别力」的核心对照，探针报告的结论就建在这上。
    """
    items = {
        "K0": Item(cid="K0", kind="control", cctype="NEUTRAL_PARAPHRASE", user="tie"),
        "K1": Item(cid="K1", kind="control", cctype="NEUTRAL_PARAPHRASE", user="tie"),
        "D0": Item(cid="D0", kind="corrupt", cctype="EXPLICITIZE", user="candidate"),
        "D1": Item(cid="D1", kind="corrupt", cctype="EXPLICITIZE", user="candidate"),
    }
    cells = {"K0": _cell("candidate"), "K1": _cell("candidate"),
             "D0": _cell("candidate"), "D1": _cell("candidate")}
    data = ProbeData(items=items, judges=_judges(cells))
    rep = run_probe(data)
    by_t = {r["transform"]: r for r in rep["rows"]}
    assert by_t["raw"]["ctrl"]["ai"] == 2 and by_t["raw"]["ctrl"]["n"] == 2
    assert by_t["flip"]["ctrl"]["ai"] == 0            # 偏差被压到 0
    # 但真劣化题上判别力被翻掉：严格口径 candidate 胜 2 题全错
    assert by_t["raw"]["strict_hit"] == 2 and by_t["flip"]["strict_hit"] == 0


# ── ⑥ IPW（corr24 非分层时必须与 raw 一致）───────────────────

def test_ipw_kappa_equals_raw_when_weights_uniform():
    pairs = [(1, 1), (1, 0), (0, 1), (0, 0)]
    w = [1.0] * 4
    wk, n_eff = ipw_kappa(pairs, w)
    assert wk == pytest.approx(kappa_bin(pairs), abs=1e-12)
    assert n_eff == pytest.approx(4.0)


# ── ⑦ wilson 小样本行为 ─────────────────────────────────────

def test_wilson_zero_hits_gives_nonzero_lower_bound():
    lo, hi = wilson(0, 5)
    assert lo == 0.0 and 0.0 < hi < 0.6
    lo, hi = wilson(5, 5)
    assert 0.4 < lo < 1.0 and hi == 1.0
    assert math.isnan(wilson(0, 0)[0])


# ── ⑧ 装载函数（只在 conftest 临时库上验证）──────────────────

def test_load_probe_empty_db_raises():
    from app.db import SessionLocal, init_db
    init_db()          # TestClient 的 lifespan 不会替它触发，必须显式
    with pytest.raises(ValueError):
        load_probe(SessionLocal)


def test_load_probe_roundtrip(tmp_path):
    """灌最小链条数据 → load_probe 读回 → run_probe 排除制数字与手算一致。

    3 题：1 控制臂（评委被偏差拉到 AI 版，✗）+ 真劣化 human 胜（✓）
    + 真劣化 candidate 胜（✓）→ n=3、agreement=2/3。REQUIRED_MODELS
    要求 kimi 与 deepseek 两家都有判定，故每题灌两条 judge_runs。
    """
    from app.db import SessionLocal, init_db
    from app.models import (Candidate, ControlledCorruption, Experiment, Frame,
                            JudgeRun, ReviewItem, Segment, Work)

    init_db()
    with SessionLocal() as s:
        # 模型类之间没有声明 relationship，SQLAlchemy 不知道跨表插入顺序，
        # 必须逐级 flush 把 parent 先落库，否则 FK 约束（PRAGMA 已开）会炸。
        s.add(Work(id="WK-t", title="测试书", source="inbox:test"))
        s.flush()
        s.add(Experiment(id="EXP-T", name="探针测试"))
        s.flush()
        s.add(Segment(id="SEG-t", work_id="WK-t", ordinal=0, text="测试原文"))
        s.flush()
        s.add(Frame(id="FR-t", experiment_id="EXP-T", segment_id="SEG-t",
                    granularity="S", extractor_model="mock", prompt_version="p"))
        s.flush()
        for i, (ct, user) in enumerate([
                ("NEUTRAL_PARAPHRASE", "tie"),
                ("EXPLICITIZE", "human"),
                ("LITERARY_OVERWRITE", "candidate")]):
            cid = f"CND-t{i}"
            s.add(Candidate(id=cid, experiment_id="EXP-T", frame_id="FR-t",
                            segment_id="SEG-t", anon_label=f"X{i}", model="gen",
                            prompt_version="corrupt_v1", text="劣化版"))
            s.add(ControlledCorruption(id=f"CC-t{i}", experiment_id="EXP-T",
                                       segment_id="SEG-t", frame_id="FR-t",
                                       candidate_id=cid, corruption_type=ct,
                                       text="劣化版"))
            s.add(ReviewItem(id=f"RV-t{i}", experiment_id="EXP-T",
                             subject_type="candidate", subject_id=cid,
                             reasons=["batch_corr24", "w:1.0"], status="done",
                             human_verdict={"winner_resolved": user}))
            # 两家评委：控制臂题两家都答 candidate（方向偏差）；真劣化题答对
            for m in (MODEL, MODEL2):
                s.add(JudgeRun(id=f"JR-t{i}-{m.split('/')[-1]}", experiment_id="EXP-T",
                               subject_type="candidate", subject_id=cid,
                               judge_kind="preference", model=m, prompt_version=PV,
                               verdict={"human_was_a": True, "winner": "A",
                                        "winner_resolved":
                                        "candidate" if i == 0 else user}))
            s.flush()
        s.commit()

    data = load_probe(SessionLocal)
    assert len(data.items) == 3
    assert {(m, PV) for m, _ in data.judges} == {(MODEL, PV), (MODEL2, PV)}
    rep = run_probe(data)
    raw = [r for r in rep["rows"] if r["transform"] == "raw" and r["model"] == MODEL]
    # 排除制：集霸答 tie 的控制臂题被剔除 → 胜方子集 n=2（两题评委全对）
    assert raw[0]["n"] == 2
    assert raw[0]["agree"] == pytest.approx(1.0)
    assert raw[0]["ctrl"]["ai"] == 1 and raw[0]["ctrl"]["n"] == 1
    # 诚实链路：n=2 全对过 Gate 线，但置换 p=1.0（两题重排后恒全对），
    # N0 必须把它否掉——「过线」绝不允许在没有显著性背书时被采信。
    dec = decide(rep["rows"])
    assert dec["passed"] is True
    assert "过线" in dec["verdict"]
    stress = stress_tests(data, rep["mapping"])
    final = _stress_verdict(dec, stress)
    assert "不采信" in final
    assert "N0" in final
