"""可观测报告 CLI（总方案 §50 任务 14）。

    python scripts/observability_report.py --hours 24 --md          # markdown 表到 stdout
    python scripts/observability_report.py --hours 24 --md --out report.md
    python scripts/observability_report.py --hours 168 --exp EXP-XXX --json
    python scripts/observability_report.py                          # 默认人类可读摘要

只读聚合（app/observability.py，纯函数），取数一律走 app.db.session() ORM——
本项目纪律：裸 sqlite3.connect 会静默读错库。窗口内 0 条时显式标 n=0，
绝不把全表数当窗口数。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description="llm_calls 可观测报告（只读聚合）")
    ap.add_argument("--hours", type=int, default=24, help="时间窗（小时，默认 24）")
    ap.add_argument("--exp", default=None, help="只看某实验（llm_calls.experiment_id）")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    ap.add_argument("--md", dest="as_md", action="store_true",
                    help="输出 markdown 表（配 --out 写文件，否则 stdout）")
    ap.add_argument("--out", default=None, help="markdown 写入的文件路径")
    ap.add_argument("--top", type=int, default=10, help="失败原因 Top-N（默认 10）")
    ap.add_argument("--now", default=None,
                    help="统计截止时间（ISO 8601 UTC；默认当前时间）")
    args = ap.parse_args()

    from app import db, observability

    db.init_db()
    try:
        with db.session() as s:
            snap = observability.snapshot(s, hours=args.hours, exp=args.exp,
                                          now=args.now, top_errors=args.top)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        raise SystemExit(1)

    if args.as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    if args.as_md:
        md = render_md(snap)
        if args.out:
            Path(args.out).write_text(md, encoding="utf-8")
            print(f"markdown 已写入 {args.out}", file=sys.stderr)
        else:
            print(md, end="")
    if not args.as_json and not args.as_md:
        print(render_text(snap))


# ── 渲染 ─────────────────────────────────────────────────────

def _fmt_rate(rate: float | None) -> str:
    return f"{rate * 100:.1f}%" if rate is not None else "n=0"


def _fmt_ms(x: float | None) -> str:
    return f"{x:.0f}" if x is not None else "n=0"


def _cell(text: str) -> str:
    """error 是自由文本，含 | 或换行会打碎 markdown 表，渲染时转义。"""
    return (text or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _exp_name(eid: str) -> str:
    return eid


def render_md(snap: dict) -> str:
    """聚合结果 → markdown 表。窗口内 0 条的行/表必须显式 n=0。"""
    hours = snap["hours"]
    win = f"{snap['since']} ~ {snap['now']}"
    scope = f"实验 {snap['exp']}" if snap["exp"] else "全部实验"
    lines: list[str] = []
    lines.append(f"# LLM 可观测报告（近 {hours}h · {scope}）")
    lines.append("")
    lines.append(f"- 窗口：`{win}`（UTC）")
    lines.append(f"- 窗口内调用：**n={snap['n_calls']}**"
                 f"（对照组：全表无时间窗 n={snap['n_full_table']}，勿混用）")
    if snap.get("window_empty"):
        lines.append(f"- ⚠ **{snap['note']}**")
    lines.append(f"- 总览：ok={snap['ok']} failed={snap['failed']} "
                 f"成功率={_fmt_rate(snap['success_rate'])} "
                 f"tokens={snap['tokens_total']}（in {snap['tokens_in']} / "
                 f"out {snap['tokens_out']}） "
                 f"延迟 p50={_fmt_ms(snap['latency_ms']['p50'])}ms "
                 f"p95={_fmt_ms(snap['latency_ms']['p95'])}ms")
    lines.append("")

    lines.extend(_dim_table("按模型", "模型", snap["by_model"]))
    lines.extend(_dim_table("按用途（工作流阶段）", "用途", snap["by_purpose"]))

    lines.append("## 按状态")
    if snap["by_status"]:
        lines.append("| 状态 | n | ok | failed | 成功率 |")
        lines.append("|---|---:|---:|---:|---:|")
        for k, b in snap["by_status"].items():
            lines.append(f"| {k} | {b['n']} | {b['ok']} | {b['failed']} | "
                         f"{_fmt_rate(b['success_rate'])} |")
    else:
        lines.append("（n=0：窗口内无调用）")
    lines.append("")

    lines.append("## 按小时（零桶显式 n=0）")
    if snap["by_hour"]:
        lines.append("| 小时（UTC） | 调用 | 失败 | tokens |")
        lines.append("|---|---:|---:|---:|")
        for h in snap["by_hour"]:
            zero = "（n=0）" if h.get("n_zero") else ""
            lines.append(f"| {h['hour']}{zero} | {h['calls']} | {h['failed']} "
                         f"| {h['tokens']} |")
    else:
        lines.append("（n=0：窗口内无调用）")
    lines.append("")

    et = snap["errors_top"]
    lines.append(f"## 失败原因 Top-{et['top_n']}"
                 f"（失败行 n={et['n_failed_rows']}，去重后 {et['n_distinct_errors']} 种）")
    if et["items"]:
        lines.append("| 失败原因 | n | 用途（前5） | 模型（前5） |")
        lines.append("|---|---:|---|---|")
        for it in et["items"]:
            lines.append(f"| {_cell(it['error'])} | {it['n']} | "
                         f"{_cell(', '.join(it['purposes'])) or '—'} | "
                         f"{_cell(', '.join(it['models'])) or '—'} |")
        if et.get("truncated"):
            lines.append("")
            lines.append(f"（另有 {et['n_distinct_errors'] - et['top_n']} 种原因未列出）")
    else:
        lines.append("（n=0：窗口内无失败）")
    lines.append("")

    ex = snap["experiments"]
    lines.append(f"## 每实验消耗排行（共 {ex['n_experiments']} 个，按 tokens 降序）")
    if ex["top"]:
        lines.append("| 实验 | n | ok | failed | 成功率 | tokens | p50(ms) | p95(ms) |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for b in ex["top"]:
            lines.append(f"| {_exp_name(b['experiment_id'])} | {b['n']} | {b['ok']} "
                         f"| {b['failed']} | {_fmt_rate(b['success_rate'])} "
                         f"| {b['tokens_total']} | {_fmt_ms(b['latency_ms']['p50'])} "
                         f"| {_fmt_ms(b['latency_ms']['p95'])} |")
        if ex.get("truncated"):
            lines.append("")
            lines.append(f"（另有 {ex['n_experiments'] - ex['top_n']} 个实验未列出）")
    else:
        lines.append("（n=0：窗口内无任何实验的调用）")
    lines.append("")
    return "\n".join(lines)


def _dim_table(title: str, col: str, dim: dict) -> list[str]:
    out = [f"## {title}"]
    if not dim:
        out.append("（n=0：窗口内无数据）")
        out.append("")
        return out
    out.append(f"| {col} | n | ok | failed | 成功率 | tokens_in | tokens_out "
               f"| p50(ms) | p95(ms) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, b in dim.items():
        out.append(f"| {k} | {b['n']} | {b['ok']} | {b['failed']} "
                   f"| {_fmt_rate(b['success_rate'])} | {b['tokens_in']} "
                   f"| {b['tokens_out']} | {_fmt_ms(b['latency_ms']['p50'])} "
                   f"| {_fmt_ms(b['latency_ms']['p95'])} |")
    out.append("")
    return out


def render_text(snap: dict) -> str:
    scope = f"实验 {snap['exp']}" if snap["exp"] else "全部"
    bar = (f"近 {snap['hours']}h（{scope}）n={snap['n_calls']}"
           f"（全表 n={snap['n_full_table']}） "
           f"ok={snap['ok']} failed={snap['failed']} "
           f"成功率={_fmt_rate(snap['success_rate'])} "
           f"tokens={snap['tokens_total']} "
           f"p50={_fmt_ms(snap['latency_ms']['p50'])}ms "
           f"p95={_fmt_ms(snap['latency_ms']['p95'])}ms")
    if snap.get("window_empty"):
        bar += f"\n⚠ {snap['note']}"
    top = snap["errors_top"]["items"][:3]
    if top:
        bar += "\n失败原因 Top：" + "；".join(f"{it['error'][:40]}×{it['n']}"
                                             for it in top)
    exps = snap["experiments"]["top"][:5]
    if exps:
        bar += "\n实验消耗 Top：" + "；".join(
            f"{b['experiment_id']} tokens={b['tokens_total']}" for b in exps)
    return bar


if __name__ == "__main__":
    main()
