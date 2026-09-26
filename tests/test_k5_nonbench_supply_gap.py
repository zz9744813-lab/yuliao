"""K5 非基准供给池缺口只读盘查回归（scripts/k5_nonbench_supply_gap.py，
任务 nonbench-supply-gap 2026-09-26）。

全部离线：tmp_path 造夹具库（真库零触碰）、零网络、零模型调用、不跑任何
`--run`/`--live`。钉死的事：

① **严格布尔三态分桶**：`integrity.src_ok` 只有 JSON `true` 算过闸、JSON `false`
   算判坏；字符串 `"false"`/`"true"`、数字 1/0、`null`、缺键、非法 JSON 一律记
   「未校验」——**字符串 `"false"` 绝不落进「通过」（也不落进判坏）**。
   判据本身与 `source_check.parse_src_ok`/`needs_check` 逐值对账（一支笔）。
② **fixture/synthetic/commentary fail-closed 排除**：三类来源整作进
   `excluded_source_type` 桶、不入池、K3 来源闸理由逐字等于
   `k2_extract_backfill._k3_source_gate` 的理由串；排除集常量与
   `app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES` 是**同一个对象**。
③ **只读性**：`open_ro` 连接写必抛；`build_result` 跑完库文件字节与 mtime 不变、
   不产生 `-wal`/`-shm` 副作用。
④ **缺库非零退出**（`main` 返回 2）、`--work-id` 全空值同样非零；`--work-id`
   只收窄不放宽（收窄后逐作品读数与全量逐字相等；不存在的 work_id = 空集不报错）。
⑤ 分桶互斥、求和恒等于段总数（逐作品 + 全库）；两次运行逐字节一致。
⑥ 与 K2 真判据同源对账：池内 `kept_nonbenchmark` 必须等于用
   `k2_extract_backfill._src_ok` + `text_clean` + role 现算的保留段数。
⑦ 策略状态闸只读复算：抽取宇宙 status 集合从 K2 源码 AST 取（行号自指、多处
   命中即 fail-closed），与 K3 合格集无交集 ⇒ 恒 0 属结构性；表不可读 ⇒ 「未知」
   且供给侧照常出数；反向探针（把抽取宇宙扩到 verified）判据翻正 ⇒ 结论没写死。
⑧ 机械推算不放宽任何门：上界 = 合格 + 未校验可回段；判坏段（`src_ok=false`）
   与来源级排除段绝不进上界。

纪律：本文件绝不写仓内路径——`main()` 一律显式传 `--md-out`/`--json-out` 到
tmp_path，或用 `--print-only`。
"""
from __future__ import annotations

import hashlib
import importlib.util as _u
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import k2_extract_backfill as k2b                            # noqa: E402
import source_check as sc                                    # noqa: E402
from app import knowledge_query as KQ                        # noqa: E402

_spec = _u.spec_from_file_location(
    "k5nb", ROOT / "scripts" / "k5_nonbench_supply_gap.py")
k5nb = _u.module_from_spec(_spec)
sys.modules["k5nb"] = k5nb
_spec.loader.exec_module(k5nb)

FUHAN = k5nb.FUHAN_WORK_ID                                   # WK-dc90993434e9
GOOD = "合格正文文本，长度足够不被长度闸门干扰。"
BUCKETS = [k for k, _ in k5nb.BUCKET_ORDER]


def _seg(sid, work, ig, role=None, clean=GOOD):
    """(id, work_id, integrity, role, seg_version, text_clean)"""
    return (sid, work, ig, role, 1, clean)


# ── 夹具库（每条都是某一闸的靶子）────────────────────────────────────
WORKS = [
    (FUHAN, "覆汉（夹具替身）"),
    ("WK-nonben", "前缀合规源"),
    ("WK-fix", "夹具源"),
    ("WK-syn", "合成源"),
    ("WK-com", "评注源"),
    ("WK-badtv", "版本不合格的人类源"),
    ("WK-mt", "其他来源"),
    # WK-noreg 故意既无 works 行也无登记：同时验 (无 works 行) 兜底
]
WORK_SOURCES = [
    # (work_id, source_type, text_version, license_purposes)
    (FUHAN, "human_fiction", "corpus-v1", '["train"]'),
    ("WK-nonben", "production_nonbenchmark_k2v2", "corpus-v2-mirror", "[]"),
    ("WK-fix", "fixture", "corpus-v1", "[]"),
    ("WK-syn", "synthetic", "corpus-v1", "[]"),
    ("WK-com", "commentary", "corpus-v1", "[]"),
    ("WK-badtv", "human_fiction", "corpus-v3", "[]"),
    ("WK-mt", "model_output", "corpus-v1", "[]"),
]
SEGMENTS = [
    # 覆汉替身：三态全谱 + 各闸靶子（18 段）
    _seg("f_true", FUHAN, '{"src_ok": true}'),
    _seg("f_true_train", FUHAN, '{"src_ok": true}', role="train"),
    _seg("f_true_nullrole", FUHAN, '{"src_ok": true}', role=None),
    _seg("f_bench", FUHAN, '{"src_ok": true}', role="benchmark"),       # role 闸
    _seg("f_false", FUHAN, '{"src_ok": false}', role="train"),          # 判坏
    _seg("f_str_false", FUHAN, '{"src_ok": "false"}'),                  # ①核心
    _seg("f_str_true", FUHAN, '{"src_ok": "true"}'),
    _seg("f_num_1", FUHAN, '{"src_ok": 1}'),
    _seg("f_num_0", FUHAN, '{"src_ok": 0}'),
    _seg("f_null", FUHAN, '{"src_ok": null}'),
    _seg("f_missing", FUHAN, '{}'),
    _seg("f_otherkey", FUHAN, '{"lang_ok": true}', role="train"),
    _seg("f_unver_key", FUHAN, '{"src_ok_unverified": true}'),          # 真库脏态写法
    _seg("f_nojson", FUHAN, None),
    _seg("f_badjson", FUHAN, "not-json{{"),
    _seg("f_list", FUHAN, "[1,2]"),
    _seg("f_true_blanks", FUHAN, '{"src_ok": true}', clean="   "),      # 清洗闸
    _seg("f_true_empty", FUHAN, '{"src_ok": true}', clean=""),
    # 前缀白名单合规源：全通过
    _seg("n_1", "WK-nonben", '{"src_ok": true}', role="train"),
    _seg("n_2", "WK-nonben", '{"src_ok": true}', role=None),
    # fixture / synthetic / commentary：整作按设计排除
    _seg("x_1", "WK-fix", '{"src_ok": true}'),
    _seg("x_2", "WK-fix", '{"src_ok": true}', role="benchmark"),
    _seg("y_1", "WK-syn", '{"src_ok": true}'),
    _seg("z_1", "WK-com", '{"src_ok": true}'),
    # 入池但 K3 来源闸因 text_version 拒
    _seg("v_1", "WK-badtv", '{"src_ok": true}'),
    # 白名单外、排除集外
    _seg("m_1", "WK-mt", '{"src_ok": true}'),
    # 未登记（fail-closed 整作排除）
    _seg("r_1", "WK-noreg", '{"src_ok": true}'),
    _seg("r_2", "WK-noreg", '{"src_ok": "false"}', role="train"),
]
N_SEG = len(SEGMENTS)                                       # 28

# 期望读数（逐段手算后钉死；改夹具必须同步改这里，否则对账全废）
EXPECT_FUHAN = {"n_segments": 18, "src_true": 6, "src_false": 1, "src_loose": 5,
                "src_missing": 3, "src_nojson": 3, "src_unverified": 11,
                "role_benchmark": 1, "src_ok_false": 1, "src_ok_unverified": 11,
                "text_clean_empty": 2, "eligible": 3}
EXPECT_TOT = {"n_segments": N_SEG, "src_true": 15, "src_false": 1,
              "src_loose": 6, "src_missing": 3, "src_nojson": 3,
              "src_unverified": 12}
EXPECT_TOT_B = {"no_registry": 2, "excluded_source_type": 4,
                "source_type_not_compliant": 1, "role_benchmark": 1,
                "src_ok_false": 1, "src_ok_unverified": 11,
                "text_clean_empty": 2, "eligible": 6}

# 策略表：抽取宇宙只收 hypothesis/active；verified 行**不在**抽取宇宙里
STRATEGIES = [
    # (id, strategy_key, version, status, observation_status, scope)
    ("1", "sk-hyp", "1", "hypothesis", "observed", "UNCERTAIN"),
    ("2", "sk-act", "2", "active", "replicated", "WORK"),
    ("3", "sk-verb", "2", "verified", "observed", "WORK"),
    ("4", "sk-retire", "1", "retired", "observed", "WORK"),
    ("5", "sk-super", "1", "superseded", "replicated", "AUTHOR"),
]


def make_db(tmp_path: Path, *, segments=SEGMENTS, strategies=STRATEGIES,
            name: str = "fixture_nb.db") -> Path:
    p = tmp_path / name
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE works(id TEXT PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE work_sources(work_id TEXT NOT NULL UNIQUE,
                                  source_type TEXT NOT NULL,
                                  text_version TEXT NOT NULL,
                                  license_purposes TEXT NOT NULL);
        CREATE TABLE segments(id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
                              integrity TEXT, role TEXT, seg_version INTEGER,
                              text_clean TEXT);
        CREATE TABLE expression_strategies_v2(id TEXT PRIMARY KEY,
                                  strategy_key TEXT NOT NULL, version TEXT,
                                  status TEXT, observation_status TEXT,
                                  scope TEXT);
    """)
    con.executemany("INSERT INTO works VALUES (?,?)", WORKS)
    con.executemany("INSERT INTO work_sources VALUES (?,?,?,?)", WORK_SOURCES)
    con.executemany("INSERT INTO segments VALUES (?,?,?,?,?,?)", segments)
    if strategies is not None:
        con.executemany(
            "INSERT INTO expression_strategies_v2 VALUES (?,?,?,?,?,?)",
            strategies)
    con.commit()
    con.close()
    return p


def _res(tmp_path: Path, **kw) -> dict:
    return k5nb.build_result(make_db(tmp_path, **kw))


def _work(res: dict, wid: str) -> dict:
    return next(w for w in res["works"] if w["work_id"] == wid)


# ── ① 严格布尔三态分桶 ───────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ('{"src_ok": true}', "true"),
    ('{"src_ok": false}', "false"),
    ('{"src_ok": "false"}', "loose"),        # 字符串 false：既非 true 也非 false
    ('{"src_ok": "true"}', "loose"),
    ('{"src_ok": "True"}', "loose"),
    ('{"src_ok": 1}', "loose"),
    ('{"src_ok": 0}', "loose"),
    ('{"src_ok": null}', "loose"),
    ('{"src_ok": []}', "loose"),
    ('{"src_ok": {}}', "loose"),
    ('{"src_ok_unverified": true}', "missing"),   # 键不是 src_ok ⇒ 缺键
    ('{}', "missing"),
    ('{"lang_ok": true}', "missing"),
    (None, "nojson"),
    ("not-json{{", "nojson"),
    ("[1,2]", "nojson"),
    ('"text"', "nojson"),
])
def test_src_state_five_states(raw, want):
    assert k5nb.src_state(raw) == want


def test_string_false_never_counts_as_passed(tmp_path):
    res = _res(tmp_path)
    w = _work(res, FUHAN)
    assert w["buckets"]["eligible"] == 3                  # 只有 3 个 JSON true 段
    assert w["buckets"]["src_ok_false"] == 1              # 只有真 JSON false
    assert w["buckets"]["src_ok_unverified"] == 11        # 含字符串 "false"
    assert w["kept_nonbenchmark"] == 3
    assert w["recoverable_if_verified"] == 11
    assert w["judged_bad_nonbench"] == 1
    for key, val in EXPECT_FUHAN.items():
        assert (w["buckets"] if key in BUCKETS else w)[key] == val, key
    assert res["totals"]["buckets"]["eligible"] == 6


def test_src_state_agrees_with_source_check():
    """一支笔对账：`src_state` 的 true/false 判定必须与
    `source_check.parse_src_ok` 逐值一致（本脚本不自己再判一遍布尔）。"""
    for val in (True, False, "false", "true", "True", 1, 0, 1.0, None, [],
                "yes", {}, [True], ""):
        have = {"src_ok": val}
        parsed = sc.parse_src_ok(val)
        st = k5nb.src_state(json.dumps(have))
        assert sc.needs_check(have) is (parsed is None)   # 未校验必须重查
        if parsed is True:
            assert st == "true"
        elif parsed is False:
            assert st == "false"
        else:
            assert st in k5nb.UNVERIFIED_STATES, val


def test_text_clean_and_role_judges_match_k2():
    """`text_clean`/`role` 两支判据同样与 K2 对齐（含全空白串与 NULL role）。"""
    for clean in (GOOD, "", "   ", "\n\t ", None, "字"):
        assert k5nb.text_clean_ok(clean) == (1 if (clean or "").strip() else 0)
    for role in (None, "train", "benchmark", "Benchmark", "", "draft"):
        key = role if role is not None else "NULL"
        assert k5nb.role_nonbench_ok(role) == (
            1 if k2b._role_passes_scope("nonbenchmark", key) else 0)
    assert k5nb.role_nonbench_ok("benchmark") == 0        # 唯一被剔的 role
    assert k5nb.role_nonbench_ok(None) == 1               # NULL ≠ benchmark


# ── ② fixture/synthetic/commentary fail-closed 排除 ──────────────────
def test_excluded_source_types_is_the_same_object():
    assert k5nb.EXCLUDED_SOURCE_TYPES is KQ.DEFAULT_EXCLUDED_SOURCE_TYPES
    assert {"fixture", "synthetic", "commentary"} <= k5nb.EXCLUDED_SOURCE_TYPES
    # 桶顺序（先同源排除、后白名单）与 K2 抽段宇宙等价的前提：两支集合不相交
    assert not (k2b.NONBENCHMARK_SOURCE_TYPES & k5nb.EXCLUDED_SOURCE_TYPES)
    assert not any(t.startswith(k2b.NONBENCHMARK_SOURCE_TYPE_PREFIX)
                   for t in k5nb.EXCLUDED_SOURCE_TYPES)


@pytest.mark.parametrize("wid,st", [
    ("WK-fix", "fixture"), ("WK-syn", "synthetic"), ("WK-com", "commentary")])
def test_excluded_source_types_fail_closed(tmp_path, wid, st):
    res = _res(tmp_path)
    w = _work(res, wid)
    assert w["source_type"] == st
    assert w["hit_excluded_source_type"] is True
    assert w["in_pool"] is False
    assert w["kept_nonbenchmark"] == 0
    assert w["recoverable_if_verified"] == 0
    assert w["buckets"]["excluded_source_type"] == w["n_segments"]
    assert all(w["buckets"][k] == 0 for k in BUCKETS if k != "excluded_source_type")
    assert w["k3_source_gate"] == f"excluded_source_type:{st}"


def test_fixture_segments_never_enter_pool_even_when_clean(tmp_path):
    """`x_1` 是 `src_ok: true` + text_clean 非空 + role 非基准——按段级三门完全
    合格，但来源是 fixture ⇒ 整作排除（来源级 fail-closed 优先于段级门）。"""
    res = _res(tmp_path)
    w = _work(res, "WK-fix")
    assert w["src_true"] == 2 and w["role_benchmark_segments"] == 1
    assert w["kept_nonbenchmark"] == 0
    assert w["buckets"]["role_benchmark"] == 0            # 来源级先吃掉，不再细分
    assert w["buckets"]["src_ok_false"] == 0 and w["buckets"]["eligible"] == 0


def test_no_registry_and_whitelist_buckets(tmp_path):
    res = _res(tmp_path)
    noreg = _work(res, "WK-noreg")
    assert noreg["registered"] is False and noreg["in_pool"] is False
    assert noreg["title"] == "(无 works 行)"
    assert noreg["k3_source_gate"] == "no_registry"
    assert noreg["buckets"]["no_registry"] == 2
    assert noreg["src_true"] == 1 and noreg["src_loose"] == 1   # 未登记也出三态
    mt = _work(res, "WK-mt")
    assert mt["registered"] is True and mt["compliant_source"] is False
    assert mt["hit_excluded_source_type"] is False and mt["in_pool"] is False
    assert mt["buckets"]["source_type_not_compliant"] == 1
    nb = _work(res, "WK-nonben")                          # 前缀白名单也算合规
    assert nb["compliant_source"] is True and nb["in_pool"] is True
    assert nb["k3_source_gate"] is None
    assert nb["kept_nonbenchmark"] == 2


def test_bad_text_version_in_pool_but_k3_source_gate_rejects(tmp_path):
    res = _res(tmp_path)
    w = _work(res, "WK-badtv")
    assert w["in_pool"] is True                 # 抽取侧白名单只看 source_type
    assert w["text_version_allowed"] is False
    assert w["k3_source_gate"] == "text_version:corpus-v3"
    assert w["kept_nonbenchmark"] == 1          # K2 抽段宇宙仍收它（段级三门全过）
    pj = res["projection"]
    assert pj["pool_sources_with_k3_source_gate_ok"] == (
        res["totals"]["pool_sources"] - 1)
    t = res["totals"]
    assert t["candidate_sources"] == 8          # 7 登记 + 1 未登记
    assert t["pool_sources"] == 3 and t["qualified_sources"] == 3
    assert t["pool_segments"] == 21             # 18 + 2 + 1


# ── ③ 只读性 ─────────────────────────────────────────────────────────
def test_open_ro_rejects_writes(tmp_path):
    con = k5nb.open_ro(make_db(tmp_path))
    for sql in ("INSERT INTO works VALUES ('Z1','写入门钉死')",
                'UPDATE segments SET integrity=\'{"src_ok": true}\'',
                "DELETE FROM segments",
                "DROP TABLE works"):
        with pytest.raises(sqlite3.OperationalError):
            con.execute(sql)
    con.close()


def test_build_result_leaves_db_untouched(tmp_path):
    db = make_db(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    st = db.stat()
    k5nb.build_result(db)
    after = db.stat()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert after.st_mtime_ns == st.st_mtime_ns
    assert not (tmp_path / (db.name + "-wal")).exists()
    assert not (tmp_path / (db.name + "-shm")).exists()


# ── ④ CLI：缺库非零 + 只收窄不放宽 ───────────────────────────────────
def test_missing_db_returns_2(tmp_path, capsys):
    assert k5nb.main(["--db", str(tmp_path / "nope.db"), "--print-only"]) == 2
    assert "库不存在" in capsys.readouterr().err


def test_empty_work_id_returns_2(tmp_path, capsys):
    db = make_db(tmp_path)
    assert k5nb.main(["--db", str(db), "--work-id", " , ", "--print-only"]) == 2
    assert "--work-id 全为空值" in capsys.readouterr().err


def test_print_only_writes_nothing(tmp_path, capsys):
    db = make_db(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert k5nb.main(["--db", str(db), "--print-only"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    out = capsys.readouterr().out
    assert "mode=ro 只读" in out and "[结论]" in out and "覆汉" in out


def test_default_doc_path_is_the_acceptance_file():
    """红线：默认 md 路径就是任务书那份文档；本回归绝不走到它（下面的 CLI 测
    一律显式传 tmp 路径），只钉住路径本身。"""
    assert k5nb.DOC_REL == Path("docs") / "K5非基准供给缺口_20260926.md"


def test_cli_writes_only_into_tmp(tmp_path, capsys):
    db = make_db(tmp_path)
    md, js = tmp_path / "o" / "gap.md", tmp_path / "o" / "gap.json"
    assert k5nb.main(["--db", str(db), "--md-out", str(md),
                      "--json-out", str(js)]) == 0
    text = md.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    for needle in ("src_ok", "text_clean", "覆汉", "严格布尔",
                   "被各闸吃掉的段数分桶", "机械推算", "策略状态闸", "红线声明",
                   "与主控 K2 真跑读数对照"):
        assert needle in text, needle
    assert "对账一致" in out and "覆汉" in out
    res = json.loads(js.read_text(encoding="utf-8"))
    assert res["readonly"] is True
    assert res["totals"]["bucket_sum_equals_total"] is True


def test_work_id_only_narrows(tmp_path):
    db = make_db(tmp_path)
    full = k5nb.build_result(db)
    wf = tuple(sc.parse_work_ids([f"{FUHAN},WK-fix"]))
    narrowed = k5nb.build_result(db, work_filter=wf)
    assert [w["work_id"] for w in narrowed["works"]] == [FUHAN, "WK-fix"]
    for wid in (FUHAN, "WK-fix"):
        assert _work(narrowed, wid) == _work(full, wid)   # 逐字段与全量一致
    assert (narrowed["totals"]["buckets"]["eligible"]
            <= full["totals"]["buckets"]["eligible"])
    assert narrowed["totals"]["candidate_sources"] == 2
    # 不存在的 work_id：空集 + 不报错（绝不借它扩宽到不合规来源）
    empty = k5nb.build_result(db, work_filter=("NOPE",))
    assert empty["works"] == [] and empty["totals"]["n_segments"] == 0
    assert empty["totals"]["bucket_sum_equals_total"] is True
    assert empty["conclusion"]["verdict"] == "策略门卡死"


# ── ⑤ 分桶互斥求和 + 确定性 ─────────────────────────────────────────
def test_buckets_partition_every_segment(tmp_path):
    res = _res(tmp_path)
    t = res["totals"]
    assert sorted(t["buckets"]) == sorted(BUCKETS)        # 桶集合无夹带无遗漏
    assert t["n_segments"] == N_SEG
    assert t["bucket_sum"] == t["n_segments"] == sum(EXPECT_TOT_B.values())
    assert t["bucket_sum_equals_total"] is True
    for key, val in EXPECT_TOT.items():
        assert t[key] == val, key
    for key, val in EXPECT_TOT_B.items():
        assert t["buckets"][key] == val, key
    for w in res["works"]:
        assert sum(w["buckets"][k] for k in BUCKETS) == w["n_segments"], w["work_id"]
    assert t["src_true"] + t["src_false"] + t["src_unverified"] == t["n_segments"]
    assert t["src_unverified"] == t["src_missing"] + t["src_loose"] + t["src_nojson"]


def test_deterministic_byte_identical(tmp_path):
    db = make_db(tmp_path)
    a = json.dumps(k5nb.build_result(db), ensure_ascii=False, sort_keys=True)
    b = json.dumps(k5nb.build_result(db), ensure_ascii=False, sort_keys=True)
    assert a == b
    res = json.loads(a)
    assert not [k for k in res if k in ("ts", "timestamp", "generated_at", "now")]
    assert [w["work_id"] for w in res["works"]] == sorted(
        w["work_id"] for w in res["works"])


# ── ⑥ 与 K2 真判据同源对账 ───────────────────────────────────────────
def test_kept_equals_k2_own_gates(tmp_path):
    """逐段用 K2 自己的判据现算，必须等于本盘查的 `kept_nonbenchmark`：
    `nonbenchmark_compliant_source` ∧ `role != benchmark` ∧ `k2b._src_ok` ∧
    `text_clean` 非空——一支笔，两侧口径漂移即红。"""
    db = make_db(tmp_path)
    res = k5nb.build_result(db)
    reg = {ws[0]: ws for ws in WORK_SOURCES}
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT id, work_id, integrity, role, text_clean"
                           " FROM segments").fetchall()
    finally:
        con.close()
    expect: dict[str, int] = {}
    for _sid, wid, ig, role, clean in rows:
        ws = reg.get(wid)
        if ws is None or not k2b.nonbenchmark_compliant_source(ws[1]):
            continue
        if (role or "") == k2b.BENCHMARK_ROLE:
            continue
        # 非字典 JSON（如 `[1,2]`）在 K2 的 `_src_ok` 里会 AttributeError——
        # 本盘查的口径是「非严格布尔即未校验 ⇒ 不过闸」，故先归一化再交给同一支笔。
        try:
            integ = json.loads(ig or "{}")
        except Exception:                                   # noqa: BLE001
            integ = {}
        if not isinstance(integ, dict):
            continue
        if not k2b._src_ok(SimpleNamespace(integrity=ig)):
            continue
        if not (clean or "").strip():
            continue
        expect[wid] = expect.get(wid, 0) + 1
    assert expect == {FUHAN: 3, "WK-nonben": 2, "WK-badtv": 1}
    got = {w["work_id"]: w["kept_nonbenchmark"] for w in res["works"]
           if w["kept_nonbenchmark"]}
    assert got == expect
    assert sum(expect.values()) == res["totals"]["eligible_segments"]


def test_k3_source_gate_reasons_match_k2(tmp_path):
    """K3 来源闸理由串逐字复用 `k2b._k3_source_gate`（同一支笔的返回值）。"""
    res = _res(tmp_path)
    for wid, want in {FUHAN: None, "WK-nonben": None, "WK-badtv":
                      "text_version:corpus-v3", "WK-fix":
                      "excluded_source_type:fixture", "WK-noreg":
                      "no_registry"}.items():
        assert _work(res, wid)["k3_source_gate"] == want, wid


# ── ⑦ 策略状态闸只读复算 ─────────────────────────────────────────────
def test_k2_status_universe_from_source_ast():
    uni = k5nb.k2_status_universe()
    assert uni["ok"] is True and uni["hits"] == 1
    assert uni["statuses"] == ["active", "hypothesis"]
    assert uni["line"] >= 1
    line = k5nb.K2_SRC.read_text(encoding="utf-8").splitlines()[uni["line"] - 1]
    assert "ExpressionStrategyV2.status.in_(" in line      # 行号自指一致
    assert uni["path"].endswith("k2_extract_backfill.py")


def test_k2_status_universe_fail_closed(tmp_path):
    assert k5nb.k2_status_universe(tmp_path / "nope.py")["ok"] is False
    zero = tmp_path / "zero.py"
    zero.write_text("x = 1\n", encoding="utf-8")
    assert (k5nb.k2_status_universe(zero)["ok"], k5nb.k2_status_universe(zero)["hits"]) == (False, 0)
    two = tmp_path / "two.py"
    two.write_text('a = q.filter(ExpressionStrategyV2.status.in_(("hypothesis",)))\n'
                   'b = q.filter(ExpressionStrategyV2.status.in_(("active",)))\n',
                   encoding="utf-8")
    r2 = k5nb.k2_status_universe(two)
    assert r2["ok"] is False and r2["hits"] == 2           # 多处命中 = 口径歧义


def test_strategy_gate_structurally_closed(tmp_path):
    res = _res(tmp_path)
    g = res["strategy_gate"]
    assert g["universe_statuses"] == ["active", "hypothesis"]
    assert g["n_extract_universe"] == 2                   # 只 sk-hyp / sk-act
    assert g["n_status_gate_pass"] == 0                   # 恒 0
    assert g["universe_eligible_intersection"] == []
    assert g["universe_disjoint_from_eligible"] is True
    assert g["status_distribution_all"] == {"active": 1, "hypothesis": 1,
                                            "retired": 1, "superseded": 1,
                                            "verified": 1}
    assert g["scope_buckets"] == {"UNCERTAIN": 1, "WORK": 1}
    co = res["conclusion"]
    assert co["policy_gate"]["verdict"] == "卡死"
    assert "与供给量无关" in co["policy_gate"]["reason"]
    assert "无交集" in co["policy_gate"]["reason"] and "晋升" in co["policy_gate"]["reason"]
    assert co["verdict"] == "两闸都有份（须分开处理）"
    md = k5nb.render_markdown(res)
    assert "结构性结论" in md and "无交集" in md


def test_status_gate_not_hardcoded(tmp_path, monkeypatch):
    """反向探针：把抽取宇宙扩到 verified（只喂假读数，不改任何常量）⇒ 恒 0 必须
    翻正、归因必须变——证明「卡死」真挂钩 status 判据，不是写死的结论。"""
    monkeypatch.setattr(k5nb, "k2_status_universe", lambda path=None: {
        "path": Path(k5nb.K2_SRC).as_posix(), "ok": True, "hits": 1, "why": "",
        "statuses": ["active", "hypothesis", "verified"], "line": 1})
    res = k5nb.build_result(make_db(tmp_path, name="probe.db"))
    g = res["strategy_gate"]
    assert g["n_extract_universe"] == 3 and g["n_status_gate_pass"] == 1
    assert g["universe_disjoint_from_eligible"] is False
    assert g["universe_eligible_intersection"] == ["verified"]
    co = res["conclusion"]
    assert co["policy_gate"]["verdict"] == "未卡死"
    assert co["verdict"] == "供给门卡死"
    assert "结构性结论" not in k5nb.render_markdown(res)


def test_strategy_gate_missing_table_fail_closed(tmp_path):
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE expression_strategies_v2")
    con.commit()
    con.close()
    res = k5nb.build_result(db)
    assert "error" in res["strategy_gate"]
    assert "不可读" in res["strategy_gate"]["error"]
    co = res["conclusion"]
    assert co["policy_gate"]["verdict"] == "未知"
    assert co["verdict"] == "供给门卡死"                  # 一闸不可读不拖垮另一闸
    assert res["totals"]["eligible_segments"] == 6
    assert "fail-closed" in k5nb.render_markdown(res)


# ── ⑧ 机械推算不放宽任何门 ───────────────────────────────────────────
def test_projection_bounds_no_gate_loosening(tmp_path):
    res = _res(tmp_path)
    pj, t = res["projection"], res["totals"]
    assert pj["eligible_now"] == t["eligible_segments"] == 6
    assert pj["recoverable_if_verified"] == 11 == pj["unverified_in_pool"]
    assert pj["eligible_upper_bound"] == 17
    assert pj["eligible_upper_bound"] == pj["eligible_now"] + pj["recoverable_if_verified"]
    assert pj["eligible_lower_bound"] == pj["eligible_now"]   # 全判 false ⇒ 不涨
    assert pj["judged_bad_not_recoverable"] == t["buckets"]["src_ok_false"] == 1
    # 判坏段与来源级排除段绝不进上界
    assert pj["eligible_upper_bound"] <= (
        t["n_segments"] - sum(t["buckets"][k] for k in BUCKETS
                              if k not in ("eligible", "src_ok_unverified")))
    assert "不放宽任何门" in pj["note"]
    assert "source_check" in pj["note"]
    co = res["conclusion"]
    assert co["supply_gate"]["verdict"] == "卡死"
    assert "数据侧" in co["supply_gate"]["who"] and "source_check.py" in co["supply_gate"]["who"]
    assert "策略审查席" in co["policy_gate"]["who"]
    assert co["supply_gate"]["detail"]["eaten_by_src_ok_unverified"] == 11
    assert co["supply_gate"]["detail"]["eaten_by_text_clean_empty"] == 2
    assert co["supply_gate"]["detail"]["eaten_by_src_ok_false"] == 1


def test_render_lines_carry_the_three_words(tmp_path):
    res = _res(tmp_path)
    md, so = k5nb.render_markdown(res), k5nb.render_stdout(res)
    for needle in ("src_ok", "text_clean", "覆汉"):
        assert needle in md and needle in so
    assert "| `eligible` |" in md
    assert "(对账一致)" in so                              # 分桶求和对账
    assert "[覆汉" in so                                   # 真跑时单独一行
    # 空池也要能渲染（§3b 兜底行），且不得越权叙事
    only = k5nb.build_result(make_db(tmp_path, name="only.db"),
                             work_filter=("WK-fix",))
    assert only["totals"]["eligible_segments"] == 0
    assert only["totals"]["pool_sources"] == 0
    assert only["totals"]["bucket_sum_equals_total"] is True
    assert "无非基准池作品" in k5nb.render_markdown(only)


def test_broken_schema_fail_closed(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(p).close()
    res = k5nb.build_result(p)
    assert res["conclusion"]["verdict"] == "不可盘查"
    assert res["issues"] and "segments 表缺失" in res["issues"][0]
    assert "totals" not in res and "works" not in res
    md = k5nb.render_markdown(res)                          # 不可盘查也必须可渲染
    assert "fail-closed" in md and "库结构不满足盘查前提" in md
    assert "fail-closed" in k5nb.render_stdout(res)
