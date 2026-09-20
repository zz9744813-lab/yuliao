"""bal-v2 长度方向重生成管线的**规格测试**（先于实现，2026-09-20）。

规格来源：docs/proposal-length-balanced-regen-20260920.md §1/§1.5 + 监督方指令。
**按规格写死，不许跟着实现走**：
· 窗口是闭区间：恰在 0.60/0.92/1.08/1.80 → 接受；两端 ±0.01 之外 → 拒收；
· L 窗上界必须 < S 窗下界（防放宽后 0.98~1.02 重叠复发）；
· 6 压缩型强制 L 窗 + prompt 压缩指令；膨胀型与控制臂保持 any；
· judge_verify 越窗 → len_window 原因 → status=rejected_length；
· 503 与 failed_parse 必须可分类，批次验收"503=0、非 ok 只许 failed_parse"；
· final_report 档一读数「先最新 split 再集内最高」三边界：同 created_at
  按 id tiebreak / 只有旧集读旧集 / 新集无跑分不顶替（半成品不改口径）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import controlled_corruption as CC  # noqa: E402
import preflight_models as PF      # noqa: E402
import final_report as FR         # noqa: E402

# ── 1. 窗口规格本体（写死数字，改规格必须红这里）──────────────

def test_window_values_pinned():
    assert CC.LEN_WINDOW_L == (0.60, 0.92), "L 窗（variant 更短）= 闭区间 [0.60, 0.92]"
    assert CC.LEN_WINDOW_S == (1.08, 1.80), "S 窗（variant 更长）= 闭区间 [1.08, 1.80]"


def test_l_upper_strictly_below_s_lower():
    """硬约束：L 上界 < S 下界。放宽窗口出现 0.98~1.02 重叠时这里必须红。"""
    assert CC.LEN_WINDOW_L[1] < CC.LEN_WINDOW_S[0]


@pytest.mark.parametrize("ratio,expected", [
    # L 窗 8 边界用例的 L 半（闭区间：端点接受、±0.01 外拒收）
    (0.60, True), (0.59, False),
    (0.92, True), (0.93, False),
    # S 窗的另一半
    (1.08, True), (1.07, False),
    (1.80, True), (1.81, False),
])
def test_window_boundary_eight_cases(ratio, expected):
    win = CC.LEN_WINDOW_L if ratio < 1.0 else CC.LEN_WINDOW_S
    assert CC.window_accepts(ratio, win) is expected


def test_window_accepts_none_window_is_any():
    assert CC.window_accepts(1.0, None) is True   # any 窗不约束


# ── 2. 类型分派 ─────────────────────────────────────────────

COMPRESSION = ("SUBTEXT_ERASE", "ABSTRACT_SUMMARY", "RHYTHM_FLATTEN",
               "LITERARY_OVERWRITE", "EMOTION_LABEL", "DIALOGUE_EXPOSITION")


def test_six_compression_types_get_l_window():
    for t in COMPRESSION:
        assert CC.window_for(t) == CC.LEN_WINDOW_L, f"{t} 必须强制 L 窗"


def test_inflation_and_control_stay_any():
    for t in ("EXPLICITIZE", "ADJECTIVE_INFLATION", "REDUNDANCY",
              "NEUTRAL_PARAPHRASE"):
        assert CC.window_for(t) is None, f"{t} 保持 any（控制臂与膨胀型不进窗）"


# ── 3. prompt 压缩指令（只给压缩型；病句硬拒不许放松）──────

def test_len_directive_only_for_compression():
    d = CC.len_directive_for("SUBTEXT_ERASE")
    assert "60%" in d and "90%" in d, "压缩型 prompt 必须明写 60%~90%"
    assert CC.len_directive_for("EXPLICITIZE") == "", "膨胀型不加长度指令"
    assert CC.len_directive_for("NEUTRAL_PARAPHRASE") == "", "控制臂不加长度指令"


# ── 4. judge_verify 并窗 + 状态映射 ────────────────────────

def _v(ok=True):
    return {"contradicts_source": not ok, "ungrammatical": False, "drift": 0.1}


def test_judge_verify_in_window_accepts():
    ok, _drift, why = CC.judge_verify(_v(), 0.75, window=CC.LEN_WINDOW_L)
    assert ok and why == ""


def test_judge_verify_out_of_window_rejects_with_len_reason():
    ok, _drift, why = CC.judge_verify(_v(), 0.95, window=CC.LEN_WINDOW_L)
    assert not ok and why.startswith("len_window"), \
        "越窗拒收必须带 len_window 前缀——状态映射靠它分诊 rejected_length"


def test_judge_verify_none_window_keeps_global_bounds():
    ok, _d, why = CC.judge_verify(_v(), 1.0, window=None)
    assert ok, "any 窗内（全局 0.55~1.8）照常放行"


def test_reject_status_mapping():
    assert CC.reject_status("len_window=0.95") == "rejected_length"
    assert CC.reject_status("ungrammatical") == "rejected_drift"
    assert CC.reject_status("drift=0.90") == "rejected_drift"


def test_judge_verify_grammatical_hard_reject_unchanged():
    """病句硬拒不受窗口改动影响（规格红线：长度约束不得污染语法质量）。"""
    ok, _d, why = CC.judge_verify({"contradicts_source": False,
                                   "ungrammatical": True, "drift": 0.1},
                                  0.75, window=CC.LEN_WINDOW_L)
    assert not ok and why == "ungrammatical"


# ── 5. 503 vs failed_parse 分类（批次验收先决判据）──────────

def test_classify_llm_failure_503_family():
    assert PF.classify_llm_failure(
        "HTTP 503 model_not_found: No available channel for model deepseek/deepseek-v4.1-flash"
    ) == "503"
    assert PF.classify_llm_failure("model_not_found: 无可用渠道") == "503"


def test_classify_llm_failure_failed_parse_family():
    assert PF.classify_llm_failure("failed_parse: JSON 解析失败") == "failed_parse"
    assert PF.classify_llm_failure("") == "failed_parse"


def test_classify_llm_failure_other():
    assert PF.classify_llm_failure("timeout: read deadline exceeded") == "other"


class _Row:
    def __init__(self, status, error):
        self.status, self.error = status, error


def test_batch_llm_health_acceptance_criterion():
    ok, msg = PF.batch_llm_health([_Row("ok", ""), _Row("failed_parse", "x")])
    assert ok, "非 ok 只许 failed_parse——failed_parse 不破坏验收"
    ok, msg = PF.batch_llm_health([_Row("ok", ""), _Row("failed", "HTTP 503 ...")])
    assert not ok and "503" in msg, "任何一条 503 → 批次不成立"
    ok, msg = PF.batch_llm_health([_Row("ok", "")])
    assert ok


# ── 6. final_report 档一读数三边界 ──────────────────────────

def _mkdb(sets_and_runs):
    """临时库：(set_id, name, created_at, [(model, n, correct, acc)])。"""
    import os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)          # Windows 不关句柄会阻止 teardown 的 unlink
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript("""
    create table benchmark_sets (id text primary key, name text,
                                 kind text, created_at text, n_items int);
    create table benchmark_runs (id text, set_id text, model text,
                                 n int, n_correct int, accuracy real);
    """)
    for sid, name, created, runs in sets_and_runs:
        con.execute("insert into benchmark_sets values (?,?,?,?,0)",
                    (sid, name, "length_balanced", created))
        for i, (model, n, correct, acc) in enumerate(runs):
            con.execute("insert into benchmark_runs values (?,?,?,?,?,?)",
                        (f"{sid}-{i}", sid, model, n, correct, acc))
    con.commit()
    return con, path


def test_gate_bal_reading_same_created_at_tiebreaks_by_id():
    """同秒建集：id 大的（新）胜——确定性 tiebreak。"""
    con, path = _mkdb([
        ("BS-old", "bal-v1", "2026-09-20T10:00:00", [("m-old", 38, 28, 0.737)]),
        ("BS-new-a", "bal-v2a", "2026-09-20T11:00:00", [("m-a", 40, 30, 0.750)]),
        ("BS-new-b", "bal-v2b", "2026-09-20T11:00:00", [("m-b", 40, 32, 0.800)]),
    ])
    try:
        row = FR.gate_bal_reading(con)
        assert row["set_name"] == "bal-v2b" and row["accuracy"] == 0.800
    finally:
        con.close()
        Path(path).unlink(missing_ok=True)


def test_gate_bal_reading_only_old_set_reads_old():
    con, path = _mkdb([("BS-old", "bal-v1", "2026-09-20T10:00:00",
                         [("qoder/Qwen3.8-Flash", 38, 28, 0.737)])])
    try:
        row = FR.gate_bal_reading(con)
        assert row is not None and row["set_name"] == "bal-v1"
    finally:
        con.close()
        Path(path).unlink(missing_ok=True)


def test_gate_bal_reading_half_done_set_does_not_replace():
    """新集存在但没有任何跑分 → 不顶替旧集（半成品不得静默成为档一口径）。"""
    con, path = _mkdb([
        ("BS-old", "bal-v1", "2026-09-20T10:00:00", [("m-old", 38, 28, 0.737)]),
        ("BS-new", "bal-v2", "2026-09-20T11:00:00", []),   # 无跑分
    ])
    try:
        row = FR.gate_bal_reading(con)
        assert row is not None and row["set_name"] == "bal-v1", \
            "无跑分的新集必须被跳过，档一读数保持旧集"
    finally:
        con.close()
        Path(path).unlink(missing_ok=True)


def test_gate_bal_reading_empty_db_returns_none():
    con, path = _mkdb([])
    try:
        assert FR.gate_bal_reading(con) is None
    finally:
        con.close()
        Path(path).unlink(missing_ok=True)
