"""K3-B 桥：每场首次 prepare 之前查冻知识包；同任务恢复不重查。

契约（监督 2026-09-22）：
- 查询与冻结移到每场首次 prepare **之前**——job 不存在才查询（v2
  /knowledge/query）并 freeze_package；job 已存在（含崩溃恢复）→ 直接
  复用该 job request 里的冻结包，**不重查**（同任务恢复语义）；
- Writer 只消费本场冻结包：桥返回的 KnowledgePackage 进 prepare 后被
  冻进 request/context（既有机制），writer 读的是 job 的冻结副本；
- 同幂等键异输入必冲突：桥按 idem 键复用旧包 + 新 plan → prepare
  request 哈希不符 → idempotency_input_conflict（不静默覆盖）；
- 跨 book 拒绝：policy.book_id 由 plan 构造 + validate_plan 的
  knowledge_scope_conflict 双闸；
- 服务故障不许静默回退：unavailable/unsupported → RuntimeFault，
  empty → 零技巧包照常跑（诚实空，不硬凑）；
- 旧手选模式（knowledge.genome_package）与既有收据不动：本模块只
  读 job request、只在 LG 库新增 packages 行，不写 runtime store。
"""
from __future__ import annotations

import json

from .contracts import KnowledgePackage, RuntimeFault, Technique, digest
from .store import Store


def _techniques_from_selected(selected: list[dict]) -> list[Technique]:
    """选中策略 → Runtime 技巧（只取进上下文的 for_context 条目，≤3）。

    无原文：source_refs 只带策略 id/版本/证据 instance id（§4.2）；
    evidence_status 用 v2 效果层（枚举恰好与旧 Technique 契约一致）。"""
    out: list[Technique] = []
    for e in selected:
        if not e.get("for_context"):
            continue
        uncertain = [f"{u['dimension']}={u['state']}"
                     for u in e.get("uncertain_items", [])]
        out.append(Technique(
            id=f"{e['strategy_id']}.v{e['version']}",   # ASCII 契约模式
            operation=e["abstract_operation"],
            conditions=uncertain or ["（无显式条件——按操作适用性采用）"],
            exceptions=list(e.get("failure_modes") or []) or
            ["条件不匹配时不采用；不得改变事实或人物意图。"],
            source_refs=[f"language-genome:strategy_v2/{e['strategy_id']}"
                         f"@{e['version']}"] +
                        [f"instance/{r['instance_id']}"
                         for r in e.get("evidence", [])][:3],
            evidence_status=e.get("effect_status") or "untested"))
        if len(out) >= 3:
            break
    return out


def frozen_package_for_scene(store: Store, lg_session, plan, *,
                             context_items: int = 3) -> tuple[KnowledgePackage, dict]:
    """首 prepare 前查冻；恢复复用不重查。返回 (包, 对账元数据)。"""
    from .. import knowledge_query as kq   # LG 侧服务层（只读+包写入）
    from ..knowledge import PACKAGE_CONTRACT_VERSION

    job_id = "scene-" + digest([plan.book_id, plan.branch_id,
                                plan.idempotency_key])[:24]
    try:
        job = store.job(job_id)          # store.job 缺席即抛 job_not_found
    except RuntimeFault:
        job = None
    if job is not None:
        # 同任务恢复：冻结包已在 job request 里——不重查（重查会拿到
        # 库漂移后的不同包，破坏「本场冻结」语义）
        knowledge = KnowledgePackage.model_validate(
            json.loads(job["request"])["knowledge"])
        return knowledge, {"reused": True, "job_id": job_id}

    policy = {"contract_version": PACKAGE_CONTRACT_VERSION,
              "book_id": plan.book_id, "branch_id": plan.branch_id,
              "scene_id": plan.scene_id,
              "plan_sha256": digest(plan.model_dump()),
              "semantic_requirements": {"goal": plan.goal, "pov": plan.pov,
                                       "style": plan.style},
              "limits": {"context_items": context_items}}
    resp = kq.query_knowledge(policy, lg_session)
    if resp["status"] == "unavailable":
        raise RuntimeFault("knowledge_query_unavailable")
    if resp["status"] == "unsupported":
        raise RuntimeFault("knowledge_query_unsupported")
    if resp["status"] == "matched":
        kq.freeze_package(resp, lg_session)   # 冻结在首 prepare 前 ✓
    techniques = _techniques_from_selected(resp.get("selected", []))
    pkg = KnowledgePackage(
        schema_version="scene-knowledge/2",
        package_id="kq-" + (resp.get("package_sha256") or
                            digest(techniques)[:20])[:20],
        book_id=plan.book_id, source_kind="knowledge_query_v2",
        techniques=techniques)
    return pkg, {"reused": False, "query_status": resp["status"],
                 "package_sha256": resp.get("package_sha256"),
                 "n_selected": len(resp.get("selected", []))}
