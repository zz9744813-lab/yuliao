"""位置偏差诊断回归（2026-09-16）。

背景：v4 的"挑 candidate 比例"在 r25/s30 上 4 次独立测量**都恰好 0.500**
（联合概率约 1/1200）。这个"过于整齐"暴露了一个从未测过的混淆：**评委有强位置偏差**
（实测选 A 率 0.74–0.80）。A/B 位置是随机化的，所以位置偏差**不偏置 κ**，
但会**吃掉统计功效**——这是"κ 一直上不去"的一个可能真因。

本文件锁死诊断的**符号约定**。分析时我把"位置偏差"与"内容判据"的方向搞混过一次，
而这类符号错误不会报错，只会给出反的结论（本项目反复踩的坑）。

⚠ 测试用**显式 db_path**（依赖注入），不 monkeypatch 模块全局 `DB`：
`import heldout_eval` 与 `import scripts.heldout_eval` 是两个独立模块实例，
patch 错对象会让函数静默返回 None，且**可能误写生产库**（本项目已踩过）。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import scripts.heldout_eval as HE  # noqa: E402
from scripts.heldout_eval import _position_diagnostic  # noqa: E402

PV = HE.PROMPT_VARIANTS["v4"][1]


def _mk(tmp_path, rows):
    """rows: [(cid, human_was_a, winner, user_verdict, verdict_override)] → 临时库路径。"""
    dbp = tmp_path / "pos.db"
    con = sqlite3.connect(dbp)
    con.execute("create table review_items (id text primary key, experiment_id text,"
                " subject_id text, human_verdict text, reasons text, status text,"
                " reviewed_at text)")
    con.execute("create table judge_runs (id integer primary key autoincrement,"
                " experiment_id text, subject_type text, subject_id text, judge_kind text,"
                " model text, prompt_version text, verdict text, confidence real,"
                " abstain integer, status text, created_at text)")
    for i, (cid, h, w, u, ov) in enumerate(rows):
        con.execute("insert into review_items values (?,?,?,?,?,?,?)",
                    (f"RV{i}", "EXP", cid, json.dumps({"winner_resolved": u}),
                     json.dumps([]), "done", "2026-09-16T00:00:00Z"))
        verdict = ov if ov is not None else json.dumps({"human_was_a": h, "winner": w})
        con.execute("insert into judge_runs (experiment_id,subject_type,subject_id,judge_kind,"
                    "model,prompt_version,verdict,abstain,status) values (?,?,?,?,?,?,?,0,'ok')",
                    ("EXP", "candidate", cid, "preference", "m", PV, verdict))
    con.commit()
    con.close()
    return dbp


def _run(dbp, rows):
    cids = [r[0] for r in rows]
    user = {r[0]: (1 if r[3] == "human" else 0) for r in rows}
    return _position_diagnostic(None, user, cids, "m", "v4", db_path=dbp)


def test_content_sensitive_judge_scores_perfect(tmp_path):
    """有内容判断力的评委：准确率 1.0、选 A 率 0.5。

    ⚠ 构造要点：**"A 是候选" ≠ "A 更差"**。若用户判候选胜，则候选（＝A）才是更好的那段。
    真值方向必须由 (human_was_a, 用户判定) **共同**决定，不能只看 human_was_a。
    这里让用户永远判 human（所以人类那段永远更好）：
      - A 是 human（i 偶）→ A 更好 → 选 A
      - A 是候选（i 奇）→ B 更好 → 选 B
    """
    rows = []
    for i in range(20):
        h = i % 2 == 0
        rows.append((f"g{i}", h, "A" if h else "B", "human", None))
    pd = _run(_mk(tmp_path, rows), rows)
    assert pd is not None
    assert pd["pick_a"] == pytest.approx(0.5), "本例位置平衡，选A率应为 0.5"
    assert pd["acc"] == pytest.approx(1.0), "永远选中更好的那边 → 准确率 1.0"
    assert pd["acc"] > pd["baseline_acc"]
    assert pd["human_prec"] == pytest.approx(1.0), "说 human 时全对"


def test_pure_position_bias_equals_baseline(tmp_path):
    """纯位置偏差（永远选 A）：准确率应**恰好等于**「永远选 A」基线。

    核心回归：若诊断把位置伪影报成"有判断力"，就会把噪声当能力。
    """
    rows = []
    for i in range(20):
        h = i % 2 == 0
        rows.append((f"p{i}", h, "A", "human" if h else "candidate", None))
    pd = _run(_mk(tmp_path, rows), rows)
    assert pd["pick_a"] == pytest.approx(1.0)
    assert pd["acc"] == pytest.approx(pd["baseline_acc"]), (
        f"纯位置偏差的准确率应恰等于基线：acc={pd['acc']} base={pd['baseline_acc']}")


def test_negative_information_detected(tmp_path):
    """反向评委（永远专挑**更差**的一边）：准确率 0，明显低于基线。

    项目实测：v3 的准确率 0.457/0.370 **低于**位置基线 —— 即 v3 是负信息的。
    构造：用户永远判 human（人类更好），评委则 A 是 human 时选 B、A 是候选时选 A，
    即永远选更差的那边 → 准确率 0。
    """
    rows = []
    for i in range(20):
        h = i % 2 == 0
        rows.append((f"n{i}", h, "B" if h else "A", "human", None))
    pd = _run(_mk(tmp_path, rows), rows)
    assert pd["acc"] == pytest.approx(0.0), "永远选更差的 → 准确率 0"
    assert pd["acc"] < pd["baseline_acc"] - 0.4, "应明显低于位置基线"


def test_invalid_verdicts_skipped(tmp_path):
    """verdict 为 null / 非 dict 的记录必须跳过，不能污染 n。"""
    rows = []
    for i in range(6):
        h = i % 2 == 0
        rows.append((f"x{i}", h, "A" if h else "B", "human" if h else "candidate", None))
    rows[0] = (rows[0][0], rows[0][1], rows[0][2], rows[0][3], "null")
    pd = _run(_mk(tmp_path, rows), rows)
    assert pd["n"] == 5, f"无效记录应被跳过，实得 n={pd['n']}"


def test_diagnostic_never_touches_production_db(tmp_path):
    """❗安全回归：诊断只读传入的 db_path，绝不碰生产库。

    这不是洁癖：写测试时若 patch 到错误模块实例，`sqlite3.connect(HE.DB)`
    会连上真实库并执行 UPDATE。本项目已在开发过程中真实发生过一次。
    """
    rows = [(f"s{i}", i % 2 == 0, "A", "human" if i % 2 == 0 else "candidate", None)
            for i in range(4)]
    dbp = _mk(tmp_path, rows)
    prod = Path(HE.DB)
    before = prod.stat().st_mtime_ns if prod.exists() else None
    _run(dbp, rows)
    after = prod.stat().st_mtime_ns if prod.exists() else None
    assert before == after, "诊断不得改动生产库"


def test_db_path_defaults_to_module_db(tmp_path):
    """不传 db_path 时应回落到模块默认 DB（保持线上调用不变）。"""
    sig_ok = _position_diagnostic.__defaults__
    assert sig_ok is not None


# ── verdict 双形态（ORM dict vs 裸 sqlite TEXT）──────────────────
def test_as_dict_accepts_both_orm_and_raw():
    """❗核心回归：verdict 从 ORM 来是 dict、从裸 sqlite 来是 str，两种都必须认。

    实测事故（2026-09-16）：`_human_was_a` 走 SQLAlchemy，JSON 列**已经解析成 dict**，
    我又 `json.loads` 一次 → 抛 TypeError → 被 `except` 静默吞掉 → 全部返回 None，
    表现为"待跑 0 次调用"，白跑一次全量反序。这类静默 None 是本项目的高频故障模式。
    """
    assert HE._as_dict({"winner": "A"}) == {"winner": "A"}
    assert HE._as_dict('{"winner": "A"}') == {"winner": "A"}
    assert HE._as_dict(None) is None
    assert HE._as_dict("not json") is None
    assert HE._as_dict("[1,2]") is None, "非 dict 的合法 JSON 也要拒绝"
    assert HE._as_dict(42) is None


def test_human_was_a_reads_orm_dict():
    """_human_was_a 必须能从 ORM 的 dict 形态 verdict 里读出 human_was_a。

    注意：conftest 已把 `LG_DATABASE_URL` 指向临时库，故 init_db 建的是临时表，
    不会碰生产数据。
    """
    from app import db as _db
    from app.models import Experiment, JudgeRun

    _db.init_db()
    with _db.session() as s:
        # judge_runs.experiment_id 有外键约束，先确保实验行存在
        if not s.get(Experiment, HE.EXP):
            s.add(Experiment(id=HE.EXP, name="t", status="created", config={}, stats={}))
            s.commit()
        s.query(JudgeRun).filter_by(subject_id="T-PROBE").delete()
        s.add(JudgeRun(experiment_id=HE.EXP, subject_type="candidate",
                       subject_id="T-PROBE", judge_kind="preference", model="m",
                       prompt_version="judge_preference_v4_heldout",
                       verdict={"human_was_a": False, "winner": "A"},
                       abstain=False, status="ok"))
        s.commit()
    try:
        assert HE._human_was_a("T-PROBE", "m", "judge_preference_v4_heldout") is False
    finally:
        with _db.session() as s:
            s.query(JudgeRun).filter_by(subject_id="T-PROBE").delete()
            s.commit()


# ── 两序合并规则 ──────────────────────────────────────────────
def test_two_order_agreement_is_adopted():
    """两序指向同一内容方向 → 采纳。"""
    calls = []

    def fake_read(cid, model, pv, con):
        calls.append(pv)
        return "human" if not pv.endswith(HE.REVERSE_SUFFIX) else "human"

    import scripts.heldout_eval as H2
    orig = H2._read_verdict
    H2._read_verdict = fake_read
    try:
        assert H2._two_order_verdict("c", "m", "pv", None) == "human"
    finally:
        H2._read_verdict = orig
    assert len(calls) == 2, "必须读正反两序"


def test_two_order_disagreement_abstains():
    """两序方向相反 → 弃权（位置偏差主导，不含内容信息）。

    这是校正的核心：若换边就换答案，说明驱动它的是位置而非文本。
    硬算进 agreement 只会稀释；**弃权比瞎猜更诚实**。
    """
    import scripts.heldout_eval as H2

    def fake_read(cid, model, pv, con):
        return "candidate" if not pv.endswith(HE.REVERSE_SUFFIX) else "human"

    orig = H2._read_verdict
    H2._read_verdict = fake_read
    try:
        assert H2._two_order_verdict("c", "m", "pv", None) is None
    finally:
        H2._read_verdict = orig


def test_two_order_missing_one_side_abstains():
    """任一序缺失 → 弃权（不能拿单序冒充两序）。"""
    import scripts.heldout_eval as H2

    def fake_read(cid, model, pv, con):
        return None if pv.endswith(HE.REVERSE_SUFFIX) else "human"

    orig = H2._read_verdict
    H2._read_verdict = fake_read
    try:
        assert H2._two_order_verdict("c", "m", "pv", None) is None
    finally:
        H2._read_verdict = orig
