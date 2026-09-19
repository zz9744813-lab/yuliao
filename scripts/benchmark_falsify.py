"""子基准跑分结果的否掉检验（纪律①：过线读数采信前，先设计最可能否掉它的检验）。

2026-09-19 起 nat-v1 / hvai-v1 出现 ~0.9 的高读数——正是交接 §0.7 附的那条教训
「显著优于此前瓶颈的结果，先设计一个最可能否掉它的检验」的适用场景。本工具对
benchmark_runs 里的已有跑分做六项检验，**只读库、零 LLM 成本**：

· N0 聚类置换检验：题目聚在少量段落上（nat-v1 201 题只来自 15 段），n 不是独立
  样本数（交接 §9 统计 8）。零假设下对**每段**做符号翻转（段内所有题的 A/B 对调
  ——相当于该段的方向随机），重排 B 次答对率的零分布。位置已随机化，0.5 是正确的
  无信号基准。段级翻转保守于按题翻转（段效应共享）。
· N1 位置偏差：报 pick-A 率 vs 答案-A 率。恒选 A 的模型在均衡答案集上应只有 0.5，
  偏离说明读数里有位置成分（评审台 §5 的老坑）。
· N2 留一类型敏感性：剔除任一 corruption_type 后答对率的最大波动。单一类型撑起
  的"过线"不算过线。
· N3 未答率：n / 集合条目。failed_parse 偏多（deepseek 实测 ~9%）会静默缩水 n，
  必须显形（纪律④）。
· N4 长度基线（军师 P1-3）：与"只选较短文本"的简单规则配对比较——位置随机化了
  但长度没有，不显著优于它就不算真信号。
· N5 长度分层正确率（指标硬化）：把已答题按「人类答案在更短侧 / 更长侧」分两层
  （等长单列 equal；lengths 缺 key 的题单列 missing、形状坏掉的题单列 bad_answer /
  bad_len 并显形，不伪装成 equal 也不炸整份报告——T-N5FIX① / T-N5GATE③；非有限值
  NaN/±inf 同属坏形状，一并挡在 _ab_len 进 bad_len，T-N5POLISH①）。两层各
  ≥10 题时"人类侧更长"层必须**在单侧二项意义下优于机会线**才算过门：只看"层规模够
  不够"或只看点估计 acc>=0.5 都收不住尺子（真 acc=0.4 时漏判率 0.367，复算见 GATE
  上方注释）。数据缺陷（缺长度 + 坏形状）占比 > len_defect_cap ⇒ fail-closed 判不过
  （与同文件 N4 口径一致）；层规模不足 ⇒ 判 None 但必须在报表显形"N5 因样本不足跳过"。

结论措辞是**分级的**（provisional 纪律）：
  pass  = N0 p<0.05 且 N2 波动 < 0.05 且 N1 |偏差| < 0.2 且 N3 >= 0.9
          且显著优于"只选较短"基线，且（两层各 ≥10 题时）"人类侧更长"层命中数
          ≥ ceil(n/2)+1（等价于单侧 P[X>=命中数|n,0.5] < 0.5），且长度数据缺陷
          占比不超过 len_defect_cap
  weak  = N0 过但其它有一项存疑
  fail  = N0 未过
用法：
    python scripts/benchmark_falsify.py --set BS-9fb5d1ac1134
    python scripts/benchmark_falsify.py --set BS-9fb5d1ac1134 --md
退出码：0 = 全部 run 检验完成；1 = 集合没有条目；3 = 有 run 被降级（报告不完整，
        降级条数同时打 stderr，见 falsify_set 的 T-N5POLISH③）。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.models import BenchmarkItem, BenchmarkRun  # noqa: E402

FLIP_B = 20000          # 聚类置换重排次数
CLUST_B = 5000          # 段级 bootstrap 次数
SEED = 20260919         # 检验本身也要可复现
GATE = {"perm_p": 0.05, "sensitivity": 0.05, "pos_bias": 0.2, "answered": 0.9,
        "long_layer_acc": 0.5, "long_layer_min_n": 10,
        "long_layer_p": 0.5, "len_defect_cap": 0.30}   # N5：长层判定的最小层规模 / 单侧门槛 / 数据缺陷上限
# T-N5GATE①（订正并收紧 T-N5FIX②）：N5 判据从「点估计 acc>=0.5」换成「单侧二项优于
# 机会线」，同时把 T-N5FIX② 写错的漏判概率改对。代码语义 acc>=0.5 ⇒ 命中数 X>=ceil(n/2)，
# 真 acc=0.4 时的误放行（漏判）概率复算如下（Binom(n,0.4) 尾概率）：
#     n= 3   P[X>=2] = 0.3520      ← min_n=3 时的实际漏判率
#     n=10   P[X>=5] = 0.3669      ← min_n=10 时的实际漏判率
#     n=15   P[X>=8] = 0.2131
# ⇒ 原注释宣称的「n=10≈17%、n=15≈9%」是 P[X>=ceil(n/2)+1] 口径（0.1662 / 0.0950），
#   代码从未实现过，属于把"想要的尺子"当成"已有的尺子"。更要紧的是：**min_n 从 3 提到
#   10 本身几乎不降低漏判率**（0.352→0.367，反而略升，因为偶数层正好 0.5 也算过），
#   真正收紧必须靠 p 门槛。故新判据要求单侧 P[X>=X_obs | n,0.5] < long_layer_p(=0.5)，
#   逐 n 等价于 X>=ceil(n/2)+1（n 为奇数时 X=(n+1)/2 的 p 恰等于 0.5，按"不进则不退"
#   判不过），于是同一真值 acc=0.4 下的漏判率变为：
#     n=10   P[X>=6] = 0.1662（≈17%）      n=15   P[X>=9] = 0.0950（≈9.5%）
#   ——报表口径与注释口径这次对上了。保留 long_layer_acc=0.5 作为显式下限（p 门槛已蕴含
#   它，写出来是为了让"长层过半规模且低于机会线"这类情形在任何 p 取值下都判死）。
# 层规模 < min_n 仍判 None（不参与判定），但必须在返回 dict 与 --md 主表显形为"N5 因
# 样本不足跳过"；数据缺陷（缺长度 + 坏形状）占比 > len_defect_cap 则 fail-closed 判
# 不过——与同文件 N4「基线算不出就不许 pass」的口径一致（T-N5GATE②）。


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _load(set_id: str):
    """读集合条目（段/答案/类型/AB 长度）+ 各 run 的逐题 pick。只读。"""
    with db.session() as s:
        items = s.query(BenchmarkItem).filter_by(set_id=set_id).all()
        if not items:
            raise SystemExit(f"集合 {set_id} 没有条目")
        meta = {x.id: (x.segment_id, x.answer, (x.meta or {}).get("corruption_type", ""))
                for x in items}
        # 军师 P1-3：长度基线必检——"只选较短文本"的简单规则曾是两个集合上的
        # 最高分（nat-v1 规则 0.940 vs 模型 0.915；hvai 0.867 vs 0.827）。
        lengths = {x.id: (len(x.text_a or ""), len(x.text_b or "")) for x in items}
        n_items = len(items)
        runs = []
        for r in s.query(BenchmarkRun).filter_by(set_id=set_id).all():
            picks = (r.detail or {}).get("picks") or {}
            runs.append({"model": r.model, "n": r.n, "correct": r.n_correct,
                         "accuracy": r.accuracy, "picks": picks,
                         "created_at": r.created_at})
    return meta, n_items, runs, lengths


def _binom_two_sided(k: int, n: int) -> float:
    from math import comb
    if n == 0:
        return 1.0
    def pm(x):
        return comb(n, x) * (0.5 ** n)
    p0 = pm(k)
    return min(1.0, sum(pm(x) for x in range(n + 1) if pm(x) <= p0 + 1e-12))


def _binom_one_sided_ge(k: int, n: int) -> float:
    """单侧尾概率 P[X >= k]（零假设 p=0.5）。N5 长层判据用（T-N5GATE①）：值越小 =
    长层命中数越不可能是猜出来的。k<=0 时为 1.0（毫无证据）。"""
    from math import comb
    if n == 0:
        return 1.0
    return min(1.0, sum(comb(n, x) * (0.5 ** n) for x in range(max(0, k), n + 1)))


def _ab_len(lengths: dict, iid) -> tuple[int, int] | None:
    """取 lengths[iid] 的 (A侧, B侧) 二元组；形状坏掉（不是可比较的二元组）返回 None。

    T-N5GATE③：原来 `la, lb = lengths[iid]` 直接解包，一条脏数据（三元组 / None / 字符串）
    就能让 falsify_run → falsify_set → main 整条只读分析链抛穿、全部 run 的报表一起没了。
    这里把异常收敛成 None，由调用方计进 bad_len 桶并显形（fail-closed-but-reported）。
    缺 key（无长度数据）同样返回 None——N5 调用方会先单独判 missing，不会混进 bad_len。
    二元组但元素不是数字（如字符串 "10" 会被解成 "1","0"）也按坏形状处理，不能让脏值
    混进 short/long 层参与判定。
    T-N5POLISH①：非有限值（NaN / ±inf）同样是坏形状，一律挡在这里。旧实现只查类型，
    而 float('nan') 与 float('inf') 都是合法 float，于是能穿透到判层与 N4：
      · _length_layer(nan, 20, 'A') == 'long'、(nan, 20, 'B') == 'short'——NaN 参与比较
        恒 False，判层结果由 ans 字母单方面决定，脏数据直接污染**承重**的长层；
      · _length_layer(inf, inf, ...) == 'equal'（-inf 同理）——正是 T-N5FIX① 要消灭的
        「脏数据伪装成 equal」；
      · N4 里 `pred = 'A' if la < lb else ('B' if lb < la else None)` 对 NaN 两侧都
        False ⇒ pred=None 静默 skip，连缺陷都不算。
    挡住之后由调用方计进 bad_len 桶并算入 length_defect_rate（缺陷率显形、可被
    len_defect_cap 抓住），equal/short/long 只接受真正可比较的有限值。
    """
    try:
        la, lb = lengths[iid]
    except (TypeError, ValueError, KeyError):
        return None
    for v in (la, lb):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if not math.isfinite(v):            # NaN / +inf / -inf 不可比较，按坏形状收敛
            return None
    return la, lb


def _length_layer(la: int, lb: int, ans: str) -> str:
    """按「人类答案落在更短侧 / 更长侧 / 等长」归层——N5 的唯一判层入口。

    隐含约定（T-N5FIX⑤）：lengths[iid] = (len(text_A), len(text_B))，**第0元素=A侧、
    第1元素=B侧**，与 meta 答案 'A'/'B' 同序。顺序若写反，(la<lb) 与 (ans=="A") 的
    比较会整体翻转，short/long 两层对调——好 run 被判死、纯长度驱动反被放行。故把判层
    收敛到这一个函数，并由 tests/test_benchmark_falsify.py::test_length_layer_convention
    _pins_ab_order 用手算 fixture 锁死；ans 非 A/B 视为形状错误直接抛。
    """
    if ans not in ("A", "B"):
        raise ValueError(f"长度判层要求答案为 'A'/'B'（与 lengths 同序），收到 {ans!r}")
    if la == lb:
        return "equal"
    return "short" if (la < lb) == (ans == "A") else "long"


def falsify_run(meta: dict, run: dict, n_items: int | None = None,
                lengths: dict | None = None) -> dict:
    """对单个 run 做六项检验。picks 缺的题按未答处理（不进分母，但进 N3）。

    N3 的分母是**集合条目数**（n_items）——DB 里 run.n 是已答数，用它当分母
    恒为 1，failed_parse 缩水 n 会被静默放过（2026-09-19 deepseek nat-v1 实测 19 题未答）。
    """
    answered = {iid: pick for iid, pick in run["picks"].items() if pick in ("A", "B")}
    n_ans = len(answered)
    if n_ans == 0:
        return {"model": run["model"], "verdict": "fail", "reason": "无可用答案"}
    n_hits = sum(1 for iid, p in answered.items() if meta[iid][1] == p)
    acc = n_hits / n_ans
    ans_a_rate = sum(1 for iid in answered if meta[iid][1] == "A") / n_ans
    pick_a_rate = sum(1 for p in answered.values() if p == "A") / n_ans

    # N0：段级符号翻转置换。段分组只看**已答**的题；每组按伯努利翻转该组全部
    # 题的 pick（A↔B），重算 acc。组内题共享段效应，翻转必须整组进行。
    segs: dict[str, list[str]] = {}
    for iid, pick in answered.items():
        segs.setdefault(meta[iid][0], []).append(iid)
    base_segs = {g: [(meta[i][1], answered[i]) for i in ids] for g, ids in segs.items()}
    rng = random.Random(SEED)
    hits = 0
    for _ in range(FLIP_B):
        k = 0
        for ids in base_segs.values():
            flip = rng.random() < 0.5
            for ans, pick in ids:
                eff = pick if not flip else ("A" if pick == "B" else "B")
                k += (eff == ans)
        if k / n_ans >= acc - 1e-12:
            hits += 1
    perm_p = hits / FLIP_B

    # 段级 cluster bootstrap CI（重采样段，不是题）
    accs = []
    gs = list(base_segs.values())
    for _ in range(CLUST_B):
        num = den = 0
        for g in (rng.choice(gs) for _ in range(len(gs))):
            for ans, pick in g:
                num += (pick == ans)
                den += 1
        if den:
            accs.append(num / den)
    accs.sort()
    lo = accs[int(0.025 * len(accs))]
    hi = accs[int(0.975 * len(accs)) - 1]

    # N1 位置偏差
    pos_bias = abs(pick_a_rate - 0.5) if abs(ans_a_rate - 0.5) < 0.1 else None

    # N2 留一类型敏感性
    types = {meta[iid][2] or "∅" for iid in answered}
    worst = acc
    for drop in types:
        keep = [(iid, p) for iid, p in answered.items() if (meta[iid][2] or "∅") != drop]
        if keep:
            worst = min(worst, sum(1 for iid, p in keep if meta[iid][1] == p) / len(keep))
    sensitivity = acc - worst

    # N3 未答率：分母 = 集合条目数（不是 run.n，那已经是已答数）
    answered_rate = n_ans / n_items if n_items else n_ans / (run["n"] or 1)
    # P1-4：全题有效成功率——漏答按错算（只报已答 acc 是选择性汇报）
    effective_acc = run["correct"] / n_items if n_items else None

    # N4（军师 P1-3）长度基线："只选较短文本"的简单规则读数 + 配对比较。
    # 位置已随机化但**长度没有**——若劣化侧普遍更长，"选短"就是免费高分，
    # 模型读数必须显著超过它才算真信号。
    len_correct = model_beat = baseline_beat = 0
    n_both = 0
    for iid, pick in answered.items():
        pair = _ab_len(lengths, iid) if lengths else None
        if pair is None:
            continue                      # 缺长度 / 长度形状坏：规则无输入，跳过配对
        la, lb = pair
        pred = "A" if la < lb else ("B" if lb < la else None)
        if pred is None:
            continue                      # 等长：规则无答案，算它错，跳过配对
        n_both += 1
        b_ok = (pred == meta[iid][1])
        m_ok = (pick == meta[iid][1])
        len_correct += b_ok
        if m_ok and not b_ok:
            model_beat += 1
        if b_ok and not m_ok:
            baseline_beat += 1
    len_acc = len_correct / n_both if n_both else None
    disc = model_beat + baseline_beat
    beat_p = _binom_two_sided(min(model_beat, baseline_beat), disc) if disc else 1.0

    # N5（指标硬化）长度分层正确率：把已答题按「人类答案在更短侧 / 更长侧」分两层
    # （等长的单列 equal）。总 acc 一高就把长度混淆盖住了——模型完全可能只在
    # "人类侧更短"的题上对（等价于免费"选短"规则）。分母 = 该层已答数。
    # T-N5FIX①：lengths 里缺 key 的题**不再默认 (0,0)** 落进 equal（那会让 short/long
    # 被抽空 → length_strat=None → 与同文件 N4 fail-closed 口径相反的静默 fail-open）。
    # 缺数据的题单独计 missing 桶并在报表显形（缺多少题、占比），不得伪装成 equal。
    # T-N5GATE③：答案非 A/B、长度值非 (int,int) 二元组这类脏数据也不再抛穿——单列
    # bad_answer / bad_len 桶计数显形，与 missing 一起算进"数据缺陷率"。
    layers = ("short", "long", "equal", "missing", "bad_answer", "bad_len")
    strat_n = {g: 0 for g in layers}
    strat_hit = {g: 0 for g in layers}
    length_strat = None
    n_missing_len = 0
    n_bad_shape = 0
    long_p = None
    long_p_one = None
    if lengths:
        for iid, pick in answered.items():
            ok = (pick == meta[iid][1])
            if iid not in lengths:                 # 缺长度数据：单独成桶，显形
                layer = "missing"
            elif meta[iid][1] not in ("A", "B"):   # 人类答案坏：不喂给会抛的 _length_layer
                layer = "bad_answer"
            else:
                pair = _ab_len(lengths, iid)
                layer = "bad_len" if pair is None else _length_layer(pair[0], pair[1], meta[iid][1])
            strat_n[layer] += 1
            strat_hit[layer] += ok
        n_missing_len = strat_n["missing"]
        n_bad_shape = strat_n["bad_answer"] + strat_n["bad_len"]
        # 与 N0/N4 同一把尺子：对「人类侧更长」层披露双侧 p（透明），并**用单侧 p 判定**
        # （T-N5GATE①：点估计 acc>=0.5 在真 acc=0.4 时漏判 0.367，收不住；单侧门槛见
        # GATE 上方复算）。判死口径 = 长层没有单侧证据优于机会线 ⇒ 不许 pass。
        if strat_n["long"]:
            long_p = _binom_two_sided(strat_hit["long"], strat_n["long"])
            long_p_one = _binom_one_sided_ge(strat_hit["long"], strat_n["long"])
        length_strat = {g: {"n": strat_n[g],
                            "acc": round(strat_hit[g] / strat_n[g], 4) if strat_n[g] else None}
                        for g in layers}

    min_n = GATE["long_layer_min_n"]
    n_short, n_long = strat_n["short"], strat_n["long"]
    defect_rate = (n_missing_len + n_bad_shape) / n_ans
    # T-N5GATE②：几种"没判"的状态必须可区分，且缺陷超限要 fail-closed（与 N4 一致）：
    # ① 整列缺失（lengths 为 None/{}）→ None + 显形；② 有列但缺陷占比超上限 → False
    # （不许静默跳过）；③ 层规模不足 → None 但必须显式标"N5 因样本不足跳过"；
    # ④ 长层为空/单侧 p 未算出（判定前提不成立，T-N5POLISH②）→ None + 显式标"长层为空"。
    if length_strat is None:
        ls_check = None
        n5_skip = {"reason": "无长度列", "n_short": None, "n_long": None, "min_n": min_n}
    elif defect_rate > GATE["len_defect_cap"]:
        ls_check = False
        n5_skip = {"reason": "长度数据缺陷超上限", "rate": round(defect_rate, 4),
                   "cap": GATE["len_defect_cap"], "n_short": n_short, "n_long": n_long,
                   "min_n": min_n}
    elif n_short < min_n or n_long < min_n:
        ls_check = None
        n5_skip = {"reason": "样本不足", "n_short": n_short, "n_long": n_long, "min_n": min_n}
    elif n_long <= 0 or long_p_one is None:
        # T-N5POLISH②：把判定分支的两个**隐式前提**显式化——`strat_hit["long"] / n_long`
        # 要求 n_long > 0；`long_p_one < GATE["long_layer_p"]` 要求 long_p_one 已算出
        # （长层为空时是 None，None < 0.5 直接 TypeError）。此前它们只靠"默认
        # long_layer_min_n = 10 ≥ 1 ⇒ 空层必先落进上面的样本不足分支"这一**取值域**
        # 间接成立；配置一旦被改成 0/负数就会崩。前提不成立 ⇒ 判 None 并说明原因，
        # 不得依赖配置取值域，也不得改默认值。
        ls_check = None
        n5_skip = {"reason": "长层为空", "n_short": n_short, "n_long": n_long, "min_n": min_n}
    else:
        ls_check = (strat_hit["long"] / n_long >= GATE["long_layer_acc"]
                    and long_p_one < GATE["long_layer_p"])
        n5_skip = None

    checks = {
        "perm_p": perm_p < GATE["perm_p"],
        "sensitivity": sensitivity < GATE["sensitivity"],
        "pos_bias": pos_bias is None or pos_bias < GATE["pos_bias"],
        "answered": answered_rate >= GATE["answered"],
        "beats_length": (len_acc is not None and acc > len_acc
                         and beat_p < 0.05),   # P1-3：不显著优于"只选较短"不许 pass
        "length_stratified": ls_check,         # N5：None=无长度列/层规模不足/长层为空（见 length_strat_skip）
    }
    gate_ok = all(v for v in checks.values() if v is not None)   # None 不阻塞
    verdict = ("pass" if gate_ok
               else "fail" if not checks["perm_p"]
               else "weak")
    # T-N5FIX④：原 dict 里有两处 "n_answered" 键（一处 = 由 picks 现算的 n_ans、
    # 一处 = DB 的 run["n"]），后值静默覆盖前者。这是真 bug——n_ans 才是所有检验实际
    # 用的已答数。现令 n_answered = n_ans，DB 值单独放 n_db 保留可比对，不再互相覆盖。
    # T-N5GATE④：wilson 原来吃 DB 的 run["correct"]/run["n"]，与现算 acc 不同源——
    # 两者不等时报表里"acc 落在自己的 CI 外面"。现统一到 n_hits/n_ans，并把 n 不一致
    # 显式标成 n_mismatch（不再让读者自己发现 acc 与 CI 打架）。
    return {"model": run["model"], "n_answered": n_ans, "n_db": run["n"],
            "n_mismatch": n_ans != run["n"], "n_hits": n_hits, "n_hits_db": run["correct"],
            "acc": round(acc, 4),
            "wilson": [round(v, 4) for v in wilson(n_hits, n_ans)],
            "cluster_ci": [round(lo, 4), round(hi, 4)],
            "perm_p": round(perm_p, 4), "flip_groups": len(segs),
            "pick_a_rate": round(pick_a_rate, 4), "answer_a_rate": round(ans_a_rate, 4),
            "pos_bias": None if pos_bias is None else round(pos_bias, 4),
            "sensitivity": round(sensitivity, 4), "answered_rate": round(answered_rate, 4),
            "effective_acc": round(effective_acc, 4) if effective_acc is not None else None,
            "length_baseline": {"acc": round(len_acc, 4) if len_acc is not None else None,
                                 "n_pairs": n_both,
                                 "model_beat": model_beat, "baseline_beat": baseline_beat,
                                 "sign_p": round(beat_p, 4)},
            "length_strat": length_strat,
            "length_missing": None if length_strat is None else
                {"n": n_missing_len, "rate": round(n_missing_len / n_ans, 4)},
            "length_bad": None if length_strat is None else
                {"n": n_bad_shape, "rate": round(n_bad_shape / n_ans, 4)},
            "length_defect_rate": None if length_strat is None else round(defect_rate, 4),
            "length_strat_skip": n5_skip,
            "length_strat_long_p": None if long_p is None else round(long_p, 4),
            "length_strat_long_p_one_sided": None if long_p_one is None else round(long_p_one, 4),
            "checks": checks, "verdict": verdict}


def falsify_set(set_id: str) -> dict:
    """跑完一个集合的全部 run；单条意外只降级它自己那一行。

    T-N5POLISH③：降级行沿用 verdict="fail"（不动取值，也不新增 "acc" 键——main() 的
    `if "acc" not in r` 渲染守卫正是靠"降级行不含 acc"这一性质），但**另加显式标记**
    degraded / error_type，把"根本没测成"与"实测没过门"区分开——否则报告自身的可证伪性
    被削弱：读者从判定列看不出这一行是结论还是事故。降级条数以 stderr 汇总 + 非零退出码
    （见 main）双通道警示。
    """
    meta, n_items, runs, lengths = _load(set_id)
    out = []
    n_degraded = 0
    for r in runs:
        try:
            out.append(falsify_run(meta, r, n_items=n_items, lengths=lengths))
        except Exception as e:                    # noqa: BLE001
            # T-N5GATE③：N5/N4 内部的脏数据已在桶里收敛，这里兜住的是"其它"意外
            # （如 picks 引用了已删除的 item）。单条坏 run 只降级它自己那一行，
            # 不许把整份只读报告的其余 run 一起带走。
            n_degraded += 1
            out.append({"model": r["model"], "verdict": "fail",
                        "reason": f"检验异常 {type(e).__name__}: {e}",
                        "degraded": True, "error_type": type(e).__name__})
    if n_degraded:
        print(f"[warn] {set_id}: {n_degraded}/{len(runs)} 个 run 检验异常被降级"
              f"（verdict=fail 是占位、不是实测结论；见行内 degraded/error_type）",
              file=sys.stderr)
    return {"set_id": set_id, "n_items": n_items,
            "gates": GATE,
            "n_degraded": n_degraded,
            "runs": out}


def _fmt_length_strat(strat: dict | None, min_n: int | None = None,
                      defect_cap: float | None = None) -> str:
    """主表"分层acc(短/长)"单元格："0.95/0.60 (n=8/12)"。

    T-N5GATE②③：三种"没判成"的状态必须在同一格里可区分，且**最坏情形不许隐身**——
    原先 `if s['acc'] is None or l['acc'] is None: return "—"` 排在 missing 之前，
    于是"有长度列但题题缺 key"（short/long 被抽空）会显示成与"根本没有长度列"完全
    一样的 —，读者看到的是"没数据"而不是"数据全缺 N 题"。现先算缺陷数再决定降级显示：
      · strat 为 None/{}      → "—"（整列不存在，无从判定）
      · 任一层为空            → "—" 前缀，但仍追加 [缺长度 N] / [坏数据 N]
      · 传 min_n 且任一层不足 → 追加 [N5跳过:n短X/n长Y<min_n]（样本不足，没参与判定）
      · 传 defect_cap 且超限  → 追加 [N5缺陷P%>cap]（此时判定已是 False，不是跳过）
    不传 min_n/defect_cap 时格式与历史一致（既有精确格式断言依赖这一点）。
    """
    if not strat:
        return "—"
    s, l = strat["short"], strat["long"]
    miss = strat.get("missing", {}).get("n") or 0
    bad = (strat.get("bad_answer", {}).get("n") or 0) + (strat.get("bad_len", {}).get("n") or 0)
    if s["acc"] is None or l["acc"] is None:
        cell = "—"
    else:
        cell = f"{s['acc']:.2f}/{l['acc']:.2f} (n={s['n']}/{l['n']})"
    if miss:
        cell += f" [缺长度 {miss}]"
    if bad:
        cell += f" [坏数据 {bad}]"
    if defect_cap is not None:
        n_ans = sum((v.get("n") or 0) for v in strat.values())
        rate = (miss + bad) / n_ans if n_ans else 0.0
        if rate > defect_cap:
            cell += f" [N5缺陷{rate:.0%}>{defect_cap:.0%}·判不过]"
    if min_n is not None and (s["n"] < min_n or l["n"] < min_n):
        cell += f" [N5跳过:n短{s['n']}/n长{l['n']}<{min_n}]"
    return cell


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args()
    out = falsify_set(args.set)
    if args.md:
        print("| 模型 | 已答acc | 全题成功率 | 段bootstrap CI | 置换p(段翻转) | 长度基线(超它?) | 分层acc(短/长) | pickA/ansA | 留一波动 | 未答率 | 判定 |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in out["runs"]:
            if "acc" not in r:
                print(f"| {r['model']} | — | — | — | — | — | — | — | — | — | fail（{r.get('reason','')}）|")
                continue
            lb = r["length_baseline"]
            pb = r["pos_bias"]
            print(f"| {r['model']} | {r['acc']:.3f} | {r['effective_acc']:.3f} "
                  f"| [{r['cluster_ci'][0]:.3f},{r['cluster_ci'][1]:.3f}] "
                  f"| {r['perm_p']:.4f}（{r['flip_groups']}段）"
                  f"| {lb['acc']:.3f}（{lb['model_beat']}:{lb['baseline_beat']} p={lb['sign_p']:.3f}）"
                  f"| {_fmt_length_strat(r['length_strat'], GATE['long_layer_min_n'], GATE['len_defect_cap'])} "
                  f"| {r['pick_a_rate']:.2f}/{r['answer_a_rate']:.2f} "
                  f"| {r['sensitivity']:.3f} | {r['answered_rate']:.2f} | **{r['verdict']}** |")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    # T-N5POLISH③：有 run 被降级 ⇒ 这份报告不完整，用非零退出码把"不完整"变成机器可读
    # 信号（stderr 汇总行 + 退出码双通道，二者受众不同）。取 3 是为了和 _load 的
    # "集合没有条目"SystemExit（默认码 1）区分开。二选一里选退出码而不是再加一条汇总行：
    # --md 模式下 stdout 整块是要粘进评审材料的表格，往里混非表行会破坏 Markdown 表；
    # 而 stderr 已经承载降级条数，再补一行只是重复，退出码才是主控/CI 漏不掉的独立信号。
    # 本工具只读库、零 LLM 成本，非零退出不可能污染任何数据，最坏只是调用方需显式接管。
    if out.get("n_degraded"):
        sys.exit(3)


if __name__ == "__main__":
    main()
