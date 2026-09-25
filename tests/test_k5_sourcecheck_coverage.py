"""K5 源校验覆盖盘查回归（scripts/k5_sourcecheck_coverage.py，任务 lg-sourcecheck-coverage）。

全部用 tmp_path 临时夹具库离线跑：真库零触碰、不连网络、零模型调用。钉死：
① 严格布尔三态（true/false/缺键）各归其位；
② 数字 1 / 字符串 "true" / 数字 0 等**类型不严值不得算作已校验**
   （json_type 严格口径 vs `IS TRUE` 松口径的差值单独可测——source_check.py
   ::parse_src_ok 三态纪律的 SQL 镜像）；
③ integrity 为 NULL / 非法 JSON 的段 fail-closed 记「未校验」；
④ 逐作品排序稳定：同输入两次输出逐字节一致，并列按 work_id 升序；
⑤ 只读性：build_result 跑完目标库字节内容与 mtime 均不变，ro 连接写必抛；
⑥ 代价估算为纯算术且全部输入常量带行号取自 scripts/source_check.py 源码；
⑦ 口径影响、verdict 三分支（可校验/不可校验/证据不足）。
"""
from __future__ import annotations

import hashlib
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
    "k5sc", ROOT / "scripts" / "k5_sourcecheck_coverage.py")
k5sc = _u.module_from_spec(_spec)
sys.modules["k5sc"] = k5sc
_spec.loader.exec_module(k5sc)

LONG = "这是一段足够长的夹具中文文本用来把长度闸门的干扰排除掉"  # ≥10 字，无错字表命中


def _seg(sid, work, ig, role=None, sv=1, text=LONG, clean=LONG):
    return (sid, work, ig, role, sv, text, clean)


# (id, work_id, integrity, role, seg_version, text, text_clean)
SEGMENTS = [
    # W1：严格布尔三态 + 类型不严 + 非法 JSON（全 sv1）
    _seg("t1", "W1", '{"src_ok": true}'),
    _seg("f1", "W1", '{"src_ok": false}', role="train"),
    _seg("b1", "W1", '{"src_ok": true}', role="benchmark"),
    _seg("m1", "W1", "{}"),                              # 缺键→未校验
    _seg("m2", "W1", '{"lang_ok": true}', role="train"),  # 缺键→未校验
    _seg("l1", "W1", '{"src_ok": 1}'),                    # 数字1：IS TRUE 漏入
    _seg("l2", "W1", '{"src_ok": "true"}'),               # 字符串→loose
    _seg("l3", "W1", '{"src_ok": null}'),                 # null→loose
    _seg("n1", "W1", None),                              # NULL→fail-closed 未校验
    _seg("n2", "W1", "not-json{{"),                      # 非法JSON→fail-closed 未校验
    # W2：白名单空数组；全部已校验
    _seg("p1", "W2", '{"src_ok": true}', role="train", sv=1),
    _seg("p2", "W2", '{"src_ok": false}', role="train", sv=2),
    # W3：未登记作品（无 work_sources 行），全 sv2、全未校验
    _seg("u1", "W3", "{}", sv=2),                                     # 未校验，不跳过
    _seg("u2", "W3", '{"src_ok": "false"}', role="train", sv=2,
         text="吴天斗罗出场了这是一段仍然够长的文本", clean=None),      # 规则命中→零LLM
    _seg("u3", "W3", None, sv=2, text="短文本", clean=""),             # 过短→零LLM
    _seg("u4", "W3", "{}", sv=2, clean=None,
         text="仞千雪出现了这里没有掉字问题啊啊"),                      # 千雪前有仞：不算命中
    _seg("u5", "W3", '{"src_ok": 0}', sv=2, clean=None,
         text="千雪站在城头看着远方风雪很大"),                          # 首处即千雪→命中
]
WORKS = [("W1", "作品甲"), ("W2", "作品乙"), ("W3", "作品丙")]
WORK_SOURCES = [
    ("W1", "human_fiction", "corpus-v1", json.dumps(["train"])),
    ("W2", "fixture", "corpus-v2-mirror", json.dumps([])),
    # W3 故意不登记
]

UNVERIFIED_IDS = {"m1", "m2", "l1", "l2", "l3", "n1", "n2",
                  "u1", "u2", "u3", "u4", "u5"}


def make_db(tmp_path: Path, segments=SEGMENTS) -> Path:
    make_db._seq = getattr(make_db, "_seq", 0) + 1
    p = tmp_path / f"fixture_cov_{make_db._seq}.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE works(id TEXT PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE work_sources(work_id TEXT NOT NULL UNIQUE,
                                  source_type TEXT NOT NULL,
                                  text_version TEXT NOT NULL,
                                  allowed_purposes TEXT NOT NULL);
        CREATE TABLE segments(id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
                              integrity TEXT, role TEXT, seg_version INTEGER,
                              text TEXT, text_clean TEXT);
    """)
    con.executemany("INSERT INTO works VALUES (?,?)", WORKS)
    con.executemany("INSERT INTO work_sources VALUES (?,?,?,?)", WORK_SOURCES)
    con.executemany("INSERT INTO segments VALUES (?,?,?,?,?,?,?)", segments)
    con.commit()
    con.close()
    return p


def _result(tmp_path: Path) -> dict:
    return k5sc.build_result(make_db(tmp_path))


def _ids(db: Path, where: str) -> set[str]:
    con = k5sc.open_ro(db)
    try:
        sql = "SELECT s.id FROM segments s"
        if where:
            sql += f" WHERE {where}"
        return {r[0] for r in con.execute(sql)}
    finally:
        con.close()


def _res_of(db: Path) -> dict:
    return k5sc.build_result(db)


# ── ① 严格布尔三态 + 三分类计数 ──────────────────────────────────────
def test_totals_strict_tristate(tmp_path):
    t = _result(tmp_path)["totals"]
    assert t["total"] == 17
    assert t["src_true"] == 3 and t["src_false"] == 2       # true/false=已校验
    assert t["checked_strict"] == 5
    assert t["unverified"] == 12                            # 其余全部=未校验
    assert t["unverified_missing"] == 4                     # m1 m2 u1 u4
    assert t["unverified_loose"] == 5                       # l1 l2 l3 u2 u5
    assert t["unverified_nojson"] == 3                      # n1 n2 u3
    assert t["unverified_ratio"] == round(12 / 17, 6)


def test_unverified_membership(tmp_path):
    db = make_db(tmp_path)
    got = _ids(db, k5sc.UNCHECKED)
    assert got == UNVERIFIED_IDS                             # 逐段钉死
    assert not got & {"t1", "f1", "b1", "p1", "p2"}          # 严格布尔段一个都不进


# ── ② 数字 1 / 字符串 "true" 不得算作已校验（松紧口径之差可测）────────
def test_loose_values_not_counted_checked(tmp_path):
    t = _result(tmp_path)["totals"]
    # 松口径 IS TRUE 会把数字 1 的 l1 当 true；严格口径只认 json_type='true'
    assert t["is_true_loose_over_strict_leak"] == 1          # 掺水差值 = {l1}
    db = make_db(tmp_path)
    loose_true_ids = _ids(db, "s.integrity IS NOT NULL AND json_valid(s.integrity)=1"
                              " AND json_extract(s.integrity,'$.src_ok') IS TRUE")
    strict_true_ids = _ids(db, "s.integrity IS NOT NULL AND json_valid(s.integrity)=1"
                               " AND json_type(s.integrity,'$.src_ok')='true'")
    assert strict_true_ids == {"t1", "b1", "p1"}
    assert loose_true_ids == strict_true_ids | {"l1"}        # 数字 1 漏入松口径
    w3 = [w for w in _res_of(db)["by_work"] if w["work_id"] == "W3"][0]
    assert w3["checked_strict"] == 0                         # u2("false")/u5(0) 不算已查
    assert w3["unverified"] == 5                              # 且字符串"true"同理（l2）
    w1 = [w for w in _res_of(db)["by_work"] if w["work_id"] == "W1"][0]
    assert w1["checked_strict"] == 3
    assert w1["unverified"] == 7


# ── ③ 非法 JSON fail-closed 记未校验 ─────────────────────────────────
def test_invalid_json_fail_closed(tmp_path):
    db = make_db(tmp_path)
    nojson = _ids(db, "s.integrity IS NULL OR json_valid(s.integrity)=0")
    assert nojson == {"n1", "n2", "u3"}
    got = _ids(db, k5sc.UNCHECKED)
    assert nojson <= got                                     # 绝不当「已校验」也不当「判坏」


# ── ④ 排序稳定 + 同输入两次输出逐字节一致 ────────────────────────────
def test_sort_stable_and_deterministic(tmp_path):
    db = make_db(tmp_path)
    res = _res_of(db)
    assert [w["work_id"] for w in res["by_work"]] == ["W1", "W3", "W2"]  # 未校验降序
    a = json.dumps(res, ensure_ascii=False, sort_keys=True)
    b = json.dumps(_res_of(db), ensure_ascii=False, sort_keys=True)
    assert a == b                                            # 逐字节一致


def test_tie_break_work_id_asc(tmp_path):
    segs = [
        _seg("x1", "TA", "{}"),                              # 各 1 未校验、占比同为 1.0
        _seg("x2", "TB", "not-json{{"),
    ]
    db = make_db(tmp_path, segs)
    res = _res_of(db)
    order = [w["work_id"] for w in res["by_work"] if w["work_id"] in ("TA", "TB")]
    assert order == ["TA", "TB"]
    # 同一库两次运行（含并列维）输出逐字节一致
    res2 = _res_of(db)
    assert json.dumps(res, sort_keys=True) == json.dumps(res2, sort_keys=True)


# ── ⑤ 只读性：mtime 与内容不变；ro 连接写必抛 ────────────────────────
def test_readonly_db_untouched(tmp_path):
    db = make_db(tmp_path)
    before_bytes = hashlib.sha256(db.read_bytes()).hexdigest()
    before_stat = db.stat()
    k5sc.build_result(db)
    after_stat = db.stat()
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_bytes
    assert not (tmp_path / (db.name + "-wal")).exists()      # 未产生 WAL 副作


def test_ro_connection_rejects_writes(tmp_path):
    con = k5sc.open_ro(make_db(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO works VALUES ('ZX', '写入门钉死')")
        con.commit()
    con.close()


# ── ⑥ 代价估算：纯算术、常量带行号取自 source_check.py 源码 ──────────
def test_cost_arithmetic(tmp_path):
    c = _result(tmp_path)["cost"]
    assert c["unverified_segments"] == 12
    assert c["zero_llm_skips"] == 3           # u2(吴天) + u3(过短) + u5(千雪首处)
    assert c["calls_upper"] == 12 and c["calls_lower"] == 9
    assert c["batches_conc_upper"] == 2 and c["batches_conc_lower"] == 2   # conc=8
    assert c["batches_serial_upper"] == 12 and c["batches_serial_lower"] == 9
    assert c["fetch_batches_in_chunk"] == 1   # 每批上限 500 的取数批
    assert c["breaker"]["earliest_trip_wave"] == 3   # 20 次调用 ÷ 每批 8 → 第 3 批


def test_cost_constants_have_line_numbers(tmp_path):
    c = _result(tmp_path)["cost"]
    k = c["constants"]
    src_lines = (ROOT / "scripts" / "source_check.py").read_text(
        encoding="utf-8").splitlines()
    for name in ("CONC_DEFAULT", "BREAKER_MIN_CALLS", "BREAKER_FAIL_RATE",
                 "MIN_TEXT_CHARS", "IN_CHUNK", "MODEL", "POOL_WORKERS",
                 "CALLS_PER_SEGMENT"):
        assert k[name]["line"] >= 1
        assert k[name]["line"] <= len(src_lines)
    assert k["CONC_DEFAULT"]["value"] == 8
    assert k["BREAKER_MIN_CALLS"]["value"] == 20
    assert k["BREAKER_FAIL_RATE"]["value"] == 0.3
    assert k["MIN_TEXT_CHARS"]["value"] == 10
    assert k["IN_CHUNK"]["value"] == 500
    assert k["CALLS_PER_SEGMENT"]["value"] == 1     # 每次调用 1 段 = check_one 一次 chat


def test_parse_constants_vs_source_file():
    consts = k5sc.parse_constants(ROOT / "scripts" / "source_check.py")
    assert consts["ok"] is True and consts["missing"] == []
    lines = (ROOT / "scripts" / "source_check.py").read_text(
        encoding="utf-8").splitlines()
    for name, item in consts["items"].items():
        assert lines[item["line"] - 1].strip() == item["raw"], name   # 行号自指一致
    assert consts["check_one_chat_calls"]["count"] == 1
    typos = {t["bad"] for t in consts["known_typos"]}
    assert typos == {"吴天", "了天斗罗", "千雪"}


def test_parse_constants_missing_file_fail_closed(tmp_path):
    consts = k5sc.parse_constants(tmp_path / "nope.py")
    assert consts["ok"] is False


# ── ⑦ 口径影响 + verdict 三分支 ──────────────────────────────────────
def test_caliber_impact(tmp_path):
    ci = _result(tmp_path)["caliber_impact"]
    assert ci["A_loose_total"] == 2        # t1 + l1（数字1 漏入松口径 A）
    assert ci["A_strict_total"] == 1       # 严格布尔只剩 t1
    assert ci["B_total"] == 9              # W1 非 benchmark 9 段（白名单非空）
    assert ci["unverified_in_A"] == 1      # l1：未校验但满足 IS TRUE
    assert ci["unverified_in_A_strict"] == 0
    assert ci["unverified_in_B"] == 7
    assert ci["unchanged_if_kept_unverified"] is True   # 不补跑 ⇒ A/B 不变


def test_verdict_ok(tmp_path):
    co = _result(tmp_path)["conclusion"]
    assert co["verdict"] == "可校验"
    assert "从未校验" in co["reason"] or "未校验" in co["reason"]
    assert any("--run" in s for s in co["not_done"])       # 未自跑清单在案


def test_verdict_not_verifiable_missing_schema(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()                              # 连 segments 都没有
    co = k5sc.build_result(p)["conclusion"]
    assert co["verdict"] == "不可校验"


def test_verdict_insufficient_constants(tmp_path):
    bad = {"path": "x", "ok": False, "missing": ["CONC_DEFAULT"], "items": {},
           "check_one_chat_calls": None, "known_typos": []}
    co = k5sc.build_result(make_db(tmp_path), consts=bad)["conclusion"]
    assert co["verdict"] == "证据不足"


# ── 渲染与 CLI（tmp 夹具库；不触真库、不写仓内文件）──────────────────
def test_render_and_cli(tmp_path, capsys):
    db = make_db(tmp_path)
    outjson = tmp_path / "o" / "cov.json"
    md = tmp_path / "o" / "cov.md"
    assert k5sc.main(["--db", str(db), "--out", str(outjson),
                      "--md-out", str(md)]) == 0
    res = json.loads(outjson.read_text(encoding="utf-8"))
    assert res["conclusion"]["verdict"] == "可校验"
    text = md.read_text(encoding="utf-8")
    for needle in ("严格布尔", "未校验", "调用次数上界", "口径", "未自跑",
                   "真跑 stdout 原样片段"):
        assert needle in text
    stdout = capsys.readouterr().out
    assert "mode=ro 只读" in stdout and "[结论] 可校验" in stdout


def test_cli_print_only_writes_nothing(tmp_path, capsys):
    db = make_db(tmp_path)
    outjson = tmp_path / "should_not_exist.json"
    rc = k5sc.main(["--db", str(db), "--out", str(outjson),
                    "--md-out", str(tmp_path / "sne.md"), "--print-only"])
    assert rc == 0
    assert not outjson.exists() and not (tmp_path / "sne.md").exists()


def test_cli_missing_db_returns_2(tmp_path):
    assert k5sc.main(["--db", str(tmp_path / "nope.db")]) == 2
