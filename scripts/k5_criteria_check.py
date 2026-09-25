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

用法（真跑命令见 docs/K5判据核验_20260925.md §6）：
    python scripts/k5_criteria_check.py --out k5_report_20260925.json
  - `--repo-root` 缺省＝脚本自身推导的仓库根（ROOT），不硬编码主仓机器路径；
  - `--out` 给裸文件名一律落 `<repo>/docs/`（仓根不再新增报告）；
  - 报告里的路径字段相对仓库根、`generated_at` 带时区偏移（可移植、可对账）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
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
                   "arms": 2, "scenes": 10,
                   # 收据不可读时的保守兜底单价（不静默：price_src 会写明来源）
                   "fallback_per_call": 542}
# P1 的逐场口径（会审 2026-09-25 第 7 条）：三场每场 A/B 各 committed 一次
K4_REQUIRED_SCENES = ("s1", "s2", "s3")
K4_REQUIRED_ARMS = ("A", "B")
P1_NAME = "K4 三场每场 A/B 各 committed（共 6 处，failures=0）"
# 报告落位（会审 2026-09-25 第 8 条）：仓根不再新增报告，与同类报告同放 docs/
REPORT_DIR = ROOT / "docs"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def portable_path(path, repo_root=None) -> str | None:
    """绝对路径 → 报告里可移植的相对写法（相对仓库根）。

    会审 2026-09-25 第 8 条：报告内嵌 `F:\\...` 机器绝对路径换台机器/换
    worktree 就对不上账，故一律相对化；仓库根之外的路径不内嵌盘符，退化成
    `<仓外>/文件名` 占位（宁可少写，也不产出不可移植的证据行）。"""
    if path is None:
        return None
    p = Path(path)
    base = Path(repo_root) if repo_root is not None else ROOT
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        return f"<outside-repo>/{p.name}"


def resolve_out_path(out) -> Path:
    """报告落位规则：绝对路径照写；相对路径按仓库根解析；**裸文件名落
    `<repo>/docs/`**（仓根不再新增报告）。"""
    p = Path(out)
    if p.is_absolute():
        return p
    if p.parent == Path("."):
        return REPORT_DIR / p.name
    return ROOT / p


def _committed_by_scene(committed) -> dict:
    """逐场 committed 计数（三场缺场记 0；口径外的场照实列出便于对账）。"""
    per = {s: 0 for s in K4_REQUIRED_SCENES}
    for scene, _arm in committed:
        key = str(scene)
        per[key] = per.get(key, 0) + 1
    return per


def _committed_arms(committed) -> dict:
    """逐场 committed 的臂集合（分布校验用，不看总数）。"""
    arms: dict = {}
    for scene, arm in committed:
        arms.setdefault(str(scene), set()).add(str(arm))
    return arms


def latest_k4_artifact(repo_root: Path) -> Path | None:
    """最新三场真跑收据：out_k4_3*/k4_paired.json 按 mtime 取最新。"""
    cands = sorted(Path(repo_root).glob("out_k4_3*/k4_paired.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _world_db_tokens(art: dict) -> tuple[int, str]:
    """世界库实耗 tokens（含失败臂）：逐 job 的 calls.response 里
    tokens_in+tokens_out 求和（只读打开，mode=ro）。

    为什么必须算：三场收据 receipts 只覆盖 committed 臂（s1 两臂），
    s2 两臂诚实失败后的 6+6 次调用实耗**只在世界库 calls 表**里。
    2026-09-25 会审双席一致指出：止损若只累加 receipts，会把 12,810
    报成 2,167，止损线形同虚设。不可读时返回 (0, 原因)，由调用方
    如实降级并在证据行披露（不静默当成 0）。
    """
    wd = art.get("worlds_dir")
    if not wd:
        return 0, "收据无 worlds_dir 字段——失败臂实耗不可核，止损按收据口径（会少计）"
    total, n_db, errs = 0, 0, []
    for db in sorted(Path(wd).glob("arm*/*.sqlite")):
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                for (resp,) in con.execute("select response from calls"):
                    if not resp:
                        continue
                    try:
                        d = json.loads(resp)
                    except (TypeError, ValueError):
                        continue
                    total += int(d.get("tokens_in") or 0) + int(d.get("tokens_out") or 0)
                n_db += 1
            finally:
                con.close()
        except sqlite3.Error as e:
            errs.append(f"{db.parent.name}: {e}")
    if not n_db:
        return 0, ("世界库不可读（%s）——失败臂实耗不可核，止损按收据口径（会少计）"
                   % ("; ".join(errs) or "无 arm*/*.sqlite"))
    return total, (f"世界库 {n_db} 个 arm 库实耗={total}（含失败臂）"
                   + ("；部分库不可读：%s" % "; ".join(errs) if errs else ""))


def has_ten_scene_artifact(repo_root: Path) -> bool:
    """10 场真跑收据是否存在（out_k4_10*/k4_paired.json）——C5 的满足前提。"""
    return any(Path(repo_root).glob("out_k4_10*/k4_paired.json"))


def _crit(cid, name, source, satisfied, evidence, missing):
    return {"id": cid, "name": name, "source": source,
            "satisfied": satisfied, "evidence": evidence, "missing": missing}


def check_criteria(artifact_path: Path | None,
                   ten_scene_present: bool = False,
                   repo_root: Path | None = None) -> list[dict]:
    """P0–P3 + C1–C5 逐条核验（只读收据；缺收据=缺证据=未满足）。

    `repo_root` 只用于把证据里的路径写成相对仓库根的可移植形式（缺省取脚本
    推导的仓库根）。"""
    out: list[dict] = []
    art = None
    if artifact_path is not None and artifact_path.exists():
        art = _load_json(artifact_path)
    art_rel = portable_path(artifact_path, repo_root)
    if art is None:
        out.append(_crit("P0", "通道健康（原 402 拍板已失效改判）", DOC
                         + " §1 / docs/K4_首轮真跑_证据_20260923.md",
                         False, [], "缺任何 live 真跑收据（out_k4_3*/"
                         "k4_paired.json 不存在）——通道可用性无实证"))
        for cid, name in (("P1", P1_NAME),
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
    tokens_receipts = sum(r.get("usage", {}).get("tokens", 0) for r in receipts)
    tokens_worlds, worlds_note = _world_db_tokens(art)
    # 止损口径取两者较大者：收据只覆盖 committed 臂，失败臂实耗只在世界库；
    # 只用收据会系统性少计（会审双席 2026-09-25 一致指出）。
    tokens = max(tokens_receipts, tokens_worlds)
    nonempty = sum(1 for p in packages if p.get("n_techniques", 0) >= 1)
    pkg_scenes = len(packages)
    live = art.get("live") is True

    # P0 通道健康：有 live 收据且真出过正文 = 通道可用实证
    out.append(_crit("P0", "通道健康（402 已证伪改判）",
                     DOC + " §1 / docs/K4_首轮真跑_证据_20260923.md",
                     bool(live and committed),
                     [f"收据 {art_rel}：live={live}，committed="
                      f"{len(committed)} 处"],
                     [] if (live and committed) else
                     ["live 收据中无 committed 正文——通道可用性未实证"]))

    # P1 逐场分布校验（会审 2026-09-25 第 7 条）：三场每场 A/B 各 committed
    # 一次且总数恰 6、failures=0。只看 len(committed)==6 会被两种形态骗过：
    # ① 6 处集中在少数场；② 4 场/10 场收据被 out_k4_3* glob 误匹配（>6）。
    per_scene = _committed_by_scene(committed)
    arms_by_scene = _committed_arms(committed)
    p1_total_ok = len(committed) == 6
    p1_extra = sorted(set(arms_by_scene) - set(K4_REQUIRED_SCENES))
    p1_gap_scenes = [s for s in K4_REQUIRED_SCENES if s not in arms_by_scene]
    p1_lack = {s: sorted(set(K4_REQUIRED_ARMS) - arms_by_scene.get(s, set()))
               for s in K4_REQUIRED_SCENES
               if set(K4_REQUIRED_ARMS) - arms_by_scene.get(s, set())}
    p1_dist_ok = (not p1_extra and not p1_gap_scenes and not p1_lack
                  and all(per_scene[s] == 2 for s in K4_REQUIRED_SCENES))
    p1_ok = bool(live and p1_total_ok and p1_dist_ok and not failures)
    p1_missing: list[str] = []
    if not live:
        p1_missing.append("收据 live≠True——P1 只认 live 真跑收据")
    if not p1_total_ok:
        p1_missing.append(f"committed 总数={len(committed)}≠6（每场 2 处×三场）")
    if p1_extra:
        p1_missing.append(f"committed 出现在三场口径外的场 {p1_extra}"
                          "（疑 4 场/10 场收据被 out_k4_3* glob 误匹配）"
                          "——总数达标也不判过")
    if p1_gap_scenes:
        p1_missing.append(f"三场中完全无 committed 记录的场：{p1_gap_scenes}"
                          "——不满足『每场 A/B 各 1』的分布")
    for _s, _lack in sorted(p1_lack.items()):
        p1_missing.append(f"{_s} 缺 {'/'.join(_lack)} 臂 committed"
                          "（每场须 A/B 各 1 次）")
    if failures:
        p1_missing.append(f"failures={len(failures)}≠0——缺『三场全过』的真跑收据")
    if not p1_ok and not p1_missing:      # 不许「未满足却说不出缺什么」
        p1_missing.append(f"逐场分布不满足三场每场 A/B 各 committed：per_scene={per_scene}")
    arms_view = {s: sorted(arms_by_scene.get(s, set())) for s in K4_REQUIRED_SCENES}
    out.append(_crit("P1", P1_NAME,
                     DOC + " §1/§6（期望每场 2/2、failures=0）", p1_ok,
                     [f"committed={len(committed)}/6（总数）",
                      f"per_scene={per_scene}",
                      f"逐场 committed 臂={arms_view}",
                      f"failures={len(failures)}",
                      f"skipped={len(a.get('skipped') or [])}"],
                     [] if p1_ok else p1_missing))

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
    models = receipts[0].get("models") if receipts else None
    # 会审 2026-09-25 指出：C5 是「三场+10 场同通道」的一致性判据，
    # 10 场收据缺位时不能给 satisfied=True；证据键缺失也不许静默判过。
    # C5 判据原文（docs/K5-A §2）：10 场用与 K4 相同通道，换通道须标
    # channel_changed 并对 C1 复核。故 C5 的满足前提是**10 场收据存在**；
    # 只有三场收据时不能判过（会审 2026-09-25 一致指出）。
    c5_ok = bool(live and cc is True and models is not None
                 and ten_scene_present)
    out.append(_crit("C5", "通道一致性（换通道须标 channel_changed 并复核 C1）",
                     DOC + " §2（A6 写死：未标 → C1 与 C5 均不过）", c5_ok,
                     [f"收据 channel_changed={cc}；writer/verifier 记录={models}"],
                     [] if c5_ok else (
                         ["三场收据未标 channel_changed=true（A6：C1 与 C5 均判不过）"]
                         if cc is not True else []) + (
                         [f"writer/verifier 证据键缺位（models={models}）——"
                          "缺证据不许判过"] if models is None else []) + (
                         ["10 场收据缺位（无 out_k4_10*/k4_paired.json）："
                          "C5 是 10 场通道一致性判据，只有三场收据不能判过"]
                         if not ten_scene_present else [])))

    # 止损（§4）附核
    out.append(_crit("C4+", "总量止损（真跑 tokens ≤800 万）",
                     DOC + " §4", tokens <= STOP_LOSS_TOKENS,
                     [f"收据口径 tokens={tokens_receipts}（仅 committed 臂）",
                      worlds_note,
                      f"止损取用={tokens} ≤ {STOP_LOSS_TOKENS}"],
                     [] if tokens <= STOP_LOSS_TOKENS else
                     [f"已越线（{tokens} > {STOP_LOSS_TOKENS}），停止扩张"]))
    return out


def ten_scene_dry_run(repo_root: Path, py: str) -> dict:
    """十场离线 dry-run：真库只读（LG_DATABASE_URL 指真库）、FixtureClient
    零真实调用、freeze=False 零库写；产物落临时目录。逐场给出结构闸门状态
    + 预计预算 + 拦路判据（按判据核验的当前态投影）。"""
    env = dict(os.environ)
    env["LG_DATABASE_URL"] = (
        f"sqlite:///{(Path(repo_root) / 'data' / 'language_genome.db').as_posix()}")
    # 会审 2026-09-25 指出：全量继承父环境会把调用方 shell 里残留的
    # K4_ALLOW_LIVE=1 等闸门变量原样传进子进程——本段必须**确定离线**，
    # 故显式清掉 live 闸门变量（不依赖「驱动器缺省离线」这一隐式前提）。
    for gate in ("K4_ALLOW_LIVE", "LG_LIVE", "LG_LLM_MODE"):
        env.pop(gate, None)
    with tempfile.TemporaryDirectory(prefix="k5_dry_") as td:
        out_dir = Path(td) / "dry10"
        cmd = [py, "scripts/k4_paired_scenes.py",
               "--scenes", "10", "--out", str(out_dir)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                               cwd=str(ROOT), timeout=600)
        except subprocess.TimeoutExpired:
            # 红线精神：失败形态必须如实落成 ok=False，不许裸异常栈崩
            return {"ok": False, "mode": "dry_run", "live": None,
                    "error": "dry-run 子进程超时（600s）——未产出报告；"
                             "不得据此推断十场路径通畅"}
        if r.returncode != 0:
            return {"ok": False, "mode": "dry_run", "live": None,
                    "error": (r.stderr or r.stdout)[-500:]}
        art_file = out_dir / "k4_paired.json"
        if not art_file.exists():
            return {"ok": False, "mode": "dry_run", "live": None,
                    "error": "dry-run 子进程 rc=0 但未产出 k4_paired.json"}
        art_all = _load_json(art_file)
        # 断言子进程真的走离线：live 必须显式为 False（不许只看 returncode）
        if art_all.get("live") is not False:
            return {"ok": False, "mode": "dry_run", "live": art_all.get("live"),
                    "error": f"dry-run 子进程 live={art_all.get('live')!r} ≠ False"
                             "——疑似走到了 live 路径，已中止（不消耗调用）"}
        art = art_all["artifacts"]
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
    # 实测单价从最新三场收据实算（会审 2026-09-25 指出：硬编码魔数会
    # 在收据更新后脱节）。失败臂实耗取自世界库（与止损同口径）。
    art3 = latest_k4_artifact(repo_root)
    calls_real, tokens_real, price_src = 0, 0, "无收据——单价不可实算"
    if art3 is not None:
        try:
            a3 = _load_json(art3).get("artifacts") or {}
            rcpts = a3.get("receipts") or []
            calls_real = sum(int(r.get("usage", {}).get("calls", 0)) for r in rcpts)
            tok_r = sum(int(r.get("usage", {}).get("tokens", 0)) for r in rcpts)
            tok_w, _note = _world_db_tokens(_load_json(art3))
            # 世界库 calls 行数 = 全部臂的调用数（含失败臂）
            wd = _load_json(art3).get("worlds_dir")
            n_world = 0
            if wd:
                for db in sorted(Path(wd).glob("arm*/*.sqlite")):
                    try:
                        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
                        n_world += con.execute("select count(*) from calls").fetchone()[0]
                        con.close()
                    except sqlite3.Error:
                        pass
            calls_real = max(calls_real, n_world)
            tokens_real = max(tok_r, tok_w)
            if calls_real:
                price_src = (f"{art3.parent.name} 实算：{tokens_real} tokens/"
                             f"{calls_real} 调用（含失败臂，世界库口径）")
        except (OSError, ValueError, KeyError):
            price_src = f"{art3.parent.name} 读取失败——单价不可实算"
    measured_per_call = (round(tokens_real / calls_real) if calls_real
                         else K4_SCENE_BUDGET.get("fallback_per_call", 0))
    # blockers 由判据实际结果派生（会审指出：硬编码列表会与判据表脱节）
    _crits = check_criteria(art3, has_ten_scene_artifact(repo_root), repo_root)
    blockers = [f"{c['id']}（{c['name']}）"
                for c in _crits if c["satisfied"] is not True] or                ["无（全部判据机械层满足——但仍不构成 K5 通过，见 C1）"]
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
                             f"tokens/调用（{price_src}）；"
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


def build_report(artifact_path: Path | None,
                 ten_scene_present: bool = False,
                 repo_root: Path | None = None) -> dict:
    """纯函数：判据核验报告（不含十场子进程段——离线可测）。

    可移植性（会审 2026-09-25 第 8 条）：路径字段相对 `repo_root` 书写、
    `generated_at` 带时区偏移，报告换机器/换 worktree 仍可对账。"""
    criteria = check_criteria(artifact_path, ten_scene_present, repo_root)
    mech_ok = sum(1 for c in criteria if c["satisfied"] is True)
    return {
        "mode": "dry_run",
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "doc": DOC, "k4_artifact": portable_path(artifact_path, repo_root),
        "criteria": criteria,
        "n_criteria": len(criteria), "n_satisfied": mech_ok,
        # 红线：dry-run/只读核验永远不构成「K5 已通」——C1 人工复核离线
        # 结构性缺证据，k5_established 恒 False（tests 钉死，防话术漂移）
        "k5_established": False,
        "verdict": "K5 未通——dry-run 与只读核验不构成通过；当前判据 "
                   f"{mech_ok}/{len(criteria)} 项机械层满足，C1 人工复核"
                   "结构性缺证据（详见 missing）",
    }


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(ROOT), dest="repo_root",
                    help="仓库根（收据与真库所在；本脚本对其只读）。"
                         "缺省＝脚本自身推导的仓库根，不硬编码机器路径")
    ap.add_argument("--k4-artifact", default="", dest="k4_artifact",
                    help="三场收据路径（缺省自动取最新 out_k4_3*/k4_paired.json）")
    ap.add_argument("--out", default="",
                    help="报告 JSON 落盘：裸文件名一律落 <repo>/docs/"
                         "（仓根不再新增报告）；带目录或绝对路径按给定写入")
    ap.add_argument("--py", default=sys.executable,
                    help="驱动器子进程解释器（缺省当前解释器）")
    return ap


def main() -> None:
    a = build_argparser().parse_args()
    repo = Path(a.repo_root)
    art = Path(a.k4_artifact) if a.k4_artifact else latest_k4_artifact(repo)
    report = build_report(art, has_ten_scene_artifact(repo), repo)
    report["ten_scene_dry_run"] = ten_scene_dry_run(repo, a.py)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if a.out:
        out_path = resolve_out_path(a.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"[k5_criteria_check] 报告已写 {out_path}")


if __name__ == "__main__":
    main()
