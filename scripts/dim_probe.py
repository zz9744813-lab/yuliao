"""逐维度探针（2026-09-17）：把「哪边更好」拆成集霸自己列的五个维度。

## 为什么

集霸描述目标是「让 AI 会写：**语义、语言、词汇搭配、节奏、情感**」。
而此前所有"整体更好"的问法全部失败（κ≈0.1、大样本 AUC≈0.50）。
如果失败的根因是"整体偏好"把多个维度混成一团，那**逐维度问**应该至少有一个维度能对上。

本探针一次调用问五个维度，逐维度与集霸判定对齐——**不用改 UI、不占用他任何时间**。

## 维度（用他的话）

`语义贴合`（有没有写偏/漏了原意）、`语言`（句子通不通、是否顺）、
`用词`（词是否准、是否俗套）、`节奏`（快慢疏密）、`情感`（情绪是否真、是否到位）

## 用法

    python scripts/dim_probe.py --dry-run
    python scripts/dim_probe.py --models moonshotai/kimi-k3 --conc 3
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
import preflight_models as pf  # noqa: E402  # 批量防呆①：开跑前校验模型名在网关池内
from app import db  # noqa: E402
from app.context_ablation import scene_context  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import Candidate, JudgeRun, Segment  # noqa: E402

PV = "dim_rating_v1"
DIMS = ["语义贴合", "语言", "用词", "节奏", "情感"]
SYSTEM = "你是中文小说编辑。只按被问的维度比较，不要给总体偏好。"
PROMPT = """前文：
{ctx}
下面是同一场景的两种写法 A 与 B。

请**逐个维度**比较，每个维度只能答 A、B 或 tie：

{dims}

只输出一行 JSON（每个维度一个键）：
{{"语义贴合":"A|B|tie", "语言":"A|B|tie", "用词":"A|B|tie", "节奏":"A|B|tie", "情感":"A|B|tie"}}

【A】
{a}

【B】
{b}"""

_lock = threading.Lock()
_cnt = {"ok": 0, "failed": 0, "skip": 0}


def load_cids() -> list[str]:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, c.id cid from review_items ri
           join candidates c on c.id = ri.subject_id
           where ri.status='done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""").fetchall()
    con.close()
    out, seen = [], set()
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        if hv.get("winner_resolved") not in ("human", "candidate"):
            continue
        if r["cid"] in seen:
            continue
        seen.add(r["cid"])
        out.append(r["cid"])
    return out


def one(cid: str, model: str) -> None:
    import random
    with db.session() as s:
        done = s.query(JudgeRun).filter_by(subject_type="candidate", subject_id=cid,
                                           judge_kind="dim_rating", model=model,
                                           prompt_version=PV, status="ok").first()
        if done:
            with _lock:
                _cnt["skip"] += 1
            return
        cand = s.get(Candidate, cid)
        human = s.get(Segment, cand.segment_id)
        htext, ctext, exp = human.text, cand.text, cand.experiment_id
        ctx_texts, _ = scene_context(s, human)
    near = ctx_texts[-1:]
    ctx = (near[0] + "\n\n") if near else ""
    rng = random.Random(f"heldout:{cid}")      # 与偏好评委同种子 → 位置分配一致，可比
    human_first = rng.random() < 0.5
    a, b = (htext, ctext) if human_first else (ctext, htext)
    body = PROMPT.format(ctx=ctx, dims="\n".join(f"- {d}" for d in DIMS), a=a, b=b)
    payload, status = None, "ok"
    try:
        r = chat(model=model, system=SYSTEM, user=body, purpose="dim_probe",
                 prompt_version=PV, temperature=0.0, max_tokens=800)
        raw = (r.text or "").strip()
        got = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        res = {}
        for d in DIMS:
            v = got.get(d)
            if v in ("A", "B", "tie"):
                if v == "tie":
                    res[d] = "tie"
                else:
                    res[d] = "human" if ((v == "A") == human_first) else "candidate"
        payload = {"per_dim": res, "human_was_a": human_first}
        if not res:
            status = "failed_parse"
    except Exception as e:  # noqa: BLE001
        status, payload = "failed", {"error": str(e)[:200]}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=exp, subject_type="candidate", subject_id=cid,
                       judge_kind="dim_rating", model=model, prompt_version=PV,
                       verdict=payload, status=status))
        s.commit()
    with _lock:
        _cnt["ok" if status == "ok" else "failed"] += 1


def report() -> None:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, jr.verdict v from judge_runs jr
           join review_items ri on ri.subject_id = jr.subject_id and ri.status='done'
           where jr.judge_kind='dim_rating' and jr.status='ok'""").fetchall()
    con.close()
    user, dim, pick_a = {}, {}, {}
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        u = hv.get("winner_resolved")
        if u not in ("human", "candidate"):
            continue
        d = json.loads(r["v"]) if isinstance(r["v"], str) else r["v"]
        hwa = d.get("human_was_a")
        for k, v in (d.get("per_dim") or {}).items():
            if v in ("human", "candidate"):
                key = k
                user.setdefault(key, []).append(1 if u == "human" else 0)
                dim.setdefault(key, []).append(1 if v == "human" else 0)
                pick_a.setdefault(key, []).append(1 if ((v == "human") == bool(hwa)) else 0)
    print()
    print("== 逐维度与集霸判定的一致度 ==")
    print(f"{'维度':10s}{'n':>5s}{'agreement':>11s}{'κ':>9s}{'挑A率':>8s}")
    for k in DIMS:
        if k not in user:
            continue
        pairs = list(zip(user[k], dim[k]))
        n = len(pairs)
        po = sum(1 for a, b in pairs if a == b) / n
        pj = sum(b for _, b in pairs) / n
        pu = sum(a for a, _ in pairs) / n
        pe = pj * pu + (1 - pj) * (1 - pu)
        kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
        pa = sum(pick_a.get(k, [0])) / max(1, len(pick_a.get(k, [1])))
        print(f"{k:10s}{n:5d}{po:11.3f}{kappa:+9.3f}{pa:8.3f}")
    print()
    print("（挑A率 ≈ 1.0 = 纯位置判断；对照整体偏好口径 κ ≈ +0.1）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="moonshotai/kimi-k3")
    ap.add_argument("--conc", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    db.init_db()
    if args.report:
        report()
        return
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    cids = load_cids()
    print(f"待探条目 {len(cids)} × {len(models)} 模型 = {len(cids)*len(models)} 次调用")
    if args.dry_run:
        print("dry-run：未发起")
        return
    # 批量防呆①（P0 死 id 事故）：池外模型的表现是 failed=整批，与"没货"同形
    pf.require_models(models, source="dim_probe")
    jobs = [(c, m) for m in models for c in cids]
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: one(*j), jobs):
            pass
    print(f"完成：ok={_cnt['ok']} failed={_cnt['failed']} skip={_cnt['skip']}")
    report()


if __name__ == "__main__":
    main()
