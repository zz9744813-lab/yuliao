"""FastAPI 薄层 + 最小 Web 控制台。

启动：uvicorn app.main:app --reload --port 8787
控制台：http://127.0.0.1:8787/  （鉴权见 app/access.py：loopback 免令牌需
显式 LG_LOCAL_BYPASS=1，默认本机访问也需令牌）

主要端点（档位注明，2026-09-25 起两档令牌生效，见 app/access.py）：
  POST /corpus/import-inbox | import-distiller | import-file   [管理档=admin]
                                                               （建段/扩产入口，评审档 403）
  GET  /works /segments /corpus/stats                         [评审档]
  POST /experiments                                            [管理档=admin]（建实验=扩产）
  POST /experiments/{id}/run                                   [管理档=admin]（启 run=烧钱/扩产）
  GET  /experiments /experiments/{id} /experiments/{id}/stages /experiments/{id}/report(.json)
                                                               [评审档]
  GET  /experiments/{id}/review          队列（含优先级理由）    [评审档]
  GET  /experiments/{id}/review/next     盲评取题：匿名 A/B，服务端暗记映射   [评审档]
  GET  /experiments/{id}/review/{rid}/serve  改判入口：重端已判题   [评审档]
  POST /review/{id}/verdict    {"winner":"A|B|tie|both_bad|cant_judge","reasons":[...]}
                                                               [评审档]（评审者的写入口，专属）
  GET  /review/batch/{b} | /review/batch/{b}/done   批次概览 / 已判清单   [评审档]
  GET  /llm/stats                                            [评审档]
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import access, config, console, corpus, db, engine, experiments, observability
from .ids import new_id
from .models import (Candidate, Experiment, Job, ReviewItem, ReviewPresentation,
                     Segment, Work, LlmCall)

app = FastAPI(title="Language Genome — SemanticFrame Calibration Lab", version="0.2.0")
logger = logging.getLogger(__name__)

_STATIC = Path(__file__).resolve().parent / "static"
# 研究台（第二界面正式版，websrc/）：16 模块只读页。与 _STATIC 分离是为了让
# 前端源码留在仓库根、不混进应用包；挂载点见下方 /lab 路由。
_WEBSRC = Path(__file__).resolve().parent.parent / "websrc"

# 远程访问门：经隧道/代理的请求需令牌；loopback 免令牌只在显式
# LG_LOCAL_BYPASS=1 时开启（R2，2026-09-23：不再隐式默认，见 app/access.py）。
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
# 以下三个导入端点 = 建段/扩产入口，只认管理档（app/access.requires_admin）；
# 评审令牌走这里一律 403。既有行为/实现零改动，档位由访问门中间件裁决。

@app.post("/corpus/import-inbox")
def import_inbox():
    with db.session() as s:
        return corpus.import_inbox(s)


class FileImport(BaseModel):
    # 审计 P1（2026-09-23）：形状校验挡空串/超长；真正的内容闸在
    # corpus.import_file（允许根 + 扩展名白名单 + 体积上限）。
    path: str = Field(min_length=1, max_length=1024)
    title: str | None = None
    author: str | None = None
    note: str | None = None


@app.post("/corpus/import-file")
def import_file(body: FileImport):
    with db.session() as s:
        # 守卫拒因（import_root_not_allowed 等）以 HTTP 200 + error 字段
        # **原样透出**——与既有错误返回风格一致，不改 500。
        return corpus.import_file(s, body.path, title=body.title,
                                  author=body.author, note=body.note)


class DistillerImport(BaseModel):
    # 默认值留在签名里（既有调用方依赖）；corpus.import_distiller 把这两个
    # 逐字路径列为内置精确白名单例外（见 app/corpus.py 的
    # _DISTILLER_BUILTIN_EXACT 注释），其余路径必须在 LG_IMPORT_ROOTS 允许根内。
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


# /segments 翻页上界收口（独立审查 2026-09-23 F2 [严重]，lg-fix-server-caps-residual）：
# 旧签名 limit/offset 无上界，配合 [:80] 预览可对整库正文无界翻页外流（该端点
# 同时是审计 P1「评审令牌可读取」的读出口）。口径 = 越界一律 422、**不 clamp**
# （与 ExperimentIn 同一纪律：clamp 让调用者拿到的页与意图不符）。
#   · limit ∈ [1, 200]：前端实际只用 ?limit=8（index.html:1433），200 封顶单页；
#   · offset ≥ 0 且 offset+limit ≤ 5000（总量闸）：单令牌可外流的正文总量
#     封顶在 5000×80 字；再往外翻必须按 work_id 收窄或走受控导出。
_MAX_SEGMENT_LIMIT = 200
_MAX_SEGMENT_SCAN = 5000


@app.get("/segments")
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
        # [:80] 是响应里段文本预览的**硬上限**（审计 P1：防整库正文经列表端点
        # 批量外流）。长度/字段语义已定，不要放宽或改名。
        return [{"id": x.id, "work": x.work_id, "chars": x.n_chars, "sents": x.n_sentences,
                 "text": x.text[:80] + "…"} for x in rows]


@app.get("/corpus/stats")
def corpus_stats():
    with db.session() as s:
        return corpus.segment_stats(s)


# ── 实验 ────────────────────────────────────────────────────

# ExperimentIn 校验常量（审计 P1 2026-09-23 余项：入参枚举 + 数量/长度/区间上限）。
# 数值依据全部来自仓库内实测口径（app/experiments.DEFAULT_CONFIG、app/config.py、
# scripts/ 与 docs/calibration-design.md 的历史实验取值），并写明安全余量：
#
#   granularities  真值域 = frames_schema.Granularity 的 S/M/L 三档 Literal
#                  （DEFAULT_CONFIG 默认 ["S","M","L"]），白名单即全值域，无放宽。
#   n_segments     默认 24（Phase 1 定标实验）；脚本最大观测 50
#                  （scripts/corpus_matrix_extract.py）→ 上限 200（≈4× 观测值）。
#                  成本随段数近似线性放大（再乘粒度/模型/温度/采样数），必须封顶。
#   samples_per_pair  默认 2、观测 1~2 → 上限 8（4× 余量）。每帧候选数
#                  = 模型数×温度数×采样数，封顶后再乘也已有硬上界。
#   adversarial_k  默认 8；docs/calibration-design.md 记 "16 更稳但贵一倍" → 上限 32；
#                  下限 0（scripts/bench_recon_setup.py 用 k=0 跳过对抗层，是既有口径）。
#   concurrency    全部既有实验取值 4；网关单次超时 300s（config.HTTP_TIMEOUT_S）→
#                  上限 16（4× 余量）：挡持令牌者把并发拉满放大费用/撞网关限速。
#   模型列表       DEFAULT_RECON_MODELS 实测 4 个、DEFAULT_TEMPERATURES 4 个 →
#                  各上限 8（2× 余量，换模/消融有余地）。
#   work_ids       Work.id = new_id("WK") 形如 "WK-"+12hex、列宽 String(32) →
#                  元素上限 64（全语料 Work 远低于此；不限域用 None，无需穷举传入）。
_GRANULARITIES = ("S", "M", "L")
_MAX_SEGMENTS = 200
_MAX_SAMPLES_PER_PAIR = 8
_MAX_ADVERSARIAL_K = 32
from .limits import MAX_CONCURRENCY as _MAX_CONCURRENCY
_MAX_MODEL_LIST = 8
_MAX_TEMP_LIST = 8
_MAX_WORK_IDS = 64
# 模型 ID 字符白名单（纵深防御：前端收口另派 lg-fix-frontend-escape）。
# 实测网关模型名只含字母数字与 . - _ / : @ +（deepseek-v4.1-flash、
# moonshotai/kimi-k3、z-ai/glm-5.3、agnes-3.0-flash）；引号/尖括号/空白/
# 换行/反斜杠等一律进不来——这些串会原样存进 config 并随响应回到任意
# 渲染端（列表页、控制台、研究台），必须挡在落库前。
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}")
# work_id 同上，字符集收紧到 ID 生成器实际产出（字母数字、-、_），列宽 32。
_WORK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class ExperimentIn(BaseModel):
    """POST /experiments 入参契约（服务端强制，改前端绕不过）。

    越界一律 422（pydantic RequestValidationError），**不 clamp、不静默过滤**：
    clamp 会让落库的实验配置与调用者意图不符，属新的对账隐患；混着非法值时
    整单拒绝，错误信息带字段名与合法域，可行动。
    唯一的重解释是刻意约定：**空列表 [] 视同未提供（None）**——与既有前端
    空表单口径一致（`create_experiment` 对 falsy 一律落到 DEFAULT_CONFIG），
    并顺带挡掉 `granularities: []` 这种"零帧零候选的空实验"。非空列表里的
    任何非法元素都会整单 422，不存在"丢坏留好"。
    """
    # 数值字段：下限挡 0/负数（无意义且绕闸），上限见上方常量的依据注释。
    n_segments: int | None = Field(default=None, ge=1, le=_MAX_SEGMENTS)
    granularities: list[str] | None = None
    recon_models: list[str] | None = None
    judge_models: list[str] | None = None
    temperatures: list[float] | None = None
    samples_per_pair: int | None = Field(default=None, ge=1,
                                         le=_MAX_SAMPLES_PER_PAIR)
    adversarial_k: int | None = Field(default=None, ge=0,
                                      le=_MAX_ADVERSARIAL_K)
    concurrency: int | None = Field(default=None, ge=1, le=_MAX_CONCURRENCY)
    work_ids: list[str] | None = None

    @field_validator("granularities")
    @classmethod
    def _v_granularities(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        bad = [x for x in v if x not in _GRANULARITIES]
        if bad:
            raise ValueError(
                "granularities 只允许 " + "/".join(_GRANULARITIES)
                + " 的子集（frames_schema 三档 Literal），非法值: "
                + repr(bad[:3]))
        if len(set(v)) != len(v):
            raise ValueError("granularities 不允许重复项（重复=同粒度多跑一遍）")
        return list(v)

    @staticmethod
    def _v_token_list(v: list[str] | None, *, name: str, pattern: re.Pattern,
                      max_items: int, item_hint: str) -> list[str] | None:
        if not v:
            return None
        if len(v) > max_items:
            raise ValueError(f"{name} 最多 {max_items} 个元素，收到 {len(v)} 个")
        for x in v:
            if not pattern.fullmatch(x):
                raise ValueError(f"{name} 含非法元素（{item_hint}）: {x[:40]!r}")
        return list(v)

    @field_validator("recon_models", "judge_models")
    @classmethod
    def _v_models(cls, v: list[str] | None, info) -> list[str] | None:
        return cls._v_token_list(
            v, name=info.field_name, pattern=_MODEL_ID_RE,
            max_items=_MAX_MODEL_LIST,
            item_hint="只允许字母数字与 . - _ / : @ +，长度 1~64")

    @field_validator("work_ids")
    @classmethod
    def _v_work_ids(cls, v: list[str] | None) -> list[str] | None:
        return cls._v_token_list(
            v, name="work_ids", pattern=_WORK_ID_RE, max_items=_MAX_WORK_IDS,
            item_hint="只允许字母数字与 - _，长度 1~32（Work.id 列宽口径）")

    @field_validator("temperatures")
    @classmethod
    def _v_temperatures(cls, v: list[float] | None) -> list[float] | None:
        if not v:
            return None
        if len(v) > _MAX_TEMP_LIST:
            raise ValueError(f"temperatures 最多 {_MAX_TEMP_LIST} 个元素，"
                             f"收到 {len(v)} 个")
        for t in v:
            # 采样温度合法域 0~2（各网关口径上限；既有实验实测 0.3~1.1，
            # config.DEFAULT_TEMPERATURES 最大 1.1）。越界温度是配置错误，
            # 拒掉而不是夹到 2.0。
            if not 0.0 <= t <= 2.0:
                raise ValueError(f"temperatures 元素必须在 0.0~2.0，收到 {t!r}")
        return list(v)


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


# ── R1（审计残留 2026-09-23）：实验 run 的**全局预算闸** ─────────
# 单请求上限（ExperimentIn 全量上限）挡不住费用放大：持令牌者可以循环
# 创建/运行顶格实验——等单个实验跑完再发下一个，资源消耗照样无界。
# 本闸在 run 入口限制**全局并发运行中实验数**，默认 1（最保守：一次只烧
# 一个实验的钱，审计建议口径）；LG_MAX_RUNNING_EXPERIMENTS 可显式调高，
# 但解析结果恒 ≥1——预算闸不许被环境变量关掉。
#
# 原子性：检查与占用在同一把进程锁内完成，不存在「先查后设」的 TOCTOU
# 窗口——所有 run 请求串行通过 _budget_acquire，赢家的占用先于任何后来
# 者的检查。作用域=本进程：serve_remote.sh 只起单 uvicorn 进程（无
# --workers），进程内计数与真实运行一一对应；进程重启计数归零，而旧后台
# 线程也随进程消亡，不存在「计数清了但活还在烧」的错位。
#
# 归还路径（每条占用恰好归还一次）：
#   1. claim_run 失败（含 409 竞争输家）→ 立刻归还；
#   2. claim_run 抛异常 → 归还后原样上抛；
#   3. 正常启动 → 看护线程 join 后台引擎线程后归还——engine.run 的
#      finally 必定收尾实验行，join 返回即该 run 已不再执行。看护线程
#      daemon、不持任何其它资源，不阻塞进程退出。
# 已知取舍：运行入口有 404/冻结（400）检查在预算闸**之前**，非法请求
# 不消耗预算；同一实验重跑撞上预算满时会得 429（而非 409）——语义仍
# 可读（"等当前实验跑完"），不给预算闸开「逐实验豁免」的口子。
_RUN_BUDGET_LOCK = threading.Lock()
_budget_active = 0


def _budget_max() -> int:
    raw = (os.environ.get("LG_MAX_RUNNING_EXPERIMENTS") or "").strip()
    if not raw:
        return 1
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _budget_active_count() -> int:
    with _RUN_BUDGET_LOCK:
        return _budget_active


def _budget_acquire() -> bool:
    global _budget_active
    with _RUN_BUDGET_LOCK:
        if _budget_active >= _budget_max():
            return False
        _budget_active += 1
        return True


def _budget_release() -> None:
    global _budget_active
    with _RUN_BUDGET_LOCK:
        _budget_active = max(0, _budget_active - 1)


def _reset_budget_for_tests() -> None:
    """测试隔离钩子：把预算计数清零。只许测试调用（归还语义的回归钉在
    tests/test_remote_caps_budget.py 的 test_budget_released_*）；生产路径
    不重置——运行中的实验不该被任何隐式动作豁免预算。"""
    global _budget_active
    with _RUN_BUDGET_LOCK:
        _budget_active = 0


def _watch_budget_release(t) -> None:
    """后台 run 结束后归还预算位。测试替身可能返回非 Thread 对象——
    那种情况没有可 join 的执行体，同步归还（不多占一毫秒）。"""
    def _w():
        try:
            if isinstance(t, threading.Thread):
                t.join()
        finally:
            _budget_release()
    threading.Thread(target=_w, daemon=True,
                     name="run-budget-watchdog").start()


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
        if (e.config or {}).get("frozen"):
            # 冻结必须在领取**之前**拒（五轮：先领再拒会把行卡在
            # running+token——engine.run 的冻结检查晚于 API 领取）
            raise HTTPException(400, "experiment 已冻结（Phase 1 定标实验），禁止重跑；复现在新实验进行")
    # R1 全局预算闸：先占位再领取——占位失败 429（不碰领取，零副作用）；
    # 领取失败/异常立即归还占位（见上方注释的归还路径）。
    if not _budget_acquire():
        raise HTTPException(
            429, f"全局运行预算已满：运行中实验数已达上限 {_budget_max()}"
                 f"（LG_MAX_RUNNING_EXPERIMENTS，默认 1）。请等当前实验跑完"
                 f"再试；确需并行请显式调高该环境变量")
    # A07 四轮：**领取即闸**——在请求内做原子领取（旧「先查后启」是竞争
    # 窗口；旧快路径还把 running+NULL-owner 的存量行直接挡回，自动对账
    # 永远走不到）。领到 → 带凭据启动后台线程，响应如实 started；没领到
    # → 409 already_running（不发「已启动」的假响应；五轮：claim 失败先
    # 重核存在性——两步之间被删的实验应报 404 而不是 409）。
    token = new_id("RUN")
    try:
        claimed = engine.claim_run(exp_id, token)
    except Exception:
        _budget_release()
        raise
    if not claimed:
        _budget_release()
        with db.session() as s:
            if not s.get(Experiment, exp_id):
                raise HTTPException(404, "experiment 不存在")
        raise HTTPException(409, "already_running：执行权被持有（存量卡死排查用 "
                                 "run_experiment.py --list-stuck / --release）")
    t = engine.run_experiment_background(exp_id, stages, token=token)
    _watch_budget_release(t)   # 后台 run 结束（或替身不可 join）时归还预算位
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
# 同时保留 DB 表 review_presentations（main 分支 A01 落库路径）：双持久，读取先内存后 DB。
_BLIND_CAP = 512
_BLIND_LAST_CAP = 512    # 索引上限**不得大于** _BLIND_CAP：否则留下"rid→已逐出 pid"的悬挂条目
                          # （会审 qwen 席：那种悬挂条目过去会走进 legacy 且照常入库）
_BLIND_MAP: dict[str, dict] = {}       # presentation_id → entry
_BLIND_LAST: dict[str, str] = {}       # review_id → 最近一次 presentation_id。
                                       # 整改后（无 pid ⇒ 409，见 verdict）**不再有读取方**：
                                       # 猜义路径 _blind_latest 已删，这里只为落盘格式
                                       # （blind_presentations.json 的 "last" 键）与审计保留。
_BLIND_LOCK = threading.Lock()
_BLIND_LOADED_FOR: str | None = None   # 已装载的呈现文件路径（懒加载哨兵，见 _blind_ensure_loaded）
_SERVE_CURSOR: dict[str, int] = {}   # 批次轮换游标（进程内缓存；真值落盘，见下）

# 游标**落盘**：2026-09-17 集霸反馈"一堆题在那轮换来乱换去"。
# 根因是游标只在进程内存里 —— 当天重启 4 次（改代码/改 UI），每次归零，
# 队列就从第一批待判题重新开始，于是他反复看到同一批题。
# 落盘后重启不再回退；想从头再来删掉这个文件即可。
# 路径必须从 config.DATA_DIR 派生（A04，审查 20260920-1810）：旧硬编码
# 直接指向仓库 data/serve_cursor.json，测试只隔离了 LG_DATA_DIR 与数据库、
# 管不住这个路径——unlink/写入全打在真实文件上，测试批次键（c41/rj*/pr*）
# 污染正式游标，多轮全量测试还反复销毁历史内容。派生后测试自动落进
# LG_DATA_DIR 的临时目录，正式文件不再被测试触碰。
# 本常量被 tests/test_cursor_isolation.py 钉住（等值校验）；**运行时读写**一律
# 走 _cursor_file()——每次从 config.DATA_DIR 派生，不固化在模块导入那一刻：
# 脚本/测试里"先 import app.api 再改 DATA_DIR"很常见，固化后会照旧写到**真实**
# 游标文件上——现场文件里已经留下 c41/rj1..rj8 等测试批次键，就是这么来的（会审两席同指）。
_CURSOR_FILE = config.DATA_DIR / "serve_cursor.json"


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
        # 会审建议：告警必须带**文件路径**——只给批次键定位不到是哪个数据目录坏了
        logger.warning("游标文件读不出（%s，key=%s）：%s，本次从 0 开始", _cursor_file(), key, e)
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


def _sha16(t: str) -> str:
    """呈现两侧文本的短指纹（A01：冻结「当时端出的是什么」，审计追溯用）。"""
    return hashlib.sha1((t or "").encode("utf-8")).hexdigest()[:16]


def _blind_get(presentation_id: str) -> dict | None:
    """按**呈现**取映射：拿不到就是过期/未知，调用方必须拒绝，不许回退猜测。"""
    _blind_ensure_loaded()
    with _BLIND_LOCK:
        return _BLIND_MAP.get(presentation_id)


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
    # A01（审查 2026-09-20）：每次端题落一行**不可变呈现**——旧实现按 review_id
    # 只存一份进程内映射，同题重出题会覆盖旧行，旧页面提交的 A/B 被新映射
    # 静默误译（实测：第一页 A=原文、第二页 A=候选，第一页投 A 记成 candidate，
    # 污染最贵的用户偏好标签）。提交必须绑定 presentation_id 按呈现当时的排列
    # 解读；DB 持久化 = 跨重启、多 worker 共识。呈现先落库再端出——
    # 页面端出去的瞬间就可能被提交。
    pr = ReviewPresentation(review_id=r.id, human_first=human_first,
                            ctx_mode=mode,
                            text_a_sha=_sha16(a), text_b_sha=_sha16(b))
    s.add(pr)
    s.commit()
    ah, bh = _fp16(a), _fp16(b)
    _blind_put(pr.id, {"review_id": r.id, "human_first": human_first, "ctx_mode": mode,
                       "presentation_id": pr.id, "ctx_scope": ctx_scope,
                       "a_hash": ah, "b_hash": bh, "created_at": _now_iso()})
    return {"review_id": r.id,
            "presentation_id": pr.id,
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


# 批注 kind 唯一口径（独立审查 2026-09-23 F1 [严重]，lg-fix-server-caps-residual）：
# **口径源 = app/static/index.html:1108 MARK_KINDS，两边必须同步**。
# 066b754 的 commit message 声称做了本收口但代码里不存在——批注 kind 曾任意
# 字符串直通落库（仅 [:40] 截断，40 字符足够放注入串），再经 serve 改判回填与
# 响应回显原样透出。现在服务端强枚举：越界 422（与 ExperimentIn 口径一致）。
# 存量兼容性论证（审查 2026-09-23 真库快照）：review_items.human_verdict 存量
# kind 分布 = 用词 29 / 解释过度 19 / 其他 14 / 逻辑 1，全部落在 MARK_KINDS 内，
# 收口不拒任何存量合法值；库内 "other" 为零行。旧默认 "other" 随之改为 "其他"：
# "other" 不在枚举口径内，保留即豁免一个枚举外值；"其他" 是其直译，
# kind 缺省时落库语义不变，前端回填同一口径（index.html:1091 `a.kind||'其他'`）。
_MARK_KINDS = ("用词", "解释过度", "情绪直给", "节奏", "逻辑", "意象", "其他")


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
    kind: str = "其他"   # 缺陷类型标签，服务端强枚举（见 _MARK_KINDS / _v_kind）
    note: str = ""

    @field_validator("kind")
    @classmethod
    def _v_kind(cls, v: str) -> str:
        if v not in _MARK_KINDS:
            raise ValueError("kind 只允许 " + "/".join(_MARK_KINDS)
                             + "（口径源 = index.html MARK_KINDS），非法值: "
                             + repr(v[:40]))
        return v


class Verdict(BaseModel):
    winner: str  # A|B|tie|both_bad|cant_judge
    reasons: list[str] = []
    annotations: list[Annotation] = []
    # A01：本次提交对应哪一次端题呈现——带 A/B 语义的提交必须绑定
    # （二轮口径：无 pid 不再回退猜最近呈现，见 verdict 内 409）
    presentation_id: str = ""
    a_hash: str | None = None            # 页面看到的 A 文本指纹（可选回传，用于证伪过期页面）
    b_hash: str | None = None


@app.post("/review/{review_id}/verdict")
def verdict(review_id: str, body: Verdict):
    from datetime import datetime
    if body.winner not in ("A", "B", "tie", "both_bad", "cant_judge"):
        raise HTTPException(400, "winner 必须是 A|B|tie|both_bad|cant_judge")
    with db.session() as s:
        r = s.get(ReviewItem, review_id)
        if not r:
            raise HTTPException(404, "not found")
        # A01：A/B 的含义按「提交绑定的那次呈现」解读，不按全局最新映射——
        # 重出题不再改变旧页面提交的语义。双持久：内存呈现映射（含指纹，
        # 先查）+ DB review_presentations 行（重启兜底，无指纹字段则跳过证伪）。
        served = None
        pid_given = (body.presentation_id or "").strip() or None
        binding = "none"
        pid_used: str | None = None
        if pid_given:
            binding = "presentation_id"
            served = _blind_get(pid_given)
            if served is not None:
                if served.get("review_id") != review_id:
                    raise HTTPException(400, "presentation_id 与该题不匹配——"
                                             "别拿别题的呈现提交")
                # 指纹证伪：页面回传它**看到**的两侧文本指纹，与冻结值不符
                # ⇒ 页面拿着旧文本投新题，拒绝（会审：冻结字段必须真被用上）
                for key, sent in (("a_hash", body.a_hash), ("b_hash", body.b_hash)):
                    if sent and served.get(key) and sent != served[key]:
                        raise HTTPException(409, "页面显示的文本与本次呈现不一致（呈现已被替换）："
                                                 "请重新端题后再判")
                pid_used = pid_given
            else:
                pr = s.get(ReviewPresentation, pid_given)
                if pr is not None:
                    if pr.review_id != review_id:
                        raise HTTPException(400, "presentation_id 与该题不匹配——"
                                                 "别拿别题的呈现提交")
                    served = {"human_first": pr.human_first, "ctx_mode": pr.ctx_mode,
                              "presentation_id": pr.id}
                    pid_used = pr.id
                elif pid_given.startswith("PR-"):
                    # 本服务原生格式（app/ids.new_id("PR")）却查无 → 呈现行被删/来自历史库
                    raise HTTPException(404, f"呈现 {pid_given} 不存在（已被清理或来自历史）："
                                             "请从「已判回顾」重新端题后再判")
                else:
                    # 非本服务格式 ⇒ 过期/超出保留上限/来自另一个实例：拒绝，不回退猜
                    raise HTTPException(409, "本次呈现不存在或已被清理（超出保留上限，或来自另一个实例）："
                                             "请从「已判回顾」重新端题后再判")
        else:
            # A01 二轮（知识化调整方案 §1.1，2026-09-21）：旧客户端不带
            # pid——禁止按「最近一次呈现」猜含义（23:14 复现：旧页面提交
            # 被按新映射误译成 candidate，HTTP 200 打到最贵的偏好标签上）。
            # 凡带 A/B 语义（winner A/B 或有批注）的提交必须呈现绑定：
            # 该题存在任何呈现行而无 pid → 409 拒收，让客户端重取题重提；
            # 完全没有呈现行的 pre-A01 历史题保留「存原始值」unresolved
            # 路径（那是如实存未知，不是猜）。
            # 会审整改（2026-09-25，qwen 席 [严重]）：本分支**不再调用**
            # _blind_latest——"留痕"不是防线，且内存映射存活/逐出两种状态
            # 下同一提交的命运不同（服务端重启时序决定客户端结局）。
            if body.winner in ("A", "B") or body.annotations:
                has_presentations = s.query(
                    ReviewPresentation.id).filter_by(
                    review_id=review_id).first() is not None
                if has_presentations:
                    raise HTTPException(
                        409, "提交未带呈现绑定（页面过期/旧客户端）——"
                             "A/B 含义无法确定，禁止按最近呈现猜测；"
                             "请重新取题后再提交")
                if body.winner in ("A", "B"):
                    # 从无呈现行的历史题：A/B 依旧依赖一个从未存在过的排列，
                    # 存原始值同样是"不知道按哪套解读"的脏判定（口径钉在
                    # test_pending_never_served_now_rejected）→ 拒收。
                    # 仅 tie/both_bad/cant_judge、及无归属语义的纯批注，
                    # 才走上面注释说的「存原始值」unresolved 路径（binding=none）。
                    raise HTTPException(
                        409, "提交未带呈现绑定（页面过期/旧客户端），且本服务已无该题的呈现映射"
                             "（未端出/重启过/已被清理）——A/B 含义无法确定，禁止按最近呈现猜义；"
                             "请重新端题后再提交")
        human_first = served.get("human_first") if served else None
        prev = r.human_verdict if r.status == "done" else None
        # 改判纪律（2026-09-16）：已判题允许覆盖（改判），但只有映射还活着才能把
        # A/B 翻回 human/candidate。映射丢了（A01 落库前的历史题）又要投 A/B 时
        # 宁可 409 拒绝，也不让"原始 A/B"覆盖语义值——那会产生不知道按哪套解读的脏判定。
        if prev is not None and human_first is None and body.winner in ("A", "B"):
            raise HTTPException(409, "该题已判过且找不到它的 A/B 呈现（A01 落库前的历史）；"
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
                # 枚举校验后 [:40] 只是惰性兜底（MARK_KINDS 最长 4 字）；
                # 旧 `or "other"` 兜底已删——"other" 不在口径内（见 _MARK_KINDS）。
                "kind": a.kind[:40],
                "note": (a.note or "")[:300],
                "verified": verified,      # True/False=校验结果；None=映射丢失无法校验
            })
        r.status = "done"
        r.human_verdict = {
            "winner_raw": body.winner,
            "winner_resolved": resolved,
            "mapping_note": mapping_note,
            "human_was_a": human_first,
            # A01：本次判定按哪次呈现解读（审计追溯：排列/文本指纹见 review_presentations）
            "presentation_id": pid_used,
            "presentation_binding": binding,   # presentation_id | none
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


# ── 知识查询（K3-A，知识化方案 §6.1–6.2/§7.1）────────────────

@app.post("/knowledge/query")
def knowledge_query(body: dict):
    """K3-A：POST /knowledge/query——语义需求显式输入，策略经固定过滤顺序；
    只读（不写包；冻结归 K3-B）。旧名 /genome/query 只做文档统一，绝不
    新建第二套查询服务（本端点即唯一入口）。"""
    from . import knowledge_query as kq
    try:
        with db.session() as s:
            return kq.query_knowledge(body or {}, s)
    except kq.PolicyError as e:
        raise HTTPException(400, str(e))


@app.get("/knowledge/capabilities")
def knowledge_capabilities():
    from . import knowledge_query as kq
    with db.session() as s:
        return kq.capabilities(s)


@app.get("/knowledge/packages/{package_id}")
def knowledge_package(package_id: str):
    from . import knowledge_query as kq
    with db.session() as s:
        pkg = kq.get_package(package_id, s)
        if pkg is None:
            raise HTTPException(404, f"知识包不存在：{package_id}")
        return pkg


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
