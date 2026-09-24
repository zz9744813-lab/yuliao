"""K3-A 卡驱动回归：27 张冻结测试卡（12 dev + 15 acceptance）。

验收纪律（监督 2026-09-22）：期望值**手写**（不是实现跑出来的答案键）；
开发卡与验收卡分开存放；error 类经 HTTP 层（400/404）；硬排除与来源
隔离（fixture 冒充/基准泄漏/镜像重复）必须全通过。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient          # noqa: E402

from knowledge_seed import seed_knowledge          # noqa: E402
from registry_anchor import anchor as _anchor      # noqa: E402  登记行内容锚同源
from app import db, knowledge_query as kq          # noqa: E402
from app.main import app                           # noqa: E402

CARDS = Path(__file__).parent / "query_cards"
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def seeded():
    seed_knowledge()
    yield
    from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,
                            StrategyInstance, Work, WorkSource)
    _WORKS = ("WK-α", "WK-β", "WK-αM", "WK-BENCH", "WK-FIX", "WK-LIC")
    with db.session() as s:
        s.query(StrategyInstance).filter(
            StrategyInstance.work_id.in_(_WORKS)).delete(
            synchronize_session=False)
        for sid in [r.id for r in s.query(ExpressionStrategyV2.id)
                    if r.id.startswith("ESV2-")]:
            s.query(StrategyCondition).filter_by(strategy_id=sid).delete(
                synchronize_session=False)
        s.query(ExpressionStrategyV2).filter(
            ExpressionStrategyV2.id.like("ESV2-%")).delete(
            synchronize_session=False)
        for wid in _WORKS:
            s.query(WorkSource).filter_by(work_id=wid).delete(
                synchronize_session=False)
            s.query(Segment).filter_by(work_id=wid).delete(
                synchronize_session=False)
            s.query(Work).filter_by(id=wid).delete(synchronize_session=False)
        s.commit()


def _cards():
    out = []
    for sub in ("dev", "acceptance"):
        for p in sorted((CARDS / sub).glob("*.json")):
            out.append(pytest.param(json.loads(p.read_text(encoding="utf-8")),
                                    id=p.stem))
    return out


def _check(card, resp):
    exp = card["expected"]
    body = resp.json() if hasattr(resp, "json") else resp
    if "status" in exp:
        assert body["status"] == exp["status"], body
    if "http_status_code" in exp:
        assert resp.status_code == exp["http_status_code"], resp.text[:200]
    if "selected_count" in exp:
        assert len(body.get("selected", [])) == exp["selected_count"], body
    keys = [e["strategy_key"] for e in body.get("selected", [])]
    for k in exp.get("must_select", []):
        assert k in keys, f"应选中 {k}，实选 {keys}"
    for k in exp.get("must_not_select", []):
        assert k not in keys, f"不该选中 {k}"
    for k, reason in exp.get("must_reject_reason", {}).items():
        hit = [r for r in body.get("rejected", [])
               if r["strategy_key"] == k and r["reason"] == reason]
        assert hit, f"{k} 应以 {reason} 拒绝，实得 {body.get('rejected')}"
    for sc in exp.get("score_component", []):
        e = next(x for x in body["selected"]
                 if x["strategy_key"] == sc["strategy_key"])
        got = e["score_components"][sc["component"]]
        assert got == sc["equals"], \
            f"{sc['strategy_key']}.{sc['component']}={got}≠{sc['equals']}"
    if "uncertain_dimension_includes" in exp:
        u = exp["uncertain_dimension_includes"]
        e = next(x for x in body["selected"]
                 if x["strategy_key"] == u["strategy_key"])
        assert any(i["dimension"] == u["dimension"] for i in e["uncertain_items"])
    if "selected_order_head" in exp:
        assert keys[:len(exp["selected_order_head"])] == \
            exp["selected_order_head"], keys
    if "context_count" in exp:
        assert body["budget"]["context"] == exp["context_count"], body["budget"]
    if "reason_contains" in exp:
        assert exp["reason_contains"] in (body.get("reason") or ""), body
    if "served_observation_status" in exp:
        for e in body["selected"]:
            assert e["served_observation_status"] == \
                exp["served_observation_status"], e
    for k in exp.get("first_selected_has_keys", []):
        assert k in body["selected"][0], k
    for k in exp.get("response_has_keys", []):
        assert k in body, k
    for frag in exp.get("response_json_not_contains", []):
        assert frag not in json.dumps(body, ensure_ascii=False), \
            f"响应泄漏原文：{frag}"
    for k in exp.get("has_keys", []):
        assert k in body, k


@pytest.mark.parametrize("card", _cards())
def test_card(card, seeded):
    entry = card["entry"]
    if entry == "service":
        with db.session() as s:
            _check(card, kq.query_knowledge(card["policy"], s))
    elif entry == "http":
        _check(card, client.post("/knowledge/query", json=card["policy"]))
    elif entry == "http_package":
        _check(card, client.get(
            f"/knowledge/packages/{card['package_id']}"))
    elif entry == "http_capabilities":
        _check(card, client.get("/knowledge/capabilities"))
    else:
        raise AssertionError(f"未知 entry: {entry}")


def test_readonly_discipline(tmp_path):
    """真库只读纪律：mode=ro 打开库副本，查询管道全程不写不炸。"""
    import sqlite3
    import sqlalchemy as sa
    from sqlalchemy.orm import Session as OrmSession
    src = Path(db.engine.url.database)
    dst = tmp_path / "ro.db"
    # WAL 库直接 copy 会丢 -wal 内容且 ro 连接打不开——用 backup API 出干净单文件
    with sqlite3.connect(str(src)) as sconn, \
            sqlite3.connect(str(dst)) as dconn:
        sconn.backup(dconn)
    eng = sa.create_engine(
        "sqlite://", creator=lambda: sqlite3.connect(
            f"file:{dst.resolve().as_posix()}?mode=ro", uri=True),
        future=True)
    with OrmSession(eng) as sess:
        resp = kq.query_knowledge(
            {"contract_version": 2, "book_id": "WK-6e5d2623",
             "semantic_requirements": {}}, sess)
        assert resp["status"] == "empty", \
            f"查询只出 verified（库内只有 hypothesis/种子 scope 不含该书）；" \
            f"只读连接全程不写不炸，实得 {resp['status']}：{resp.get('reason', '')}"


def test_package_roundtrip(tmp_path):
    """包冻结/读取闭环（临时库语义；K3-A HTTP 不暴露写端点）。"""
    with db.session() as s:
        resp = kq.query_knowledge(
            {"contract_version": 2, "book_id": "WK-α",
             "semantic_requirements": {"节奏": "短句"}}, s)
        pid = kq.freeze_package(resp, s)
        got = kq.get_package(pid, s)
        assert got and got["package_sha256"] == resp["package_sha256"]
        assert got["contract_version"] == 2
        # 幂等：再冻同包不翻倍
        assert kq.freeze_package(resp, s) == pid


def test_unavailable_on_store_error(monkeypatch):
    """库异常 → unavailable（不用旧缓存/静默回退掩盖服务故障）。"""
    def _boom(*a, **k):
        from sqlalchemy.exc import OperationalError
        raise OperationalError("stmt", {}, Exception("boom"))
    monkeypatch.setattr(kq, "fingerprint_knowledge", _boom)
    with db.session() as s:
        resp = kq.query_knowledge(
            {"contract_version": 2, "book_id": "WK-α"}, s)
    assert resp["status"] == "unavailable"
    assert "OperationalError" in resp["reason"]
    assert "boom" not in resp["reason"], "异常原文不许回显（信息外泄）"


def test_freeze_rejects_stale_snapshot(monkeypatch):
    """快照过期（库知识变了）→ 冻结响亮拒绝，不许落一个过期包。"""
    with db.session() as s:
        resp = kq.query_knowledge(
            {"contract_version": 2, "book_id": "WK-α",
             "semantic_requirements": {"时长": "独特-快照测"}}, s)
    resp["snapshot_fingerprint"] = "0" * 64          # 伪造过期快照
    with db.session() as s:
        with pytest.raises(ValueError, match="快照已变化"):
            kq.freeze_package(resp, s)


# ── 来源检索策略下限回归（审计 P1「客户端能放宽来源硬拦」对账）────────
# 口径：excluded_source_types/excluded_uses=并集（只可加不可减）；
# allowed_text_versions=交集（只可收窄不可放宽）。

def _selected_keys(resp) -> list[str]:
    return [e["strategy_key"] for e in resp.get("selected", [])]


def _rejected_pairs(resp) -> set:
    return {(r["strategy_key"], r["reason"])
            for r in resp.get("rejected", [])}


def test_caller_cannot_relax_excluded_source_types(seeded):
    """①调用方传 ["other"] 不能整集替换默认排除集——fixture 仍被拦。"""
    pol = {"contract_version": 2, "book_id": "WK-α",
           "semantic_requirements": {},
           "source_policy": {"excluded_source_types": ["other"]}}
    with db.session() as s:
        resp = kq.query_knowledge(pol, s)
    assert ("J-夹具来源", "excluded_no_evidence") in _rejected_pairs(resp), \
        resp["rejected"]
    assert "J-夹具来源" not in _selected_keys(resp)


def test_excluded_source_types_union_adds_caller_items(seeded):
    """并集语义的「加」侧：调用方新增排除类型生效（附加排除），且
    默认集不被替换——fixture 路径在同一 policy 下依旧拦截。"""
    from app.models import (ExpressionStrategyV2, StrategyInstance,
                            Work, WorkSource)
    try:
        with db.session() as s:
            s.add(Work(id="WK-OT", title="其他来源书", source="test"))
            s.flush()
            s.add(WorkSource(work_id="WK-OT", canonical_work_id="WK-OT",
                             source_type="other", text_version="corpus-v1",
                             text_sha256=_anchor(s, "WK-OT"),
                             purpose_basis="t", identity_purposes=[],
                             license_purposes=[], license_basis=None,
                             metadata_status="verified", metadata_basis="t"))
            s.add(ExpressionStrategyV2(
                id="ESV2-OT", strategy_key="O-其他来源", version=1,
                abstract_operation="x", invariants=[], effect_hypothesis="x",
                failure_modes=[], status="verified", source="seed",
                scope="WORK", scope_ids=["WK-OT"], scope_basis="t",
                observation_status="observed", effect_status="pilot_verified"))
            s.add(StrategyInstance(
                id="SI-OT1", strategy_id="ESV2-OT", strategy_version=1,
                work_id="WK-OT", segment_id="SEG-ot", frame_id=None,
                text_version="corpus-v1", span_start=0, span_end=10,
                evidence_text="x", evidence_sha256="0" * 64,
                conditions_observed={}, observed_content="",
                extractor_model="t", status="verified"))
            s.commit()
            # 不加附加排除：other 来源计入合格证据（证明附加项真的生效）
            refs0, n0, _ = kq._evidence_for(s, "ESV2-OT", {})
            assert n0 == 1 and refs0, (refs0, n0)
            pol = {"source_policy": {"excluded_source_types": ["other"]}}
            refs1, n1, st1 = kq._evidence_for(s, "ESV2-OT", pol)
            assert n1 == 0 and not refs1, (refs1, n1)
            assert "SI-OT1:excluded_source_type:other" in st1, st1
            # 默认集不被调用方集合替换：fixture 冒充路径仍拦
            refs2, n2, st2 = kq._evidence_for(s, "ESV2-J", pol)
            assert n2 == 0 and not refs2, (refs2, n2)
            assert "SI-J1:excluded_source_type:fixture" in st2, st2
    finally:
        with db.session() as s:
            s.query(StrategyInstance).filter_by(id="SI-OT1").delete(
                synchronize_session=False)
            s.query(ExpressionStrategyV2).filter_by(id="ESV2-OT").delete(
                synchronize_session=False)
            s.query(WorkSource).filter_by(work_id="WK-OT").delete(
                synchronize_session=False)
            s.query(Work).filter_by(id="WK-OT").delete(
                synchronize_session=False)
            s.commit()


def test_empty_source_policy_matches_default(seeded):
    """②传空 source_policy / 不传——与默认行为完全一致（含 fixture 仍拦）。"""
    pol = {"contract_version": 2, "book_id": "WK-α",
           "semantic_requirements": {"节奏": "短句", "视角": "限知"}}
    with db.session() as s:
        d = kq.query_knowledge(pol, s)
        e = kq.query_knowledge({**pol, "source_policy": {}}, s)
    assert _selected_keys(d) == _selected_keys(e), (d, e)
    assert _rejected_pairs(d) == _rejected_pairs(e)
    assert ("J-夹具来源", "excluded_no_evidence") in _rejected_pairs(e)


def test_allowed_text_versions_outside_default_gives_empty(seeded):
    """③默认外版本 ⇒ 交集空集 ⇒ 全部实例 stripped（text_version 理由）、
    查询空结果；调用方把默认外版本混进集合也放宽不了。

    口径注：stripped 理由里的版本号是**实例自身的 text_version**
    （app/knowledge_query.py 剥落行 f"{ins.id}:text_version:{ins.text_version}"），
    不是调用方点名的版本；调用方点名的 corpus-v0 不会出现在理由里——
    这正是「调用方取值范围不参与封底判定」的观测面。
    """
    with db.session() as s:
        refs, n, stripped = kq._evidence_for(
            s, "ESV2-A",
            {"source_policy": {"allowed_text_versions": ["corpus-v0"]}})
        assert refs == [] and n == 0, (refs, n)
        assert stripped == ["SI-A1:text_version:corpus-v1",
                            "SI-A2:text_version:corpus-v1"], stripped
        assert all("corpus-v0" not in r for r in stripped), stripped
        # 混入默认外版本不放宽：生效集仍是交集（这里是 corpus-v1 一项）
        resp = kq.query_knowledge(
            {"contract_version": 2, "book_id": "WK-α",
             "semantic_requirements": {},
             "source_policy": {"allowed_text_versions":
                               ["corpus-v1", "corpus-v9"]}}, s)
    assert resp["status"] == "matched", resp["selected"]
    assert _selected_keys(resp), "收窄到 corpus-v1 后仍有合格证据可匹配"


def test_caller_cannot_widen_text_versions_beyond_default(seeded):
    """放宽攻击面：库里造一条默认外版本（corpus-v9）实例，调用方把该版本
    写进 allowed_text_versions 也进不了合格集（交集封顶于服务端默认）。"""
    from app.models import StrategyInstance
    ins = StrategyInstance(
        id="SI-TV9", strategy_id="ESV2-A", strategy_version=2,
        work_id="WK-α", segment_id="SEG-tv9", frame_id=None,
        text_version="corpus-v9", span_start=30, span_end=40,
        evidence_text="x", evidence_sha256="0" * 64,
        conditions_observed={}, observed_content="", extractor_model="t",
        status="verified")
    try:
        with db.session() as s:
            s.add(ins)
            s.commit()
            # 基线：默认口径下默认外版本本就被拦
            refs0, _, st0 = kq._evidence_for(s, "ESV2-A", {})
            assert all(r["instance_id"] != "SI-TV9" for r in refs0)
            assert "SI-TV9:text_version:corpus-v9" in st0, st0
            # 放宽尝试：调用方点名要 corpus-v9 ⇒ 交集为空 ⇒ 连老证据也全拦
            refs1, n1, st1 = kq._evidence_for(
                s, "ESV2-A",
                {"source_policy": {"allowed_text_versions": ["corpus-v9"]}})
            assert n1 == 0 and refs1 == [], (refs1, n1)
            assert "SI-TV9:text_version:corpus-v9" in st1, st1
    finally:
        with db.session() as s:
            s.query(StrategyInstance).filter_by(id="SI-TV9").delete(
                synchronize_session=False)
            s.commit()


def test_excluded_uses_additive_semantics(seeded):
    """④excluded_uses=附加禁用用途：命中者被拦（acc-07 同口径），
    且附加排除不误伤无该用途的其他来源。"""
    pol = {"contract_version": 2, "book_id": "WK-α",
           "semantic_requirements": {"节奏": "短句", "视角": "限知"},
           "source_policy": {"excluded_uses": ["benchmark_source"]}}
    with db.session() as s:
        resp = kq.query_knowledge(pol, s)
    assert ("M-授权用途", "excluded_no_evidence") in _rejected_pairs(resp), \
        resp["rejected"]
    assert "A-短句加速" in _selected_keys(resp), resp["selected"]


def test_capabilities_reports_source_policy_floor(seeded):
    """capabilities 如实报出服务端封底口径（并集/交集语义说明）。"""
    resp = client.get("/knowledge/capabilities")
    assert resp.status_code == 200
    floor = resp.json()["source_policy_floor"]
    assert floor["excluded_source_types"] == \
        sorted(kq.DEFAULT_EXCLUDED_SOURCE_TYPES)
    assert floor["allowed_text_versions"] == \
        sorted(kq.DEFAULT_ALLOWED_TEXT_VERSIONS)
    assert "union" in floor["semantics"]["excluded_source_types"]
    assert "intersection" in floor["semantics"]["allowed_text_versions"]
