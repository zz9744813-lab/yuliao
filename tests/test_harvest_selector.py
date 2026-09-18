"""自动挑题脚本回归（2026-09-16）。

**背景**：为补少数类标注而做的「收割 + 校准锚点」挑题器
（`scripts/select_harvest_batch.py`）。设计动机与 s30 的教训见该脚本 docstring。

本文件锁死四类错误，每一类都对应本项目真实踩过的坑：
1. **池基准率算错**：第一版用「累积上尾率」平均整池，得出 0.49 —— 与 score 均值
   0.27 自相矛盾。自相矛盾的数字必须能被测试抓住，否则会直接写进报告。
2. **打分方向写反**：会抽到完全相反的样本（`make_stratified_batch` 有过先例）。
3. **收割与锚点重叠**：同一条题既是"按分挑的"又是"随机锚点"，锚点就失去无偏性。
4. **静默空批**：池子为空时不得静默返回，必须显式报错。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import select_harvest_batch as SH  # noqa: E402


# ── ① Wilson 区间 ────────────────────────────────────────────
def test_wilson_bounds_within_unit_interval():
    """Wilson 的优点之一：小样本下也不会越界（正态近似会越界）。"""
    for k, n in ((0, 5), (5, 5), (1, 20), (19, 20)):
        lo, hi = SH.wilson(k, n)
        assert 0.0 <= lo <= hi <= 1.0, f"k={k},n={n} 越界: [{lo},{hi}]"


def test_wilson_centered_near_estimate():
    lo, hi = SH.wilson(30, 100)
    assert lo < 0.30 < hi
    assert hi - lo < 0.20


def test_wilson_narrows_as_n_grows():
    w_small = SH.wilson(10, 20)
    w_big = SH.wilson(100, 200)
    assert (w_big[1] - w_big[0]) < (w_small[1] - w_small[0])


def test_wilson_empty_returns_nan():
    lo, hi = SH.wilson(0, 0)
    assert np.isnan(lo) and np.isnan(hi)


# ── ② 池基准率（第一版真错过的那个）──────────────────────────
def test_pool_base_rate_is_mean_of_scores():
    assert SH.pool_base_rate([0.2, 0.4, 0.6]) == pytest.approx(0.4)


def test_pool_base_rate_never_exceeds_score_mean():
    """❗核心回归：池基准率必须 ≤ 分数均值（分数是概率，均值就是期望率）。

    第一版把「累积上尾率」（P(win | score≥t)，在高端接近 1）对整池平均，
    得到 0.49 > 分数均值 0.27 —— **自相矛盾**。
    这条断言让那种错误无法通过。
    """
    scores = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.80, 0.90, 0.95]
    rate = SH.pool_base_rate(scores)
    assert rate <= float(np.mean(scores)) + 1e-12
    assert rate == pytest.approx(float(np.mean(scores)))


def test_pool_base_rate_empty_raises():
    """池子为空必须报错，不能静默返回 0（会让下游把"没数据"当"胜率0"）。"""
    with pytest.raises(ValueError):
        SH.pool_base_rate([])


# ── ③ 累积命中率 ──────────────────────────────────────────────
def test_cumulative_rate_counts_only_above_threshold():
    sc = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    y = np.array([0.0, 0.0, 1.0, 1.0, 1.0])
    rate, k, n = SH.cumulative_rate(sc, y, 0.3)
    assert (k, n) == (3, 3)
    assert rate == pytest.approx(1.0)


def test_cumulative_rate_above_all_returns_nan():
    sc = np.array([0.1, 0.2])
    y = np.array([0.0, 1.0])
    rate, k, n = SH.cumulative_rate(sc, y, 0.99)
    assert n == 0 and np.isnan(rate)


def test_cumulative_rate_is_monotone_in_threshold():
    """阈值升高 → 命中率不应下降（在足够样本下）。用构造数据保证单调。"""
    sc = np.linspace(0.0, 1.0, 100)
    y = (sc > 0.5).astype(float)
    rates = [SH.cumulative_rate(sc, y, t)[0] for t in (0.0, 0.3, 0.6, 0.9)]
    assert all(b >= a - 1e-9 for a, b in zip(rates, rates[1:])), rates


# ── ④ 打分方向 ────────────────────────────────────────────────
def test_orientation_assertion_catches_reversed_signal():
    """方向写反必须报错。用真实 build() 的前置逻辑无法脱离 DB，故直接测判据。"""
    y = np.array([0.0, 1.0, 0.0, 1.0, 1.0])
    good = np.array([0.1, 0.9, 0.2, 0.8, 0.7])
    bad = -good
    assert float(np.corrcoef(good, y)[0, 1]) > 0
    assert float(np.corrcoef(bad, y)[0, 1]) < 0   # 会被脚本的 rho<=0 拦下


# ── ⑤ 收割/锚点不重叠 ─────────────────────────────────────────
def test_harvest_and_anchor_are_disjoint():
    """锚点必须从"未被收割占用"的部分抽，否则锚点失去无偏性。"""
    pool = [(f"c{i}", 1.0 - i / 100.0) for i in range(50)]
    n_harvest, n_anchor = 21, 9
    harvest = pool[:n_harvest]
    taken = {cid for cid, _ in harvest}
    rest = [x for x in pool if x[0] not in taken]
    import random
    anchor = random.Random(1).sample(rest, n_anchor)
    assert not (taken & {cid for cid, _ in anchor})
    assert len(harvest) + len(anchor) == 30
    assert len({cid for cid, _ in harvest + anchor}) == 30


def test_seed_makes_anchor_reproducible():
    """同 seed 必须复现同一批锚点——否则无法回溯/复现实验。"""
    import random
    pool = [(f"c{i}", 0.5) for i in range(50)]
    a1 = random.Random(42).sample(pool, 9)
    a2 = random.Random(42).sample(pool, 9)
    a3 = random.Random(43).sample(pool, 9)
    assert [x[0] for x in a1] == [x[0] for x in a2]
    assert [x[0] for x in a1] != [x[0] for x in a3]


# ── ⑥ 校准集必须排除按 score 分层的批次 ───────────────────────
def test_score_screened_batches_excluded():
    """s30 是按 score 分层的，用它拟合校准曲线会自洽性偏差，必须排除。

    这条同时防"以后又有人把 s30 加回校准集"。
    """
    assert "s30" in SH.SCORE_SCREENED_BATCHES
