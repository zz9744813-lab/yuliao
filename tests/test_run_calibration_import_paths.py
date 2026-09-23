"""`scripts/run_calibration.py` 的导入路径回归（审计 P1 导入守卫合入后的行为修复）。

**背景**：`95c7a9a` 给 `app/corpus.py` 的 `import_file` / `import_distiller` 加了路径
守卫——只接受**绝对路径**、realpath 必须落在 `LG_IMPORT_ROOTS`（默认 `DATA_DIR` +
`ROOT/inbox`）内。脚本原来把 CLI 参数原样透传（`scripts/run_calibration.py:65`），
于是 `--file 相对路径.txt` 被 `import_not_absolute` 拒了：既有工具的行为回归。

本文件锁死修复后的四条口径：
1. 相对路径按**当前工作目录**解析成绝对路径后才交给守卫（用户命令行直觉不变）；
2. 被守卫拒绝时给**可行动**提示（含 `LG_IMPORT_ROOTS` 与 `--import-root` 两条出路），
   退出码 `EXIT_IMPORT_REFUSED`，且**零库写**——与「文件不存在」（继续跑、退出码 0）
   可区分；
3. `--import-root <dir>`（可重复）把目录**并入**允许根，且不丢掉守卫原默认根；
4. `--distiller-db/--distiller-root` 同规则，且**不依赖**真实 `F:\\agi\\novel-distiller`
   存在（路径解析对不存在的路径同样成立）。

第 2 条用 monkeypatch 模拟守卫的拒绝返回值（本分支基线尚未合入守卫；合入后同一
断言仍然成立，因为它锁的是**脚本对守卫返回值的口径**，不是守卫内部实现）。
离线，不碰模型网关。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_calibration as RC  # noqa: E402
from app import config, corpus, db  # noqa: E402
from app.models import Segment, Work  # noqa: E402

BOOK_TEXT = ("这是一部用来做导入回归的极短作品。它的每一句都刻意写得长一些，"
             "好让切分器至少产出一段可用的文本。第二段继续说些无关紧要的情节，"
             "主角在雨里走了很久，很久，还是没有到家。\n第三段换了场景，"
             "屋里灯亮着，桌上有一封没拆的信，信纸边缘已经发黄发脆。")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """别把别的测试（或本机）的 LG_IMPORT_ROOTS 带进来。"""
    monkeypatch.delenv(RC.IMPORT_ROOTS_ENV, raising=False)
    db.init_db()


def _args(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["run_calibration.py", *argv])
    return RC.parse_args()


def _stub_inbox(monkeypatch):
    """本文件只测路径口径：inbox 目录扫描（真读真写）在这里不关心。"""
    monkeypatch.setattr(RC.corpus, "import_inbox",
                        lambda s, inbox=None: {"imported": 0, "skipped": 0})


def _counts(s):
    return s.query(Work).count(), s.query(Segment).count()


# ── 1. 相对路径 → 绝对路径后才交给守卫 ─────────────────────────

def test_relative_file_passed_to_guard_as_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "书.txt").write_text(BOOK_TEXT, encoding="utf-8")
    seen = []

    def fake_import_file(s, path, **kw):
        seen.append(path)
        return {"imported": 0, "error": f"文件不存在: {path}"}

    monkeypatch.setattr(RC.corpus, "import_file", fake_import_file)
    _stub_inbox(monkeypatch)
    args = _args(monkeypatch, ["--file", "书.txt"])

    rc = RC.run_imports(None, args)

    assert rc == 0, "文件不存在沿用既有行为（打印后继续），不是守卫拒绝"
    assert len(seen) == 1
    assert Path(seen[0]).is_absolute(), f"传给守卫的仍是相对路径: {seen[0]!r}"
    assert Path(seen[0]).resolve() == (tmp_path / "书.txt").resolve()


def test_abs_helper_resolves_cwd_relative_and_keeps_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cwd = Path(os.getcwd())
    assert RC._abs("a/b.txt") == str(cwd / "a" / "b.txt")
    assert Path(RC._abs(str(tmp_path / "x.txt"))).resolve() == (tmp_path / "x.txt").resolve()
    missing = cwd / "不存在" / "y.txt"
    assert not missing.exists()
    assert Path(RC._abs(str(missing))).is_absolute()  # 解析不要求路径存在


# ── 2. 守卫拒绝 ⇒ 可行动提示 + 专用退出码 + 零库写 ─────────────

@pytest.mark.parametrize("rule", [
    "import_not_absolute", "import_root_not_allowed",
    "import_ext_not_allowed", "import_too_large",
])
def test_guard_refusal_is_actionable_with_zero_write(tmp_path, monkeypatch, capsys, rule):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out.txt").write_text(BOOK_TEXT, encoding="utf-8")

    def fake_import_file(s, path, **kw):
        return {"imported": 0, "error": f"{rule}: {path} 不在允许根内"}

    monkeypatch.setattr(RC.corpus, "import_file", fake_import_file)
    args = _args(monkeypatch, ["--file", "out.txt"])

    with db.session() as s:
        before = _counts(s)
        rc = RC.run_imports(s, args)
        after = _counts(s)

    out = capsys.readouterr().out
    assert rc == RC.EXIT_IMPORT_REFUSED == 3, f"退出码要能区分守卫拒绝: rc={rc}"
    assert after == before, f"守卫拒绝后不得有任何库写: {before} → {after}"
    assert rule in out
    assert RC.IMPORT_ROOTS_ENV in out, "提示里要写清 LG_IMPORT_ROOTS 这条出路"
    assert "--import-root" in out, "提示里要写清本次调用可用的 --import-root"
    assert str(config.DATA_DIR) in out, "提示里要写清默认根（DATA_DIR / inbox）"


def test_missing_file_hint_differs_from_guard_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # 直接给定「文件不存在」这一类返回值：两种世界的口径都要成立
    monkeypatch.setattr(RC.corpus, "import_file",
                        lambda s, p, **kw: {"imported": 0, "error": f"文件不存在: {p}"})
    _stub_inbox(monkeypatch)
    args = _args(monkeypatch, ["--file", "nope.txt"])

    rc = RC.run_imports(None, args)

    out = capsys.readouterr().out
    assert rc == 0, "文件不存在不是守卫拒绝：既有行为是打印后继续"
    assert "文件不存在" in out
    assert RC.IMPORT_ROOTS_ENV not in out, "两类失败必须可区分，别把缺文件说成守卫拒绝"


# ── 3. --import-root：并入允许根（真导入，不 mock corpus） ──────

def test_import_root_makes_outside_file_really_importable(tmp_path, monkeypatch, capsys):
    src = tmp_path / "外部的书.txt"
    src.write_text(BOOK_TEXT, encoding="utf-8")
    assert str(tmp_path) != str(config.DATA_DIR)  # 确实在默认根之外
    args = _args(monkeypatch, ["--file", str(src), "--import-root", str(tmp_path)])

    with db.session() as s:
        before = _counts(s)
        rc = RC.run_imports(s, args)
        after = _counts(s)
    out = capsys.readouterr().out

    assert rc == 0
    assert "'imported': 1" in out, f"--import-root 放行后应真导入成功: {out}"
    assert after[0] == before[0] + 1, "库里应多出这一本书"
    assert after[1] > before[1], "这本书应真的被切成段落"
    roots = os.environ[RC.IMPORT_ROOTS_ENV].split(os.pathsep)
    norms = [os.path.normcase(str(Path(r).resolve())) for r in roots]
    assert os.path.normcase(str(tmp_path.resolve())) in norms
    # 关键：并入而不是替换——守卫原默认根必须还在
    for default in [str(p) for p in getattr(corpus, "_default_import_roots",
                                            lambda: [config.DATA_DIR,
                                                      config.ROOT / "inbox"])()]:
        assert os.path.normcase(str(Path(default).resolve())) in norms, \
            f"默认根被 --import-root 挤掉了: {roots}"


def test_import_root_repeatable_and_untouched_without_flag(tmp_path, monkeypatch):
    a, b = tmp_path / "ra", tmp_path / "rb"
    args = _args(monkeypatch, ["--import-root", str(a), "--import-root", str(b)])
    RC._apply_import_roots(args.import_root)
    norms = [os.path.normcase(r) for r in
             os.environ[RC.IMPORT_ROOTS_ENV].split(os.pathsep)]
    assert os.path.normcase(str(a.resolve())) in norms
    assert os.path.normcase(str(b.resolve())) in norms

    RC._apply_import_roots(None)  # 没给 --import-root 就不该动环境变量
    assert RC.IMPORT_ROOTS_ENV in os.environ  # 保持原值，不隐式清空


# ── 4. distiller 同规则，且不依赖真实 novel-distiller 目录存在 ──

def test_distiller_paths_passed_as_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_import_distiller(s, distiller_db, distiller_root):
        seen["db"], seen["root"] = distiller_db, distiller_root
        return {"imported": 0, "error": f"distiller db 不存在: {distiller_db}"}

    monkeypatch.setattr(RC.corpus, "import_distiller", fake_import_distiller)
    _stub_inbox(monkeypatch)
    args = _args(monkeypatch, ["--use-distiller", "--distiller-db", "d/app.sqlite3",
                               "--distiller-root", "dist"])

    rc = RC.run_imports(None, args)

    assert rc == 0, "db 不存在不是守卫拒绝"
    for key, expect in (("db", tmp_path / "d" / "app.sqlite3"),
                        ("root", tmp_path / "dist")):
        assert Path(seen[key]).is_absolute(), f"{key} 仍是相对路径: {seen[key]!r}"
        assert Path(seen[key]).resolve() == expect.resolve()


def test_default_distiller_paths_do_not_need_to_exist(tmp_path, monkeypatch):
    """脚本默认指向仓库外的 novel-distiller：只要求解析成绝对路径，不要求它在。"""
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(RC.corpus, "import_distiller",
                        lambda s, d, r: seen.append((d, r)) or {"imported": 0})
    _stub_inbox(monkeypatch)
    args = _args(monkeypatch, ["--use-distiller"])

    assert RC.run_imports(None, args) == 0
    assert all(Path(p).is_absolute() for p in seen[0])


@pytest.mark.parametrize("rule", ["import_not_absolute", "import_root_not_allowed"])
def test_distiller_guard_refusal_is_actionable_with_zero_write(tmp_path, monkeypatch,
                                                              capsys, rule):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RC.corpus, "import_distiller",
                        lambda s, d, r: {"imported": 0,
                                         "error": f"{rule}: {r} 不在允许根内"})
    args = _args(monkeypatch, ["--use-distiller", "--distiller-db", "d/app.sqlite3",
                              "--distiller-root", "dist"])

    with db.session() as s:
        before = _counts(s)
        rc = RC.run_imports(s, args)
        after = _counts(s)

    out = capsys.readouterr().out
    assert rc == RC.EXIT_IMPORT_REFUSED
    assert after == before, "守卫拒绝后不得有任何库写"
    assert RC.IMPORT_ROOTS_ENV in out and "--import-root" in out
