"""K3 可达预演的两段口径钉死（2026-09-24 主控派工：旧版只复刻来源闸，把
真库「可达 33703」与 K3 真身「selected=0」对不上的乐观假象，修成
`upper_bound`（来源闸上界，明确未含 scope/status/预算闸）+ `would_pass`
（真判据预演，走 K3 真函数）——见 docs/K3_可达预演口径_20260924.md）。

钉住的事：
1. **当前真库的口径真相**：8 条策略全 hypothesis/UNCERTAIN 时，真判据预演的
   `would_pass_all == 0`，且 `excluded_scope_uncertain` 计数 > 0（防止以后
   又把预演改回乐观口径）；
2. **反向钉**：scope 明确（WORK+scope_ids 命中）、status 已升格（verified）
   的 fixture，真判据预演 `would_pass_all > 0`——预演不是恒 0 的死门；拔掉
   任一闸（status 降回 hypothesis / scope 改 UNCERTAIN / text_version 出允许集）
   立即变 0、对应剔除桶出现；
3. **与 query_knowledge 一致性**：同书同策略——没证据时真查询只差在
   `excluded_no_evidence`（预演说「全过」恰是「只剩证据没落」），落 verified
   实例后真查询就直接命中；预演方向与真查询同向；
4. **改前字段逐字兼容**：`pairs`/`would_reach_k3`/`stripped` 语义不变，
   `upper_bound.label/note` 写清「上界，未含 scope/status/预算闸」，且
   `would_pass.would_pass_source` 恒等于 `would_reach_k3`（同源断言：新段与
   改前来源闸是同一判定）。

全部离线（conftest 临时库），不跑 --live、不联网、不碰生产库；K3 侧
app/knowledge_query.py 一行不改，只 import 复用。
"""
from __future__ import annotations

import hashlib as _h
import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2b", ROOT / "scripts" / "k2_extract_backfill.py")
k2b = _u.module_from_spec(_spec); _spec.loader.exec_module(k2b)

from app import db                                # noqa: E402
from app import knowledge_query as KQ             # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        StrategyInstance, Work, WorkSource)

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"
_seq = [0]


def _mk(tag, *, segs, source_type="human_fiction", text_version="corpus-v1",
        status="hypothesis", observation_status="observed", scope="WORK",
        scope_ids=None) -> dict:
    """一部作品 + 若干段 segs=[(role, src_ok, clean_ok)] + 登记行 + 一条策略。

    scope_ids=None → WORK 自动绑自家作品（成对时能过 scope 闸）；UNCERTAIN
    传 scope_ids=[]。返回 {work_id, seg_ids, key}。"""
    _seq[0] += 1
    key = f"k3r-{_seq[0]}-{tag}"
    db.init_db()
    with db.session() as s:
        w = Work(title=f"t-{key}", source="test:k3preview")
        s.add(w)
        s.flush()
        for i, (role, src_ok, clean_ok) in enumerate(segs):
            s.add(Segment(work_id=w.id, ordinal=i, text=TEXT,
                          text_clean=TEXT if clean_ok else "", role=role,
                          n_chars=len(TEXT), n_sentences=1,
                          integrity=json.dumps({"src_ok": bool(src_ok)})))
        s.flush()
        import register_work_sources as REG       # 单一哈希口径（锚复核纪律）
        sha, _ = REG._work_sha256(s, w.id)
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type=source_type, text_version=text_version,
                         text_sha256=sha,
                         purpose_basis="测试夹具：只验预演口径，非生产语料",
                         identity_purposes=["research"], license_purposes=[],
                         license_basis="seed", metadata_status="verified",
                         metadata_basis="seed"))
        s.add(ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            effect_hypothesis=f"假设-{key}", status=status,
            observation_status=observation_status, scope=scope,
            scope_ids=(scope_ids if scope_ids is not None
                       else ([w.id] if scope == "WORK" else []))))
        s.commit()
        seg_ids = [x.id for x in s.query(Segment).filter_by(work_id=w.id)
                   .order_by(Segment.ordinal).all()]
    return {"work_id": w.id, "seg_ids": seg_ids, "key": key}


def _queues_for(s, work_id: str, key: str) -> dict:
    """构造 build_queues 同形队列（只看该作品自家段）。显式构造、不经
    build_queues，因为 status=verified 不在抽取池（hypothesis/active）——
    反向钉正要用「已升格」的策略验证预演不是死门。形状逐字与 build_queues
    一致：item = {strategy, segment, text, text_version}。"""
    st = (s.query(ExpressionStrategyV2)
          .filter_by(strategy_key=key).one())
    ws = (s.query(WorkSource)
          .filter(WorkSource.work_id == work_id).one())
    segs = (s.query(Segment).filter_by(work_id=work_id)
            .order_by(Segment.ordinal).all())
    return {st.id: [{"strategy": st, "segment": seg, "text": seg.text_clean,
                     "text_version": ws.text_version} for seg in segs]}


def _strategy(s, key: str) -> ExpressionStrategyV2:
    return s.query(ExpressionStrategyV2).filter_by(strategy_key=key).one()


# ── 1. 当前真库口径真相：8 条全 hypothesis/UNCERTAIN → would_pass_all==0 ──
def test_true_preview_pins_current_truth_eight_hypothesis_uncertain():
    works = [_mk(f"pin-{i}", segs=[("train", True, True)],
                 scope="UNCERTAIN") for i in range(8)]
    with db.session() as s:
        queues, _stats = k2b.build_queues(
            s, strategy_keys=tuple(w["key"] for w in works),
            source_scope="nonbenchmark")
        prev = k2b.k3_evidence_preview(s, queues)
    wp = prev["would_pass"]
    # K3 真身同账：8 条假设策略一条都进不了合格集（fact 2 的根因）
    with db.session() as s:
        strs = [_strategy(s, w["key"]) for w in works]
    assert len(strs) == 8
    assert all(st.status == "hypothesis" and st.scope == "UNCERTAIN"
               and not st.scope_ids for st in strs)
    assert all(st.status not in KQ.eligible_statuses(st.version) for st in strs)
    # 真判据预演对着这一真库真相照实报 0，且逐闸分桶有 excluded_scope_uncertain
    assert wp["would_pass_all"] == 0, wp
    assert wp["reasons"].get("excluded_scope_uncertain", 0) > 0, wp["reasons"]
    assert wp["reasons"]["excluded_scope_uncertain"] == wp["pairs"]
    assert wp["reasons"].get("status_not_eligible", 0) == wp["pairs"]
    # 旧口径字段逐字兼容 + 明确标注上界
    assert wp["pairs"] == prev["upper_bound"]["pairs"] == prev["pairs"]
    assert prev["upper_bound"]["upper_bound"] is True
    assert "上界" in prev["upper_bound"]["note"], prev["upper_bound"]["note"]
    assert "status" in prev["upper_bound"]["note"]
    assert "预算闸" in prev["upper_bound"]["note"]


# ── 2. 反向钉：scope 明确 + status 升格 → would_pass_all > 0 ───────────
def test_reverse_pin_scope_clear_status_upgraded_passes_and_gates_gate():
    good = _mk("rp-good", segs=[("train", True, True), (None, True, True)],
               status="verified", observation_status="observed")
    no_status = _mk("rp-nostatus", segs=[("train", True, True)],
                    status="hypothesis")
    no_scope = _mk("rp-noscope", segs=[("train", True, True)],
                   status="verified", observation_status="observed",
                   scope="UNCERTAIN", scope_ids=[])
    odd_tv = _mk("rp-odd", segs=[("train", True, True)],
                 status="verified", observation_status="observed",
                 text_version="k2v2-pilot")
    with db.session() as s:
        prev_good = k2b.k3_evidence_preview(
            s, _queues_for(s, good["work_id"], good["key"]))
        prev_ns = k2b.k3_evidence_preview(
            s, _queues_for(s, no_status["work_id"], no_status["key"]))
        prev_nsc = k2b.k3_evidence_preview(
            s, _queues_for(s, no_scope["work_id"], no_scope["key"]))
        prev_odd = k2b.k3_evidence_preview(
            s, _queues_for(s, odd_tv["work_id"], odd_tv["key"]))
    # 全过：四闸 AND 恒等，理由桶为空
    wg = prev_good["would_pass"]
    assert wg["would_pass_all"] == wg["pairs"] > 0, wg
    assert wg["reasons"] == {}, wg["reasons"]
    assert wg["would_pass_status"] == wg["pairs"]
    assert wg["would_pass_scope"] == wg["pairs"]
    assert wg["would_pass_condition"] == wg["pairs"]
    assert wg["would_pass_source"] == wg["pairs"]
    assert wg["would_pass_source"] == prev_good["would_reach_k3"]
    # 拔掉 status：立即 0，status_not_eligible 满桶
    assert prev_ns["would_pass"]["would_pass_all"] == 0
    assert prev_ns["would_pass"]["reasons"].get(
        "status_not_eligible", 0) == prev_ns["would_pass"]["pairs"]
    # 拔掉 scope：立即 0，excluded_scope_uncertain 满桶
    assert prev_nsc["would_pass"]["would_pass_all"] == 0
    assert prev_nsc["would_pass"]["reasons"][
        "excluded_scope_uncertain"] == prev_nsc["would_pass"]["pairs"]
    # 拔掉来源版本：would_pass_source 恒 0，upper_bound 同步如实剔除
    assert prev_odd["would_pass"]["would_pass_all"] == 0
    assert prev_odd["would_pass"]["would_pass_source"] == 0
    assert "text_version:k2v2-pilot" in prev_odd["would_pass"]["reasons"]
    assert prev_odd["upper_bound"]["stripped"] == {"text_version:k2v2-pilot": 1}
    assert prev_odd["would_reach_k3"] == 0


# ── 3. 来源闸在真判据段同样照实（benchmark 段 per-pair 分桶）──────────
def test_would_pass_reports_benchmark_source_gate_at_pair_level():
    a = _mk("bs-bench", source_type="human_fiction",
            segs=[("benchmark", True, True)] * 2)
    with db.session() as s:
        queues, _stats = k2b.build_queues(
            s, strategy_keys=(a["key"],), source_scope="benchmark")
        prev = k2b.k3_evidence_preview(s, queues)
    wp = prev["would_pass"]
    assert prev["pairs"] == 2 and prev["would_reach_k3"] == 0
    assert prev["stripped"] == {"benchmark_source": 2}   # 改前字段逐字不变
    assert wp["would_pass_all"] == 0 and wp["would_pass_source"] == 0
    assert wp["reasons"]["benchmark_source"] == 2, wp["reasons"]


# ── 4. 与 query_knowledge 一致性：证据落库前后方向同向 ─────────────────
def test_query_knowledge_consistency_before_and_after_evidence():
    g = _mk("cq-good", segs=[("train", True, True), (None, True, True)],
            status="verified", observation_status="observed")
    policy = {"book_id": g["work_id"],
              "limits": {"candidate_cap": 10, "context_items": 0}}
    with db.session() as s:
        prev = k2b.k3_evidence_preview(
            s, _queues_for(s, g["work_id"], g["key"]))
        resp = KQ.query_knowledge(policy, s)
    # 预演说「全过」；证据还没落 → 真查询只差证据本身
    assert prev["would_pass"]["would_pass_all"] > 0, prev["would_pass"]
    assert g["key"] not in [e["strategy_key"] for e in resp["selected"]]
    assert any(r["strategy_key"] == g["key"]
               and r["reason"] == "excluded_no_evidence"
               for r in resp["rejected"]), resp["rejected"]
    # 落 verified 实例（模拟抽到并核验）后，同一 policy 真查询命中该策略
    with db.session() as s:
        st = _strategy(s, g["key"])
        segs = (s.query(Segment).filter_by(work_id=g["work_id"])
                .order_by(Segment.ordinal).all())
        for seg in segs:
            ev = TEXT[0:8]
            s.add(StrategyInstance(
                strategy_id=st.id, strategy_version=st.version,
                work_id=g["work_id"], segment_id=seg.id, text_version="corpus-v1",
                span_start=0, span_end=8, evidence_text=ev,
                evidence_sha256=_h.sha256(ev.encode()).hexdigest(),
                conditions_observed={}, observed_content="克制沉默",
                extractor_model="fx", status="verified"))
        s.commit()
        resp2 = KQ.query_knowledge(
            {"book_id": g["work_id"],
             "limits": {"candidate_cap": 10, "context_items": 0}}, s)
    assert resp2["status"] == "matched", resp2["status"]
    assert g["key"] in [e["strategy_key"] for e in resp2["selected"]], \
        resp2["selected"]


def test_query_knowledge_consistency_pin_still_empty_after_instances():
    """反向一致性：8 条 hypothesis/UNCERTAIN 即便落 verified 实例，真查询
    status 预筛就把整批挡在门外（considered 不含它们）——preview 的
    status_not_eligible 满桶与真预筛同根。断言用 key 级对账，不依赖全局
    selected（共享测试库里其他用例可能另有命中）。"""
    w = _mk("cq-pin", segs=[("train", True, True)] * 2, scope="UNCERTAIN")
    with db.session() as s:
        st = _strategy(s, w["key"])
        segs = (s.query(Segment).filter_by(work_id=w["work_id"]).all())
        for seg in segs:
            ev = TEXT[0:8]
            s.add(StrategyInstance(
                strategy_id=st.id, strategy_version=st.version,
                work_id=w["work_id"], segment_id=seg.id,
                text_version="corpus-v1", span_start=0, span_end=8,
                evidence_text=ev,
                evidence_sha256=_h.sha256(ev.encode()).hexdigest(),
                conditions_observed={}, observed_content="x",
                extractor_model="fx", status="verified"))
        s.commit()
        resp = KQ.query_knowledge({"book_id": w["work_id"]}, s)
    assert st.status == "hypothesis"
    assert w["key"] not in [e["strategy_key"] for e in resp["selected"]]


# ── 5. 公开 API 形状（k3_would_pass 逐对判据）────────────────────────
def test_would_pass_public_api_shape():
    g = _mk("api-shape", segs=[("train", True, True)], status="verified",
            observation_status="observed")
    with db.session() as s:
        st = _strategy(s, g["key"])
        seg = (s.query(Segment).filter_by(work_id=g["work_id"]).one())
        v = k2b.k3_would_pass(
            s, st, seg, "corpus-v1",
            work_source=(s.query(WorkSource)
                         .filter(WorkSource.work_id == g["work_id"]).one()))
    assert v["would_pass_all"] is True and v["reasons"] == []
    for k in ("would_pass_status", "would_pass_scope", "would_pass_condition",
              "would_pass_source", "would_pass_all"):
        assert k in v and isinstance(v[k], bool), v