"""留出集对照评估（2026-09-14）。

回答一个问题：**rubric 到底有没有用？**

背景：v4 的 rubric 维度是从 99 条旧判定里反推出来的，又在同一批数据上"验证"过——
那是循环验证，任何提升都是上界。唯一干净的检验是**在没见过的题上复测**。

本脚本：
1. 取 `batch_r25` 的**真留出子集**（排除建批前就已判的题——那些进过推导集）；
2. 在同一批题上分别跑 v3（无 rubric）与 v4（有 rubric），两版都带上文；
3. 报 agreement / κ / 响应偏差，并做配对检验（McNemar + bootstrap CI）。

⚠ 为什么必须排除建批前已判的题：rubric 从它们身上推导而来，
再拿它们"验证"等于自己考自己。脚本会按 `reviewed_at` 与建批时间自动切分。

用法：
    python scripts/heldout_eval.py --dry-run
    python scripts/heldout_eval.py
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config, db
from app.context_ablation import scene_context
from app.judges import PROMPT_VARIANTS, judge_preference
from app.models import Candidate, JudgeRun, ReviewItem, Segment
import preflight_models as pf

DB = Path(__file__).resolve().parent.parent / "data" / "language_genome.db"
EXP = "EXP-0911-B82D"
BATCH = "r25"
# 建批时刻（r25 创建于 2026-09-14 13:15Z 之后）。此前的 done 题进过推导集。
BATCH_EPOCH = "2026-09-14T13:15:00Z"
JUDGES = ("moonshotai/kimi-k3", config.DEFAULT_LLM_MODEL)

_lock = threading.Lock()
# first_error：本轮首条失败**原文**。judge_runs 只存 status、不存错误串，
# 而"failed=全部"和"池子没货"在计数上长得一模一样（2026-09-20 P0 事故）→ 就地留一份。
_counter = {"ok": 0, "failed": 0, "skip": 0, "first_error": ""}


def load_items(heldout_only: bool, batch: str = BATCH,
               epoch: str | None = BATCH_EPOCH,
               batches: tuple[str, ...] | None = None,
               exp: str | None = None) -> list[dict]:
    """取（某批次的 / 多个批次的）有明确 human/candidate 判定的题。

    heldout_only：排除"建批前就已判过"的题（那些进过 rubric 推导集，再用来验证是自我考试）。
    仅 r25 需要该过滤；s30 的题全是新判的，不传 epoch 即可。

    batches：指定多个批次则合并取并集。**用途**：把"集霸标注过噪点的批次"
    与"未标注的批次"分开，用来检验缺陷口径的词表是否存在循环性。

    exp：实验号。None = 自动：批次只落在一个实验就用它；落在多个（跨语料混合批）
    则**不加实验过滤**（池化读数），逐语料读数用 `--exp` 分别跑。
    """
    if exp is None:
        exps = experiments_of_batch(batch, DB)
        exp = exps[0] if len(exps) == 1 else None      # None → 不过滤
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    tags = batches or (batch,)
    seen: set[str] = set()
    out: list[dict] = []
    for tag in tags:
        sql = ("""select ri.id rid, ri.subject_id cid, ri.reviewed_at, ri.human_verdict
                  from review_items ri
                  join candidates c on c.id = ri.subject_id
                  where ri.reasons like ? and ri.status = 'done'
                    and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""")
        args: list = [f"%batch_{tag}%"]
        if exp:
            sql += " and ri.experiment_id = ?"
            args.append(exp)
        rows = con.execute(sql, args).fetchall()
        for r in rows:
            if r["cid"] in seen:
                continue
            if heldout_only and epoch and (r["reviewed_at"] or "") < epoch:
                continue
            hv = json.loads(r["human_verdict"]) if r["human_verdict"] else {}
            w = hv.get("winner_resolved")
            if w not in ("human", "candidate"):
                continue
            seen.add(r["cid"])
            out.append({"rid": r["rid"], "cid": r["cid"], "user": w})
    con.close()
    return out


def experiments_of_batch(batch: str, db_path: Path | str | None = None) -> list[str]:
    """批次标签落在哪些实验上（0/1/多个）。"""
    con = sqlite3.connect(db_path or DB)
    rows = con.execute(
        "select distinct experiment_id from review_items where reasons like ?",
        (f"%batch_{batch}%",)).fetchall()
    con.close()
    return sorted(r[0] for r in rows if r[0])


def resolve_exp(batch: str, db_path: Path | str | None = None) -> str:
    """由批次标签反查**唯一**实验号。跨语料混合批（mix30 等）会显式报错，
    调用方若想支持混合批，应改用 `experiments_of_batch`（见 run_one 按候选取实验）。

    写死 B82D 会让跨语料批次**静默加载 0 条**（本项目最忌讳的失败形态），
    所以这里宁可报错也不猜。
    """
    exps = experiments_of_batch(batch, db_path)
    if not exps:
        raise SystemExit(f"批次 {batch} 在库里不存在（没有任何 review_item 带此标签）")
    if len(exps) > 1:
        raise SystemExit(f"批次 {batch} 跨了多个实验 {exps} —— 这是跨语料混合批；"
                         f"分析请用 --exp 指定单个语料，或让工具走混合模式。")
    return exps[0]


def _done(s, cid: str, model: str, pv: str, exp: str | None = None) -> bool:
    # model__in（不是 model=）：历史行存的是已下线的旧 id，按新名精确查会把
    # "已判过"看成"没判过"→ 重复烧额度并多写一行，κ 就在重复样本上算。
    return bool(s.query(JudgeRun).filter_by(
        experiment_id=exp or EXP, subject_type="candidate", subject_id=cid,
        judge_kind="preference", model__in=config.model_any(model), prompt_version=pv,
        status="ok").first())

REVERSE_SUFFIX = "_rev"


def _as_dict(v) -> dict | None:
    """把 verdict 统一成 dict。

    ⚠ 必须两种都兼容：走 SQLAlchemy ORM 时 JSON 列**已经解析成 dict**，
    再 `json.loads` 会抛 TypeError；走裸 sqlite3 时拿到的是 TEXT，必须 `json.loads`。
    只支持一种会导致另一端**静默返回 None**（本项目已因这个坑白跑一次全量）。
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


def _human_was_a(cid: str, model: str, pv: str, exp: str | None = None) -> bool | None:
    """读某条已有的判定记录的 human_was_a（用于构造它的"反序"重跑）。"""
    with db.session() as s:
        # model__in：正向判定可能写于改名之前，精确查会把"有反序可对"看成"没有"，
        # 于是反序臂静默缺样本（读数少一截却不报错）。
        r = (s.query(JudgeRun)
             .filter_by(experiment_id=exp or EXP, subject_type="candidate", subject_id=cid,
                        judge_kind="preference", prompt_version=pv,
                        status="ok")
             .filter(JudgeRun.model.in_(config.model_any(model)))
             .order_by(JudgeRun.created_at.desc()).first())
        d = _as_dict(r.verdict) if r else None
        return None if not d else d.get("human_was_a")


def run_one(cid: str, ctx: str, model: str, variant: str,
            force_human_a: bool | None = None, pv_suffix: str = "",
            exp: str | None = None) -> None:
    _pv = PROMPT_VARIANTS[variant][1] + pv_suffix
    with db.session() as s:
        cand = s.get(Candidate, cid)
        if cand is None:
            with _lock:
                _counter["failed"] += 1
            return
        # 实验号取**候选自己的**：跨语料混合批里同批题分属不同实验，
        # 按批次级参数写会让记录挂到错误的实验上（_done 也会查错地方）。
        exp = exp or cand.experiment_id or EXP
        if _done(s, cid, model, _pv, exp):
            with _lock:
                _counter["skip"] += 1
            return
        human = s.get(Segment, cand.segment_id)
        htext, ctext = human.text, cand.text
    rng = random.Random(f"heldout:{cid}{pv_suffix}")
    out = judge_preference(human_text=htext, candidate_text=ctext, model=model,
                           rng=rng, context=ctx, variant=variant,
                           force_human_a=force_human_a)
    payload = None
    if out.get("status") == "ok":
        payload = {**out["verdict"], "human_was_a": out.get("human_was_a"),
                   "winner_resolved": out.get("winner_resolved")}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=exp, subject_type="candidate", subject_id=cid,
                       judge_kind="preference", model=model, prompt_version=_pv,
                       verdict=payload, confidence=out.get("confidence"),
                       abstain=bool(out.get("abstain", False)), status=out["status"]))
        s.commit()
    with _lock:
        if out["status"] == "ok":
            _counter["ok"] += 1
        else:
            _counter["failed"] += 1
            if not _counter["first_error"]:
                _counter["first_error"] = pf.redact(
                    out.get("error") or out.get("raw") or f'status={out["status"]}')


def _kappa(pairs: list[tuple[int, int]]) -> float:
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


def _weighted_kappa(pairs: list[tuple[int, int]], weights: list[float]) -> tuple[float, float]:
    """逆概率加权 κ，用于还原**分层抽样**下的总体一致性。

    分层抽样（对"候选胜"过采样）会让 raw κ 有偏；每条样本代表 1/w 个总体单元，
    按 w=1/入样概率 加权即可无偏估计总体 κ。

    返回 (kappa, 有效样本量 n_eff)。n_eff = (Σw)²/Σw² —— 权重悬殊时它会远小于名义 n，
    这是必报的诚实指标：**加权能去偏，但去不掉方差**。
    """
    if not pairs:
        return float("nan"), 0.0
    W = float(sum(weights))
    if W <= 0:
        return float("nan"), 0.0
    n_eff = W * W / sum(x * x for x in weights) if weights else 0.0

    def wsum(pred) -> float:
        return sum(w for (u, j), w in zip(pairs, weights) if pred(u, j))

    po = (wsum(lambda u, j: u and j) + wsum(lambda u, j: not u and not j)) / W
    pj = (wsum(lambda u, j: j)) / W          # 评委说 human 的加权比例
    pu = (wsum(lambda u, j: u)) / W          # 用户说 human 的加权比例
    pe = pj * pu + (1 - pj) * (1 - pu)
    return ((po - pe) / (1 - pe) if pe < 1 else float("nan")), n_eff


def _weights_from_reasons(rid: str) -> float:
    """从 review_item.reasons 读入样概率 w，返回逆概率权重。"""
    with db.session() as s:
        r = s.get(ReviewItem, rid)
        for t in (r.reasons or []) if r else []:
            if t.startswith("w:"):
                try:
                    p = float(t[2:])
                    if p > 0:
                        return 1.0 / p
                except ValueError:
                    pass
    return 1.0


def _mcnemar(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n * 2)


def _position_diagnostic(got: dict, user: dict, cids: list[str],
                         model: str, variant: str,
                         db_path: Path | str | None = None) -> dict | None:
    """位置偏差诊断（2026-09-16 加，此前从未测过）。

    为什么必报：A/B 位置是随机化的，所以位置偏差**不会偏置 κ**，但会**吃掉统计功效**
    —— 评委大部分方差来自"偏好 A 还是 B"而非内容。实测选 A 率高达 0.74–0.80。
    不报这个数，就会把"κ 上不去"误判成"评委没能力"，其实可能是位置噪声稀释。

    db_path：显式传入以便测试用临时库，**不要靠 monkeypatch 模块全局 `DB`**——
    `import heldout_eval` 与 `import scripts.heldout_eval` 是两个独立模块实例，
    patch 错对象时函数会静默返回 None，且测试可能误写生产库（本项目已踩过）。

    返回 {'n','pick_a','acc','baseline_acc','human_prec','cand_prec'}
    - acc：评委选的边是否等于"更好"的一边
    - baseline_acc：**"永远选 A"的准确率**。评委准确率若低于它，说明评委是负信息的。
    """
    n = acc = pick_a = 0
    base_hit = base_n = 0
    jh = jhc = jc = jcc = 0
    con = sqlite3.connect(db_path or DB)
    con.row_factory = sqlite3.Row
    for cid in cids:
        r = con.execute(
            """select verdict from judge_runs where subject_id=? and judge_kind='preference'
               and model=? and prompt_version=? order by created_at desc limit 1""",
            (cid, model, PROMPT_VARIANTS[variant][1])).fetchone()
        if not r or not r["verdict"]:
            continue
        d = _as_dict(r["verdict"])
        if not d:
            continue
        h, pick = d.get("human_was_a"), d.get("winner")
        if h is None or pick not in ("A", "B"):
            continue
        uw = "human" if user[cid] else "candidate"
        # 与位置无关的真值方向：「A 更好」由 human_was_a 与用户判定共同确定
        a_better = (h and uw == "human") or ((not h) and uw == "candidate")
        base_n += 1
        base_hit += bool(a_better)
        n += 1
        pick_a += pick == "A"
        acc += (pick == "A") == a_better
        jw = "human" if ((pick == "A") == h) else "candidate"
        if jw == "human":
            jh += 1
            jhc += uw == "human"
        else:
            jc += 1
            jcc += uw == "candidate"
    con.close()
    if not n or not base_n:
        return None
    return {"n": n, "pick_a": pick_a / n, "acc": acc / n, "baseline_acc": base_hit / base_n,
            "human_prec": jhc / jh if jh else float("nan"),
            "cand_prec": jcc / jc if jc else float("nan")}


def _two_order_verdict(cid: str, model: str, base_pv: str, con) -> str | None:
    """合并正序与反序两次判定，返回内容方向（human/candidate）或 None。

    合并规则（关键）：
    - 两序都指向同一内容方向 → 采纳（这是**顺序无关**的判断，可信度高）
    - 两序指向相反方向 → **位置偏差主导**，返回 None（弃权，不计入 κ 分母）
    - 任一序缺失/弃权 → None

    为什么"相反就弃权"是正确做法：若评委在正序说 human、反序还说 human，
    那说明它换边后仍认人类段更好——这才是内容信号。若换边就换答案，
    说明驱动它的是"哪边摆在 A"而非文本，这种判定不含内容信息，硬算进
    agreement 只会稀释。**弃权比瞎猜更诚实，且能提高剩余样本的可信度。**
    """
    a = _read_verdict(cid, model, base_pv, con)
    b = _read_verdict(cid, model, base_pv + REVERSE_SUFFIX, con)
    if a is None or b is None:
        return None
    return a if a == b else None


def _read_verdict(cid: str, model: str, pv: str, con) -> str | None:
    # model_any：历史判定写于改名前（model 列存旧 id），只查新名会静默清空
    any_ids = config.model_any(model)
    r = con.execute(
        f"""select verdict, abstain from judge_runs where subject_id=? and judge_kind='preference'
            and model in ({",".join("?" * len(any_ids))})
            and prompt_version=? order by created_at desc limit 1""",
        (cid, *any_ids, pv)).fetchone()
    if not r or not r["verdict"] or r["abstain"]:
        return None
    d = _as_dict(r["verdict"])
    if not d:
        return None
    return d.get("winner_resolved") if d.get("winner_resolved") in ("human", "candidate") else None


def report(items: list[dict], batch: str = BATCH, reverse: bool = False) -> None:
    cids = [x["cid"] for x in items]
    user = {x["cid"]: (1 if x["user"] == "human" else 0) for x in items}
    rids = {x["cid"]: x["rid"] for x in items}
    weights = {c: _weights_from_reasons(rids[c]) for c in cids}
    n_weighted = sum(1 for c in cids if weights[c] != 1.0)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    got: dict[tuple[str, str, str], str | None] = {}
    for m in JUDGES:
        for v in ("v3", "v4"):
            pv = PROMPT_VARIANTS[v][1]
            for cid in cids:
                got[(m, v, cid)] = _read_verdict(cid, m, pv, con)
    con.close()

    print(f"\n{'='*74}")
    print(f"留出集 {batch}：n={len(items)}")
    hr = sum(user.values()) / len(user)
    print(f"用户判 human 比例 = {hr:.3f}")
    # !! 必报基线：基础率极端偏斜时，恒定答多数类会在 agreement 上碾压评委。
    # 不报这个基线，就会把"评委很差"误读成"评委还行"（反之亦然）。
    const = [(user[c], 1) for c in cids]      # judge 恒答 human(=1)
    c_agree = sum(1 for u, j in const if u == j) / len(const)
    print(f"【必报基线】恒定答 human：agreement={c_agree:.3f}  κ={_kappa(const):+.3f}"
          f"   ← agreement 上限的参照物")
    if n_weighted:
        print(f"本批含分层抽样条目 {n_weighted}/{len(cids)} → 同时报**逆概率加权 κ**（还原总体）")
    print(f"{'='*74}")
    rng = np.random.default_rng(20260914)
    for m in JUDGES:
        print(f"\n--- {m.split('/')[-1]} ---")
        print(f"{'口径':8s}{'n':>4s}{'agreement':>11s}{'κ':>9s}{'挑candidate':>13s}"
              f"{'加权κ':>9s}{'n_eff':>8s}")
        table = {}
        for v in ("v3", "v4"):
            pairs, ws = [], []
            for cid in cids:
                jw = got[(m, v, cid)]
                if jw is None:
                    continue
                pairs.append((user[cid], 1 if jw == "human" else 0))
                ws.append(weights[cid])
            table[v] = {cid: got[(m, v, cid)] for cid in cids}
            n = len(pairs)
            if not n:
                print(f"{v:8s}   无样本")
                continue
            agree = sum(1 for u, j in pairs if u == j) / n
            pick = sum(1 for _, j in pairs if j == 0) / n
            wk, n_eff = _weighted_kappa(pairs, ws)
            print(f"{v:8s}{n:4d}{agree:11.3f}{_kappa(pairs):+9.3f}{pick:13.3f}"
                  f"{wk:+9.3f}{n_eff:8.1f}")

        # 位置偏差诊断：必报。否则会把"κ 上不去"误判成能力问题，
        # 而真因可能是位置噪声稀释了功效（实测选 A 率 0.74–0.80）。
        for v in ("v3", "v4"):
            pd = _position_diagnostic(got, user, cids, m, v)
            if not pd:
                continue
            flag = "  ⚠ 低于位置基线（负信息）" if pd["acc"] < pd["baseline_acc"] else ""
            print(f"  [位置诊断 {v}] n={pd['n']}  选A率={pd['pick_a']:.3f}  "
                  f"准确率={pd['acc']:.3f}  vs「永远选A」基线 {pd['baseline_acc']:.3f}"
                  f" ({pd['acc']-pd['baseline_acc']:+.3f}){flag}")
            print(f"              判human时精度={pd['human_prec']:.3f}  "
                  f"判candidate时精度={pd['cand_prec']:.3f}")

        # 配对检验：两版都给出结论的题
        common = [c for c in cids
                  if table["v3"][c] and table["v4"][c]]
        if len(common) >= 5:
            b = c_ = 0
            for cid in common:
                ok3 = (table["v3"][cid] == items_user_side(user, cid))
                ok4 = (table["v4"][cid] == items_user_side(user, cid))
                if ok3 and not ok4:
                    b += 1
                elif ok4 and not ok3:
                    c_ += 1
            p3 = [(user[x], 1 if table["v3"][x] == "human" else 0) for x in common]
            p4 = [(user[x], 1 if table["v4"][x] == "human" else 0) for x in common]
            d = []
            for _ in range(2000):
                idx = rng.integers(0, len(common), len(common))
                s3 = [p3[i] for i in idx]
                s4 = [p4[i] for i in idx]
                v = _kappa(s4) - _kappa(s3)
                if np.isfinite(v):
                    d.append(v)
            lo, hi = (np.percentile(d, [2.5, 97.5]) if d else (float("nan"),) * 2)
            print(f"  配对（n={len(common)}）：McNemar v3对v4错={b}, v3错v4对={c_}, "
                  f"p={_mcnemar(b, c_):.3f}")
            print(f"  bootstrap κ 差(v4−v3) 95%CI = [{lo:+.3f}, {hi:+.3f}]"
                  f"  → {'可信' if lo > 0 else '含 0，不显著'}")

    if reverse:
        _report_two_order(items, user, cids, weights)


def _report_two_order(items: list[dict], user: dict, cids: list[str],
                      weights: dict[str, float]) -> None:
    """正反两序合并后的 ρ 对比：这是位置偏差校正的交付物。

    报三列：正序（单序，有位置噪声）/ 反序（单序）/ **两序合并**（位置偏差相消）。
    若合并后 κ 明显上升，说明位置偏差确实是功效的主要杀手。
    """
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print(f"\n{'='*74}")
    print("位置偏差校正（正反两序）")
    print(f"{'='*74}")
    print(f"{'评委':10s}{'口径':5s}{'正序κ':>9s}{'反序κ':>9s}{'两序合并κ':>11s}"
          f"{'合并n':>7s}{'翻转率':>8s}")

    def sub(side: str) -> list[tuple[int, int]]:
        out = []
        for cid in cids:
            j = _read_verdict(cid, m, PROMPT_VARIANTS[v][1] + side, con)
            if j is not None:
                out.append((user[cid], 1 if j == "human" else 0))
        return out

    for m in JUDGES:
        for v in ("v3", "v4"):
            fwd, rev = sub(""), sub(REVERSE_SUFFIX)
            both = []
            for cid in cids:
                jw = _two_order_verdict(cid, m, PROMPT_VARIANTS[v][1], con)
                if jw is not None:
                    both.append((user[cid], 1 if jw == "human" else 0))
            covered = len(fwd) + len(rev)
            # 翻转率 = 两序有效但内容方向相反的题占比。**这是比 κ 更根本的诊断**：
            #   0.00 = 纯内容判断（换边不改答案）
            #   1.00 = 纯位置判断（永远选摆位 A）
            #   2p(1−p) ≈ 与位置无关的随机（p=0.75 时约 0.375）
            # 实测 0.25–0.57 → 说明判定相当大程度由摆位驱动，即**内容信号本身弱**。
            flip_rate = 1 - (2 * len(both) / covered) if covered else float("nan")
            print(f"{m.split('/')[-1][:9]:10s}{v:5s}{_kappa(fwd):+9.3f}{_kappa(rev):+9.3f}"
                  f"{_kappa(both):+11.3f}{len(both):7d}{flip_rate:8.3f}")
    print("\n  两序合并规则：两序指向同一内容方向才采纳；**方向相反则弃权**"
          "（说明驱动它的是摆位而非文本，不含内容信息）。")
    print("  翻转率的三种参照：纯内容=0.00 ／ 纯位置=1.00 ／ 与位置无关的随机≈2p(1−p)。")
    print("  ⚠ 结论（s30+r25 n=46 实测）：两序合并**未见可靠提升**（kimi v4 略降、"
          "deepseek v4 升、v3 持平），且 n 腰斩使 CI 更宽。")
    print("  → **不要把两序当默认**：成本翻倍、收益不确定。它的价值在于诊断。")
    con.close()


def _batch_has_strata(btags: tuple[str, ...], exp: str | None = None) -> bool:
    """该批是否含「收割+锚点」分层 —— 决定要不要提示跑框自检。exp=None 不过滤实验。"""
    con = sqlite3.connect(DB)
    try:
        for t in btags:
            sql = ("select count(*) from review_items "
                   "where status='done' and reasons like ? and reasons like '%stratum:%'")
            args: list = [f"%batch_{t}%"]
            if exp:
                sql += " and experiment_id=?"
                args.append(exp)
            if con.execute(sql, args).fetchone()[0]:
                return True
        return False
    finally:
        con.close()


def items_user_side(user: dict, cid: str) -> str:    return "human" if user[cid] else "candidate"


def _run_reverse(items: list[dict], conc: int, exp: str | None = None) -> None:
    """位置偏差校正第二趟：把人类段换到另一侧重跑。

    为什么值得多花一倍调用：实测评委选 A 率 0.74–0.80（见 `_position_diagnostic`）。
    A/B 位置虽是随机的（不偏置 κ），但位置方差**吃掉功效**，是 CI 一直很宽的原因之一。
    正反两序合并后位置偏差相消，剩下的是内容判断。

    存储：结果写进 `…_rev` 后缀的 prompt_version，与正序结果并存、可追溯。
    """
    print("· 位置偏差校正：反序重跑（人类段换到另一侧）")
    jobs = []
    for m in JUDGES:
        for v in ("v3", "v4"):
            base = PROMPT_VARIANTS[v][1]
            for x in items:
                hwa = _human_was_a(x["cid"], m, base, exp)
                if hwa is None:
                    continue          # 正序没有有效记录，不构造反序
                jobs.append((x["cid"], None, m, v, not hwa))
    # 上下文按 cid 预取
    ctx_by_cid: dict[str, str] = {}
    with db.session() as s:
        for x in items:
            cand = s.get(Candidate, x["cid"])
            human = s.get(Segment, cand.segment_id)
            texts, _ = scene_context(s, human)
            ctx_by_cid[x["cid"]] = (chr(10) * 2).join(texts)
    print(f"  待跑 {len(jobs)} 次调用")
    with ThreadPoolExecutor(max_workers=conc) as pool:
        for _ in pool.map(
                lambda j: run_one(j[0], ctx_by_cid[j[0]], j[2], j[3],
                                  force_human_a=j[4], pv_suffix=REVERSE_SUFFIX,
                                  exp=exp), jobs):
            pass
    print(f"  完成：ok={_counter['ok']} failed={_counter['failed']} skip={_counter['skip']}")
    if _counter["failed"]:
        print(f"        首条错误原文：{_counter['first_error'] or '（未捕获到异常文本）'}")
    report(items, batch="（正序+反序）", reverse=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=BATCH, help="批次名（r25 / s30 …）")
    ap.add_argument("--all-r25", action="store_true",
                    help="含建批前判过的题（那些进过推导集，非留出）")
    ap.add_argument("--no-epoch", action="store_true",
                    help="不做建批时刻过滤（s30 这类全新批次用）")
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--variants", default="v3,v4",
                    help="要跑的口径，逗号分隔（v3,v4,defect）。默认 v3,v4 保持行为不变。")
    ap.add_argument("--reverse", action="store_true",
                    help="位置偏差校正：对已有判定**反序重跑**一遍（人类段换到另一侧）")
    ap.add_argument("--exp", default=None,
                    help="实验号；默认由批次标签反查（跨语料批次属于别的实验，写死 B82D 会加载到 0 条）")
    args = ap.parse_args()
    args.variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    for _v in args.variants:
        if _v not in PROMPT_VARIANTS:
            raise SystemExit(f"未知口径 {_v}；可选 {list(PROMPT_VARIANTS)}")

    # 批量防呆①（P0 死 id 事故）：正序与 --reverse 两条路都会发调用，闸门放最前面。
    if not args.dry_run:
        pf.require_models(list(JUDGES), source="heldout_eval")
        _counter["first_error"] = ""      # 本轮失败原因只属于本轮

    db.init_db()
    # --batch 支持逗号分隔多批次（用途：在"未标注过"的优先队列上跑缺陷口径，
    # 以排除缺陷类型词表的循环性）。
    btags = tuple(t.strip() for t in args.batch.split(",") if t.strip())
    if args.exp:
        exp = args.exp
    else:
        exps = experiments_of_batch(btags[0])
        if not exps:
            raise SystemExit(f"批次 {btags[0]} 在库里不存在（没有任何 review_item 带此标签）")
        exp = exps[0] if len(exps) == 1 else None
        if exp is None:
            print(f"批次 {btags[0]} 跨 {len(exps)} 个实验 {exps}（跨语料混合批）")
            print("  → 池化读数（不加实验过滤）；逐语料用 --exp 分别跑")
        elif exp != EXP:
            print(f"实验号 = {exp}（由批次反查；非默认的 {EXP}）")
    epoch = None if (args.no_epoch or "r25" not in btags) else BATCH_EPOCH
    items = load_items(heldout_only=not args.all_r25, batch=btags[0], epoch=epoch,
                       batches=btags if len(btags) > 1 else None, exp=exp)
    scope = "仅真留出" if (epoch and not args.all_r25) else "全部已判"
    print(f"批次 {','.join(btags)}：{len(items)} 条（{scope}）")
    if not items and not args.reverse:
        # 空数据不继续：不给护栏的话会在算"上文中位数"时 IndexError，报错毫无指向性。
        raise SystemExit(f"批次 {','.join(btags)} 没有已判题目 —— 没有可评测的数据。"
                         f"（集霸判完后重跑；评委分若未预跑，也会在这一步之后才发起调用。）")

    if args.reverse:
        _run_reverse(items, args.conc, exp)
        return

    # 预先取好上下文（同一 cid 的 A/B 两版共用同一份上文，保证对照公平）
    ctx_by_cid: dict[str, str] = {}
    with db.session() as s:
        for x in items:
            cand = s.get(Candidate, x["cid"])
            human = s.get(Segment, cand.segment_id)
            texts, _ = scene_context(s, human)
            ctx_by_cid[x["cid"]] = (chr(10) * 2).join(texts)
    med = sorted(len(v) for v in ctx_by_cid.values())[len(ctx_by_cid) // 2]
    print(f"上文中位 {med} 字")

    n_calls = len(items) * len(JUDGES) * len(args.variants)
    print(f"预计 {n_calls} 次调用（{len(items)} 题 × {len(JUDGES)} 评委 × "
          f"{len(args.variants)} 口径：{','.join(args.variants)}）")
    if args.dry_run:
        print("dry-run：未发起")
        return

    jobs = [(x["cid"], ctx_by_cid[x["cid"]], m, v)
            for m in JUDGES for v in args.variants for x in items]
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: run_one(*j, exp=exp), jobs):
            pass
    print(f"完成：ok={_counter['ok']} failed={_counter['failed']} skip={_counter['skip']}")
    if _counter["failed"]:
        print(f"       首条错误原文：{_counter['first_error'] or '（未捕获到异常文本）'}")
    if len(experiments_of_batch(btags[0])) > 1:
        print()
        print("⚠ 本批跨多个实验（跨语料）：各语料的入样概率不同，")
        print("  **逆概率加权 κ 无意义**——加权还原出的不是任何真实总体。请读原始 κ；")
        print("  要逐语料读数用 --exp 分别跑。")
    report(items, batch=args.batch)

    if _batch_has_strata(btags, exp):
        print()
        print("· 本批含「收割+锚点」分层 → 判完必做框自检（决定挑题策略能否继续）：")
        print(f"    python scripts/anchor_check.py --batch {args.batch}")
        print("    主检验 = 收割段是否兑现建批承诺（锚点那一侧功效极低，见 §3）。")

    print()
    print("· 段级聚类：人类段落数才是有效独立单位（详见交接文档 §9 第 8 条），"
          "按 n 算的 CI 偏乐观。")
    con = sqlite3.connect(DB)
    try:
        sql = ("""select count(distinct c.segment_id), count(*)
                  from review_items ri join candidates c on c.id = ri.subject_id
                  where ri.reasons like ? and ri.status='done'""")
        args: list = [f"%batch_{btags[0]}%"]
        if exp:
            sql += " and ri.experiment_id=?"
            args.append(exp)
        nseg, nitem = con.execute(sql, args).fetchone()
    finally:
        con.close()
    print(f"  本批已判 {nitem} 条，只覆盖 {nseg} 个不同人类段落")


if __name__ == "__main__":
    main()
