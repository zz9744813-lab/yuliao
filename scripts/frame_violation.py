"""帧约束违规检测 —— 方案 §11.1 SemanticVerifier × §4.2 SemanticFrame × §52 第二问。

## 为什么做这个

方案 §52 要系统定量回答的第二个问题就是：
**「AI 为什么喜欢把隐含信息说完整？」**

而 §4.2 的 SemanticFrame **本来就声明了这些约束**，实测库里 305/313 个 L 主帧都带：

```
must_not_state                例：["箭是谁射的", "阴魂是否已被消灭"]
reader_should_infer           例：["战斗暂时进入间隙", "众人身份不凡却同样畏惧死亡"]
expression_constraints        例：{"explicitness":"low","psychological_explanation_allowed":false,
                                    "dialogue_allowed":false,"rhythm_target":"缓慢平叙"}
```

也就是说：**语义侧早就写明了"哪些不许直说、哪些该让读者自己推断"**，
剩下的只是一个可测量的问题——**候选文本有没有违反它**。

这不需要集霸再判任何题，且直接对应 §4.5 ExpressionResidual 里的
`analysis.added_information / explicitness / psychological_labeling`。

## 口径（严格照 §11.1）

SemanticVerifier **只判信息**（事实一致性 / 信息增加 / 信息丢失 / 语义漂移），
**禁止评价文采**——所以本探针问的是"这些内容有没有被写出来"，不是"写得好不好"。

落库：`judge_kind='semantic'`、`prompt_version='frame_constraint_v1'`（可审计、可重跑）。

用法：
    python scripts/frame_violation.py --dry-run
    python scripts/frame_violation.py --models moonshotai/kimi-k3 --conc 4
    python scripts/frame_violation.py --report
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
from _conc_guard import check_conc, pool_workers  # noqa: E402  # 并发闸共用入口（上界 app/limits.MAX_CONCURRENCY + 运行时兜底）
from app import db  # noqa: E402
from app.gateway import chat, is_serial_model  # noqa: E402
from app.models import Candidate, Frame, JudgeRun, ReviewItem, Segment  # noqa: E402

PV = "frame_constraint_v1"
SYSTEM = ("你是语义校核器。只核对该说的信息是否被说出、不该说的信息是否被说出，"
          "不评价文笔、不给总体好坏判断。")
PROMPT = """下面是一个语义帧对某段文字的**约束**，以及一段**候选文本**（由该帧重建）。

【不许直接写出】must_not_state：{mns}
【应留给读者推断】reader_should_infer：{rsi}
【表达约束】{ec}

候选文本：
{cand}

请只核对信息层面，逐条回答：
1. must_not_state 里的每一条，候选有没有**直接写出来**（不是"能不能推断"，是"说没说出口"）；
2. reader_should_infer 里的每一条，候选有没有**替读者把结论说破**；
3. 候选有没有明显超出 expression_constraints 的地方（尤指 psychological_explanation_allowed=false
   却出现直接的心理解释）。

只输出一行 JSON：
{{"mns_stated":[{{"item":"...","stated":true|false,"quote":"原文片段或空"}}],
  "rsi_spoiled":[{{"item":"...","spoiled":true|false,"quote":"..."}}],
  "psych_explained":true|false}}"""

_lock = threading.Lock()
_cnt = {"ok": 0, "failed": 0, "skip": 0}


def rich_frames() -> dict[str, dict]:
    """segment_id → 富帧 payload（带 must_not_state 的 L 主帧）。"""
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select segment_id, payload, experiment_id from frames
           where granularity='L' and is_primary=1 and status!='failed'""").fetchall()
    con.close()
    out = {}
    for r in rows:
        d = json.loads(r["payload"])
        if "must_not_state" in d or "reader_should_infer" in d:
            out[f"{r['experiment_id']}|{r['segment_id']}"] = d
    return out


def load_targets() -> list[dict]:
    """已判候选（二选一）+ 其富帧。"""
    frames = rich_frames()
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, c.id cid, c.text ctext, c.segment_id seg,
                  c.experiment_id exp, c.model
           from review_items ri join candidates c on c.id = ri.subject_id
           where ri.status='done' and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')"""
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        hv = json.loads(r["hv"]) if r["hv"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        fr = frames.get(f"{r['exp']}|{r['seg']}")
        if not fr:
            continue
        out.append({"cid": r["cid"], "cand": r["ctext"], "frame": fr,
                    "user": w, "exp": r["exp"], "model": r["model"]})
    return out


def one(item: dict, model: str) -> None:
    cid = item["cid"]
    with db.session() as s:
        done = s.query(JudgeRun).filter_by(subject_type="candidate", subject_id=cid,
                                           judge_kind="semantic", model=model,
                                           prompt_version=PV, status="ok").first()
        if done:
            with _lock:
                _cnt["skip"] += 1
            return
    fr = item["frame"]
    body = PROMPT.format(
        mns=json.dumps(fr.get("must_not_state") or [], ensure_ascii=False),
        rsi=json.dumps(fr.get("reader_should_infer") or [], ensure_ascii=False),
        ec=json.dumps(fr.get("expression_constraints") or {}, ensure_ascii=False),
        cand=(item["cand"] or "")[:2500])
    payload, status = None, "ok"
    try:
        r = chat(model=model, system=SYSTEM, user=body, purpose="frame_constraint",
                 prompt_version=PV, temperature=0.0, max_tokens=900)
        raw = (r.text or "").strip()
        d = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        mns = d.get("mns_stated") or []
        rsi = d.get("rsi_spoiled") or []
        payload = {
            "mns_total": len(fr.get("must_not_state") or []),
            "mns_stated": sum(1 for x in mns if x.get("stated")),
            "rsi_total": len(fr.get("reader_should_infer") or []),
            "rsi_spoiled": sum(1 for x in rsi if x.get("spoiled")),
            "psych_explained": bool(d.get("psych_explained")),
            "mns_detail": mns[:8], "rsi_detail": rsi[:8],
        }
        # 兼作可读的"方向"字段，方便与既有工具共用
        payload["winner_resolved"] = ("candidate" if (payload["mns_stated"] or payload["rsi_spoiled"])
                                      else "human")
    except Exception as e:  # noqa: BLE001
        status, payload = "failed", {"error": str(e)[:200]}
    with db.session() as s:
        s.add(JudgeRun(experiment_id=item["exp"], subject_type="candidate", subject_id=cid,
                       judge_kind="semantic", model=model, prompt_version=PV,
                       verdict=payload, status=status))
        s.commit()
    with _lock:
        _cnt["ok" if status == "ok" else "failed"] += 1


def report() -> None:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select jr.subject_id cid, jr.model, jr.verdict v, ri.human_verdict hv, c.model cmodel
           from judge_runs jr
           join review_items ri on ri.subject_id = jr.subject_id and ri.status='done'
           join candidates c on c.id = jr.subject_id
           where jr.judge_kind='semantic' and jr.prompt_version=? and jr.status='ok'""",
        (PV,)).fetchall()
    con.close()
    agg = {}
    for r in rows:
        d = json.loads(r["v"]) if isinstance(r["v"], str) else r["v"]
        hv = json.loads(r["hv"]) if r["hv"] else {}
        w = hv.get("winner_resolved")
        if w not in ("human", "candidate"):
            continue
        agg.setdefault(r["model"], []).append((d, w, r["cmodel"]))
    print()
    print("== 帧约束违规 vs 集霸判定 ==")
    for m, items in agg.items():
        n = len(items)
        viol = [x for x in items if x[0].get("mns_stated") or x[0].get("rsi_spoiled")]
        # 违规 → 集霸判候选胜的比例
        vc = sum(1 for x in viol if x[1] == "candidate") / max(1, len(viol))
        # 不违规 → 候选胜比例
        nv = [x for x in items if not (x[0].get("mns_stated") or x[0].get("rsi_spoiled"))]
        nc = sum(1 for x in nv if x[1] == "candidate") / max(1, len(nv))
        print(f"\n  {m.split('/')[-1][:14]}  n={n}")
        print(f"    违规 {len(viol):3d} 条 → 集霸判候选胜 {vc:.3f}")
        print(f"    不违规 {len(nv):3d} 条 → 集霸判候选胜 {nc:.3f}")
        print(f"    违规则（must_not_state 说出口 / 推断被说破）")
        bym = {}
        for d, w, cm in items:
            k = cm.split("/")[-1][:14]
            a = bym.setdefault(k, [0, 0, 0])
            a[0] += 1
            a[1] += bool(d.get("mns_stated") or d.get("rsi_spoiled"))
            a[2] += bool(d.get("psych_explained"))
        print("    按生成模型：")
        for k, (n2, v2, p2) in sorted(bym.items(), key=lambda x: -x[1][0]):
            print(f"      {k:16s} n={n2:3d}  违规率 {v2/n2:.2f}  心理解释率 {p2/n2:.2f}")


def _run_pool(jobs: list, conc: int, models) -> None:
    """执行本脚本全部判定调用；worker 数由 pool_workers 兜底（上限截断+串行强制）。"""
    workers = pool_workers(conc, models, serial_check=is_serial_model)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(lambda j: one(*j), jobs):
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="moonshotai/kimi-k3")
    ap.add_argument("--conc", type=int, default=4,
                    help=f"线程池并发（上界 app/limits.MAX_CONCURRENCY，越界报错退出；"
                         f"--models 命中单账号 CLI 通道时强制串行 workers=1）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    check_conc(ap, args.conc, "--conc")   # 闸在 db.init_db() 之前：越界响亮报错退出
    db.init_db()
    if args.report:
        report()
        return
    items = load_targets()
    print(f"已判候选且有富帧 = {len(items)} 条")
    print(f"需 {len(items)*len(args.models.split(','))} 次调用")
    if args.dry_run:
        print("dry-run：未发起")
        return
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    # 批量防呆①（P0 死 id 事故）：池外模型的表现是 failed=整批，与"没货"同形
    pf.require_models(models, source="frame_violation")
    jobs = [(it, m) for m in models for it in items]
    _run_pool(jobs, args.conc, models)
    print(f"完成：ok={_cnt['ok']} failed={_cnt['failed']} skip={_cnt['skip']}")
    report()


if __name__ == "__main__":
    main()
