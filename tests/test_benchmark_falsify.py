"""否掉检验工具的回归（2026-09-19）。

用合成 run 钉住三件事：
1. **全对 run** 必须过 N0（段翻转置换 p 极小）、聚类 CI 包住点估计；
2. **纯猜 run**（pick 与答案独立随机）必须不过 N0——检验不能放行噪声；
3. **恒选 A 的 run** 在均衡答案集上必须被 N1 位置偏差抓住；
4. 未答率缩水（picks 缺题）必须被 N3 显形；
5. 检验本身可复现（同种子同 p 值）；
6. 长度分层判定（N5，指标硬化）：只在「人类侧更短」的题上对的模型不得 pass；
   两层都强不阻塞 pass；任一层 < long_layer_min_n(=10) 判 None、不参与判定。
7. T-N5FIX 回归：长层 acc<0.5 必不 pass；小层判 None 且不阻塞；lengths 缺 key 的题
   落 missing 桶并显形（不再伪装 equal）；lengths 的 (A侧,B侧) 与 meta A/B 同序约定
   被手算 fixture 锁死。
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


# ---------------------------------------------------------------- T-N5FIX 回归
# 会审 bd103bbe55 双席共同指出的 P1：N5 缺长度数据静默 fail-open、长层判据与同
# 文件统计尺度不一致、dict 字面量重复键 n_answered 覆盖、(la<lb)==(ans=="A")
# 判层的 A/B 同序约定无锁。以下逐条钉死。

def test_length_layer_convention_pins_ab_order():
    """⑤ 锁死约定 lengths[iid]=(len(A),len(B)) 且与 meta A/B 同序：手算四层，
    并把「两侧长度写反 → short/long 必须整体翻转」钉成断言（反序是隐性事故）。"""
    assert BF._length_layer(10, 20, "A") == "short"   # A 更短 & 人类答 A → 人类侧更短
    assert BF._length_layer(10, 20, "B") == "long"    # B 更长 & 人类答 B → 人类侧更长
    assert BF._length_layer(20, 10, "A") == "long"    # A 更长 & 人类答 A → 人类侧更长
    assert BF._length_layer(20, 10, "B") == "short"   # B 更短 & 人类答 B → 人类侧更短
    assert BF._length_layer(15, 15, "A") == "equal"
    assert BF._length_layer(15, 15, "B") == "equal"
    # 反序（把 A/B 两侧长度对调）必须让同一答案的归层翻转——否则 short/long 整体对调
    assert BF._length_layer(10, 20, "A") != BF._length_layer(20, 10, "A")
    with pytest.raises(ValueError):
        BF._length_layer(10, 20, "C")                 # 答案非 A/B → 形状错误，直接抛


def test_n5_long_layer_below_chance_blocks_pass():
    """① 长层 acc<0.5 且两层都 ≥min_n(=10) → length_stratified=False，必不 pass；
    且用同一把尺子（_binom_two_sided）披露长层 p 值极小。"""
    spec = [("A", 10, 20), ("B", 20, 10),   # 短层 ×2（人类答在更短侧）
            ("B", 10, 20), ("A", 20, 10)]   # 长层 ×2（人类答在更长侧）
    meta, lengths = _meta_per_seg(spec, n_seg=10, prefix="K")   # 20 短 + 20 长，无等长
    picks = {iid: ("A" if la < lb else "B") for iid, (la, lb) in lengths.items()}  # 纯"选短"
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=40, lengths=lengths)
    ls = out["length_strat"]
    assert ls["short"]["n"] == 20 and ls["long"]["n"] == 20
    assert ls["short"]["acc"] == 1.0 and ls["long"]["acc"] == 0.0
    assert ls["long"]["n"] >= BF.GATE["long_layer_min_n"]
    assert out["checks"]["length_stratified"] is False
    assert out["verdict"] != "pass"
    assert out["length_strat_long_p"] is not None and out["length_strat_long_p"] < 0.05


def test_n5_under_min_layer_returns_none_and_does_not_block():
    """② 长层 n 小于下限（8 < min_n=10）→ checks['length_stratified'] is None，
    且不阻塞：其余检验全过 ⇒ verdict 仍为 pass。"""
    spec = ([("A", 10, 20)] * 15 + [("B", 20, 10)] * 15       # 短层 30（人类侧更短）
            + [("B", 10, 20)] * 4 + [("A", 20, 10)] * 4)      # 长层 8（<10，但够 beats_length）
    meta, lengths = _meta_scattered(spec, n_seg=10, prefix="N")
    picks = {iid: meta[iid][1] for iid in meta}               # 全对
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=38, lengths=lengths)
    ls = out["length_strat"]
    assert (ls["short"]["n"], ls["long"]["n"]) == (30, 8)
    assert ls["long"]["n"] < BF.GATE["long_layer_min_n"]
    assert out["checks"]["length_stratified"] is None
    others = {k: v for k, v in out["checks"].items() if k != "length_stratified"}
    assert all(others.values()), others                       # 证明只有 N5 缺席，其余都过
    assert out["verdict"] == "pass"                           # None 不阻塞 pass


def test_n5_missing_length_items_surface_in_missing_bucket_not_equal():
    """③ lengths 缺 key 的题必须落 missing 桶并显形，绝不伪装成 equal（fail-open 根因）。"""
    spec = [("A", 10, 20), ("B", 20, 10), ("B", 10, 20), ("A", 20, 10)]   # 短2+长2，无等长
    meta, lengths = _meta_per_seg(spec, n_seg=10, prefix="G")            # 40 题，equal 恒为 0
    for iid in list(lengths)[:5]:
        del lengths[iid]                                                  # 抽掉 5 题的长度数据
    picks = {iid: meta[iid][1] for iid in meta}                          # 全对
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=40, lengths=lengths)
    ls = out["length_strat"]
    assert ls["missing"]["n"] == 5                       # 5 题进 missing 桶
    assert ls["equal"]["n"] == 0                         # 不得被塞进 equal
    assert ls["short"]["n"] + ls["long"]["n"] == 35      # 40-5 才是有长度数据的题
    assert out["length_missing"]["n"] == 5
    assert out["length_missing"]["rate"] == pytest.approx(5 / 40)
    assert " [缺长度 5]" in BF._fmt_length_strat(ls)     # 报表显形


def test_return_dict_surfaces_answered_and_db_n_separately():
    """④ 修掉重复键：n_answered = 由 picks 现算的已答数（不再被 DB run['n'] 静默覆盖），
    DB 值单列 n_db，两者可并存比对。"""
    picks = {iid: _ANS[iid][1] for iid in list(_ANS)[:30]}   # 只答 30/60
    out = BF.falsify_run(_ANS, _run(picks, n=60), lengths=_LEN)   # run.n=DB 值 60
    assert out["n_answered"] == 30      # 现算的已答数，不被 run['n']=60 覆盖
    assert out["n_db"] == 60            # DB 原值保留在独立键
    assert out["answered_rate"] == pytest.approx(0.5)

