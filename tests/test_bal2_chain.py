"""bal-v2 建集链的规格测试（PROD 收尾前先行写，2026-09-20 监督指令）。

链条：PROD → split_benchmark（划基准）→ build_length_balanced(bal-v2)
→ benchmark_run → benchmark_falsify → 档一读数。
本文件钉住链条上前两步的契约：
1. **split_benchmark 的 exp 限定**（CLI --exp 必须透传——监督方实测缺口）：
   限定 exp 时只标记该实验劣化行所在段；不限定时全库（现有行为，供对照）。
   理由留痕：不限定会混入旧实验的无窗样本，破坏 bal-v2 的
   「强制长度方向」性质；按 exp 限定使划分可复现。
2. **bal-v2 建集契约**：幂等键=set 名（bal-v2 ≠ bal-v1 不互踩）；
   spec.split=2 版本标记；per_side 按真实库存定，**不足 40 不凑数**——
   按提案预注册写成库存不足的结论（不足时 items < 2×40 也必须是两侧
   严格配平，不许一侧凑）。
"""
from __future__ import annotations

import sys
import uuid as _uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import controlled_corruption as CC          # noqa: E402
import benchmark_build as BB               # noqa: E402
from app import db                         # noqa: E402
from app.models import (ControlledCorruption, Experiment, Frame,  # noqa: E402
                        Segment, Work)

_UNIQ = _uuid.uuid4().hex[:8]


def _seed_exp_corruption(exp_id: str, seg_role: str | None, ctype="SUBTEXT_ERASE",
                        status="ok", ratio=0.8):
    """一段 + 一条劣化行（挂指定实验）。返回 segment id。"""
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t", status="created",
                             config={}, stats={}))
        w = Work(title=f"t-bal2-{exp_id}-{_UNIQ}", source="test:bal2")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0,
                      text="他把茶喝完才起身，屋外风声很紧，谁也没有再说话，窗纸被吹得鼓了一下。",
                      role=seg_role, integrity='{"src_ok": true}',
                      n_sentences=2, n_chars=36)
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp_id, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        s.add(ControlledCorruption(
            experiment_id=exp_id, segment_id=seg.id, frame_id=fr.id,
            corruption_type=ctype, variable="x", generator_model="g",
            prompt_version="corrupt_v2", text="他喝完茶起身，风声很紧。",
            n_chars=10, drift={}, drift_score=0.1, fact_consistent=True,
            drift_ok=True, verify_model="v", verify_pv="pv", status=status,
            len_ratio=ratio))
        s.commit()
        return seg.id


# ── 1. split_benchmark 的 exp 限定 ─────────────────────────

def test_split_benchmark_scoped_by_exp():
    """CLI --exp 必须透传到 split_benchmark：限定 exp 时只标记该实验的段。"""
    db.init_db()
    e1 = f"EXP-SCOPE-A-{_UNIQ}"
    e2 = f"EXP-SCOPE-B-{_UNIQ}"
    id1 = _seed_exp_corruption(e1, None)
    id2 = _seed_exp_corruption(e2, None)
    out = CC.split_benchmark(n=10, seed=20260920, exp=e1)
    with db.session() as s:
        r1 = s.get(Segment, id1).role
        r2 = s.get(Segment, id2).role
    assert out.get("marked", 0) >= 1 and r1 == "benchmark", "exp 限定的实验该被标记"
    assert r2 is None, "别的实验不许被顺带标记（不限定=全库混选的缺口）"


def test_split_benchmark_cli_passes_exp():
    """CLI 接线：--split-benchmark 时 args.exp 必须传给 split_benchmark。
    （静态断言源码里的调用点——透传是行为前提，不是实现细节。）"""
    src = (ROOT / "scripts" / "controlled_corruption.py").read_text(encoding="utf-8")
    needle = "out = split_benchmark(n=n, seed=args.seed, dry_run=args.dry_run"
    assert needle in src, "split_benchmark 的 CLI 调用点找不到（改名需同步本断言）"
    # 透传 exp（args.exp 为空时 exp=None=全库旧行为，显式 --exp 时限定）
    call_region = src.split(needle, 1)[1][:200]
    assert "args.exp" in call_region, \
        "CLI 必须把 args.exp 透传给 split_benchmark——监督方实测的缺口"


# ── 2. bal-v2 建集契约 ──────────────────────────────────────

def test_bal_v2_idempotent_key_is_name():
    """幂等键=set 名：bal-v2 与 bal-v1 不互踩（建 bal-v2 不影响 bal-v1 库存）。"""
    out1 = BB.build_length_balanced("bal-name-a", version=1, seed=31, per_side=1)
    n1 = out1["items"]
    out2 = BB.build_length_balanced("bal-name-b", version=1, seed=31, per_side=1)
    with db.session() as s:
        a = s.query(BB.BenchmarkSet).filter_by(name="bal-name-a").first()
        b = s.query(BB.BenchmarkSet).filter_by(name="bal-name-b").first()
        na = s.query(BB.BenchmarkItem).filter_by(set_id=a.id).count()
        nb = s.query(BB.BenchmarkItem).filter_by(set_id=b.id).count()
    assert a and b and a.id != b.id
    assert na == n1 and nb == out2["items"], "两个名字各自独立成集"


def test_bal_v2_carries_split_marker():
    """spec 带 split=2 版本标记（提案：采样宇宙变化必须带版本，同 seed 可复现）。"""
    out = BB.build_length_balanced(f"bal-split-{_UNIQ}", version=1, seed=41, per_side=1)
    with db.session() as s:
        st = s.get(BB.BenchmarkSet, out["set_id"])
    assert (st.spec or {}).get("split") == 2, "bal 系集合 spec.split 必须标 2（v1 时代的库外集）"


def test_bal_build_reports_insufficient_stock_honestly():
    """per_side 超过库存时：两侧仍严格配平（各 k=min(per_side, 库存)），
    **不许一侧凑数**——按提案预注册写成库存不足的结论由跑分方解读，
    建集方只负责配平与如实报 S/L 数。"""
    out = BB.build_length_balanced(f"bal-short-{_UNIQ}", version=1, seed=51,
                                  per_side=10_000)   # 远超库存
    assert out["S"] == out["L"], "不足时也必须两侧配平（k=min）"
    assert out["items"] == out["S"] + out["L"]
