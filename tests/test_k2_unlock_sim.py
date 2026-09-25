"""`scripts/k2_unlock_sim.py` 的回归钉（离线、只读、零网关、零生产库）。

全部跑在 **pytest 临时目录里的独立 SQLite 夹具库** 上（自造 engine，不走
app/db 的共享测试库，也不设 WAL）：夹具写完后 `engine.dispose()`，仿真再
以 `mode=ro` + `PRAGMA query_only=1` 只读打开——所以「仿真只读」这件事在
测试里是真的被验证了（跑完还比对库文件字节哈希）。

钉住的事（逐条对应派工要求）：
1. **三层闸的先后与必要性**：S0（现状）/S1（只开层1 status）/S2（只开层3
   scope）全部 `selected=0`；S3（层1+层3 同开）`selected>0`；
2. **仿真只读性质**：源码扫描零写路径 + 扫描器反向钉（合成可写片段必被
   判红，防止扫描器是死门）+ 只读会话调用写入口即抛错 + 跑完库文件字节
   不变 + `query_only` 回读为 1（并对照可写连接回读为 0，证明这道闸有效）；
3. **GLOBAL 只预留不授予**：`excluded_scope_global_reserved`；
4. **缺 `scope_basis` 被形式闸拒绝**：`grant_scope_valid` 判 False，并如实
   记录「K3 查询层不查依据」这一差异（拍板必须把依据写进 `scope_basis`）；
5. **基准段硬剔**：`role='benchmark'` 段的实例在任何场景都不回流，只有基准
   证据的策略在 S3 仍 `excluded_no_evidence`；
6. **id 口径**：真库 work_id 是 `WK-`+12hex，文档短写（前缀）由脚本解析并
   如实报差异。
"""
from __future__ import annotations

import hashlib
import importlib.util as importlib_util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import create_engine, event, func                     # noqa: E402
from sqlalchemy.orm import sessionmaker                               # noqa: E402

from app import knowledge as K                                        # noqa: E402
from app import knowledge_query as KQ                                 # noqa: E402
from app.db import Base                                               # noqa: E402
from app.models import (ExpressionStrategyV2, Segment, StrategyInstance,  # noqa: E402
                        Work, WorkSource)

_spec = importlib_util.spec_from_file_location(
    "k2_unlock_sim", ROOT / "scripts" / "k2_unlock_sim.py")
sim = importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def fixture_db(tmp_path):
    """1 登记世界 + 2 策略 + 3 实例（其中 2 条落在 benchmark 段）。

    夹具库的**真值**刻意做成真库现状的形状：策略全 `hypothesis` +
    `scope=UNCERTAIN`，实例全 `verified`——于是 S0 天然是「零改动的现状」，
    S1/S2/S3 的差别只在仿真内存里模拟，绝不写库。
    返回 (db_path, book_id, key_a, key_b)。"""
    db_path = tmp_path / "sim_fixture.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    import app.models  # noqa: F401  确保全部表已注册进 Base.metadata
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine)
    with sm() as s:
        w = Work(id="WK-abcd1234ef56", title="夹具书", source="test:k2_unlock_sim")
        s.add(w)
        s.flush()
        seg_train = Segment(work_id=w.id, ordinal=0, text=TEXT, text_clean=TEXT,
                            n_sentences=2, n_chars=len(TEXT), role="train")
        seg_bench = Segment(work_id=w.id, ordinal=1, text=TEXT, text_clean=TEXT,
                            n_sentences=2, n_chars=len(TEXT), role="benchmark")
        s.add_all([seg_train, seg_bench])
        s.flush()
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type="human_fiction", text_version="corpus-v1",
                         text_sha256=_sha256_of(TEXT),
                         purpose_basis="测试夹具：只验闸口径，非生产语料",
                         identity_purposes=["research"], license_purposes=[],
                         license_basis=None, allowed_purposes=[],
                         metadata_status="verified", metadata_basis="fixture"))
        a = ExpressionStrategyV2(id="ESV2-aaaa00000001", strategy_key="esv-sim-a",
                                 abstract_operation="克制沉默地收尾",
                                 effect_hypothesis="留白提高余味",
                                 status="hypothesis", observation_status="observed",
                                 effect_status="untested", scope="UNCERTAIN",
                                 scope_ids=[], scope_basis="")
        b = ExpressionStrategyV2(id="ESV2-bbbb00000001", strategy_key="esv-sim-b",
                                 abstract_operation="以物代情",
                                 effect_hypothesis="物件承载情绪",
                                 status="hypothesis", observation_status="observed",
                                 effect_status="untested", scope="UNCERTAIN",
                                 scope_ids=[], scope_basis="")
        s.add_all([a, b])
        s.flush()
        s.add_all([
            # A：一条非基准证据（S3 唯一能出包的证据）+ 一条基准段证据（硬剔）
            _inst(a, w.id, seg_train.id, 0, 8),
            _inst(a, w.id, seg_bench.id, 0, 8),
            # B：只有基准段证据 ⇒ 任何场景都 excluded_no_evidence
            _inst(b, w.id, seg_bench.id, 0, 8)])
        book_id = w.id                       # commit 前取，避免会话关闭后
        keys = (a.strategy_key, b.strategy_key)  # DetachedInstanceError
        s.commit()
    engine.dispose()                       # 关掉可写连接，之后只读打开
    return db_path, book_id, keys[0], keys[1]


def _sha256_of(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _inst(strategy, work_id, seg_id, start, end) -> StrategyInstance:
    ev = TEXT[start:end]
    return StrategyInstance(strategy_id=strategy.id,
                            strategy_version=strategy.version,
                            work_id=work_id, segment_id=seg_id,
                            text_version="corpus-v1", span_start=start,
                            span_end=end, evidence_text=ev,
                            evidence_sha256=_sha256_of(ev),
                            conditions_observed={}, observed_content="夹具",
                            extractor_model="fixture", reviewer_version="t",
                            status="verified")


# ── 1. 三层闸的先后与必要性 ────────────────────────────────────────────
def test_s0_s1_s2_selected_zero_and_s3_unlocks(fixture_db):
    db_path, book, key_a, key_b = fixture_db
    out = sim.simulate(db_path, books=[book])
    assert out["books_evaluated"] == [book]
    # 自校验门：S0 逐策略 selected=0（与 K3 真身口径一致）
    assert out["selfcheck_s0_matches_k3_truth"]["passed"] is True, \
        out["selfcheck_s0_matches_k3_truth"]
    sel = {sid: sc["selected_by_book"][book] for sid, sc in
           out["scenarios"].items()}
    assert sel["S0"] == 0, sel
    assert sel["S1"] == 0, sel          # 只开层1 ⇒ 层3 是真闸
    assert sel["S2"] == 0, sel          # 只开层3 ⇒ 层1 是真闸
    assert sel["S3"] == 1, sel          # 两层同开 ⇒ A 出包，B 仍无证据
    assert out["scenarios"]["S3"]["per_book"][book]["k3_status"] == "matched"
    assert out["scenarios"]["S3"]["per_book"][book]["selected_strategy_keys"] \
        == [key_a]
    # 层1/层3 各自「开了几条」也被分开报出（证明单开一层不够）
    assert out["scenarios"]["S1"]["layer1_open_by_book"][book] == 2
    assert out["scenarios"]["S1"]["layer3_open_by_book"][book] == 0
    assert out["scenarios"]["S2"]["layer1_open_by_book"][book] == 0
    assert out["scenarios"]["S2"]["layer3_open_by_book"][book] == 2


def test_s1_blocks_on_scope_and_s2_blocks_on_status(fixture_db):
    db_path, book, key_a, key_b = fixture_db
    out = sim.simulate(db_path, books=[book])
    s1 = out["scenarios"]["S1"]["per_book"][book]
    v1 = {e["strategy_key"]: e["pipeline_verdict"] for e in s1["per_strategy"]}
    assert v1[key_a] == "excluded_scope_uncertain", v1   # 有证据也卡在层3
    assert v1[key_b] == "excluded_no_evidence", v1       # 只有基准证据
    s2 = out["scenarios"]["S2"]["per_book"][book]
    v2 = {e["strategy_key"]: e["pipeline_verdict"] for e in s2["per_strategy"]}
    assert v2 == {key_a: "status_not_eligible", key_b: "status_not_eligible"}
    # S2 里层3 判据本身是「过」的——被层1 预筛掉，压根没进管道
    assert all(e["layer3_scope_verdict"] == "pass" for e in s2["per_strategy"])
    assert s2["budget"]["considered"] == 0


def test_s3_reports_instance_and_unique_interval_counts(fixture_db):
    db_path, book, key_a, key_b = fixture_db
    out = sim.simulate(db_path, books=[book])
    rows = {e["strategy_key"]: e for e in
            out["scenarios"]["S3"]["per_book"][book]["per_strategy"]}
    assert rows[key_a]["layer2_eligible_instances"] == 1
    assert rows[key_a]["layer2_unique_intervals"] == 1
    assert rows[key_a]["layer2_root_works"] == [book]
    assert rows[key_a]["layer2_benchmark_stripped"] == 1
    assert rows[key_a]["in_package"] is True
    assert rows[key_b]["layer2_eligible_instances"] == 0
    assert rows[key_b]["in_package"] is False
    # 「唯一(根作品,span) 区间数」直接来自真判据 _evidence_for 的第三个返回值
    ev = out["unlock_summary"]["evidence_available_by_strategy"]["ESV2-aaaa00000001"]
    assert ev["eligible_instances"] == 1 and ev["unique_root_span_intervals"] == 1


# ── 2. 只读纪律 ────────────────────────────────────────────────────────
def test_source_has_no_write_path(fixture_db):
    src = Path(sim.__file__).read_text(encoding="utf-8")
    assert sim.write_path_scan(src) == []


def test_write_path_scanner_is_not_a_dead_gate():
    """反向钉：把可写写法喂给扫描器，必须逐条判红（否则上面的绿没有意义）。"""
    dirty = "\n".join([
        "with db.session() as s:",
        "    s.commit()",
        "    s.flush()",
        "    s.add(row)",
        "    c.exec_driver_sql('DELETE FROM works')",
        "con = sqlite3.connect('file:x.db?mode=rw', uri=True)",
        "c.execute('INSERT INTO works (id) VALUES (1)')"])
    hits = sim.write_path_scan(dirty)
    joined = "\n".join(hits).lower()      # DML 命中回显原大小写（DELETE/INSERT）
    for needle in ("commit", "flush", "add", "delete", "insert", "mode=rw"):
        assert needle in joined, hits
    assert len(hits) >= 6, hits
    # 干净片段不误报（扫描器不是「全红」糊弄）
    assert sim.write_path_scan(
        "s.query(Work).all()\nprint('hi', flush=True)\n"
        "con.execute('PRAGMA query_only=ON')") == []


def test_readonly_session_refuses_every_write_entry(fixture_db):
    db_path, book, _a, _b = fixture_db
    engine, info = sim.build_readonly_engine(db_path)
    assert info["query_only"] == 1 and info["mode_ro"] is True
    assert "mode=ro" in info["dbapi_uri"]
    s = sim.session_for(engine)
    try:
        for call in (lambda: s.commit(), lambda: s.flush(),
                     lambda: s.add(Work(id="WK-x", title="t", source="s")),
                     lambda: s.delete(Work(id="WK-y", title="t", source="s")),
                     lambda: s.merge(Work(id="WK-z", title="t", source="s"))):
            with pytest.raises(sim.ReadOnlyViolation):
                call()
        # 只读会话照常能查
        assert s.query(Work).filter_by(id=book).first() is not None
    finally:
        s.close()
        engine.dispose()


def test_readonly_guard_would_reject_a_writable_connection(tmp_path, fixture_db):
    """同库可写连接回读 query_only=0 ⇒ build_readonly_engine 的这道断言有效。"""
    db_path, _book, _a, _b = fixture_db
    writable = create_engine(f"sqlite:///{db_path.as_posix()}")
    with writable.connect() as c:
        assert int(c.exec_driver_sql("PRAGMA query_only").scalar() or 0) == 0
    writable.dispose()
    engine, info = sim.build_readonly_engine(db_path)
    assert info["query_only"] == 1
    engine.dispose()
    with pytest.raises(sim.SimError):
        sim.build_readonly_engine(tmp_path / "nope.db")


def test_simulate_leaves_the_database_bytes_untouched(fixture_db):
    db_path, book, _a, _b = fixture_db
    before = _sha256(db_path)
    out = sim.simulate(db_path, books=[book])
    assert out["readonly"]["scan_hits"] == []
    assert _sha256(db_path) == before, "仿真写了库——违反只读纪律"


# ── 3. GLOBAL 只预留不授予 ─────────────────────────────────────────────
def test_global_scope_stays_reserved(fixture_db):
    db_path, book, key_a, _b = fixture_db
    out = sim.simulate(db_path, books=[book])
    run = out["scenarios"]["S4_global"]["per_book"][book]
    assert run["n_selected"] == 0
    row = {e["strategy_key"]: e for e in run["per_strategy"]}[key_a]
    assert row["layer3_scope_verdict"] == "excluded_scope_global_reserved"
    assert row["pipeline_verdict"] == "excluded_scope_global_reserved"
    assert out["scenarios"]["S4_global"]["selected_by_book"][book] == 0
    # 同源核对：真函数与真常量（不是脚本自己另立一套名字）
    assert "GLOBAL" in K.SCOPES
    engine, _info = sim.build_readonly_engine(db_path)
    s = sim.session_for(engine)
    try:
        st = s.query(ExpressionStrategyV2).filter_by(strategy_key=key_a).one()
        st.scope, st.scope_ids, st.scope_basis = "GLOBAL", [book], "占位依据"
        assert KQ._scope_matches(s, st, sim.sim_policy(book)) == \
            "excluded_scope_global_reserved"
        s.expire_all()
    finally:
        s.close()
        engine.dispose()


# ── 4. 缺 scope_basis 被形式闸拒绝 ─────────────────────────────────────
def test_missing_scope_basis_fails_formal_gate(fixture_db):
    db_path, book, key_a, _b = fixture_db
    # 真判据：形式闸（app/knowledge.grant_scope_valid）
    assert K.grant_scope_valid("WORK", [book], sim.SIM_SCOPE_BASIS) is True
    assert K.grant_scope_valid("WORK", [book], "   ") is False
    assert K.grant_scope_valid("WORK", [], "有依据但没范围") is False
    assert K.grant_scope_valid("UNCERTAIN", [], "") is True
    out = sim.simulate(db_path, books=[book])
    nb = out["scenarios"]["S3_no_basis"]["per_book"][book]
    row = {e["strategy_key"]: e for e in nb["per_strategy"]}[key_a]
    assert row["simulated_scope_basis_len"] == 0
    assert row["formal_scope_valid"] is False
    # 如实记录差异：查询层不看依据 ⇒ 若只改 status/scope 不写依据，K3 会照收。
    # 这条不是「可以通过」，而是拍板必须把依据落进 scope_basis 的理由。
    assert row["in_package"] is True
    s3 = out["scenarios"]["S3"]["per_book"][book]
    s3_row = {e["strategy_key"]: e for e in s3["per_strategy"]}[key_a]
    assert s3_row["simulated_scope_basis_len"] > 0
    assert s3_row["formal_scope_valid"] is True
    assert any("scope_basis" in c for c in
               out["unlock_summary"]["caveats"])


# ── 5. 基准段证据被硬剔，且不因升格回流 ────────────────────────────────
def test_benchmark_instances_are_stripped_in_every_scenario(fixture_db):
    db_path, book, key_a, key_b = fixture_db
    out = sim.simulate(db_path, books=[book])
    assert out["census"]["benchmark_instances_stripped_by_k3"] == 2
    for sid, sc in out["scenarios"].items():
        rows = {e["strategy_key"]: e for e in sc["per_book"][book]["per_strategy"]}
        assert rows[key_a]["layer2_benchmark_stripped"] == 1, sid
        assert rows[key_b]["layer2_benchmark_stripped"] == 1, sid
        # 只有基准证据的 B 在任何场景都进不了包
        assert rows[key_b]["in_package"] is False, sid
    assert out["scenarios"]["S3"]["per_book"][book]["selected_strategy_keys"] \
        == [key_a]
    # 逐策略 role 分布也对得上：夹具里 A 挂 train+benchmark、B 只挂 benchmark
    per = {r["strategy_key"]: r for r in
           out["census"]["strategies"]["per_strategy"]}
    assert per[key_a]["roles"] == {"train": 1, "benchmark": 1}
    assert per[key_b]["roles"] == {"benchmark": 1}
    assert per[key_a]["work_ids"] == {book: 2}
    assert out["census"]["strategies"]["instances_by_segment_role"] == {
        "train": 1, "benchmark": 2}


# ── 6. work_id 口径 / 自发现 / 输出形状 ────────────────────────────────
def test_truncated_book_id_is_resolved_and_reported(fixture_db):
    db_path, book, _a, _b = fixture_db
    assert len(book) == 15                       # WK- + 12hex（app/ids.py）
    truncated = book[:12]
    out = sim.simulate(db_path, books=[truncated])
    req = out["census"]["worlds_requested"][0]
    assert req["exact_match"] is False and req["looks_truncated_vs_schema"] is True
    assert req["prefix_matches"] == [book]
    assert out["books_evaluated"] == [book]
    assert out["census"]["discovered_nonbenchmark_books"] == [book]
    assert out["census"]["worlds"][book]["registered"] is True
    assert out["census"]["worlds"][book]["source_type"] == "human_fiction"


def test_unregistered_world_still_gates_on_registry(fixture_db, tmp_path):
    """把登记行拿掉（改副本库，不碰夹具原库）⇒ 层2 以 no_registry 剔除。"""
    db_path, book, key_a, _b = fixture_db
    clone = tmp_path / "unregistered.db"
    clone.write_bytes(db_path.read_bytes())
    engine = create_engine(f"sqlite:///{clone.as_posix()}")
    with sessionmaker(bind=engine)() as s:
        row = s.query(WorkSource).filter_by(work_id=book).one()
        s.delete(row)
        s.commit()
    engine.dispose()
    before = _sha256(clone)
    out = sim.simulate(clone, books=[book])
    assert _sha256(clone) == before
    row = {e["strategy_key"]: e for e in
           out["scenarios"]["S3"]["per_book"][book]["per_strategy"]}[key_a]
    assert row["layer2_no_registry_stripped"] == 2
    assert row["pipeline_verdict"] == "excluded_no_evidence"
    assert out["scenarios"]["S3"]["selected_by_book"][book] == 0


def test_output_is_json_and_selfcheck_fields_present(fixture_db):
    db_path, book, _a, _b = fixture_db
    out = sim.simulate(db_path, books=[book])
    text = json.dumps(out, ensure_ascii=False)
    assert '"S3"' in text and "excluded_scope" in text
    for key in ("readonly", "census", "scenarios", "unlock_summary",
                "selfcheck_s0_matches_k3_truth"):
        assert key in out, key
    # 指纹覆盖 status/scope/scope_ids/scope_basis（见 FINGERPRINT_TABLE_FIELDS），
    # ⇒ 六个场景的 snapshot_fingerprint 必两两不同：这同时反证「内存里的模拟值
    # 真的进了查询层」，不是脚本自己贴的报告字段。
    fps = {sid: sc["per_book"][book]["snapshot_fingerprint"]
           for sid, sc in out["scenarios"].items()}
    assert all(v and len(v) == 64 for v in fps.values()), fps
    assert len(set(fps.values())) == len(fps), fps
    assert out["scenarios"]["S3"]["per_book"][book]["package_sha256"]
    assert out["unlock_summary"]["packages_nonempty_by_scenario"]["S0"] == 0
    assert out["unlock_summary"]["packages_nonempty_by_scenario"]["S3"] == 1


def test_no_model_calls_and_no_http_in_module_source():
    """零模型调用：仿真模块不引 gateway/HTTP/LLM 入口。"""
    src = Path(sim.__file__).read_text(encoding="utf-8")
    for banned in ("gateway", "requests", "httpx", "urllib.request",
                   "LLM_MODE", "invoke("):
        assert banned not in src, banned
