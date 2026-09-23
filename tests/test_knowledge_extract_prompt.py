"""K2 抽取请求携带策略定义正文回归（审计 P1 2026-09-23：K2 模型没有收到
策略定义——即便引用位置机械正确，模型不知道自己在找什么）。

钉住的事：
1. 注入定义后 payload 的 `strategy` 四字段逐字等于传入值（不是「非空」）；
2. 不传定义时 payload 与旧形状完全一致（回归钉死不红）；
3. system 文本出现抽象操作语义要求（「你抽的是该抽象操作的实例」）；
4. 模型回 `{"none": true}` 仍走原路径（unverified/no_instance 口径不变）；
5. 证据门未被放松：伪造 span（evidence 非 text 子串）仍被拒。

断言须能区分新旧实现：回退 app/knowledge_extract.py 的改动应有用例变红
（变异推演见各用例注释）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import knowledge_extract as KE  # noqa: E402

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
    """测试 client：捕获 system/payload，回一个可配置的模型应答。"""
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


def test_payload_carries_strategy_definition():
    """变异推演：回退 payload 改动（strategy 键不进 payload）→ 本例红
    （KeyError 'strategy'）。四字段逐字断言，非空不算过。"""
    c = _Capture()
    _extract(c, strategy_def=dict(DEF))
    p = c.calls[0]["payload"]
    assert p["strategy"]["abstract_operation"] == DEF["abstract_operation"]
    assert p["strategy"]["invariants"] == DEF["invariants"]
    assert p["strategy"]["effect_hypothesis"] == DEF["effect_hypothesis"]
    assert p["strategy"]["failure_modes"] == DEF["failure_modes"]
    # 既有键不被挤掉
    assert p["strategy_id"] == "ESV2-x" and p["text"] == TEXT


def test_payload_unchanged_without_definition():
    """回归钉死：不传定义（缺省 None）时 payload 与旧形状完全一致——
    多一个键也不行。变异推演：无条件把 strategy 塞进 payload → 本例红。"""
    c = _Capture()
    _extract(c)
    assert c.calls[0]["payload"] == {"strategy_id": "ESV2-x", "text": TEXT}
    # 显式 None 同样保持旧形状
    c2 = _Capture()
    _extract(c2, strategy_def=None)
    assert c2.calls[0]["payload"] == {"strategy_id": "ESV2-x", "text": TEXT}


def test_system_prompt_mentions_abstract_operation():
    """system 文本必须出现抽象操作语义要求（「你抽的是该抽象操作的实例」）。
    变异推演：回退 system 前缀改动 → 本例红。不传定义时不得混入前缀。"""
    c = _Capture()
    _extract(c, strategy_def=dict(DEF))
    sys_text = c.calls[0]["system"]
    assert "抽象操作" in sys_text
    assert "你抽的是" in sys_text
    # 输出契约字段仍在（不因改输入丢契约）
    for k in ("span_start", "span_end", "evidence_text",
              "observed_content", '"none": true'):
        assert k in sys_text
    c2 = _Capture()
    _extract(c2)
    assert "抽象操作" not in c2.calls[0]["system"]


def test_none_true_still_returns_unverified():
    """模型回 {"none": true} 仍走原路径：unverified + no_instance_claimed
    口径不变（不因改输入而变）。变异推演：改 none 分支 → 回归套件红。"""
    c = _Capture(reply={"text": '{"none": true}', "tokens_in": 3,
                        "tokens_out": 1, "actual_model": "fx"})
    r = _extract(c, strategy_def=dict(DEF))
    assert r["status"] == "unverified"
    assert r["raw"] == {"none": True}
    r2 = _extract(_Capture(reply=c.reply))   # 不传定义同口径
    assert r2["status"] == "unverified" and r2["raw"] == {"none": True}


def test_output_gate_still_rejects_fake_span():
    """证据门/输出门未被放松：evidence 不是 text 逐字子串（伪造 span）
    仍被拒（unverified）。变异推演：放松 gate_output → 本例红。"""
    fake = {"span_start": 0, "span_end": 5,
            "evidence_text": "伪造的原文", "observed_content": "x"}
    c = _Capture(reply={"text": json.dumps(fake, ensure_ascii=False),
                        "tokens_in": 5, "tokens_out": 2,
                        "actual_model": "fx"})
    r = _extract(c, strategy_def=dict(DEF))
    assert r["status"] == "unverified" and r["reason"] == "output_gate"
    # repair_span 救不了不存在的子串：find < 0 原样返回，门照拦
    assert r["raw"]["evidence_text"] == "伪造的原文"
