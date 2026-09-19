"""否掉检验工具的回归（2026-09-19）。

用合成 run 钉住三件事：
1. **全对 run** 必须过 N0（段翻转置换 p 极小）、聚类 CI 包住点估计；
2. **纯猜 run**（pick 与答案独立随机）必须不过 N0——检验不能放行噪声；
3. **恒选 A 的 run** 在均衡答案集上必须被 N1 位置偏差抓住；
4. 未答率缩水（picks 缺题）必须被 N3 显形；
5. 检验本身可复现（同种子同 p 值）；
6. 长度分层判定（N5，指标硬化）：只在「人类侧更短」的题上对的模型不得 pass；
   两层都强不阻塞 pass；任一层 <3 题判 None、不参与判定。
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
    """合成条目：n_seg 段 × per_seg 题；答案 A/B 各半（或偏斜）。
    长度表让长度基线平凡可算：A 侧 10 字 / B 侧 20 字（A 恒更短）。"""
    meta = {}
    lengths = {}
    for g in range(n_seg):
        for j in range(per_seg):
            iid = f"I{g}-{j}"
            ans = "A" if (j % 2 == 0 if balanced else True) else "B"
            meta[iid] = (f"SEG{g}", ans, "T1")
            lengths[iid] = (10, 20)
    return meta, lengths


def _run(picks: dict, n=None):
    correct = sum(1 for iid, p in picks.items() if _ANS[iid] == p)
    return {"model": "t", "n": n or len(picks), "correct": correct,
            "accuracy": correct / max(1, n or len(picks)), "picks": picks,
            "created_at": "t"}


_ANS, _LEN = _meta()


def test_all_correct_run_passes_n0():
    picks = {iid: _ANS[iid][1] for iid in _ANS}   # 答案键是 (seg, ans, type)，取 ans
    out = BF.falsify_run(_ANS, _run(picks), lengths=_LEN)
    assert out["acc"] == 1.0
    assert out["perm_p"] < 0.001, "全对在段翻转零假设下应几乎不可能"
    lo, hi = out["cluster_ci"]
    assert lo <= out["acc"] <= hi + 1e-9 or hi >= out["acc"], "聚类 CI 应覆盖点估计附近"
    assert out["verdict"] == "pass"


def test_coin_flip_run_fails_n0():
    import random
    rng = random.Random(7)
    picks = {iid: rng.choice("AB") for iid in _ANS}
    out = BF.falsify_run(_ANS, _run(picks), lengths=_LEN)
    assert out["perm_p"] > 0.05, "纯猜不该过置换检验"
    assert out["verdict"] == "fail"


def test_constant_a_run_caught_by_position_bias():
    picks = {iid: "A" for iid in _ANS}
    out = BF.falsify_run(_ANS, _run(picks), lengths=_LEN)
    assert out["pick_a_rate"] == 1.0
    assert out["pos_bias"] is not None and out["pos_bias"] >= BF.GATE["pos_bias"], \
        "恒选A必须被位置偏差检查抓住"
    assert out["verdict"] != "pass"


def test_missing_answers_surface_in_n3():
    picks = {iid: _ANS[iid][1] for iid in list(_ANS)[:30]}   # 只答 30/60
    out = BF.falsify_run(_ANS, _run(picks, n=len(_ANS)), lengths=_LEN)     # run.n = 集合大小
    assert out["answered_rate"] == pytest.approx(0.5)
    assert out["checks"]["answered"] is False, "一半未答必须被 N3 显形"


def test_falsify_is_reproducible():
    import random
    rng = random.Random(11)
    picks = {iid: (rng.choice("AB") if rng.random() < 0.8 else _ANS[iid]) for iid in _ANS}
    a = BF.falsify_run(_ANS, _run(picks), lengths=_LEN)
    b = BF.falsify_run(_ANS, _run(picks), lengths=_LEN)
    assert a["perm_p"] == b["perm_p"] and a["cluster_ci"] == b["cluster_ci"], \
        "同种子两次检验结果必须逐位一致"


# ---------------------------------------------------------------- N5 长度分层
# 夹具约定：lengths[iid] = (len(text_a), len(text_b))；la<lb 时 A 侧更短。
# 「人类侧更短」层 = 答案落在更短一侧的题；「人类侧更长」层 = 答案在更长一侧。
# 标准夹具 _LEN 恒为 (10,20)，故短层=30 道 A 答案题、长层=30 道 B 答案题。

def _run_m(picks: dict, meta: dict, n=None):
    """同 _run，但 correct 按给定 meta 计算（_run 钉死 _ANS）。"""
    correct = sum(1 for iid, p in picks.items() if meta[iid][1] == p)
    return {"model": "t", "n": n or len(picks), "correct": correct,
            "accuracy": correct / max(1, n or len(picks)), "picks": picks,
            "created_at": "t"}


def _meta_per_seg(spec, n_seg=10, prefix="P"):
    """每段放同一组 spec：spec = [(ans, la, lb), ...]，段内组成一致。"""
    meta, lengths = {}, {}
    for g in range(n_seg):
        for j, (ans, la, lb) in enumerate(spec):
            iid = f"{prefix}{g}-{j}"
            meta[iid] = (f"{prefix}SEG{g}", ans, "T1")
            lengths[iid] = (la, lb)
    return meta, lengths


def _meta_scattered(spec, n_seg=10, prefix="S"):
    """第 k 题进第 k % n_seg 段（可控错题落在不同段）：spec = [(ans, la, lb), ...]。"""
    meta, lengths = {}, {}
    for k, (ans, la, lb) in enumerate(spec):
        seg, j = k % n_seg, k // n_seg
        iid = f"{prefix}{seg}-{j}"
        meta[iid] = (f"{prefix}SEG{seg}", ans, "T1")
        lengths[iid] = (la, lb)
    return meta, lengths


def test_length_gate_flags_model_correct_only_on_short_side():
    """(a) 模型只在「人类侧更短」的题上对、长侧全错（等价纯"选更短"规则）
    → length_stratified 判 False，且不得 pass。"""
    spec = [("A", 10, 20), ("B", 20, 10), ("A", 10, 20),    # 短层 ×3（2 道答A+1 道答B）
            ("B", 10, 20), ("A", 20, 10), ("B", 10, 20)]    # 长层 ×3（1 道答A+2 道答B）
    meta, lengths = _meta_per_seg(spec, n_seg=10, prefix="M")
    picks = {iid: ("A" if la < lb else "B") for iid, (la, lb) in lengths.items()}
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=60, lengths=lengths)
    ls = out["length_strat"]
    assert ls["short"]["n"] == 30 and ls["long"]["n"] == 30
    assert ls["short"]["acc"] == 1.0 and ls["long"]["acc"] == 0.0
    assert out["checks"]["length_stratified"] is False
    assert out["verdict"] != "pass"


def test_length_gate_alone_downgrades_pass_to_weak():
    """(a') 其余检验全过、唯独「人类侧更长」层 acc<0.5 → 只被新判据压成 weak。
    66 道短层全对 + 14 道长层只对 6 道（<0.5）；但比"选更短"基线显著强
    （6:0，p=2/64<0.05），beats_length 仍过——证明新判据独立承重。"""
    wrong_long = [("B", 10, 20)] * 4 + [("A", 20, 10)] * 4          # 长层、模型答错
    right_long = [("B", 10, 20)] * 3 + [("A", 20, 10)] * 3          # 长层、模型答对
    right_short = [("A", 10, 20)] * 33 + [("B", 20, 10)] * 33       # 短层、模型答对
    spec = wrong_long + right_long + right_short   # 8 道错题散进 8 个不同段
    meta, lengths = _meta_scattered(spec, n_seg=10, prefix="L")
    picks = {}
    for k, (ans, la, lb) in enumerate(spec):
        iid = f"L{k % 10}-{k // 10}"
        shorter = "A" if la < lb else "B"
        picks[iid] = shorter if k < 8 else ans     # 错题=选了更短侧；其余=答对
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=80, lengths=lengths)
    ls = out["length_strat"]
    assert ls["short"] == {"n": 66, "acc": 1.0}
    assert ls["long"] == {"n": 14, "acc": 0.4286}
    others = {k: v for k, v in out["checks"].items() if k != "length_stratified"}
    assert all(others.values()), others
    assert out["checks"]["length_stratified"] is False
    assert out["verdict"] == "weak"


def test_length_gate_does_not_block_strong_layers():
    """(b) 两层都强（各 ≥3 题且长层 acc ≥0.5）→ 判 True，不影响原 pass。
    标准夹具全对模型：短层 30 题对 30、长层 30 题对 30。"""
    picks = {iid: _ANS[iid][1] for iid in _ANS}
    out = BF.falsify_run(_ANS, _run(picks), lengths=_LEN)
    ls = out["length_strat"]
    assert ls["short"] == {"n": 30, "acc": 1.0}
    assert ls["long"] == {"n": 30, "acc": 1.0}
    assert ls["equal"] == {"n": 0, "acc": None}
    assert out["checks"]["length_stratified"] is True
    assert out["verdict"] == "pass"


def test_length_gate_none_when_layer_under_three():
    """(c) 短层只有 2 题（<3）→ length_stratified=None，不参与判定也不阻塞 pass。"""
    spec = ([("B", 10, 20)] * 28 + [("A", 20, 10)] * 28       # 长层 56
            + [("A", 10, 20), ("B", 20, 10)]                  # 短层 2（<3）
            + [("A", 15, 15), ("B", 15, 15)])                 # 等长 2，单列
    meta, lengths = _meta_scattered(spec, n_seg=10, prefix="E")
    picks = {iid: meta[iid][1] for iid in meta}               # 全对
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=60, lengths=lengths)
    ls = out["length_strat"]
    assert (ls["short"]["n"], ls["long"]["n"], ls["equal"]["n"]) == (2, 56, 2)
    assert out["checks"]["length_stratified"] is None
    assert out["verdict"] == "pass"


def test_equal_length_items_bucketed_separately():
    """等长题单列 equal 桶，不进短/长层，也不影响两层都强时的 pass。"""
    spec = [("A", 10, 20), ("B", 20, 10), ("A", 10, 20), ("B", 20, 10),  # 短层 ×4
            ("B", 10, 20),                                               # 长层 ×1
            ("A", 15, 15)]                                               # 等长 ×1
    meta, lengths = _meta_per_seg(spec, n_seg=10, prefix="Q")
    picks = {iid: meta[iid][1] for iid in meta}                          # 全对
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=60, lengths=lengths)
    ls = out["length_strat"]
    assert (ls["short"]["n"], ls["long"]["n"], ls["equal"]["n"]) == (40, 10, 10)
    assert ls["equal"]["acc"] == 1.0
    assert out["checks"]["length_stratified"] is True
    assert out["verdict"] == "pass"


def test_md_length_strat_cell_format():
    """主表"分层acc(短/长)"单元格格式；None 或任一层空显示 —。"""
    assert BF._fmt_length_strat({"short": {"n": 8, "acc": 0.95},
                                 "long": {"n": 12, "acc": 0.6}}) == "0.95/0.60 (n=8/12)"
    assert BF._fmt_length_strat(None) == "—"
    assert BF._fmt_length_strat({"short": {"n": 0, "acc": None},
                                 "long": {"n": 12, "acc": 0.6}}) == "—"
