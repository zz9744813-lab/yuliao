"""K2 非 benchmark 试点通道回归（scripts/k2_extract_backfill.py 的
`--source-scope`，主控派工 2026-09-23，审计 P0 主线 1）。

钉住的事：
1. **默认口径逐字不变**：scope=benchmark 的段宇宙（取段集合+顺序）与改造前的
   历史实现逐项相等；报告的既有键与取值一字不动（新增键只做加法）；
2. **nonbenchmark 显式判定**：只收 source_type ∈ {human_fiction} ∪ 前缀
   production_nonbenchmark_ 的合规人类语料，**且**段 role 显式判定「不等于
   benchmark」（train 与 NULL 都算非基准——不是「把 role 当 null」的模糊口径）；
   被排除的来源与段数、入池段的 role 分布全部留痕进汇总；
3. **--dry-run 零真实调用零库写**（extract_segment 计数为 0、StrategyInstance
   行数不变），且预演报出候选来源数/合格段数/可配对总数/逐来源排除原因；
4. **不碰 K3 硬口径**：k3_preview 只是只读预演——默认口径下全部对被判
   benchmark_source 剔除（P0「可进 K3 的证据恒 0」在夹具里复现），
   试点源 text_version 不在 K3 允许集内时同样如实报剔除（不放宽、不改写）；
5. **既有的幂等/预算/双闸门不放宽**：nonbenchmark 下重跑跳已落行、
   预算超限显式 blocked_budget、--live 缺 K2_ALLOW_LIVE 或 LG_LLM_MODE≠real
   时 SystemExit 且**未发起任何调用**；
6. CLI：--source-scope 只接两值（其余 argparse 拒），--help 写清两值语义。

全部离线（conftest 的临时库 + mock LLM），不跑 --live、不联网、不碰生产库。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2nb", ROOT / "scripts" / "k2_extract_backfill.py")
k2b = _u.module_from_spec(_spec); _spec.loader.exec_module(k2b)

from app import db, knowledge_extract as KE                # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,     # noqa: E402
                        StrategyInstance, Work, WorkSource)

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"
_seq = [0]

# 改造前的历史实现（scripts/k2_extract_backfill.py@53bb00c 逐字搬来当尺子）：
# 默认口径必须与它逐项相等——口径漂移在这里当场红。
def _legacy_eligible_segments(s) -> list:
    rows = []
    for seg in (s.query(Segment).filter(Segment.role == "benchmark")
                .order_by(Segment.ordinal, Segment.work_id).all()):
        try:
            integ = json.loads(seg.integrity or "{}")
        except Exception:                                      # noqa: BLE001
            integ = {}
        if integ.get("src_ok") is not True or not (seg.text_clean or "").strip():
            continue
        rows.append(seg)
    return rows


class _FxOK:
    """夹具客户端：给出与原文逐字对齐的证据（走既有输出门/证据门，不放松）。"""
    def __init__(self):
        self.calls = 0

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        self.calls += 1
        body = {"span_start": 0, "span_end": 8,
                "evidence_text": TEXT[0:8], "observed_content": "克制沉默"}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 5, "actual_model": "fx"}


def _mk(tag: str, *, source_type: str, segs: list[tuple],
        text_version: str = "corpus-v1", register: bool = True,
        strategy: bool = True, scope: str = "WORK") -> dict:
    """一部作品 + 若干段（segs=[(role, src_ok, clean_ok), ...]）+ 登记行 +
    一条假设策略（默认 scope=WORK 绑自家作品——共享测试库里 UNCERTAIN=全集
    会把别的用例吸进来）。返回 work_id / 段 id（按 ordinal）/ 策略键。"""
    _seq[0] += 1
    key = f"k2nb-{_seq[0]}-{tag}"
    db.init_db()
    with db.session() as s:
        w = Work(title=f"t-{key}", source="test:k2nb")
        s.add(w)
        s.flush()
        for i, (role, src_ok, clean_ok) in enumerate(segs):
            s.add(Segment(work_id=w.id, ordinal=i, text=TEXT,
                          text_clean=TEXT if clean_ok else "",
                          role=role, n_chars=len(TEXT), n_sentences=1,
                          integrity=json.dumps({"src_ok": bool(src_ok)})))
        s.flush()
        import register_work_sources as REG       # 单一哈希口径（锚复核纪律）
        sha, _ = REG._work_sha256(s, w.id)
        if register:
            s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                             source_type=source_type, text_version=text_version,
                             text_sha256=sha,
                             purpose_basis="试点夹具：只验抽取侧口径，非生产语料",
                             identity_purposes=["research"], license_purposes=[],
                             license_basis="seed", metadata_status="verified",
                             metadata_basis="seed"))
        s.add(ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            effect_hypothesis=f"假设-{key}", status="hypothesis",
            scope=scope, scope_ids=[w.id] if scope == "WORK" else []))
        s.commit()
        seg_ids = [x.id for x in s.query(Segment).filter_by(work_id=w.id)
                   .order_by(Segment.ordinal).all()]
    return {"work_id": w.id, "seg_ids": seg_ids, "key": key}


def _line(trace: dict, work_id: str, list_key: str) -> dict | None:
    for row in trace[list_key]:
        if row["work_id"] == work_id:
            return row
    return None


def _n_instances() -> int:
    with db.session() as s:
        return s.query(StrategyInstance).count()


# ── 1. 默认口径逐字不变 ─────────────────────────────────────────────
def test_default_scope_universe_verbatim_vs_legacy():
    a = _mk("bench", source_type="human_fiction",
            segs=[("benchmark", True, True),          # 合格基准段
                  ("benchmark", False, False),        # src_ok=false + 空 clean
                  ("benchmark", True, False)])        # 只有 text_clean 空
    b = _mk("mixed", source_type="production_nonbenchmark_k2v2",
            segs=[("train", True, True), (None, True, True),
                  ("benchmark", True, True)])
    with db.session() as s:
        legacy_ids = [x.id for x in _legacy_eligible_segments(s)]
        got_default = [x.id for x in k2b.eligible_segments(s)]
        got_explicit = [x.id for x in k2b.eligible_segments(
            s, source_scope="benchmark")]
    assert got_default == legacy_ids, \
        "默认 scope=benchmark 与改造前的历史实现不再逐项相等——口径漂移"
    assert got_explicit == got_default, "显式传默认值必须等于不传"
    assert a["seg_ids"][0] in got_default and a["seg_ids"][1] not in got_default \
        and a["seg_ids"][2] not in got_default
    assert b["seg_ids"][2] in got_default
    assert b["seg_ids"][0] not in got_default and b["seg_ids"][1] not in got_default, \
        "非基准段不得因「新通道」而混进默认口径"


def test_default_scope_report_legacy_keys_unchanged():
    """既有键/取值逐字保持（新增只做加法）：既有口径的 5 个 skips 键、
    n_pending_pairs/would_attempt/attempted/written 语义不变。"""
    a = _mk("keys", source_type="human_fiction",
            segs=[("benchmark", True, True)] * 3)
    with db.session() as s:
        rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                              strategy_keys=(a["key"],))
    assert rep["source_scope"] == "benchmark"
    for k in ("n_strategies", "n_eligible_segments", "skipped_done",
              "skipped_scope_no_segments", "skipped_no_text_version"):
        assert k in rep["skips"], f"既有 skips 键被删：{k}"
    assert rep["skips"]["n_strategies"] == 1
    assert rep["skips"]["skipped_done"] == 0
    assert rep["skips"]["skipped_no_text_version"] == 0
    assert rep["n_pending_pairs"] == 3 and rep["would_attempt"] == 3
    assert rep["attempted"] == 0 and rep["written"] == 0


# ── 2. nonbenchmark 的显式判定与留痕 ────────────────────────────────
def test_nonbenchmark_admits_compliant_source_and_drops_benchmark_role():
    ok = _mk("nb-ok", source_type="production_nonbenchmark_k2v2",
             segs=[("train", True, True),        # 0 收
                   (None, True, True),           # 1 收（NULL≠benchmark）
                   ("benchmark", True, True),    # 2 排除（基准段）
                   ("train", False, True)])      # 3 排除（src_ok=false）
    hf = _mk("nb-hf", source_type="human_fiction", segs=[("train", True, True)])
    fixture = _mk("nb-fixture", source_type="fixture", segs=[("train", True, True)])
    comment = _mk("nb-commentary", source_type="commentary",
                  segs=[(None, True, True)])
    synthetic = _mk("nb-synthetic", source_type="synthetic",
                    segs=[("train", True, True)])
    bench_only = _mk("nb-benchonly", source_type="human_fiction",
                     segs=[("benchmark", True, True)])
    noreg = _mk("nb-noreg", source_type="human_fiction",
                segs=[("train", True, True)], register=False)
    works = [ok, hf, fixture, comment, synthetic, bench_only, noreg]
    wf = tuple(x["work_id"] for x in works)
    with db.session() as s:
        segs, trace = k2b.segment_universe(s, source_scope="nonbenchmark",
                                           work_filter=wf)
    ids = {x.id for x in segs}
    assert ok["seg_ids"][0] in ids and ok["seg_ids"][1] in ids, \
        "合规人类源的非基准段（train 与 NULL）必须收——判据是「不等于 benchmark」"
    assert hf["seg_ids"][0] in ids
    assert ok["seg_ids"][2] not in ids, "基准段不得进 nonbenchmark 宇宙"
    assert ok["seg_ids"][3] not in ids, "src_ok=false 不得因新通道而放宽"
    for w in (fixture, comment, synthetic, bench_only, noreg):
        assert w["seg_ids"][0] not in ids, f"不合格来源被收进来了：{w['work_id']}"
    assert all((x.role or "") != "benchmark" for x in segs)
    # work_filter 下留痕是本用例作品的封闭账：数字必须逐条对得上
    assert trace["n_sources_seen"] == len(works)
    assert trace["n_sources_qualified"] == 2, {x["work_id"]: x["reason"]
                                               for x in trace["excluded_sources"]}
    assert trace["n_sources_excluded"] == 5
    reasons = {x["work_id"]: x["reason"] for x in trace["excluded_sources"]}
    assert reasons[fixture["work_id"]] == "source_type_not_compliant:fixture"
    assert reasons[comment["work_id"]] == "source_type_not_compliant:commentary"
    assert reasons[synthetic["work_id"]] == "source_type_not_compliant:synthetic"
    assert reasons[bench_only["work_id"]] == "no_nonbenchmark_role_segment"
    assert reasons[noreg["work_id"]] == "no_registry"
    q = _line(trace, ok["work_id"], "qualified_sources")
    assert q["n_segments"] == 4 and q["n_segments_in_scope"] == 3
    assert q["n_segments_dropped_by_scope"] == 1      # 那 1 条 benchmark 段
    assert q["n_segments_dropped_by_source_gate"] == 1    # 那 1 条 src_ok=false
    assert q["n_segments_kept"] == 2
    assert q["segments_by_role"] == {"NULL": 1, "benchmark": 1, "train": 2}
    assert trace["segments_by_role"] == {"NULL": 1, "train": 2}, \
        "入池段的 role 分布必须留痕（基准段计数恒 0）"
    assert trace["excluded_segments"] == {"src_ok_not_true": 1}
    assert trace["source_scope"] == "nonbenchmark"
    # 默认口径在同一批作品上的对照账：为什么历史 82 条全是 benchmark
    with db.session() as s:
        _segs_b, trace_b = k2b.segment_universe(s, work_filter=wf)
    reasons_b = {x["work_id"]: x["reason"] for x in trace_b["excluded_sources"]}
    assert trace_b["n_sources_qualified"] == 2
    assert reasons_b[fixture["work_id"]] == "no_benchmark_role_segment"
    # ok 作品在默认口径下有合格 benchmark 段 → 进 qualified_sources（不在
    # excluded_sources 里），.get() 取不到即非「no_benchmark_role_segment」
    assert reasons_b.get(ok["work_id"]) != "no_benchmark_role_segment"
    assert _line(trace_b, ok["work_id"], "qualified_sources") is not None


def test_nonbenchmark_requires_registration_and_text_version():
    """合规人类源但缺登记/缺 text_version：段可入宇宙（段宇宙只管来源口径），
    配对阶段仍按 K1-A 契约 skip——新通道不绕开既有登记闸。"""
    notv = _mk("nb-notv", source_type="production_nonbenchmark_k2v2",
               segs=[("train", True, True)], text_version="")
    with db.session() as s:
        segs, trace = k2b.segment_universe(
            s, source_scope="nonbenchmark", work_filter=(notv["work_id"],))
    assert segs and trace["n_sources_qualified"] == 1
    with db.session() as s:
        rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                              strategy_keys=(notv["key"],),
                              source_scope="nonbenchmark")
    assert rep["n_pending_pairs"] == 0
    assert rep["skips"]["skipped_no_text_version"] == 1
    assert rep["would_attempt"] == 0


def test_scope_and_source_type_predicates_are_explicit():
    """判定支笔的单元测试：白名单精确值/前缀，与「role 为 null」的模糊口径划清。"""
    assert k2b.nonbenchmark_compliant_source("human_fiction") is True
    assert k2b.nonbenchmark_compliant_source("production_nonbenchmark_k2v2") is True
    assert k2b.nonbenchmark_compliant_source("production_nonbenchmark_") is True
    assert k2b.nonbenchmark_compliant_source(" human_fiction ") is True  # 去空白后判型
    for bad in ("fixture", "synthetic", "commentary", "benchmark_source",
                "train_corpus", "", None, "   ", " fixture ", "HUMAN_FICTION",
                "human_fiction_mirror", "production_benchmark_x",
                "nonbenchmark"):
        assert k2b.nonbenchmark_compliant_source(bad) is False, bad
    # 段 role：判据是「不等于 benchmark」，NULL/train 都算非基准
    assert k2b._role_passes_scope("nonbenchmark", "train") is True
    assert k2b._role_passes_scope("nonbenchmark", "NULL") is True
    assert k2b._role_passes_scope("nonbenchmark", "benchmark") is False
    assert k2b._role_passes_scope("benchmark", "benchmark") is True
    assert k2b._role_passes_scope("benchmark", "NULL") is False
    assert k2b._role_passes_scope("benchmark", "train") is False
    with pytest.raises(SystemExit, match="source_scope"):
        with db.session() as s:
            k2b.segment_universe(s, source_scope="whatever")


# ── 3. dry-run 零真实调用、零库写，且报出核对数字 ─────────────────────
def test_dry_run_zero_calls_zero_writes_both_scopes(monkeypatch):
    a = _mk("dry-bench", source_type="human_fiction",
            segs=[("benchmark", True, True)] * 2)
    b = _mk("dry-nb", source_type="production_nonbenchmark_k2v2",
            segs=[("train", True, True), (None, True, True)])
    calls = {"n": 0}

    def _spy(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("dry-run 不许发起抽取调用")
    monkeypatch.setattr(KE, "extract_segment", _spy)
    before = _n_instances()
    for scope, key, pairs in (("benchmark", a["key"], 2),
                              ("nonbenchmark", b["key"], 2)):
        with db.session() as s:
            rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                                  strategy_keys=(key,), source_scope=scope)
        assert rep["mode"] == "dry_run" and rep["source_scope"] == scope
        assert rep["attempted"] == 0 and rep["written"] == 0
        assert rep["n_pending_pairs"] == pairs and rep["would_attempt"] == pairs
        # 预演核对数字（主控问「为什么 82 条全是 benchmark」时看这三行+原因表）
        assert rep["n_candidate_sources"] == rep["skips"]["n_sources_seen"] >= 2
        assert rep["skips"]["n_eligible_segments"] >= pairs
        assert isinstance(rep["skips"]["excluded_sources"], list)
        assert rep["skips"]["excluded_sources"], "被排除来源必须逐个报出原因"
    assert calls["n"] == 0, "dry-run 触达了抽取调用"
    assert _n_instances() == before, "dry-run 写了库——违反零库写承诺"


def test_main_dry_run_cli_passes_scope_and_prints_tail(capsys, monkeypatch):
    """CLI 接线：--source-scope 必须传到 run_backfill（否则试点口径形同虚设），
    且 dry-run 全链路零调用零库写。"""
    b = _mk("cli-nb", source_type="production_nonbenchmark_k2v2",
            segs=[("train", True, True), ("benchmark", True, True)])
    calls = {"n": 0}

    def _spy(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("dry-run 不许发起抽取调用")
    monkeypatch.setattr(KE, "extract_segment", _spy)
    before = _n_instances()
    seen = {}
    real = k2b.run_backfill

    def _spy_run(s, client, **kw):
        seen.update(kw)
        return real(s, client, **kw)
    monkeypatch.setattr(k2b, "run_backfill", _spy_run)
    monkeypatch.setattr(sys, "argv", ["k2b", "--dry-run", "--source-scope",
                                      "nonbenchmark", "--limit", "48"])
    k2b.main()
    out = capsys.readouterr().out
    assert seen["source_scope"] == "nonbenchmark" and seen["dry_run"] is True
    assert calls["n"] == 0 and _n_instances() == before
    assert '"source_scope": "nonbenchmark"' in out
    assert "[k2_extract_backfill] scope=nonbenchmark" in out
    assert "候选来源=" in out and "合格段=" in out and "可配对=" in out
    assert "K3 可达预演=" in out


# ── 4. 不碰 K3 硬口径：preview 只是只读预演 ─────────────────────────
def test_k3_preview_reproduces_p0_zero_for_benchmark_scope():
    """默认口径下，抽到的对**全部**被 K3 判 benchmark_source 剔除——
    审计 P0「可进 K3 的生产证据为 0」在夹具里复现；本驱动一行不改 K3。"""
    a = _mk("k3-bench", source_type="human_fiction",
            segs=[("benchmark", True, True)] * 2)
    with db.session() as s:
        queues, _stats = k2b.build_queues(s, strategy_keys=(a["key"],))
        prev = k2b.k3_evidence_preview(s, queues)
    assert prev["pairs"] == 2 and prev["would_reach_k3"] == 0
    assert prev["stripped"] == {"benchmark_source": 2}, prev


def test_k3_preview_admits_pilot_and_still_honours_text_version_gate():
    """试点源（合规人类语料 + 非基准段）在 K3 来源闸下可达；
    text_version 不在 K3 允许集时**照样剔除**——抽取侧不替 K3 放宽硬口径。"""
    good = _mk("k3-nb", source_type="production_nonbenchmark_k2v2",
               segs=[("train", True, True), (None, True, True)])
    odd = _mk("k3-nb-odd", source_type="human_fiction",
              segs=[("train", True, True)], text_version="k2v2-pilot")
    with db.session() as s:
        queues, _stats = k2b.build_queues(
            s, strategy_keys=(good["key"],), source_scope="nonbenchmark")
        prev = k2b.k3_evidence_preview(s, queues)
    assert prev["pairs"] == 2 and prev["would_reach_k3"] == 2, prev
    with db.session() as s:
        queues2, _ = k2b.build_queues(
            s, strategy_keys=(odd["key"],), source_scope="nonbenchmark")
        prev2 = k2b.k3_evidence_preview(s, queues2)
    assert prev2["would_reach_k3"] == 0
    assert prev2["stripped"] == {"text_version:k2v2-pilot": 1}, prev2
    # K3 侧的默认排除集/允许版本一字未动（本任务禁止改它）
    from app import knowledge_query as KQ
    assert KQ.DEFAULT_EXCLUDED_SOURCE_TYPES == frozenset(
        {"fixture", "synthetic", "commentary"})
    assert "benchmark" not in KQ.DEFAULT_EXCLUDED_SOURCE_TYPES   # 段级另判
    with db.session() as s:
        rep = k2b.run_backfill(s, None, limit=48, dry_run=True,
                              strategy_keys=(odd["key"],),
                              source_scope="nonbenchmark")
    assert rep["k3_preview"]["stripped"] == {"text_version:k2v2-pilot": 1}


# ── 5. 既有幂等/预算/双闸门在 new 口径下不放宽 ──────────────────────
def test_nonbenchmark_run_persists_and_rerun_is_idempotent():
    b = _mk("idem-nb", source_type="production_nonbenchmark_k2v2",
            segs=[("train", True, True), (None, True, True)])
    fx = _FxOK()
    with db.session() as s:
        rep1 = k2b.run_backfill(s, fx, limit=48, strategy_keys=(b["key"],),
                                source_scope="nonbenchmark")
    assert rep1["attempted"] == 2 and rep1["verified"] == 2 and rep1["written"] == 2
    assert fx.calls == 2
    with db.session() as s:
        rows = (s.query(StrategyInstance)
                .filter_by(strategy_id=(s.query(ExpressionStrategyV2)
                                        .filter_by(strategy_key=b["key"])
                                        .one().id)).all())
        assert len(rows) == 2 and all(r.status == "verified" for r in rows)
        assert all(TEXT[r.span_start:r.span_end] == r.evidence_text for r in rows)
    with db.session() as s:
        rep2 = k2b.run_backfill(s, _FxOK(), limit=48, strategy_keys=(b["key"],),
                                source_scope="nonbenchmark")
    assert rep2["attempted"] == 0 and rep2["written"] == 0
    assert rep2["skips"]["skipped_done"] == 2, "幂等位必须照旧占用（新通道不例外）"


def test_nonbenchmark_budget_breakpoint_is_explicit():
    b = _mk("budget-nb", source_type="production_nonbenchmark_k2v2",
            segs=[("train", True, True), (None, True, True)])
    with db.session() as s:
        rep = k2b.run_backfill(s, _FxOK(), limit=48, max_calls=1,
                              strategy_keys=(b["key"],),
                              source_scope="nonbenchmark")
    assert rep["blocked_budget"] is True, "预算断点必须显式（禁静默放行）"
    assert rep["attempted"] == 1 and rep["written"] == 1   # 已抽候选保留
    assert rep["budget"]["calls"] == 1 and "blocked_at" in rep


def test_live_double_gate_refuses_without_calls(monkeypatch):
    """--live 双闸 fail-closed：缺 K2_ALLOW_LIVE=1 或缺 LG_LLM_MODE=real 都
    SystemExit，且**未发起任何调用**（预检/客户端/建队列零触达）。"""
    touches = {"run": 0, "extract": 0, "preflight": 0}

    def _count(key, exc=None):
        def _f(*a, **k):
            touches[key] += 1
            if exc:
                raise exc
            return {}
        return _f
    import preflight_models as PF
    monkeypatch.setattr(k2b, "run_backfill", _count("run"))
    monkeypatch.setattr(KE, "extract_segment", _count("extract"))
    monkeypatch.setattr(PF, "require_models", _count("preflight"))
    monkeypatch.setattr(k2b, "LLM_MODE", "mock")
    monkeypatch.delenv("K2_ALLOW_LIVE", raising=False)
    monkeypatch.setattr(sys, "argv", ["k2b", "--live", "--source-scope",
                                      "nonbenchmark", "--extractor-model", "m"])
    with pytest.raises(SystemExit, match="K2_ALLOW_LIVE"):
        k2b.main()
    monkeypatch.setenv("K2_ALLOW_LIVE", "1")
    monkeypatch.setattr(sys, "argv", ["k2b", "--live", "--source-scope",
                                      "nonbenchmark", "--extractor-model", "m"])
    with pytest.raises(SystemExit, match="LG_LLM_MODE=real"):
        k2b.main()
    assert touches == {"run": 0, "extract": 0, "preflight": 0}, touches


# ── 6. CLI 口径参数本身 ────────────────────────────────────────────
def test_cli_scope_choices_and_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["k2b", "--dry-run", "--source-scope",
                                      "whatever"])
    with pytest.raises(SystemExit) as e:
        k2b.main()
    assert e.value.code != 0, "非法 source_scope 必须被 argparse 拒"
    monkeypatch.setattr(sys, "argv", ["k2b", "--help"])
    with pytest.raises(SystemExit) as e0:
        k2b.main()
    assert e0.value.code == 0
    # argparse 会按终端宽度折行（中文长串还可能被切断）——去空白后比原文，
    # 断言的是 help 必须写清两值语义，不是它怎么排版
    flat = "".join(capsys.readouterr().out.split())
    assert "--source-scope" in flat
    assert "默认benchmark=现状逐字不变" in flat, "help 未写明默认值=现状不变"
    for needle in ("production_nonbenchmark_", "{human_fiction}",
                   "不等于'benchmark'", "把role当null", "不改K3",
                   "不放宽既有幂等/预算/双闸门"):
        assert needle in flat, f"help 未写清两值语义：{needle}"
    assert k2b.DEFAULT_SOURCE_SCOPE == "benchmark"
    assert k2b.SOURCE_SCOPES == ("benchmark", "nonbenchmark")
