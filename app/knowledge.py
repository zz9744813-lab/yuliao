"""K1-B 知识契约（知识化调整方案 §4.2/§4.3）：v2 词表、机械核对、
保守映射与版本协商——单一口径，模型与脚本与测试共用。

纪律（方案原文落死）：
- v1 只是 legacy 假设来源：兼容旧包时保守映射为 hypothesis，观测/效果
  层**绝不**从 v1 数据自动授予；跨语料出现不得误译成质量通过；
- GLOBAL 本阶段只预留契约，不自动授予（scope 需带具体范围 ID 与依据）；
- 谓词三值 true/false/**unknown**——缺证据不是假；
- 实例证据必须机械核对 text[span_start:span_end] == 存证文本。
"""
from __future__ import annotations

import hashlib

# ── 词表（§4.2/§4.3）───────────────────────────────────────────
SCOPES = frozenset({"WORK", "AUTHOR", "GENRE", "GLOBAL", "UNCERTAIN"})
OBSERVATION_STATUS = frozenset({"hypothesis", "observed", "replicated"})
# v1 Runtime Technique.evidence_status 的三值恰好是 v2 效果层枚举——
# 保守映射即恒等（绝不映射到 observation）
EFFECT_STATUS = frozenset({"untested", "pilot_verified", "quality_supported"})
STRATEGY_STATUS = frozenset({"hypothesis", "verified", "retired", "superseded"})
INSTANCE_STATUS = frozenset({"proposed", "verified", "rejected"})
CONDITION_KINDS = frozenset({"good_when", "bad_when", "neutral_when"})
PREDICATE_STATES = frozenset({"true", "false", "unknown"})
LINK_KINDS = frozenset({"supports", "contradicts", "refines", "alternative_of"})
# 知识边端点类型（to 端允许 Distiller 机制 id——引用，不复制原文）
LINK_ENDPOINT_KINDS = frozenset({
    "strategy_v2", "strategy_instance", "strategy_condition",
    "distiller_mechanism"})

# 知识包接口契约版本（K3 查询服务与本模块协商用）
PACKAGE_CONTRACT_VERSION = 2


def verify_instance_span(text: str, span_start: int, span_end: int,
                         evidence_text: str) -> bool:
    """§4.2 机械核对：text[span_start:span_end] 必须与存证**完全相符**。

    schema 合格不代表推断成立——本函数只证「存证是那个区间的原文」，
    语义归纳须单独审查。"""
    if span_start < 0 or span_end <= span_start:
        return False
    return (text or "")[span_start:span_end] == (evidence_text or "")


def evidence_sha256(evidence_text: str) -> str:
    return hashlib.sha256((evidence_text or "").encode("utf-8")).hexdigest()


def conservative_effect_from_v1(v1_status: str | None) -> str:
    """v1 Runtime Technique.evidence_status → v2 效果层的**保守恒等映射**。

    v1 三值（hypothesis/pilot_verified/quality_supported）只覆盖效果层；
    缺失/未知 → untested。观察层（observation_status）**永不**从 v1 推出
    ——一律 hypothesis（方案 §4.3：不能直接往旧包塞 observed 等新枚举）。"""
    return v1_status if v1_status in EFFECT_STATUS else "untested"


def negotiate_package_version(requested: int | None,
                              supported: int = PACKAGE_CONTRACT_VERSION) -> tuple[int, str]:
    """知识包/查询的版本协商（K3 服务与旧包兼容的契约）。

    返回 (服务版本, 说明)。规则：
    - requested=None（旧包，不带版本）→ 保守服务 v1 语义：只出
      observation_status=hypothesis 的内容（§4.3 兼容旧包规则）；
    - requested <= supported → 按请求版本服务；
    - requested > supported → 拒绝（(0, unsupported)），不静默降级。"""
    if requested is None:
        return 1, "legacy 包：保守映射为 hypothesis（不授予 observed/效果层）"
    if requested <= supported:
        return requested, "ok"
    return 0, f"unsupported：包契约版本 {requested} > 服务端 {supported}"


def grant_scope_valid(level: str, scope_ids: list, basis: str) -> bool:
    """scope 晋级的形式闸（§4.3）：非 UNCERTAIN 必须带具体范围 ID 与依据。

    GLOBAL 本阶段只预留——出现在数据里必须有集霸级依据，本函数只做
    形式校验，语义晋级由人审。"""
    if level not in SCOPES:
        return False
    if level == "UNCERTAIN":
        return True
    return bool(scope_ids) and bool((basis or "").strip())
