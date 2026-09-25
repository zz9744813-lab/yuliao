"""K5 晋升探针·账本 schema 回归（派工 fix/probe-ledger-schema，全离线）。

被测缺陷：探针把 scripts/k2_pairs_gen.py 的**写手调用遥测**账本
（行键 event/op/segment_id/writer_model/n_chars_ai/ts，无 gates_ok）
当成门账本数 ⇒ n_gates_ok 恒 0 ⇒ A4 被「读错文件」判死。

红线逐条：
(a) 喂遥测账本 → ledger.kind=="writer_telemetry"，门统计保持 null（不猜），
    missing 说「探针未找到门账本」而**不得**出现「过门数=0」式确定性断言；
(b) 喂合成门账本（gates_ok:true 若干行）→ n_gates_ok/n_written 等于造的
    真值；
(c) 两种账本同时在场 → 认门账本做统计、遥测账本如实标注不参与；
(d) 门账本路径可 --gate-ledger 指定；找不到 → n_gates_ok:null + error；
(e) 既有硬约束不变：只读、model_calls=0、git_writes=0、
    no_status_change_advice=true。

反向验证锚（任务书硬性要求）：把 _classify_ledger_rows 的
「if n_gate:」改成「if rows:」（只要有行就算门账本）的退化版，
test_writer_telemetry_* / test_mixed_* 立即红；恢复后转绿。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k5pl", ROOT / "scripts" / "k5_promotion_wire_probe.py")
k5pl = _u.module_from_spec(_spec)
sys.modules["k5pl"] = k5pl
_spec.loader.exec_module(k5pl)

MISSING_DB_NAME = "no_such_dir"      # 库不可读 → 过门对数走「不可核」文案


# ---------------------------------------------------------------- 夹具
def _tele_row(i):
    """写手遥测行（现场读取的 k2pairs_ledger.jsonl 实际键形，不臆造）。"""
    if i % 2:
        return {"event": "writer_call", "op": "add_interpretation",
                "segment_id": f"seg{i}", "writer_model": "mc22-flash",
                "n_chars_ai": 200 + i, "ts": "2026-09-24T09:3x:00"}
    return {"event": "cache_hit", "op": "split_beats",
            "segment_id": f"seg{i}", "cache": f"C:/cache/{i}.json"}


def _gate_row(i, *, gates_ok=True, outcome=None):
    """门账本行 = k2_contrast_extract.ledger_entry() 的完整键集。"""
    return {
        "pair_id": f"k2pair-{i:04d}", "protocol": "paired_contrast_v2",
        "strategy_key": "v2:解释腔", "op": "add_interpretation",
        "op_label": "加解释", "scene_keys": ["战斗"], "span_start": 0,
        "span_end": 6, "human_text": "人类侧", "human_sha256": f"h{i}",
        "ai_text": "AI侧", "ai_sha256": f"a{i}", "ai_side_chars": 3,
        "gates_ok": gates_ok,
        "gate_results": {"G1_形制": "pass", "G2_长度比": "pass"},
        "reject_reasons": [] if gates_ok else ["长度比超界"],
        "persist_outcome": outcome if outcome is not None
        else ("written" if gates_ok else "gated_out"),
    }


def _jsonl(tmp_path: Path, name: str, rows: list) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    return p


def _pairs48(tmp_path: Path) -> Path:
    pf = tmp_path / "_pairs" / "k2_pairs_20260924.json"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"pairs": [{"op": f"op{i}"}
                                        for i in range(48)]}),
                  encoding="utf-8")
    return pf


def _a4(tmp_path: Path, ledger: Path, gate_ledger: Path | None = None):
    return k5pl.a4_feasibility(tmp_path / MISSING_DB_NAME / "x.db",
                               tmp_path, _pairs48(tmp_path), ledger,
                               gate_ledger)


# ---------------------------------------------------------------- (a) 遥测账本
def test_writer_telemetry_ledger_recognised_and_not_counted(tmp_path):
    """(a) 喂遥测账本：kind 如实、门统计 null、missing 只说「未找到门账本」。

    反向验证锚：若分类器退化成「只要有行就算门账本」，本用例 kind 变
    "gate"、n_gates_ok 变 0 —— 立即红。
    """
    tel = _jsonl(tmp_path, "k2pairs_ledger.jsonl", [_tele_row(i)
                                                    for i in range(96)])
    a4 = _a4(tmp_path, tel)
    led = a4["ledger"]
    assert led["kind"] == "writer_telemetry"
    assert led["n_rows"] == 96                       # 行数照实报
    assert led["n_gates_ok"] is None                 # 但不参与门统计
    assert led["n_written"] is None
    assert "event" in led["schema_keys"]
    assert "gates_ok" not in led["schema_keys"]
    assert "遥测" in (led["note"] or "")
    assert a4["a4_feasible_now"] is False
    # missing 必须指认「探针未找到门账本」，不许断言「过门数=0」
    assert any("未找到门账本" in m and "不猜" in m for m in a4["missing"])
    for m in a4["missing"]:
        if ("过门" in m or "落库行数" in m) and "不可核" not in m:
            assert "=0" not in m, m


def test_gate_ledger_missing_is_null_not_zero(tmp_path):
    """(d) 找不到门账本：n_gates_ok=null + error 文案，沿用不猜纪律。"""
    a4 = _a4(tmp_path, tmp_path / "absent_ledger.jsonl")
    led = a4["ledger"]
    assert led["kind"] == "unknown"
    assert led["n_gates_ok"] is None and led["n_rows"] is None
    assert "不猜" in (led["error"] or "")
    assert any("门账本不可核" in m for m in a4["missing"])
    assert a4["a4_feasible_now"] is False


# ---------------------------------------------------------------- (b) 门账本
def test_synthetic_gate_ledger_counts_truth(tmp_path):
    """(b) 合成门账本 5 行（gates_ok:true×3，其中 written×2）→ 读数=真值。"""
    gate = _jsonl(tmp_path, "k2_pairs.jsonl",
                  [_gate_row(0), _gate_row(1),
                   _gate_row(2, outcome="skipped_duplicate"),
                   _gate_row(3, gates_ok=False),
                   _gate_row(4, gates_ok=False)])
    a4 = _a4(tmp_path, gate)
    led = a4["ledger"]
    assert led["kind"] == "gate"
    assert led["n_rows"] == 5
    assert led["n_gates_ok"] == 3
    assert led["n_written"] == 2
    assert not any("门账本" in m for m in a4["missing"])   # 门账本在场不报缺


# ---------------------------------------------------------------- (c) 双账本
def test_probe_reads_both_ledgers_gate_wins_for_stats(tmp_path):
    """同时认两种账本：遥测占坑而门账本经 gate_ledger_file 给入 →
    统计取门账本真值，遥测在 all_ledgers 里如实标注不参与。"""
    tel = _jsonl(tmp_path, "k2pairs_ledger.jsonl", [_tele_row(i)
                                                    for i in range(12)])
    gate = _jsonl(tmp_path, "k2_pairs.jsonl", [_gate_row(i) for i in range(3)]
                  + [_gate_row(9, gates_ok=False)])
    a4 = _a4(tmp_path, tel, gate_ledger=gate)
    assert a4["ledger"]["kind"] == "gate"
    assert a4["ledger"]["n_gates_ok"] == 3
    assert a4["ledger"]["n_written"] == 3
    kinds = {l["kind"] for l in a4["all_ledgers"]}
    assert kinds == {"gate", "writer_telemetry"}
    tele_view = next(l for l in a4["all_ledgers"]
                     if l["kind"] == "writer_telemetry")
    assert tele_view["n_gates_ok"] is None           # 遥测永不产门计数


def test_mixed_rows_classified_as_gate_not_diluted(tmp_path):
    """同档混有门行与遥测行：判 gate，且 n_gates_ok 只数真过门行
    （反向验证锚之二：「只要有行就算门账本」的退化版在纯遥测档上即红）。"""
    mixed = _jsonl(tmp_path, "mixed.jsonl",
                   [_gate_row(0), _tele_row(1), _tele_row(3),
                    _gate_row(4, gates_ok=False)])
    led = k5pl._parse_ledger(mixed)
    assert led["kind"] == "gate"
    assert led["n_gates_ok"] == 1 and led["n_written"] == 1
    assert k5pl._classify_ledger_rows([]) == "unknown"


# ---------------------------------------------------------------- (e) 端到端
def test_cli_gate_ledger_param_and_discipline(tmp_path, capsys):
    """--gate-ledger 走通 CLI；只读性：跑完 tmp 树零新增文件；
    硬约束原样：model_calls=0、git_writes=0、no_status_change_advice=true。"""
    tel = _jsonl(tmp_path, "tel.jsonl", [_tele_row(i) for i in range(6)])
    gate = _jsonl(tmp_path, "gate.jsonl", [_gate_row(i) for i in range(2)]
                  + [_gate_row(8, gates_ok=False)])
    pf = _pairs48(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix()
                    for p in tmp_path.rglob("*"))
    argv_backup = sys.argv
    sys.argv = ["k5_promotion_wire_probe.py", "--repo-root", str(tmp_path),
                "--ledger", str(tel), "--gate-ledger", str(gate),
                "--pairs-file", str(pf), "--out", ""]
    try:
        code = k5pl.main()
    finally:
        sys.argv = argv_backup
    assert code == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["a4"]["ledger"]["kind"] == "gate"
    assert rep["a4"]["ledger"]["n_gates_ok"] == 2
    assert {l["kind"] for l in rep["a4"]["all_ledgers"]} == {
        "gate", "writer_telemetry"}
    assert rep["a4"]["advice_status_change"].startswith("none")
    d = rep["discipline"]
    assert d["model_calls"] == 0 and d["git_writes"] == 0
    assert d["db_mode"] == "ro" and d["no_status_change_advice"] is True
    after = sorted(p.relative_to(tmp_path).as_posix()
                   for p in tmp_path.rglob("*"))
    assert after == before                           # 唯一允许的写=--out


# ---------------------------------------------------------------- 既有契约
def test_a4_key_contract_superset(tmp_path):
    """旧键契约不破（a4 二级键）+ 新键 kind/schema_keys/all_ledgers 在位。"""
    gate = _jsonl(tmp_path, "k2_pairs.jsonl", [_gate_row(0)])
    a4 = _a4(tmp_path, gate)
    assert {"pairs_file", "ledger", "db", "thresholds",
            "a4_feasible_now", "missing", "advice_status_change"} <= set(a4)
    assert "all_ledgers" in a4
    assert {"kind", "schema_keys"} <= set(a4["ledger"])
    json.dumps(a4, ensure_ascii=False)               # 全 JSON 可序列化
    # db 读数仍走真 SQL：无库 → None + 「不可核」文案（不猜纪律不变）
    assert a4["db"]["strategy_instances_paired_rows"] is None
    assert any("过门对在库行数不可核" in m for m in a4["missing"])
