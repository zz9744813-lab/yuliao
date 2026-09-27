"""简报 C 选项代价数字的离线钉子（只读真库、零模型、零写库）。

2026-09-25 在基线 c1d045b 的真库只读实测：原始 C 场景下，覆汉
WK-dc90993434e9 为 1 张非空包，琼明神女录 WK-6e5d2623 为 1 张非空包，
合计 2 张；两张都引用策略 ESV2-0450910b989f 的同一份证据，其根作品集合
含琼明神女录 WK-6e5d2623。若上游按“查询作品 allowed_purposes 为空则不得出包”
收紧，覆汉 1→0 且归零，琼明 1→1 且不受影响，合计 2→1。

这里固定的是简报中的反事实代价算术，不把当前 query_knowledge 描述成已经
执行用途闸：原始选择与收紧后的有效张数都来自同一次只读 C 查询，后者只按
查询作品的 allowed_purposes 做包级准入掩码。测试不修改登记行，也不 flush
或提交任何对象；引擎回读 mode_ro=True、PRAGMA query_only=1。

── 「这张包里选中的到底是哪几条策略」= 现算，不是抄答案 ──
钉子②原先把 `selected_ids == ("ESV2-0450910b989f",)` 和
`evidence_root_works == ((QIONGMING,),)` 写成死字面量。那是**09-25 那天真库
状态的快照**，不是判据：K2 链夜间写库（09-27 00:44 落库，8 条策略
observed→replicated、实例 83→155）让 8 条策略都拿到了非基准合格证据，
选包从 1 条变 8 条、根作品集从 1 个变 2 个，快照当场失效 ⇒ 单跑也红。
现在期望值由本文件自带的**参考实现** `_oracle_package()` 从只读真库现算：
它只 `SELECT` 原始行（expression_strategies_v2 / strategy_instances /
segments / work_sources / strategy_conditions），在**同一次只读快照**上自己
实现 C 场景下 K3 的选包集合语义（层1 资格 → 层2 证据 → 条件闸 → 层3 范围
→ 同 key 取最高 version → 排序 → candidate_cap 截断），**一行被测管线的
函数都不调**（不调 `query_knowledge` / `_evidence_for` / `_scope_matches` /
`_condition_pipeline`）。门禁取值（合格集、排除集、文本版本集、范围特异性、
candidate_cap）**引用 app.knowledge_query 的常量与 `eligible_statuses` 单一
入口**而非抄字面量——口径搬家时参考实现跟着搬，搬完两边不等就会红（这正是
本钉子要的对照）。判据取策略行**内存里的当前值**（`apply_scenario` 刚把
status/scope/scope_ids 强制成 C 场景值），不取 `C_SPEC` 里的 `["<BOOK>"]`
未代入占位串。

数据再变会怎样：新策略/新实例/新根作品进来 ⇒ 期望值与被测值**一起**变，
门依旧绿；变的是数字，不是判据。只有「K3 选包逻辑与本参考实现不再同义」
（改了管线没同步这里、或反过来）才转红——那是一次真事件，不是夜间写入
的噪声。仍需人来复核的只有三种，都写成带文案的硬断言而不是静默放行：
`STRATEGY_ID` 掉出任一书（叙述点名的那条策略换血）、它掉出但同时被换血
掩盖、或它的根作品集合里不再有 WK-6e5d2623。

反向自检（均用同一命令 `F:/Hermes/hermes-agent/venv/Scripts/python.exe -m
pytest -q -p no:warnings tests/test_brief_cost_numbers.py`）：
 - 承重变异①（收紧后期望 1 改 0）→ exit 1，`.F.`，1 failed / 2 passed，
   失败为 `assert 1 == 0`。
 - 承重变异②（参考实现的层2 去掉「基准段硬剔」——
   `if role_by_seg.get(ins.segment_id) == "benchmark"` 改成 `if False and ...`）
   → exit 1，2 failed / 1 passed；两条 `selected_ids` 逐值对照同时红，
   并打出两行：管线 `('ESV2-d5beaf06f0bc', 'ESV2-19bfec4ffc65', …)` /
   参考 `('ESV2-564ec4504e80', 'ESV2-d5beaf06f0bc', …)`，pytest 报
   `At index 0 diff: 'ESV2-d5beaf06f0bc' != 'ESV2-564ec4504e80'`
   （真库 13 条 benchmark 实例一放行，区间数与排序分量立刻漂移）。
 - 恢复后重跑同一命令 → exit 0，`...`，3 passed。
未自跑清单：无。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import knowledge_query as KQ
from app.models import (ExpressionStrategyV2, Segment, StrategyCondition,
                        StrategyInstance, WorkSource)

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "brief_cost_k2_unlock_sim", ROOT / "scripts" / "k2_unlock_sim.py")
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

REAL_DB = Path("F:/agi/language-genome/data/language_genome.db")
FUHAN = "WK-dc90993434e9"
QIONGMING = "WK-6e5d2623"
STRATEGY_ID = "ESV2-0450910b989f"
C_SPEC = {
    "status": "verified",
    "scope": "WORK",
    "scope_ids": ["<BOOK>"],
    "scope_basis": sim.SIM_SCOPE_BASIS,
}


def _c_condition_gate_ok(s, strategy_id) -> bool:
    """C 场景（`semantic_requirements={}`）下的条件闸——逐行读 strategy_conditions。

    镜像 `_condition_pipeline`：`evaluate_predicate` 只认 operator=eq 且维度在
    requirements 里，requirements 为空 ⇒ state **恒为 unknown**（true 永不出现）。
    于是：bad_when 的 `state=="true"` 恒不成立 ⇒ 永不触发；neutral_when 直接
    `continue`（不计正支持）；required 的 good_when 撞 `state != "true"` ⇒
    `excluded_required_unknown` 一律出包。合起来=**只允许非 required 的 good_when**。
    """
    for c in s.query(StrategyCondition).filter_by(strategy_id=strategy_id).all():
        if c.kind != "good_when":
            continue          # bad_when 恒不触发；neutral_when 不计正支持
        if c.required:
            return False      # required 且 state≠"true" ⇒ 硬排除
    return True


def _oracle_scope_ok(s, st, book: str) -> bool:
    """层3 范围闸的独立实现（只读策略行当前 scope/scope_ids + 登记行）。

    与 `_scope_matches` 同义但自成一路：WORK→book 命中 scope_ids；
    AUTHOR/GENRE→经登记行反查；GLOBAL 本阶段只预留不授予、UNCERTAIN 不匹配
    ⇒ 一律拒。C 场景下 `apply_scenario` 把所有行强制成 WORK，另两路只是为了
    让本函数与被测方**全域**同义（口径搬家时不会在这里悄悄分叉）。
    """
    if st.scope == "WORK":
        return bool(book) and book in (st.scope_ids or [])
    if st.scope in ("AUTHOR", "GENRE"):
        reg = s.query(WorkSource).filter_by(work_id=book).first()
        if reg is None:
            return False                      # 无登记的来源不作证据
        if st.scope == "AUTHOR":
            return bool(reg.author_id) and reg.author_id in (st.scope_ids or [])
        return bool(set(st.scope_ids or []) & set(reg.genre_ids or []))
    return False                              # GLOBAL 预留 / UNCERTAIN 不匹配


def _c_eligible_intervals(s, strategy_id) -> set:
    """该策略在 C 场景下的合格证据区间集合 {(canonical_work_id, start, end)}。

    镜像 `_evidence_for` 的六道硬拦（顺序无关，集合语义相同）：有登记行、
    不是基准段、来源类型不在排除集、无禁用用途、文本版本在允许集，
    镜像/重跑按 (根作品, span) 聚合去重。C 的 policy 不带 source_policy ⇒
    require_same_book_evidence 关闭（不裁跨作品证据，只仍记根作品）。
    """
    instances = (s.query(StrategyInstance)
                 .filter(StrategyInstance.strategy_id == strategy_id,
                         StrategyInstance.status.in_(
                             KQ.ELIGIBLE_INSTANCE_STATUS)).all())
    if not instances:
        return set()
    seg_ids = {i.segment_id for i in instances}
    work_ids = {i.work_id for i in instances}
    role_by_seg = dict(s.query(Segment.id, Segment.role)
                       .filter(Segment.id.in_(seg_ids)).all())
    reg_by_work = {r.work_id: r for r in s.query(WorkSource)
                   .filter(WorkSource.work_id.in_(work_ids)).all()}
    intervals: set = set()
    for ins in instances:
        reg = reg_by_work.get(ins.work_id)
        if reg is None:
            continue
        if role_by_seg.get(ins.segment_id) == "benchmark":
            continue
        if reg.source_type in KQ.DEFAULT_EXCLUDED_SOURCE_TYPES:
            continue
        if set(reg.license_purposes or []) & KQ.DEFAULT_EXCLUDED_USES:
            continue
        if ins.text_version not in KQ.DEFAULT_ALLOWED_TEXT_VERSIONS:
            continue
        intervals.add((reg.canonical_work_id, ins.span_start, ins.span_end))
    return intervals


def _oracle_package(s, policy: dict) -> list[dict]:
    """C 场景「这本书能出哪张包」的**独立参考实现**（期望值来源）。

    与 `KQ.query_knowledge` 的关系：读同一批原始行、在**同一次只读快照**上
    各自算一遍，但**一行被测管线的函数都不调**（不调 `query_knowledge` /
    `_evidence_for` / `_scope_matches` / `_condition_pipeline`，只引用
    `app.knowledge_query` 里的门禁常量与 `eligible_statuses` 单一入口）。
    步骤逐字对齐固定过滤顺序：层1 status/observation → 层2 证据非空 →
    条件闸 → 层3 范围 → 同 strategy_key 取最高 version → 排序 → cap 截断。

    判据一律取**策略行在内存里的当前值**（`apply_scenario` 刚把 status/scope/
    scope_ids 强制成 C 场景值，`<BOOK>` 占位已在那一层代入了）——被测方看到的
    就是这些值；`policy["book_id"]` 同理。**不读 C_SPEC**：C_SPEC 里
    `scope_ids=["<BOOK>"]` 是未代入的占位串，拿它当判据只会得到恒假（09-27
    第一版就是这么写成参考恒为空集的，参考实现自己先红了一轮）。
    """
    book = policy.get("book_id") or ""
    cap = int((policy.get("limits") or {}).get("candidate_cap",
                                               KQ.CANDIDATE_CAP_MAX))
    kept: dict[str, tuple] = {}          # strategy_key → (version, 行, 区间集)
    for st in s.query(ExpressionStrategyV2).all():
        if st.status not in KQ.eligible_statuses(st.version):
            continue                     # 层1a：status 资格集（按自身 version 分桶）
        if st.observation_status not in KQ.ELIGIBLE_OBSERVATION:
            continue                     # 层1b：观察资格
        intervals = _c_eligible_intervals(s, st.id)      # 层2
        if not intervals:
            continue
        if not _c_condition_gate_ok(s, st.id):
            continue                     # 条件闸（= _condition_pipeline 在 {} 下）
        if not _oracle_scope_ok(s, st, book):
            continue                     # 层3
        cur = kept.get(st.strategy_key)
        if cur is None or st.version > cur[0]:
            kept[st.strategy_key] = (st.version, st, intervals)
    ranked = sorted(
        kept.values(),
        key=lambda item: (0,                             # required_matches：{} 下恒 0
                          0,                             # good_when_matches：{} 下恒 0
                          -len(item[2]),                 # evidence_count
                          -KQ.SCOPE_SPECIFICITY.get(item[1].scope, 0),
                          item[1].strategy_key))
    return [{"strategy_id": st.id,
             "roots": tuple(sorted({root for root, _a, _b in intervals}))}
            for _v, st, intervals in ranked[:cap]]


def _measure_c_cost(session, strategies, snap, book: str) -> dict:
    sim.apply_scenario(session, strategies, C_SPEC, book)
    try:
        policy = sim.sim_policy(book)
        response = sim.KQ.query_knowledge(policy, session)
        selected = list(response.get("selected") or [])
        registry = session.query(sim.WorkSource).filter_by(work_id=book).one()
        purposes = list(registry.allowed_purposes or [])
        admitted = bool(selected) and bool(purposes)
        # 期望值：同一次只读快照上，由本文件的参考实现现算（不调被测管线）
        oracle = _oracle_package(session, policy)
        return {
            "allowed_purposes": tuple(purposes),
            "raw_package_count": int(bool(selected)),
            "raw_status": response.get("status"),
            "package_count_after_tightening": int(admitted),
            "zero_after_tightening": not admitted,
            "selected_ids": tuple(entry.get("strategy_id") for entry in selected),
            "evidence_root_works": tuple(
                tuple(entry.get("evidence_root_works") or ())
                for entry in selected
            ),
            "oracle_selected_ids": tuple(o["strategy_id"] for o in oracle),
            "oracle_evidence_root_works": tuple(o["roots"] for o in oracle),
        }
    finally:
        sim.restore(session, strategies, snap)


@pytest.fixture(scope="module")
def c_cost_observation() -> dict:
    try:
        if not REAL_DB.is_file():
            pytest.skip(f"真库缺失或不可读：{REAL_DB}")
        engine, info = sim.build_readonly_engine(REAL_DB)
    except (OSError, sim.SimError, SQLAlchemyError) as exc:
        pytest.skip(f"真库缺失或不可读：{REAL_DB}（{type(exc).__name__}: {exc}）")

    session = None
    strategies = []
    snap = None
    try:
        assert info["mode_ro"] is True
        assert "mode=ro" in info["dbapi_uri"]
        assert info["query_only"] == 1
        session = sim.session_for(engine)
        strategies = sim.all_strategies(session)
        if not strategies:
            pytest.skip(f"真库不可用于本场景：{REAL_DB} 中没有可仿真策略")
        if STRATEGY_ID not in {st.id for st in strategies}:
            pytest.skip(f"真库不可用于本叙述：{REAL_DB} 中没有 {STRATEGY_ID}")
        for book in (FUHAN, QIONGMING):
            if session.query(sim.WorkSource).filter_by(work_id=book).first() is None:
                pytest.skip(f"真库不可用于本叙述：{REAL_DB} 缺 {book} 的来源登记行")
        snap = sim.snapshot(strategies)
        return {
            "fuhan": _measure_c_cost(session, strategies, snap, FUHAN),
            "qiongming": _measure_c_cost(session, strategies, snap, QIONGMING),
        }
    except (OSError, sim.SimError, SQLAlchemyError) as exc:
        pytest.skip(f"真库缺失或不可读：{REAL_DB}（{type(exc).__name__}: {exc}）")
    finally:
        if session is not None:
            if strategies and snap is not None:
                sim.restore(session, strategies, snap)
            session.close()
        engine.dispose()


def test_fuhan_zero_allowed_purposes_drops_its_c_package(c_cost_observation):
    observed = c_cost_observation["fuhan"]

    assert observed["allowed_purposes"] == ()
    assert observed["raw_status"] == "matched"
    assert observed["raw_package_count"] == 1
    assert observed["package_count_after_tightening"] == 0
    assert observed["zero_after_tightening"] is True
    # 选包内容 = 参考实现现算的期望值（逐值，含顺序）
    assert observed["selected_ids"] == observed["oracle_selected_ids"], \
        ("覆汉 C 包与参考实现不符：K3 选包口径已漂移（逐值对照含顺序）\n"
         f"  管线={observed['selected_ids']}\n"
         f"  参考={observed['oracle_selected_ids']}")
    assert observed["evidence_root_works"] == observed["oracle_evidence_root_works"], \
        "覆汉 C 包的证据根作品与参考实现不符"
    # 叙述本身：简报说「两张都引用这条策略」——覆汉这张也得真的在引用它。
    assert STRATEGY_ID in observed["selected_ids"], \
        f"{STRATEGY_ID} 不再出现在覆汉 C 包里（现有 {observed['selected_ids']}）" \
        "——反事实代价算术的叙述前提已变，须复核后更新本钉子"


def test_qiongming_package_survives_fuhan_zero_authorization(c_cost_observation):
    observed = c_cost_observation["qiongming"]

    assert observed["allowed_purposes"] == (
        "research", "training_source", "benchmark_source")
    assert observed["raw_status"] == "matched"
    assert observed["raw_package_count"] == 1
    assert observed["package_count_after_tightening"] == 1
    assert observed["zero_after_tightening"] is False
    # 期望值从只读真库现算（_oracle_package），不是 09-25 那天的字面量快照：
    # 夜间写入（新策略/新实例）让两边一起动，门不颤；只有管线与参考实现
    # 不同义时才红。
    assert observed["selected_ids"] == observed["oracle_selected_ids"], \
        ("琼明 C 包与参考实现不符：K3 选包口径已漂移（逐值对照含顺序）\n"
         f"  管线={observed['selected_ids']}\n"
         f"  参考={observed['oracle_selected_ids']}")
    assert observed["evidence_root_works"] == observed["oracle_evidence_root_works"], \
        "琼明 C 包的证据根作品与参考实现不符"
    # 叙述本身：简报点名的那条策略必须仍在琼明包里（掉出去是一次真事件，
    # 须人来改叙述，不接受静默换血）。
    assert STRATEGY_ID in observed["selected_ids"], \
        f"{STRATEGY_ID} 不再出现在琼明 C 包里（现有 {observed['selected_ids']}）" \
        "——反事实代价算术的叙述前提已变，须复核后更新本钉子"
    # 同一条策略在琼明这本书上的证据**根作品集合**：完整的逐值对照交给上面的
    # oracle 比对；这里只钉住叙述里那句可复核的实质——它确实由琼明的证据支撑
    # （集合本身可能随夜间写入增删根作品，故只钉「含 WK-6e5d2623」而非写死全集）。
    named_roots = observed["evidence_root_works"][
        observed["selected_ids"].index(STRATEGY_ID)]
    assert QIONGMING in named_roots, \
        (f"{STRATEGY_ID} 的证据根作品里已没有 {QIONGMING}（现有 {named_roots}）"
         "——「证据根作品为琼明」这句叙述的前提已变，须复核后更新本钉子")


def test_c_nonempty_package_count_changes_from_two_to_one(c_cost_observation):
    before = (
        c_cost_observation["fuhan"]["raw_package_count"]
        + c_cost_observation["qiongming"]["raw_package_count"]
    )
    after = (
        c_cost_observation["fuhan"]["package_count_after_tightening"]
        + c_cost_observation["qiongming"]["package_count_after_tightening"]
    )

    assert before == 2
    assert after == 1
    assert c_cost_observation["fuhan"]["raw_package_count"] == 1
    assert c_cost_observation["fuhan"]["package_count_after_tightening"] == 0
    assert c_cost_observation["qiongming"]["raw_package_count"] == 1
    assert c_cost_observation["qiongming"]["package_count_after_tightening"] == 1
