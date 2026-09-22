"""K2-A 离线骨架回归（零配额预置，FixtureClient 零真实调用）。

三道门 + live 守卫逐项钉死：预算超限显式拒、输出不完整 → unverified、
证据不符 → rejected_evidence、默认路径绝不触发真实调用。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import knowledge_extract as KE        # noqa: E402

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，缓缓道：这件事先不提。"


class _Fx:
    def __init__(self, payload=None, bad=None):
        self.payload, self.bad = payload, bad

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if self.bad == "json":
            return {"text": "不是json", "tokens_in": 1, "tokens_out": 1,
                    "actual_model": "fx"}
        body = self.payload if self.payload is not None else {
            "span_start": 0, "span_end": 8,
            "evidence_text": TEXT[0:8], "observed_content": "沉默过渡"}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}


def test_budget_gate_rejects_explicitly():
    b = KE.ExtractBudget(max_calls=2, max_tokens=100)
    b.check()
    b.check()
    with pytest.raises(KE.ExtractBudgetExceeded, match="预算超限"):
        b.check()


def test_budget_gate_token_ceiling():
    b = KE.ExtractBudget(max_calls=10, max_tokens=20)
    b.check()
    b.spend(15, 10)
    with pytest.raises(KE.ExtractBudgetExceeded, match="预算超限"):
        b.check()


def test_output_gate_complete_passes_and_recomputes_sha():
    raw = {"span_start": 0, "span_end": 8,
           "evidence_text": TEXT[0:8], "observed_content": "x"}
    clean, st = KE.gate_output(raw, TEXT, "SEG-1", "ESV2-A", "fx")
    assert st == "proposed" and clean["evidence_sha256"] == \
        KE.hashlib.sha256(TEXT[0:8].encode()).hexdigest()


def test_output_gate_missing_or_bad_fields_unverified():
    cases = [
        {"span_start": -1, "span_end": 5, "evidence_text": TEXT[0:5]},   # 负 span
        {"span_start": 2, "span_end": 1, "evidence_text": "x"},           # 倒序
        {"span_start": 0, "span_end": 999, "evidence_text": "x"},         # 越界
        {"span_start": 0, "span_end": 8, "evidence_text": "不一致文本"},   # 证据不符
        {"span_start": 0, "span_end": 8},                                 # 缺证据
        {},                                                                # 全缺
    ]
    for raw in cases:
        _, st = KE.gate_output(raw, TEXT, "SEG-1", "ESV2-A", "fx")
        assert st == "unverified", raw


def test_extract_complete_verified():
    r = KE.extract_segment(_Fx(), strategy_id="ESV2-A", strategy_version=1,
                           work_id="WK-x", segment_id="SEG-1", text=TEXT,
                           text_version="corpus-v1",
                           budget=KE.ExtractBudget())
    assert r["status"] == "verified"
    assert r["span_start"] == 0 and r["evidence_sha256"]


def test_extract_bad_json_unverified():
    r = KE.extract_segment(_Fx(bad="json"), strategy_id="ESV2-A",
                           strategy_version=1, work_id="WK-x",
                           segment_id="SEG-1", text=TEXT,
                           text_version="corpus-v1",
                           budget=KE.ExtractBudget())
    assert r["status"] == "unverified" and "invalid_extraction_json" in r["reason"]


def test_extract_budget_blocks_before_call():
    calls = {"n": 0}

    class _Count(_Fx):
        def invoke(self, **kw):
            calls["n"] += 1
            return super().invoke(**kw)
    b = KE.ExtractBudget(max_calls=1)
    KE.extract_segment(_Count(), strategy_id="A", strategy_version=1,
                       work_id="W", segment_id="S", text=TEXT,
                       text_version="v", budget=b)
    with pytest.raises(KE.ExtractBudgetExceeded):
        KE.extract_segment(_Count(), strategy_id="A", strategy_version=1,
                           work_id="W", segment_id="S", text=TEXT,
                           text_version="v", budget=b)
    assert calls["n"] == 1, "超限那次不许发起调用（花 token 前拒）"


def test_live_guard_default_off_and_requires_client():
    with pytest.raises(RuntimeError, match="client 未注入"):
        KE.extract_segment(None, strategy_id="A", strategy_version=1,
                           work_id="W", segment_id="S", text=TEXT,
                           text_version="v", budget=KE.ExtractBudget())
    # FixtureClient 默认路径完整可跑（live=False 无网关依赖）
    r = KE.extract_segment(_Fx(), strategy_id="A", strategy_version=1,
                           work_id="W", segment_id="S", text=TEXT,
                           text_version="v", budget=KE.ExtractBudget())
    assert r["status"] == "verified"


def test_gate_evidence_single_caliber():
    from app import knowledge as K
    clean = {"span_start": 0, "span_end": 8, "evidence_text": TEXT[0:8]}
    assert KE.gate_evidence(clean, TEXT) == "verified"
    clean2 = {"span_start": 0, "span_end": 8, "evidence_text": "别的内容"}
    assert KE.gate_evidence(clean2, TEXT) == "rejected_evidence"
    assert hasattr(K, "verify_instance_span")
