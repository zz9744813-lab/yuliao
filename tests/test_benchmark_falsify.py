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
8. T-N5GATE 回归：判定尺子换成单侧二项（X>=ceil(n/2)+1）并在边界 n=min_n 处参与判定；
   层不足/列缺失必须显形且不混同；数据缺陷超上限 fail-closed 压 pass；坏答案与坏长度
   形状不抛穿且计数显形；wilson 与 acc 同源并标 n_mismatch；n_ans==0 走提前 return。
9. T-N5POLISH 回归：非有限长度（NaN/±inf）挡在 _ab_len 进 bad_len 并抬升缺陷率；
   ls_check 的两个隐式前提（n_long>0 / long_p_one 非 None）在 min_n 极端取值下显式
   判"长层为空"而不是崩；意外降级行带 degraded/error_type 标记且不含 acc 键、其余 run
   完整、md/json 两分支都能打印并以退出码 3 警示；_fmt_length_strat 的"— + 缺陷后缀"
   分支矩阵逐格钉死。
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


# ---------------------------------------------------------------- T-N5GATE 回归
# 会审复审指出 T-N5FIX 只修了"缺长度伪装 equal"，**判定尺子本身没收紧**：判据仍是点
# 估计 acc>=0.5（真 acc=0.4 时 n=10 漏判率 0.3669，比 min_n=3 的 0.3520 还高），注释
# 里的 17%/9% 是另一个口径（X>=ceil(n/2)+1）。以下逐条钉死新尺子与显形口径。

DROP = object()          # _mk 里的哨兵：该题**不写入 lengths**（= 缺长度数据）


def _mk(spec, n_seg=10, prefix="Z"):
    """spec = [(ans, lenval, pick), ...] → meta / lengths / picks。
    lenval：二元组 = 正常 (len_A, len_B)；DROP = 不写 key（缺长度）；其它任意值 = 坏形状。
    id 规则与 _meta_scattered 一致（第 k 题进第 k % n_seg 段），便于手算分层。"""
    meta, lengths, picks = {}, {}, {}
    for k, (ans, lenval, pick) in enumerate(spec):
        iid = f"{prefix}{k % n_seg}-{k // n_seg}"
        meta[iid] = (f"{prefix}SEG{k % n_seg}", ans, "T1")
        if lenval is not DROP:
            lengths[iid] = lenval
        if pick is not None:
            picks[iid] = pick
    return meta, lengths, picks


def _short_hits(h):
    """h 道长层命中 + (10-h) 道长层 miss + 12 道短层命中 ⇒ 长层 n 恰为 10。"""
    return ([("B", (10, 20), "B")] * h                    # 长层、答对
            + [("A", (20, 10), "B")] * (10 - h)           # 长层、答错
            + [("A", (10, 20), "A")] * 6                  # 短层、答对
            + [("B", (20, 10), "B")] * 6)                 # 短层、答对


def test_n5_boundary_layer_exactly_min_n_participates():
    """① 长层 n 恰等于 long_layer_min_n → **必须参与判定**（不是 None），且新尺子在
    ceil(n/2)+1 处切：n=10 时 5/10（点估计 acc=0.5，旧判据会放行）判 False，6/10 判 True。"""
    min_n = BF.GATE["long_layer_min_n"]
    meta, lengths, picks = _mk(_short_hits(5), prefix="B")
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=22, lengths=lengths)
    ls = out["length_strat"]
    assert ls["long"]["n"] == min_n and ls["short"]["n"] == 12
    assert ls["long"]["acc"] == 0.5                             # 点估计正好压在机会线上
    assert out["length_strat_skip"] is None, "n 恰等于 min_n 不属于跳过情形"
    assert out["checks"]["length_stratified"] is False, \
        "acc=0.5 没有单侧证据优于机会线（P[X>=5|10]=0.623），必须判不过"
    assert out["length_strat_long_p_one_sided"] == pytest.approx(0.623, abs=1e-3)

    meta, lengths, picks = _mk(_short_hits(6), prefix="B")
    out6 = BF.falsify_run(meta, _run_m(picks, meta), n_items=22, lengths=lengths)
    assert out6["checks"]["length_stratified"] is True, \
        "6/10 有单侧证据（P[X>=6|10]=0.377<0.5）→ 过门"
    assert out6["length_strat_long_p_one_sided"] < BF.GATE["long_layer_p"]


def test_n5_one_sided_ruler_equals_ceil_half_plus_one():
    """① 补：奇数层的 p 恰等于 0.5 那一点也判不过（注释里承诺的"不进则退"口径），
    且判据与 X>=ceil(n/2)+1 逐 n 等价——长层 n=15 时 8/15 不过、9/15 过。"""
    for h, want in ((8, False), (9, True)):
        spec = ([("B", (10, 20), "B")] * h + [("A", (20, 10), "B")] * (15 - h)
                + [("A", (10, 20), "A")] * 8 + [("B", (20, 10), "B")] * 8)
        meta, lengths, picks = _mk(spec, prefix="O")
        out = BF.falsify_run(meta, _run_m(picks, meta), n_items=31, lengths=lengths)
        assert out["length_strat"]["long"]["n"] == 15
        assert out["checks"]["length_stratified"] is want, f"h={h}"
    # 手算核对：n=15、h=8 时单侧 p 正好 0.5（严格小于才过门）
    assert BF._binom_one_sided_ge(8, 15) == pytest.approx(0.5)
    assert BF._binom_one_sided_ge(9, 15) < 0.5
    assert BF._binom_one_sided_ge(0, 10) == 1.0 and BF._binom_one_sided_ge(10, 10) == pytest.approx(1 / 1024)


def test_n5_all_length_keys_missing_is_reported_not_confused_with_no_column():
    """② 有长度列但题题缺 key（最坏情形）：报表必须显示缺失数、length_missing 不得为
    None、判定必须 fail-closed；与"根本没有长度列"（strat=None → — 且 reason=无长度列）区分开。"""
    spec = [("A", DROP, "A"), ("B", DROP, "B")] * 10           # 20 题，长度全缺
    meta, lengths, picks = _mk(spec, prefix="X")
    lengths["X99"] = (5, 5)                                    # 列存在（只是与本题集不相交）
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=20, lengths=lengths)
    ls = out["length_strat"]
    assert ls["missing"]["n"] == 20 and ls["short"]["n"] == 0 and ls["long"]["n"] == 0
    assert out["length_missing"] == {"n": 20, "rate": 1.0}      # 不得报成 None
    assert out["checks"]["length_stratified"] is False          # fail-closed，不静默跳过
    assert out["length_strat_skip"]["reason"] == "长度数据缺陷超上限"
    cell = BF._fmt_length_strat(ls, BF.GATE["long_layer_min_n"], BF.GATE["len_defect_cap"])
    assert "[缺长度 20]" in cell and "[N5缺陷" in cell          # 最坏情形显形
    assert cell != "—", "旧实现此处正好返回 —，与「根本没长度列」无法区分"

    # 对照：整列不存在（lengths=None）→ 才允许 None / —
    out2 = BF.falsify_run(meta, _run_m(picks, meta), n_items=20, lengths=None)
    assert out2["length_strat"] is None and out2["length_missing"] is None
    assert out2["length_strat_skip"]["reason"] == "无长度列"
    assert BF._fmt_length_strat(out2["length_strat"], 10, 0.3) == "—"


def test_n5_bad_answer_and_bad_length_shape_do_not_raise_and_are_counted():
    """③ 坏答案（非 A/B）与坏长度形状（三元组 / None / 字符串）经 falsify_run 不抛穿，
    分别进 bad_answer / bad_len 桶并显形；且绝不落到 equal（那是另一种 fail-open）。"""
    spec = ([("A", (10, 20), "A"), ("B", (20, 10), "B")] * 8        # 16 短层，全对
            + [("C", (10, 20), "A"), ("", (20, 10), "B"),           # 2 条坏答案
               ("A", (10, 20, 30), "A"), ("B", None, "B"),
               ("A", "10", "A")]                                     # 3 条坏长度形状
            + [("B", (10, 20), "B"), ("A", (20, 10), "A")] * 6)      # 12 长层，全对
    meta, lengths, picks = _mk(spec, prefix="D")
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=len(spec), lengths=lengths)
    ls = out["length_strat"]
    assert ls["bad_answer"]["n"] == 2 and ls["bad_len"]["n"] == 3
    assert ls["equal"]["n"] == 0, "脏数据不得伪装成等长层"
    assert ls["short"]["n"] == 16 and ls["long"]["n"] == 12
    assert out["length_bad"]["n"] == 5
    assert out["length_bad"]["rate"] == pytest.approx(5 / 33, abs=1e-4)
    assert out["length_defect_rate"] == pytest.approx(5 / 33, abs=1e-4)
    cell = BF._fmt_length_strat(ls, BF.GATE["long_layer_min_n"], BF.GATE["len_defect_cap"])
    assert "[坏数据 5]" in cell and "[缺长度 0]" not in cell
    # 缺陷率 5/33≈15% < 上限 30% ⇒ 不因缺陷强制判不过，层几何正常参与
    assert out["checks"]["length_stratified"] is True
    # 判层函数本身的契约不变（答案非 A/B 仍然直接抛，由调用方挡在外面）
    with pytest.raises(ValueError):
        BF._length_layer(10, 20, "C")


def test_wilson_uses_recomputed_hits_and_flags_n_mismatch():
    """④ n_ans != n_db（DB 虚报）时各字段期望值：wilson 与 acc 必须同源——CI 包住现算
    acc（旧实现吃 DB 的 60/60 → CI=[1,1]，把 acc=0.667 挡在自己区间外面），并显式 n_mismatch。"""
    answered_ids = list(_ANS)[:30]
    picks = {iid: _ANS[iid][1] for iid in answered_ids[:20]}                    # 20 对
    picks.update({iid: ("B" if _ANS[iid][1] == "A" else "A")
                  for iid in answered_ids[20:]})                                # 10 错
    run = {"model": "t", "n": 60, "correct": 60, "accuracy": 1.0,               # DB 虚报
           "picks": picks, "created_at": "t"}
    out = BF.falsify_run(_ANS, run, n_items=60, lengths=_LEN)
    assert out["n_answered"] == 30 and out["n_db"] == 60
    assert out["n_mismatch"] is True
    assert out["n_hits"] == 20 and out["n_hits_db"] == 60
    assert out["acc"] == pytest.approx(round(20 / 30, 4))
    assert out["wilson"] == [round(v, 4) for v in BF.wilson(20, 30)]            # 现算口径
    assert out["wilson"][0] <= out["acc"] <= out["wilson"][1], "acc 必须落在自己的 CI 内"
    assert out["wilson"] != [round(v, 4) for v in BF.wilson(60, 60)]
    assert out["answered_rate"] == pytest.approx(0.5)
    assert out["effective_acc"] == pytest.approx(1.0), "P1-4 仍按 DB correct/条目数（口径未动）"
    # 对照：DB 与现算一致时 n_mismatch 为 False，且 wilson 与旧结果同值
    ok = {iid: _ANS[iid][1] for iid in _ANS}
    out2 = BF.falsify_run(_ANS, _run(ok), n_items=60, lengths=_LEN)
    assert out2["n_mismatch"] is False and out2["n_hits"] == 60
    assert out2["wilson"] == [round(v, 4) for v in BF.wilson(60, 60)]


def test_n5_defect_rate_over_cap_forces_fail_closed_and_blocks_pass():
    """⑤ missing 占比超 len_defect_cap(=0.30) ⇒ length_stratified=False 且不得 pass，
    即使其余检验（含收紧后的长层判据）全部通过。边界：占比恰等于 cap 不触发（严格 >）。"""
    base_picks = {iid: _ANS[iid][1] for iid in _ANS}            # 全对（本可 pass）
    ids = list(_ANS)

    def _with_dropped(m):
        lengths = dict(_LEN)
        for iid in ids[:m]:
            del lengths[iid]
        return lengths

    over = BF.falsify_run(_ANS, _run(base_picks), n_items=60, lengths=_with_dropped(19))
    assert over["length_defect_rate"] == pytest.approx(round(19 / 60, 4))
    assert over["length_defect_rate"] > BF.GATE["len_defect_cap"]
    assert over["checks"]["length_stratified"] is False
    assert over["length_strat_skip"]["reason"] == "长度数据缺陷超上限"
    assert over["verdict"] != "pass", "缺陷超限必须压掉 pass"
    assert all(v for k, v in over["checks"].items() if k != "length_stratified"), \
        "证明只有 N5 在承重：其余检验全过"
    assert "[N5缺陷" in BF._fmt_length_strat(over["length_strat"], 10, BF.GATE["len_defect_cap"])

    at_cap = BF.falsify_run(_ANS, _run(base_picks), n_items=60, lengths=_with_dropped(18))
    assert at_cap["length_defect_rate"] == pytest.approx(0.30)
    assert at_cap["checks"]["length_stratified"] is True
    assert at_cap["verdict"] == "pass", "占比正好等于上限不触发（口径为严格大于）"


def test_empty_picks_with_lengths_takes_no_answer_branch():
    """主控已核实 n_missing_len/n_ans 的 ZeroDivisionError 不可达：n_ans==0 在
    falsify_run 开头就 return。此测试把这条分支钉死（picks 全空 / 全非 A/B 两种形状）。
    注意：不要为此加守卫或改返回结构。"""
    lengths = dict(_LEN)                                        # lengths 非空
    for picks in ({}, {iid: "?" for iid in _ANS}):              # 无 pick / pick 全坏
        out = BF.falsify_run(_ANS, {"model": "t", "n": 0, "correct": 0, "accuracy": 0.0,
                                    "picks": picks, "created_at": "t"},
                             n_items=60, lengths=lengths)
        assert out["verdict"] == "fail" and out["reason"] == "无可用答案"
        assert "length_missing" not in out, "提前 return，根本不进 N5 计算"


# ---------------------------------------------------------------- T-N5POLISH 回归
# f4315d3 会审两席（glm-5.3-flash / qwen3.8-flash）的一般/建议级残留四条：
# ① 非有限长度（NaN/±inf）能穿透 _ab_len 的类型检查；② ls_check 判定分支的两个隐式
# 前提（n_long>0、long_p_one 非 None）只靠 min_n 取值域兜着；③ 意外降级行与"真的没过门"
# 共用 verdict=fail，报告自身的可证伪性被削弱；④ 上述新控制流此前无测试。逐条钉死。

NAN, INF, NINF = float("nan"), float("inf"), float("-inf")


def _p_row(model: str, meta: dict, picks: dict) -> dict:
    row = _run_m(picks, meta)
    row["model"] = model
    return row


def test_ab_len_rejects_non_finite_but_keeps_finite_numbers():
    """① 单元级：NaN/±inf 任一侧出现即按坏形状收敛成 None；有限值（含 0 与浮点）原样
    放行——修的是"非有限值可比较性"，不是把 0/浮点误伤成脏数据。"""
    for bad in ((NAN, 20), (20, NAN), (NAN, NAN), (INF, INF), (NINF, NINF),
                (INF, 20), (NINF, 20), (20, INF)):
        assert BF._ab_len({"x": bad}, "x") is None, f"{bad} 必须按坏形状收敛"
    assert BF._ab_len({"x": (10, 20)}, "x") == (10, 20)
    assert BF._ab_len({"x": (0, 0)}, "x") == (0, 0), "0 是有限值，不是脏数据"
    assert BF._ab_len({"x": (10.5, 20)}, "x") == (10.5, 20)
    assert BF._ab_len({"x": (True, 20)}, "x") is None       # bool 挡法不变
    assert BF._ab_len({}, "x") is None and BF._ab_len(None, "x") is None


def test_n5_non_finite_lengths_land_in_bad_len_and_raise_defect_rate():
    """① 端到端：修复前 (NAN,20)+答A 判成 long、(20,NAN)+答B 判成 short（NaN 比较恒
    False ⇒ 归层由答案字母单方面决定，直接污染承重的长层）、(INF,INF) 判成 equal
    （T-N5FIX① 要消灭的"伪装"）。修复后四条全部落 bad_len、equal/short/long 不受污染，
    并且**缺陷率分母第一次摸得到它们**（rate 从 0 抬到 4/28）。"""
    spec = ([("A", (10, 20), "A"), ("B", (20, 10), "B")] * 6       # 12 短层，全对
            + [("B", (10, 20), "B"), ("A", (20, 10), "A")] * 6     # 12 长层，全对
            + [("A", (NAN, 20), "A"), ("B", (20, NAN), "B"),      # 4 条非有限长度
               ("A", (INF, INF), "A"), ("B", (NINF, 20), "B")])
    meta, lengths, picks = _mk(spec, prefix="FN")
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=len(spec), lengths=lengths)
    ls = out["length_strat"]
    assert ls["bad_len"]["n"] == 4 and ls["bad_answer"]["n"] == 0
    assert ls["equal"]["n"] == 0, "(inf,inf) 不得伪装成等长层"
    assert (ls["short"]["n"], ls["long"]["n"]) == (12, 12), "NaN 不得混进 short/long 改变层规模"
    assert ls["short"]["acc"] == 1.0 and ls["long"]["acc"] == 1.0
    assert out["length_bad"]["n"] == 4
    assert out["length_defect_rate"] == pytest.approx(round(4 / 28, 4))
    assert out["length_bad"]["rate"] == pytest.approx(round(4 / 28, 4))
    # 4/28≈14% < 上限 30% ⇒ 不因缺陷强制判不过；长层 12/12 仍照常过收紧后的单侧门槛
    assert out["checks"]["length_stratified"] is True
    cell = BF._fmt_length_strat(ls, BF.GATE["long_layer_min_n"], BF.GATE["len_defect_cap"])
    assert "[坏数据 4]" in cell


def test_n5_empty_long_layer_with_degenerate_min_n_is_undecidable_not_crash(monkeypatch):
    """② 判定分支的两个隐式前提（除法要 n_long>0、比大小要 long_p_one 已算出）此前只靠
    默认 min_n=10 的取值域间接成立。把 long_layer_min_n 临时置 0 造出"空长层"：必须
    既不 ZeroDivisionError 也不 TypeError，且判定可解释（None + reason=长层为空）。
    默认取值域下同一夹具仍归「样本不足」——新守卫不许劫持常规路径。"""
    spec = [("A", (10, 20), "A"), ("B", (20, 10), "B")] * 8      # 16 短层、长层恒空
    meta, lengths, picks = _mk(spec, prefix="EL")
    assert BF.GATE["long_layer_min_n"] == 10, "本次只加守卫，不许改默认值"

    out0 = BF.falsify_run(meta, _run_m(picks, meta), n_items=16, lengths=lengths)
    assert out0["length_strat"]["long"]["n"] == 0
    assert out0["checks"]["length_stratified"] is None
    assert out0["length_strat_skip"]["reason"] == "样本不足"

    monkeypatch.setitem(BF.GATE, "long_layer_min_n", 0)          # 只改测试期取值
    out = BF.falsify_run(meta, _run_m(picks, meta), n_items=16, lengths=lengths)
    assert out["checks"]["length_stratified"] is None, "前提不成立 ⇒ 不可判（既不是崩也不是过）"
    assert out["length_strat_skip"]["reason"] == "长层为空"
    assert out["length_strat_skip"]["n_long"] == 0
    assert out["length_strat_skip"]["min_n"] == 0
    assert out["length_strat_long_p_one_sided"] is None
    # 判定可解释：N5 没参与，其余检验照常各归各位（本夹具模型恰等于"选更短"基线，
    # 唯一没过的是 beats_length；verdict 因此是 weak，不是 None 崩出来的 fail）
    assert out["checks"]["beats_length"] is False
    assert all(out["checks"][k] for k in ("perm_p", "sensitivity", "pos_bias", "answered"))
    assert out["verdict"] == "weak"

    # 对照：min_n=0 但长层非空 ⇒ 判定照常参与（守卫不许把可判的情形一并关掉）
    meta2, lengths2, picks2 = _mk(_short_hits(6), prefix="EN")
    out2 = BF.falsify_run(meta2, _run_m(picks2, meta2), n_items=22, lengths=lengths2)
    assert out2["length_strat"]["long"]["n"] == 10
    assert out2["checks"]["length_stratified"] is True
    assert out2["length_strat_skip"] is None


def test_degraded_run_is_marked_others_stay_intact_and_md_renders(monkeypatch, capsys):
    """③+④① 一条 run 真抛异常（TypeError）：只降级它自己那一行——
    · 形状契约不变：verdict 仍是 "fail"、**不含 acc 键**（main() 的 `if "acc" not in r`
      渲染守卫正依赖这一点，加键会把它打穿）；
    · 新增显式标记 degraded / error_type，集合级 n_degraded 计数 + stderr 条数警示；
    · 其余 run 的检验结果完整不受牵连；
    · main() 的 --md 分支**真的能打印**这一行（含 "fail（"），文本分支 json 可解析，
      两分支都以退出码 3 警示"报告不完整"（区别于 _load「集合没有条目」的 1）。"""
    import json
    meta, lengths = _meta()
    picks = {iid: meta[iid][1] for iid in meta}                 # 全对基线（本身可 pass）
    runs = [_p_row("good-1", meta, picks),
            {"model": "boom", "n": 60, "correct": 60, "accuracy": 1.0,
             "picks": dict(picks), "created_at": "t"},
            _p_row("good-2", meta, picks)]
    real_run = BF.falsify_run

    def fake_run(m, r, n_items=None, lengths=None):
        if r["model"] == "boom":
            raise TypeError("合成意外：picks 引用了已删除的 item")
        return real_run(m, r, n_items=n_items, lengths=lengths)

    monkeypatch.setattr(BF, "falsify_run", fake_run)
    monkeypatch.setattr(BF, "_load", lambda sid: (meta, len(meta), runs, lengths))
    out = BF.falsify_set("BS-synthetic")

    rows = {r["model"]: r for r in out["runs"]}
    assert len(out["runs"]) == 3, "一条坏 run 不许带走其余 run"
    bad = rows["boom"]
    assert bad["verdict"] == "fail"
    assert "acc" not in bad, "不许给降级行新增 acc 键（main 渲染守卫依赖其缺失）"
    assert bad["degraded"] is True and bad["error_type"] == "TypeError"
    assert "检验异常 TypeError" in bad["reason"]
    assert out["n_degraded"] == 1
    assert rows["good-1"]["verdict"] == "pass" and rows["good-2"]["verdict"] == "pass"
    assert rows["good-1"]["checks"]["length_stratified"] is True
    err = capsys.readouterr().err
    assert "1/3" in err and "降级" in err, "降级条数必须打到 stderr"

    capsys.readouterr()                                          # 清屏，只验 main 的输出
    monkeypatch.setattr(BF, "falsify_set", lambda sid: out)
    monkeypatch.setattr(sys, "argv", ["benchmark_falsify.py", "--set", "BS-synthetic", "--md"])
    with pytest.raises(SystemExit) as ei:
        BF.main()
    assert ei.value.code == 3, "有降级 ⇒ 非零退出码 3"
    md = capsys.readouterr().out
    lines = md.splitlines()
    assert "fail（" in md and "检验异常 TypeError" in md, "降级行必须能在 md 里打印出来"
    assert sum(1 for ln in lines if ln.startswith("| boom |")) == 1
    assert sum(1 for ln in lines if ln.startswith("| good-")) == 2, "其余 run 表格行照常"
    assert "**pass**" in md

    monkeypatch.setattr(sys, "argv", ["benchmark_falsify.py", "--set", "BS-synthetic"])
    with pytest.raises(SystemExit) as ei2:
        BF.main()
    assert ei2.value.code == 3
    payload = json.loads(capsys.readouterr().out)                # 文本模式与形状无关
    assert payload["n_degraded"] == 1
    assert [r for r in payload["runs"] if r["model"] == "boom"][0]["degraded"] is True


def test_clean_set_has_no_degraded_marks_and_exits_zero(monkeypatch, capsys):
    """③ 反向对照：全部 run 正常时不许出现 degraded / n_degraded>0 / stderr 警示，
    main() 也不抛 SystemExit（退出码 0）——警示不能变成常态噪声。"""
    meta, lengths = _meta()
    picks = {iid: meta[iid][1] for iid in meta}
    runs = [_p_row("good-1", meta, picks), _p_row("good-2", meta, picks)]
    monkeypatch.setattr(BF, "_load", lambda sid: (meta, len(meta), runs, lengths))
    out = BF.falsify_set("BS-synthetic")
    assert out["n_degraded"] == 0
    assert all("degraded" not in r and "error_type" not in r for r in out["runs"])
    assert capsys.readouterr().err == "", "无降级不许多打警示"
    monkeypatch.setattr(BF, "falsify_set", lambda sid: out)
    monkeypatch.setattr(sys, "argv", ["benchmark_falsify.py", "--set", "BS-synthetic", "--md"])
    assert BF.main() is None, "干净报告退出码 0（不抛 SystemExit）"
    assert "fail（" not in capsys.readouterr().out


def _p_strat(s_n, l_n, miss=0, bad_ans=0, bad_len=0):
    """造 length_strat 单元格输入：acc 用固定手算值（短 0.95 / 长 0.60），n=0 的层
    acc 必为 None——与 falsify_run 的 `round(hit/n,4) if n else None` 口径一致。"""
    def g(n, acc):
        return {"n": n, "acc": acc if n else None}
    return {"short": g(s_n, 0.95), "long": g(l_n, 0.60), "equal": g(0, None),
            "missing": g(miss, 1.0), "bad_answer": g(bad_ans, 1.0), "bad_len": g(bad_len, 1.0)}


def test_fmt_length_strat_branch_matrix_dash_plus_defect_suffixes():
    """④② _fmt_length_strat 的分支矩阵（f4315d3 写进 docstring 的诉求，逐格钉死）：
    任一层为空 ⇒ 单元格以 "—" 开头，但 [缺长度 N] / [坏数据 N] **仍然追加**；只有真的
    一点缺陷都没有、层又不为空时才回到历史格式，只有"层为空且无缺陷"才允许只剩 —。"""
    # 短层空、长层有值
    c = BF._fmt_length_strat(_p_strat(0, 12, miss=3, bad_len=2))
    assert c.startswith("—") and c == "— [缺长度 3] [坏数据 2]"
    assert c != "—", "旧实现在这里正好返回裸 —，与「根本没长度列」无法区分"
    # 长层空、短层有值
    c = BF._fmt_length_strat(_p_strat(12, 0, miss=0, bad_ans=2))
    assert c.startswith("—") and c == "— [坏数据 2]"
    # 两层都空 + 缺长度
    c = BF._fmt_length_strat(_p_strat(0, 0, miss=5))
    assert c == "— [缺长度 5]"
    # 两层都空 + 坏答案与坏长度合并计数
    c = BF._fmt_length_strat(_p_strat(0, 0, bad_ans=1, bad_len=2))
    assert c == "— [坏数据 3]"
    # 两层都空且无缺陷 ⇒ 才允许只剩 —
    assert BF._fmt_length_strat(_p_strat(0, 0)) == "—"
    # 后缀可叠加：min_n 不足 + defect_cap 超限各自追加
    c = BF._fmt_length_strat(_p_strat(0, 0, miss=5), min_n=10)
    assert c == "— [缺长度 5] [N5跳过:n短0/n长0<10]"
    c = BF._fmt_length_strat(_p_strat(0, 0, miss=5), min_n=10, defect_cap=0.3)
    assert "[N5缺陷" in c and "[缺长度 5]" in c and "[N5跳过" in c
    c = BF._fmt_length_strat(_p_strat(4, 6, miss=2), min_n=10)
    assert c == "0.95/0.60 (n=4/6) [缺长度 2] [N5跳过:n短4/n长6<10]", \
        "层非空但不足 ⇒ 数值照常显示，跳过与缺陷后缀各自追加（不以 — 开头）"
    # 不传 min_n/defect_cap ⇒ 历史格式逐字不变（既有精确格式断言依赖这一点）
    assert BF._fmt_length_strat(_p_strat(12, 12)) == "0.95/0.60 (n=12/12)"
    assert BF._fmt_length_strat(_p_strat(12, 12), 10, 0.3) == "0.95/0.60 (n=12/12)"
    assert BF._fmt_length_strat(None) == "—" and BF._fmt_length_strat({}) == "—"



