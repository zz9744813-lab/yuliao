"""导入路径/体积守卫回归（审计 P1 2026-09-23 的可机械验收半）。

锁死 `corpus.import_file` / `corpus.import_distiller` 四条：
1. 非绝对路径 ⇒ `import_not_absolute`；
2. 允许根（LG_IMPORT_ROOTS）外的绝对路径 ⇒ `import_root_not_allowed`
   （只判断、不读文件内容）；
3. 根内但扩展名不在白名单 ⇒ `import_ext_not_allowed`；超体积上限 ⇒
   `import_too_large`（stat 判断，不整读进内存）；
4. **任一拒绝 ⇒ 零库写**（Work/Segment 计数前后不变）。

全部用 tmp_path + monkeypatch，不碰真实数据库/真实语料。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, corpus, db  # noqa: E402
from app.models import Segment, Work  # noqa: E402

# 够长的一句话段落：make_segments 默认 min_chars=40，保证成功用例能出段
SAMPLE_TEXT = (
    "这是守卫回归用的第一句样本文本，长度刻意超过最小阈值以便正常成段。"
    "第二句同样足够长，让切分器有活可干而不被并进相邻段落。"
    "第三句继续补足整体长度，覆盖段落边界与句末切分的常规路径。"
    "第四句收尾，本文件只用于测试导入行为，不进入任何正式语料。"
)


@pytest.fixture(autouse=True)
def _fresh_db():
    db.init_db()
    yield


@pytest.fixture
def import_root(tmp_path, monkeypatch):
    """把允许根指到 tmp_path（每用例独立、不碰真库真语料）。"""
    monkeypatch.delenv("LG_IMPORT_MAX_BYTES", raising=False)
    monkeypatch.setenv("LG_IMPORT_ROOTS", str(tmp_path))
    return tmp_path


def _counts(session):
    return session.query(Work).count(), session.query(Segment).count()


def _assert_zero_write(before, after):
    assert after == before, f"拒绝路径产生库写：before={before} after={after}"


# ── ① 绝对性 ─────────────────────────────────────────────────
def test_relative_path_rejected(import_root):
    fp = import_root / "样本.txt"
    fp.write_text(SAMPLE_TEXT, encoding="utf-8")
    rel = str(Path(config.ROOT.name) / fp.name)  # 形如 "lg-fix-import-guard/样本.txt"
    with db.session() as s:
        before = _counts(s)
        out = corpus.import_file(s, rel)
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_NOT_ABSOLUTE in out["error"]
        _assert_zero_write(before, _counts(s))


def test_empty_path_rejected(import_root):
    with db.session() as s:
        out = corpus.import_file(s, "   ")
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_NOT_ABSOLUTE in out["error"]


# ── ② 允许根 ─────────────────────────────────────────────────
def test_outside_root_rejected_without_reading(import_root):
    """允许根外的绝对路径 ⇒ 拒。win.ini 只判断存在性之外不做任何读取。"""
    outside = Path("C:/Windows/win.ini")
    with db.session() as s:
        before = _counts(s)
        out = corpus.import_file(s, str(outside))
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_ROOT in out["error"]
        _assert_zero_write(before, _counts(s))


def test_relative_root_entry_resolves_against_repo_root(monkeypatch, tmp_path):
    """LG_IMPORT_ROOTS 里的相对项以仓库根为基准 ⇒ tmp（仓库外）仍被拒。"""
    monkeypatch.setenv("LG_IMPORT_ROOTS", "data")
    outside = tmp_path / "x.txt"
    outside.write_text(SAMPLE_TEXT, encoding="utf-8")
    with db.session() as s:
        out = corpus.import_file(s, str(outside))
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_ROOT in out["error"]


def test_default_roots_are_data_dir_and_inbox(monkeypatch):
    """未设 LG_IMPORT_ROOTS 时默认根 = DATA_DIR 与仓库 inbox/（常量口径，不建目录）。"""
    monkeypatch.delenv("LG_IMPORT_ROOTS", raising=False)
    assert corpus._import_roots() == [config.DATA_DIR, config.ROOT / "inbox"]


# ── ③ 白名单扩展名 ───────────────────────────────────────────
@pytest.mark.parametrize("suffix", [".exe", ".db", ".sqlite3", ".py"])
def test_non_whitelisted_ext_rejected(import_root, suffix):
    fp = import_root / f"payload{suffix}"
    fp.write_bytes(b"MZ\x00\x01 not a text file at all")
    with db.session() as s:
        before = _counts(s)
        out = corpus.import_file(s, str(fp))
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_EXT in out["error"]
        _assert_zero_write(before, _counts(s))


def test_whitelisted_exts_constants():
    assert ".txt" in corpus.IMPORT_EXT_WHITELIST
    assert ".md" in corpus.IMPORT_EXT_WHITELIST
    assert ".text" in corpus.IMPORT_EXT_WHITELIST
    for bad in (".sqlite3", ".db", ".py", ".exe"):
        assert bad not in corpus.IMPORT_EXT_WHITELIST


# ── ④ 体积上限（stat 判断） ──────────────────────────────────
def test_oversize_rejected(import_root, monkeypatch):
    monkeypatch.setenv("LG_IMPORT_MAX_BYTES", "10")
    fp = import_root / "大样本.txt"
    fp.write_text(SAMPLE_TEXT, encoding="utf-8")  # 远超 10 字节
    with db.session() as s:
        before = _counts(s)
        out = corpus.import_file(s, str(fp))
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_SIZE in out["error"]
        _assert_zero_write(before, _counts(s))


def test_max_bytes_default_is_32mib(monkeypatch):
    monkeypatch.delenv("LG_IMPORT_MAX_BYTES", raising=False)
    assert corpus._import_max_bytes() == 32 * 1024 * 1024
    assert corpus.IMPORT_DEFAULT_MAX_BYTES == 32 * 1024 * 1024


# ── ⑤ 正常导入保持既有语义 ───────────────────────────────────
def test_import_success_inside_root(import_root):
    fp = import_root / "good.txt"
    fp.write_text(SAMPLE_TEXT, encoding="utf-8")
    with db.session() as s:
        out = corpus.import_file(s, str(fp), title="书名", author="作者", note="备注")
        assert out["imported"] == 1, out
        assert out["segments"] > 0
        assert out["source"] == f"file:{fp.resolve()}"
        w = s.get(Work, out["work_id"])
        assert w.title == "书名" and w.author == "作者" and w.note == "备注"
        # 去重键：同文件再导 ⇒ skipped、不新建 Work
        n_works = s.query(Work).count()
        again = corpus.import_file(s, str(fp))
        assert again == {"imported": 0, "skipped": 1, "source": out["source"]}
        assert s.query(Work).count() == n_works


def test_error_shape_passthrough_style(import_root):
    """拒绝返回与既有风格一致：HTTP 层原样透出，dict 含 imported=0 与 error 文本。"""
    with db.session() as s:
        out = corpus.import_file(s, str(import_root / "不存在.txt" ))
        # 根内但不存在：保持既有「文件不存在」文案（守卫之外、行为不变）
        assert out["imported"] == 0
        assert "文件不存在" in out["error"]


# ── ⑥ import_distiller 允许根校验 ────────────────────────────
def test_distiller_db_outside_root_rejected(import_root):
    outside_db = import_root.parent / "evil" / "app.sqlite3"
    outside_db.parent.mkdir(parents=True, exist_ok=True)
    outside_db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    with db.session() as s:
        before = _counts(s)
        out = corpus.import_distiller(s, str(outside_db), str(import_root))
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_ROOT in out["error"]
        _assert_zero_write(before, _counts(s))


def test_distiller_root_outside_root_rejected(import_root):
    outside_root = import_root.parent / "evil_root"
    outside_root.mkdir(parents=True, exist_ok=True)
    db_in = import_root / "d.sqlite3"
    db_in.write_bytes(b"SQLite format 3\x00")
    with db.session() as s:
        before = _counts(s)
        out = corpus.import_distiller(s, str(db_in), str(outside_root))
        assert out["imported"] == 0
        assert corpus.IMPORT_ERR_ROOT in out["error"]
        _assert_zero_write(before, _counts(s))


def test_distiller_builtin_defaults_are_whitelisted_exceptions():
    """历史默认路径（api.DistillerImport 默认值）是内置精确白名单例外：
    守卫放行这两个逐字路径（不实际执行导入，避免真语料入库）；
    同前缀下的其它路径仍受允许根约束。"""
    from app.api import DistillerImport
    model = DistillerImport()
    for p in (Path(model.db_path), Path(model.root)):
        guarded = corpus._guard_import_path(str(p), check_suffix=False,
                                            extra_exact=corpus._DISTILLER_BUILTIN_EXACT)
        assert isinstance(guarded, Path), f"内置默认路径被根守卫误拒: {p} -> {guarded}"
    other = str(Path(model.root) / "data" / "someone-else.sqlite3")
    guarded = corpus._guard_import_path(other, check_suffix=False,
                                        extra_exact=corpus._DISTILLER_BUILTIN_EXACT)
    assert isinstance(guarded, dict)
    assert corpus.IMPORT_ERR_ROOT in guarded["error"]
