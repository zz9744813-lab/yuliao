# -*- coding: utf-8 -*-
"""segments 列级瘦身迁移（scripts/slim_segments.py）在**真 SQLite 小副本**上的回归。

判据不是「看起来更小」，而是任务 1 节的硬口径：
  ① 行数、id/work_id/ordinal/n_chars/created_at 逐行不变；
  ② `text` 列逐字节不变（迁移只许动 text_clean/integrity/cleaned）；
  ③ **有效正文**逐字节不变 —— 读侧两种口径都钉：
     `text_clean if text_clean is not None else text`（semantic_review）
     `text_clean or text`（api.py:907 / register_work_sources 内容锚）；
  ④ integrity：`loads_any` 前后等价，eligible 位仍被 `LIKE 'i1:1%'` 命中；
  ⑤ cleaned 计数 == 迁移前 SUM(text_clean IS NOT NULL)；
  ⑥ 反向用例：把某行 text 改脏后重跑，text 仍是脏值（迁移不越权修正文）；
  ⑦ 对活库路径不加 --force-live 必须拒绝执行。

副本用裸 sqlite3 建 ~2000 行（等值/不等值/JSON integrity/紧凑 integrity/
空 text_clean/含中文换行/带附加键），**不走 app.db 全局引擎**（那是 conftest 的
临时库，与离线脚本各读各的路径）——离线迁移器吃的就是「一个 db 文件路径」。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import slim_segments as SLIM  # noqa: E402
from app import segment_integrity as si  # noqa: E402

SEG_DDL = """
CREATE TABLE segments (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    chapter TEXT,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_clean TEXT,
    n_sentences INTEGER DEFAULT 0,
    n_chars INTEGER DEFAULT 0,
    integrity TEXT,
    role TEXT,
    seg_version INTEGER DEFAULT 1,
    created_at TEXT
)
"""


def _integ_json(text: str, ordinal: int, *, extra: dict | None = None) -> str:
    d = si.analyze(text, ordinal=ordinal)
    if extra:
        d.update(extra)
    return json.dumps(d, ensure_ascii=False)


def _build(db: Path) -> dict:
    """建 ~2000 行混合副本；返回每行迁移前的快照（按 id 索引）。"""
    con = sqlite3.connect(str(db))
    con.executescript(SEG_DDL)
    CN = "祠堂前的雪还没化，陈三爷来得最早。\n他把灯笼挂上门框，试了三次才扣好。\n"
    CLEAN = "祠堂前的雪还没化，陈三爷来得最早。他把灯笼挂上门框，试了三次才扣好。"  # 去换行=真清洗结果
    rows = []
    i = 0
    for i in range(2000):
        rid_kind = i % 5
        if rid_kind == 0:                    # 等值行（text_clean == text，含中文换行）
            text, clean = CN, CN
        elif rid_kind == 1:                  # 不等值行（真清洗：text_clean != text）
            text, clean = CN, CLEAN
        elif rid_kind == 2:                  # 空 text_clean 行（清洗产出为空，text 非空）
            text, clean = CN, ""
        elif rid_kind == 3:                  # text_clean 为 NULL（未清洗）
            text, clean = CN, None
        else:                                # 等值行（单行正文，无换行）
            text, clean = CLEAN, CLEAN

        # integrity 形态轮换：JSON 基键（可压）/ 已紧凑 / 带附加键（不可压）
        if i % 3 == 0:
            integ = _integ_json(text, i)
        elif i % 3 == 1:
            integ = si.pack_raw(_integ_json(text, i))   # 预置紧凑形态
        else:
            integ = _integ_json(text, i, extra={"src_ok": True, "severity": "low"})

        seg_id = f"SEG{i:06d}"
        rows.append((seg_id, f"WK{i % 7:03d}", None, i, text, clean,
                     3, len(text), integ, None, 1, f"2026-09-{(i % 28) + 1:02d}T00:00:00Z"))
    con.executemany(
        "INSERT INTO segments (id,work_id,chapter,ordinal,text,text_clean,"
        "n_sentences,n_chars,integrity,role,seg_version,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    snap = _snapshot(con)
    con.close()
    return snap


def _snapshot(con: sqlite3.Connection) -> dict:
    con.row_factory = sqlite3.Row
    out = {}
    for r in con.execute("SELECT * FROM segments ORDER BY rowid").fetchall():
        d = dict(r)
        tc = d["text_clean"]
        out[d["id"]] = {
            "rowid": d["rowid"], "work_id": d["work_id"], "ordinal": d["ordinal"],
            "n_chars": d["n_chars"], "created_at": d["created_at"],
            "text": d["text"],
            # 两种有效正文口径 + integrity loads_any（None 归一，便于比较）
            "eff_strict": tc if tc is not None else d["text"],
            "eff_or": (tc or d["text"] or ""),
            "tc_was_null": tc is None,
            "tc_was_nonnull": tc is not None,
            "integ": si.loads_any(d["integrity"]) if d["integrity"] is not None else None,
            "integ_raw": d["integrity"],
        }
    con.row_factory = None
    return out


def _readback(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    snap = _snapshot(con)
    n = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    cols = {r[1] for r in con.execute("PRAGMA table_info(segments)").fetchall()}
    cleaned = con.execute("SELECT COUNT(*) FROM segments WHERE cleaned=1").fetchone()[0] \
        if "cleaned" in cols else None
    con.close()
    return {"rows": n, "cleaned": cleaned, "by_id": snap}


# ── 主回归 ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    db = tmp_path_factory.mktemp("slim") / "copy.db"
    before = _build(db)
    assert SLIM.run(str(db)) == 0
    return db, before, _readback(db)


def test_row_count_and_cleaned_delta(migrated):
    db, before, after = migrated
    assert after["rows"] == len(before) == 2000
    # ⑤ cleaned 计数 == 迁移前 SUM(text_clean IS NOT NULL)（含空串）
    pre_nonnull = sum(1 for v in before.values() if v["tc_was_nonnull"])
    assert after["cleaned"] == pre_nonnull


def test_immutable_columns_byte_identical(migrated):
    db, before, after = migrated
    for sid, b in before.items():
        a = after["by_id"][sid]
        assert a["rowid"] == b["rowid"]
        assert a["work_id"] == b["work_id"]
        assert a["ordinal"] == b["ordinal"]
        assert a["n_chars"] == b["n_chars"]
        assert a["created_at"] == b["created_at"]


def test_text_column_never_touched(migrated):
    """② text 逐字节不变 —— 迁移只许动 text_clean/integrity/cleaned。"""
    db, before, after = migrated
    for sid, b in before.items():
        assert after["by_id"][sid]["text"] == b["text"], sid


@pytest.mark.parametrize("key", ["eff_strict", "eff_or"])
def test_effective_text_invariant_both_readers(migrated, key):
    """③ 有效正文逐字节不变（两种读侧口径都钉：semantic_review 与 api/内容锚）。"""
    db, before, after = migrated
    for sid, b in before.items():
        assert after["by_id"][sid][key] == b[key], (sid, key)


def test_integrity_loads_any_equivalent(migrated):
    """④ loads_any 前后等价；含附加键行原样保留。"""
    db, before, after = migrated
    for sid, b in before.items():
        assert after["by_id"][sid]["integ"] == b["integ"], sid


def test_equal_rows_nulled_but_unequal_preserved(migrated):
    """text_clean==text 的行必须归 NULL；不等值（真清洗/空串）行必须原样保留。"""
    db, before, after = migrated
    con = sqlite3.connect(str(db))
    n_null, kept_diff = 0, 0
    for sid, b in before.items():
        tc = con.execute("SELECT text_clean FROM segments WHERE id=?", (sid,)).fetchone()[0]
        kind = _classify(b, sid)
        if kind == "equal":
            assert tc is None, (sid, "等值行 text_clean 应归 NULL")
            n_null += 1
        elif kind in ("different", "empty"):
            assert tc is not None, (sid, "非等值行 text_clean 必须保留")
            kept_diff += 1
    con.close()
    assert n_null > 0 and kept_diff > 0


def _classify(b, sid):
    """由构造规律反推原行类别（sid 尾号 i → i%5）。"""
    i = int(sid[3:])
    k = i % 5
    return {0: "equal", 1: "different", 2: "empty", 3: "null", 4: "equal"}[k]


def test_eligible_prefix_like_still_hits(migrated):
    """④ 紧凑行 eligible 位仍可被 SQL LIKE 'i1:1%' 命中；带附加键行仍是合法 JSON 供裸 SQL 读。"""
    db, before, after = migrated
    con = sqlite3.connect(str(db))
    # 至少一行紧凑且 eligible=True 应被 'i1:1%' 命中
    hits = con.execute(
        "SELECT COUNT(*) FROM segments WHERE integrity LIKE 'i1:1%'").fetchone()[0]
    assert hits > 0
    # 带 src_ok 的行必须仍是原始 JSON（含 src_ok 子串），未被误压
    with_src = [sid for i, (sid, b) in enumerate(before.items())
                if i % 3 == 2 and _classify(b, sid) is not None and "src_ok" in (b["integ_raw"] or "")]
    assert with_src
    for sid in with_src:
        raw = con.execute("SELECT integrity FROM segments WHERE id=?", (sid,)).fetchone()[0]
        assert raw == before[sid]["integ_raw"], (sid, "带附加键行 integrity 不应被改写")
        assert "src_ok" in raw and json.loads(raw)["src_ok"] is True
    con.close()


def test_reverse_dirty_text_not_fixed(tmp_path):
    """⑥ 反向用例：把某行 text 改脏后重跑，text 必须仍是脏值（迁移不越权修正文）。"""
    db = tmp_path / "rev.db"
    before = _build(db)
    assert SLIM.run(str(db)) == 0
    con = sqlite3.connect(str(db))
    target = next(sid for i, sid in enumerate(before)
                  if _classify(before[sid], sid) == "null")
    dirty = "\nDIRTY\n" + con.execute(
        "SELECT text FROM segments WHERE id=?", (target,)).fetchone()[0]
    con.execute("UPDATE segments SET text=? WHERE id=?", (dirty, target))
    con.commit()
    con.close()

    assert SLIM.run(str(db)) == 0
    con = sqlite3.connect(str(db))
    got = con.execute("SELECT text FROM segments WHERE id=?", (target,)).fetchone()[0]
    con.close()
    assert got == dirty, "迁移不得「顺手修正文」"
    # 其余行 text 也未被动过
    after = _readback(db)
    for sid, b in before.items():
        if sid != target:
            assert after["by_id"][sid]["text"] == b["text"]


def test_live_path_refused_without_force(tmp_path):
    """⑦ 对活库路径不加 --force-live 必须拒绝执行（退出码 2）。"""
    assert SLIM.is_live_db("D:/language-genome-data/language_genome.db") is True
    assert SLIM.is_live_db("D:\\language-genome-data\\language_genome.db") is True
    assert SLIM.is_live_db("f:/AGI/language-genome/data/LANGUAGE_GENOME.DB") is True
    assert SLIM.is_live_db(str(tmp_path / "copy.db")) is False
    rc = SLIM.main(["--db", "D:/language-genome-data/language_genome.db"])
    assert rc == 2
    # 非活库的 dry-run 必须放行（返回 0）
    db = tmp_path / "ok.db"
    _build(db)
    assert SLIM.main(["--db", str(db), "--dry-run"]) == 0


def test_dry_run_zero_write(tmp_path):
    """--dry-run：零写入 —— text_clean/integrity/cleaned 全不动。"""
    db = tmp_path / "dry.db"
    before = _build(db)
    assert SLIM.run(str(db), dry_run=True) == 0
    after = _readback(db)
    for sid, b in before.items():
        assert after["by_id"][sid]["text"] == b["text"]
        assert after["by_id"][sid]["integ_raw"] == b["integ_raw"]
        # dry-run 不建列也不回填：非等值行的 text_clean 保持原值
    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(segments)").fetchall()}
    n_notnull = con.execute(
        "SELECT COUNT(*) FROM segments WHERE text_clean IS NOT NULL").fetchone()[0]
    con.close()
    assert "cleaned" not in cols                       # DRY-RUN 绝不改 schema
    pre_nonnull = sum(1 for v in before.values() if v["tc_was_nonnull"])
    assert n_notnull == pre_nonnull                    # 一行都没归 NULL


def test_limit_scope_and_vacuum(tmp_path):
    """--limit 只处理前 N 行；--vacuum 后物理文件仍可读且行数不变。"""
    db = tmp_path / "lim.db"
    before = _build(db)
    assert SLIM.run(str(db), limit=500, vacuum=True) == 0
    con = sqlite3.connect(str(db))
    # 前 500 行（rowid<=500）里的等值行应已归 NULL；范围外等值行仍非 NULL
    in_scope = con.execute(
        "SELECT COUNT(*) FROM segments WHERE rowid<=500 AND text_clean IS NOT NULL "
        "AND text_clean = text").fetchone()[0]
    out_scope = con.execute(
        "SELECT COUNT(*) FROM segments WHERE rowid>500 AND text_clean IS NOT NULL "
        "AND text_clean = text").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    con.close()
    assert in_scope == 0
    assert out_scope > 0
    assert total == 2000
