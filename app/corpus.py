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
import sqlite3
from pathlib import Path

from sqlalchemy.orm import Session

from . import config
from .metrics_det import _sentences  # 复用同一句切口径
from .models import Segment, Work

DEFAULTS = dict(min_chars=40, max_chars=300, target_sentences=4, max_sentences=10)


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
    """从任意绝对路径导入一本书（不经 inbox 目录拷贝）。"""
    fp = Path(path)
    if not fp.exists():
        return {"imported": 0, "error": f"文件不存在: {fp}"}
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
    segs = session.query(Segment).all()
    n_chars = [s.n_chars for s in segs]
    n_sents = [s.n_sentences for s in segs]
    return {
        "n_segments": len(segs),
        "n_works": session.query(Work).count(),
        "chars_total": sum(n_chars),
        "chars_mean": round(sum(n_chars) / max(len(n_chars), 1), 1),
        "sentences_mean": round(sum(n_sents) / max(len(n_sents), 1), 2),
    }
