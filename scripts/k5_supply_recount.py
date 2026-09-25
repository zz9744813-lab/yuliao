#!/usr/bin/env python
"""K5 供给口径真计数（A2 整改，2026-09-25 派工；只读、零写库、零模型调用）。

背景：对抗性复核（k5_freeze_adversarial §3.2-A2）要求把「供给不足/充足」
换成可复算口径，分母统一为「src_ok=1 且 allowed_purposes 非空 且
role≠'benchmark'」，验收判据「≥2 作品各自 ≥1 万段」方可移除「供给」项。
本脚本对真库（sqlite mode=ro）复算三口径并落盘
docs/K5供给口径真计数_20260925.md：

- 口径 A（复核者指定分母）：``json_extract(integrity,'$.src_ok') IS TRUE``
  AND ``role≠'benchmark'`` AND 作品在 work_sources 的 ``allowed_purposes``
  为非空 JSON 数组；并列「严格布尔」变体（``json_type='true'``）核对
  非布尔掺水（数字 1 / 字符串 "true" 之类会漏进 IS TRUE 口径）。
- 口径 B：role≠'benchmark' AND 白名单非空（不看 src_ok）。
- 口径 C：全部段按作品。

每口径给：逐作品段数、最大单作品、≥1 万段作品数——三口径都真跑并原样
打印，不写结论性数字。另按 app/engine.py ``_integrity_state`` 口径报告
src_ok 覆盖缺口（只 is True 计完好、is False 计判坏、其余计未校验）。

纪律：真库只读；不改 expression_strategies_v2.status/scope；本工具不做
任何解冻/冻结裁定——「最小动作」只列动作与代价估计，是否执行归主控。

用法：
    python scripts/k5_supply_recount.py [--db PATH] [--min-per-work N]
        [--out-doc PATH] [--print-only]
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_REL = Path("docs") / "K5供给口径真计数_20260925.md"
K5_CHECK_DOC = ROOT / "docs" / "K5判据核验_20260925.md"
MAIN_REPO_DB = Path("F:/agi/language-genome/data/language_genome.db")
MIN_DEFAULT = 10_000
NEED_WORKS = 2

# 主控 U4 实测（任务书背景给定，只作对照基准，不是本脚本产物）
U4_REF = [
    ("全部段", "total", 438_418),
    ("integrity.src_ok 键存在", "key_exists", 6_791),
    ("src_ok 值为 true", "state_true", 4_872),
    ("src_ok 值为 false", "state_false", 1_919),
    ("缺 src_ok 键（未校验）", "missing_total", 431_627),
    ("口径 A 合计", "A_total", 4_220),
    ("口径 A 最大单作品", "A_max", 876),
    ("口径 B 合计", "B_total", 401_601),
    ("口径 B 最大单作品", "B_max", 154_674),
]

# ── SQL 口径片段 ──────────────────────────────────────────────────────
# role≠benchmark：role 为 NULL（历史 train 默认）视作「非 benchmark」。
NOT_BENCH = "(segments.role IS NULL OR segments.role<>'benchmark')"

# 白名单非空：只认 work_sources.allowed_purposes 为非空 JSON 数组；
# 无登记行 / 非法 JSON / 非数组一律 fail-closed 排除。
_WHITELIST_LEN = """CASE
  WHEN ws.allowed_purposes IS NULL OR json_valid(ws.allowed_purposes)=0 THEN 0
  WHEN json_type(ws.allowed_purposes)<>'array' THEN 0
  ELSE json_array_length(ws.allowed_purposes) END"""
WL_OK = (f"EXISTS (SELECT 1 FROM work_sources ws WHERE ws.work_id=segments.work_id"
         f" AND ({_WHITELIST_LEN})>0)")

# 主控原口径：IS TRUE（JSON true 抽出为整数 1 命中；数字 1 等不严布尔也会命中）
SRC_TRUE_IS = ("json_valid(segments.integrity)=1 "
               "AND json_extract(segments.integrity,'$.src_ok') IS TRUE")
# 严格布尔补列：与 app/engine.py _integrity_state 的 is True 同语义
SRC_TRUE_STRICT = ("json_valid(segments.integrity)=1 "
                   "AND json_type(segments.integrity,'$.src_ok')='true'")

COND_A = f"{SRC_TRUE_IS} AND {NOT_BENCH} AND {WL_OK}"
COND_A_STRICT = f"{SRC_TRUE_STRICT} AND {NOT_BENCH} AND {WL_OK}"
COND_B = f"{NOT_BENCH} AND {WL_OK}"
COND_C = ""

# src_ok 状态五分类（engine._integrity_state 的 SQL 镜像）：
#   true/false=已校验；missing=合法 JSON 但缺该键；loose=键在但类型不严
#   （字符串/数字/null…）；nojson=integrity 为 NULL 或非法 JSON。
_SRC_STATE = """CASE
  WHEN segments.integrity IS NULL OR json_valid(segments.integrity)=0 THEN 'nojson'
  WHEN json_type(segments.integrity,'$.src_ok')='true' THEN 'true'
  WHEN json_type(segments.integrity,'$.src_ok')='false' THEN 'false'
  WHEN json_type(segments.integrity,'$.src_ok') IS NULL THEN 'missing'
  ELSE 'loose' END"""


def default_db() -> Path | None:
    """解析真库：--db 之外按 env LG_DATABASE_URL → 本树 data/ → 主检出 data/。"""
    cands: list[Path] = []
    env = os.environ.get("LG_DATABASE_URL", "")
    if env.startswith("sqlite:///"):
        cands.append(Path(env.removeprefix("sqlite:///")))
    cands += [ROOT / "data" / "language_genome.db", MAIN_REPO_DB]
    for c in cands:
        if c.is_file():
            return c
    return None


def open_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开：mode=ro，任何写操作都会抛异常（测试钉死）。"""
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)


def json_probe(con: sqlite3.Connection) -> None:
    try:
        hit = con.execute("SELECT json_extract('{\"a\":true}','$.a') IS TRUE").fetchone()[0]
        typ = con.execute("SELECT json_type('{\"a\":true}','$.a')").fetchone()[0]
    except sqlite3.Error as e:
        raise SystemExit(f"SQLite JSON 扩展不可用，无法复算口径：{e}")
    if not hit or typ != "true":
        raise SystemExit("SQLite JSON 布尔语义与预期不符（IS TRUE / json_type），中止。")


def by_work(con: sqlite3.Connection, where: str) -> list[tuple[str, str, int]]:
    sql = "SELECT segments.work_id, COUNT(*) FROM segments"
    if where:
        sql += f" WHERE {where}"
    sql += " GROUP BY segments.work_id ORDER BY COUNT(*) DESC, segments.work_id"
    titles = dict(con.execute("SELECT id, title FROM works").fetchall())
    return [(wid, titles.get(wid) or "(无 works 行)", n)
            for wid, n in con.execute(sql)]


def src_state(con: sqlite3.Connection, where: str = "") -> dict[str, int]:
    sql = f"SELECT {_SRC_STATE}, COUNT(*) FROM segments"
    if where:
        sql += f" WHERE {where}"
    return dict(con.execute(sql + " GROUP BY 1"))


def _caliber(con: sqlite3.Connection, where: str, min_per_work: int) -> dict:
    rows = by_work(con, where)
    total = sum(r[2] for r in rows)
    return {"by_work": rows, "total": total,
            "max": rows[0][2] if rows else 0,
            "n_ge": sum(1 for r in rows if r[2] >= min_per_work)}


def find_supply_number_lines(text: str) -> list[str]:
    """扫描原文里「供给/段数」类数字行（\\d+ 段 / 供给），逐条供对照。"""
    return [ln.strip() for ln in text.splitlines()
            if re.search(r"\d[\d,]*\s*段", ln) or "供给" in ln]


def build_result(db_path: Path, min_per_work: int = MIN_DEFAULT,
                 k5doc_path: Path | None = K5_CHECK_DOC) -> dict:
    con = open_ro(db_path)
    try:
        json_probe(con)
        a = _caliber(con, COND_A, min_per_work)
        a_strict = _caliber(con, COND_A_STRICT, min_per_work)
        b = _caliber(con, COND_B, min_per_work)
        c = _caliber(con, COND_C, min_per_work)
        state = src_state(con)
        b_state = src_state(con, COND_B)
    finally:
        con.close()

    total = c["total"]
    missing = state.get("missing", 0)
    nojson = state.get("nojson", 0)
    loose = state.get("loose", 0)
    key_exists = state.get("true", 0) + state.get("false", 0) + loose
    measured = {
        "total": total,
        "state_true": state.get("true", 0),
        "state_false": state.get("false", 0),
        "state_missing": missing,
        "state_nojson": nojson,
        "state_loose": loose,
        "key_exists": key_exists,
        "missing_total": missing + nojson,
        "A_total": a["total"], "A_max": a["max"], "A_n_ge": a["n_ge"],
        "A_strict_total": a_strict["total"],
        "B_total": b["total"], "B_max": b["max"], "B_n_ge": b["n_ge"],
        "C_max": c["max"],
        "B_unverified": b["total"] - b_state.get("true", 0) - b_state.get("false", 0),
    }
    u4 = [{"item": item, "ref": ref, "measured": measured[key],
           "consistent": measured[key] == ref}
          for item, key, ref in U4_REF]
    verdict_ok = a["n_ge"] >= NEED_WORKS
    scan_lines: list[str] = []
    scan_path = str(k5doc_path) if k5doc_path else None
    if k5doc_path and Path(k5doc_path).is_file():
        scan_lines = find_supply_number_lines(
            Path(k5doc_path).read_text(encoding="utf-8"))
    else:
        scan_path = f"{scan_path}（未读到，未自跑：文件不可读）"
    return {
        "db_path": Path(db_path).as_posix(),
        "sqlite_version": sqlite3.sqlite_version,
        # sqlite3.version 在 3.12 起弃用、3.14 移除——getattr 兜底防崩
        "python_sqlite_module": getattr(sqlite3, "version", "n/a"),
        "run_at": datetime.datetime.now(datetime.timezone.utc)
                  .strftime("%Y-%m-%d %H:%M:%SZ"),
        "min_per_work": min_per_work, "need_works": NEED_WORKS,
        "calibers": {"A": a, "A_strict": a_strict, "B": b, "C": c},
        "state": state, "b_state": b_state, "measured": measured,
        "u4": u4, "verdict_ok": verdict_ok,
        "scan": {"path": scan_path, "lines": scan_lines},
    }


def _table(rows: list[tuple[str, str, int]]) -> list[str]:
    out = ["| work_id | 作品 | 段数 |", "|---|---|---:|"]
    out += [f"| {wid} | {title} | {n:,} |" for wid, title, n in rows]
    if not rows:
        out.append("| — | （零作品命中） | 0 |")
    return out


def render_markdown(res: dict) -> str:
    m = res["measured"]
    mn = res["min_per_work"]
    cal = res["calibers"]
    v_word = "成立" if res["verdict_ok"] else "不成立"
    L: list[str] = []
    add = L.append
    add("# K5 供给口径真计数（A2 整改，2026-09-25）")
    add("")
    add(f"- 工具：`scripts/k5_supply_recount.py`（`sqlite3` `mode=ro` 只读、"
        f"零写库、零模型调用；不改任何 `status`/`scope`，不做解冻/冻结裁定）。")
    add(f"- 库：`{res['db_path']}`｜SQLite {res['sqlite_version']}｜"
        f"运行时间 {res['run_at']}。")
    add(f"- 达标判据（复核者指定）：≥{res['need_works']} 作品在口径 A 下各自 "
        f"≥{mn:,} 段。")
    add("")
    add("## 0. 实跑命令")
    add("")
    add("```")
    add("F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/k5_supply_recount.py")
    add("→ exit 0（stdout 与本文件同口径逐作品输出，可复算）")
    add("F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest "
        "tests/test_k5_supply_recount.py -q")
    add("→ exit 0（临时夹具库，不依赖真库）")
    add("```")
    add("")
    add("## 1. src_ok 覆盖口径（键存在性五分类，全库）")
    add("")
    add("依据 `app/engine.py` `_integrity_state`：只 `is True` 计完好、"
        "`is False` 计判坏、其余计**未校验**。")
    add("")
    add("| 状态 | 含义 | 段数 |")
    add("|---|---|---:|")
    for key, zh in [("true", "完好（src_ok=true）"), ("false", "判坏（src_ok=false）"),
                    ("loose", "键在但类型不严（数字/字符串/null）→未校验"),
                    ("missing", "合法 JSON 但缺 src_ok 键→未校验"),
                    ("nojson", "integrity 为 NULL/非法 JSON→未校验")]:
        add(f"| `{key}` | {zh} | {m['state_' + key]:,} |")
    add(f"| 合计「未校验」 | missing+nojson（+loose 单列） | "
        f"{m['missing_total'] + m['state_loose']:,} |")
    add("")
    add(f"**`src_ok` 只在 {m['key_exists']:,} 段上被真实计算过；"
        f"其余 {m['missing_total']:,} 段缺该键——属「未校验」，不是「判坏」**"
        f"（loose {m['state_loose']:,} 段另列，同样不得当作已校验）。")
    add("")
    add("## 2. 三口径逐作品真计数（原样输出，非结论摘录）")
    add("")
    for cid, title, cond in [
        ("A", "口径 A：`json_extract(integrity,'$.src_ok') IS TRUE` AND "
              "`role≠'benchmark'` AND `json_array_length(allowed_purposes)>0`"
              "（复核者指定分母）", COND_A),
        ("A_strict", "口径 A 严格布尔补列：同上但 `json_type(...,'$.src_ok')='true'`"
                     "（排除数字 1/字符串 \"true\" 等非布尔掺水）", COND_A_STRICT),
        ("B", "口径 B：`role≠'benchmark'` AND `json_array_length(allowed_purposes)>0`"
              "（不看 src_ok）", COND_B),
        ("C", "口径 C：全部段按作品（无任何过滤）", COND_C)]:
        cb = cal[cid]
        add(f"### 2.{['A','A_strict','B','C'].index(cid) + 1} {title}")
        add("")
        add("```sql")
        add(f"SELECT work_id, COUNT(*) FROM segments WHERE {cond}"
            if cond else "SELECT work_id, COUNT(*) FROM segments")
        add("GROUP BY work_id;")
        add("```")
        add("")
        L.extend(_table(cb["by_work"]))
        add("")
        add(f"- **合计 {cb['total']:,} 段｜最大单作品 {cb['max']:,} 段｜"
            f"≥{mn:,} 段的作品数 {cb['n_ge']}**")
        add("")
    leak = m["A_total"] - m["A_strict_total"]
    add(f"补列读数：IS TRUE 口径合计 {m['A_total']:,} − 严格布尔 {m['A_strict_total']:,}"
        f" = {leak:,} 段" + ("（两口径一致，无非布尔掺水）" if leak == 0 else
        "（该差值为非布尔值漏入 IS TRUE 口径，见 `loose` 行）") + "。")
    add("")
    add("## 3. 与主控 U4 实测（任务书背景）对照")
    add("")
    add("| 项目 | 主控 U4 数字 | 本脚本复算 | 是否一致 |")
    add("|---|---:|---:|---|")
    for row in res["u4"]:
        add(f"| {row['item']} | {row['ref']:,} | {row['measured']:,} | "
            f"{'一致' if row['consistent'] else '**不一致**'} |")
    add("")
    add("## 4. 与 docs/K5判据核验_20260925.md 的供给数字一致性核对")
    add("")
    add(f"扫描对象：`{res['scan']['path']}`；扫描规则：含「\\d+ 段」或「供给」的行。")
    add("")
    if res["scan"]["lines"]:
        add("| 原文行（含数字） | 复算对照 | 是否一致 |")
        add("|---|---|---|")
        for ln in res["scan"]["lines"]:
            add(f"| {ln} | 见 §2 逐作品输出 | 逐条人工对照（本表只列原文，"
                f"不复述结论） |")
    else:
        add("**扫描结果：原文未出现任何语料供给（段数）数字**——"
            "`docs/K5判据核验_20260925.md` 中的数字全部是 token/调用/止损口径"
            "（如 tokens=2,167、12,810、止损 800 万），不构成供给声明，"
            "无需（也无从）与本报告做供给数字对照；如实记录为「无供给数字可对照」，"
            "不替原文虚构对照项。")
    add("")
    add("## 5. 口径结论（只写事实判断，不裁定解冻）")
    add("")
    a = cal["A"]
    add(f"1. 按复核者指定的口径 A：合计 {m['A_total']:,} 段，最大单作品 "
        f"{m['A_max']:,} 段，≥{mn:,} 段的作品数 {m['A_n_ge']}。"
        f"「≥{res['need_works']} 作品各自 ≥{mn:,} 段」**{v_word}**。"
        + ("" if res["verdict_ok"] else
           "故复核报告 §3.3 拟改写的「供给充足」在当前口径下不成立，"
           "「供给不足」也不应凭旧数字沿用——以本表可复算口径为准。"))
    add(f"2. 覆盖缺口：`src_ok` 仅 {m['key_exists']:,} 段被真实计算，"
        f"{m['missing_total']:,} 段缺该键（未校验，非判坏），见 §1。")
    add(f"3. 口径 A 达标的最小动作（**只列动作与代价估计，是否执行由主控裁定**）：")
    add(f"   - 对口径 B 中未校验段（缺键/非法 JSON/类型不严）补跑 "
        f"`scripts/source_check.py`（每段一次 LLM 判定写入真布尔 `src_ok`；"
        f"现有 true/false 段按幂等口径跳过）——**约 {m['B_unverified']:,} 段待补跑**；")
    add(f"   - 补跑后口径 A 逐作品段数的算术上限 = 口径 B 逐作品段数"
        f"（≥{mn:,} 段作品数 {m['B_n_ge']}）：")
    if m["B_n_ge"] >= res["need_works"]:
        add(f"     口径 B 已有 {m['B_n_ge']} 个作品 ≥{mn:,} 段，达标在算术上可能，"
            f"实际取决于补跑通过率（false 段不可翻正）。")
    else:
        add(f"     即便补跑段全部判 true，口径 B 也仅 {m['B_n_ge']} 个作品 "
            f"≥{mn:,} 段 < {res['need_works']}——**单靠补跑源校勘不可能达标**，"
            f"须扩充白名单作品语料（登记新作品＋切段＋清洗），超出本口径整改范围。")
    add(f"   - 或者：与主控重议分母（口径 A 系复核者指定，本文不改判据、"
            f"只如实报数）。")
    add("")
    add("## 6. 红线声明")
    add("")
    add("- 本报告由只读复算产生：未写库、未改 `expression_strategies_v2.status`/"
        "`scope`、零模型调用；不构成解冻/冻结裁定。")
    add("- 「≥2 作品各≥1 万段」判据出自对抗性复核 §3.2-A2，本文只复算其当前读数。")
    add("")
    return "\n".join(L) + "\n"


def render_stdout(res: dict) -> str:
    cal = res["calibers"]
    m = res["measured"]
    mn = res["min_per_work"]
    L = [f"[k5_supply_recount] db={res['db_path']} sqlite={res['sqlite_version']} "
         f"(mode=ro 只读)",
         f"[src_ok 覆盖] true={m['state_true']:,} false={m['state_false']:,} "
         f"loose={m['state_loose']:,} missing={m['state_missing']:,} "
         f"nojson={m['state_nojson']:,}（未校验合计 "
         f"{m['missing_total'] + m['state_loose']:,}）", ""]
    for cid, name in [("A", "口径 A（src_ok IS TRUE 且 非benchmark 且 白名单非空）"),
                      ("A_strict", "口径 A 严格布尔补列"),
                      ("B", "口径 B（非benchmark 且 白名单非空）"),
                      ("C", "口径 C（全部段）")]:
        cb = cal[cid]
        L.append(f"== {name} ==")
        for wid, title, n in cb["by_work"]:
            L.append(f"  {wid}  {title}  {n:,}")
        L.append(f"  -- 合计 {cb['total']:,} 段｜最大单作品 {cb['max']:,}｜"
                 f"≥{mn:,} 段作品数 {cb['n_ge']}")
        L.append("")
    L.append(f"结论：按口径 A，『≥{res['need_works']} 作品各自 ≥{mn:,} 段』"
             f"{'成立' if res['verdict_ok'] else '不成立'}"
             f"（最大单作品 {m['A_max']:,} 段，达标作品数 {m['A_n_ge']}）。"
             f"不代主控裁定解冻。")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help="库路径（默认 env LG_DATABASE_URL → 本树 data/ → 主检出 data/）")
    ap.add_argument("--min-per-work", type=int, default=MIN_DEFAULT)
    ap.add_argument("--out-doc", default=str(ROOT / DOC_REL))
    ap.add_argument("--print-only", action="store_true", help="只打印，不写文档")
    args = ap.parse_args(argv)

    if args.db:
        db = Path(args.db)
        if not db.is_file():
            print(f"[k5_supply_recount] 指定的库不存在：{db}", file=sys.stderr)
            return 2
    else:
        db = default_db()
        if db is None:
            print("[k5_supply_recount] 找不到库（LG_DATABASE_URL / 本树 data/ / "
                  f"{MAIN_REPO_DB} 均无）", file=sys.stderr)
            return 2

    res = build_result(db, args.min_per_work)
    print(render_stdout(res), end="")
    if not args.print_only:
        out = Path(args.out_doc)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(res), encoding="utf-8")
        print(f"[k5_supply_recount] 已写 {out.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
