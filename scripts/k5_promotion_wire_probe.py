"""K5 晋升接线探针（lg-promotion-wiring-probe，2026-09-25 派工）。

复核报告 k5_freeze_adversarial_20260925.md §3.2 A4 的最小补齐动作：机械回答
「核验器 C3/P2 的输入（A 臂包）到底从哪来、晋升链断在哪一段、
knowledge_packages / strategy_conditions 的唯一写入路径是否存在、
35/48 过门对是否已在库」——只报事实与链断点，不产任何 status 变更建议，
严禁把「卡未晋升」当成放松判据的口径。

与 scripts/k5_criteria_check.py::promotion_gap_preflight 的分工：那条只答
「卡未晋升 / K3 过滤 / 不足证据」四态分流；本探针补它没答的三件事：
① C3/P2 消费字段的**产出方脚本与行号**（静态 grep + 动态真跑双证据）；
② 两张目标表的**每一处写入口**（文件:行号）与「无人工步骤可达性」；
③ 门到门五段链（status→K3 资格→conditions→packages→K4 preflight_world）
   的逐段非空读数——结论撞到第一条不满足的段就停下（不跳段猜）。

纪律（任务书硬约束）：
- 真库只读：sqlite3.connect("file:...?mode=ro", uri=True)；零 UPDATE/INSERT；
- 零模型调用、零 git 写操作；唯一允许的写 = --out 报告文件本身；
- 库不可读 → verdict=「证据不足」（不许猜）；文件缺 → 如实 None + 原因，
  不造半套数据。

用法：
    python scripts/k5_promotion_wire_probe.py                     # 缺省仓库根
    python scripts/k5_promotion_wire_probe.py --repo-root <主仓>   # 真库实测
    python scripts/k5_promotion_wire_probe.py --gate-ledger <门账本> \
       --ledger <遥测账本>                                        # 路径覆盖
    python scripts/k5_promotion_wire_probe.py --out ""            # 只打印不落盘
  报告缺省落 F:/Hermes/team/reports/k5_promotion_wire_probe_20260925.json。

账本口径（2026-09-26 修正，本派工核心）：探针原先把
F:/Hermes/team/judge/k2pairs_ledger.jsonl 写死成「旁路账本」——那是
scripts/k2_pairs_gen.py 的**写手调用遥测**（event=writer_call/cache_hit，
无 gates_ok/pair_id/persist_outcome），拿它数门 ⇒ 恒得 n_gates_ok=0，
把 A4 判死是「证据链断在探针自己身上」。现探针同时认两种账本并按 schema
分类（a4.ledger.kind = gate / writer_telemetry / unknown）：只有门账本
（k2_contrast_extract.write_pairs_ledger 产物，缺省 <repo>/k2_pairs.jsonl）
参与 n_gates_ok/n_written 统计；遥测账本如实标注、绝不参与门统计；
门账本找不到 → n_gates_ok=null + 「不猜」文案，不产出「过门数=0」断言。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 工作树根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PROBE = "k5_promotion_wire_probe"
DEFAULT_OUT = "F:/Hermes/team/reports/k5_promotion_wire_probe_20260925.json"
DEFAULT_PAIRS_REL = "_pairs/k2_pairs_20260924.json"    # 48 对成对对照文件
# 写手调用遥测账本（k2_pairs_gen.py 追加 event=writer_call/cache_hit 行；
# 与门是否通过无关，绝不参与门统计）——即旧版误当成旁路账本的硬编码路径
DEFAULT_TELEMETRY_LEDGER = "F:/Hermes/team/judge/k2pairs_ledger.jsonl"
# 真门账本（k2_contrast_extract.write_pairs_ledger 产物）：仓内真实缺省名
# k2_pairs.jsonl（见 k2_contrast_extract.py --pairs-ledger 缺省，live 跑时
# 相对仓库根落盘）
DEFAULT_GATE_LEDGER_REL = "k2_pairs.jsonl"
A4_REQUIRED_PAIRS = 48
A4_REQUIRED_PASSES = 35
PAIRED_PROTOCOL = "paired_contrast_v2"
VERDICTS = ("卡未晋升", "K3 过滤", "写入路径缺失", "证据不足")
# 两类账本的 schema 判别标记（现场读取的键集，非臆造）：
#   门账本行 = ledger_entry()：pair_id/…/gates_ok/gate_results/persist_outcome
#   遥测行   = k2_pairs_gen：event/op/segment_id/writer_model/n_chars_ai/ts
#              或 {event:"cache_hit",…,cache}
GATE_LEDGER_MARKERS = ("pair_id", "gates_ok", "persist_outcome")
TELEMETRY_LEDGER_MARKERS = ("event", "writer_model", "cache", "n_chars_ai")


def portable_path(path, repo_root: Path) -> str | None:
    """绝对路径 → 相对仓库根的可移植写法（仓外路径不内嵌盘符）。"""
    if path is None:
        return None
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        return f"<outside-repo>/{p.name}"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        ).isoformat(timespec="seconds")


def _grep_lines(text: str, pattern: str) -> list[int]:
    rx = re.compile(pattern)
    return [i for i, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):    # ValueError ⊃ UnicodeDecodeError
        return None


# ------------------------------------------------ ① C3/P2 输入源（静态）
# 消费方（k5_criteria_check.py）逐字段的机械定位：字段名 → 消费点正则
CONSUMER_SPECS = [
    ("artifacts.packages（包列表本体）", r'packages\s*=\s*a\.get\("packages"\)'),
    ("artifacts.packages[].n_techniques", r'get\("n_techniques"'),
    ("artifacts.packages[].arm", r'get\("arm"\)'),
    ("artifacts.packages（场数=len(packages)）", r'pkg_scenes\s*=\s*len\(packages\)'),
]
# 产出侧链条：谁生产收据里的 packages，谁供给它的上游
UPSTREAM_SPECS = [
    ("收据 packages 落盘点", "scripts/k4_paired_scenes.py",
     r'four\["packages"\]\.append\('),
    ("A 臂包计算入口（import）", "scripts/k4_paired_scenes.py",
     r'import frozen_package_for_scene|frozen_package_for_scene\('),
    ("收据 JSON 落盘", "scripts/k4_paired_scenes.py", r'"k4_paired\.json"'),
    ("K3 查询实现（query_knowledge 定义）", "app/knowledge_query.py",
     r'^def query_knowledge'),
    ("K3 资格集合定义（单源）", "app/knowledge_query.py",
     r'^ELIGIBLE_STATUS\b|^ELIGIBLE_OBSERVATION'),
    ("K3-B 包写入实现（freeze_package 定义）", "app/knowledge_query.py",
     r'^def freeze_package'),
]


def c3_p2_static(repo_root: Path) -> dict:
    """静态证据：C3/P2 消费的字段名逐个列出，并标出产出方脚本与行号
    （行号是当场 grep 出来的，不复算即漂移）。"""
    consumer_rel = "scripts/k5_criteria_check.py"
    consumer = repo_root / consumer_rel
    text = _read(consumer)
    consumed = []
    for field, pat in CONSUMER_SPECS:
        consumed.append({"field": field,
                         "consumed_at": f"{consumer_rel}:{_grep_lines(text, pat)}"
                                        if text is not None else None,
                         "lines": _grep_lines(text, pat) if text is not None
                         else []})
    upstream = []
    for hop, rel, pat in UPSTREAM_SPECS:
        t = _read(repo_root / rel)
        upstream.append({"hop": hop, "file": rel,
                         "lines": _grep_lines(t, pat) if t is not None else [],
                         "file_exists": t is not None})
    # 收据里 packages[] 条目实际携带的键（从 append 块现场抽取，不手抄）
    keys_at_append: list[str] = []
    producer = _read(repo_root / "scripts/k4_paired_scenes.py")
    if producer is not None:
        lines = producer.splitlines()
        for ln in _grep_lines(producer, r'four\["packages"\]\.append\('):
            block = "\n".join(lines[ln - 1:ln + 7])
            for m in re.finditer(r'"(\w+)":', block):
                if m.group(1) not in keys_at_append:
                    keys_at_append.append(m.group(1))
    return {"consumer_file": consumer_rel, "consumer_exists": text is not None,
            "consumed_fields": consumed,
            "receipt_package_keys": keys_at_append,
            "upstream_chain": upstream}


# ------------------------------------------------ ① C3/P2 输入源（动态）
def c3_p2_dynamic(repo_root: Path, artifact: Path | None = None) -> dict:
    """真跑一次 k5_criteria_check.build_report(<缺省>)：把 criteria 里
    C3/P2 两条的 satisfied / missing 原样落盘（不摘录不改写）。"""
    out: dict = {"artifact_used": None, "ok": False, "criteria": {},
                 "promotion_gap_verdict": None, "error": None}
    try:
        import k5_criteria_check as k5c
        art = Path(artifact) if artifact else k5c.latest_k4_artifact(repo_root)
        report = k5c.build_report(art, k5c.has_ten_scene_artifact(repo_root),
                                  repo_root)
        for c in report.get("criteria") or []:
            if c.get("id") in ("C3", "P2"):
                out["criteria"][c["id"]] = c
        out["promotion_gap_verdict"] = (
            (report.get("promotion_gap_preflight") or {})
            .get("conclusion") or {}).get("verdict")
        out["artifact_used"] = portable_path(art, repo_root)
        out["ok"] = {"C3", "P2"} <= set(out["criteria"])
    except Exception as exc:                    # 探针不许崩：失败即事实
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ------------------------------------------------ ② 写入路径盘点
TABLE_WRITER_PATTERNS = {
    "knowledge_packages": [
        (r'\binsert\s+into\s+knowledge_packages\b', "sql_insert"),
        (r'(?<!\w)KnowledgePackage\(', "orm_construct"),
        (r'(?<!\w)freeze_package\(', "session_writer_call"),
    ],
    "strategy_conditions": [
        (r'\binsert\s+into\s+strategy_conditions\b', "sql_insert"),
        (r'(?<!\w)StrategyCondition\(', "orm_construct"),
    ],
}
HUMAN_GATE_TOKENS = (r'K4_ALLOW_LIVE', r'LG_LIVE', r'K2CONTRAST_ALLOW_LIVE',
                     r'--live', r'freeze=a\.live', r'freeze=True')


def _iter_py(repo_root: Path):
    skip_dirs = {".git", "__pycache__", ".pytest_cache", "node_modules"}
    for p in sorted(repo_root.rglob("*.py")):
        if any(part in skip_dirs or part.startswith("out_k4_")
               for part in p.relative_to(repo_root).parts[:-1]):
            continue
        yield p


def scan_writers(repo_root: Path) -> dict:
    """全仓 grep：knowledge_packages / strategy_conditions 的每一处
    INSERT/写入口（文件:行号），逐处判定「现有脚本能否在无人工步骤下
    产出非空行」。判定口径（机械）：
      - tests/ 下的命中 = 测试脚手架写，不构成生产路径；
      - app/models.py 里的 class 行 = 表声明，不是写入；
      - 非测试写入点若同文件带人工闸门 token（--live、K4_ALLOW_LIVE 等）
        或 freeze 只随 live 打开 → gated_by_human=true；
      - ORM 类 KnowledgePackage 若该文件从 scene_runtime/contracts 导入
        （运行契约对象，非 ORM 行）→ 判「不写库」。"""
    tables: dict[str, list] = {t: [] for t in TABLE_WRITER_PATTERNS}
    for path in _iter_py(repo_root):
        rel = portable_path(path, repo_root) or path.name
        text = _read(path)
        if text is None:
            continue
        is_test = rel.startswith("tests/")
        contract_only = False
        if "KnowledgePackage(" in text:
            m = re.search(r'from\s+([\w.]+)\s+import\s*\(?'
                          r'[^)]*KnowledgePackage', text)
            contract_only = bool(m and ("scene_runtime" in m.group(1)
                                        or "contracts" in m.group(1)))
        for table, specs in TABLE_WRITER_PATTERNS.items():
            for pat, kind in specs:
                for ln in _grep_lines(text, pat):
                    line = text.splitlines()[ln - 1]
                    if "class " in line.split(table)[0] or \
                            re.match(r'\s*class\s+\w+\(', line):
                        continue              # ORM 表声明行不算写入点
                    gated = [t for t in HUMAN_GATE_TOKENS
                             if re.search(t, text)] if not is_test else []
                    hits_contract = (contract_only and
                                     "KnowledgePackage(" in line)
                    if is_test:
                        role, can, basis = ("test_scaffold", False,
                                            "测试脚手架写，不构成生产路径")
                    elif hits_contract:
                        role, can, basis = ("runtime_contract", False,
                                            "scene_runtime 契约对象（内存包），"
                                            "不写 knowledge_packages 表")
                    elif gated:
                        role, can, basis = ("gated_writer", False,
                                            "同文件存在人工闸门 "
                                            f"{[g for g in gated]}——"
                                            "无授权/无 live 即零写")
                    elif kind == "session_writer_call" and \
                            re.match(r'\s*def\s+freeze_package', line):
                        role, can, basis = ("library_writer", False,
                                            "函数定义本身不是触发方；可达性由"
                                            "调用点判定（本表逐调用点列行号）")
                    else:
                        role, can, basis = ("candidate_writer", False,
                                            "非测试写入点；可达性未证实——"
                                            "其触发链若含 live/授权步骤则仍为人工")
                    tables[table].append({"hit": f"{rel}:{ln}", "kind": kind,
                                          "role": role,
                                          "produces_rows_without_human": can,
                                          "basis": basis})
    per_table = {}
    for table, hits in tables.items():
        per_table[table] = {
            "hits": hits,
            "reachable_without_human": any(
                h["produces_rows_without_human"] for h in hits),
        }
    overall = all(v["reachable_without_human"] for v in per_table.values())
    return {"tables": per_table, "reachable_without_human": overall,
            "note": "可达=false 指「现无脚本能在无人工步骤（live 双闸授权/"
                    "runtime 真跑）下产出非空行」；本探针据此只报断点，"
                    "不提任何 status/判据变更建议"}


# ------------------------------------------------ ③ 门到门链断点
def _open_ro(db_path: Path) -> tuple[sqlite3.Connection | None, str | None]:
    if not db_path.exists():
        return None, "库文件不存在（mode=ro 打开前即缺）"
    try:
        return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
                               uri=True), None
    except sqlite3.Error as exc:
        return None, f"mode=ro 打开失败：{type(exc).__name__}"


def _count(con: sqlite3.Connection, sql: str) -> tuple[int | None, str | None]:
    try:
        return con.execute(sql).fetchone()[0], None
    except sqlite3.Error as exc:
        return None, f"{type(exc).__name__}: {exc}"


def chain_segments(db_path: Path, repo_root: Path,
                   paired_rows_db: int | None) -> dict:
    """五段链逐段给「当前是否有非空行 / 下一段需要什么输入」。
    结论：撞到第一条不满足的段就停下（不跳段猜）；库不可读=证据不足。"""
    seg_defs = [
        ("expression_strategies_v2.status（卡状态行）",
         "K3 资格集合筛出的可服务卡（status∈verified 且 observation∈"
         "observed/replicated）"),
        ("K3 eligible_statuses（可服务卡）",
         "strategy_conditions 条件行（由 K3 条件管道对可服务卡物化）"),
        ("strategy_conditions",
         "knowledge_packages 包行（K3-B freeze_package 冻结写入）"),
        ("knowledge_packages",
         "K4 preflight_world 输入：登记世界 + 35/48 过门成对对照在库"
         "（喂两席判定→晋升链）"),
        ("K4 preflight_world（过门对是否已在库）", "A4：过门对喂晋升链"),
    ]
    con, err = _open_ro(db_path)
    segments: list[dict] = []
    readings: dict = {}
    if con is not None:
        try:
            readings["cards"], e1 = _count(
                con, "SELECT COUNT(*) FROM expression_strategies_v2")
            dist = {}
            if readings["cards"]:
                try:
                    dist = {st: n for st, n in con.execute(
                        "SELECT status, COUNT(*) FROM expression_strategies_v2"
                        " GROUP BY status").fetchall()}
                except sqlite3.Error:
                    dist = {}
            readings["status_dist"] = dist
            n_eligible = None
            try:
                from app import knowledge_query as _kq
                st_set = ",".join(f"'{s}'" for s in sorted(
                    set(_kq.ELIGIBLE_STATUS)))
                ob_set = ",".join(f"'{s}'" for s in sorted(
                    set(_kq.ELIGIBLE_OBSERVATION)))
                n_eligible, _ = _count(
                    con, "SELECT COUNT(*) FROM expression_strategies_v2 "
                    f"WHERE status IN ({st_set}) AND observation_status "
                    f"IN ({ob_set})")
            except Exception:
                n_eligible = None
            readings["eligible_cards"] = n_eligible
            readings["conditions"], e3 = _count(
                con, "SELECT COUNT(*) FROM strategy_conditions")
            readings["packages"], e4 = _count(
                con, "SELECT COUNT(*) FROM knowledge_packages")
            readings["table_errors"] = {k: v for k, v in {
                "expression_strategies_v2": e1, "strategy_conditions": e3,
                "knowledge_packages": e4}.items() if v}
        finally:
            con.close()
    readings["paired_contrast_rows_in_db"] = paired_rows_db

    def _nonempty(key: str) -> bool | None:
        v = readings.get(key)
        return None if v is None else bool(v)

    seg_predicates = [
        (_nonempty("cards"),
         "库内一张卡都没有——链无从谈起"),
        (_nonempty("eligible_cards"),
         "无 status∈verified 且 observation∈observed/replicated 的卡——"
         "晋升链断在「卡未晋升」，与 K3 过滤无关"),
        (_nonempty("conditions"),
         "有可服务卡而 strategy_conditions 零行——条件物化段断链"),
        (_nonempty("packages"),
         "有条件行而 knowledge_packages 零行——包冻结写路段断链"
         "（写入路径盘点见 writers 节）"),
        (((paired_rows_db is not None
           and paired_rows_db >= A4_REQUIRED_PASSES)
          if paired_rows_db is not None else None),
         f"过门成对对照在库行数 <{A4_REQUIRED_PASSES}（或不可核）——"
         "A4 的喂料不存在"),
    ]
    fail_verdicts = ["证据不足", "卡未晋升", "K3 过滤", "写入路径缺失",
                     "证据不足"]
    verdict, reason, stopped_at = None, None, None
    for i, ((name, next_req), (passed, why), verdict_word) in enumerate(
            zip(seg_defs, seg_predicates, fail_verdicts), 1):
        key = ("cards", "eligible_cards", "conditions", "packages",
               "paired_contrast_rows_in_db")[i - 1]
        seg = {"n": i, "name": name, "reading": readings.get(key),
               "nonempty": None if readings.get(key) is None
               else bool(readings.get(key)),
               "next_stage_requires": next_req,
               "passed": None if con is None else passed}
        segments.append(seg)
        if con is not None and verdict is None:
            stopped_at = name
            if passed is False:
                verdict, reason = verdict_word, why
            elif passed is None:
                # 读数取不到＝没证据，不许把「不可核」折叠成任何缺口态
                verdict, reason = ("证据不足",
                                   f"『{name}』段读数不可取（见 readings/"
                                   "table_errors）——不许猜")
    if con is None:
        verdict, reason, stopped_at = ("证据不足", f"真库不可读（{err}）"
                                       "——不许猜", seg_defs[0][0])
    if verdict is None:                       # 全段非空：没撞断点也不硬套
        verdict, reason = ("证据不足", "五段读数均非空——本探针口径内未撞到"
                           "断点；这不构成『链已接通』的通过性结论，需主控复收")
    return {"db_path": portable_path(db_path, repo_root),
            "read_mode": "ro", "db_available": con is not None,
            "db_error": err, "readings": readings, "segments": segments,
            "conclusion": {"verdict": verdict, "reason": reason,
                           "stopped_at_segment": stopped_at}}


# ------------------------------------------------ ④ A4 可行性
def _parse_pairs_file(path: Path) -> tuple[int | None, str | None]:
    text = _read(path)
    if text is None:
        return None, "文件不存在/不可读"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"JSON 解析失败：{exc}"
    if isinstance(data, list):
        return len(data), None
    if isinstance(data, dict):
        for k in ("pairs", "items", "data"):
            if isinstance(data.get(k), list):
                return len(data[k]), None
    return None, "schema 未识别（不猜对数）"


def _classify_ledger_rows(rows: list) -> str:
    """按行键集机械判账本类型：gate（门账本）/ writer_telemetry（写手遥测）
    / unknown。判别只看 schema 标记，**不看有没有行**——「有行就算门账本」
    正是本派工要修的错（遥测账本行数最多，恒把门统计污染成 0）。"""
    def _hit(r, markers):
        return isinstance(r, dict) and any(m in r for m in markers)
    n_gate = sum(1 for r in rows if _hit(r, GATE_LEDGER_MARKERS))
    n_tele = sum(1 for r in rows
                 if _hit(r, TELEMETRY_LEDGER_MARKERS)
                 and not _hit(r, GATE_LEDGER_MARKERS))
    if n_gate:
        # 混档（门行+遥测行同档）也判 gate：门统计只数带 gates_ok/
        # persist_outcome 的行，遥测行不构成门证据（见 _parse_ledger）
        return "gate"
    if n_tele:
        return "writer_telemetry"
    return "unknown"


def _parse_ledger(path: Path) -> dict:
    out = {"path": str(path), "exists": path.exists(), "n_rows": None,
           "n_gates_ok": None, "n_written": None, "kind": "unknown",
           "schema_keys": [], "error": None, "note": None}
    if not out["exists"]:
        out["error"] = "账本文件不存在（不猜数字）"
        return out
    try:
        rows = [json.loads(l) for l in
                path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError) as exc:
        out["error"] = f"读取失败：{type(exc).__name__}"
        return out
    out["n_rows"] = len(rows)
    keys: set = set()
    for r in rows:
        if isinstance(r, dict):
            keys |= set(r)
    out["schema_keys"] = sorted(keys)
    out["kind"] = _classify_ledger_rows(rows)
    if out["kind"] == "gate":
        out["n_gates_ok"] = sum(1 for r in rows
                                if isinstance(r, dict)
                                and r.get("gates_ok") is True)
        out["n_written"] = sum(1 for r in rows
                               if isinstance(r, dict)
                               and r.get("persist_outcome") == "written")
    elif out["kind"] == "writer_telemetry":
        out["note"] = ("写手调用遥测账本（k2_pairs_gen 的 writer_call/"
                       "cache_hit 事件计数）——与门是否通过无关，不参与"
                       "门统计；n_gates_ok/n_written 保持 null（不猜）")
    elif not rows:
        out["error"] = "账本为空行集，schema 无从判定（不猜）"
    else:
        out["error"] = ("schema 未识别：既无门账本标记（pair_id/gates_ok/"
                        "persist_outcome）也无遥测标记（event/writer_model）"
                        "——不猜门统计数字")
    return out


def a4_feasibility(db_path: Path, repo_root: Path, pairs_file: Path,
                   ledger_file: Path,
                   gate_ledger_file: Path | None = None) -> dict:
    """A4 前提的机械核对：35/48 过门对是否**已经存在于库中**。
    真跑 SQL：strategy_instances(extractor_model=paired_contrast_v2) 行数、
    v2 卡在 strategy_reviews/judge_runs 的席位读数；文件侧数 pairs 文件
    与账本（门账本与写手遥测账本**同时**认，按 schema 分类，只有门账本
    参与 n_gates_ok/n_written；找不到门账本 → null+「不猜」文案，绝不
    产出「过门数=0」断言）。**不得**据此产任何 status 变更建议。"""
    pf = {"path": portable_path(pairs_file, repo_root) if pairs_file.exists()
          else str(pairs_file),
          "exists": pairs_file.exists()}
    pf["n_pairs"], pf["error"] = _parse_pairs_file(pairs_file)
    ledgers = [_parse_ledger(ledger_file)]
    if gate_ledger_file is not None and Path(gate_ledger_file) != ledger_file:
        ledgers.append(_parse_ledger(gate_ledger_file))
    ledger = next((l for l in ledgers if l["kind"] == "gate"), ledgers[0])
    db: dict = {"strategy_instances_paired_rows": None, "by_status": None,
                "v2_card_reviews": None, "judge_runs_total": None,
                "errors": {}}
    con, err = _open_ro(db_path)
    if con is not None:
        try:
            try:
                db["by_status"] = {st: n for st, n in con.execute(
                    "SELECT status, COUNT(*) FROM strategy_instances "
                    "WHERE extractor_model=? GROUP BY status",
                    (PAIRED_PROTOCOL,)).fetchall()}
                db["strategy_instances_paired_rows"] = sum(
                    db["by_status"].values())
            except sqlite3.Error as exc:
                db["errors"]["strategy_instances"] = str(exc)
            try:
                db["v2_card_reviews"], _ = _count(con,
                    "SELECT COUNT(*) FROM strategy_reviews sr JOIN "
                    "expression_strategies_v2 e ON sr.strategy_id=e.id "
                    "WHERE e.strategy_key LIKE 'v2:%'")
            except sqlite3.Error as exc:
                db["errors"]["strategy_reviews"] = str(exc)
            try:
                db["judge_runs_total"], _ = _count(
                    con, "SELECT COUNT(*) FROM judge_runs")
            except sqlite3.Error as exc:
                db["errors"]["judge_runs"] = str(exc)
        finally:
            con.close()
    else:
        db["error"] = err
    missing: list[str] = []
    n_pairs = pf.get("n_pairs")
    n_db = db["strategy_instances_paired_rows"]
    if ledger["kind"] == "writer_telemetry":
        missing.append(
            f"探针未找到门账本：已读 {ledger['path']} 是写手调用遥测账本"
            f"（schema_keys={ledger['schema_keys']}），与门是否通过无关——"
            "过门对数不可核（n_gates_ok=null，不猜；请用 --gate-ledger "
            "指定 k2_contrast_extract 产物）")
    elif ledger["kind"] != "gate":
        missing.append(f"门账本不可核（{ledger.get('error') or 'schema 未识别'}）"
                       "——n_gates_ok=null，不猜数字")
    if n_pairs is None:
        missing.append(f"pairs 文件对数不可核（{pf.get('error')}）")
    elif n_pairs < A4_REQUIRED_PAIRS:
        missing.append(f"pairs 文件仅 {n_pairs} 对 <{A4_REQUIRED_PAIRS}")
    if n_db is None:
        missing.append(f"过门对在库行数不可核（{db.get('error') or '表读失败'}）")
    elif n_db < A4_REQUIRED_PASSES:
        missing.append(f"过门成对对照落库行数={n_db} <{A4_REQUIRED_PASSES}"
                       "（35/48 只存在于账本口径，未入库为行）")
    return {"pairs_file": pf, "ledger": ledger, "all_ledgers": ledgers,
            "db": db,
            "thresholds": {"required_pairs": A4_REQUIRED_PAIRS,
                           "required_gate_passed_in_db": A4_REQUIRED_PASSES},
            "a4_feasible_now": (n_pairs is not None and n_db is not None
                                and n_pairs >= A4_REQUIRED_PAIRS
                                and n_db >= A4_REQUIRED_PASSES),
            "missing": missing,
            "advice_status_change": "none（任务硬约束：本探针只报事实与"
                                    "链断点，不产任何 status 变更建议）"}


# ------------------------------------------------ 组装
def run_probe(repo_root: Path, db_path: Path | None = None,
              artifact: Path | None = None, pairs_file: Path | None = None,
              ledger_file: Path | None = None,
              gate_ledger_file: Path | None = None) -> dict:
    repo_root = Path(repo_root)
    db_path = Path(db_path) if db_path else repo_root / "data" / \
        "language_genome.db"
    pairs_file = Path(pairs_file) if pairs_file else \
        repo_root / DEFAULT_PAIRS_REL
    # ledger_file 槽缺省＝写手遥测账本（旧版硬编码处）；门账本另按仓内
    # 真实缺省名 <repo>/k2_pairs.jsonl 找——两路同时认，kind 由 schema 判
    ledger_file = Path(ledger_file) if ledger_file else \
        Path(DEFAULT_TELEMETRY_LEDGER)
    gate_file = Path(gate_ledger_file) if gate_ledger_file else \
        repo_root / DEFAULT_GATE_LEDGER_REL
    a4 = a4_feasibility(db_path, repo_root, pairs_file, ledger_file,
                        gate_file)
    chain = chain_segments(db_path, repo_root,
                           a4["db"]["strategy_instances_paired_rows"])
    return {
        "probe": PROBE,
        "task": "lg-promotion-wiring-probe（A4 最小补齐：只报事实与链断点）",
        "generated_at": _now_iso(),
        "repo_root": portable_path(repo_root, repo_root),
        "db_path": portable_path(db_path, repo_root),
        "db_available": chain["db_available"],
        "c3_p2_inputs": {"static": c3_p2_static(repo_root),
                         "dynamic": c3_p2_dynamic(repo_root, artifact)},
        "writers": scan_writers(repo_root),
        "chain": chain,
        "a4": a4,
        "discipline": {"db_mode": "ro", "model_calls": 0, "git_writes": 0,
                       "verdict_vocabulary": list(VERDICTS),
                       "no_status_change_advice": True},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(ROOT),
                    help="仓库根（真库/_pairs/脚本所在；对其只读）")
    ap.add_argument("--db", default="", help="库路径覆盖（缺省 <repo>/data/"
                    "language_genome.db）")
    ap.add_argument("--k4-artifact", default="", dest="k4_artifact",
                    help="C3/P2 动态段用的三场收据（缺省自动取最新）")
    ap.add_argument("--pairs-file", default="", help="48 对 pairs 文件覆盖")
    ap.add_argument("--ledger", default="",
                    help="旁路账本覆盖（缺省=写手遥测账本 "
                    f"{DEFAULT_TELEMETRY_LEDGER}；按 schema 分类，遥测行"
                    "绝不参与门统计）")
    ap.add_argument("--gate-ledger", default="", dest="gate_ledger",
                    help="门账本覆盖（k2_contrast_extract.write_pairs_ledger "
                    f"产物）；缺省 <repo>/{DEFAULT_GATE_LEDGER_REL}")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help='报告落盘路径；""＝只打印不落盘')
    a = ap.parse_args()
    report = run_probe(
        Path(a.repo_root), Path(a.db) if a.db else None,
        Path(a.k4_artifact) if a.k4_artifact else None,
        Path(a.pairs_file) if a.pairs_file else None,
        Path(a.ledger) if a.ledger else None,
        Path(a.gate_ledger) if a.gate_ledger else None)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if a.out:
        out_path = Path(a.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"[k5_promotion_wire_probe] 报告已写 {out_path}",
                  file=sys.stderr)
        except OSError as exc:
            print(f"[k5_promotion_wire_probe] 警告：报告落盘失败（{exc}）"
                  "——JSON 已完整打印，不构成失败", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
