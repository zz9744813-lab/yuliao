"""A03 回归：训练导出验收器（审查 20260920-1810）。

审查实测（09-19 旧导出）：SFT/Rewrite 各 114 行与基准文本重合、RM 172 组
同源同文不同分、旧文件在源码修复后未重导——旧导出差点被拿去训练。

锁死验收器契约：
1. 基准重合按「忽略空白、≥50 字、目标+前文」口径被逮出（含 dict 形态
   的前文——rewrite 的 context={prev1,prev2}，漏一层就是漏一片）；
2. RM 同源同文不同分 → 冲突组数 >0，验收红；
3. 主键缺失行计为缺键；
4. accept_for_training：无 manifest / sha 不符 / 验收未过 → 一律拒收；
5. 干净导出 → 三项全 0 → PASS + 训练入口接受。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_training_export as VT              # noqa: E402
from app import db                               # noqa: E402
from app.models import BenchmarkItem, BenchmarkSet, Segment, Work  # noqa: E402

BENCH_TEXT = "他把茶喝完才起身，屋外风声很紧，谁也没有再说话，窗纸被吹得鼓了一下，远处还有狗吠不止。"   # 42 字
LONG_BENCH = (BENCH_TEXT + "于是他又想了很久，越想越觉得心里发沉，"
              "索性把灯芯拨亮了一些，坐下来慢慢写了一封信，寄往远方。")   # ≥50 字
SHORT_TXT = "短文本不参与比较。"


@pytest.fixture()
def bench_seed():
    """测试库里冻一条基准条目（≥50 字），验收器重算重合必须命中它。"""
    db.init_db()
    with db.session() as s:
        st = BenchmarkSet(id="BS-vt", name="vt", version=1,
                          kind="corruption_detection", n_items=1, spec={}, note="")
        s.add(st)
        s.flush()
        s.add(BenchmarkItem(set_id=st.id, segment_id="SEG-vt", kind="x",
                            context="", text_a=LONG_BENCH, text_b="X",
                            answer="A", meta={}))
        s.commit()
    yield LONG_BENCH
    with db.session() as s:
        s.query(BenchmarkItem).filter_by(set_id="BS-vt").delete()
        s.query(BenchmarkSet).filter_by(id="BS-vt").delete()
        s.commit()


def _write_rows(tmp_path, name, rows):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def test_bench_overlap_detected_including_dict_context(bench_seed, tmp_path):
    """重合三连：target 命中 / dict 形态前文命中 / 短文本不误报。"""
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": "S1", "target": LONG_BENCH},      # 目标命中
        {"id": "R2", "segment_id": "S2", "target": "无关文本" * 30,
         "prev1": SHORT_TXT},                                        # 短前文不报
    ])
    rep = VT.verify(str(p))
    assert rep["bench_overlap_rows"] == 1
    assert rep["acceptance"]["passed"] is False, "基准重合必须拒收"
    # rewrite 的 context 是 dict：递归抽出 prev1 命中也必须逮住（曾漏）
    p2 = _write_rows(tmp_path, "rewrite_v9.jsonl", [
        {"id": "R3", "segment_id": "S3", "output": "正常输出" * 40,
         "context": {"prev1": LONG_BENCH, "prev2": "另段"}},
    ])
    rep2 = VT.verify(str(p2))
    assert rep2["bench_overlap_rows"] == 1, "dict 形态前文漏检=漏一片"


def test_rm_conflict_groups_fail_acceptance(bench_seed, tmp_path):
    """RM 同源同文不同分 → 冲突组 >0，验收红；消解后归零。"""
    p = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "A1", "segment_id": "S9", "text": "同一段文本" * 20, "score": 0.9},
        {"id": "A2", "segment_id": "S9", "text": "同一段文本" * 20, "score": 0.1},
        {"id": "A3", "segment_id": "S9", "text": "同一段文本" * 20, "score": 0.9},
    ])
    rep = VT.verify(str(p))
    assert rep["rm_conflict_groups"] == 1, "同源同文两档分数=一组冲突"
    assert rep["acceptance"]["passed"] is False


def test_missing_segment_key_fails(bench_seed, tmp_path):
    p = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "A1", "text": "缺主键的行", "score": 0.5},
    ])
    rep = VT.verify(str(p))
    assert rep["missing_key_rows"] == 1 and rep["acceptance"]["passed"] is False


def test_clean_export_passes_and_training_gate(tmp_path, bench_seed):
    """干净导出 → PASS；manifest 旁挂 + 训练入口三态：接受/无清单拒/改动拒。"""
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": "S1", "target": "完全无关的训练文本" * 30},
        {"id": "R2", "segment_id": "S2", "target": "另一段无关文本" * 30,
         "prev1": "前文", "prev2": "再前文"},
    ])
    rep = VT.verify(str(p))
    assert rep["acceptance"]["passed"] is True, "干净导出不该误伤"
    mf = p.with_suffix(".manifest.json")
    mf.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    ok, problems = VT.accept_for_training(str(p))
    assert ok and not problems
    # 无 manifest 的文件（旧导出的形状）→ 拒收
    p2 = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "B1", "segment_id": "S1", "text": "x" * 60, "score": 0.5}])
    ok2, _ = VT.accept_for_training(str(p2))
    assert not ok2, "没有验收清单的旧导出必须拒收"
    # 有 manifest 但文件被改（sha 不符）→ 拒收
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "R3", "segment_id": "S1",
                            "target": "y" * 60}, ensure_ascii=False) + "\n")
    ok3, problems3 = VT.accept_for_training(str(p))
    assert not ok3 and any("sha256" in x for x in problems3), "改动过的导出必须拒收"


def test_src_unverified_counted(tmp_path, bench_seed):
    """源质量独立复核：段 integrity 无 src_ok → 如实报数（不静默）。"""
    db.init_db()
    with db.session() as s:
        w = Work(title="t-vt", source="test:vt")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="一段文字", role=None,
                      integrity=None, n_sentences=1, n_chars=4)
        s.add(seg)
        s.commit()
        sid = seg.id
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": sid, "target": "无关文本" * 30},
    ])
    rep = VT.verify(str(p))
    assert rep["src_unverified_segments"] == 1, "未过源质量闸的段必须计数上报"
