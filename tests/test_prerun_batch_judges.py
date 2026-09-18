"""评委分预跑脚本回归（2026-09-16）。

**背景**：`heldout_eval.load_items` 只取 `status='done'`，而 preference 评委分是在评测
流程里现跑的 → 「集霸判完 → 出报告」把评委侧的问题推迟到集霸花掉时间之后才暴露。
`scripts/prerun_batch_judges.py` 把评委侧前移，本文件锁死它与评测的**纳入条件一致性**
（不一致会让预跑白跑或让评测仍缺数，两者都是静默的）：

1. 预跑必须能取到 `pending` 题（这正是它存在的理由）。
2. 纳入条件必须与评测一致：非 `reconstruct_v1/recon_ctx_v1` 的候选不跑。
3. 覆盖核对必须真的能发现缺口 —— 它是"不信内存计数器"的那道防线。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prerun_batch_judges as PB  # noqa: E402

EXP = "EXP-0911-B82D"
MODELS = ("m-one", "m-two")


def _make_db(tmp_path: Path, items: list[dict], runs: list[dict] | None = None) -> Path:
    """items: {cid, batch, status, prompt_version}; runs: {cid, model, pv, status}"""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("create table candidates (id text primary key, prompt_version text)")
    con.execute("""create table review_items (
        id integer primary key autoincrement, experiment_id text, subject_id text,
        status text, reasons text)""")
    con.execute("""create table judge_runs (
        id integer primary key autoincrement, experiment_id text, subject_type text,
        subject_id text, judge_kind text, model text, prompt_version text,
        status text, created_at text)""")
    for it in items:
        con.execute("insert into candidates values (?,?)",
                    (it["cid"], it.get("prompt_version", "reconstruct_v1")))
        con.execute("insert into review_items (experiment_id, subject_id, status, reasons) "
                    "values (?,?,?,?)",
                    (EXP, it["cid"], it.get("status", "pending"),
                     f'["batch_{it.get("batch", "hX")}"]'))
    for r in runs or []:
        con.execute("insert into judge_runs (experiment_id, subject_type, subject_id, "
                    "judge_kind, model, prompt_version, status, created_at) "
                    "values (?,?,?,?,?,?,?,?)",
                    (EXP, "candidate", r["cid"], "preference", r["model"],
                     r["pv"], r.get("status", "ok"), r.get("created_at", "2026-09-16T00:00:00Z")))
    con.commit()
    con.close()
    return db


# ── ① 必须能取到待判题（预跑的全部意义）──────────────────────
def test_load_cids_includes_pending(tmp_path):
    db = _make_db(tmp_path, [{"cid": "CND-1", "status": "pending"},
                             {"cid": "CND-2", "status": "done"}])
    assert PB.load_cids("hX", db_path=db) == ["CND-1", "CND-2"]


def test_load_cids_excludes_other_prompt_versions(tmp_path):
    """纳入条件必须与 heldout_eval 一致，否则预跑的分评测读不到。"""
    db = _make_db(tmp_path, [{"cid": "CND-1"},
                             {"cid": "CND-2", "prompt_version": "other_v9"}])
    assert PB.load_cids("hX", db_path=db) == ["CND-1"]


def test_load_cids_batch_isolation_and_order(tmp_path):
    db = _make_db(tmp_path, [{"cid": "CND-b"}, {"cid": "CND-a"},
                             {"cid": "CND-c", "batch": "hOTH"}])
    assert PB.load_cids("hX", db_path=db) == ["CND-a", "CND-b"]   # 有序 → --limit 可复现


def test_load_cids_empty_batch_returns_empty(tmp_path):
    db = _make_db(tmp_path, [{"cid": "CND-1", "batch": "other"}])
    assert PB.load_cids("hX", db_path=db) == []


# ── ② 覆盖核对必须能发现缺口 ─────────────────────────────────
def test_verify_reports_missing_run(tmp_path):
    items = [{"cid": "CND-1"}, {"cid": "CND-2"}]
    runs = [{"cid": "CND-1", "model": m, "pv": "judge_preference_v4_heldout"} for m in MODELS]
    db = _make_db(tmp_path, items, runs)
    gaps = PB.verify(["CND-1", "CND-2"], MODELS, ["v4"], db_path=db)
    assert len(gaps) == 2 and all("CND-2" in g for g in gaps)


def test_verify_flags_failed_status(tmp_path):
    """status=failed 不算覆盖 —— 否则评测会拿不到分却看不出来。"""
    runs = [{"cid": "CND-1", "model": m, "pv": "judge_preference_v4_heldout",
             "status": "failed"} for m in MODELS]
    db = _make_db(tmp_path, [{"cid": "CND-1"}], runs)
    assert len(PB.verify(["CND-1"], MODELS, ["v4"], db_path=db)) == 2


def test_verify_clean_when_all_present(tmp_path):
    runs = [{"cid": "CND-1", "model": m, "pv": "judge_preference_v4_heldout"}
            for m in MODELS]
    db = _make_db(tmp_path, [{"cid": "CND-1"}], runs)
    assert PB.verify(["CND-1"], MODELS, ["v4"], db_path=db) == []


def test_verify_takes_latest_run(tmp_path):
    """同一 (cid, 评委, 口径) 先失败后成功，应判为已覆盖。"""
    runs = [{"cid": "CND-1", "model": "m-one", "pv": "judge_preference_v4_heldout",
             "status": "failed", "created_at": "2026-09-16T00:00:00Z"},
            {"cid": "CND-1", "model": "m-one", "pv": "judge_preference_v4_heldout",
             "status": "ok", "created_at": "2026-09-16T01:00:00Z"}]
    db = _make_db(tmp_path, [{"cid": "CND-1"}], runs)
    assert PB.verify(["CND-1"], ("m-one",), ["v4"], db_path=db) == []


def test_variant_prompt_version_literals():
    """预跑写的 prompt_version 必须正好是评测读的那两个字符串。

    写错一个后缀（例如漏掉 `_heldout`）不会报错：预跑会"成功"写进一批评测永远读不到的
    记录，评测再自己重跑一遍 —— 多花一倍调用，且没人会发现。用字面量锁死。
    """
    assert PB.he.PROMPT_VARIANTS["v3"][1] == "judge_preference_v3_heldout"
    assert PB.he.PROMPT_VARIANTS["v4"][1] == "judge_preference_v4_heldout"
