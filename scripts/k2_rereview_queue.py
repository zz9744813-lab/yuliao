"""K2 复审队列驱动器（审计 P1 第 3 条收口件，2026-09-26）。

背景：审计 P1 第 3 条——K2 模型没收到策略定义，「已得 82 条需复审」。
机制侧已修（`app/knowledge_extract.py` 的 `extract_segment(..., strategy_def=...)`
+ `REVIEW_MARKER_NEW_DEF="k2def-v1"` 复用 `strategy_instances.reviewer_version`
列；`scripts/k2_extract_backfill.py` 已传 strategy_def）。**本驱动补缺的
最后一环**：把「reviewer_version 为空/None 的既有实例」枚举成**复审队列**。

默认档（只读，零调用零库写）：
- 真库 `mode=ro` 只读，枚举 `strategy_instances.reviewer_version` 为
  空/None 的行（=未按新口径复审的 legacy 批，审计所说「需复审」）；
- 逐条给 `strategy_id / strategy_key / strategy_version / segment_id /
  work_id / text_version / 现有 status / reviewer_version`，按策略聚合
  「待复审条数」；
- 逐条校验能否拿到**非空**策略定义正文（abstract_operation /
  invariants / effect_hypothesis / failure_modes 四字段，口径与
  k2_extract_backfill.py 的 strategy_def 构造逐字同源）——拿不到的**单列
  missing_strategy_def 桶**并 fail-closed 计入报告（不许静默跳过；
  桶是队列的分区，队列总数口径不变）；
- 缺库/缺表 → 非零退出（fail-closed）。

live 档（本任务**不许真跑**——无额度拍板）：
- 双闸：`--live` + 环境变量 `K2_REREVIEW_ALLOW_LIVE=1`；缺环境变量 →
  SystemExit 且**未发起任何调用**；
- **必须 `--limit`**：无上限不许起跑；发起任何调用前先打印逐条计划；
- 执行：每条 1 次 `KE.extract_segment(..., strategy_def=...)`（复审＝按
  带定义的新口径重抽该段并核对），**只写标记位 reviewer_version**
  （k2def-v1）——不改 status / span / scope 任何一行；新结果与既有行的
  分歧（status/span 不一致）**只留痕进报告**，绝不自动改原行；
- 预算闸复用既有常量：`KE.ExtractBudget()`（max_calls=20 /
  max_tokens=50_000，与 k2_extract_backfill.py 默认同值，不另写口径）。

用法：
    python scripts/k2_rereview_queue.py                  # 默认只读档：全队列
    python scripts/k2_rereview_queue.py --out report.json
    K2_REREVIEW_ALLOW_LIVE=1 python scripts/k2_rereview_queue.py --live \
        --extractor-model <m> --limit 5                  # 拍板后才许（本任务不跑）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# 单源 import（不本地重定义——k2_extract_backfill 同款纪律）：
# REVIEW_MARKER_NEW_DEF / extract_segment / ExtractBudget / 输出契约常量
from app import knowledge_extract as KE                    # noqa: E402
from app.config import LLM_MODE                            # noqa: E402

REVIEW_MARKER = KE.REVIEW_MARKER_NEW_DEF       # "k2def-v1"
DEF_FIELDS = ("abstract_operation", "invariants", "effect_hypothesis",
              "failure_modes")   # 与 k2_extract_backfill.py 的 strategy_def
                                   # 构造逐字段同源（scripts/…:574-579）


def _ro_connect(db_path: Path):
    """真库一律 mode=ro 只读（默认档的零写库承诺在连接层成立）。"""
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _parse_json_list(raw) -> list | None:
    """invariants/failure_modes 列存 JSON 文本——解析失败按缺定义处理
    （fail-closed，不许把解析失败静默当空表）。"""
    if raw is None:
        return None
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return v if isinstance(v, list) else None


def _def_gap(card: dict | None) -> str | None:
    """策略定义正文非空校验：四字段全非空 → None（可复审）；否则给
    缺口原因（fail-closed：缺定义的条目不进可复审集，但留在队列计数里）。"""
    if card is None:
        return "strategy_row_missing"
    if not (card["abstract_operation"] or "").strip():
        return "abstract_operation_empty"
    inv = _parse_json_list(card["invariants"])
    if not inv:
        return "invariants_missing_or_unparseable"
    if not (card["effect_hypothesis"] or "").strip():
        return "effect_hypothesis_empty"
    fm = _parse_json_list(card["failure_modes"])
    if not fm:
        return "failure_modes_missing_or_unparseable"
    return None


def enumerate_queue(db_path: Path) -> dict:
    """默认只读档：mode=ro 枚举 reviewer_version 空/None 的实例 →
    复审队列 + 逐策略聚合 + missing_strategy_def 桶。
    缺库/缺表 → SystemExit(2)（非零，fail-closed）。"""
    if not db_path.exists():
        raise SystemExit(f"[k2_rereview_queue] 真库不存在：{db_path}"
                         "——缺库非零退出（fail-closed，不许静默空报告）")
    con = _ro_connect(db_path)
    try:
        try:
            rows = con.execute(
                "SELECT id, strategy_id, strategy_version, work_id, "
                "segment_id, text_version, status, "
                "coalesce(reviewer_version, '') FROM strategy_instances "
                "WHERE reviewer_version IS NULL OR reviewer_version = '' "
                "ORDER BY strategy_id, segment_id, id").fetchall()
            n_total = con.execute(
                "SELECT COUNT(*) FROM strategy_instances").fetchone()[0]
            n_reviewed = con.execute(
                "SELECT COUNT(*) FROM strategy_instances "
                "WHERE reviewer_version = ?",
                (REVIEW_MARKER,)).fetchone()[0]
            cards = {sid: {"strategy_key": key, "version": ver,
                           "abstract_operation": ao, "invariants": inv,
                           "effect_hypothesis": eff, "failure_modes": fm}
                     for sid, key, ver, ao, inv, eff, fm in con.execute(
                         "SELECT id, strategy_key, version, "
                         "abstract_operation, invariants, "
                         "effect_hypothesis, failure_modes "
                         "FROM expression_strategies_v2").fetchall()}
        except sqlite3.Error as exc:
            raise SystemExit(
                f"[k2_rereview_queue] 真库只读查询失败："
                f"{type(exc).__name__}: {exc}——缺表/损坏按缺库处理"
                "（非零退出，不许静默）") from exc
    finally:
        con.close()
    queue, missing = [], []
    for (iid, sid, sver, wid, segid, tv, status, rv) in rows:
        card = cards.get(sid)
        item = {"instance_id": iid, "strategy_id": sid,
                "strategy_key": card["strategy_key"] if card else None,
                "strategy_version": sver, "segment_id": segid,
                "work_id": wid, "text_version": tv, "status": status,
                "reviewer_version": rv}
        gap = _def_gap(card)
        if gap:
            item["missing_reason"] = gap
            missing.append(item)     # 单列桶：fail-closed 计入，不静默跳过
        else:
            queue.append(item)
    # 按策略聚合「待复审条数」——missing 是队列分区，不改总数口径：
    # n_queue = 可复审(n_ready) + 缺定义(n_missing_strategy_def)
    agg: dict[tuple, dict] = {}
    for item in queue + missing:
        k = (item["strategy_id"], item["strategy_key"])
        a = agg.setdefault(k, {"strategy_id": item["strategy_id"],
                               "strategy_key": item["strategy_key"],
                               "pending": 0, "missing_def": 0})
        a["pending"] += 1
        if "missing_reason" in item:
            a["missing_def"] += 1
    by_strategy = sorted(agg.values(),
                         key=lambda x: (-x["pending"],
                                        str(x["strategy_key"])))
    return {
        "mode": "read_only", "read_mode": "ro",
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "review_marker": REVIEW_MARKER,
        "ledger": {
            "n_total_instances": n_total,
            "n_reviewed_new_def": n_reviewed,      # 已按新口径复审（k2def-v1）
            "n_queue": len(queue) + len(missing),  # 待复审（空/None reviewer_version）
            "n_ready": len(queue),                 # 定义正文齐备，可复审
            "n_missing_strategy_def": len(missing),  # 缺定义桶（fail-closed）
        },
        "by_strategy": by_strategy,
        "queue": queue,
        "missing_strategy_def": missing,
        "note": "复审队列=reviewer_version 为空/None 的既有实例（legacy 批）；"
                "missing_strategy_def 是队列的分区（缺定义正文，fail-closed "
                "计入、不许静默跳过），不改队列总数口径。",
    }


def build_strategy_def(card_row) -> dict:
    """复审用定义正文构造——与 k2_extract_backfill.py:574-579 逐字同源
    （不许另写口径）。card_row 为 expression_strategies_v2 的 ORM 行。"""
    return {"abstract_operation": card_row.abstract_operation,
            "invariants": list(card_row.invariants or []),
            "effect_hypothesis": card_row.effect_hypothesis,
            "failure_modes": list(card_row.failure_modes or [])}


def _execute_rereview(plan: list[dict], extractor_model: str) -> dict:
    """live 执行（本任务不跑）：逐条 1 次 extract_segment（带定义的新
    口径）→ 只写 reviewer_version 标记；新结果与既有行分歧留痕不自动改。
    预算闸复用 KE.ExtractBudget() 既有常量（max_calls=20 / 50_000）。"""
    from app import db
    from app.models import ExpressionStrategyV2, Segment, StrategyInstance
    budget = KE.ExtractBudget()

    class _Adapter:
        """invoke 契约 → app.gateway.chat（与 k2_extract_backfill 同款：
        A05 逐次记账/A06 完成原因门自动生效）。"""
        def invoke(self, *, role, system, payload, max_tokens, timeout):
            from app import gateway
            r = gateway.chat(model=extractor_model, system=system,
                             user=json.dumps(payload, ensure_ascii=False),
                             purpose="k2_rereview_queue",
                             max_tokens=max_tokens)
            return {"text": r.text, "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out, "actual_model": extractor_model}

    client = _Adapter()
    results = []
    with db.session() as s:
        for item in plan:
            st = s.get(ExpressionStrategyV2, item["strategy_id"])
            seg = s.get(Segment, item["segment_id"])
            text = (seg.text_clean or seg.text or "") if seg is not None else ""
            r = KE.extract_segment(
                client, strategy_id=item["strategy_id"],
                strategy_version=item["strategy_version"],
                work_id=item["work_id"], segment_id=item["segment_id"],
                text=text, text_version=item["text_version"],
                budget=budget, live=True,
                strategy_def=build_strategy_def(st) if st is not None else None)
            inst = s.query(StrategyInstance).filter_by(
                id=item["instance_id"]).one()
            # 复审只写标记位——不改 status / span 任何一行（审计明令）：
            inst.reviewer_version = REVIEW_MARKER
            results.append({
                "instance_id": item["instance_id"],
                "strategy_key": item["strategy_key"],
                "old_status": item["status"],
                "new_status": r.get("status"),
                "span_agree": (r.get("span_start") == inst.span_start
                               and r.get("span_end") == inst.span_end),
                "reviewer_version": REVIEW_MARKER})
        s.commit()
    return {"mode": "live", "n_rereviewed": len(results),
            "budget": {"calls": budget.calls, "tokens": budget.tokens,
                       "max_calls": budget.max_calls,
                       "max_tokens": budget.max_tokens},
            "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="K2 复审队列驱动器（审计 P1"
                                             " 第 3 条：83 条 legacy 实例"
                                             "按新口径（带策略定义）复审）")
    ap.add_argument("--repo-root", default=str(ROOT), dest="repo_root",
                    help="仓库根（缺省=脚本自身推导的仓库根）")
    ap.add_argument("--db", default="",
                    help="真库路径（缺省 <repo>/data/language_genome.db）")
    ap.add_argument("--out", default="", help="报告 JSON 落盘（可选）")
    ap.add_argument("--limit", type=int, default=None,
                    help="live 档必填：本轮复审条数上限；默认只读档忽略"
                         "（全队列枚举）")
    ap.add_argument("--live", action="store_true",
                    help="真跑复审（拍板后）：需 K2_REREVIEW_ALLOW_LIVE=1"
                         " + --limit；本任务禁跑")
    ap.add_argument("--extractor-model", default="", dest="extractor_model")
    a = ap.parse_args(argv)
    db_path = Path(a.db) if a.db else (Path(a.repo_root) / "data"
                                       / "language_genome.db")
    if not a.live:
        report = enumerate_queue(db_path)
        text = json.dumps(report, ensure_ascii=False, indent=1)
        print(text)
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(text, encoding="utf-8")
            print(f"[k2_rereview_queue] 报告已写 {a.out}")
        return 0
    # ── live 双闸（顺序钉：任何可能发起调用的构造之前）──
    if os.environ.get("K2_REREVIEW_ALLOW_LIVE") != "1":
        raise SystemExit("--live 需要环境变量 K2_REREVIEW_ALLOW_LIVE=1"
                         "（双闸：未拍板防误跑烧钱）——未发起任何调用")
    if a.limit is None:
        raise SystemExit("--live 必须显式 --limit（逐条计划先行，"
                         "无上限不许起跑）——未发起任何调用")
    if not a.extractor_model:
        raise SystemExit("--live 需要 --extractor-model——未发起任何调用")
    if LLM_MODE != "real":
        raise SystemExit(f"--live 需要 LG_LLM_MODE=real（当前 {LLM_MODE}）"
                         "——未发起任何调用")
    from app.live_guard import live_lock
    with live_lock("k2_rereview_queue"):
        from preflight_models import require_models
        require_models((a.extractor_model,), source="k2_rereview_queue")
        report = enumerate_queue(db_path)
        plan = report["queue"][:a.limit]      # 只有定义齐备的条目可复审
        # 发起任何调用前打印逐条计划（任务书口径）：
        print(json.dumps({"live": True, "extractor_model": a.extractor_model,
                          "limit": a.limit, "n_eligible": len(plan),
                          "n_blocked_missing_def":
                              report["ledger"]["n_missing_strategy_def"],
                          "plan": plan}, ensure_ascii=False, indent=1))
        result = _execute_rereview(plan, a.extractor_model)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0


if __name__ == "__main__":
    sys.exit(main())
