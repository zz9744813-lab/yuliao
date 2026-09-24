"""锚复核的顺序无关性 + 反向钉（假红修复的回归）。

背景（docs/registry_test_anchor_fix_20260924.md）：
``test_clean_tree_passes_and_roots_only`` 单跑绿、与 test_k2_pairs_gen.py
同跑红——别的测试文件建的**有内容却无锚**登记行，被 work_registry 的
clean_tree 扫到（clean_tree 的占位登记只补**无登记行**的 Work，
见 tests/test_work_registry.py:90-91），于是 ``verify()`` 的锚复核
（scripts/verify_work_registry.py:180 的 ``(r.text_sha256 or None) !=
(sha or None)``）各报一条 ``anchor_drift``，把 ``n_mismatch`` 顶到非零。

本文件钉死两件事：
1. **顺序无关**——一条带锚（修复后形态）的遗留登记行，在 clean_tree 下
   ``n_mismatch == 0``，不再制造跨文件假红；
2. **反向钉**（不许把门修松）——有内容却无锚、或锚与内容不符时，
   ``verify()`` 仍必须响亮报 ``anchor_drift``。判定口径一字未动。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import verify_work_registry as VRW                       # noqa: E402
from app import db                                        # noqa: E402
from app.models import Segment, Work, WorkSource          # noqa: E402
from registry_anchor import anchor as _anchor             # noqa: E402

# 复用 test_work_registry 的 clean_tree：它把共享库整到一致态
# （给无登记行的遗留 Work 补带锚占位 + monkeypatch V2_MAP）。
from test_work_registry import clean_tree                 # noqa: E402,F401

TXT = "钉住内容锚的测试句：风过疏竹，雁渡寒潭，事过而心不留痕。"


def _mk(s, wid, segs=2):
    """造一部**有内容**的 Work（segs 段），返回后由调用方补登记行。"""
    s.add(Work(id=wid, title=wid, source="test:anchor_order"))
    s.flush()
    for i in range(segs):
        s.add(Segment(work_id=wid, ordinal=i, text=TXT, text_clean=TXT,
                      role=None, n_sentences=1, n_chars=len(TXT)))
    s.flush()
    s.commit()      # 不 commit 则 with 退出即回滚（SessionLocal 无 autocommit），
                    # 三个用例会全部空转：两个反向钉假红、顺序钉假绿


def _reg(s, wid, sha):
    """登记行：sha 由调用方决定——带锚（修复后）/ 无锚（旧 bug 形态）。"""
    s.add(WorkSource(work_id=wid, canonical_work_id=wid,
                     source_type="synthetic", text_version="corpus-v1",
                     text_sha256=sha, purpose_basis="test",
                     identity_purposes=[], license_purposes=[],
                     license_basis=None, metadata_status="verified",
                     metadata_basis="test"))
    s.flush()
    s.commit()      # 同 _mk：登记行必须真落库，否则 verify() 什么都看不到


def _purge(wid):
    with db.session() as s:
        s.query(Segment).filter_by(work_id=wid).delete()
        s.query(WorkSource).filter_by(work_id=wid).delete()
        s.query(Work).filter_by(id=wid).delete()
        s.commit()


def test_anchored_registered_row_is_order_independent(clean_tree):
    """有内容 + 带锚的登记行（修复后形态）：不产 anchor_drift，且
    clean_tree 整库一致 ⇒ n_mismatch==0（这条正是假红修复的验收）。"""
    wid = "WK-anchororder-ok"
    try:
        with db.session() as s:
            _mk(s, wid)
            _reg(s, wid, _anchor(s, wid))
        rep = VRW.verify()
        assert not any(m["kind"] == "anchor_drift" and m.get("work") == wid
                       for m in rep["mismatch"]), rep["mismatch"][:5]
        assert rep["n_mismatch"] == 0, rep["mismatch"][:5]
    finally:
        _purge(wid)


def test_content_work_without_anchor_still_drifts(clean_tree):
    """反向钉①：有内容却无锚（旧 bug 形态）→ verify 必须报 anchor_drift。
    锚复核判定口径未放宽，门没被修松。"""
    wid = "WK-anchororder-drift"
    try:
        with db.session() as s:
            _mk(s, wid)
            _reg(s, wid, None)          # 无锚 = 假红源头
        rep = VRW.verify()
        drift = [m for m in rep["mismatch"]
                 if m["kind"] == "anchor_drift" and m.get("work") == wid]
        assert drift, "无锚的有内容登记行必须被判 anchor_drift（不得放宽判定）"
        assert rep["n_mismatch"] >= 1
    finally:
        _purge(wid)


def test_content_churn_after_anchor_still_drifts(clean_tree):
    """反向钉②：锚=登记时的事实。带锚登记后库内容再变 → anchor_drift
    （证明补锚只是把登记行对齐到登记时内容，不是绕过漂移检测）。"""
    wid = "WK-anchororder-churn"
    try:
        with db.session() as s:
            _mk(s, wid, segs=1)
            _reg(s, wid, _anchor(s, wid))
        with db.session() as s:
            seg = s.query(Segment).filter_by(work_id=wid).first()
            seg.text = seg.text + "（事后改动）"
            seg.text_clean = seg.text
            s.commit()
        rep = VRW.verify()
        assert any(m["kind"] == "anchor_drift" and m.get("work") == wid
                   for m in rep["mismatch"])
    finally:
        _purge(wid)


def test_zero_segment_registered_row_anchor_is_null_no_drift(clean_tree):
    """0 段作品锚= NULL 是如实，不是缺失：登记后 verify 不报漂移
    （与 test_work_registry 的 0 段契约同源，防补锚误伤空作品）。"""
    wid = "WK-anchororder-empty"
    try:
        with db.session() as s:
            _mk(s, wid, segs=0)
            sha = _anchor(s, wid)
            assert sha is None, "0 段作品锚必须为 NULL"
            _reg(s, wid, sha)
        rep = VRW.verify()
        assert not any(m["kind"] == "anchor_drift" and m.get("work") == wid
                       for m in rep["mismatch"])
    finally:
        _purge(wid)
