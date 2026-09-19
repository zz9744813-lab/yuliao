"""T-CORPUS-V2 回归：TYPO_MAP / 入库闸门 / corpus v2 版本化产出。

钉住：
1. typo_map：lookbehind 正确（千仞雪 子串不误伤）、特异性优先、apply 计数；
2. 入库闸门：add_work 对命中文本**只记 note 不改文本**（修复走版本化）；
3. corpus v2：新 Work 1:1 镜像、text=修复后文本、v1 原样、integrity 带
   corpus_v2_source 映射锚、幂等（重跑跳过）、map 文件逐段落行；
4. v2 段 integrity 逐字携带 v1 校勘结论（重判是 source_check 的职责）。
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
