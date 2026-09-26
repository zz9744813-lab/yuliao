"""K5 晋升三级门逐条解释器回归（scripts/k5_promotion_gate_explain.py）。

**全离线、零真库依赖**：所有用例都用 tmp_path 里的**合成库**（自建 ORM 临时
sqlite）构造五种卡点——四个门各一例 + 全通一例，逐级判词与汇总计数逐字断言。
真库一次都不碰（`db_path` 永远指 tmp_path 内的合成库）。

红线（任务书钉死，逐条对号）：
① 判据单源：门判词必须取自 `app.knowledge_query` 的常量/函数——断言
   `single_source` 清单里的每个符号在 KQ 上真实存在，且解释器读的合格集
   就是 KQ 的合格集（上游若另写一套，本用例立刻红）；
② 逐级判词方向可证伪：每门的 pass 翻转、拒因分类、blocked_at「取第一条
   不过的级」都逐条钉死（不是只测「全红」）；
③ **单源自证**：`library_crosscheck` 必须 n_disagree=0（解释器落点 vs 库侧
   `query_knowledge` 落点）——把 status 门判据改成恒真时这条与 ② 同时变红；
④ 拒因分类 fail-visible：未登记判词落 `unknown:*` 并单独报出，不静默归并；
⑤ 只读性：解释器跑完后合成库字节内容与 mtime 均未变，目录不冒新文件；
⑥ `--print-only` 零写盘；`--doc-out` 只重写机器区（叙述段原样保留、幂等）。
另钉：库不可读 → `db_available=false` + 如实原因，不猜任何判词。
"""
from __future__ import annotations

import importlib.util as _u
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k5e", ROOT / "scripts" / "k5_promotion_gate_explain.py")
k5e = _u.module_from_spec(_spec)
sys.modules["k5e"] = k5e
_spec.loader.exec_module(k5e)

from app import knowledge_query as KQ                       # noqa: E402
from app.db import Base                                     # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,      # noqa: E402
                        StrategyCondition, StrategyInstance, Work, WorkSource)

TXT = "夜里起了风，他把没写完的信重新拿起又放下。"
# 顶层契约键（缺一即红）
TOP_KEYS = {"schema", "explain", "task", "generated_at", "repo_root",
            "db_path", "db_mode", "db_opened", "db_available", "db_error",
            "policy", "gate_order", "gate_titles", "single_source",
            "constants_as_read", "strategies", "summary",
            "library_crosscheck", "closure_criteria",
            "closure_criteria_note", "discipline"}
GATE_KEYS = {"gate1_status", "gate2_observation", "gate3_evidence",
             "gate_condition", "gate4_scope"}
DISC_KEYS = {"db_mode", "db_writes", "model_calls", "git_writes",
             "no_status_or_scope_adjudication", "no_one_click_promotion_advice",
             "no_gate_relaxation", "criteria_single_sourced_from", "forbid"}


# ------------------------------------------------------ 合成库构造
def _mk_synth(tmp_path: Path, cards, instances=(), conditions=(), works=(),
              name="synth.db"):
    """自建合成库（ORM 建表 + 插行）——**不碰真库、不依赖 tests/knowledge_seed
    的共享测试库**（本文件只认自己 tmp_path 里的这份）。

    cards=[dict(id,key,version,status,obs,scope,scope_ids)]
    works=[dict(id,role,source_type,license_purposes)]
    instances=[dict(id,strategy_id,work_id,span,tv,status)]
    conditions=[dict(strategy_id,kind,dimension,value,required)]
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    db = tmp_path / name
    eng = create_engine(f"sqlite:///{db.as_posix()}", future=True)
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    seg_of: dict[str, str] = {}
    with S() as s:
        for w in works:
            s.add(Work(id=w["id"], title=w["id"], source="file:synth"))
            s.flush()
            seg = Segment(work_id=w["id"], ordinal=0, text=TXT,
                          role=w.get("role"), n_sentences=1, n_chars=len(TXT))
            s.add(seg)
            s.flush()
            seg_of[w["id"]] = seg.id
            s.add(WorkSource(work_id=w["id"],
                             canonical_work_id=w.get("canonical", w["id"]),
                             genre_ids=[], source_type=w.get("source_type",
                                                             "human_fiction"),
                             text_version="corpus-v1", text_sha256="0" * 64,
                             purpose_basis="synth",
                             identity_purposes=["research"],
                             license_purposes=w.get("license_purposes") or [],
                             license_basis=w.get("license_basis"),
                             metadata_status="verified",
                             metadata_basis="synth"))
        for c in cards:
            s.add(ExpressionStrategyV2(
                id=c["id"], strategy_key=c["key"], version=c.get("version", 1),
                abstract_operation=c["key"], invariants=[],
                effect_hypothesis="x", failure_modes=[],
                status=c.get("status", "verified"),
                source="synth", scope=c.get("scope", "WORK"),
                scope_ids=c.get("scope_ids") or [],
                scope_basis="synth",
                observation_status=c.get("obs", "observed"),
                effect_status=c.get("effect", "untested")))
        for c in conditions:
            s.add(StrategyCondition(
                id=f"SC-{c['strategy_id']}-{c['dimension']}",
                strategy_id=c["strategy_id"], strategy_version=1,
                kind=c.get("kind", "good_when"),
                dimension=c["dimension"], operator="eq",
                value={"v": c["value"]}, required=c.get("required", False),
                predicate_state=c.get("predicate_state", "unknown"),
                evidence_refs=[], version=1))
        for i in instances:
            s.add(StrategyInstance(
                id=i["id"], strategy_id=i["strategy_id"], strategy_version=1,
                work_id=i["work_id"], segment_id=seg_of[i["work_id"]],
                frame_id=None, text_version=i.get("tv", "corpus-v1"),
                span_start=i.get("span", (0, 10))[0],
                span_end=i.get("span", (0, 10))[1],
                evidence_text=TXT, evidence_sha256="0" * 64,
                conditions_observed={}, observed_content="synth",
                extractor_model="synth", status=i.get("status", "verified")))
        s.commit()
    eng.dispose()
    return db


def _explain(db: Path, policy=None):
    return k5e.build_report(tmp_path_owner(db), db, dict(policy or {}))


def tmp_path_owner(db: Path) -> Path:
    return db.parent


def _by_key(report, key):
    return next(i for i in report["strategies"] if i["strategy_key"] == key)


# ------------------------------------------------- ① 判据单源（不另写一套）
def test_criteria_single_sourced_from_knowledge_query(tmp_path):
    """①解释器读的合格集/判据符号就是 app.knowledge_query 的那些——
    上游若另写一套判据（自造 ELIGIBLE 字面量等），本用例立刻红。"""
    db = _mk_synth(tmp_path, [dict(id="ESV2-A", key="A")])
    rep = _explain(db)
    assert rep["constants_as_read"]["ELIGIBLE_STATUS_DEFAULT"] == \
        sorted(KQ.eligible_statuses(None))
    assert rep["constants_as_read"]["ELIGIBLE_OBSERVATION"] == \
        sorted(KQ.ELIGIBLE_OBSERVATION)
    assert rep["constants_as_read"]["ELIGIBLE_INSTANCE_STATUS"] == \
        sorted(KQ.ELIGIBLE_INSTANCE_STATUS)
    assert rep["constants_as_read"]["DEFAULT_EXCLUDED_SOURCE_TYPES"] == \
        sorted(KQ.DEFAULT_EXCLUDED_SOURCE_TYPES)
    assert rep["single_source"]["local_criteria_written"].startswith("none")
    assert rep["discipline"]["criteria_single_sourced_from"] == \
        "app/knowledge_query.py"
    # 清单里每个符号在 KQ 上真实存在（函数可调用 / 常量有值）
    for c in rep["single_source"]["symbols"]:
        name = c["symbol"].split("(")[0]
        assert hasattr(KQ, name), f"单源符号在 knowledge_query 上不存在：{name}"
    # 门序与 query_knowledge 的执行顺序对齐（候选筛→证据→条件→范围）
    assert rep["gate_order"] == list(k5e.GATE_ORDER)
    assert rep["gate_order"][:2] == ["gate1_status", "gate2_observation"]
    assert rep["gate_order"][-1] == "gate4_scope"


def test_status_gate_uses_version_bucket(tmp_path):
    """①status 门逐版本取 eligible_statuses(version)（不是硬编码 'verified'）——
    改 KQ 的版本桶口径本用例跟着走，改本文件的判据则红。"""
    db = _mk_synth(tmp_path, [dict(id="ESV2-V2", key="V2", version=2,
                                   status="hypothesis")])
    rep = _explain(db)
    g1 = _by_key(rep, "V2")["gates"]["gate1_status"]
    assert g1["expected"] == sorted(KQ.eligible_statuses(2))
    assert g1["pass"] is False
    assert g1["reason"] == "status_not_eligible:hypothesis"
    assert "eligible_statuses" in g1["source_symbol"]


# --------------------------------------------------- ② 逐级判词（方向可证伪）
def test_four_block_points_plus_all_pass(tmp_path):
    """②四种卡点各一例 + 全通一例：逐级判词、blocked_at、汇总计数逐字断言。"""
    db = _mk_synth(
        tmp_path,
        cards=[
            dict(id="ESV2-S", key="卡在status", status="hypothesis"),
            dict(id="ESV2-O", key="卡在obs", obs="hypothesis"),
            dict(id="ESV2-E", key="卡在证据", scope_ids=["WK-A"]),
            dict(id="ESV2-P", key="卡在scope", scope_ids=["WK-A"],
                 scope="UNCERTAIN"),
            dict(id="ESV2-OK", key="全通", scope_ids=["WK-A"]),
        ],
        works=[dict(id="WK-A"), dict(id="WK-B", role="benchmark")],
        instances=[
            dict(id="SI-E1", strategy_id="ESV2-E", work_id="WK-B",   # 基准段
                 span=(0, 5)),
            dict(id="SI-E2", strategy_id="ESV2-E", work_id="WK-B",
                 span=(6, 12)),
            dict(id="SI-P1", strategy_id="ESV2-P", work_id="WK-A",
                 span=(0, 5)),
            dict(id="SI-OK1", strategy_id="ESV2-OK", work_id="WK-A",
                 span=(0, 5)),
        ])
    rep = _explain(db, {"book_id": "WK-A"})

    # 卡点 1：status 门（第 1 级）——后面几级照样报出（可核证据，不是短路日志）
    s = _by_key(rep, "卡在status")
    assert s["blocked_at"] == "gate1_status"
    assert s["blocked_reason"] == "status_not_eligible:hypothesis"
    assert s["gates"]["gate2_observation"]["pass"] is True

    # 卡点 2：observation 门（第 2 级）
    o = _by_key(rep, "卡在obs")
    assert o["blocked_at"] == "gate2_observation"
    assert o["blocked_reason"] == "observation_not_eligible:hypothesis"

    # 卡点 3：证据门（第 3 级）——两段全被 benchmark_source 剔净
    e = _by_key(rep, "卡在证据")
    assert e["blocked_at"] == "gate3_evidence"
    assert e["blocked_reason"] == "excluded_no_evidence"
    assert e["gates"]["gate3_evidence"]["evidence_count"] == 0
    assert e["gates"]["gate3_evidence"]["instances_considered"] == 2
    assert e["gates"]["gate3_evidence"]["stripped"]["by_category"] == \
        {"benchmark_source": 2}
    assert e["gates"]["gate3_evidence"]["reason_source"].startswith("library")

    # 卡点 4：scope 门（第 4 级，UNCERTAIN 归哪类拒因由库判词给出）
    p = _by_key(rep, "卡在scope")
    assert p["blocked_at"] == "gate4_scope"
    assert p["blocked_reason"] == "excluded_scope_uncertain"
    assert p["gates"]["gate4_scope"]["reject_category"] == "uncertain"

    # 全通一例
    ok = _by_key(rep, "全通")
    assert ok["blocked_at"] is None and ok["all_gates_pass"] is True
    assert all(ok["gates"][g]["pass"] for g in k5e.GATE_ORDER)
    assert ok["gates"]["gate3_evidence"]["evidence_count"] == 1

    # 汇总计数
    sm = rep["summary"]
    assert sm["n_strategies"] == 5
    assert sm["blocked_at_counts"] == {"gate1_status": 1, "gate2_observation": 1,
                                       "gate3_evidence": 1, "gate4_scope": 1,
                                       "none(all_pass)": 1}
    assert sm["pass_per_gate"] == {"gate1_status": 4, "gate2_observation": 4,
                                   "gate3_evidence": 2, "gate_condition": 5,
                                   "gate4_scope": 2}
    assert sm["n_all_gates_pass"] == 1
    assert sm["evidence"]["stripped_total"] == 2
    assert sm["evidence"]["stripped_by_category"] == {"benchmark_source": 2}
    assert sm["evidence"]["net_evidence_intervals"] == 2
    assert sm["evidence"]["n_strategies_with_net_evidence"] == 2
    assert sm["evidence"]["instances_considered"] == 4


def test_blocked_at_is_first_failing_gate_not_last(tmp_path):
    """②blocked_at 取**第一条**不过的级：多级皆红时只报最早那条
    （改成「最后一条」或「任一」本用例立刻红）。"""
    db = _mk_synth(
        tmp_path,
        cards=[dict(id="ESV2-X", key="多级皆红", status="hypothesis",
                    obs="hypothesis", scope="UNCERTAIN")])
    rep = _explain(db)
    x = _by_key(rep, "多级皆红")
    red = [g for g in k5e.GATE_ORDER if not x["gates"][g]["pass"]]
    assert len(red) >= 2                       # 前提：确实多级红
    assert x["blocked_at"] == red[0] == "gate1_status"


def test_strip_classification_covers_every_reason_kind(tmp_path):
    """②拒因分类：benchmark / 来源类型 / license / 版本 / 未登记 五类各一例，
    计数与取值逐字断言（分类表漏项落 unknown:*，不许静默归并）。"""
    db = _mk_synth(
        tmp_path,
        cards=[dict(id="ESV2-C", key="分类卡", scope_ids=["WK-A"])],
        works=[dict(id="WK-A"),
               dict(id="WK-BENCH", role="benchmark"),
               dict(id="WK-FIX", source_type="fixture"),
               dict(id="WK-LIC", license_purposes=["training_source"],
                    license_basis="synth"),
               dict(id="WK-TVV")],
        instances=[
            dict(id="SI-1", strategy_id="ESV2-C", work_id="WK-BENCH",
                 span=(0, 3)),
            dict(id="SI-2", strategy_id="ESV2-C", work_id="WK-FIX",
                 span=(0, 3)),
            dict(id="SI-3", strategy_id="ESV2-C", work_id="WK-LIC",
                 span=(0, 3)),
            dict(id="SI-4", strategy_id="ESV2-C", work_id="WK-TVV",
                 span=(0, 3), tv="test-fixture"),
            dict(id="SI-5", strategy_id="ESV2-C", work_id="WK-A",
                 span=(0, 3)),
        ])
    rep = _explain(db, {"book_id": "WK-A",
                        "source_policy": {"excluded_uses":
                                          ["training_source"]}})
    g3 = _by_key(rep, "分类卡")["gates"]["gate3_evidence"]
    assert g3["stripped"]["by_category_value"] == {
        "benchmark_source": 1,
        "excluded_source_type:fixture": 1,
        "excluded_use": 1,
        "text_version:test-fixture": 1,
    }
    assert g3["stripped"]["unknown_categories"] == {}
    assert g3["evidence_count"] == 1            # WK-A 那段净剩
    assert "no_registry" in g3["stripped"]["category_labels"]
    assert "mirror_dedup" in g3["stripped"]["category_labels"]


def test_classify_stripped_unknown_is_visible():
    """④未登记判词 → unknown:* 且单独报出（分类表漏项 fail-visible）。"""
    out = k5e.classify_stripped(["SI-1:brand_new_reason", "SI-2:brand_new_reason",
                                 "SI-3:benchmark_source"])
    assert out["by_category"] == {"benchmark_source": 1,
                                  "unknown:brand_new_reason": 2}
    assert out["unknown_categories"] == {"brand_new_reason": 2}
    assert out["unknown_categories_note"]
    assert out["total"] == 3


def test_scope_verdict_verbatim_from_library(tmp_path):
    """②scope 判词逐字取自 KQ._scope_matches：GLOBAL/WORK 未命中/UNCERTAIN
    三种各一例，解释器不得改写（改成自己的词即红）。"""
    db = _mk_synth(
        tmp_path,
        cards=[dict(id="ESV2-G", key="全局", scope="GLOBAL"),
               dict(id="ESV2-W", key="作品未命中", scope="WORK",
                    scope_ids=["WK-OTHER"]),
               dict(id="ESV2-U", key="不确定", scope="UNCERTAIN")])
    rep = _explain(db, {"book_id": "WK-A"})
    assert _by_key(rep, "全局")["gates"]["gate4_scope"]["reason"] == \
        "excluded_scope_global_reserved"
    assert _by_key(rep, "作品未命中")["gates"]["gate4_scope"]["reason"] == \
        "excluded_scope"
    assert _by_key(rep, "不确定")["gates"]["gate4_scope"]["reason"] == \
        "excluded_scope_uncertain"
    for key in ("全局", "作品未命中", "不确定"):
        g4 = _by_key(rep, key)["gates"]["gate4_scope"]
        assert g4["reason_source"].startswith("library")


def test_condition_gate_reuses_pipeline(tmp_path):
    """②条件管道判词取自 KQ._condition_pipeline（required unknown → 不许
    强行采用）：拦截理由逐字断言。"""
    db = _mk_synth(
        tmp_path,
        cards=[dict(id="ESV2-R", key="必需未知", scope_ids=["WK-A"]),
               dict(id="ESV2-GW", key="good命中", scope_ids=["WK-A"])],
        works=[dict(id="WK-A")],
        instances=[dict(id="SI-R", strategy_id="ESV2-R", work_id="WK-A"),
                   dict(id="SI-GW", strategy_id="ESV2-GW", work_id="WK-A")],
        conditions=[dict(strategy_id="ESV2-R", dimension="情绪", value="克制",
                         required=True),
                    dict(strategy_id="ESV2-GW", dimension="情绪", value="克制")])
    rep = _explain(db, {"book_id": "WK-A"})
    r = _by_key(rep, "必需未知")
    assert r["blocked_at"] == "gate_condition"
    assert r["blocked_reason"] == "excluded_required_unknown"
    g = _by_key(rep, "good命中")
    assert g["gates"]["gate_condition"]["pass"] is True
    assert g["gates"]["gate_condition"]["components"]["good_when_matches"] == 0
    assert g["gates"]["gate_condition"]["n_uncertain"] == 1
    assert g["all_gates_pass"] is True


# ------------------------------------------- ③ 单源自证（query_knowledge 对账）
def test_library_crosscheck_agrees_on_all_five_shapes(tmp_path):
    """③解释器落点 vs 库侧 `query_knowledge` 落点：五种形态逐条一致，
    n_disagree 必须 0（把 status 门判据改成恒真时这里与 ② 同时变红）。"""
    db = _mk_synth(
        tmp_path,
        cards=[
            dict(id="ESV2-S", key="卡在status", status="hypothesis"),
            dict(id="ESV2-O", key="卡在obs", obs="hypothesis"),
            dict(id="ESV2-E", key="卡在证据", scope_ids=["WK-A"]),
            dict(id="ESV2-P", key="卡在scope", scope_ids=["WK-A"],
                 scope="UNCERTAIN"),
            dict(id="ESV2-OK", key="全通", scope_ids=["WK-A"]),
        ],
        works=[dict(id="WK-A"), dict(id="WK-B", role="benchmark")],
        instances=[dict(id="SI-E1", strategy_id="ESV2-E", work_id="WK-B",
                        span=(0, 5)),
                   dict(id="SI-P1", strategy_id="ESV2-P", work_id="WK-A",
                        span=(0, 5)),
                   dict(id="SI-OK1", strategy_id="ESV2-OK", work_id="WK-A",
                        span=(0, 5))])
    rep = _explain(db, {"book_id": "WK-A"})
    cc = rep["library_crosscheck"]
    assert cc["error"] is None
    assert cc["n_disagree"] == 0 and cc["mismatches"] == []
    assert cc["n_agree"] == 5
    assert cc["query_knowledge_status"] == "matched"
    assert cc["selected_keys"] == ["全通"]
    by = {r["strategy_key"]: r for r in cc["rows"]}
    assert by["卡在status"]["library"] == "candidate_filtered_out"
    assert by["卡在obs"]["library"] == "candidate_filtered_out"
    assert by["卡在证据"]["library"] == "rejected:excluded_no_evidence"
    assert by["卡在scope"]["library"] == "rejected:excluded_scope_uncertain"
    assert by["全通"]["library"] == "selected"
    assert all(r["agree"] for r in cc["rows"])


def test_crosscheck_detects_disagreement(tmp_path):
    """③对账器自身可证伪：喂一条与库侧落点不符的「解释结果」，
    n_disagree 必须变正（否则这条对账形同虚设）。"""
    db = _mk_synth(tmp_path, [dict(id="ESV2-A", key="A", status="hypothesis")])
    s = k5e.open_ro_session(db)
    try:
        fake = [{"strategy_key": "A", "gates": {
            "gate1_status": {"pass": True},
            "gate2_observation": {"pass": True},
            "gate3_evidence": {"pass": False, "reason": "excluded_no_evidence"},
            "gate_condition": {"pass": True, "reason": "pass"},
            "gate4_scope": {"pass": True, "reason": "pass"}}}]
        cc = k5e.library_crosscheck(s, {}, fake)
        assert cc["n_disagree"] == 1
        assert cc["mismatches"][0]["strategy_key"] == "A"
    finally:
        s.close()


# ------------------------------------------------------------ 契约与纪律
def test_json_key_contract(tmp_path):
    """顶层/二级契约键逐个点名（缺键即红）；db_mode 恒 ro；纪律字段钉死
    「不裁定 status/scope、不给一键升格建议」。"""
    db = _mk_synth(tmp_path, [dict(id="ESV2-A", key="A")])
    rep = _explain(db)
    assert TOP_KEYS <= set(rep)
    assert DISC_KEYS <= set(rep["discipline"])
    assert rep["db_mode"] == "ro"
    assert rep["discipline"]["db_writes"] == 0
    assert rep["discipline"]["model_calls"] == 0
    assert rep["discipline"]["git_writes"] == 0
    assert rep["discipline"]["no_status_or_scope_adjudication"] is True
    assert rep["discipline"]["no_one_click_promotion_advice"] is True
    assert rep["schema"] == "k5_promotion_gate_explain/v1"
    for item in rep["strategies"]:
        assert GATE_KEYS <= set(item["gates"])
        assert set(item["gates"]) == GATE_KEYS
        for g in k5e.GATE_ORDER:
            assert {"pass", "reason", "reason_source", "source_symbol",
                    "name_cn"} <= set(item["gates"][g])
    assert json.dumps(rep, ensure_ascii=False)      # 全 JSON 可序列化


def test_closure_criteria_are_mechanical(tmp_path):
    """收口判据清单：每条带可执行式 + 当前读数 + 机械判定；阈值是占位
    （target_is_placeholder=True）——不许把阈值写成已拍板值。"""
    db = _mk_synth(
        tmp_path,
        cards=[dict(id="ESV2-S", key="卡在status", status="hypothesis"),
               dict(id="ESV2-OK", key="全通", scope_ids=["WK-A"])],
        works=[dict(id="WK-A")],
        instances=[dict(id="SI-OK1", strategy_id="ESV2-OK", work_id="WK-A")])
    rep = _explain(db, {"book_id": "WK-A"})
    crit = {c["id"]: c for c in rep["closure_criteria"]}
    assert {"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G5-SQL"} <= set(crit)
    for c in crit.values():
        assert c["executable"], c["id"]
        assert c["target_is_placeholder"] is True
    assert crit["G1"]["current"] == 1 and crit["G1"]["satisfied"] is True
    assert crit["G2"]["current"] == 1 and crit["G2"]["satisfied"] is True
    assert crit["G8"]["current"] == 1 and crit["G8"]["satisfied"] is True
    assert crit["G5"]["current"] == 1 and crit["G5"]["satisfied"] is False
    # SQL 里的值域必须由 knowledge_query 常量展开（未手抄）
    sql = crit["G5-SQL"]["executable"]
    for v in KQ.DEFAULT_EXCLUDED_SOURCE_TYPES:
        assert f"'{v}'" in sql
    for v in KQ.DEFAULT_ALLOWED_TEXT_VERSIONS:
        assert f"'{v}'" in sql
    for v in KQ.ELIGIBLE_INSTANCE_STATUS:
        assert f"'{v}'" in sql
    assert rep["closure_criteria_note"]


def test_db_unreadable_reported_not_guessed(tmp_path):
    """库不可读 → db_available=false + 如实原因；**不许**猜出任何判词/
    汇总数字（凭空造读数即红）。"""
    rep = k5e.build_report(tmp_path, tmp_path / "absent.db", {})
    assert rep["db_available"] is False
    assert rep["db_error"]
    assert rep["strategies"] == []
    assert rep["summary"]["n_strategies"] == 0
    assert rep["summary"]["evidence"]["net_evidence_intervals"] is None
    assert rep["closure_criteria"] == []
    assert rep["library_crosscheck"]["n_disagree"] == 0
    assert rep["library_crosscheck"]["rows"] == []


# ------------------------------------------------------------- 只读性
def test_readonly_synth_db_untouched(tmp_path):
    """解释器跑完后合成库字节内容与 mtime 均未变，目录不冒新文件
    （-wal/-shm/-journal 一并盯）。"""
    db = _mk_synth(
        tmp_path,
        cards=[dict(id="ESV2-E", key="卡在证据", scope_ids=["WK-A"])],
        works=[dict(id="WK-A"), dict(id="WK-B", role="benchmark")],
        instances=[dict(id="SI-1", strategy_id="ESV2-E", work_id="WK-B")])
    before = db.read_bytes()
    before_mtime = os.stat(db).st_mtime_ns
    before_list = sorted(p.name for p in tmp_path.iterdir())
    _explain(db, {"book_id": "WK-A"})
    assert db.read_bytes() == before
    assert os.stat(db).st_mtime_ns == before_mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == before_list


def test_print_only_writes_nothing(tmp_path, capsys):
    """--print-only：除 stdout 外零写盘（--doc-out/--json-out 一并忽略）。"""
    db = _mk_synth(tmp_path, [dict(id="ESV2-A", key="A")])
    doc = tmp_path / "should_not_exist.md"
    json_out = tmp_path / "should_not_exist.json"
    before = sorted(p.name for p in tmp_path.iterdir())
    argv = sys.argv
    sys.argv = ["k5_promotion_gate_explain.py", "--db", str(db),
                "--repo-root", str(tmp_path), "--print-only",
                "--doc-out", str(doc), "--json-out", str(json_out)]
    try:
        code = k5e.main()
    finally:
        sys.argv = argv
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["explain"] == "k5_promotion_gate_explain"
    assert not doc.exists() and not json_out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_doc_write_refreshes_machine_region_only(tmp_path):
    """默认写文档：机器区被刷新，标记之外的**人工叙述段原样保留**
    （复跑不抹掉人写的段——否则主控复跑会把叙述清空）。"""
    db = _mk_synth(tmp_path, [dict(id="ESV2-A", key="A")])
    doc = tmp_path / "report.md"
    doc.write_text(
        "# 标题（人写）\n\n纪律段（人写，只读不裁定）\n\n"
        f"{k5e.DOC_BEGIN}\n旧机器内容\n{k5e.DOC_END}\n\n尾注（人写）\n",
        encoding="utf-8")
    region = k5e.render_doc(_explain(db), {})
    action = k5e.write_doc(doc, region)
    text = doc.read_text(encoding="utf-8")
    assert action == "machine-region-refreshed"
    assert text.startswith("# 标题（人写）")
    assert "纪律段（人写，只读不裁定）" in text
    assert "尾注（人写）" in text
    assert "旧机器内容" not in text
    assert "逐策略逐级判词" in text
    # 幂等：同内容复跑不再变
    before = text
    k5e.write_doc(doc, k5e.render_doc(_explain(db), {}))
    again = doc.read_text(encoding="utf-8")
    assert again.split("<!-- 本段由")[-1] == before.split("<!-- 本段由")[-1]
