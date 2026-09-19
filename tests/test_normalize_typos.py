"""字表归一化（授权项 2）回归测试。

钉住：频次/上下文规则的 lookbehind 正确性；只修 text_clean 不动 text；
work 白名单（规则不越书应用）；integrity 重判翻回；apply 幂等；
AB 诊断集两侧 human 只差错字、variant 同源、位置同种子。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import normalize_typos as NT  # noqa: E402
from app import db  # noqa: E402
from app.models import BenchmarkItem, Candidate, Experiment, Frame, Segment, Work  # noqa: E402


def test_hit_respects_lookbehind():
    assert NT._hit("千雪来了", "千雪", "仞") == 1
    assert NT._hit("千仞雪来了", "千雪", "仞") == 0, "千仞雪的子串不算错字"
    assert NT._hit("吴天宗", "吴天", None) == 1


def test_repaired_respects_work_allowlist():
    # 将夜/斗罗的 吴天→昊天 都修
    assert NT._repaired("吴天宗", "将夜（猫腻）")[0] == "昊天宗"
    assert NT._repaired("吴天锤", "斗罗大陆（唐家三少）")[0] == "昊天锤"
    # 琼明没有适用规则，原样返回
    assert NT._repaired("吴天宗", "琼明神女录（精校）") == ("吴天宗", 0)
    # 千雪 lookbehind 在 apply 路径同样生效
    t, k = NT._repaired("千仞雪和千雪", "斗罗大陆（唐家三少）")
    assert t == "千仞雪和千仞雪" and k == 1


def test_apply_fixes_text_clean_only_and_idempotent():
    db.init_db()
    with db.session() as s:
        w = Work(title="斗罗大陆（唐家三少）-typo-t1", source="test:typo")
        s.add(w); s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="吴天宗的千雪来了。",
                      integrity='{"src_ok": false, "defects": ["疑似掉字：`千雪`"], "checked_pv": "source_integrity_v1"}',
                      n_sentences=1, n_chars=10)
        s.add(seg); s.flush()
        sid = seg.id
        s.commit()
    out = NT.apply_repairs()
    with db.session() as s:
        seg = s.get(Segment, sid)
        assert seg.text == "吴天宗的千雪来了。", "原文 text 不许动"
        assert seg.text_clean == "昊天宗的千仞雪来了。"
        # 军师 P0 退回：字表工具只许改字，**不许碰 integrity**——
        # 修人名不能顺手放行 LLM 查出的缺句/截断；重判是 source_check 的职责
        assert seg.integrity == '{"src_ok": false, "defects": ["疑似掉字：`千雪`"], "checked_pv": "source_integrity_v1"}',             "字表工具改动了 integrity（越权）"
    assert out["segments_touched"] >= 1
    # 幂等：再跑一遍该段不再变化
    out2 = NT.apply_repairs()
    with db.session() as s:
        seg = s.get(Segment, sid)
        assert seg.text_clean == "昊天宗的千仞雪来了。"


def test_build_ab_sets_differ_only_in_human_side(tmp_path):
    db.init_db()
    with db.session() as s:
        exp = "EXP-TYPO-AB-T"
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w = Work(title="斗罗大陆（唐家三少）-typo-t2", source="test:typo")
        s.add(w); s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="吴天宗的千雪来了。",
                      text_clean="昊天宗的千仞雪来了。", role=None,
                      integrity='{"src_ok": true}', n_sentences=1, n_chars=10)
        s.add(seg); s.flush()
        fr = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr); s.flush()
        s.add(Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg.id,
                        anon_label="X1", model="m", prompt_version="reconstruct_v1",
                        text="候选文本，足够长以通过一切过滤门槛检查。"))
        s.commit()
        seg_id = seg.id
    # build_ab 读的是 EXP-TYPO-AB；测试用自己的实验 → 直接调用内部逻辑太绕，
    # 这里用 monkeypatch 换实验 id 验证配对语义
    import scripts.normalize_typos as nt2
    orig_exp = nt2.EXP if hasattr(nt2, "EXP") else "EXP-TYPO-AB"
    src_exp = "EXP-TYPO-AB"
    # 直接打补丁：临时把实验 id 常量换成测试实验
    import types
    code_src = Path(nt2.__file__).read_text(encoding="utf-8").replace(
        'EXP = "EXP-TYPO-AB"', f'EXP = "{exp}"')
    mod = types.ModuleType("nt_test")
    mod.__dict__["__file__"] = nt2.__file__
    exec(compile(code_src, "nt_test", "exec"), mod.__dict__)
    out = mod.build_ab(10, seed=3, dry_run=False)
    assert len(out["sets"]) == 2
    set_ids = {x["name"]: x["set_id"] for x in out["sets"]}
    with db.session() as s:
        before = {i.id: i for i in s.query(BenchmarkItem).filter_by(set_id=set_ids["typo-before-v1"]).all()}
        after = {i.id: i for i in s.query(BenchmarkItem).filter_by(set_id=set_ids["typo-after-v1"]).all()}
    assert len(before) == len(after) == 1
    b, a = list(before.values())[0], list(after.values())[0]
    assert {b.text_a, b.text_b} == {seg_id and "吴天宗的千雪来了。", "候选文本，足够长以通过一切过滤门槛检查。"}
    assert "昊天宗的千仞雪来了。" in {a.text_a, a.text_b}, "after 侧 human 应是修复后文本"
    assert (b.answer == "A") == (a.answer == "A"), "同种子下两侧位置必须一致"
