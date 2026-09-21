"""K1-A 回归：来源登记与镜像对账（知识化方案 §4.1 / 监督 2026-09-21）。

锁死契约：
1. 登记全覆盖（漏登/悬空都是 mismatch）；空登记表宁拒不恒绿；
2. 镜像（works.v2_of）的 canonical 必须回连根作品——**不计独立复现**；
   独立人类源计数只认根作品；fixture 只验契约不加分；
3. corpus_v2_map 逐条回连（悬空= mismatch；文件缺席如实记 skipped
   不冒充通过）；
4. register_work_sources 幂等登记：可考据才填（作者核对依据入表），
   不可考据留空 + metadata_status 如实；
5. 基准源普查（根作品的 benchmark 段计数）进证据——K2 隔离三查的
   源身份层。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import register_work_sources as REG        # noqa: E402
import verify_work_registry as VRW         # noqa: E402
from app import db                         # noqa: E402
from app.models import Author, Genre, Segment, Work, WorkSource  # noqa: E402

TXT = "夜风把窗纸吹得鼓了一下，屋里静得能听见灯芯燃烧的声音，他坐着没有说话。"


def _mk(work_id, title, *, v2_of=None, source="file:test", n_segs=2, role=None):
    with db.session() as s:
        s.query(Work).filter(Work.id == work_id).delete()
        w = Work(id=work_id, title=title, source=source, v2_of=v2_of)
        s.add(w)
        s.flush()
        for i in range(n_segs):
            s.add(Segment(work_id=w.id, ordinal=i, text=TXT,
                           n_sentences=1, n_chars=len(TXT), role=role))
        s.commit()


def _reg(work_id, canonical, source_type, *, author_id=None, title="t"):
    with db.session() as s:
        old = s.query(WorkSource).filter_by(work_id=work_id).first()
        if old:
            s.delete(old)
            s.flush()          # 先落删——同 work_id 的 UNIQUE 不许撞
        s.add(WorkSource(work_id=work_id, canonical_work_id=canonical,
                         author_id=author_id, genre_ids=[],
                         source_type=source_type, text_version="corpus-v1",
                         purpose_basis="test", allowed_purposes=["research"],
                         metadata_status="verified", metadata_basis="test"))
        s.commit()


@pytest.fixture()
def clean_tree(tmp_path, monkeypatch):
    """根作品 + corpus v2 镜像 + fixture 的最小树 + 有效 corpus_v2_map。

    共享库现实：全量套件跑到本文件时库里已有其他测试文件遗留的 Work——
    对账的**全覆盖**是生产契约（必须保留），所以给存量作品补 synthetic
    占位登记（不计人类源、不给任何计数加分），teardown 只删自己建的行。"""
    db.init_db()
    with db.session() as s:
        existing = [w for w in s.query(Work).all()]
    placeholders: list[str] = []
    for w in existing:
        if s_worksource_exists(w.id):
            continue                     # 已有登记行（别的测试或本文件）不碰
        if w.v2_of:                       # 他文件留下的 corpus v2 镜像：
            _reg(w.id, w.v2_of, "human_fiction")   # 占位也必须回连根（对账口径）
        else:
            ttype = "fixture" if REG._is_fixture(w) else "synthetic"
            _reg(w.id, w.id, ttype)
        placeholders.append(w.id)
    _mk("WK-troot", "试作根（测试侠）", n_segs=3, role="benchmark")
    _mk("WK-tmirror", "试作根（测试侠）（corpus v2）", v2_of="WK-troot", n_segs=3)
    _mk("WK-tfix", "fixture_test", source="inbox:fixture_test.txt", n_segs=1)
    with db.session() as s:
        root_seg = (s.query(Segment).filter(Segment.work_id == "WK-troot")
                    .order_by(Segment.ordinal).all())
    _reg("WK-troot", "WK-troot", "human_fiction")
    _reg("WK-tmirror", "WK-troot", "human_fiction")
    _reg("WK-tfix", "WK-tfix", "fixture")
    # 有效 map：两条 v1_segment 都在库里
    mp = tmp_path / "map.jsonl"
    mp.write_text("\n".join(
        json.dumps({"work": "试作根（测试侠）", "v1_segment": seg.id,
                    "ordinal": i}, ensure_ascii=False)
        for i, seg in enumerate(root_seg)) + "\n", encoding="utf-8")
    monkeypatch.setattr(VRW, "V2_MAP", mp)
    yield {}
    with db.session() as s:
        # 树：本测试建的 Work/Segment/登记行全删
        for wid in ("WK-troot", "WK-tmirror", "WK-tfix"):
            s.query(Segment).filter_by(work_id=wid).delete()
            s.query(WorkSource).filter_by(work_id=wid).delete()
            s.query(Work).filter_by(id=wid).delete()
        # 占位：**只删我建的登记行**——Work/Segment 是其他测试文件的数据，
        # 碰了就是破坏共享库
        for wid in placeholders:
            s.query(WorkSource).filter_by(work_id=wid).delete()
        s.commit()


def s_worksource_exists(work_id: str) -> bool:
    with db.session() as s:
        return s.query(WorkSource).filter_by(work_id=work_id).first() is not None


def test_clean_tree_passes_and_counts_roots_only(clean_tree):
    rep = VRW.verify()
    assert rep["n_mismatch"] == 0
    assert rep["independent_human_sources"] == 1, \
        "镜像/fixture 不计独立人类源——只认根作品"
    root = rep["root_works"][0]
    assert root["n_segments"] == 3 and root["n_benchmark_segments"] == 3, \
        "基准源普查（隔离三查的源身份层）必须入证据"
    mine_mirror = [m for m in rep["mirror_works"] if m["work_id"] == "WK-tmirror"]
    assert mine_mirror and mine_mirror[0]["canonical_work_id"] == "WK-troot"
    assert rep["corpus_v2_map"]["checked"] is True


def test_missing_registry_is_mismatch(clean_tree):
    with db.session() as s:
        s.query(WorkSource).filter_by(work_id="WK-tmirror").delete()
        s.commit()
    rep = VRW.verify()
    assert any(m["kind"] == "work_without_registry" and m["work"] == "WK-tmirror"
               for m in rep["mismatch"])


def test_empty_registry_refuses(clean_tree):
    with db.session() as s:
        s.query(WorkSource).delete()
        s.commit()
    with pytest.raises(SystemExit, match="宁拒不恒绿"):
        VRW.verify()


def test_mirror_canonical_mismatch(clean_tree):
    _reg("WK-tmirror", "WK-tmirror", "human_fiction")   # 错：不回连根
    rep = VRW.verify()
    assert any(m["kind"] == "mirror_canonical_mismatch" for m in rep["mismatch"])


def test_fixture_type_mismatch(clean_tree):
    _reg("WK-tfix", "WK-tfix", "human_fiction")          # fixture 冒充人类语料
    rep = VRW.verify()
    assert any(m["kind"] == "fixture_type_mismatch" for m in rep["mismatch"])


def test_dangling_registry_row(clean_tree):
    """悬空登记行只能从非 ORM 路径进来（raw SQL / 库恢复）——对账必须有
    这道防线；用原生 sqlite 连接（默认 FK 关）注入。"""
    import sqlite3
    from app import config as appcfg
    path = appcfg.DATABASE_URL.replace("sqlite:///", "")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO work_sources (id, work_id, canonical_work_id,"
                " author_id, genre_ids, source_type, text_version,"
                " text_sha256, purpose_basis, allowed_purposes,"
                " metadata_status, metadata_basis, created_at)"
                " VALUES ('WSRC-dangle','WK-ghost','WK-ghost',NULL,'[]',"
                "'human_fiction','corpus-v1',NULL,'test','[]','verified',"
                "'raw 注入（绕 ORM 的 FK）','t')")
    con.commit()
    con.close()
    try:
        rep = VRW.verify()
        assert any(m["kind"] == "registry_dangling_work" for m in rep["mismatch"])
    finally:
        con = sqlite3.connect(path)
        con.execute("DELETE FROM work_sources WHERE id='WSRC-dangle'")
        con.commit()
        con.close()


def test_map_orphan_entry_is_mismatch(clean_tree, tmp_path, monkeypatch):
    mp = tmp_path / "bad.jsonl"
    mp.write_text(json.dumps({"work": "x", "v1_segment": "SEG-nope",
                              "ordinal": 0}, ensure_ascii=False) + "\n",
                  encoding="utf-8")
    monkeypatch.setattr(VRW, "V2_MAP", mp)
    rep = VRW.verify()
    assert any(m["kind"] == "map_orphan_entry" for m in rep["mismatch"])


def test_map_absent_recorded_as_skipped(clean_tree, tmp_path, monkeypatch):
    monkeypatch.setattr(VRW, "V2_MAP", tmp_path / "nope.jsonl")
    rep = VRW.verify()
    assert rep["corpus_v2_map"]["checked"] is False, \
        "map 缺席如实记 skipped——不冒充通过"


def test_register_idempotent_and_honest(tmp_path, monkeypatch):
    """register_work_sources：幂等登记；可考据才填，不可考据留空+partial。"""
    db.init_db()
    _mk("WK-r1", "试作甲（测试侠）", n_segs=1)
    _mk("WK-r1m", "试作甲（测试侠）（corpus v2）", v2_of="WK-r1", n_segs=1)
    _mk("WK-r1f", "fixture_probe", source="inbox:fixture_probe.txt", n_segs=1)
    _mk("WK-r2", "无名氏作品（精校）", n_segs=1)          # 不可考据
    monkeypatch.setattr(REG, "_ROOT_META", {
        "试作甲（测试侠）": {"author": "猫腻", "genres": ["玄幻"],
                        "basis": "核对测试"},
        "无名氏作品（精校）": {"author": None, "genres": [],
                          "basis": "不可考据测试：留空待补"}})
    monkeypatch.setitem(REG._AUTHORS, "猫腻", "测试核对依据")
    my_ids = {"WK-r1", "WK-r1m", "WK-r1f", "WK-r2"}
    try:
        rows = REG.register(only=my_ids)   # 作用域登记：共享库他作品不在此列
        rows = [r for r in rows if r["work_id"] in my_ids]
        by = {r["work_id"]: r for r in rows}
        assert by["WK-r1"]["canonical_work_id"] == "WK-r1"
        assert by["WK-r1m"]["canonical_work_id"] == "WK-r1", "镜像回连根作品"
        assert by["WK-r1f"]["source_type"] == "fixture", "夹具分型不给人类计数加分"
        assert by["WK-r2"]["author_id"] is None and \
            by["WK-r2"]["metadata_status"] == "partial", \
            "不可考据：留空 + partial，不许猜"
        with db.session() as s:
            a = s.query(Author).filter_by(name="猫腻").first()
            assert a and "测试核对依据" in a.verified_basis, "作者核对依据入表"
            g = s.query(Genre).filter_by(name="玄幻").first()
            assert g and g.id in by["WK-r1"]["genre_ids"]
        # 幂等：重跑不报错、行数不翻倍
        rows2 = REG.register(only=my_ids)
        rows2 = [r for r in rows2 if r["work_id"] in my_ids]
        with db.session() as s:
            n = s.query(WorkSource).filter(
                WorkSource.work_id.in_(["WK-r1", "WK-r1m", "WK-r1f", "WK-r2"])).count()
            assert n == 4, "幂等：一行一 Work"
        assert len(rows2) == len(rows)
    finally:
        with db.session() as s:
            for wid in ("WK-r1", "WK-r1m", "WK-r1f", "WK-r2"):
                s.query(Segment).filter_by(work_id=wid).delete()
                s.query(WorkSource).filter_by(work_id=wid).delete()
                s.query(Work).filter_by(id=wid).delete()
            s.commit()
