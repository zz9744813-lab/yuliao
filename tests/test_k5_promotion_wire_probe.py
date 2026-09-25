"""K5 晋升接线探针回归（scripts/k5_promotion_wire_probe.py，全离线）。

红线（任务书钉死，逐条对号）：
① 库不可读 → verdict「证据不足」——不许猜、不许造半套读数；
② 四态分流各有用例：卡未晋升 / K3 过滤 / 写入路径缺失 / 证据不足，
   全部用 tmp_path 造小 sqlite 构造；
③ JSON 键集稳定（键缺失即红）——顶层与二级契约键逐个点名；
④ 只读性：探针跑完后目标库文件的**字节内容与 mtime 均未变**，
   且 data/ 目录不冒出新文件（-wal/-shm 等）。
另钉：第一条不满足的段就停（多段皆断时只报最早段，不许跳段）；
静态段字段-产出方行号可复算；写入路径盘点把 tests/ 命中判为脚手架；
A4 段机械数 pairs/账本/库行数，且不含任何 status 变更建议。
"""
from __future__ import annotations

import importlib.util as _u
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k5p", ROOT / "scripts" / "k5_promotion_wire_probe.py")
k5p = _u.module_from_spec(_spec)
sys.modules["k5p"] = k5p
_spec.loader.exec_module(k5p)

# 顶层契约键（③：任一缺失即红）
TOP_KEYS = {"probe", "task", "generated_at", "repo_root", "db_path",
            "db_available", "c3_p2_inputs", "writers", "chain", "a4",
            "discipline"}
SECOND_KEYS = {
    "c3_p2_inputs": {"static", "dynamic"},
    "writers": {"tables", "reachable_without_human", "note"},
    "chain": {"db_path", "read_mode", "db_available", "db_error",
              "readings", "segments", "conclusion"},
    "a4": {"pairs_file", "ledger", "db", "thresholds", "a4_feasible_now",
           "missing", "advice_status_change"},
    "discipline": {"db_mode", "model_calls", "git_writes",
                   "verdict_vocabulary", "no_status_change_advice"},
}
CONCLUSION_KEYS = {"verdict", "reason", "stopped_at_segment"}


def _mk_db(tmp_path: Path, cards=(), n_packages=0, n_conditions=0,
           paired_rows=0, paired_status="proposed"):
    """离线假库（raw sqlite，最小列集，与 test_k5_criteria_check 同构口径）：
    cards=[(key,status,observation)]；paired_rows=strategy_instances 里
    extractor_model=paired_contrast_v2 的行数。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)   # parents=True：允许 tmp_path/<子目录> 形式的假库根
    db = data / "language_genome.db"
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE expression_strategies_v2(
      id TEXT, strategy_key TEXT, version INTEGER, status TEXT,
      scope TEXT, observation_status TEXT);
    CREATE TABLE strategy_reviews(
      id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT,
      judge_kind TEXT, reviewer_model TEXT, verdict TEXT);
    CREATE TABLE knowledge_packages(id TEXT);
    CREATE TABLE strategy_conditions(id TEXT);
    CREATE TABLE strategy_instances(
      id TEXT, extractor_model TEXT, status TEXT);
    """)
    for i, (key, status, obs) in enumerate(cards):
        con.execute("INSERT INTO expression_strategies_v2 VALUES (?,?,?,?,?,?)",
                    (f"ESV2-{i:03d}", key, 1, status, "UNCERTAIN", obs))
    for _ in range(n_packages):
        con.execute("INSERT INTO knowledge_packages VALUES ('KP-x')")
    for _ in range(n_conditions):
        con.execute("INSERT INTO strategy_conditions VALUES ('SC-x')")
    for i in range(paired_rows):
        con.execute("INSERT INTO strategy_instances VALUES (?,?,?)",
                    (f"SI-{i:03d}", "paired_contrast_v2", paired_status))
    con.commit()
    con.close()
    return tmp_path


def _artifact(tmp_path: Path):
    """离线三场收据夹具（A/B 各三包、全空 n_techniques）。"""
    pkgs = [{"scene": f"s{i}", "arm": arm, "n_techniques": 0}
            for i in (1, 2, 3) for arm in "AB"]
    p = tmp_path / "art.json"
    p.write_text(json.dumps({
        "live": True, "artifacts": {"prose": [], "packages": pkgs,
                                    "failures": [], "skipped": [],
                                    "receipts": []}}, ensure_ascii=False),
        encoding="utf-8")
    return p


def _probe(tmp_path: Path):
    art = _artifact(tmp_path) if (tmp_path / "data").exists() else None
    rep = k5p.run_probe(tmp_path, artifact=art,
                        ledger_file=tmp_path / "absent_ledger.jsonl")
    return rep


# ---------------------------------------------------------------- ① 缺库不猜
def test_db_unreadable_is_insufficient(tmp_path):
    """①真库不可读 → 「证据不足」：不许猜出任何缺口态，也不许产半套读数。"""
    rep = _probe(tmp_path)                       # tmp 下无 data/
    assert rep["db_available"] is False
    concl = rep["chain"]["conclusion"]
    assert concl["verdict"] == "证据不足"
    assert all(s["passed"] is None for s in rep["chain"]["segments"])
    assert rep["a4"]["a4_feasible_now"] is False
    assert any("不可核" in m for m in rep["a4"]["missing"])


# ---------------------------------------------------------------- ② 四态分流
def test_verdict_card_not_promoted(tmp_path):
    """②-1：八卡式全 hypothesis（无 verified）→『卡未晋升』，停在第 2 段。"""
    _mk_db(tmp_path, cards=[("卡甲", "hypothesis", "observed"),
                            ("卡乙", "hypothesis", "hypothesis")])
    rep = _probe(tmp_path)
    concl = rep["chain"]["conclusion"]
    assert concl["verdict"] == "卡未晋升"
    assert concl["stopped_at_segment"].startswith("K3 eligible_statuses")
    segs = rep["chain"]["segments"]
    assert segs[0]["passed"] is True and segs[1]["passed"] is False


def test_verdict_k3_filter(tmp_path):
    """②-2：有可服务卡而 strategy_conditions 零行 →『K3 过滤』。"""
    _mk_db(tmp_path, cards=[("卡甲", "verified", "observed")],
           n_conditions=0, n_packages=0)
    rep = _probe(tmp_path)
    concl = rep["chain"]["conclusion"]
    assert concl["verdict"] == "K3 过滤"
    assert rep["chain"]["readings"]["eligible_cards"] == 1


def test_verdict_write_path_missing(tmp_path):
    """②-3：条件行已有而 knowledge_packages 零行 →『写入路径缺失』。"""
    _mk_db(tmp_path, cards=[("卡甲", "verified", "observed")],
           n_conditions=2, n_packages=0)
    rep = _probe(tmp_path)
    concl = rep["chain"]["conclusion"]
    assert concl["verdict"] == "写入路径缺失"
    assert concl["stopped_at_segment"] == "knowledge_packages"


def test_verdict_insufficient_at_preflight_stage(tmp_path):
    """②-4：包行也有而过门对未入库（<35 行）→ 第 5 段不满足 →『证据不足』。"""
    _mk_db(tmp_path, cards=[("卡甲", "verified", "observed")],
           n_conditions=2, n_packages=1, paired_rows=0)
    rep = _probe(tmp_path)
    concl = rep["chain"]["conclusion"]
    assert concl["verdict"] == "证据不足"
    assert concl["stopped_at_segment"].startswith("K4 preflight_world")


def test_stop_at_first_failing_segment(tmp_path):
    """『撞到第一条不满足的段就停下』的反向验证锚：多段皆断时只许报最早段
    （若实现改成跳段/后段优先，本用例立即红）。"""
    _mk_db(tmp_path, cards=[("卡甲", "hypothesis", "hypothesis")],
           n_conditions=0, n_packages=0, paired_rows=0)
    rep = _probe(tmp_path)
    concl = rep["chain"]["conclusion"]
    assert concl["verdict"] == "卡未晋升"          # 而不是后三段的态
    assert concl["stopped_at_segment"].startswith("K3 eligible_statuses")


# ---------------------------------------------------------------- ③ 键集稳定
def test_json_key_contract(tmp_path):
    """③：顶层与二级契约键逐个点名（键缺失即红）；verdict 词表钉死四态。"""
    _mk_db(tmp_path, cards=[("卡甲", "verified", "observed")],
           n_conditions=1, n_packages=0)
    rep = _probe(tmp_path)
    assert TOP_KEYS <= set(rep)
    for section, keys in SECOND_KEYS.items():
        assert keys <= set(rep[section]), section
    assert CONCLUSION_KEYS <= set(rep["chain"]["conclusion"])
    assert set(rep["discipline"]["verdict_vocabulary"]) == {
        "卡未晋升", "K3 过滤", "写入路径缺失", "证据不足"}
    # 键形在「有库/无库」两种运行间也必须同构（缺库不许吞键）
    rep2 = k5p.run_probe(tmp_path / "nowhere",
                         ledger_file=tmp_path / "absent_ledger.jsonl")
    assert TOP_KEYS <= set(rep2)
    for section, keys in SECOND_KEYS.items():
        assert keys <= set(rep2[section]), section
    assert json.dumps(rep, ensure_ascii=False)      # 全 JSON 可序列化


# ---------------------------------------------------------------- ④ 只读性
def test_readonly_db_untouched(tmp_path):
    """④：探针跑完后目标库字节内容与 mtime 均未变，且 data/ 无新文件。"""
    _mk_db(tmp_path, cards=[("卡甲", "hypothesis", "observed")],
           n_conditions=3, n_packages=2, paired_rows=40)
    db = tmp_path / "data" / "language_genome.db"
    before_bytes = db.read_bytes()
    before_mtime = os.stat(db).st_mtime_ns
    before_listing = sorted(p.name for p in (tmp_path / "data").iterdir())
    _probe(tmp_path)
    assert db.read_bytes() == before_bytes
    assert os.stat(db).st_mtime_ns == before_mtime
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) \
        == before_listing


# ---------------------------------------------------------------- 静态段
def test_static_traces_consumed_fields_and_producers():
    """①静态证据（真仓）：C3/P2 消费字段与产出方行号可复算——
    patterns 漂移即红（消费点/上游链行号必须非空且为正整数行）。"""
    st = k5p.c3_p2_static(ROOT)
    assert st["consumer_exists"] is True
    by_field = {c["field"]: c for c in st["consumed_fields"]}
    for f in ("artifacts.packages（包列表本体）",
              "artifacts.packages[].n_techniques",
              "artifacts.packages[].arm"):
        assert by_field[f]["lines"], f"消费点 grep 落空：{f}"
    hops = {h["hop"]: h for h in st["upstream_chain"]}
    assert hops["收据 packages 落盘点"]["lines"], "packages 落盘点漂移"
    assert hops["K3 资格集合定义（单源）"]["lines"], "ELIGIBLE_* 定义漂移"
    assert hops["K3-B 包写入实现（freeze_package 定义）"]["lines"]
    # 收据 packages[] 携带的键现场抽取（不手抄）：arm/n_techniques 必须在
    assert {"arm", "n_techniques"} <= set(st["receipt_package_keys"])


# ---------------------------------------------------------------- 写入路径
def test_writers_inventory_classifies_hits():
    """②写入路径盘点（真仓 grep）：两表各有命中；tests/ 命中判为脚手架；
    非测试写入点全部 gated/未证实可达 → reachable_without_human=False。"""
    w = k5p.scan_writers(ROOT)
    for table in ("knowledge_packages", "strategy_conditions"):
        hits = w["tables"][table]["hits"]
        assert hits, f"{table} 一处写入口都没盘出来——grep 口径失效"
        assert any(h["role"] == "test_scaffold" for h in hits)
        assert w["tables"][table]["reachable_without_human"] is False
    assert w["reachable_without_human"] is False
    kp_hints = {h["hit"] for h in w["tables"]["knowledge_packages"]["hits"]}
    assert any("app/knowledge_query.py" in h for h in kp_hints), \
        "freeze_package（唯一库写实现）必须在盘点里"


# ---------------------------------------------------------------- A4 段
def test_a4_counts_pairs_ledger_and_db(tmp_path):
    """④：pairs 文件/旁路账本/库行数逐项真数；48+≥35 齐备才 feasible。"""
    pairs = [{"op": f"op{i}"} for i in range(48)]
    pf = tmp_path / "_pairs" / "k2_pairs_20260924.json"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"pairs": pairs}), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    lines = [{"gates_ok": True, "persist_outcome": "written"}] * 35 + \
            [{"gates_ok": False, "persist_outcome": "gated_out"}] * 13
    ledger.write_text("\n".join(json.dumps(r) for r in lines),
                      encoding="utf-8")
    # 库未入库（paired 行 0）→ false + missing 指到「未入库为行」
    _mk_db(tmp_path, cards=[("卡甲", "verified", "observed")])
    a4 = k5p.a4_feasibility(tmp_path / "data" / "language_genome.db",
                            tmp_path, pf, ledger)
    assert a4["pairs_file"]["n_pairs"] == 48
    assert a4["ledger"]["n_gates_ok"] == 35
    assert a4["ledger"]["n_written"] == 35
    assert a4["a4_feasible_now"] is False
    assert any("未入库为行" in m for m in a4["missing"])
    assert "status 变更建议" in a4["advice_status_change"] or \
        a4["advice_status_change"].startswith("none")
    # 过门对入库 40 行 ≥35 → feasible 翻转 True（判据方向可证伪）
    _mk_db(tmp_path / "sec", cards=[])            # 建第二个库（tmp 内）
    db2 = tmp_path / "sec" / "data" / "language_genome.db"
    con = sqlite3.connect(db2)
    for i in range(40):
        con.execute("INSERT INTO strategy_instances VALUES (?,?,?)",
                    (f"SI-{i}", "paired_contrast_v2", "proposed"))
    con.commit()
    con.close()
    a4b = k5p.a4_feasibility(db2, tmp_path, pf, ledger)
    assert a4b["db"]["strategy_instances_paired_rows"] == 40
    assert a4b["a4_feasible_now"] is True
    assert a4b["missing"] == []


def test_a4_files_absent_reported_not_guessed(tmp_path):
    """A4：pairs/账本文件缺位 → 如实 None + 原因，missing 指到「不可核」。"""
    _mk_db(tmp_path, cards=[])
    a4 = k5p.a4_feasibility(tmp_path / "data" / "language_genome.db",
                            tmp_path, tmp_path / "no.json",
                            tmp_path / "no.jsonl")
    assert a4["pairs_file"]["n_pairs"] is None
    assert a4["pairs_file"]["error"]
    assert a4["ledger"]["n_rows"] is None
    assert a4["a4_feasible_now"] is False


def test_main_out_empty_writes_nothing(tmp_path, capsys):
    """探针唯一允许的写=--out 报告；--out "" 时除 stdout 外零文件写出。"""
    _mk_db(tmp_path, cards=[("卡甲", "hypothesis", "observed")])
    before = sorted(p.relative_to(tmp_path).as_posix()
                    for p in tmp_path.rglob("*"))
    argv_backup = sys.argv
    sys.argv = ["k5_promotion_wire_probe.py", "--repo-root",
                str(tmp_path), "--out", ""]
    try:
        code = k5p.main()
    finally:
        sys.argv = argv_backup
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["probe"] == "k5_promotion_wire_probe"
    after = sorted(p.relative_to(tmp_path).as_posix()
                   for p in tmp_path.rglob("*"))
    assert after == before
