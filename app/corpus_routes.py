"""语料导入与只读浏览接口。权限由 app.access 的全局中间件裁决。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from . import corpus, db
from .models import Segment, Work

router = APIRouter()

# 三个导入端点会扩产，只认管理档；全局 app.access 中间件负责鉴权。


@router.post("/corpus/import-inbox")
def import_inbox():
    with db.session() as s:
        return corpus.import_inbox(s)


class FileImport(BaseModel):
    # 形状校验在此；允许根、扩展名和大小守卫在 corpus.import_file。
    path: str = Field(min_length=1, max_length=1024)
    title: str | None = None
    author: str | None = None
    note: str | None = None


@router.post("/corpus/import-file")
def import_file(body: FileImport):
    with db.session() as s:
        return corpus.import_file(s, body.path, title=body.title,
                                  author=body.author, note=body.note)


class DistillerImport(BaseModel):
    # 历史默认值也是 corpus.import_distiller 的内置精确白名单例外。
    db_path: str = r"F:\agi\novel-distiller\data\app.sqlite3"
    root: str = r"F:\agi\novel-distiller"


@router.post("/corpus/import-distiller")
def import_distiller(body: DistillerImport):
    with db.session() as s:
        return corpus.import_distiller(s, body.db_path, body.root)


@router.get("/works")
def list_works():
    with db.session() as s:
        works = s.query(Work).order_by(Work.created_at).all()
        # work_id 索引上的一次聚合，避免每本书再发一次 count 查询。
        counts = dict(s.query(Segment.work_id, func.count())
                      .group_by(Segment.work_id).all())
        return [{"id": w.id, "title": w.title, "author": w.author,
                 "source": w.source, "note": w.note,
                 "segments": counts.get(w.id, 0), "created_at": w.created_at}
                for w in works]


_MAX_SEGMENT_LIMIT = 200
_MAX_SEGMENT_SCAN = 5000
# 列表仅给 80 字预览。翻页超过 5000 段必须按 work_id 收窄，
# 否则一个评审令牌就能无界枚举正文；越界明确 422，不静默截断。


@router.get("/segments")
def list_segments(work_id: str | None = None,
                  limit: int = Query(default=20, ge=1, le=_MAX_SEGMENT_LIMIT),
                  offset: int = Query(default=0, ge=0)):
    if offset + limit > _MAX_SEGMENT_SCAN:
        raise HTTPException(
            422, f"offset+limit 超过总量闸 ≤{_MAX_SEGMENT_SCAN}"
                 f"（收到 offset={offset}, limit={limit}）；请按 work_id 收窄翻页")
    with db.session() as s:
        q = s.query(Segment).order_by(Segment.id)
        if work_id:
            q = q.filter(Segment.work_id == work_id)
        rows = q.offset(offset).limit(limit).all()
        return [{"id": x.id, "work": x.work_id, "chars": x.n_chars,
                 "sents": x.n_sentences, "text": x.text[:80] + "…"}
                for x in rows]


@router.get("/corpus/stats")
def corpus_stats():
    with db.session() as s:
        return corpus.segment_stats(s)
