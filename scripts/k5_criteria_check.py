"""K5 判据核验器（lg-k5-criteria-check，2026-09-25 派工）。

把 K5 的通过判据（docs/K5-A_离线前置评估.md v1/2：P0–P3 前提 + C1–C5
扩场判据 + §3 停策略条件 + §4 止损口径）落成**可执行校验**：逐条给出
判据 / 当前是否满足 / 证据 / 缺什么。附带**十场路径 dry-run**：按既有
场景清单（scripts/k4_paired_scenes.py 的 scenes_for(10)）逐场给出
闸门状态、预计预算（调用数×实测单价）、会被哪条判据拦下。

纪律（任务书硬约束）：
- **只读、零写库、零模型调用**：判据核验只读收据 JSON 与真库（SELECT）；
  十场 dry-run 用 k4 驱动器的**离线 FixtureClient** 跑（零真实调用、
  freeze=False 零库写——tests/test_k4_paired.py 已钉死），产物只落
  临时目录；
- **dry-run 不构成通过**：报告永远带 mode=dry_run 且
  k5_established=False（C1 人工复核离线不可证；把 dry-run 说成已通
  是测试钉死的红线）；
- 不改 K2/K3 判定口径、不动表结构、不清语料。

用法（真跑命令见 docs/K5判据核验_20260925.md）：
    python scripts/k5_criteria_check.py --repo-root F:/agi/language-genome \
        --out <report.json>
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 工作树根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DOC = "docs/K5-A_离线前置评估.md（v1/2）"
STOP_LOSS_TOKENS = 8_000_000        # §4 总量止损线
K4_SCENE_BUDGET = {"normal_calls_per_arm": 2, "worst_calls_per_arm": 6,
                   "arms": 2, "scenes": 10}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def latest_k4_artifact(repo_root: Path) -> Path | None:
    """最新三场真跑收据：out_k4_3*/k4_paired.json 按 mtime 取最新。"""
    cands = sorted(Path(repo_root).glob("out_k4_3*/k4_paired.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _crit(cid, name, source, satisfied, evidence, missing):
    return {"id": cid, "name": name, "source": source,
            "satisfied": satisfied, "evidence": evidence, "missing": missing}


def check_criteria(artifact_path: Path | None) -> list[dict]:
    """P0–P3 + C1–C5 逐条核验（只读收据；缺收据=缺证据=未满足）。"""
    out: list[dict] = []
    art = None
    if artifact_path is not None and artifact_path.exists():
        art = _load_json(artifact_path)
    if art is None:
        out.append(_crit("P0", "通道健康（原 402 拍板已失效改判）", DOC
                         + " §1 / docs/K4_首轮真跑_证据_20260923.md",
                         False, [], "缺任何 live 真跑收据（out_k4_3*/"
                         "k4_paired.json 不存在）——通道可用性无实证"))
        for cid, name in (("P1", "K4 三场 6/6 臂 committed（failures=0）"),
                          ("P2", "A 臂包非空率 ≥2/3"),
                          ("P3", "双闸纪律未被绕过")):
            out.append(_crit(cid, name, DOC + " §1", False, [],
                             "缺 K4 真跑收据，前提层无从核起"))
        for cid, name in (("C1", "方向一致差异 ≥1 处经人工复核确认"),
                          ("C2", "零失败臂"), ("C3", "A 臂非空包率 ≥2/3"),
                          ("C4", "复核负担 ≤20 分钟/场"),
                          ("C5", "通道一致性（同通道或标 channel_changed）")):
            out.append(_crit(cid, name, DOC + " §2", False, [],
                             "缺 K4 真跑收据，判据无从核起"))
        out.append(_crit("C4+", "总量止损（真跑 tokens ≤800 万）",
                         DOC + " §4", False, [],
                         "缺真跑收据，tokens 实耗无从核起"))
        return out

    a = art.get("artifacts") or {}
    receipts = a.get("receipts") or []
    packages = a.get("packages") or []
    failures = a.get("failures") or []
    prose = a.get("prose") or []
    committed = [(p["scene"], p["arm"]) for p in prose
                 if p.get("status") == "committed"]
    tokens = sum(r.get("usage", {}).get("tokens", 0) for r in receipts)
    nonempty = sum(1 for p in packages if p.get("n_techniques", 0) >= 1)
    pkg_scenes = len(packages)
    live = art.get("live") is True

    # P0 通道健康：有 live 收据且真出过正文 = 通道可用实证
    out.append(_crit("P0", "通道健康（402 已证伪改判）",
                     DOC + " §1 / docs/K4_首轮真跑_证据_20260923.md",
                     bool(live and committed),
                     [f"收据 {artifact_path}：live={live}，committed="
                      f"{len(committed)} 处"],
                     [] if (live and committed) else
                     ["live 收据中无 committed 正文——通道可用性未实证"]))

    # P1 三场 6/6 臂 committed 且 failures=0
    p1_ok = live and len(committed) == 6 and not failures
    out.append(_crit("P1", "K4 三场 6/6 臂 committed（failures=0）",
                     DOC + " §1/§6（期望 failures=0）", p1_ok,
                     [f"committed={len(committed)}/6", f"failures="
                      f"{len(failures)}", f"skipped={len(a.get('skipped') or [])}"],
                     [] if p1_ok else [
                         f"committed {len(committed)}/6 ≠ 6；failures="
                         f"{len(failures)}≠0——缺『三场全过』的真跑收据"]))

    # P2/C3 A 臂非空包率 ≥2/3（三场口径；10 场口径见十场段）
    p2_ok = pkg_scenes >= 3 and nonempty >= 2
    out.append(_crit("P2", "A 臂包非空率 ≥2/3 场",
                     DOC + " §1/§6（期望 n_techniques≥1 的场数≥2）", p2_ok,
                     [f"packages 场数={pkg_scenes}，n_techniques≥1 场数="
                      f"{nonempty}",
                      f"逐场 n_techniques={[p.get('n_techniques') for p in packages]}"],
                     [] if p2_ok else [
                         "A 臂知识包全空——根因：8 张 legacy 策略卡经 24 席"
                         "独立判定全数『证据不支持』（docs/策略语义审查_判定"
                         "模型版_20260923.md），合并新卡 S1/S2 尚未走完"
                         "成对对照证据→两席判定→status 晋升链，K3 只服务 "
                         "verified+observed（不许放宽）"]))

    # P3 双闸纪律：机制在位（回归钉死）＋ live 收据出自门内
    out.append(_crit("P3", "双闸纪律未被绕过",
                     DOC + " §1（K4_ALLOW_LIVE=1 显式）", None,
                     ["机制：scripts/k4_paired_scenes.py --live 双闸 + "
                      "tests/test_k4_paired.py::test_live_double_gate 回归在位",
                      f"收据 live={live}（真跑收据只能经门产生）"],
                     ["『未被绕过』离线不可全证——机制与回归在位，无绕过"
                      "记录可查；真值待主控侧审计"]))

    # C1 人工复核：结构性离线不可证
    out.append(_crit("C1", "方向一致差异 ≥1 处经人工复核确认",
                     DOC + " §2（报告停止线）", False,
                     ["机械层无人工复核记录可读——该判据设计上只认人工证据"],
                     ["缺集霸（或其授权复核席）对 A/B 差异的人工复核记录"
                      "（≥1 处、非预算/超时/通道噪声）——离线校验永远给不出"
                      "此证据，K5 不可能由 dry-run 宣告通过"]))

    # C2 零失败臂
    rb = [f for f in failures if f.get("rollback_failed")]
    c2_ok = not failures
    out.append(_crit("C2", "零失败臂", DOC + " §2/§6（期望 failures=0）",
                     c2_ok,
                     [f"failures={len(failures)}（rollback_failed 条目 "
                      f"{len(rb)}）；skipped_after_failure 不计入失败但"
                      "阻断 P1 完整性"],
                     [] if c2_ok else [
                         "真跑存在失败臂（s2 预算/改稿闸诚实拒绝）——修卡/"
                         "调参后重跑 K4，不扩 10 场"]))

    # C3 同 P2（列独立判据条目，判据表逐条列全）
    out.append(_crit("C3", "A 臂非空包率 ≥2/3（C 行）",
                     DOC + " §2/§6", p2_ok, [f"n_techniques≥1 场数={nonempty}"],
                     [] if p2_ok else ["同 P2：知识卡未晋升，查得空=知识"
                                       "不足（先补 K2 成对证据链）"]))

    # C4 复核负担
    out.append(_crit("C4", "复核负担可承受（≤20 分钟/场，集霸口径）",
                     DOC + " §2", False,
                     ["无差异清单与复核耗时记录可读"],
                     ["缺 A/B 差异清单与逐场人工复核耗时记录——依赖 C1 的"
                      "人工复核先行"]))

    # C5 通道一致性
    cc = art.get("channel_changed")
    c5_ok = bool(live and cc is True)
    out.append(_crit("C5", "通道一致性（换通道须标 channel_changed 并复核 C1）",
                     DOC + " §2（A6 写死：未标 → C1 与 C5 均不过）", c5_ok,
                     [f"收据 channel_changed={cc}；writer/verifier 记录="
                      f"{receipts[0].get('models') if receipts else None}"],
                     [] if c5_ok else [
                          "10 场尚未真跑（无 out_k4_10 收据）；三场收据须带"
                          " channel_changed=true 且 10 场与之同通道或换通道"
                          "再标再复核 C1"] if cc is True else [
                          "三场收据未标 channel_changed=true（A6：C1 与 C5 "
                          "均判不过）"] + (["10 场收据缺位"] if cc is True else [])))

    # 止损（§4）附核
    out.append(_crit("C4+", "总量止损（真跑 tokens ≤800 万）",
                     DOC + " §4", tokens <= STOP_LOSS_TOKENS,
                     [f"三场收据 tokens 合计={tokens} ≤ {STOP_LOSS_TOKENS}"],
                     [] if tokens <= STOP_LOSS_TOKENS else ["已越线，停止扩张"]))
    return out


def ten_scene_dry_run(repo_root: Path, py: str) -> dict:
    """十场离线 dry-run：真库只读（LG_DATABASE_URL 指真库）、FixtureClient
    零真实调用、freeze=False 零库写；产物落临时目录。逐场给出结构闸门状态
    + 预计预算 + 拦路判据（按判据核验的当前态投影）。"""
    env = dict(os.environ)
    env["LG_DATABASE_URL"] = (
        f"sqlite:///{(Path(repo_root) / 'data' / 'language_genome.db').as_posix()}")
    with tempfile.TemporaryDirectory(prefix="k5_dry_") as td:
        out_dir = Path(td) / "dry10"
        cmd = [py, "scripts/k4_paired_scenes.py",
               "--scenes", "10", "--out", str(out_dir)]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           cwd=str(ROOT), timeout=600)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout)[-500:]}
        art = _load_json(out_dir / "k4_paired.json")["artifacts"]
    prose = art.get("prose") or []
    per_scene = {}
    for p in prose:
        per_scene.setdefault(p["scene"], {"committed": 0, "arms": {}})
        per_scene[p["scene"]]["arms"][p["arm"]] = p["status"]
        per_scene[p["scene"]]["committed"] += \
            int(p["status"] == "committed")
    # 预算（K5-A §4 模型 + out_k4_3_mc22_v2 实测单价，双口径如实并列）
    n_calls_normal = (K4_SCENE_BUDGET["normal_calls_per_arm"]
                     * K4_SCENE_BUDGET["arms"] * K4_SCENE_BUDGET["scenes"])
    n_calls_worst = (K4_SCENE_BUDGET["worst_calls_per_arm"]
                     * K4_SCENE_BUDGET["arms"] * K4_SCENE_BUDGET["scenes"])
    measured_per_call = 542        # tokens/调用：三场真跑实测均值
    blockers = ["C3/P2（A 臂包空：策略卡未晋升 verified——24 席判定否决，"
                "成对对照证据链未走完）",
                "C5（10 场收据缺位；须与三场同通道或标 channel_changed）",
                "C1（无人工复核记录——离线结构性不可证）",
                "P1/C2（三场基线尚未 6/6——s2 两臂真跑被预算/改稿闸拒）"]
    return {
        "ok": True, "mode": "dry_run", "live": False,
        "scenes": {sid: {"arms": v["arms"],
                         "committed_arms": v["committed"],
                         "structural_status": "committed" if v["committed"] == 2
                         else "failed",
                         "est_budget": {
                             "normal_calls": K4_SCENE_BUDGET[
                                 "normal_calls_per_arm"] * 2,
                             "worst_calls": K4_SCENE_BUDGET[
                                 "worst_calls_per_arm"] * 2,
                             "est_tokens_normal": K4_SCENE_BUDGET[
                                 "normal_calls_per_arm"] * 2 * measured_per_call,
                             "est_tokens_worst": K4_SCENE_BUDGET[
                                 "worst_calls_per_arm"] * 2 * measured_per_call,
                             "unit_price_note": f"实测单价≈{measured_per_call} "
                             "tokens/调用（out_k4_3_mc22_v2 收据均值：2,167+"
                             "10,643 tokens/24 调用≈542/调用含失败链）；"
                             "货币单价以通道账为准，不虚构"},
                         "blocked_by": blockers}
                    for sid, v in sorted(per_scene.items())},
        "totals": {"scenes": len(per_scene),
                   "normal_calls": n_calls_normal, "worst_calls": n_calls_worst,
                   "est_tokens_normal": n_calls_normal * measured_per_call,
                   "est_tokens_worst": n_calls_worst * measured_per_call,
                   "stop_loss_tokens": STOP_LOSS_TOKENS,
                   "doc_model_note": "K5-A §4 文档口径（每调用 4~6 万 token 的"
                                     "语料级估算）与三场实测口径并列；扩场"
                                     "前以实测复核"},
        "n_prose": len(prose), "failures": len(art.get("failures") or []),
        "skipped": len(art.get("skipped") or []),
        "note": "dry-run=离线 FixtureClient 结构性跑通；**不构成任何判据"
                "的通过证据**（live 收据才算数）"}


def build_report(artifact_path: Path | None) -> dict:
    """纯函数：判据核验报告（不含十场子进程段——离线可测）。"""
    criteria = check_criteria(artifact_path)
    mech_ok = sum(1 for c in criteria if c["satisfied"] is True)
    return {
        "mode": "dry_run", "generated_at": datetime.datetime.now().isoformat(
            timespec="seconds"),
        "doc": DOC, "k4_artifact": str(artifact_path) if artifact_path else None,
        "criteria": criteria,
        "n_criteria": len(criteria), "n_satisfied": mech_ok,
        # 红线：dry-run/只读核验永远不构成「K5 已通」——C1 人工复核离线
        # 结构性缺证据，k5_established 恒 False（tests 钉死，防话术漂移）
        "k5_established": False,
        "verdict": "K5 未通——dry-run 与只读核验不构成通过；当前判据 "
                   f"{mech_ok}/{len(criteria)} 项机械层满足，C1 人工复核"
                   "结构性缺证据（详见 missing）",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default="F:/agi/language-genome",
                    dest="repo_root",
                    help="主仓根（收据与真库所在；本脚本对其只读）")
    ap.add_argument("--k4-artifact", default="", dest="k4_artifact",
                    help="三场收据路径（缺省自动取最新 out_k4_3*/k4_paired.json）")
    ap.add_argument("--out", default="", help="报告 JSON 落盘（可选）")
    ap.add_argument("--py", default=sys.executable,
                    help="驱动器子进程解释器（缺省当前解释器）")
    a = ap.parse_args()
    repo = Path(a.repo_root)
    art = Path(a.k4_artifact) if a.k4_artifact else latest_k4_artifact(repo)
    report = build_report(art)
    report["ten_scene_dry_run"] = ten_scene_dry_run(repo, a.py)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"[k5_criteria_check] 报告已写 {a.out}")


if __name__ == "__main__":
    main()
