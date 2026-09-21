"""K3-A 测试共享种子库（冻结卡驱动用）：13 条策略覆盖全部过滤分支。

种子按监督口径设计（每条策略钉一个拒绝理由或匹配形态）：
A WORK 匹配（book-α，2 证据）；A' 同 key 高版本（去重）；
B AUTHOR 匹配（AUTH-1）；C UNCERTAIN；D GLOBAL；E hypothesis（状态）；
F bad_when 命中；G required unknown；H 无证据；J fixture 来源；
K 基准段证据；L 镜像重复（聚合=1）；M 授权禁用用途。
"""
from __future__ import annotations

from app import db
from app.models import (Author, ExpressionStrategyV2, Genre, KnowledgePackage,
                         Segment, StrategyCondition, StrategyInstance, Work,
                         WorkSource)

TXT = "夜里起了风，灯芯轻轻跳了一下，他坐在桌前把没写完的信重新拿起又放下。"


def _strategy(s, sid, key, version=1, *, status="verified",
              observation="observed", effect="pilot_verified", scope="WORK",
              scope_ids=None):
    s.add(ExpressionStrategyV2(id=sid, strategy_key=key, version=version,
                               abstract_operation=f"{key} 的抽象操作",
                               invariants=["不改动事实"], effect_hypothesis="对照任务 X",
                               failure_modes=["过度使用"], status=status,
                               source="seed", scope=scope,
                               scope_ids=scope_ids or [], scope_basis="种子",
                               observation_status=observation, effect_status=effect))


def _condition(s, sid, kind, dim, value, required=False, state="unknown"):
    s.add(StrategyCondition(strategy_id=sid, strategy_version=1, kind=kind,
                            dimension=dim, operator="eq", value={"v": value},
                            required=required, predicate_state=state))


def _instance(s, sid, iid, work, seg, span=(0, 10), tv="corpus-v1",
              status="verified"):
    s.add(StrategyInstance(id=iid, strategy_id=sid, strategy_version=1,
                           work_id=work, segment_id=seg, frame_id=None,
                           text_version=tv, span_start=span[0], span_end=span[1],
                           evidence_text=TXT[span[0]:span[1]],
                           evidence_sha256="0" * 64, conditions_observed={},
                           observed_content="种子观察", extractor_model="seed",
                           status=status))


def _work(s, wid, title, *, role=None, source="file:seed", v2_of=None,
          reg_type="human_fiction", canonical=None, author_id=None,
          genre_ids=None, license_purposes=None, license_basis=None):
    s.add(Work(id=wid, title=title, source=source, v2_of=v2_of))
    s.flush()      # Work 先落——无 relationship 时 flush 按表名排序，
                   # segments<works 会先插导致 FK 撞（同因：test_work_registry._mk）
    seg = Segment(work_id=wid, ordinal=0, text=TXT, role=role,
                  n_sentences=1, n_chars=len(TXT))
    s.add(seg)
    s.flush()
    import register_work_sources as REG        # 单一哈希口径（不本地重定义）
    sha, _n = REG._work_sha256(s, wid)
    s.add(WorkSource(work_id=wid, canonical_work_id=canonical or wid,
                     author_id=author_id, genre_ids=genre_ids or [],
                     source_type=reg_type, text_version="corpus-v1",
                     text_sha256=sha,           # 锚必须带——verify 锚复核
                     purpose_basis="seed",
                     identity_purposes=["research"],
                     license_purposes=license_purposes or [],
                     license_basis=license_basis, metadata_status="verified",
                     metadata_basis="seed"))
    return seg.id


def seed_knowledge():
    """在共享测试库建 K3-A 卡驱动用的全部种子（幂等：先清后建）。"""
    db.init_db()
    _WORKS = ("WK-α", "WK-β", "WK-αM", "WK-BENCH", "WK-FIX", "WK-LIC")
    with db.session() as s:
        # 只清本种子的行（共享库他文件数据不碰——全表 DELETE 会撞 FK/毁邻测）
        s.query(StrategyInstance).filter(
            StrategyInstance.work_id.in_(_WORKS)).delete(
            synchronize_session=False)
        for sid in [r.id for r in s.query(ExpressionStrategyV2.id).all()
                    if r.id.startswith("ESV2-")]:
            s.query(StrategyCondition).filter_by(strategy_id=sid).delete(
                synchronize_session=False)
        s.query(ExpressionStrategyV2).filter(
            ExpressionStrategyV2.id.like("ESV2-%")).delete(
            synchronize_session=False)
        s.query(KnowledgePackage).delete(synchronize_session=False)
        for wid in _WORKS:
            s.query(WorkSource).filter_by(work_id=wid).delete(
                synchronize_session=False)
            s.query(Segment).filter_by(work_id=wid).delete(
                synchronize_session=False)
            s.query(Work).filter_by(id=wid).delete(synchronize_session=False)
        if not s.query(Author).filter_by(id="AUTH-1").first():
            s.add(Author(id="AUTH-1", name="测试作者甲", verified_basis="种子"))
        g = s.query(Genre).filter_by(name="玄幻").first()
        if g is None:
            s.add(Genre(id="GEN-1", name="玄幻"))
        s.flush()
        seg_a = _work(s, "WK-α", "书甲", author_id="AUTH-1",
                      genre_ids=["GEN-1"])
        seg_b = _work(s, "WK-β", "书乙", author_id="AUTH-1",
                      genre_ids=["GEN-1"])
        seg_mir = _work(s, "WK-αM", "书甲（corpus v2）", v2_of="WK-α",
                        canonical="WK-α", author_id="AUTH-1",
                        genre_ids=["GEN-1"])   # 继承根——verify 逐行比对
        seg_bench = _work(s, "WK-α", "x", role="benchmark") if False else None
        seg_bench = _work(s, "WK-BENCH", "基准书", role="benchmark")
        seg_fix = _work(s, "WK-FIX", "fixture_x",
                        source="inbox:fixture_x.txt", reg_type="fixture")
        seg_lic = _work(s, "WK-LIC", "授权书", reg_type="human_fiction",
                        license_purposes=["benchmark_source"],
                        license_basis="授权测试")
        s.flush()
        _strategy(s, "ESV2-A", "A-短句加速", scope_ids=["WK-α"])
        _condition(s, "ESV2-A", "good_when", "节奏", "短句")
        _condition(s, "ESV2-A", "good_when", "场景", "追逃")
        _condition(s, "ESV2-A", "good_when", "视角", "限知", required=True)
        _condition(s, "ESV2-A", "bad_when", "场景", "静谧回忆")
        _condition(s, "ESV2-A", "neutral_when", "时长", "短篇")
        _instance(s, "ESV2-A", "SI-A1", "WK-α", seg_a, (0, 10))
        _instance(s, "ESV2-A", "SI-A2", "WK-α", seg_a, (12, 22))
        _strategy(s, "ESV2-A2", "A-短句加速", version=2, scope_ids=["WK-α"])
        _condition(s, "ESV2-A2", "good_when", "节奏", "短句")
        _instance(s, "ESV2-A2", "SI-A3", "WK-α", seg_a, (0, 10))
        _strategy(s, "ESV2-B", "B-作者习惯", scope="AUTHOR",
                  scope_ids=["AUTH-1"])
        _condition(s, "ESV2-B", "good_when", "节奏", "长句")
        _instance(s, "ESV2-B", "SI-B1", "WK-β", seg_b, (0, 10))
        _strategy(s, "ESV2-C", "C-泛用", scope="UNCERTAIN")
        _instance(s, "ESV2-C", "SI-C1", "WK-α", seg_a, (0, 10))
        _strategy(s, "ESV2-D", "D-全局", scope="GLOBAL")
        _instance(s, "ESV2-D", "SI-D1", "WK-α", seg_a, (0, 10))
        _strategy(s, "ESV2-E", "E-假设", status="hypothesis",
                  observation="hypothesis", effect="untested")
        _instance(s, "ESV2-E", "SI-E1", "WK-α", seg_a, (0, 10))
        _strategy(s, "ESV2-F", "F-冲突", scope_ids=["WK-α"])
        _condition(s, "ESV2-F", "bad_when", "场景", "追逃")
        _instance(s, "ESV2-F", "SI-F1", "WK-α", seg_a, (0, 10))
        _strategy(s, "ESV2-G", "G-未知必需", scope_ids=["WK-α"])
        _condition(s, "ESV2-G", "good_when", "情绪", "克制", required=True)
        _instance(s, "ESV2-G", "SI-G1", "WK-α", seg_a, (0, 10))
        _strategy(s, "ESV2-H", "H-无证据", scope_ids=["WK-α"])
        _strategy(s, "ESV2-J", "J-夹具来源", scope_ids=["WK-FIX"])
        _instance(s, "ESV2-J", "SI-J1", "WK-FIX", seg_fix, (0, 10))
        _strategy(s, "ESV2-K", "K-基准证据", scope_ids=["WK-BENCH"])
        _instance(s, "ESV2-K", "SI-K1", "WK-BENCH", seg_bench, (0, 10))
        _strategy(s, "ESV2-L", "L-镜像", scope_ids=["WK-α"])
        _condition(s, "ESV2-L", "good_when", "节奏", "短句")
        _instance(s, "ESV2-L", "SI-L1", "WK-α", seg_a, (0, 10))
        _instance(s, "ESV2-L", "SI-L2", "WK-αM", seg_mir, (0, 10))
        _strategy(s, "ESV2-M", "M-授权用途", scope_ids=["WK-LIC"])
        _instance(s, "ESV2-M", "SI-M1", "WK-LIC", seg_lic, (0, 10))
        s.commit()
