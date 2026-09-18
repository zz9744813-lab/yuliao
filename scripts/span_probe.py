"""缺陷定位探针（2026-09-17）：把评测从「整段偏好」换成「缺陷位置」。

## 为什么要换

同一个信号，三种全局口径全部失败：
- 词表密度（明喻/微动作/叙述者判断）→ 预测集霸判断 AUC **0.427**；
- LLM 直接问「哪段把话说得更尽」→ 对齐率 **0.24**；
- LLM few-shot（喂他自己标的 17 处原文当例子）→ 对齐率 **0.17**，且 83% 判「人类更啰嗦」。

三者一致地抓不到他的判断。而集霸自己说过：**「有些段落整体其实还可以，
但总有几处坏的把整段拖垮」**——他的信号是**局部**的，不是整段的。
他也自发去划词标注（49 处），从没自发说过"这段整体更好"。

## 本探针

给 LLM **一段**文本（不说这是人类还是候选），让它指出它认为的缺陷位置（按集霸的
类别词表），然后与**集霸本人标的 span** 比位置重合。

关键：这是 span vs span，不是 passage vs passage。评测用 `--eval`。

用法：
    python scripts/span_probe.py --run            # 对全部带批注的题跑探针
    python scripts/span_probe.py --eval           # 算与集霸标注的重合
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

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.gateway import chat  # noqa: E402
from app.models import Candidate, JudgeRun, Segment  # noqa: E402

PV = "span_defect_v1"
KINDS = ["用词", "解释过度", "情绪直给", "节奏", "逻辑", "意象", "其他"]
SYSTEM = "你是中文小说编辑，只做一件事：指出文本里的缺陷位置，不做总体评价。"
PROMPT = """下面是一段中文小说正文。作者审稿时会指出**具体哪几处**写得不好，
类别从这些里选：{kinds}。

只做一件事：找出你认为该被指出的位置，**逐字引用原文**（8~40 字），并给类别。

正文：
{text}

只输出一行 JSON（没有就空数组）：
{{"defects": [{{"quote": "原文片段", "kind": "类别"}}]}}"""

_lock = threading.Lock()
_cnt = {"ok": 0, "failed": 0, "skip": 0}


def annotated_items() -> list[dict]:
    """有集霸 span 标注的题（candidate 侧标注 → 探候选文本；human 侧 → 探人类文本）。"""
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.id rid, ri.human_verdict hv, s.text stext, c.text ctext
           from review_items ri join candidates c on c.id = ri.subject_id
           join segments s on s.id = c.segment_id
           where ri.status='done'""").fetchall()
    con.close()
    out = []
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        anns = [a for a in (hv.get("annotations") or []) if a.get("target") in ("human", "candidate")]
        if not anns:
            continue
        for side in ("candidate", "human"):
            mine = [a for a in anns if a["target"] == side and a.get("verified")]
            if not mine:
                continue
            out.append({"rid": r["rid"], "side": side,
                        "text": r["ctext"] if side == "candidate" else r["stext"],
                        "mine": [{"quote": a.get("text"), "kind": a.get("kind")} for a in mine]})
    return out


def _cid_for(rid: str) -> str | None:
    con = sqlite3.connect(he.DB)
    r = con.execute("select subject_id from review_items where id=?", (rid,)).fetchone()
    con.close()
    return r[0] if r else None


def one(item: dict, model: str) -> None:
    side, text = item["side"], item["text"] or ""
    cid = _cid_for(item["rid"])
    tag = f"{cid}|{side}"
    with db.session() as s:
        done = s.query(JudgeRun).filter_by(subject_type="candidate", subject_id=tag,
                                           judge_kind="span_defect", model=model,
                                           prompt_version=PV, status="ok").first()
        if done:
            with _lock:
                _cnt["skip"] += 1
            return
        exp = (s.get(Candidate, cid).experiment_id if cid else None)
    body = PROMPT.format(kinds="、".join(KINDS), text=text[:3000])
    payload, status = None, "ok"
    try:
        r = chat(model=model, system=SYSTEM, user=body,
                 purpose="span_defect_probe", prompt_version=PV,
                 temperature=0.0, max_tokens=1200)
        raw = (r.text or "").strip()
        m = raw[raw.find("{"): raw.rfind("}") + 1]
        got = json.loads(m).get("defects", [])
        payload = {"defects": [{"quote": str(d.get("quote", ""))[:60],
                                "kind": d.get("kind")} for d in got][:12]}
    except Exception as e:  # noqa: BLE001
        status, payload = "failed", {"error": str(e)[:200]}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=exp or he.EXP, subject_type="candidate", subject_id=tag,
                       judge_kind="span_defect", model=model, prompt_version=PV,
                       verdict=payload, status=status))
        s.commit()
    with _lock:
        _cnt["ok" if status == "ok" else "failed"] += 1


def overlap(mine: list[dict], got: list[dict]) -> dict:
    """集霸的 span 有几个被探针命中（双向：他的→探针、探针的→他的）。"""
    mt = [m["quote"] for m in mine if m.get("quote")]
    gt = [g["quote"] for g in got if g.get("quote")]
    hit = 0
    for a in mt:
        if any((a in b) or (b in a) or _share(a, b) for b in gt):
            hit += 1
    hit2 = 0
    for b in gt:
        if any((a in b) or (b in a) or _share(a, b) for a in mt):
            hit2 += 1
    return {"mine": len(mt), "got": len(gt), "mine_hit": hit, "got_hit": hit2}


def _share(a: str, b: str, k: int = 6) -> bool:
    """至少共享 k 个连续字（中文里比字符集交集稳）。"""
    if len(a) < k or len(b) < k:
        return False
    return any(a[i:i + k] in b for i in range(len(a) - k + 1))


def run(models, conc) -> None:
    items = annotated_items()
    jobs = [(it, m) for m in models for it in items]
    print(f"待探针文本 = {len(items)} × {len(models)} 评委 = {len(jobs)} 次调用")
    with ThreadPoolExecutor(max_workers=conc) as pool:
        for _ in pool.map(lambda j: one(*j), jobs):
            pass
    print(f"完成：ok={_cnt['ok']} failed={_cnt['failed']} skip={_cnt['skip']}")


def evaluate(by_model: bool = True) -> None:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    got = {}
    for r in con.execute("""select subject_id, model, verdict from judge_runs
                            where judge_kind='span_defect' and status='ok'"""):
        d = json.loads(r["verdict"]) if isinstance(r["verdict"], str) else r["verdict"]
        got.setdefault(r["subject_id"], {})[r["model"]] = (d or {}).get("defects", [])
    con.close()
    agg: dict[str, list] = {}
    for it in annotated_items():
        cid = _cid_for(it["rid"])
        tag = f"{cid}|{it['side']}"
        per = got.get(tag)
        if not per:
            continue
        for model, defects in per.items():
            ov = overlap(it["mine"], defects)
            agg.setdefault(model, []).append((ov, it["side"]))
    print()
    print("== 探针标记 vs 集霸标注：位置重合 ==")
    for model, rows in agg.items():
        n = len(rows)
        mine = sum(o["mine"] for o, _ in rows)
        hit = sum(o["mine_hit"] for o, _ in rows)
        gt = sum(o["got"] for o, _ in rows)
        hit2 = sum(o["got_hit"] for o, _ in rows)
        print(f"  {model.split('/')[-1][:12]:13s} 文本 {n:3d} 篇")
        print(f"      集霸标了 {mine:3d} 处，其中 {hit:3d} 处被探针也标到 = {hit/max(1,mine):.2f}")
        print(f"      探针标了 {gt:3d} 处，其中 {hit2:3d} 处与集霸重合 = {hit2/max(1,gt):.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--models", default=",".join(he.JUDGES))
    ap.add_argument("--conc", type=int, default=4)
    a = ap.parse_args()
    db.init_db()
    if a.run:
        run([m.strip() for m in a.models.split(",") if m.strip()], a.conc)
    if a.eval:
        evaluate()


if __name__ == "__main__":
    main()
