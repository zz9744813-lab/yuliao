"""审计 P1 回归：冻结包指纹必须覆盖影响「选知识/包内容」的全部字段（2026-09-23）。

旧指纹只认策略 (id,version,strategy_key) + work_sources 四列——原地改正文/
状态/条件/证据而 id、version 不变时指纹不动，过期包被静默复用。本文件钉死
新口径：内容/条件/证据/用途许可任一变更→指纹必变；created_at 变更→指纹不变
（幂等）；字段集合防漂移（机械枚举模型列）；旧算法值 ≠ 新算法值（历史包必失效）。

自包含：临时 sqlite（conftest 已建）+ 本文件自建 FP-* 行、用完即清；
零真实库、零网络、零真实模型调用。
"""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, knowledge_query as kq  # noqa: E402
from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,  # noqa: E402
                        StrategyInstance, Work, WorkSource)  # noqa: E402

_TXT = "他把没写完的信拿起又放下，终究没有封口。"


def _fp_old_impl(s) -> str:
    """任务内联的「旧口径」算法（历史包指纹）——只用于证新≠旧，不参与生产。"""
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


def _cleanup():
    with db.session() as c:
        c.query(StrategyInstance).filter(
            StrategyInstance.id.like("FP%")).delete(synchronize_session=False)
        c.query(StrategyCondition).filter(
            StrategyCondition.id.like("FP%")).delete(synchronize_session=False)
        c.query(ExpressionStrategyV2).filter(
            ExpressionStrategyV2.id.like("FP%")).delete(synchronize_session=False)
        c.query(WorkSource).filter(
            WorkSource.id.like("FP%")).delete(synchronize_session=False)
        c.query(Segment).filter(Segment.id.like("FP%")).delete(
            synchronize_session=False)
        c.query(Work).filter(Work.id.like("FP%")).delete(
            synchronize_session=False)
        c.commit()


@pytest.fixture()
def s():
    """每个测试一份干净的 FP 图（策略+条件+实例+登记表+作品/段），用完即清。"""
    db.init_db()
    _cleanup()                       # 防邻测残留
    with db.session() as sess:
        sess.add(Work(id="FPWK-1", title="指纹书", source="file:fp"))
        sess.flush()                 # Work 先落——FK 序（同 knowledge_seed._work）
        sess.add(Segment(id="FPSEG-1", work_id="FPWK-1", ordinal=0, text=_TXT,
                         n_sentences=1, n_chars=len(_TXT)))
        sess.add(WorkSource(
            id="FPWS-1", work_id="FPWK-1", canonical_work_id="FPWK-1",
            author_id=None, genre_ids=[], source_type="human_fiction",
            text_version="corpus-v1", text_sha256="a" * 64,
            purpose_basis="fp", identity_purposes=["research"],
            license_purposes=[], license_basis=None,
            metadata_status="verified", metadata_basis="fp"))
        sess.add(ExpressionStrategyV2(
            id="FPES-1", strategy_key="FP-K", version=1,
            abstract_operation="抽象操作甲", invariants=["不改动事实"],
            effect_hypothesis="假设甲", failure_modes=["过度使用"],
            status="verified", source="fp", scope="WORK", scope_ids=["FPWK-1"],
            scope_basis="fp", observation_status="observed",
            effect_status="pilot_verified"))
        sess.add(StrategyCondition(
            id="FPSC-1", strategy_id="FPES-1", strategy_version=1,
            kind="good_when", dimension="节奏", operator="eq",
            value={"v": "短句"}, required=False, predicate_state="unknown",
            evidence_refs=[], version=1))
        sess.add(StrategyInstance(
            id="FPSI-1", strategy_id="FPES-1", strategy_version=1,
            work_id="FPWK-1", segment_id="FPSEG-1", frame_id=None,
            text_version="corpus-v1", span_start=0, span_end=5,
            evidence_text=_TXT[:5], evidence_sha256="0" * 64,
            conditions_observed={}, observed_content="种子观察",
            extractor_model="fp", reviewer_version="", status="verified"))
        sess.commit()
        yield sess
    _cleanup()


# ── 确定性 ───────────────────────────────────────────────────────────
def test_deterministic_same_db_twice(s):
    assert kq.fingerprint_knowledge(s) == kq.fingerprint_knowledge(s)


# ── 内容变更必变（同 id/version 不变）──────────────────────────────────
def test_abstract_operation_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(ExpressionStrategyV2, "FPES-1").abstract_operation = "改了抽象操作"
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_invariants_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(ExpressionStrategyV2, "FPES-1").invariants = ["换了不变项"]
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_effect_hypothesis_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(ExpressionStrategyV2, "FPES-1").effect_hypothesis = "换了效果假设"
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_strategy_status_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(ExpressionStrategyV2, "FPES-1").status = "retired"
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_strategy_observation_status_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(ExpressionStrategyV2, "FPES-1").observation_status = "hypothesis"
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


# ── 条件变更 ─────────────────────────────────────────────────────────
def test_condition_added(s):
    before = kq.fingerprint_knowledge(s)
    s.add(StrategyCondition(
        id="FPSC-2", strategy_id="FPES-1", strategy_version=1,
        kind="bad_when", dimension="场景", operator="eq", value={"v": "静谧"},
        required=False, predicate_state="unknown", evidence_refs=[], version=1))
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_condition_modified(s):
    before = kq.fingerprint_knowledge(s)
    s.get(StrategyCondition, "FPSC-1").value = {"v": "长句"}
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


# ── 证据变更 ─────────────────────────────────────────────────────────
def test_instance_status_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(StrategyInstance, "FPSI-1").status = "rejected"
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_evidence_text_only_anchor_untouched(s):
    """坏行：只改存证正文、evidence_sha256 锚**故意不动**——指纹仍必变。"""
    before = kq.fingerprint_knowledge(s)
    ins = s.get(StrategyInstance, "FPSI-1")
    ins.evidence_text = "完全不同的正文内容，锚没跟着改。"
    assert ins.evidence_sha256 == "0" * 64      # 锚原样
    s.flush()
    after = kq.fingerprint_knowledge(s)
    assert after != before


# ── 用途 / 许可变更（work_sources）────────────────────────────────────
def test_work_source_license_purposes_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(WorkSource, "FPWS-1").license_purposes = ["training_source"]
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_work_source_identity_purposes_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(WorkSource, "FPWS-1").identity_purposes = ["test_contract"]
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


def test_work_source_text_sha_change(s):
    before = kq.fingerprint_knowledge(s)
    s.get(WorkSource, "FPWS-1").text_sha256 = "b" * 64
    s.flush()
    assert kq.fingerprint_knowledge(s) != before


# ── created_at 排除：只动审计戳指纹不变（幂等护栏）─────────────────────
def test_created_at_change_is_ignored(s):
    before = kq.fingerprint_knowledge(s)
    s.get(ExpressionStrategyV2, "FPES-1").created_at = "2099-01-01T00:00:00Z"
    s.get(WorkSource, "FPWS-1").created_at = "2099-01-01T00:00:00Z"
    s.flush()
    assert kq.fingerprint_knowledge(s) == before


# ── 字段覆盖防漂移：机械枚举模型列，未纳入未排除即红 ────────────────────
_MODELS = (ExpressionStrategyV2, StrategyCondition, StrategyInstance, WorkSource)


def test_field_coverage_no_drift():
    for model in _MODELS:
        table = model.__tablename__
        declared = set(kq.FINGERPRINT_TABLE_FIELDS[table])
        for col in model.__table__.columns:
            assert (col.name in declared
                    or col.name in kq.FINGERPRINT_EXCLUDED_FIELDS), \
                f"{table}.{col.name} 既未纳入指纹也未显式排除（字段漂移）"


def test_excluded_fields_not_declared():
    for model in _MODELS:
        table = model.__tablename__
        declared = set(kq.FINGERPRINT_TABLE_FIELDS[table])
        assert not (declared & set(kq.FINGERPRINT_EXCLUDED_FIELDS)), \
            f"{table} 同时声明纳入又排除同一列"


# ── 旧指纹失效证明：历史包不会被静默复用 ──────────────────────────────
def test_old_fingerprint_value_differs_from_new(s):
    old = _fp_old_impl(s)
    new = kq.fingerprint_knowledge(s)
    assert old != new, "扩容后新算法值须与旧口径不同，否则历史包被误判未过期"
