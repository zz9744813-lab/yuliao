"""K1-A 回归：来源登记与镜像对账（知识化方案 §4.1 / 会审二轮否决逐条）。

锁死契约（qwen 席六条否决的反例全覆盖）：
1. 全覆盖：漏登/悬空 mismatch；空登记表宁拒不恒绿；
2. 悬空三侧：author_id / genre_ids / canonical_work_id 无 FK 的列
   由对账承担完整性（悬空即 mismatch）；
3. 镜像回连到**真根**：canonical 链回溯 + 防环（A↔B 互指）、自指
   镜像（v2_of==自身）虚增计数被拒；继承（author/genre）逐行比对；
4. 独立人类源只认真根（human_fiction + canonical==自身 + v2_of 空）；
   fixture/synthetic/镜像不加分；
5. 内容锚 = 登记时的事实：库内容漂移 → register 响亮报错 +
   verify anchor_drift；--reset-anchor 显式重锚；0 段作品锚为 NULL；
6. 身份≠授权：license_purposes 非空必须带 license_basis，且只允许
   training_source/benchmark_source 两个词；
7. register 幂等、可考据才填（不可考据留空+partial）、未知根作品
   标题响亮失败、dry-run 与正式输出同形（text_sha256/n_segments）；
8. corpus_v2_map 逐条回连；缺席如实 skipped 不冒充通过。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import register_work_sources as REG        # noqa: E402
import verify_work_registry as VRW          # noqa: E402
from app import db                         # noqa: E402
from app.models import Author, Genre, Segment, Work, WorkSource  # noqa: E402
from registry_anchor import anchor as _anchor  # noqa: E402  单一哈希来源

TXT = "夜风把窗纸吹得鼓了一下，屋里静得能听见灯芯燃烧的声音，他坐着没有说话。"


def _mk(work_id, title, *, v2_of=None, source="file:test", n_segs=2, role=None):
    with db.session() as s:
        # 先删旧 Segment/登记行——重跑同 id 会累积同 ordinal 分段（会审
        # 二轮）；登记行不删会 FK 挡住 Work 删除
        s.query(Segment).filter_by(work_id=work_id).delete()
        s.query(WorkSource).filter_by(work_id=work_id).delete()
        s.query(Work).filter_by(id=work_id).delete()
        w = Work(id=work_id, title=title, source=source, v2_of=v2_of)
        s.add(w)
        s.flush()
        for i in range(n_segs):
            s.add(Segment(work_id=w.id, ordinal=i, text=TXT,
                           n_sentences=1, n_chars=len(TXT), role=role))
        s.commit()


def _reg(work_id, canonical, source_type, *, author_id=None, genre_ids=None,
         license_purposes=None, license_basis=None):
    """手工造登记行（含内容锚——锚复核必须过；锚与占位路径同源）。"""
    with db.session() as s:
        sha = _anchor(s, work_id)
        old = s.query(WorkSource).filter_by(work_id=work_id).first()
        if old:
            s.delete(old)
            s.flush()          # 先落删——同 work_id 的 UNIQUE 不许撞
        s.add(WorkSource(work_id=work_id, canonical_work_id=canonical,
                         author_id=author_id, genre_ids=genre_ids or [],
                         source_type=source_type, text_version="corpus-v1",
                         text_sha256=sha, purpose_basis="test",
                         identity_purposes=["research"],
                         license_purposes=license_purposes or [],
                         license_basis=license_basis,
                         metadata_status="verified", metadata_basis="test"))
        s.commit()


def _worksource_exists(work_id: str) -> bool:
    with db.session() as s:
        return s.query(WorkSource).filter_by(work_id=work_id).first() is not None


@pytest.fixture()
def clean_tree(tmp_path, monkeypatch):
    """根作品 + corpus v2 镜像 + fixture 的最小树 + 有效 corpus_v2_map。

    共享库现实：全量套件跑到本文件时库里已有其他测试文件遗留的 Work——
    对账的**全覆盖**是生产契约（必须保留），所以给存量作品补占位登记
    （镜像占位回连其根；fixture 用统一分类器），teardown 只删自建行。"""
    db.init_db()
    with db.session() as s:
        existing = [w for w in s.query(Work).all()]
    placeholders: list[str] = []
    for w in existing:
        if _worksource_exists(w.id):
            continue                     # 已有登记行（别的测试或本文件）不碰
        if w.v2_of:                       # 遗留镜像：占位也必须回连根
            _reg(w.id, w.v2_of, "human_fiction")
        else:
            _reg(w.id, w.id,
                 "fixture" if REG._is_fixture(w) else "synthetic")
        placeholders.append(w.id)
    _mk("WK-troot", "试作根（测试侠）", n_segs=3, role="benchmark")
    _mk("WK-tmirror", "试作根（测试侠）（corpus v2）", v2_of="WK-troot", n_segs=3)
    _mk("WK-tfix", "fixture_test", source="inbox:fixture_test.txt", n_segs=1)
    # 测试树的登记元数据（register 直调的用例共用）；词表清空——
    # fixture 内不许对真实作者/题材词汇产生写
    monkeypatch.setattr(REG, "_ROOT_META", {
        "试作根（测试侠）": {"author": None, "genres": [],
                       "basis": "测试树根：无作者（fixture）"},
        "空作品（测试）": {"author": None, "genres": [],
                     "basis": "空作品测试（fixture）"}})
    monkeypatch.setattr(REG, "_AUTHORS", {})
    monkeypatch.setattr(REG, "_GENRES", set())
    _reg("WK-troot", "WK-troot", "human_fiction")
    _reg("WK-tmirror", "WK-troot", "human_fiction")
    _reg("WK-tfix", "WK-tfix", "fixture")
    with db.session() as s:
        root_seg = (s.query(Segment).filter(Segment.work_id == "WK-troot")
                    .order_by(Segment.ordinal).all())
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
        # 占位：**只删我建的登记行**——Work/Segment 是其他测试文件的数据
        for wid in placeholders:
            s.query(WorkSource).filter_by(work_id=wid).delete()
        s.commit()


def test_clean_tree_passes_and_roots_only(clean_tree):
    rep = VRW.verify()
    assert rep["n_mismatch"] == 0, rep["mismatch"][:5]
    roots = {r["work_id"] for r in rep["root_works"]}
    assert "WK-troot" in roots, "自建根必须在独立人类源里"
    mine_root = [r for r in rep["root_works"] if r["work_id"] == "WK-troot"][0]
    assert mine_root["n_segments"] == 3 and mine_root["n_benchmark_segments"] == 3, \
        "基准源普查（隔离三查的源身份层）必须入证据"
    # 占位/自建的镜像与 fixture 都不许进独立源
    assert "WK-tmirror" not in roots and "WK-tfix" not in roots
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


def test_root_canonical_mismatch(clean_tree):
    _reg("WK-troot", "WK-tfix", "human_fiction")         # 根不指向自身
    rep = VRW.verify()
    assert any(m["kind"] == "root_canonical_mismatch" for m in rep["mismatch"])


def test_fixture_type_mismatch_uses_unified_classifier(clean_tree):
    """统一分类器（source 或 title）——仅靠 source 前缀识别的夹具被登记成
    human_fiction 也必须被拦（会审二轮：两套口径的洞）。"""
    _mk("WK-srcfix", "低调夹具", source="inbox:fixture_quiet.txt", n_segs=1)
    try:
        _reg("WK-srcfix", "WK-srcfix", "human_fiction")   # 标题不带 fixture
        rep = VRW.verify()
        assert any(m["kind"] == "fixture_type_mismatch" and m["work"] == "WK-srcfix"
                   for m in rep["mismatch"])
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(work_id="WK-srcfix").delete()
            s.query(WorkSource).filter_by(work_id="WK-srcfix").delete()
            s.query(Work).filter_by(id="WK-srcfix").delete()
            s.commit()


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
                " identity_purposes, license_purposes, license_basis,"
                " metadata_status, metadata_basis, created_at)"
                " VALUES ('WSRC-dangle','WK-ghost','WK-ghost',NULL,'[]',"
                "'human_fiction','corpus-v1',NULL,'test','[]','[]','[]',NULL,"
                "'verified','raw 注入','t')")
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


def test_author_and_genre_and_canonical_dangling(clean_tree):
    """悬空三侧（会审二轮 BLOCK 项）：author_id / genre_ids /
    canonical_work_id 无 FK，完整性由对账承担。"""
    _mk("WK-dangle", "悬空测试作品", n_segs=1)
    try:
        _reg("WK-dangle", "WK-dangle", "human_fiction",
             author_id="AUTH-nope", genre_ids=["GEN-nope"])
        with db.session() as s:      # canonical 悬空（指向不存在的作品）
            r = (s.query(WorkSource).filter_by(work_id="WK-dangle").first())
            r.canonical_work_id = "WK-nope"
            s.commit()
        rep = VRW.verify()
        kinds = {m["kind"] for m in rep["mismatch"] if m.get("work") == "WK-dangle"}
        assert {"author_dangling", "genre_dangling", "canonical_dangling"} <= kinds
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(work_id="WK-dangle").delete()
            s.query(WorkSource).filter_by(work_id="WK-dangle").delete()
            s.query(Work).filter_by(id="WK-dangle").delete()
            s.commit()


def test_self_mirror_rejected_by_register_and_verify(clean_tree):
    """自指镜像（v2_of==自身）会被 register 响亮拒绝；手工登记成
    canonical=自身 + human_fiction 时 verify 也必须报——不许虚增独立源。"""
    _mk("WK-selfmir", "自指镜像作品", v2_of="WK-selfmir", n_segs=1)
    try:
        with pytest.raises(SystemExit, match="自指镜像"):
            REG.register(only={"WK-selfmir"})
        _reg("WK-selfmir", "WK-selfmir", "human_fiction")
        rep = VRW.verify()
        assert any(m["kind"] == "self_mirror" and m["work"] == "WK-selfmir"
                   for m in rep["mismatch"])
        assert all(r["work_id"] != "WK-selfmir" for r in rep["root_works"]), \
            "自指镜像不许进独立人类源"
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(work_id="WK-selfmir").delete()
            s.query(WorkSource).filter_by(work_id="WK-selfmir").delete()
            s.query(Work).filter_by(id="WK-selfmir").delete()
            s.commit()


def test_mirror_cycle_detected(clean_tree):
    """A↔B 互指镜像：每行 canonical==v2_of 都成立，但链回不到真根——
    环必须被对账点出（会审二轮 BLOCK 项）。"""
    _mk("WK-cycA", "环甲", v2_of="WK-cycB", n_segs=1)
    _mk("WK-cycB", "环乙", v2_of="WK-cycA", n_segs=1)
    try:
        _reg("WK-cycA", "WK-cycB", "human_fiction")
        _reg("WK-cycB", "WK-cycA", "human_fiction")
        rep = VRW.verify()
        kinds = {m["kind"] for m in rep["mismatch"]}
        assert "mirror_root_chain" in kinds, "canonical/v2_of 环必须现形"
    finally:
        with db.session() as s:
            for wid in ("WK-cycA", "WK-cycB"):
                s.query(Segment).filter_by(work_id=wid).delete()
                s.query(WorkSource).filter_by(work_id=wid).delete()
                s.query(Work).filter_by(id=wid).delete()
            s.commit()


def test_mirror_inherit_mismatch(clean_tree):
    """镜像行 author/genre 必须与根一致——继承是登记数据，逐行比对。"""
    _reg("WK-tmirror", "WK-troot", "human_fiction", author_id="AUTH-nope")
    rep = VRW.verify()
    assert any(m["kind"] == "mirror_inherit_mismatch" and m["work"] == "WK-tmirror"
               for m in rep["mismatch"])


def test_license_requires_basis_and_known_purposes(clean_tree):
    """身份≠授权（会审二轮 BLOCK 项）：license_purposes 非空必须带
    license_basis；未知授权词必须报。"""
    _reg("WK-troot", "WK-troot", "human_fiction",
         license_purposes=["training_source"])           # 无依据
    rep = VRW.verify()
    assert any(m["kind"] == "license_without_basis" and m["work"] == "WK-troot"
               for m in rep["mismatch"])
    _reg("WK-troot", "WK-troot", "human_fiction",
         license_purposes=["weird_purpose"], license_basis="有人拍板")
    rep = VRW.verify()
    assert any(m["kind"] == "license_purpose_unknown" and m["work"] == "WK-troot"
               for m in rep["mismatch"])
    # 合法授权形态：带依据 + 已知词 → 不报
    _reg("WK-troot", "WK-troot", "human_fiction",
         license_purposes=["training_source"],
         license_basis="集霸 2026-09-21 授权（测试形态）")
    rep = VRW.verify()
    assert not any("license" in m["kind"] and m.get("work") == "WK-troot"
                   for m in rep["mismatch"])


def test_anchor_drift_detected_by_register_and_verify(clean_tree):
    """内容锚=登记时的事实：库内容变后 register 重跑→SystemExit；
    verify→anchor_drift；--reset-anchor 显式重锚后恢复。"""
    _mk("WK-troot", "试作根（测试侠）", n_segs=3, role="benchmark")   # 内容未变
    REG.register(only={"WK-troot", "WK-tmirror", "WK-tfix"})          # 锚已立
    # 内容漂移：改一段文本
    with db.session() as s:
        seg = (s.query(Segment).filter(Segment.work_id == "WK-troot")
               .order_by(Segment.ordinal).first())
        seg.text = seg.text + "（改动了）"
        s.commit()
    with pytest.raises(SystemExit, match="内容漂移"):
        REG.register(only={"WK-troot"})
    rep = VRW.verify()
    assert any(m["kind"] == "anchor_drift" and m["work"] == "WK-troot"
               for m in rep["mismatch"])
    # 显式重锚（确认漂移无害后）→ 恢复
    REG.register(only={"WK-troot"}, reset_anchor=True)
    rep = VRW.verify()
    assert not any(m["kind"] == "anchor_drift" and m.get("work") == "WK-troot"
                   for m in rep["mismatch"])


def test_zero_segment_work_anchor_is_null(clean_tree):
    _mk("WK-empty", "空作品（测试）", n_segs=0)
    try:
        rows = REG.register(only={"WK-empty"})
        assert rows[0]["text_sha256"] is None, \
            "0 段作品的锚=如实为 NULL，不是缺失也不是假哈希"
        assert rows[0]["n_segments"] == 0
        _reg("WK-empty", "WK-empty", "synthetic")       # 锚 NULL 入库
        rep = VRW.verify()
        assert not any(m["kind"] == "anchor_drift" and m.get("work") == "WK-empty"
                       for m in rep["mismatch"]), "0 段 NULL 锚必须过复核"
    finally:
        with db.session() as s:
            s.query(WorkSource).filter_by(work_id="WK-empty").delete()
            s.query(Work).filter_by(id="WK-empty").delete()
            s.commit()


def test_dry_run_shape_matches_real_and_reads_only(clean_tree):
    """dry-run 与正式输出同形（text_sha256/n_segments 必在）；只读——
    词表 flush 随 close 回滚；既有登记行逐字段零改动（前后快照对比）。"""
    def _snapshot(work_id):
        with db.session() as s:
            r = s.query(WorkSource).filter_by(work_id=work_id).first()
            return {c: getattr(r, c) for c in
                    ("canonical_work_id", "author_id", "genre_ids",
                     "source_type", "text_version", "text_sha256",
                     "purpose_basis", "metadata_status", "metadata_basis")
                    } if r else None
    before = _snapshot("WK-troot")
    rows = REG.register(dry_run=True,
                        only={"WK-troot", "WK-tmirror", "WK-tfix"})
    by = {r["work_id"]: r for r in rows}
    assert {"text_sha256", "n_segments"} <= set(by["WK-troot"].keys()), \
        "dry-run 与正式输出同形——下游解析不得 KeyError"
    assert by["WK-troot"]["n_segments"] == 3 and by["WK-troot"]["text_sha256"]
    after = _snapshot("WK-troot")
    assert before == after, "dry-run 是只读预演——登记行逐字段零改动"


def test_register_idempotent_unknown_title_and_vocab_cleanup(tmp_path, monkeypatch):
    """register：未知根作品标题响亮失败（生产契约）；作用域内幂等、
   可考据才填、镜像继承复制；finally 清理测试自建的词汇与行。"""
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
        # 未知标题（不在作用域外处理）在作用域内不会遇到；作用域外
        # （生产）遇到未知根作品必须响亮失败——用局部库外作品验证
        _mk("WK-unknown", "没登记元数据的陌生作品", n_segs=1)
        with pytest.raises(SystemExit, match="未登记元数据的根作品"):
            REG.register(only={"WK-unknown"})
        with db.session() as s:
            s.query(Segment).filter_by(work_id="WK-unknown").delete()
            s.query(Work).filter_by(id="WK-unknown").delete()
            s.commit()

        rows = REG.register(only=my_ids)
        by = {r["work_id"]: r for r in rows}
        assert by["WK-r1"]["canonical_work_id"] == "WK-r1"
        assert by["WK-r1m"]["canonical_work_id"] == "WK-r1", "镜像回连根作品"
        assert by["WK-r1m"]["author_id"] == by["WK-r1"]["author_id"], \
            "镜像 author 从根复制（继承是登记数据）"
        assert by["WK-r1f"]["source_type"] == "fixture", \
            "夹具分型不给人类计数加分"
        assert by["WK-r2"]["author_id"] is None and \
            by["WK-r2"]["metadata_status"] == "partial", \
            "不可考据：留空 + partial，不许猜"
        with db.session() as s:
            a = s.query(Author).filter_by(name="猫腻").first()
            assert a and "测试核对依据" in a.verified_basis, "作者核对依据入表"
            g = s.query(Genre).filter_by(name="玄幻").first()
            assert g and g.id in by["WK-r1"]["genre_ids"]
            # 授权面默认留空（身份≠授权）
            r1 = s.query(WorkSource).filter_by(work_id="WK-r1").first()
            assert r1.license_purposes == [] and r1.license_basis is None
        # 幂等：重跑不报错、行数不翻倍
        rows2 = REG.register(only=my_ids)
        assert len(rows2) == len(rows)
        with db.session() as s:
            n = s.query(WorkSource).filter(
                WorkSource.work_id.in_(my_ids)).count()
            assert n == 4, "幂等：一行一 Work"
    finally:
        with db.session() as s:
            for wid in ("WK-r1", "WK-r1m", "WK-r1f", "WK-r2"):
                s.query(Segment).filter_by(work_id=wid).delete()
                s.query(WorkSource).filter_by(work_id=wid).delete()
                s.query(Work).filter_by(id=wid).delete()
            # 清理测试自建词汇（register 提交的词表行——会审二轮：不许残留）
            s.query(Author).filter_by(name="猫腻").delete()
            s.query(Genre).filter_by(name="玄幻").delete()
            s.commit()


# ── 会审三轮：授权保留 / 局部镜像 / 词表回滚锁 / 组合口径 ──────────

def test_license_survives_idempotent_rerun(clean_tree, monkeypatch):
    """三轮严重项：license_purposes/license_basis 由集霸授权位在外部写入
    ——幂等重登**不许擦**（旧实现 setattr 全量覆写，授权静默丢失）。"""
    REG.register(only={"WK-troot"})               # 先按 register 路径立行
    with db.session() as s:
        r = s.query(WorkSource).filter_by(work_id="WK-troot").first()
        r.license_purposes = ["training_source"]
        r.license_basis = "集霸 2026-09-21 授权（三轮测试形态）"
        s.commit()
    REG.register(only={"WK-troot"})               # 重登：授权必须原样幸存
    with db.session() as s:
        r = s.query(WorkSource).filter_by(work_id="WK-troot").first()
        assert r.license_purposes == ["training_source"], \
            "幂等重登把外部授予的授权擦空——不可逆授权丢失（三轮严重项）"
        assert r.license_basis == "集霸 2026-09-21 授权（三轮测试形态）"
    rep = VRW.verify()
    assert not any("license" in m["kind"] and m.get("work") == "WK-troot"
                   for m in rep["mismatch"]), "保留的授权+依据必须过授权闸"


def test_only_mirror_reads_root_from_db_or_fails(clean_tree, monkeypatch):
    """三轮一般项：--only 只圈镜像不圈根——根已登记→回读根的登记行
    （继承不许抹空）；根未登记→响亮失败。"""
    # 根已登记（fixture 里 _reg 立过）→ 局部补登镜像继承必须来自根行
    rows = REG.register(only={"WK-tmirror"})
    with db.session() as s:
        r = s.query(WorkSource).filter_by(work_id="WK-tmirror").first()
        assert r.canonical_work_id == "WK-troot"
    # 根未登记 → 响亮失败，不许静默抹空继承
    _mk("WK-r9", "孤根（测试）", n_segs=1)
    _mk("WK-r9m", "孤根（测试）（corpus v2）", v2_of="WK-r9", n_segs=1)
    try:
        with pytest.raises(SystemExit, match="根作品.*未登记"):
            REG.register(only={"WK-r9m"})
    finally:
        with db.session() as s:
            for wid in ("WK-r9", "WK-r9m"):
                s.query(Segment).filter_by(work_id=wid).delete()
                s.query(WorkSource).filter_by(work_id=wid).delete()
                s.query(Work).filter_by(id=wid).delete()
            s.commit()


def test_dry_run_rolls_back_vocabulary_writes(tmp_path):
    """三轮一般项：dry-run 只读承诺的机制依据 = Session close 不提交、
    未 commit 的词表 flush 随之回滚——用**真实词表写入**锁死该语义，
    不许靠『恰好没写』的侥幸。"""
    db.init_db()
    _mk("WK-dry", "干燥跑作品（测试）", n_segs=1)
    uniq_author = "干燥跑专用作者X9"
    try:
        import register_work_sources as R2
        old_authors = dict(R2._AUTHORS)
        old_meta = dict(R2._ROOT_META)
        old_genres = set(R2._GENRES)
        R2._AUTHORS = {uniq_author: "测试依据"}
        R2._ROOT_META = {"干燥跑作品（测试）": {"author": uniq_author,
                                           "genres": set(), "basis": "测试"}}
        R2._GENRES = set()
        try:
            rows = R2.register(dry_run=True, only={"WK-dry"})
            assert rows and rows[0]["text_sha256"] is not None
            with db.session() as s:
                assert s.query(Author).filter_by(name=uniq_author).first() \
                    is None, "dry-run 的词表 flush 必须随 close 回滚（只读承诺）"
                assert s.query(WorkSource).filter_by(work_id="WK-dry").first() \
                    is None, "dry-run 不落登记行"
        finally:
            R2._AUTHORS = old_authors
            R2._ROOT_META = old_meta
            R2._GENRES = old_genres
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(work_id="WK-dry").delete()
            s.query(WorkSource).filter_by(work_id="WK-dry").delete()
            s.query(Work).filter_by(id="WK-dry").delete()
            s.query(Author).filter_by(name=uniq_author).delete()
            s.commit()


def test_fixture_mirror_combo_fixture_wins(clean_tree, monkeypatch):
    """三轮建议：既是 fixture 又是 v2_of 镜像的组合口径——fixture 分类
    胜出（register 的 fixture 分支先于镜像分支，verify 的镜像检查跳过
    fixture 行），两脚本口径一致，不许互相打架。"""
    _mk("WK-fm", "fixture_镜像组合", source="inbox:fixture_combo.txt",
        v2_of="WK-troot", n_segs=1)
    try:
        rows = REG.register(only={"WK-fm"})
        assert rows[0]["source_type"] == "fixture", "组合口径：fixture 胜出"
        assert rows[0]["canonical_work_id"] == "WK-fm"
        rep = VRW.verify()
        assert not any(m.get("work") == "WK-fm" for m in rep["mismatch"]), \
            "fixture+镜像组合不许被 verify 的镜像检查误报"
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(work_id="WK-fm").delete()
            s.query(WorkSource).filter_by(work_id="WK-fm").delete()
            s.query(Work).filter_by(id="WK-fm").delete()
            s.commit()
