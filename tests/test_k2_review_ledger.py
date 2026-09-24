"""K2 复审台账回归（审计 P1 第 3 条：把「已得 82 条需复审」落成可对账进度 +
策略定义结构化注入 + 输出契约）。离线、零真实调用、零真库写。

覆盖：
① strategy_def=None 时 payload 与现状逐字一致（与既有 5 例不冲突）；
② 有定义时四字段逐字在 payload，且输出契约（归类理由末尾）结构化注入；
③ 只读统计入口 review_ledger_stats 在夹具库上返回正确三计数
   （含「未复审」与「新口径」各一条），且只读不改写；
④ 默认不传参数时零写库（monkeypatch 计数：SessionLocal/persist_instance 均 0 次）；
⑤ 写库入口 persist_instance 正确落 reviewer_version 标记（新口径 vs 遗留）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import knowledge_extract as KE          # noqa: E402
from app import models                            # noqa: E402

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"

DEF = {
    "abstract_operation": "以环境微变替代直接情绪描写（灯花跳一下）",
    "invariants": ["不出现情绪词", "保留动作主体"],
    "effect_hypothesis": "读者自行推得人物克制情绪，效果强于直陈",
    "failure_modes": ["环境变化与情绪无关联时成摆设"],
}


def _budget():
    return KE.ExtractBudget(max_calls=10, max_tokens=50_000)


class _Capture:
    """测试 client：捕获 system/payload，回一个可配置模型应答（零真实调用）。"""
    def __init__(self, reply: dict | None = None):
        self.reply = reply if reply is not None else {
            "text": json.dumps(
                {"span_start": 0, "span_end": 8,
                 "evidence_text": TEXT[0:8], "observed_content": "克制沉默"},
                ensure_ascii=False),
            "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}
        self.calls: list[dict] = []

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        self.calls.append({"role": role, "system": system,
                           "payload": payload, "max_tokens": max_tokens,
                           "timeout": timeout})
        return self.reply


def _extract(client, **kw):
    return KE.extract_segment(
        client, strategy_id="ESV2-x", strategy_version=1,
        work_id="w1", segment_id="seg1", text=TEXT, text_version="tv1",
        budget=_budget(), **kw)


# ① 默认 payload 逐字一致
def test_default_payload_verbatim():
    """strategy_def=None 时 payload 与现状逐字一致（{"strategy_id","text"}），
    system 无「抽象操作」前缀。与 test_payload_unchanged_without_definition 同口径、
    不冲突（本文件独立）。"""
    c = _Capture()
    _extract(c)
    assert c.calls[0]["payload"] == {"strategy_id": "ESV2-x", "text": TEXT}
    assert "抽象操作" not in c.calls[0]["system"]


# ② 有定义时四字段逐字在 payload + 输出契约
def test_defined_payload_four_fields_and_contract():
    """有定义时 payload 含 strategy 四字段（逐字）且新增 output_contract
    （归类理由末尾字段）；system 也要求 classification_rationale 置末尾。
    默认路径不含 output_contract（逐字不变）。"""
    c = _Capture()
    _extract(c, strategy_def=dict(DEF))
    p = c.calls[0]["payload"]
    # 四字段逐字
    assert p["strategy"]["abstract_operation"] == DEF["abstract_operation"]
    assert p["strategy"]["invariants"] == DEF["invariants"]
    assert p["strategy"]["effect_hypothesis"] == DEF["effect_hypothesis"]
    assert p["strategy"]["failure_modes"] == DEF["failure_modes"]
    # 输出契约结构化注入
    assert p["output_contract"]["classification_rationale_last"] is True
    assert "classification_rationale" in p["output_contract"]["note"]
    assert "classification_rationale" in c.calls[0]["system"]
    # 默认路径不得混入 output_contract
    c0 = _Capture()
    _extract(c0)
    assert "output_contract" not in c0.calls[0]["payload"]


# ③ 只读统计三计数（夹具库）
@pytest.fixture
def fixture_session():
    eng = create_engine("sqlite:///:memory:")     # 独立内存库，绝不碰真库
    models.Base.metadata.create_all(eng)
    s = Session(eng)
    # 一条 legacy（未复审）：reviewer_version 空
    s.add(models.StrategyInstance(
        strategy_id="ESV2-A", strategy_version=1, work_id="W1",
        segment_id="S1", text_version="tv1", span_start=0, span_end=3,
        evidence_text="他站", evidence_sha256="sha-legacy",
        observed_content="o1", extractor_model="fx", status="verified",
        reviewer_version=""))
    # 一条新口径（已按带 strategy_def 复审）：reviewer_version = k2def-v1
    s.add(models.StrategyInstance(
        strategy_id="ESV2-B", strategy_version=1, work_id="W1",
        segment_id="S2", text_version="tv1", span_start=0, span_end=3,
        evidence_text="他站", evidence_sha256="sha-new",
        observed_content="o2", extractor_model="fx", status="verified",
        reviewer_version=KE.REVIEW_MARKER_NEW_DEF))
    s.commit()
    yield s
    s.close()


def test_review_ledger_stats_counts(fixture_session):
    """只读统计：n_total / n_reviewed_new_def / n_legacy 正确，且只读不改写。"""
    stats = KE.review_ledger_stats(fixture_session)
    assert stats == {"n_total": 2, "n_reviewed_new_def": 1, "n_legacy": 1}
    # 只读钉死：调用后 session 无 pending 写（没偷偷 add/commit）
    assert len(fixture_session.new) == 0


# ④ 默认不传参数零写库
def test_default_extract_zero_writes(monkeypatch):
    """默认（不传 strategy_def）抽取必须零写库：唯一写入口 persist_instance
    不被调用，且即便 monkeypatch 掉 SessionLocal 也 0 次 add/commit。
    同时默认返回结果不含 reviewer_version（不会伪装成新口径已复审）。"""
    writes = {"add": 0, "commit": 0}

    class _FakeSession:
        def add(self, obj):
            writes["add"] += 1
        def commit(self):
            writes["commit"] += 1

    class _FakeLocal:
        def __call__(self):
            return _FakeSession()

    monkeypatch.setattr(KE, "SessionLocal", _FakeLocal, raising=False)

    persist_calls = {"n": 0}

    def _spy_persist(session, result):
        persist_calls["n"] += 1
        return None

    monkeypatch.setattr(KE, "persist_instance", _spy_persist)

    c = _Capture()
    r_default = _extract(c)                     # 默认，无 strategy_def
    assert writes["add"] == 0 and writes["commit"] == 0
    assert persist_calls["n"] == 0
    assert "reviewer_version" not in r_default  # 默认不伪装成已复审

    # 即便给了定义，extract_segment 仍是纯函数、不自动写库
    # （标记只进返回 dict，落库由调用方显式 persist_instance）
    c2 = _Capture()
    r_def = _extract(c2, strategy_def=dict(DEF))
    assert writes["add"] == 0 and persist_calls["n"] == 0
    assert r_def.get("reviewer_version") == KE.REVIEW_MARKER_NEW_DEF


# ⑤ 写库入口正确落标记
def test_persist_instance_carries_review_marker():
    """persist_instance 是 K2 实例唯一写库入口：默认结果落 legacy（reviewer_version 空），
    新口径结果落 k2def-v1；status 原样（绝不批量改）。写后 read 即可对账。"""
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = Session(eng)

    res_legacy = {
        "strategy_id": "ESV2-A", "strategy_version": 1, "work_id": "W1",
        "segment_id": "S1", "text_version": "tv1", "span_start": 0,
        "span_end": 3, "evidence_text": "他站", "evidence_sha256": "sha-1",
        "observed_content": "o1", "extractor_model": "fx", "status": "verified",
    }
    KE.persist_instance(s, res_legacy)          # 无 reviewer_version → legacy

    res_new = dict(res_legacy, segment_id="S2", evidence_sha256="sha-2",
                   reviewer_version=KE.REVIEW_MARKER_NEW_DEF)
    KE.persist_instance(s, res_new)            # 带标记 → 新口径

    s.commit()
    stats = KE.review_ledger_stats(s)
    assert stats == {"n_total": 2, "n_reviewed_new_def": 1, "n_legacy": 1}
    s.close()
