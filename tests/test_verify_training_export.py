"""A03 回归：训练导出验收器 + 训练入口（审查 20260920-1810；会审 09-21 三轮加固）。

审查实测（09-19 旧导出）：SFT/Rewrite 各 114 行与基准文本重合、RM 172 组
同源同文不同分、32 行源未校勘、旧文件在源码修复后未重导——差点被拿去训练。

锁死契约（含三轮 BLOCK 反例）：
1. 基准重合按「忽略空白、≥50 字、目标+前文」被逮出（含 dict 形态前文）；
2. **半漂移**：目标字段置空/占位而前文键还在 → 拒（三轮 BLOCK 项——
   「取到字段的文本数」与「可比文本数」分列，空串混不进比较数）；
3. RM 同源同文多分 / 分数不可哈希 → 拒；主键缺失 → 拒；
4. 源质量全家入闸：未校勘段与悬空段（库中查无）都进 manifest 都红；
5. 基准哈希集为空（连错库）→ 宁可拒，不做恒绿检查；
6. 全字段漂移（0 条可比文本）→ 拒（比较空转的假绿）；
7. accept 防手改 + 结构化：当场重算交叉核对 sha/kind/行数/基准哈希数；
   verify 的 SystemExit 在 accept 侧转 (False, problems)——每个文件都
   拿得到拒收理由，不在第一个坏文件裸崩（三轮 BLOCK 项）；
8. 训练入口 train_entry：任一拒绝非零退出 + 参数位 kind 校验（--sft
   传 rm 文件=张冠李戴拒收）；
9. 同源不相加：并集数 + 行数口径/源段口径两种虚增分列；
10. 干净导出（真实 src_ok 段）→ 全 0 → PASS。
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
    """测试库（conftest 的 LG_DATABASE_URL 临时库，非生产库）：一条冻结基准
    + 一个 src_ok 段。id 在 commit 前取出（不赌 ORM 实例过期语义），
    teardown 全清——共享库不留残留。"""
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
        s.flush()
        sid, wid = seg.id, w.id
        s.commit()
        yield {"seg_id": sid, "work_id": wid}
    with db.session() as s:
        s.query(BenchmarkItem).filter_by(set_id="BS-vt").delete()
        s.query(BenchmarkSet).filter_by(id="BS-vt").delete()
        s.query(Segment).filter_by(id=sid).delete()
        s.query(Work).filter_by(id=wid).delete()
        s.commit()


def _write_rows(tmp_path, name, rows):
    p = tmp_path / name
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def test_bench_overlap_detected_including_dict_context(clean_env, tmp_path):
    """重合三连：target 命中 / dict 形态前文命中 / 短文本不误报。"""
    seg = clean_env["seg_id"]
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg, "target": LONG_BENCH},
        {"id": "R2", "segment_id": seg, "target": "无关文本" * 30,
         "prev1": SHORT_TXT},
    ])
    rep = VT.verify(str(p), write_manifest=False)
    assert rep["bench_overlap_rows"] == 1
    assert rep["acceptance"]["passed"] is False, "基准重合必须拒收"
    assert rep["bench_overlap_examples"][0]["line_no"] == 1, "示例必须带行号定位"
    # rewrite 的 context 是 dict：递归抽出 prev1 命中也必须逮住（曾漏）
    p2 = _write_rows(tmp_path, "rewrite_v9.jsonl", [
        {"id": "R3", "segment_id": seg, "output": "正常输出" * 40,
         "context": {"prev1": LONG_BENCH, "prev2": "另段"}},
    ])
    rep2 = VT.verify(str(p2), write_manifest=False)
    assert rep2["bench_overlap_rows"] == 1, "dict 形态前文漏检=漏一片"


def test_half_drift_empty_target_refused(clean_env, tmp_path):
    """三轮 BLOCK 项：目标字段置空、前文键还在（半漂移）→ 拒。

    旧实现的 n_texts 在 ≥50 过滤前自增，空占位也算比较数——「字段名在
    值变空」照样亮绿。现在：取到文本数与可比文本数分列，且每行目标
    字段必须有 ≥50 字可比文本。"""
    seg = clean_env["seg_id"]
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg, "target": "",      # 目标置空
         "prev1": "前文很长但不是基准文本" * 10},            # 前文键还在
    ])
    rep = VT.verify(str(p), write_manifest=False)
    assert rep["rows_target_empty"] == 1, "目标置空必须点行数"
    assert rep["n_texts_seen"] > rep["n_texts_comparable"], \
        "空占位只能进 seen、不许混进可比数"
    assert rep["acceptance"]["passed"] is False, "半漂移不许亮绿"
    # 对照：目标**短而非空**（1-49 字）= 合法短文本，只报不闸
    p2 = _write_rows(tmp_path, "writer_sft_v9b.jsonl", [
        {"id": "R9", "segment_id": seg, "target": "短的目标文本" * 3,
         "prev1": "长前文" * 20},
    ])
    rep2 = VT.verify(str(p2), write_manifest=False)
    assert rep2["rows_target_short"] == 1 and rep2["rows_target_empty"] == 0
    assert rep2["acceptance"]["passed"] is True, "合法短目标（真件实测 73/48 行）不许误伤"


def test_rm_conflict_bad_score_and_missing_key_fail(clean_env, tmp_path):
    seg = clean_env["seg_id"]
    p = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "A1", "segment_id": seg, "text": "同一段文本" * 20, "score": 0.9},
        {"id": "A2", "segment_id": seg, "text": "同一段文本" * 20, "score": 0.1},
        {"id": "A3", "text": "缺主键且目标过短", "score": 0.5},
        {"id": "A4", "segment_id": seg, "text": "多维分数" * 20,
         "score": {"sub": [0.1, 0.2]}},                      # 不可哈希分数
    ])
    rep = VT.verify(str(p), write_manifest=False)
    assert rep["rm_conflict_groups"] == 1, "同源同文两档分数=一组冲突"
    assert rep["missing_key_rows"] == 1
    assert rep["rm_bad_score_rows"] == 1, "不可哈希分数必须计数拒收，不裸炸"
    assert rep["acceptance"]["passed"] is False


def test_src_unverified_gates_acceptance(clean_env, tmp_path):
    """源质量是闸不是报：未校勘段 >0 → 验收红（A03 原缺陷回归）。"""
    with db.session() as s:
        w = Work(title="t-vt2", source="test:vt2")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="x", role=None,
                      integrity=None, n_sentences=1, n_chars=1)
        s.add(seg)
        s.flush()
        sid, wid = seg.id, w.id
        s.commit()
    try:
        p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
            {"id": "R1", "segment_id": sid, "target": "无关文本" * 30},
        ])
        rep = VT.verify(str(p), write_manifest=False)
        assert rep["src_unverified_segments"] == 1, "未过源质量闸的段必须计数"
        assert rep["acceptance"]["passed"] is False, \
            "源质量只报不闸=未校勘段混进训练还亮绿"
    finally:
        with db.session() as s:
            s.query(Segment).filter_by(id=sid).delete()
            s.query(Work).filter_by(id=wid).delete()
            s.commit()


def test_src_missing_gates_and_is_in_manifest(clean_env, tmp_path):
    """悬空段（库中查无）：入闸入清单，不裸抛（三轮 BLOCK 项）。"""
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": "SEG-nope", "target": "无关文本" * 30},
    ])
    rep = VT.verify(str(p), write_manifest=False)   # 不 SystemExit
    assert rep["src_missing_segments"] == 1
    assert rep["acceptance"]["passed"] is False, "悬空段=导出与库不同源，必须红"


def test_empty_bench_set_refuses(clean_env, tmp_path, monkeypatch):
    """基准哈希集为空（连错库/基准未冻结）→ 宁可拒，不做恒绿检查。"""
    import export_training as ET
    monkeypatch.setattr(ET, "reset_bench_cache", lambda: None)
    monkeypatch.setattr(ET, "_bench_hashes", lambda: set())
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": clean_env["seg_id"], "target": "无关" * 40}])
    with pytest.raises(SystemExit, match="基准哈希集为空"):
        VT.verify(str(p), write_manifest=False)


def test_schema_drift_zero_comparable_refuses(clean_env, tmp_path):
    """字段名漂移（target→response）→ 0 条可比文本 → 拒（比较空转假绿）。"""
    p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": clean_env["seg_id"], "response": "改名后的字段" * 30}])
    with pytest.raises(SystemExit, match="0 条可比文本"):
        VT.verify(str(p), write_manifest=False)


def test_clean_export_passes_and_training_gate(tmp_path, clean_env):
    """干净导出（真实 src_ok 段）→ PASS；训练入口四态：
    接受 / 无清单拒 / 改动拒（sha）/ 手改 manifest+sha 对齐仍被当场重算逮住。"""
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
    # 文件被改（sha 不符）→ 拒收；坏 JSON 行在 accept 侧转结构化理由（不裸崩）
    with p.open("a", encoding="utf-8") as f:
        f.write("{这是坏 json\n")
    ok3, problems3 = VT.accept_for_training(str(p))
    assert not ok3 and any("sha256" in x or "当场重算" in x for x in problems3)
    # 手改 manifest 且 sha 对齐到位（先改文件再取新 sha）→ 仍被当场重算逮住。
    # 用独立干净文件做——坏 JSON 行会让重算结构性失败、盖过 overlap 这条路径。
    p3 = _write_rows(tmp_path, "rm_v9x.jsonl", [
        {"id": "C1", "segment_id": seg, "text": "z" * 60, "score": 0.5}])
    VT.verify(str(p3))                 # 旁挂 manifest
    with p3.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "C2", "segment_id": seg,
                            "text": LONG_BENCH}, ensure_ascii=False) + "\n")
    mf3 = p3.with_suffix(".manifest.json")
    man3 = json.loads(mf3.read_text(encoding="utf-8"))
    man3["acceptance"]["passed"] = True
    man3["bench_overlap_rows"] = 0
    man3["n_rows"] = 2                 # 行数也对齐——只留「当场重算」这一条防线
    man3["sha256"] = VT._sha256(p3)    # 追加完才取 sha——排除 sha 不符这条独立理由
    mf3.write_text(json.dumps(man3, ensure_ascii=False), encoding="utf-8")
    ok4, problems4 = VT.accept_for_training(str(p3))
    assert not ok4, "sha 对齐的手改 manifest 也必须被当场重算逮住"
    assert any("当场重算未通过" in x for x in problems4), \
        "拒收理由必须来自重算结果本身，不是碰巧 sha 不符"
    # 文件不存在 → 结构化拒收而不是裸 traceback
    ok5, _ = VT.accept_for_training(str(tmp_path / "nope.jsonl"))
    assert not ok5


def test_train_entry_rejects_unverified_and_miskinded(tmp_path, clean_env, monkeypatch):
    """训练入口接线：任一拒绝非零退出 + 参数位 kind 校验。"""
    seg = clean_env["seg_id"]
    good = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg, "target": "干净文本" * 30}])
    VT.verify(str(good))               # 生成 manifest
    monkeypatch.setattr(sys, "argv", ["train_entry.py", "--sft", str(good)])
    TE.main()                           # 全过 → 正常返回
    # 未验收文件 → 非零退出
    bad = _write_rows(tmp_path, "rm_v9.jsonl", [
        {"id": "B1", "segment_id": seg, "text": "y" * 60, "score": 0.5}])
    monkeypatch.setattr(sys, "argv", ["train_entry.py", "--rm", str(bad)])
    with pytest.raises(SystemExit) as ei:
        TE.main()
    assert ei.value.code, "未过验收必须非零退出"
    # 张冠李戴：--sft 位传 rm 文件 → 拒
    monkeypatch.setattr(sys, "argv", ["train_entry.py", "--sft", str(bad)])
    with pytest.raises(SystemExit):
        TE.main()


def test_union_distinct_sources_two_overcounts(tmp_path, clean_env):
    """同源不相加的机器口径：并集 + 行数口径/源段口径两种虚增分列。"""
    seg = clean_env["seg_id"]
    a = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
        {"id": "R1", "segment_id": seg}, {"id": "R2", "segment_id": seg}])
    b = _write_rows(tmp_path, "rewrite_v9.jsonl", [
        {"id": "W1", "segment_id": seg}])
    u = VT.union_distinct_sources([str(a), str(b)])
    assert u["union_distinct_sources"] == 1, "两文件同一源段，并集=1"
    assert u["sum_rows"] == 3
    assert u["overcount_if_summing_rows"] == 3 - 1, "行数口径虚增=行和-并集"
    assert u["overcount_if_summing_sources"] == 1 + 1 - 1, "源段口径虚增=段和-并集"


def test_export_schema_contract_tripwire():
    """绊线：验收器的比较字段与导出器 emit 的行键必须同步——导出器改
    字段名而验收器不跟，会静默少比。源码文本断言是粗绊线，红总比漏好。"""
    import export_training as ET
    src = Path(ET.__file__).read_text(encoding="utf-8")
    for kind, keys in VT._KIND_FIELDS.items():
        for k in keys:
            assert f'"{k}"' in src, \
                f"{kind} 的验收字段 {k} 在 export_training 源码中不存在——" \
                "导出器改了键名？两头必须一起改（否则验收静默少比）"


def test_context_piece_alone_hits_isolation(tmp_path):
    """A11（审查 20260920-1810）：多段基准 context 的隔离空隙——
    「段落A\n\n段落B」的基准 context，A 单独作为训练目标不命中整段
    哈希（70 字复现实测放行）。修复：组成段落各自入哈希集。"""
    db.init_db()
    para_a = ("他把茶喝完才起身，屋外风声很紧，谁也没有再说话，"
              "窗纸被吹得鼓了一下，远处的狗吠了一声又渐渐停了，屋里只剩灯花轻响。")   # ≥50
    para_b = "灯芯跳了一下，他坐回桌前，把没有写完的信重新拿起，又慢慢放下了，夜还很长。"
    seg = None
    with db.session() as s:
        st = BenchmarkSet(id="BS-a11", name="a11", version=1,
                          kind="corruption_detection", n_items=1, spec={}, note="")
        s.add(st)
        s.flush()
        s.add(BenchmarkItem(set_id=st.id, segment_id="SEG-a11", kind="x",
                            context=f"{para_a}\n\n{para_b}",       # 多段 context
                            text_a="题面甲", text_b="题面乙", answer="A", meta={}))
        w = Work(title="t-a11", source="test:a11")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="源文本", role=None,
                       integrity='{"src_ok": true}', n_sentences=1, n_chars=4)
        s.add(seg)
        s.commit()
        seg_id, wid, st_id = seg.id, w.id, st.id
    try:
        # 训练行 target = 基准 context 的**组成段落 A 单独**——旧整段哈希
        # 命不中，隔离放行（审查 70 字复现）；修复后必须逮住
        p = _write_rows(tmp_path, "writer_sft_v9.jsonl", [
            {"id": "R1", "segment_id": seg_id, "target": para_a,
             "prev1": "无关前文" * 10}])
        rep = VT.verify(str(p), write_manifest=False)
        assert rep["bench_overlap_rows"] == 1, \
            "基准 context 的组成段落单独出现必须算重合（A11 空隙）"
        assert rep["acceptance"]["passed"] is False
        # 对照：与基准内容完全无关的行不受影响（组成段落哈希不误伤）
        p2 = _write_rows(tmp_path, "writer_sft_v9b.jsonl", [
            {"id": "R2", "segment_id": seg_id,
             "target": "完全无关的另一段训练文本，内容与基准毫无关系。" * 3}])
        rep2 = VT.verify(str(p2), write_manifest=False)
        assert rep2["bench_overlap_rows"] == 0 and \
            rep2["acceptance"]["passed"] is True, "组成段落哈希不许误伤干净行"
    finally:
        with db.session() as s:
            s.query(BenchmarkItem).filter_by(set_id="BS-a11").delete()
            s.query(BenchmarkSet).filter_by(id="BS-a11").delete()
            s.query(Segment).filter_by(id=seg_id).delete()
            s.query(Work).filter_by(id=wid).delete()
            s.commit()
