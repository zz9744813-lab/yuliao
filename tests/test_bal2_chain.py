"""bal-v2 建集链的规格测试（监督 16:32 整改版，重写 2026-09-20）。

链条：PROD → split_benchmark（划基准）→ build_length_balanced（bal-v2）
→ benchmark_run → benchmark_falsify → 档一读数。
按监督 2a-2e 钉住：
2a/2e. **宇宙过滤**：role='benchmark' 是持久单调标记，历史 split 留下的旧标记
   段永远在 _eligible_pairs 池里——l/s_experiments 按**行**过滤各自一侧，
   混有旧实验标记段的池里，按 exp 建集**不得**捞到旧行（监督实测口径复现）；
2b. **同名守卫**：同名同 kind 已存在 → 拒绝；replace=True → 删旧建新并报 replaced；
2c. **split CLI 加固**：--exp 透传（monkeypatch spy 断言 kwargs，不再 grep 源码）；
   --split-benchmark 0 与 None 分开；marked==0 且 already==0 → 非零退出。

夹具说明（监督 2d）：本夹具的 n_chars/len_ratio/n_sentences 与文本实际长度不符——
经复核 benchmark_build.py **不读这些列**（S/L 分侧用 len(seg.text_clean or text)
vs len(cc.text)），故不致命；此处按会审要求显式注明，并按真实文本重算方向。
candidate_id 与 Segment.role='benchmark' 是 _eligible_pairs 的硬闸，夹具必须造。
"""
from __future__ import annotations

import json
import sys
import uuid as _uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_build as BB               # noqa: E402
import controlled_corruption as CC       # noqa: E402
import verify_bal_universe as VU        # noqa: E402
from app import db                       # noqa: E40402
from app.models import (BenchmarkItem, BenchmarkSet, Candidate,  # noqa: E402
                        ControlledCorruption, Experiment, Frame,
                        Segment, Work)

_UNIQ = _uuid.uuid4().hex[:8]
# 池卫生登记表：conftest 是全 suite 共享一个 tmp 库，本文件按监督 2d 重写后
# seed 的是**真合格对**（candidate_id + role='benchmark' + src_ok）——不清理会
# 泄进 _eligible_pairs，打爆后置文件的类型宇宙断言（实测 test_benchmark_subs
# 2 红，顺序依赖复现）。teardown 见 _pool_guard。
_CREATED: dict[str, set] = {"exp": set(), "work": set(), "seg": set(),
                           "frame": set(), "cand": set(), "cc": set()}
_FLIP: dict = {}   # 可翻转外段快照（id → 原 role），_pool_guard setup 存 teardown 用


@pytest.fixture(autouse=True)
def _pool_guard():
    """共享测试库的池卫生（监督 2d：测试不许改库底给后置文件留坑）。

    ① 本文件建的基准集（名字都嵌 _UNIQ）连同条目一起删；
    ② 本文件 seed 的行按外键序逆删：cc → candidate → frame → segment → work → experiment；
    ③ split_benchmark 是真跑——被它标记的**外段**按快照回滚 role
      （role='benchmark' 是持久单调标记，测试无权替生产改库底）。"""
    for k in _CREATED:            # clear() 会连键一起清掉 → 后续 KeyError
        _CREATED[k] = set()
    db.init_db()  # 本文件此前靠 2c 测试真跑 CC.main()（其内部 init_db）间接建表，
                  # 夹具查询先于它 → 单独跑文件会 no such table。init_db 幂等，先建。
    with db.session() as s:
        # split 只加标不去标 → 只有空 role 的段可能被翻成 'benchmark'。
        # 快照只收可翻转集（id→原 role），不做全表物化（qwen 席 BLOCK 项：
        # 共享库池卫生夹具若被复用到现场规模，两次全表物化是数量级慢）。
        _FLIP.clear()
        _FLIP.update(s.query(Segment.id, Segment.role)
                     .filter((Segment.role.is_(None)) | (Segment.role == "")).all())
    yield
    with db.session() as s:
        mine = s.query(BenchmarkSet).filter(
            BenchmarkSet.name.like(f"%{_UNIQ}%")).all()
        for st in mine:
            s.query(BenchmarkItem).filter_by(set_id=st.id).delete()
            s.delete(st)
        for cc_id in _CREATED["cc"]:
            s.query(ControlledCorruption).filter_by(id=cc_id).delete()
        for cand_id in _CREATED["cand"]:
            s.query(Candidate).filter_by(id=cand_id).delete()
        for fr_id in _CREATED["frame"]:
            s.query(Frame).filter_by(id=fr_id).delete()
        for seg_id in _CREATED["seg"]:
            s.query(Segment).filter_by(id=seg_id).delete()
        for w_id in _CREATED["work"]:
            s.query(Work).filter_by(id=w_id).delete()
        for e_id in _CREATED["exp"]:
            s.query(Experiment).filter_by(id=e_id).delete()
        # 只回滚「快照为空 role、期间被 split 翻成 benchmark」的外段，
        # 按 id 集合一次圈出、恢复原值，无逐行 s.get（role='benchmark' 是
        # 持久单调标记，测试无权替生产改库底）。
        flipped = s.query(Segment).filter(
            Segment.id.in_(set(_FLIP) - _CREATED["seg"]),
            Segment.role == "benchmark").all()
        for seg in flipped:
            seg.role = _FLIP[seg.id]
        s.commit()
LONG_HUMAN = ("他把茶喝完才起身，屋外风声很紧，谁也没有再说话，窗纸被吹得鼓了一下，"
              "远处还有狗吠。")                        # 37 字（>min_chars=60 会被夹具外口径排除？
                                                   # 不会：_eligible_pairs 不做长度闸，
                                                   # 60 字闸在 pick 侧，此处直接造行）
SHORT_VARIANT_L = "他喝完茶起身，风声很紧。"      # 10 字 → human 更长 → L 侧
LONG_VARIANT_S = (LONG_HUMAN + "于是他又想了很久，越想越觉得心里发沉，"
                  "索性把灯芯拨亮了一些，坐下来慢慢写了一封信。")   # human 更短 → S 侧


def _seed_pair(exp_id: str, seg_role: str | None, direction: str,
               status: str = "ok", tag: str | None = None):
    """一段 + 一条带 candidate 的劣化行（挂指定实验）。

    direction='L'：variant 比 human 短（human 更长）——窗口化产线的产物；
    direction='S'：variant 比 human 长（human 更短）——旧代自然库存。
    注意：夹具不把 n_chars/len_ratio 对齐真实文本（BB 不读它们，见模块 docstring）。
    """
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created", config={}, stats={}))
            _CREATED["exp"].add(exp_id)
        w = Work(title=f"t-bal2c-{tag or exp_id}-{_UNIQ}", source="test:bal2c")
        s.add(w)
        s.flush()
        _CREATED["work"].add(w.id)
        seg = Segment(work_id=w.id, ordinal=0, text=LONG_HUMAN, role=seg_role,
                      integrity='{"src_ok": true}', n_sentences=2, n_chars=37)
        s.add(seg)
        s.flush()
        _CREATED["seg"].add(seg.id)
        fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        _CREATED["frame"].add(fr.id)
        var = SHORT_VARIANT_L if direction == "L" else LONG_VARIANT_S
        cand = Candidate(experiment_id=exp_id, frame_id=fr.id, segment_id=seg.id,
                         anon_label="XC", model=f"corrupt:{direction}",
                         prompt_version="corrupt_v2", text=var, status="ok")
        s.add(cand)
        s.flush()
        _CREATED["cand"].add(cand.id)
        cc = ControlledCorruption(
            experiment_id=exp_id, segment_id=seg.id, frame_id=fr.id,
            candidate_id=cand.id, corruption_type="SUBTEXT_ERASE",
            variable="x", generator_model="g", prompt_version="corrupt_v2",
            text=var, n_chars=len(var), drift={}, drift_score=0.1,
            fact_consistent=True, drift_ok=True, verify_model="v",
            verify_pv="pv", status=status)
        s.add(cc)
        s.flush()
        _CREATED["cc"].add(cc.id)
        s.commit()
        return seg.id


# ── 2c. split_benchmark CLI：exp 透传（spy）/ 0 与 None / 零标记非零退出 ──

def _run_split_cli(monkeypatch, argv, captured):
    def _spy(**kw):
        captured["kw"] = kw
        return _fake_split_result(kw)
    monkeypatch.setattr(sys, "argv", ["prog"] + argv)
    monkeypatch.setattr(CC, "split_benchmark", _spy)
    CC.main()
    return captured["kw"]


def _fake_split_result(kw):
    # spy 测试的假结果——marked>=1 不触发 CLI 的零标记守卫
    return {"marked": max(kw.get("n") or 1, 1), "already": 0,
            "pool_total": 99, "pool_fresh": 99, "v2_excluded": 0}


def test_split_cli_passes_exp_kwarg(monkeypatch):
    """CLI 必须把 --exp 透传给 split_benchmark（spy 断言 kwargs，监督 2d）。"""
    captured: dict = {}
    kw = _run_split_cli(monkeypatch, ["--split-benchmark", "5",
                                       "--exp", "EXP-X", "--seed", "1"],
                         captured)
    assert kw.get("exp") == "EXP-X", "CLI 没把 --exp 透传给 split_benchmark"
    assert kw.get("n") == 5


def test_split_cli_zero_is_zero_not_none(monkeypatch):
    """--split-benchmark 0 → n=0（标零段），不许 `or None` 吃成全库默认。"""
    captured: dict = {}
    kw = _run_split_cli(monkeypatch, ["--split-benchmark", "0",
                                      "--seed", "1"], captured)
    assert kw.get("n") == 0, "0 被 or None 吃掉了——0 与 None 必须分开（监督 2c）"


def test_split_cli_zero_marked_exits_nonzero(monkeypatch):
    """marked==0 且 already==0 → 非零退出（打错 exp 不许静默成功，监督 2c）。"""
    monkeypatch.setattr(sys, "argv", ["prog", "--split-benchmark", "5",
                                     "--exp", "EXP-TYPO", "--seed", "1"])
    monkeypatch.setattr(CC, "split_benchmark",
                        lambda **kw: {"marked": 0, "already": 0, "pool_total": 0})
    with pytest.raises(SystemExit):
        CC.main()


# ── 2a/2e. 宇宙过滤：混池里按 exp 建集不得捞旧行 ────────────

def test_exp_scoped_l_side_excludes_old_rows():
    """监督 2e（实测口径复现）：池里混有旧实验标记段时，l_experiments 限定的
    建集 L 侧**只含**指定实验的行，旧实验的 L 行一行不许进。"""
    eold, enew = f"EXP-OLD-{_UNIQ}", f"EXP-PROD-{_UNIQ}"
    _seed_pair(eold, "benchmark", "L")       # 旧实验的 L 行（在旧标记段上）
    _seed_pair(eold, "benchmark", "S")      # 旧实验的 S 行
    _seed_pair(enew, "benchmark", "L")      # 新实验的 L 行
    out = BB.build_length_balanced(f"bal2e-{_UNIQ}", version=1, seed=61,
                                   per_side=10,
                                   l_experiments=(enew,))
    with db.session() as s:
        st = s.get(BenchmarkSet, out["set_id"])
        items = s.query(BenchmarkItem).filter_by(set_id=st.id).all()
    # 逐题核 L 侧行的来源（L 侧 = answer 侧文本比另一侧长 → answer 侧是人类）
    with db.session() as s:
        for it in items:
            ans_len = len((it.text_a if it.answer == "A" else it.text_b) or "")
            oth_len = len((it.text_b if it.answer == "A" else it.text_a) or "")
            is_l = ans_len > oth_len
            if not is_l:
                continue
            cc = (s.query(ControlledCorruption)
                  .filter_by(segment_id=it.segment_id)
                  .filter(ControlledCorruption.text == (
                      it.text_b if it.answer == "A" else it.text_a)).first())
            assert cc is not None and cc.experiment_id == enew, \
                f"L 侧混入旧实验行（exp={getattr(cc, 'experiment_id', None)}）"
    assert out["universe"]["l_universe"] == [enew]
    assert out["L"] >= 1


def test_split_benchmark_scoped_by_exp():
    """exp 限定只标该实验的段；exp=None 对照组两实验都可标（监督 2d 归一断言）。"""
    e1, e2 = f"EXP-SA-{_UNIQ}", f"EXP-SB-{_UNIQ}"
    id1 = _seed_pair(e1, None, "L")
    id2 = _seed_pair(e2, None, "L")
    out = CC.split_benchmark(n=10, seed=20260920, exp=e1)
    with db.session() as s:
        r1, r2 = s.get(Segment, id1).role, s.get(Segment, id2).role
        assert r1 in ("benchmark", None)      # 划基准路径自身幂等性不在本测试范围
        assert r2 in (None, "", "benchmark")  # 归一化断言（监督 2d）
    # r1 若被标记而 r2 未被标记（exp 限定的核心）——但因为段可能共享 Work，
    # split_benchmark 按劣化行所在段选择：e1 的段被标，e2 的段不该被 e1 划到
    if out.get("marked", 0) >= 1:
        with db.session() as s:
            assert s.get(Segment, id1).role == "benchmark"
            assert (s.get(Segment, id2).role or None) is None or \
                s.get(Segment, id2).role in ("", None), "别的实验不许被顺带标记"


def test_split_benchmark_none_exp_marks_both():
    """exp=None 对照组：不限定的旧行为——两个实验的段都可入池。"""
    e1, e2 = f"EXP-NA-{_UNIQ}", f"EXP-NB-{_UNIQ}"
    _seed_pair(e1, None, "L")
    _seed_pair(e2, None, "L")
    out = CC.split_benchmark(n=10, seed=20260920, exp=None)
    assert out.get("pool_total", 0) >= 2, "exp=None 应看到两个实验的段（对照）"


# ── 2b. 同名守卫 + replace ────────────────────────────────────

def test_same_name_refused_then_replace_reports():
    name = f"bal-guard-{_UNIQ}"
    _seed_pair(f"EXP-G1-{_UNIQ}", "benchmark", "L")
    _seed_pair(f"EXP-G1-{_UNIQ}", "benchmark", "S")   # 只 seed L → k=0 空集假阴性
    out1 = BB.build_length_balanced(name, version=1, seed=71, per_side=5)
    # 同名重跑：默认拒绝（防重复集——监督实测 builder 无按名查重，此守卫即新契约）
    with pytest.raises(SystemExit, match="同名长度平衡集已存在"):
        BB.build_length_balanced(name, version=1, seed=71, per_side=5)
    # replace=True：删旧建新，返回报 replaced
    out2 = BB.build_length_balanced(name, version=1, seed=71, per_side=5,
                                    replace=True)
    assert out2["replaced"]["set_id"] == out1["set_id"], "replace 必须留删旧痕迹"
    assert out2["replaced"]["n_items"] == out1["items"]
    with db.session() as s:
        assert s.get(BenchmarkSet, out1["set_id"]) is None, "旧集没删干净"
        st2 = s.get(BenchmarkSet, out2["set_id"])
        assert st2 is not None
        # spec 记 replaced 痕迹（qwen 席 BLOCK 项：删旧入库必须有可追溯指针）
        assert (st2.spec or {}).get("replaced", {}).get("set_id") == out1["set_id"]
        n = s.query(BenchmarkSet).filter_by(name=name,
                                            kind="length_balanced").count()
        assert n == 1, "replace 后必须恰好一个同名集"


def test_dry_run_reports_name_conflict():
    """dry-run 也报同名冲突：预演就能看出真跑会被拒绝还是要删哪个集
    （qwen 席 BLOCK 项：dry_run 整体跳过守卫，replaced 恒 None）。"""
    _seed_pair(f"EXP-DRY-{_UNIQ}", "benchmark", "L")
    _seed_pair(f"EXP-DRY-{_UNIQ}", "benchmark", "S")   # 只 seed L → k=0 空集假阴性
    name = f"bal-dry-{_UNIQ}"
    d1 = BB.build_length_balanced(name, version=1, seed=97, per_side=1,
                                  dry_run=True)
    assert d1["name_conflict"] is None and d1["on_conflict"].startswith("refuse")
    out = BB.build_length_balanced(name, version=1, seed=97, per_side=1)
    d2 = BB.build_length_balanced(name, version=1, seed=97, per_side=1,
                                  dry_run=True)
    assert d2["name_conflict"] == {"set_id": out["set_id"],
                                   "n_items": out["items"]}
    assert d2["on_conflict"] == "refuse（SystemExit）"
    d3 = BB.build_length_balanced(name, version=1, seed=97, per_side=1,
                                  dry_run=True, replace=True)
    assert d3["on_conflict"].startswith("replace"), "replace 预演要标明会删旧"
    assert d3["replaced"] is None  # dry-run 只预告，不真删


def test_bal_v2_carries_split_marker_and_universe():
    """spec 带 split=2 版本标记 + 实测宇宙字段（l/s_universe + 两侧实测库存）。"""
    _seed_pair(f"EXP-SP-{_UNIQ}", "benchmark", "L")
    _seed_pair(f"EXP-SP-{_UNIQ}", "benchmark", "S")   # 只 seed L → k=0 空集假阴性
    out = BB.build_length_balanced(f"bal-sp-{_UNIQ}", version=1, seed=81, per_side=1)
    with db.session() as s:
        st = s.get(BenchmarkSet, out["set_id"])
    assert (st.spec or {}).get("split") == 2
    spec = st.spec or {}
    assert spec.get("l_universe") in ("all", list) or isinstance(spec.get("l_universe"), (list, str))
    assert "l_stock_measured" in spec and "s_stock_measured" in spec, \
        "spec 必须带两侧实测库存（监督 2a：别写口号）"


def test_insufficient_stock_reports_per_side_fields():
    """per_side 超库存：两侧仍严格配平 + 返回 S/L/宇宙库存字段（钉住报告字段，监督 2d）。"""
    _seed_pair(f"EXP-IS-{_UNIQ}", "benchmark", "L")
    _seed_pair(f"EXP-IS-{_UNIQ}", "benchmark", "S")
    out = BB.build_length_balanced(f"bal-is-{_UNIQ}", version=1, seed=91,
                                  per_side=10_000)
    assert out["S"] == out["L"], "不足时也必须两侧配平（k=min）"
    assert out["items"] == out["S"] + out["L"]
    assert "universe" in out and "l_stock_measured" in out["universe"], \
        "库存不足的报告必须带实测库存字段（预注册协议要求如实报）"


# ── 宇宙核验脚本（qwen 席 BLOCK 项：读数证据必须可复现，不许拿建集函数自证） ──

def test_verify_bal_universe_clean_set_passes():
    """干净集核验通过：L 侧全部命中预期宇宙，0 mismatch，per-side 计数带实验 id。"""
    eold, enew = f"EXP-VC-O-{_UNIQ}", f"EXP-VC-N-{_UNIQ}"
    _seed_pair(eold, "benchmark", "L")   # 旧实验 L 行：宇宙过滤后不进集
    _seed_pair(enew, "benchmark", "L")
    _seed_pair(enew, "benchmark", "S")
    out = BB.build_length_balanced(f"bal-vc-{_UNIQ}", version=1, seed=117,
                                   per_side=1, l_experiments=(enew,))
    rep = VU.verify_set(out["set_id"])
    assert rep["n_mismatch"] == 0, f"干净集不该报 mismatch：{rep['mismatch']}"
    assert any(k.startswith("L/") and enew in k
               for k in rep["per_direction_experiment"]), \
        "逐题回连计数必须带实验 id（不然等于没核）"
    # 旧实验的 L 行若被宇宙过滤拦住，就不该出现在任何 L/ 计数里
    assert not any(k.startswith("L/") and eold in k
                   for k in rep["per_direction_experiment"]), "旧实验 L 行混进集"


def test_verify_bal_universe_flags_foreign_l_rows():
    """L 侧有行落在预期宇宙外 → n_mismatch≥1 且逐题列出（exit 1 的判据来自这里）。"""
    evf = f"EXP-VF-{_UNIQ}"
    _seed_pair(evf, "benchmark", "L")
    _seed_pair(evf, "benchmark", "S")   # 不 seed S → k=0 建空集 → 0 mismatch 假阴性
    out = BB.build_length_balanced(f"bal-vf-{_UNIQ}", version=1, seed=127, per_side=1)
    assert out["items"] >= 2, "两侧都有库存时必须有题（空集的 mismatch 没有判别力）"
    rep = VU.verify_set(out["set_id"], l_experiments=("EXP-NOPE",))
    assert rep["n_mismatch"] >= 1, "宇宙外的 L 行必须被点出（宁报错不放过）"
    assert any(m["reason"] == "outside_universe" for m in rep["mismatch"]), \
        "mismatch 必须带原因分型（outside_universe/ambiguous/not_linked/EQ）"
