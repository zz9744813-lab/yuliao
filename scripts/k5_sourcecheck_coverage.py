#!/usr/bin/env python
"""K5 源校验覆盖盘查（2026-09-25 派工 lg-sourcecheck-coverage；只读、零模型调用、零 git 写）。

背景：`segments.integrity` 的 `src_ok` 是下游「合格段」的唯一源完整性命据
（`scripts/source_check.py` 写入，JSON true=完好）。主控 2026-09-25 真库只读
实测：438418 段中 431627 段 `src_ok` 键缺失——是「未校验」而不是「判坏」；
`source_check.py::needs_check`（L193-197）把非严格布尔一律当未校验要重查，
所以这批是**可被校验、但从未校验**的存量。本脚本盘查其覆盖面、分布与补跑代价。

纪律与口径：
- 真库 `sqlite3` `mode=ro` 打开；任何写操作会抛异常（测试钉死）。
- **严格布尔三态**：只用 `json_type(integrity,'$.src_ok')='true'/'false'` 判定
  （与 `source_check.py::parse_src_ok` L151-158 的三态纪律同语义）。
  **不许**用 `json_extract(...) IS TRUE`：SQLite 里 JSON true 抽出为整数 1，
  `1 IS TRUE` 为真 ⇒ 数字 1 / 字符串 "true" 这类**类型不严的存量脏值**会漏进
  「已校验」口径（k5_supply_recount.py 的 SRC_TRUE_IS 即此松口径，本脚本仅用它
  复算口径 A 的读数差，绝不用于覆盖判定）。
- 代价估算的全部输入常量从 `scripts/source_check.py` **源码里读出来并标注行号**
  （`parse_constants`），读不到即 fail-closed 判「证据不足」，不得凭空写数。
- 本脚本**不跑校验**：只盘查与算术估算；`source_check.py --run` 一分钱都没花。

用法：
    python scripts/k5_sourcecheck_coverage.py [--db PATH]
        [--out F:/Hermes/team/reports/k5_sourcecheck_coverage_20260925.json]
        [--md-out docs/K5源校验覆盖盘查_20260925.md] [--print-only]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_CHECK_REL = "scripts/source_check.py"
SOURCE_CHECK = ROOT / "scripts" / "source_check.py"
DOC_REL = Path("docs") / "K5源校验覆盖盘查_20260925.md"
MAIN_REPO_DB = Path("F:/agi/language-genome/data/language_genome.db")
DEFAULT_OUT = Path("F:/Hermes/team/reports/k5_sourcecheck_coverage_20260925.json")

# 主控 2026-09-25 真库只读实测（任务书背景给定）——只作对照基准，非本脚本产物
REF_U4 = [
    ("全部段", "total", 438_418),
    ("src_ok = true（完好）", "src_true", 4_872),
    ("src_ok = false（判坏）", "src_false", 1_919),
    ("未校验（主控合并口径）", "unverified", 431_627),
    ("口径 A（IS TRUE 松口径）合计", "A_loose_total", 4_220),
]

# ── 从 scripts/source_check.py 源码读输入常量（值 + 行号，缺一即 fail-closed）──
_CONST_PATTERNS = {
    # 每次调用 1 段所用的模型（默认值；LG_SOURCE_MODEL 可覆盖）
    "MODEL": r'^MODEL = os\.environ\.get\("LG_SOURCE_MODEL", "([^"]+)"',
    # 熔断：累计满 MIN_CALLS 次调用后，失败率 > FAIL_RATE 当场中止
    "BREAKER_MIN_CALLS": r"^BREAKER_MIN_CALLS = (\d+)",
    "BREAKER_FAIL_RATE": r"^BREAKER_FAIL_RATE = ([0-9.]+)",
    # 并发默认值（每批 = 一个并发波，批内最多 CONC_DEFAULT 段同时开跑）
    "CONC_DEFAULT": r'add_argument\("--conc", type=int, default=(\d+)',
    # 空/过短段直接判坏（零 LLM 路径之一）
    "MIN_TEXT_CHARS": r"if not text or len\(text\.strip\(\)\) < (\d+):",
    # 段 id 取数分批上限（SQL 变量闸，非 LLM 批）
    "IN_CHUNK": r"for i in range\(0, len\(ids\), (\d+)\):",
    # 串行兜底：workers 由 pool_workers 决定（命中单账号 CLI 通道 → workers=1）
    "POOL_WORKERS": r"(workers = pool_workers\()",
}


def parse_constants(path: Path = SOURCE_CHECK) -> dict:
    """读 source_check.py 源码，抽出代价估算输入常量（每项带行号与原文行）。

    返回 {"ok": bool, "missing": [...], "items": {name: {value,line,raw}},
          "check_one_chat_calls": {count,line,raw},
          "known_typos": [{bad,good,line}], "path": ...}
    任一必需常量缺失 / KNOWN_TYPOS 不可解析 / check_one 内 chat( 次数≠1 → ok=False
    （fail-closed：代价估算判「证据不足」，不得凭空写数）。
    """
    out: dict = {"path": Path(path).as_posix(), "ok": False, "missing": [],
                 "items": {}, "check_one_chat_calls": None, "known_typos": []}
    try:
        src = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        out["missing"].append(f"<文件不可读: {e}>")
        return out
    lines = src.splitlines()
    for name, pat in _CONST_PATTERNS.items():
        hit = None
        for i, ln in enumerate(lines, 1):
            m = re.search(pat, ln)
            if m:
                hit = {"value": m.group(1), "line": i, "raw": ln.strip()}
                break
        if hit is None:
            out["missing"].append(name)
        else:
            out["items"][name] = hit
    # check_one 函数体内 chat( 出现次数（= 每段 LLM 调用数，必须恰为 1）
    m = re.search(r"^def check_one\(.*?(?=^def )", src, re.S | re.M)
    if not m:
        out["missing"].append("CHECK_ONE")
    else:
        n_chat = m.group(0).count("chat(")
        pos = m.group(0).find("chat(")
        line = src[:m.start() + pos].count("\n") + 1 if pos >= 0 else -1
        out["check_one_chat_calls"] = {"count": n_chat, "line": line,
                                       "raw": lines[line - 1].strip()
                                       if 0 < line <= len(lines) else ""}
        if n_chat != 1:
            out["missing"].append("CHECK_ONE_CHAT_COUNT")
    # KNOWN_TYPOS 确定性规则表（零 LLM 路径之二：规则命中直接判坏）
    m = re.search(r"^KNOWN_TYPOS = \{(.*?)^\}", src, re.S | re.M)
    if not m:
        out["missing"].append("KNOWN_TYPOS")
    else:
        base = src[:m.start()].count("\n")  # `KNOWN_TYPOS = {` 行之前已有的行数
        entries = []
        for off, ln in enumerate(m.group(1).splitlines(), 2):
            t = re.match(r'\s*"([^"\\]+)":\s*"([^"\\]+)",?\s*(?:#.*)?$', ln)
            if t:
                entries.append({"bad": t.group(1), "good": t.group(2),
                                "line": base + off})
        if any(re.search(r"['\\]", t["bad"] + t["good"]) for t in entries):
            out["missing"].append("KNOWN_TYPOS_QUOTES")  # fail-closed：不拼进 SQL
        if not entries:
            out["missing"].append("KNOWN_TYPOS_ENTRIES")
        out["known_typos"] = entries
    out["ok"] = not out["missing"]
    return out


def _typed_items(consts: dict) -> dict:
    it = consts["items"]
    return {k: it[k]["value"] for k in it}


# ── 只读访问 ──────────────────────────────────────────────────────────
def default_db() -> Path | None:
    """解析真库：env LG_DATABASE_URL → 本树 data/ → 主检出 data/。"""
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
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def json_probe(con: sqlite3.Connection) -> bool:
    try:
        hit = con.execute("SELECT json_extract('{\"a\":true}','$.a') IS TRUE").fetchone()[0]
        typ = con.execute("SELECT json_type('{\"a\":true}','$.a')").fetchone()[0]
    except sqlite3.Error:
        return False
    return bool(hit) and typ == "true"


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


# ── SQL 口径片段 ──────────────────────────────────────────────────────
# 严格布尔五态（source_check.parse_src_ok 三态的 SQL 镜像）：
#   true/false=已校验（严格布尔）；missing=合法 JSON 缺键；loose=键在但类型不严
#   （数字/字符串/null）；nojson=NULL/非法 JSON。后三者一律 = 未校验。
def state_expr(col: str) -> str:
    return f"""CASE
  WHEN {col} IS NULL OR json_valid({col})=0 THEN 'nojson'
  WHEN json_type({col},'$.src_ok')='true' THEN 'true'
  WHEN json_type({col},'$.src_ok')='false' THEN 'false'
  WHEN json_type({col},'$.src_ok') IS NULL THEN 'missing'
  ELSE 'loose' END"""


STATE = state_expr("s.integrity")
UNCHECKED = f"({STATE}) IN ('missing','loose','nojson')"

# 待检查文本口径：与 source_check.py L331 `(x.text_clean or x.text)` 逐字同义
TXT = ("CASE WHEN s.text_clean IS NULL OR s.text_clean='' "
       "THEN s.text ELSE s.text_clean END")

# 口径 A/B（与 scripts/k5_supply_recount.py 同判据，用于 §4 口径影响复算）
NOT_BENCH = "(s.role IS NULL OR s.role<>'benchmark')"
_WL_LEN = """CASE
  WHEN ws.allowed_purposes IS NULL OR json_valid(ws.allowed_purposes)=0 THEN 0
  WHEN json_type(ws.allowed_purposes)<>'array' THEN 0
  ELSE json_array_length(ws.allowed_purposes) END"""
WL_OK = ("EXISTS (SELECT 1 FROM work_sources ws WHERE ws.work_id=s.work_id"
         f" AND ({_WL_LEN})>0)")
COND_A_LOOSE = (f"(s.integrity IS NOT NULL AND json_valid(s.integrity)=1 "
                "AND json_extract(s.integrity,'$.src_ok') IS TRUE)"
                f" AND {NOT_BENCH} AND {WL_OK}")
COND_A_STRICT = (f"(s.integrity IS NOT NULL AND json_valid(s.integrity)=1 "
                 "AND json_type(s.integrity,'$.src_ok')='true')"
                 f" AND {NOT_BENCH} AND {WL_OK}")
COND_B = f"{NOT_BENCH} AND {WL_OK}"


def _save_pred(min_chars: int, typos: list[dict]) -> str:
    """零 LLM 路径（source_check.run 内先于 chat 的两个 return）的 SQL 镜像。

    方向纪律：本谓词命中的段集合必须 ⊆ source_check 实际跳过 LLM 的段集合
    （宁可高估调用数、不可低估——「下界」必须是真的下界）：
    · 过短判定用 `length(trim(TXT)) < MIN_TEXT_CHARS`（source_check.py L429
      `len(text.strip()) < 10`；SQL trim 只去空格，比 Python strip 去得少 ⇒
      trim<10 蕴含 strip<10，命中即真跳过）；TXT 空/NULL 同 L429 `not text`。
    · 规则命中镜像 source_check.py L414-415 `rule_defects(text)`（KNOWN_TYPOS
      子串命中直接判坏，不花 LLM 的钱）：`吴天`/`了天斗罗` 为纯子串，逐字等价；
      `千雪` 的 `(?<!仞)` 负向后顾只检查**首处**出现位置（后续出现漏检 ⇒
      少算跳过数，方向安全）。
    """
    arms = [f"{TXT} IS NULL", f"{TXT}=''",
            f"length(trim({TXT})) < {int(min_chars)}"]
    for t in typos:
        bad = t["bad"]
        if bad == "千雪":
            # (?<!仞)千雪：首处位置 1（前面没字符）必命中；否则看前一个字符
            arms.append(
                f"(instr({TXT},'{bad}')>0 AND (instr({TXT},'{bad}')=1 "
                f"OR substr({TXT}, instr({TXT},'{bad}')-1, 1)<>'仞'))")
        else:
            arms.append(f"instr({TXT},'{bad}')>0")
    return "(" + " OR ".join(arms) + ")"


def _group_state(con: sqlite3.Connection, extra_where: str, dim_expr: str,
                 dim_label: str) -> list[dict]:
    where = extra_where + (" AND " if extra_where else "")
    rows = con.execute(
        f"SELECT {dim_expr} AS d, {STATE} AS st, COUNT(*) AS n "
        f"FROM segments s {where}GROUP BY 1, 2").fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        key = r["d"] if r["d"] is not None else "(null)"
        b = agg.setdefault(str(key), {dim_label: str(key), "total": 0,
                                      "true": 0, "false": 0, "missing": 0,
                                      "loose": 0, "nojson": 0})
        b["total"] += r["n"]
        b[r["st"]] = b.get(r["st"], 0) + r["n"]
    for b in agg.values():
        b["unverified"] = b["missing"] + b["loose"] + b["nojson"]
        b["checked_strict"] = b["true"] + b["false"]
        b["unverified_ratio"] = round(b["unverified"] / b["total"], 6) if b["total"] else 0.0
        b["check_rate"] = round(b["checked_strict"] / b["total"], 6) if b["total"] else 0.0
    out = sorted(agg.values(),
                 key=lambda x: (-x["unverified"], -x["total"], x[dim_label]))
    tot = sum(b["unverified"] for b in out) or 1
    for b in out:
        b["share_of_unverified"] = round(b["unverified"] / tot, 6)
    return out


def _explicit_batch_fields(con: sqlite3.Connection) -> list[str]:
    fields: list[str] = []
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        quoted = table.replace('"', '""')
        for row in con.execute(f'PRAGMA table_info("{quoted}")'):
            name = row[1]
            lowered = name.lower()
            if "import" in lowered or "batch" in lowered:
                fields.append(f"{table}.{name}")
    return fields


def _distribution_reading(rows: list[dict], key: str) -> str:
    usable = [r for r in rows if "note" not in r]
    if not usable:
        return "该维缺列或无数据，不能判断。"
    parts = [
        (f"{r[key]}：未校验 {r['unverified']:,}，占全部未校验 "
         f"{r['share_of_unverified']:.2%}，组内未校验率 "
         f"{r['unverified_ratio']:.2%}，组内已校验率 {r['check_rate']:.2%}")
        for r in usable
    ]
    return "；".join(parts) + "。以上仅为分布数字，不据此作导入批次或版本因果判断。"


def _profile_assessment(seg_rows: list[dict], text_rows: list[dict],
                        batch_fields: list[str]) -> dict:
    if batch_fields:
        batch_reading = ("库结构发现显式 import/batch 字段：" +
                         ", ".join(batch_fields) +
                         "；本报告不把字段名本身解释为未校验原因。")
    else:
        batch_reading = ("库结构未发现列名含 import/batch 的显式导入批次字段；"
                         "只能按 seg_version/text_version 分层，不能给导入批次因果结论。")
    seg_reading = _distribution_reading(seg_rows, "seg_version")
    usable_seg = [r for r in seg_rows if "note" not in r]
    if usable_seg:
        total = sum(r["total"] for r in usable_seg)
        largest = max(usable_seg, key=lambda r: r["total"])
        unverified_total = sum(r["unverified"] for r in usable_seg)
        total_share = largest["total"] / total if total else 0.0
        unverified_share = (largest["unverified"] / unverified_total
                            if unverified_total else 0.0)
        rate_spread = (max(r["unverified_ratio"] for r in usable_seg) -
                       min(r["unverified_ratio"] for r in usable_seg))
        seg_reading += (f" 最大的 seg_version={largest['seg_version']} 占全部段 "
                        f"{total_share:.2%}、占未校验 {unverified_share:.2%}"
                        f"（差 {abs(unverified_share - total_share) * 100:.4f} 个百分点）；"
                        f"各组未校验率极差 {rate_spread * 100:.2f} 个百分点。"
                        "数字未把未校验孤立到单一 seg_version。")
    return {
        "import_batch": {
            "explicit_fields": batch_fields,
            "reading": batch_reading,
        },
        "seg_version": {
            "reading": seg_reading,
        },
        "text_version": {
            "reading": _distribution_reading(text_rows, "text_version"),
        },
    }


def build_result(db_path: Path, consts: dict | None = None) -> dict:
    """真跑盘查（只读）。返回结构确定（无时间戳），同库两次输出逐字节一致。"""
    consts = consts if consts is not None else parse_constants()
    res: dict = {
        "tool": "k5_sourcecheck_coverage",
        "db_path": Path(db_path).as_posix(),
        "sqlite_version": sqlite3.sqlite_version,
        "readonly": True,
        "constants": consts,
        "strict_discipline": {
            "rule": ("已校验 = json_type(integrity,'$.src_ok') ∈ {'true','false'}"
                     "（严格布尔，source_check.py::parse_src_ok L151-158 / "
                     "needs_check L193-197 同语义）；数字 1、字符串 \"true\"、"
                     "null、缺键、非法 JSON 一律计「未校验」。"),
            "why_not_is_true": ("json_extract(...,'$.src_ok') IS TRUE 是松口径："
                                "SQLite 把 JSON true 抽出为整数 1，数字 1 也满足 "
                                "1 IS TRUE ⇒ 类型不严的脏值混入「已校验」，"
                                "正是 source_check 三态纪律要排除的写法。"
                                "本脚本仅在 §4 口径影响里用它复算口径 A 的读数差。"),
        },
        "issues": [],
    }
    con = open_ro(db_path)
    try:
        probe_ok = json_probe(con)
        tabs = _tables(con)
        required = {
            "segments": {"id", "work_id", "integrity", "role", "seg_version",
                         "text", "text_clean"},
            "works": {"id", "title"},
            "work_sources": {"work_id", "source_type", "text_version",
                             "allowed_purposes"},
        }
        table_cols = {name: _cols(con, name) if name in tabs else set()
                      for name in required}
        schema_ok = (probe_ok and all(
            name in tabs and cols <= table_cols[name]
            for name, cols in required.items()))
        if not schema_ok:
            missing_bits = []
            if not probe_ok:
                missing_bits.append("JSON1 扩展不可用")
            for name, cols in required.items():
                if name not in tabs:
                    missing_bits.append(f"{name} 表缺失")
                elif not cols <= table_cols[name]:
                    missing_bits.append(f"{name} 缺列 {sorted(cols - table_cols[name])}")
            res["issues"].append("；".join(missing_bits) + "——覆盖盘查不可行，fail-closed。")
            res["conclusion"] = {"verdict": "不可校验",
                                 "reason": "库结构不满足盘查前提：" + "；".join(missing_bits),
                                 "not_done": NOT_DONE}
            return res
        seg_cols = table_cols["segments"]

        # §1 全库五态 + 严格/松口径读数差
        st = dict(con.execute(
            f"SELECT {STATE}, COUNT(*) FROM segments s GROUP BY 1").fetchall())
        total = sum(st.values())
        src_true, src_false = st.get("true", 0), st.get("false", 0)
        unverified = st.get("missing", 0) + st.get("loose", 0) + st.get("nojson", 0)
        leak = con.execute(
            "SELECT COUNT(*) FROM segments s "
            "WHERE s.integrity IS NOT NULL AND json_valid(s.integrity)=1 "
            "AND json_extract(s.integrity,'$.src_ok') IS TRUE "
            "AND COALESCE(json_type(s.integrity,'$.src_ok'),'')<>'true'"
        ).fetchone()[0]
        res["totals"] = {
            "total": total, "src_true": src_true, "src_false": src_false,
            "checked_strict": src_true + src_false,
            "unverified": unverified,
            "unverified_missing": st.get("missing", 0),
            "unverified_loose": st.get("loose", 0),
            "unverified_nojson": st.get("nojson", 0),
            "unverified_ratio": round(unverified / total, 6) if total else 0.0,
            "is_true_loose_over_strict_leak": leak,
        }

        # §1 逐作品盘查（未校验数降序；并列按未校验占比降序、再 work_id 升序）
        titles = ({r["id"]: r["title"] for r in
                   con.execute("SELECT id, title FROM works")}
                  if "works" in tabs else {})
        ws_cols = _cols(con, "work_sources") if "work_sources" in tabs else set()
        ws_reg_ok = "work_sources" in tabs and {"work_id", "source_type",
                                                "text_version"} <= ws_cols
        ws_pv_ok = "work_sources" in tabs and {"work_id", "text_version"} <= ws_cols
        ws_wl_ok = "work_sources" in tabs and {"work_id", "allowed_purposes"} <= ws_cols
        ws_rows = ({r["work_id"]: r for r in con.execute(
            "SELECT work_id, source_type, text_version FROM work_sources")}
            if ws_reg_ok else {})
        by_state: dict[str, dict[str, int]] = {}
        for r in con.execute(
                f"SELECT s.work_id AS w, {STATE} AS st, COUNT(*) AS n "
                "FROM segments s GROUP BY 1, 2").fetchall():
            by_state.setdefault(r["w"], {})[r["st"]] = r["n"]
        roles: dict[str, dict[str, int]] = {}
        has_role = "role" in seg_cols
        if has_role:
            for r in con.execute(
                    "SELECT s.work_id AS w, COALESCE(s.role,'(null)') AS ro, "
                    "COUNT(*) AS n FROM segments s GROUP BY 1, 2").fetchall():
                roles.setdefault(r["w"], {})[r["ro"]] = r["n"]
        works = []
        for w, c in sorted(by_state.items()):
            n_total = sum(c.values())
            n_true, n_false = c.get("true", 0), c.get("false", 0)
            n_un = n_total - n_true - n_false
            reg = ws_rows.get(w)
            works.append({
                "work_id": w,
                "title": titles.get(w, "(无 works 行)"),
                "source_type": (reg["source_type"] if reg else "(未登记)"),
                "text_version": (reg["text_version"] if reg else "(未登记)"),
                "roles": dict(sorted(roles.get(w, {}).items())),
                "segments": n_total,
                "checked_strict": n_true + n_false,
                "src_true": n_true,
                "src_false": n_false,
                "unverified": n_un,
                "unverified_ratio": round(n_un / n_total, 6) if n_total else 0.0,
            })
        works.sort(key=lambda x: (-x["unverified"], -x["unverified_ratio"],
                                  x["work_id"]))
        res["by_work"] = works

        # §2 未校验分布画像：seg_version / work_id / text_version 三维
        has_sv = "seg_version" in seg_cols
        seg_rows = (_group_state(con, "", "s.seg_version", "seg_version")
                    if has_sv else
                    [{"note": "segments 无 seg_version 列，本维缺席"}])
        text_rows = (
            _group_state(con, "",
                         "COALESCE((SELECT w.text_version FROM work_sources w"
                         " WHERE w.work_id=s.work_id), '(未登记)')",
                         "text_version") if ws_pv_ok else
            [{"note": "work_sources 缺 work_id/text_version 列，text_version 维缺席"}])
        res["unverified_profile"] = {
            "note": ("只给分布数字，不给因果推测。「相关性」读法：某维值的 "
                     "share_of_unverified 高 ⇒ 未校验集中于该值；check_rate 是"
                     "该值自身的已校验比例。两者分开看（大盘子大 ≠ 更相关）。"),
            "by_seg_version": seg_rows,
            "by_work": [{"work_id": x["work_id"], "segments": x["segments"],
                         "unverified": x["unverified"],
                         "share_of_unverified": (
                             round(x["unverified"] / unverified, 6)
                             if unverified else 0.0),
                         "check_rate": (round(x["checked_strict"] / x["segments"], 6)
                                        if x["segments"] else 0.0)}
                        for x in works],
            "by_text_version": text_rows,
            "assessment": _profile_assessment(
                seg_rows, text_rows, _explicit_batch_fields(con)),
        }

        # §3 代价估算（纯算术；常量全部来自 source_check.py 源码 + 行号）
        cost: dict = {"inputs_from": SOURCE_CHECK_REL}
        if not consts["ok"]:
            cost["error"] = ("输入常量读不全（fail-closed，不凭空写数）："
                             + ",".join(consts["missing"]))
        else:
            items = _typed_items(consts)
            conc = int(items["CONC_DEFAULT"])
            min_chars = int(items["MIN_TEXT_CHARS"])
            chunk = int(items["IN_CHUNK"])
            bmin = int(items["BREAKER_MIN_CALLS"])
            brate = float(items["BREAKER_FAIL_RATE"])
            per_call = consts["check_one_chat_calls"]["count"]
            if {"text", "text_clean"} <= seg_cols:
                save = con.execute(
                    f"SELECT COUNT(*) FROM segments s WHERE {UNCHECKED} "
                    f"AND {_save_pred(min_chars, consts['known_typos'])}").fetchone()[0]
                save_note = ""
            else:
                save = 0  # 文本列缺席：零 LLM 路径无法量化，下界取上界（费用侧高估）
                save_note = "segments 缺 text/text_clean 列，零 LLM 跳过数按 0 计"
            upper = unverified * per_call
            lower = max(0, (unverified - save)) * per_call
            cost.update({
                "constants": {
                    "CONC_DEFAULT": {"value": conc, "line": consts["items"]["CONC_DEFAULT"]["line"]},
                    "MIN_TEXT_CHARS": {"value": min_chars, "line": consts["items"]["MIN_TEXT_CHARS"]["line"]},
                    "IN_CHUNK": {"value": chunk, "line": consts["items"]["IN_CHUNK"]["line"]},
                    "BREAKER_MIN_CALLS": {"value": bmin, "line": consts["items"]["BREAKER_MIN_CALLS"]["line"]},
                    "BREAKER_FAIL_RATE": {"value": brate, "line": consts["items"]["BREAKER_FAIL_RATE"]["line"]},
                    "CALLS_PER_SEGMENT": {"value": per_call, "line": consts["check_one_chat_calls"]["line"]},
                    "MODEL": {"value": items["MODEL"], "line": consts["items"]["MODEL"]["line"]},
                    "POOL_WORKERS": {"line": consts["items"]["POOL_WORKERS"]["line"]},
                    "KNOWN_TYPOS": [{**t} for t in consts["known_typos"]],
                },
                "unverified_segments": unverified,
                "zero_llm_skips": save,
                **({"zero_llm_skips_note": save_note} if save_note else {}),
                "zero_llm_paths": ("规则命中 KNOWN_TYPOS（L414-415，命中直接判坏不调 LLM）"
                                   f"或 text 空/过短 <{min_chars} 字（L429）。"
                                   "千雪 负向后顾只查首处出现，跳过数按下界计。"),
                "calls_upper": upper,
                "calls_lower": lower,
                "batches_conc_upper": math.ceil(upper / conc),
                "batches_conc_lower": math.ceil(lower / conc),
                "batches_serial_upper": upper,   # 单账号 CLI 通道命中时 workers=1（pool_workers）
                "batches_serial_lower": lower,
                "fetch_batches_in_chunk": math.ceil(unverified / chunk),
                "breaker": {
                    "min_calls_before_trip": bmin,
                    "fail_rate_line": brate,
                    "earliest_trip_wave": math.ceil(bmin / conc),
                    "note": (f"熔断在累计满 {bmin} 次调用后才可能触发（conc={conc} "
                             f"时最早第 {math.ceil(bmin / conc)} 批）；实际失败率 "
                             f"≤{brate:.0%} 则单轮不中止、一次扫完，"
                             f">{brate:.0%} 则当场中止、剩余段留待下轮。"),
                },
            })
        res["cost"] = cost

        # §4 口径影响：未校验段若保持未校验，口径 A/B 变不变（真跑 SQL）
        if ws_wl_ok and has_role:
            a_loose = con.execute(
                f"SELECT COUNT(*) FROM segments s WHERE {COND_A_LOOSE}").fetchone()[0]
            a_strict = con.execute(
                f"SELECT COUNT(*) FROM segments s WHERE {COND_A_STRICT}").fetchone()[0]
            b_cnt = con.execute(
                f"SELECT COUNT(*) FROM segments s WHERE {COND_B}").fetchone()[0]
            ua = con.execute(
                f"SELECT COUNT(*) FROM segments s WHERE {UNCHECKED} "
                f"AND ({COND_A_LOOSE})").fetchone()[0]
            ua_s = con.execute(
                f"SELECT COUNT(*) FROM segments s WHERE {UNCHECKED} "
                f"AND ({COND_A_STRICT})").fetchone()[0]
            ub = con.execute(
                f"SELECT COUNT(*) FROM segments s WHERE {UNCHECKED} "
                f"AND ({COND_B})").fetchone()[0]
            res["caliber_impact"] = {
                "A_loose_total": a_loose, "A_strict_total": a_strict,
                "B_total": b_cnt,
                "unverified_in_A": ua, "unverified_in_A_strict": ua_s,
                "unverified_in_B": ub,
                "unchanged_if_kept_unverified": True,
                "reading": (
                    "「保持未校验」= 不补跑、库里一个字都不改：口径 A/B 的谓词"
                    "作用在现状数据上，成员判定逐段独立，未校验段的存量状态不变"
                    "⇒ 两口径读数不变（实测即现值：A 松 "
                    f"{a_loose:,} / A 严格 {a_strict:,} / B {b_cnt:,}）。"
                    "细分：missing/nojson 段结构性不满足 `IS TRUE`；loose 段中"
                    "数字 1 这类脏值**会**漏进松口径 A——「未校验 ∧ A」实测 "
                    f"{ua:,}（其中脏值掺水即 leak），而严格布尔口径下恒 "
                    f"{ua_s:,}。口径 B 不消费 src_ok，未校验段本就已按其"
                    f"自身条件计入/排除（「未校验 ∧ B」= {ub:,} 为 B 池内"
                    "可补查量）。补跑 source_check 才可能改变 A/B；不补跑则纹丝不动。"),
            }
        else:
            res["caliber_impact"] = {
                "error": ("work_sources(allowed_purposes)/segments(role) 列缺席，"
                          "口径 A/B 无法复算（fail-closed）。")}

        # §5 结论
        verdict = "可校验"
        reason = (f"未校验 {unverified:,} 段（占 {res['totals']['unverified_ratio']:.2%}"
                  f"）全部有可判定的 integrity 口径（严格布尔三态），段文本与"
                  f" source_check.py 的选取通道存在——是「可被校验、但从未校验」"
                  f"的存量，非「判坏」。")
        if unverified == 0:
            reason = "全库无未校验段（未校验=0），盘查平凡成立。"
        if not consts["ok"]:
            verdict = "证据不足"
            reason = ("代价估算输入常量读不全（" + ",".join(consts["missing"])
                      + "）：覆盖表仍真实可查，但调用数上下界无法给出，判证据不足。")
        res["conclusion"] = {"verdict": verdict, "reason": reason,
                             "not_done": NOT_DONE}
        return res
    finally:
        con.close()


NOT_DONE = [
    "未真跑任何源校验（source_check.py --run 一次都没执行，零 LLM 调用、零费用）",
    "未写库：真库以 mode=ro 打开，data/ 下文件 mtime 与内容不变",
    "未做 git 写操作（无 commit/merge/push）",
    "未修改任何既有文件；交付仅本任务清单内 4 个文件",
    "未对「未校验为何集中于此」给因果结论（只给分布数字）",
]


# ── 渲染 ──────────────────────────────────────────────────────────────
def render_markdown(res: dict, stdout_text: str | None = None) -> str:
    L: list[str] = []
    add = L.append
    add("# K5 源校验覆盖盘查（2026-09-25）")
    add("")
    add(f"- 工具：`scripts/k5_sourcecheck_coverage.py`（`sqlite3` `mode=ro` 只读、"
        f"零模型调用、零 git 写；输出 JSON 确定序，同库两次运行逐字节一致）。")
    add(f"- 库：`{res['db_path']}`｜SQLite {res['sqlite_version']}。")
    add(f"- 三态纪律：{res['strict_discipline']['rule']}")
    add("")
    if stdout_text:
        add("## 0. 真跑 stdout 原样片段")
        add("")
        add("```text")
        add(stdout_text.rstrip())
        add("```")
        add("")
    t = res.get("totals")
    if t:
        add("## 1. 全库覆盖读数（严格布尔）")
        add("")
        add("| 状态 | 段数 |")
        add("|---|---:|")
        add(f"| 全部段 | {t['total']:,} |")
        add(f"| `src_ok=true`（完好） | {t['src_true']:,} |")
        add(f"| `src_ok=false`（判坏） | {t['src_false']:,} |")
        add(f"| 未校验（缺键 {t['unverified_missing']:,} + 类型不严 "
            f"{t['unverified_loose']:,} + 非法/NULL {t['unverified_nojson']:,}） "
            f"| {t['unverified']:,}（{t['unverified_ratio']:.2%}） |")
        add(f"| `IS TRUE` 松口径比严格口径多算的段（数字 1 等脏值） "
            f"| {t['is_true_loose_over_strict_leak']:,} |")
        add("")
        add("| 项目 | 主控 U4 参照 | 本脚本复算 |")
        add("|---|---:|---:|")
        m = {k: t.get(k) for k in
             ("total", "src_true", "src_false", "unverified")}
        if res.get("caliber_impact") and "A_loose_total" in res["caliber_impact"]:
            m["A_loose_total"] = res["caliber_impact"]["A_loose_total"]
        for item, key, ref in REF_U4:
            add(f"| {item} | {ref:,} | {m.get(key, '—'):,} |"
                if isinstance(m.get(key), int) else f"| {item} | {ref:,} | — |")
        add("")
        add("## 1b. 逐作品盘查（按未校验数降序）")
        add("")
        add("| work_id | source_type | 段数 | 已查(严格) | true | false | 未校验 | 未校验占比 | role 分布 |")
        add("|---|---|---:|---:|---:|---:|---:|---:|---|")
        for w in res["by_work"]:
            roles = ", ".join(f"{k}:{v:,}" for k, v in w["roles"].items())
            add(f"| {w['work_id']} | {w['source_type']} | {w['segments']:,} "
                f"| {w['checked_strict']:,} | {w['src_true']:,} "
                f"| {w['src_false']:,} | {w['unverified']:,} "
                f"| {w['unverified_ratio']:.2%} | {roles} |")
        add("")
    up = res.get("unverified_profile")
    if up:
        add("## 2. 未校验段分布画像（只给数字，不给因果推测）")
        add("")
        add(up["note"])
        for dim, key, label in [("按 seg_version", "by_seg_version", "seg_version"),
                                ("按 work_id", "by_work", "work_id"),
                                ("按 text_version", "by_text_version", "text_version")]:
            add("")
            add(f"### {dim}")
            add("")
            rows = up[key]
            if rows and "note" in rows[0]:
                add(f"_{rows[0]['note']}_")
                continue
            add(f"| {label} | 段数 | 未校验 | 占全部未校验 | 组内未校验率 | 组内已校验率 |")
            add("|---|---:|---:|---:|---:|---:|")
            for r in rows:
                total = r["total"] if "total" in r else r["segments"]
                ratio = r.get("unverified_ratio", r["unverified"] / total)
                add(f"| {r[label]} | {total:,} | {r['unverified']:,} "
                    f"| {r['share_of_unverified']:.2%} | {ratio:.2%} "
                    f"| {r['check_rate']:.2%} |")
        assessment = up.get("assessment", {})
        if assessment:
            add("")
            add("### 导入批次 / seg_version 数字判读")
            add("")
            add(f"- 导入批次字段：{assessment['import_batch']['reading']}")
            add(f"- seg_version：{assessment['seg_version']['reading']}")
            add(f"- text_version：{assessment['text_version']['reading']}")
        add("")
    c = res.get("cost")
    if c:
        add("## 3. 补跑代价估算（纯算术；常量逐条来自 source_check.py 源码+行号）")
        add("")
        if "error" in c:
            add(f"**{c['error']}**")
        else:
            k = c["constants"]
            add(f"- 未校验段 {c['unverified_segments']:,}；"
                f"零 LLM 路径可跳过（下界口径）{c['zero_llm_skips']:,}："
                f"{c['zero_llm_paths']}")
            add(f"- 每段调用数 = {k['CALLS_PER_SEGMENT']['value']}"
                f"（`source_check.py` L{k['CALLS_PER_SEGMENT']['line']} check_one 内"
                f"恰一次 `chat(`）")
            add(f"- **调用次数上界 = {c['calls_upper']:,}；下界 = {c['calls_lower']:,}**")
            add(f"- 批次数（每批 = 一个并发波，批上限 conc="
                f"{k['CONC_DEFAULT']['value']}，`source_check.py` L{k['CONC_DEFAULT']['line']}）："
                f"上界 {c['batches_conc_upper']:,} 批 / 下界 {c['batches_conc_lower']:,} 批；"
                f"命中单账号 CLI 通道时 workers=1（L{k['POOL_WORKERS']['line']} "
                f"pool_workers 串行强制）⇒ 上界 {c['batches_serial_upper']:,} / "
                f"下界 {c['batches_serial_lower']:,} 批。")
            add(f"- 段 id 取数分批（SQL 变量闸，非 LLM 批）：每批上限 "
                f"{k['IN_CHUNK']['value']}（L{k['IN_CHUNK']['line']}）⇒ "
                f"{c['fetch_batches_in_chunk']:,} 次取数批。")
            add(f"- 熔断闸：累计满 {k['BREAKER_MIN_CALLS']['value']} 次调用"
                f"（L{k['BREAKER_MIN_CALLS']['line']}）后失败率 > "
                f"{k['BREAKER_FAIL_RATE']['value']}（L{k['BREAKER_FAIL_RATE']['line']}）"
                f"当场中止 ⇒ conc={k['CONC_DEFAULT']['value']} 时最早第 "
                f"{c['breaker']['earliest_trip_wave']} 批中止；{c['breaker']['note']}")
            add(f"- 模型：`{k['MODEL']['value']}`（L{k['MODEL']['line']}，"
                f"LG_SOURCE_MODEL 可覆盖）")
        add("")
    ci = res.get("caliber_impact")
    if ci and "reading" in ci:
        add("## 4. 口径影响（真跑 SQL）")
        add("")
        add(ci["reading"])
        add("")
        add(f"- 现值：口径 A（松 IS TRUE）= {ci['A_loose_total']:,}；"
            f"口径 A（严格布尔）= {ci['A_strict_total']:,}；口径 B = {ci['B_total']:,}。")
        add(f"- 「未校验 ∧ A」= {ci['unverified_in_A']:,}；"
            f"「未校验 ∧ B」= {ci['unverified_in_B']:,}。")
    elif ci:
        add("## 4. 口径影响")
        add("")
        add(f"**{ci['error']}**")
        add("")
    co = res.get("conclusion", {})
    add("## 5. 结论")
    add("")
    add(f"- verdict：**{co.get('verdict', '证据不足')}**｜{co.get('reason', '')}")
    add("")
    add("### 未自跑（本任务未做的事）")
    add("")
    for n in co.get("not_done", NOT_DONE):
        add(f"- {n}")
    add("")
    return "\n".join(L) + "\n"


def render_stdout(res: dict) -> str:
    t = res.get("totals", {})
    c = res.get("cost", {})
    co = res.get("conclusion", {})
    L = [f"[k5_sourcecheck_coverage] db={res['db_path']} "
         f"sqlite={res['sqlite_version']} (mode=ro 只读、零模型调用)"]
    if t:
        L.append(f"[覆盖] 全部 {t['total']:,}｜已校验(严格布尔) {t['checked_strict']:,}"
                 f"（true {t['src_true']:,} / false {t['src_false']:,}）｜"
                 f"未校验 {t['unverified']:,}（{t['unverified_ratio']:.2%}，"
                 f"缺键 {t['unverified_missing']:,}/不严 {t['unverified_loose']:,}/"
                 f"非法 {t['unverified_nojson']:,}）｜IS TRUE 松口径掺水 "
                 f"{t['is_true_loose_over_strict_leak']:,}")
    if "calls_upper" in c:
        conc = c["constants"]["CONC_DEFAULT"]["value"]
        L.append(f"[代价] 调用次数 下界 {c['calls_lower']:,} ~ 上界 {c['calls_upper']:,}"
                 f"（零 LLM 路径可跳过 {c['zero_llm_skips']:,}）；"
                 f"批次 conc={conc}: {c['batches_conc_lower']:,}~{c['batches_conc_upper']:,}"
                 f"，串行: {c['batches_serial_lower']:,}~{c['batches_serial_upper']:,}")
    elif "error" in c:
        L.append(f"[代价] {c['error']}")
    ci = res.get("caliber_impact", {})
    if ci and "reading" in ci:
        L.append(f"[口径] A(松)={ci['A_loose_total']:,} A(严)={ci['A_strict_total']:,} "
                 f"B={ci['B_total']:,}；未校验∧A={ci['unverified_in_A']:,} "
                 f"未校验∧B={ci['unverified_in_B']:,}（保持未校验则 A/B 不变）")
    for w in res.get("by_work", [])[:5]:
        L.append(f"  · {w['work_id']} {w['title']}：未校验 {w['unverified']:,}"
                 f"/{w['segments']:,}（{w['unverified_ratio']:.2%}）")
    L.append(f"[结论] {co.get('verdict', '证据不足')}｜{co.get('reason', '')}")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help="库路径（默认 env LG_DATABASE_URL → 本树 data/ → 主检出 data/）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"JSON 输出路径（默认 {DEFAULT_OUT}）")
    ap.add_argument("--md-out", default=str(ROOT / DOC_REL),
                    help="markdown 报告输出路径（默认本仓 docs/）")
    ap.add_argument("--print-only", action="store_true", help="只打印，不写任何文件")
    args = ap.parse_args(argv)

    if args.db:
        db = Path(args.db)
        if not db.is_file():
            print(f"[k5_sourcecheck_coverage] 指定的库不存在：{db}", file=sys.stderr)
            return 2
    else:
        db = default_db()
        if db is None:
            print("[k5_sourcecheck_coverage] 找不到库（LG_DATABASE_URL / 本树 data/ / "
                  f"{MAIN_REPO_DB} 均无）", file=sys.stderr)
            return 2

    consts = parse_constants()
    res = build_result(db, consts=consts)
    stdout_text = render_stdout(res)
    print(stdout_text, end="")
    if not args.print_only:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, sort_keys=True,
                                  indent=2) + "\n", encoding="utf-8")
        print(f"[k5_sourcecheck_coverage] 已写 {out.as_posix()}")
        md = Path(args.md_out)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(res, stdout_text), encoding="utf-8")
        print(f"[k5_sourcecheck_coverage] 已写 {md.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
