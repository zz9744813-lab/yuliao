"""T-CORPUS-V2 回归：TYPO_MAP / 入库闸门 / corpus v2 版本化产出 / 会审①收口。

钉住：
1. typo_map：lookbehind 正确（千仞雪 子串不误伤）、特异性优先、apply 计数；
2. 入库闸门：add_work 对命中文本**只记 note 不改文本**（修复走版本化）；
3. corpus v2：新 Work 1:1 镜像、text=修复后文本、v1 原样、integrity 带
   corpus_v2_source 映射锚、幂等（重跑跳过）、map 文件逐段落行；
4. v2 段 integrity 逐字携带 v1 校勘结论（重判是 source_check 的职责）；
5. 会审①：v2 段任何分支都不继承 role；幂等键是 v1 work.id 派生的稳定键
   （同名多 Work 各自镜像，不静默丢弃）；历史行可 backfill 收口。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import corpus, db  # noqa: E402
from app.models import Segment, Work  # noqa: E402
from app.typo_map import apply, hits, total  # noqa: E402


# ── 1. TYPO_MAP 本体 ────────────────────────────────────────

def test_typo_map_lookbehind_and_specificity():
    assert hits("千仞雪来了") == {}, "千仞雪 的子串不算 千雪"
    assert hits("千雪来了") == {"千雪": 1}
    assert hits("吴天宗") == {"吴天": 1}
    assert hits("了天斗罗") == {"了天斗罗": 1}, "特异性规则先于 吴天 生效"
    assert total("吴天锤与千雪") == 2


def test_typo_map_apply():
    new, n = apply("吴天宗的千雪，还有千仞雪。")
    assert new == "昊天宗的千仞雪，还有千仞雪。" and n == 2
    assert apply(None) == ("", 0)


# ── 2. 入库闸门：只记录不改动 ────────────────────────────────

def test_add_work_intake_gate_records_but_preserves():
    db.init_db()
    raw = "吴天宗的千雪走了。他看着昊天锤。" * 2
    with db.session() as s:
        w = corpus.add_work(s, title="t-v2-gate", text=raw, source="test:v2",
                            note="base")
        s.commit()
        sid = w.id
    with db.session() as s:
        w = s.get(Work, sid)
        segs = s.query(Segment).filter(Segment.work_id == w.id).all()
        assert segs and all("吴天宗的千雪" in (sg.text or "") for sg in segs),             "入库闸门不许改段落原文"
        assert "[typo_scan]" in (w.note or "") and "吴天×2" in (w.note or "")
    # 干净文本不产生 note
    with db.session() as s:
        w2 = corpus.add_work(s, title="t-v2-gate-clean",
                             text="干干净净的一段正文，没有任何已知错字模式。",
                             source="test:v2")
        s.commit()
        sid2 = w2.id
    with db.session() as s:
        w2 = s.get(Work, sid2)
        assert "[typo_scan]" not in (w2.note or "")


# ── 3. corpus v2 版本化产出 ─────────────────────────────────

def _seed_v1_work(title: str) -> str:
    db.init_db()
    with db.session() as s:
        w = Work(title=title, source="test:v2-seed")
        s.add(w)
        s.flush()
        segs = [
            Segment(work_id=w.id, ordinal=0, text="吴天宗的千雪来了。",
                    text_clean="吴天宗的千雪来了。",
                    integrity='{"src_ok": true}', n_sentences=1, n_chars=11),
            Segment(work_id=w.id, ordinal=1, text="他看着昊天锤不说话。",
                    text_clean="他看着昊天锤不说话。",
                    integrity='{"src_ok": false, "defects": ["截断"], "checked_pv": "source_integrity_v1"}',
                    n_sentences=1, n_chars=10),
        ]
        s.add_all(segs)
        s.commit()
        return w.id


def test_corpus_v2_build_mirrors_and_repairs(tmp_path):
    v1_id = _seed_v1_work("斗罗大陆（唐家三少）-v2t")
    import scripts.corpus_fix_v2 as CF
    out = CF.build(only=("斗罗大陆",), map_path=tmp_path / "map.jsonl")
    assert out["created"] and out["created"][0]["segments"] == 2
    with db.session() as s:
        v2 = s.query(Work).filter(Work.title.like("斗罗大陆%corpus v2%")).one()
        rows = (s.query(Segment).filter(Segment.work_id == v2.id)
                .order_by(Segment.ordinal).all())
        v1_segs = (s.query(Segment).filter(Segment.work_id == v1_id)
                   .order_by(Segment.ordinal).all())
    # 修复：吴天→昊天、千雪→千仞雪
    assert rows[0].text == "昊天宗的千仞雪来了。"
    assert rows[1].text == "他看着昊天锤不说话。"
    # v1 原样
    assert v1_segs[0].text == "吴天宗的千雪来了。"
    # integrity 携带映射锚与 v1 结论
    i0 = json.loads(rows[0].integrity)
    assert i0["corpus_v2_source"] == v1_segs[0].id and i0["src_ok"] is True
    i1 = json.loads(rows[1].integrity)
    assert i1["src_ok"] is False, "v1 判坏结论随行携带（重判是 source_check 的职责）"
    # 映射文件
    map_rows = [json.loads(l) for l in
                (tmp_path / "map.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(map_rows) == 2 and {m["v1_segment"] for m in map_rows} == \
        {v1_segs[0].id, v1_segs[1].id}


def test_corpus_v2_idempotent(tmp_path):
    _seed_v1_work("斗罗大陆（唐家三少）-v2t2")
    import scripts.corpus_fix_v2 as CF
    out1 = CF.build(only=("斗罗大陆",), map_path=tmp_path / "m.jsonl")
    n1 = len(out1["created"])
    out2 = CF.build(only=("斗罗大陆",), map_path=tmp_path / "m.jsonl")
    assert out2["created"] == [] and out2["skipped"], "重跑必须幂等跳过"
    assert n1 == 1


# ── 4. 会审补课：apply 黄金用例（幂等 / 顺序 / 空值）──────────

def test_apply_is_idempotent_and_golden():
    raw = "吴天宗的千雪见到了了天斗罗。"
    once, n1 = apply(raw)
    twice, n2 = apply(once)
    # 黄金值：吴天→昊天(1) + 千雪→千仞雪(1) + 了天斗罗→昊天斗罗(1)
    assert once == "昊天宗的千仞雪见到了昊天斗罗。" and n1 == 3
    assert twice == once and n2 == 0, "修复必须幂等（修复产物不再命中字表）"


def test_hits_none_and_empty():
    assert hits(None) == {} and hits("") == {}
    assert apply("") == ("", 0)


# ── 4. 会审①：v2 段不继承 role ─────────────────────────────

def test_corpus_v2_segments_do_not_inherit_role(tmp_path):
    """v1 里 role='benchmark' 的段若被 v2 原样继承，同一内容会双份入池/入 gold。
    v2 段必须 role=None（docstring 承诺的行为），且 v1 的标注原样保留。"""
    v1_id = _seed_v1_work("斗罗大陆（唐家三少）-v2role")
    with db.session() as s:
        # 额外造一个 benchmark 角色的 v1 段
        s.add(Segment(work_id=v1_id, ordinal=9, text="吴天宗的千雪又来了。",
                      text_clean="吴天宗的千雪又来了。", role="benchmark",
                      integrity='{"src_ok": true}', n_sentences=1, n_chars=11))
        s.commit()
    import scripts.corpus_fix_v2 as CF
    out = CF.build(only=("v2role",), map_path=tmp_path / "m-role.jsonl")
    mine = [c for c in out["created"] if c["v1_work"] == v1_id]
    assert len(mine) == 1
    with db.session() as s:
        # 一律按 id 定位（行数只做旁证）：本测试专属 v2 work + 本测试专属 v1 work
        v2_id = mine[0]["v2_work"]
        rows = s.query(Segment).filter(Segment.work_id == v2_id).all()
        v1_rows = s.query(Segment).filter(Segment.work_id == v1_id).all()
    assert len(rows) == 3
    assert all(r.role is None for r in rows), \
        "v2 段继承了 v1 的 role——同文双份入池/入 gold，基准被污染"
    assert sum(1 for r in v1_rows if r.role == "benchmark") == 1, \
        "v1 侧标注必须原样保留（基准只在 v1 侧维护）"


# ── 5. 会审①收口：幂等键（稳定血缘键）+ 存量回填 ──────────────

def test_corpus_v2_stable_key_mirrors_each_same_title_work(tmp_path):
    """幂等键是 v1 work.id 派生的稳定键：同名多 Work 各自镜像，不静默丢弃。

    旧实现按 Work.title 查已存在，第二本同名 Work 会被第一本的 v2 顶掉——
    既不报错也不产出（会审①的第二条严重项）。
    """
    a = _seed_v1_work("斗罗大陆（唐家三少）-u0cdup")
    b = _seed_v1_work("斗罗大陆（唐家三少）-u0cdup")        # 与 a 完全同名
    import scripts.corpus_fix_v2 as CF
    out = CF.build(only=("u0cdup",), map_path=tmp_path / "dup.jsonl")
    assert out["same_title_v1_works"] == {"斗罗大陆（唐家三少）-u0cdup": 2}
    assert len(out["created"]) == 2, "同名第二本 Work 被标题键静默跳过了"
    assert {c["v1_work"] for c in out["created"]} == {a, b}
    with db.session() as s:
        rows = s.query(Work).filter(Work.v2_of.in_([a, b])).all()
        assert len(rows) == 2, "每本 v1 各自有一本带稳定键的 v2"
        assert {r.v2_of for r in rows} == {a, b}
    out2 = CF.build(only=("u0cdup",), map_path=tmp_path / "dup2.jsonl")
    assert out2["created"] == [] and len(out2["skipped"]) == 2, "重跑必须按稳定键幂等跳过"
    assert all("v2_work" in sk for sk in out2["skipped"]), "跳过要报出命中了哪本 v2（不许静默）"


def test_corpus_v2_build_adopts_legacy_row_instead_of_double_creating(tmp_path):
    """历史行（回填前 v2_of 为空）：必须按「标题 + 锚点血缘」采纳，绝不再镜像一份。"""
    v1_id = _seed_v1_work("斗罗大陆（唐家三少）-u0cadopt")
    import scripts.corpus_fix_v2 as CF
    CF.build(only=("u0cadopt",), map_path=tmp_path / "a1.jsonl")
    with db.session() as s:
        legacy = s.query(Work).filter(Work.v2_of == v1_id).one()
        legacy_id = legacy.id
        legacy.v2_of = None                 # 回到"回填前"的形态
        s.commit()
    out = CF.build(only=("u0cadopt",), map_path=tmp_path / "a2.jsonl")
    assert out["created"] == [] and out["skipped"], "稳定键缺失时按血缘采纳，不许重复镜像"
    with db.session() as s:
        assert s.get(Work, legacy_id).v2_of == v1_id, "采纳时顺手补齐稳定键"
        n = s.query(Work).filter(
            Work.title == "斗罗大陆（唐家三少）-u0cadopt（corpus v2）").count()
        assert n == 1


def test_corpus_v2_backfill_clears_inherited_role_and_restores_key(tmp_path):
    """只改生成器不回填 ≠ 修完：历史 v2 行的继承 role 要显式清零、稳定键要补上。"""
    v1_id = _seed_v1_work("斗罗大陆（唐家三少）-u0cbf")
    import scripts.corpus_fix_v2 as CF
    out = CF.build(only=("u0cbf",), map_path=tmp_path / "b0.jsonl")
    v2_id = [c for c in out["created"] if c["v1_work"] == v1_id][0]["v2_work"]
    with db.session() as s:
        # 复现污染现场：旧代码 role=seg.role 把 v1 的 benchmark 抄给了 v2
        for seg in s.query(Segment).filter(Segment.work_id == v2_id).all():
            seg.role = "benchmark"
        w2 = s.get(Work, v2_id)
        w2.v2_of = None                     # 回到回填前
        for seg in s.query(Segment).filter(Segment.work_id == v1_id).all():
            seg.role = "benchmark"          # v1 侧基准段（不许被动）
        s.commit()

    dry = CF.backfill(apply=False)
    entry = [e for e in dry["v2_works"] if e["v2_work"] == v2_id][0]
    assert entry["v2_of_before"] is None and entry["v2_of_after"] == v1_id, \
        "稳定键靠段上的 corpus_v2_source 锚反查，不靠标题"
    assert entry["role_before"] == {"benchmark": 2}
    assert dry["role_cleared_total"].get("benchmark", 0) >= 2
    with db.session() as s:
        assert s.get(Work, v2_id).v2_of is None, "dry-run 一律不落库"
        assert all(r.role == "benchmark" for r in
                   s.query(Segment).filter(Segment.work_id == v2_id).all())

    wet = CF.backfill(apply=True)
    wentry = [e for e in wet["v2_works"] if e["v2_work"] == v2_id][0]
    assert wentry["role_after"] == {} and wentry["lineage"] == "resolved"
    with db.session() as s:
        assert s.get(Work, v2_id).v2_of == v1_id, "apply 后稳定键必须落库"
        assert all(r.role is None for r in
                   s.query(Segment).filter(Segment.work_id == v2_id).all()), \
            "回填后 v2 段仍带 role——基准侧仍是同文双份"
        v1_roles = [r.role for r in s.query(Segment).filter(Segment.work_id == v1_id)
                    .order_by(Segment.ordinal).all()]
        assert v1_roles == ["benchmark", "benchmark"], "v1 侧基准段一律不动（基准只在 v1 维护）"
        assert "[U0c backfill]" in (s.get(Work, v2_id).note or ""), "回填必须留痕可审计"
    # 幂等：再跑一次没有新的可清
    again = CF.backfill(apply=True)
    aentry = [e for e in again["v2_works"] if e["v2_work"] == v2_id][0]
    assert aentry["role_before"] == {} and aentry["v2_of_after"] == v1_id


def test_corpus_v2_all_build_branches_leave_role_none(tmp_path):
    """构造点唯一 + 覆盖所有分支：命中/未命中错字、integrity 缺失/非 JSON、带 role ——
    v2 段一律 role=None，且 v1 标注原样。"""
    db.init_db()
    with db.session() as s:
        w = Work(title="斗罗大陆（唐家三少）-u0cbranch", source="test:v2-seed")
        s.add(w)
        s.flush()
        s.add_all([
            Segment(work_id=w.id, ordinal=0, text="吴天宗的千雪来了。",
                    text_clean="吴天宗的千雪来了。", integrity='{"src_ok": true}',
                    role="benchmark", n_sentences=1, n_chars=11),
            Segment(work_id=w.id, ordinal=1, text="干净的一段正文。",
                    text_clean="干净的一段正文。", integrity=None,
                    role="train", n_sentences=1, n_chars=8),
            Segment(work_id=w.id, ordinal=2, text="另一段正文。",
                    text_clean=None, integrity="{not json",
                    role=None, n_sentences=1, n_chars=6),
        ])
        s.commit()
        v1_id = w.id
    import scripts.corpus_fix_v2 as CF
    out = CF.build(only=("u0cbranch",), map_path=tmp_path / "br.jsonl")
    mine = [c for c in out["created"] if c["v1_work"] == v1_id]
    assert len(mine) == 1 and mine[0]["segments"] == 3
    assert mine[0]["integrity_unparsed"] == 1, "非 JSON 分支要计数告警，不许静默"
    with db.session() as s:
        rows = (s.query(Segment).filter(Segment.work_id == mine[0]["v2_work"])
                .order_by(Segment.ordinal).all())
        assert [r.role for r in rows] == [None, None, None], \
            "任一分支继承 role 都会让同文段双份入池/入基准"
        assert rows[0].integrity is None or "corpus_v2_source" in rows[0].integrity
        assert json.loads(rows[0].integrity)["corpus_v2_source"]
        assert rows[1].integrity is None, "v1 integrity 缺失 → v2 也缺失"
        assert rows[2].integrity == "{not json", "非 JSON 原样随行（人工补映射）"
        v1_rows = (s.query(Segment).filter(Segment.work_id == v1_id)
                   .order_by(Segment.ordinal).all())
        assert [r.role for r in v1_rows] == ["benchmark", "train", None]
