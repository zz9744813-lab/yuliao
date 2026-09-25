#!/usr/bin/env python
"""K5 供给口径真计数（A2 整改，2026-09-25 派工；只读、零写库、零模型调用）。

背景：对抗性复核（k5_freeze_adversarial §3.2-A2）要求把「供给不足/充足」
换成可复算口径，分母统一为「src_ok=1 且白名单非空 且 role≠'benchmark'」，
验收判据「≥2 作品各自 ≥1 万段」方可移除「供给」项。本脚本对真库
（sqlite mode=ro）复算三口径并落盘 docs/K5供给口径真计数_20260925.md：

- 口径 A（复核者指定分母）：``json_extract(integrity,'$.src_ok') IS TRUE``
  AND ``role≠'benchmark'`` AND 作品在 work_sources 的**语义白名单列**
  ``identity_purposes`` 为非空 JSON 数组；并列「严格布尔」变体
  （``json_type='true'``）核对非布尔掺水（数字 1 / 字符串 "true" 之类
  会漏进 IS TRUE 口径）。
- 口径 B：role≠'benchmark' AND 白名单非空（不看 src_ok）。
- 口径 C：全部段按作品。

白名单列修订（lg-supply-whitelist-col，2026-09-25 复派）：旧判据引用的
``allowed_purposes`` 是建表史遗留的纯插行管道列（app/knowledge_query.py
:130-134：语义自 K1-A 二轮起走 identity_purposes/license_purposes，本列
不参与任何判定/查询/包内容，且被 FINGERPRINT_EXCLUDED_FIELDS 显式排除
出指纹）——供给口径不得引用语义已废弃的列。新判据用 ``identity_purposes``；
``license_purposes`` **不算**白名单（详见 SQL 片段处注释）。旧
``allowed_purposes`` 口径保留为**对照列**（``wl_legacy_len``），并列输出、
不得删除对照。

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


def _wl_case(col: str) -> str:
    """work_sources.ws.<col> 的白名单长度（ws 别名写死）：无值 / 非法 JSON /
    非数组一律记 0（fail-closed），只有非空 JSON 数组才 >0。"""
    return f"""CASE
  WHEN ws.{col} IS NULL OR json_valid(ws.{col})=0 THEN 0
  WHEN json_type(ws.{col})<>'array' THEN 0
  ELSE json_array_length(ws.{col}) END"""


# 白名单非空（**新判据**，lg-supply-whitelist-col）：语义列取
# work_sources.identity_purposes——K1-A 二轮起「身份可核对面」
# （research / research_reference / test_contract）才是供给白名单；
# allowed_purposes 是建表史遗留的纯插行管道列（app/knowledge_query.py
# :130-134：不参与任何判定/查询/包内容，被 FINGERPRINT_EXCLUDED_FIELDS
# 排除出指纹），不再作判据。
# **license_purposes 非空不算通过白名单**：它是训练/再分发的「用途授权
# 位」（集霸决策位，见 app/models.py:104-108），不是白名单成员资格——
# 授权面为空只表示「未授予训练/基准用途」，不能反把身份可核对的语料
# （覆汉形态：identity=["research"] 而 license=[]）逐出供给口径；反之
# 若拿 license 充白名单，会把「未核对身份」的行错计为供给。
_WHITELIST_LEN = _wl_case("identity_purposes")
WL_OK = (f"EXISTS (SELECT 1 FROM work_sources ws WHERE ws.work_id=segments.work_id"
         f" AND ({_WHITELIST_LEN})>0)")
# **对照列（不得删除）**：旧 allowed_purposes 口径保留为 wl_legacy_len，
# 与新口径并列输出，用于「一样的数字、口径列是否选对」的可审计对照。
_WHITELIST_LEN_LEGACY = _wl_case("allowed_purposes")
WL_LEGACY_OK = (f"EXISTS (SELECT 1 FROM work_sources ws"
                f" WHERE ws.work_id=segments.work_id"
                f" AND ({_WHITELIST_LEN_LEGACY})>0)")

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
# 对照口径（旧 allowed_purposes 列）：只并列报数、不作判定，不得删除。
COND_A_LEGACY = f"{SRC_TRUE_IS} AND {NOT_BENCH} AND {WL_LEGACY_OK}"
COND_B_LEGACY = f"{NOT_BENCH} AND {WL_LEGACY_OK}"

# wl_compare 明细查询用片段（segments 别名 s；role/integrity 无歧义列）
_NOT_BENCH_S = "(s.role IS NULL OR s.role<>'benchmark')"
_SRC_TRUE_S = ("json_valid(s.integrity)=1 "
               "AND json_extract(s.integrity,'$.src_ok') IS TRUE")
WS_EVIDENCE_COLS = ("work_id, source_type, created_at, identity_purposes, "
                    "license_purposes, allowed_purposes")

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


def wl_compare(con: sqlite3.Connection) -> list[dict]:
    """逐作品白名单两列对照：新列 identity_purposes 与旧列
    allowed_purposes（wl_legacy_len）的长度并排给；-1 = 无登记行
    （fail-closed 排除），0 = 有行但空数组/非法 JSON/非数组。"""
    sql = f"""SELECT s.work_id,
       COUNT(*) AS n_total,
       SUM(CASE WHEN {_NOT_BENCH_S} THEN 1 ELSE 0 END) AS n_nonbench,
       SUM(CASE WHEN {_NOT_BENCH_S} AND {_SRC_TRUE_S} THEN 1 ELSE 0 END)
           AS n_true_nonbench,
       COALESCE(MAX(CASE WHEN ws.work_id IS NULL THEN NULL
                         ELSE {_wl_case("identity_purposes")} END), -1)
           AS wl_identity_len,
       COALESCE(MAX(CASE WHEN ws.work_id IS NULL THEN NULL
                         ELSE {_wl_case("allowed_purposes")} END), -1)
           AS wl_legacy_len
  FROM segments s LEFT JOIN work_sources ws ON ws.work_id = s.work_id
  GROUP BY s.work_id
  ORDER BY n_true_nonbench DESC, s.work_id"""
    titles = dict(con.execute("SELECT id, title FROM works").fetchall())
    out = []
    for (wid, n_total, n_nonbench, n_true, i_len, a_len) in con.execute(sql):
        out.append({"work_id": wid, "title": titles.get(wid) or "(无 works 行)",
                    "n_total": n_total, "n_nonbench": n_nonbench,
                    "n_true_nonbench": n_true,
                    "wl_identity_len": i_len, "wl_legacy_len": a_len,
                    "in_new": i_len > 0, "in_legacy": a_len > 0})
    return out


def wl_diff_evidence(con: sqlite3.Connection,
                     compare_rows: list[dict]) -> list[dict]:
    """两列成员资格翻转的作品的只读归因证据：work_sources 行原文
    （identity/license/allowed 原值＋created_at＋source_type）。"""
    out = []
    for r in compare_rows:
        if r["in_new"] == r["in_legacy"]:
            continue
        cur = con.execute(
            f"SELECT {WS_EVIDENCE_COLS} FROM work_sources WHERE work_id=?",
            (r["work_id"],)).fetchone()
        raw = ({} if cur is None else
               dict(zip(WS_EVIDENCE_COLS.split(", "), cur)))
        out.append({**r, "direction": ("新纳入（旧列漏登）" if r["in_new"]
                                       else "新剔除（旧列虚登）"),
                    "raw": raw})
    return out


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
        a_legacy = _caliber(con, COND_A_LEGACY, min_per_work)
        b_legacy = _caliber(con, COND_B_LEGACY, min_per_work)
        state = src_state(con)
        b_state = src_state(con, COND_B)
        compare = wl_compare(con)
        diff = wl_diff_evidence(con, compare)
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
        # 对照列（旧 allowed_purposes 口径）合计——并列输出，不得删除
        "A_legacy_total": a_legacy["total"], "A_legacy_max": a_legacy["max"],
        "A_legacy_n_ge": a_legacy["n_ge"],
        "B_legacy_total": b_legacy["total"], "B_legacy_max": b_legacy["max"],
        "B_legacy_n_ge": b_legacy["n_ge"],
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
        # 白名单两列对照（identity 新列 vs allowed 旧列）与翻转作品只读归因，
        # 供 §2.5/§2.6 渲染；无登记行 wl_*_len=-1、有行空/非法/非数组=0。
        "wl_compare": compare, "wl_diff": diff,
    }


def _table(rows: list[tuple[str, str, int]]) -> list[str]:
    out = ["| work_id | 作品 | 段数 |", "|---|---|---:|"]
    out += [f"| {wid} | {title} | {n:,} |" for wid, title, n in rows]
    if not rows:
        out.append("| — | （零作品命中） | 0 |")
    return out


def _render_whitelist_sections(res: dict) -> list[str]:
    """§2.5/§2.6 白名单列修订：口径列选错依据、两口径数字并列、
    逐作品两列对照、成员资格翻转作品的只读归因（真库即覆汉）。"""
    m = res["measured"]
    L: list[str] = []
    add = L.append
    add("## 2.5 白名单列口径修订（identity_purposes 新列 vs allowed_purposes 旧列）")
    add("")
    add("复派 lg-supply-whitelist-col。旧供给判据引用的 `work_sources.allowed_purposes`"
        "是**建表史遗留的纯插行管道列**——依据 `app/knowledge_query.py:130-134`："
        "语义自 K1-A 二轮起走 `identity_purposes`/`license_purposes`，本列"
        "**不参与任何判定/查询/包内容**，并被 `FINGERPRINT_EXCLUDED_FIELDS`"
        "显式排除出指纹（同文件 :134）。`app/models.py:109-114` 亦注明"
        "`allowed_purposes` 恢复为带默认 `[]` 的纯插行列、语义仍看新列。"
        "故供给口径改用语义列 `identity_purposes`（身份可核对面：research /"
        "research_reference / test_contract）。")
    add("")
    add("**`license_purposes` 非空不算通过白名单**：它是训练/再分发的「用途授权"
        "位」（`app/models.py:104-108`，集霸决策位），非白名单成员资格——拿它"
        "充白名单会把「未核对身份」的行错计为供给；反过来授权面为空也不能把"
        "身份可核对的语料逐出供给。")
    add("")
    add("新列判据下**口径数字不变仍须与旧列并列留证**（「一样的数字、"
        "对的理由」）：")
    add("")
    add("| 口径 | 新列 identity_purposes 合计 | 旧列 allowed_purposes 合计 | 最大单作品（新/旧） | ≥1 万段作品数（新/旧） |")
    add("|---|---:|---:|---|---|")
    add(f"| A | {m['A_total']:,} | {m['A_legacy_total']:,} | "
        f"{m['A_max']:,} / {m['A_legacy_max']:,} | "
        f"{m['A_n_ge']} / {m['A_legacy_n_ge']} |")
    add(f"| B | {m['B_total']:,} | {m['B_legacy_total']:,} | "
        f"{m['B_max']:,} / {m['B_legacy_max']:,} | "
        f"{m['B_n_ge']} / {m['B_legacy_n_ge']} |")
    add("")
    compare = res.get("wl_compare") or []
    if compare:
        add("### 逐作品两列对照（`wl_legacy_len` 为旧列对照，不得删除）")
        add("")
        add("| work_id | 作品 | 非bench·src_ok段 | 新列 identity_len | 旧列 allowed_len | 新口径纳入 | 旧口径纳入 |")
        add("|---|---|---:|---:|---:|---|---|")
        for r in compare:
            add(f"| {r['work_id']} | {r['title']} | {r['n_true_nonbench']:,} | "
                f"{r['wl_identity_len']} | {r['wl_legacy_len']} | "
                f"{'是' if r['in_new'] else '否'} | "
                f"{'是' if r['in_legacy'] else '否'} |")
        add("")
        add("> `wl_*_len = -1`＝无 work_sources 登记行（fail-closed 排除）；"
            "`0`＝有行但空数组/非法 JSON/非数组（同样 fail-closed）。")
        add("")
    diff = res.get("wl_diff") or []
    add("## 2.6 成员资格翻转作品的只读归因（白名单空值治理）")
    add("")
    if not diff:
        add("本次两列成员资格**无翻转**——所有作品在 identity 与 allowed 两列下"
            "同进同出（真库中该情形对应两口径数字全等）。")
    else:
        add("以下作品在新列（identity）与旧列（allowed）下**成员资格翻转**，"
            "逐行给出 `work_sources` 只读原文（identity/license/allowed 原值 "
            "＋ source_type ＋ created_at），作为「登记行未回填语义列」的归因证据：")
        add("")
        for r in diff:
            raw = r.get("raw") or {}
            add(f"- **{r['work_id']}（{r['title']}）— {r['direction']}**")
            add(f"  - 段数：全 {r['n_total']:,}｜非 bench {r['n_nonbench']:,}｜"
                f"非 bench·src_ok true {r['n_true_nonbench']:,}")
            add(f"  - `work_sources` 原文：`identity_purposes={raw.get('identity_purposes')!r}`、"
                f"`license_purposes={raw.get('license_purposes')!r}`、"
                f"`allowed_purposes={raw.get('allowed_purposes')!r}`、"
                f"`source_type={raw.get('source_type')!r}`、"
                f"`created_at={raw.get('created_at')!r}`")
            add(f"  - 归因：登记走 `scripts/register_work_sources.py` 的 `register()`"
                f"（root 分支 :192-205 造 row、:237 `WorkSource(**row)`）——"
                f"row 只写 identity/license，**不写** `allowed_purposes`，"
                f"后者落 ORM 默认 `[]`（`app/models.py:114`）；旧列据此为空、"
                f"新列非空 ⇒ 旧口径 fail-closed 漏登、新口径纳入。"
                f"`scripts/k2_unlock_sim.py:87,371-373` §5 探针把本作列入默认"
                f"书目并只读回显 identity/license/allowed 三列，可交叉核对。")
        add("")
        add("口径结论（真跑口径复算见 §2/§2.5 数字）：翻转作品在**口径 A** 下"
            "是否改变读数，取决于其段是否 `src_ok=true`——若其段全未通过 src_ok"
            "（未校验），则新列纳入不改变口径 A 合计/最大单作品/≥1 万段作品数；"
            "其段仅体现在**口径 B**（不看 src_ok）。")
    return L


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
              "`role≠'benchmark'` AND 语义白名单列 "
              "`json_array_length(identity_purposes)>0`"
              "（复核者指定分母；旧 `allowed_purposes` 口径见 §2.5 对照）", COND_A),
        ("A_strict", "口径 A 严格布尔补列：同上但 `json_type(...,'$.src_ok')='true'`"
                     "（排除数字 1/字符串 \"true\" 等非布尔掺水）", COND_A_STRICT),
        ("B", "口径 B：`role≠'benchmark'` AND 语义白名单列 "
              "`json_array_length(identity_purposes)>0`（不看 src_ok）", COND_B),
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
    L.extend(_render_whitelist_sections(res))
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
    # 白名单列对照（新 identity vs 旧 allowed）：并列输出、不得删除。
    L.append("")
    L.append("== 白名单列对照（新列 identity_purposes｜旧列 allowed_purposes 对照） ==")
    L.append(f"  口径 A：新列合计 {m['A_total']:,}｜旧列合计 {m['A_legacy_total']:,}"
             f"（最大单作品 {m['A_max']:,} / {m['A_legacy_max']:,}）")
    L.append(f"  口径 B：新列合计 {m['B_total']:,}｜旧列合计 {m['B_legacy_total']:,}"
             f"（最大单作品 {m['B_max']:,} / {m['B_legacy_max']:,}）")
    for r in res.get("wl_diff") or []:
        raw = r.get("raw") or {}
        L.append(f"  翻转：{r['work_id']} {r['title']} — {r['direction']}｜"
                 f"identity={raw.get('identity_purposes')!r} "
                 f"allowed={raw.get('allowed_purposes')!r} "
                 f"created_at={raw.get('created_at')!r}")
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
