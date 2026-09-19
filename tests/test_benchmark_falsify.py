"""否掉检验工具的回归（2026-09-19）。

用合成 run 钉住三件事：
1. **全对 run** 必须过 N0（段翻转置换 p 极小）、聚类 CI 包住点估计；
2. **纯猜 run**（pick 与答案独立随机）必须不过 N0——检验不能放行噪声；
3. **恒选 A 的 run** 在均衡答案集上必须被 N1 位置偏差抓住；
4. 未答率缩水（picks 缺题）必须被 N3 显形；
5. 检验本身可复现（同种子同 p 值）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_falsify as BF  # noqa: E402


def _meta(n_seg=10, per_seg=6, balanced=True):
    """合成条目：n_seg 段 × per_seg 题；答案 A/B 各半（或偏斜）。"""
    meta = {}
    for g in range(n_seg):
        for j in range(per_seg):
            iid = f"I{g}-{j}"
            ans = "A" if (j % 2 == 0 if balanced else True) else "B"
            meta[iid] = (f"SEG{g}", ans, "T1")
    return meta


def _run(picks: dict, n=None):
    correct = sum(1 for iid, p in picks.items() if _ANS[iid] == p)
    return {"model": "t", "n": n or len(picks), "correct": correct,
            "accuracy": correct / max(1, n or len(picks)), "picks": picks,
            "created_at": "t"}


_ANS = _meta()


def test_all_correct_run_passes_n0():
    picks = {iid: _ANS[iid][1] for iid in _ANS}   # 答案键是 (seg, ans, type)，取 ans
    out = BF.falsify_run(_ANS, _run(picks))
    assert out["acc"] == 1.0
    assert out["perm_p"] < 0.001, "全对在段翻转零假设下应几乎不可能"
    lo, hi = out["cluster_ci"]
    assert lo <= out["acc"] <= hi + 1e-9 or hi >= out["acc"], "聚类 CI 应覆盖点估计附近"
    assert out["verdict"] == "pass"


def test_coin_flip_run_fails_n0():
    import random
    rng = random.Random(7)
    picks = {iid: rng.choice("AB") for iid in _ANS}
    out = BF.falsify_run(_ANS, _run(picks))
    assert out["perm_p"] > 0.05, "纯猜不该过置换检验"
    assert out["verdict"] == "fail"


def test_constant_a_run_caught_by_position_bias():
    picks = {iid: "A" for iid in _ANS}
    out = BF.falsify_run(_ANS, _run(picks))
    assert out["pick_a_rate"] == 1.0
    assert out["pos_bias"] is not None and out["pos_bias"] >= BF.GATE["pos_bias"], \
        "恒选A必须被位置偏差检查抓住"
    assert out["verdict"] != "pass"


def test_missing_answers_surface_in_n3():
    picks = {iid: _ANS[iid][1] for iid in list(_ANS)[:30]}   # 只答 30/60
    out = BF.falsify_run(_ANS, _run(picks, n=len(_ANS)))     # run.n = 集合大小
    assert out["answered_rate"] == pytest.approx(0.5)
    assert out["checks"]["answered"] is False, "一半未答必须被 N3 显形"


def test_falsify_is_reproducible():
    import random
    rng = random.Random(11)
    picks = {iid: (rng.choice("AB") if rng.random() < 0.8 else _ANS[iid]) for iid in _ANS}
    a = BF.falsify_run(_ANS, _run(picks))
    b = BF.falsify_run(_ANS, _run(picks))
    assert a["perm_p"] == b["perm_p"] and a["cluster_ci"] == b["cluster_ci"], \
        "同种子两次检验结果必须逐位一致"
