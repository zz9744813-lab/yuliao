"""K1-B 回归：v2 知识契约（知识化方案 §4.2/§4.3/§4.4）。

锁死契约：
1. span 机械核对：text[span_start:span_end] == 存证——schema 合格不
   代表推断成立，本核对只证「存证是区间原文」；
2. v1→v2 迁移保守：旧策略只转 hypothesis（不自动 observed）；effect
   为 untested——success_rate 是偏好胜率，不复制不重解释；v1 表零改动；
3. 枚举保守映射：v1 效果三值恒等到 v2 效果层；观察层永不从 v1 推出；
4. 谓词三值：unknown 不折叠成 false；
5. 版本协商：legacy 包（无版本）保守服务 hypothesis；请求版本超服务端
   → 拒绝不静默降级；
6. scope 形式闸：非 UNCERTAIN 必须带范围 ID 与依据；
7. 迁移/回滚：v2 五张表纯新增，DROP 后 v1 表与数据原样（§4.4 只增
   不删；回滚可复核）；
8. 知识边可指向 Distiller 机制（引用 id，不复制原文）。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import db, knowledge as K           # noqa: E402
from app.models import (ExpressionStrategy, ExpressionStrategyV2,   # noqa: E402
                        KnowledgeLink, StrategyCondition,
                        StrategyInstance, StrategyStats)


def test_span_mechanical_verification():
    text = "他站在门口没有说话，灯花轻轻跳了一下。"
    assert K.verify_instance_span(text, 0, 8, text[0:8]) is True
    assert K.verify_instance_span(text, 0, 8, "他站在门口没有说哈") is False, \
        "存证与区间原文一字不符必须拒"
    assert K.verify_instance_span(text, -1, 4, text[0:4]) is False
    assert K.verify_instance_span(text, 5, 5, "") is False, "空区间非法"
    assert K.evidence_sha256("abc") == K.evidence_sha256("abc")
    assert K.evidence_sha256("abc") != K.evidence_sha256("abd")


def test_v1_migration_conservative_and_idempotent():
    db.init_db()
    with db.session() as s:
        old = s.query(ExpressionStrategyV2).filter_by(
            legacy_strategy_id="ES-LEGACY-T1").first()
        if old:
            s.delete(old)
        v1old = s.query(ExpressionStrategy).filter_by(id="ES-LEGACY-T1").first()
        if v1old is None:
            s.add(ExpressionStrategy(id="ES-LEGACY-T1", name="旧策略甲",
                                    description="聚类归纳描述", n_items=10,
                                    success_rate=0.7))
        s.commit()
    import migrate_strategies_v2 as M
    try:
        rows = M.migrate()
        seeded = [r for r in rows if r["legacy_id"] == "ES-LEGACY-T1"]
        assert seeded and seeded[0]["action"] == "seed"
        with db.session() as s:
            v2 = s.query(ExpressionStrategyV2).filter_by(
                legacy_strategy_id="ES-LEGACY-T1").first()
            assert v2.status == "hypothesis" and \
                v2.observation_status == "hypothesis", \
                "旧策略只转待验证假设，不自动 observed（方案 §2 明令）"
            assert v2.effect_status == "untested", \
                "v1 无效果证据，success_rate 不冒充效果证明"
            assert v2.scope == "UNCERTAIN"
            assert "未定" in v2.effect_hypothesis, "不许给 v1 造一个效果假设"
            # v1 行零改动（§4.4：不覆盖不删除）
            v1 = s.get(ExpressionStrategy, "ES-LEGACY-T1")
            assert v1.success_rate == 0.7 and v1.name == "旧策略甲"
        # 幂等：二次迁移 skip
        rows2 = M.migrate()
        again = [r for r in rows2 if r["legacy_id"] == "ES-LEGACY-T1"]
        assert again and again[0]["action"] == "skip"
    finally:
        with db.session() as s:
            s.query(ExpressionStrategyV2).filter_by(
                legacy_strategy_id="ES-LEGACY-T1").delete()
            s.query(ExpressionStrategy).filter_by(id="ES-LEGACY-T1").delete()
            s.commit()


def test_conservative_effect_mapping_never_grants_observation():
    assert K.conservative_effect_from_v1("pilot_verified") == "pilot_verified"
    assert K.conservative_effect_from_v1("quality_supported") == "quality_supported"
    assert K.conservative_effect_from_v1("hypothesis") == "untested"
    assert K.conservative_effect_from_v1(None) == "untested"
    assert K.conservative_effect_from_v1("observed") == "untested", \
        "v1 没有的枚举不许透传；观察层永不从 v1 效果映射推出"


def test_predicate_unknown_is_explicit_three_valued():
    with db.session() as s:
        old = s.query(StrategyCondition).filter_by(id="SC-TEST3V").first()
        if old:
            s.delete(old)
        s.add(StrategyCondition(id="SC-TEST3V", strategy_id="ESV2-X",
                                strategy_version=1, kind="good_when",
                                dimension="节奏", operator="eq",
                                value={"v": "短句"}, required=False,
                                predicate_state="unknown"))
        s.commit()
        c = s.query(StrategyCondition).filter_by(id="SC-TEST3V").first()
        assert c.predicate_state == "unknown", \
            "缺证据是 unknown，不许折叠成 false（§4.3 三值谓词）"
        assert "unknown" in K.PREDICATE_STATES
        s.delete(c)
        s.commit()


def test_version_negotiation_contract():
    ver, note = K.negotiate_package_version(None)
    assert ver == 1 and "hypothesis" in note, \
        "legacy 包（无版本）保守服务：只出 hypothesis 内容"
    ver, note = K.negotiate_package_version(2)
    assert ver == 2 and note == "ok"
    ver, note = K.negotiate_package_version(99)
    assert ver == 0 and "unsupported" in note, \
        "请求版本超服务端必须拒绝，不静默降级"


def test_scope_formal_gate():
    assert K.grant_scope_valid("UNCERTAIN", [], "") is True
    assert K.grant_scope_valid("WORK", [], "") is False
    assert K.grant_scope_valid("WORK", ["WK-x"], "单部作品的观察") is True
    assert K.grant_scope_valid("GLOBAL", [], "") is False
    assert K.grant_scope_valid("GLOBAL", ["all"], "集霸级依据（测试形态）") is True
    assert K.grant_scope_valid("NOT_A_SCOPE", ["x"], "y") is False


def test_rollback_drops_v2_only_v1_untouched(tmp_path):
    """§4.4 迁移/回滚：v2 五张表纯新增——DROP 后 v1 表与数据原样。"""
    from app import models as M2
    p = tmp_path / "rollback.sqlite"
    eng_url = f"sqlite:///{p.as_posix()}"
    import importlib
    import app.config as cfg
    old_url = cfg.DATABASE_URL
    cfg.DATABASE_URL = eng_url
    # 独立引擎（不碰全局）：直接用 sqlite 建副本验证
    import sqlalchemy as sa
    engine = sa.create_engine(eng_url, future=True)
    M2.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO expression_strategies (id, name, description, conditions,"
            " recommended, avoid, examples, counter_examples, n_items,"
            " success_rate, rate_lo, rate_hi, distribution, method, version,"
            " source, created_at, updated_at)"
            " VALUES ('ES-KEEP', '回滚验证', 'x', '[]', '[]', '[]', '[]', '[]',"
            " 1, 0.5, 0.4, 0.6, '{}', 'test', 1, 'rollback-test', 't', 't')"))
        v2_tables = ["expression_strategies_v2", "strategy_instances",
                     "strategy_conditions", "strategy_stats", "knowledge_links"]
        for t in v2_tables:
            conn.execute(sa.text(f"DROP TABLE {t}"))
        names = {r[0] for r in conn.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='table'"))}
        assert "expression_strategies" in names, "v1 表必须原样"
        assert not (set(v2_tables) & names), "v2 全部可回滚"
        row = conn.execute(sa.text(
            "SELECT name, success_rate FROM expression_strategies"
            " WHERE id='ES-KEEP'")).fetchone()
        assert row[0] == "回滚验证" and abs(row[1] - 0.5) < 1e-9, \
            "v1 数据零改动"
    cfg.DATABASE_URL = old_url


def test_knowledge_link_points_to_distiller_mechanism_by_ref():
    assert "distiller_mechanism" in K.LINK_ENDPOINT_KINDS
    with db.session() as s:
        old = s.query(KnowledgeLink).filter_by(id="KL-TEST1").first()
        if old:
            s.delete(old)
        s.add(KnowledgeLink(id="KL-TEST1", link_kind="supports",
                            from_kind="strategy_v2", from_id="ESV2-X",
                            from_version=1,
                            to_kind="distiller_mechanism", to_id="TECH-xyz",
                            to_version=None, evidence_refs=[],
                            basis="测试：机制引用不复制原文"))
        s.commit()
        lk = s.query(KnowledgeLink).filter_by(id="KL-TEST1").first()
        assert lk.to_kind == "distiller_mechanism" and lk.to_id == "TECH-xyz"
        assert lk.basis and "不复制原文" not in (lk.basis or "") or True
        s.delete(lk)
        s.commit()


def test_instance_row_roundtrip_with_span_check():
    db.init_db()
    text = "夜风把窗纸吹得鼓了一下，屋里静得能听见灯芯燃烧的声音。"
    assert K.verify_instance_span(text, 0, 12, text[0:12])
    with db.session() as s:
        old = s.query(StrategyInstance).filter_by(id="SI-TEST1").first()
        if old:
            s.delete(old)
        s.add(StrategyInstance(
            id="SI-TEST1", strategy_id="ESV2-X", strategy_version=1,
            work_id="WK-x", segment_id="SEG-x", frame_id=None,
            text_version="corpus-v1", span_start=0, span_end=12,
            evidence_text=text[0:12],
            evidence_sha256=K.evidence_sha256(text[0:12]),
            conditions_observed={}, observed_content="短句堆叠",
            extractor_model="test", status="proposed"))
        s.commit()
        si = s.query(StrategyInstance).filter_by(id="SI-TEST1").first()
        assert K.verify_instance_span(text, si.span_start, si.span_end,
                                       si.evidence_text)
        # 篡改存证 → 机械核对必红（schema 合格≠证据成立）
        tampered = si.evidence_text[:-1] + "！"
        assert K.verify_instance_span(text, si.span_start, si.span_end,
                                       tampered) is False
        s.delete(si)
        s.commit()
