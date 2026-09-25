"""K5 判据核验器回归（scripts/k5_criteria_check.py，全离线假夹具）。

两条红线（任务书钉死）：
1. **判据缺证据必须报未满足**——缺收据 / 收据缺件，任何判据都不许
   静默判 satisfied；
2. **禁止把 dry-run 说成已通**——即使全部机械判据满足（伪造的完美收据），
   报告的 mode=dry_run、k5_established 恒 False、verdict 恒含「未通」。

2026-09-25 遗留整改另钉两组：
- 遗留 A（会审第 7 条）：P1 必须**逐场**校验「三场每场 A/B 各 committed」，
  不得只看 `len(committed)==6` 总数；
- 遗留 B（会审第 8 条）：报告不得内嵌机器绝对路径、`generated_at` 带时区、
  `--repo-root` 缺省由脚本自身推导、报告落位不再进仓根。
"""
from __future__ import annotations

import importlib.util as _u
import json
import re
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

# 报告文本里不该出现的形态：盘符级绝对路径；该出现的：带偏移的时间戳
_ABS_RE = re.compile(r"[A-Za-z]:[\\/]")
_TZ_RE = re.compile(r"[+-]\d{2}:\d{2}$")


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
            "receipts": [{"usage": {"tokens": 1000, "calls": 2},
                          "models": {"writer": "deepseek-v4.1-flash"}}
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
                                    _perfect_artifact()), True)
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

def test_stop_loss_counts_failed_arm_world_tokens(tmp_path):
    """会审 2026-09-25 双席一致指出的真缺陷：收据只覆盖 committed 臂，
    失败臂实耗只在世界库。止损若只累加 receipts 会系统性少计（12,810
    被报成 2,167，止损线形同虚设）。本钉子：世界库可读时取两者较大者，
    并在证据行同时披露两套口径。"""
    import sqlite3
    wd = tmp_path / "worlds"
    for arm in ("arm1", "arm2"):
        d = wd / arm
        d.mkdir(parents=True)
        con = sqlite3.connect(str(d / "k4.sqlite"))
        con.execute("create table calls (job text, response text)")
        for _ in range(6):                      # 失败臂 6 次调用
            con.execute("insert into calls values (?,?)",
                        ("j", json.dumps({"tokens_in": 500, "tokens_out": 100})))
        con.commit(); con.close()
    art = _perfect_artifact()
    art["worlds_dir"] = str(wd)
    art["artifacts"]["receipts"] = [{"usage": {"tokens": 100, "calls": 2}}
                                    for _ in range(2)]      # 收据只 200
    report = k5c.build_report(_write(tmp_path, "worlds.json", art))
    c4p = [c for c in report["criteria"] if c["id"] == "C4+"][0]
    ev = " ".join(c4p["evidence"])
    assert "收据口径 tokens=200" in ev, ev
    assert "世界库" in ev and "7200" in ev, ev          # 12 次 × 600
    assert c4p["satisfied"] is True


def test_world_db_unreadable_discloses_not_silent(tmp_path):
    """世界库不可读 ⇒ 不得静默当 0：证据行必须如实披露降级原因。"""
    art = _perfect_artifact()
    art["worlds_dir"] = str(tmp_path / "absent_dir")
    report = k5c.build_report(_write(tmp_path, "nodb.json", art))
    c4p = [c for c in report["criteria"] if c["id"] == "C4+"][0]
    assert any("不可核" in e or "不可读" in e for e in c4p["evidence"]), c4p["evidence"]


def test_c5_not_satisfied_without_ten_scene_receipts(tmp_path):
    """会审指出：C5 是「10 场与三场同通道」的一致性判据；只有三场收据
    （10 场收据缺位）时不得判 satisfied=True。证据键 models 缺位同理。"""
    art = _perfect_artifact()
    for r in art["artifacts"]["receipts"]:
        r["models"] = {"writer": "deepseek-v4.1-flash"}
    p3 = _write(tmp_path, "three.json", art)
    by3 = {c["id"]: c for c in k5c.build_report(p3)["criteria"]}
    assert by3["C5"]["satisfied"] is False, "缺 10 场收据时 C5 不得判过"
    assert any("10 场收据缺位" in m for m in by3["C5"]["missing"]), by3["C5"]
    by3b = {c["id"]: c for c in k5c.build_report(p3, True)["criteria"]}
    assert by3b["C5"]["satisfied"] is True, "三场标了 channel_changed + 10 场在位应判过"
    # models 键缺位 ⇒ 缺证据不许判过
    art2 = _perfect_artifact()
    for r in art2["artifacts"]["receipts"]:
        r.pop("models", None)
    by3c = {c["id"]: c for c in k5c.build_report(
        _write(tmp_path, "nomodels.json", art2), True)["criteria"]}
    assert by3c["C5"]["satisfied"] is False
    assert any("证据键缺位" in m for m in by3c["C5"]["missing"]), by3c["C5"]


def test_ten_scene_dry_run_timeout_and_live_guard(tmp_path, monkeypatch):
    """十场 dry-run 失败形态必须落成 ok=False（不裸异常）；且子进程若报
    live≠False 必须中止（不许只看 returncode 就当离线跑通）。"""
    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    # 超时分支
    def _boom(*a, **k):
        raise k5c.subprocess.TimeoutExpired(cmd="x", timeout=600)
    monkeypatch.setattr(k5c.subprocess, "run", _boom)
    r = k5c.ten_scene_dry_run(tmp_path, "py")
    assert r["ok"] is False and "超时" in r["error"]

    # 子进程声称 live=True ⇒ 中止
    def _live(*a, **k):
        return _R()
    monkeypatch.setattr(k5c.subprocess, "run", _live)
    monkeypatch.setattr(k5c, "_load_json", lambda p: {"live": True, "artifacts": {}})
    monkeypatch.setattr(k5c.Path, "exists", lambda self: True)
    r2 = k5c.ten_scene_dry_run(tmp_path, "py")
    assert r2["ok"] is False and "live" in r2["error"]


def test_ten_scene_dry_run_clears_live_gate_env(tmp_path, monkeypatch):
    """调用方 shell 残留 K4_ALLOW_LIVE=1 不得传进子进程（显式清闸）。"""
    seen = {}

    class _R:
        returncode = 1
        stdout = ""
        stderr = "stop"

    def _cap(cmd, **k):
        seen.update(k.get("env") or {})
        return _R()
    monkeypatch.setenv("K4_ALLOW_LIVE", "1")
    monkeypatch.setenv("LG_LIVE", "1")
    monkeypatch.setattr(k5c.subprocess, "run", _cap)
    k5c.ten_scene_dry_run(tmp_path, "py")
    assert "K4_ALLOW_LIVE" not in seen and "LG_LIVE" not in seen
    assert "LG_DATABASE_URL" in seen


# ─── 遗留 A（会审第 7 条）：P1 逐场分布校验 ───────────────────────────
def test_p1_requires_two_arms_in_every_scene(tmp_path):
    """P1 不得只看总数 6：① 6 处 committed 集中在一场 ⇒ False；
    ② 三场各 2/2 ⇒ True；③ 4 场 8 处（10 场收据被 out_k4_3* 误匹配）
    ⇒ 总数达标也不判过。"""
    # ① 6 处全在 s1（A/B 各重复三次）——旧口径 len==6 会误判满足
    art = _perfect_artifact()
    art["artifacts"]["prose"] = [
        {"scene": "s1", "arm": arm, "status": "committed", "text": "x"}
        for arm in ("A", "A", "A", "B", "B", "B")]
    by = {c["id"]: c for c in k5c.build_report(
        _write(tmp_path, "one_scene.json", art))["criteria"]}
    ev = " ".join(by["P1"]["evidence"])
    assert by["P1"]["satisfied"] is False, by["P1"]
    assert "per_scene={'s1': 6, 's2': 0, 's3': 0}" in ev, ev
    assert any("s2" in m and "s3" in m for m in by["P1"]["missing"]), \
        by["P1"]["missing"]

    # ② 三场各 2/2（其余前提满足）⇒ P1 满足，且不留 missing 话术
    by2 = {c["id"]: c for c in k5c.build_report(
        _write(tmp_path, "each2.json", _perfect_artifact()))["criteria"]}
    assert by2["P1"]["satisfied"] is True, by2["P1"]
    assert by2["P1"]["missing"] == []
    assert "per_scene={'s1': 2, 's2': 2, 's3': 2}" in " ".join(
        by2["P1"]["evidence"]), by2["P1"]["evidence"]

    # ③ 4 场 8 处 committed ⇒ 超出三场口径，不得判过
    art3 = _perfect_artifact()
    art3["artifacts"]["prose"] = [
        {"scene": f"s{i}", "arm": arm, "status": "committed", "text": "x"}
        for i in (1, 2, 3, 4) for arm in ("A", "B")]
    by3 = {c["id"]: c for c in k5c.build_report(
        _write(tmp_path, "four_scenes.json", art3))["criteria"]}
    assert by3["P1"]["satisfied"] is False, by3["P1"]
    assert any("口径外" in m and "s4" in m for m in by3["P1"]["missing"]), \
        by3["P1"]["missing"]


def test_p1_names_the_missing_arm(tmp_path):
    """三场齐但 s3 只有 A 臂 ⇒ 每场 2/2 不成立，且 missing 要指名缺哪一臂
    （旧口径只报「5/6≠6」，说不出谁没交）。"""
    art = _perfect_artifact()
    art["artifacts"]["prose"] = [p for p in art["artifacts"]["prose"]
                                 if not (p["scene"] == "s3" and p["arm"] == "B")]
    by = {c["id"]: c for c in k5c.build_report(
        _write(tmp_path, "no_s3b.json", art))["criteria"]}
    assert by["P1"]["satisfied"] is False, by["P1"]
    assert any("s3 缺 B" in m for m in by["P1"]["missing"]), by["P1"]["missing"]
    assert "per_scene={'s1': 2, 's2': 2, 's3': 1}" in " ".join(
        by["P1"]["evidence"]), by["P1"]["evidence"]


# ─── 遗留 B（会审第 8 条）：路径可移植 + 报告落位 ──────────────────────
def test_report_contains_no_machine_absolute_paths(tmp_path):
    """报告文本内不出现机器绝对路径：`k4_artifact` 与 P0 证据行都相对
    仓库根书写；`generated_at` 带时区偏移（假 repo-root 在 tmp 下跑）。"""
    repo = tmp_path / "repo"
    (repo / "out_k4_3_x").mkdir(parents=True)
    art_file = repo / "out_k4_3_x" / "k4_paired.json"
    art_file.write_text(json.dumps(_perfect_artifact(), ensure_ascii=False),
                        encoding="utf-8")
    report = k5c.build_report(art_file, True, repo)
    text = json.dumps(report, ensure_ascii=False)
    assert report["k4_artifact"] == "out_k4_3_x/k4_paired.json", \
        report["k4_artifact"]
    by = {c["id"]: c for c in report["criteria"]}
    assert "out_k4_3_x/k4_paired.json" in " ".join(by["P0"]["evidence"]), \
        by["P0"]["evidence"]
    assert str(tmp_path) not in text, "报告内不得出现机器绝对路径"
    assert not _ABS_RE.search(text), "报告内不得内嵌盘符级绝对路径"
    assert _TZ_RE.search(report["generated_at"]), report["generated_at"]


def test_portable_path_outside_repo_has_no_drive_letter(tmp_path):
    """仓外收据：退化成 `<仓外>/文件名` 占位，仍不内嵌盘符。"""
    art_file = tmp_path / "loose.json"
    art_file.write_text(json.dumps(_perfect_artifact(), ensure_ascii=False),
                        encoding="utf-8")
    repo = tmp_path / "repo2"
    (repo / "docs").mkdir(parents=True)
    report = k5c.build_report(art_file, False, repo)
    text = json.dumps(report, ensure_ascii=False)
    assert report["k4_artifact"] == "<outside-repo>/loose.json", \
        report["k4_artifact"]
    assert not _ABS_RE.search(text), text[:300]


def test_report_out_path_lands_in_docs_not_repo_root(tmp_path):
    """落位规则：裸文件名一律进 <repo>/docs/（仓根不再新增报告）；
    带目录的相对路径按仓库根解析；绝对路径按给定写入。"""
    assert k5c.resolve_out_path("k5_report_20260925.json") == \
        k5c.ROOT / "docs" / "k5_report_20260925.json"
    assert k5c.resolve_out_path("docs/k5_r.json") == k5c.ROOT / "docs" / "k5_r.json"
    assert k5c.resolve_out_path("sub/k5_r.json") == k5c.ROOT / "sub" / "k5_r.json"
    abs_p = tmp_path / "out" / "k5_r.json"
    assert k5c.resolve_out_path(str(abs_p)) == abs_p


def test_repo_root_default_is_script_derived():
    """--repo-root 缺省改由脚本自身推导（ROOT），不再硬编码主仓机器路径。"""
    a = k5c.build_argparser().parse_args([])
    assert a.repo_root == str(k5c.ROOT), "缺省仓库根须由脚本自身推导"
    assert (Path(a.repo_root) / "scripts" / "k5_criteria_check.py").is_file(), \
        "推导出的仓库根须真的含本脚本——硬编码主仓路径做不到这一点"

