"""K2 复审「预算墙逐条提交」回归（scripts/k2_rereview_queue.py::_execute_rereview）。

钉住的原缺陷（基线 30b7533 的写法）：

    with db.session() as s:
        for item in plan:
            r = KE.extract_segment(..., budget=budget, live=True, ...)
            inst.reviewer_version = REVIEW_MARKER
        s.commit()                      # ← 整批末尾单次提交

`KE.ExtractBudget` 是「一次 run」的闸（默认 max_calls=20）：plan 长于闸时，
第 21 条必抛 `KE.ExtractBudgetExceeded` ⇒ 异常穿出 `with` ⇒ 会话回滚
⇒ 前 20 条**已完成的复审连同样本调用一起丢**（白烧），
且 `--limit > 20` 在该实现下永远不可能成功。

本文件把这条洞锁死（全离线：假 extractor + 测试专用 SQLite，
不联网、不调 LLM、不碰真库）。闸缩到 max_calls=3，plan 给 5 条，
复现「plan 长度 > 预算上限」的同构场景。六个钉：
① 预算墙**不再穿出**：撞墙按「本轮到此为止」优雅收口，返回
   `stopped_by_budget=True` / `budget_note` / `remaining_in_plan`；
② 撞墙前已完成的条目**真的在库里**（独立 session 读回），未跑到的仍为空；
③ 持久性**逐条**成立：第 k 次 extractor 被调用时，前 k-1 条已在库里
   ——同时锁死「整批末尾单次提交」和「撞墙时才补一枪」两种回退写法；
④ 非预算异常（网关抖动）同样不许把已提交的条目回滚掉；
⑤ plan ≤ 预算闸时照常全跑，`stopped_by_budget is False`；
⑥ 复审只写 reviewer_version：status / span / evidence 一行不动（审计明令）。

反向验证（任务书要求）：把 `_execute_rereview` 的逐条 `s.commit()` 变异回
「整批末尾单次提交」，本文件必须转红；恢复后转绿。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k2rq_budget", ROOT / "scripts" / "k2_rereview_queue.py")
k2rq = _u.module_from_spec(_spec)
sys.modules["k2rq_budget"] = k2rq
_spec.loader.exec_module(k2rq)

from app import db, knowledge_extract as KE                # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,     # noqa: E402
                        StrategyInstance, Work)

REVIEW_MARKER = KE.REVIEW_MARKER_NEW_DEF   # 单源口径，不本地重定义
TEXT = "他站着没说话，灯花跳了一下。半晌他把茶盏搁回去，慢慢开口说了正事。"
SPAN = (0, 8)                              # 夹具内所有实例的 span 口径
MAX_CALLS = 3                              # 本文件的「一次 run」预算闸
_n = [0]


def _seed(n_items: int):
    """独立夹具：1 作品 + 1 定义齐备策略 + n_items 条 reviewer_version 为空的
    待复审实例。plan 口径与 enumerate_queue 的 queue 条目逐字同构。"""
    _n[0] += 1
    key = f"k2rb-{_n[0]}"
    db.init_db()
    plan = []
    with db.session() as s:
        w = Work(title=f"t-k2rb-{key}", source=f"test:{key}")
        s.add(w)
        s.flush()
        st = ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            invariants=["保留短句"], effect_hypothesis=f"假设-{key}",
            failure_modes=["堆修饰"], status="hypothesis",
            scope="WORK", scope_ids=[w.id])
        s.add(st)
        s.flush()
        for i in range(n_items):
            seg = Segment(work_id=w.id, ordinal=i, text=TEXT, text_clean=TEXT,
                          role="benchmark", n_chars=len(TEXT), n_sentences=1,
                          integrity='{"src_ok": true}')
            s.add(seg)
            s.flush()
            ev = TEXT[SPAN[0]:SPAN[1]]
            inst = StrategyInstance(
                strategy_id=st.id, strategy_version=st.version,
                work_id=w.id, segment_id=seg.id, text_version=f"tv-{key}",
                span_start=SPAN[0], span_end=SPAN[1], evidence_text=ev,
                evidence_sha256=hashlib.sha256(ev.encode()).hexdigest(),
                observed_content="克制沉默", extractor_model="fx",
                reviewer_version="", status="verified")
            s.add(inst)
            s.flush()
            plan.append({"instance_id": inst.id, "strategy_id": st.id,
                         "strategy_key": key, "strategy_version": st.version,
                         "segment_id": seg.id, "work_id": w.id,
                         "text_version": f"tv-{key}", "status": "verified"})
        s.commit()
    return key, plan


def _markers(plan):
    """独立 session 读回：只看库里有什么，不看任何内存态。"""
    ids = [p["instance_id"] for p in plan]
    with db.session() as s:
        rows = (s.query(StrategyInstance)
                .filter(StrategyInstance.id.in_(ids)).all())
        return {r.id: r.reviewer_version for r in rows}


def _done(plan):
    return {iid for iid, rv in _markers(plan).items() if rv == REVIEW_MARKER}


def _install(monkeypatch, plan, probe, *, boom_at=None):
    """假 extractor：闸用**真** `budget.check()`（撞墙即抛真
    ExtractBudgetExceeded，不另写口径）；每次被叫到先记录
    「此刻库里已落几条」+「strategy_def 是否带上新口径定义正文」。
    boom_at=非预算异常注入点（第 boom_at 次起抛 RuntimeError）。"""
    real_budget = KE.ExtractBudget
    monkeypatch.setattr(KE, "ExtractBudget",
                        lambda: real_budget(max_calls=MAX_CALLS))

    def _fake(client, *, strategy_id, strategy_version, work_id, segment_id,
              text, text_version, budget, live=False, strategy_def=None,
              **kw):
        probe.append({
            "committed_before_call": len(_done(plan)),
            "strategy_def_ok": bool(strategy_def and (
                strategy_def.get("abstract_operation") or "").strip()),
        })
        budget.check()
        if boom_at is not None and budget.calls >= boom_at:
            raise RuntimeError("网关抖动（与预算无关）")
        return {"status": "verified", "span_start": SPAN[0],
                "span_end": SPAN[1], "evidence_text": text[SPAN[0]:SPAN[1]],
                "observed_content": "克制沉默", "extractor_model": "fx",
                "text_version": text_version,
                "reviewer_version": REVIEW_MARKER}

    monkeypatch.setattr(KE, "extract_segment", _fake)


def _call(plan):
    """跑一次 _execute_rereview，把异常收进返回值（不让它掀掉后面的断言）：
    返回 (result|None, exception|None)。"""
    try:
        return k2rq._execute_rereview(plan, "fx-model"), None
    except Exception as exc:                      # noqa: BLE001 — 就是要接住
        return None, exc


def test_budget_wall_does_not_escape_and_keeps_completed(monkeypatch):
    """钉①+②：plan(5) > 闸(3) ⇒ 第 4 次撞墙。异常必须不穿出（旧写法穿出
    ⇒ 整批回滚 ⇒ 库里 0 条=白烧）；返回值如实报预算墙；已完成的 3 条逐条
    落库、未跑到的 2 条仍为空。"""
    key, plan = _seed(5)
    probe: list = []
    _install(monkeypatch, plan, probe)
    assert _done(plan) == set(), "夹具态：开局一条都没复审"

    ret, exc = _call(plan)
    assert exc is None, (
        f"预算墙穿出 with 会话=整批回滚（原缺陷复发）：{type(exc).__name__}: {exc}")
    assert ret["mode"] == "live"
    assert ret["n_rereviewed"] == MAX_CALLS, "撞墙前的 3 条必须算已完成"
    assert ret["stopped_by_budget"] is True, "撞墙必须如实上报，不许静默"
    assert ret["remaining_in_plan"] == len(plan) - MAX_CALLS
    assert "预算" in ret["budget_note"], ret["budget_note"]
    assert ret["budget"]["calls"] == MAX_CALLS
    assert ret["budget"]["max_calls"] == MAX_CALLS
    assert [r["instance_id"] for r in ret["results"]] \
        == [p["instance_id"] for p in plan[:MAX_CALLS]], "results 只含已提交的条目"

    marks = _markers(plan)
    assert {i for i, v in marks.items() if v == REVIEW_MARKER} \
        == {p["instance_id"] for p in plan[:MAX_CALLS]}, \
        "已完成的复审必须真在库里（旧写法这里会是 0 条——整批回滚白烧）"
    assert all(marks[p["instance_id"]] == "" for p in plan[MAX_CALLS:]), \
        "没跑到的条目不许被顺手标记"


def test_each_completed_item_is_durable_before_next_call(monkeypatch):
    """钉③：第 k 次 extractor 被调用时，前 k-1 条已在库里 ⇒ 提交是**逐条**
    发生的。回退成「整批末尾单次提交」或「撞墙时才补一次提交」，探针都会
    退化成 [0,0,0,0] ⇒ 本用例转红。"""
    key, plan = _seed(5)
    probe: list = []
    _install(monkeypatch, plan, probe)
    _call(plan)
    assert [p["committed_before_call"] for p in probe] == [0, 1, 2, 3], \
        f"必须逐条提交（实测探针 {probe}）"
    assert len(probe) == MAX_CALLS + 1, "闸=3 ⇒ 第 4 次调用被拒（探针含被拒那次）"
    assert all(p["strategy_def_ok"] for p in probe), \
        "复审必须按带 strategy_def 的新口径发起（审计 P1 第 3 条机制侧）"


def test_non_budget_exception_keeps_already_committed(monkeypatch):
    """钉④：与预算无关的异常（网关抖动）同样不许把此前已提交的条目回滚——
    逐条提交的意义不止于预算墙。"""
    key, plan = _seed(4)
    probe: list = []
    _install(monkeypatch, plan, probe, boom_at=2)   # 第 2 次抛非预算异常
    ret, exc = _call(plan)
    assert ret is None and isinstance(exc, RuntimeError), \
        "非预算异常仍须如实上抛（不许顺手吞掉一切失败）"
    assert "网关抖动" in str(exc)
    assert _done(plan) == {plan[0]["instance_id"]}, \
        "第 1 条已提交就必须活下来（旧写法：整批回滚，0 条存活=白烧）"


def test_plan_within_budget_runs_all_and_reports_no_stop(monkeypatch):
    """钉⑤：plan(2) ≤ 闸(3) ⇒ 全跑完：stopped_by_budget=False、
    remaining_in_plan=0、库里 2 条都在（锁住「预算够用路径」不被改坏）。"""
    key, plan = _seed(2)
    probe: list = []
    _install(monkeypatch, plan, probe)
    ret, exc = _call(plan)
    assert exc is None, f"预算够用却抛出：{exc}"
    assert ret["n_rereviewed"] == 2
    assert ret["stopped_by_budget"] is False
    assert ret["remaining_in_plan"] == 0
    assert ret["budget"]["calls"] == 2 < ret["budget"]["max_calls"]
    assert _done(plan) == {p["instance_id"] for p in plan}
    assert [p["committed_before_call"] for p in probe] == [0, 1]


def test_rereview_writes_marker_only(monkeypatch):
    """钉⑥：复审只动 reviewer_version——status / span / evidence_text /
    extractor_model 一行都不许改（审计明令「不许用批量改 status 冒充验收」）。"""
    key, plan = _seed(2)
    probe: list = []
    _install(monkeypatch, plan, probe)
    _call(plan)
    ids = [p["instance_id"] for p in plan]
    with db.session() as s:
        rows = (s.query(StrategyInstance)
                .filter(StrategyInstance.id.in_(ids)).all())
        assert len(rows) == 2
        for r in rows:
            assert r.reviewer_version == REVIEW_MARKER
            assert r.status == "verified", "复审不改 status"
            assert (r.span_start, r.span_end) == SPAN, "复审不改 span"
            assert r.evidence_text == TEXT[SPAN[0]:SPAN[1]], "复审不改存证文本"
            assert r.extractor_model == "fx", "复审不改抽取模型位"
