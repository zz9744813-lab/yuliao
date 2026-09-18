"""「解释过度」探针（2026-09-17）。

## 为什么做这个

集霸的 49 处划词批注里，**「解释过度」17 处全部出现在候选侧，且标了它的题候选胜率 0/17**
—— 这是他最稳定的一条批评，也是"让 AI 会写"最该教的那件事（克制、留白、不把话说完）。

但**简单标记不管用**：我先试了"明喻/叙述者判断/微动作"等词表密度，
逐题中位数差 = 0.00、预测他判断的 AUC = 0.427（比瞎猜还差）、逐批从 0.225 跳到 0.755。
（教训同项目记载的"缺陷词表过拟合"：词表在标注过的那批上好看，换批就崩。）

## 本探针要回答的两个问题

1. **可判别性**：LLM 能不能稳定判断"哪一段把话说得更尽"？
2. **相关性**：它指出的"说尽的一侧"，是不是就是集霸判输的那一侧？

第 2 问是关键：如果成立，我们就第一次拿到了**一个有方向的、可复现的维度信号**——
不是"哪边更好"那种糊问题，而是"这段有没有把该留白的东西说白"。

## 口径

- 与盲评一致的匿名 A/B（位置随机、种子由 cid 决定，可复现）；带上文（与用户所见同源）。
- 任务：**只问解释/留白，不问好坏**。判词 A/B/tie。
- 落库 `judge_kind='overexplain'`、`prompt_version='overexplain_v1'`，可审计、可重跑（幂等）。

用法：
    python scripts/overexplain_probe.py --dry-run
    python scripts/overexplain_probe.py            # 全部已判二选一条目
    python scripts/overexplain_probe.py --batch nq50
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heldout_eval as he  # noqa: E402  （复用它的 DB/上下文/JUDGES/幂等风格）
from app import db  # noqa: E402
from app.context_ablation import scene_context  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import Candidate, JudgeRun, Segment  # noqa: E402

PV = "overexplain_v1"
PV_FS = "overexplain_fs_v1"      # few-shot（用作者自己的标注当例子）
BATCH = "overexplain"
SYSTEM = "你是中文小说编辑，只回答被问的那一件事，不要评价好坏。"
PROMPT = """下面是一个场景的前文，以及紧接其后的两段文字（A 与 B），它们写的是**同一个场景**。

{ctx}【A】
{a}

【B】
{b}

只回答一件事：**A 和 B，哪一段更倾向于把本该留给读者体会的东西直接说出来**
（例如用"像/仿佛/显得"把暗示点明、替读者下结论"到底还是…"、把微动作逐帧列出、
直接陈述人物内心状态）？

不要判断哪段写得好，只看**谁说得更尽、留白更少**。

只输出一行 JSON：{{"more_explained": "A"|"B"|"tie"}}"""

# few-shot：用**作者本人标过**的「解释过度」原文当例子。
# 为什么必须这样：第一版我用自己转述的例子（比喻点明/微动作逐帧），结果测成了
# "辞藻浓不浓"——琼明人类原文本身就是辞藻浓的艳情小说，于是探针倒向人类侧
# （冒烟 n=21 对齐率仅 0.24，即它指的往往是**赢**的那侧）。用作者自己的标注
# 才能真正对齐他的概念。
PROMPT_FS = """作者本人审稿时会标出「解释过度」的地方。以下是他标过的几处原文片段：

{examples}

下面是一个场景的前文，以及紧接其后的两段文字（A 与 B），写的是**同一个场景**。

{ctx}【A】
{a}

【B】
{b}

只回答一件事：**A 和 B 哪一段更像上面那些被作者标出来的写法**（把话说完、不给读者留余地）？

不要判断哪段写得好，只看**哪一段更像"解释过度"的那种写法**。

只输出一行 JSON：{{"more_explained": "A"|"B"|"tie"}}"""


def load_examples(exclude_batch: str | None = None, k: int = 5) -> list[str]:
    """取作者标注过的「解释过度」原文片段；排除 test 批次，保证跨批（不循环）。"""
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, ri.reasons rs from review_items ri
           where ri.status='done'""").fetchall()
    con.close()
    out = []
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        tags = json.loads(r["rs"] or "[]")
        if exclude_batch and f"batch_{exclude_batch}" in tags:
            continue
        for a in (hv.get("annotations") or []):
            if a.get("kind") != "解释过度" or a.get("target") != "candidate":
                continue
            t = (a.get("text") or "").strip()
            if 8 <= len(t) <= 60:
                out.append(t)
    return out[:k]

_lock = threading.Lock()
_cnt = {"ok": 0, "failed": 0, "skip": 0}


def load_items(batch: str | None = None) -> list[dict]:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.id rid, ri.human_verdict hv, ri.reasons rs, c.id cid
           from review_items ri join candidates c on c.id = ri.subject_id
           where ri.status='done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""").fetchall()
    con.close()
    out, seen = [], set()
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        if hv.get("winner_resolved") not in ("human", "candidate"):
            continue
        if batch:
            tags = json.loads(r["rs"] or "[]")
            if f"batch_{batch}" not in tags:
                continue
        if r["cid"] in seen:
            continue
        seen.add(r["cid"])
        out.append({"rid": r["rid"], "cid": r["cid"], "batch": batch,
                    "user_human": hv["winner_resolved"] == "human"})
    return out


def one(item: dict, model: str) -> None:
    cid = item["cid"]
    ex = load_examples(exclude_batch=item.get("batch"))
    pv = PV_FS if ex else PV           # 两个口径分开存，便于对比
    with db.session() as s:
        done = (s.query(JudgeRun).filter_by(subject_type="candidate", subject_id=cid,
                                            judge_kind="overexplain", model=model,
                                            prompt_version=pv, status="ok").first())
        if done:
            with _lock:
                _cnt["skip"] += 1
            return
        cand = s.get(Candidate, cid)
        human = s.get(Segment, cand.segment_id)
        htext, ctext = human.text, cand.text
        ctx_texts, _ = scene_context(s, human)
    near = ctx_texts[-1:] or []
    ctx = ("前文：\n" + near[0] + "\n\n") if near else ""

    rng = random.Random(f"xprobe:{cid}")
    human_first = rng.random() < 0.5
    a, b = (htext, ctext) if human_first else (ctext, htext)
    payload, status, verdict = None, "ok", None
    if ex:
        body = PROMPT_FS.format(examples=chr(10).join(f"· {x}" for x in ex),
                                ctx=ctx, a=a, b=b)
    else:
        body = PROMPT.format(ctx=ctx, a=a, b=b)
    try:
        r = chat(model=model, system=SYSTEM, user=body,
                 purpose="overexplain_probe", prompt_version=pv,
                 temperature=0.0, max_tokens=1200)
        raw = (r.text or "").strip()
        m = raw[raw.find("{"): raw.rfind("}") + 1]
        pick = json.loads(m).get("more_explained")
        if pick in ("A", "B"):
            more_human = (pick == "A") == human_first
            verdict = "human" if more_human else "candidate"
        elif pick == "tie":
            verdict = "tie"
        else:
            status = "failed_parse"
    except Exception as e:  # noqa: BLE001
        status, payload = "failed", {"error": str(e)[:200]}
    if status == "ok":
        payload = {"more_explained": verdict, "human_was_a": human_first}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=cand.experiment_id, subject_type="candidate",
                       subject_id=cid, judge_kind="overexplain", model=model,
                       prompt_version=pv, verdict=payload, status=status))
        s.commit()
    with _lock:
        _cnt["ok" if status == "ok" else "failed"] += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None, help="只跑某批（默认全部已判二选一条目）")
    ap.add_argument("--models", default=",".join(he.JUDGES))
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    db.init_db()
    items = load_items(args.batch)
    print(f"待探针条目 = {len(items)} × {len(models)} 评委 = {len(items)*len(models)} 次调用")
    if args.dry_run or not items:
        print("dry-run：未发起")
        return
    jobs = [(it, m) for m in models for it in items]
    with ThreadPoolExecutor(max_workers=args.conc) as pool:
        for _ in pool.map(lambda j: one(*j), jobs):
            pass
    print(f"完成：ok={_cnt['ok']} failed={_cnt['failed']} skip={_cnt['skip']}")


if __name__ == "__main__":
    main()
