"""LLM 覆写正文保留门（审计《language-genome-code-audit-20260923》非阻断项）。

锁定的不变量：

1. LLM 垃圾输出（过短，如"已修复"）→ 不覆盖 text_clean，计 llm_rejected；
2. 截断输出（远短于原文）→ 不覆盖；
3. 合法修复（长度相当、拼音还原）→ 正常覆盖，计 llm_ok，llm_rejected 不动；
4. 空/None 输出 → 走原 llm_failed 路径（回归：加门不改这条）；
5. 门可关：CLEAN_TEXT_GUARD=0 时垃圾输出照写（行为与加门前一致）；
6. 短原文豁免比例卡：只卡绝对下限，不把正常短段全拒；
7. 上限卡（GUARD_MAX_RATIO）：`out = src + 垃圾`（反例 b）→ 'oversize'，不落库；
8. 相似卡（GUARD_MIN_SIMILARITY + SequenceMatcher）：同长度跑题（反例 a）→ 'divergent'；
9. 拉丁占比卡（GUARD_MAX_LATIN_RATIO）：整段无声调拼音化（反例 c）→ 'latinized'
   —— 该形态 looks_broken 兜不住，本卡是唯一信号。
7~9 与 truncated 同享 GUARD_SHORT_SRC 短原文豁免（正常短段改写不得被新卡误杀）。

全程不碰网络：llm_repair_batch 被 monkeypatch 成假函数。
"""
import sys
from difflib import SequenceMatcher
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clean_text as ct  # noqa: E402
from app import db  # noqa: E402
from app.models import Segment, Work  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_stat():
    """_stat 是模块级累加器，每个用例从零计。"""
    for k in ct._stat:
        ct._stat[k] = 0
    yield


@pytest.fixture(autouse=True)
def _clean_guard_segs():
    """本文件与 test_clean_text.py 共享同一个临时库：跑前清掉上例残留的段，
    保证 run_llm 的 todo 里只有本例种下的段（统计断言才可绝对计数）。"""
    db.init_db()
    with db.session() as s:
        s.query(Segment).filter(Segment.work_id.in_(
            s.query(Work.id).filter(Work.source == "test:seed-guard"))).delete(
            synchronize_session=False)
        s.query(Work).filter(Work.source == "test:seed-guard").delete(
            synchronize_session=False)
        s.commit()
    yield


def _seed(text: str, clean: str | None = None) -> int:
    db.init_db()
    with db.session() as s:
        w = Work(title="t-guard", source="test:seed-guard")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=text, text_clean=clean,
                      n_sentences=1, n_chars=len(text))
        s.add(seg)
        s.commit()
        return seg.id


def _text_clean(seg_id: int) -> str | None:
    with db.session() as s:
        return s.get(Segment, seg_id).text_clean


def _install_llm(monkeypatch, mapping: dict[str, str | None]) -> None:
    """把 llm_repair_batch 换成查表假函数；未列入表的输入原样返回（幂等）。"""
    monkeypatch.setattr(ct, "llm_repair_batch",
                        lambda texts: [mapping.get(t, t) for t in texts])


def _run_llm(**kw):
    before = dict(ct._stat)
    r = ct.run_llm(conc=1, **kw)
    delta = {k: ct._stat[k] - before[k] for k in ct._stat}
    return r, delta


# 60 字左右的脏段（含拼音粘连 → needs_llm 认得出）
SRC = ("他顺着白sè的石阶一路上行，两侧的jīng神屏障随他的脚步次第亮起，"
       "山风卷着雾气扑面而来，把那点残存的暖意也一并吹散了。")


def test_short_junk_output_rejected(monkeypatch):
    """短垃圾（"已修复"）不覆盖，计 llm_rejected，text_clean 保持原值。"""
    sid = _seed(SRC)
    assert ct.needs_llm(SRC)
    _install_llm(monkeypatch, {SRC: "已修复"})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_rejected"] == 1
    assert d["llm_ok"] == 0
    assert d["llm_failed"] == 0


def test_truncated_output_rejected(monkeypatch):
    """原文 200+ 字只回 50 字 → 视为截断，不覆盖。"""
    long_src = SRC + "他推门进来，屋里没人，只有炉火还在低低地烧着。" * 10
    assert len(long_src.strip()) >= 200
    assert ct.needs_llm(long_src)
    sid = _seed(long_src)
    truncated = long_src[:50]
    _install_llm(monkeypatch, {long_src: truncated})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_rejected"] == 1
    assert d["llm_ok"] == 0


def test_valid_repair_accepted(monkeypatch):
    """长度相当、拼音还原的合法修复 → 正常覆盖，计 llm_ok，llm_rejected 不动。"""
    sid = _seed(SRC)
    fixed = SRC.replace("白sè", "白色").replace("jīng神", "精神")
    assert fixed != SRC
    assert ct.guard_verdict(SRC, fixed) == "accept"   # 长度相当、无截断
    _install_llm(monkeypatch, {SRC: fixed})
    _, d = _run_llm()
    assert _text_clean(sid) == fixed
    assert d["llm_ok"] == 1
    assert d["llm_rejected"] == 0
    assert d["still_broken"] == 0


def test_empty_output_still_llm_failed(monkeypatch):
    """out 为空/None → 原 llm_failed 路径（回归：门不加戏）。"""
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: ""})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_failed"] == 1
    assert d["llm_rejected"] == 0
    assert d["llm_ok"] == 0


def test_guard_off_restores_old_behavior(monkeypatch):
    """关掉门（env）→ 与加门前一致：垃圾输出照写 text_clean。"""
    monkeypatch.setenv(ct.GUARD_ENV, "0")
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: "已修复"})
    _, d = _run_llm()
    assert _text_clean(sid) == "已修复"
    assert d["llm_ok"] == 1
    assert d["llm_rejected"] == 0


def test_guard_off_by_param(monkeypatch):
    """关掉门（参数）→ 同上；且参数优先于 env。"""
    monkeypatch.setenv(ct.GUARD_ENV, "1")          # env 说开，参数说关 → 关
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: "已修复"})
    _, d = _run_llm(guard=False)
    assert _text_clean(sid) == "已修复"
    assert d["llm_ok"] == 1


def test_guard_on_by_param_overrides_env(monkeypatch):
    monkeypatch.setenv(ct.GUARD_ENV, "0")          # env 说关，参数说开 → 开
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: "已修复"})
    _, d = _run_llm(guard=True)
    assert _text_clean(sid) is None
    assert d["llm_rejected"] == 1


def test_identical_output_skips_write(monkeypatch):
    """out 与 src 完全相同 → 幂等跳过（不写、不计 ok、不计 rejected）。"""
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: SRC})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_identical"] == 1
    assert d["llm_ok"] == 0
    assert d["llm_rejected"] == 0


def test_short_src_exempt_from_ratio():
    """短原文（≤ GUARD_SHORT_SRC）不做比例卡，只卡绝对下限。"""
    src = ("他顺着白sè的石阶一路上行，山风卷着雾气扑面而来，"
           "把残存的暖意也一并吹散了。一路无言。")[:ct.GUARD_SHORT_SRC]
    out = "他顺着白色的石阶一路上行，山风卷雾气而来。"
    assert len(src.strip()) == ct.GUARD_SHORT_SRC
    assert len(out) >= ct.GUARD_MIN_LEN                      # 过绝对下限
    assert len(out) < ct.GUARD_MIN_RATIO * len(src.strip())  # 但低于 0.6×src
    assert ct.guard_verdict(src, out) == "accept"
    # 同样的 out 配长原文 → 照判截断（豁免只限短原文）
    long_src = src + "他推门进来，屋里没人，只有炉火还在低低地烧着，映得他半边脸发红。"
    assert ct.guard_verdict(long_src, out) == "truncated"


def test_guard_verdict_unit():
    assert ct.guard_verdict(SRC, "已修复") == "too_short"
    assert ct.guard_verdict(SRC, SRC) == "identical"
    assert ct.guard_verdict(SRC, SRC.replace("白sè", "白色")) == "accept"


# ── 三张补强卡（孤儿裁定 §7「三污染放行」收口）────────────────────
# 反例逐条取自原审 §4：(a) 同长度跑题、(b) 原文+垃圾追加、(c) 无声调拼音化。
# 加门前三种形态**全部落 accept**（现 main `guard_verdict` 只有四态穷尽链）。

# (a) 长度相当、内容换掉：60 字原文 → 61 字无关段落
OFFTOPIC = ("厨房里的水龙头滴了一夜，池子里泡着两只没洗的碗，窗台上的绿萝已经枯了半边，"
            "他蹲下去拧紧了阀门，听见楼道里有人拖着箱子下楼。")
# (b) 原文照抄 + 追加垃圾（水印式推广语连缀）
APPENDED = SRC + "阅读全文请记住本站最快更新无弹窗。" * 12
# (c) 整段无声调拼音化（SRC 的内容，字全换成拼音字母）
PINYIN = ("ta shun zhe bai se de shi jie yi lu shang xing, liang ce de jing shen ping zhang "
          "sui ta de jiao bu ci di liang qi, shan feng juan zhe wu qi pu mian er lai, "
          "ba na dian can cun de nuan yi ye yi bing chui san le.")


def test_oversize_card_blocks_appended_junk():
    """反例 (b)：`out = src + 垃圾` 长度只增不减 → 旧门 accept 放行；上限卡判 'oversize'。"""
    assert len(APPENDED.strip()) > ct.GUARD_MAX_RATIO * len(SRC.strip())   # 确实越过上限
    assert len(APPENDED.strip()) > len(SRC.strip())                        # 截断卡看不见它
    assert ct.guard_verdict(SRC, APPENDED) == "oversize"


def test_oversize_card_has_independent_blocking_value(monkeypatch):
    """上限卡不是"陪跑卡"：追加的是**与原文高度重复**的尾巴（相似度 0.9、无拉丁），
    相似卡与拉丁卡都放行，只有上限卡拦得住 —— 且**不落库**。
    （去掉本卡本用例必须转红：verdict 变 accept、text_clean 被写入。）"""
    bloated = SRC + "0" * 13                       # 60 → 73 字，越过 1.2×60 = 72
    assert len(bloated) > ct.GUARD_MAX_RATIO * len(SRC.strip())
    assert SequenceMatcher(None, SRC.strip(), bloated).ratio() >= ct.GUARD_MIN_SIMILARITY
    assert ct._latin_ratio(bloated) <= ct.GUARD_MAX_LATIN_RATIO
    assert ct.guard_verdict(SRC, bloated) == "oversize"
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: bloated})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_rejected"] == 1
    assert d["llm_ok"] == 0


def test_divergent_card_blocks_same_length_offtopic():
    """反例 (a)：同长度跑题 → 长度类卡全过（旧门 accept）；相似卡判 'divergent'。"""
    assert len(OFFTOPIC.strip()) <= ct.GUARD_MAX_RATIO * len(SRC.strip())   # 不触发上限卡
    assert len(OFFTOPIC.strip()) >= ct.GUARD_MIN_RATIO * len(SRC.strip())   # 不触发截断卡
    sim = SequenceMatcher(None, SRC.strip(), OFFTOPIC.strip()).ratio()
    assert sim < ct.GUARD_MIN_SIMILARITY                                    # 相似度确实低
    assert ct.guard_verdict(SRC, OFFTOPIC) == "divergent"


def test_divergent_card_blocks_offtopic_not_written(monkeypatch):
    """反例 (a) 的落库级版本：同长度跑题不得覆写 text_clean（去掉本卡此行必转红）。"""
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: OFFTOPIC})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_rejected"] == 1
    assert d["llm_ok"] == 0


def test_latin_card_blocks_silent_pinyin():
    """反例 (c)：整段无声调拼音化 → 判 'latinized'，且 looks_broken 兜不住（本卡是唯一信号）。"""
    assert ct._latin_ratio(SRC.strip()) <= ct.GUARD_MAX_LATIN_RATIO      # 原文是中文
    assert ct._latin_ratio(PINYIN.strip()) > ct.GUARD_MAX_LATIN_RATIO    # 结果拉丁成灾
    assert ct.looks_broken(PINYIN) is False   # 原审 :113-119 口径：无声调纯拼音两查皆不命中
    assert ct.guard_verdict(SRC, PINYIN) == "latinized"


def test_latinized_output_not_written(monkeypatch):
    """反例 (c) 走完整 run_llm 路径：'latinized' 不落库，计 llm_rejected。"""
    sid = _seed(SRC)
    assert ct.needs_llm(SRC)
    _install_llm(monkeypatch, {SRC: PINYIN})
    _, d = _run_llm()
    assert _text_clean(sid) is None
    assert d["llm_rejected"] == 1
    assert d["llm_ok"] == 0


def test_oversize_and_divergent_not_written(monkeypatch):
    """反例 (a)(b) 走完整 run_llm 路径：新态同样不落库（'不得落库'是硬要求）。"""
    sid_a = _seed(SRC)                                  # (a) → divergent
    src_b = SRC + "山路的尽头有一座石坊，坊上没有刻字，只有风穿过时的一声低响。"
    assert len(src_b.strip()) > ct.GUARD_SHORT_SRC
    assert ct.needs_llm(src_b)
    sid_b = _seed(src_b)                                # (b) → oversize
    _install_llm(monkeypatch, {SRC: OFFTOPIC, src_b: APPENDED})
    _, d = _run_llm()
    assert _text_clean(sid_a) is None
    assert _text_clean(sid_b) is None
    assert d["llm_rejected"] == 2
    assert d["llm_ok"] == 0


def test_latin_card_has_independent_blocking_value():
    """拉丁占比卡不是"陪跑卡"：构造一个**过得了上限卡与相似卡**的半拼音化输出，
    只有本卡挡得住（去掉本卡该用例必须转红 ⇒ 变 accept ⇒ 污染照样落库）。"""
    src = ("他顺着白色的石阶一路上行两侧的精神屏障随他的脚步次第亮起山风卷着雾气扑面而来"
           "把那点残存的暖意也一并吹散了他推门进来屋里没人")[:60]
    out = src[:35] + "tuimengjinlaiwuliren"        # 前半照抄 + 后半无声调拼音（首字母压缩）
    assert len(out) <= ct.GUARD_MAX_RATIO * len(src)                          # 上限卡不触发
    assert SequenceMatcher(None, src, out).ratio() >= ct.GUARD_MIN_SIMILARITY  # 相似卡不触发
    assert ct.guard_verdict(src, out) == "latinized"


def test_half_pinyin_output_not_written(monkeypatch):
    """同上的**落库级**版本：needs_llm 认得出的原文，结果后半段被拼音化——
    上限卡（62 ≤ 72）与相似卡（0.64 ≥ 0.6）都放行，只有拉丁占比卡拦得住。"""
    half_pinyin = SRC.replace("白sè", "白色").replace("jīng神", "精神")[:41] + \
        "shanfengjuanzhewuqipu"
    assert len(half_pinyin) <= ct.GUARD_MAX_RATIO * len(SRC.strip())
    assert SequenceMatcher(None, SRC.strip(), half_pinyin).ratio() >= ct.GUARD_MIN_SIMILARITY
    assert ct.guard_verdict(SRC, half_pinyin) == "latinized"
    sid = _seed(SRC)
    _install_llm(monkeypatch, {SRC: half_pinyin})
    _, d = _run_llm()
    assert _text_clean(sid) is None          # 去掉本卡此行必转红（污染被写入 text_clean）
    assert d["llm_rejected"] == 1
    assert d["llm_ok"] == 0


def test_normal_repair_still_accepted_after_three_cards():
    """反向用例：正常修复不得被新卡误杀。
    (i) 长原文 + 拼音还原 → accept；(ii) 带水印原文 + 剥水印还原 → accept；
    (iii) 短原文（≤ GUARD_SHORT_SRC）+ 合理改写（长度缩、字符大换）→ accept（短原文豁免）。
    """
    fixed = SRC.replace("白sè", "白色").replace("jīng神", "精神")
    assert ct.guard_verdict(SRC, fixed) == "accept"
    assert SequenceMatcher(None, SRC.strip(), fixed.strip()).ratio() >= ct.GUARD_MIN_SIMILARITY
    wm_src = ("他顺着白sè的石阶一路上行，两侧的jīng神屏障随他的脚步次第亮起，"
              "(手打中文网7*24小时不间断更新纯txt手打小说m)"
              "山风卷着雾气扑面而来，把那点残存的暖意也一并吹散了。")
    assert len(wm_src.strip()) > ct.GUARD_SHORT_SRC
    assert ct.guard_verdict(wm_src, fixed) == "accept"
    short_src = "他顺着白sè的石阶一路上行，山风卷着雾气扑面而来，把残存的暖意也吹散了。"
    short_out = "他顺着白色的石阶一路上行，山风卷雾气而来。"
    assert len(short_src.strip()) <= ct.GUARD_SHORT_SRC
    assert len(short_out) < ct.GUARD_MIN_RATIO * len(short_src.strip())   # 长原文会判截断
    assert len(short_out) >= ct.GUARD_MIN_LEN                             # 但过绝对下限
    assert ct.guard_verdict(short_src, short_out) == "accept"


def test_oversize_boundary_is_strict():
    """上限卡边界：`out ≤ 1.2×src` 放行（严格 >），越过一字即 'oversize'。
    追加数字（非拉丁字母、不改相似度判定方向）以免串到拉丁占比卡。"""
    src = ("他顺着白色的石阶一路上行两侧的精神屏障随他的脚步次第亮起山风卷着雾气扑面而来"
           "把那点暖意吹散了他推门")[:41]
    assert len(src) > ct.GUARD_SHORT_SRC                       # 41 > 40，比例卡生效
    assert 49 <= ct.GUARD_MAX_RATIO * len(src) < 50            # 1.2×41 = 49.2
    assert ct.guard_verdict(src, src + "0" * 8) == "accept"    # 49 字：不越上限
    assert ct.guard_verdict(src, src + "0" * 9) == "oversize"  # 50 字：越上限


def test_existing_four_states_unchanged_by_new_cards():
    """新卡不得改变既有四态语义（判定顺序：identical → too_short → truncated → 新卡 → accept）。"""
    assert ct.guard_verdict(SRC, "已修复") == "too_short"
    assert ct.guard_verdict(SRC, SRC) == "identical"
    assert ct.guard_verdict(SRC, SRC.replace("白sè", "白色")) == "accept"
    long_src = SRC + "他推门进来，屋里没人，只有炉火还在低低地烧着。" * 10
    assert ct.guard_verdict(long_src, long_src[:50]) == "truncated"   # 截断优先于新卡
    assert ct.guard_verdict(long_src, "已修复") == "too_short"        # 绝对下限优先于新卡
    # 短原文豁免对新卡同样生效（既有口径：≤GUARD_SHORT_SRC 不做比例卡）
    short = SRC[:ct.GUARD_SHORT_SRC]
    assert len(short) == ct.GUARD_SHORT_SRC
    assert ct.guard_verdict(short, OFFTOPIC) == "accept"

