"""K4 收据世界目录对账（审计非阻断项第二段，lg-k4-worlds-receipt，2026-09-26）。

任务书三条验收点，逐条钉死：
① **新收据含 worlds_dir**——live 逐条收据记绝对路径 + cleaned 标志，且这份
   地址是**消费侧真在用**的：k4_accept_report 以它（+ 收据 job_id 在
   arm*/k4.sqlite 的只读反查）定位该臂世界库，不再只靠「产物顶层 worlds_dir
   + 工厂序 arm1=A/arm2=B」猜。
② **旧收据（缺 worlds_dir 键）读入不炸**——2026-09-23/24 形态的收据照常出
   报告：退回产物顶层键口径（顶层也没有 ⇒ 地址不可知，cleaned=None，既不判
   已清理也不假装知道），判定口径与 summary 逐字不变。
③ **已清理的目录被如实标注，不伪装成「仍在」**——写收据时目录在、报告时已被
   删 ⇒ cleaned=true + 依据串；世界库不可核的原因分「已清理 / 地址不可知 /
   在但该臂库不在」三类，不混成一句。收据说 cleaned=true 而目录仍在的矛盾
   也如实并列（不替任一方圆场）。

纪律：零真实调用（FxClient 顶替 GatewayClient、freeze=False 强绑、绝不加
--live）、世界库一律 mode=ro 只读、所有夹具目录都在 tmp_path 下自建自销，
不碰真库 data/language_genome.db、不写仓库任何位置。
"""
from __future__ import annotations

import importlib.util as _u
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k4ps", ROOT / "scripts" / "k4_paired_scenes.py")
k4 = _u.module_from_spec(_spec)
sys.modules["k4ps"] = k4
_spec.loader.exec_module(k4)

_ar = _u.spec_from_file_location(
    "k4ar2", ROOT / "scripts" / "k4_accept_report.py")
k4ar = _u.module_from_spec(_ar)
sys.modules["k4ar2"] = k4ar
_ar.loader.exec_module(k4ar)

from knowledge_seed import seed_knowledge          # noqa: E402
from app import db                                  # noqa: E402
from app.scene_runtime.store import Store           # noqa: E402

# 与 app/scene_runtime/store.py 同形的最小世界库（只读反查只用到这两张表）
SCHEMA = """
CREATE TABLE jobs(
  id TEXT PRIMARY KEY, book TEXT, branch TEXT, scene TEXT,
  idem TEXT, request_hash TEXT NOT NULL, request TEXT NOT NULL,
  context TEXT NOT NULL, status TEXT NOT NULL, verified TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE calls(
  job TEXT, stage TEXT, request_hash TEXT NOT NULL, status TEXT NOT NULL,
  request TEXT NOT NULL, response TEXT, error TEXT,
  started_at TEXT NOT NULL, duration_ms INTEGER,
  PRIMARY KEY(job,stage));
"""


def _mk_arm_db(wd: Path, arm_n: int, *, scene="s2", max_calls=6,
               n_calls=6, job_status="running", job_id=None):
    """造 wd/arm{n}/k4.sqlite（arm1=A、arm2=B，与 main() 的工厂序同形）。"""
    d = wd / f"arm{arm_n}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "k4.sqlite"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    jid = job_id or f"job-{scene}-{'AB'[arm_n - 1]}"
    plan = {"plan": {"scene_id": scene,
                     "idempotency_key": f"k4-{scene}-{'AB'[arm_n - 1]}"},
            "budget": {"max_calls": max_calls, "max_rewrites": 2}}
    con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, "WK-K4", "main", scene, plan["plan"]["idempotency_key"],
                 "h", json.dumps(plan), "{}", job_status, None, "t", "t"))
    for i in range(n_calls):
        con.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?)",
                    (jid, f"writer.{i // 2}" if i % 2 == 0
                     else f"verifier.{i // 2}", "h", "succeeded",
                     "{}", '{"text":"x"}', None, "t", 1))
    con.commit()
    con.close()
    return p


def _write_artifact(tmp_path: Path, *, worlds_dir, receipts, failures=None,
                    prose=None, skips=None, name="k4_paired.json") -> Path:
    art = {"artifacts": {"prose": prose or [], "packages": [],
                         "receipts": receipts, "failures": failures or [],
                         "skipped": skips or []},
           "analysis": {}, "live": True, "channel_changed": False}
    if worlds_dir is not None:          # 旧收据：顶层键都可以没有
        art["worlds_dir"] = str(worlds_dir)
    p = tmp_path / name
    p.write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")
    return p


def _arm_factory(wd: Path):
    """逐字复刻 main() 的工厂：wd/arm{n}/k4.sqlite，A 先 B 后。"""
    n = {"i": 0}

    def make():
        n["i"] += 1
        store = Store(wd / f"arm{n['i']}" / "k4.sqlite")
        store.create_world(k4.build_world())
        return store
    return make


def _live_four(wd: Path, *, created_at="2026-09-26T00:00:00Z"):
    """真跑一次 run_paired（live=True + FxClient + freeze=False，零真实调用），
    返回 four——逐条收据的 worlds_dir 四键由生产侧真实生成，不手写夹具。"""
    wd.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        four = k4.run_paired(_arm_factory(wd), k4.FxClient(), s,
                             live=True, freeze=False,
                             worlds_dir=str(wd), worlds_created_at=created_at)
    assert not four["failures"], four["failures"]
    return four


def _by(rows):
    return {(r["scene"], r["arm"]): r for r in rows}


def arm_n(arm: str) -> int:
    """main() 工厂序：A 先建 ⇒ arm1、B ⇒ arm2（只作断言用的事实换算）。"""
    return 1 if arm == "A" else 2


# ── ① 新收据含 worlds_dir，且报告真的按它定位每臂世界库 ─────────────────────

def test_live_receipts_carry_worlds_dir_and_report_links_each_arm(tmp_path):
    """live 逐条收据带 worlds_dir（绝对路径）+ worlds_dir_cleaned；报告以收据
    worlds_dir + 收据 job_id 只读反查出该臂世界库（arm1=A / arm2=B 由证据
    定，不是猜），pass 行的依据里带得出「产物出自哪个目录的哪个库」。"""
    seed_knowledge()
    wd = tmp_path / "k4_worlds_live"
    four = _live_four(wd)
    assert len(four["receipts"]) == 6
    for r in four["receipts"]:
        assert r["worlds_dir"] == str(wd.resolve())
        assert Path(r["worlds_dir"]).is_absolute()
        assert r["worlds_dir_exists"] is True
        assert r["worlds_dir_cleaned"] is False, \
            "live 收据必须显式落 worlds_dir_cleaned（消费侧不靠缺键猜清理）"
    art = _write_artifact(tmp_path, worlds_dir=wd,
                          receipts=four["receipts"],
                          prose=four["prose"])
    report = k4ar.classify(art)
    w = report["worlds"]
    assert w["source"] == "receipt", w
    assert w["worlds_dir"] == str(wd.resolve())
    assert w["cleaned"] is False and w["exists"] is True
    assert w["receipts_with_worlds_dir"] == 6
    for arm, n in (("A", 1), ("B", 2)):
        a = w["arms"][arm]
        assert a["locate"] == "receipt_job_id", a
        assert Path(a["world_db"]) == wd / f"arm{n}" / "k4.sqlite", a
        assert a["readable"] is True, a
    assert report["summary"]["pass"] == 6, report["summary"]
    for (sc, arm), row in _by(report["rows"]).items():
        joined = "".join(row["basis"])
        assert "产物世界库：" in joined
        assert f"arm{arm_n(arm)}" in joined
        assert "cleaned=False" in joined, joined


def test_receipt_worlds_dir_wins_over_top_level(tmp_path):
    """收据 worlds_dir 优先于产物顶层键，且不一致时如实并列（不静默择一）：
    顶层指向垃圾路径、收据指向真目录 ⇒ 报告照样定位到真目录的世界库。"""
    seed_knowledge()
    wd = tmp_path / "k4_worlds_truth"
    four = _live_four(wd)
    art = _write_artifact(tmp_path, worlds_dir="Z:/stale-top-level",
                          receipts=four["receipts"], prose=four["prose"])
    w = k4ar.classify(art)["worlds"]
    assert w["source"] == "receipt" and w["worlds_dir"] == str(wd.resolve())
    assert w["top_level"] == "Z:/stale-top-level"
    assert "不一致" in w["drift"] and "Z:/stale-top-level" in w["drift"], w
    assert w["arms"]["A"]["world_db"] == str(wd / "arm1" / "k4.sqlite")
    assert w["arms"]["B"]["world_db"] == str(wd / "arm2" / "k4.sqlite")


# ── ② 旧收据（缺 worlds_dir 键）读入不炸、口径不变 ──────────────────────────

def test_old_receipt_without_worlds_dir_falls_back_verbatim(tmp_path):
    """2026-09-23 形态旧收据（无 worlds_dir/worlds_dir_exists/
    worlds_dir_cleaned）：读入不报错，退回产物顶层键口径并标出来源；判定与
    summary 与历史口径逐字相同（缺字段不许翻判、不许当成已清理）。"""
    wd = tmp_path / "old_worlds"
    wd.mkdir()
    _mk_arm_db(wd, 1, scene="s2", n_calls=6)
    _mk_arm_db(wd, 2, scene="s2", n_calls=6)
    old_receipts = [{"scene": "s1", "arm": "A", "job_id": "j-old-1",
                     "usage": {"calls": 2, "tokens": 900,
                               "verifier_invalid_retries": 0}},
                    {"scene": "s1", "arm": "B", "job_id": "j-old-2",
                     "usage": {"calls": 2, "tokens": 950,
                               "verifier_invalid_retries": 0}}]
    assert not any("worlds_dir" in r for r in old_receipts)
    art = _write_artifact(
        tmp_path, worlds_dir=wd, receipts=old_receipts,
        prose=[{"scene": "s1", "arm": "A", "status": "committed"},
               {"scene": "s1", "arm": "B", "status": "committed"}],
        failures=[
            {"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
             "error": "call_budget_exhausted", "rollback_failed": False},
            {"scene": "s2", "arm": "B", "error_type": "RuntimeFault",
             "error": "rewrite_budget_exhausted:missing_or_unplanned_event",
             "rollback_failed": False}],
        skips=[{"scene": "s3", "arm": "A", "skipped_after": "s2"},
               {"scene": "s3", "arm": "B", "skipped_after": "s2"}])
    report = k4ar.classify(art)                       # 不抛异常＝钉「读入不炸」
    w = report["worlds"]
    assert w["source"] == "top_level" and w["worlds_dir"] == str(wd)
    assert w["receipts_with_worlds_dir"] == 0
    assert w["recorded_exists"] is None and w["recorded_cleaned"] is None
    assert w["cleaned"] is False and w["exists"] is True
    for arm in ("A", "B"):
        assert w["arms"][arm]["locate"] == "factory_order_fallback", \
            "旧收据无 worlds_dir/job_id 命中 ⇒ 退回工厂序口径并标明"
    assert report["summary"] == {
        "pass": 2, "honest_failure": 2, "defect_undetermined": 0,
        "insufficient_evidence": 0, "cascade_skip": 2, "total": 6}
    rows = _by(report["rows"])
    assert rows[("s2", "A")]["verdict"] == "honest_failure"
    assert rows[("s2", "B")]["verdict"] == "honest_failure"


def test_no_worlds_dir_anywhere_is_unknown_not_cleaned(tmp_path):
    """收据与产物顶层都没有 worlds_dir（更老的收据）⇒ 地址不可知：
    cleaned=None（**不判已清理、也不假装仍在**）；世界库不可核的原因写
    「地址不可知」，判据照旧退证据不足。"""
    art = _write_artifact(
        tmp_path, worlds_dir=None, receipts=[],
        failures=[{"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
                   "error": "call_budget_exhausted",
                   "rollback_failed": False}])
    report = k4ar.classify(art)
    w = report["worlds"]
    assert w["source"] == "unknown" and w["worlds_dir"] is None
    assert w["cleaned"] is None, "不可知 ≠ 已清理：不得替缺席编事实"
    assert w["exists"] is False
    assert "不可知" in w["cleaned_basis"]
    assert all(a["locate"] == "no_worlds_dir" for a in w["arms"].values())
    row = _by(report["rows"])[("s2", "A")]
    assert row["verdict"] == "insufficient_evidence"
    assert "地址不可知" in row["note"] and "已清理" not in row["note"]


# ── ③ 已清理的目录如实标注，不伪装成「仍在」 ───────────────────────────────

def test_cleaned_worlds_dir_marked_truthfully(tmp_path):
    """写收据时目录在、报告时已被删 ⇒ cleaned=true + 依据串 + drift；
    世界库不可核原因写「已不存在（cleaned=true）」；pass 行依据也写
    cleaned=True、报告时目录在=False——**任何一处都不许假装仍在**。"""
    wd = tmp_path / "k4_worlds_cleaned"
    wd.mkdir()
    _mk_arm_db(wd, 1, scene="s2", n_calls=6)
    _mk_arm_db(wd, 2, scene="s2", n_calls=6)
    receipts = [{"scene": "s1", "arm": "A", "job_id": "jc-1",
                 "worlds_dir": str(wd.resolve()),
                 "worlds_dir_exists": True, "created_at":
                 "2026-09-26T00:00:00Z", "worlds_dir_cleaned": False,
                 "usage": {"calls": 2, "tokens": 900,
                           "verifier_invalid_retries": 0}},
                {"scene": "s2", "arm": "A", "job_id": "job-s2-A",
                 "worlds_dir": str(wd.resolve()),
                 "worlds_dir_exists": True, "created_at":
                 "2026-09-26T00:00:00Z", "worlds_dir_cleaned": False,
                 "usage": {"calls": 6, "tokens": 0,
                           "verifier_invalid_retries": 0}}]
    art = _write_artifact(
        tmp_path, worlds_dir=wd, receipts=receipts,
        prose=[{"scene": "s1", "arm": "A", "status": "committed"}],
        failures=[{"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
                   "error": "call_budget_exhausted",
                   "rollback_failed": False}])
    shutil.rmtree(wd)                    # 跑完清理：目录没了，收据还指着它
    report = k4ar.classify(art)
    w = report["worlds"]
    assert w["cleaned"] is True, "已清理必须如实标 true"
    assert w["exists"] is False
    assert w["recorded_exists"] is True and w["recorded_cleaned"] is False
    assert "收据记 worlds_dir_exists=true" in w["cleaned_basis"], w
    assert "worlds_dir_exists=true" in w["drift"], w
    assert all(a["readable"] is False for a in w["arms"].values())
    rows = _by(report["rows"])
    assert rows[("s1", "A")]["verdict"] == "pass"     # 判定不因清理而翻
    joined = "".join(rows[("s1", "A")]["basis"])
    assert "cleaned=True" in joined and "报告时目录在=False" in joined, joined
    note = rows[("s2", "A")]["note"]
    assert rows[("s2", "A")]["verdict"] == "insufficient_evidence"
    assert "cleaned=true" in note and "已不存在" in note, note


def test_recorded_cleaned_true_conflict_is_not_smoothed_over(tmp_path):
    """收据记 worlds_dir_cleaned=true、但报告时目录仍在 ⇒ 两个事实如实并列
    （exists=True / recorded_cleaned=True / drift 记矛盾），报告不替任一方
    圆场，也不因为矛盾就崩。"""
    wd = tmp_path / "k4_worlds_conflict"
    wd.mkdir()
    _mk_arm_db(wd, 1, scene="s2", n_calls=6)
    art = _write_artifact(
        tmp_path, worlds_dir=wd,
        receipts=[{"scene": "s2", "arm": "A", "job_id": "job-s2-A",
                   "worlds_dir": str(wd.resolve()),
                   "worlds_dir_exists": True, "worlds_dir_cleaned": True}],
        failures=[{"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
                   "error": "call_budget_exhausted",
                   "rollback_failed": False}])
    report = k4ar.classify(art)
    w = report["worlds"]
    assert w["exists"] is True and w["cleaned"] is False
    assert w["recorded_cleaned"] is True
    assert "矛盾" in w["drift"], w["drift"]
    row = _by(report["rows"])[("s2", "A")]
    assert row["verdict"] == "honest_failure", row


def test_offline_artifact_top_level_dir_reported_as_cleaned(tmp_path,
                                                            monkeypatch):
    """离线跑（收据无 worlds_dir 键，顶层键有）：main() 的 finally 已把临时
    世界目录用后即删 ⇒ 报告必须标 cleaned=true、不把顶层地址说成「仍在」，
    也不因缺收据字段崩。这是离线产物现状的如实对账（不是缺陷判定）。"""
    seed_knowledge()
    out_dir = tmp_path / "out_offline"
    monkeypatch.setattr(sys, "argv", ["k4", "--out", str(out_dir)])
    k4.main()
    art_path = out_dir / "k4_paired.json"
    art = json.loads(art_path.read_text(encoding="utf-8"))
    assert art["live"] is False
    assert not Path(art["worlds_dir"]).exists(), "前提：离线目录已清理"
    report = k4ar.classify(art_path)
    w = report["worlds"]
    assert w["source"] == "top_level" and w["cleaned"] is True
    assert w["exists"] is False
    assert "无法细分" in w["cleaned_basis"], w
    assert report["summary"]["pass"] == 6, report["summary"]
    joined = "".join(report["rows"][0]["basis"])
    assert "cleaned=True" in joined, joined
