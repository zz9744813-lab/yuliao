"""K5 供给口径真计数回归（scripts/k5_supply_recount.py，A2 整改）。

全部用 tmp_path 临时夹具库，真库零触碰、测试不依赖真库存在。钉死：
① 三口径 SQL 语义——构造 src_ok true/false/missing/loose/nojson、
   benchmark 角色、白名单空/非法/非数组/未登记 的组合，逐口径断言计数；
② 缺 src_ok 键的段**不得**被计入口径 A（防「missing 当 true」）；
③ 真库只读门：mode=ro 连接上任何写入必须抛错；
④ 白名单列修订（lg-supply-whitelist-col）：语义判据走 identity_purposes，
   旧 allowed_purposes 只作对照列并列输出（wl_legacy_len）——覆盖空 identity
   fail-closed、覆汉形态（identity 非空 / allowed 空 → 新纳入）、无登记行、
   非法 JSON / 非数组、license 非空不充白名单、新旧两列数字并列。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

_spec = _u.spec_from_file_location(
    "k5sr", ROOT / "scripts" / "k5_supply_recount.py")
k5sr = _u.module_from_spec(_spec)
sys.modules["k5sr"] = k5sr
_spec.loader.exec_module(k5sr)

# ── 夹具设计（期望值手算钉死）─────────────────────────────────────────
# 白名单语义列改为 identity_purposes（lg-supply-whitelist-col）；旧
# allowed_purposes 作对照列。为保持三口径既有期望值，令 identity 列的
# 成员资格与旧 allowed 完全同构（W1/W6 非空、W2 空、W4 非法、W5 非数组、
# W3 无登记行），故 A/B/C 计数不因换列而变——换列的**差异**用第二套夹具
# make_db_covhan（覆汉形态：identity 非空、allowed 空）单独钉。
# 口径 A（IS TRUE ∧ 非benchmark ∧ identity 白名单非空）命中 5 段：
#   s_t1 s_t2 s_l1(数字1漏入) s_w6a s_w6b
# 口径 A 严格布尔命中 4 段：s_t1 s_t2 s_w6a s_w6b（排除 s_l1）
# 口径 B（非benchmark ∧ identity 白名单非空）命中 12 段：W1 除 s_b1 外 10 段 + W6 2 段
# 口径 C（全部段）17 段：W1=11 W2=1 W3=1 W4=1 W5=1 W6=2
WORKS = [
    ("W1", "白名单齐全作品"),
    ("W2", "空白名单作品"),
    ("W3", "未登记作品"),
    ("W4", "白名单非法JSON作品"),
    ("W5", "白名单非数组作品"),
    ("W6", "达标第二作品"),
]
# (work_id, source_type, created_at, identity_purposes, license_purposes, allowed_purposes)
# identity/allowed 成员资格刻意同构，使既有 A/B/C 期望值不变。
WORK_SOURCES = [
    ("W1", "human_fiction", "2026-09-21T00:00:00Z",
     json.dumps(["research", "train"]), json.dumps([]),
     json.dumps(["train", "judge"])),
    ("W2", "human_fiction", "2026-09-21T00:00:00Z",
     json.dumps([]), json.dumps([]), json.dumps([])),        # 空 identity → 排除
    # W3 无登记行 → fail-closed 排除（wl_identity_len = -1）
    ("W4", "human_fiction", "2026-09-21T00:00:00Z",
     "not-json{{", json.dumps([]), json.dumps(["train"])),   # 非法 identity→排除；旧列非空→翻转
    ("W5", "human_fiction", "2026-09-21T00:00:00Z",
     json.dumps({"a": 1}), json.dumps([]),
     json.dumps(["train"])),                                  # 非数组 identity→排除
    ("W6", "human_fiction", "2026-09-21T00:00:00Z",
     json.dumps(["research"]), json.dumps([]),
     json.dumps(["train"])),
]
# (id, work_id, integrity, role)
SEGMENTS = [
    ("s_t1", "W1", '{"src_ok": true}', None),        # 完好（role NULL 视作非 bench）
    ("s_t2", "W1", '{"src_ok": true}', "train"),     # 完好
    ("s_f1", "W1", '{"src_ok": false}', None),       # 判坏
    ("s_m1", "W1", "{}", None),                      # 缺 src_ok 键 → 未校验
    ("s_m2", "W1", '{"lang_ok": true}', None),       # 缺 src_ok 键 → 未校验
    ("s_l1", "W1", '{"src_ok": 1}', None),           # 数字 1：漏入 IS TRUE，非严格 true
    ("s_l2", "W1", '{"src_ok": "true"}', None),      # 字符串 → loose
    ("s_l3", "W1", '{"src_ok": null}', None),        # null → loose
    ("s_n1", "W1", None, None),                      # integrity NULL → nojson
    ("s_n2", "W1", "not-json{{", None),              # 非法 JSON → nojson
    ("s_b1", "W1", '{"src_ok": true}', "benchmark"),  # benchmark：A/B 排除，C 计入
    ("s_w2", "W2", '{"src_ok": true}', None),        # 空 identity
    ("s_w3", "W3", '{"src_ok": true}', None),        # 无 work_sources 登记行
    ("s_w4", "W4", '{"src_ok": true}', None),        # identity 非法 JSON
    ("s_w5", "W5", '{"src_ok": true}', None),        # identity 非数组
    ("s_w6a", "W6", '{"src_ok": true}', None),
    ("s_w6b", "W6", '{"src_ok": true}', "train"),
]

MISSING_KEY_IDS = {"s_m1", "s_m2"}     # 合法 JSON 但缺键
NOJSON_IDS = {"s_n1", "s_n2"}          # NULL/非法 JSON
LOOSE_IDS = {"s_l1", "s_l2", "s_l3"}   # 键在但类型不严

_WS_DDL = """
    CREATE TABLE works(id TEXT PRIMARY KEY, title TEXT NOT NULL);
    CREATE TABLE work_sources(work_id TEXT PRIMARY KEY,
                              source_type TEXT,
                              created_at TEXT,
                              identity_purposes TEXT,
                              license_purposes TEXT,
                              allowed_purposes TEXT);
    CREATE TABLE segments(id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
                          ordinal INTEGER NOT NULL, text TEXT NOT NULL,
                          integrity TEXT, role TEXT);
"""
_WS_COLS = ("work_id, source_type, created_at, identity_purposes, "
            "license_purposes, allowed_purposes")
_SEG_SQL = ("INSERT INTO segments(id, work_id, ordinal, text, integrity, role) "
            "VALUES (?,?,?,?,?,?)")


def _insert_segments(con, segments):
    con.executemany(
        _SEG_SQL,
        [(sid, wid, i, "夹具段", ig, role)
         for i, (sid, wid, ig, role) in enumerate(segments)])


def _fresh(tmp_path, tag):
    # 同测试内多次建库互不冲突（tmp_path 复用）——序号 + tag 命名唯一
    _fresh._seq = getattr(_fresh, "_seq", 0) + 1
    p = tmp_path / f"fixture_{tag}_{_fresh._seq}.db"
    con = sqlite3.connect(p)
    con.executescript(_WS_DDL)
    return con, p


def make_db(tmp_path: Path) -> Path:
    con, p = _fresh(tmp_path, "supply")
    con.executemany("INSERT INTO works VALUES (?,?)", WORKS)
    con.executemany(f"INSERT INTO work_sources({_WS_COLS}) VALUES (?,?,?,?,?,?)",
                    WORK_SOURCES)
    _insert_segments(con, SEGMENTS)
    con.commit()
    con.close()
    return p


# 覆汉形态专用夹具：identity 非空、allowed 空（新纳入 / 旧列漏登）；
# 仅授权（license 非空、identity 空）仍不算白名单；空 identity fail-closed。
COVHAN_WORKS = [
    ("CH", "覆汉（榴弹怕水）"),     # identity=["research"]、allowed=[]
    ("LIC", "仅授权作品"),           # identity=[]、license=["training_source"]
    ("EMP", "空identity作品"),       # identity=[]
]
COVHAN_SOURCES = [
    ("CH", "production_nonbenchmark", "2026-09-23T00:00:00Z",
     json.dumps(["research"]), json.dumps([]), json.dumps([])),
    ("LIC", "human_fiction", "2026-09-23T00:00:00Z",
     json.dumps([]), json.dumps(["training_source"]), json.dumps([])),
    ("EMP", "human_fiction", "2026-09-23T00:00:00Z",
     json.dumps([]), json.dumps([]), json.dumps([])),
]
COVHAN_SEGMENTS = [
    ("c1", "CH", '{"src_ok": true}', "train"),
    ("c2", "CH", '{"src_ok": true}', None),
    ("l1", "LIC", '{"src_ok": true}', "train"),   # 授权非空但 identity 空 → 不入白名单
    ("e1", "EMP", '{"src_ok": true}', "train"),   # 空 identity → 排除
]


def make_db_covhan(tmp_path: Path) -> Path:
    con, p = _fresh(tmp_path, "covhan")
    con.executemany("INSERT INTO works VALUES (?,?)", COVHAN_WORKS)
    con.executemany(f"INSERT INTO work_sources({_WS_COLS}) VALUES (?,?,?,?,?,?)",
                    COVHAN_SOURCES)
    _insert_segments(con, COVHAN_SEGMENTS)
    con.commit()
    con.close()
    return p


def _result(tmp_path: Path, min_per_work: int = 2) -> dict:
    # k5doc_path=None：一致性核对列不在本测范围（真库文档由脚本 main 覆盖）
    return k5sr.build_result(make_db(tmp_path), min_per_work=min_per_work,
                             k5doc_path=None)


def _result_covhan(tmp_path: Path, min_per_work: int = 2) -> dict:
    return k5sr.build_result(make_db_covhan(tmp_path),
                             min_per_work=min_per_work, k5doc_path=None)


def _ids_where(db: Path, cond: str) -> set[str]:
    con = k5sr.open_ro(db)
    try:
        sql = "SELECT segments.id FROM segments"
        if cond:
            sql += f" WHERE {cond}"
        return {r[0] for r in con.execute(sql)}
    finally:
        con.close()



# ── ① 三口径计数语义 ─────────────────────────────────────────────────
def test_three_caliber_counts(tmp_path):
    m = _result(tmp_path)["measured"]
    assert m["A_total"] == 5 and m["A_max"] == 3
    assert m["A_strict_total"] == 4          # 数字 1 不计入严格布尔
    assert m["B_total"] == 12 and m["B_max"] == 10
    assert m["total"] == 17 and m["C_max"] == 11   # 口径 C = 全部段
    # 最大单作品逐表核对
    res = _result(tmp_path)
    assert [(w, n) for w, _, n in res["calibers"]["A"]["by_work"]] == \
        [("W1", 3), ("W6", 2)]
    assert [(w, n) for w, _, n in res["calibers"]["C"]["by_work"]][:2] == \
        [("W1", 11), ("W6", 2)]


def test_n_ge_and_verdict(tmp_path):
    # 阈值 2：W1/W6 各 ≥2 段 → 达标作品数 2、判据成立
    res = _result(tmp_path, min_per_work=2)
    assert res["measured"]["A_n_ge"] == 2
    assert res["verdict_ok"] is True
    # 阈值 1 万：夹具量级必然不达标
    res2 = _result(tmp_path, min_per_work=10_000)
    assert res2["measured"]["A_n_ge"] == 0
    assert res2["verdict_ok"] is False


# ── ② 缺 src_ok 键不得计入 A（防 missing 当 true）───────────────────
def test_missing_src_ok_not_in_A(tmp_path):
    db = make_db(tmp_path)
    a_ids = _ids_where(db, k5sr.COND_A)
    assert a_ids == {"s_t1", "s_t2", "s_l1", "s_w6a", "s_w6b"}
    # 缺键 / integrity 缺失或非法 / 字符串与 null 值 / benchmark，一律出 A
    assert not a_ids & (MISSING_KEY_IDS | NOJSON_IDS | {"s_l2", "s_l3", "s_b1"})
    assert a_ids & (MISSING_KEY_IDS | NOJSON_IDS) == set()


def test_strict_boolean_excludes_numeric_leak(tmp_path):
    db = make_db(tmp_path)
    strict = _ids_where(db, k5sr.COND_A_STRICT)
    assert strict == {"s_t1", "s_t2", "s_w6a", "s_w6b"}
    assert "s_l1" not in strict              # 数字 1 是「未校验」，非真校验
    m = _result(tmp_path)["measured"]
    assert m["A_total"] - m["A_strict_total"] == 1


def test_benchmark_and_whitelist_fail_closed(tmp_path):
    db = make_db(tmp_path)
    b_ids = _ids_where(db, k5sr.COND_B)
    assert "s_b1" not in b_ids               # benchmark 出 B（也出 A）
    assert not b_ids & {"s_w2", "s_w3", "s_w4", "s_w5"}   # 白名单缺/空/非法/非数组
    c_ids = _ids_where(db, k5sr.COND_C)      # 口径 C 无过滤，全都在
    assert {"s_b1", "s_w2", "s_w3", "s_w4", "s_w5"} <= c_ids
    assert len(c_ids) == len(SEGMENTS)


def test_src_state_five_way_classification(tmp_path):
    res = _result(tmp_path)
    assert res["state"] == {"true": 9, "false": 1, "missing": 2,
                            "loose": 3, "nojson": 2}
    m = res["measured"]
    assert m["key_exists"] == 13             # true+false+loose（被真实写过的键）
    assert m["missing_total"] == 4           # missing+nojson → 「未校验」主体
    assert m["B_unverified"] == 7            # B 口径内未 true/false 判定的段


# ── ③ 只读门 ─────────────────────────────────────────────────────────
def test_ro_connection_rejects_writes(tmp_path):
    con = k5sr.open_ro(make_db(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO works VALUES ('ZX', '写入门钉死')")
        con.commit()
    con.close()


# ── 报告渲染 / CLI（不触真库、不写仓内文档）──────────────────────────
def test_markdown_has_required_strings(tmp_path):
    res = _result(tmp_path, min_per_work=10_000)
    md = k5sr.render_markdown(res)
    for needle in ("口径 A", "src_ok", "未校验", "不成立"):
        assert needle in md
    # 三口径逐作品表原样在文档里（非结论摘录）
    assert "### 2.1 口径 A" in md and "### 2.3 口径 B" in md \
        and "### 2.4 口径 C" in md
    assert "| s_t1 |" not in md              # 逐作品是聚合表，不是段清单


def test_stdout_prints_all_three_calibers(tmp_path):
    out = k5sr.render_stdout(_result(tmp_path))
    for needle in ("== 口径 A", "== 口径 B", "== 口径 C",
                   "W1", "W6", "合计", "成立"):
        assert needle in out


def test_cli_exit0_writes_doc_to_tmp(tmp_path, capsys):
    db = make_db(tmp_path)
    outdoc = tmp_path / "out" / "recount.md"
    rc = k5sr.main(["--db", str(db), "--min-per-work", "2",
                    "--out-doc", str(outdoc)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "== 口径 A" in captured and "== 口径 C" in captured
    text = outdoc.read_text(encoding="utf-8")
    assert "口径 A" in text and "src_ok" in text and "未校验" in text
    assert "成立" in text                     # 阈值 2 时夹具判「成立」


def test_cli_print_only_does_not_write(tmp_path, capsys):
    db = make_db(tmp_path)
    outdoc = tmp_path / "should_not_exist.md"
    rc = k5sr.main(["--db", str(db), "--min-per-work", "2",
                    "--out-doc", str(outdoc), "--print-only"])
    assert rc == 0
    assert not outdoc.exists()


def test_cli_missing_db_returns_2(tmp_path, capsys):
    assert k5sr.main(["--db", str(tmp_path / "nope.db")]) == 2


def test_find_supply_number_lines():
    text = "合计 4,220 段\n供给不足\n无数字行\n最大单作品876段\ntokens=2,167"
    assert k5sr.find_supply_number_lines(text) == [
        "合计 4,220 段", "供给不足", "最大单作品876段"]


# ── ④ 白名单列修订（lg-supply-whitelist-col：identity_purposes 新判据）────
# 判据列从语义废弃的 allowed_purposes 换成 identity_purposes；旧列只作
# 对照列 wl_legacy_len 并列输出。以下逐条钉死两列语义差异。

# ①：登记行存在但 identity_purposes 为空数组 → fail-closed 排除
def test_empty_identity_fail_closed(tmp_path):
    db = make_db(tmp_path)
    b_ids = _ids_where(db, k5sr.COND_B)
    assert "s_w2" not in b_ids               # W2 有登记行、identity=[]
    cmp = {r["work_id"]: r for r in _result(tmp_path)["wl_compare"]}
    assert cmp["W2"]["wl_identity_len"] == 0        # 有行但空数组 → 0（非 -1）
    assert cmp["W2"]["in_new"] is False


# ②：identity_purposes 非空、allowed_purposes 为空（覆汉形态）→ 新口径纳入、旧口径漏登
def test_covhan_form_included_in_new_excluded_in_legacy(tmp_path):
    db = make_db_covhan(tmp_path)
    a_new = _ids_where(db, k5sr.COND_A)
    a_legacy = _ids_where(db, k5sr.COND_A_LEGACY)
    assert {"c1", "c2"} <= a_new            # 覆汉形态：identity=["research"] → 纳入
    assert a_legacy == set()                # 旧列 allowed=[] → fail-closed 漏登


# ③：作品无 work_sources 登记行 → 仍被排除（wl_identity_len = -1）
def test_no_registration_row_excluded(tmp_path):
    db = make_db(tmp_path)
    b_ids = _ids_where(db, k5sr.COND_B)
    assert "s_w3" not in b_ids
    cmp = {r["work_id"]: r for r in _result(tmp_path)["wl_compare"]}
    assert cmp["W3"]["wl_identity_len"] == -1       # 无登记行
    assert cmp["W3"]["in_new"] is False


# ④：identity_purposes 为非法 JSON / 非数组 → fail-closed
def test_illegal_or_nonarray_identity_fail_closed(tmp_path):
    db = make_db(tmp_path)
    a_ids = _ids_where(db, k5sr.COND_A)
    assert not a_ids & {"s_w4", "s_w5"}             # 非法 JSON / 非数组，皆出 A
    cmp = {r["work_id"]: r for r in _result(tmp_path)["wl_compare"]}
    assert cmp["W4"]["wl_identity_len"] == 0        # 非法 JSON → 0
    assert cmp["W5"]["wl_identity_len"] == 0        # 非数组 → 0
    assert cmp["W4"]["in_new"] is False and cmp["W4"]["in_legacy"] is True


# ④b：license_purposes 非空**不算**通过白名单（identity 空即排除）
def test_license_only_not_whitelist(tmp_path):
    db = make_db_covhan(tmp_path)
    b_ids = _ids_where(db, k5sr.COND_B)
    assert b_ids == {"c1", "c2"}                    # 仅 identity 非空的 CH 入白名单
    assert not b_ids & {"l1", "e1"}                 # LIC：license 非空但 identity 空 → 排除


# ⑤：新口径合计与旧口径合计**并列输出**（两列都在），对照列不得删除
def test_two_caliber_totals_output_in_parallel(tmp_path):
    m = _result_covhan(tmp_path)["measured"]
    assert m["A_total"] == 2 and m["A_legacy_total"] == 0   # 新纳入覆汉、旧列漏登
    assert m["B_total"] == 2 and m["B_legacy_total"] == 0
    for k in ("A_legacy_total", "A_legacy_max", "A_legacy_n_ge",
              "B_legacy_total", "B_legacy_max", "B_legacy_n_ge"):
        assert k in m                               # 旧列对照键齐全


# ⑤b：wl_compare 逐作品两列并列 + wl_diff 翻转归因（覆汉形态）
def test_wl_compare_has_both_columns_and_covhan_flip(tmp_path):
    res = _result_covhan(tmp_path)
    cmp = {r["work_id"]: r for r in res["wl_compare"]}
    assert {"wl_identity_len", "wl_legacy_len"} <= set(cmp["CH"].keys())
    assert cmp["CH"]["wl_identity_len"] == 1 and cmp["CH"]["wl_legacy_len"] == 0
    diff = res["wl_diff"]
    assert [d["work_id"] for d in diff] == ["CH"]
    assert "新纳入" in diff[0]["direction"]
    assert diff[0]["raw"]["identity_purposes"] == '["research"]'   # 归因：原文新列非空
    assert diff[0]["raw"]["allowed_purposes"] == "[]"              # 旧列原文空数组


# ⑤c：render_markdown 落出新列判据 + 两口径对照 + 翻转归因
def test_render_markdown_has_whitelist_revision_section(tmp_path):
    md = k5sr.render_markdown(_result_covhan(tmp_path, min_per_work=10_000))
    assert "白名单列口径修订" in md                  # §2.5 标题
    assert "语义白名单列" in md                      # 口径 A/B 标题已改述新列
    assert "wl_legacy_len" in md                    # 对照列名在文档
    assert "identity_purposes" in md                # 新判据列名在文档
    assert "成员资格翻转" in md and "新纳入" in md   # §2.6 覆汉治理证据

