"""import_corpus_v2 中断续跑回归（审计《language-genome-code-audit-20260923》非阻断项）。

缺陷实证：旧脚本先落 `Work` 再每 2000 段提交一次 ⇒ 中途崩溃/锁失败会在库里留下
**只有前若干段的半本**，而重跑只按 `Work.source` 命中就打印「已导入过」退 0
⇒ 半本永远补不齐，下游也没有「未完成」标记可识别。

钉死四条：
1. 完整导入后重跑 ⇒ 幂等跳过（保留「已导入过」+ 退 0），段数/行内容分毫不变；
2. 模拟中断（小批量提交 + 写到一半炸）⇒ 库里只留 `import_state: partial` 的书
   （无 anchors，下游不得当完整本用）；重跑续补缺口：段数与一次性完整导入一致、
   ordinal 无缺口无重复、已落库的段不换主键；
3. 一致性/兼容性边界：源文件变了（或重复 ordinal）⇒ 清理整本重导；改动前的存量
   完整本（无标记但有 anchors）仍判 complete 跳过；改动前的真半本被补齐；同源的
   **别人的** Work（v1 导入行）不参与续跑判定，沿用跳过且一段都不动；0 段 / 1 段
   边界输入不炸；
4. 退出码：跳过=0、导入=0、修复半本=0（+ 告警含 work id 与补齐段数）、参数不齐=2
   ——CLI 退码用子进程真跑临时库验证。

自包含：conftest 的临时 sqlite + tmp_path 造书，零网络、零真实库（`data/` 不碰）。
"""
from __future__ import annotations

import importlib.util as _u
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = _u.spec_from_file_location("ic", ROOT / "scripts" / "import_corpus_v2.py")
IC = _u.module_from_spec(_spec); _spec.loader.exec_module(IC)

from app import db, segmenter_v2                       # noqa: E402
from app.corpus import _read_text_loose                # noqa: E402
from app.ids import new_id                             # noqa: E402
from app.models import Segment, Work                   # noqa: E402

_n = [0]


def _para(i) -> str:
    return (f"甲{i}：他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"
            f"隔壁屋的灯还亮着，影子在窗纸上晃了两下。")


def _text(paras: int) -> str:
    return "\n".join(_para(i) for i in range(paras))


def _book(tmp_path: Path, *, paras: int = 8, text: str | None = None) -> str:
    """造一本小书；返回绝对路径字符串（脚本按 `file:路径` 记账）。每段一个 v2 段。"""
    _n[0] += 1
    fp = tmp_path / f"book-{_n[0]}.txt"
    fp.write_text(_text(paras) if text is None else text, encoding="utf-8")
    return str(fp)


def _title(key: str) -> str:
    _n[0] += 1
    return f"t-icv2-{key}-{_n[0]}"


def _chunks(path: str) -> list[str]:
    return segmenter_v2.make_segments_v2(_read_text_loose(Path(path)))


def _work(path: str) -> Work:
    with db.session() as s:
        return s.query(Work).filter_by(source=f"file:{path}").one()


def _rows(work_id: str) -> list[tuple[int, str, str]]:
    """该书按 ordinal 排序的 (ordinal, text, 主键)——顺序与内容都要和一次性导入对齐。"""
    with db.session() as s:
        return [(x.ordinal, x.text, x.id) for x in
                (s.query(Segment).filter(Segment.work_id == work_id)
                 .order_by(Segment.ordinal).all())]


def _n_rows(work_id: str) -> int:
    with db.session() as s:
        return s.query(Segment).filter(Segment.work_id == work_id).count()


def _seed_work(path: str, *, title: str, note: str | None, anchors: str | None,
               texts: list[str], seg_version: int = 2) -> str:
    """直接建库内行（造「改动前的存量半本 / 完整本 / 别人写的同源书」）。"""
    db.init_db()
    with db.session() as s:
        w = Work(id=new_id("WK"), title=title, source=f"file:{path}",
                 note=note, anchors=anchors)
        s.add(w)
        s.flush()
        for i, ch in enumerate(texts):
            s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=ch,
                          n_chars=len(ch), seg_version=seg_version, integrity="{}"))
        s.commit()
        return w.id


def _crash_after(limit: int | None):
    """把中断做成「第 limit+1 次段分析时抛异常」——真实崩溃就落在分批提交的循环里。"""
    real, state = IC.si.analyze, {"n": 0, "limit": limit}

    def flaky(text, *, ordinal=0):
        state["n"] += 1
        if state["limit"] is not None and state["n"] > state["limit"]:
            raise RuntimeError("模拟中断（Ctrl-C / WAL 锁失败）")
        return real(text, ordinal=ordinal)

    return flaky, state


# ── 1. 完整导入的幂等 ────────────────────────────────────────

def test_full_import_then_rerun_is_idempotent(tmp_path, capsys):
    path = _book(tmp_path, paras=6)
    assert IC.main([path, _title("idem"), "训练语料"]) == 0
    assert len(_chunks(path)) == 6, "夹具得真切出多段，否则本用例什么都没钉"
    w = _work(path)
    assert IC.import_state(w) == IC.COMPLETE
    assert json.loads(w.anchors)["segmenter"] == 2
    before = _rows(w.id)
    capsys.readouterr()

    assert IC.main([path, _title("idem"), "训练语料"]) == 0
    assert "已导入过" in capsys.readouterr().out
    assert _rows(w.id) == before, "幂等重跑不许动已有的一行"


# ── 2. 半本 + 续跑 ──────────────────────────────────────────

def test_interrupt_then_rerun_resumes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(IC, "BATCH", 2)                  # 每 2 段提交 ⇒ 崩溃会留下已落库的半本
    flaky, st = _crash_after(4)                          # 第 5 段分析时炸：前 4 段已提交
    monkeypatch.setattr(IC.si, "analyze", flaky)
    path = _book(tmp_path, paras=8)
    with pytest.raises(RuntimeError, match="模拟中断"):
        IC.main([path, _title("resume"), "训练语料"])

    chunks = _chunks(path)
    assert len(chunks) == 8
    w = _work(path)
    assert IC.import_state(w) == IC.PARTIAL, "半本必须带未完成标记"
    assert w.anchors is None, "未完成的书不得有三重锚（下游据此判可用）"
    assert [o for o, _, _ in _rows(w.id)] == [0, 1, 2, 3], "已提交的段落库、未提交的回滚"

    st["limit"] = None                                   # 中断原因消失：同一路径重跑
    capsys.readouterr()
    assert IC.main([path, _title("resume"), "训练语料"]) == 0
    out = capsys.readouterr().out
    assert "[告警]" in out and f"work={w.id}" in out, "修复半本必须点名是哪本书"
    assert "已提交 4 段" in out and "期望 8 段" in out and "补齐 4 段" in out

    got = _rows(w.id)
    assert [o for o, _, _ in got] == list(range(8)), "ordinal 无缺口"
    assert len({sid for _, _, sid in got}) == len(got) == 8, "无重复段、无重复 ordinal"
    assert [t for _, t, _ in got] == chunks, "补齐后的书必须等于一次性完整导入"
    assert IC.import_state(_work(path)) == IC.COMPLETE

    assert IC.main([path, _title("resume"), "训练语料"]) == 0
    assert "已导入过" in capsys.readouterr().out
    assert _rows(w.id) == got, "补齐后再重跑不得再动一行"


def test_resume_keeps_committed_rows(tmp_path, capsys, monkeypatch):
    """续跑只写缺失段：已提交的段保留原主键（不重插、不产生第二份）。"""
    monkeypatch.setattr(IC, "BATCH", 3)
    flaky, st = _crash_after(4)
    monkeypatch.setattr(IC.si, "analyze", flaky)
    path = _book(tmp_path, paras=9)
    with pytest.raises(RuntimeError):
        IC.main([path, _title("keep"), "训练语料"])
    wid = _work(path).id
    head = {o: sid for o, _, sid in _rows(wid)}
    assert len(head) == 3                                    # 第 3 段后提交过一次的痕迹
    st["limit"] = None
    IC.main([path, _title("keep"), "训练语料"])
    after = {o: sid for o, _, sid in _rows(wid)}
    assert all(after[o] == sid for o, sid in head.items()), "已落库的段不许被替换"
    assert len(after) == len(set(after.values())) == 9


# ── 3. 一致性判据与存量行兼容 ───────────────────────────────

def test_changed_source_file_rebuilds(tmp_path, capsys, monkeypatch):
    """半本 + 源文件已改 ⇒ 库内段与新切分不一致：清理整本重导（确定且幂等，不拼盘）。"""
    monkeypatch.setattr(IC, "BATCH", 2)
    flaky, st = _crash_after(4)
    monkeypatch.setattr(IC.si, "analyze", flaky)
    path = _book(tmp_path, paras=8)
    with pytest.raises(RuntimeError):
        IC.main([path, _title("mut"), "训练语料"])
    wid = _work(path).id
    old = {t for _, t, _ in _rows(wid)}
    st["limit"] = None
    Path(path).write_text("\n".join(_para(i) + "他又补了一句。" for i in range(5)),
                          encoding="utf-8")
    new = _chunks(path)
    assert len(new) == 5 and not old & set(new), "夹具要保证新旧切分确实不同"

    capsys.readouterr()
    assert IC.main([path, _title("mut"), "训练语料"]) == 0
    out = capsys.readouterr().out
    assert "[告警]" in out and "整本重导" in out and f"work={wid}" in out
    got = _rows(wid)
    assert [t for _, t, _ in got] == new, "重导完全按当前源文件，不留半本残段"
    assert _n_rows(wid) == len(got) == 5 and not old & {t for _, t, _ in got}
    assert IC.import_state(_work(path)) == IC.COMPLETE
    capsys.readouterr()
    assert IC.main([path, _title("mut"), "训练语料"]) == 0
    assert "已导入过" in capsys.readouterr().out and _n_rows(wid) == 5


def test_duplicate_ordinal_in_partial_rebuilds(tmp_path, capsys):
    """库里有重复 ordinal（历史脏行）⇒ 走清理重导，续跑不会二次撞序。"""
    path = _book(tmp_path, paras=5)
    chunks = _chunks(path)
    wid = _seed_work(path, title="脏行半本", note="corpus_role: r; segmenter=v2",
                     anchors=None, texts=chunks[:2])
    with db.session() as s:                               # 再塞一行撞 ordinal=1
        seg = s.query(Segment).filter(Segment.work_id == wid,
                                      Segment.ordinal == 1).one()
        s.add(Segment(id=new_id("SEG"), work_id=wid, ordinal=1, text=seg.text,
                      n_chars=seg.n_chars, seg_version=2, integrity="{}"))
        s.commit()
    assert IC.import_state(_work(path)) == IC.PARTIAL
    capsys.readouterr()
    assert IC.main([path, _title("dup"), "训练语料"]) == 0
    out = capsys.readouterr().out
    assert "整本重导" in out and "重复 ordinal 1 段" in out
    assert [t for _, t, _ in _rows(wid)] == chunks
    assert _n_rows(wid) == 5, "重导后不留重复行"


def test_legacy_complete_row_still_skips(tmp_path, capsys):
    """改动前的存量完整本（note 无标记、anchors 已写）⇒ 判 complete，幂等跳过。"""
    path = _book(tmp_path, paras=4)
    chunks = _chunks(path)
    wid = _seed_work(path, title="legacy-complete",
                     note="corpus_role: 旧行; segmenter=v2",
                     anchors=json.dumps({"segmenter": 2}, ensure_ascii=False),
                     texts=chunks)
    assert IC.import_state(_work(path)) == IC.COMPLETE, "老行按 anchors 回退判完整本"
    assert IC.main([path, "legacy-complete", "旧行"]) == 0
    assert "已导入过" in capsys.readouterr().out
    assert _n_rows(wid) == 4


def test_legacy_partial_row_is_repaired(tmp_path, capsys):
    """改动前的真半本（无标记、无 anchors）⇒ 续跑补齐并告警：老库里躺着的半本有救。"""
    path = _book(tmp_path, paras=6)
    chunks = _chunks(path)
    wid = _seed_work(path, title="legacy-partial",
                     note="corpus_role: 旧行; segmenter=v2", anchors=None,
                     texts=chunks[:2])
    assert IC.import_state(_work(path)) == IC.PARTIAL
    assert IC.main([path, "legacy-partial", "旧行"]) == 0
    out = capsys.readouterr().out
    assert "[告警]" in out and f"work={wid}" in out and "补齐 4 段" in out
    assert [t for _, t, _ in _rows(wid)] == chunks
    assert IC.import_state(_work(path)) == IC.COMPLETE


def test_foreign_work_with_same_source_untouched(tmp_path, capsys):
    """同源但是别人写的 Work（如 `app.corpus.import_file` 的 v1 本）⇒ 跳过，一段都不动。

    旧口径对这种行同样打印「已导入过」跳过；新逻辑若把它当半本，重跑会把 v1 的段
    当成「不一致的脏行」清掉——那是数据损失，必须钉住。"""
    path = _book(tmp_path, paras=5)
    chunks = _chunks(path)
    wid = _seed_work(path, title="v1-book", note=None, anchors=None,
                     texts=chunks, seg_version=1)
    assert not IC.is_ours(_work(path))
    assert IC.main([path, "v1-book", "训练语料"]) == 0
    assert "已导入过" in capsys.readouterr().out
    with db.session() as s:
        segs = (s.query(Segment).filter(Segment.work_id == wid)
                .order_by(Segment.ordinal).all())
    assert [g.seg_version for g in segs] == [1] * 5, "v1 段不能被 v2 重导覆盖"
    assert [g.text for g in segs] == chunks


# ── 4. 边界输入与退出码 ─────────────────────────────────────

def test_zero_segment_input(tmp_path, capsys):
    """0 段输入：不炸、落一本 complete 的空书，重跑仍幂等跳过。"""
    path = _book(tmp_path, text="")
    assert _chunks(path) == []
    assert IC.main([path, _title("empty"), "训练语料"]) == 0
    w = _work(path)
    assert IC.import_state(w) == IC.COMPLETE and _n_rows(w.id) == 0
    assert json.loads(w.anchors)["eligible_rate"] == 0.0
    capsys.readouterr()
    assert IC.main([path, _title("empty"), "训练语料"]) == 0
    out = capsys.readouterr().out
    assert "已导入过" in out and _n_rows(w.id) == 0


def test_single_segment_input(tmp_path, capsys, monkeypatch):
    """1 段输入 + 小批量提交：一次提交即完成，不留半本；重跑幂等。"""
    monkeypatch.setattr(IC, "BATCH", 1)
    path = _book(tmp_path, text=_para(1))
    assert len(_chunks(path)) == 1
    assert IC.main([path, _title("one"), "基准语料"]) == 0
    w = _work(path)
    assert [o for o, _, _ in _rows(w.id)] == [0]
    assert IC.import_state(w) == IC.COMPLETE
    capsys.readouterr()
    assert IC.main([path, _title("one"), "基准语料"]) == 0
    assert "已导入过" in capsys.readouterr().out and _n_rows(w.id) == 1


def test_usage_error_exit_2(capsys):
    """参数不齐：用法走 stderr，退码 2（调用方要能判「根本没干活」）。"""
    assert IC.main([]) == 2
    err = capsys.readouterr().err
    assert "用法" in err and "import_corpus_v2.py" in err


def test_cli_exit_codes_on_temp_db(tmp_path):
    """子进程真跑 CLI：导入退 0、重跑「已导入过」退 0，且只写临时库（真库 data/ 不碰）。"""
    book = _book(tmp_path, paras=6)
    env = dict(os.environ,
               LG_DATABASE_URL=f"sqlite:///{(tmp_path / 'cli.db').as_posix()}",
               LG_DATA_DIR=str(tmp_path), LG_LLM_MODE="mock", PYTHONIOENCODING="utf-8")

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "import_corpus_v2.py"),
             book, "cli-book", "训练语料"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)

    real_db = ROOT / "data" / "language_genome.db"

    def _real_db_state():
        """真库指纹：存在性 + mtime_ns + size（真库本就存在时不得被本次 CLI 创建/改写）。"""
        if not real_db.exists():
            return (False, None, None)
        st = real_db.stat()
        return (True, st.st_mtime_ns, st.st_size)

    before = _real_db_state()
    first, second = run(), run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "v2 段 6" in first.stdout
    assert second.returncode == 0, second.stdout + second.stderr
    assert "已导入过" in second.stdout
    assert (tmp_path / "cli.db").exists()
    assert _real_db_state() == before, "临时库之外的真库不得被创建或改写"
