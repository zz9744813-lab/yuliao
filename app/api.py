"""FastAPI 薄层 + 最小 Web 控制台。

启动：uvicorn app.main:app --reload --port 8787
控制台：http://127.0.0.1:8787/  （无鉴权，仅本机使用）

主要端点：
  POST /corpus/import-inbox | import-distiller | import-file
  GET  /works /segments /corpus/stats
  POST /experiments            (body = experiment config override)
  POST /experiments/{id}/run   (后台线程跑实验引擎的阶段状态机；body 可带 {"stages":[...]} 子集)
  GET  /experiments /experiments/{id} /experiments/{id}/stages /experiments/{id}/report(.json)
                               （/stages = 引擎各阶段状态，任务 12）
  GET  /experiments/{id}/review          队列（含优先级理由）
  GET  /experiments/{id}/review/next     盲评取题：匿名 A/B，服务端暗记映射
  GET  /experiments/{id}/review/{rid}/serve  改判入口：重端已判题（A/B 重洗 + 回填原判）
  POST /review/{id}/verdict    {"winner":"A|B|tie|both_bad|cant_judge","reasons":[...]}
                               （已判题可覆盖改判；响应附本题信号标签，判定后才可展示）
  GET  /review/batch/{b} | /review/batch/{b}/done   批次概览 / 已判清单
  GET  /llm/stats
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import access, config, console, corpus, db, engine, experiments, observability
from .models import Candidate, Experiment, Job, ReviewItem, Segment, Work, LlmCall

app = FastAPI(title="Language Genome — SemanticFrame Calibration Lab", version="0.2.0")
logger = logging.getLogger(__name__)

_STATIC = Path(__file__).resolve().parent / "static"
# 研究台（第二界面正式版，websrc/）：16 模块只读页。与 _STATIC 分离是为了让
# 前端源码留在仓库根、不混进应用包；挂载点见下方 /lab 路由。
_WEBSRC = Path(__file__).resolve().parent.parent / "websrc"

# 远程访问门：本机直连免鉴权；经隧道/代理的请求需令牌（见 app/access.py）。
# 未配置令牌时不做任何事，本机使用行为完全不变。
access.install(app)


@app.on_event("startup")
def _init():
    db.init_db()


@app.get("/", include_in_schema=False)
def index():
    # no-store：控制台是本机迭代工具，改完 index.html 必须立刻可见，不能被浏览器缓存住旧版。
    return FileResponse(_STATIC / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# 研究台共享件（tokens.css / base.css / lg.js）。
# 用 StaticFiles 而非 FileResponse：靠 Last-Modified/ETag 自动失效，改完即生效，
# 不需要像 HTML 页那样打 no-store。目录缺失时不挂载，避免静默启动失败。
if (_WEBSRC / "_shared").is_dir():
    app.mount("/lab/_shared", StaticFiles(directory=_WEBSRC / "_shared"), name="lab-shared")


# ── 语料 ────────────────────────────────────────────────────

@app.post("/corpus/import-inbox")
def import_inbox():
    with db.session() as s:
        return corpus.import_inbox(s)


class FileImport(BaseModel):
    path: str
    title: str | None = None
    author: str | None = None
    note: str | None = None


@app.post("/corpus/import-file")
def import_file(body: FileImport):
    with db.session() as s:
        return corpus.import_file(s, body.path, title=body.title,
                                  author=body.author, note=body.note)


class DistillerImport(BaseModel):
    db_path: str = r"F:\agi\novel-distiller\data\app.sqlite3"
    root: str = r"F:\agi\novel-distiller"


@app.post("/corpus/import-distiller")
def import_distiller(body: DistillerImport):
    with db.session() as s:
        return corpus.import_distiller(s, body.db_path, body.root)


@app.get("/works")
def list_works():
    with db.session() as s:
        works = s.query(Work).order_by(Work.created_at).all()
        out = []
        for w in works:
            n = s.query(Segment).filter_by(work_id=w.id).count()
            out.append({"id": w.id, "title": w.title, "author": w.author,
                        "source": w.source, "note": w.note,
                        "segments": n, "created_at": w.created_at})
        return out


@app.get("/segments")
def list_segments(work_id: str | None = None, limit: int = 20, offset: int = 0):
    with db.session() as s:
        q = s.query(Segment).order_by(Segment.id)
        if work_id:
            q = q.filter(Segment.work_id == work_id)
        rows = q.offset(offset).limit(limit).all()
        return [{"id": x.id, "work": x.work_id, "chars": x.n_chars, "sents": x.n_sentences,
                 "text": x.text[:80] + "…"} for x in rows]


@app.get("/corpus/stats")
def corpus_stats():
    with db.session() as s:
        return corpus.segment_stats(s)


# ── 实验 ────────────────────────────────────────────────────

class ExperimentIn(BaseModel):
    n_segments: int | None = None
    granularities: list[str] | None = None
    recon_models: list[str] | None = None
    judge_models: list[str] | None = None
    temperatures: list[float] | None = None
    samples_per_pair: int | None = None
    adversarial_k: int | None = None
    concurrency: int | None = None
    work_ids: list[str] | None = None


@app.post("/experiments")
def create_exp(body: ExperimentIn):
    with db.session() as s:
        overrides = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            exp = experiments.create_experiment(s, overrides)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": exp.id, "config": {k: v for k, v in exp.config.items()
                                         if k != "segment_ids"}}


class RunIn(BaseModel):
    stages: list[str] | None = None   # 引擎阶段子集（如 ["plan","extract"]）；None=全部


@app.post("/experiments/{exp_id}/run")
def run_exp(exp_id: str, body: RunIn | None = None):
    # 2026-09-18 接实验引擎（任务 12）：后台线程跑阶段状态机
    # plan → source_check → extract → reconstruct → residual → judge → report。
    # 响应 {"status","id"} 与旧管线完全兼容（只加 stages 字段），前端不用改。
    stages = body.stages if body else None
    with db.session() as s:
        e = s.get(Experiment, exp_id)
        if not e:
            raise HTTPException(404, "experiment 不存在")
        if e.status == "running":
            return {"status": "already_running", "id": exp_id}
    engine.run_experiment_background(exp_id, stages)
    return {"status": "started", "id": exp_id, "stages": engine.ENGINE_STAGES}


@app.get("/experiments/{exp_id}/stages")
def exp_stages(exp_id: str):
    """实验引擎各阶段状态（任务 12）：没跑过的阶段记 pending，跑过的带计数与 token。"""
    with db.session() as s:
        e = s.get(Experiment, exp_id)
        if not e:
            raise HTTPException(404, "not found")
        eng = (e.stats or {}).get("engine") or {}
        return {"id": e.id, "status": e.status, "error": e.error,
                "stage_order": engine.ENGINE_STAGES,
                "current_stage": eng.get("current_stage"),
                "stages": engine.stage_states(e)}


@app.get("/experiments")
def list_experiments():
    with db.session() as s:
        rows = s.query(Experiment).order_by(Experiment.created_at.desc()).limit(50).all()
        out = []
        for e in rows:
            pend = s.query(ReviewItem).filter_by(experiment_id=e.id, status="pending").count()
            out.append({"id": e.id, "status": e.status, "created_at": e.created_at,
                        "review_pending": pend,
                        "n_segments": len((e.config or {}).get("segment_ids") or []),
                        "granularities": (e.config or {}).get("granularities"),
                        "current_stage": (e.stats or {}).get("current_stage")})
        return out


@app.get("/experiments/{exp_id}")
def get_exp(exp_id: str):
    with db.session() as s:
        e = s.get(Experiment, exp_id)
        if not e:
            raise HTTPException(404, "not found")
        jobs = s.query(Job).filter(Job.kind.like("stage:%")).all()
        mine = [j for j in jobs if (j.payload or {}).get("experiment_id") == exp_id]
        return {
            "id": e.id, "status": e.status, "error": e.error,
            "stats": e.stats,
            "config": {k: v for k, v in e.config.items() if k != "segment_ids"},
            "job_states": {j.kind: {"status": j.status, "attempts": j.attempts,
                                    "last_error": j.last_error} for j in mine},
        }


@app.get("/experiments/{exp_id}/report", response_class=PlainTextResponse)
def get_report(exp_id: str):
    from .models import ReportFile
    with db.session() as s:
        rf = s.query(ReportFile).filter_by(experiment_id=exp_id, kind="calibration_md")\
            .order_by(ReportFile.created_at.desc()).first()
        if not rf:
            raise HTTPException(404, "报告还没生成（跑完 report stage 后再看）")
        return Path(rf.path).read_text(encoding="utf-8")


@app.get("/experiments/{exp_id}/report.json")
def get_report_json(exp_id: str):
    from .models import ReportFile
    with db.session() as s:
        rf = s.query(ReportFile).filter_by(experiment_id=exp_id, kind="calibration_json")\
            .order_by(ReportFile.created_at.desc()).first()
        if not rf:
            raise HTTPException(404, "报告还没生成")
        import json as _json
        return _json.loads(Path(rf.path).read_text(encoding="utf-8"))


# ── 人评（盲评 A/B）──────────────────────────────────────────

# 盲评映射：presentation_id → {"review_id", "human_first", "ctx_mode", ...}。
# **呈现不可变**（审计 A01）：每次出题生成新的 presentation_id，提交必须带上它。
# 老实现只按 review_id 存一份全局映射 —— 两个页面打开同一题、或旧页面还在时重新出题，
# 旧页面的选择会按**新**映射解释，污染最贵的用户偏好标签（审计已复现）。
# 进程内存 + **落盘**：单 worker 下重启仍能解析；多 worker 下**按条目合并写**（不做整表覆盖），
# 但仍是各进程独立内存 + 非事务合并 —— 注释不夸大承诺（会审 glm 席）。过期或未知 pid 一律拒绝，不再猜。
_BLIND_CAP = 512
_BLIND_LAST_CAP = 512    # 索引上限**不得大于** _BLIND_CAP：否则留下"rid→已逐出 pid"的悬挂条目
                          # （会审 qwen 席：那种悬挂条目过去会走进 legacy 且照常入库）
_BLIND_MAP: dict[str, dict] = {}       # presentation_id → entry
_BLIND_LAST: dict[str, str] = {}       # review_id → 最近一次 presentation_id（老前端不传 pid 时兜底）
_BLIND_LOCK = threading.Lock()
_BLIND_LOADED_FOR: str | None = None   # 已装载的呈现文件路径（懒加载哨兵，见 _blind_ensure_loaded）
_SERVE_CURSOR: dict[str, int] = {}   # 批次轮换游标（进程内缓存；真值落盘，见下）

# 游标**落盘**：2026-09-17 集霸反馈"一堆题在那轮换来乱换去"。
# 根因是游标只在进程内存里 —— 当天重启 4 次（改代码/改 UI），每次归零，
# 队列就从第一批待判题重新开始，于是他反复看到同一批题。
# 落盘后重启不再回退；想从头再来删掉这个文件即可。
# 持久状态**每次读写时**从 config.DATA_DIR 派生（审计 A04）。不要固化在模块导入那一刻：
# 脚本/测试里"先 import app.api 再改 DATA_DIR"很常见，固化后会照旧写到**真实**游标文件上 ——
# 现场文件里已经留下 c41/rj1..rj8 等测试批次键，就是这么来的（会审两席同指）。
def _data_file(name: str) -> Path:
    return Path(config.DATA_DIR) / name


def _cursor_file() -> Path:
    return _data_file("serve_cursor.json")


def _present_file() -> Path:
    return _data_file("blind_presentations.json")


def _fp16(text: str) -> str:
    """文本指纹（sha256 截断 16 位）：冻结"这一次端出的是哪两个文本"。

    带长度前缀：否则空串与""同值、退化情况下两侧指纹相同，证伪逻辑就失去区分度（会审 glm 席）。
    """
    import hashlib
    t = text or ""
    return hashlib.sha256(f"{len(t)}|{t}".encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    """本文件惯用函数内导入（顶层没有 datetime 名），所以包一层，别再踩 NameError。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict) -> None:
    """临时文件 + os.replace 原子替换：进程中途被杀不会留半截 JSON（会审两席）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名带 uuid：只带 pid 时，同进程两个线程并发保存会写同一个 tmp（会审 glm 席）
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()      # 别在 data/ 里留半个临时文件（会审 qwen 席）
        except Exception:
            pass
        raise


def _blind_load() -> None:
    """启动时把落盘的呈现映射读回内存（重启后旧页面仍可安全提交）。

    文件缺失 = 正常（首次运行）；文件**损坏** = 备份为 .corrupt 并告警 ——
    不让"静默加载成空映射"把问题藏起来（会审两席）。
    """
    path = _present_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning("盲评呈现文件读不出（%s）：%s", path, e)
        return
    try:
        data = json.loads(raw)
    except Exception as e:
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            path.replace(backup)
        except Exception:
            pass
        logger.warning("盲评呈现文件损坏（%s）：%s；已备份到 %s，本次从空映射开始", path, e, backup)
        return
    with _BLIND_LOCK:
        for pid, entry in (data.get("presentations") or {}).items():
            _BLIND_MAP[pid] = entry
        for rid, pid in (data.get("last") or {}).items():
            _BLIND_LAST[rid] = pid
        while len(_BLIND_MAP) > _BLIND_CAP:
            _BLIND_MAP.pop(next(iter(_BLIND_MAP)))
        while len(_BLIND_LAST) > _BLIND_LAST_CAP:
            _BLIND_LAST.pop(next(iter(_BLIND_LAST)))


def _load_cursor(key: str) -> int:
    if key in _SERVE_CURSOR:
        return _SERVE_CURSOR[key]
    try:
        data = json.loads(_cursor_file().read_text(encoding="utf-8"))
        cur = int(data.get(key, 0))
    except FileNotFoundError:
        cur = 0
    except Exception as e:
        logger.warning("游标文件读不出（%s）：%s，本次从 0 开始", key, e)
        cur = 0
    _SERVE_CURSOR[key] = cur
    return cur


def _save_cursor(key: str, value: int) -> None:
    _SERVE_CURSOR[key] = value
    try:
        path = _cursor_file()
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data[key] = value
        _atomic_write_json(path, data)
    except Exception as e:
        # 落盘失败不影响出题（只是重启后会回退）；但**不再静默**（会审：同一函数族两种失败口径）
        logger.warning("游标落盘失败（%s）：%s", key, e)


def _blind_put(presentation_id: str, entry: dict) -> None:
    _blind_ensure_loaded()
    with _BLIND_LOCK:
        _BLIND_MAP[presentation_id] = entry
        _BLIND_LAST[entry["review_id"]] = presentation_id
        while len(_BLIND_MAP) > _BLIND_CAP:
            _BLIND_MAP.pop(next(iter(_BLIND_MAP)))
        while len(_BLIND_LAST) > _BLIND_LAST_CAP:
            _BLIND_LAST.pop(next(iter(_BLIND_LAST)))
        _blind_save_locked()


def _blind_save_locked() -> None:
    """落盘（调用方须持锁）。**先读盘并入内存再写**：多 worker 下若整表覆盖，
    后写的进程会把先写进程的呈现悄悄抹掉（会审 glm 席：last-write-wins 静默丢数据）。
    合并后仍有逐出，所以两块都按上限裁。失败只告警 —— 后果是重启后旧页面不能再提交。
    """
    try:
        path = _present_file()
        merged_p: dict[str, dict] = {}
        merged_l: dict[str, str] = {}
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
            merged_p = dict(disk.get("presentations") or {})
            merged_l = dict(disk.get("last") or {})
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("盲评呈现文件读不出（%s）：%s；本次直接覆盖写", path, e)
        merged_p.update(_BLIND_MAP)
        merged_l.update(_BLIND_LAST)
        # 逐出按 created_at 从旧到新：只按插入序会把 disk 上先到的条目排在队首，
        # 于是"刚端出、还没提交"的呈现可能被清掉（用户随即 409）——会审 glm 席。
        while len(merged_p) > _BLIND_CAP:
            oldest = min(merged_p, key=lambda k: (merged_p[k] or {}).get("created_at") or "")
            merged_p.pop(oldest, None)
        while len(merged_l) > _BLIND_LAST_CAP:
            merged_l.pop(next(iter(merged_l)))
        _BLIND_MAP.clear(); _BLIND_MAP.update(merged_p)
        _BLIND_LAST.clear(); _BLIND_LAST.update(merged_l)
        _atomic_write_json(path, {"presentations": merged_p, "last": merged_l})
    except Exception as e:
        logger.warning("盲评呈现落盘失败：%s（重启后旧页面将无法提交）", e)


def _blind_ensure_loaded() -> None:
    """首次使用时懒加载（**不是** import 期一次性读）。

    会审两席：读写路径已改成每次从 config.DATA_DIR 派生，若装载仍固化在导入那一刻，
    "先 import 再改 DATA_DIR"就变成"写新目录、内存里却还是旧目录的映射"——
    方向相反的同类污染（把真实环境的呈现带进测试进程）。目录变了就整块换一套。
    """
    global _BLIND_LOADED_FOR
    path = str(_present_file())
    if _BLIND_LOADED_FOR == path:
        return
    with _BLIND_LOCK:
        if _BLIND_LOADED_FOR == path:
            return
        _BLIND_MAP.clear()
        _BLIND_LAST.clear()
    _blind_load()                      # 自己取锁，别在持锁时调用（Lock 不可重入）
    with _BLIND_LOCK:
        _BLIND_LOADED_FOR = path


def _blind_get(presentation_id: str) -> dict | None:
    """按**呈现**取映射：拿不到就是过期/未知，调用方必须拒绝，不许回退猜测。"""
    _blind_ensure_loaded()
    with _BLIND_LOCK:
        return _BLIND_MAP.get(presentation_id)


def _blind_latest(review_id: str) -> tuple[str | None, dict | None]:
    """**单次持锁**取出「最近一次呈现」的 pid 与映射。

    会审 glm 席：分两次读（先 pid 再映射）中间若另一请求重端同题，
    落库的 presentation_id 与真正用于解释的映射就不是同一份，事后甄别会被误导。
    """
    _blind_ensure_loaded()
    with _BLIND_LOCK:
        pid = _BLIND_LAST.get(review_id)
        return (pid, _BLIND_MAP.get(pid)) if pid else (None, None)


def _blind_latest_pid(review_id: str) -> str | None:
    return _blind_latest(review_id)[0]


def _blind_for_review(review_id: str) -> dict | None:
    """兼容老入口：取该题**最近一次**呈现的**映射**（不带 pid 的提交走它，结果标 binding=legacy_review_id）。

    这是**过渡**路径，不是修复：老客户端永远不带 pid 就永远绕过 A01。
    弃用时间表：前端 index.html 已在取题/重端/会话内改判三处全部带 pid，
    新前端上线后（评审台无 pid 提交应趋近于 0）即可把这条兜底改为直接 409（会审 glm 席意见）。
    """
    return _blind_latest(review_id)[1]


# 装载改为**首次使用时懒加载**（_blind_ensure_loaded）：见该函数 docstring 的会审依据。


@app.get("/review/batch/{batch}")
def batch_info(batch: str):
    """批次概览：该 batch 落在哪个实验、共几题、已判几题。供前端定位默认批次。"""
    with db.session() as s:
        tag = f"batch_{batch}"
        hit = [r for r in s.query(ReviewItem).all() if tag in (r.reasons or [])]
        if not hit:
            raise HTTPException(404, f"批次 {tag} 不存在")
        exps = sorted({r.experiment_id for r in hit})
        done = sum(1 for r in hit if r.status == "done")
        return {"batch": tag, "experiment_id": exps[0], "experiments": exps,
                "total": len(hit), "done": done, "pending": len(hit) - done}


@app.get("/review/batch/{batch}/done")
def batch_done(batch: str, limit: int = 300):
    """本批已判清单（前端的「已判回顾 / 改判」入口）：只出轻量字段，不含文本。"""
    with db.session() as s:
        tag = f"batch_{batch}"
        rows = [r for r in s.query(ReviewItem).filter_by(status="done").all()
                if tag in (r.reasons or [])]
        if not rows:
            raise HTTPException(404, f"批次 {tag} 还没有已判题")
        rows.sort(key=lambda r: r.reviewed_at or "", reverse=True)
        items = []
        for r in rows[:limit]:
            hv = r.human_verdict or {}
            items.append({
                "id": r.id, "experiment_id": r.experiment_id,
                "winner": hv.get("winner_resolved"),
                "n_annotations": hv.get("n_annotations") or 0,
                "rejudged": bool(hv.get("rejudged")),
                "reviewed_at": r.reviewed_at,
            })
        return {"batch": tag, "experiments": sorted({r.experiment_id for r in rows}),
                "done": len(rows), "items": items}


@app.get("/experiments/{exp_id}/review")
def review_queue(exp_id: str, limit: int = 50, status: str = "pending"):
    with db.session() as s:
        rows = s.query(ReviewItem).filter_by(experiment_id=exp_id, status=status)\
            .order_by(ReviewItem.priority.desc()).limit(limit).all()
        return [{"id": r.id, "subject_type": r.subject_type,
                 "subject_id": r.subject_id, "priority": r.priority,
                 "reasons": r.reasons} for r in rows]


def _display_text(seg) -> str:
    """给评审台看的正文：清洗版优先（见 scripts/clean_text.py）。

    ⚠ A/B 两侧与 `_side_texts`（批注偏移校验）**必须用同一个函数**：
    给集霸看清洗版、却拿原文校验批注偏移，会让所有划词批注的偏移对不上。
    """
    return (getattr(seg, "text_clean", None) or seg.text or "")


def _ctx_display(texts: list[str]) -> list[str]:
    """上文里**丢掉仍是坏文本的段**（清洗后还带拼音粘连的）。

    宁可少给一段上文，也不要让评审人对着 `白sè雾气…(手打中文网7*24小时不间断更新…)`
    判文笔——集霸 2026-09-18 原话就是这个问题。A/B 正文不受影响（另有清洗路径）。
    """
    try:
        from scripts.clean_text import looks_broken
    except Exception:                     # 脚本目录不在 path 时退化为不过滤
        return texts
    keep = [t for t in texts if t and not looks_broken(t)]
    return keep


def _side_texts(s, r, human_first) -> dict:
    """重建该题端出时的 A/B 两侧文本（批注偏移校验用）。映射未知时返回空。"""
    if human_first is None or r.subject_type != "candidate":
        return {}
    cand = s.get(Candidate, r.subject_id)
    if not cand or not cand.text:
        return {}
    human = s.get(Segment, cand.segment_id)
    if not human:
        return {}
    h = _display_text(human)
    return {"A": h, "B": cand.text} if human_first else {"A": cand.text, "B": h}


def _serve_payload(s, r, ctx_scope: str = "near") -> dict:
    """把一条 ReviewItem 端成盲评题（匿名 A/B + 上文），并登记 A/B 映射。

    pending / done 都可端（done = 改判重端，A/B 重新随机）。
    返回里**绝不带 reasons / priority**：那些信号（human_upset、分数、层位）会
    在判定前锚定评审人——2026-09-16 审查发现旧前端把 score/stratum 标签直接
    显示在判定按钮上方。信号改由 verdict 响应在判定**后**返回，供事后展示。

    ctx_scope（2026-09-17 加）：`near` = 只给最近一段（默认）；`scene` = 回溯到场景起点。
    起因：集霸反馈"太折磨了"。实测单题要读 ≈4100 字，其中**上文占 3970 字（97%）**，
    而 A/B 两段只有 137 字——阅读成本几乎全在上文。
    两个 scope 都会下发（`context` 按 scope 给，`context_full` 恒为全场景），
    前端可让用户按需展开；但**判定与评委口径以 `ctx_mode` 记录的那个为准**
    （§6⑥：评委与用户必须吃同一份上下文）。
    """
    from .context_ablation import scene_context
    cand = s.get(Candidate, r.subject_id) if r.subject_type == "candidate" else None
    if not cand or not cand.text:
        raise HTTPException(400, f"评审对象异常: {r.subject_id}")
    human = s.get(Segment, cand.segment_id)
    if not human:
        raise HTTPException(400, "human segment 缺失")
    # 与 Judge 侧共用同一实现（context_ablation.scene_context），
    # 保证"用户带什么上下文判，Judge 就带什么上下文判"。
    ctx_texts, ctx_mode = scene_context(s, human)
    # 清洗后的文本优先（拼音还原 / 站点水印剔除，见 scripts/clean_text.py）。
    # 起因（集霸 2026-09-18）：「先把原始文本的那些拼音广告啥的搞一下不然评个屁啊」
    # ——伪影主要出现在**上文**里（实测 corr24 有 4/24 题的上文带 `白sè`、`(手打中文网…)`）。
    ctx_texts = _ctx_display(ctx_texts)
    full = (chr(10) * 2).join(ctx_texts)
    near = (chr(10) * 2).join(ctx_texts[-1:]) if ctx_texts else ""
    if ctx_scope == "scene":
        context, mode = full, ctx_mode
    else:
        context, mode = near, f"near1/{ctx_mode}"
    human_first = random.random() < 0.5
    htext = _display_text(human)
    a, b = (htext, cand.text) if human_first else (cand.text, htext)
    # 呈现不可变（审计 A01）：本次出题的排列、双文本哈希、上下文口径一并冻结在 presentation_id 下。
    presentation_id = uuid.uuid4().hex
    ah, bh = _fp16(a), _fp16(b)
    _blind_put(presentation_id, {"review_id": r.id, "human_first": human_first, "ctx_mode": mode,
                                 "ctx_scope": ctx_scope, "a_hash": ah, "b_hash": bh,
                                 "created_at": _now_iso()})
    return {"review_id": r.id, "presentation_id": presentation_id,
            "context": context, "context_full": full, "ctx_mode": mode,
            "n_ctx": len(ctx_texts), "ctx_scope": ctx_scope,
            "a_hash": ah, "b_hash": bh,     # 页面照抄回传 → 服务端可证伪"旧页面按新映射猜"
            "text_a": a, "text_b": b,
            "note": "A/B 已匿名打乱。上文只帮你进入场景——判语感时若上下文天然接不上（v1 旧切段），只比对 A/B 本身的行文即可"}


def _servable_cond():
    """端出白名单的 SQL 条件：白名单**或** `corrupt_*` 整族。

    为什么不直接 `in_(SERVABLE_PROMPT_VERSIONS)`：生成口径升版时（corrupt_v1→v2）
    白名单不同步，取题接口就会把批次里**明明还在待判**的题当成不存在 ——
    集霸 2026-09-18 判到 12/24 时遇到的就是这个："好像评完了？"
    """
    from sqlalchemy import or_
    return or_(Candidate.prompt_version.in_(config.SERVABLE_PROMPT_VERSIONS),
               Candidate.prompt_version.like("corrupt\_%", escape="\\"))


def _pick_next(s, batch: str | None, exp_id: str | None,
               ctx_scope: str = "near") -> dict:
    """取下一道待评题并端成 A/B。exp_id=None = 不限实验（跨语料混合批用）。

    轮换游标走**固定的整批列表**（含已判），遇到已判的跳过 —— 不是"在 pending 列表上取模"。
    旧实现（pending 列表 + `cur % len`）有个静默缺陷：列表会随判定变短，于是
    ① 只浏览不判定时游标会重复停在同几道题；② 一旦有题在游标**后面**被判掉，
       游标处的题就被整段跳过。2026-09-16 实测：mix30 判了 18 题后，
       5 道将夜一次都没被端出过（集霸会以为"这批判完了"，其实有题从未出现）。
    固定列表 + 跳过已判 = 每道题恰好出一次。

    只出平行文本候选（`config.is_servable_pv`）：消融条件 `recon_ctxonly_v1` 是自由续写、
    与人类段不是同一内容，"哪边更好"不成立。该白名单比抽样池多 `corrupt_*` 整族
    （受控劣化对照，§7）——按批次显式端出，但永不进随机抽样池。
    """
    q = s.query(ReviewItem).join(Candidate, Candidate.id == ReviewItem.subject_id)\
        .filter(_servable_cond())\
        .order_by(ReviewItem.priority.desc(), ReviewItem.id)
    if exp_id:
        q = q.filter(ReviewItem.experiment_id == exp_id)
    rows = q.limit(500).all()
    if batch:
        want = "batch_" + batch
        rows = [r for r in rows if want in (r.reasons or [])]
    if not rows:
        raise HTTPException(404, "队列已清空")
    key = batch or "_all"
    cur = _load_cursor(key)
    n = len(rows)
    for step in range(n):
        i = (cur + step) % n
        if rows[i].status == "pending":
            _save_cursor(key, i + 1)
            return _serve_payload(s, rows[i], ctx_scope)
    raise HTTPException(404, "队列已清空")


@app.get("/review/next")
def review_next_any(batch: str | None = None, ctx: str = "near"):
    """按**批次**取题，不限实验 —— 跨语料混合批（下例 mix30：凡人10+将夜5+琼明15）
    的同批题分属不同 experiment，按单实验路径取会被 experiment_id 过滤掉其余部分。"""
    with db.session() as s:
        return _pick_next(s, batch, None, ctx)


@app.get("/experiments/{exp_id}/review/next")
def review_next(exp_id: str, batch: str | None = None, ctx: str = "near"):
    """取一道待评题，匿名化成 A/B 返回。绝不暴露哪边是 Human。

    Phase 1.5 规程：默认附带上文（孤立段评审已被证实有仪器偏差）。
    batch=xxx → 只出该批次标记题。

    只出平行文本候选（`BLIND_REVIEW_PROMPT_VERSIONS`）：消融条件
    `recon_ctxonly_v1` 是自由续写、与人类段不是同一内容，"哪边更好"不成立。
    """
    with db.session() as s:
        return _pick_next(s, batch, exp_id, ctx)


@app.get("/experiments/{exp_id}/review/{review_id}/serve")
def review_serve_one(exp_id: str, review_id: str):
    """按 id 重端一道**已判**题——「改判」入口。A/B 重新随机并刷新映射；
    附 prev（原判定 + 批注，已按本次新 A/B 位翻译好）供前端回填。"""
    with db.session() as s:
        r = s.get(ReviewItem, review_id)
        if not r or r.experiment_id != exp_id:
            raise HTTPException(404, "not found")
        if r.status != "done":
            raise HTTPException(400, f"该题状态 {r.status}，改判入口只接已判题")
        cand = s.get(Candidate, r.subject_id) if r.subject_type == "candidate" else None
        if not cand or not config.is_servable_pv(cand.prompt_version):
            raise HTTPException(400, "非盲评白名单口径，不重端")
        out = _serve_payload(s, r)
        hv = r.human_verdict or {}
        served_now = _blind_get(out["presentation_id"])   # 就用刚生成的那一份，别二次查找（会审 qwen 席）
        new_hf = bool(served_now.get("human_first")) if served_now else None   # .get：落盘恢复的条目字段缺失也不许 500（会审 qwen 席）
        w_res = hv.get("winner_resolved")
        prev = {"winner_resolved": w_res, "winner_side": None,
                "reviewed_at": r.reviewed_at, "annotations": []}
        if new_hf is None:
            # 同一请求内 _serve_payload 刚写过呈现，理论上取不到只可能是落盘/内存异常。
            # 分支保留（防 500），但必须留日志 —— 否则静默降级没人知道（会审两席要求有覆盖）。
            logger.warning("改判端题后取不到呈现（review=%s）：旧判定与批注无法翻回 A/B", review_id)
            prev["note"] = "no_presentation: 取不到本次呈现，旧判定无法翻回 A/B（不猜）"
        elif w_res in ("human", "candidate"):
            prev["winner_side"] = "A" if (w_res == "human") == new_hf else "B"
        elif w_res in ("tie", "both_bad", "cant_judge"):
            prev["winner_side"] = w_res
        for a in hv.get("annotations") or []:
            t = a.get("target")
            if t not in ("human", "candidate") or new_hf is None:
                continue  # mapping_lost / 无呈现的粗数据，无法安全回填位置，跳过
            side = "A" if (t == "human") == new_hf else "B"
            prev["annotations"].append({"side": side, "start": a.get("start"),
                                        "end": a.get("end"), "text": a.get("text"),
                                        "kind": a.get("kind"), "note": a.get("note")})
        out["prev"] = prev
        # 已判过 ⇒ 信号标签不再是"判定前锚点"，可以一并展示
        out["tags"] = [x for x in (r.reasons or []) if not x.startswith("batch_")]
        return out


class Annotation(BaseModel):
    """盲评过程中的「噪点」批注：指出某一段里具体哪几处坏了。

    动机（集霸 2026-09-14）：有些段落整体其实还可以，但总有几处坏了把整段拖垮。
    只给 A/B 一个胜负会丢掉这个信息——批注是**用户亲手指认的缺陷位置**，
    比事后从指标反推相关性干净得多（见 scripts/pref_drivers.py 的间接做法）。
    """
    side: str            # "A" | "B"（匿名侧，服务端再翻译成 human/candidate）
    start: int           # 相对该侧全文的字符偏移
    end: int
    text: str = ""       # 冗余存被选中的原文，便于人工核对
    kind: str = "other"  # 缺陷类型标签
    note: str = ""


class Verdict(BaseModel):
    winner: str  # A|B|tie|both_bad|cant_judge
    presentation_id: str | None = None   # 本次呈现的不可变标识（审计 A01；不带则走 legacy 兜底并标记）
    a_hash: str | None = None            # 页面看到的 A 文本指纹（可选回传，用于证伪过期页面）
    b_hash: str | None = None
    reasons: list[str] = []
    annotations: list[Annotation] = []


@app.post("/review/{review_id}/verdict")
def verdict(review_id: str, body: Verdict):
    from datetime import datetime
    if body.winner not in ("A", "B", "tie", "both_bad", "cant_judge"):
        raise HTTPException(400, "winner 必须是 A|B|tie|both_bad|cant_judge")
    # 呈现绑定（审计 A01）：带 pid 就**只**认那一份呈现；pid 未知/过期一律拒绝，
    # 不回退到"最近一次呈现"去猜（正是那个猜测让旧页面的选择按新映射解释）。
    pid_given = (body.presentation_id or "").strip() or None
    if pid_given:
        served = _blind_get(pid_given)
        binding = "presentation_id"
        if served is None:
            raise HTTPException(409, "本次呈现不存在或已被清理（超出保留上限，或来自另一个实例）："
                                     "请从「已判回顾」重新端题后再判")
        if served.get("review_id") != review_id:
            raise HTTPException(400, "presentation_id 与 review_id 不匹配（疑似串页提交），已拒绝")
        # 指纹证伪：页面回传它**看到**的两侧文本指纹，与冻结值不符 ⇒ 页面拿着旧文本投新题，拒绝
        for key, sent in (("a_hash", body.a_hash), ("b_hash", body.b_hash)):
            if sent and served.get(key) and sent != served[key]:
                raise HTTPException(409, "页面显示的文本与本次呈现不一致（呈现已被替换）："
                                         "请重新端题后再判")
        pid_used = pid_given
    else:
        # 过渡路径（老客户端不带 pid）：**单次持锁**同时取 pid 与映射，二者必属同一份呈现。
        # 连最近一份呈现都没有（重启且未落盘/已被清理）⇒ 无从解释 A/B，直接 409，绝不猜。
        pid_used, served = _blind_latest(review_id)
        binding = "legacy_review_id" if served else "none"
        if served is None:
            raise HTTPException(409, "本服务没有该题的呈现映射（重启过或已被清理）："
                                     "请从「已判回顾」重新端题后再判")
    human_first = served.get("human_first") if served else None
    with db.session() as s:
        r = s.get(ReviewItem, review_id)
        if not r:
            raise HTTPException(404, "not found")
        prev = r.human_verdict if r.status == "done" else None
        # 改判纪律（2026-09-16）：已判题允许覆盖（改判），但只有映射还活着才能把
        # A/B 翻回 human/candidate。映射丢了（服务重启）又要投 A/B 时宁可 409 拒绝，
        # 也不让"原始 A/B"覆盖语义值——那会产生不知道按哪套解读的脏判定。
        if prev is not None and human_first is None and body.winner in ("A", "B"):
            raise HTTPException(409, "该题已判过且本进程没有它的 A/B 映射（服务重启过）；"
                                     "请从「已判回顾」点改判重新端题")
        resolved = body.winner
        mapping_note = None
        if body.winner in ("A", "B"):
            if human_first is None:
                mapping_note = "mapping_lost: 服务端重启过，本次只存原始 A/B"
            else:
                resolved = "human" if (body.winner == "A") == bool(human_first) else "candidate"
        # 批注过盲评翻译（A/B → human/candidate）并做偏移校验：
        # 按本题实际端出的文本核对 start/end/text，越界夹回、文字对不上标
        # verified=False——不丢数据，但下游统计能区分可信批注。
        side_texts = _side_texts(s, r, human_first)
        anns = []
        for a in body.annotations:
            if a.side not in ("A", "B"):
                continue
            target = None
            if human_first is not None:
                target = "human" if (a.side == "A") == bool(human_first) else "candidate"
            start, end, verified = int(a.start), int(a.end), None
            st = side_texts.get(a.side)
            if st is not None:
                n = len(st)
                start = max(0, min(start, n))
                end = max(start, min(end, n))
                verified = bool(end > start) and st[start:end] == (a.text or "")
            anns.append({
                "side_raw": a.side,
                "target": target,          # human | candidate | None(映射丢失)
                "start": start, "end": end,
                "text": (a.text or "")[:300],
                "kind": (a.kind or "other")[:40],
                "note": (a.note or "")[:300],
                "verified": verified,      # True/False=校验结果；None=映射丢失无法校验
            })
        r.status = "done"
        r.human_verdict = {
            "winner_raw": body.winner,
            "winner_resolved": resolved,
            "presentation_id": pid_used,
            "presentation_binding": binding,   # presentation_id | legacy_review_id | none
            "mapping_note": mapping_note,
            "human_was_a": human_first,
            "ctx_mode": served.get("ctx_mode") if served else None,
            "reasons": body.reasons,
            "annotations": anns,
            "n_annotations": len(anns),
            "rejudged": prev is not None,
            "prev_winner_resolved": (prev or {}).get("winner_resolved"),
            "n_verdicts": (int((prev or {}).get("n_verdicts") or 1) + 1) if prev else 1,
        }
        r.reviewed_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        s.commit()
        # 判定之后信号标签（score/stratum/human_upset…）不再是锚点，随响应返回供展示
        tags = [x for x in (r.reasons or []) if not x.startswith("batch_")]
        return {"ok": True, "resolved": resolved, "mapping_note": mapping_note,
                "n_annotations": len(anns), "rejudged": prev is not None,
                "tags": tags}


# ── 观测 ────────────────────────────────────────────────────

@app.get("/llm/stats")
def llm_stats(hours: int = 24, exp: str | None = None):
    # 原全表口径（by_purpose.calls/tokens/failed）原样保留：控制台 loadUsage 只读它。
    # 任务 14 增量：window = 近 hours 小时的时间窗聚合（模型/用途/状态/小时/失败
    # Top-N/实验排行），字段只加不减，见 app/observability.py。
    if hours < 1 or hours > 24 * 30:
        raise HTTPException(400, f"hours 取值 1~720，收到 {hours}")
    with db.session() as s:
        rows = s.query(LlmCall).all()
        out: dict = {}
        for r in rows:
            k = r.purpose
            d = out.setdefault(k, {"calls": 0, "tokens": 0, "failed": 0})
            d["calls"] += 1
            d["tokens"] += r.tokens_in + r.tokens_out
            if r.status != "ok":
                d["failed"] += 1
        resp = {"mode": config.LLM_MODE, "by_purpose": out}
        try:
            resp["window"] = observability.snapshot(s, hours=hours, exp=exp)
        except ValueError as e:
            # 查无此实验：报错，不静默给空窗口（纪律④）
            raise HTTPException(404, str(e))
        return resp


# ── 只读控制台（总方案 §18 的 16 个模块页，T2）──────────────

@app.get("/console")
def console_index():
    """模块索引：slug/标题清单（前端外壳按此渲染导航）。"""
    return {"modules": console.index()}


@app.get("/console/ui", include_in_schema=False)
def console_ui():
    """第二界面旧外壳（T3，2026-09-19 起降级）：302 跳研究台正式版。

    2026-09-19：websrc 16 页接通后，本页不再是入口。**文件保留不删**
    （app/static/console.html 与 tests/test_console_page.py 的静态契约仍在跑），
    只是访问它会被送到 /lab/overview.html，避免两个第二界面并存让人走错。
    集霸决策 ②A，见 docs/frontend-plan-2026-09-19.md。
    """
    return RedirectResponse(url="/lab/overview.html", status_code=302)


@app.get("/console/{module}")
def console_module(module: str):
    """GET-only 数据面；聚合逻辑在 app/console.py（全部只读，回归测试钉住）。"""
    try:
        return console.module_data(module)
    except KeyError:
        raise HTTPException(404, f"未知控制台模块：{module}；可用：{console.MODULE_ORDER}")


# ── 研究工作台（websrc 16 页，2026-09-19 接通）──────────────
# 第二界面的正式版：每模块一页、左侧栏导航、GET-only 只读台账。
# 与 /console/ui（旧外壳）的关系：旧外壳保留代码但前端已不再指向它，见 docs/frontend-plan-2026-09-19.md。
# 访问门（app/access.py）是全局 http middleware，/lab/* 自动继承令牌闸，无需在此重复鉴权。

_LAB_PAGES = {
    "overview.html", "corpus.html", "frames.html", "arena.html", "residual.html",
    "strategy.html", "judges.html", "preference.html", "hardcase.html", "benchmark.html",
    "experiments.html", "models.html", "training.html", "workflow.html",
    "observability.html", "settings.html",
}


@app.get("/lab", include_in_schema=False)
def lab_root():
    """研究台首页（总览总控台）。"""
    return RedirectResponse(url="/lab/overview.html", status_code=302)


@app.get("/lab/{page}", include_in_schema=False)
def lab_page(page: str):
    """研究台单页。

    白名单式取文件：只认 websrc/ 下的 16 个页面名，挡掉目录穿越
    （`..%2f`、绝对路径、非常规扩展名一律 404）。HTML 走 no-store，
    与本项目其它页面同一约定——改完必须立刻可见，不被浏览器钉在旧版。
    """
    if page not in _LAB_PAGES:
        raise HTTPException(404, f"未知研究台页面：{page}")
    target = (_WEBSRC / page).resolve()
    if target.parent != _WEBSRC.resolve() or not target.is_file():
        raise HTTPException(404, f"研究台页面缺失：{page}")
    return FileResponse(target, headers={"Cache-Control": "no-store"})
