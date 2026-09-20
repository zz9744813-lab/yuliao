"""A03 回归：训练导出验收器 + 训练入口（审查 20260920-1810；会审 09-21 二轮加固）。

审查实测（09-19 旧导出）：SFT/Rewrite 各 114 行与基准文本重合、RM 172 组
同源同文不同分、32 行源未校勘、旧文件在源码修复后未重导——差点被拿去训练。

锁死契约（含二轮 BLOCK 反例）：
1. 基准重合按「忽略空白、≥50 字、目标+前文」被逮出（含 dict 形态前文）；
2. RM 同源同文多分 → 冲突组拒收；主键缺失拒收；
3. 源质量**闸**而不只是报：未校勘源段 >0 → 验收红（审查原缺陷之一，
   「只报不闸」会让未校勘段混进训练还亮绿）；
4. 基准哈希集为空（连错库）→ 宁可拒，不做恒绿检查；
5. 字段名漂移导致 0 条文本参与比较 → 拒（比较空转的假绿）；
6. accept_for_training 防手改：重跑纯检查并与 manifest 交叉核对——
   手改 passed 位 / 备份文件冒名 / sha 不符 → 一律拒收；
7. 训练入口 train_entry：任一文件拒收 → 非零退出；
8. 同源不相加：union_distinct_sources 按源段并集报，行数相加虚增可见；
9. 干净导出（真实已校勘段）→ 全 0 → PASS。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import train_entry as TE                       # noqa: E402
import verify_training_export as VT              # noqa: E402
from app import db                               # noqa: E402
from app.models import (BenchmarkItem, BenchmarkSet, Segment,  # noqa: E402
                        Work)

BENCH_TEXT = "他把茶喝完才起身，屋外风声很紧，谁也没有再说话，窗纸被吹得鼓了一下，远处还有狗吠不止。"   # 42 字
LONG_BENCH = (BENCH_TEXT + "于是他又想了很久，越想越觉得心里发沉，"
              "索性把灯芯拨亮了一些，坐下来慢慢写了一封信，寄往远方。")   # ≥50 字
SHORT_TXT = "短文本不参与比较。"


@pytest.fixture()
def clean_env():
    """测试库：一条冻结基准（≥50 字）+ 一个 src_ok 段。全部 teardown 清净。"""
    db.init_db()
    with db.session() as s:
        st = BenchmarkSet(id="BS-vt", name="vt", version=1,
                          kind="corruption_detection", n_items=1, spec={}, note="")
        s.add(st)
        s.flush()
        s.add(BenchmarkItem(set_id=st.id, segment_id="SEG-vt", kind="x",
                            context="", text_a=LONG_BENCH, text_b="X",
                            answer="A", meta={}))
        w = Work(title="t-vt", source="test:vt")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="一段文字", role=None,
                      integrity='{"src_ok": true}', n_sentences=1, n_chars=4)
        s.add(seg)
        s.commit()
        yield {"seg_id": seg.id, "work_id": w.id}
    with db.session() as s:
        s.query(BenchmarkItem).filter_by(set_id="BS-vt").delete()
        s.query(BenchmarkSet).filter_by(id="BS-vt").delete()
        s.query(Segment).filter_by(id=seg.id).delete()
        s.query(Work).filter_by(id=w.id).delete()
        s.commit()


def _write_rows(tmp_path, name, rows):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def test_bench_overlap_detected_including_dict_context(clean_env, tmp_path):
    """重合三连：target 命中 / dict 形态前文命中 / 短文本不误报。"""
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": clean_env["seg_id"], "target": LONG_BENCH},
        {"id": "R2", "segment_id": clean_env["seg_id"], "target": "无关文本" * 30,
         "prev1": SHORT_TXT},
    ])
    rep = VT.verify(str(p), write_manifest=False)
    assert rep["bench_overlap_rows"] == 1
    assert rep["acceptance"]["passed"] is False, "基准重合必须拒收"
    assert rep["bench_overlap_examples"][0]["line_no"] == 1, "示例必须带行号定位"
    # rewrite 的 context 是 dict：递归抽出 prev1 命中也必须逮住（曾漏）
    p2 = _write_rows(tmp_path, "rewrite_v9.jsonl", [
        {"id": "R3", "segment_id": clean_env["seg_id"], "output": "正常输出" * 40,
         "context": {"prev1": LONG_BENCH, "prev2": "另段"}},
    ])
    rep2 = VT.verify(str(p2), write_manifest=False)
    assert rep2["bench_overlap_rows"] == 1, "dict 形态前文漏检=漏一片"


def test_rm_conflict_and_missing_key_fail(clean_env, tmp_path):
    p = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "A1", "segment_id": clean_env["seg_id"],
         "text": "同一段文本" * 20, "score": 0.9},
        {"id": "A2", "segment_id": clean_env["seg_id"],
         "text": "同一段文本" * 20, "score": 0.1},
        {"id": "A3", "text": "缺主键的行", "score": 0.5},
    ])
    rep = VT.verify(str(p), write_manifest=False)
    assert rep["rm_conflict_groups"] == 1, "同源同文两档分数=一组冲突"
    assert rep["missing_key_rows"] == 1
    assert rep["acceptance"]["passed"] is False


def test_src_unverified_gates_acceptance(clean_env, tmp_path):
    """源质量是闸不是报：未校勘段 >0 → 验收红（二轮 BLOCK 项）。
    （挂 clean_env：空基准库会被「宁可拒」规则直接拒——本测要的是源质量红，
    不是基准空红。）"""
    db.init_db()
    with db.session() as s:
        w = Work(title="t-vt2", source="test:vt2")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="x", role=None,
                      integrity=None, n_sentences=1, n_chars=1)
        s.add(seg)
        s.commit()
        sid = seg.id
    try:
        p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
            {"id": "R1", "segment_id": sid, "target": "无关文本" * 30},
        ])
        rep = VT.verify(str(p), write_manifest=False)
        assert rep["src_unverified_segments"] == 1, "未过源质量闸的段必须计数"
        assert rep["acceptance"]["passed"] is False, \
            "源质量只报不闸=未校勘段混进训练还亮绿（A03 原缺陷回归）"
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(id=sid).delete()
            s.query(Work).filter_by(id=w.id).delete()
            s.commit()


def test_empty_bench_set_refuses(clean_env, tmp_path, monkeypatch):
    """基准哈希集为空（连错库/基准未冻结）→ 宁可拒，不做恒绿检查（二轮 BLOCK 项）。"""
    import export_training as ET
    monkeypatch.setattr(ET, "reset_bench_cache", lambda: None)
    monkeypatch.setattr(ET, "_bench_hashes", lambda: set())
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": clean_env["seg_id"], "target": "无关" * 40}])
    with pytest.raises(SystemExit, match="基准哈希集为空"):
        VT.verify(str(p), write_manifest=False)


def test_schema_drift_zero_texts_compared_refuses(clean_env, tmp_path):
    """字段名漂移（target→response）→ 0 条文本参与比较 → 拒（比较空转假绿）。"""
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": clean_env["seg_id"], "response": "改名后的字段" * 30}])
    with pytest.raises(SystemExit, match="比较空转|0 条文本"):
        VT.verify(str(p), write_manifest=False)


def test_clean_export_passes_and_training_gate(tmp_path, clean_env):
    """干净导出（真实 src_ok 段）→ PASS；训练入口三态：接受/无清单拒/改动拒。"""
    seg = clean_env["seg_id"]
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg, "target": "完全无关的训练文本" * 30},
        {"id": "R2", "segment_id": seg, "target": "另一段无关文本" * 30,
         "prev1": "前文", "prev2": "再前文"},
    ])
    rep = VT.verify(str(p))            # 默认旁挂写 manifest
    assert rep["acceptance"]["passed"] is True, "干净导出不该误伤"
    ok, problems = VT.accept_for_training(str(p))
    assert ok and not problems
    # 无 manifest 的文件（旧导出的形状）→ 拒收
    p2 = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "B1", "segment_id": seg, "text": "x" * 60, "score": 0.5}])
    ok2, _ = VT.accept_for_training(str(p2))
    assert not ok2, "没有验收清单的旧导出必须拒收"
    # 手改 manifest 的 passed 位 → 当场重算逮住（防手改，二轮 BLOCK 项）
    mf = p.with_suffix(".manifest.json")
    man = json.loads(mf.read_text(encoding="utf-8"))
    man["acceptance"]["passed"] = True
    man["bench_overlap_rows"] = 0
    man["sha256"] = VT._sha256(p)      # sha 对齐，伪装到位
    mf.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    # 在文件里塞进一段基准重合文本 → 当场重算必须拒
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "R3", "segment_id": seg,
                            "target": LONG_BENCH}, ensure_ascii=False) + "\n")
    ok3, problems3 = VT.accept_for_training(str(p))
    assert not ok3, "手改 manifest + 改文件必须被当场重算逮住"
    # 文件不存在 → 结构化拒收而不是裸 traceback
    ok4, _ = VT.accept_for_training(str(tmp_path / "nope.jsonl"))
    assert not ok4


def test_train_entry_rejects_unverified(tmp_path, clean_env, monkeypatch):
    """训练入口接线：任一文件未过验收 → 非零退出（二轮 BLOCK 严重项）。"""
    seg = clean_env["seg_id"]
    good = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg, "target": "干净文本" * 30}])
    VT.verify(str(good))               # 生成 manifest
    monkeypatch.setattr(sys, "argv", ["train_entry.py", "--sft", str(good)])
    TE.main()                           # 全过 → 正常返回
    bad = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "B1", "segment_id": seg, "text": "y" * 60, "score": 0.5}])
    monkeypatch.setattr(sys, "argv", ["train_entry.py", "--rm", str(bad)])
    with pytest.raises(SystemExit) as ei:
        TE.main()
    assert ei.value.code, "未过验收必须非零退出"


def test_union_distinct_sources(tmp_path, clean_env):
    """同源不相加的机器口径：并集数 + 行数相加的虚增量可见。"""
    seg = clean_env["seg_id"]
    a = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg}, {"id": "R2", "segment_id": seg}])
    b = _write_rows(tmp_path, "rewrite_v9.jsonl", [
        {"id": "W1", "segment_id": seg}])
    u = VT.union_distinct_sources([str(a), str(b)])
    assert u["union_distinct_sources"] == 1, "两文件同一源段，并集=1"
    assert u["sum_rows_would_overcount_by"] == 1, "按行数相加会虚增"
