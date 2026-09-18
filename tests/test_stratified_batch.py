"""分层抽样 + 逆概率加权回归（2026-09-14）。

背景：r25 纯随机后人胜率 76%，候选胜只占 24%。κ 的方差由少数类决定，
纯随机要拿到 10 条候选胜需判约 42 条。所以改用**对"候选胜"过采样**的分层抽样。

锁死三件事：
1. **打分方向**：`score = P(候选胜)`，高 score 应对应高候选胜率。
   上一轮做探索分析时我把分箱列名标反了，差点按反方向抽样——故把方向做成断言。
2. **加权 κ 正确性**：等权时须与普通 κ 完全一致；分层抽样下须能去偏。
3. **抽样池**只含"可盲评 + 有 det 指标 + 未判"的候选。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import heldout_eval as HE
import make_stratified_batch as MSB


# ── ① 打分方向 ────────────────────────────────────────────────
def test_orientation_assertion_passes_when_correct():
    # 完全单调的例子，r 应为 1.0
    r = MSB._assert_orientation(np.array([0.0, 0.0, 1.0, 1.0]),
                                np.array([0.0, 0.0, 1.0, 1.0]))
    assert r > 0.99


def test_orientation_assertion_accepts_positive_but_noisy():
    """方向对但有噪声时不得误报——断言的是符号，不是强度。"""
    r = MSB._assert_orientation(np.array([0.1, 0.4, 0.6, 0.9]),
                                np.array([0.0, 0.0, 1.0, 1.0]))
    assert r > 0.8   # 实测约 0.857


def test_orientation_assertion_fires_when_inverted():
    """核心回归：方向反了必须启动即失败，不能静默抽反。

    这正是我上一轮的实际错误（把分箱列名标反，误以为低分=候选胜）。
    若没有这个断言，会抽到方向完全相反的样本，而且事后很难发现。
    """
    with pytest.raises(SystemExit) as e:
        MSB._assert_orientation(np.array([0.1, 0.4, 0.6, 0.9]),
                                np.array([1.0, 1.0, 0.0, 0.0]))
    assert "方向错误" in str(e.value)


def test_allocation_favours_top_stratum():
    """配额必须压在候选胜率最高的 S1——否则分层就白做了。"""
    assert MSB.ALLOC["S1"] > MSB.ALLOC["S2"]
    assert MSB.ALLOC["S1"] > MSB.ALLOC["S3"]
    assert abs(sum(MSB.ALLOC.values()) - 1.0) < 1e-9
    # S1 的分数下界必须最高
    assert MSB.STRATA[0][1] > MSB.STRATA[1][1] > MSB.STRATA[2][1]


# ── ② 加权 κ ─────────────────────────────────────────────────
def test_weighted_kappa_equals_plain_when_equal_weights():
    pairs = [(1, 1), (1, 0), (0, 0), (0, 1), (1, 1), (0, 0)]
    wk, n_eff = HE._weighted_kappa(pairs, [1.0] * len(pairs))
    assert abs(wk - HE._kappa(pairs)) < 1e-12
    assert abs(n_eff - len(pairs)) < 1e-9


def test_weighted_kappa_deflates_inflated_raw_kappa():
    """分层抽样会**抬高** raw κ；加权须把它还原到总体 κ。

    构造（可解析验算）：
      - 总体：90% 人类胜；评委在两类上都是 90% 判对。
      - 样本：人类胜 20 条 / 候选胜 20 条（对少数类过采样到 50/50）。
      - 人类胜样本每条代表 9 个总体单元，候选胜样本每条代表 1 个。

    解析总体 κ：
      po = .9*.9 + .1*.9 = .90；p_judge_human = .9*.9 + .1*.1 = .82；p_user_human = .90
      pe = .82*.90 + .18*.10 = .756 → κ = (.90-.756)/(1-.756) = 0.590

    而平衡样本的 raw κ = (.9-.5)/(1-.5) = 0.800 —— **高于总体**。
    即：对少数类过采样会把 κ 抬虚，加权是**下调**而非上调。
    （我最初把方向写反了，误以为加权要"上调"；实际 raw 才是虚高的那个。）
    """
    human_pairs = [(1, 1)] * 18 + [(1, 0)] * 2      # 20 条
    cand_pairs = [(0, 0)] * 18 + [(0, 1)] * 2       # 20 条
    pairs = human_pairs + cand_pairs
    ws = [9.0] * len(human_pairs) + [1.0] * len(cand_pairs)

    raw = HE._kappa(pairs)
    wk, n_eff = HE._weighted_kappa(pairs, ws)
    assert raw > 0.75, f"平衡样本 raw κ 应虚高（约 0.80），实得 {raw:.3f}"
    assert abs(wk - 0.590) < 0.02, f"加权 κ 应等于解析总体 κ 0.590，实得 {wk:.3f}"
    assert wk < raw, f"加权应把虚高的 raw 还原下去：raw={raw:.3f} weighted={wk:.3f}"
    assert n_eff < len(pairs), "权重悬殊时有效样本量必须小于名义 n"


def test_weighted_kappa_preserves_value_when_no_oversampling():
    """没有过采样（权重全 1）时不得改动 κ——避免"为加权而加权"。"""
    pairs = [(1, 1), (1, 0), (0, 0), (0, 1)] * 5
    raw = HE._kappa(pairs)
    wk, _ = HE._weighted_kappa(pairs, [1.0] * len(pairs))
    assert abs(wk - raw) < 1e-12


def test_weighted_kappa_reports_low_n_eff_for_skewed_weights():
    """权重悬殊 → n_eff 远小于 n。这是必报的诚实指标（加权去偏不去方差）。"""
    pairs = [(1, 1)] * 5 + [(0, 0)] * 5
    ws = [1.0] * 5 + [100.0] * 5
    _, n_eff = HE._weighted_kappa(pairs, ws)
    assert n_eff < 6, f"权重悬殊时 n_eff 应远小于 10，实得 {n_eff:.1f}"


def test_weighted_kappa_handles_empty():
    wk, n_eff = HE._weighted_kappa([], [])
    assert np.isnan(wk) and n_eff == 0.0


# ── ③ 抽样池 ─────────────────────────────────────────────────
def test_pool_invariants_documented():
    """池子定义必须同时满足三条；这里锁住代码里引用的白名单来源。"""
    from app.config import BLIND_REVIEW_PROMPT_VERSIONS
    assert "reconstruct_v1" in BLIND_REVIEW_PROMPT_VERSIONS
    # 分层打分依赖 residuals_det，而 recon_ctx_v1 没有 det 指标 → 会被 det join 自然排除
    assert MSB.FEATURES and len(MSB.FEATURES) == 6


def test_features_match_pref_drivers_top6():
    """分层用的特征必须与 pref_drivers 的 top-6 一致，否则打分口径会漂。"""
    import pref_drivers as PD
    for k in MSB.FEATURES:
        assert k in PD.load()[0], f"特征 {k} 不在 det 指标集里"
