"""work_sources.source_type 列宽与合法取值一致性回归（审计口径风险收口）。

问题（2026-09-25）：`app/models.py` 把 source_type 定成 String(20)，但被
设计为合法取值的非基准试点类型是 `production_nonbenchmark_k2v2`（长 28，
`scripts/k2_extract_backfill.py::NONBENCHMARK_SOURCE_TYPE_PREFIX` = 24 +
`k2v2` = 28）。SQLite 不校验 String 长度所以「能跑」，换 Postgres/MySQL
立刻 `value too long`。本文件钉死：

① 列宽 ≥ 28（且 ≥ 实际最长设计取值长度）；
② 28 字符的 `production_nonbenchmark_k2v2` 经模型层真插一行 flush/commit
   成功——证明夹具值在模型层合法（tests/test_source_check_nonbench_scope.py
   :136/:362 与 tests/test_k2_extract_nonbenchmark.py :133/:172/:237
   已经在用它）；
③ 登记写入口（scripts/register_work_sources.py 的 `_check_source_type_width`）
   对超宽值**响亮报错**：经 REG.register() 整链路验证 SystemExit 且
   退出码非 0，不许只断言字符串、不许静默截断/clamp；
④ 28 字符设计值本身必须能过写入口校验（宽度改了才不误伤试点源）。

纪律：全部离线（conftest 临时 sqlite + mock LLM），零网络、不碰真库、
不动排除集/查询侧语义（`excluded_source_types` 等一个字不动）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sqlalchemy import String

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import register_work_sources as REG        # noqa: E402
from app import db                         # noqa: E402
from app.models import Segment, Work, WorkSource  # noqa: E402

# 全仓设计取值（K1-A 四型 + K2 试点前缀族实例）——
# 只要还有取值超过当前列宽，① 就必须红
K2V2_VALUE = "production_nonbenchmark_k2v2"
DESIGNED_VALUES = ("fixture", "synthetic", "commentary",
                   "human_fiction", K2V2_VALUE)
LONGEST_DESIGNED = max(len(v) for v in DESIGNED_VALUES)


def _col_width() -> int:
    col_type = WorkSource.__table__.c.source_type.type
    assert isinstance(col_type, String), \
        "source_type 必须仍是定宽 String——改成 Text 会丢列宽约束"
    return col_type.length


# ── ① 列宽 ≥ 28 且 ≥ 最长设计取值 ──────────────────────────────────

def test_source_type_column_fits_longest_designed_value():
    """列宽必须装下全部设计取值：K2 试点值 28 是硬下界。"""
    width = _col_width()
    assert width is not None and width >= 28, \
        f"source_type 列宽 {width} 装不下 28 字符的 {K2V2_VALUE}"
    assert len(K2V2_VALUE) == 28, "production_nonbenchmark_k2v2 长度锚定 28"
    assert width >= LONGEST_DESIGNED, \
        f"列宽 {width} < 最长设计取值 {LONGEST_DESIGNED}"


def test_28_char_value_is_the_longest_currently_designed():
    """把「最长设计取值」写死为 28——任何新类型超过它必须回来改列宽。"""
    assert LONGEST_DESIGNED == 28


# ── ② 28 字符设计值在模型层真插一行成功 ────────────────────────────

def test_28_char_designed_value_inserts_and_commits():
    """夹具值 `production_nonbenchmark_k2v2` 经模型层真插一行必须成功：
    这是 test_source_check_nonbench_scope/:136,:362 与
    test_k2_extract_nonbenchmark/:133,:172,:237 在用的同一个值——模型层
    装不下，批量的夹具/试点登记会在换库后全部炸。"""
    db.init_db()
    wid = None
    try:
        with db.session() as s:
            w = Work(title="t-width-k2v2", source="test:width:insert")
            s.add(w)
            s.flush()
            wid = w.id
            s.add(WorkSource(
                work_id=w.id, canonical_work_id=w.id,
                source_type=K2V2_VALUE, text_version="k2v2-pilot",
                text_sha256=None, purpose_basis="测试：只验模型层列宽",
                identity_purposes=["research"], license_purposes=[],
                license_basis="test", metadata_status="verified",
                metadata_basis="test"))
            s.commit()
        with db.session() as s:
            row = s.query(WorkSource).filter_by(work_id=wid).one()
            assert row.source_type == K2V2_VALUE, "28 字符值必须原样落库"
    finally:
        if wid is not None:
            with db.session() as s:
                s.query(WorkSource).filter_by(work_id=wid).delete()
                s.query(Work).filter_by(id=wid).delete()
                s.commit()


# ── ④ 写入口对 28 字符设计值放行 ──────────────────────────────────

def test_write_entry_accepts_28_char_designed_value():
    """列宽改到 64 后，28 字符设计值必须能过写入口——不许误伤试点源。"""
    REG._check_source_type_width(K2V2_VALUE)          # 不抛即过


# ── ③ 写入口对超宽值响亮报错（整链路，退出码非 0）──────────────────

def test_validator_rejects_overwide_string():
    """超出列宽（65 > 64）的值必须 SystemExit 且退出码非 0——
    只断言异常不是只断言字符串。"""
    with pytest.raises(SystemExit) as e:
        REG._check_source_type_width("x" * 65)
    assert e.value.code != 0, "超宽 source_type 必须非零退出"


def test_register_rejects_overwide_source_type(monkeypatch):
    """整链路：把列宽临时压到 5，真走 REG.register() 登记一条
    source_type='fixture'（7 字符）——写入口必须当场 SystemExit 非 0，
    证明超宽值在登记路径被硬拦（不是只有独立函数拦）。"""
    db.init_db()
    monkeypatch.setattr(REG, "_AUTHORS", {})          # 词表零污染
    monkeypatch.setattr(REG, "_GENRES", set())
    monkeypatch.setattr(REG, "_ROOT_META", {})
    # 把模型声明宽度临时压小，令既有登记值「fixture」变超宽——
    # 语义 = 出现任何超过声明列宽的值都必须被登记写入口拒绝
    monkeypatch.setattr(WorkSource.__table__.c.source_type.type, "length", 5)
    wid = None
    try:
        with db.session() as s:
            w = Work(title="t-width-reject", source="inbox:fixture_width.txt")
            s.add(w)
            s.flush()
            wid = w.id
            s.add(Segment(work_id=w.id, ordinal=0, text="x",
                          text_clean="x", n_sentences=1, n_chars=1,
                          integrity="{}"))
            s.commit()
        with db.session() as s:
            assert s.query(WorkSource).filter_by(work_id=wid).first() is None
        with pytest.raises(SystemExit) as e:
            REG.register(only={wid})
        assert e.value.code != 0, "超宽 source_type 必须非零退出"
        with db.session() as s:
            assert s.query(WorkSource).filter_by(work_id=wid).first() is None, \
                "被拒的超宽登记不许留下半截行"
    finally:
        if wid is not None:
            with db.session() as s:
                s.query(Segment).filter_by(work_id=wid).delete()
                s.query(WorkSource).filter_by(work_id=wid).delete()
                s.query(Work).filter_by(id=wid).delete()
                s.commit()