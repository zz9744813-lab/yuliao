"""K5 晋升写侧执行器回归（scripts/k5_promotion_write.py，S4）。

**全离线、零真库依赖**：所有用例只在 tmp_path 里自建的**合成库**上写；真库
一个字节都不碰（判定连接永远是 `mode=ro`，写路径只对本目录副本打开）。
每条硬契约都有**方向可证伪**的用例（不是只测「全红」）：

- 契约 1（缺省即 `--dry-run`，零写入）：md5 + mtime 逐字节不变；并且把
  `open_write_connection` 换成「一调用就炸」——dry-run 跑通即证明进程**没有**
  打开可写连接（不是只信它自己打印的话）。批量升格（选择集 ≠1）与无签认
  （`--reviewer` 空）都在写之前被拦。
- 契约 2（拒绝语义 + 判词同函数同源）：空证据 ⇒ `exit 2` 且 stdout 含字面量
  `NO-PROMOTE`；**同一 reviewer 下 dry-run 报告与 commit 报告逐字节相同**，
  真写前打印的「复检判词」也等于该串——判词只有 `evaluate()`/`line_of()`
  一个产出点，没有「各写自己一套」的第二分支。
- 契约 3（单事务）：`--commit` 恰好改 1 行策略 + 写 1 行审计；两种中途失败
  （审计主键撞键重放 / CAS 旧值失配 → rowcount=0）都**整体回滚**，两表
  内容与回滚前逐字节一致。
- 契约 4（禁跳级）：`--to verified`（从 hypothesis 起跨 2 级）判 `skip_ladder`
  且点名被跨过的级、库不变；降级/同级/契约外取值各自判词；反向还有**四级
  阶梯全走通**的可证伪路径——终态 `status=verified` 且 `scope≠UNCERTAIN`，
  `KQ.query_knowledge` 从「选不出它」变成**选中它**（K3 合格集 0→≥1 的最小闭环）。
- 契约 5（审计行不可变）：字段含 `from_status/to_status/evidence_ref/ts`；
  `verify_promotion_audits()` 只读判 ok=True；DB 层触发器阻断 UPDATE/DELETE；
  **源码 grep 钉死**本件没有 UPDATE/DELETE `promotion_audits` 的写路径；
  手工插一条坏审计行（空 evidence_ref + 跨级）校验函数必须点名（fail-visible）。

另钉：门判词与 `k5_promotion_gate_explain.explain_strategy` 的 gate1..gate4
逐字一致（单源）、`src_ok` 严格布尔（`"true"` 字符串不算）、新口径复审标记、
独立根作品复现、基准段永不计数、scope 逐级截断与 `GLOBAL` 不写、缺登记不猜。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k5w", ROOT / "scripts" / "k5_promotion_write.py")
k5w = _u.module_from_spec(_spec)
sys.modules["k5w"] = k5w
_spec.loader.exec_module(k5w)

GE = k5w.GE                                             # noqa: E402  判词单源
KQ = k5w.KQ                                             # noqa: E402
from app import knowledge as K                           # noqa: E402
from app import knowledge_extract as KE                  # noqa: E402
from app.db import Base                                  # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,   # noqa: E402
                        StrategyInstance, Work, WorkSource)

TXT = "夜里起了风，他把没写完的信重新拿起又放下。"
OK_INT = json.dumps({"src_ok": True})
SCRIPT_SRC = (ROOT / "scripts" / "k5_promotion_write.py").read_text(
    encoding="utf-8")
# 文档串里合法地提到 `BEGIN IMMEDIATE` / `eligible_statuses` 等词（说明性引用），
# 源码纪律断言只看**代码体**，所以把 docstring 摘掉再 grep。
CODE_SRC = SCRIPT_SRC.replace(k5w.__doc__ or "", "")


# ==================================================== 合成库构造
def _card(id, key, *, status="hypothesis", obs="hypothesis", version=1,
          scope="UNCERTAIN", scope_ids=(), effect="untested"):
    return dict(id=id, key=key, status=status, obs=obs, version=version,
                scope=scope, scope_ids=list(scope_ids), effect=effect)


def _work(id, *, canonical=None, author="AU-1", genres=("g1",),
          source_type="human_fiction", tv="corpus-v1", integrity=OK_INT,
          role=None):
    return dict(id=id, canonical=canonical or id, author=author,
                genres=list(genres), source_type=source_type, tv=tv,
                integrity=integrity, role=role)


def _inst(id, strategy_id, work_id, *, span=(0, 10), tv="corpus-v1",
          status="verified", reviewer=KE.REVIEW_MARKER_NEW_DEF):
    return dict(id=id, strategy_id=strategy_id, work_id=work_id, span=span,
                tv=tv, status=status, reviewer=reviewer)


def _mk(tmp_path: Path, cards, works, instances, *, name="synth.db"):
    """自建合成库（ORM 建表 + 插行）——本文件只认 tmp_path 里这一份。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    db = tmp_path / name
    eng = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with S() as s:
        for w in works:
            s.add(Work(id=w["id"], title=w["id"], source="file:synth"))
            s.add(Segment(id=f"SG-{w['id']}", work_id=w["id"], ordinal=0,
                          text=TXT, role=w["role"], n_sentences=1,
                          n_chars=len(TXT), integrity=w["integrity"]))
            s.add(WorkSource(work_id=w["id"], canonical_work_id=w["canonical"],
                             author_id=w["author"], genre_ids=w["genres"],
                             source_type=w["source_type"], text_version=w["tv"],
                             text_sha256="0" * 64, purpose_basis="synth",
                             identity_purposes=["research"], license_purposes=[],
                             metadata_status="verified", metadata_basis="synth"))
        for c in cards:
            s.add(ExpressionStrategyV2(
                id=c["id"], strategy_key=c["key"], version=c["version"],
                abstract_operation=c["key"], invariants=[],
                effect_hypothesis="x", failure_modes=[], status=c["status"],
                source="synth", scope=c["scope"], scope_ids=c["scope_ids"],
                scope_basis="synth", observation_status=c["obs"],
                effect_status=c["effect"]))
        for i in instances:
            s.add(StrategyInstance(
                id=i["id"], strategy_id=i["strategy_id"], strategy_version=1,
                work_id=i["work_id"], segment_id=f"SG-{i['work_id']}",
                frame_id=None, text_version=i["tv"], span_start=i["span"][0],
                span_end=i["span"][1], evidence_text=TXT,
                evidence_sha256="0" * 64, conditions_observed={},
                observed_content="synth", extractor_model="synth",
                reviewer_version=i["reviewer"], status=i["status"]))
        s.commit()
    eng.dispose()
    return db


def _promotable(tmp_path: Path, *, key="t-restrain", card_id="ESV2-T",
                name="synth.db", **kw):
    """**可晋升**的标准场景：2 部登记作品、各 1 段 `src_ok=true` 且已按新口径
    复审的证据，策略卡在 `hypothesis` 起点、`scope=UNCERTAIN`。"""
    cards = kw.pop("cards", None)
    works = kw.pop("works", None)
    insts = kw.pop("instances", None)
    if cards is None:
        cards = [_card(card_id, key, **kw)]
    if works is None:
        works = [_work("WK-A", canonical="CW-A", author="AU-1", genres=["g1"]),
                 _work("WK-B", canonical="CW-B", author="AU-1", genres=["g1"])]
    if insts is None:
        insts = [_inst("SI-1", card_id, "WK-A", span=(0, 10)),
                 _inst("SI-2", card_id, "WK-B", span=(0, 10))]
    return _mk(tmp_path, cards, works, insts, name=name)


def _rows(db, table, cols="*", where=""):
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            f"SELECT {cols} FROM {table}{where}").fetchall()
    finally:
        con.close()


def _digest(db) -> str:
    return hashlib.md5(Path(db).read_bytes()).hexdigest()


def _card_row(db, card_id="ESV2-T"):
    """(status, observation_status, scope, scope_ids, scope_basis)。"""
    return _rows(db, "expression_strategies_v2",
                 "status, observation_status, scope, scope_ids, scope_basis",
                 f" WHERE id='{card_id}'")[0]


def _verdict(db, card_id="ESV2-T", to=None, reviewer="R1"):
    s = k5w.open_ro_session(db)
    try:
        st = s.query(ExpressionStrategyV2).filter_by(id=card_id).one()
        return k5w.evaluate(s, st, {}, to, reviewer)
    finally:
        s.close()


def _main(db, *argv):
    return k5w.main(["--db", str(db), *argv])


def _commit(db, card_id="ESV2-T", reviewer="R1", to=None):
    argv = ["--commit", "--strategy", card_id, "--reviewer", reviewer]
    return _main(db, *(argv + (["--to", to] if to else [])))


# ============================================== 契约 1：缺省零写入
def test_dry_run_is_default_and_writes_nothing(tmp_path, monkeypatch):
    """契约 1：不带 `--commit` 即 dry-run——**库文件 md5 与 mtime 都不变**；
    且判定阶段一次都没调用可写连接入口（把入口换成「一调用就炸」来证）。"""
    db = _promotable(tmp_path)
    dig, mt = _digest(db), os.stat(db).st_mtime_ns

    def boom(_p):
        raise AssertionError("dry-run 打开了可写连接")

    monkeypatch.setattr(k5w, "open_write_connection", boom)
    assert _main(db, "--dry-run") == k5w.EXIT_OK      # 前置齐 ⇒ PROMOTE ⇒ rc=0
    assert _digest(db) == dig and os.stat(db).st_mtime_ns == mt
    assert _card_row(db)[:3] == ("hypothesis", "hypothesis", "UNCERTAIN")


def test_dry_run_is_the_default_mode(tmp_path, capsys):
    """缺省（不带模式项）即 `--dry-run`：报告头写着 mode=dry-run、零写入。"""
    db = _promotable(tmp_path)
    dig = _digest(db)
    capsys.readouterr()
    assert _main(db) == k5w.EXIT_OK
    assert "mode=dry-run" in capsys.readouterr().out
    assert _digest(db) == dig


def test_commit_without_reviewer_refused_before_write(tmp_path):
    """无签认不写：`--commit` 缺 `--reviewer` ⇒ rc=1 且库一字节不变。"""
    db = _promotable(tmp_path)
    dig = _digest(db)
    assert _main(db, "--commit", "--strategy", "ESV2-T") == k5w.EXIT_ERROR
    assert _digest(db) == dig
    assert _card_row(db)[:3] == ("hypothesis", "hypothesis", "UNCERTAIN")
    assert _rows(db, "expression_strategies_v2", "COUNT(*)") == [(1,)]


def test_batch_promotion_refused(tmp_path):
    """契约 1 + U2：`--commit` 选择集 ≠1 条即拒（批量升格任何读数下不解冻）。"""
    db = _promotable(
        tmp_path,
        cards=[_card("ESV2-T", "t-restrain"), _card("ESV2-U", "u-restrain")],
        instances=[_inst("SI-1", "ESV2-T", "WK-A"), _inst("SI-2", "ESV2-T", "WK-B"),
                   _inst("SI-3", "ESV2-U", "WK-A"), _inst("SI-4", "ESV2-U", "WK-B")])
    dig = _digest(db)
    assert _main(db, "--commit", "--reviewer", "R1") == k5w.EXIT_ERROR
    assert _digest(db) == dig
    assert [r[0] for r in _rows(db, "expression_strategies_v2", "id")] == \
        ["ESV2-T", "ESV2-U"]
    for cid in ("ESV2-T", "ESV2-U"):
        assert _card_row(db, cid)[:3] == ("hypothesis", "hypothesis", "UNCERTAIN")


def test_dry_run_and_commit_flags_are_mutually_exclusive(tmp_path):
    db = _promotable(tmp_path)
    dig = _digest(db)
    assert _main(db, "--dry-run", "--commit", "--reviewer", "R") == k5w.EXIT_ERROR
    assert _digest(db) == dig


# ================================ 契约 2：拒绝语义 + 判词单源同函数
def test_empty_evidence_refuses_exit2_with_literal(tmp_path, capsys):
    """契约 2：证据为空 ⇒ **非零退出（约定 2）**且 stdout 含字面量 `NO-PROMOTE`。"""
    db = _promotable(tmp_path, instances=[])
    dig = _digest(db)
    # 判词本体钉死：拒因必须**逐字等于** gate3 解释器给的那句
    # （不是「顺带包含」——否则删掉本分支、改由后一条弱规则兜底也测不出来）。
    v = _verdict(db)
    assert v["decision"] == k5w.NO_PROMOTE
    assert v["reason"] == "excluded_no_evidence" == \
        v["gates"]["gate3_evidence"]
    assert "reason=excluded_no_evidence " in v["line"]
    capsys.readouterr()
    assert _main(db, "--dry-run") == k5w.EXIT_REFUSED == 2
    out = capsys.readouterr().out
    assert "NO-PROMOTE" in out
    assert "reason=excluded_no_evidence " in out        # 判词字面量取自库/解释器
    assert "note=净剩 0 段" in out
    assert _digest(db) == dig
    # --commit 档同一拒因、同一退出码（拒因由同一函数产出，两档不分叉）
    capsys.readouterr()
    assert _commit(db) == k5w.EXIT_REFUSED == 2
    assert "reason=excluded_no_evidence " in capsys.readouterr().out
    assert _digest(db) == dig


def test_verdict_line_identical_between_dry_run_and_commit(tmp_path):
    """契约 2：同一 reviewer 下 dry-run 与 commit 的判词**逐字节相同**
    （只有 `line_of` 一个渲染点，不存在各写一套的第二分支）。"""
    db = _promotable(tmp_path)
    dig = _digest(db)
    dr = k5w.build_report(db, tmp_path, {}, ["ESV2-T"], None, "R1", "dry-run")
    cm = k5w.build_report(db, tmp_path, {}, ["ESV2-T"], None, "R1", "commit")
    assert [v["line"] for v in dr["verdicts"]] == [v["line"] for v in cm["verdicts"]]
    assert dr["verdicts"][0]["decision"] == k5w.PROMOTE
    assert dr["discipline"]["db_writes_during_judgement"] == 0
    assert dr["discipline"]["planned_writes"] == 0
    assert cm["discipline"]["planned_writes"] == 1     # 只是计划；判定不写
    assert _digest(db) == dig                          # commit 档判定也不写


def test_commit_recheck_prints_the_same_line(tmp_path, capsys):
    """真写前的「复检判词」等于 dry-run 判词（同一 `evaluate` 产出）。"""
    db = _promotable(tmp_path)
    dr = k5w.build_report(db, tmp_path, {}, ["ESV2-T"], None, "R1", "dry-run")
    capsys.readouterr()
    assert _commit(db) == k5w.EXIT_OK
    out = capsys.readouterr().out
    assert dr["verdicts"][0]["line"] in out
    assert "复检判词（与 dry-run 同一函数产出）" in out


def test_gate_reasons_verbatim_from_explainer(tmp_path):
    """单源：写侧判词里的 gate1..gate4 = 解释器 `explain_strategy` 的原话。"""
    db = _promotable(tmp_path, cards=[_card("ESV2-T", "t-restrain"),
                                      _card("ESV2-G", "g-bad", obs="nope",
                                            scope="GLOBAL")])
    s = k5w.open_ro_session(db)
    try:
        for st in s.query(ExpressionStrategyV2).order_by(
                ExpressionStrategyV2.id):
            exp = GE.explain_strategy(s, st, {})
            v = k5w.evaluate(s, st, {}, None, "R1")
            for g in GE.GATE_ORDER:
                assert v["gates"][g] == exp["gates"][g]["reason"], (st.id, g)
            assert v["blocked_at"] == exp["blocked_at"]
            assert (v["evidence"]["evidence_count"]
                    == exp["gates"]["gate3_evidence"]["evidence_count"])
            if v["decision"] == k5w.NO_PROMOTE:       # 拒绝行里五门判词逐字可核
                for g in GE.GATE_ORDER:
                    assert f"{g}={v['gates'][g]}" in v["line"]
    finally:
        s.close()


# ============================================== 契约 3：单事务单行
def test_commit_changes_exactly_one_row_and_one_audit(tmp_path):
    db = _promotable(tmp_path)
    assert _commit(db) == k5w.EXIT_OK
    assert _card_row(db)[:3] == ("hypothesis", "observed", "WORK")
    aud = _rows(db, k5w.AUDIT_TABLE, "audit_id, strategy_id")
    assert len(aud) == 1 and aud[0][1] == "ESV2-T"    # 审计行数 == 变更行数 == 1
    assert _rows(db, "expression_strategies_v2", "COUNT(*)") == [(1,)]


def test_rollback_when_audit_replay_collides(tmp_path):
    """契约 3 中途失败：同一次变迁重放撞审计主键 ⇒ **两表整体回滚**。"""
    db = _promotable(tmp_path)
    assert _commit(db) == k5w.EXIT_OK
    # 人为把策略行退回起点（＝把同一次变迁再执行一遍）⇒ 审计主键必撞
    con = sqlite3.connect(db.as_posix())
    con.execute("UPDATE expression_strategies_v2 SET observation_status="
                "'hypothesis', scope='UNCERTAIN', scope_ids='[]' WHERE id='ESV2-T'")
    con.commit()
    cards, aud = _rows(db, "expression_strategies_v2", "*"), \
        _rows(db, k5w.AUDIT_TABLE, "*")
    con.close()
    assert len(aud) == 1
    assert _commit(db) == k5w.EXIT_ERROR
    assert _rows(db, "expression_strategies_v2", "*") == cards
    assert _rows(db, k5w.AUDIT_TABLE, "*") == aud     # 没多出第二条审计


def test_rollback_when_cas_guard_sees_zero_rows(tmp_path):
    """契约 3：CAS 旧值失配（rowcount=0）⇒ PromotionGuardError + 整体回滚。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    db = _promotable(tmp_path)
    eng = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    with S() as s:
        st = s.query(ExpressionStrategyV2).filter_by(id="ESV2-T").one()
        verdict = k5w.evaluate(s, st, {}, None, "R1")
    s.close()
    eng.dispose()
    assert verdict["decision"] == k5w.PROMOTE
    verdict["status"] = "retired"                     # 事务内实读与前置不符
    cards = _rows(db, "expression_strategies_v2", "*")
    with pytest.raises(k5w.PromotionGuardError):
        k5w.commit_promotion(db, verdict)
    assert _rows(db, "expression_strategies_v2", "*") == cards
    assert _rows(db, k5w.AUDIT_TABLE) == []           # 表已建（DDL）但零行


# ============================================== 契约 4：禁跳级
def test_skip_ladder_refused_with_skipped_rungs(tmp_path, capsys):
    """契约 4：`hypothesis → verified` 跨 2 级 ⇒ 点名被跨过的级、零写入。"""
    db = _promotable(tmp_path)
    dig = _digest(db)
    capsys.readouterr()
    assert _main(db, "--dry-run", "--strategy", "ESV2-T", "--to", "verified") \
        == k5w.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "NO-PROMOTE" in out and "skip_ladder" in out
    assert "observed" in out and "replicated" in out   # 被跨过的级逐条点名
    assert _digest(db) == dig


def test_same_level_and_demotion_and_off_ladder_target_refused(tmp_path):
    db = _promotable(tmp_path)
    assert _verdict(db, to="hypothesis")["reason"].startswith("no_step:")
    assert _verdict(db, to="retired")["reason"] == "off_ladder:to=retired"
    assert _commit(db) == k5w.EXIT_OK
    down = _verdict(db, to="hypothesis")
    assert down["reason"].startswith("not_promotion:")        # 降级不是晋升
    assert down["level"] == "observed"
    jump = _verdict(db, to="verified")
    assert jump["reason"].startswith("skip_ladder:observed")
    assert jump["skipped"] == ["replicated"]


def test_top_of_ladder_refused(tmp_path):
    """已在阶梯顶 ⇒ `already_top`：本件不越权推效果层/推广范围。"""
    db = _promotable(tmp_path, status="verified", obs="replicated",
                    scope="WORK", scope_ids=["WK-A"])
    v = _verdict(db)
    assert v["decision"] == k5w.NO_PROMOTE
    assert v["reason"].startswith("already_top:verified")


def test_full_ladder_walk_ends_k3_eligible(tmp_path):
    """四级阶梯逐级走通 ⇒ 终态被 `KQ.query_knowledge` **选中**（K3 合格集 0→≥1）。"""
    db = _promotable(tmp_path)
    s = k5w.open_ro_session(db)
    qr0 = KQ.query_knowledge({"book_id": "WK-A"}, s)
    s.close()
    assert "t-restrain" not in {e["strategy_key"] for e in qr0["selected"]}

    steps = [(None, ("hypothesis", "observed", "WORK")),
             ("replicated", ("hypothesis", "replicated", "AUTHOR")),
             ("verified", ("verified", "replicated", "GENRE"))]
    for n, (tgt, exp) in enumerate(steps, 1):
        assert _commit(db, to=tgt) == k5w.EXIT_OK, tgt
        assert _card_row(db)[:3] == exp, tgt
        assert len(_rows(db, k5w.AUDIT_TABLE)) == n, tgt        # 一步一条审计
    # 三步三条审计、to_status 恰是阶梯前缀（逐级不跳）
    assert [r[0] for r in _rows(db, k5w.AUDIT_TABLE, "to_status")] == \
        ["observed", "replicated", "verified"]

    integ = k5w.verify_promotion_audits(db)
    assert integ["ok"] is True and integ["n_audits"] == 3
    assert integ["n_strategies_verified"] == 1
    s = k5w.open_ro_session(db)
    qr = KQ.query_knowledge({"book_id": "WK-A"}, s)
    s.close()
    assert {e["strategy_key"] for e in qr["selected"]} == {"t-restrain"}
    assert qr["status"] == "matched"
    assert qr["selected"][0]["status"] == "verified"
    assert qr["selected"][0]["scope"] != "UNCERTAIN"


# ============================================== 契约 5：审计不可变
def test_audit_row_fields_and_readonly_verification(tmp_path):
    """契约 5：审计行含 `from_status/to_status/evidence_ref/ts`（+门版本/签认）。"""
    db = _promotable(tmp_path)
    assert _commit(db) == k5w.EXIT_OK
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    a = dict(con.execute(f"SELECT * FROM {k5w.AUDIT_TABLE}").fetchone())
    con.close()
    assert a["from_status"] == "hypothesis" and a["to_status"] == "observed"
    assert json.loads(a["evidence_ref"]) == ["SI-1", "SI-2"]
    assert a["reviewer"] == "R1" and a["tool"] == k5w.TOOL
    assert a["gate_version"] == k5w.GATE_VERSION
    assert a["scope_rule_version"] == k5w.SCOPE_RULE_VERSION
    assert json.loads(a["scope_ids"]) == ["WK-A", "WK-B"]
    assert k5w.SCOPE_RULE_VERSION in a["scope_basis"]
    datetime.fromisoformat(a["ts"])                   # 可解析的入账时刻
    out = k5w.verify_promotion_audits(db)
    assert out["ok"] is True and out["db_mode"] == "ro" and out["n_audits"] == 1


def test_audit_table_blocks_update_and_delete(tmp_path):
    """契约 5：append-only 在 **DB 层**成立（触发器），不靠自觉。"""
    db = _promotable(tmp_path)
    assert _commit(db) == k5w.EXIT_OK
    con = sqlite3.connect(db.as_posix())
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(f"UPDATE {k5w.AUDIT_TABLE} SET to_status='verified'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(f"DELETE FROM {k5w.AUDIT_TABLE}")
    con.rollback()
    assert len(con.execute(f"SELECT * FROM {k5w.AUDIT_TABLE}").fetchall()) == 1
    con.close()


def test_no_update_or_delete_code_path_on_audit_table():
    """契约 5：本件**源码里没有** UPDATE/DELETE `promotion_audits` 的写路径
    （触发器 DDL 写的是 `BEFORE UPDATE ON` / `BEFORE DELETE ON`，不被本 grep 命中）。"""
    assert re.search(r"\bUPDATE\s+" + k5w.AUDIT_TABLE + r"\b", CODE_SRC,
                     re.I) is None
    assert re.search(r"\bDELETE\s+FROM\s+" + k5w.AUDIT_TABLE + r"\b", CODE_SRC,
                     re.I) is None
    assert CODE_SRC.count("UPDATE expression_strategies_v2") == 1   # 唯一写入口
    assert CODE_SRC.count("BEGIN IMMEDIATE") == 1
    assert CODE_SRC.count("DELETE FROM") == 0


def test_verify_audits_is_fail_visible_on_bad_rows(tmp_path):
    """契约 5：坏审计行（空 evidence_ref / 跨级 / 计数注水）必须被点名。"""
    db = _promotable(tmp_path)
    assert _commit(db) == k5w.EXIT_OK
    con = sqlite3.connect(db.as_posix())
    con.execute(
        f"INSERT INTO {k5w.AUDIT_TABLE} ({', '.join(k5w.AUDIT_COLUMNS)}) "
        "VALUES (" + ",".join("?" * len(k5w.AUDIT_COLUMNS)) + ")",
        ("PAUD-BAD", k5w.TOOL, k5w.GATE_VERSION, "ESV2-T", "t-restrain", 1,
         "hypothesis", "verified", "hypothesis", "verified", "hypothesis",
         "replicated", "UNCERTAIN", "WORK", "[]", "x", k5w.SCOPE_RULE_VERSION,
         "[]", 2, "R1", "2026-09-26T00:00:00+08:00", "0" * 64, "{}"))
    con.commit()
    con.close()
    out = k5w.verify_promotion_audits(db)
    assert out["ok"] is False
    kinds = {v["audit_id"]: set(v["kinds"]) for v in out["violations"]}
    assert set(kinds) == {"PAUD-BAD"}                 # 好行不误报
    assert "empty_evidence_ref" in kinds["PAUD-BAD"]
    assert "evidence_count_mismatch" in kinds["PAUD-BAD"]
    assert any(k.startswith("non_adjacent_step") for k in kinds["PAUD-BAD"])
    assert any(k.startswith("state_regression") for k in kinds["PAUD-BAD"])


# ============================================ 前置条件逐条（W2）
@pytest.mark.parametrize("integ", [json.dumps({"src_ok": "true"}),
                                   json.dumps({"src_ok_unverified": True}),
                                   json.dumps({"src_ok": False}), None])
def test_src_ok_must_be_strict_true(tmp_path, integ):
    """`"true"` 字符串/未校验/判坏都不算证据（判定权在 `source_check.parse_src_ok`）。"""
    db = _promotable(tmp_path, name="d.db",
                     works=[_work("WK-A", integrity=integ),
                            _work("WK-B", integrity=integ)],
                     instances=[_inst("SI-1", "ESV2-T", "WK-A"),
                                _inst("SI-2", "ESV2-T", "WK-B")])
    v = _verdict(db)
    assert v["reason"].startswith("no_src_ok_evidence:")
    assert v["evidence"]["evidence_count"] == 2       # 过闸，但没过 src_ok
    assert v["evidence"]["src_ok_ids"] == []


def test_src_ok_true_promotes(tmp_path):
    """同构场景只改 integrity 为严格 true ⇒ 判词翻成 PROMOTE（方向可证伪）。"""
    db = _promotable(tmp_path)
    v = _verdict(db)
    assert v["decision"] == k5w.PROMOTE
    assert v["evidence"]["src_ok_ids"] == ["SI-1", "SI-2"]


def test_review_new_def_required(tmp_path):
    """W2(ii)：复审未发生 ⇒ 判 `no_review_new_def`（本件不代写复审）。"""
    db = _promotable(tmp_path,
                     instances=[_inst("SI-1", "ESV2-T", "WK-A", reviewer=""),
                                _inst("SI-2", "ESV2-T", "WK-B", reviewer="")])
    v = _verdict(db)
    assert v["reason"].startswith("no_review_new_def:")
    assert KE.REVIEW_MARKER_NEW_DEF in v["reason"]
    assert v["evidence"]["src_ok_ids"] == ["SI-1", "SI-2"]
    assert v["evidence"]["reviewed_ids"] == []
    assert _main(db, "--dry-run") == k5w.EXIT_REFUSED


def test_replicated_requires_independent_roots(tmp_path):
    """§4.3 独立来源复现：同根作品不同 span ⇒ `single_root_no_replication`。"""
    db = _promotable(tmp_path, works=[_work("WK-A", canonical="CW-A"),
                                      _work("WK-B", canonical="CW-A")],
                     instances=[_inst("SI-1", "ESV2-T", "WK-A", span=(0, 10)),
                                _inst("SI-2", "ESV2-T", "WK-B", span=(20, 30))])
    assert _verdict(db)["decision"] == k5w.PROMOTE    # observed 级不要求复现
    assert _commit(db) == k5w.EXIT_OK
    second = _verdict(db, to="replicated")
    assert second["reason"].startswith("single_root_no_replication:")
    assert second["evidence"]["roots"] == ["CW-A"]


def test_benchmark_evidence_never_counts(tmp_path):
    """基准段被 `_evidence_for` 剔除 ⇒ 晋升无输入（拒因字面量在库侧）。"""
    db = _promotable(tmp_path, works=[_work("WK-A", role="benchmark"),
                                      _work("WK-B", role="benchmark")])
    v = _verdict(db)
    assert v["reason"] == "excluded_no_evidence"
    assert v["evidence"]["stripped"] == {"benchmark_source": 2}


def test_instance_status_not_eligible_is_stripped(tmp_path):
    """证据实例 status 不在 `KQ.ELIGIBLE_INSTANCE_STATUS` ⇒ 库侧剔除，不另写口径。"""
    db = _promotable(tmp_path,
                     instances=[_inst("SI-1", "ESV2-T", "WK-A", status="proposed"),
                                _inst("SI-2", "ESV2-T", "WK-B", status="proposed")])
    v = _verdict(db)
    assert v["evidence"]["evidence_count"] == 0
    assert v["reason"] == "excluded_no_evidence"


def test_excluded_source_type_never_counts(tmp_path):
    """fixture/synthetic/commentary 冒充人类证据 ⇒ 剔除（判据取自 KQ 常量）。"""
    db = _promotable(tmp_path, works=[_work("WK-A", source_type="fixture"),
                                      _work("WK-B", source_type="synthetic")])
    v = _verdict(db)
    assert v["evidence"]["evidence_count"] == 0
    assert v["evidence"]["stripped"] == {"excluded_source_type": 2}


# ============================================== scope 推导（带规则版本）
def test_scope_candidates_and_capping(tmp_path):
    """scope 逐级：证据支持 GENRE 也只写下一级；`GLOBAL` 永不出现在候选里。"""
    db = _promotable(tmp_path)
    s = k5w.open_ro_session(db)
    cands, why = k5w.scope_candidates(s, ["WK-A", "WK-B"])
    v = k5w.evaluate(s, s.query(ExpressionStrategyV2).filter_by(
        id="ESV2-T").one(), {}, None, "R1")
    s.close()
    assert [c[0] for c in cands] == ["WORK", "AUTHOR", "GENRE"] and why == ""
    assert all(c[0] != "GLOBAL" for c in cands) and "GLOBAL" not in k5w.SCOPE_LADDER
    assert v["plan"]["scope_to"] == "WORK"
    assert v["plan"]["scope_capped"] and "截断" in v["plan"]["scope_capped"]
    assert "capped" in v["line"] and k5w.SCOPE_RULE_VERSION in v["line"]
    step = k5w.scope_step("WORK", "GENRE")
    assert step[0] == "AUTHOR" and step[1]                    # 也只走一级


def test_scope_candidates_refuse_to_guess_without_registry(tmp_path):
    """缺登记行 ⇒ 不猜 scope（防御性入口：`evaluate` 里同判词为 `scope_undeterminable`）。"""
    db = _promotable(tmp_path)
    con = sqlite3.connect(db.as_posix())
    con.execute("DELETE FROM work_sources WHERE work_id='WK-B'")
    con.commit()
    s = k5w.open_ro_session(db)
    cands, why = k5w.scope_candidates(s, ["WK-A", "WK-B"])
    assert cands == [] and why == "no_registry:WK-B"
    # 同库同策略走 evaluate：SI-2 已被库剔除 ⇒ 剩 WK-A 可推 WORK（不越界猜更大范围）
    v = k5w.evaluate(s, s.query(ExpressionStrategyV2).filter_by(
        id="ESV2-T").one(), {}, None, "R1")
    s.close()
    assert v["plan"]["scope_to"] == "WORK"
    assert v["plan"]["scope_ids"] == ["WK-A"]
    con.close()


def test_scope_global_row_is_refused_not_overwritten(tmp_path):
    db = _promotable(tmp_path, scope="GLOBAL", scope_ids=["*"])
    v = _verdict(db)
    assert v["reason"].startswith("off_ladder:scope=GLOBAL")


def test_off_ladder_observation_column_value_is_refused(tmp_path):
    """行列取值不在阶梯词表内 ⇒ fail-visible 拒，不猜当前级。"""
    db = _promotable(tmp_path, obs="bogus")
    v = _verdict(db)
    assert v["reason"].startswith("off_ladder:observation_status=bogus")
    assert v["level"] == ""


# ============================================== 只读性 / 用法面
def test_judgement_leaves_db_and_directory_untouched(tmp_path):
    """判定跑完：库字节与 mtime 不变、目录不冒新文件（只读承诺的可执行形式）。"""
    db = _promotable(tmp_path)
    listing = sorted(p.name for p in tmp_path.iterdir())
    dig, mt = _digest(db), os.stat(db).st_mtime_ns
    for argv in (["--dry-run"], ["--dry-run", "--to", "verified"],
                 ["--dry-run", "--book-id", "WK-A"], ["--verify-audits"],
                 ["--dry-run", "--json"]):
        assert _main(db, *argv) in (k5w.EXIT_OK, k5w.EXIT_REFUSED), argv
    assert _digest(db) == dig and os.stat(db).st_mtime_ns == mt
    assert sorted(p.name for p in tmp_path.iterdir()) == listing


def test_verify_audits_on_db_without_audit_table(tmp_path):
    """本件没写过 ⇒ 无审计表：校验判「零行且 ok」，不是崩、不是猜。"""
    db = _promotable(tmp_path)
    dig = _digest(db)
    out = k5w.verify_promotion_audits(db)
    assert out["ok"] is True and out["n_audits"] == 0
    assert _main(db, "--verify-audits") == k5w.EXIT_OK
    assert _digest(db) == dig


def test_unknown_strategy_is_usage_error(tmp_path):
    db = _promotable(tmp_path)
    dig = _digest(db)
    assert _main(db, "--dry-run", "--strategy", "ESV2-NOPE") == k5w.EXIT_ERROR
    assert _main(db, "--commit", "--strategy", "ESV2-NOPE",
                 "--reviewer", "R") == k5w.EXIT_ERROR
    assert _digest(db) == dig


def test_missing_db_reports_unreadable_not_guess(tmp_path, monkeypatch):
    """库不可读 ⇒ 如实报「不可读」并 rc=1，**不猜任何判词**。

    同时钉死测试卫生：候选路径里的「主仓绝对回退」在本用例里被换到 tmp_path
    之外——本文件任何用例都不得打开真库。"""
    monkeypatch.setattr(GE, "DB_FALLBACK_ABS",
                        str(tmp_path / "outside" / "absent.db"))
    # 2026-09-26 主控修：本用例必须让**全部**候选都不存在才算「不可读」。
    # 原写法只换了 DB_FALLBACK_ABS，`--repo-root` 仍取默认 ROOT ⇒ 在**主仓 cwd**
    # 下第二候选 `<repo>/data/language_genome.db`（真库 439MB）存在 ⇒ 命中真库、
    # rc=2（NO-PROMOTE）而非 rc=1，既假红又真的打开了生产库（与本用例 docstring
    # 「本文件任何用例都不得打开真库」自相矛盾）。worktree 里无 data/ ⇒ 恰好掩盖。
    empty_root = tmp_path / "emptyrepo"
    empty_root.mkdir()
    for argv in (["--dry-run"], ["--verify-audits"]):
        assert _main(tmp_path / "nope.db", *argv,
                     "--repo-root", str(empty_root)) == k5w.EXIT_ERROR


def test_explicit_db_wins_over_repo_candidates(tmp_path):
    """测试卫生：本文件的判定永远跑在 tmp_path 的合成库上（真库零接触）——
    `--db` 显式命中即返回，不走解释器的主仓回退候选。"""
    db = _promotable(tmp_path)
    picked, cands = GE.resolve_db(str(db), ROOT)
    assert picked == Path(str(db)) and cands[0] == str(db)
    assert picked.parent == tmp_path and picked.name == "synth.db"


def test_local_rules_are_only_ladder_and_scope_and_audit_schema():
    """本地只写「库里不存在、写侧固有」的规则；门判据一律单源（不本地重写）。"""
    assert k5w.SCOPE_RULE_VERSION == "scope-derive-1"
    assert k5w.LADDER == ("hypothesis", "observed", "replicated", "verified")
    # 每级落地的列取值都在 app/knowledge.py 既有词表内（不新造枚举）
    for st_, obs_ in k5w.LADDER_COLUMNS.values():
        assert st_ in K.STRATEGY_STATUS and obs_ in K.OBSERVATION_STATUS
    assert k5w.LADDER_COLUMNS["verified"] == ("verified", "replicated")
    # scope 推导函数体里不出现 GLOBAL ⇒ 该候选永不产生（§4.3 只预留契约）
    body = CODE_SRC.split("def scope_candidates")[1].split("\ndef scope_step")[0]
    body = re.sub(r'""".*?"""', "", body, flags=re.S)   # 摘掉 docstring 的说明性引用
    assert "GLOBAL" not in body
    assert k5w.SCOPE_LADDER == ("UNCERTAIN", "WORK", "AUTHOR", "GENRE")
    # 门判据一律引用上游符号，本地**不定义**任何合格集/排除集/允许集
    assert re.search(r"^(ELIGIBLE|DEFAULT_EXCLUDED|DEFAULT_ALLOWED)\w*\s*=",
                     CODE_SRC, re.M) is None
    assert "GE.explain_strategy" in CODE_SRC            # 逐级判词单源
    assert "sc.parse_src_ok" in CODE_SRC                # src_ok 唯一支笔
    assert "KE.REVIEW_MARKER_NEW_DEF" in CODE_SRC       # 复审标记不新造
    assert "KQ._evidence_for" in CODE_SRC               # 净剩证据同一函数
    assert k5w.AUDIT_COLUMNS[:1] == ("audit_id",)
    for must in ("from_status", "to_status", "evidence_ref", "ts"):
        assert must in k5w.AUDIT_COLUMNS
    # 审计列与建表 DDL 逐列对齐（防重复列/漏列——每列恰好声明一次）
    assert len(set(k5w.AUDIT_COLUMNS)) == len(k5w.AUDIT_COLUMNS)
    for c in k5w.AUDIT_COLUMNS:
        assert len(re.findall(rf"^  {c} (TEXT|INTEGER)", k5w.AUDIT_DDL,
                              re.M)) == 1, c
    assert k5w.AUDIT_DDL.count("PRIMARY KEY") == 1
    assert len(re.findall(r"NOT NULL", k5w.AUDIT_DDL)) == len(k5w.AUDIT_COLUMNS) - 1


def test_audit_ddl_is_idempotent(tmp_path):
    """建表 DDL 幂等（`IF NOT EXISTS`）——重跑不炸、不重复建表、不改数据行。"""
    db = _promotable(tmp_path)
    con = sqlite3.connect(db.as_posix())
    k5w.ensure_audit_schema(con)
    con.commit()
    con.close()
    dig = _digest(db)                     # 表已建；此后两次调用都应逐字节不变
    con = sqlite3.connect(db.as_posix())
    k5w.ensure_audit_schema(con)          # 第二次不报错
    k5w.ensure_audit_schema(con)          # 第三次同样
    assert con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name=?", (k5w.AUDIT_TABLE,)).fetchone()
    assert con.execute("SELECT COUNT(*) FROM " + k5w.AUDIT_TABLE).fetchone() == (0,)
    con.commit()
    con.close()
    assert _digest(db) == dig             # DDL 幂等 ⇒ 库字节不变
    assert _rows(db, "expression_strategies_v2", "status, observation_status, "
            "scope") == [("hypothesis", "hypothesis", "UNCERTAIN")]
