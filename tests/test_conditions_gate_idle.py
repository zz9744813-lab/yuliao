"""`scripts/conditions_gate_idle.py` 的回归钉（临时 sqlite 夹具，不碰真库）。

钉死的是**判据本身**，不是某一版真库读数：
① 夹具 `strategy_conditions` 空 ⇒ 每条策略 `is_idle=True`、`n_idle==n_strategies`、
   `total_condition_rows==0`（**钉死「空转」这个读数本身**——空转是读数，不是
   「条件都满足」，两者不许混读）；
② 插 3 行（1 required good_when + 1 非 required good_when + 1 bad_when）⇒
   各计数各管什么：`condition_rows==3` / `is_idle=False` / `required_rows==1` /
   `good_when_rows==1` / `bad_when_rows==1`（挤成一个数就测不出分桶）；
③ `neutral_when` 计入 `neutral_when_rows` 且**不进**两个可达上限
   （`_condition_pipeline` 里 neutral_when 显式 continue，不计正支持）；
④ 空转库的上限 = 0，与「有 3 行」夹具的 1 对照 ⇒ 上限是**算出来的**、
   不是写死的常量；
⑤ 确定性：同夹具库跑两次，JSON 逐字一致（`generated_at` 之外；固定 `now`
   时连时间戳一起逐字一致）；
⑥ 零写入：走 CLI 跑完夹具库分块 sha256 **逐字节不变**，目录里除
   `fixture.db`/报告文件外不外溢任何文件；
⑦ 只读承诺在连接层：正则 + AST 扫脚本源码，断言不出现写语句，且所有 SQL
   字面量只以 SELECT/PRAGMA 开头；
⑧ 跑不起来 ⇒ 返回码 2：库不存在 / 不是 sqlite 库 / 缺表，各自带原因，
   不抛未捕获异常、不在磁盘上留文件；
⑨ 复用口径而非另写：monkeypatch 掉 `app.knowledge_query` 的合格集判据返回值，
   脚本读数**必须随之变化** ⇒ 证明真的在用它，不是把值拷了一份。
外加：反向自检（有一行 ⇒ 必不空转）、BINARY 排序键写死、真库只读冒烟。
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import importlib.util as _u
import json
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

SCRIPT = ROOT / "scripts" / "conditions_gate_idle.py"
_spec = _u.spec_from_file_location("cgi", SCRIPT)
cgi = _u.module_from_spec(_spec)
_spec.loader.exec_module(cgi)

from app import db                                    # noqa: E402
from app.models import (ExpressionStrategyV2, StrategyCondition)  # noqa: E402

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)

# 夹具策略照抄真库现况：status 全 'hypothesis'（不在 eligible_statuses(1)
# = {"verified"} 内）、observation_status 'replicated'（在合格集内）⇒
# n_eligible=0 是**读出来的**结果，不是夹具摆出来的常量。
HYPOTHESIS = "hypothesis"
REPLICATED = "replicated"


def _fresh(tmp_path: Path, name: str = "fixture.db"):
    """独立夹具库：只建表；不用 app.db 的全局 engine，也不开 WAL（不产 -wal/-shm）。"""
    path = tmp_path / name
    eng = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    db.Base.metadata.create_all(eng)
    return path, sessionmaker(bind=eng, future=True, expire_on_commit=False)


def _seed_strategy(SM, key: str, status: str = HYPOTHESIS,
                   obs: str = REPLICATED) -> str:
    with SM() as s:
        st = ExpressionStrategyV2(
            strategy_key=key, abstract_operation=f"操作-{key}",
            effect_hypothesis=f"假设-{key}", status=status, scope="WORK",
            scope_ids=[], observation_status=obs)
        s.add(st)
        s.commit()
        return st.id


def _seed_conditions(SM, sid: str, specs) -> None:
    """specs: (kind, required, dimension) 列表。"""
    with SM() as s:
        for kind, required, dim in specs:
            s.add(StrategyCondition(strategy_id=sid, strategy_version=1,
                                    kind=kind, dimension=dim, operator="eq",
                                    value={}, required=required))
        s.commit()


def _row(rep: dict, key: str) -> dict:
    return [r for r in rep["strategies"] if r["strategy_key"] == key][0]


def _sha256(path: Path) -> str:
    """分块哈希：真库可能数百 MB，不整读进内存。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 23), b""):
            h.update(chunk)
    return h.hexdigest()


def _idle_fixture(tmp_path: Path):
    """条件表为空的夹具：三条策略，零条件行。"""
    path, SM = _fresh(tmp_path)
    ids = {k: _seed_strategy(SM, k) for k in ("k-a", "k-b", "k-c")}
    return path, SM, ids


# ───────────────────────────────────────────────────── ① 空转 = 读数本身
def test_empty_condition_table_means_every_strategy_idle(tmp_path):
    """`strategy_conditions` 空 ⇒ 每条策略 is_idle=True、n_idle==n_strategies、
    total_condition_rows==0，且两个可达上限都是 0（不是「条件都满足」）。"""
    path, SM, ids = _idle_fixture(tmp_path)
    rep = cgi.build_report(path, now=NOW)
    s = rep["summary"]
    assert [r["is_idle"] for r in rep["strategies"]] == [True] * 3
    assert s["n_strategies"] == 3
    assert s["n_idle"] == s["n_strategies"], "空转面没数全"
    assert s["n_with_rows"] == 0
    assert s["total_condition_rows"] == 0
    assert s["condition_rows_attributed"] == 0
    assert s["orphan_condition_rows"] == 0
    for r in rep["strategies"]:
        assert r["condition_rows"] == 0
        for k in ("required_rows", "good_when_rows", "bad_when_rows",
                  "neutral_when_rows", "other_kind_rows"):
            assert r[k] == 0, f"{k} 在空表上非 0：{r[k]}"
        # 空转 ⇒ 分量现状恒 0，且上限也是 0（没有行可抬起它）
        assert r["required_matches_now"] == 0 and r["good_when_matches_now"] == 0
        assert r["required_matches_max"] == 0 and r["good_when_matches_max"] == 0
    assert s["required_matches"]["max_reachable"] == 0
    assert s["good_when_matches"]["max_reachable"] == 0
    assert s["required_matches"]["current"] == 0
    # 拒绝理由不可达：没有 bad_when/required 行 ⇒ 这两类理由永不出现
    assert s["rejection_reasons_reachable"] == {
        "excluded_bad_when": False, "excluded_required_false": False,
        "excluded_required_unknown": False,
        "note": s["rejection_reasons_reachable"]["note"]}
    assert s["kind_rows"] == {"good_when_required": 0, "good_when_optional": 0,
                              "bad_when": 0, "neutral_when": 0, "other": 0}


# ───────────────────────────────────── ② 三个计数各管各的，不许挤成一个数
def test_three_rows_each_counter_keeps_its_own_meaning(tmp_path):
    """1 required good_when + 1 非 required good_when + 1 bad_when ⇒
    condition_rows=3 / required_rows=1 / good_when_rows=1 / bad_when_rows=1。"""
    path, SM, ids = _idle_fixture(tmp_path)
    _seed_conditions(SM, ids["k-a"], [
        ("good_when", True, "视角"),
        ("good_when", False, "节奏"),
        ("bad_when", False, "视角"),
    ])
    rep = cgi.build_report(path, now=NOW)
    row = _row(rep, "k-a")
    assert row["condition_rows"] == 3
    assert row["is_idle"] is False, "有行还说空转"
    assert row["required_rows"] == 1 and row["good_when_rows"] == 1
    assert row["bad_when_rows"] == 1
    assert row["neutral_when_rows"] == 0 and row["other_kind_rows"] == 0
    # required 与非 required 是**同一种 kind** 的两半，别被合并成一个数
    assert row["good_when_rows_total"] == 2
    # 两个分量各自只由自己的行抬起
    assert row["required_matches_max"] == row["required_rows"] == 1
    assert row["good_when_matches_max"] == row["good_when_rows"] == 1
    # 有行策略的现状不是静态读数 ⇒ null（不假造分量）
    assert row["required_matches_now"] is None
    assert row["good_when_matches_now"] is None
    # 同库另两条仍是空转：逐策略判定，不是一库一结论
    assert [_row(rep, k)["is_idle"] for k in ("k-b", "k-c")] == [True, True]
    s = rep["summary"]
    assert (s["n_idle"], s["n_with_rows"]) == (2, 1)
    assert s["total_condition_rows"] == 3 == s["condition_rows_attributed"]
    assert s["kind_rows"] == {"good_when_required": 1, "good_when_optional": 1,
                              "bad_when": 1, "neutral_when": 0, "other": 0}
    assert s["required_matches"]["max_reachable"] == 1
    assert s["good_when_matches"]["max_reachable"] == 1
    assert s["required_matches"]["current"] is None
    assert s["required_matches"]["idle_strategies_known_zero"] == 2
    assert s["required_matches"]["query_dependent_strategies"] == 1
    # 有了 bad_when/required 行 ⇒ 两类拒绝理由变成「可触达」
    assert s["rejection_reasons_reachable"]["excluded_bad_when"] is True
    assert s["rejection_reasons_reachable"]["excluded_required_false"] is True
    assert s["rejection_reasons_reachable"]["excluded_required_unknown"] is True


# ─────────────────────────────────────── ③ neutral_when 不计正支持
def test_neutral_when_counted_but_counts_toward_neither_cap(tmp_path):
    """neutral_when 行进 `neutral_when_rows`，不进任何可达上限
    （_condition_pipeline：非 good_when 一律 continue，neutral_when 不计正支持）。"""
    path, SM, ids = _idle_fixture(tmp_path)
    _seed_conditions(SM, ids["k-a"], [
        ("neutral_when", False, "人称"),
        ("neutral_when", True, "时态"),          # required=True 也不改口径
        ("good_when", True, "视角"),
    ])
    rep = cgi.build_report(path, now=NOW)
    row = _row(rep, "k-a")
    assert row["neutral_when_rows"] == 2, "neutral_when 行没被数进来"
    assert row["condition_rows"] == 3
    assert row["required_rows"] == 1 and row["good_when_rows"] == 0
    # 上限只认 good_when 两半 ⇒ 3 行里只有 1 行抬得起 required_matches
    assert row["required_matches_max"] == 1
    assert row["good_when_matches_max"] == 0
    s = rep["summary"]
    assert s["kind_rows"]["neutral_when"] == 2
    assert s["required_matches"]["max_reachable"] == 1
    assert s["good_when_matches"]["max_reachable"] == 0
    assert s["total_condition_rows"] == 3


# ───────────────────── ④ 上限是算出来的：空库 0 vs 有行库 1（同一段代码）
def test_caps_are_computed_not_hardcoded_constants(tmp_path):
    """同一套代码：空表夹具上限 0，插 3 行夹具 required 上限 1 ⇒ 上限随行数变。"""
    empty_dir = tmp_path / "empty"
    rows_dir = tmp_path / "rows"
    empty_dir.mkdir()
    rows_dir.mkdir()
    p_empty, SM1, ids1 = _idle_fixture(empty_dir)
    p_rows, SM2, ids2 = _idle_fixture(rows_dir)
    _seed_conditions(SM2, ids2["k-a"], [
        ("good_when", True, "视角"),
        ("good_when", False, "节奏"),
        ("bad_when", False, "视角"),
    ])
    a = cgi.build_report(p_empty, now=NOW)["summary"]
    b = cgi.build_report(p_rows, now=NOW)["summary"]
    assert (a["required_matches"]["max_reachable"],
            a["good_when_matches"]["max_reachable"]) == (0, 0)
    assert (b["required_matches"]["max_reachable"],
            b["good_when_matches"]["max_reachable"]) == (1, 1)
    # 对照的是同一个字段（不是两个各写死的数）
    assert a["required_matches"] != b["required_matches"]


# ──────────────────────────────────────────────── ⑤ 输出确定（逐字一致）
def test_two_runs_on_same_db_produce_identical_json(tmp_path):
    """同夹具库跑两次：`generated_at` 之外逐字一致；固定 `now` 时连它也一致。"""
    path, SM, ids = _idle_fixture(tmp_path)
    _seed_conditions(SM, ids["k-b"], [
        ("good_when", True, "视角"), ("good_when", False, "节奏"),
        ("bad_when", False, "视角"), ("neutral_when", False, "人称")])
    out1, out2 = tmp_path / "r1.json", tmp_path / "r2.json"
    assert cgi.main(["--db", str(path), "--out", str(out1)]) == 0
    assert cgi.main(["--db", str(path), "--out", str(out2)]) == 0
    d1, d2 = json.loads(out1.read_text(encoding="utf-8")), \
        json.loads(out2.read_text(encoding="utf-8"))
    d1.pop("generated_at"), d2.pop("generated_at")
    assert d1 == d2, "同库两次读数不同 ⇒ 有未写死的排序/取值来源"
    assert out1.read_text(encoding="utf-8").endswith("\n"), "报告缺末尾换行"
    assert "行" in out1.read_text(encoding="utf-8")   # ensure_ascii=False
    # 固定 now ⇒ 连时间戳一起逐字一致
    t1 = cgi.dump_json(cgi.build_report(path, now=NOW))
    t2 = cgi.dump_json(cgi.build_report(path, now=NOW))
    assert t1 == t2
    # 排序键写死：策略按 strategy_key → version → id
    assert cgi.criteria_row_sort_key() == \
        ["strategy_key", "version", "strategy_id"]


def test_row_sort_is_binary_on_strategy_key(tmp_path):
    """排序按 BINARY（逐码点）而非不区分大小写：'B' 排在 'a' 之前。"""
    path, SM = _fresh(tmp_path)
    for key in ("b-lower", "A-upper", "a-lower2", "B-upper2"):
        _seed_strategy(SM, key)
    rep = cgi.build_report(path, now=NOW)
    got = [r["strategy_key"] for r in rep["strategies"]]
    assert got == sorted(got), f"策略行未按写死的排序键排：{got}"
    assert got == ["A-upper", "B-upper2", "a-lower2", "b-lower"]
    assert rep["criteria"]["row_sort_order"] == "BINARY"
    assert rep["criteria"]["row_sort_key"] == \
        ["strategy_key", "version", "strategy_id"]


# ───────────────────────────────────────────────────── ⑥ 零写入
def test_cli_run_leaves_fixture_db_byte_identical(tmp_path):
    """走 CLI 跑完：夹具库分块 sha256 逐字节不变，目录不外溢副作用文件。"""
    path, SM, ids = _idle_fixture(tmp_path)
    _seed_conditions(SM, ids["k-a"], [("good_when", True, "视角"),
                                      ("bad_when", False, "节奏")])
    SM().bind.dispose()                          # 释放夹具写连接，只留只读句柄
    before = _sha256(path)
    bound = db.SessionLocal                      # 跑完必须还原成原绑定
    out = tmp_path / "report.json"
    rc = cgi.main(["--db", str(path), "--out", str(out)])
    assert rc == 0, "盘查跑完必须 rc=0（空转/有行都是读数，不是故障）"
    assert db.SessionLocal is bound, "SessionLocal 未还原——会污染后续用例"
    assert _sha256(path) == before, "只读承诺破了：夹具库文件被写过"
    assert [p.name for p in sorted(tmp_path.iterdir())] == \
        ["fixture.db", "report.json"], "跑出了 -wal/-shm 之类副作用文件"
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["readonly"] is True
    assert rep["open_mode"] == "sqlite uri mode=ro"
    assert rep["summary"]["total_condition_rows"] == 2
    assert rep["summary"]["n_with_rows"] == 1


# ────────────────────────────── ⑦ 只读承诺在连接层（源码静态扫）
_WRITE_SQL = (
    r"\bINSERT\s+INTO\b", r"\bUPDATE\s+[\"'`\[]?[A-Za-z_].*\bSET\b",
    r"\bDELETE\s+FROM\b", r"\bCREATE\s+(TABLE|INDEX|UNIQUE|TRIGGER|VIEW)\b",
    r"\bDROP\s+(TABLE|INDEX|TRIGGER|VIEW)\b", r"\bALTER\s+TABLE\b",
    r"\bREPLACE\s+INTO\b", r"\bUPSERT\b", r"\bVACUUM\b", r"\bATTACH\b",
    r"\bDETACH\b", r"\bBEGIN\b.?\s*\bTRANSACTION\b", r"\bPRAGMA\s+\w+\s*=",
)


def test_source_carries_no_write_statements():
    """正则 + AST 双扫脚本源码：无写语句、无建表/迁移入口、SQL 只以只读开头。

    「无建表/迁移入口」按**调用点**判（AST 扫 `Call.func`），不按裸文本判：
    脚本 docstring 里写着「不调 `db.init_db()`」这类**否定声明**，裸文本黑名单
    会把这份声明自己判成违规——检查必须只落在代码上，否则结论不可信。
    """
    src = SCRIPT.read_text(encoding="utf-8")
    for pat in _WRITE_SQL:
        m = re.search(pat, src, re.IGNORECASE)
        assert m is None, f"脚本源码出现写/改库语句 {pat}：{m.group(0)!r}"
    tree = ast.parse(src)
    called = {ast.unparse(n.func) for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    for banned in ("init_db", "create_all", "executescript", "commit",
                   "session", "SessionLocal", "sessionmaker", "create_engine",
                   "begin", "rollback"):
        assert not any(banned in c for c in called), \
            f"脚本调了 {banned}（调用集={sorted(called)}）"
    # 判据是「所有 SQL 字面量只读」，不靠关键词黑名单兜底
    verbs = re.compile(r"^\s*(SELECT|PRAGMA|WITH)\b", re.IGNORECASE)
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            if not re.match(r"^\s*(SELECT|PRAGMA|WITH|INSERT|UPDATE|DELETE|"
                            r"CREATE|DROP|ALTER|REPLACE)\b", text, re.I):
                continue
            checked += 1
            assert verbs.match(text), \
                f"非只读 SQL 字面量：{text[:60]!r}"
    assert checked >= 3, f"只扫到 {checked} 条 SQL 字面量，扫法失效"
    # 读数确实走 mode=ro URI 连接（连接层，不是靠自觉）
    assert "mode=ro" in src and "uri=True" in src
    assert "sqlite3.connect" in called or any(
        c.endswith("sqlite3.connect") for c in called), sorted(called)


# ───────────────────────────────── ⑧ 跑不起来 = 返回码 2，不留文件
def test_unrunnable_inputs_exit_2_without_creating_files(tmp_path):
    """库不存在 / 不是 sqlite 库 / 缺表 ⇒ 一律 rc=2，不抛未捕获异常、不留文件。"""
    assert cgi.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert list(tmp_path.iterdir()) == [], "库不存在时在磁盘上留了文件"

    junk = tmp_path / "junk.db"
    junk.write_text("this is not a sqlite database", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())
    assert cgi.main(["--db", str(junk)]) == 2, "非 sqlite 库未被拒（mode=ro 形同虚设）"
    assert sorted(p.name for p in tmp_path.iterdir()) == before

    import sqlite3
    hollow = tmp_path / "hollow.db"
    con = sqlite3.connect(str(hollow))
    con.execute("CREATE TABLE unrelated (id INTEGER)")
    con.commit()
    con.close()
    names_before = sorted(p.name for p in tmp_path.iterdir())
    assert cgi.main(["--db", str(hollow)]) == 2, "缺表未被拒"
    # 缺表那一跑也不许写库
    assert sorted(p.name for p in tmp_path.iterdir()) == names_before


def test_missing_criteria_source_exits_2(monkeypatch, tmp_path):
    """判据源导不进 ⇒ rc=2（不另写一套 status 兜底口径）。"""
    path, SM, ids = _idle_fixture(tmp_path)

    def _boom():
        raise RuntimeError("判据源 app.knowledge_query 导不进：模拟失败")

    monkeypatch.setattr(cgi, "load_knowledge_query", _boom)
    assert cgi.main(["--db", str(path)]) == 2
    with pytest.raises(RuntimeError):
        cgi.build_report(path, now=NOW)


# ────────────────────── ⑨ 复用既有判据（monkeypatch 换返回值，读数跟着变）
def test_reuses_knowledge_query_eligibility_criteria(monkeypatch, tmp_path):
    """真在用 app.knowledge_query 的判据：换掉它的返回值 ⇒ 本盘查器读数跟着变。

    三段：① 收紧 status 集（换掉 eligible_statuses）② 收紧 observation 集
    （换掉 ELIGIBLE_OBSERVATION）③ 默认口径下 n_eligible=0 且逐行分列
    status_eligible / observation_eligible——8 条 hypothesis 策略读出
    「status 不合格、observation 合格」，正是真库现况的形状。
    """
    from app import knowledge_query as KQ
    path, SM = _fresh(tmp_path)
    ids = {k: _seed_strategy(SM, k) for k in ("k-a", "k-b", "k-c")}
    _seed_strategy(SM, "k-verified", status="verified")
    base = cgi.build_report(path, now=NOW)
    # 夹具里 1 verified + 3 hypothesis ⇒ 只有 verified 那条合格
    assert base["summary"]["n_eligible"] == 1
    assert _row(base, "k-a")["eligible"] is False
    assert _row(base, "k-a")["status_eligible"] is False
    assert _row(base, "k-a")["observation_eligible"] is True
    assert _row(base, "k-verified")["eligible"] is True
    assert base["criteria"]["eligible_observation"] == \
        sorted(KQ.ELIGIBLE_OBSERVATION)
    assert base["criteria"]["eligible_status_sets"] == {"1": ["verified"]}
    # 复用位置（函数名 + 行号）在报告里，且行号是真读出来的
    names = {s["name"]: s["definition_line"] for s in base["reuse"]["symbols"]}
    assert set(names) == {"eligible_statuses", "ELIGIBLE_OBSERVATION",
                          "_condition_pipeline"}
    assert all(isinstance(v, int) and v > 0 for v in names.values()), names
    kq_src = Path(KQ.__file__).read_text(encoding="utf-8").splitlines()
    assert kq_src[names["eligible_statuses"] - 1].startswith(
        "def eligible_statuses("), "行号没对上 eligible_statuses 的定义行"
    assert kq_src[names["ELIGIBLE_OBSERVATION"] - 1].startswith(
        "ELIGIBLE_OBSERVATION ="), "行号没对上 ELIGIBLE_OBSERVATION 的定义行"
    assert base["reuse"]["source"] == "app/knowledge_query.py"

    # ① 换掉 eligible_statuses 的返回值（放宽到只含 hypothesis）⇒ 读数必须变：
    #    3 条 hypothesis 变合格，那条 verified 反而掉出合格集。
    monkeypatch.setattr(KQ, "eligible_statuses",
                        lambda version=None: frozenset({"hypothesis"}))
    widened = cgi.build_report(path, now=NOW)
    assert widened["summary"]["n_eligible"] == 3, "没用上游判据（换值不生效）"
    assert _row(widened, "k-a")["status_eligible"] is True
    assert _row(widened, "k-verified")["status_eligible"] is False
    assert widened["criteria"]["eligible_status_sets"] == {"1": ["hypothesis"]}
    # 空转读数不受判据影响（两件事分开）：idle 面原样
    assert widened["summary"]["n_idle"] == base["summary"]["n_idle"] == 4

    # ② 换掉 ELIGIBLE_OBSERVATION（收紧成空集）⇒ 合格集归零
    monkeypatch.undo()
    monkeypatch.setattr(KQ, "ELIGIBLE_OBSERVATION", frozenset())
    squeezed = cgi.build_report(path, now=NOW)
    assert squeezed["summary"]["n_eligible"] == 0
    assert squeezed["criteria"]["eligible_observation"] == []
    assert all(not r["observation_eligible"] for r in squeezed["strategies"])
    assert all(not r["eligible"] for r in squeezed["strategies"])
    assert squeezed["summary"]["n_idle"] == 4, "观察层收紧不该改动空转面"


# ─────────────────────────────────────────── 反向自检（必不红的那一例）
def test_reverse_self_check_non_idle_strategy_is_never_flagged_idle(tmp_path):
    """反向自检：插了行的策略必须报 `is_idle=False`、上限抬起来。

    只钉「空转报得出来」是不够的：一个「逢库必报空转」的坏工具照样全绿。
    本例要求同一套代码在**有行**的库上一例不红——空转与非空转两个方向都判对，
    红绿信号才算闭环。
    """
    path, SM, ids = _idle_fixture(tmp_path)
    _seed_conditions(SM, ids["k-c"], [("good_when", True, "视角")])
    rep = cgi.build_report(path, now=NOW)
    s = rep["summary"]
    assert s["n_idle"] == 2 and s["n_with_rows"] == 1
    assert s["n_strategies"] == 3
    assert s["total_condition_rows"] == 1
    row = _row(rep, "k-c")
    assert row["is_idle"] is False
    assert row["required_matches_max"] == 1 and row["good_when_matches_max"] == 0
    assert [_row(rep, k)["is_idle"] for k in ("k-a", "k-b")] == [True, True]
    assert s["required_matches"]["current"] is None
    assert s["required_matches"]["idle_strategies_known_zero"] == 2
    assert s["rejection_reasons_reachable"]["excluded_bad_when"] is False
    assert s["rejection_reasons_reachable"]["excluded_required_false"] is True
    line = cgi.summary_line(s)
    assert line == ("n_strategies=3 / n_idle=2 / n_with_rows=1 / "
                    "total_condition_rows=1 / n_eligible=0"), line
    assert "excluded_bad_when 可达=False" in cgi.impact_line(s)


def test_orphan_condition_rows_are_reported_not_silently_dropped(tmp_path):
    """条件行挂在库外 strategy_id 上 ⇒ 单独点名，不摊到任何策略头上。"""
    path, SM, ids = _idle_fixture(tmp_path)
    _seed_conditions(SM, "ESV2-not-in-v2", [("good_when", True, "视角"),
                                            ("bad_when", False, "节奏")])
    rep = cgi.build_report(path, now=NOW)
    s = rep["summary"]
    assert s["orphan_condition_rows"] == 2
    assert s["orphan_condition_strategy_ids"] == ["ESV2-not-in-v2"]
    assert s["total_condition_rows"] == 2
    assert s["condition_rows_attributed"] == 0, "孤儿行被摊到了策略头上"
    assert all(r["condition_rows"] == 0 for r in rep["strategies"])


# ────────────────────────────────────────────── 真库只读冒烟（如实 skip）
def test_real_db_readonly_smoke():
    """真库只读冒烟：mode=ro 连得上、跑得完、读数与直查一致、真库未被写过。

    真库不在/不可读 ⇒ skip 并带原因，**不伪造成通过**。这里钉的是「跑得通 +
    读数自洽（行数、桶数、idle 面三者互相对得上）」，不把「真库恰好 0 行」当
    永久断言——条件面一旦有数据（那是主控/集霸的裁定），本例仍应绿。
    """
    db_path = cgi.MAIN_CHECKOUT_DB
    if not Path(db_path).exists():
        pytest.skip(f"真库不可读：{db_path} 不存在（冒烟例如实 skip）")
    try:
        con = cgi.ro_connect(db_path)
        n_cond = cgi.read_total_condition_rows(con)
        n_strat = len(cgi.read_strategies(con))
        con.close()
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"真库 mode=ro 打开失败：{type(exc).__name__}: {exc}")
    p = Path(db_path)
    stat0, sha0 = p.stat(), _sha256(p)
    rep = cgi.build_report(p)
    stat1 = p.stat()
    if (stat0.st_size, stat0.st_mtime_ns) != (stat1.st_size, stat1.st_mtime_ns):
        pytest.skip("真库在本例运行期间被其它进程改动（size/mtime 变了）："
                    "本例全程只持 mode=ro 句柄，无从归因，skip 不伪造")
    assert _sha256(p) == sha0, "size/mtime 未变而内容哈希变了——只读承诺破了"
    s = rep["summary"]
    assert s["n_strategies"] == n_strat
    assert s["total_condition_rows"] == n_cond
    assert s["n_idle"] + s["n_with_rows"] == n_strat
    assert s["condition_rows_attributed"] + s["orphan_condition_rows"] == n_cond
    assert sum(s["kind_rows"].values()) == s["condition_rows_attributed"]
    assert s["required_matches"]["max_reachable"] == \
        s["kind_rows"]["good_when_required"]
    assert s["good_when_matches"]["max_reachable"] == \
        s["kind_rows"]["good_when_optional"]
    if n_cond == 0:
        # 派工背景的主控读数：条件面无数据 ⇒ 全库空转、上限 0
        # 注意：策略数是**库的真实读数**，会随 K 线增臂而变（曾写死 8，库里加到 10 后误红）。
        # 这里只钉「全库空转」这条关系 + 一个下限，不钉具体数字。
        assert s["n_idle"] == s["n_strategies"] >= 8
        assert s["required_matches"]["current"] == 0
