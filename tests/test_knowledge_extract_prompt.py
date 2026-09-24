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

import pytest  # 用于 ② 串味负例的 xfail 已知缺口钉死

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


# ═══════════════════════════════════════════════════════════════════
# 多策略误归类负例组（审计 P1 第 3 条后半句：用多策略负例测归类）
#
# 离线、零真实调用：夹具回什么模型就"说"什么——本组钉的是
# extract_segment 在**当前实现下**的归类行为，绝不修改实现去迎合期望。
# 关键事实：当前三道门（预算/输出/证据）只核 span 逐字 + 字段完整，
# 从不比对 observed_content 与策略抽象操作的语义 →
# "串味"（给 B 的定义、observed_content 却描述 C）现状照常放行。
# ═══════════════════════════════════════════════════════════════════

# 三条彼此可区分的抽象操作策略——借以制造"串味"：
#   B = 少动作直给疑问（本组要验的目标）
#   C = 铺陈渲染（环境景物层层叠加烘托）——与 B 在表层文本上可重叠
#   A = 环境微变替代情绪描写（沿用文件顶部 DEF）
MULTI_TEXT = ("窗外的雨忽然停了。他没抬头，指节敲了敲桌沿，"
              "低声问：你打算瞒我到几时？")

DEF_B = {
    "abstract_operation": "少动作直给疑问：以极简动作配合一句短问直戳，不给铺陈",
    "invariants": ["动作极简", "必须含一句短问", "不给铺陈渲染"],
    "effect_hypothesis": "读者被短问直接击中，情绪无缓冲",
    "failure_modes": ["短问与上下文无关时成突兀"],
}
DEF_C = {
    "abstract_operation": "铺陈渲染：以层层环境景物叠加烘托情绪",
    "invariants": ["环境景物叠加", "情绪靠烘托不靠直说"],
    "effect_hypothesis": "情绪被景物浸透，余味更长",
    "failure_modes": ["景物与情绪无关联时成堆砌"],
}


def _extract_text(text, client, *, strategy_id="ESV2-x", strategy_def=None):
    """本组专用抽取入口（允许自定义 text 与 strategy_def；不复用顶部
    _extract 以免改动其既定语义）。"""
    return KE.extract_segment(
        client, strategy_id=strategy_id, strategy_version=1,
        work_id="w1", segment_id="seg1", text=text, text_version="tv1",
        budget=_budget(), strategy_def=strategy_def)


def test_multistrategy_positive_B_verified_by_its_def():
    """①正例：给 B 的定义，夹具回一条 B 的真实实例（逐字子串 + 合法
    offset）→ 应 verified。证明"给定义后正例仍走通"，与 ②③ 对照。
    变异推演：回退 strategy 注入 → 本例仍绿（注入只补输入不改归类），
    故本例主要价值是给 ② 串味问题提供"正确归类长啥样"的基线。"""
    span = "指节敲了敲桌沿，低声问：你打算瞒我到几时？"
    i = MULTI_TEXT.find(span)
    reply = {"text": json.dumps(
        {"span_start": i, "span_end": i + len(span),
         "evidence_text": span,
         "observed_content": "极简动作（敲桌沿）配合一句直戳的短问，"
                             "不给任何铺陈，正是 B 的少动作直给疑问"},
        ensure_ascii=False), "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}
    c = _Capture(reply=reply)
    r = _extract_text(MULTI_TEXT, c, strategy_id="ESV2-B", strategy_def=dict(DEF_B))
    assert r["status"] == "verified"
    # payload 确实带上了 B 的四字段
    p = c.calls[0]["payload"]
    assert p["strategy"]["abstract_operation"] == DEF_B["abstract_operation"]
    assert p["strategy"]["invariants"] == DEF_B["invariants"]
    assert p["strategy"]["effect_hypothesis"] == DEF_B["effect_hypothesis"]
    assert p["strategy"]["failure_modes"] == DEF_B["failure_modes"]


def test_multistrategy_positive_C_verified_by_its_def():
    """①补充：给 C 的定义，夹具回 C 的真实实例（雨停景物烘托）→ verified。
    同一段文本既含 B 可抽的短问、也含 C 可抽的景物烘托——分类正确与否
    取决于 observed_content 是否对应所给策略；当前实现不看 observed_content，
    所以 ② 才会把"描述 C 的回复"照常放行（verified）。"""
    span = "窗外的雨忽然停了。"
    i = MULTI_TEXT.find(span)
    reply = {"text": json.dumps(
        {"span_start": i, "span_end": i + len(span),
         "evidence_text": span,
         "observed_content": "雨停的景物被点出，不直说情绪而靠烘托，"
                             "正是 C 的铺陈渲染"},
        ensure_ascii=False), "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}
    c = _Capture(reply=reply)
    r = _extract_text(MULTI_TEXT, c, strategy_id="ESV2-C", strategy_def=dict(DEF_C))
    assert r["status"] == "verified"


def test_crossflavor_currently_passes_known_gap():
    """②串味负例——**实际行为钉死**（不许改实现迎合期望）：
    给 B 的定义（少动作直给疑问），但夹具 observed_content 描述的是另一条
    策略 C（铺陈渲染），span 仍逐字合法。当前实现只核对 span 逐字 + 字段
    完整、从不比对 observed_content 与策略语义 → 实际放行（verified）。
    本例如实钉住该行为。这是已知缺口，期望行为见
    test_crossflavor_should_be_blocked（xfail 钉住，注明机械口径为何不可分）。"""
    span = "窗外的雨忽然停了。"
    i = MULTI_TEXT.find(span)
    reply = {"text": json.dumps(
        {"span_start": i, "span_end": i + len(span),
         "evidence_text": span,
         "observed_content": "此处铺陈渲染到位：雨停的景物被层层叠加，"
                             "烘托出欲说还休的情绪，是 C 策略的典型体现"},
        ensure_ascii=False), "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}
    c = _Capture(reply=reply)
    r = _extract_text(MULTI_TEXT, c, strategy_id="ESV2-B", strategy_def=dict(DEF_B))
    # 实际行为：放行（现状）
    assert r["status"] == "verified"
    # 钉子：payload 带的是 B 的定义，而 observed_content 描述的是 C——
    # 离线机械口径下实现无法发现这处串味（这就是缺口所在）
    assert c.calls[0]["payload"]["strategy"]["abstract_operation"] == \
        DEF_B["abstract_operation"]
    assert "铺陈渲染" in r["observed_content"]


@pytest.mark.xfail(strict=True,
    reason="已知缺口：机械口径无法区分串味。observed_content 与策略抽象操作"
           "的语义对齐需要模型判（或人工复审），零真实调用下不可分；现状放行。"
           "strict=True：一旦实现能拦下、本例变 XPASS，套件会红，强制移除 xfail "
           "并更新交付，防止「悄悄修了又不说」。")
def test_crossflavor_should_be_blocked():
    """②串味负例——**期望行为**（当前未实现，xfail 钉住）：
    给 B 的定义、observed_content 却描述 C → 应当 unverified（拦下）。
    当前实现做不到，故本例预期失败（xfail）。一旦引入模型判据或人工复审门，
    本例将转为通过，届时须删除该 xfail 并据实改写交付文档。"""
    span = "窗外的雨忽然停了。"
    i = MULTI_TEXT.find(span)
    reply = {"text": json.dumps(
        {"span_start": i, "span_end": i + len(span),
         "evidence_text": span,
         "observed_content": "此处铺陈渲染到位：雨停的景物被层层叠加，"
                             "烘托出欲说还休的情绪，是 C 策略的典型体现"},
        ensure_ascii=False), "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}
    c = _Capture(reply=reply)
    r = _extract_text(MULTI_TEXT, c, strategy_id="ESV2-B", strategy_def=dict(DEF_B))
    # 期望行为：串味应被拦下
    assert r["status"] == "unverified"


def test_empty_vs_defined_def_multi_strategy():
    """③空定义 vs 有定义差异（多策略场景）：同一段文本 + 同一夹具回复，
    1) strategy_def=None（旧口径）→ payload 旧形状、system 无"抽象操作"前缀；
    2) strategy_def=DEF_C → payload 逐字带 C 的四字段、system 有前缀；
    3) 多策略互不串：给 B 定义时 payload 是 B 的四字段，不是 C 的。
    证明"给定义"确实改变了发往模型的输入，且多策略各自定义独立。"""
    span = "窗外的雨忽然停了。"
    i = MULTI_TEXT.find(span)
    reply = {"text": json.dumps(
        {"span_start": i, "span_end": i + len(span),
         "evidence_text": span, "observed_content": "景物烘托"},
        ensure_ascii=False), "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}

    # 1) 旧口径（缺省 None）：payload 旧形状、无前缀
    c0 = _Capture(reply=reply)
    _extract_text(MULTI_TEXT, c0, strategy_id="ESV2-C", strategy_def=None)
    assert c0.calls[0]["payload"] == {"strategy_id": "ESV2-C", "text": MULTI_TEXT}
    assert "抽象操作" not in c0.calls[0]["system"]

    # 2) 新口径：带 C 定义，四字段逐字在 payload
    cC = _Capture(reply=reply)
    _extract_text(MULTI_TEXT, cC, strategy_id="ESV2-C", strategy_def=dict(DEF_C))
    pC = cC.calls[0]["payload"]
    assert pC["strategy"]["abstract_operation"] == DEF_C["abstract_operation"]
    assert pC["strategy"]["invariants"] == DEF_C["invariants"]
    assert pC["strategy"]["effect_hypothesis"] == DEF_C["effect_hypothesis"]
    assert pC["strategy"]["failure_modes"] == DEF_C["failure_modes"]
    assert "抽象操作" in cC.calls[0]["system"]

    # 3) 多策略互不串：给 B 定义时 payload 是 B 的四字段，不是 C 的
    cB = _Capture(reply=reply)
    _extract_text(MULTI_TEXT, cB, strategy_id="ESV2-B", strategy_def=dict(DEF_B))
    assert cB.calls[0]["payload"]["strategy"]["abstract_operation"] == \
        DEF_B["abstract_operation"]
    assert cB.calls[0]["payload"]["strategy"]["abstract_operation"] != \
        DEF_C["abstract_operation"]
