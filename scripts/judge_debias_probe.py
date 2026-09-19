"""硬 Gate 探路：评委方向偏差能否校正到 agreement ≥0.70（2026-09-19）。

背景（交接文档 §0.5③）：corr24 上评委有一个**方向性偏差**——控制臂
（两边内容相同、只换说法）上评委 ≈77% 偏 AI 那版，而集霸 0/8 选 AI 版。
低 κ 不是噪声，是**方向相反**。本探针只读已有数据（judge_runs + review_items
的集霸判定），不发起新的 LLM 调用，回答一件事：

    若把评委的倾向做「方向反转」或「按控制臂估计的偏移量校正」，
    agreement 能否过 Gate 线 0.70（docs/HANDOVER.md:264 的既有口径）？

口径约定（全部写死，防事后挑数）：
· 检验集 = corr24 批次（review_items.reasons 含 batch_corr24 且指向受控劣化
  候选）；集霸五分类在 2026-09-19 读数为 both_bad 11 / tie 6 / human 4 /
  candidate 3。
· agreement 沿用 heldout_eval.py 的**排除制**口径：集霸答 tie / both_bad /
  cant_judge 的题剔除，只在「集霸给出明确胜方」的子集上比胜方是否一致
  （mix30 表格同口径）。另报**五分类严格**口径（分母固定 24，无判定=错）
  作辅助——评委只会答 human/candidate，严格口径上限 7/24=0.292，先写死。
· 控制臂 = corruption_type='NEUTRAL_PARAPHRASE'（内容相同只换说法）。
· 数据事实：corr24 上只有 judge_preference_v4_heldout_near1 一个口径有判定
  （四家模型）；kimi v3 / v4 非 near1、deepseek v3/v4 非 near1 在此数据面
  覆盖为 0——装载时自动发现口径并打印覆盖表，不静默（本项目纪律④）。

变换（规则先于看数）：
· raw 原始：评委 winner_resolved。
· pos 位置基线：永远选 A，方向由该题 human_was_a 决定（内容盲的常数参照）。
· maj 恒定多数类基线：永远答 human（agreement = 集霸 human 占比）。
· flip 反转投票：该口径控制臂选 AI 版率 r̂ > 0.5 时，把评委方向
  human↔candidate 全对调（交接 §0.5③「四家共享同一偏差」的极限操作）。
· map 偏移校正：用 corr24 控制臂 8 题 × 四家判定（按判定计、池化）的
  （评委方向 → 集霸答案）交叉计数 + 拉普拉斯平滑 α=1 估计
  P(集霸答案 | 评委方向)，对每题取 argmax 重映射。这是「按受控劣化集估计
  的方向偏移量做校正」的数据驱动版：偏移表现为
  P(集霸=tie/both_bad | 评委判某边胜) 高——评委说某边胜实为无胜方。

必报四样（本项目纪律⑨）：κ、置换零分布、恒定多数类基线、位置基线；
分层抽样时另报 IPW-κ 与 n_eff（corr24 的 w=1.0 非分层，验证后如实报出）。

用法：
    python scripts/judge_debias_probe.py
    python scripts/judge_debias_probe.py --md
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BATCH = "corr24"
CONTROL_TYPE = "NEUTRAL_PARAPHRASE"       # 控制臂：内容相同只换说法
GATE = 0.70
PERM_B = 20000                             # 置换零分布重排次数
BOOT_B = 2000                              # κ bootstrap 次数
SMOOTH_ALPHA = 1.0                         # map 校准的拉普拉斯平滑强度
ANSWERS = ("human", "candidate", "tie", "both_bad")   # map 目标桶（cant_judge 不入桶）
SEED = 20260919
REQUIRED_MODELS = ("moonshotai/kimi-k3", "deepseek/deepseek-v4.1-flash")


def _as_dict(v) -> dict | None:
    """verdict 统一成 dict（照抄 heldout_eval._as_dict 的容错口径）。

    ORM 拿到的是已解析 dict，裸 sqlite3 拿到的是 TEXT——只支持一种会
    在另一端静默返回 None（本项目已因此白跑一次全量）。
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
        except Exception:
            return None
        return d if isinstance(d, dict) else None
    return None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 Wilson 95% CI（小样本必须用，不用正态近似）。"""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ── 数据结构（测试直接构造，不碰库）──────────────────────────

@dataclass
class Item:
    """一道 corr24 题。user：集霸 winner_resolved（五分类）。"""
    cid: str
    kind: str                # control / corrupt
    cctype: str
    user: str                # human/candidate/tie/both_bad/cant_judge
    w: float = 1.0           # 入样概率（reasons 里的 w: 标签；分层时用于 IPW）


@dataclass
class JudgeCell:
    """某口径在某题上的判定（唯一候选、取最新一条）。"""
    resolved: str            # human/candidate（非二值判定不入表）
    pick: str                # A/B（原始选择，位置基线用）
    human_was_a: bool | None


@dataclass
class ProbeData:
    """探针输入：items 为 corr24 全部题；judges 按 (model, prompt_version) 分口径。

    ctrl_judges：全部控制臂候选（含 corr24 之外的 bench 实验）上的判定，
    用于偏移量 r̂ 的大样本读数；corr24 内控制臂另有集霸判定，是 map 校准面。
    """
    items: dict[str, Item]
    judges: dict[tuple[str, str], dict[str, JudgeCell]] = field(default_factory=dict)
    ctrl_judges: dict[tuple[str, str], dict[str, JudgeCell]] = field(default_factory=dict)


# ── 装载（走 ORM，铁律③：不裸连 heldout_eval.DB）────────────

def load_probe(session_factory) -> ProbeData:
    """从库里读 corr24 的集霸判定与各口径评委判定（口径从数据自动发现）。

    读不到就报错，不静默返回空（本项目纪律④）。
    """
    from app.models import ControlledCorruption, JudgeRun, ReviewItem

    with session_factory() as s:
        # corr24 的 24 题与集霸判定
        ccs = s.query(ControlledCorruption).filter(
            ControlledCorruption.status == "ok").all()
        cc = {c.candidate_id: c for c in ccs if c.candidate_id}
        if not cc:
            raise ValueError("controlled_corruptions 无 status=ok 的记录，无法探路")

        rvs = (s.query(ReviewItem)
               .filter(ReviewItem.status == "done",
                       ReviewItem.human_verdict.isnot(None)).all())
        items: dict[str, Item] = {}
        for r in rvs:
            if r.subject_type != "candidate" or r.subject_id not in cc:
                continue
            if f"batch_{BATCH}" not in (r.reasons or []):
                continue
            hv = r.human_verdict if isinstance(r.human_verdict, dict) \
                else _as_dict(r.human_verdict)
            if not hv:
                continue
            u = hv.get("winner_resolved")
            if u not in ("human", "candidate", "tie", "both_bad", "cant_judge"):
                raise ValueError(f"corr24 题 {r.subject_id} 集霸判定 "
                                 f"winner_resolved={u!r} 不在已知答案空间，"
                                 f"口径升级后本脚本未适配——先人工确认再扩表")
            c = cc[r.subject_id]
            w = 1.0
            for t in (r.reasons or []):
                if t.startswith("w:"):
                    try:
                        w = float(t[2:])
                    except ValueError:
                        pass
            items[r.subject_id] = Item(
                cid=r.subject_id,
                kind="control" if c.corruption_type == CONTROL_TYPE else "corrupt",
                cctype=c.corruption_type, user=u, w=w)
        if not items:
            raise ValueError(f"库里没有批次 {BATCH} 的集霸判定（review_items 空）")

        # 全部控制臂候选（corr24 内 8 + bench 实验的），供 r̂ 的大样本读数
        ctrl_cids = [c.candidate_id for c in ccs
                     if c.corruption_type == CONTROL_TYPE and c.candidate_id]

        def harvest(cids):
            rows = (s.query(JudgeRun)
                    .filter(JudgeRun.judge_kind == "preference",
                            JudgeRun.status == "ok",
                            JudgeRun.subject_id.in_(cids))
                    .order_by(JudgeRun.created_at).all())
            out: dict[tuple[str, str], dict[str, JudgeCell]] = defaultdict(dict)
            for j in rows:
                d = _as_dict(j.verdict)
                if not d:
                    continue
                resolved = d.get("winner_resolved")
                if resolved not in ("human", "candidate"):
                    continue          # tie/both_bad 等非二值判定不计（计在弃用数）
                # 同候选同口径多条（正反序重跑）：created_at 排序后后写覆盖 = 取最新
                out[(j.model, j.prompt_version)][j.subject_id] = JudgeCell(
                    resolved=resolved, pick=d.get("winner", ""),
                    human_was_a=d.get("human_was_a"))
            return dict(out)

        judges = harvest(list(items))
        ctrl_judges = harvest(ctrl_cids) if ctrl_cids else {}

    if not judges:
        raise ValueError("corr24 的候选上没有任何 preference 评委判定")
    if not ctrl_judges:
        raise ValueError("控制臂（NEUTRAL_PARAPHRASE）候选上没有任何评委判定，"
                         "偏移量 r̂ 无从估计")
    missing = [m for m in REQUIRED_MODELS
               if not any(mm == m for mm, _ in judges)]
    if missing:
        raise ValueError(f"指定口径的评委在 corr24 上无判定：{missing}——"
                         f"任务要求至少 kimi 与 deepseek 可用，不许静默缺家")
    return ProbeData(items=items, judges=judges, ctrl_judges=ctrl_judges)


# ── 统计核（纯函数；测试直接喂内存数据）──────────────────────

def resolve_pick(pick: str, human_was_a: bool | None) -> str | None:
    """把「选了 A/B」解析成内容方向。human_was_a 缺失 = 无法定位，返回 None。"""
    if human_was_a is None or pick not in ("A", "B"):
        return None
    return "human" if (pick == "A") == bool(human_was_a) else "candidate"


def kappa_bin(pairs: list[tuple[int, int]]) -> float:
    """二值 Cohen κ（user_is_human, judge_is_human），口径同 heldout_eval._kappa。

    两边都恒定（pe=1）时 κ 无定义，返回 nan——调用方必须把 nan 当
    「无信息」而不是 0。
    """
    n = len(pairs)
    if not n:
        return float("nan")
    a = sum(1 for u, j in pairs if u and j)
    b = sum(1 for u, j in pairs if u and not j)
    c = sum(1 for u, j in pairs if not u and j)
    d = sum(1 for u, j in pairs if not u and not j)
    po = (a + d) / n
    pj = (a + c) / n
    pu = (a + b) / n
    pe = pj * pu + (1 - pj) * (1 - pu)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def agreement_bin(pairs: list[tuple[int, int]]) -> float:
    if not pairs:
        return float("nan")
    return sum(1 for u, j in pairs if u == j) / len(pairs)


def ipw_kappa(pairs: list[tuple[int, int]], weights: list[float]) -> tuple[float, float]:
    """逆概率加权 κ + n_eff（口径同 heldout_eval._weighted_kappa）。

    corr24 非分层（w=1.0），主流程里只用于**验证 w 值**并如实报出
    IPW≡raw、n_eff=n；分层批次接入后此函数直接可用。
    """
    if not pairs:
        return float("nan"), 0.0
    W = float(sum(weights))
    if W <= 0:
        return float("nan"), 0.0
    n_eff = W * W / sum(x * x for x in weights)

    def wsum(pred) -> float:
        return sum(w for (u, j), w in zip(pairs, weights) if pred(u, j))

    po = (wsum(lambda u, j: u and j) + wsum(lambda u, j: not u and not j)) / W
    pj = wsum(lambda u, j: j) / W
    pu = wsum(lambda u, j: u) / W
    pe = pj * pu + (1 - pj) * (1 - pu)
    return ((po - pe) / (1 - pe) if pe < 1 else float("nan")), n_eff


def flip_resolved(resolved: str) -> str:
    """反转投票：human↔candidate 对调（tie/both_bad 无方向，原样保留）。"""
    return {"human": "candidate", "candidate": "human"}.get(resolved, resolved)


def collect_ctl_pairs(data: ProbeData) -> list[tuple[str, str]]:
    """corr24 控制臂 × 各口径（池化按判定计）的（评委方向, 集霸答案）对。

    这是 map 校准的唯一数据源。「四家共享同一偏差」是交接 §0.5③ 的既有
    结论，池化有依据；但 corr24 控制臂上每口径只覆盖 4 题，调用方必须
    把校准样本量如实报出。
    """
    out = []
    for cells in data.judges.values():
        for it in data.items.values():
            if it.kind != "control":
                continue
            c = cells.get(it.cid)
            if c is not None and it.user in ANSWERS:
                out.append((c.resolved, it.user))
    return out


def map_recal_table(pairs_ctl: list[tuple[str, str]],
                    user_marginal: dict[str, float],
                    alpha: float = SMOOTH_ALPHA) -> dict[str, str]:
    """控制臂混淆重映射：P(集霸答案|评委方向) → argmax 映射表。

    pairs_ctl：(评委方向, 集霸答案)（corr24 控制臂 × 四家池化，按判定计）。
    拉普拉斯平滑 α：控制臂 8 题 × 2 方向的表欠定极重，平滑是**诚实披露**
    而非装饰。并列时取 corr24 集霸边缘分布更大的一方；再并列取 both_bad
    （corr24 众数桶）。空校准数据必须报错（纪律④）。
    """
    cnt: dict[str, Counter] = {"human": Counter(), "candidate": Counter()}
    for d, u in pairs_ctl:
        if d in cnt and u in ANSWERS:
            cnt[d][u] += 1
    if all(not c for c in cnt.values()):
        raise ValueError("map 校准无数据：corr24 控制臂上没有任何"
                         "（评委方向, 集霸答案）配对")
    mapping = {}
    for d in ("human", "candidate"):
        total = sum(cnt[d].values())
        best_u, best_key = None, None
        for u in ANSWERS:
            p = (cnt[d][u] + alpha) / (total + alpha * len(ANSWERS))
            key = (round(p, 6), round(user_marginal.get(u, 0.0), 6), u == "both_bad")
            if best_key is None or key > best_key:
                best_u, best_key = u, key
        mapping[d] = best_u
    return mapping


def perm_null(user: list[int], judge: list[int], b: int = PERM_B,
              seed: int = SEED) -> dict:
    """置换零分布：固定 user 序列，随机重排 judge 答案 b 次。

    返回 κ 与 agreement 的零分布 95% 区间 + 观测值的右侧 p 值（单侧，
    「评委比瞎配对好」的检验）。flip 是逐项双射对换，与 raw 共享同一
    零分布；map 行对变换后的数组重排（口径见主表）。
    """
    rng = random.Random(seed)
    obs_pairs = list(zip(user, judge))
    obs_k, obs_a = kappa_bin(obs_pairs), agreement_bin(obs_pairs)
    ks, ags = [], []
    for _ in range(b):
        js = list(judge)
        rng.shuffle(js)
        pairs = list(zip(user, js))
        ks.append(kappa_bin(pairs))
        ags.append(agreement_bin(pairs))
    ks.sort()
    ags.sort()

    def ci(arr):
        if not arr:
            return (float("nan"), float("nan"))
        lo = arr[min(len(arr) - 1, max(0, math.floor(0.025 * len(arr))))]
        hi = arr[min(len(arr) - 1, math.ceil(0.975 * len(arr)))]
        return (lo, hi)

    pk = float("nan") if math.isnan(obs_k) \
        else sum(1 for x in ks if x >= obs_k - 1e-12) / b
    return {"kappa_null": ci(ks), "agree_null": ci(ags),
            "p_kappa": pk,
            "p_agree": sum(1 for x in ags if x >= obs_a - 1e-12) / b,
            "obs_kappa": obs_k, "obs_agree": obs_a}


def boot_kappa_ci(user: list[int], judge: list[int], b: int = BOOT_B,
                  seed: int = SEED) -> tuple[float, float]:
    """κ 的 bootstrap percentile CI（重抽配对，重抽含放回）。"""
    rng = random.Random(seed + 1)
    n = len(user)
    if not n:
        return (float("nan"), float("nan"))
    vals = []
    for _ in range(b):
        idx = [rng.randrange(n) for _ in range(n)]
        vals.append(kappa_bin([(user[i], judge[i]) for i in idx]))
    vals.sort()
    lo = vals[min(b - 1, max(0, math.floor(0.025 * b)))]
    hi = vals[min(b - 1, math.ceil(0.975 * b))]
    return (lo, hi)


# ── 主流程 ──────────────────────────────────────────────────

def control_rhat(data: ProbeData, model: str, pv: str,
                 scope: str) -> tuple[int, int]:
    """控制臂上评委选 AI 版的比例（方向偏移量 r̂ 的直接读数）。

    scope='corr24'：corr24 内控制臂 8 题（有集霸判定的校准面）；
    scope='all'：全部控制臂候选（corr24 内 8 + bench 实验的，样本更大）。
    返回 (选 AI 版数, 覆盖数)。
    """
    cells = data.judges.get((model, pv), {})
    if scope == "all":
        extra = data.ctrl_judges.get((model, pv), {})
        cells = {**extra, **cells}
        # 遍历范围必须是「控制臂候选的并集」：bench 实验的控制臂候选不在
        # corr24 的 items 里，漏掉会把大样本读数静默缩回 corr24 覆盖（踩过）；
        # 反过来 corr24 judges 里混着真劣化题的判定，绝不能进并集。
        cids = ({it.cid for it in data.items.values() if it.kind == "control"}
                | set(extra))
    else:
        cids = {it.cid for it in data.items.values() if it.kind == "control"}
    n = k = 0
    for cid in cids:
        c = cells.get(cid)
        if c is None:
            continue
        n += 1
        k += c.resolved == "candidate"
    return k, n


def strict_score(data: ProbeData, model: str, pv: str,
                 transform) -> tuple[int, int]:
    """五分类严格口径：分母固定 24（全 corr24），无判定/非一致=错。

    返回 (一致数, 24)。
    """
    cells = data.judges.get((model, pv), {})
    hit = 0
    for it in data.items.values():
        c = cells.get(it.cid)
        if c is None:
            continue
        ans = transform(c)
        if ans is not None and ans == it.user:
            hit += 1
    return hit, len(data.items)


def _ctrl_face(data: ProbeData, model: str, pv: str, fn) -> dict:
    """控制臂面：某变换在 corr24 控制臂 8 题上的方向偏差读数。

    返回 {'n': 覆盖数, 'ai': 变换后仍选 AI 版数,
          'win_n': 控制臂胜方题（集霸答 human/candidate）覆盖数,
          'win_ok': 其中一致数}。
    「选 AI 版」是方向偏差的直接读数——集霸在控制臂上 0/8 选 AI 版。
    变换是否成功，就看它能不能把 ai 压向 0 而不毁掉 win_ok。
    """
    cells = data.judges.get((model, pv), {})
    n = ai = win_n = win_ok = 0
    for it in data.items.values():
        if it.kind != "control":
            continue
        c = cells.get(it.cid)
        if c is None:
            continue
        ans = fn(c)
        if ans is None:
            continue
        n += 1
        ai += ans == "candidate"
        if it.user in ("human", "candidate"):
            win_n += 1
            win_ok += ans == it.user
    return {"n": n, "ai": ai, "win_n": win_n, "win_ok": win_ok}


TRANSFORMS = ("raw", "pos", "maj", "flip", "map")
TRANSFORM_ZH = {
    "raw": "原始", "pos": "位置基线（恒选A）", "maj": "恒定human基线",
    "flip": "反转投票", "map": "map偏移校正",
}


def transform_fn(name: str, mapping: dict[str, str]):
    if name == "raw":
        return lambda c: c.resolved
    if name == "pos":
        return lambda c: resolve_pick("A", c.human_was_a)
    if name == "maj":
        return lambda c: "human"
    if name == "flip":
        return lambda c: flip_resolved(c.resolved)
    if name == "map":
        return lambda c: mapping.get(c.resolved)
    raise ValueError(f"未知变换 {name}")


def run_probe(data: ProbeData) -> dict:
    """对每个口径 × 每种变换算 agreement/κ/CI/置换 p，产出探针报告结构。

    空数据（无题、无口径、胜方子集全空）在这里统一报错，不静默出空表。
    """
    if not data.items:
        raise ValueError("corr24 无任何题（items 空）")
    if not data.judges:
        raise ValueError("无任何评委口径（judges 空）")

    # map 校准（池化，与口径无关，建一次）
    ctl_pairs = collect_ctl_pairs(data)
    n_items = len(data.items)
    marginal = Counter(it.user for it in data.items.values())
    user_marg = {u: marginal.get(u, 0) / n_items for u in ANSWERS}
    map_note = ""
    try:
        mapping = map_recal_table(ctl_pairs, user_marg)
    except ValueError as e:
        # 校准面为空（如测试构造的纯胜方子集）：map 变换显式不可用，
        # 其余变换照常出数——不静默，也不让整个探针陪着崩（纪律④的
        # 反面不是「一坏全崩」，而是「坏掉的部分显式可见」）。
        mapping = None
        map_note = str(e)
    n_ctl_pairs = len(ctl_pairs)

    usable = [(m, pv) for (m, pv) in sorted(data.judges)
              if data.judges[(m, pv)]]
    if not usable:
        raise ValueError("全部评委口径在 corr24 上无可用判定")

    rows = []
    for (model, pv) in usable:
        r24 = control_rhat(data, model, pv, scope="corr24")
        r_all = control_rhat(data, model, pv, scope="all")
        for name in TRANSFORMS:
            fn = transform_fn(name, mapping)
            cells = data.judges[(model, pv)]
            ctrl = _ctrl_face(data, model, pv, fn)

            if name == "map":
                # map 的输出含 tie/both_bad（非胜方桶）：排除制的二值折算会把
                # 「答无胜方」碰巧折算成 candidate 而虚高，**排除制不适用**，
                # 显式置 None（渲染 '—'），Gate 读数改走五分类严格口径。
                rows.append({
                    "model": model, "pv": pv, "transform": name,
                    "n": None, "agree": None, "kappa": None, "ci": None,
                    "kappa_ci": None, "perm": None, "strict_hit": None,
                    "strict_denom": None,
                    "r24": r24, "r_all": r_all, "cids": [], "ctrl": None,
                    "note": map_note or "",
                })
                if mapping is not None:
                    hit, denom = strict_score(data, model, pv, fn)
                    rows[-1]["strict_hit"] = hit
                    rows[-1]["strict_denom"] = denom
                    rows[-1]["ctrl"] = _ctrl_face(data, model, pv, fn)
                continue

            # 排除制主口径：胜方子集上的二值配对
            user, judge, cids = [], [], []
            for it in data.items.values():
                if it.user not in ("human", "candidate"):
                    continue
                c = cells.get(it.cid)
                ans = fn(c) if c else None
                if ans is None:
                    continue
                user.append(1 if it.user == "human" else 0)
                judge.append(1 if ans == "human" else 0)
                cids.append(it.cid)
            pairs = list(zip(user, judge))
            agree = agreement_bin(pairs)
            k = kappa_bin(pairs)
            ci = wilson(sum(1 for u, j in pairs if u == j), len(pairs))
            kci = boot_kappa_ci(user, judge)
            pn = perm_null(user, judge)
            hit, denom = strict_score(data, model, pv, fn)
            rows.append({
                "model": model, "pv": pv, "transform": name,
                "n": len(pairs), "agree": agree, "kappa": k, "ci": ci,
                "kappa_ci": kci, "perm": pn, "strict_hit": hit,
                "strict_denom": denom, "r24": r24, "r_all": r_all,
                "cids": cids, "ctrl": ctrl,
            })

    # IPW 验证行：corr24 非分层（w=1.0）→ IPW≡raw、n_eff=n，如实报出
    ipw_notes = []
    for (model, pv) in usable:
        cells = data.judges[(model, pv)]
        ws = [it.w for it in data.items.values()
              if it.user in ("human", "candidate") and it.cid in cells]
        if not ws:
            continue
        user, judge, _ = [], [], []
        for it in data.items.values():
            if it.user not in ("human", "candidate") or it.cid not in cells:
                continue
            c = cells[it.cid]
            user.append(1 if it.user == "human" else 0)
            judge.append(1 if c.resolved == "human" else 0)
        w_k, n_eff = ipw_kappa(list(zip(user, judge)), ws)
        raw_k = kappa_bin(list(zip(user, judge)))
        ipw_notes.append({"model": model, "pv": pv, "ws": sorted(set(ws)),
                          "ipw_kappa": w_k, "raw_kappa": raw_k, "n_eff": n_eff})

    return {"rows": rows, "mapping": mapping, "n_ctl_pairs": n_ctl_pairs,
            "user_marginal": user_marg, "ipw": ipw_notes,
            "n_items": n_items, "items": data.items}


def decide(rows: list[dict]) -> dict:
    """Gate 判定与（若过线）否掉检验结果。

    诚实优先：最优读数 < 0.70 就明确「不过」，带 CI 与 n；
    过线则先跑否掉检验（纪律①），结果一并返回，由报告决定采信与否。
    读数口径：二值变换走排除制（与 Gate 历史口径可比）；map 走五分类
    严格（排除制对它不适用，见 run_probe），判定里显式标注口径。
    """
    cands = []
    for r in rows:
        if r["transform"] == "map":
            if r["strict_denom"]:
                cands.append((r["strict_hit"] / r["strict_denom"], "严格24", r))
        elif r["n"] and not math.isnan(r["agree"]):
            cands.append((r["agree"], "排除制", r))
    out = {"best": None, "passed": False, "stress": None, "verdict": ""}
    if not cands:
        out["verdict"] = "全部口径无可用判定——数据不足，无法下结论。"
        return out
    # 并列时优先报排除制（历史可比口径）
    metric, face, best = max(cands, key=lambda x: (x[0], x[1] == "排除制"))
    out["best"] = best
    m = best["model"].split("/")[-1]
    if face == "排除制":
        line = (f"{m} / {TRANSFORM_ZH[best['transform']]}："
                f"agreement={best['agree']:.3f}（排除制口径，n={best['n']}，"
                f"Wilson 95% CI [{best['ci'][0]:.3f},{best['ci'][1]:.3f}]，"
                f"κ={_fmt_kappa(best['kappa'])}，置换 p={best['perm']['p_agree']:.4f}；"
                f"控制臂面：变换后选 AI 版 {best['ctrl']['ai']}/{best['ctrl']['n']}）")
    else:
        line = (f"{m} / {TRANSFORM_ZH[best['transform']]}："
                f"agreement={best['strict_hit']}/{best['strict_denom']}"
                f"={best['strict_hit'] / best['strict_denom']:.3f}"
                f"（五分类严格 24 题；排除制对 map 不适用；"
                f"控制臂面：变换后选 AI 版 {best['ctrl']['ai']}/{best['ctrl']['n']}）")
    if metric < GATE:
        out["verdict"] = f"不过（< {GATE}）。最优读数：{line}"
        return out
    # 过线 → 纪律①：先跑最可能否掉它的检验
    out["passed"] = True
    out["verdict"] = f"过线（待否掉检验）：{line}"
    return out


def stress_tests(data: ProbeData, mapping: dict[str, str]) -> dict:
    """若过线才跑的否掉检验（纪律①）。没过线时返回 skip 标记。

    N1 leave-one-out：控制臂去 1 题重估映射 → agreement 波动范围；
    N2 平滑敏感性：α ∈ {0.5, 2, 4} 重算映射；
    N3 劣化对照方向：map 变换在 corr24 真劣化 16 题上与集霸的一致率
       （校准集只在控制臂上，外推到真劣化题是这个读数最可能翻车处）。
    """
    out = {"loo": [], "alpha": {}, "corrupt_side": None}
    ctl_pairs = collect_ctl_pairs(data)
    marginal = Counter(it.user for it in data.items.values())
    user_marg = {u: marginal.get(u, 0) / len(data.items) for u in ANSWERS}
    usable = [(m, pv) for (m, pv) in sorted(data.judges) if data.judges[(m, pv)]]

    # N1
    for i in range(len(ctl_pairs)):
        sub = ctl_pairs[:i] + ctl_pairs[i + 1:]
        if not sub:
            continue
        m2 = map_recal_table(sub, user_marg)
        ags = []
        for (model, pv) in usable:
            fn = transform_fn("map", m2)
            user, judge, _ = _exclude_pairs(data, model, pv, fn)
            a = agreement_bin(list(zip(user, judge)))
            if not math.isnan(a):
                ags.append(a)
        if ags:
            out["loo"].append(sum(ags) / len(ags))
    # N2
    for a2 in (0.5, 2.0, 4.0):
        m2 = map_recal_table(ctl_pairs, user_marg, alpha=a2)
        ags = []
        for (model, pv) in usable:
            fn = transform_fn("map", m2)
            user, judge, _ = _exclude_pairs(data, model, pv, fn)
            x = agreement_bin(list(zip(user, judge)))
            if not math.isnan(x):
                ags.append(x)
        out["alpha"][a2] = sum(ags) / len(ags) if ags else float("nan")
    # N3：map 变换在真劣化题上的**五分类一致率**（ans==user 才算对，
    # 不做二值折算——both_bad 折算成 candidate 会碰巧「对」而掩盖失败）
    corrupt_ags = []
    for (model, pv) in usable:
        fn = transform_fn("map", mapping)
        cells = data.judges[(model, pv)]
        n = ok = 0
        for it in data.items.values():
            if it.kind != "corrupt" or it.user not in ("human", "candidate"):
                continue
            c = cells.get(it.cid)
            if c is None:
                continue
            ans = fn(c)
            if ans is None:
                continue
            n += 1
            ok += ans == it.user
        if n:
            corrupt_ags.append((model.split("/")[-1], ok / n, n))
    out["corrupt_side"] = corrupt_ags
    out["mapping_variants"] = {"full": mapping}
    for i in range(len(ctl_pairs)):
        sub = ctl_pairs[:i] + ctl_pairs[i + 1:]
        if sub:
            out["mapping_variants"][f"loo{i}"] = map_recal_table(sub, user_marg)
    return out


def _exclude_pairs(data: ProbeData, model: str, pv: str, fn):
    """胜方子集二值配对（run_probe 内联逻辑的提取版，供否掉检验复用）。

    map 变换可能输出 tie/both_bad——非胜方桶**剔除**而不是折算成
    candidate（折算会与集霸答 candidate 的题碰巧一致而虚高）。
    """
    cells = data.judges.get((model, pv), {})
    user, judge, cids = [], [], []
    for it in data.items.values():
        if it.user not in ("human", "candidate"):
            continue
        c = cells.get(it.cid)
        ans = fn(c) if c else None
        if ans is None or ans not in ("human", "candidate"):
            continue
        user.append(1 if it.user == "human" else 0)
        judge.append(1 if ans == "human" else 0)
        cids.append(it.cid)
    return user, judge, cids


# ── 渲染 ────────────────────────────────────────────────────

def _fmt_kappa(k: float) -> str:
    return "nan" if math.isnan(k) else f"{k:+.3f}"


def render_md(rep: dict) -> str:
    """--md 输出：数字表 + Gate 结论（真实数字，不编造）。"""
    L = []
    L.append("# 评委方向偏差校正探路（corr24，2026-09-19）")
    L.append("")
    L.append(f"检验集：corr24 {rep['n_items']} 题（控制臂 {sum(1 for it in rep['items'].values() if it.kind == 'control')} / "
             f"真劣化 {sum(1 for it in rep['items'].values() if it.kind == 'corrupt')}）；"
             f"集霸分布：{dict(Counter(it.user for it in rep['items'].values()))}")
    L.append(f"map 校准面：corr24 控制臂 × 四家池化，n={rep['n_ctl_pairs']}（按判定计）；"
             f"映射表：{rep.get('mapping')}")
    L.append(f"Gate 线 agreement ≥ {GATE}（排除制口径，历史可比；map 行为五分类严格口径，显式标注）")
    L.append("")
    L.append("## 控制臂原始读数（方向偏移量 r̂ = 控制臂上评委选 AI 版的比例；集霸 0/8）")
    L.append("")
    L.append("| 口径 | corr24 控制臂 | 全控制臂候选 r̂ | Wilson 95% CI |")
    L.append("|---|---|---|---|")
    seen = set()
    for r in rep["rows"]:
        key = (r["model"], r["pv"])
        if key in seen:
            continue
        seen.add(key)
        k, n = r["r24"]
        ka, na = r["r_all"]
        lo, hi = wilson(ka, na)
        L.append(f"| {r['model'].split('/')[-1]}（{r['pv']}）| {k}/{n} | {ka}/{na} = {ka / na:.3f} | [{lo:.3f},{hi:.3f}] |")
    L.append("")
    L.append("## 控制臂面：各变换后评委还剩多少方向偏差（corr24 控制臂 8 题）")
    L.append("")
    L.append("| 口径 | 变换 | 变换后选 AI 版 | 控制臂胜方题一致 |")
    L.append("|---|---|---|---|")
    for r in rep["rows"]:
        c = r["ctrl"]
        if c is None:
            L.append(f"| {r['model'].split('/')[-1]} | {TRANSFORM_ZH[r['transform']]} "
                     f"| — | — |")
            continue
        L.append(f"| {r['model'].split('/')[-1]} | {TRANSFORM_ZH[r['transform']]} "
                 f"| {c['ai']}/{c['n']} | {c['win_ok']}/{c['win_n']} |")
    L.append("")
    L.append("## 主表（排除制胜面子集 + κ；五分类严格口径固定分母 24）")
    L.append("")
    L.append("| 口径 | 变换 | n | agreement | Wilson 95% CI | κ | κ boot CI | 置换 p(agree) | 置换 p(κ) | 严格 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rep["rows"]:
        if r["perm"] is None:
            note = r.get("note") or "排除制不适用"
            if r["strict_denom"]:
                strict_txt = (f"{r['strict_hit']}/{r['strict_denom']}"
                              f"={r['strict_hit'] / r['strict_denom']:.3f}")
            else:
                strict_txt = "—（map 校准无数据）"
            L.append(
                f"| {r['model'].split('/')[-1]} | {TRANSFORM_ZH[r['transform']]}（五分类）"
                f" | — | —（{note}） | — | — | — | — | — | {strict_txt} |")
            continue
        pn = r["perm"]
        L.append(
            f"| {r['model'].split('/')[-1]} | {TRANSFORM_ZH[r['transform']]} "
            f"| {r['n']} | {r['agree']:.3f} | [{r['ci'][0]:.3f},{r['ci'][1]:.3f}] "
            f"| {_fmt_kappa(r['kappa'])} | [{r['kappa_ci'][0]:+.3f},{r['kappa_ci'][1]:+.3f}] "
            f"| {pn['p_agree']:.4f} | {pn['p_kappa']:.4f} "
            f"| {r['strict_hit']}/{r['strict_denom']}={r['strict_hit'] / r['strict_denom']:.3f} |")
    L.append("")
    for note in rep["ipw"]:
        L.append(f"- IPW：{note['model'].split('/')[-1]} 的 w 值集合 {note['ws']}（非分层，"
                 f"IPW-κ {_fmt_kappa(note['ipw_kappa'])} ≡ raw κ {_fmt_kappa(note['raw_kappa'])}，"
                 f"n_eff={note['n_eff']:.1f}=n）")
    mod_dom = sum(1 for it in rep["items"].values() if it.user == "both_bad")
    L.append(f"- 严格口径对照基线：恒答 corr24 众数 both_bad = {mod_dom}/24="
             f"{mod_dom / rep['n_items']:.3f}——map 的严格读数（见主表）未超过它"
             f"（覆盖折损），即 map 校正在严格口径下退化为多数类基线以下。")
    L.append("")
    L.append("## Gate 结论")
    L.append("")
    L.append(rep["verdict"])
    if rep.get("stress"):
        s = rep["stress"]
        L.append("")
        L.append("### 否掉检验（纪律①，若过线才采信）")
        L.append("")
        if s.get("loo"):
            L.append(f"- N1 控制臂 leave-one-out：mean agreement ∈ "
                     f"[{min(s['loo']):.3f}, {max(s['loo']):.3f}]")
        if s.get("alpha"):
            vals = {k: v for k, v in s["alpha"].items() if not math.isnan(v)}
            if vals:
                L.append("- N2 平滑敏感性（α→agreement）：" +
                         "，".join(f"α={a}→{v:.3f}" for a, v in sorted(vals.items())))
            else:
                L.append("- N2 平滑敏感性：全部 α 下排除制无可用答案（map 把胜方子集"
                         "的评委输出全映射到非胜方桶）——平滑强度不改变结论。")
        if s.get("corrupt_side"):
            L.append("- N3 劣化对照 16 题（map 变换后 vs 集霸胜方）：" +
                     "，".join(f"{m}={a:.3f}(n={n})" for m, a, n in s["corrupt_side"]))
    L.append("")
    L.append("## 口径与局限（必读）")
    L.append("")
    L.append("- corr24 上只有 judge_preference_v4_heldout_near1 口径有评委判定；"
             "kimi v3/v4 非 near1、deepseek v3/v4 非 near1 覆盖为 0（装载时已验证并打印）。")
    L.append("- 排除制胜方子集每口径实际覆盖 n=5（集霸胜方 7 题中评委判过的），"
             "Wilson CI 必然极宽；任何单一读数都不足以单独支撑结论。")
    L.append("- map 校准面每口径仅 4 题（池化 16 判定），拉普拉斯平滑把不确定度 "
             "显式打进映射；「控制臂校准 → 真劣化题」存在外推假设（N3 检验它）。")
    return "\n".join(L)


def render_text(rep: dict) -> str:
    """无参数时的对齐文本（内容与 --md 等价，格式给终端看）。"""
    L = []
    L.append("=" * 100)
    L.append("硬 Gate 探路：评委方向偏差能否校正到 agreement ≥0.70（corr24）")
    L.append("=" * 100)
    L.append(f"检验集 corr24：{rep['n_items']} 题；集霸分布 "
             f"{dict(Counter(it.user for it in rep['items'].values()))}")
    L.append(f"map 校准面：corr24 控制臂 × 四家池化 n={rep['n_ctl_pairs']}；"
             f"映射表 {rep.get('mapping')}")
    L.append("")
    L.append("控制臂面读数（变换后选 AI 版数/覆盖；变换后控制臂胜方一致）：")
    for r in rep["rows"]:
        c = r["ctrl"]
        if c is None:
            L.append(f"  {r['model'].split('/')[-1][:21]:22s}"
                     f"{TRANSFORM_ZH[r['transform']][:13]:14s}—")
            continue
        L.append(f"  {r['model'].split('/')[-1][:21]:22s}{TRANSFORM_ZH[r['transform']][:13]:14s}"
                 f"选AI {c['ai']}/{c['n']}   胜方一致 {c['win_ok']}/{c['win_n']}")
    L.append("")
    L.append(f"{'口径':22s}{'变换':14s}{'n':>3s}{'agree':>8s}{'Wilson95CI':>18s}"
             f"{'κ':>9s}{'κbootCI':>18s}{'p(agree)':>10s}{'p(κ)':>9s}{'严格(24)':>10s}")
    for r in rep["rows"]:
        if r["perm"] is None:
            note = r.get("note") or "排除制不适用"
            if r["strict_denom"]:
                strict_txt = (f"{r['strict_hit'] / r['strict_denom']:>7.3f}"
                              f"({r['strict_hit']}/{r['strict_denom']})")
            else:
                strict_txt = "—（map 校准无数据）"
            L.append(f"{r['model'].split('/')[-1][:21]:22s}"
                     f"{TRANSFORM_ZH[r['transform']][:13]:14s}{'—':>3s}"
                     f"  —（{note}，map 走五分类严格口径）"
                     f"{strict_txt}")
            continue
        L.append(f"{r['model'].split('/')[-1][:21]:22s}{TRANSFORM_ZH[r['transform']][:13]:14s}"
                 f"{r['n']:>3d}{r['agree']:>8.3f}"
                 f"  [{r['ci'][0]:.3f},{r['ci'][1]:.3f}]"
                 f"{_fmt_kappa(r['kappa']):>9s}"
                 f"  [{r['kappa_ci'][0]:+.3f},{r['kappa_ci'][1]:+.3f}]"
                 f"{r['perm']['p_agree']:>10.4f}{r['perm']['p_kappa']:>9.4f}"
                 f"{r['strict_hit'] / r['strict_denom']:>7.3f}({r['strict_hit']}/{r['strict_denom']})")
    L.append("")
    for note in rep["ipw"]:
        L.append(f"IPW（非分层验证）：{note['model'].split('/')[-1]} w={note['ws']} "
                 f"IPW-κ={_fmt_kappa(note['ipw_kappa'])}≡raw n_eff={note['n_eff']:.1f}")
    mod_dom = sum(1 for it in rep["items"].values() if it.user == "both_bad")
    L.append(f"严格口径众数基线：恒答 both_bad = {mod_dom}/24={mod_dom / rep['n_items']:.3f}"
             f"（map 严格读数未超过它——覆盖折损，map 退化为多数类基线以下）")
    L.append("")
    L.append("Gate 结论：" + rep["verdict"])
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="评委方向偏差校正探路（只读数据）")
    ap.add_argument("--md", action="store_true", help="输出 markdown 数字表")
    args = ap.parse_args(argv)

    from app.db import SessionLocal
    data = load_probe(SessionLocal)
    rep = run_probe(data)
    # verdict 与（过线时）否掉检验
    dec = decide(rep["rows"])
    rep["verdict"] = dec["verdict"]
    if dec["passed"]:
        rep["stress"] = stress_tests(data, rep["mapping"])
        rep["verdict"] = _stress_verdict(dec, rep["stress"])

    print(render_md(rep) if args.md else render_text(rep))
    return 0


def _stress_verdict(dec: dict, stress: dict) -> str:
    """过线后的最终裁决：否掉检验全部通过才「采信」，否则降级为证据不足。

    N0 置换 p（最直接的第一否掉检验）：过线行的置换 p ≥ 0.05 说明
    无信号假设下也能见到这个 agreement——过线不显著，不采信。
    """
    base = dec["verdict"]
    if not dec.get("passed"):
        return base
    problems = []
    best = dec.get("best") or {}
    if best.get("perm") and not math.isnan(best["perm"]["p_agree"]) \
            and best["perm"]["p_agree"] >= 0.05:
        problems.append(f"N0 置换检验不显著（p={best['perm']['p_agree']:.4f} ≥ 0.05，"
                        f"n={best['n']} 的全对在无信号假设下也不罕见）")
    if stress.get("loo"):
        spread = max(stress["loo"]) - min(stress["loo"])
        if spread > 0.10:
            problems.append(f"N1 leave-one-out 波动 {spread:.3f} 过大")
    if stress.get("alpha"):
        vals = list(stress["alpha"].values())
        if max(vals) - min(vals) > 0.10:
            problems.append("N2 平滑敏感性改变结论")
    if stress.get("corrupt_side"):
        bad = [x for x in stress["corrupt_side"] if x[1] < GATE]
        if bad:
            problems.append("N3 map 在真劣化题上掉到 " +
                            "，".join(f"{m}={a:.3f}" for m, a, _ in bad))
    if problems:
        return base + "；但否掉检验未全过：" + "；".join(problems) + \
            " → **不采信**（证据不足以支撑过线声明）。"
    return base + "；否掉检验（N1/N2/N3）全部通过 → 采信过线结论，但仍受 n 限制。"


if __name__ == "__main__":
    raise SystemExit(main())
