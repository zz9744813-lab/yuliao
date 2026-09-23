"""原始文本清洗回归（集霸 2026-09-18：「先把原始文本的那些拼音广告啥的搞一下」）。

锁定的不变量：

1. **规则清洗只删伪影，不动正文**：站点水印、空括号、行首尾碎片删掉；
   正常的中文标点（引号/括号/破折号/省略号）一个字都不许动。
2. **拼音不靠删**：`白sè` 删成 `白` 会丢字，必须留给 LLM 还原（`needs_llm` 认得出）。
3. **端出优先用清洗版**：A/B 与上文都走同一份 `text_clean`，
   否则会出现"给用户看清洗版、拿原文校验批注偏移"的错位。
4. **上文宁可少给，不许给脏的**：清洗后仍是坏文本的段直接不上屏。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from clean_text import clean_rules, looks_broken, needs_llm  # noqa: E402
from app import db  # noqa: E402
from app.api import _ctx_display, _display_text  # noqa: E402
from app.models import Experiment, Segment, Work  # noqa: E402


@pytest.mark.parametrize("raw,want", [
    ("他推门进来。()", "他推门进来。"),
    ("(手打中文网7*24小时不间断更新纯txt手打小说m)他推门进来。", "他推门进来。"),
    ("（未完待续）小.说。t/x/t天.堂\n来到悬崖前。", "来到悬崖前。"),
    ("[]小.说.t.xt.天.堂 / 虽然帝国民风朴素", "虽然帝国民风朴素"),
    ("阅读请锁定{　}那位身份尊贵的", "那位身份尊贵的"),
])
def test_rules_remove_watermarks(raw, want):
    assert clean_rules(raw) == want


@pytest.mark.parametrize("text", [
    "他说：“你不必再来了。”",
    "（这一段是插叙）——就这样结束了……",
    "（一）他推门进来。",
    "黑夜越来寒冷，光明越发炽烈，把整个天空分成了两半。",
])
def test_rules_keep_legit_text(text):
    """正常中文标点一字不动——清洗器过度清洗比不清洗更危险（会改写作者的文本）。"""
    assert clean_rules(text) == text


def test_pinyin_is_left_for_llm_not_deleted():
    """`白sè` 不能删成 `白`：删了就是悄悄改写了原文。"""
    assert needs_llm("一股股白sè雾气在虚空中浮现而出。")
    assert needs_llm("他早就发现陈皮皮今天的jīng神状态有些问题。")
    assert not needs_llm("他说：“你不必再来了。”")
    # 规则清洗后仍留拼音 → 必须仍被 needs_llm 认出来
    assert needs_llm(clean_rules("黑色sè的光幕。(手打中文网7*24小时不间断更新纯txt手打小说m)"))


def test_looks_broken_flags_unrepaired_text():
    assert looks_broken("一股股白sè雾气浮现而出，二者均凝望着蓝sè光幕中的情形。")   # 粘连 → 坏
    assert not looks_broken("一股股白色雾气浮现而出，二者均凝望着蓝色光幕中的情形。")
    assert looks_broken("")                       # 空/过短同样是坏（水印删干净后只剩空壳）


def _seg(text, clean=None):
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, "EXP-CLEAN"):
            s.add(Experiment(id="EXP-CLEAN", name="t", status="created", config={}, stats={}))
        w = Work(title="t-clean", source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=text, text_clean=clean,
                      n_sentences=1, n_chars=len(text))
        s.add(seg)
        s.commit()
        return seg


def test_display_text_prefers_clean():
    seg = _seg("一股股白sè雾气浮现。(手打中文网7*24小时不间断更新纯txt手打小说m)",
               clean="一股股白色雾气浮现。")
    assert _display_text(seg) == "一股股白色雾气浮现。"


def test_display_text_falls_back_to_raw_when_not_cleaned():
    seg = _seg("他推门进来，屋里没人。", clean=None)
    assert _display_text(seg) == "他推门进来，屋里没人。"


def test_ctx_display_drops_broken_context():
    """上文带拼音时不显示，而不是显示成乱码。"""
    ok = "过了数日的某个午后，他翻看到了中间部分。"
    dirty = "白sè雾气神sè各异滚滚而来"
    assert _ctx_display([dirty]) == []
    assert _ctx_display([ok, dirty]) == [ok]
    assert _ctx_display([ok]) == [ok]


def test_segment_has_text_clean_column():
    """清洗结果必须落在独立列上：原文保留可审计，下游读清洗版。"""
    assert "text_clean" in Segment.__table__.columns


def test_ai_flavor_does_not_claim_what_it_cannot_do():
    """AI 味检测器**尚未验证有效**，接口不许把分数说成"AI 味"结论。

    2026-09-18 实测：规则层对他的 63 条批注段级命中 7.5%~20.8%（低于抛硬币），
    模型层在真实模型输出上给的分**比人类原文更低**。
    这个测试只钉住"模块能跑、返回结构稳定"，**不钉住有效性**——
    有效性没有证据，谁要说有效，先拿 §⑳ 的表来过。
    """
    from app.ai_flavor import analyze, analyze_v2, obvious
    r = analyze("他推门进来，屋里没人。")
    assert 0.0 <= r.score <= 1.0 and isinstance(r.hits, list)
    r2 = analyze_v2("只是肩背微微偏了半寸，那半寸不碍礼数，却让她的叩首更深地落在父亲脚前。")
    assert r2.score > 0, "v2 至少要能抓到'细节+意义解释'的结构"
    assert isinstance(obvious("随便一句话。"), bool)


def test_span_flavor_detector_contract(tmp_path):
    """片段级检测器的**接口**契约（有效性另有外部验证，不在这里断言）。

    有效性证据见 docs/phase1.5-plan.md §⑳-1：按题分组 AUC 0.710、
    构造劣化对外部验证 75.9%。有一折 AUC 0.446（低于随机）→ 不稳定，
    所以这里只钉"给得出片段、分数有界、不因空文本崩"。
    """
    # fastembed 是可选依赖（bge-small-zh 向量，未在 requirements 声明，flavor_train.py 懒加载）。
    # 这不是跳过缺陷：依赖未装时该契约无从判定，只能显式 skip，不能算 fail。
    pytest.importorskip("fastembed", reason="可选依赖：bge-small-zh 向量，未在 requirements 声明")
    import flavor_span as FS
    r = FS.score_spans("她停了一拍，问：那要是喜欢陆姐姐，还能喜欢别人吗？")
    assert set(r) >= {"max", "mean_top", "spans"}
    assert 0.0 <= r["max"] <= 1.0
    for sp in r["spans"]:
        assert 0.0 <= sp["score"] <= 1.0 and sp["end"] > sp["start"]
    empty = FS.score_spans("")
    assert empty["max"] == 0.0 and empty["spans"] == []
