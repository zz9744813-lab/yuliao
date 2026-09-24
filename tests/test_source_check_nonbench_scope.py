"""source_check `nonbench`（K2 非基准试点供给池）范围回归 + `--work-id` 收窄。

钉住的事（任务书 2026-09-24）：
① scope=nonbench 的选中集合 = 夹具库里合规非基准段（`role != 'benchmark'`
   ——NULL 也算非基准——**且**所属作品在 work_sources 登记为合规人类语料
   **且** text_clean 非空），并排除 fixture 源段与 benchmark 段两条反例；
② `--work-id` 只收窄不放宽：给不合规/不存在的 work_id 结果是「空集 + 不报错」，
   绝不借它扩宽到不合规来源；
③ 合规来源判据**单源漂移即红**：直读 scripts/k2_extract_backfill.py 源码
   字面量比对（照抄 tests/test_k2_pairs_gen.py 的漂移断言写法）；
④ used/all-frames/bench 三档回归不变：对照改造前的 targets() 语义，断言 id
   集合逐字相等。

纪律：全部离线（conftest 临时 sqlite + mock LLM），零网络、零密钥读取、
不跑 --run、不碰真库。
"""
from __future__ import annotations

import ast
import importlib.util as _u
import json
import re
import sys
from pathlib import Path

import pytest

from sqlalchemy import or_ as _or

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import source_check as sc                                    # noqa: E402
from app import db                                           # noqa: E402
from app.models import (Candidate, ControlledCorruption,     # noqa: E402
                        Experiment, Frame, Segment, Work, WorkSource)

TEXT = "他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"


def _seed_work(s, *, source_type=None, register=True, label="t-scnb") -> str:
    """一部作品。register=True 时登记 WorkSource（source_type 列 NOT NULL，
    故 register=False 才表达「有作品无登记行」。）"""
    w = Work(title=label, source="test:source_check_nonbench")
    s.add(w)
    s.flush()
    if register:
        s.add(WorkSource(work_id=w.id, canonical_work_id=w.id,
                         source_type=source_type, text_version="corpus-v1",
                         purpose_basis="test", identity_purposes=["research"],
                         license_purposes=[], license_basis="test",
                         metadata_status="verified", metadata_basis="test"))
        s.flush()
    return w.id


def _seed_seg(s, wid, *, role=None, text_clean=TEXT, integrity="{}") -> str:
    seg = Segment(work_id=wid, ordinal=0, text=TEXT, text_clean=text_clean,
                  role=role, n_sentences=1, n_chars=len(TEXT),
                  integrity=integrity)
    s.add(seg)
    s.flush()
    return seg.id


# ── 改造前 targets() 语义的独立参考实现（尺子；与本实现共用 DB 上的当前状态）──

def _legacy_targets_ids(scope: str) -> set[str]:
    """改动前的 targets() 语义（钉：used/all-frames/bench 任何漂移即红）。"""
    with db.session() as s:
        if scope == "used":
            ids = {r[0] for r in s.query(Candidate.segment_id).distinct()}
            ids |= {r[0] for r in s.query(ControlledCorruption.segment_id).distinct()}
        elif scope == "all-frames":
            ids = {r[0] for r in s.query(Frame.segment_id).filter(
                Frame.granularity == "L").distinct()}
        else:                                    # bench（改动前 `else` 分支）
            ids = {r[0] for r in s.query(Segment.id).filter(
                Segment.role == "benchmark")}
        segs = s.query(Segment).filter(Segment.id.in_(ids)).all()
    out = set()
    for x in segs:
        try:
            have = json.loads(x.integrity or "{}")
        except Exception:                                    # noqa: BLE001
            have = {}
        if not isinstance(have, dict):
            have = {}
        if sc.needs_check(have):
            out.add(x.id)
    return out


def _ref_nonbench_ids() -> set[str]:
    """nonbench 判据的独立参考实现（同一 predicate、同一 needs_check 幂等门）。"""
    with db.session() as s:
        reg = {ws.work_id: ws for ws in s.query(WorkSource).all()}
        compliant = {wid for wid, ws in reg.items()
                     if sc.k2b.nonbenchmark_compliant_source(ws.source_type)}
        cand = s.query(Segment.id, Segment.text_clean, Segment.integrity).filter(
            Segment.work_id.in_(compliant),
            _or(Segment.role.is_(None), Segment.role != "benchmark")).all()
    out = set()
    for sid, tc, integ in cand:
        if not (tc or "").strip():
            continue
        try:
            have = json.loads(integ or "{}")
        except Exception:                                    # noqa: BLE001
            have = {}
        if not isinstance(have, dict):
            have = {}
        if sc.needs_check(have):
            out.add(sid)
    return out


# ── ① nonbench 选中集合 + 反例 ────────────────────────────────────────

def test_nonbench_selects_compliant_and_excludes_counterexamples():
    db.init_db()
    with db.session() as s:
        wid_h = _seed_work(s, source_type="human_fiction", label="t-nb-human")
        s_train = _seed_seg(s, wid_h, role="train")
        s_null = _seed_seg(s, wid_h, role=None)
        s_h_bench = _seed_seg(s, wid_h, role="benchmark")
        s_h_nocl = _seed_seg(s, wid_h, role="train", text_clean="")
        s_h_ws = _seed_seg(s, wid_h, role="train", text_clean="   ")
        s_h_skip = _seed_seg(s, wid_h, role="train",
                             integrity='{"src_ok": true}')
        wid_pb = _seed_work(s, source_type="production_nonbenchmark_k2v2",
                            label="t-nb-pb")
        s_pb = _seed_seg(s, wid_pb, role="train")
        wid_fx = _seed_work(s, source_type="fixture", label="t-nb-fx")
        s_fx = _seed_seg(s, wid_fx, role="train")
        wid_noreg = _seed_work(s, register=False, label="t-nb-noreg")
        s_noreg = _seed_seg(s, wid_noreg, role="train")
        s.commit()
    todo_ids = {x[0] for x in sc.targets("nonbench")}
    # 选中集合 = 夹具库里全部「合规人类源 + 非基准 + text_clean 非空 + 需检查」
    assert todo_ids == _ref_nonbench_ids()
    # 正面：合规人类源的非基准段（train 与 NULL）都进——判据是「不等于 benchmark」
    assert {s_train, s_null, s_pb} <= todo_ids
    # 反例一：fixture 源段（role!=benchmark 且 text_clean 非空）被来源门排除
    assert s_fx not in todo_ids
    # 反例二：benchmark 段（来源与 clean 都合规）被 role 门排除
    assert s_h_bench not in todo_ids
    # text_clean 空/纯空白、未登记来源、严格布尔已检查段 一律在 todo 之外
    assert s_h_nocl not in todo_ids
    assert s_h_ws not in todo_ids
    assert s_noreg not in todo_ids
    assert s_h_skip not in todo_ids
    # 选中集的 role 恒非 benchmark，来源恒合规（结构自检）
    with db.session() as s:
        rows = s.query(Segment).filter(Segment.id.in_(todo_ids)).all()
        ws = {x.work_id: x for x in s.query(WorkSource).all()}
    assert all((r.role or "") != "benchmark" for r in rows)
    assert all(sc.k2b.nonbenchmark_compliant_source(ws[r.work_id].source_type)
               for r in rows)


# ── ② --work-id 只收窄，绝不扩宽 ──────────────────────────────────────

def test_work_id_only_narrows_never_widens():
    db.init_db()
    with db.session() as s:
        wid_h = _seed_work(s, source_type="human_fiction", label="t-wid-h")
        sid_h1 = _seed_seg(s, wid_h, role="train")
        sid_h2 = _seed_seg(s, wid_h, role="train")
        sid_bench = _seed_seg(s, wid_h, role="benchmark")
        wid_fx = _seed_work(s, source_type="fixture", label="t-wid-fx")
        sid_fx = _seed_seg(s, wid_fx, role="train")
        s.commit()
    full = {x[0] for x in sc.targets("nonbench")}
    assert {sid_h1, sid_h2} <= full
    assert sid_fx not in full
    # 收窄到合规作品 = 该作品内的非基准合规段（⊆ 全集）
    narrowed = {x[0] for x in sc.targets("nonbench", work_ids=[wid_h])}
    assert narrowed == {sid_h1, sid_h2}
    assert narrowed <= full
    # 不合规 work_id：空集 + 不报错 + 不放行（fixture 段绝不因指定 work-id 进池）
    assert sc.targets("nonbench", work_ids=[wid_fx]) == []
    assert sc.targets("nonbench", work_ids=["WK-does-not-exist"]) == []
    # 合规 + 不合规 混合：仍只留合规作品段与 scope 选取集的交集
    mixed = {x[0] for x in sc.targets("nonbench", work_ids=[wid_h, wid_fx])}
    assert mixed == {sid_h1, sid_h2}
    # 对旧三档同样只收窄：bench 收窄到无基准段的作品 = 空集
    assert sc.targets("bench", work_ids=[wid_fx]) == []
    assert {x[0] for x in sc.targets("bench", work_ids=[wid_h])} == {sid_bench}


def test_parse_work_ids_repeatable_and_comma():
    assert sc.parse_work_ids([]) == []
    assert sc.parse_work_ids(None) == []
    assert sc.parse_work_ids(["WK-a", "WK-b,WK-c ", ",WK-a,"]) == \
        ["WK-a", "WK-b", "WK-c"]


# ── ③ 合规来源判据单源漂移即红 ────────────────────────────────────────

def test_nonbench_compliance_single_source_no_drift():
    """直读 scripts/k2_extract_backfill.py 源码字面量比对（照抄
    tests/test_k2_pairs_gen.py 的漂移断言写法）：改一边不改另一边即红。"""
    src = (ROOT / "scripts" / "k2_extract_backfill.py").read_text(
        encoding="utf-8")
    m_t = re.search(
        r"^NONBENCHMARK_SOURCE_TYPES\s*=\s*frozenset\((\{[^}]*\})\)",
        src, re.M)
    m_p = re.search(
        r'^NONBENCHMARK_SOURCE_TYPE_PREFIX\s*=\s*["\']([^"\']*)["\']',
        src, re.M)
    assert m_t and m_p, "k2_extract_backfill 常量声明形态变了——两边同步核查"
    # 源码字面量 ↔ 运行时常量（同一来源的两种读法，改任何一边即红）
    assert set(ast.literal_eval(m_t.group(1))) == \
        set(sc.k2b.NONBENCHMARK_SOURCE_TYPES), "来源类型白名单漂移"
    assert m_p.group(1) == sc.k2b.NONBENCHMARK_SOURCE_TYPE_PREFIX, \
        "合规前缀漂移"
    # 再把白名单钉到 K2 试点文档契约的规范值上（改 backfill 白名单即红）
    assert set(ast.literal_eval(m_t.group(1))) == {"human_fiction"}
    assert m_p.group(1) == "production_nonbenchmark_"
    # 判定入口唯一：source_check 直接复用 backfill 的函数本体，不另写一套
    assert sc.k2b.nonbenchmark_compliant_source.__module__ == \
        "k2_extract_backfill"
    assert sc.k2b.nonbenchmark_compliant_source("fixture") is False
    assert sc.k2b.nonbenchmark_compliant_source("human_fiction") is True
    assert sc.k2b.nonbenchmark_compliant_source(
        "production_nonbenchmark_x") is True
    # source_check 本体不得出现第二套常量/前缀定义（单源：第二处即红）
    sc_src = (ROOT / "scripts" / "source_check.py").read_text(
        encoding="utf-8")
    assert "NONBENCHMARK_SOURCE_TYPES" not in sc_src
    assert "NONBENCHMARK_SOURCE_TYPE_PREFIX" not in sc_src


# ── ④ used/all-frames/bench 三档回归不变 ───────────────────────────────

def test_three_legacy_scopes_verbatim():
    db.init_db()
    with db.session() as s:
        wid_h = _seed_work(s, source_type="human_fiction", label="t-byte-h")
        sid_bench = _seed_seg(s, wid_h, role="benchmark", integrity="{}")
        sid_train = _seed_seg(s, wid_h, role="train", integrity="{}")
        sid_ok = _seed_seg(s, wid_h, role="benchmark",
                           integrity='{"src_ok": true}')     # 已检查 → 一律跳过
        wid_fx = _seed_work(s, source_type="fixture", label="t-byte-fx")
        sid_fx = _seed_seg(s, wid_fx, role="train", integrity="{}")
        exp = Experiment(id="EXP-BYT", name="t", config={}, status="done")
        s.add(exp)
        s.flush()
        fr_l = Frame(experiment_id=exp.id, segment_id=sid_fx, granularity="L",
                     extractor_model="m", prompt_version="pv")
        s.add(fr_l)
        fr_m = Frame(experiment_id=exp.id, segment_id=sid_train, granularity="M",
                     extractor_model="m", prompt_version="pv")
        s.add(fr_m)
        s.flush()
        for sid in (sid_bench, sid_fx):
            s.add(Candidate(experiment_id=exp.id, frame_id=fr_l.id,
                            segment_id=sid, anon_label="X17", model="m",
                            temperature=0.8, prompt_version="pv", text="x",
                            status="ok"))
        s.add(ControlledCorruption(experiment_id=exp.id, segment_id=sid_train,
                                   corruption_type="test", text="x"))
        s.commit()
    for scope in ("used", "all-frames", "bench"):
        got = {x[0] for x in sc.targets(scope)}
        assert got == _legacy_targets_ids(scope), \
            f"scope={scope} 与改动前 targets() 语义不再逐项相等——口径漂移"
    # 点对点核对（与参考实现独立地钉语义；共享库上只断包含/排除，不断全库相等）
    used = {x[0] for x in sc.targets("used")}
    assert {sid_bench, sid_fx, sid_train} <= used
    assert sid_ok not in used
    af = {x[0] for x in sc.targets("all-frames")}
    assert sid_fx in af and sid_train not in af and sid_ok not in af
    bench = {x[0] for x in sc.targets("bench")}
    assert sid_bench in bench
    assert sid_train not in bench and sid_ok not in bench


# ── CLI 接线：--scope nonbench / --work-id 传到 run，help 写清语义 ──────

def test_main_cli_wires_nonbench_and_work_id(monkeypatch):
    seen = {}

    def _spy_run(scope="used", conc=8, limit=0, ids=None, exp_id=None,
                 work_ids=None):
        seen.update(scope=scope, conc=conc, limit=limit, work_ids=work_ids)
        return {"aborted": False}
    monkeypatch.setattr(sc, "run", _spy_run)
    monkeypatch.setattr(sys, "argv",
                        ["sc", "--run", "--scope", "nonbench",
                         "--work-id", "WK-dc90993434e9", "--conc", "4"])
    sc.main()
    assert seen["scope"] == "nonbench"
    assert seen["work_ids"] == ["WK-dc90993434e9"]
    assert seen["conc"] == 4
    # 逗号分隔 + 重复参数都展开后传入
    monkeypatch.setattr(sys, "argv",
                        ["sc", "--run", "--scope", "bench",
                         "--work-id", "WK-a,WK-b", "--work-id", "WK-b"])
    seen.clear()
    sc.main()
    assert seen["scope"] == "bench"
    assert seen["work_ids"] == ["WK-a", "WK-b"]


def test_cli_scope_choices_and_help_write_semantics(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sc", "--run", "--scope", "whatever"])
    with pytest.raises(SystemExit) as e:
        sc.main()
    assert e.value.code != 0, "非法 scope 必须被 argparse 拒"
    monkeypatch.setattr(sys, "argv", ["sc", "--help"])
    with pytest.raises(SystemExit) as e0:
        sc.main()
    assert e0.value.code == 0
    flat = "".join(capsys.readouterr().out.split())
    # nonbench 语义必须写死在 help：判据/来源白名单/单源入口/fail-closed
    assert "nonbench" in flat
    assert "不等于" in flat and "NULL" in flat
    assert "human_fiction" in flat and "production_nonbenchmark_" in flat
    assert "text_clean" in flat
    assert "nonbenchmark_compliant_source" in flat
    assert "fail-closed" in flat
    # --work-id 只收窄的语义写死在 help
    assert "WK" in flat and "收窄" in flat and "⊆" in flat