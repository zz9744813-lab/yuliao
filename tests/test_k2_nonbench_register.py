"""K2 非基准登记器回归（scripts/k2_nonbench_register.py，2026-09-26）。

**全离线、零真库依赖**：所有用例只在 tmp_path 自建的合成库上跑；真库一个
字节都不碰（判定连接永远 `mode=ro`，preflight 的写只发生在 tempfile 里的
一次性副本，收尾 rmtree）。每条硬契约都有**方向可证伪**的用例：

- C1（缺省 dry-run 零写入）：md5+mtime 逐字节不变；`open_write_connection`
  换成「一调用就炸」后 dry-run/preflight 仍全绿——判定阶段没打开可写连接
  是被**结构**证明的，不是听它自报。
- C2（前缀族 + 显式 role）：拼出的 source_type 必过
  `nonbenchmark_compliant_source`；把该唯一入口换成恒 False，登记计划当场
  拒（证明判据走的是单源函数，不是本地又写一条）；role=benchmark / 空
  各自拒且零写入。
- C3（只增不改）：段 role 显式落非 benchmark、integrity 留 NULL（不自证
  src_ok）；**源码 grep 钉死**本件无 UPDATE/DELETE 语句。
- C4（preflight 对照）：登记前两指标 0（仿形：证据全压 benchmark 段 +
  fixture 来源），登记后（合成库模拟）两指标 >0，且 stdout 同时含
  `k3_reachable_segments` / `a_arm_evidence_count` 字面量；登记后仍 0 ⇒
  rc=2（NO-GO），不许静默绿灯。
- C5（单事务 + 幂等）：中途 IntegrityError ⇒ 三表整体回滚；重跑 = 全
  identical、**一次连接都不开**（boom 替身自证）；内容漂移响亮拒绝、零写。
- C6（零模型调用）：`live_model_client` 换成炸后三种模式全绿；`--live`
  直接 rc=1。

另钉：诚实注记（合成模拟≠K4 解冻，status 门通过数如实 0）、缺库不猜
（候选全缺 ⇒ db_unreadable，bd256c1 同款假红修复：空 --repo-root +
monkeypatch DB_FALLBACK_ABS）。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2nr", ROOT / "scripts" / "k2_nonbench_register.py")
k2nr = _u.module_from_spec(_spec)
sys.modules["k2nr"] = k2nr
_spec.loader.exec_module(k2nr)

k2b = k2nr.k2b                                           # noqa: E402  口径单源
GE = k2nr.GE                                             # noqa: E402
KQ = k2nr.KQ                                             # noqa: E402
from app.db import Base                                   # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,    # noqa: E402
                        StrategyInstance, Work, WorkSource)

SCRIPT_SRC = (ROOT / "scripts" / "k2_nonbench_register.py").read_text(
    encoding="utf-8")
# 纪律 grep 只看代码体：docstring 里合法地**谈** UPDATE/DELETE（说明「无」），
# 不能算违纪——先把模块 docstring 摘掉。
CODE_SRC = SCRIPT_SRC.replace(k2nr.__doc__ or "", "")

TXT_A = "他把没写完的信重新拿起又放下。\n\n窗外起了风，灯芯爆了一下。"
TXT_B = "队伍在泥地里挪，谁也不说话。\n\n哨子响了两声，又是三声。"


# ==================================================== 合成库 / 语料构造
def _boom(_p):
    raise AssertionError("判定/dry-run 阶段打开了可写连接")


def _seed(tmp_path: Path, name="synth.db") -> Path:
    """仿形真库现状：策略卡全 hypothesis；证据全压在 benchmark 段上；
    另有一个 fixture 来源的散段——**登记前**两个指标都该是 0。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    db = tmp_path / name
    eng = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with S() as s:
        for cid in ("ESV2-A", "ESV2-B"):
            s.add(ExpressionStrategyV2(
                id=cid, strategy_key=cid.lower(), version=1,
                abstract_operation=cid, invariants=[],
                effect_hypothesis="x", failure_modes=[], status="hypothesis",
                source="synth", scope="UNCERTAIN", scope_ids=[],
                scope_basis="synth", observation_status="hypothesis",
                effect_status="untested"))
        s.add(Work(id="WK-BENCH", title="基准样书", source="file:synth"))
        s.add(WorkSource(work_id="WK-BENCH", canonical_work_id="WK-BENCH",
                         author_id=None, genre_ids=[],
                         source_type="human_fiction", text_version="corpus-v1",
                         text_sha256="0" * 64, purpose_basis="synth",
                         identity_purposes=["research"], license_purposes=[],
                         allowed_purposes=[], metadata_status="verified",
                         metadata_basis="synth"))
        for n in (0, 1):
            s.add(Segment(id=f"SEG-B{n}", work_id="WK-BENCH", ordinal=n,
                          text=TXT_A, text_clean=TXT_A, n_sentences=2,
                          n_chars=len(TXT_A), role="benchmark"))
        s.add(StrategyInstance(
            id="SI-BENCH", strategy_id="ESV2-A", strategy_version=1,
            work_id="WK-BENCH", segment_id="SEG-B0", frame_id=None,
            text_version="corpus-v1", span_start=0, span_end=len(TXT_A),
            evidence_text=TXT_A, evidence_sha256="0" * 64,
            conditions_observed={}, observed_content="synth",
            extractor_model="synth", reviewer_version="k2def-v1",
            status="verified"))
        s.add(Work(id="WK-FIX", title="测试夹具", source="file:synth"))
        s.add(WorkSource(work_id="WK-FIX", canonical_work_id="WK-FIX",
                         author_id=None, genre_ids=[],
                         source_type="fixture", text_version="corpus-v1",
                         text_sha256="0" * 64, purpose_basis="synth",
                         identity_purposes=[], license_purposes=[],
                         allowed_purposes=[], metadata_status="verified",
                         metadata_basis="synth"))
        s.add(Segment(id="SEG-F0", work_id="WK-FIX", ordinal=0, text=TXT_B,
                      text_clean=TXT_B, n_sentences=2, n_chars=len(TXT_B),
                      role=None))
        s.commit()
    eng.dispose()
    return db


def _corpus(tmp_path: Path, name="corpus") -> Path:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "book-甲.txt").write_text(TXT_A, encoding="utf-8")
    (d / "book-乙.txt").write_text(TXT_B, encoding="utf-8")
    return d


def _digest(db) -> str:
    return hashlib.md5(Path(db).read_bytes()).hexdigest()


def _mt(db) -> int:
    return os.stat(db).st_mtime_ns


def _rows(db, table, cols="*", where=""):
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            f"SELECT {cols} FROM {table}{where}").fetchall()
    finally:
        con.close()


def _main(db, corpus, *argv):
    return k2nr.main(["--db", str(db), "--corpus", str(corpus),
                      "--tag", "k2nb", *argv])


def _commit(db, corpus, extra=()):
    return _main(db, corpus, "--commit", "--reviewer", "R1", *extra)


# ============================================== C1：缺省 dry-run 零写入
def test_dry_run_is_default_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """契约 C1：不带模式项即 dry-run——md5+mtime 逐字节不变，且全程没有
    打开可写连接（入口换成「一调用就炸」来结构性证明）。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    dig, mt = _digest(db), _mt(db)
    monkeypatch.setattr(k2nr, "open_write_connection", _boom)
    assert _main(db, c) == k2nr.EXIT_OK
    assert _digest(db) == dig and _mt(db) == mt
    out = capsys.readouterr().out
    assert "mode=dry-run" in out and "planned_writes=" in out


def test_preflight_never_opens_real_write_connection(tmp_path, monkeypatch):
    """契约 C1：preflight 的写只发生在 tempfile 副本——真库写入口换炸后
    preflight 仍全绿。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    dig = _digest(db)
    monkeypatch.setattr(k2nr, "open_write_connection", _boom)
    assert _main(db, c, "--preflight") == k2nr.EXIT_OK
    assert _digest(db) == dig


# ============================================ C4：登记前/后对照两行
def test_preflight_contrast_before_zero_after_positive(tmp_path, capsys):
    """契约 C4：仿形库登记前两指标 0；「登记后（合成库模拟）」两指标 >0；
    stdout 两行都含字面量 `k3_reachable_segments` / `a_arm_evidence_count`。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    assert _main(db, c, "--preflight") == k2nr.EXIT_OK
    out = capsys.readouterr().out
    assert "登记前" in out and "登记后（合成库模拟）" in out
    before = next(ln for ln in out.splitlines() if "登记前" in ln)
    after = next(ln for ln in out.splitlines() if "登记后" in ln)
    assert "k3_reachable_segments=0" in before
    assert "a_arm_evidence_count=0" in before
    reach = int(after.split("k3_reachable_segments=")[1].split()[0])
    a_arm = int(after.split("a_arm_evidence_count=")[1].split()[0])
    assert reach > 0 and a_arm > 0
    assert "PRECHECK-PASS" in out


def test_preflight_refuses_when_after_stays_zero(tmp_path, capsys):
    """契约 C4（可证伪方向）：登记后指标仍 0 ⇒ rc=2 + NO-GO，不静默绿灯。
    触发法：把段文本版本登记成 K3 不允许的值（text_version 门拦一切）。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    bad_tv = "test-fixture-v0"
    assert bad_tv not in KQ.DEFAULT_ALLOWED_TEXT_VERSIONS
    dig, mt = _digest(db), _mt(db)
    assert _main(db, c, "--preflight", "--text-version", bad_tv) \
        == k2nr.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "NO-GO" in out and "after_zero" in out
    assert _digest(db) == dig and _mt(db) == mt


def test_preflight_note_is_honest_about_scope(tmp_path, monkeypatch):
    """诚实边界：A 臂非空 = 合成库模拟；status 门通过数如实 0——本件不
    产出任何「K4 解冻」含义。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    rep = tmp_path / "r.json"
    assert _main(db, c, "--preflight", "--json-out", str(rep)) == k2nr.EXIT_OK
    r = json.loads(rep.read_text(encoding="utf-8"))
    pf = r["preflight"]
    assert pf["after"]["a_arm_evidence_count"] > 0
    assert pf["before"]["n_k3_status_gate_pass_strategies"] == 0
    assert pf["after"]["n_k3_status_gate_pass_strategies"] == 0
    assert "不构成" in pf["note"] and "K4" in pf["note"]
    assert "合成库模拟" in pf["note"]


# ============================================== C2：前缀族 + 显式 role
def test_source_type_is_prefix_family_and_single_sourced(tmp_path,
                                                         monkeypatch, capsys):
    """契约 C2：拼出的 source_type 以 production_nonbenchmark_ 前缀族开头，
    且**过唯一判定入口**——把入口换成恒 False，计划当场拒（证明没有第二
    支笔可以绕）。"""
    plan = k2nr.build_plan([], tag="k2nb", role="train",
                           text_version="corpus-v1", basis="t")
    assert plan["source_type"].startswith("production_nonbenchmark_")
    assert k2b.nonbenchmark_compliant_source(plan["source_type"])

    db, c = _seed(tmp_path), _corpus(tmp_path)
    dig = _digest(db)
    monkeypatch.setattr(k2nr.k2b, "nonbenchmark_compliant_source",
                        lambda st: False)
    assert _main(db, c) == k2nr.EXIT_REFUSED
    assert "NO-GO" in capsys.readouterr().out
    assert _digest(db) == dig
    monkeypatch.undo()
    assert _main(db, c) == k2nr.EXIT_OK                    # 恢复后照常


def test_role_benchmark_or_empty_refused(tmp_path, capsys):
    """契约 C2：role 显式非空且 != benchmark，否则拒登记且零写入。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    dig, mt = _digest(db), _mt(db)
    assert _main(db, c, "--role", "benchmark") == k2nr.EXIT_REFUSED
    assert _main(db, c, "--role", "") == k2nr.EXIT_REFUSED
    out = capsys.readouterr().out
    assert out.count("NO-GO") == 2
    assert _digest(db) == dig and _mt(db) == mt


# ==================================== C3：只增不改（增量行 + 诚实 integrity）
def test_registered_rows_are_additive_and_honest(tmp_path):
    """登记落库后的行形态：段 role 显式 train、integrity NULL（不自证
    src_ok）、来源类型前缀族、license_purposes 空（无授权不填）；
    既有 benchmark 段一字节未动（只增不改）。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    n_work0 = len(_rows(db, "works"))
    bench0 = _rows(db, "segments", "id, role, integrity, text",
                   " WHERE id LIKE 'SEG-B%'")
    assert _commit(db, c) == k2nr.EXIT_OK
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    nb = con.execute("SELECT id, work_id, role, integrity FROM segments"
                     " WHERE work_id NOT IN ('WK-BENCH','WK-FIX')"
                     " ORDER BY id").fetchall()
    lic = con.execute("SELECT source_type, license_purposes, author_id,"
                      " metadata_status FROM work_sources"
                      " WHERE source_type LIKE 'production_nonbenchmark_%'"
                      ).fetchall()
    bench1 = con.execute("SELECT id, role, integrity, text FROM segments"
                         " WHERE id LIKE 'SEG-B%'").fetchall()
    con.close()
    assert len(nb) == 4                                    # 2 文件 × 2 段
    assert {r[2] for r in nb} == {"train"}                 # role 显式非空
    assert all(r[3] is None for r in nb)                   # integrity 留 NULL
    assert len(lic) == 2
    assert all(json.loads(r[1]) == [] and r[2] is None
               and r[3] == "unverified" for r in lic)
    assert bench1 == bench0                                # 既有行原样
    assert len(_rows(db, "works")) == n_work0 + 2


def test_no_update_delete_write_paths_in_source():
    """契约 C3：源码纪律——本件代码体没有任何 UPDATE/DELETE 语句
    （只 INSERT；docstring 谈「无 UPDATE/DELETE」是说明，不算）。"""
    import re
    assert not re.search(r"\bUPDATE\s+\w+", CODE_SRC), "出现 UPDATE 写路径"
    assert not re.search(r"DELETE\s+FROM", CODE_SRC), "出现 DELETE 写路径"
    assert CODE_SRC.count("INSERT INTO") >= 4              # 三表 + 探针
    assert "ROLLBACK" in CODE_SRC and "BEGIN IMMEDIATE" in CODE_SRC


# ==================================================== C5：单事务 + 幂等
def test_commit_changes_exactly_the_new_rows(tmp_path):
    db, c = _seed(tmp_path), _corpus(tmp_path)
    w0, s0, r0 = (len(_rows(db, t)) for t in
                  ("works", "segments", "work_sources"))
    assert _commit(db, c) == k2nr.EXIT_OK
    assert len(_rows(db, "works")) == w0 + 2
    assert len(_rows(db, "segments")) == s0 + 4
    assert len(_rows(db, "work_sources")) == r0 + 2
    # 探针实例不落真库：strategy_instances 只多不少——登记器自己不插证据
    assert len(_rows(db, "strategy_instances")) == 1


def test_rollback_when_midwrite_conflict(tmp_path, monkeypatch):
    """契约 C5 中途失败：事务内撞唯一键 ⇒ 三表整体回滚，一字节不多。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    monkeypatch.setattr(k2nr, "segment_id_for",
                        lambda w, o, t: "SEG-DUP")   # 全库同 id ⇒ 秒撞主键
    before = tuple(len(_rows(db, t)) for t in
                   ("works", "segments", "work_sources",
                    "strategy_instances"))
    assert _commit(db, c) == k2nr.EXIT_ERROR
    after = tuple(len(_rows(db, t)) for t in
                  ("works", "segments", "work_sources",
                   "strategy_instances"))
    assert before == after                                 # 整体回滚


def test_recommit_is_idempotent_and_opens_no_connection(tmp_path, monkeypatch,
                                                        capsys):
    """契约 C5 幂等强形态：同内容重跑 = 全 identical、**一次可写连接都不开**
    （入口换炸仍绿），库文件逐字节不变。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    assert _commit(db, c) == k2nr.EXIT_OK
    dig, mt = _digest(db), _mt(db)
    monkeypatch.setattr(k2nr, "open_write_connection", _boom)
    capsys.readouterr()
    assert _commit(db, c) == k2nr.EXIT_OK                  # _boom 未被触发
    assert "skipped_identical=2" in capsys.readouterr().out
    assert _digest(db) == dig and _mt(db) == mt


def test_content_drift_refused_loudly(tmp_path, capsys):
    """契约 C5（K1-A 锚纪律）：同 work 键内容变了 ⇒ drift_conflict 响亮
    拒绝（--commit 与 --preflight 都拒），不静默重锚、零写入。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    assert _commit(db, c) == k2nr.EXIT_OK
    dig, mt = _digest(db), _mt(db)
    (c / "book-甲.txt").write_text(TXT_A + "\n\n被人改过一句。",
                                   encoding="utf-8")
    capsys.readouterr()
    assert _commit(db, c) == k2nr.EXIT_REFUSED
    assert "drift_conflict" in capsys.readouterr().out
    assert _main(db, c, "--preflight") == k2nr.EXIT_REFUSED
    assert _digest(db) == dig and _mt(db) == mt


# ==================================================== C6：零模型调用
def test_zero_model_calls_and_live_refused(tmp_path, monkeypatch, capsys):
    """契约 C6：三种模式全绿 ≠ 碰过模型入口；`--live` 直接拒且零写入。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    dig = _digest(db)
    monkeypatch.setattr(k2nr, "live_model_client", _boom)
    assert _main(db, c) == k2nr.EXIT_OK
    assert _main(db, c, "--preflight") == k2nr.EXIT_OK
    assert _commit(db, c) == k2nr.EXIT_OK
    assert _main(db, c, "--live") == k2nr.EXIT_ERROR
    assert "live_refused" in capsys.readouterr().out
    assert len(_rows(db, "works")) >= 3                    # 无异常即未炸


# ============================================ 单源与不猜（判据函数级）
def test_metrics_route_through_single_source_gates(tmp_path, monkeypatch):
    """可达段指标必须经 `k2b._k3_source_gate`：把它换成恒给拒因，
    reachable 立刻归零；换成恒过闸，则连 benchmark/fixture 段都算可达
    ——两个方向都证明指标取的是该函数，不是本地另写一套。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    assert k2nr.compute_metrics(db)["k3_reachable_segments"] == 0
    monkeypatch.setattr(k2nr.k2b, "_k3_source_gate",
                        lambda seg, tv, *, work_source=None: "boom")
    assert k2nr.compute_metrics(db)["k3_reachable_segments"] == 0
    monkeypatch.undo()
    monkeypatch.setattr(k2nr.k2b, "_k3_source_gate",
                        lambda seg, tv, *, work_source=None: None)
    assert k2nr.compute_metrics(db)["k3_reachable_segments"] == 3


def test_benchmark_evidence_never_counts(tmp_path):
    """方向钉死（C3 语义）：证据全压 benchmark 段 ⇒ 登记前 a_arm 必 0；
    benchmark 剔除走的是 `_evidence_for` 本尊，不是登记器的话术。"""
    db, c = _seed(tmp_path), _corpus(tmp_path)
    m = k2nr.compute_metrics(db)
    assert m["a_arm_evidence_count"] == 0
    assert m["segments_total"] == 3
    s = k2nr.open_ro_session(db)
    refs, n, stripped = KQ._evidence_for(s, "ESV2-A", {})
    s.close()
    assert n == 0 and stripped == ["SI-BENCH:benchmark_source"]


def test_missing_db_reports_unreadable_not_guess(tmp_path, monkeypatch,
                                                 capsys):
    """缺库不猜（bd256c1 假红修复同款）：--db 缺省 ⇒ 候选**全部**不存在
    （空 --repo-root + 顶掉兜底绝对路径）才判 db_unreadable——不许在主仓
    cwd 误开真库。"""
    empty = tmp_path / "empty-root"
    empty.mkdir()
    monkeypatch.setattr(GE, "DB_FALLBACK_ABS",
                        str(tmp_path / "no-such" / "x.db"))
    assert k2nr.main(["--corpus", str(tmp_path), "--tag", "k2nb",
                      "--repo-root", str(empty)]) == k2nr.EXIT_ERROR
    out = capsys.readouterr().out
    assert "db_unreadable" in out and "NO-GO" in out
