"""`backfill_v2_sentences.py` 的版本作用域 + `--verify` 一致性校验回归
（任务 lg-nsent-version-pred，2026-09-25；审计 lg-review-round-20260925 §1.6 收口）。

口径差实证：审查席指出 `scan()` 的查询只有 `n_sentences == 0` + 文本非空过滤，
**没有 `seg_version` 谓词**，而脚本叙述/文档一律以「v2 段 424,294 行」为口径——
「说只补 v2，实际全库补 0 值非空段」。功能安全（v1 非空段本就 ≥1），但声明与
实现不吻合。修法：`scan(seg_versions=None)` 默认**保持全库既有行为**并把「全库」
讲清楚；`--seg-version 2` 提供严格作用域；`--verify` 只读校验逐版本报数，
「非空文本且 n_sentences=0」残留必须为 0 才 exit 0。

钉死七条：
1. 默认（seg_versions=None）＝全库：v1 的 0 值行也会被补（既有行为不变），
   但 v1 **已有非 0 值一律不动**；返回的 versions 分布同时含 1 和 2；
2. `seg_versions=[2]`：v1 的 0 值行**不动**，v2 的 0 值行补齐；空文本保持 0、
   原有非 0 不动的安全过滤在任何模式下不放宽；
3. dry-run（含限定版本时）零 commit：`Session.commit` 一调用就抛，照跑不误；
4. `--verify` 有残留 exit 1、清干净 exit 0；限定 `--seg-version` 时残留门只看
   该版本；`--verify` 与 `--apply`/`--work` 互斥（参数层拒，exit 2）；
5. `--verify` 零写入：commit/flush 全钉成「一调用就抛」，夹具快照分毫不变；
6. 口径单源（AST + 行为双钉）：脚本不 import `re`、不直调 `_sentences`/
   `_count_sents`，句数只能经唯一入口 `n_sents`——它就是 `import_corpus_v2`
   的函数本体（别名直取，不许包第二层）；
7. CLI 真跑（子进程 + 独立临时库，不碰真库）：全库 verify 红 → 限定 v2 apply →
   v2 verify 绿但全库仍红（v1 残留）→ 全库 apply → 全库 verify 绿。

自包含：conftest 的临时 sqlite + tmp_path；零网络、零真实库、零真实模型请求。
进程内用例的写路径一律 `--work` 钉在自己的夹具书上（与既有测试同一纪律），
全库写口径只在独立子进程临时库里演。
"""
from __future__ import annotations

import ast
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


BF = _load("bf_scope", "backfill_v2_sentences.py")  # 被测：补数脚本（自带一份 import_corpus_v2＝BF.IC）

from app import db                                 # noqa: E402
from app.ids import new_id                         # noqa: E402
from app.models import Segment, Work               # noqa: E402

_n = [0]


def _para(i) -> str:
    return (f"甲{i}：他把茶盏搁回去，半天没有说话，外头风声一阵紧过一阵。"
            f"隔壁屋的灯还亮着，影子在窗纸上晃了两下。")


def _seed(texts: list[str], *, version: int,
          counts: list[int] | None = None) -> str:
    """造夹具书：每段按 counts 写 n_sentences（默认全 0），seg_version 指定版本。"""
    _n[0] += 1
    with db.session() as s:
        w = Work(id=new_id("WK"), title=f"scope-{_n[0]}", source=f"file:scope-{_n[0]}",
                 note="corpus_role: 训练语料; import_state: complete")
        s.add(w)
        s.flush()
        for i, ch in enumerate(texts):
            s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=ch,
                          n_sentences=(counts[i] if counts else 0),
                          n_chars=len(ch), seg_version=version, integrity="{}"))
        s.commit()
        return w.id


def _counts(work_id: str) -> list[int]:
    with db.session() as s:
        return [x.n_sentences for x in s.query(Segment)
                .filter(Segment.work_id == work_id).order_by(Segment.ordinal).all()]


def _snapshot() -> list[tuple]:
    """全库快照（只读）：零写入/幂等的硬判据。"""
    with db.session() as s:
        return sorted((x.id, x.seg_version, x.n_sentences, x.n_chars, x.text or "",
                       x.text_clean or "") for x in s.query(Segment).all())


@pytest.fixture(autouse=True)
def _db_ready():
    db.init_db()


# ── 1. 默认＝全库口径（既有行为不变），v1 已有非 0 值不动 ─────────

def test_default_scope_is_whole_db_backfills_v1_zero_rows_too():
    """不指定 seg_versions：v1 的 0 值行也补（全库口径如实保留），但 v1 的
    既有非 0 值分毫不动——这正是 §1.6 说的『说只补 v2、实际全库』。"""
    v1 = _seed([_para(1), "单句结束。", _para(2)], version=1, counts=[0, 7, 0])
    v2 = _seed([_para(3)], version=2)
    r = BF.scan(batch=2, sample=0, apply=True, work_ids=[v1, v2])
    assert sorted(r["versions"]) == [1, 2], "默认必须同时覆盖两个版本（全库）"
    assert r["versions"][1]["updated"] == 2 and r["versions"][2]["updated"] == 1
    assert _counts(v1) == [2, 7, 2], "v1 的 0 值行按同源口径补齐；已有值 7 不许动"
    assert _counts(v2) == [2]


# ── 2. seg_versions=[2]：v1 的 0 值行不动 ────────────────────────

def test_seg_version_2_leaves_v1_zero_rows_untouched():
    v1 = _seed([_para(1)], version=1)                    # v1 的 0 值行
    v2 = _seed([_para(2), "   ", "两句话。第二句！", "本就是 5。"],
               version=2, counts=[0, 0, 0, 5])           # 空文本/非 0 值边界
    r = BF.scan(batch=2, sample=0, apply=True, work_ids=[v1, v2], seg_versions=[2])
    assert sorted(r["versions"]) == [2], "限定 v2 后不许命中任何别的版本"
    assert r["seg_versions"] == [2]
    assert _counts(v1) == [0], "seg_versions=[2] 时 v1 的 0 值行必须原样不动"
    assert _counts(v2) == [2, 0, 2, 5], \
        "v2：0 值补齐、空文本保持 0、原有非 0 值不动（安全过滤任何模式不放宽）"
    assert r["matched"] == 3 and r["blank"] == 1 and r["updated"] == 2


def test_scan_rejects_empty_version_list():
    """[] 是「谁都不碰」的笔误，不是「全库」——必须炸，不许静默放行。"""
    with pytest.raises(ValueError):
        BF.scan(seg_versions=[])
    with pytest.raises(ValueError):
        BF.verify(seg_versions=[])
    with pytest.raises(ValueError):
        BF.scan(seg_versions=[0])


# ── 3. dry-run（含限定版本）零 commit ────────────────────────────

def test_dry_run_with_version_scope_commits_nothing(capsys, monkeypatch):
    v1 = _seed([_para(1)], version=1)
    v2 = _seed([_para(2), "   "], version=2)
    before = _snapshot()

    def no_commit(self, *a, **kw):
        raise AssertionError("dry-run 不许 commit")

    monkeypatch.setattr(Session, "commit", no_commit)
    try:
        assert BF.main(["--seg-version", "2", "--work", v1, "--work", v2]) == 0
    finally:
        monkeypatch.undo()
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "零写入" in out
    assert "seg_version∈{2}" in out, "打印必须写明版本作用域"
    assert "版本分布 seg_version=2" in out and "seg_version=1" not in out.split("版本分布")[1], \
        "命中分布里只许出现 v2"
    assert _counts(v1) == [0] and _counts(v2) == [0, 0], "dry-run 之后一行都不许变"
    assert _snapshot() == before


# ── 4+5. --verify：残留门 + 零写入 ───────────────────────────────

def test_verify_flags_residual_and_writes_nothing(capsys, monkeypatch):
    """有残留（非空文本 + n_sentences=0）→ exit 1 + 样本 id；全程零写入。

    限定 `--seg-version` 的门（v1 残留时 v2 门应为绿）在下方 CLI 闭环用例里演；
    进程内共享临时库可能留有别的用例的 0 值行，全库门的绝对计数不在这里断言。"""
    _seed([_para(1)], version=2)                    # 留一行残留：非空文本 + n_sentences=0
    before = _snapshot()

    def boom(self, *a, **kw):
        raise AssertionError("--verify 是只读校验，不许 commit/flush")

    monkeypatch.setattr(Session, "commit", boom)
    monkeypatch.setattr(Session, "flush", boom)
    try:
        assert BF.main(["--verify"]) == 1, "有残留必须 exit 非 0"
    finally:
        monkeypatch.undo()
    out = capsys.readouterr().out
    assert "VERIFY" in out and "零写入" in out
    assert "残留" in out and "验证明不合格" in out and "样本 id" in out
    assert "seg_version=2" in out, "逐版本统计必须点名 seg_version"
    assert _snapshot() == before, "--verify 之后库内容分毫不变"


def test_verify_rejects_mutually_exclusive_args(capsys):
    for bad in (["--verify", "--apply"], ["--verify", "--work", "WK_x"]):
        with pytest.raises(SystemExit) as e:
            BF.main(bad)
        assert e.value.code == 2, "--verify 与写库/局部化参数互斥，参数层直接拒"
    assert "--verify" in capsys.readouterr().err


# ── 6. 口径单源：AST 钉死没有第二套切句器 + 行为钉死唯一入口 ─────

def test_script_has_no_second_sentence_segmenter():
    """源码扫描：不许 import/引用 `re`，不许直调 `_sentences`/`_count_sents`，
    句数只能经唯一入口 `n_sents`。"""
    src = (ROOT / "scripts" / "backfill_v2_sentences.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "re" not in imported, "不许自己 import re（切句规则只有上游一份）"
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "re" not in names
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_sentences" not in called and "_count_sents" not in called, \
        "只许经 n_sents 单点入口，不许绕过去直调上游私有实现"
    assert "n_sents" in called


def test_backfill_n_sents_routes_to_single_entry(monkeypatch):
    """行为侧同源：backfill 的 `n_sents`（＝`import_corpus_v2.n_sents` 别名）把活
    整个交给 `segmenter_v2._count_sents`。与既有 `test_n_sents_delegates_to_segmenter_v2`
    同一机关，钉的是补数这一侧；配合既有测试的 `_count_sents ≡ len(metrics_det._sentences)`
    恒等式 ⇒ 回填与两条导入路径落同一个切句实现（不存在第二套）。"""
    import app.segmenter_v2 as segmenter_v2

    seen = []

    def fake(text):
        seen.append(text)
        return 7

    monkeypatch.setattr(segmenter_v2, "_count_sents", fake)
    assert BF.n_sents("他说。她答。") == 7
    assert seen == ["他说。她答。"]
    # 别名直取：补数脚本用的就是它自己那份 import_corpus_v2 的 n_sents，不重新包一层
    assert BF.n_sents is BF.IC.n_sents
    assert BF.n_sents.__code__.co_filename.replace("\\", "/").endswith(
        "scripts/import_corpus_v2.py"), \
        "补数不许重新包一层口径，n_sents 本体只能住在导入脚本里"
    wid = _seed(["两句。还是两句！"], version=2)
    r = BF.scan(apply=True, sample=0, work_ids=[wid])
    assert r["updated"] == 1 and _counts(wid) == [7], "补数写库走的必须就是这个入口"


# ── 7. CLI 真跑（子进程 + 独立临时库；全库/限定作用域与退出码闭环）──

_SEED = """
import sys
sys.path.insert(0, r"%(root)s")
from app import db
from app.ids import new_id
from app.models import Segment, Work
db.init_db()
with db.session() as s:
    w = Work(id=new_id("WK"), title="cli-scope", source="file:cli-scope", note="n")
    s.add(w); s.flush()
    # v2：一句段 + 空文本段；v1：一行 bug 式 0 值非空段（默认全库口径该管它）
    rows = [("甲：一句话。第二句。", 2), ("   ", 2), ("乙：只有一句。", 1)]
    for i, (t, ver) in enumerate(rows):
        s.add(Segment(id=new_id("SEG"), work_id=w.id, ordinal=i, text=t,
                      n_sentences=0, n_chars=len(t), seg_version=ver, integrity="{}"))
    s.commit()
"""

_READ = (
    "import sys; sys.path.insert(0, r'%s')\n"
    "from app import db\nfrom app.models import Segment\n"
    "with db.session() as s:\n"
    "    print(sorted((x.seg_version, x.n_sentences) for x in s.query(Segment).all()))\n") % str(ROOT)


def _env_for(home: Path) -> dict:
    return dict(os.environ, LG_DATABASE_URL=f"sqlite:///{(home / 'cli.db').as_posix()}",
                LG_DATA_DIR=str(home), LG_LLM_MODE="mock", PYTHONIOENCODING="utf-8")


def _run(home: Path, script: str, *args: str, code: str | None = None):
    argv = [sys.executable, *([script] if code is None else ["-c", code]), *args]
    return subprocess.run(argv, cwd=str(ROOT), env=_env_for(home), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180)


@pytest.fixture
def cli_db(tmp_path) -> SimpleNamespace:
    seed = _run(tmp_path, "", code=_SEED % {"root": str(ROOT)})
    assert seed.returncode == 0, seed.stdout + seed.stderr
    assert (tmp_path / "cli.db").exists()
    return SimpleNamespace(dir=tmp_path,
                           script=str(ROOT / "scripts" / "backfill_v2_sentences.py"))


def _read(home: Path) -> str:
    r = _run(home, "", code=_READ)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def test_cli_verify_apply_verify_full_loop(cli_db):
    h, sc = cli_db.dir, cli_db.script
    assert _read(h) == "[(1, 0), (2, 0), (2, 0)]", "夹具：v1 一行 0 值非空，v2 两行 0（含空文本）"

    v0 = _run(h, sc, "--verify")
    assert v0.returncode == 1, f"两行非空 0 值（v1 一行、v2 一行）⇒ 全库 verify 必须红：{v0.stdout}"
    assert "全库（查询不含 seg_version 谓词）" in v0.stdout, "默认口径必须写明是全库"
    assert "仍有 2 行" in v0.stdout and "样本 id" in v0.stdout, v0.stdout
    assert "seg_version=1" in v0.stdout and "seg_version=2" in v0.stdout

    a1 = _run(h, sc, "--apply", "--seg-version", "2")
    assert a1.returncode == 0 and "seg_version∈{2}" in a1.stdout, a1.stdout
    assert "版本分布 seg_version=2" in a1.stdout and "版本分布 seg_version=1" not in a1.stdout
    assert _read(h) == "[(1, 0), (2, 0), (2, 2)]", "限定 v2：只补 v2 非空行，v1 与空文本不动"

    v1 = _run(h, sc, "--verify", "--seg-version", "2")
    assert v1.returncode == 0 and "验证合格" in v1.stdout, v1.stdout
    v2 = _run(h, sc, "--verify")
    assert v2.returncode == 1 and "样本 id" in v2.stdout, \
        "v2 已净但全库仍有 v1 残留 ⇒ 门必须还红，并列出样本 id"

    a2 = _run(h, sc, "--apply")                        # 默认全库：把 v1 那行也收掉
    assert a2.returncode == 0 and "updated 1" in a2.stdout, a2.stdout
    assert _read(h) == "[(1, 1), (2, 0), (2, 2)]"

    v3 = _run(h, sc, "--verify")
    assert v3.returncode == 0 and "验证合格" in v3.stdout, v3.stdout
    after = _read(h)
    assert _run(h, sc, "--verify").returncode == 0
    assert _read(h) == after, "--verify 反复跑也必须零写入"


def test_cli_help_documents_scope_and_verify(tmp_path):
    r = _run(tmp_path, str(ROOT / "scripts" / "backfill_v2_sentences.py"), "--help")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--seg-version" in r.stdout and "--verify" in r.stdout
    assert "全库" in r.stdout, "帮助必须把『默认＝全库口径』讲清楚"
