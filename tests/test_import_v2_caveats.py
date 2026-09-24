"""《覆汉》入库三条 caveat 修复回归（独立核查席 IMPORT_CHECK.md 实锤）。

钉死三条修复 + 一条红线（默认路径不变）：
1. 卷首元数据（书名行 / `作者：X` / `内容简介：` 头块）在 `caveats=True` 下
   不作为正文段入库，原文逐行记账进 `Work.anchors["front_matter"]`（不静默丢弃），
   作者回填 `Work.author`；
2. 末段截断（全篇最后一段段尾无句读）显式标记：该段 integrity JSON 键
   `"truncated": true` + `anchors["last_truncated"]`——复用既有 JSON 字段体系，
   **不加列**（建表史 NOT NULL 无默认的老坑），并证明向后兼容；
3. chapter 回填复用 `segmenter_v2._CHAPTER_RE`：正文里章节标题行→该章各段
   `Segment.chapter`；解析不到留空（None），不猜。
4. 默认路径逐字不变：不带开关时段内容与裸 `make_segments_v2` 一致、
   chapter 全空、integrity 键集与 `segment_integrity.analyze` 全等、
   anchors 无新增键——既有 tests/test_import_corpus_v2_resume.py 必须继续全绿。

自包含：conftest 的临时 sqlite + tmp_path 造书，零网络、零真实库。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = _u.spec_from_file_location("ic_cav", ROOT / "scripts" / "import_corpus_v2.py")
IC = _u.module_from_spec(_spec); _spec.loader.exec_module(IC)

from app import corpus_import_v2 as civ2, db, segment_integrity as si, segmenter_v2  # noqa: E402
from app.models import Segment, Work                                                  # noqa: E402

_n = [0]


def _para(i) -> str:
    """一个能独立成 v2 段的正文段（≥40 字、终止标点收尾、非风险开场、无引号）。"""
    return (f"甲{i}：他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"
            f"隔壁屋的灯还亮着，影子在窗纸上晃了两下。")


# 复刻核查席采到的《覆汉》头部样本（caveat 1 的原文证据）
FRONT = ["覆汉", "作者：榴弹怕水", "内容简介：", "努力闻达于诸侯，以求苟全性命于乱世！"]
# 裸书名行识别依赖导入标题提示（_classify_meta 精确匹配）⇒ 凡带头部块的用例
# 导入标题必须传 TITLE（与 FRONT[0] 一致），否则剥离退化为 no-op。
TITLE = "覆汉"
TAIL_TRUNC = "双方从上午战到日落，孙文台"   # caveat 2：全书最后一段断在半句


def _book(tmp_path: Path, *, front: bool = True, chapters: bool = True,
          truncated_tail: bool = True) -> str:
    lines: list[str] = []
    if front:
        lines += FRONT + [""]
    if chapters:
        lines += ["第1章 初临汉末", _para(10), _para(11), "", "第2章 风起",
                  _para(20), _para(21)]
    else:
        lines += [_para(10), _para(11), _para(20), _para(21)]
    lines.append(TAIL_TRUNC if truncated_tail else _para(99))
    _n[0] += 1
    fp = tmp_path / f"cav-{_n[0]}.txt"
    fp.write_text("\n".join(lines), encoding="utf-8")
    return str(fp)


def _work(path: str) -> Work:
    with db.session() as s:
        return s.query(Work).filter_by(source=f"file:{path}").one()


def _segs(work_id: str) -> list:
    """session 内取纯值（ORM 对象出 session 属性访问不可靠）。"""
    from collections import namedtuple
    Row = namedtuple("Row", "ordinal text chapter integrity")
    with db.session() as s:
        return [Row(g.ordinal, g.text, g.chapter, g.integrity) for g in
                (s.query(Segment).filter(Segment.work_id == work_id)
                 .order_by(Segment.ordinal).all())]


# ── 红线：默认路径逐字不变 ──────────────────────────────────

def test_default_path_unchanged_still_carries_old_caveats(tmp_path):
    """不带开关：段内容 == 裸 make_segments_v2(全文)，首段仍是元数据脏段，
    chapter 全空，integrity 无新键，anchors 无新键——下游一切按旧口径。"""
    path = _book(tmp_path)
    assert IC.main([path, "默认本", "训练语料"]) == 0
    w = _work(path)
    from app.corpus import _read_text_loose
    plain = segmenter_v2.make_segments_v2(_read_text_loose(Path(path)))
    segs = _segs(w.id)
    assert [g.text for g in segs] == plain
    assert "作者：榴弹怕水" in segs[0].text, "旧缺陷在默认路径依旧存在（开关才修）"
    assert all(g.chapter is None for g in segs)
    base_keys = set(si.analyze("x", ordinal=0))
    for g in segs:
        assert set(json.loads(g.integrity)) == base_keys, "默认路径 integrity 不得混入新键"
    a = json.loads(w.anchors)
    assert "front_matter" not in a and "last_truncated" not in a
    assert w.author is None


# ── caveat 1：卷首元数据剥离 + 记账 ─────────────────────────

def test_front_matter_not_body_but_accounted(tmp_path):
    path = _book(tmp_path)
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    w = _work(path)
    segs = _segs(w.id)
    assert segs and not any(
        ("内容简介" in g.text) or g.text.startswith("作者：") or g.text == "覆汉"
        for g in segs), "元数据段不得作为正文入库"
    assert segs[0].text.startswith("甲10"), "ordinal=0 必须是真正文"
    fm = json.loads(w.anchors)["front_matter"]
    assert fm["lines"] == FRONT, "剥离的行必须逐字记账（不静默丢弃）"
    assert fm["author"] == "榴弹怕水"
    assert fm["intro"] == ["努力闻达于诸侯，以求苟全性命于乱世！"]
    assert w.author == "榴弹怕水", "元数据作者回填 Work.author"


def test_front_matter_absent_is_noop():
    body = _para(1) + "\n" + _para(2)
    kept, fm = civ2.strip_front_matter(body, title="无头本")
    assert kept == body and fm == {}, "不含元数据头 ⇒ 逐字节原样 + 空记账"


def test_intro_block_bounded_and_stops_at_blank(tmp_path):
    """内容简介头块遇空行即止：其后的正文一段都不能被吃掉。"""
    text = "内容简介：\n一句很短的引子。\n" + "\n\n".join(_para(i) for i in (3, 4, 5))
    body, fm = civ2.strip_front_matter(text, title="any")
    assert fm["intro"] == ["一句很短的引子。"]
    for i in (3, 4, 5):
        assert _para(i) in body, "正文段不得被头块吞掉"


# ── caveat 2：末段截断显式标记（不加列） ────────────────────

def test_truncated_tail_marked_not_silent(tmp_path):
    path = _book(tmp_path, truncated_tail=True)
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    w = _work(path)
    segs = _segs(w.id)
    last = segs[-1]
    assert last.text.rstrip().endswith("孙文台")
    flags = json.loads(last.integrity)
    assert flags["truncated"] is True, "末段截断必须显式标记"
    assert all("truncated" not in json.loads(g.integrity) for g in segs[:-1]), \
        "标记只属于全篇最后一段"
    assert json.loads(w.anchors)["last_truncated"] is True
    # 向后兼容：多余 JSON 键不破坏既有读取口
    assert isinstance(si.is_naturalness_eligible(flags), bool)


def test_clean_tail_not_marked(tmp_path):
    path = _book(tmp_path, truncated_tail=False)
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    w = _work(path)
    segs = _segs(w.id)
    assert "truncated" not in json.loads(segs[-1].integrity)
    assert json.loads(w.anchors)["last_truncated"] is False


def test_no_new_columns_on_segment():
    """截断标记走 integrity JSON——不得给段表加列（真库建表史 NOT NULL 无默认炸插行）。"""
    cols = {c.name for c in Segment.__table__.columns}
    assert "truncated" not in cols and "chapter" in cols


# ── caveat 3：chapter 回填（复用切分器标题正则） ────────────

def test_chapter_backfill_from_body_headings(tmp_path):
    path = _book(tmp_path)
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    segs = _segs(_work(path).id)
    got = [g.chapter for g in segs]
    assert got == ["第1章 初临汉末"] * 2 + ["第2章 风起"] * 3, \
        "各段按所属章节标题回填"
    assert [g.text for g in segs] == segmenter_v2.make_segments_v2(
        civ2.strip_front_matter(
            Path(path).read_text(encoding="utf-8"), title=TITLE)[0]), \
        "回填不得改变段内容与顺序"


def test_chapter_left_empty_when_unparsable(tmp_path):
    """无章节标题行的书：chapter 一律留空（None），不瞎猜。"""
    path = _book(tmp_path, chapters=False)
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    assert all(g.chapter is None for g in _segs(_work(path).id))


def test_chapter_regex_variants_covered():
    """标题口径 = segmenter_v2._CHAPTER_RE：回/节变体同样可回填。"""
    text = ("第5回 虎牢关\n" + _para(60) + "\n第3节 温酒\n" + _para(61) + "\n" + _para(62))
    prep = civ2.prepare_import(text, title="变体本")
    assert prep.chapters == ["第5回 虎牢关", "第3节 温酒", "第3节 温酒"]


# ── 组合：开关路径整体自洽 + 幂等 ───────────────────────────

def test_caveats_import_idempotent_rerun(tmp_path, capsys):
    path = _book(tmp_path)
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    segs = _segs(_work(path).id)
    assert len(segs) == 5, "头部块不计段后：5 个正文段"
    assert IC.import_state(_work(path)) == IC.COMPLETE
    capsys.readouterr()
    assert IC.main([path, TITLE, "训练语料", "--caveats"]) == 0
    assert "已导入过" in capsys.readouterr().out
    assert len(_segs(_work(path).id)) == 5


def test_usage_still_exit_2_with_flag(capsys):
    assert IC.main(["--caveats"]) == 2
    err = capsys.readouterr().err
    assert "用法" in err and "import_corpus_v2.py" in err
