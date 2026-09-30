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
    # 历史行的 JSON 文本必须真的含 legacy 子串（`json.dumps` 默认分隔符带空格），
    # 且**不含**紧凑前缀 —— 两侧互斥，漏一个就是漏一类行
    # （`LEGACY_ELIGIBLE_TRUE_LIKE` 是带 `%` 通配的 LIKE 模式 ⇒ 比对时剥掉通配符）
    assert si.LEGACY_ELIGIBLE_TRUE_LIKE.strip("%") in raw, raw
    assert not raw.startswith(si._CODEC_PREFIX)
    stored = si.pack_raw(raw)
    assert stored.startswith(si.ELIGIBLE_TRUE_LIKE[:-1]), stored
    assert si.LEGACY_ELIGIBLE_TRUE_LIKE not in stored
    # 不合格段：两种形态都不该被筛到
    bad_raw = json.dumps(si.analyze("」他抬头看了看天。"), ensure_ascii=False)
    assert si.LEGACY_ELIGIBLE_TRUE_LIKE.strip("%") not in bad_raw
    assert not si.pack_raw(bad_raw).startswith(si.ELIGIBLE_TRUE_LIKE[:-1])


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
        # 把其中一行改回历史 JSON 形态（模拟 2026-09-30 前入库的行）——
        # 必须走裸 SQL：ORM 写路径会把基键行压成紧凑串
        s.connection().exec_driver_sql(
            "UPDATE segments SET integrity=? WHERE id=?", (ok_raw, legacy_id))
        s.commit()
        rows = dict(s.connection().exec_driver_sql(
            "SELECT id, integrity FROM segments WHERE id IN (?,?)",
            (packed_id, legacy_id)).fetchall())
        assert si.is_packed(rows[packed_id])                       # 新行=紧凑形态
        assert rows[legacy_id] == ok_raw                           # 历史行=原始 JSON
        # 负向：单挂紧凑口径会漏掉历史行，单挂历史口径会漏掉紧凑行
        assert not rows[legacy_id].startswith(si.ELIGIBLE_TRUE_LIKE[:-1])
        assert si.LEGACY_ELIGIBLE_TRUE_LIKE.strip("%") not in rows[packed_id]
    with _db.session() as s2:
        pool = near_dup.train_sampling_pool(s2, work_ids=[w.id], eligible_only=True)
        got = sorted(x.id for x in pool)
    assert got == sorted([packed_id, legacy_id]), got


# ── 会审（2026-09-30，glm-5.3 + qwen3.8-flash）点名的缺口 ──────────────────

@pytest.mark.parametrize("bad_ratio", [float("nan"), float("inf"), float("-inf"),
                                       "0.5", None, True, 10 ** 9 + 0.0])
def test_ratio_not_encodable_falls_back_without_raising(bad_ratio):
    """NaN/±inf/字符串/越界 ratio ⇒ 原样保留 JSON，**绝不抛**。

    pack_raw 挂在 ORM 写侧：抛异常会把整本入册打断（`int(round(nan))` 是
    ValueError、`round(inf)` 是 OverflowError）。
    """
    d = si.analyze("翌日清晨，城门大开。")
    d["dialogue_ratio"] = bad_ratio
    raw = json.dumps(d, ensure_ascii=False, allow_nan=True)
    packed = si.pack_raw(raw)                      # 不抛
    assert packed == raw and not si.is_packed(packed)


def test_unpack_rejects_leftover_bits_and_malformed_bodies():
    """畸形/截断紧凑串必须判非法，不能「成功」解成错误值。"""
    good = si.pack_raw(json.dumps(si.analyze("翌日清晨，城门大开。"), ensure_ascii=False))
    assert si.unpack(good)
    for bad in (good + "0",            # 尾部多一位 hex ⇒ 高位残 bit
                good + "ffff",
                si._CODEC_PREFIX,      # 只有前缀
                si._CODEC_PREFIX + "1",        # 缺 hex 体
                si._CODEC_PREFIX + "9abc",     # eligible 位非法
                si._CODEC_PREFIX + "1zzz"):
        assert si.unpack(bad) == {}, bad
        # 解不开的紧凑串**原样返回**（不糊成 "{}"）：坏数据要在下游炸出来
        assert si.canonical_json(bad) == bad


def test_zero_value_row_roundtrips():
    """全 0 行（n=0 ⇒ `format(0,"x")=="0"`）前导零丢失后仍要能解回全 0。"""
    d = {k: 0.0 for k in ("quote_integrity", "antecedent_integrity",
                          "dialogue_integrity", "scene_boundary",
                          "context_dependency", "truncation_risk")}
    d.update({"dialogue_ratio": 0.0, "eligible": False})
    raw = json.dumps(d, ensure_ascii=False)
    packed = si.pack_raw(raw)
    assert packed == si._CODEC_PREFIX + "00", packed
    assert si.canonical_json(packed) == raw


def test_unpack_or_none_three_states():
    """空={} / 合法=dict / 解不出=None（clean_text 的 `_integrity_flags` 口径）。"""
    assert si.unpack_or_none(None) == {}
    assert si.unpack_or_none("") == {}
    assert si.unpack_or_none("   ") == {}
    raw = json.dumps(si.analyze("翌日清晨，城门大开。"), ensure_ascii=False)
    assert si.unpack_or_none(raw)["eligible"] is True
    assert si.unpack_or_none(si.pack_raw(raw))["eligible"] is True
    for bad in ("not json", si._CODEC_PREFIX + "zzz", "[1,2]", "42"):
        assert si.unpack_or_none(bad) is None, bad


def test_integrity_column_ddl_affinity_unchanged():
    """紧凑化只改**值**，不改列 DDL/affinity（裸 SQL 读者与索引不受影响）。"""
    from app import db as _db
    from app.models import Segment

    _db.init_db()
    with _db.session() as s:
        cols = {r[1]: r[2] for r in s.connection().exec_driver_sql(
            "PRAGMA table_info(segments)").fetchall()}
    assert cols["integrity"] == "TEXT", cols
    assert Segment.__table__.c.integrity.type.impl.__class__.__name__ == "Text"
    assert not Segment.__table__.c.integrity.nullable is False or True   # 可空性未改
    assert Segment.__table__.c.integrity.nullable is True


def test_field_value_sets_cover_real_analyze_output():
    """`_FIELD_VALUES` 必须穷举 `analyze()` 的真实浮点连加结果。

    漂移的后果是**静默**的：该行落回原始 JSON，压缩收益归零而无人察觉。
    这里用真语料样本把每个指标的取值逐一钉在集合里。
    """
    texts = SAMPLES + [
        "「你来做什么？」他问。「我等你。」她答。",
        "但是，他还是走了。",
        "她笑了，然后又哭了，最后什么也没说。",
        "，半句开场。",
        "」孤悬闭引号开场。",
        "半晌，他才开口。",
    ]
    for i, t in enumerate(texts):
        d = si.analyze(t, ordinal=i)
        for name, allowed in si._FIELD_VALUES.items():
            assert d[name] in allowed, (name, d[name], allowed)
        assert isinstance(d["dialogue_ratio"], float)
        assert 0.0 <= d["dialogue_ratio"] <= 1.0
        assert isinstance(d["eligible"], bool)
        assert si.is_packed(si.pack_raw(json.dumps(d, ensure_ascii=False))), d


def test_k2_integrity_dict_reads_packed_rows():
    """`scripts/k2_extract_backfill.integrity_dict`（读取侧唯一解析层）吃紧凑行。"""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for p in (str(root), str(root / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from k2_extract_backfill import integrity_dict, integrity_flag_state
    except ImportError as exc:                                # 环境缺依赖：跳过而非假绿
        pytest.skip(f"k2_extract_backfill 不可导入: {exc}")

    d = si.analyze("翌日清晨，城门大开。")
    packed = si.pack_raw(json.dumps(d, ensure_ascii=False))
    assert integrity_dict(packed)["eligible"] is True          # 裸列值（紧凑形态）
    assert integrity_dict(packed)["scene_boundary"] == d["scene_boundary"]
    # 带 src_ok 的行永不压缩 ⇒ 解析层行为与压缩前逐字一致
    with_src = {**d, "src_ok": True}
    raw_src = json.dumps(with_src, ensure_ascii=False)
    assert integrity_dict(raw_src)["src_ok"] is True
    assert integrity_flag_state(raw_src, "src_ok") is True
    # 紧凑基键行没有 src_ok 键 ⇒ 三态是「未校验」（None），不是「坏 JSON」
    assert integrity_flag_state(packed, "src_ok") is None
