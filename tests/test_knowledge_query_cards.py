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
