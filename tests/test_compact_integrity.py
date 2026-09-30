# -*- coding: utf-8 -*-
"""integrity 紧凑编码（2026-09-30 语料容量）——无损、读侧无感、旧值兼容。

为什么这些断言是必须的（不是形式主义）：
  * 编码把 195 字节 JSON 压成 9 字符，**任何一个 bit 错位都会静默改写语义**
    （eligible 反转 = 语料被自然度校准排除），所以逐字段往返必须打红。
  * 库里已有 5300 万段旧 JSON、且 K5 审计脚本走**裸 SQL + JSON1** 读 `$.src_ok`
    ⇒ 「带附加键的行必须原样保留」是硬约束，不是优化。
"""
from __future__ import annotations

import json

import pytest

from app import segment_integrity as si
from app.models import CompactIntegrity

BASE_KEYS = {"quote_integrity", "antecedent_integrity", "dialogue_integrity",
             "scene_boundary", "context_dependency", "truncation_risk",
             "dialogue_ratio", "eligible"}

# 真实语料里出现过的取值组合（含全 0、全 1、半值、风险项非零、ratio 非零）
SAMPLES = [
    "祠堂前的雪还没化，陈三爷来得最早。他把灯笼挂在门框上，手指冻得发僵，试了三次才把挂环扣进去。",
    "「你来做什么？」他问。",
    "但这件事，他一直记着。",
    "他笑了。她也笑了。然后两人都没说话。",
    "「」。",
    "这是一个特别长的句子，" * 12,
    "翌日清晨，城门大开。",
    "，接着他就走了",
    "」他抬头看了看天。",
    "……",
]


def _analyze_all():
    return [si.analyze(t, ordinal=i) for i, t in enumerate(SAMPLES)]


def test_analyze_key_set_unchanged():
    """基键集必须与编码器认的 8 键一致（口径漂移要立刻可见）。"""
    for d in _analyze_all():
        assert set(d) == BASE_KEYS


def test_roundtrip_every_sample_is_byte_identical():
    for d in _analyze_all():
        raw = json.dumps(d, ensure_ascii=False)
        packed = si.pack_raw(raw)
        assert packed is not None
        assert si.canonical_json(packed) == raw, (raw, packed)
        assert json.loads(si.canonical_json(packed)) == d


def test_packed_is_short():
    """基键行的紧凑串必须 ≤ 12 字符（9 是本设计值，留出前缀演进的余量）。"""
    for d in _analyze_all():
        packed = si.pack_raw(json.dumps(d, ensure_ascii=False))
        assert len(packed) <= 12, packed


@pytest.mark.parametrize("value", [0.0, 0.3, 0.4, 0.5, 0.7, 0.8, 0.9, 1.0])
def test_context_dependency_full_value_set_roundtrips(value):
    """连接词/裸代词/半句的组合值一个都不能掉进「回退原始 JSON」。"""
    d = si.analyze("他说。")
    d["context_dependency"] = value
    raw = json.dumps(d, ensure_ascii=False)
    assert si.is_packed(si.pack_raw(raw)), f"{value} 未进紧凑编码"


@pytest.mark.parametrize("value", [0.0, 0.2, 0.3, 0.5, 0.6, 0.7, 0.8, 1.0])
def test_truncation_risk_full_value_set_roundtrips(value):
    d = si.analyze("他说。")
    d["truncation_risk"] = value
    raw = json.dumps(d, ensure_ascii=False)
    assert si.is_packed(si.pack_raw(raw)), f"{value} 未进紧凑编码"


@pytest.mark.parametrize("ratio", [0.0, 0.001, 0.123, 0.5, 0.999, 1.0])
def test_dialogue_ratio_precision(ratio):
    d = si.analyze("他说。")
    d["dialogue_ratio"] = round(ratio, 3)
    raw = json.dumps(d, ensure_ascii=False)
    back = json.loads(si.canonical_json(si.pack_raw(raw)))
    assert back["dialogue_ratio"] == round(ratio, 3)


def test_extra_keys_kept_raw_for_sql_json1():
    """带 src_ok 等附加键的行**必须**原样保持合法 JSON —— 裸 SQL 的 json_type 靠它。"""
    d = si.analyze("他说。")
    d.update({"src_ok": True, "severity": "low", "defects": [], "checked_pv": "source_integrity_v1"})
    raw = json.dumps(d, ensure_ascii=False)
    packed = si.pack_raw(raw)
    assert packed == raw and not si.is_packed(packed)      # 逐字节不动，不加任何标记
    assert si.canonical_json(packed) == raw
    assert json.loads(packed)["src_ok"] is True            # 裸 SQL/裸 json.loads 也能读


def test_clean_pending_llm_flag_kept_raw():
    d = si.analyze("他说。")
    d["clean_pending_llm"] = True
    raw = json.dumps(d, ensure_ascii=False)
    assert si.canonical_json(si.pack_raw(raw)) == raw


def test_unknown_value_falls_back_losslessly():
    d = si.analyze("他说。")
    d["context_dependency"] = 0.123          # 口径改了：不在取值集内
    raw = json.dumps(d, ensure_ascii=False)
    packed = si.pack_raw(raw)
    assert not si.is_packed(packed)
    assert si.canonical_json(packed) == raw


def test_legacy_raw_json_untouched():
    """库里旧行的原始 JSON 直接喂进来必须逐字节不变（不重排、不重编码）。"""
    raw = '{"quote_integrity": 1.0, "antecedent_integrity": 1.0, "dialogue_integrity": 1.0, "scene_boundary": 0.0, "context_dependency": 0.0, "truncation_risk": 0.0, "dialogue_ratio": 0.0, "eligible": true}'
    assert si.pack_raw(si.pack_raw(raw)) == si.pack_raw(raw)      # 幂等
    assert si.canonical_json(raw) == raw


def test_pack_idempotent_on_packed():
    raw = json.dumps(si.analyze("他说。"), ensure_ascii=False)
    once = si.pack_raw(raw)
    assert si.pack_raw(once) == once
    assert si.pack_raw(si.pack_raw(once)) == once


def test_unpack_tolerates_garbage():
    for bad in (None, "", "not json", "i1:zzz", "i1:ffffff"):
        assert si.unpack(bad) == {}
        assert si.canonical_json(None) is None


def test_loads_any_keeps_nojson_distinction():
    """审计分桶要能分开「非法 JSON」与「合法但缺键」——不能被紧凑编码糊掉。"""
    assert si.loads_any(si.pack_raw(json.dumps(si.analyze("他说。"), ensure_ascii=False)))
    with pytest.raises(ValueError):
        si.loads_any(None)
    with pytest.raises(ValueError):
        si.loads_any("i1:zzz")
    with pytest.raises(ValueError):
        si.loads_any("not json")
    with pytest.raises(ValueError):
        si.loads_any("[1,2]")


def test_orm_decorator_is_transparent():
    """ORM 写→读必须让 `json.loads(seg.integrity)` 这类既有读者无感。

    用 conftest 的隔离库直接建行（上一版 monkeypatch `LG_DATA_DIR` + 删 `app.*`
    模块再重导入，会把 session 绑到另一个引擎上 ⇒ 写进去的行读不到，测试自身
    假红——已改回常规路径）。
    """
    from app import db as _db
    from app.models import Segment, Work

    _db.init_db()
    raw = json.dumps(si.analyze("祠堂前的雪还没化，陈三爷来得最早。"), ensure_ascii=False)
    with _db.session() as s:
        w = Work(title="t-orm-codec", source="test:orm-codec")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="正文", integrity=raw)
        s.add(seg)
        s.commit()
        seg_id = seg.id
        seg = s.query(Segment).filter(Segment.id == seg_id).one()
        assert json.loads(seg.integrity) == json.loads(raw)      # 读者口径不变
        assert set(json.loads(seg.integrity)) == BASE_KEYS
        # 落盘的是紧凑形态（证明确实省了空间，而不是装饰器空转）
        stored = s.connection().exec_driver_sql(
            "SELECT integrity FROM segments WHERE id=?", (seg_id,)).fetchone()[0]
        assert si.is_packed(stored), stored
        assert len(stored) <= 12
    assert isinstance(Segment.__table__.c.integrity.type, CompactIntegrity)


def test_eligible_is_first_char_of_packed():
    """eligible 必须落在前缀后第 1 个字符：SQL `LIKE 'i1:1%'` 靠它筛。"""
    ok = si.pack_raw(json.dumps(si.analyze("翌日清晨，城门大开。"), ensure_ascii=False))
    bad = si.pack_raw(json.dumps(si.analyze("」他抬头看了看天。"), ensure_ascii=False))
    assert ok.startswith(si._CODEC_PREFIX + "1"), ok
    assert bad.startswith(si._CODEC_PREFIX + "0"), bad
    assert si.is_eligible(ok) and not si.is_eligible(bad)
    assert si.unpack(ok)["eligible"] is True


def test_eligible_like_patterns_cover_both_storage_forms():
    """双口径：历史 JSON 行与紧凑行都要被 SQL 筛到（漏一个=采样域静默变小）。"""
    legacy, packed = si.eligible_like_patterns()
    assert legacy == si.LEGACY_ELIGIBLE_TRUE_LIKE
    assert packed == si.ELIGIBLE_TRUE_LIKE == si._CODEC_PREFIX + "1%"
    raw = json.dumps(si.analyze("翌日清晨，城门大开。"), ensure_ascii=False)
    assert legacy in raw or legacy == '%"eligible": true%'
    assert si.pack_raw(raw).startswith(packed[:-1])


def test_near_dup_eligible_only_sees_both_forms():
    """`train_sampling_pool(eligible_only=True)` 必须同时吃两种落盘形态。

    库里 5,800 万段是历史 JSON、新段是紧凑编码，长期并存；只挂一个 `LIKE`
    会让采样域悄悄少一半（不报错，只少段）。
    """
    from app import db as _db
    from app import near_dup
    from app.models import Segment, Work

    _db.init_db()
    ok_raw = json.dumps(si.analyze("翌日清晨，城门大开。"), ensure_ascii=False)
    bad_raw = json.dumps(si.analyze("」他抬头看了看天。"), ensure_ascii=False)
    with _db.session() as s:
        w = Work(title="t-elig-both", source="test:elig-both-forms")
        s.add(w)
        s.flush()
        packed_seg = Segment(work_id=w.id, ordinal=1, text="翌日清晨，城门大开。",
                             integrity=ok_raw)                 # ORM 写 ⇒ 紧凑形态
        s.add(packed_seg)
        legacy_seg = Segment(work_id=w.id, ordinal=0, text="翌日清晨，城门大开。",
                             integrity=bad_raw)
        s.add(legacy_seg)
        s.add(Segment(work_id=w.id, ordinal=2, text="」他抬头看了看天。",
                      integrity=bad_raw))                      # 不合格段
        s.commit()
        packed_id, legacy_id = packed_seg.id, legacy_seg.id
        # 把其中一行改回历史 JSON 形态（模拟 2026-09-30 前入库的行）
        s.connection().exec_driver_sql(
            "UPDATE segments SET integrity=? WHERE id=?", (ok_raw, legacy_id))
        s.commit()
        assert si.is_packed(s.connection().exec_driver_sql(
            "SELECT integrity FROM segments WHERE id=?", (packed_id,)).fetchone()[0])
    with _db.session() as s2:
        pool = near_dup.train_sampling_pool(s2, work_ids=[w.id], eligible_only=True)
        got = sorted(x.id for x in pool)
    assert got == sorted([packed_id, legacy_id]), got
