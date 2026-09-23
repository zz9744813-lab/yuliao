"""LLM 覆写正文保留门（审计《language-genome-code-audit-20260923》非阻断项）。

锁定的不变量：

1. LLM 垃圾输出（过短，如"已修复"）→ 不覆盖 text_clean，计 llm_rejected；
2. 截断输出（远短于原文）→ 不覆盖；
3. 合法修复（长度相当、拼音还原）→ 正常覆盖，计 llm_ok，llm_rejected 不动；
4. 空/None 输出 → 走原 llm_failed 路径（回归：加门不改这条）；
5. 门可关：CLEAN_TEXT_GUARD=0 时垃圾输出照写（行为与加门前一致）；
6. 短原文豁免比例卡：只卡绝对下限，不把正常短段全拒。

全程不碰网络：llm_repair_batch 被 monkeypatch 成假函数。
"""
import sys
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
