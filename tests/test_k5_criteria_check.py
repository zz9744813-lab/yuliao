"""K5 判据核验器回归（scripts/k5_criteria_check.py，全离线假夹具）。

两条红线（任务书钉死）：
1. **判据缺证据必须报未满足**——缺收据 / 收据缺件，任何判据都不许
   静默判 satisfied；
2. **禁止把 dry-run 说成已通**——即使全部机械判据满足（伪造的完美收据），
   报告的 mode=dry_run、k5_established 恒 False、verdict 恒含「未通」。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k5c", ROOT / "scripts" / "k5_criteria_check.py")
k5c = _u.module_from_spec(_spec)
sys.modules["k5c"] = k5c
_spec.loader.exec_module(k5c)


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _perfect_artifact():
    """全部机械判据可过的假收据（离线夹具；不代表任何真跑）。"""
    return {
        "live": True, "channel_changed": True,
        "artifacts": {
            "prose": [{"scene": f"s{i}", "arm": arm, "status": "committed",
                       "text": "x"}
                      for i in (1, 2, 3) for arm in ("A", "B")],
            "packages": [{"scene": f"s{i}", "n_techniques": 2}
                         for i in (1, 2, 3)],
            "failures": [], "skipped": [],
            "receipts": [{"usage": {"tokens": 1000, "calls": 2}}
                         for _ in range(6)],
        },
    }


def test_missing_artifact_reports_all_unsatisfied(tmp_path):
    """红线1：判据缺证据必须报未满足——收据不存在 ⇒ 全部判据
    satisfied=False 且逐条带 missing 证据说明，无一条静默通过。"""
    report = k5c.build_report(tmp_path / "nope.json")
    assert report["mode"] == "dry_run"
    assert report["n_criteria"] >= 10
    for c in report["criteria"]:
        assert c["satisfied"] is False, c["id"]
        assert c["missing"], f"{c['id']} 缺证据时必须给 missing 说明"
    assert report["k5_established"] is False


def test_incomplete_artifact_reports_unsatisfied(tmp_path):
    """红线1（缺件形态）：收据存在但不完整（部分 committed / 有失败臂 /
    空包 / 未标 channel_changed）⇒ 对应判据逐条未满足且证据行如实。"""
    art = _perfect_artifact()
    art["artifacts"]["prose"] = art["artifacts"]["prose"][:2]   # 仅 2/6 committed
    art["artifacts"]["failures"] = [{"scene": "s2", "arm": "A",
                                     "error_type": "RuntimeFault",
                                     "error": "call_budget_exhausted"}]
    art["artifacts"]["packages"] = [{"scene": f"s{i}", "n_techniques": 0}
                                    for i in (1, 2, 3)]        # 空包
    art["channel_changed"] = False                            # 未标
    report = k5c.build_report(_write(tmp_path, "bad.json", art))
    by = {c["id"]: c for c in report["criteria"]}
    assert by["P1"]["satisfied"] is False
    assert any("committed" in e for e in by["P1"]["evidence"])
    assert by["C2"]["satisfied"] is False
    assert by["P2"]["satisfied"] is False and by["C3"]["satisfied"] is False
    assert by["C5"]["satisfied"] is False
    assert "A6" in "".join(by["C5"]["missing"]), "未标须引用 A6 写死口径"


def test_dry_run_never_claims_established(tmp_path):
    """红线2：禁止把 dry-run 说成已通——**完美收据**（机械判据全过）下
    k5_established 仍 False、mode 仍 dry_run、verdict 仍含「未通」；
    C1/C4 人工证据结构性缺席是设计使然。"""
    report = k5c.build_report(_write(tmp_path, "perfect.json",
                                    _perfect_artifact()))
    by = {c["id"]: c for c in report["criteria"]}
    mech_true = [cid for cid, c in by.items() if c["satisfied"] is True]
    assert {"P0", "P1", "P2", "C2", "C3", "C5"} <= set(mech_true), \
        "完美夹具应让机械判据通过（否则夹具错了）"
    assert by["C1"]["satisfied"] is False and by["C1"]["missing"]
    assert by["C4"]["satisfied"] is False and by["C4"]["missing"]
    assert report["k5_established"] is False, \
        "dry-run/只读核验永远不构成 K5 通过"
    assert report["mode"] == "dry_run"
    assert "未通" in report["verdict"]
    assert report["n_satisfied"] < report["n_criteria"]


def test_latest_artifact_selection(tmp_path):
    """收据发现口径：out_k4_3*/k4_paired.json 按 mtime 取最新（离线
    假文件核序，不依赖真仓）。"""
    d1 = tmp_path / "out_k4_3_a"; d1.mkdir()
    d2 = tmp_path / "out_k4_3_b"; d2.mkdir()
    f1 = d1 / "k4_paired.json"; f1.write_text("{}", encoding="utf-8")
    f2 = d2 / "k4_paired.json"; f2.write_text("{}", encoding="utf-8")
    old = time.time() - 1000
    import os
    os.utime(f1, (old, old))                    # f1 旧
    assert k5c.latest_k4_artifact(tmp_path) == f2
    assert k5c.latest_k4_artifact(tmp_path / "absent") is None


def test_stop_loss_line_honored(tmp_path):
    """止损口径：收据 tokens 合计越线 ⇒ C4+ 未满足并停止扩张提示。"""
    art = _perfect_artifact()
    art["artifacts"]["receipts"] = [
        {"usage": {"tokens": 5_000_000}} for _ in range(6)]   # 3,000 万
    report = k5c.build_report(_write(tmp_path, "over.json", art))
    c4p = [c for c in report["criteria"] if c["id"] == "C4+"][0]
    assert c4p["satisfied"] is False
    assert any("越线" in m for m in c4p["missing"])
