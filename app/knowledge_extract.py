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

from . import knowledge as K
from .models import StrategyInstance


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


def extract_segment(client, *, strategy_id: str, strategy_version: int,
                   work_id: str, segment_id: str, text: str,
                   text_version: str, budget: ExtractBudget,
                   live: bool = False) -> dict:
    """单段抽取：预算闸→调用→输出门→证据门。返回带 status 的结果 dict。

    client 契约（与既有网关同形）：invoke(*, role, system, payload,
    max_tokens, timeout) → {"text": json_str, "tokens_in", "tokens_out"}。
    live=True 时调用方必须已注入真网关 client；默认 False 仅供测试与
    FixtureClient——拍板前任何脚本都不许传 True（回归钉死）。"""
    if not client:
        raise RuntimeError("client 未注入（离线骨架需要显式注入测试 client）")
    budget.check()             # 调用前闸（超限在花 token 前拒）
    reply = client.invoke(role="extractor",
                         system="从文本中抽取策略实例（span+证据+观察）",
                         payload={"strategy_id": strategy_id, "text": text},
                         max_tokens=2000, timeout=60)
    budget.spend(int(reply.get("tokens_in", 0)),
                 int(reply.get("tokens_out", 0)))
    import json
    try:
        raw = json.loads(reply["text"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"status": "unverified", "reason": "invalid_extraction_json"}
    clean, st = gate_output(raw, text, segment_id, strategy_id,
                            extractor_model=reply.get("actual_model", "unknown"))
    if st != "proposed":
        return {"status": st, "reason": "output_gate", "raw": raw}
    ev = gate_evidence(clean, text)
    clean.update({"strategy_version": strategy_version, "work_id": work_id,
                  "text_version": text_version, "status": ev})
    return clean
