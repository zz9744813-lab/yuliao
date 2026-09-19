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
    python scripts/export_training.py --pairs          # DPO 对照对
    python scripts/export_training.py --from-frames    # SFT（帧→人类原文）
    python scripts/export_training.py --rm             # 奖励模型数据（text/score/label_source）
    python scripts/export_training.py --rewrite        # 改写对（instruction=帧要点 / output=人类原文）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_eval as he  # noqa: E402
from app import db  # noqa: E402
from app.context_ablation import neighbors  # noqa: E402
from app.models import (BenchmarkItem, Candidate, ControlledCorruption,  # noqa: E402
                        ExpressionStrategy, Frame, JudgeRun, ReviewItem, Segment, Work)
from make_random_batch import looks_watermarked  # noqa: E402

OUT_DIR = ROOT / "data" / "exports"

# 控制臂不是劣化：它是"中性改写"，用来量评委偏差（见 controlled_corruption.CONTROLS）。
# **绝不能进 DPO 对**——那等于教模型"换个说法就是错的"。
CONTROL_TYPES = frozenset({"NEUTRAL_PARAPHRASE"})

# winner_resolved → (人类侧分, 候选侧分)。解析不出的（'B' / cant_judge / None）不在表里，
# 跳过并计数——**不许把读不懂的判定硬编成分数**（纪律④）。
# tie 有两种历史写法：review_items 用 'tie'，judge_runs 用 'equal'，都认。
SCORE_MAP = {
    "human": (1.0, 0.0),
    "candidate": (0.0, 1.0),
    "tie": (0.5, 0.5), "equal": (0.5, 0.5),
    "both_bad": (0.0, 0.0),   # 两边都不好：都给 0，不硬造高低
}

RM_PROVENANCE = {
    "user_verdict": "集霸盲评裁定（review_items.status=done）",
    "corruption_variable": "controlled_corruption（§7）：人类原文 vs 单变量劣化，变量级标签",
    "judge_majority": "评委多数票（judge_runs, preference）——弱标签",
}


def _extras_boundary(s, work_id: str, seg_version: int | None) -> int | None:
    """番外区边界：第一个「番外正文段」的 ordinal；没有番外则 None。

    复用 make_random_batch.extras_start 的识别依据（「番外」开头的段），但**不能
    直接套用**：v1 老切分把多章标题连排成目录簇（琼明 v1 ordinal 29 起就是
    「番外 多年之后3…番外 多年之后4…」，一段含 3~4 个「番外」），按老口径会把
    整部书误判成番外——本轮实测误剔 192 条已判定候选。目录簇的特征是「番外」
    **多次出现**，真正的番外正文段只出现一次（v1 正文起点 12455、v2 起点 18308，
    两种切分下都验证过）。所以这里只认单次出现的段当边界。
    """
    rows = (s.query(Segment.ordinal, Segment.text)
            .filter(Segment.work_id == work_id, Segment.seg_version == seg_version,
                    Segment.text.like("番外%"))
            .order_by(Segment.ordinal).all())
    for ordinal, text in rows:
        if (text or "").count("番外") == 1:
            return ordinal
    return None


_BENCH_HASHES: set | None = None


def _bench_hashes() -> set:
    """内容级隔离（军师 P1-5）：全部基准条目冻结文本（a/b/≥50字context）的规范化哈希。

    为什么在 _excluded_reason 内部懒加载：隔离只查目标段 role 会漏两种情况——
    ①同一内容在不同切分版本下有两个 segment id（一个 benchmark、一个 None→可训练，
    hvai 原文就是这样漏进训练导出的）；②条目 context（邻段原文）与可训练段相同。
    懒加载让不接触基准表的调用方零开销。
    """
    global _BENCH_HASHES
    if _BENCH_HASHES is None:
        import hashlib
        def _norm(t: str) -> str:
            return "".join((t or "").split())
        hs = set()
        with db.session() as s:
            for it in s.query(BenchmarkItem).all():
                for t in (it.text_a, it.text_b):
                    if t:
                        hs.add(hashlib.md5(_norm(t).encode("utf-8")).hexdigest())
                if it.context and len(it.context) >= 50:
                    hs.add(hashlib.md5(_norm(it.context).encode("utf-8")).hexdigest())
        _BENCH_HASHES = hs
    return _BENCH_HASHES


def _dedupe_rm_rows(pending: list[dict]) -> tuple[list[dict], int]:
    """RM 行去重（军师 P1-6 / 会审二轮）：同段同文本多来源冲突只留一条。

    规则：来源优先级 user_verdict(3) > corruption_variable(2) > judge_majority(1)；
    同优先级平局按 id 字典序取最小（id 均为 "CID-hex"/"SEG-hex" 定长字符串，
    字典序与生成序一致）。缺 segment_id → KeyError 响炸（emit 侧另有
    _require_row_keys 前置闸，这里是同一不变量的第二道闸）。
    返回 (去重后的行列表按 id 排序, 丢弃数)。
    """
    prio = {"user_verdict": 3, "corruption_variable": 2, "judge_majority": 1}
    def _norm_txt(t: str) -> str:
        return "".join((t or "").split())
    best: dict[tuple, dict] = {}
    n_conflict_dropped = 0
    for row in pending:
        if not row.get("segment_id"):
            # emit 侧 _require_row_keys 已拦过一道；这里兜 None/空串值
            raise KeyError(f"RM 行缺 segment_id：{row.get('id')!r}")
        key = (row["segment_id"], _norm_txt(row["text"]))
        cur = best.get(key)
        if cur is None:
            best[key] = row
            continue
        n_conflict_dropped += 1
        cand_prio = prio.get(row["label_source"], 0)
        cur_prio = prio.get(cur["label_source"], 0)
        if cand_prio > cur_prio or (cand_prio == cur_prio and row["id"] < cur["id"]):
            best[key] = row
    rows = sorted(best.values(), key=lambda r: r["id"])
    return rows, n_conflict_dropped


def _require_row_keys(segment_id, candidate_id) -> None:
    """RM 行主键前置校验：path.open("w") 会清空旧文件，写半截才 KeyError 的
    窗口必须关死（会审 2026-09-19）。缺任一键 → 响亮 ValueError。"""
    if not segment_id or not candidate_id:
        raise ValueError(f"RM 行缺主键（segment_id/candidate_id）：{segment_id!r}/{candidate_id!r}")


def _hits_bench_text(text: str | None) -> bool:
    if not text:
        return False
    import hashlib
    return hashlib.md5("".join(text.split()).encode("utf-8")).hexdigest() in _bench_hashes()


def _excluded_reason(s, seg: Segment | None, starts: dict, *,
                     for_train: bool = False) -> str:
    """训练导出的隔离闸门：返回剔除原因（空串 = 放行）。

    硬约束（任务 13 钉死，回归测试锁定）——以下段落**一律不进任何训练导出**：

    1. `role='benchmark'`（§14：基准段被训练读到就不再是基准）；
    2. 番外区段（集霸 2026-09-17 定的策略：只排番外，其余照抽；
       边界 = 该作品第一个番外正文段的 ordinal，见 _extras_boundary）；
    3. 水印伪影段（盗版 txt 掺拼音/乱码，只污染人类那一侧，会制造不公平比较，
       见 make_random_batch.looks_watermarked）。

    for_train=True 再加两条质量闸（SFT/RM 口径；DPO 的段在劣化生成端已查过源）：
    fixture_* 夹具作品、源校勘判坏（integrity.src_ok=false）的段。

    starts 缓存 {(work_id, seg_version): extras 起始 ordinal}，避免每段一查。
    """
    if seg is None:
        return "missing"
    if seg.role == "benchmark":
        return "benchmark"
    key = (seg.work_id, seg.seg_version)
    if key not in starts:
        starts[key] = _extras_boundary(s, seg.work_id, seg.seg_version)
    st = starts[key]
    if st is not None and seg.ordinal >= st:
        return "extras"
    if looks_watermarked(seg.text or ""):
        return "watermark"
    if for_train:
        work = s.get(Work, seg.work_id)
        if work is not None and (work.title or "").startswith("fixture"):
            return "fixture"
        try:
            integ = json.loads(seg.integrity or "{}")
        except Exception:
            integ = {}
        # 军师 P1-6：训练口径要求 src_ok **必须 True**——"没查过=不可用"（铁律）。
        # 旧口径只排 False，32 条未校勘段就这么混进了 SFT。
        # 会审意见：False（查过且判坏）与 None（从未查过）是**互斥口径**，分开报。
        if integ.get("src_ok") is False:
            return "bad_src"
        if integ.get("src_ok") is not True:   # 缺键与 null 同属"从未校验"
            return "src_unverified"
    # 内容级隔离（P1-5）：无论 role，正文命中基准冻结文本即剔除（跨切分孪生/同文）
    if _hits_bench_text(seg.text_clean or seg.text if seg else None):
        return "benchmark_content"
    # 第②种泄漏：条目 context（基准段的邻段原文）与可训练段的邻段相同
    if for_train and seg is not None:
        prevs = (s.query(Segment)
                 .filter(Segment.work_id == seg.work_id,
                         Segment.seg_version == seg.seg_version,
                         Segment.ordinal < seg.ordinal,
                         Segment.ordinal >= seg.ordinal - 2)
                 .all())
        for pv_seg in prevs:
            if _hits_bench_text(pv_seg.text_clean or pv_seg.text):
                return "benchmark_content"
    return ""


def _majority_winner(ws: list[str]) -> str:
    """同一候选的评委多票 → 多数票。无票返回空串；平票归 equal（不硬造胜负）。"""
    if not ws:
        return ""
    c: dict[str, int] = {}
    for w in ws:
        c[w] = c.get(w, 0) + 1
    top = max(c.values())
    leads = [k for k, v in c.items() if v == top]
    return leads[0] if len(leads) == 1 else "equal"


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
    starts: dict = {}
    n_hold: dict[str, int] = {}
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
            reason = _excluded_reason(s, seg, starts)   # §14 基准 / 番外 / 水印：不进导出
            if reason:
                n_skip += 1
                n_hold[reason] = n_hold.get(reason, 0) + 1
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
            "n_hints": len(hints), "n_held_out": n_hold}


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
    n = n_suspect = n_user = n_bench = n_ctrl = n_strict_skip = n_dirty = 0
    n_bench_content = 0
    by_type: dict[str, int] = {}
    starts: dict = {}
    with db.session() as s, path.open("w", encoding="utf-8") as f:
        for r in rows:
            seg = s.get(Segment, r["seg"])
            cand = s.get(Candidate, r["cid"])
            if seg is None or cand is None:
                continue
            reason = _excluded_reason(s, seg, starts)   # §14 基准 / 番外 / 水印：不进导出
            if reason == "benchmark":
                n_bench += 1
                continue
            if reason in ("watermark", "extras") or reason == "benchmark_content":
                n_dirty += 1
                if reason == "benchmark_content":
                    n_bench_content += 1
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
            "n_benchmark_content_excluded": n_bench_content, "n_benchmark_held_out": n_bench, "n_control_excluded": n_ctrl,
            "n_dirty_held_out": n_dirty, "by_type": by_type}


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
    n = n_bad = n_bench = n_l4 = n_fixture = n_dirty = 0
    n_bench_content = 0
    n_src_unv = 0
    segs_covered: set = set()
    by_work: dict[str, int] = {}
    starts: dict = {}
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
            reason = _excluded_reason(s, seg, starts, for_train=True)
            if reason == "benchmark":            # §14：基准段不进训练
                n_bench += 1
                continue
            if reason == "fixture":
                n_fixture += 1
                continue
            if reason in ("bad_src", "src_unverified"):
                n_bad += 1
                if reason == "src_unverified":
                    n_src_unv += 1
                continue
            if reason == "benchmark_content":    # P1-5：内容级隔离
                n_bench_content += 1
                continue
            if reason:                           # watermark / extras（任务 13 硬约束）
                n_dirty += 1
                continue
            text = seg.text_clean or seg.text
            if not text or len(text.strip()) < 20:
                n_bad += 1
                continue
            segs_covered.add(seg.id)
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
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            by_work[rec["work"]] = by_work.get(rec["work"], 0) + 1
    return {"path": str(path), "n": n, "n_skipped_bad_src": n_bad,
            "n_segments_covered": len(segs_covered), "n_src_unverified_excluded": n_src_unv,
            "n_skipped_benchmark": n_bench, "n_skipped_fixture": n_fixture,
            "n_skipped_dirty": n_dirty, "by_work": by_work}


def export_rm(ver: str, out_dir: Path | None = None) -> dict:
    """奖励模型（RM）训练数据 —— 任务 13 补齐的第一种口径（总方案 §42）。

    一行一条，骨架只有三件：`text / score / label_source`（另带 weak、side、suspect
    与溯源 id）。分数**只来自库里已有的真实标注**，一条都不许编：

    ① `corruption_variable` —— 受控劣化对（人类原文 vs 单变量劣化版）。
       「劣化版更差」这个标签来自劣化**变量本身**（§7 的已知答案，与 DPO 默认方向
       同一条真值来源）；集霸**裁定过**的条目改用他的判定（升级为 user_verdict），
       评委多数票偏好劣化版的带 `suspect: true`（§7.5 反例，须人工复核）。
    ② `user_verdict` —— 集霸判定过的 review_items（status=done），winner_resolved
       映射成分数（SCORE_MAP）：human→1/0，candidate→0/1，tie/equal→0.5/0.5，
       both_bad→0/0。
    ③ `judge_majority` —— 评委判定（judge_runs, preference）按候选聚合多数票，
       **显式 `weak: true`**（§52：评委不是真理源，只能当弱标签）。

    隔离（任务 13 硬约束）：基准段 / 番外段 / 水印段一律不进（_excluded_reason）；
    控制臂 NEUTRAL_PARAPHRASE 是中性改写不是劣化，**正负例都不给**。

    summary 同名落盘 `rm_<ver>_summary.json`：条数 / 来源分布 / 正负比。
    """
    dest = out_dir or OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"rm_{ver}.jsonl"
    sum_path = dest / f"rm_{ver}_summary.json"
    with db.session() as s:
        # —— 预取三类来源的原始标注，避免循环里 N+1 ——
        uvs: dict[str, dict] = {}
        for ri in s.query(ReviewItem).filter(ReviewItem.status == "done").all():
            uvs[ri.subject_id] = he._as_dict(ri.human_verdict) or {}
        votes: dict[str, list[str]] = {}
        for jr in (s.query(JudgeRun)
                   .filter(JudgeRun.judge_kind == "preference",
                           JudgeRun.status == "ok").all()):
            w = (he._as_dict(jr.verdict) or {}).get("winner_resolved")
            if w:
                votes.setdefault(jr.subject_id, []).append(w)
        cc_by_cand: dict[str, ControlledCorruption] = {}
        for cc in (s.query(ControlledCorruption)
                   .filter(ControlledCorruption.status == "ok",
                           ControlledCorruption.candidate_id.isnot(None)).all()):
            cc_by_cand[cc.candidate_id] = cc

        subjects = sorted(set(uvs) | set(votes) | set(cc_by_cand))
        starts: dict = {}
        n = n_skip_missing = n_unresolved = n_text_empty = n_ctrl = 0
        n_bench = n_wm = n_ex = n_fix = n_bad = 0
        n_bench_content = 0
        n_src_unv = 0
        by_source = {"corruption_variable": 0, "user_verdict": 0, "judge_majority": 0}
        pos = neg = neu = n_weak = 0
        pending: list[dict] = []
        for cid in subjects:
                cand = s.get(Candidate, cid)
                if cand is None:
                    # 孤儿判定（候选行已不在库，verdict 里也没有原文）：跳过并计数，不许编文本
                    n_skip_missing += 1
                    continue
                seg = s.get(Segment, cand.segment_id)
                reason = _excluded_reason(s, seg, starts, for_train=True)
                if reason == "benchmark":
                    n_bench += 1
                    continue
                if reason == "watermark":
                    n_wm += 1
                    continue
                if reason == "extras":
                    n_ex += 1
                    continue
                if reason == "fixture":
                    n_fix += 1
                    continue
                if reason == "bad_src":
                    n_bad += 1
                    continue
                if reason == "src_unverified":
                    n_src_unv += 1
                    continue
                if reason == "benchmark_content":    # P1-5：内容级隔离
                    n_bench_content += 1
                    continue
                if reason:
                    n_skip_missing += 1
                    continue
                cc = cc_by_cand.get(cid)
                if cc is not None and cc.corruption_type in CONTROL_TYPES:
                    n_ctrl += 1              # 控制臂：中性改写，不是负例也不是正例
                    continue
                htext = (seg.text_clean or seg.text or "").strip()
                if not htext or len(htext) < 20:
                    n_skip_missing += 1
                    continue
                nb = neighbors(s, seg)
                work = s.get(Work, seg.work_id)
                base = {
                    "work": work.title if work else "",
                    "segment_id": seg.id, "candidate_id": cand.id,
                    "pv": cand.prompt_version, "model": cand.model,
                    "prev2": (nb["prev2"].text if nb.get("prev2") else ""),
                    "prev1": (nb["prev1"].text if nb.get("prev1") else ""),
                }

                def emit(side: str, text: str, score: float, src: str,
                         weak: bool, suspect: bool = False) -> None:
                    nonlocal n_text_empty
                    if not (text or "").strip():
                        n_text_empty += 1
                        return
                    _require_row_keys(seg.id, cid)   # 会审：主键前置校验，写半截窗口关死
                    row = dict(base)
                    row.update({
                        "id": f"{cid}:{side}:{src}",
                        "text": text, "score": score,
                        "label_source": src, "weak": weak, "suspect": suspect,
                        "side": side,
                        "corruption_type": (cc.corruption_type if cc else ""),
                        "corruption_variable": (cc.variable if cc else ""),
                        "provenance": RM_PROVENANCE[src],
                    })
                    pending.append(row)

                # ② 集霸裁定（强标签，优先于变量标签）
                sc = SCORE_MAP.get(uvs.get(cid, {}).get("winner_resolved"))
                if sc:
                    emit("human", htext, sc[0], "user_verdict", weak=False)
                    emit("candidate", cand.text, sc[1], "user_verdict", weak=False)
                elif cid in uvs:
                    n_unresolved += 1        # 判过但解析不出（'B'/cant_judge）：不编分
                # ① 变量标签（没有裁定才用；裁定过的不再回退到默认方向）
                if cc and sc is None:
                    suspect = _majority_winner(votes.get(cid, [])) == "candidate"
                    emit("human", htext, 1.0, "corruption_variable",
                         weak=False, suspect=suspect)
                    emit("candidate", cand.text, 0.0, "corruption_variable",
                         weak=False, suspect=suspect)
                # ③ 评委多数票（弱标签）
                mj = _majority_winner(votes.get(cid, []))
                msc = SCORE_MAP.get(mj)
                if msc:
                    emit("human", htext, msc[0], "judge_majority", weak=True)
                    emit("candidate", cand.text, msc[1], "judge_majority", weak=True)

    # 军师 P1-6：同段同文本多来源分数冲突的处理规则——按来源优先级保留一条
    # （user_verdict 强标签 > corruption_variable 构造性 > judge_majority 弱标），
    # 同优先级平局按 id **字典序**取最小（id 均为 "前缀-hex" 字符串，定长同前缀，
    # 字典序==数值序；确定性 tie-break，重跑逐字节可复现），其余丢弃并计数。
    # **先定规则再混合**，不静默保留冲突分数。行缺 segment_id 在此响炸：
    # emit 侧已有 _require_row_keys 前置校验，这里是同一不变量的第二道闸。
    rows, n_conflict_dropped = _dedupe_rm_rows(pending)
    with path.open("w", newline="", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    n = len(rows)
    n_weak = sum(1 for r in rows if r.get("weak"))
    by_source = Counter(r["label_source"] for r in rows)
    by_source = {k: by_source.get(k, 0) for k in
                 ("corruption_variable", "user_verdict", "judge_majority")}
    pos = sum(1 for r in rows if r["score"] > 0.5)
    neg = sum(1 for r in rows if r["score"] < 0.5)
    neu = n - pos - neg
    summary = {
        "path": str(path), "summary_path": str(sum_path),
        "n": n, "by_source": by_source,
        "pos": pos, "neg": neg, "neutral": neu,
        "pos_neg_ratio": (round(pos / neg, 3) if neg else None),
        "n_weak": n_weak,
        "n_conflict_dropped": n_conflict_dropped,
        "n_skip_missing": n_skip_missing, "n_unresolved_verdict": n_unresolved,
        "n_text_empty": n_text_empty,
        "n_benchmark_content_excluded": n_bench_content, "n_benchmark_held_out": n_bench, "n_watermark_excluded": n_wm,
        "n_extras_excluded": n_ex, "n_fixture_excluded": n_fix,
        "n_bad_src_excluded": n_bad, "n_src_unverified_excluded": n_src_unv,
        "n_control_excluded": n_ctrl,
    }
    sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return summary


NEGATIVE_PROVENANCE = ("controlled_corruption（§7）——负面模式库：只标'不该这么写'，"
                       "不假定原文即目标")


def export_negatives(ver: str, out_dir: Path | None = None) -> dict:
    """负面模式库 —— 「只学避免哪些写法」的口径（§8 修订版路线 c，gold standard
    未定前唯一可独立推进的训练路线）。

    与 DPO 对（--pairs）的差别：不构造 chosen/rejected 偏好对（默认方向已被集霸
    裁定否掉），只把**单变量劣化版**收成"不该这么写"的负面样例，原文附带作参照。

    收录条件（2026-09-19 起 code 化——此前 v1 文件是临时命令写的、生产者未入库，
    且早于病句重查的最终状态，混入已判病条目 = 变量污染）：
    status=ok + drift_ok（与 DPO 对同款总闸；v2 复核前的老行 fact_consistent
    只是缺省 False 不是判否，不单独设闸）+ 非 ungrammatical（§7.7）+
    非控制臂 NEUTRAL_PARAPHRASE（§7.5 纪律 2）+ _excluded_reason(for_train=True)
    隔离闸全过（§14 基准段/番外/水印/夹具/坏源）。

    summary 同名落盘 `negatives_<ver>_summary.json`：条数 / 类型分布 / 语料分布。
    """
    dest = out_dir or OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"corrupt_negatives_{ver}.jsonl"
    sum_path = dest / f"negatives_{ver}_summary.json"
    starts: dict = {}
    n = n_bench = n_ex = n_wm = n_fix = n_bad = 0
    n_bench_content = 0
    n_src_unv = 0
    n_control = n_ungram = n_missing = 0
    neg_segs: set = set()
    by_type: dict[str, int] = {}
    by_work: dict[str, int] = {}
    with db.session() as s:
        ccs = (s.query(ControlledCorruption)
               .filter(ControlledCorruption.status == "ok")
               .filter(ControlledCorruption.drift_ok == True)  # noqa: E712
               .filter(ControlledCorruption.candidate_id.isnot(None)).all())
        with path.open("w", encoding="utf-8") as f:
            for cc in ccs:
                seg = s.get(Segment, cc.segment_id)
                if seg is None or not cc.text:
                    n_missing += 1
                    continue
                reason = _excluded_reason(s, seg, starts, for_train=True)
                if reason == "benchmark":
                    n_bench += 1
                    continue
                if reason == "extras":
                    n_ex += 1
                    continue
                if reason == "watermark":
                    n_wm += 1
                    continue
                if reason == "fixture":
                    n_fix += 1
                    continue
                if reason == "bad_src":
                    n_bad += 1
                    continue
                if reason == "src_unverified":
                    n_src_unv += 1
                    continue
                if reason == "benchmark_content":    # P1-5：内容级隔离
                    n_bench_content += 1
                    continue
                if cc.corruption_type == "NEUTRAL_PARAPHRASE":
                    n_control += 1
                    continue
                dr = cc.drift or {}
                if isinstance(dr, str):
                    try:
                        dr = json.loads(dr)
                    except Exception:
                        dr = {}
                if dr.get("ungrammatical"):
                    n_ungram += 1
                    continue
                human = seg.text_clean or seg.text
                prevs = (s.query(Segment)
                         .filter(Segment.work_id == seg.work_id,
                                 Segment.seg_version == seg.seg_version,
                                 Segment.ordinal < seg.ordinal)
                         .order_by(Segment.ordinal.desc()).limit(2).all())
                ctx1 = ctx2 = ""
                for pv_seg, slot in ((prevs[0] if len(prevs) > 0 else None, "p1"),
                                     (prevs[1] if len(prevs) > 1 else None, "p2")):
                    if pv_seg is None or looks_watermarked(pv_seg.text or ""):
                        continue    # 上文宁可少给，不给脏的（§7.6 纪律 4）
                    txt = (pv_seg.text_clean or pv_seg.text or "")
                    if slot == "p1":
                        ctx1 = txt
                    else:
                        ctx2 = txt
                f.write(json.dumps({
                    "id": cc.id,
                    "segment_id": cc.segment_id,
                    "failure_text": cc.text,
                    "failure_variable": cc.variable,
                    "failure_mode": cc.corruption_type,
                    "source_human": human,
                    "context_prev1": ctx1,
                    "context_prev2": ctx2,
                    "drift": dr,
                    "provenance": NEGATIVE_PROVENANCE,
                    "pv": cc.prompt_version,
                }, ensure_ascii=False) + "\n")
                n += 1
                neg_segs.add(cc.segment_id)
                by_type[cc.corruption_type] = by_type.get(cc.corruption_type, 0) + 1
                work = s.get(Work, seg.work_id)
                by_work[(work.title if work else "∅")] = by_work.get(work.title if work else "∅", 0) + 1
    summary = {"path": str(path), "summary_path": str(sum_path), "n": n,
               "n_src_unverified_excluded": n_src_unv,
               "n_source_segments": len(neg_segs),
               "by_type": by_type, "by_work": by_work,
               "n_benchmark_content_excluded": n_bench_content, "n_benchmark_held_out": n_bench, "n_extras_excluded": n_ex,
               "n_watermark_excluded": n_wm, "n_fixture_excluded": n_fix,
               "n_bad_src_excluded": n_bad, "n_control_excluded": n_control,
               "n_ungrammatical_excluded": n_ungram, "n_skip_missing": n_missing}
    sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def export_rewrite(ver: str, out_dir: Path | None = None) -> dict:
    """改写训练对 —— 任务 13 补齐的第二种口径。

    `instruction = 语义帧要点`（L 主帧 payload 的 JSON 串，event/intention/
    reader_effect），`output = 人类原文`（text_clean 优先）。这就是 §42 的
    「Semantic → Expression」改写监督信号，**不需要任何人工标签**（质量 L1）。

    「L 级优先」：只用 L 主帧（实测 M 帧覆盖的段全部同时有 L 帧，无遗漏）。
    隔离同 --rm：基准段 / 番外段 / 水印段 / 夹具 / 坏源一律不进（_excluded_reason）。
    summary 同名落盘 `rewrite_<ver>_summary.json`：条数 / 粒度与语料分布。
    """
    dest = out_dir or OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"rewrite_{ver}.jsonl"
    sum_path = dest / f"rewrite_{ver}_summary.json"
    with db.session() as s:
        frames = (s.query(Frame).filter(Frame.granularity == "L", Frame.is_primary == True)  # noqa: E712
                  .filter(Frame.status != "failed").all())
        starts: dict = {}
        n = n_skip_missing = 0
        n_bench = n_wm = n_ex = n_fix = n_bad = 0
        n_bench_content = 0
        n_src_unv = 0
        segs_covered: set = set()
        by_work: dict[str, int] = {}
        granularity_dist: dict[str, int] = {}
        with path.open("w", encoding="utf-8") as f:
            for fr in frames:
                seg = s.get(Segment, fr.segment_id)
                if seg is None or not fr.payload:
                    n_skip_missing += 1
                    continue
                reason = _excluded_reason(s, seg, starts, for_train=True)
                if reason == "benchmark":
                    n_bench += 1
                    continue
                if reason == "watermark":
                    n_wm += 1
                    continue
                if reason == "extras":
                    n_ex += 1
                    continue
                if reason == "fixture":
                    n_fix += 1
                    continue
                if reason == "bad_src":
                    n_bad += 1
                    continue
                if reason == "src_unverified":
                    n_src_unv += 1
                    continue
                if reason:
                    n_skip_missing += 1
                    continue
                text = (seg.text_clean or seg.text or "").strip()
                if not text or len(text) < 20:
                    n_skip_missing += 1
                    continue
                nb = neighbors(s, seg)
                work = s.get(Work, seg.work_id)
                rec = {
                    "id": fr.id,
                    "instruction": json.dumps(fr.payload, ensure_ascii=False,
                                              sort_keys=True),
                    "output": text,
                    "context": {"prev2": (nb["prev2"].text if nb.get("prev2") else ""),
                                "prev1": (nb["prev1"].text if nb.get("prev1") else "")},
                    "frame": fr.payload,
                    "granularity": fr.granularity,
                    "work": work.title if work else "",
                    "segment_id": seg.id, "frame_id": fr.id,
                    "pv": fr.prompt_version,
                    "quality": "L1",             # §44：自动抽取（无人工判定）
                    "provenance": "L 主帧要点 → 人类原文（自动，无需人工判定）",
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                segs_covered.add(seg.id)
                by_work[rec["work"]] = by_work.get(rec["work"], 0) + 1
                granularity_dist[fr.granularity] = granularity_dist.get(fr.granularity, 0) + 1
    summary = {
        "path": str(path), "summary_path": str(sum_path),
        "n": n, "by_work": by_work, "granularity_dist": granularity_dist,
        "n_skip_missing": n_skip_missing,
        "n_segments_covered": len(segs_covered), "n_src_unverified_excluded": n_src_unv,
        "n_benchmark_content_excluded": n_bench_content, "n_benchmark_held_out": n_bench, "n_watermark_excluded": n_wm,
        "n_extras_excluded": n_ex, "n_fixture_excluded": n_fix,
        "n_bad_src_excluded": n_bad,
    }
    sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ver", default="v1")
    ap.add_argument("--pairs", action="store_true", help="另导出受控劣化对照对（DPO 形态）")
    ap.add_argument("--strict", action="store_true",
                    help="--pairs 的严格模式：只导集霸判「人类原文胜」的对（默认按假定方向导）")
    ap.add_argument("--from-frames", action="store_true",
                    help="从所有 L 主帧导出 SFT（frame→人类原文，无需人工标签）")
    ap.add_argument("--negatives", action="store_true",
                    help="负面模式库（corrupt_negatives_<ver>.jsonl；只标'不该这么写'）")
    ap.add_argument("--rm", action="store_true",
                    help="导出奖励模型训练数据（text/score/label_source，三来源标注）")
    ap.add_argument("--rewrite", action="store_true",
                    help="导出改写训练对（instruction=语义帧要点，output=人类原文）")
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
    if args.negatives:
        q = export_negatives(args.ver)
        print(f"负面库导出完成 → {q['path']}")
        print(f"  共 {q['n']} 条（类型分布 {json.dumps(q['by_type'], ensure_ascii=False)}）")
        print(f"  §14 隔离：基准 {q['n_benchmark_held_out']} / 番外 {q['n_extras_excluded']} / "
              f"水印 {q['n_watermark_excluded']} / 夹具 {q['n_fixture_excluded']} / "
              f"坏源 {q['n_bad_src_excluded']}；控制臂剔除 {q['n_control_excluded']}；"
              f"病句剔除 {q['n_ungrammatical_excluded']}")
        print(f"  summary → {q['summary_path']}")
        return
    if args.rm:
        q = export_rm(args.ver)
        print(f"RM 导出完成 → {q['path']}")
        print(f"  共 {q['n']} 行（来源分布 {json.dumps(q['by_source'], ensure_ascii=False)}）")
        print(f"  正 {q['pos']} / 负 {q['neg']} / 中性 {q['neutral']}（正负比 "
              f"{q['pos_neg_ratio']}）；弱标签（评委）{q['n_weak']} 行")
        print(f"  §14 隔离：基准 {q['n_benchmark_held_out']} / 番外 {q['n_extras_excluded']} / "
              f"水印 {q['n_watermark_excluded']} / 夹具 {q['n_fixture_excluded']} / "
              f"坏源 {q['n_bad_src_excluded']}；控制臂剔除 {q['n_control_excluded']}")
        print(f"  跳过：孤儿判定/无段/短文 {q['n_skip_missing']} / 判定解析不出 "
              f"{q['n_unresolved_verdict']} / 空文本 {q['n_text_empty']}")
        print(f"  summary → {q['summary_path']}")
        return
    if args.rewrite:
        q = export_rewrite(args.ver)
        print(f"Rewrite 导出完成 → {q['path']}")
        print(f"  共 {q['n']} 对（粒度分布 {json.dumps(q['granularity_dist'], ensure_ascii=False)}）")
        print(f"  §14 隔离：基准 {q['n_benchmark_held_out']} / 番外 {q['n_extras_excluded']} / "
              f"水印 {q['n_watermark_excluded']} / 夹具 {q['n_fixture_excluded']} / "
              f"坏源 {q['n_bad_src_excluded']} / 无帧或短文 {q['n_skip_missing']}")
        print(f"  语料分布 {json.dumps(q['by_work'], ensure_ascii=False)}")
        print(f"  summary → {q['summary_path']}")
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
