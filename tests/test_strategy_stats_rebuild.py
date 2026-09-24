"""strategy_stats 的 usable_evidence 与 K3 真实口径对齐回归（主控派工
2026-09-23，复审实跑证虚高后收口）。

背景：usable_evidence 若在 SSR 本地复刻 K3 剔除链必虚高——复审席实跑
复现「SSR usable 6 vs K3 真实 ev_count 2」（根因：只复刻 6 道剔除链中的
benchmark 一道，漏 no_registry / excluded_source_type / text_version /
mirror_dedup，且计数单位实例 vs 唯一区间也不同）。本文件钉死：

1. compute() 直接调 kq._evidence_for(s, id, {})（只读），usable_evidence
   取第 2 返回值 ev_count——与 K3 **逐值相等**，四道遗漏剔除（#1 无登记 /
   #3 来源类型 / #5 文本版本 / #6 镜像去重）同时命中的夹具也不漂移；
2. benchmark_stripped = stripped 中 :benchmark_source 后缀条目数；
3. 既有字段计算式逐字不动：valid 仍含基准段实例（SSR 口径如实保留，
   与 usable_evidence 的差 = 两套口径的差距，不许悄悄合并）；
4. docstring 如实：零数据写（非零库写，main() 会 db.init_db() 可能有
   DDL）+「服务端封底 policy、调用方收窄不含」的口径限定。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "ssr", ROOT / "scripts" / "strategy_stats_rebuild.py")
ssr = _u.module_from_spec(_spec); _spec.loader.exec_module(ssr)

from app import db, knowledge_query as kq               # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        StrategyInstance, StrategyStats, Work, WorkSource)

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"
_n = [0]


def _seed() -> tuple[str, str, dict[str, str]]:
    """一套独立夹具：五部作品覆盖 K3 六道剔除链中的四道遗漏 + benchmark。

    · w_noreg：故意不登记 → #1 no_registry；
    · w_fixture：登记 source_type=fixture → #3 excluded_source_type；
    · w_tv9：登记合格，但实例 text_version=corpus-v9 → #5 text_version；
    · w_root + 镜像 w_mirror（canonical 回连 w_root）：同区间重复实例
      → #6 mirror_dedup；另一不同区间实例保留；
    · w_bench：登记合格、corpus-v1，但段 role=benchmark → benchmark_source。
    全部实例 verified（另加 1 条 proposed 钉 attempts/missing 口径）——
    虚高对照的关键：SSR 旧口径 valid=7，K3 真实可用区间只有 2。
    实例 id 显式给定，stripped 可逐条断言。
    """
    _n[0] += 1
    key = f"ssr-{_n[0]}"
    db.init_db()
    with db.session() as s:
        w_root = Work(title=f"t-ssr-root-{key}", source="test:ssr")
        s.add(w_root)
        s.flush()
        w_mirror = Work(title=f"t-ssr-mirror-{key}", source="test:ssr",
                        v2_of=w_root.id)   # 镜像身份只认 works.v2_of（§4.1）
        w_noreg = Work(title=f"t-ssr-noreg-{key}", source="test:ssr")
        w_fixture = Work(title=f"t-ssr-fixture-{key}", source="test:ssr")
        w_tv9 = Work(title=f"t-ssr-tv9-{key}", source="test:ssr")
        w_bench = Work(title=f"t-ssr-bench-{key}", source="test:ssr")
        s.add_all([w_mirror, w_noreg, w_fixture, w_tv9, w_bench])
        s.flush()

        import register_work_sources as REG

        def _reg(w, canonical, stype):
            sha, _ = REG._work_sha256(s, w.id)
            s.add(WorkSource(work_id=w.id, canonical_work_id=canonical,
                             source_type=stype, text_version="corpus-v1",
                             text_sha256=sha, purpose_basis="测试夹具",
                             identity_purposes=["research"],
                             license_purposes=[], license_basis="seed",
                             metadata_status="verified",
                             metadata_basis="seed"))

        _reg(w_root, w_root.id, "human_fiction")
        _reg(w_mirror, w_root.id, "human_fiction")
        _reg(w_fixture, w_fixture.id, "fixture")
        _reg(w_tv9, w_tv9.id, "human_fiction")
        _reg(w_bench, w_bench.id, "human_fiction")
        # w_noreg 故意不登记 → #1

        def _seg(w, ordinal, role=None):
            seg = Segment(work_id=w.id, ordinal=ordinal, text=TEXT,
                          text_clean=TEXT, role=role, n_chars=len(TEXT),
                          n_sentences=1, integrity='{"src_ok": true}')
            s.add(seg)
            s.flush()
            return seg

        st = ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            effect_hypothesis=f"假设-{key}", status="hypothesis",
            scope="WORK", scope_ids=[w_root.id])
        s.add(st)
        s.flush()

        def _inst(iid, seg, tv, s0, s1, status="verified"):
            ev = TEXT[s0:s1]
            s.add(StrategyInstance(
                id=f"SI-{iid}-{key}", strategy_id=st.id,
                strategy_version=st.version, work_id=seg.work_id,
                segment_id=seg.id, text_version=tv, span_start=s0,
                span_end=s1, evidence_text=ev,
                evidence_sha256=hashlib.sha256(ev.encode()).hexdigest(),
                observed_content="x", extractor_model="fx", status=status))

        _inst("A-NOREG", _seg(w_noreg, 0), "corpus-v1", 0, 8)      # #1
        _inst("B-FIX", _seg(w_fixture, 0), "corpus-v1", 0, 8)      # #3
        _inst("C-TV9", _seg(w_tv9, 0), "corpus-v9", 0, 8)          # #5
        _inst("D1-ROOT", _seg(w_root, 0), "corpus-v1", 0, 8)       # 计
        _inst("D2-MIRR", _seg(w_mirror, 0), "corpus-v1", 0, 8)     # #6 去重
        _inst("D3-ROOT", _seg(w_root, 1), "corpus-v1", 16, 24)     # 计
        _inst("E-BENCH", _seg(w_bench, 0, role="benchmark"),
              "corpus-v1", 0, 8)                                    # 基准段
        _inst("P-PROP", _seg(w_root, 2), "corpus-v1", 0, 4,
              status="proposed")               # missing 口径：不进 K3 证据
        s.commit()
        ids = {"root": w_root.id, "mirror": w_mirror.id,
               "noreg": w_noreg.id, "fixture": w_fixture.id,
               "tv9": w_tv9.id, "bench": w_bench.id}
        return key, st.id, ids


def test_usable_evidence_matches_k3_value_for_value():
    """虚高夹具改后：usable_evidence 必须与 kq._evidence_for(...)[1] 逐值
    相等（K3 ev_count=2），四道遗漏剔除 + 基准段剔除逐条命中 stripped；
    既有字段计算式逐字不动（valid 仍含基准段实例）。"""
    key, st_id, ids = _seed()
    rep = ssr.run(apply=False)
    mine = [r for r in rep["rows"] if r["strategy_key"] == key][0]
    with db.session() as s:
        refs, ev_count, stripped = kq._evidence_for(s, st_id, {})
    # 逐值相等（K3 唯一区间口径，非实例数）
    assert mine["usable_evidence"] == ev_count, \
        f"usable_evidence={mine['usable_evidence']} vs K3 ev_count={ev_count}"
    assert ev_count == 2, \
        f"夹具应命中 K3 ev_count=2（虚高复现口径），实得 {ev_count}"
    assert len(refs) == 2
    assert {r["canonical_work"] for r in refs} == {ids["root"]}
    # 四道遗漏剔除逐条命中（实例 id 显式给定）
    assert f"SI-A-NOREG-{key}:no_registry" in stripped
    assert f"SI-B-FIX-{key}:excluded_source_type:fixture" in stripped
    assert f"SI-C-TV9-{key}:text_version:corpus-v9" in stripped
    assert f"SI-D2-MIRR-{key}:mirror_dedup" in stripped
    # benchmark_stripped = :benchmark_source 后缀条目数
    assert f"SI-E-BENCH-{key}:benchmark_source" in stripped
    assert mine["benchmark_stripped"] == 1
    # 既有字段语义未削弱（计算式逐字不动）：
    # valid=7（verified 全计，含基准段/fixture/无登记实例——SSR 口径如实
    # 保留），attempts=8、missing=1（proposed），镜像经 canonical 回连同根。
    assert mine["valid"] == 7 and mine["attempts"] == 8
    assert mine["rejected"] == 0 and mine["missing"] == 1
    assert mine["counter_examples"] == 0
    assert mine["root_works"] == 5
    assert mine["unique_source_intervals"] == 6, \
        "去重 (根, evidence_sha256)：D1/D2 同根同文只计一次"
    # by_root_work 精确值（镜像 3 条全回连根，root_works 只认根）
    assert mine["by_root_work"] == {
        ids["noreg"]: 1, ids["fixture"]: 1, ids["tv9"]: 1,
        ids["root"]: 3, ids["bench"]: 1}
    # 口径差如实可见：7 条 verified vs K3 真实可用 2——差距不被悄悄合并
    assert mine["valid"] - mine["usable_evidence"] == 5


def test_apply_persists_usable_evidence_in_extras_and_dry_run_zero_write():
    """新计数只旁路追加进 extras（既有列零改动）；dry-run 零数据写。"""
    key, st_id, ids = _seed()
    with db.session() as s:
        before = s.query(StrategyStats).count()
    rep = ssr.run(apply=False)
    assert rep["mode"] == "dry_run"
    with db.session() as s:
        assert s.query(StrategyStats).count() == before, \
            "dry-run 写了 strategy_stats——违反零数据写承诺"
    rep2 = ssr.run(apply=True)
    assert rep2["applied"] >= 1
    with db.session() as s:
        row = s.query(StrategyStats).filter_by(strategy_id=st_id).one()
        # 既有列不变
        assert row.valid == 7 and row.attempts == 8
        assert row.unique_source_intervals == 6 and row.root_works == 5
        # 新计数在 extras 旁路
        assert row.extras["usable_evidence"] == 2
        assert row.extras["benchmark_stripped"] == 1
        assert row.extras["by_root_work"][ids["root"]] == 3


def test_docstring_honest_wording():
    """钉死 docstring 两处过度声明的修正不再回退。"""
    doc = (ROOT / "scripts" / "strategy_stats_rebuild.py").read_text(
        encoding="utf-8")
    assert "零库写" not in doc, \
        "「零库写」是过度声明（main() 会 db.init_db()，可能有 DDL）"
    assert "零数据写" in doc
    assert "调用方收窄不含" in doc, \
        "usable_evidence 必须限定为服务端封底 policy 口径"
