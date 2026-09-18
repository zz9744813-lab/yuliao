"""Writer 训练数据导出 —— 总方案 §42 / 任务 13（2026-09-17 补建）。

## 方案对训练数据的规定（§42）

```
Writer 输入：SemanticFrame + ExpressionStrategy + StyleProfile + Context
Writer 输出：Sentence / Paragraph
不要训练：Prompt → 整章小说
初期重点：Semantic → Expression
```

## 本导出的口径

**监督信号来自集霸的判定**——这是项目里最贵、也唯一可信的标签：

- 集霸判**人类胜** → 训练目标 = **人类原文**（该语义下作者认可的写法）
- 集霸判**候选胜** → 训练目标 = **候选文本**（作者认可 AI 侧那次表达，26.5% 的少数类，
  但正是"AI 也能写对"的样本，最值钱）
- 其余（tie/both_bad/未判）→ 不进 L4；若只是要预训练语料，可按 L1/L2 另导

**数据分级（§44）**：L4 = 集霸判定过；L2 = 多模型一致但无人工；L1 = 自动抽取。

## 输出

`data/exports/writer_train_<ver>.jsonl`，一行一条：
```json
{"id":"...","prev2":"...","prev1":"...","frame":{...},"target":"...",
 "target_side":"human|candidate","quality":"L4","strategy_hint":[...],
 "work":"...","segment_id":"...","candidate_id":"...","pv":"..."}
```

用法：
    python scripts/export_training.py --dry-run
    python scripts/export_training.py --ver v1
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.context_ablation import neighbors  # noqa: E402
from app.models import (Candidate, ControlledCorruption, ExpressionStrategy,  # noqa: E402
                        Frame, JudgeRun, ReviewItem, Segment, Work)

OUT_DIR = ROOT / "data" / "exports"

# 控制臂不是劣化：它是"中性改写"，用来量评委偏差（见 controlled_corruption.CONTROLS）。
# **绝不能进 DPO 对**——那等于教模型"换个说法就是错的"。
CONTROL_TYPES = frozenset({"NEUTRAL_PARAPHRASE"})


def strategy_hints() -> list[str]:
    """把策略库里"该避免的 AI 习惯"读出来，作为训练样本的提示（§42 的 ExpressionStrategy 输入）。"""
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("select name, avoid from expression_strategies").fetchall()
    con.close()
    hints = []
    for r in rows:
        av = json.loads(r["avoid"] or "[]")
        if av:
            hints.append(f"{r['name']}：{av[0]}")
    return hints


def export(ver: str) -> dict:
    con = sqlite3.connect(he.DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """select ri.human_verdict hv, c.id cid, c.text ctext, c.segment_id seg,
                  c.experiment_id exp, c.prompt_version pv, c.model
           from review_items ri join candidates c on c.id = ri.subject_id
           where ri.status='done'
             and c.prompt_version in ('reconstruct_v1','recon_ctx_v1')""").fetchall()
    con.close()

    hints = strategy_hints()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"writer_train_{ver}.jsonl"
    n_l4 = n_skip = 0
    dist = {"human": 0, "candidate": 0}
    with db.session() as s, path.open("w", encoding="utf-8") as f:
        for r in rows:
            # ⚠ ORM 的 JSON 列**已经解析成 dict**，再 json.loads 会抛 TypeError。
            # 本项目为这个坑白跑过好几次（见 HANDOVER 陷阱清单），统一走 _as_dict。
            raw = r["hv"]
            if isinstance(raw, str):
                try:
                    hv = json.loads(raw)
                except Exception:
                    hv = {}
            else:
                hv = raw or {}
            w = hv.get("winner_resolved")
            if w not in ("human", "candidate"):
                n_skip += 1
                continue
            cand = s.get(Candidate, r["cid"])
            seg = s.get(Segment, r["seg"])
            if cand is None or seg is None:
                n_skip += 1
                continue
            fr = (s.query(Frame).filter_by(experiment_id=r["exp"], segment_id=r["seg"],
                                           granularity="L", is_primary=True)
                  .filter(Frame.status != "failed").first())
            if fr is None or not fr.payload:
                n_skip += 1                      # 没有 L 帧就无法做 Semantic→Expression
                continue
            nb = neighbors(s, seg)
            work = s.get(Work, seg.work_id)
            target = cand.text if w == "candidate" else seg.text
            rec = {
                "id": r["cid"],
                "prev2": (nb["prev2"].text if nb.get("prev2") else ""),
                "prev1": (nb["prev1"].text if nb.get("prev1") else ""),
                "frame": fr.payload,
                "target": target,
                "target_side": w,
                "quality": "L4",                 # §44：集霸判定过 = L4
                "strategy_hint": hints,          # §42 的 ExpressionStrategy 输入
                "work": work.title if work else "",
                "segment_id": r["seg"], "candidate_id": r["cid"],
                "pv": r["pv"], "model": r["model"],
                "provenance": "集霸盲评判定（review_items.status=done）",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_l4 += 1
            dist[w] += 1
    return {"path": str(path), "n_l4": n_l4, "n_skip": n_skip, "dist": dist,
            "n_hints": len(hints)}


def export_corrupt_pairs(ver: str, out_dir: Path | None = None,
                         strict: bool = False) -> dict:
    """strict=True：**只用集霸判"人类原文胜"的对**。

    为什么需要这条：2026-09-18 他判完 corr24 全部 24 题，结果是
    两边都不好 11 / 打平 6 / 他选劣化版 3 / 他选人类原文 4。
    也就是说"人类原文 = chosen"这条默认假设**在这批数据上只有 17% 成立**。
    拿默认假设去训 DPO，等于让模型去模仿他认为不好（或两边都差）的文本。
    strict 模式只留下他真正背书的那些对——数据少，但方向是对的。
    """
    """导出受控劣化对照对（§7 工作流 B → §42 训练数据）。

    一行一对：`chosen = 人类原文`，`rejected = 劣化版`，两边**同一段语义、同一个上文**，
    rejected 侧还带**被判定的那个变量名**（corruption_type / variable）——
    这正是"让 AI 会写"最缺的教材：不是"这段不好"，而是"这段把该留白的东西说破了"。

    ⚠ **§14 基准隔离**：`Segment.role='benchmark'` 的段一律**不出现在导出里**。
    基准题一旦进了训练集就不再是基准（"不得被训练直接读取"是方案对 Hidden
    Benchmark 的硬要求），而 corruption 检测正是基准的必含项之一。
    被隔离掉的条数会打印出来——**静默少数据比少数据更危险**。

    ⚠ 两条纪律（§7.5 允许反例 / §52 不把评委当真理源）：

    1. `suspect` 标记：评委多数**偏好劣化版**的 pair 会被标出来，默认也导出，
       但带 `"suspect": true`。它们要么是"该劣化在此语境下确实更好"（真反例），
       要么是评委读不出这个变量——两种都可能，**在下游使用前必须先由集霸裁定**。
    2. `human_verdict` 字段：集霸判过的话原样带上（`corruption_worse` / `corruption_better`），
       没有就是空。下游可以只用他判过的子集，不必相信未裁定的部分。
    """
    # ⚠ 走 ORM 而不是裸 sqlite3 + `he.DB`：`heldout_eval.DB` 是**写死的生产库路径**，
    # 在测试（LG_DATABASE_URL 指向临时库）下会静默去读生产数据 —— 本轮实测踩到过，
    # 表现为"导出条数对不上、基准隔离读数为 0"，不报错、只给错数。
    with db.session() as s0:
        ccs = (s0.query(ControlledCorruption)
               .filter(ControlledCorruption.status == "ok")
               .filter(ControlledCorruption.candidate_id.isnot(None)).all())
        votes: dict[str, dict] = {}
        for jr in (s0.query(JudgeRun)
                   .filter(JudgeRun.judge_kind == "preference",
                           JudgeRun.status == "ok").all()):
            d = votes.setdefault(jr.subject_id, {"n": 0, "w": 0})
            d["n"] += 1
            v = jr.verdict or {}
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:
                    v = {}
            d["w"] += (v.get("winner_resolved") == "candidate")
        uvs = {}
        for ri in (s0.query(ReviewItem).filter(ReviewItem.status == "done").all()):
            uvs[ri.subject_id] = ri.human_verdict
        rows = [{"ccid": c.id, "cid": c.candidate_id, "seg": c.segment_id,
                 "ct": c.corruption_type, "var": c.variable, "vtext": c.text,
                 "note": c.gen_note, "drift": c.drift, "ds": c.drift_score,
                 "lr": c.len_ratio, "vm": c.verify_model, "gm": c.generator_model,
                 "exp": c.experiment_id,
                 "nj": votes.get(c.candidate_id, {}).get("n", 0),
                 "nw": votes.get(c.candidate_id, {}).get("w", 0),
                 "hv": uvs.get(c.candidate_id)} for c in ccs]

    # out_dir 可覆盖：测试必须能把它指到临时目录，否则会往**生产** data/exports 里写垃圾
    # （本轮实测：跑一次测试就在生产目录留下 corrupt_dpo_testbench.jsonl）。
    dest = out_dir or OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"corrupt_dpo_{ver}.jsonl"
    n = n_suspect = n_user = n_bench = n_ctrl = n_strict_skip = 0
    by_type: dict[str, int] = {}
    with db.session() as s, path.open("w", encoding="utf-8") as f:
        for r in rows:
            seg = s.get(Segment, r["seg"])
            cand = s.get(Candidate, r["cid"])
            if seg is None or cand is None:
                continue
            if seg.role == "benchmark":          # §14：基准段不进训练导出
                n_bench += 1
                continue
            if r["ct"] in CONTROL_TYPES:         # 控制臂只用于测量，不是训练信号
                n_ctrl += 1
                continue
            fr = (s.query(Frame).filter_by(experiment_id=r["exp"], segment_id=r["seg"],
                                           granularity="L", is_primary=True)
                  .filter(Frame.status != "failed").first())
            nb = neighbors(s, seg)
            work = s.get(Work, seg.work_id)
            nj, nw = int(r["nj"] or 0), int(r["nw"] or 0)
            raw_hv = r["hv"]
            hv = (json.loads(raw_hv) if isinstance(raw_hv, str)
                  else (raw_hv or {}))
            w = hv.get("winner_resolved")
            suspect = nj > 0 and nw * 2 > nj          # 评委多数偏好劣化版
            # **集霸的判定优先于"人类原文=chosen"这条默认假设**（2026-09-18 实测）：
            # 他判过的 11 题里，"两边都不好"5 题、"打平"2 题、**选了劣化版**3 题，
            # 只有 1 题选人类原文。也就是说这批劣化对里"人类原文更好"并不普遍成立
            # ——不能拿它当默认的真值，必须按他的判定标出来。
            if w in ("candidate", "both_bad", "tie"):
                suspect = True
            if strict:
                # 只留他判"人类原文胜"的；没判过的也不留（strict 就是"只信裁定"）
                if w != "human":
                    n_strict_skip += 1
                    continue
            rec = {
                "id": r["ccid"],
                "prev2": (nb["prev2"].text if nb.get("prev2") else ""),
                "prev1": (nb["prev1"].text if nb.get("prev1") else ""),
                "frame": fr.payload if fr else None,
                "chosen": seg.text,                    # 人类原文
                "rejected": r["vtext"],                # 劣化版
                "corruption_type": r["ct"],
                "corruption_variable": r["var"],       # 这一对只差的**那一个变量**
                "generator": r["gm"], "verifier": r["vm"],
                "drift": float(r["ds"] or 0.0), "len_ratio": float(r["lr"] or 0.0),
                "judge_votes": {"n": nj, "corruption_picked": nw},
                "suspect": suspect,                    # §7.5 反例 / 评委读不出
                "user_verdict": (w or ""),             # 集霸判过就有
                "quality": "L4" if w else "L2",
                "work": work.title if work else "",
                "segment_id": r["seg"], "candidate_id": r["cid"],
                "provenance": "controlled_corruption（§7）：人类原文 vs 单变量劣化版",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            n_suspect += suspect
            n_user += bool(w)
            by_type[r["ct"]] = by_type.get(r["ct"], 0) + 1
    return {"path": str(path), "n": n, "n_suspect": n_suspect, "n_user": n_user,
            "n_benchmark_held_out": n_bench, "n_control_excluded": n_ctrl,
            "by_type": by_type}


def export_sft_from_frames(ver: str, out_dir: Path | None = None) -> dict:
    """从**所有** L 主帧导出 SFT 样本（§42：Semantic → Expression）。

    关键认识：**这条线不需要集霸判题**——SFT 的目标文本就是人类原文本身。
    `export()` 只出"集霸判过"的 L4（230 条），于是把 250+ 条没人判过的帧全扔了；
    但"给定帧，人是怎么写的"这个监督信号**天然存在**，不需要任何人工标签。

    排除：`role='benchmark'` 的段（§14 隔离）、源校勘判坏的段、
    以及没有 L 主帧或帧失败的段。
    质量分级（§44）：L4 = 集霸判过；L1 = 自动抽取（本函数默认产出 L1）。
    """
    dest = out_dir or OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"writer_sft_{ver}.jsonl"
    n = n_bad = n_bench = n_l4 = n_fixture = 0
    by_work: dict[str, int] = {}
    with db.session() as s, path.open("w", encoding="utf-8") as f:
        judged = {}
        for ri in s.query(ReviewItem).filter(ReviewItem.status == "done").all():
            judged[ri.subject_id] = ri.human_verdict
        frames = (s.query(Frame).filter(Frame.granularity == "L", Frame.is_primary == True)  # noqa: E712
                  .filter(Frame.status != "failed").all())
        for fr in frames:
            seg = s.get(Segment, fr.segment_id)
            if seg is None or not fr.payload:
                continue
            if seg.role == "benchmark":          # §14：基准段不进训练
                n_bench += 1
                continue
            # fixture_* 是测试夹具（10~17 段），既不可外推也会污染统计 —— 训练数据同样不许进
            w_title = (s.get(Work, seg.work_id).title if s.get(Work, seg.work_id) else "") or ""
            if w_title.startswith("fixture"):
                n_fixture += 1
                continue
            try:
                integ = json.loads(seg.integrity or "{}")
            except Exception:
                integ = {}
            if integ.get("src_ok") is False:
                n_bad += 1
                continue
            text = seg.text_clean or seg.text
            if not text or len(text.strip()) < 20:
                n_bad += 1
                continue
            nb = neighbors(s, seg)
            work = s.get(Work, seg.work_id)
            rec = {
                "id": fr.id,
                "prev2": (nb["prev2"].text if nb.get("prev2") else ""),
                "prev1": (nb["prev1"].text if nb.get("prev1") else ""),
                "frame": fr.payload,
                "target": text,
                "target_side": "human",
                "quality": "L1",                 # §44：自动抽取（无人工）
                "strategy_hint": strategy_hints(),
                "work": work.title if work else "",
                "segment_id": seg.id, "frame_id": fr.id,
                "pv": fr.prompt_version,
                "provenance": "L 主帧 → 人类原文（自动，无需人工判定）",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
            n += 1
            by_work[rec["work"]] = by_work.get(rec["work"], 0) + 1
    return {"path": str(path), "n": n, "n_skipped_bad_src": n_bad,
            "n_skipped_benchmark": n_bench, "n_skipped_fixture": n_fixture,
            "by_work": by_work}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ver", default="v1")
    ap.add_argument("--pairs", action="store_true", help="另导出受控劣化对照对（DPO 形态）")
    ap.add_argument("--strict", action="store_true",
                    help="--pairs 的严格模式：只导集霸判「人类原文胜」的对（默认按假定方向导）")
    ap.add_argument("--from-frames", action="store_true",
                    help="从所有 L 主帧导出 SFT（frame→人类原文，无需人工标签）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    if args.dry_run:
        con = sqlite3.connect(he.DB)
        n = con.execute("""select count(*) from review_items ri join candidates c on c.id=ri.subject_id
                           where ri.status='done' and c.prompt_version in
                           ('reconstruct_v1','recon_ctx_v1')""").fetchone()[0]
        con.close()
        print(f"dry-run：候选条目 {n} 条；策略提示 {len(strategy_hints())} 条")
        return
    if args.from_frames:
        q = export_sft_from_frames(args.ver)
        print(f"SFT 导出完成 → {q['path']}")
        print(f"  样本 {q['n']} 条（L1 自动）；跳过：坏源 {q['n_skipped_bad_src']} / "
              f"基准段 {q['n_skipped_benchmark']} / 夹具 {q['n_skipped_fixture']}")
        print(f"  语料分布 {json.dumps(q['by_work'], ensure_ascii=False)}")
        return
    if args.pairs:
        p = export_corrupt_pairs(args.ver, strict=args.strict)
        print(f"劣化对照对导出完成 → {p['path']}")
        print(f"  共 {p['n']} 对（其中被评委判「劣化版更好」的嫌疑对 "
              f"{p['n_suspect']} 条；集霸已裁定 {p['n_user']} 条）")
        print(f"  §14 基准隔离：{p['n_benchmark_held_out']} 对留在基准里、未进训练导出")
        print(f"  控制臂剔除：{p['n_control_excluded']} 对（中性改写不是劣化，不能当 rejected）")
        if p.get("n_strict_skipped"):
            print(f"  strict：跳过 {p['n_strict_skipped']} 对（集霸未判、或判的不是人类胜）")
        print(f"  类型分布 {json.dumps(p['by_type'], ensure_ascii=False)}")
        return
    info = export(args.ver)
    print(f"导出完成 → {info['path']}")
    print(f"  L4 样本 = {info['n_l4']} 条（人类侧目标 {info['dist']['human']} / "
          f"候选侧目标 {info['dist']['candidate']}）")
    print(f"  跳过 = {info['n_skip']} 条（无 L 帧 / 非二选一）")
    print(f"  每条带 ExpressionStrategy 提示 {info['n_hints']} 条（§42 的输入之一）")


if __name__ == "__main__":
    main()
