"""scope_ids 声明 vs 单书证据实况：只披露、不改判定（lg-scope-singlebook-disclose）。

真库读数（主控本轮只读实测，`book_id=WK-6e5d2623`、8 条策略全 pass、
`selected=8`）：每条策略 `evidence_root_works` 只含 2 个作品而 `scope_ids`
最长声明 3 个——「声称 3 本、证据只有 2 本」，且没有任何字段告诉调用方
「这条策略在我这本书上到底有几条**同书**证据」⇒ 12 条证据被误读成全在
自己书上。本文件把这条缺口钉成四个**只读披露**字段，并钉住披露前后
判定逐字不变（`selected` 条数、`evidence_count`、`score_components`、
`evidence_cross_work`/`evidence_root_works`/`for_context`）。

夹具在 pytest 临时目录造独立 SQLite，写完即以 `mode=ro` + `PRAGMA
query_only=ON` 打开，收尾核对库文件 sha256 前后一致——零写入，不碰真库。
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import knowledge_query as kq
from app.db import Base
from app.models import (ExpressionStrategyV2, Segment, StrategyInstance,
                        Work, WorkSource)

TEXT = "灯花轻轻跳了一下，他终于开口。"
SHA256 = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()

# 条目上必须逐字在位的既有字段（披露只许加字段，不许动/删这些）
ENTRY_DECISION_FIELDS = ("score_components", "uncertain_items", "evidence",
                         "evidence_cross_work", "evidence_root_works",
                         "for_context")
COMP_DECISION_FIELDS = ("required_matches", "good_when_matches",
                        "evidence_count", "scope_specificity")
DISCLOSURE_FIELDS = ("evidence_same_book_count",
                     "evidence_scope_ids_declared",
                     "evidence_scope_ids_with_evidence",
                     "evidence_scope_unbacked_ids")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_work(session, work_id, *, canonical=None, author_id=None,
              genre_ids=None, registry=True) -> str | None:
    session.add(Work(id=work_id, title=work_id, source="test:scope-disclose"))
    session.flush()
    if not registry:
        return None
    segment = Segment(work_id=work_id, ordinal=0, text=TEXT, text_clean=TEXT,
                      n_sentences=1, n_chars=len(TEXT), role="train")
    session.add(segment)
    session.add(WorkSource(
        work_id=work_id, canonical_work_id=canonical or work_id,
        author_id=author_id, genre_ids=list(genre_ids or []),
        source_type="human_fiction", text_version="corpus-v1",
        text_sha256=SHA256, purpose_basis="test fixture",
        identity_purposes=["research"], license_purposes=[],
        metadata_status="verified", metadata_basis="test fixture"))
    session.flush()
    return segment.id


def _add_strategy(session, strategy_id, strategy_key, scope_ids, *,
                  scope="WORK") -> None:
    session.add(ExpressionStrategyV2(
        id=strategy_id, strategy_key=strategy_key, version=1,
        abstract_operation="test operation", invariants=[],
        effect_hypothesis="test hypothesis", failure_modes=[],
        status="verified", source="test", scope=scope, scope_ids=scope_ids,
        scope_basis="test fixture", observation_status="observed",
        effect_status="untested"))


def _add_instance(session, instance_id, strategy_id, work_id, segment_id, *,
                  start=0, end=8) -> None:
    evidence = TEXT[start:end]
    session.add(StrategyInstance(
        id=instance_id, strategy_id=strategy_id, strategy_version=1,
        work_id=work_id, segment_id=segment_id, frame_id=None,
        text_version="corpus-v1", span_start=start, span_end=end,
        evidence_text=evidence,
        evidence_sha256=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        conditions_observed={}, observed_content="test observation",
        extractor_model="test", reviewer_version="test", status="verified"))


def _seed(session) -> dict:
    """1 本查询书（alpha，带作者/题材登记）+ 4 条证据书 + 6 条策略。"""
    ids: dict = {"author": "AU-sd-1", "genre": "GN-sd-1"}
    for name in ("alpha", "beta", "gamma", "delta"):
        ids[name] = f"WK-sd-{name}"
    alpha_seg = _add_work(session, ids["alpha"], author_id=ids["author"],
                          genre_ids=[ids["genre"]])
    beta_seg = _add_work(session, ids["beta"])
    gamma_seg = _add_work(session, ids["gamma"])
    delta_seg = _add_work(session, ids["delta"])
    # ⑤查询书缺 WorkSource 登记行 ⇒ 根作品不可解析
    ids["noreg"] = "WK-sd-noreg"
    _add_work(session, ids["noreg"], registry=False)

    # ①声明 3 本、证据只来自 2 本（真库缺口形状）
    _add_strategy(session, "ESV2-sd-partial", "sd-partial",
                  [ids["alpha"], ids["beta"], ids["gamma"]])
    _add_instance(session, "SI-sd-partial-1", "ESV2-sd-partial",
                  ids["alpha"], alpha_seg)
    _add_instance(session, "SI-sd-partial-2", "ESV2-sd-partial",
                  ids["beta"], beta_seg, start=4, end=12)

    # ②声明 1 本、证据同书（同书证据真存在的情形）
    _add_strategy(session, "ESV2-sd-exact", "sd-exact", [ids["alpha"]])
    _add_instance(session, "SI-sd-exact-1", "ESV2-sd-exact",
                  ids["alpha"], alpha_seg)

    # ③声明 1 本、证据**全在别的书**（零同书证据的极端形状）
    _add_strategy(session, "ESV2-sd-foreign", "sd-foreign", [ids["alpha"]])
    _add_instance(session, "SI-sd-foreign-1", "ESV2-sd-foreign",
                  ids["beta"], beta_seg)

    # ④非 WORK 范围：scope_ids 是作者/题材 id，作品级 unbacked 不适用
    _add_strategy(session, "ESV2-sd-author", "sd-author", [ids["author"]],
                  scope="AUTHOR")
    _add_instance(session, "SI-sd-author-1", "ESV2-sd-author",
                  ids["gamma"], gamma_seg)
    _add_instance(session, "SI-sd-author-2", "ESV2-sd-author",
                  ids["beta"], beta_seg, start=4, end=12)
    _add_strategy(session, "ESV2-sd-genre", "sd-genre", [ids["genre"]],
                  scope="GENRE")
    _add_instance(session, "SI-sd-genre-1", "ESV2-sd-genre",
                  ids["delta"], delta_seg)

    # ⑥声明了查询书、证据却在登记缺行的书旁边（未判跨作品方向）
    _add_strategy(session, "ESV2-sd-noreg", "sd-noreg", [ids["noreg"]])
    _add_instance(session, "SI-sd-noreg-1", "ESV2-sd-noreg",
                  ids["delta"], delta_seg, start=4, end=12)

    session.commit()
    return ids


@pytest.fixture()
def disclose_store(tmp_path):
    db_path = tmp_path / "scope-singlebook-disclose.db"
    write_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(write_engine)
    with sessionmaker(bind=write_engine)() as session:
        ids = _seed(session)
    write_engine.dispose()
    before = _sha256(db_path)

    db_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    read_engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(db_uri, uri=True),
        future=True,
    )

    @event.listens_for(read_engine, "connect")
    def _set_query_only(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    try:
        with sessionmaker(bind=read_engine)() as session:
            assert session.connection().exec_driver_sql(
                "PRAGMA query_only").scalar() == 1
            yield session, ids
    finally:
        read_engine.dispose()
    assert _sha256(db_path) == before


def _policy(book_id, source_policy=None):
    policy = {"contract_version": 2, "book_id": book_id,
              "semantic_requirements": {}}
    if source_policy is not None:
        policy["source_policy"] = source_policy
    return policy


def _by_key(response) -> dict:
    return {e["strategy_key"]: e for e in response["selected"]}


# ①四个新字段在每个 selected 条目上存在且类型正确
def test_disclosure_fields_present_with_right_types(disclose_store):
    session, ids = disclose_store
    response = kq.query_knowledge(_policy(ids["alpha"]), session)

    assert response["status"] == "matched"
    assert response["selected"]
    for entry in response["selected"]:
        for field in DISCLOSURE_FIELDS[:3]:
            assert field in entry, f"{entry['strategy_key']} 缺披露字段 {field}"
            assert isinstance(entry[field], int) and \
                not isinstance(entry[field], bool), \
                f"{field} 必须是 int：{entry[field]!r}"
        assert isinstance(entry[DISCLOSURE_FIELDS[3]], list)
        assert all(isinstance(i, str)
                   for i in entry[DISCLOSURE_FIELDS[3]])
        # 计数不为负；WORK 范围下 声明 = 有证据 + 零证据（去重后）
        assert entry["evidence_same_book_count"] >= 0
        assert entry["evidence_scope_ids_declared"] >= 0
        if entry["scope"] == "WORK":
            assert entry["evidence_scope_ids_with_evidence"] + \
                len(entry["evidence_scope_unbacked_ids"]) == \
                entry["evidence_scope_ids_declared"]


# ②声明 3 本、证据只来自 2 本 ⇒ unbacked 恰好点名缺证据那一本
def test_declared_three_but_evidence_two_names_the_unbacked_one(disclose_store):
    session, ids = disclose_store
    entry = _by_key(kq.query_knowledge(
        _policy(ids["alpha"]), session))["sd-partial"]

    assert entry["scope"] == "WORK"
    assert entry["evidence_root_works"] == sorted([ids["alpha"], ids["beta"]])
    assert entry["evidence_scope_ids_declared"] == 3
    assert entry["evidence_scope_ids_with_evidence"] == 2
    assert entry["evidence_scope_unbacked_ids"] == [ids["gamma"]]
    assert ids["gamma"] not in entry["evidence_root_works"]
    # 同书证据只有 1 条区间（alpha 那条），另 1 条是跨书聚合
    assert entry["evidence_same_book_count"] == 1
    assert entry["score_components"]["evidence_count"] == 2
    assert entry["evidence_cross_work"] is True
    assert entry["for_context"] is False


# ③一致时 unbacked 空；反向（声明 1 本、证据全在别书）点名唯一声明项
def test_unbacked_empty_when_declaration_matches_evidence(disclose_store):
    session, ids = disclose_store
    entries = _by_key(kq.query_knowledge(_policy(ids["alpha"]), session))

    exact = entries["sd-exact"]
    assert exact["evidence_scope_ids_declared"] == 1
    assert exact["evidence_scope_ids_with_evidence"] == 1
    assert exact["evidence_scope_unbacked_ids"] == []
    assert exact["evidence_same_book_count"] == 1
    assert exact["evidence_cross_work"] is False

    foreign = entries["sd-foreign"]
    assert foreign["evidence_scope_ids_declared"] == 1
    assert foreign["evidence_scope_ids_with_evidence"] == 0
    assert foreign["evidence_scope_unbacked_ids"] == [ids["alpha"]]
    assert foreign["evidence_same_book_count"] == 0
    assert foreign["evidence_cross_work"] is True
    assert foreign["evidence_root_works"] == [ids["beta"]]


def test_non_work_scope_does_not_emit_book_level_unbacked(disclose_store):
    """scope≠WORK：scope_ids 是作者/题材 id，作品级名单**不适用**。"""
    session, ids = disclose_store
    entries = _by_key(kq.query_knowledge(_policy(ids["alpha"]), session))

    author = entries["sd-author"]
    assert author["scope"] == "AUTHOR"
    assert author["evidence_scope_ids_declared"] == 1
    assert author["evidence_scope_ids_with_evidence"] == 0
    assert author["evidence_scope_unbacked_ids"] == []
    assert author["evidence_same_book_count"] == 0
    # 披露不改变既有跨作品报告
    assert author["evidence_root_works"] == sorted(
        [ids["beta"], ids["gamma"]])
    assert author["evidence_cross_work"] is True
    assert author["score_components"]["evidence_count"] == 2

    genre = entries["sd-genre"]
    assert genre["scope"] == "GENRE"
    assert genre["evidence_scope_ids_declared"] == 1
    assert genre["evidence_scope_ids_with_evidence"] == 0
    assert genre["evidence_scope_unbacked_ids"] == []
    assert genre["evidence_root_works"] == [ids["delta"]]


def test_query_book_without_registry_reports_zero_same_book(disclose_store):
    """查询书缺登记行：根作品不可解析 ⇒ 同书证据 0、声明项被点名。"""
    session, ids = disclose_store
    entry = _by_key(kq.query_knowledge(
        _policy(ids["noreg"]), session))["sd-noreg"]

    assert entry["evidence_cross_work"] is False
    assert "not determined" in entry["evidence_cross_work_note"]
    assert entry["evidence_same_book_count"] == 0
    assert entry["evidence_scope_ids_declared"] == 1
    assert entry["evidence_scope_ids_with_evidence"] == 0
    assert entry["evidence_scope_unbacked_ids"] == [ids["noreg"]]


# ④判定不变：用本仓 `_evidence_for` 现算值对拍 selected/证据数/分量
def test_disclosure_does_not_change_verdicts(disclose_store):
    session, ids = disclose_store
    policy = _policy(ids["alpha"])
    response = kq.query_knowledge(policy, session)
    query_root = kq._book_canonical_work_id(session, policy["book_id"])

    assert response["status"] == "matched"
    assert set(_by_key(response)) == {"sd-partial", "sd-exact", "sd-foreign",
                                      "sd-author", "sd-genre"}
    # 排序也未变：证据数降序 → 2 条的 partial/author 在前，1 条组内按
    # scope 特异性(WORK>AUTHOR>GENRE)再按 strategy_key
    assert [e["strategy_key"] for e in response["selected"]] == [
        "sd-partial", "sd-author", "sd-exact", "sd-foreign", "sd-genre"]

    for entry in response["selected"]:
        refs, ev_count, _stripped = kq._evidence_for(
            session, entry["strategy_id"], policy)
        assert entry["evidence"] == refs
        assert len(entry["evidence"]) == ev_count
        assert entry["evidence_root_works"] == sorted(
            {r["canonical_work"] for r in refs})
        assert entry["score_components"] == {
            "required_matches": 0, "good_when_matches": 0,
            "evidence_count": ev_count,
            "scope_specificity": kq.SCOPE_SPECIFICITY[entry["scope"]],
        }
        # 披露字段与现算 refs 同源（不是另跑一次查询的结果）
        assert entry["evidence_same_book_count"] == len(
            {(r["canonical_work"], tuple(r["span"])) for r in refs
             if r["canonical_work"] == query_root})
        for field in ENTRY_DECISION_FIELDS:
            assert field in entry, f"披露改动删掉了字段 {field}"
        for field in COMP_DECISION_FIELDS:
            assert field in entry["score_components"]


def test_disclosure_survives_same_book_narrowing_switch(disclose_store):
    """收窄开关：同书策略照旧，跨书/异根证据按既有口径被剔（判定未变）。"""
    session, ids = disclose_store
    policy = _policy(ids["alpha"], {"require_same_book_evidence": True})
    response = kq.query_knowledge(policy, session)
    entries = _by_key(response)

    assert response["status"] == "matched"
    assert set(entries) == {"sd-partial", "sd-exact"}
    assert {(r["strategy_key"], r["reason"]) for r in response["rejected"]} \
        == {("sd-foreign", "excluded_no_evidence"),
            ("sd-author", "excluded_no_evidence"),
            ("sd-genre", "excluded_no_evidence"),
            ("sd-noreg", "excluded_no_evidence")}

    partial = entries["sd-partial"]
    # 收窄后只剩 alpha 一条同书证据 ⇒ 声明 3 本里 2 本变成零证据
    assert partial["evidence_same_book_count"] == 1
    assert partial["evidence_scope_ids_declared"] == 3
    assert partial["evidence_scope_ids_with_evidence"] == 1
    assert partial["evidence_scope_unbacked_ids"] == [ids["beta"], ids["gamma"]]
    assert partial["evidence_cross_work"] is False
    assert partial["score_components"]["evidence_count"] == 1

    exact = entries["sd-exact"]
    assert exact["evidence_scope_unbacked_ids"] == []
    assert exact["evidence_scope_ids_with_evidence"] == 1


def test_capabilities_documents_the_four_disclosure_fields(disclose_store):
    session, _ids = disclose_store
    fields = kq.capabilities(session)["evidence_provenance"]["package_fields"]

    assert set(DISCLOSURE_FIELDS) <= set(fields)
    assert "canonical" in fields["evidence_same_book_count"]
    assert "零合格证据" in fields["evidence_scope_unbacked_ids"]
    # 既有三字段的说明仍在（只增不减）
    assert {"evidence_cross_work", "evidence_root_works",
            "evidence_cross_work_note"} <= set(fields)
