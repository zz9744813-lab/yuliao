"""strategy_stats 证据分档回归（主控派工 lg-stats-evidence-class2，2026-09-27）。

背景（审计非阻断项）：投影历来只按「观察证据」口径报数（verified 实例数，
**含** benchmark 基准段实例）——而 K3 准入判据（app/knowledge_query.py::
_evidence_for：ELIGIBLE_INSTANCE_STATUS 只认 verified，且基准段实例硬拦
:benchmark_source）是另一个数；「82 条有证据」被读成「K3 能用 82 条」是
2026-09-26 简报已实锤的误读形态。本文件钉死三点：

1. 分档正确性：构造「benchmark 有证据」与「非 benchmark 有证据」两组
   样本，断言只有后者进 extras.k3_eligible_instances /
   extras.k3_eligible_root_works；
2. 同判据：k3_eligible_* 与 kq._evidence_for 的返回值（refs）逐值对照
   ——字段从**同一次**准入判据链调用派生，禁止本地另复刻准入判据
   （本项目反复出现的缺陷形态：只复刻一道剔除就虚高）；
3. 既有字段值不变：既有列与 extras 键按夹具真值逐条钉死
   （valid/unique_source_intervals/root_works/usability/by_root_work
   等），口径不许悄悄改。
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
    "ssr_ec", ROOT / "scripts" / "strategy_stats_rebuild.py")
ssr = _u.module_from_spec(_spec); _spec.loader.exec_module(ssr)

from app import config, db, knowledge_query as kq        # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        StrategyInstance, StrategyStats, Work, WorkSource)

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"
_n = [0]


def _seed() -> tuple[str, dict[str, str]]:
    """两套样本组（同库共存，另一文件 test_strategy_stats_rebuild 的夹具
    也在——所有断言只按本夹具 strategy_key 取行，互不干扰）：

    · 策略 "-bonly"：**只有 benchmark 证据**——登记合格、corpus-v1、
      verified，但段 role=benchmark → K3 硬拦，观察口径 valid=1；
    · 策略 "-mixed"：benchmark 证据（w_bench）+ 非 benchmark 证据
      （w_plain）+ w_plain 的镜像同区间重复（mirror_dedup）各一条——
      观察口径三条全计，K3 可用口径只剩 w_plain 一条。
    实例 id 显式给定，stripped 可逐条断言。
    """
    _n[0] += 1
    key = f"ec-{_n[0]}"
    db.init_db()
    with db.session() as s:
        w_plain = Work(title=f"t-ec-plain-{key}", source="test:ec")
        s.add(w_plain)
        s.flush()
        w_mirror = Work(title=f"t-ec-mirror-{key}", source="test:ec",
                        v2_of=w_plain.id)   # 镜像身份只认 works.v2_of（§4.1）
        w_bench = Work(title=f"t-ec-bench-{key}", source="test:ec")
        w_bench2 = Work(title=f"t-ec-bench2-{key}", source="test:ec")
        s.add_all([w_mirror, w_bench, w_bench2])
        s.flush()

        import register_work_sources as REG

        def _reg(w, canonical):
            sha, _ = REG._work_sha256(s, w.id)
            s.add(WorkSource(work_id=w.id, canonical_work_id=canonical,
                             source_type="human_fiction",
                             text_version="corpus-v1", text_sha256=sha,
                             purpose_basis="测试夹具",
                             identity_purposes=["research"],
                             license_purposes=[], license_basis="seed",
                             metadata_status="verified",
                             metadata_basis="seed"))

        _reg(w_plain, w_plain.id)
        _reg(w_mirror, w_plain.id)   # 镜像 canonical 回连根
        _reg(w_bench, w_bench.id)
        _reg(w_bench2, w_bench2.id)

        def _seg(w, role=None):
            seg = Segment(work_id=w.id, ordinal=0, text=TEXT,
                          text_clean=TEXT, role=role, n_chars=len(TEXT),
                          n_sentences=1, integrity='{"src_ok": true}')
            s.add(seg)
            s.flush()
            # 段建在登记之后——按当前库内容重算锚，否则 verify 判
            # anchor_drift（单一哈希来源见 tests/registry_anchor.py）。
            from registry_anchor import refresh as _refresh
            _refresh(s, w.id)
            return seg

        st_mixed = ExpressionStrategyV2(
            strategy_key=f"{key}-mixed",
            abstract_operation=f"操作-{key}-mixed",
            effect_hypothesis=f"假设-{key}-mixed", status="hypothesis",
            scope="WORK", scope_ids=[w_plain.id])
        st_bonly = ExpressionStrategyV2(
            strategy_key=f"{key}-bonly",
            abstract_operation=f"操作-{key}-bonly",
            effect_hypothesis=f"假设-{key}-bonly", status="hypothesis",
            scope="WORK", scope_ids=[w_bench2.id])
        s.add_all([st_mixed, st_bonly])
        s.flush()

        def _inst(iid, st, seg, s0, s1):
            ev = TEXT[s0:s1]
            s.add(StrategyInstance(
                id=f"SI-{iid}-{key}", strategy_id=st.id,
                strategy_version=st.version, work_id=seg.work_id,
                segment_id=seg.id, text_version="corpus-v1",
                span_start=s0, span_end=s1, evidence_text=ev,
                evidence_sha256=hashlib.sha256(ev.encode()).hexdigest(),
                observed_content="x", extractor_model="fx",
                status="verified"))

        seg_plain = _seg(w_plain)
        seg_mirror = _seg(w_mirror)
        seg_bench = _seg(w_bench, role="benchmark")
        seg_bench2 = _seg(w_bench2, role="benchmark")
        # mixed 组：benchmark 有证据 + 非 benchmark 有证据 + 镜像重复
        _inst("BENCH", st_mixed, seg_bench, 0, 8)
        _inst("PLAIN", st_mixed, seg_plain, 0, 8)
        _inst("MIRR", st_mixed, seg_mirror, 0, 8)
        # bonly 组：只有 benchmark 证据
        _inst("BONLY", st_bonly, seg_bench2, 0, 8)
        s.commit()
        return key, {"st_mixed": st_mixed.id, "st_bonly": st_bonly.id,
                     "plain": w_plain.id, "mirror": w_mirror.id,
                     "bench": w_bench.id, "bench2": w_bench2.id}


def _rows(rep: dict, key: str) -> tuple[dict, dict]:
    mine = [r for r in rep["rows"] if r["strategy_key"] == f"{key}-mixed"][0]
    bonly = [r for r in rep["rows"]
             if r["strategy_key"] == f"{key}-bonly"][0]
    return mine, bonly


def test_only_non_benchmark_evidence_enters_k3_eligible():
    """分档正确性：两组样本都「有证据」（verified），但只有非 benchmark
    组进 k3_eligible_*；benchmark-only 策略观察口径 valid=1 而
    k3_eligible_*=0——「有证据」≠「K3 能用」在此显式分离。"""
    key, ids = _seed()
    rep = ssr.run(apply=False)
    mine, bonly = _rows(rep, key)
    # benchmark 有证据组：观察口径有数，K3 可用口径为零
    assert bonly["valid"] == 1 and bonly["attempts"] == 1
    assert bonly["usable_evidence"] == 0
    assert bonly["benchmark_stripped"] == 1
    assert bonly["k3_eligible_instances"] == 0
    assert bonly["k3_eligible_root_works"] == 0
    # 混合组：3 条 verified（含基准段 + 镜像重复）→ K3 可用只剩 1 实例/1 根
    assert mine["valid"] == 3
    assert mine["k3_eligible_instances"] == 1
    assert mine["k3_eligible_root_works"] == 1


def test_k3_eligible_same_criteria_as_knowledge_query():
    """同判据：k3_eligible_* 与 kq._evidence_for 的 refs 逐值对照——
    字段从同一次准入判据链派生，剔除链（:benchmark_source /
    mirror_dedup）逐条命中；不许另写一套判据（虚高教训见
    test_strategy_stats_rebuild.py 头注）。"""
    key, ids = _seed()
    rep = ssr.run(apply=False)
    mine, bonly = _rows(rep, key)
    with db.session() as s:
        refs_m, ev_m, stripped_m = kq._evidence_for(s, ids["st_mixed"], {})
        refs_b, _ev_b, stripped_b = kq._evidence_for(s, ids["st_bonly"], {})
    # 实例级 / 根作品级都与 live 判据链返回值逐值相等
    assert mine["k3_eligible_instances"] == len(refs_m)
    assert mine["k3_eligible_root_works"] == \
        len({r["canonical_work"] for r in refs_m})
    assert bonly["k3_eligible_instances"] == len(refs_b) == 0
    assert bonly["k3_eligible_root_works"] == \
        len({r["canonical_work"] for r in refs_b}) == 0
    # refs 确实只含非 benchmark、非镜像重复的那一条：镜像与根同
    # (canonical, span)，_evidence_for 内部按「先到者进 seen_intervals」
    # 去重，**行序未排序**（判据链无 ORDER BY）——因此本处只钉「恰好一条、
    # 根作品正确、被剔的那条以 mirror_dedup 命中」，不钉具体是哪条 id
    # （钉 id 等于钉 DB 行序，换后端会假红；会审 2026-09-27 指出）。
    assert len(refs_m) == 1
    assert {r["canonical_work"] for r in refs_m} == {ids["plain"]}
    assert refs_m[0]["instance_id"] in {f"SI-PLAIN-{key}", f"SI-MIRR-{key}"}
    _deduped = ({f"SI-PLAIN-{key}", f"SI-MIRR-{key}"}
                - {refs_m[0]["instance_id"]})
    assert _deduped == {x.split(":")[0] for x in stripped_m
                        if x.endswith(":mirror_dedup")}
    # 剔除链逐条命中（K3 判据生效的直接证据）
    assert f"SI-BENCH-{key}:benchmark_source" in stripped_m
    assert f"SI-MIRR-{key}:mirror_dedup" in stripped_m
    assert f"SI-BONLY-{key}:benchmark_source" in stripped_b



def test_excluded_source_type_zeroes_k3_eligible_but_not_valid():
    """判据链扩展位（会审 2026-09-27 要求）：除 benchmark / mirror_dedup
    之外，`excluded_source_type`（fixture/synthetic/commentary 冒充）同样
    在 k3_eligible_* 的归零路径上——补一条样本，防止将来只钉住两道剔除
    的测试被「判据链已扩展」悄悄骗过。

    构造：登记为 `fixture`（默认排除集内）、段 role 非 benchmark、文本版本
    合格、status=verified ⇒ 观察口径 valid=1，K3 可用口径 0（且 stripped
    以 `excluded_source_type:fixture` 命中）。"""
    _n[0] += 1
    key = f"ec-fx-{_n[0]}"
    db.init_db()
    with db.session() as s:
        w = Work(title=f"t-ec-fx-{key}", source="test:ec")
        s.add(w)
        s.flush()
        import register_work_sources as REG

        sha, _ = REG._work_sha256(s, w.id)
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type="fixture", text_version="corpus-v1",
                         text_sha256=sha, purpose_basis="测试夹具",
                         identity_purposes=["research"],
                         license_purposes=[], license_basis="seed",
                         metadata_status="verified", metadata_basis="seed"))
        seg = Segment(work_id=w.id, ordinal=0, text=TEXT, text_clean=TEXT,
                      role=None, n_chars=len(TEXT), n_sentences=1,
                      integrity='{"src_ok": true}')
        s.add(seg)
        s.flush()
        from registry_anchor import refresh as _refresh
        _refresh(s, w.id)
        st = ExpressionStrategyV2(
            strategy_key=f"{key}-fx", abstract_operation=f"操作-{key}-fx",
            effect_hypothesis=f"假设-{key}-fx", status="hypothesis",
            scope="WORK", scope_ids=[w.id])
        s.add(st)
        s.flush()
        ev = TEXT[0:8]
        s.add(StrategyInstance(
            id=f"SI-FX-{key}", strategy_id=st.id, strategy_version=st.version,
            work_id=w.id, segment_id=seg.id, text_version="corpus-v1",
            span_start=0, span_end=8, evidence_text=ev,
            evidence_sha256=hashlib.sha256(ev.encode()).hexdigest(),
            observed_content="x", extractor_model="fx", status="verified"))
        s.commit()
        st_id = st.id
    rep = ssr.run(apply=False)
    row = [r for r in rep["rows"] if r["strategy_key"] == f"{key}-fx"][0]
    assert row["valid"] == 1, "观察口径照旧计 verified 实例"
    assert row["k3_eligible_instances"] == 0
    assert row["k3_eligible_root_works"] == 0
    with db.session() as s:
        refs, ev_count, stripped = kq._evidence_for(s, st_id, {})
    assert refs == [] and ev_count == 0
    assert f"SI-FX-{key}:excluded_source_type:fixture" in stripped
    assert row["k3_eligible_instances"] == len(refs)

def test_existing_field_values_unchanged():
    """既有字段值不变（防悄悄改口径）：既有列与 extras 键按夹具真值钉死；
    --apply 落库后既有列不动、新键只追加进 extras；指纹确定性。"""
    key, ids = _seed()
    # 写库护栏（会审 2026-09-27 要求）：本用例是全文件唯一 apply=True 的
    # 不可逆写路径，必须在断言前先证明目标库是 conftest 的临时 SQLite，
    # 绝不可能是真库 data/language_genome.db。
    assert config.DATABASE_URL.startswith("sqlite:///"),         f"apply=True 只许打临时 SQLite，实为 {config.DATABASE_URL}"
    assert "lg_test_" in config.DATABASE_URL,         f"目标库不是 conftest 临时库：{config.DATABASE_URL}"
    rep = ssr.run(apply=False)
    mine, bonly = _rows(rep, key)
    # 观察证据口径（含基准段实例）——与加档前逐字同式
    assert mine["attempts"] == 3 and mine["valid"] == 3
    assert mine["rejected"] == 0 and mine["missing"] == 0
    assert mine["counter_examples"] == 0
    assert mine["unique_source_intervals"] == 2, \
        "去重 (根, evidence_sha256)：镜像与根同段同文只计一次"
    assert mine["root_works"] == 2
    assert mine["known_authors"] == 0 and mine["genres"] == 0
    assert mine["by_root_work"] == {ids["plain"]: 2, ids["bench"]: 1}
    assert mine["usable_evidence"] == 1 and mine["benchmark_stripped"] == 1
    assert bonly["unique_source_intervals"] == 1 and bonly["root_works"] == 1
    assert bonly["by_root_work"] == {ids["bench2"]: 1}
    # 落库路径：既有列 + 既有 extras 键值不变，新键旁路追加
    ssr.run(apply=True)
    with db.session() as s:
        row = s.query(StrategyStats).filter_by(
            strategy_id=ids["st_mixed"]).one()
        assert row.valid == 3 and row.attempts == 3
        assert row.unique_source_intervals == 2 and row.root_works == 2
        assert row.extras["usable_evidence"] == 1
        assert row.extras["benchmark_stripped"] == 1
        assert row.extras["by_root_work"] == {ids["plain"]: 2,
                                               ids["bench"]: 1}
        assert row.extras["k3_eligible_instances"] == 1
        assert row.extras["k3_eligible_root_works"] == 1
    rep2 = ssr.run(apply=False)
    mine2, _ = _rows(rep2, key)
    assert mine2["data_fingerprint"] == mine["data_fingerprint"], \
        "同库同知识必同指纹（重建幂等，既有口径未动）"


def test_rebuild_docstring_declares_k3_eligible_criteria():
    """口径声明如实：docstring 必须报出两个新字段与「不本地复刻判据」纪律
    （读表方第一时间看到的就是这里）。"""
    doc = (ROOT / "scripts" / "strategy_stats_rebuild.py").read_text(
        encoding="utf-8")
    assert "k3_eligible_instances" in doc
    assert "k3_eligible_root_works" in doc
    assert "不本地复刻" in doc
    assert "分档" in doc
