"""K3-A /knowledge/query 共用服务层（知识化方案 §6.1–6.2/§7.1；2026-09-22）。

单一服务层：HTTP 薄层（app/api.py）与本机适配器共用；本模块**只读**
（不 commit/add——写入归 K3-B Runtime 冻结流程）。关键词+结构化检索，
语义向量本轮不加。

纪律（监督 2026-09-21 接续指令逐条）：
- 哈希口径单一：复用 app/knowledge.py（evidence_sha256/版本协商/词表），
  禁止本地重定义；
- 过滤顺序固定：来源与版本过滤 → 必需条件/bad_when 排除 → 范围匹配
  → 去重 → 按条件匹配与证据排序——顺序不许调；
- 排序只输出**可解释分量**（required/good_when 匹配数、证据区间数、
  scope 特异性），禁止编造「成功率 0.93」；
- 响应区分 empty / unsupported / unavailable；必需条件 unknown 的策略
  不得强行采用；neutral_when 不计正支持；不为凑 5–10 条放宽门槛；
- 镜像重复按 canonical 根作品聚合区间、fixture 冒充 Human、基准上下文
  泄漏，全部在服务层硬拦（复用 K1-A work_sources 契约）；
- 包内容不含任何原文（§4.2：Writer 只得到抽象操作/条件/例外/无原文引用）；
- 语义需求=显式输入+有版本的**确定性映射**：只认 operator=eq 且维度在
  requirements 里的谓词，其余一律 unknown——模型猜测不当剧情事实。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.exc import OperationalError

from . import knowledge as K
from .models import (ExpressionStrategyV2, Segment, StrategyCondition,
                      StrategyInstance, WorkSource)

# 查询默认只出**合格**知识（方案 §4.4：hypothesis 不自动作为 v2 已验证
# 查询结果；K3 行「K1/K2 合格知识可用」）
ELIGIBLE_STATUS = frozenset({"verified"})
ELIGIBLE_OBSERVATION = frozenset({"observed", "replicated"})
# 默认排除的来源类型（fixture/synthetic/commentary 不给人类证据加分——
# K1-A 契约：fixture 只验契约）与合格文本版本
DEFAULT_EXCLUDED_SOURCE_TYPES = frozenset(
    {"fixture", "synthetic", "commentary"})
DEFAULT_ALLOWED_TEXT_VERSIONS = frozenset(
    {"corpus-v1", "corpus-v2-mirror"})
# 语义需求→谓词的确定性映射版本（映射规则升级须 bump 并在 capabilities 报出）
REQUIREMENTS_MAPPING_VERSION = 1
# scope 特异性分量（越具体越高）
SCOPE_SPECIFICITY = {"WORK": 4, "AUTHOR": 3, "GENRE": 2, "GLOBAL": 1,
                     "UNCERTAIN": 0}
CANDIDATE_CAP_MAX = 10
CONTEXT_ITEMS_MAX = 3


class PolicyError(ValueError):
    """policy 非法（上限越界等）——HTTP 层转 400，不静默降级。"""


def canonical_json(obj) -> str:
    """规范化序列化（排序键）——policy/包哈希的单一口径。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def policy_sha256(policy: dict) -> str:
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def fingerprint_knowledge(s) -> str:
    """库知识快照指纹：全部 v2 策略 (id, version) 与 K1-A 登记行的规范化
    哈希——同库同知识必同指纹；库变了（新策略/登记变更）包即失效。"""
    h = hashlib.sha256()
    for sid, ver, key in sorted(
            (r.id, r.version, r.strategy_key) for r in
            s.query(ExpressionStrategyV2.id, ExpressionStrategyV2.version,
                    ExpressionStrategyV2.strategy_key).all()):
        h.update(f"{sid}|{ver}|{key}\n".encode("utf-8"))
    for wid, can, st, tv in sorted(
            (r.work_id, r.canonical_work_id, r.source_type, r.text_version)
            for r in s.query(
                WorkSource.work_id, WorkSource.canonical_work_id,
                WorkSource.source_type, WorkSource.text_version).all()):
        h.update(f"ws|{wid}|{can}|{st}|{tv}\n".encode("utf-8"))
    return h.hexdigest()


def evaluate_predicate(condition: StrategyCondition, requirements: dict,
                        mapping_version: int = REQUIREMENTS_MAPPING_VERSION
                        ) -> str:
    """确定性谓词求值：只认 eq + 维度在显式输入里；其余一律 unknown。

    有版本：映射规则升级须 bump REQUIREMENTS_MAPPING_VERSION 并在
    capabilities 报出——unknown（缺证据）不许折叠成 false（§4.3 三值）。"""
    if mapping_version != REQUIREMENTS_MAPPING_VERSION:
        return "unknown"
    if (condition.operator or "").strip() != "eq":
        return "unknown"
    if condition.dimension not in requirements:
        return "unknown"
    want = (condition.value or {}).get("v")
    return "true" if requirements[condition.dimension] == want else "false"


def _evidence_for(s, strategy_id: str, policy: dict) -> tuple[list[dict], int, list[str]]:
    """①来源与版本过滤（固定顺序第一步）：取该策略的合格证据区间。

    硬拦（K1-A 契约复用）：基准段实例剔除（基准上下文泄漏）、
    excluded_source_types（fixture/synthetic/commentary 冒充）、
    license 禁用用途、不合格文本版本；镜像按 canonical 根作品聚合去重
    ——evidence_count=唯一 (根作品, span) 区间数，重跑不加置信度。"""
    seg_role = {sid: role for sid, role in s.query(
        Segment.id, Segment.role).all()}
    reg = {r.work_id: r for r in s.query(WorkSource).all()}
    sp = policy.get("source_policy") or {}
    excluded_types = frozenset(sp.get(
        "excluded_source_types") or DEFAULT_EXCLUDED_SOURCE_TYPES)
    excluded_uses = frozenset(sp.get("excluded_uses") or [])
    allowed_tv = frozenset(sp.get(
        "allowed_text_versions") or DEFAULT_ALLOWED_TEXT_VERSIONS)
    stripped: list[str] = []
    intervals: set[tuple[str, int, int]] = set()
    refs: list[dict] = []
    for ins in (s.query(StrategyInstance)
                .filter_by(strategy_id=strategy_id, status="verified")
                .all()):
        r = reg.get(ins.work_id)
        if r is None:
            stripped.append(f"{ins.id}:no_registry"); continue
        if seg_role.get(ins.segment_id) == "benchmark":
            stripped.append(f"{ins.id}:benchmark_source"); continue
        if r.source_type in excluded_types:
            stripped.append(f"{ins.id}:excluded_source_type:{r.source_type}"); continue
        if set(r.license_purposes or []) & excluded_uses:
            stripped.append(f"{ins.id}:excluded_use"); continue
        if ins.text_version not in allowed_tv:
            stripped.append(f"{ins.id}:text_version:{ins.text_version}"); continue
        key = (r.canonical_work_id, ins.span_start, ins.span_end)
        if key in intervals:
            stripped.append(f"{ins.id}:mirror_dedup"); continue   # 镜像重复不计
        intervals.add(key)
        refs.append({"instance_id": ins.id, "canonical_work": r.canonical_work_id,
                     "span": [ins.span_start, ins.span_end],
                     "text_version": ins.text_version})
    return refs, len(intervals), stripped


def _scope_matches(s, strategy: ExpressionStrategyV2, policy: dict) -> str:
    """③范围匹配：返回 pass 或拒绝理由（固定顺序第三步）。

    WORK→book 命中 scope_ids；AUTHOR/GENRE 经 K1-A 登记行反查本书的
    作者/题材；GLOBAL 本阶段只预留不自动授予；UNCERTAIN 不匹配。
    跨范围不升级：单部作品只支持该作品的观察（§4.3）。"""
    book = policy.get("book_id") or ""
    if strategy.scope == "WORK":
        return "pass" if book and book in (strategy.scope_ids or []) \
            else "excluded_scope"
    if strategy.scope in ("AUTHOR", "GENRE"):
        reg = s.query(WorkSource).filter_by(work_id=book).first()
        if reg is None:
            return "excluded_scope"          # 无登记的来源不作证据
        if strategy.scope == "AUTHOR":
            return "pass" if reg.author_id and reg.author_id in \
                (strategy.scope_ids or []) else "excluded_scope"
        return "pass" if set(strategy.scope_ids or []) & \
            set(reg.genre_ids or []) else "excluded_scope"
    if strategy.scope == "GLOBAL":
        return "excluded_scope_global_reserved"
    return "excluded_scope_uncertain"


def _condition_pipeline(s, strategy_id: int, requirements: dict
                        ) -> tuple[str, dict, list[dict]]:
    """②必需条件/bad_when 排除（固定顺序第二步）。

    返回 (拒绝理由|None, 分量, 不确定项)。必需 unknown 不得强行采用
    （excluded_required_unknown）；neutral_when 不计正支持；good_when
    true 才计分量。分量只有整数计数——没有编造的成功率。"""
    comps = {"required_matches": 0, "good_when_matches": 0}
    uncertain: list[dict] = []
    for c in (s.query(StrategyCondition)
              .filter_by(strategy_id=strategy_id).all()):
        state = evaluate_predicate(c, requirements)
        if c.kind == "bad_when":
            if state == "true":
                return "excluded_bad_when", comps, uncertain   # 硬排除
            continue
        if c.kind != "good_when":
            continue                       # neutral_when：不计正支持
        if c.required and state != "true":
            # 必需条件 false→排除；unknown→不得强行采用（监督口径）
            return (f"excluded_required_{state}", comps, uncertain)
        if state == "true":
            comps["good_when_matches" if not c.required
                    else "required_matches"] += 1
        else:
            uncertain.append({"dimension": c.dimension, "state": state})
    return None, comps, uncertain


def query_knowledge(policy: dict, s) -> dict:
    """K3-A 主查询：固定过滤顺序，返回 matched/empty/unsupported/unavailable。

    只读：不 commit/add——写入归 K3-B 冻结流程（freeze_package）。"""
    limits = policy.get("limits") or {}
    cap = int(limits.get("candidate_cap", CANDIDATE_CAP_MAX))
    ctx_n = int(limits.get("context_items", 3))
    max_chars = int(limits.get("max_context_chars", 1200))
    if cap > CANDIDATE_CAP_MAX:
        raise PolicyError(f"candidate_cap 越界：{cap}>{CANDIDATE_CAP_MAX}"
                          "——候选上限 10，禁止为凑数放宽")
    if ctx_n > CONTEXT_ITEMS_MAX:
        raise PolicyError(f"context_items 越界：{ctx_n}>{CONTEXT_ITEMS_MAX}"
                          "——进入正文上下文默认 0–3 条")
    ver, note = K.negotiate_package_version(policy.get("contract_version"))
    base = {"policy_sha256": policy_sha256(policy),
            "contract_negotiation": {"served": ver, "note": note},
            "mapping_version": REQUIREMENTS_MAPPING_VERSION}
    if ver == 0:
        return {**base, "status": "unsupported", "reason": note,
                "selected": [], "rejected": []}
    try:
        snap = fingerprint_knowledge(s)
        requirements = dict(policy.get("semantic_requirements") or {})
        strategies = [r for r in s.query(ExpressionStrategyV2).all()
                      if r.status in ELIGIBLE_STATUS
                      and r.observation_status in ELIGIBLE_OBSERVATION]
        rejected, kept = [], []
        for st in strategies:              # ①→⑤ 固定顺序
            refs, ev_count, _stripped = _evidence_for(s, st.id, policy)
            if ev_count == 0:
                rejected.append({"strategy_key": st.strategy_key,
                                 "reason": "excluded_no_evidence"}); continue
            reason, comps, uncertain = _condition_pipeline(
                s, st.id, requirements)
            if reason:
                rejected.append({"strategy_key": st.strategy_key,
                                 "reason": reason}); continue
            if _scope_matches(s, st, policy) != "pass":
                rejected.append({"strategy_key": st.strategy_key,
                                 "reason": _scope_matches(s, st, policy)})
                continue
            comps["evidence_count"] = ev_count
            comps["scope_specificity"] = SCOPE_SPECIFICITY.get(st.scope, 0)
            kept.append({"strategy": st, "refs": refs, "comps": comps,
                          "uncertain": uncertain})
        # ④ 去重：同 key 保最高 version——被挤掉的低版本必须显式落
        # rejected（不然它静默消失，对账少一条）
        by_key: dict[str, dict] = {}
        for item in kept:
            k = item["strategy"].strategy_key
            cur = by_key.get(k)
            if cur is None or item["strategy"].version > cur["strategy"].version:
                if cur is not None:
                    rejected.append({"strategy_key": k,
                                     "reason": "excluded_duplicate_lower_version"})
                by_key[k] = item
            else:
                rejected.append({"strategy_key": k,
                                 "reason": "excluded_duplicate_lower_version"})
        ranked = sorted(by_key.values(),
                        key=lambda x: (-x["comps"]["required_matches"],
                                       -x["comps"]["good_when_matches"],
                                       -x["comps"]["evidence_count"],
                                       -x["comps"]["scope_specificity"],
                                       x["strategy"].strategy_key))
        selected = []
        chars = 0
        for i, item in enumerate(ranked[:cap]):
            st = item["strategy"]
            entry = {
                "strategy_id": st.id, "strategy_key": st.strategy_key,
                "version": st.version, "status": st.status,
                "scope": st.scope, "observation_status": st.observation_status,
                # 方案 §4.3：兼容旧包（v1 协商）保守映射为 hypothesis——
                # 呈现层降级；完整证据层次保留在 observation_status（审计侧）
                "served_observation_status": ("hypothesis" if ver == 1
                                              else st.observation_status),
                "effect_status": st.effect_status,
                "abstract_operation": st.abstract_operation,
                "invariants": st.invariants, "failure_modes": st.failure_modes,
                "effect_hypothesis": st.effect_hypothesis,
                "score_components": item["comps"],
                "uncertain_items": item["uncertain"],
                "evidence": item["refs"],       # 只含 id/根作品/span——无原文
                "for_context": False}
            if i < ctx_n and chars < max_chars:
                entry["for_context"] = True
                chars += len(st.abstract_operation or "")
            selected.append(entry)
        pkg = {"policy": policy, "selected_ids":
               [e["strategy_id"] for e in selected], "snapshot": snap}
        package_sha = hashlib.sha256(
            canonical_json(pkg).encode("utf-8")).hexdigest()
        return {**base, "status": "matched" if selected else "empty",
                "policy_echo": policy,          # 冻结包要绑原 policy（§6.2）
                "snapshot_fingerprint": snap,
                "package_sha256": package_sha,
                "selected": selected, "rejected": rejected,
                "budget": {"considered": len(strategies),
                           "passed": len(selected) + len(rejected),
                           "selected": len(selected),
                           "context": sum(1 for e in selected if e["for_context"]),
                           "chars": chars}}
    except OperationalError as e:
        return {**base, "status": "unavailable",
                "reason": f"knowledge store unavailable: {e}"[:200],
                "selected": [], "rejected": []}


def capabilities(s) -> dict:
    """GET /knowledge/capabilities：静态词表 + 真库只读统计。"""
    from .models import KnowledgeLink
    counts = {}
    for st in s.query(ExpressionStrategyV2.status).all():
        counts[st[0]] = counts.get(st[0], 0) + 1
    return {"contract_version": K.PACKAGE_CONTRACT_VERSION,
            "mapping_version": REQUIREMENTS_MAPPING_VERSION,
            "scopes": sorted(K.SCOPES), "observation_status": sorted(
                K.OBSERVATION_STATUS), "effect_status": sorted(
                K.EFFECT_STATUS), "link_kinds": sorted(K.LINK_KINDS),
            "predicate_states": sorted(K.PREDICATE_STATES),
            "candidate_cap_max": CANDIDATE_CAP_MAX,
            "context_items_max": CONTEXT_ITEMS_MAX,
            "strategy_counts_by_status": counts,
            "n_links": s.query(KnowledgeLink).count()}


def get_package(package_sha_or_id: str, s) -> dict | None:
    """GET /knowledge/packages/{id}：只读查包；找不到返回 None（HTTP 404）。"""
    from .models import KnowledgePackage
    q = s.query(KnowledgePackage)
    row = q.filter_by(id=package_sha_or_id).first() or \
        q.filter_by(package_sha256=package_sha_or_id).first()
    if row is None:
        return None
    return {"id": row.id, "package_sha256": row.package_sha256,
            "policy_sha256": row.policy_sha256, "policy": row.policy,
            "selected": row.selected,
            "rejected_summary": row.rejected_summary,
            "snapshot_fingerprint": row.snapshot_fingerprint,
            "contract_version": row.contract_version,
            "created_at": row.created_at}


def freeze_package(response: dict, s):
    """K3-B 冻结流程用（K3-A 不在 HTTP 暴露写端点）：幂等写包。"""
    from .models import KnowledgePackage
    sha = response.get("package_sha256")
    if not sha:
        raise ValueError("response 无 package_sha256，不可冻结")
    row = s.query(KnowledgePackage).filter_by(package_sha256=sha).first()
    if row:
        return row.id
    row = KnowledgePackage(
        id="KPKG-" + sha[:24], package_sha256=sha,
        policy_sha256=response["policy_sha256"],
        policy=response.get("policy_echo") or {},
        selected=response.get("selected", []),
        rejected_summary=response.get("rejected", []),
        snapshot_fingerprint=response.get("snapshot_fingerprint", ""),
        contract_version=2)
    s.add(row)
    s.commit()
    return row.id
