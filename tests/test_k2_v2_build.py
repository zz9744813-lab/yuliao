r"""K2 v2 合并卡（S1/S2）端到端构建器回归（`scripts/k2_v2_build.py`）。

**全离线、零真库依赖、零网络**：所有用例只在 `tmp_path` 里自建的**合成库**上
写（真库一个字节都不碰：副本库闸在打开任何连接之前就拒），AI 侧由调用方注入，
两席「收据」用**本地合成的上游响应**（monkeypatch `runner._post_once`）投出，
不打任何网关。

每条硬契约都有**方向可证伪**的用例（不是只测「全绿」）：

- **副本库闸**：`D:\language-genome-data\language_genome.db` 与本仓
  `data/language_genome.db` 两个真库候选命中即 `RealDatabaseRefused`；CLI
  侧退出码 2，且**证明连可写引擎都没打开**（把 `open_engine` 换成「一调用
  就炸」）。
- **落卡纪律**：两张 v2 卡落 `hypothesis` / `version=2` /
  `legacy_strategy_id` 指血缘；`effect_hypothesis` 写「未定」而**不是编造**
  的效果断言；8 张 v1 legacy 卡的 status/scope **逐字节不变**（落卡 + 证据 +
  并 AI 侧 + 升格四阶段跑完仍不变）。
- **门判据单源**：成对证据转调 `k2_contrast_extract.run_contrast`（把入口
  换成探针可证明真的走它），晋升判词转调 `k5_promotion_write.evaluate`
  （同样探针）——本件不另写一套门、不自填判词。
- **门0 不过不落库 / 混合 op 拒收**：混合形态对与构造不符对在 live 模式下
  **零实例**落库，拒绝理由原文进 JSONL 旁路账本。
- **`hypothesis` 不许直接写 verified**：`evaluate` 判 `skip_ladder`，库里
  审计行与批准链接都是空。
- **缺两席收据不许写链接**：replicated 卡缺收据 ⇒
  `semantic_review_unverifiable:*`，卡/审计/链接三处均不变。
- **反向验证（准入门）**：合成两席 PASS ⇒ `approved_selected` admit 成功并
  给出 `link_id`；把一席判词换成 `ABSTAIN`/`BLOCK` ⇒ 同一张卡
  `approved_selected` 必须**拒**（原文含 `non_pass_vote:`），链接不写。
- **幂等**：重跑落卡不重复建行、重跑证据不重复落实例（`skip_dup_sha`）、
  重跑并 AI 侧是 no-op。
- **读数口径**：`scope_ids` 逐字可读；`versions={"2"}` 收窄时
  `hypothesis` 卡在任何收窄下都不进候选（`selected` 空）。
"""
from __future__ import annotations
import importlib.util as _u
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2v2b", ROOT / "scripts" / "k2_v2_build.py")
k2v2b = _u.module_from_spec(_spec)
sys.modules["k2v2b"] = k2v2b
_spec.loader.exec_module(k2v2b)

CX = k2v2b.CX                                             # noqa: E402  门单源
KP = k2v2b.KP                                             # noqa: E402  判词单源
from app import knowledge as K                           # noqa: E402
from app import knowledge_extract as KE                  # noqa: E402
from app.db import Base                                  # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,   # noqa: E402
                        StrategyInstance, Work, WorkSource)

OK_INT = json.dumps({"src_ok": True})
BAD_INT = json.dumps({"src_ok": False})
S1_CARD = k2v2b.card_id(CX.S1_KEY)
S2_CARD = k2v2b.card_id(CX.S2_KEY)
REVIEWER = "k2-v2-duty-agent"

# 四条**过门**的合成配对（人类侧句逐字来自下方各段的登记原文，span 可核）。
# op 按构造标注；AI 侧整段改写/加节拍，长度比 1.2–6.0。
PAIR_SPECS = (
    {"tag": "S1-A", "op": CX.OP_ADD_INTERPRETATION,
     "scene_keys": ["他", "碗"], "work": "WK-A", "seg": "SEG-A",
     "human_text": "他放下碗，没有再说话，转身出了门。",
     "ai_text": ("他搁下手里的碗，什么也没再说，径直朝门口走去。屋里其实比外头更静，"
                 "他说到底只是想避开那双眼睛，因为有些话说破便再无退路。")},
    {"tag": "S1-B", "op": CX.OP_ADD_INTERPRETATION,
     "scene_keys": ["夜雨", "他"], "work": "WK-B", "seg": "SEG-B",
     "human_text": "夜雨是他的族人，从小一起长大。",
     "ai_text": ("夜雨算是他自小一道长起来的族人。其实他心里明白，这份情分早不只停在"
                 "玩伴上，因为有些话一旦挑明，两个人都要难堪。")},
    {"tag": "S2-A", "op": CX.OP_SPLIT_BEATS,
     "scene_keys": ["他", "碗"], "work": "WK-A", "seg": "SEG-A",
     "human_text": "他把碗推到桌角，推门出去，走了。",
     "ai_text": "他慢慢地把碗推到桌角，然后轻轻推门出去，接着走了，最后头也不回。"},
    {"tag": "S2-B", "op": CX.OP_SPLIT_BEATS,
     "scene_keys": ["烟杆", "他", "碗"], "work": "WK-B", "seg": "SEG-B",
     "human_text": "他磕了磕烟杆，擦了擦手，走到桌边坐下，把碗也收了。",
     "ai_text": ("他慢慢磕了磕烟杆，轻轻擦了擦手，走到桌边坐下，把碗也收了，然后站住了"
                 "一会儿，接着又坐回原位，最后才慢慢走了出去。")},
)
# 段文本 = 前缀 + 人类侧原文 + 后缀（span 落在中间，逐字可核）。
SEG_TEXT = {
    "SEG-A": "夜里的巷子很静。{s1a}灯下没有人。{s2a}巷口的风停了。",
    "SEG-B": "院子里落了一层薄霜。{s1b}天光很淡。{s2b}",
}
# 8 张 v1 legacy 卡的占位（只读对照：本件不许碰它们任何一列）。
LEGACY = tuple(
    {"id": f"ES-{i:012x}", "status": "approved_selected",
     "scope": "WORK", "scope_ids": [f"WK-L{i}"]}
    for i in range(8))


# ==================================================== 合成库构造
def _mk(tmp_path: Path, *, legacy=True, name="synth.db") -> Path:
    """自建合成库（ORM 建表 + 插行）——本文件只认 tmp_path 里这一份。
    引擎用本件自己的 `open_engine`（WAL + foreign_keys=ON，与生产同口径，
    `ensure_semantic_schema` 要求连接开着外键）。"""
    from sqlalchemy.orm import sessionmaker
    from app.semantic_receipts import ensure_semantic_schema
    db = tmp_path / name
    engine = k2v2b.open_engine(db)
    Base.metadata.create_all(engine)
    # 写侧两张表（promotion_audits / semantic_*）在生产里由执行器按需建；
    # 合成库照样建出来，读数用例才能断言「审计行/链接为空」。
    raw = sqlite3.connect(db.as_posix())
    try:
        KP.ensure_audit_schema(raw)
        raw.commit()
    finally:
        raw.close()
    ensure_semantic_schema(engine)
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with S() as s:
        for wid, _seg_id, _author, _genre in (("WK-A", "SEG-A", "AU-1", "g1"),
                                              ("WK-B", "SEG-B", "AU-2", "g2")):
            s.add(Work(id=wid, title=wid, source="file:synth"))
        s.flush()                       # works 先落（外键指向它）
        for wid, seg_id, author, genre in (("WK-A", "SEG-A", "AU-1", "g1"),
                                           ("WK-B", "SEG-B", "AU-2", "g2")):
            text = SEG_TEXT[seg_id].format(
                s1a=PAIR_SPECS[0]["human_text"], s2a=PAIR_SPECS[2]["human_text"],
                s1b=PAIR_SPECS[1]["human_text"], s2b=PAIR_SPECS[3]["human_text"])
            s.add(Segment(id=seg_id, work_id=wid, ordinal=0, text=text,
                          text_clean=text, role=None, n_sentences=4,
                          n_chars=len(text), integrity=OK_INT))
            s.add(WorkSource(work_id=wid, canonical_work_id=wid, author_id=author,
                             genre_ids=[genre], source_type="human_fiction",
                             text_version="corpus-v1", text_sha256="0" * 64,
                             purpose_basis="synth", identity_purposes=["research"],
                             license_purposes=[], license_basis="synth",
                             metadata_status="verified", metadata_basis="synth"))
        for row in (LEGACY if legacy else ()):
            s.add(ExpressionStrategyV2(
                id=row["id"], strategy_key=f"legacy:{row['id']}", version=1,
                abstract_operation="v1 legacy", invariants=[],
                effect_hypothesis="", failure_modes=[], status=row["status"],
                source="legacy", scope=row["scope"], scope_ids=row["scope_ids"],
                scope_basis="legacy", observation_status="replicated",
                effect_status="untested"))
        s.commit()
    engine.dispose()
    return db


def _pairs(*tags: str) -> list:
    """由 PAIR_SPECS 造 `ContrastPair`（span 定位到段内真实区间）。"""
    want = set(tags)
    out = []
    for spec in PAIR_SPECS:
        if want and spec["tag"] not in want:
            continue
        text = SEG_TEXT[spec["seg"]].format(
            s1a=PAIR_SPECS[0]["human_text"], s2a=PAIR_SPECS[2]["human_text"],
            s1b=PAIR_SPECS[1]["human_text"], s2b=PAIR_SPECS[3]["human_text"])
        start = text.index(spec["human_text"])
        out.append(CX.ContrastPair(
            human_text=spec["human_text"], ai_text=spec["ai_text"],
            scene_keys=set(spec["scene_keys"]), op=spec["op"],
            span_start=start, span_end=start + len(spec["human_text"]),
            meta={"segment_id": spec["seg"], "text_version": "corpus-v1",
                  "tag": spec["tag"]}))
    return out


def _session(db: Path):
    from sqlalchemy.orm import sessionmaker
    eng = k2v2b.open_engine(db)
    return eng, sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)


def _run(db: Path, stage: str, *pairs, **kw):
    """单阶段执行（内部引擎用完即弃）。"""
    engine = k2v2b.open_engine(db)
    from sqlalchemy.orm import Session
    try:
        with Session(bind=engine) as s:
            if stage == "cards":
                return k2v2b.build_cards(s)
            if stage == "evidence":
                return k2v2b.extract_evidence(
                    s, list(pairs), live=kw.get("live", True),
                    ledger_path=kw.get("ledger", str(db.parent / "pairs.jsonl")))
            if stage == "attach":
                return k2v2b.attach_ai_side(s, list(pairs))
            if stage == "attest":
                return k2v2b.attest_instances(s)
    finally:
        engine.dispose()
    raise AssertionError(f"unknown_stage:{stage}")


def _rows(db, table, cols="*", where=""):
    con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT {cols} FROM {table}{where}").fetchall()
    finally:
        con.close()


def _v2_cards(db):
    return _rows(db, "expression_strategies_v2",
                 "id, strategy_key, version, status, observation_status, "
                 "scope, scope_ids, effect_hypothesis, legacy_strategy_id",
                 " WHERE version=2 ORDER BY id")


def _legacy_snapshot(db):
    """8 张 v1 legacy 卡的只读对照快照（逐列取值，供跑完四阶段后比对）。"""
    return _rows(db, "expression_strategies_v2",
                 "id, status, observation_status, scope, scope_ids, scope_basis",
                 " WHERE version=1 ORDER BY id")


def _instances(db, strategy_id=None):
    where = (" WHERE extractor_model='paired_contrast_v2'" +
             (f" AND strategy_id='{strategy_id}'" if strategy_id else "") +
             " ORDER BY id")
    return _rows(db, "strategy_instances",
                 "id, strategy_id, work_id, status, reviewer_version, "
                 "conditions_observed", where)


def _evidenced(tmp_path: Path, *, name="synth.db") -> Path:
    """落卡 + 四条过门配对落库 + 并 AI 侧 + 升格标记（阶梯起点）。"""
    db = _mk(tmp_path, name=name)
    _run(db, "cards")
    _run(db, "evidence", *_pairs())
    _run(db, "attach", *_pairs())
    _run(db, "attest")
    return db


# ============================================ 副本库闸（真库只读）
def test_copy_target_refuses_real_database_candidates(tmp_path):
    """真库两个候选（派工绝对路径 + 本仓 `data/` 默认位）命中即拒；
    副本（同目录另一个文件）放行。"""
    repo_data = tmp_path / "repo" / "data" / "language_genome.db"
    with pytest.raises(k2v2b.RealDatabaseRefused,
                       match="real_database_refused"):
        k2v2b.assert_copy_target(k2v2b.REAL_DB_CANDIDATES[0])
    with pytest.raises(k2v2b.RealDatabaseRefused):
        k2v2b.assert_copy_target(repo_data, repo_root=tmp_path / "repo")
    copy = tmp_path / "copy.db"
    copy.write_bytes(b"")
    assert k2v2b.assert_copy_target(copy, repo_root=tmp_path / "repo") == copy


def test_cli_refuses_real_database_before_opening_any_connection(
        tmp_path, monkeypatch, capsys):
    """CLI 侧同一道闸：退出码 2，且**一个引擎都没打开**（证明连只读探
    索都没发生，更谈不上写）。"""
    def boom(_db):
        raise AssertionError("指向真库时不得打开任何连接")

    monkeypatch.setattr(k2v2b, "open_engine", boom)
    rc = k2v2b.main(["--db", str(k2v2b.REAL_DB_CANDIDATES[0]),
                     "--stage", "cards"])
    assert rc == k2v2b.EXIT_REFUSED
    assert "real_database_refused" in capsys.readouterr().err


def test_cli_reports_missing_database_instead_of_creating_it(
        tmp_path, capsys):
    """副本库必须**确实存在**——不存在即退出码 1，不猜、不回落默认值、
    也不顺手建库。"""
    missing = tmp_path / "absent.db"
    assert k2v2b.main(["--db", str(missing), "--stage", "cards"]) == \
        k2v2b.EXIT_ERROR
    assert not missing.exists()
    assert "库不存在" in capsys.readouterr().err


# ============================================ 落卡纪律
def test_cards_land_two_v2_cards_at_hypothesis_with_lineage(tmp_path):
    """两张 v2 卡：`version=2`、`status=observation_status=hypothesis`、
    `scope=UNCERTAIN`、`legacy_strategy_id` 指向归并依据里的 v1 血缘；
    `effect_hypothesis` 如实写「未定」——不是编造的效果断言。"""
    db = _mk(tmp_path)
    rep = _run(db, "cards")
    assert sorted(rep["created"]) == sorted([S1_CARD, S2_CARD])
    rows = _v2_cards(db)
    assert [r[0] for r in rows] == sorted([S1_CARD, S2_CARD])
    for row in rows:
        (_id, key, version, status, obs, scope, scope_ids,
         effect, legacy) = row
        assert version == 2
        assert (status, obs, scope, scope_ids) == \
            ("hypothesis", "hypothesis", "UNCERTAIN", "[]")
        assert key in (CX.S1_KEY, CX.S2_KEY)
        assert legacy and legacy.startswith("ES-")
        assert k2v2b.EFFECT_UNDECIDED in effect
        assert "未定" in effect
    # 抽象操作/不变项/失败模式按合并方案 §1 落库（非空且逐卡不同）
    payload = {c["strategy_key"]: k2v2b.card_payload(c)
               for c in k2v2b.CARD_SPECS}
    assert payload[CX.S1_KEY]["abstract_operation"] != \
        payload[CX.S2_KEY]["abstract_operation"]
    for spec in payload.values():
        assert spec["invariants"] and spec["failure_modes"]
        assert spec["effect_status"] == "untested"
        assert spec["observation_status"] == "hypothesis"


def test_cards_stage_is_idempotent_and_never_overwrites(tmp_path):
    """重跑落卡：只复用、不重建（行数恒为 2）；同名卡字段被改过则
    fail-visible 报错，**不覆盖**已存在的行。"""
    db = _mk(tmp_path)
    _run(db, "cards")
    again = _run(db, "cards")
    assert again["created"] == [] and sorted(again["reused"]) == \
        sorted([S1_CARD, S2_CARD])
    assert len(_v2_cards(db)) == 2
    con = sqlite3.connect(db.as_posix())
    con.execute("UPDATE expression_strategies_v2 SET invariants='[\"改过\"]' "
                "WHERE id=?", (S1_CARD,))
    con.commit()
    con.close()
    with pytest.raises(k2v2b.EvidenceAttestError, match="card_field_drift"):
        _run(db, "cards")


def test_v1_legacy_cards_are_never_touched(tmp_path):
    """8 张 v1 legacy 卡的 status/scope/scope_ids/observation_status
    逐字节不变——落卡 + 证据 + 并 AI 侧 + 升格四阶段跑完仍不变。"""
    db = _mk(tmp_path)
    before = _legacy_snapshot(db)
    _run(db, "cards")
    _run(db, "evidence", *_pairs())
    _run(db, "attach", *_pairs())
    _run(db, "attest")
    assert _legacy_snapshot(db) == before
    assert len(before) == 8


# ============================================ 成对证据：门判据单源
def test_evidence_stage_delegates_to_single_gate_source(tmp_path, monkeypatch):
    """证据阶段转调 `k2_contrast_extract.run_contrast`（探针可证），
    门表就是 `CX.GATES`——本件不另写一套门。"""
    db = _mk(tmp_path)
    _run(db, "cards")
    seen = {}
    real = CX.run_contrast

    def spy(s, pairs, **kw):
        seen["n"] = len(pairs)
        seen["live"] = kw.get("live")
        return real(s, pairs, **kw)

    monkeypatch.setattr(CX, "run_contrast", spy)
    rep = _run(db, "evidence", *_pairs())
    assert seen == {"n": 4, "live": True}
    assert rep["written"] == 4
    assert [name for name, _f in CX.GATES] == [
        "op_construction", "keyword_cooccurrence", "scene_reference",
        "length_ratio", "anti_copy", "cross_strategy"]


def test_dry_run_evidence_writes_nothing(tmp_path):
    """`live=False`：零库写、零文件写（连旁路账本都不落）。"""
    db = _mk(tmp_path)
    _run(db, "cards")
    ledger = tmp_path / "pairs.jsonl"
    rep = _run(db, "evidence", *_pairs(), live=False, ledger=str(ledger))
    assert rep["mode"] == "dry_run" and rep["written"] == 0
    assert rep["passed"] == 4 and rep["rejected"] == 0
    assert not ledger.exists()
    assert _instances(db) == []


def test_gate_failure_and_mixed_op_pairs_are_never_persisted(tmp_path):
    """门0 不过（构造不符）与**混合 op**（S1 对混入 S2 形态）在 live 模式
    下**零实例**落库；拒绝理由原文进 JSONL 旁路账本。"""
    db = _mk(tmp_path)
    _run(db, "cards")
    good = _pairs("S1-A", "S2-A")
    mixed = CX.ContrastPair(
        human_text=good[0].human_text,
        ai_text=good[0].ai_text + "他慢慢转过身，接着又停下。",
        scene_keys={"他", "碗"}, op=CX.OP_ADD_INTERPRETATION,
        span_start=good[0].span_start, span_end=good[0].span_end,
        meta={"segment_id": "SEG-A", "text_version": "corpus-v1"})
    broken = CX.ContrastPair(
        human_text=good[0].human_text, ai_text="他放下碗，走了。",
        scene_keys={"他", "碗"}, op=CX.OP_ADD_INTERPRETATION,
        span_start=good[0].span_start, span_end=good[0].span_end,
        meta={"segment_id": "SEG-A", "text_version": "corpus-v1"})
    ledger = tmp_path / "pairs.jsonl"
    rep = _run(db, "evidence", good[0], good[1], mixed, broken,
               ledger=str(ledger))
    assert rep["written"] == 2 and rep["passed"] == 2 and rep["rejected"] == 2
    assert rep["by_op"][CX.OP_ADD_INTERPRETATION] == {"passed": 1, "rejected": 2}
    assert rep["reject_reason_classes"]["混合操作"] == 1
    assert len(_instances(db)) == 2
    lines = [json.loads(x) for x in
             Path(ledger).read_text(encoding="utf-8").splitlines()]
    assert sorted(x["persist_outcome"] for x in lines) == \
        ["gated_out", "gated_out", "written", "written"]
    rejected_reasons = [r for x in lines if x["persist_outcome"] == "gated_out"
                        for r in x["reject_reasons"]]
    assert any(r.startswith("混合操作: add_interpretation 对混入节拍形态")
               for r in rejected_reasons)
    assert any("构造不符" in r for r in rejected_reasons)
    assert all(x["gates_ok"] is False for x in lines
               if x["persist_outcome"] == "gated_out")
    # 破形对不进实例表，人类侧原文也没被当成证据存进去
    assert len(_instances(db)) == 2


def test_op_label_drifts_and_illegal_ops_are_refused(tmp_path):
    """`strategy_key` 与 op 推导不一致、非法 op、以及缺 op 的规格规格书
    ——全在进门之前抛（fail-closed），一条都不许落库。"""
    with pytest.raises(ValueError, match="S1/S2 归属由 op 决定"):
        CX.ContrastPair(human_text="他放下碗。", ai_text="其实他心里明白。",
                        scene_keys={"他"}, op=CX.OP_ADD_INTERPRETATION,
                        strategy_key=CX.S2_KEY, span_start=0, span_end=4)
    with pytest.raises(ValueError, match="非法操作标签"):
        CX.ContrastPair(human_text="x", ai_text="y", scene_keys={"他"},
                        op="事后归类", span_start=0, span_end=1)
    with pytest.raises(ValueError, match="须有 op"):
        CX.build_pairs([{"human_text": "a", "ai_text": "b",
                         "span_start": 0, "span_end": 1}])


def test_span_not_matching_registered_text_is_skipped_not_written(tmp_path):
    """人类侧 span 与登记段原文不符 ⇒ `skip_span_mismatch`，不落库。"""
    db = _mk(tmp_path)
    _run(db, "cards")
    spec = dict(PAIR_SPECS[0])
    text = SEG_TEXT["SEG-A"]
    pair = CX.ContrastPair(
        human_text=spec["human_text"], ai_text=spec["ai_text"],
        scene_keys=set(spec["scene_keys"]), op=spec["op"],
        span_start=0, span_end=len(spec["human_text"]),
        meta={"segment_id": "SEG-A", "text_version": "corpus-v1"})
    assert text[:len(spec["human_text"])] != spec["human_text"]   # 前提
    rep = _run(db, "evidence", pair)
    assert rep["written"] == 0
    assert rep["skipped"] == {"skip_span_mismatch": 1}
    assert _instances(db) == []


# ============================================ 并 AI 侧 + 升格标记
def test_attach_aligns_ai_side_and_is_idempotent(tmp_path):
    """AI 侧原文按 `human_sha256` 对齐并入 `conditions_observed`；重跑
    是 no-op（不新增实例、不重写值）；对不上的对被点名拒。"""
    db = _run_cards_evidence(tmp_path)
    pairs = _pairs()
    rep = _run(db, "attach", *pairs)
    assert rep["n_attached"] == 4 and rep["refused"] == []
    keys = {k for row in _instances(db)
            for k in json.loads(row[5])}
    assert {"ai_side_text", "ai_side_sha256", "human_side_chars"} <= keys
    again = _run(db, "attach", *pairs)
    assert again["n_attached"] == 0
    assert sorted(again["unchanged"]) == sorted(r[0] for r in _instances(db))
    stray = CX.ContrastPair(
        human_text="库里根本没有的人类侧片段。", ai_text="其实他心里明白。",
        scene_keys={"他"}, op=CX.OP_ADD_INTERPRETATION,
        span_start=0, span_end=11, meta={"segment_id": "SEG-A",
                                         "text_version": "corpus-v1"})
    refused = _run(db, "attach", stray)
    assert refused["n_attached"] == 0
    assert "无对应已落实例" in refused["refused"][0]["why"]


def _run_cards_evidence(tmp_path: Path, *, name="synth.db") -> Path:
    db = _mk(tmp_path, name=name)
    _run(db, "cards")
    _run(db, "evidence", *_pairs())
    return db


def test_attach_refuses_conflicting_ai_side_for_same_human_side(tmp_path):
    """同一 human 侧配两个不同 AI 侧 = 证据歧义 ⇒ 拒（不覆盖已入库的
    那一侧，也不新增实例）。"""
    db = _run_cards_evidence(tmp_path)
    _run(db, "attach", *_pairs())
    before = _instances(db)
    twin = CX.ContrastPair(
        human_text=PAIR_SPECS[0]["human_text"],
        ai_text=PAIR_SPECS[0]["ai_text"] + "其实屋里比外头更静。",
        scene_keys={"他", "碗"}, op=PAIR_SPECS[0]["op"],
        span_start=_pairs("S1-A")[0].span_start,
        span_end=_pairs("S1-A")[0].span_end,
        meta={"segment_id": "SEG-A", "text_version": "corpus-v1"})
    rep = _run(db, "attach", twin)
    assert rep["n_attached"] == 0
    assert rep["refused"][0]["why"].endswith("——证据歧义，拒绝")
    assert "注入 AI 侧不符" in rep["refused"][0]["why"]
    assert _instances(db) == before


def test_attest_requires_all_six_gates_pass_and_proposed_status(tmp_path):
    """升格资格三件套：协议标记 + 六门全 pass + 现 status=proposed。
    门记录缺一/有非 pass ⇒ 拒，且**不碰**该行。"""
    db = _run_cards_evidence(tmp_path)
    rep = _run(db, "attest")
    assert rep["n_attested"] == 4
    assert {row[3] for row in _instances(db)} == {"verified"}
    assert {row[4] for row in _instances(db)} == {KE.REVIEW_MARKER_NEW_DEF}
    again = _run(db, "attest")
    assert again["n_attested"] == 0
    assert all("非 proposed，不重复升格" in r["why"] for r in again["refused"])

    # 篡改一门：非 pass ⇒ 拒（行不升级）
    con = sqlite3.connect(db.as_posix())
    row = con.execute("SELECT id, conditions_observed FROM strategy_instances "
                      "WHERE extractor_model='paired_contrast_v2' LIMIT 1"
                      ).fetchone()
    obs = json.loads(row[1])
    obs["gates"]["anti_copy"] = "fail"
    con.execute("UPDATE strategy_instances SET conditions_observed=?, "
                "status='proposed', reviewer_version='' WHERE id=?",
                (json.dumps(obs, ensure_ascii=False), row[0]))
    con.commit()
    con.close()
    rep2 = _run(db, "attest")
    assert rep2["n_attested"] == 0
    assert any("门记录不全/有非 pass" in r["why"] for r in rep2["refused"])


# ============================================ 晋升阶梯（判词单源）
def test_ladder_uses_promotion_verdict_single_source(tmp_path, monkeypatch):
    """阶梯转调 `k5_promotion_write.evaluate`（探针可证）——判词只有
    一个产出点，本件不自写。"""
    db = _evidenced(tmp_path)
    seen = []
    real = KP.evaluate

    def spy(s, st, policy, requested, reviewer=KP.DEFAULT_REVIEWER_DRY,
            index=1, total=1):
        seen.append((st.id, requested, policy))
        return real(s, st, policy, requested, reviewer, index, total)

    monkeypatch.setattr(KP, "evaluate", spy)
    rep = k2v2b.ladder(db, S1_CARD, to="observed", reviewer=REVIEWER)
    assert seen == [(S1_CARD, "observed", {})]
    assert rep["committed"] is True
    assert rep["decision"] == KP.PROMOTE
    assert rep["reason"] == "step_preconditions_met"


def test_hypothesis_cannot_jump_straight_to_verified(tmp_path):
    """`hypothesis` 不许跳级直写 `verified`：`evaluate` 判 `skip_ladder`，
    卡行/审计/批准链接三处均不变。"""
    db = _evidenced(tmp_path)
    rep = k2v2b.ladder(db, S1_CARD, to="verified", reviewer=REVIEWER)
    assert rep["committed"] is False
    assert rep["reason"].startswith("skip_ladder:hypothesis->verified")
    assert "禁跳级" in rep["reason"]
    row = _rows(db, "expression_strategies_v2", "status, observation_status",
                f" WHERE id='{S1_CARD}'")[0]
    assert row == ("hypothesis", "hypothesis")
    assert _rows(db, "promotion_audits") == []
    assert _rows(db, "semantic_approval_links") == []


def test_ladder_walks_one_level_at_a_time_and_derives_work_scope(tmp_path):
    """一级一事务：hypothesis→observed→replicated，两部不同作者的登记
    作品 ⇒ scope 只到 WORK（ids 逐字 = 证据作品集），且每级一条审计行。"""
    db = _evidenced(tmp_path)
    first = k2v2b.ladder(db, S1_CARD, to="observed", reviewer=REVIEWER)
    assert first["committed"] is True
    row = _rows(db, "expression_strategies_v2",
                "status, observation_status, scope, scope_ids",
                f" WHERE id='{S1_CARD}'")[0]
    assert row[:2] == ("hypothesis", "observed")
    assert row[2] == "WORK"
    assert json.loads(row[3]) == ["WK-A", "WK-B"]
    second = k2v2b.ladder(db, S1_CARD, to="replicated", reviewer=REVIEWER)
    assert second["committed"] is True
    row = _rows(db, "expression_strategies_v2",
                "status, observation_status, scope, scope_ids",
                f" WHERE id='{S1_CARD}'")[0]
    # 阶梯列位（k5_promotion_write.LADDER_COLUMNS）：replicated 只动观察层，
    # status 列要到 verified 才翻——判定≠升格在这里也成立。
    assert row[:2] == ("hypothesis", "replicated")
    assert json.loads(row[3]) == ["WK-A", "WK-B"]
    assert [r[0] for r in _rows(db, "promotion_audits", "to_status",
                                f" WHERE strategy_id='{S1_CARD}'")] == \
        ["observed", "replicated"]


def test_verified_without_two_seat_receipts_writes_nothing(tmp_path):
    """缺两席收据 ⇒ `semantic_review_unverifiable:*`；卡行不变、审计行
    不增、`semantic_approval_links` 空（本例一个快照都没冻结 ⇒
    `snapshot_missing`；只投一席的情形见准入用例的
    `two_pass_votes_missing` 断言）。"""
    db = _evidenced(tmp_path)
    for target in ("observed", "replicated"):
        assert k2v2b.ladder(db, S1_CARD, to=target,
                            reviewer=REVIEWER)["committed"] is True
    audits_before = _rows(db, "promotion_audits")
    card_before = _rows(db, "expression_strategies_v2",
                        "status, observation_status, scope, scope_ids",
                        f" WHERE id='{S1_CARD}'")
    rep = k2v2b.ladder(db, S1_CARD, to="verified", reviewer=REVIEWER)
    assert rep["committed"] is False
    assert rep["reason"] == "semantic_review_unverifiable:snapshot_missing"
    assert _rows(db, "promotion_audits") == audits_before
    assert _rows(db, "semantic_approval_links") == []
    assert _rows(db, "expression_strategies_v2",
                 "status, observation_status, scope, scope_ids",
                 f" WHERE id='{S1_CARD}'") == card_before
    assert card_before[0][:2] == ("hypothesis", "replicated")


# ======================================== 两席收据 → 准入（合成上游响应）
def _replicated(tmp_path: Path, card_id: str) -> Path:
    db = _evidenced(tmp_path)
    for target in ("observed", "replicated"):
        assert k2v2b.ladder(db, card_id, to=target,
                            reviewer=REVIEWER)["committed"] is True
    return db


def _reviewable_round(db: Path, card_id: str, monkeypatch):
    """按当前卡行的 scope claim 冻结一轮审查快照（离线，不投网关）。"""
    from app.semantic_receipts import ensure_semantic_schema
    from app.semantic_review_store import freeze_snapshot
    claim = k2v2b.scope_claim(db, card_id)
    engine = k2v2b.open_engine(db)
    ensure_semantic_schema(engine)
    snapshot = freeze_snapshot(engine, card_id, k2v2b.V2_VERSION, claim)
    return engine, snapshot["snapshot_id"], claim


def _synthetic_vote(monkeypatch, engine, snapshot_id, seat, *, verdict="PASS",
                    cited):
    """本地合成一席上游响应（**不联网**）：投一票即锁，判词如实落库。"""
    import httpx
    import app.semantic_review_runner as runner

    provider, model = f"provider-{seat}", f"actual-{seat}"
    route = runner.ReviewRoute(f"requested-{seat}", provider, model,
                               f"channel-{seat}")
    review = {"verdict": verdict,
              "reason": "已逐条核对人类侧原文与 AI 侧新增句、op 声明与门记录",
              "cited_instance_ids": list(cited),
              "concerns": ["AI 侧增量过宽"] if verdict == "BLOCK" else []}
    request_id = f"upstream-{seat}-{snapshot_id[-8:]}"
    response = httpx.Response(
        200,
        headers={"X-LG-Upstream-Provider": provider,
                 "X-LG-Upstream-Model": model,
                 "X-LG-Upstream-Channel-Id": f"channel-{seat}",
                 "X-LG-Upstream-Request-Id": request_id},
        json={"id": request_id, "model": model,
              "choices": [{"finish_reason": "stop", "message": {
                  "role": "assistant",
                  "content": json.dumps(review, ensure_ascii=False)}}]})
    monkeypatch.setattr(runner, "_post_once", lambda raw, timeout: response)
    monkeypatch.setattr(runner, "_require_private_storage", lambda _: None)
    from app import config
    monkeypatch.setattr(config, "LLM_MODE", "real")
    monkeypatch.setattr(config, "GATEWAY_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(config, "GATEWAY_API_KEY", "synthetic-key")
    return runner.review_snapshot(engine, snapshot_id, route,
                                 max_output_tokens=512,
                                 max_request_bytes=100_000,
                                 timeout_seconds=15)


def _cited_for(db: Path, card_id: str) -> list:
    return [row[0] for row in _instances(db, card_id)]


def _admit(db: Path, card_id: str, monkeypatch) -> dict:
    """走完「两席异模型 PASS ⇒ 准入成功」的完整链，返回准入清单条目。"""
    engine, snap, claim = _reviewable_round(db, card_id, monkeypatch)
    try:
        cited = _cited_for(db, card_id)
        _synthetic_vote(monkeypatch, engine, snap, "a", cited=cited)
        one_seat = k2v2b.ladder(db, card_id, to="verified", reviewer=REVIEWER)
        assert one_seat["committed"] is False
        assert one_seat["reason"] == \
            "semantic_review_unverifiable:two_pass_votes_missing"
        assert _rows(db, "semantic_approval_links", "strategy_id",
                     f" WHERE strategy_id='{card_id}'") == []
        _synthetic_vote(monkeypatch, engine, snap, "b", cited=cited)
        rep = k2v2b.ladder(db, card_id, to="verified", reviewer=REVIEWER)
        assert rep["committed"] is True
        assert rep["reason"] == "step_preconditions_met"
        assert rep["write"]["approval_rows"] == 1
        out = k2v2b.approval_readout(db, card_id)
        assert out["admitted"] is True
        entry = out["manifest"][0]
        assert entry["strategy_id"] == card_id
        assert entry["kind"] == "pre_promotion"
        assert entry["link_id"].startswith("SAP-")
        assert entry["snapshot_id"] == snap
        assert {entry["vote_a_id"], entry["vote_b_id"]} == set(
            r[0] for r in _rows(db, "semantic_review_votes", "vote_id",
                                f" WHERE snapshot_id='{snap}'"))
        assert _rows(db, "expression_strategies_v2", "status, scope",
                     f" WHERE id='{card_id}'")[0] == ("verified", "WORK")
        assert _rows(db, "promotion_audits", "to_status",
                     f" WHERE strategy_id='{card_id}'")[-1] == ("verified",)
        assert entry["verified_audit_id"].startswith("PAUD-")
        return entry
    finally:
        engine.dispose()


def test_two_distinct_seats_admit_both_v2_cards(tmp_path, monkeypatch):
    """正向：两席**异模型** PASS ⇒ `approved_selected` admit 成功并给出
    `link_id`（K4 消费的就是这个函数）；晋升事务同事务写 verified 审计行
    与 `semantic_approval_links`。"""
    db = _evidenced(tmp_path)
    for card in (S1_CARD, S2_CARD):
        for target in ("observed", "replicated"):
            assert k2v2b.ladder(db, card, to=target,
                                reviewer=REVIEWER)["committed"] is True
    entries = [_admit(db, card, monkeypatch) for card in (S1_CARD, S2_CARD)]
    assert sorted(e["strategy_id"] for e in entries) == \
        sorted([S1_CARD, S2_CARD])
    assert len({e["link_id"] for e in entries}) == 2
    assert len({e["snapshot_id"] for e in entries}) == 2
    # 两席模型互异（准入判据：同 snapshot 恰好 2 票、模型互异）
    for entry in entries:
        models = _rows(db, "semantic_review_votes", "model_id, verdict",
                       f" WHERE snapshot_id='{entry['snapshot_id']}'")
        assert len(models) == 2
        assert len({m[0] for m in models}) == 2
        assert {m[1] for m in models} == {"PASS"}
    # 并 AI 侧之后冻结的输入里，两极都在（这是两席能 PASS 的前提）
    engine = k2v2b.open_engine(db)
    try:
        with engine.connect() as conn:
            payload = conn.exec_driver_sql(
                "SELECT review_input_json FROM semantic_review_snapshots "
                "WHERE snapshot_id=:s", {"s": entries[0]["snapshot_id"]}
            ).scalar_one()
        instances = json.loads(payload)["instances"]
        assert instances and all("ai_side_text" in i["conditions_observed"]
                                 and i["conditions_observed"]["ai_side_text"]
                                 for i in instances)
        assert all(i["evidence_text"] for i in instances)
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad", ["ABSTAIN", "BLOCK"])
def test_one_non_pass_seat_revokes_admission_of_both_cards(
        tmp_path, monkeypatch, bad):
    """反向验证：卡已准入（`link_id` 已写）后，把**新一轮**里的一席判词
    换成非 PASS ⇒ 同一个 `approved_selected` 必须**拒**（原文含
    `non_pass_vote:<verdict>`）——最新的不过审的一轮压过旧的 PASS 轮。"""
    db = _evidenced(tmp_path)
    for card in (S1_CARD, S2_CARD):
        for target in ("observed", "replicated"):
            k2v2b.ladder(db, card, to=target, reviewer=REVIEWER)
        _admit(db, card, monkeypatch)
        assert k2v2b.approval_readout(db, card)["admitted"] is True

        engine, snap, _claim = _reviewable_round(db, card, monkeypatch)
        try:
            cited = _cited_for(db, card)
            _synthetic_vote(monkeypatch, engine, snap, "a", cited=cited)
            _synthetic_vote(monkeypatch, engine, snap, "b", verdict=bad,
                            cited=cited)
            out = k2v2b.approval_readout(db, card)
            assert out["admitted"] is False
            assert out["error"].endswith("non_pass_vote:" + bad)
            # 判红后不新增链接（旧链接仍在库里，但已不再授权准入）
            assert len(_rows(db, "semantic_approval_links", "strategy_id",
                             f" WHERE strategy_id='{card}'")) == 1
        finally:
            engine.dispose()


def test_one_seat_only_never_admits(tmp_path, monkeypatch):
    """只投一席（哪怕是 PASS）⇒ 准入必拒，链接不写。"""
    db = _replicated(tmp_path, S1_CARD)
    engine, snap, _claim = _reviewable_round(db, S1_CARD, monkeypatch)
    try:
        _synthetic_vote(monkeypatch, engine, snap, "a",
                        cited=_cited_for(db, S1_CARD))
        rep = k2v2b.ladder(db, S1_CARD, to="verified", reviewer=REVIEWER)
        assert rep["committed"] is False
        assert rep["reason"] == \
            "semantic_review_unverifiable:two_pass_votes_missing"
        assert _rows(db, "semantic_approval_links") == []
    finally:
        engine.dispose()


def test_same_model_twice_is_refused_as_not_independent(tmp_path, monkeypatch):
    """两席**必须异模型**：同一上游模型投两次 ⇒ 消费侧锁票先拒
    （`k2_model_already_voted`），凑不出「两席独立」。"""
    db = _replicated(tmp_path, S1_CARD)
    engine, snap, _claim = _reviewable_round(db, S1_CARD, monkeypatch)
    try:
        cited = _cited_for(db, S1_CARD)
        _synthetic_vote(monkeypatch, engine, snap, "a", cited=cited)
        with pytest.raises(Exception) as exc:
            _synthetic_vote(monkeypatch, engine, snap, "a", cited=cited)
        assert "k2_model_already_voted" in str(exc.value)
        assert k2v2b.ladder(db, S1_CARD, to="verified",
                            reviewer=REVIEWER)["committed"] is False
    finally:
        engine.dispose()


# ============================================ 读数口径
def test_scope_ids_and_status_readout_is_verbatim(tmp_path):
    """`card_status_readout` 逐字给出两张卡的 `scope_ids`/scope/status，
    不截断、不改写（K4 消费的就是这组值）。"""
    db = _replicated(tmp_path, S1_CARD)
    rows = {r["strategy_key"]: r for r in k2v2b.card_status_readout(db)}
    s1 = rows[CX.S1_KEY]
    assert s1["strategy_id"] == S1_CARD
    assert (s1["status"], s1["observation_status"], s1["scope"]) == \
        ("hypothesis", "replicated", "WORK")
    assert s1["scope_ids"] == ["WK-A", "WK-B"]
    assert s1["scope_basis"].startswith(KP.SCOPE_RULE_VERSION)
    assert s1["legacy_strategy_id"] == k2v2b.CARDS_BY_KEY[CX.S1_KEY][
        "legacy_primary"]
    assert rows[CX.S2_KEY]["status"] == "hypothesis"
    assert rows[CX.S2_KEY]["scope_ids"] == []


def test_versions_two_readout_excludes_hypothesis_cards(tmp_path):
    """`versions={"2"}` 收窄的 K3 读数：`hypothesis` 卡在任何收窄下都
    **不进候选**（`selected` 空）——判定≠升格；升到 replicated 仍不进，
    只有 verified 才可能进（K4 消费的那一环）。"""
    db = _evidenced(tmp_path)
    rep = k2v2b.k3_readout(db, "WK-A", versions={"2"})
    assert rep["versions"] == ["2"]
    assert rep["k3_status"] == "empty"
    assert rep["selected_ids"] == []
    k2v2b.ladder(db, S1_CARD, to="observed", reviewer=REVIEWER)
    k2v2b.ladder(db, S1_CARD, to="replicated", reviewer=REVIEWER)
    still = k2v2b.k3_readout(db, "WK-A", versions={"2"})
    assert still["selected_ids"] == []          # 未 verified 仍不进候选
    assert still["scope_ids"] == {}
    # 版本收窄是收窄不是放宽：不收窄时同一张卡同样不进（hypothesis 不合格）
    wide = k2v2b.k3_readout(db, "WK-A")
    assert wide["selected_ids"] == []
    assert S1_CARD not in {e.get("strategy_id") for e in wide["rejected"]}


def test_instance_readout_reports_gates_and_both_sides(tmp_path):
    """实例读数逐条给出 op / 门结果 / 复审标记 / span，供人工复核。"""
    db = _evidenced(tmp_path)
    rows = k2v2b.instance_readout(db)
    assert len(rows) == 4
    for row in rows:
        assert set(row["gates"]) == {name for name, _f in CX.GATES}
        assert set(row["gates"].values()) == {"pass"}
        assert row["status"] == "verified"
        assert row["reviewer_version"] == KE.REVIEW_MARKER_NEW_DEF
        assert row["op"] in CX.OPS
        assert row["span"][1] > row["span"][0]
    assert {r["op"] for r in rows} == {CX.OP_ADD_INTERPRETATION,
                                       CX.OP_SPLIT_BEATS}
    assert {r["work_id"] for r in rows} == {"WK-A", "WK-B"}


# ============================================ CLI 纪律
def test_cli_requires_reviewer_for_ladder_stages(tmp_path, capsys):
    """无签认不写：`--stage ladder/verified` 缺 `--reviewer` ⇒ 退出码 1。"""
    db = _evidenced(tmp_path)
    for stage in ("ladder", "verified"):
        assert k2v2b.main(["--db", str(db), "--stage", stage]) == \
            k2v2b.EXIT_ERROR
        assert "--reviewer" in capsys.readouterr().err
    assert _rows(db, "promotion_audits") == []


def test_cli_requires_injected_ai_side_for_evidence_stages(tmp_path, capsys):
    """AI 侧只由调用方注入：缺 `--ai-side` ⇒ 退出码 1（本件不联网生成）。"""
    db = _mk(tmp_path)
    for stage in ("evidence", "attach"):
        assert k2v2b.main(["--db", str(db), "--stage", stage]) == \
            k2v2b.EXIT_ERROR
        assert "--ai-side" in capsys.readouterr().err


def test_cli_cards_stage_round_trip_on_a_copy_database(tmp_path, capsys):
    """CLI 落卡阶段在副本库上跑通（退出码 0、输出含两张卡 id）；再跑
    一次仍是 0 且 `created` 为空（幂等）。"""
    db = _mk(tmp_path)
    capsys.readouterr()
    assert k2v2b.main(["--db", str(db), "--stage", "cards"]) == k2v2b.EXIT_OK
    first = json.loads(capsys.readouterr().out)
    assert sorted(first["results"][0]["created"]) == sorted([S1_CARD, S2_CARD])
    assert k2v2b.main(["--db", str(db), "--stage", "cards"]) == k2v2b.EXIT_OK
    second = json.loads(capsys.readouterr().out)
    assert second["results"][0]["created"] == []
    assert len(_v2_cards(db)) == 2


def test_script_never_touches_the_real_database_path_in_source():
    """源码纪律：真库只读候选只出现在**闸的比较**里；全文没有复制/搬
    移/删除真库或就地把真库当写入目标的语句。"""
    src = (ROOT / "scripts" / "k2_v2_build.py").read_text(encoding="utf-8")
    assert "D:/language-genome-data/language_genome.db" in src
    for banned in ("shutil.copy", "shutil.move", "shutil.rmtree",
                   "os.remove", "os.unlink", "os.replace", "os.rename",
                   "VACUUM INTO", "ATTACH DATABASE"):
        assert banned not in src, banned
    # 副本库闸是真库路径的唯一出口：命中即抛，且 CLI 侧映射为退出码 2。
    assert "RealDatabaseRefused" in src
    assert "assert_copy_target" in src
