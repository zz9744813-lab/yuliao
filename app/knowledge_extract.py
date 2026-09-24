"""K2-A 离线骨架（零配额预置版，2026-09-22）：预算闸 + 输出门 + 证据门。

**拍板后一条命令真跑**：默认全离线（client 注入 FixtureClient 或
gateway 的 mock 模式）；真实调用只在显式 live=True 时经既有网关
（app.gateway.chat，A05 逐次记账/A06 完成原因门自动生效）。本模块
不 import 网关——client 由调用方注入，离线测试零网关依赖。

三道门（监督 2026-09-22 口径）：
1. **总预算闸**（ExtractBudget）：每次调用前检查已耗 calls/tokens，
   超限 → 显式 blocked_budget 状态 + RuntimeFault，禁静默放行；
2. **完整输出门**（gate_output）：抽取输出逐项校验——span 越界、
   evidence_text 缺失、sha 不符、必填字段缺失 → status=unverified
   （候选保留、不入 verified），不完整不许冒充完整；
3. **证据门**（verify_instance_span 复用）：text[span]==evidence 必须
   逐字相符（app.knowledge 单一口径），不符 → rejected_evidence。

产物状态机：proposed →（输出门+证据门过）verified /（不过）unverified
或 rejected_evidence；预算超限 → blocked_budget（已抽出的候选照实
保留，不丢弃）。跨作品复现 = 同一 strategy_key 在 ≥2 根作品上有
verified 实例（is_replicated 查询口径，K2 收尾时消费）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_

from . import knowledge as K
from .models import StrategyInstance

# K2 复审台账标记（审计 P1 第 3 条）：复用 strategy_instances.reviewer_version 列，
# 不新增/不删列。值语义：REVIEW_MARKER_NEW_DEF = 已按带 strategy_def 的新口径复审；
# 空（""）或缺 = 未复审（legacy，即审计所说「需复审」的那批）。写后由
# review_ledger_stats 读，可对账「82 条需复审」的进度。
REVIEW_MARKER_NEW_DEF = "k2def-v1"

# 输出契约（审计 P1 第 3 条）：要求模型把归类理由放 JSON 末尾字段，
# 与 strategy_def 一并结构化注入 payload（strategy_def 非空时）。
OUTPUT_CONTRACT = {
    "require_fields": ["span_start", "span_end", "evidence_text", "observed_content"],
    "classification_rationale_last": True,
    "note": "归类理由必须放在 JSON 最后一个字段 classification_rationale，置于末尾；"
            "整段无实例时仍输出 {\"none\": true}。",
}


class ExtractBudgetExceeded(RuntimeError):
    """预算超限：显式拒绝，不静默放行（调用方转 blocked_budget 状态）。"""


@dataclass
class ExtractBudget:
    """一次抽取 run 的总预算闸（每次调用前检查）。"""
    max_calls: int = 20
    max_tokens: int = 50_000
    calls: int = 0
    tokens: int = 0

    def check(self) -> None:
        """调用前闸（只查+计数，不记 token）：已到限即拒——
        超限那次不许发起调用（花 token 前拒绝）。"""
        if self.calls >= self.max_calls or self.tokens >= self.max_tokens:
            raise ExtractBudgetExceeded(
                f"预算超限：calls {self.calls}/{self.max_calls}，"
                f"tokens {self.tokens}/{self.max_tokens}——显式拒绝，"
                "已抽候选照实保留")
        self.calls += 1

    def spend(self, tokens_in: int, tokens_out: int) -> None:
        """调用后记实际 token（check 已计过调用数）。"""
        self.tokens += tokens_in + tokens_out


REQUIRED_MODEL_FIELDS = ("span_start", "span_end", "evidence_text",
                        "observed_content")


def gate_output(raw: dict, text: str, segment_id: str, strategy_id: str,
               extractor_model: str) -> tuple[dict, str]:
    """完整输出门：返回 (干净候选 dict, status)。

    status ∈ proposed（门全过）/ unverified（缺字段/越界/sha 不符——
    候选保留但绝不入 verified）。evidence sha 在门内重算（不信模型自报）。"""
    miss = [k for k in REQUIRED_MODEL_FIELDS if raw.get(k) is None]   # 0 是合法 span，只查 None
    # span 合法性
    try:
        s0, s1 = int(raw.get("span_start", -1)), int(raw.get("span_end", -1))
    except (TypeError, ValueError):
        return raw, "unverified"
    if miss or s0 < 0 or s1 <= s0 or s1 > len(text):
        return raw, "unverified"
    evidence = raw.get("evidence_text")
    if not isinstance(evidence, str) or \
            text[s0:s1] != evidence:
        return raw, "unverified"
    sha = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    clean = {"strategy_id": strategy_id, "segment_id": segment_id,
             "span_start": s0, "span_end": s1, "evidence_text": evidence,
             "observed_content": str(raw.get("observed_content"))[:500],
             "evidence_sha256": sha, "extractor_model": extractor_model}
    return clean, "proposed"


def gate_evidence(clean: dict, text: str) -> str:
    """证据门：机械核对（单一口径 K.verify_instance_span）。"""
    if K.verify_instance_span(text, clean["span_start"], clean["span_end"],
                              clean["evidence_text"]):
        return "verified"
    return "rejected_evidence"


def repair_span(raw: dict, text: str) -> dict:
    """span 机械重定位（2026-09-23 探针实测教训）：中文字符计数是 LLM 的
    已知弱项——模型能正确引用原文短语，但 span_start/end 常数错。证据纪律
    不变：**只有 evidence_text 是 text 的逐字子串才可能通过**。本函数以
    模型引用文本在 text 中的**首次精确出现**机械重算 offset（零猜测、
    确定性）；引用不是精确子串 → 原样返回（输出门自会拦，不静默放行）。"""
    ev = raw.get("evidence_text")
    if not isinstance(ev, str) or not ev:
        return raw
    try:
        s0, s1 = int(raw.get("span_start")), int(raw.get("span_end"))
        if text[s0:s1] == ev:
            return raw                      # 本来就精确，无需修
    except (TypeError, ValueError):
        pass
    i = text.find(ev)
    if i >= 0:
        return {**raw, "span_start": i, "span_end": i + len(ev)}
    return raw


def extract_segment(client, *, strategy_id: str, strategy_version: int,
                   work_id: str, segment_id: str, text: str,
                   text_version: str, budget: ExtractBudget,
                   live: bool = False,
                   strategy_def: dict | None = None) -> dict:
    """单段抽取：预算闸→调用→输出门→证据门。返回带 status 的结果 dict。

    client 契约（与既有网关同形）：invoke(*, role, system, payload,
    max_tokens, timeout) → {"text": json_str, "tokens_in", "tokens_out"}。
    live=True 时调用方必须已注入真网关 client；默认 False 仅供测试与
    FixtureClient——拍板前任何脚本都不许传 True（回归钉死）。
    strategy_def（审计 P1 2026-09-23）：策略定义正文（abstract_operation /
    invariants / effect_hypothesis / failure_modes）——模型必须知道自己在
    找**该抽象操作**的实例，不是这一段在写什么。None 时行为与旧版完全
    一致（既有调用与测试不红）。只补输入，不放松任何输出门。"""
    if not client:
        raise RuntimeError("client 未注入（离线骨架需要显式注入测试 client）")
    budget.check()             # 调用前闸（超限在花 token 前拒）
    system = ("从文本中抽取该策略的一个实例。只输出 JSON 对象，"
              "字段与约束：span_start（整数，≥0，text 的字符"
              "偏移）、span_end（整数，>span_start）、"
              "evidence_text（字符串，必须**逐字等于** "
              "text[span_start:span_end]，不得增删改一字）、"
              "observed_content（≤500 字，描述该处如何体现"
              "该策略）。若整段找不到该策略的实例，输出 "
              '{"none": true}——不得虚构 span。')
    if strategy_def:
        system = ("你抽的是**该抽象操作**的实例，不是这一段在写什么："
                  "只有该处文本确实呈现了下方策略定义的抽象操作时才算实例。"
                  "输出 JSON 必须在最后增加一个字段 classification_rationale，"
                  "用一句话说明这处文本为何属于该抽象操作（归类理由置于末尾）。"
                  + system)
    reply = client.invoke(role="extractor",
                         system=system,
                         payload=({"strategy_id": strategy_id, "text": text}
                                  if not strategy_def else
                                  {"strategy_id": strategy_id, "text": text,
                                   "strategy": strategy_def,
                                   "output_contract": OUTPUT_CONTRACT}),
                         max_tokens=2000, timeout=60)
    budget.spend(int(reply.get("tokens_in", 0)),
                 int(reply.get("tokens_out", 0)))
    import json
    try:
        raw = json.loads(reply["text"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"status": "unverified", "reason": "invalid_extraction_json"}
    raw = repair_span(raw, text)     # 模型 offset 不信——以引用原文机械重定位
    clean, st = gate_output(raw, text, segment_id, strategy_id,
                            extractor_model=reply.get("actual_model", "unknown"))
    if st != "proposed":
        return {"status": st, "reason": "output_gate", "raw": raw}
    ev = gate_evidence(clean, text)
    clean.update({"strategy_version": strategy_version, "work_id": work_id,
                  "text_version": text_version, "status": ev})
    if strategy_def:
        # 新口径复审标记：复用 reviewer_version 列（不新增列、不改 status）。
        # 默认路径（strategy_def=None）此处不写入，返回 dict 与现状逐字一致。
        clean["reviewer_version"] = REVIEW_MARKER_NEW_DEF
    return clean


def review_ledger_stats(session) -> dict:
    """K2 复审台账：strategy_instances 复审进度对账（只读，不改 status、不写库）。

    标记位复用现有 reviewer_version 列（审计 P1 第 3 条，不新增/不删列）：
      - n_reviewed_new_def = reviewer_version == REVIEW_MARKER_NEW_DEF
        （已按带 strategy_def 的新口径复审）
      - n_legacy = reviewer_version 为空/None（未复审）
    即审计所说「82 条需复审」的进度对账口径：n_legacy 即待复审数。"""
    q = session.query(StrategyInstance)
    n_total = q.count()
    n_reviewed_new_def = q.filter(
        StrategyInstance.reviewer_version == REVIEW_MARKER_NEW_DEF).count()
    n_legacy = q.filter(or_(
        StrategyInstance.reviewer_version.is_(None),
        StrategyInstance.reviewer_version == "")).count()
    return {"n_total": n_total,
            "n_reviewed_new_def": n_reviewed_new_def,
            "n_legacy": n_legacy}


def persist_instance(session, result: dict) -> "StrategyInstance":
    """K2 实例唯一写库入口（离线骨架不自动调用——extract_segment 仍是纯函数）。

    把抽取结果落成 strategy_instances 一行；reviewer_version 取 result 里的标记
    （新口径=REVIEW_MARKER_NEW_DEF，缺省=空=未复审）。status 原样落——
    **绝不批量改 status**（审计明令禁止用批量改 status 冒充验收）。

    纪律：不连网关、不读密钥、只写一行；与 review_ledger_stats 共用同一标记位
    （reviewer_version），写后读即可对账。"""
    inst = StrategyInstance(
        strategy_id=result["strategy_id"],
        strategy_version=result.get("strategy_version", 1),
        work_id=result.get("work_id", ""),
        segment_id=result.get("segment_id", ""),
        text_version=result.get("text_version", ""),
        span_start=int(result["span_start"]),
        span_end=int(result["span_end"]),
        evidence_text=result["evidence_text"],
        evidence_sha256=result.get("evidence_sha256", ""),
        observed_content=str(result.get("observed_content", "")),
        extractor_model=result.get("extractor_model", "unknown"),
        status=result.get("status", "proposed"),
        reviewer_version=result.get("reviewer_version", ""),
    )
    session.add(inst)
    return inst
