"""锚点自检脚本回归（2026-09-16，含 h30 判完后的修正）。

**背景**：`select_harvest_batch.py` 每批掺 30% 锚点，用来自校验抽样框；但读回
`stratum:` 的脚本一直不存在（`heldout_eval` 等里 stratum 出现 0 次）。本文件锁死
`scripts/anchor_check.py` 的五类错误：

1. **层分错**：ANCHOR 与 HARVEST 混在一起算 —— 那样锚点的无偏性就白设计了。
2. **弃权混入分母**：`both_bad` / `equal` / `cant_judge` 不是「候选没胜」，不能进分母。
3. **IPW 算成朴素平均**：分层抽样下朴素平均会被过采样的收割段带偏。
4. **静默空跑**：没数据 / 缺 `w:` 时不得静默返回一个数。
5. **❗只测没功效的那一侧**（第一版真犯过的错，h30 实测）：判定的主检验必须是
   **收割段是否兑现建批承诺**（21 条，有功效），而不是「锚点 vs 池基准率」
   （9 条，CI 宽到 [0.02,0.44]，几乎放行一切）。h30 承诺 0.696 的收割段打出 3/21
   （P=2.4e-07），而锚点那侧照样落在 CI 内 —— 第一版据此打印「✓ 可继续」，放过了真问题。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import anchor_check as AC  # noqa: E402


def _make_db(tmp_path: Path, rows: list[dict]) -> Path:
    """建最小库：只要 anchor_check 查询用到的列。

    rows 每项：{cid, verdict, stratum, p, prompt_version}
    """
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("create table candidates (id text primary key, prompt_version text)")
    con.execute("""create table review_items (
        id integer primary key autoincrement, experiment_id text, subject_id text,
        status text, reasons text, human_verdict text)""")
    for r in rows:
        con.execute("insert into candidates values (?,?)",
                    (r["cid"], r.get("prompt_version", "reconstruct_v1")))
        reasons = [f"batch_{r.get('batch', 'hX')}"]
        if r.get("stratum"):
            reasons.append(f"stratum:{r['stratum']}")
        if r.get("p") is not None:
            reasons.append(f"w:{r['p']:.4f}")
        if r.get("score") is not None:
            reasons.append(f"score:{r['score']:.4f}")
        hv = json.dumps({"winner_resolved": r["verdict"]})
        con.execute("insert into review_items "
                    "(experiment_id, subject_id, status, reasons, human_verdict) "
                    "values (?,?,?,?,?)",
                    (AC.EXP, r["cid"], r.get("status", "done"),
                     json.dumps(reasons), hv))
    con.commit()
    con.close()
    return db


def _rows(n_anchor, k_anchor, n_harvest, k_harvest, p_a=0.04, p_h=0.11):
    rows, i = [], 0
    for j in range(n_anchor):
        rows.append({"cid": f"CND-a{i}", "verdict": "candidate" if j < k_anchor else "human",
                     "stratum": "ANCHOR", "p": p_a})
        i += 1
    for j in range(n_harvest):
        rows.append({"cid": f"CND-h{i}", "verdict": "candidate" if j < k_harvest else "human",
                     "stratum": "HARVEST", "p": p_h, "score": 0.5})
        i += 1
    return rows


# ── ① 分层 ──────────────────────────────────────────────────
def test_strata_split_correctly(tmp_path):
    db = _make_db(tmp_path, _rows(9, 3, 21, 15))
    res = AC.analyze("hX", db_path=db)
    assert res["strata"]["ANCHOR"]["n"] == 9
    assert res["strata"]["ANCHOR"]["k"] == 3
    assert res["strata"]["HARVEST"]["n"] == 21
    assert res["strata"]["HARVEST"]["k"] == 15
    assert res["strata"]["ANCHOR"]["rate"] == pytest.approx(3 / 9)


def test_missing_w_is_not_silently_ignored(tmp_path):
    """缺入样概率时必须明说 IPW 算不了，而不是拿别的数顶上。"""
    rows = _rows(4, 1, 4, 3)
    for r in rows:
        r["p"] = None
    db = _make_db(tmp_path, rows)
    res = AC.analyze("hX", db_path=db)
    assert res["ipw"] is None
    assert res["strata"]["ANCHOR"]["n"] == 4      # 分层读数仍可用


# ── ② 弃权不进分母 ───────────────────────────────────────────
def test_both_bad_excluded_from_denominator(tmp_path):
    rows = _rows(4, 2, 6, 3)
    rows.append({"cid": "CND-x", "verdict": "both_bad", "stratum": "ANCHOR", "p": 0.04})
    rows.append({"cid": "CND-y", "verdict": "cant_judge", "stratum": "HARVEST", "p": 0.11})
    db = _make_db(tmp_path, rows)
    res = AC.analyze("hX", db_path=db)
    assert res["n_done"] == 12
    assert res["n_used"] == 10
    assert res["strata"]["ANCHOR"]["n"] == 4      # both_bad 没被算成 human
    assert res["strata"]["HARVEST"]["n"] == 6


def test_all_dropped_raises_not_silent(tmp_path):
    """全是弃权 = 没有命中率可算，必须报错而不是报 0.000。"""
    rows = [{"cid": "CND-1", "verdict": "both_bad", "stratum": "ANCHOR", "p": 0.04}]
    db = _make_db(tmp_path, rows)
    with pytest.raises(SystemExit):
        AC.analyze("hX", db_path=db)


def test_no_done_items_raises(tmp_path):
    """空批次（还没判）必须报错 —— 静默跑完会让人以为「检查过了」。"""
    rows = _rows(3, 1, 3, 1)
    for r in rows:
        r["status"] = "pending"
    db = _make_db(tmp_path, rows)
    with pytest.raises(SystemExit):
        AC.analyze("hX", db_path=db)


def test_other_prompt_versions_excluded(tmp_path):
    """纳入条件必须与 heldout_eval 一致：非 reconstruct_v1/recon_ctx_v1 的候选不算。"""
    rows = _rows(4, 2, 4, 2)
    rows.append({"cid": "CND-z", "verdict": "candidate", "stratum": "ANCHOR",
                 "p": 0.04, "prompt_version": "other_v9"})
    db = _make_db(tmp_path, rows)
    res = AC.analyze("hX", db_path=db)
    assert res["strata"]["ANCHOR"]["n"] == 4


# ── ③ IPW ───────────────────────────────────────────────────
def test_ipw_matches_hand_computation(tmp_path):
    """锚点 10 条中 2 条候选胜（p=0.1）；收割 10 条中 8 条（p=0.5）。

    w=1/p → 锚点权重 10、收割权重 2。
    IPW = (10×2 + 2×8) / (10×10 + 2×10) = 36/120 = 0.30
    """
    rows = []
    for j in range(10):
        rows.append({"cid": f"CND-a{j}", "verdict": "candidate" if j < 2 else "human",
                     "stratum": "ANCHOR", "p": 0.1})
    for j in range(10):
        rows.append({"cid": f"CND-h{j}", "verdict": "candidate" if j < 8 else "human",
                     "stratum": "HARVEST", "p": 0.5})
    db = _make_db(tmp_path, rows)
    res = AC.analyze("hX", db_path=db)
    assert res["ipw"]["rate"] == pytest.approx(0.30)
    # 收割段被过采样 → 加权后必须低于朴素合并率 (2+8)/20 = 0.5
    assert res["ipw"]["rate"] < 0.5
    assert res["ipw"]["n_eff"] < 20          # 权重悬殊时 n_eff 必须小于名义 n


def test_ipw_equals_naive_when_weights_equal(tmp_path):
    """权重相同（简单随机）时，IPW 必须退化成朴素比例，不能自己漂移。"""
    rows = []
    for j in range(10):
        rows.append({"cid": f"CND-a{j}", "verdict": "candidate" if j < 3 else "human",
                     "stratum": "ANCHOR", "p": 0.2})
    db = _make_db(tmp_path, rows)
    res = AC.analyze("hX", db_path=db)
    assert res["ipw"]["rate"] == pytest.approx(0.3)
    assert res["ipw"]["n_eff"] == pytest.approx(10.0)


# ── ④ 主检验：收割段是否兑现承诺（h30 的血） ─────────────────
def test_selector_falsified_when_harvest_misses_promise(tmp_path):
    """h30 的真实形态：承诺 0.696，实测 3/21。必须判成 selector_falsified。"""
    db = _make_db(tmp_path, _rows(9, 1, 21, 3))
    res = AC.analyze("hX", expect=0.271, expect_harvest=0.696,
                     db_path=db, expectations_path=tmp_path / "none.json")
    assert res["verdict"] == "selector_falsified"
    assert res["p_harvest"] < 1e-5


def test_anchor_pass_does_not_override_harvest_failure(tmp_path):
    """❗本文件最重要的一条（第一版真放过的错）。

    构造：锚点 1/9 落在 0.271 的 CI 内（第一版据此打印「✓ 框一致，可继续」），
    但收割段 3/21 远低于承诺 0.696。
    判定必须是 selector_falsified —— **绝不能**因为锚点那侧没报警就当通过。
    """
    db = _make_db(tmp_path, _rows(9, 1, 21, 3))
    res = AC.analyze("hX", expect=0.271, expect_harvest=0.696,
                     db_path=db, expectations_path=tmp_path / "none.json")
    lo, hi = res["strata"]["ANCHOR"]["ci"]
    assert lo <= 0.271 <= hi, "前提：锚点那侧确实落在 CI 内（否则测不到这个 bug）"
    assert res["verdict"] == "selector_falsified"


def test_selector_not_falsified_when_harvest_delivers(tmp_path):
    """收割段打出 15/21 —— 与承诺 0.696 相容，不得误报推翻。"""
    db = _make_db(tmp_path, _rows(9, 2, 21, 15))
    res = AC.analyze("hX", expect=0.271, expect_harvest=0.696,
                     db_path=db, expectations_path=tmp_path / "none.json")
    assert res["verdict"] != "selector_falsified"
    assert res["p_harvest"] > 0.05


def test_no_lift_flagged_when_harvest_below_anchor(tmp_path):
    """收割率不高于锚点 = 本批没有任何可证实的提升。"""
    db = _make_db(tmp_path, _rows(10, 4, 20, 6))     # 锚点 0.40 > 收割 0.30
    res = AC.analyze("hX", db_path=db, expectations_path=tmp_path / "none.json")
    assert res["verdict"] == "no_lift"
    assert res["lift"] < 1.0


def test_expectations_json_is_used_when_flags_omitted(tmp_path):
    """建批承诺落盘后，判完不传参数也能做主检验（h30 只能靠手工重建的教训）。"""
    db = _make_db(tmp_path, _rows(9, 1, 21, 3))
    expf = tmp_path / "exp.json"
    expf.write_text(json.dumps({"hX": {"pool_base_rate": 0.271, "harvest_rate": 0.696}}),
                    encoding="utf-8")
    res = AC.analyze("hX", db_path=db, expectations_path=expf)
    assert res["expect"] == pytest.approx(0.271)
    assert res["expect_harvest"] == pytest.approx(0.696)
    assert res["verdict"] == "selector_falsified"


def test_missing_expectations_json_is_not_fatal(tmp_path):
    """没有承诺文件时只报实测、明说判不了 —— 不得崩，也不得假装通过。"""
    db = _make_db(tmp_path, _rows(9, 3, 21, 15))
    res = AC.analyze("hX", db_path=db, expectations_path=tmp_path / "nope.json")
    assert res["verdict"] == "no_expect"
    assert res["expect_harvest"] is None


# ── ⑤ 框漂移（低功效，仅排除灾难性漂移）────────────────────
def test_verdict_consistent_when_expect_inside_ci(tmp_path):
    db = _make_db(tmp_path, _rows(30, 6, 10, 8))     # ANCHOR 率 0.20
    res = AC.analyze("hX", expect=0.271, db_path=db, expectations_path=tmp_path / "none.json")
    assert res["verdict"] == "consistent"


def test_verdict_frame_drift_when_expect_outside_ci(tmp_path):
    """s30 的形态：外推 0.9 而实测只有 0.2 —— 必须判成漂移。"""
    db = _make_db(tmp_path, _rows(30, 6, 10, 8))
    res = AC.analyze("hX", expect=0.9, db_path=db, expectations_path=tmp_path / "none.json")
    assert res["verdict"] == "frame_drift"


def test_verdict_no_anchor_when_stratum_absent(tmp_path):
    """没有锚点段就明说判不了 —— 不能拿收割段的率冒充池基准率。"""
    db = _make_db(tmp_path, _rows(0, 0, 12, 9))
    res = AC.analyze("hX", expect=0.271, db_path=db, expectations_path=tmp_path / "none.json")
    assert res["verdict"] == "no_anchor"


def test_batch_isolation(tmp_path):
    """只统计本批：别的批次的题不得混进来。"""
    rows = _rows(4, 1, 4, 1)
    rows.append({"cid": "CND-other", "verdict": "candidate", "stratum": "ANCHOR",
                 "p": 0.04, "batch": "hOTH"})
    db = _make_db(tmp_path, rows)
    res = AC.analyze("hX", db_path=db, expectations_path=tmp_path / "none.json")
    assert res["strata"]["ANCHOR"]["n"] == 4


def test_binom_tail_matches_known_values():
    assert AC._binom_tail_le(3, 21, 0.696) == pytest.approx(2.36e-07, rel=0.05)
    assert AC._binom_tail_le(15, 21, 0.696) > 0.05
    assert AC._binom_tail_le(5, 5, 1.0) == pytest.approx(1.0)
