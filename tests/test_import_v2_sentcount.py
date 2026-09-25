"""v2 导入 `n_sentences` 口径 + `backfill_v2_sentences.py` 补数回归（任务 lg-v2-sentence-count，2026-09-25）。

缺陷实证：`scripts/import_corpus_v2.py` 旧代码建段处硬写 `n_sentences=0`（同一行
`n_chars=len(ch)` 是对的），而 v1 路径 `app/corpus.py add_work()` 写
`len(_sentences(s))` ⇒ 两条导入路径口径不一致，v2 侧等于没写这一列；
`app/corpus.py segment_stats()` 的 `sentences_mean` 被 0 拉平成假读数
（**存储列/统计口径失真**，不是知识链污染：K2/K3 特征取自 `residuals_det.deltas` 现算）。

钉死六条：
1. v2 导入后每段的 `n_sentences` == 该段 `text` 的实际句数，且存在非 0 值；
2. 同源：`n_sents()` 就是把计数交给 `app.segmenter_v2._count_sents`（入口单点，
   不许在脚本里另写第三套切句器/正则/常量）；而它用的切句实现正是 v1 侧的
   `app.metrics_det._sentences`；
3. 与 v1 路径（`app/corpus.py import_file`）对同一文本给出**逐段相同**的句数；
4. backfill 默认 dry-run：只报 would_update 与抽样前后值，**零 commit、零写入**；
5. backfill `--apply` 在夹具库上把 0 补成真值：非空段补齐、空文本段保持 0、
   原本非 0 的行一律不动；重跑 updated=0 且快照分毫不变（幂等）；分批提交；
6. CLI 真跑（子进程 + 独立临时库）：`--help` 可跑、默认 dry-run 不改库、
   `--apply` 改库且幂等。

自包含：conftest 的临时 sqlite + tmp_path 造书；零网络、零真实库（`data/` 不碰，
`--apply` 只在夹具/临时库上验，真库由主控决定）。用 `--work` 把每个用例的
补数范围钉在自己的夹具书上，绝不越界改到别的用例落库的行。
"""
from __future__ import annotations

import importlib.util as _u
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(modname: str, filename: str):
    spec = _u.spec_from_file_location(modname, ROOT / "scripts" / filename)
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


IC = _load("ic_sc", "import_corpus_v2.py")        # 被测：v2 导入
BF = _load("bf_sc", "backfill_v2_sentences.py")   # 被测：补数脚本

from app import corpus, db, segmenter_v2           # noqa: E402
from app.ids import new_id                         # noqa: E402
from app.metrics_det import _sentences             # noqa: E402  v1 路径用的同一实现
from app.models import Segment, Work               # noqa: E402

_n = [0]


def _para(i) -> str:
    """每段两句（句末终止标点），开场非风险词、长度 ≥40 字 ⇒ v1/v2 都「一段一个 Segment」。"""
    return (f"甲{i}：他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"
            f"隔壁屋的灯还亮着，影子在窗纸上晃了两下。")


def _write_book(tmp_path: Path, paras: int = 6, *, tag: str = "sc") -> str:
    _n[0] += 1
    fp = tmp_path / f"{tag}-{_n[0]}.txt"
    fp.write_text("\n".join(_para(i) for i in range(paras)), encoding="utf-8")
    return str(fp)


def _title(tag: str) -> str:
    _n[0] += 1
    return f"t-sc-{tag}-{_n[0]}"


@pytest.fixture(autouse=True)
def _db_ready():
    db.init_db()


def _work_id_by_source(src: str) -> str:
    with db.session() as s:
        return s.query(Work).filter_by(source=src).one().id


def _rows(work_id: str) -> list[Segment]:
    with db.session() as s:
        return (s.query(Segment).filter(Segment.work_id == work_id)
                .order_by(Segment.ordinal).all())


def _snapshot(work_id: str | None = None) -> list[tuple[str, int, int, str]]:
    """(id, seg_version, n_sentences, text) 快照：零写入/幂等的硬判据。"""
    with db.session() as s:
        q = s.query(Segment)
        if work_id:
            q = q.filter(Segment.work_id == work_id)
        return sorted((x.id, x.seg_version, x.n_sentences, x.text or "") for x in q.all())


def _seed_segments(texts: list[str], *, counts: list[int] | None = None) -> str:
    """造「旧口径已落库」的夹具书：段行的 n_sentences 按 counts 写（默认全 0）。"""
    _n[0] += 1
    with db.session() as s:
        w = Work(id=new_id("WK"), title=f"seed-{_n[0]}", source=f"file:seed-{_n[0]}",
                 note="corpus_role: 训练语料; segmenter=v2; import_state: complete")
        s.add(w)
        s.flush()
        for i, ch in enumerate(texts):
            s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=ch,
                          n_sentences=(counts[i] if counts else 0),
                          n_chars=len(ch), seg_version=2, integrity="{}"))
        s.commit()
        return w.id


# ── 1. 导入侧：写的是真实句数 ────────────────────────────────

def test_import_writes_actual_sentence_counts(tmp_path, capsys):
    path = _write_book(tmp_path, paras=6)
    assert IC.main([path, _title("imp"), "训练语料"]) == 0
    assert "已导入过" not in capsys.readouterr().out
    rows = _rows(_work_id_by_source(f"file:{path}"))
    assert len(rows) == 6, "夹具得真切出 6 段，否则本用例什么都没钉"
    assert [r.n_sentences for r in rows] == [len(_sentences(r.text)) for r in rows]
    assert all(r.n_sentences >= 1 for r in rows), "非空段句数必须 ≥1，不许再硬写 0"
    assert max(r.n_sentences for r in rows) == 2, "每段两句：钉死计数值本身，不是钉死 0"


def test_old_zero_rows_wait_for_backfill_new_import_does_not(tmp_path):
    """同一份文本：旧口径落库的行保持 0（等补数脚本），新导入的行按实际句数落库。"""
    path = _write_book(tmp_path, paras=3)
    chunks = segmenter_v2.make_segments_v2(corpus._read_text_loose(Path(path)))
    old_wid = _seed_segments(chunks)                      # 模拟旧口径：全 0
    assert IC.main([path, _title("cmp"), "训练语料"]) == 0
    new_wid = _work_id_by_source(f"file:{path}")
    assert [r.n_sentences for r in _rows(old_wid)] == [0, 0, 0], "导入侧不许顺手改写历史行"
    assert [r.n_sentences for r in _rows(new_wid)] == [2, 2, 2], "新行必须按实际句数落库"


# ── 2+3. 同源：入口单点 + 与 v1 路径逐段一致 ─────────────────

def test_n_sents_delegates_to_segmenter_v2(monkeypatch):
    """口径入口单点：`n_sents()` 只能把活交给 `segmenter_v2._count_sents`。

    若有人图省事在脚本里另抄一份正则/常量（第三套切句器），本用例立刻红。"""
    seen = []

    def fake(text):
        seen.append(text)
        return 7

    monkeypatch.setattr(segmenter_v2, "_count_sents", fake)
    assert IC.n_sents("他说。她答。") == 7
    assert seen == ["他说。她答。"]


@pytest.mark.parametrize("t", [
    "一句话。", "", "   ", "甲来了。乙也来了，他笑。丙走！走吧？再来…结束；收尾。",
    _para(1), "没有终止标点的一长串字" * 4, "「你在吗。」\n第二行继续说。",
])
def test_count_sents_is_the_v1_metrics_impl(t):
    """同源证明（实现层）：`segmenter_v2._count_sents` ≡ `len(metrics_det._sentences(t))`
    ——v1 路径 `app/corpus.py add_work()` 用的就是后者，两条导入路径落同一个切句实现。"""
    assert segmenter_v2._count_sents(t) == len(_sentences(t))


def test_v2_import_matches_v1_import_file(tmp_path, monkeypatch):
    """同源证明（落库层）：同一份文本，v1 `corpus.import_file` 与 v2 导入逐段同句数。

    两本**内容相同、文件名不同**的书：`source` 键不同 ⇒ 不会互相判「已导入过」。"""
    monkeypatch.setenv("LG_IMPORT_ROOTS", str(tmp_path))
    v2_path = _write_book(tmp_path, paras=6, tag="v2")
    v1_path = _write_book(tmp_path, paras=6, tag="v1")
    assert (Path(v1_path).read_text(encoding="utf-8")
            == Path(v2_path).read_text(encoding="utf-8"))

    assert IC.main([v2_path, _title("v2"), "训练语料"]) == 0
    with db.session() as s:
        out = corpus.import_file(s, v1_path, title=_title("v1"))
        assert out["imported"] == 1, out
    v1_rows = _rows(out["work_id"])
    v2_rows = _rows(_work_id_by_source(f"file:{v2_path}"))
    assert [r.text for r in v1_rows] == [r.text for r in v2_rows], "夹具：两条路径切分一致"
    assert [r.n_sentences for r in v1_rows] == [r.n_sentences for r in v2_rows], \
        "同一文本两条导入路径的句数必须一模一样（同源）"
    assert all(r.n_sentences == 2 for r in v1_rows + v2_rows)


# ── 4. 补数默认 dry-run：零写入 ──────────────────────────────

def test_backfill_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    texts = [_para(1), _para(2), "   ", "单句就完事。"]
    wid = _seed_segments(texts)
    before = _snapshot(wid)

    def no_commit(self, *a, **kw):
        raise AssertionError("dry-run 不许 commit")

    monkeypatch.setattr(Session, "commit", no_commit)
    try:
        assert BF.main(["--work", wid]) == 0
    finally:
        monkeypatch.undo()
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "零写入" in out, "默认口径必须写明零写入"
    assert "would_update 3" in out, "非空 0 值段 3 条（空文本那条跳过）"
    assert "n_sentences 0 →" in out, "抽样必须打印前后值"
    assert _snapshot(wid) == before, "dry-run 之后夹具行一行都不许变"


def test_backfill_help_is_dry_run_by_default(capsys):
    with pytest.raises(SystemExit) as e:
        BF.main(["--help"])
    assert e.value.code == 0
    help_text = capsys.readouterr().out
    assert "dry-run" in help_text and "--apply" in help_text


def test_backfill_rejects_bad_args(capsys):
    with pytest.raises(SystemExit) as e:
        BF.main(["--batch", "0"])
    assert e.value.code == 2
    assert "--batch" in capsys.readouterr().err


# ── 5. --apply：补齐 + 幂等 + 分批提交 ───────────────────────

def test_backfill_apply_fixes_then_is_idempotent(tmp_path, capsys, monkeypatch):
    texts = [_para(1), _para(2), _para(3), "   ", "没有终止标点的一长串字",
             "两句话。第二句！"]
    keeps = [0, 0, 0, 0, 0, 5]          # 最后一行本就非 0：一律不许动
    wid = _seed_segments(texts, counts=keeps)
    other = _seed_segments(["旁白：别的书的 0 值段。"])   # 不带 --work 不许被碰
    commits: list[int] = []
    real_commit = Session.commit
    monkeypatch.setattr(Session, "commit",
                        lambda self: (commits.append(1), real_commit(self))[1])

    assert BF.main(["--apply", "--work", wid, "--batch", "2", "--sample", "2"]) == 0
    out = capsys.readouterr().out
    assert "APPLY" in out and "updated 4" in out
    assert [r.n_sentences for r in _rows(wid)] == [2, 2, 2, 0, 1, 5], \
        "非空段补成真值、空文本保持 0、原有非 0 值不动"
    assert [r.n_sentences for r in _rows(other)] == [0], "--work 范围外的书不许被改"
    assert len(commits) >= 2, "待补 4 行 ÷ 批量 2 ⇒ 必须分批提交，不许一次 commit 全表"
    after_first = _snapshot(wid)
    monkeypatch.undo()

    capsys.readouterr()
    assert BF.main(["--apply", "--work", wid, "--batch", "2"]) == 0
    assert "updated 0" in capsys.readouterr().out
    assert _snapshot(wid) == after_first, "重跑必须幂等：一行都不再变"
    assert BF.scan(batch=2, sample=0, work_ids=[wid])["matched"] == 1, \
        "只剩空文本段命中过滤器（保持 0），不再产生任何写入"


def test_backfill_apply_matches_import_counts(tmp_path):
    """补出来的值与「当初就写对」完全一致：backfill 与导入侧同口径。"""
    path = _write_book(tmp_path, paras=5)
    chunks = segmenter_v2.make_segments_v2(corpus._read_text_loose(Path(path)))
    wid = _seed_segments(chunks)
    assert BF.main(["--apply", "--work", wid]) == 0
    backfilled = [r.n_sentences for r in _rows(wid)]
    assert IC.main([path, _title("agree"), "训练语料"]) == 0
    assert [r.n_sentences for r in _rows(_work_id_by_source(f"file:{path}"))] == backfilled


# ── 6. CLI 真跑（子进程 + 独立临时库，不碰真库）──────────────

_SEED = """
import sys
sys.path.insert(0, r"%(root)s")
from app import db
from app.ids import new_id
from app.models import Segment, Work
db.init_db()
with db.session() as s:
    w = Work(id=new_id("WK"), title="cli-book", source="file:cli", note="n")
    s.add(w); s.flush()
    for i, t in enumerate(["甲：一句话。第二句。", "   ", "乙：只有一句。"]):
        s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=t,
                      n_sentences=0, n_chars=len(t), seg_version=2, integrity="{}"))
    s.commit()
"""

_READ_COUNTS = (
    "import sys; sys.path.insert(0, r'%s')\n"
    "from app import db\nfrom app.models import Segment\n"
    "with db.session() as s:\n"
    "    print(sorted(x.n_sentences for x in s.query(Segment).all()))\n") % str(ROOT)


def _env_for(home: Path) -> dict:
    return dict(os.environ, LG_DATABASE_URL=f"sqlite:///{(home / 'cli.db').as_posix()}",
                LG_DATA_DIR=str(home), LG_LLM_MODE="mock", PYTHONIOENCODING="utf-8")


def _run(home: Path, script: str, *args: str, code: str | None = None):
    argv = [sys.executable, *([script] if code is None else ["-c", code]), *args]
    return subprocess.run(argv, cwd=str(ROOT), env=_env_for(home), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180)


@pytest.fixture
def cli_db(tmp_path) -> SimpleNamespace:
    """独立临时库的子进程环境（真库 data/ 不碰）：先造三行旧口径 0 值段。"""
    seed = _run(tmp_path, "", code=_SEED % {"root": str(ROOT)})
    assert seed.returncode == 0, seed.stdout + seed.stderr
    assert (tmp_path / "cli.db").exists()
    return SimpleNamespace(dir=tmp_path, script=str(ROOT / "scripts" /
                                                      "backfill_v2_sentences.py"))


def _counts(home: Path) -> str:
    r = _run(home, "", code=_READ_COUNTS)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def test_cli_help_runs(cli_db):
    r = _run(cli_db.dir, cli_db.script, "--help")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry-run" in r.stdout and "--apply" in r.stdout


def test_cli_default_is_dry_run_then_apply_is_idempotent(cli_db):
    before = _counts(cli_db.dir)
    assert before == "[0, 0, 0]", f"夹具：三行旧口径 0 值，实际 {before}"

    dry = _run(cli_db.dir, cli_db.script)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "DRY-RUN" in dry.stdout and "would_update 2" in dry.stdout, dry.stdout
    assert _counts(cli_db.dir) == before, "默认 dry-run：库内容分毫不动"

    applied = _run(cli_db.dir, cli_db.script, "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "updated 2" in applied.stdout, applied.stdout
    after = _counts(cli_db.dir)
    assert after == "[0, 1, 2]", f"非空段补齐（2 句/1 句）、空文本保持 0，实际 {after}"

    again = _run(cli_db.dir, cli_db.script, "--apply")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "updated 0" in again.stdout, again.stdout
    assert _counts(cli_db.dir) == after, "重跑幂等"

    dry2 = _run(cli_db.dir, cli_db.script)
    assert "would_update 0" in dry2.stdout, dry2.stdout
    assert _counts(cli_db.dir) == after
