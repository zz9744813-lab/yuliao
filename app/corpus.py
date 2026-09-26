"""语料导入与 Segment 切分。

通道一：inbox 目录（.txt/.md 直接放入 data/corpus_inbox/ 后调用 import_inbox）
通道二：novel-distiller SQLite 只读适配（可选，fetch real blob text by sha）

切分规则（对应总方案 §6 Step1）：
- 1~max_sentences 句为一段，默认目标 3~6 句
- 只在句末切，绝不切句中
- 段落边界优先；过短末段并入前段
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config
from .metrics_det import _sentences  # 复用同一句切口径
from .models import Segment, Work

DEFAULTS = dict(min_chars=40, max_chars=300, target_sentences=4, max_sentences=10)

# ── 导入路径/体积守卫（审计 P1 2026-09-23 的可机械验收半）──────────
# 持令牌者曾可让服务端把**任意可读路径**整个读入建段（app/api.py 的
# import-file / import-distiller）。这里只做白名单半：
#   路径必须绝对、realpath 落在允许根内、扩展名在白名单、体积不超上限；
#   任一不满足 ⇒ 返回带规则名的错误 dict，且**零库写**（不进 add_work）。
# 权限分离与实验并发上限属设计拍板项，不在本次范围。
#
# 允许根来源：环境变量 LG_IMPORT_ROOTS（os.pathsep 分隔多个；相对路径以仓库根
# config.ROOT 为基准解析）。未设置时默认 = config.DATA_DIR 与 config.ROOT/"inbox"
# （只作校验基准，**不**创建目录）。
IMPORT_ROOTS_ENV = "LG_IMPORT_ROOTS"
IMPORT_MAX_BYTES_ENV = "LG_IMPORT_MAX_BYTES"
# 只覆盖 _read_text_loose 实际能读的纯文本类型；.sqlite3/.db/.py 等一律不进
IMPORT_EXT_WHITELIST = (".txt", ".md", ".text")
# 单文件上限，默认 32 MiB；用 stat().st_size 判断，不先整读进内存
IMPORT_DEFAULT_MAX_BYTES = 32 * 1024 * 1024
# 错误码（出现在返回的 error 字段前缀里，供调用方判别命中的规则）
IMPORT_ERR_NOT_ABSOLUTE = "import_not_absolute"
IMPORT_ERR_ROOT = "import_root_not_allowed"
IMPORT_ERR_EXT = "import_ext_not_allowed"
IMPORT_ERR_SIZE = "import_too_large"


def _default_import_roots() -> list[Path]:
    return [config.DATA_DIR, config.ROOT / "inbox"]


def _import_roots() -> list[Path]:
    """解析 LG_IMPORT_ROOTS（每次调用时读，测试可用 monkeypatch.setenv 覆盖）。"""
    raw = os.environ.get(IMPORT_ROOTS_ENV, "").strip()
    parts = [p for p in raw.split(os.pathsep) if p.strip()] if raw else []
    if not parts:
        return _default_import_roots()
    roots: list[Path] = []
    for part in parts:
        p = Path(part.strip())
        if not p.is_absolute():
            p = config.ROOT / p
        roots.append(p)
    return roots


def _import_max_bytes() -> int:
    raw = os.environ.get(IMPORT_MAX_BYTES_ENV, "").strip()
    try:
        v = int(raw)
    except ValueError:
        return IMPORT_DEFAULT_MAX_BYTES
    return v if v > 0 else IMPORT_DEFAULT_MAX_BYTES


def _norm(p: Path) -> str:
    """realpath 后归一化（Windows 大小写不敏感），用于前缀比较。"""
    return os.path.normcase(str(p.resolve()))


def _under(child_norm: str, parent_norm: str) -> bool:
    return child_norm == parent_norm or child_norm.startswith(parent_norm.rstrip("\\/") + os.sep)


def _under_import_roots(resolved_norm: str, extra_exact: tuple[str, ...] = ()) -> bool:
    """resolved 路径是否落在允许根内；extra_exact 为逐字精确匹配的内置例外。"""
    if resolved_norm in extra_exact:
        return True
    return any(_under(resolved_norm, _norm(r)) for r in _import_roots())


def _deny(rule: str, detail: str) -> dict:
    return {"imported": 0, "error": f"{rule}: {detail}"}


def _guard_import_path(path: str | Path, *, check_suffix: bool,
                       extra_exact: tuple[str, ...] = ()) -> dict | Path:
    """绝对性 + 允许根（+ 可选扩展名）校验。通过返回 resolved Path，否则返回错误 dict。"""
    p = Path(str(path).strip())
    if not p.is_absolute():
        return _deny(IMPORT_ERR_NOT_ABSOLUTE,
                     f"只接受绝对路径，收到: {str(path)!r}")
    rp = p.resolve()
    if not _under_import_roots(_norm(rp), extra_exact):
        roots_doc = os.pathsep.join(str(r) for r in _import_roots())
        return _deny(IMPORT_ERR_ROOT,
                     f"{rp} 不在允许根内（{IMPORT_ROOTS_ENV}={roots_doc!r}）")
    if check_suffix and rp.suffix.lower() not in IMPORT_EXT_WHITELIST:
        return _deny(IMPORT_ERR_EXT,
                     f"扩展名 {rp.suffix or '(无)'} 不在白名单 {IMPORT_EXT_WHITELIST}")
    return rp


# novel-distiller 的历史默认路径（api.DistillerImport 默认值即此）：既有调用方
# 依赖，无法表达进 LG_IMPORT_ROOTS（它在仓库外），故作为**内置精确白名单**例外，
# 只放行这两个逐字路径本身；换任何其它 distiller 路径都须落进允许根。
_DISTILLER_BUILTIN_DB = r"F:\agi\novel-distiller\data\app.sqlite3"
_DISTILLER_BUILTIN_ROOT = r"F:\agi\novel-distiller"
_DISTILLER_BUILTIN_EXACT = (
    _norm(Path(_DISTILLER_BUILTIN_DB)),
    _norm(Path(_DISTILLER_BUILTIN_ROOT)),
)


def _read_text_loose(fp: Path) -> str:
    """中文网文常见 utf-8(BOM)/gbk；utf-8-sig 优先，gb18030 兜底，最后 ignore。"""
    raw = fp.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def make_segments(text: str, **kw) -> list[str]:
    cfg = {**DEFAULTS, **kw}
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    out: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf.clear()

    for para in paragraphs:
        for sent in _sentences(para):
            buf.append(sent)
            cur = "".join(buf)
            over_chars = len(cur) >= cfg["max_chars"]
            over_sents = len(buf) >= cfg["max_sentences"]
            hit_target = len(buf) >= cfg["target_sentences"] and len(cur) >= cfg["min_chars"]
            if over_chars or over_sents or hit_target:
                flush()
        # 段末自然断点：若缓冲已达最小长度则收，避免跨段落义粘连
        if buf and len("".join(buf)) >= cfg["min_chars"]:
            flush()
    if buf:
        tail = "".join(buf).strip()
        # 尾部过短并入上一段
        if tail and out and len(tail) < cfg["min_chars"]:
            out[-1] = out[-1] + tail
        elif tail:
            out.append(tail)
    return out


def add_work(session: Session, *, title: str, text: str, source: str,
             author: str | None = None, chapter: str | None = None,
             note: str | None = None) -> Work:
    work = Work(title=title, author=author, source=source, note=note)
    session.add(work)
    session.flush()
    segs = make_segments(text)
    for i, s in enumerate(segs):
        words = _sentences(s)
        session.add(Segment(
            work_id=work.id, chapter=chapter, ordinal=i, text=s,
            n_sentences=len(words), n_chars=len(s),
        ))
    # autoflush=False：不再来一次 flush，同事务内的后续查询（如紧跟着建实验采样）会看不到段落
    session.flush()
    # 入库闸门（T-CORPUS-V2）：扫错字表并记入 work.note——**只记录不改动**，
    # 修复走 corpus_fix_v2.py 的版本化流程（不覆盖已入库文本）。
    from .typo_map import hits as _typo_hits
    _th = _typo_hits(text)
    if _th:
        _detail = "、".join(f"{k}×{v}" for k, v in sorted(_th.items()))
        work.note = ((work.note + "；") if work.note else "") +             f"[typo_scan] {_detail}（见 app/typo_map，修复走 corpus_fix_v2）"
        session.flush()
    return work


def import_inbox(session: Session, inbox: Path | None = None) -> dict:
    inbox = inbox or (config.DATA_DIR / "corpus_inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    done = {w.source for w in session.query(Work.source).all()}
    imported, skipped = 0, 0
    for fp in sorted(inbox.glob("**/*")):
        if fp.suffix.lower() not in (".txt", ".md"):
            continue
        key = f"inbox:{fp.relative_to(inbox).as_posix()}"
        if key in done:
            skipped += 1
            continue
        text = _read_text_loose(fp)
        add_work(session, title=fp.stem, text=text, source=key)
        imported += 1
    session.commit()
    return {"imported": imported, "skipped": skipped}


def import_file(session: Session, path: str, *, title: str | None = None,
                author: str | None = None, note: str | None = None) -> dict:
    """从绝对路径导入一本书（不经 inbox 目录拷贝）。

    审计 P1（2026-09-23）：路径/体积守卫见 `_guard_import_path` 与
    `IMPORT_*` 常量——不在允许根 / 扩展名不在白名单 / 超过
    `LG_IMPORT_MAX_BYTES`（stat 判断，不整读进内存）一律拒绝且零库写。
    """
    guarded = _guard_import_path(path, check_suffix=True)
    if isinstance(guarded, dict):
        return guarded  # 被拒：尚未触碰 session ⇒ 零库写
    fp = guarded
    if not fp.is_file():
        return {"imported": 0, "error": f"文件不存在: {fp}"}
    size = fp.stat().st_size
    limit = _import_max_bytes()
    if size > limit:
        return _deny(IMPORT_ERR_SIZE,
                     f"{fp} 共 {size} 字节，超过上限 {limit}（{IMPORT_MAX_BYTES_ENV}）")
    key = f"file:{fp}"
    if session.query(Work).filter_by(source=key).first():
        return {"imported": 0, "skipped": 1, "source": key}
    text = _read_text_loose(fp)
    work = add_work(session, title=title or fp.stem, text=text, source=key,
                    author=author, note=note)
    session.commit()
    n = session.query(Segment).filter_by(work_id=work.id).count()
    return {"imported": 1, "work_id": work.id, "segments": n, "source": key}


# ── novel-distiller 只读适配（可选通道；失败返回空并说明） ──

def _blob_path(distiller_root: Path, sha: str) -> Path | None:
    cand = [
        distiller_root / "data" / "blobs" / sha[:2] / sha,
        distiller_root / "data" / "blobs" / sha,
    ]
    for p in cand:
        if p.exists():
            return p
    return None


def import_distiller(session: Session, distiller_db: str, distiller_root: str) -> dict:
    """novel-distiller 只读导入。审计 P1：db_path 与 root 同样受允许根校验
    （越界 ⇒ import_root_not_allowed 且零库写）；历史默认路径是内置精确白名单
    例外，见 `_DISTILLER_BUILTIN_EXACT` 的注释。
    """
    for p in (distiller_db, distiller_root):
        guarded = _guard_import_path(p, check_suffix=False,
                                     extra_exact=_DISTILLER_BUILTIN_EXACT)
        if isinstance(guarded, dict):
            return guarded  # 被拒：尚未触碰 session ⇒ 零库写
    db = Path(distiller_db)
    root = Path(distiller_root)
    if not db.exists():
        return {"imported": 0, "error": f"distiller db 不存在: {db}"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT b.title, c.title, c.start_cp, c.end_cp, sr.normalized_blob_sha
        FROM chapters c
        JOIN source_revisions sr ON sr.id = c.source_revision_id
        JOIN books b ON b.id = sr.book_id
        ORDER BY b.title, c.ordinal
        """
    ).fetchall()
    done = {w.source for w in session.query(Work.source).all()}
    imported = 0
    for book_title, ch_title, s_cp, e_cp, sha in rows:
        key = f"distiller:{book_title}:{ch_title}"
        if key in done or not sha:
            continue
        bp = _blob_path(root, sha)
        if not bp:
            continue
        full = _read_text_loose(bp)
        text = full[s_cp:e_cp] if s_cp is not None and e_cp else full
        if len(text.strip()) < 80:
            continue
        add_work(session, title=f"{book_title}::{ch_title}", text=text,
                 source=key, note="distiller 只读导入；验收测试书不保证 Human Anchor 质量")
        imported += 1
    con.close()
    session.commit()
    return {"imported": imported}


def segment_stats(session: Session) -> dict:
    # /corpus/stats 是页面首屏请求。千万段真库不能把完整 ORM 行全部
    # 实例化到进程；让数据库只返回一个聚合行。
    n_segments, chars_total, sentences_total = session.query(
        func.count(Segment.id),
        func.coalesce(func.sum(Segment.n_chars), 0),
        func.coalesce(func.sum(Segment.n_sentences), 0),
    ).one()
    return {
        "n_segments": n_segments,
        "n_works": session.query(Work).count(),
        "chars_total": int(chars_total),
        "chars_mean": round(chars_total / max(n_segments, 1), 1),
        "sentences_mean": round(sentences_total / max(n_segments, 1), 2),
    }
