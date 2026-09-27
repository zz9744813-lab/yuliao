"""策略证据位可信度审计器（只读，2026-09-27 派工）：把「verified 实例里混进
AI 极/无合规声明」变成可复算读数。

背景：`expression_strategies_v2` 8 行全 `status='hypothesis'`，升格的唯一门是
证据面；但真库直读显示 `strategy_instances` 155 条全 `status='verified'`，
其中 3 条 `observed_content` 自述「AI 式」铺陈（描述的是 AI 极写法，却挂在
证据位上）、76 条通篇没有任何「符合/体现该策略」的合规声明（只是原文语义
复述）。「每策略 13~16 条合格证据」此前无法机械复核——本脚本就是复核仪器。

纪律（红线，测试逐条钉住）：
- 真库一律 `sqlite3.connect("file:...?mode=ro", uri=True)`——只读承诺在
  **连接层**成立，不靠自觉；本文件源码不出现任何库写语句（测试用正则扫
  本文件源码断言）；
- 不裁定任何 `status`/`scope`、不给升格建议、不改任何一行数据、不碰
  `expression_strategies_v2.status`；
- 输出确定性：同一库两次运行 JSON 逐字一致（`generated_at` 之外），排序键
  写死（策略按 strategy_key→version→id，实例按 id，均为 SQLite 默认
  BINARY 序）；
- 唯一允许的写 = `--out` 指定的报告文件本身（UTF-8、ensure_ascii=False、
  末尾换行）。

口径（全部在此声明，属**措辞面启发式**，不等于语义裁定）：
- 审计对象 = 各策略名下 `status='verified'` 的实例行（即挂在证据位上的
  那批；非 verified 行只计进 `n_instances`，不进 AI 极/无声明计数）；
- **AI 极措辞表** `AI_POLE_TERMS`（任务书给定的最小表，逐字收录）：
  `AI式`、`AI 式`、`典型的AI`、`AI 倾向`、`AI常`。命中规则：
  `observed_content` 含任一措辞（子串包含，区分大小写）即计入 `n_ai_pole`，
  并逐条列出实例 id（`ai_pole_instance_ids`）；
- **合规声明措辞表** `CONFORMANCE_TERMS`（任务书给定的最小表，逐字收录）：
  符合、体现、属于、该策略、本策略。`observed_content` **不含**其中任何
  一条 → 计入 `n_no_conformance_claim`（NULL/空串按「不含」处理——通篇
  没有声明可言）。两表任务书允许再补，但扩表会改变读数——任何追加必须先
  真库复跑、与主控读数对账后才可入账，故本文件保持最小表不动；
- `n_works`：该策略在采样窗内**全部**实例（含非 verified）的 distinct
  `work_id` 数；
- `--limit <n>`：每策略抽样上限——按实例 id 升序取前 n 行，所有计数都在
  该采样窗内计算（报告顶层如实回显 `limit`，缺省 null=全量）；
- `verdict`：`insufficient` ⟸ `n_ai_pole > 0` 或
  `n_no_conformance_claim >= n_verified / 2`；其余 `reviewable`。
  注意：`n_verified=0` 时右式为 `0 >= 0` 恒真 → 零证据策略也判
  `insufficient`（保守口径：没证据当然不可审）；
- 顶层汇总 = 逐策略计数之和；另附 `db_totals`（对 `strategy_instances`
  全表的直读行数，含不属于任何 v2 策略的孤行）——两者不一致即如实可见，
  不做静默对齐。

用法：
    python scripts/strategy_evidence_audit.py                       # 默认真库，打 stdout
    python scripts/strategy_evidence_audit.py --out r.json          # 同时落盘
    python scripts/strategy_evidence_audit.py --db <path> --limit 20
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

TOOL = "strategy_evidence_audit"
SCHEMA = "strategy_evidence_audit/v1"
TASK = "策略证据位可信度审计（只读）：AI 极混入/合规声明缺失的可复算读数；" \
       "不裁定 status/scope，不改任何一行数据"
DEFAULT_DB = "F:/agi/language-genome/data/language_genome.db"

AI_POLE_TERMS = ("AI式", "AI 式", "典型的AI", "AI 倾向", "AI常")
CONFORMANCE_TERMS = ("符合", "体现", "属于", "该策略", "本策略")
VERDICT_RULE = "insufficient ⟸ n_ai_pole > 0 或 " \
               "n_no_conformance_claim >= n_verified / 2；其余 reviewable"


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """真库一律 mode=ro 只读——只读承诺在连接层成立。"""
    p = Path(db_path).resolve().as_posix()
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _hit(terms, text: str) -> bool:
    return any(t in text for t in terms)


def _audit_strategy(con: sqlite3.Connection, strategy: tuple,
                    limit: int | None) -> dict:
    sid, key, version, status = strategy
    sql = ("SELECT id, work_id, status, observed_content "
           "FROM strategy_instances WHERE strategy_id = ? ORDER BY id")
    args: list = [sid]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    rows = con.execute(sql, args).fetchall()

    n_verified = 0
    ai_ids: list[str] = []
    n_no_claim = 0
    for inst_id, _work, inst_status, content in rows:
        if inst_status != "verified":
            continue
        n_verified += 1
        oc = content or ""
        if _hit(AI_POLE_TERMS, oc):
            ai_ids.append(inst_id)
        if not _hit(CONFORMANCE_TERMS, oc):
            n_no_claim += 1

    cond_ai = len(ai_ids) > 0
    cond_claim = n_no_claim >= n_verified / 2
    verdict = "insufficient" if (cond_ai or cond_claim) else "reviewable"
    return {
        "strategy_id": sid,
        "strategy_key": key,
        "version": version,
        "status": status,
        "n_instances": len(rows),
        "n_verified": n_verified,
        "n_works": len({r[1] for r in rows}),
        "n_ai_pole": len(ai_ids),
        "ai_pole_instance_ids": ai_ids,
        "n_no_conformance_claim": n_no_claim,
        "verdict": verdict,
        "verdict_rule": VERDICT_RULE,
        "verdict_basis": (
            f"n_ai_pole={len(ai_ids)} (>0 ⟹ insufficient)="
            f"{cond_ai}；n_no_conformance_claim={n_no_claim} >= "
            f"n_verified/2={n_verified / 2} ⟹ {cond_claim}；"
            f"n_verified={n_verified}"),
    }


def audit(db_path: Path, limit: int | None = None) -> dict:
    """主读数：逐策略审计 + 顶层汇总。只读，库不可读即抛错（不猜读数）。"""
    con = connect_ro(db_path)
    try:
        strategies = con.execute(
            "SELECT id, strategy_key, version, status "
            "FROM expression_strategies_v2 "
            "ORDER BY strategy_key, version, id").fetchall()
        items = [_audit_strategy(con, st, limit) for st in strategies]
        n_inst_db = con.execute(
            "SELECT COUNT(*) FROM strategy_instances").fetchone()[0]
        n_ver_db = con.execute(
            "SELECT COUNT(*) FROM strategy_instances "
            "WHERE status = 'verified'").fetchone()[0]
        n_packages = con.execute(
            "SELECT COUNT(*) FROM knowledge_packages").fetchone()[0]
    finally:
        con.close()
    return {
        "schema": SCHEMA,
        "tool": TOOL,
        "task": TASK,
        "db_path": Path(db_path).resolve().as_posix(),
        "db_mode": "ro",
        "limit": limit,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "criteria": {
            "ai_pole_terms": list(AI_POLE_TERMS),
            "conformance_terms": list(CONFORMANCE_TERMS),
            "verdict_rule": VERDICT_RULE,
            "scope_note": "n_ai_pole / n_no_conformance_claim 只统计 "
                          "status='verified' 的实例（证据位口径）；"
                          "措辞表为子串包含启发式，不等于语义裁定",
        },
        "strategies": items,
        "n_strategies": len(items),
        "n_instances_total": sum(i["n_instances"] for i in items),
        "n_verified_total": sum(i["n_verified"] for i in items),
        "n_ai_pole_total": sum(i["n_ai_pole"] for i in items),
        "n_no_conformance_claim_total": sum(
            i["n_no_conformance_claim"] for i in items),
        "n_packages": n_packages,
        "db_totals": {
            "strategy_instances_rows": n_inst_db,
            "strategy_instances_verified": n_ver_db,
            "note": "strategy_instances 全表直读（含不属于任何 v2 策略的孤行"
                    "与 limit 采样前的全量）——与顶层逐策略求和口径不同属"
                    "如实差异，不做静默对齐",
        },
        "discipline": {
            "db_mode": "ro", "db_writes": 0, "model_calls": 0,
            "no_status_or_scope_adjudication": True,
            "promotion_advice": "不给（升格与否属策略审查席）",
        },
    }


def _resolve_db(explicit: str) -> Path:
    return Path(explicit or DEFAULT_DB)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="策略证据位可信度审计器（只读）——AI 极混入/合规声明"
                    "缺失的可复算读数")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"库路径（缺省 {DEFAULT_DB}；一律 mode=ro 打开）")
    ap.add_argument("--out", default="",
                    help="报告 JSON 落盘路径（\"\"＝不落盘；UTF-8+末尾换行）")
    ap.add_argument("--limit", type=int, default=None,
                    help="每策略抽样上限（按实例 id 升序取前 n 行）")
    a = ap.parse_args(argv)
    if a.limit is not None and a.limit < 1:
        print(f"[{TOOL}] --limit 须为正整数，收到 {a.limit}", file=sys.stderr)
        return 2
    try:
        report = audit(_resolve_db(a.db), a.limit)
    except sqlite3.Error as exc:            # 库不可读 = 无读数，不猜
        print(f"[{TOOL}] 只读库打开失败：{exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=1)
    try:                                    # Windows 控制台中文兜底
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    print(text)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes((text + "\n").encode("utf-8"))
        print(f"[{TOOL}] 报告已写 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
