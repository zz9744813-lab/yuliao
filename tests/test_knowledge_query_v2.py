"""K3-A 查询侧 v2 策略口径回归（合并方案 20260923；离线临时库）。

钉死两件事：
1. **默认行为逐字不变**——不传 `versions` 时结果与加参前完全一致
   （golden 字面量手写，不是实现跑出来的答案键）；
2. **门禁没有放宽**——`hypothesis` 行在默认/`{"1"}`/`{"2"}`/`{"1","2"}`
   任何调用下都不出现，升格只可能由上游写侧改 status 完成。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, knowledge_query as kq          # noqa: E402
from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,  # noqa: E402
                        StrategyInstance, Work, WorkSource)

TXT = "夜里起了风，灯芯轻轻跳了一下，他坐在桌前把没写完的信重新拿起又放下。"
BOOK = "WK-KQV2-1"
P = "KQV2-"                      # 独立前缀：不撞 K3-A 冻结卡种子的清理口径

# (id, strategy_key, version, status, observation_status, 证据区间)
ROWS = [
    ("KQV2-V1OK", "V1-OK", 1, "verified", "observed", [(0, 10), (12, 22)]),
    ("KQV2-V1HYP", "V1-HYP", 1, "hypothesis", "observed", [(0, 10)]),
    ("KQV2-V2OK", "V2-OK", 2, "verified", "observed", [(0, 10)]),
    ("KQV2-V2HYP", "V2-HYP", 2, "hypothesis", "observed", [(0, 10)]),
    ("KQV2-V2REP", "V2-REP", 2, "verified", "replicated", [(0, 10)]),
    # 契约外版本（脏数据）：默认按 status 口径仍入选，显式 versions 才收窄
    ("KQV2-V3OK", "V3-OK", 3, "verified", "observed", [(0, 10)]),
]

POLICY = {"contract_version": 2, "book_id": BOOK,
          "semantic_requirements": {"节奏": "短句"}}


@pytest.fixture(scope="module", autouse=True)
def seeded():
    """临时库里造 v1/v2 混合语料（含 hypothesis 行），用完即清。"""
    db.init_db()
    with db.session() as s:
        _purge(s)
        s.add(Work(id=BOOK, title="口径书", source="file:kqv2"))
        s.flush()                                # Work 先落，免 FK 撞序
        seg = Segment(work_id=BOOK, ordinal=0, text=TXT, role=None,
                      n_sentences=1, n_chars=len(TXT))
        s.add(seg)
        s.flush()
        s.add(WorkSource(work_id=BOOK, canonical_work_id=BOOK, author_id=None,
                         genre_ids=[], source_type="human_fiction",
                         text_version="corpus-v1", text_sha256="0" * 64,
                         purpose_basis="t", identity_purposes=["research"],
                         license_purposes=[], license_basis=None,
                         metadata_status="verified", metadata_basis="t"))
        for sid, key, ver, status, obs, spans in ROWS:
            s.add(ExpressionStrategyV2(
                id=sid, strategy_key=key, version=ver,
                abstract_operation=f"{key} 的抽象操作", invariants=["不改动事实"],
                effect_hypothesis="对照任务 X", failure_modes=["过度使用"],
                status=status, source="kqv2", scope="WORK", scope_ids=[BOOK],
                scope_basis="t", observation_status=obs,
                effect_status="pilot_verified"))
            for i, (a, b) in enumerate(spans):
                s.add(StrategyInstance(
                    id=f"{sid}-SI{i}", strategy_id=sid, strategy_version=ver,
                    work_id=BOOK, segment_id=seg.id, frame_id=None,
                    text_version="corpus-v1", span_start=a, span_end=b,
                    evidence_text=TXT[a:b], evidence_sha256="0" * 64,
                    conditions_observed={}, observed_content="观察",
                    extractor_model="t", status="verified"))
        # V1-OK 带一条命中 good_when（可解释分量），其余无条件的策略不掺噪声
        s.add(StrategyCondition(strategy_id="KQV2-V1OK", strategy_version=1,
                               kind="good_when", dimension="节奏", operator="eq",
                               value={"v": "短句"}, required=False,
                               predicate_state="unknown"))
        s.commit()
    yield
    with db.session() as s:
        _purge(s)
        s.commit()


def _purge(s) -> None:
    ids = [r[0] for r in ROWS]
    s.query(StrategyInstance).filter(
        StrategyInstance.strategy_id.in_(ids)).delete(synchronize_session=False)
    s.query(StrategyCondition).filter(
        StrategyCondition.strategy_id.in_(ids)).delete(synchronize_session=False)
    s.query(ExpressionStrategyV2).filter(
        ExpressionStrategyV2.id.in_(ids)).delete(synchronize_session=False)
    s.query(WorkSource).filter_by(work_id=BOOK).delete(synchronize_session=False)
    s.query(Segment).filter_by(work_id=BOOK).delete(synchronize_session=False)
    s.query(Work).filter_by(id=BOOK).delete(synchronize_session=False)


def _q(s, **kw):
    return kq.query_knowledge(dict(POLICY), s, **kw)


def _keys(resp) -> list[str]:
    return [e["strategy_key"] for e in resp.get("selected", [])]


def _pairs(resp) -> set:
    return {(r["strategy_key"], r["reason"]) for r in resp.get("rejected", [])}


# ── ① 门禁常量：参数化，但默认口径与合并前同集合 ────────────────────
def test_eligible_statuses_default_is_legacy_caliber():
    assert kq.eligible_statuses() == {"verified"}
    assert kq.eligible_statuses() is kq.ELIGIBLE_STATUS
    for v in ("1", "2", 1, 2):        # int/str 同物
        assert kq.eligible_statuses(v) == {"verified"}
    # 契约外版本不猜放宽：落兜底（=当前默认口径）
    assert kq.eligible_statuses(99) == kq.ELIGIBLE_STATUS_BY_VERSION["1"]
    assert set(kq.ELIGIBLE_STATUS_BY_VERSION) == {"1", "2"}


def test_hypothesis_is_eligible_in_no_version():
    """没升格就不给：任何版本口径里 hypothesis 都不合格（本任务不改 status）。"""
    assert "hypothesis" not in kq.ELIGIBLE_STATUS
    for v in kq.ELIGIBLE_STATUS_BY_VERSION:
        assert "hypothesis" not in kq.eligible_statuses(v), v
    assert kq.eligible_statuses("2") == kq.eligible_statuses("1")


def test_eligible_statuses_returns_immutable_gate():
    gate = kq.eligible_statuses("2")
    assert isinstance(gate, frozenset)
    with pytest.raises(AttributeError):
        gate.add("hypothesis")


# ── ② 默认行为钉死（golden 字面量手写）─────────────────────────────
def test_default_call_is_byte_identical_to_golden(seeded):
    with db.session() as s:
        resp = _q(s)
    assert resp["status"] == "matched"
    assert [(e["strategy_key"], e["version"], e["status"],
             e["observation_status"],
             e["score_components"]["required_matches"],
             e["score_components"]["good_when_matches"],
             e["score_components"]["evidence_count"],
             e["score_components"]["scope_specificity"])
            for e in resp["selected"]] == [
        ("V1-OK", 1, "verified", "observed", 0, 1, 2, 4),
        ("V2-OK", 2, "verified", "observed", 0, 0, 1, 4),
        ("V2-REP", 2, "verified", "replicated", 0, 0, 1, 4),
        ("V3-OK", 3, "verified", "observed", 0, 0, 1, 4),
    ], resp["selected"]
    assert resp["rejected"] == []
    # considered=4：两条 hypothesis 行连候选集都不进（不是被别的理由挤掉）
    assert resp["budget"] == {"considered": 4, "passed": 4, "selected": 4,
                              "context": 0, "chars": 0}


def test_versions_none_equals_omitting_the_kwarg(seeded):
    """显式 versions=None ≡ 不传参（None=全部版本，行为不变）。"""
    with db.session() as s:
        a = _q(s)
        b = _q(s, versions=None)
        c = _q(s, versions={"1", "2", "3"})
    assert a == b
    assert _keys(a) == _keys(c) and a["budget"] == c["budget"]


# ── ③ 按版本查：v1/v2 互不越界 ────────────────────────────────────
def test_version_2_returns_only_v2_rows(seeded):
    with db.session() as s:
        resp = _q(s, versions={"2"})
    assert _keys(resp) == ["V2-OK", "V2-REP"], resp["selected"]
    assert all(e["version"] == 2 for e in resp["selected"])
    assert resp["budget"]["considered"] == 2      # 版本过滤发生在候选筛选
    assert resp["rejected"] == []


def test_version_1_returns_only_v1_rows(seeded):
    with db.session() as s:
        resp = _q(s, versions={"1"})
    assert _keys(resp) == ["V1-OK"], resp["selected"]
    assert resp["selected"][0]["version"] == 1
    assert resp["budget"]["considered"] == 1


def test_multi_version_set_is_union_of_buckets(seeded):
    with db.session() as s:
        resp = _q(s, versions={"1", "2"})
    assert _keys(resp) == ["V1-OK", "V2-OK", "V2-REP"], resp["selected"]
    assert "V3-OK" not in _keys(resp), "契约外版本只在显式全集里出现"


def test_empty_version_set_selects_nothing(seeded):
    """{} = 点名「零个版本」≠ None（全部版本）——不静默回退成全量。"""
    with db.session() as s:
        resp = _q(s, versions=set())
    assert resp["status"] == "empty" and resp["selected"] == []
    assert resp["budget"]["considered"] == 0


# ── ④ hypothesis 在任何调用下都不出现（默认与按版本查一致）─────────
def test_hypothesis_rows_never_selected_in_any_call(seeded):
    for vers in (None, {"1"}, {"2"}, {"1", "2"}, {"1", "2", "3"}):
        with db.session() as s:
            resp = _q(s, versions=vers)
        keys = _keys(resp)
        assert "V1-HYP" not in keys and "V2-HYP" not in keys, vers
        # 也没进候选集（不是「进来了再被别的理由挤掉」）
        assert not [k for k in ("V1-HYP", "V2-HYP")
                    if any(k == r["strategy_key"] for r in resp["rejected"])], \
            (vers, _pairs(resp))
        assert resp["budget"]["considered"] == len(
            [k for k in keys]), (vers, resp["budget"])


def test_policy_cannot_smuggle_eligibility_widening(seeded):
    """policy 里塞 versions/eligible_statuses 不生效：门禁只认服务端常量。"""
    pol = {**POLICY, "versions": ["2"], "eligible_statuses": ["hypothesis"]}
    with db.session() as s:
        resp = kq.query_knowledge(pol, s)
    assert _keys(resp) == ["V1-OK", "V2-OK", "V2-REP", "V3-OK"], resp["selected"]
    assert "V1-HYP" not in _keys(resp)


# ── ⑤ 收据字段：上层写包/冻结时能区分新旧口径 ──────────────────────
def test_selected_entries_carry_key_version_status(seeded):
    with db.session() as s:
        resp = _q(s)
    for e in resp["selected"]:
        assert {"strategy_key", "version", "status"} <= set(e), e
    assert {(e["strategy_key"], e["version"], e["status"])
            for e in resp["selected"]} == {
        ("V1-OK", 1, "verified"), ("V2-OK", 2, "verified"),
        ("V2-REP", 2, "verified"), ("V3-OK", 3, "verified")}
