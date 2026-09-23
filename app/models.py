"""数据表定义。面向 Calibration Lab 第一阶段，保持薄而完整；全部带 created_at 审计字段。

粒度枚举：frame.granularity ∈ {S, M, L}
候选匿名化：Judge prompt 只见 candidate.anon_label，模型名只存在 DB。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, or_, select
from sqlalchemy.dialects.sqlite import JSON  # SQLite/Postgres 均可用 JSON 普通列
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .ids import new_id
from .typo_map import V2_TITLE_SUFFIX


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


class Work(Base):
    __tablename__ = "works"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("WK"))
    title: Mapped[str] = mapped_column(String(200))
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(300))  # inbox:文件名 / distiller:xxx / file:路径
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchors: Mapped[str | None] = mapped_column(Text, nullable=True)  # T7 三重锚 JSON
    # corpus v2 的稳定血缘键：本 Work 是哪本 v1 Work 的 TYPO_MAP 修复镜像（存 v1 Work.id）。
    # 幂等判定只认它（外加标题后缀兼容回填前的历史行），**不认标题**——同名多 Work 会被标题键静默丢弃。
    v2_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Author(Base):
    """K1-A 来源登记（知识化方案 §4.1）：已核对的作者。

    「已核对」必须有核对依据（verified_basis）——标题/来源文件名/公开书目
    一致性的人工核对声明；不可考据的作者**留空**（WorkSource.author_id=NULL +
    metadata_status 如实标注），不许猜。"""
    __tablename__ = "authors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("AUTH"))
    name: Mapped[str] = mapped_column(String(120), unique=True)
    verified_basis: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Genre(Base):
    """K1-A：题材词表（最小集，按需增补；分配必须有核对依据）。"""
    __tablename__ = "genres"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("GEN"))
    name: Mapped[str] = mapped_column(String(60), unique=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class WorkSource(Base):
    """K1-A 来源登记：Work 的**扩展关联表**（方案 §4.1）——迁移不改旧 ID，
    works 表零改动；一行一 Work（work_id 唯一）。

    字段对照 §4.1：canonical_work_id / 已核对的 author_id / genre_ids /
    来源类型 / 文本版本·哈希 / 用途依据 / 允许用途 / 元数据核对状态与依据。

    纪律（方案原文逐条落死）：
    · corpus v2 镜像、重切段、清洗副本的 canonical_work_id 一律回连**同一根
      作品**——不计作独立复现（独立人类源计数只认
      canonical_work_id == work_id 的根作品行，见 verify_work_registry.py）；
    · 来源类型分型 fixture / synthetic / commentary / human_fiction——
      测试材料可验契约，但**不得给人类来源计数加分**；
    · src_ok=true（segments.integrity 的源检查）只证源检查通过，
      **不替代**作者身份、用途或质量证明——所以登记表独立于 integrity；
    · author_id=NULL 不是缺陷：metadata_status/metadata_basis 如实说明
      「无可靠考据来源，留空待补」。"""
    __tablename__ = "work_sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("WSRC"))
    work_id: Mapped[str] = mapped_column(String(32), ForeignKey("works.id"),
                                         unique=True, index=True)
    # 根作品 id：根作品=自身 work_id；镜像/派生=根的 work_id（回连同一根）
    canonical_work_id: Mapped[str] = mapped_column(String(32), index=True)
    author_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 已核对作者；NULL=未核对
    genre_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 来源类型：fixture / synthetic / commentary / human_fiction
    source_type: Mapped[str] = mapped_column(String(20), index=True)
    # 文本版本标签（corpus-v1 / corpus-v2-mirror / test-fixture…）；
    # seg_version 是**切分**版本，不等于「属于 corpus v2 镜像」——镜像判定
    # 只认 works.v2_of（方案 §4.1 明令）
    text_version: Mapped[str] = mapped_column(String(40))
    # 内容锚：按 ordinal 序拼接各段 text_clean（缺失用 text）后的 sha256——
    # 同一内容的不同切分/清洗版本可由版本与哈希区分对账
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 用途依据（§4.1）：**只证作者/题材署名可核对**——不构成任何授权
    purpose_basis: Mapped[str] = mapped_column(Text)
    # 身份与授权**拆开**（会审二轮 BLOCK 项：署名可核对 ≠ 用途授权）——
    # 身份可核对面（研究侧）：research / research_reference / test_contract
    identity_purposes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 训练/再分发**授权面**：training_source / benchmark_source——只有拿到
    # 集霸的显式授权记录才允许非空；未授权源一律不得进训练/基准用途
    license_purposes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 授权依据（license_purposes 非空时必填：授权人/日期/范围）
    license_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 遗留列（建表史）：真库 allowed_purposes JSON NOT NULL 无默认；K1-A 二轮
    # 把语义迁到 identity_purposes/license_purposes 时从模型删列——此后一切
    # **新插行**必炸（2026-09-23 首次新登记才暴露；存量 UPDATE 路径不受影响）。
    # 恢复为纯插行管道的带默认声明：语义仍看新列，本列不参与任何判定；
    # 测试库由本声明建表，两侧同构。
    allowed_purposes: Mapped[list] = mapped_column(JSON, default=list)
    metadata_status: Mapped[str] = mapped_column(String(20))   # verified/partial/unverified
    metadata_basis: Mapped[str] = mapped_column(Text)     # 元数据核对依据
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Segment(Base):
    """Human Anchor 的最小实验单位：1~10 句，句末/段末切分，不截断句中。"""

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("SEG"))
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    chapter: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # 清洗后的文本（拼音还原 / 站点水印剔除，见 scripts/clean_text.py）。
    # 原文 `text` **不动**：清洗是可审计、可重跑的一步；下游一律优先读 text_clean。
    text_clean: Mapped[str | None] = mapped_column(Text, nullable=True)
    n_sentences: Mapped[int] = mapped_column(Integer, default=0)
    n_chars: Mapped[int] = mapped_column(Integer, default=0)
    integrity: Mapped[str | None] = mapped_column(Text, nullable=True)  # T1 六指标 JSON
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)  # train/benchmark/None
    seg_version: Mapped[int] = mapped_column(Integer, default=1)  # 切分器版本（v2=Phase 1.5）
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


# ── corpus v2 镜像的单一判定口径（T-CORPUS-V2 / 会审①收口）────────────
# v2 = v1 的 TYPO_MAP 修复副本：同一内容在库里存在两份。基准只在 v1 侧维护，
# v2 段**永不**参与采样/建批/基准/训练导出——所有下游一律用下面两个入口判定，
# 不再各自硬编码标题字面量。

def corpus_v2_work_subq():
    """全部 corpus v2 镜像 Work 的 id 子查询（SQL 侧口径）。"""
    return select(Work.id).where(or_(
        Work.v2_of.isnot(None), Work.title.like(f"%{V2_TITLE_SUFFIX}%")))


def exclude_corpus_v2_segments():
    """Segment 查询用：排除属于 corpus v2 镜像作品的段。

        s.query(Segment).filter(...).filter(exclude_corpus_v2_segments())
    """
    return ~Segment.work_id.in_(corpus_v2_work_subq())


def is_corpus_v2_work(work: "Work | None") -> bool:
    """Python 侧判定（已拿到 Work 对象时用），与 exclude_corpus_v2_segments 同口径。"""
    return bool(work is not None
                and (work.v2_of or V2_TITLE_SUFFIX in (work.title or "")))


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="created")  # created/running/done/failed
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A07（审查 20260920-1810）：执行权领取凭据。「检查 running 再启动」
    # 不是原子操作（API 先查后启线程、CLI 与 API 并发、双请求竞争都读到
    # 阶段未完成）→ 阶段体执行 2 次、真实阶段重复生成重复计费。修复：
    # 条件 UPDATE 原子领取（只有把 status 翻成 running 的那一个赢），
    # owner/claimed_at 随领取写入；提交阶段结果前校验持有权。无自动
    # TTL 接管——数小时级长跑会被误抢，卡死用 release_run 显式释放。
    run_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_claimed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now, onupdate=_now)


class Frame(Base):
    __tablename__ = "frames"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("FR"))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), index=True)
    granularity: Mapped[str] = mapped_column(String(4))  # S/M/L
    extractor_model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok/failed/repaired
    is_primary: Mapped[bool] = mapped_column(default=True)  # 双抽时主抽取结果用于重建
    created_at: Mapped[str] = mapped_column(String(32), default=_now)

    __table_args__ = (Index("ix_frames_seg_gran", "segment_id", "granularity"),)


class LeakageScore(Base):
    __tablename__ = "leakage_scores"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("LK"))
    frame_id: Mapped[str] = mapped_column(ForeignKey("frames.id"), index=True)
    layer: Mapped[str] = mapped_column(String(20))  # char6/word3/rare/adversarial/embedding
    score: Mapped[float] = mapped_column(Float)  # 0~1，越高泄漏越重
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CND"))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    frame_id: Mapped[str] = mapped_column(ForeignKey("frames.id"), index=True)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), index=True)
    anon_label: Mapped[str] = mapped_column(String(12))  # X17 等，Judge 只能看到这个
    model: Mapped[str] = mapped_column(String(80))
    temperature: Mapped[float] = mapped_column(Float, default=0.8)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok/failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Proposition(Base):
    """Human 原文的原子语义命题分解结果；SemanticVerifier 的判据。"""

    __tablename__ = "propositions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("PP"))
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), index=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    propositions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class ResidualDet(Base):
    """确定性残差：Python 直接可算的指标差。"""

    __tablename__ = "residuals_det"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RD"))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # candidate 绝对值
    deltas: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # candidate - human
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class ResidualSem(Base):
    """语义残差：LLM 结构分析（潜台词/叙述距离/信息显隐…）。"""

    __tablename__ = "residuals_sem"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RS"))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class JudgeRun(Base):
    __tablename__ = "judge_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("JR"))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(20))  # candidate / pair
    subject_id: Mapped[str] = mapped_column(String(40), index=True)  # candidate_id 或 pair key
    judge_kind: Mapped[str] = mapped_column(String(20))  # semantic/naturalness/adversarial
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    abstain: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class ReviewItem(Base):
    """Active Human Review 队列：只把信息价值最高的样本给人看。"""

    __tablename__ = "review_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RV"))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(20))  # pair / candidate / frame
    subject_id: Mapped[str] = mapped_column(String(40))
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/done/skipped
    human_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    reviewed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ReviewPresentation(Base):
    """一次盲评「端题呈现」的不可变快照（审查 A01，2026-09-20）。

    为什么需要：旧实现只按 review_id 存一份**进程内**映射，同一题重出题会覆盖
    旧行——旧页面提交的 A/B 按新映射解读，用户偏好标签被静默污染（实测复现：
    第一页 A=原文、第二页 A=候选，第一页提交 A 被记成 candidate）。每次端题
    落一行：排列（human_first）、上下文口径、两侧文本指纹全部冻结；提交绑定
    presentation_id 按呈现当时的排列解读。DB 持久化 = 跨重启、多 worker 共识；
    旧客户端不带 id 时按该题最近一次持久化呈现回退。
    """
    __tablename__ = "review_presentations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("PR"))
    review_id: Mapped[str] = mapped_column(String(32), index=True)
    human_first: Mapped[bool] = mapped_column(Boolean)   # True=端出时 A 侧是人类原文
    ctx_mode: Mapped[str] = mapped_column(String(40), default="")
    text_a_sha: Mapped[str] = mapped_column(String(16))  # 呈现冻结的两侧文本指纹
    text_b_sha: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("LC"))
    experiment_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)  # 中转单价未知→None
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A05（审查 20260920-1810）：账本分列「逻辑调用」与「HTTP 尝试」两个口径。
    # 一次 chat() = 一个逻辑调用（logical_call_id 分组，同组 attempt_no 0..N-1）；
    # 重试环里**每次 HTTP 派发各自留痕**（含失败/被吞的那几次）。旧实现只在
    # 最终结果落一行——重试期烧掉的 token 与失败率在账上凭空消失（实测复现：
    # 两次上游请求 45 token，账上只有一条成功 15 token）。历史行两列为
    # NULL，报表口径按「每行即一个逻辑调用」回退（coalesce(lcid, id)）。
    logical_call_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("JOB"))
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now, onupdate=_now)


class ReportFile(Base):
    __tablename__ = "report_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RP"))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # calibration_md / calibration_json
    path: Mapped[str] = mapped_column(String(400))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)



class ExpressionStrategy(Base):
    """表达策略库（总方案 §4.4 / 工作流 D）。

    由 human↔candidate 的表达残差自动聚类 + LLM 归纳产出，**不是人工定义的固定清单**
    （§9：不得预设只有固定策略）。success_rate 是该簇里集霸判「候选胜」的比例，
    用来回答方案 §52 的问题——「哪些表达策略在什么语义条件下更好」。
    """

    __tablename__ = "expression_strategies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ES"))
    name: Mapped[str] = mapped_column(String(120))         # 策略名（LLM 归纳）
    description: Mapped[str] = mapped_column(Text, default="")
    conditions: Mapped[list] = mapped_column(JSON, default=list)      # 适用条件
    recommended: Mapped[list] = mapped_column(JSON, default=list)     # 建议做法
    avoid: Mapped[list] = mapped_column(JSON, default=list)           # 该避免的写法
    examples: Mapped[list] = mapped_column(JSON, default=list)        # 代表例（人类侧/AI侧）
    counter_examples: Mapped[list] = mapped_column(JSON, default=list)
    n_items: Mapped[int] = mapped_column(Integer, default=0)          # 簇大小
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)   # 该簇里候选胜率
    rate_lo: Mapped[float] = mapped_column(Float, default=0.0)        # Wilson 下界
    rate_hi: Mapped[float] = mapped_column(Float, default=0.0)
    distribution: Mapped[dict] = mapped_column(JSON, default=dict)    # 语料/作品分布
    method: Mapped[str] = mapped_column(String(60), default="")      # 产出方法（聚类口径）
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(120), default="")     # provenance
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now, onupdate=_now)


# ══════════════════════════════════════════════════════════════════
# K1-B：v2 知识契约五核心结构（知识化调整方案 §4.2/§4.3，2026-09-21）
#
# 与 v1（expression_strategies，偏好聚类产物）的关系：v1 只是 legacy
# 假设来源，默认不作为 v2 已验证查询结果——旧 8 条经
# scripts/migrate_strategies_v2.py 转成待验证假设（status=hypothesis），
# success_rate 不复制、不覆盖、不重解释（迁移纪律 §4.4）。
# 迁移只增不删：v2 五张表全部为新增，回滚 = DROP 这五张表，
# v1 表与旧 API 不受任何影响（回归 tests/test_knowledge_v2.py 钉死）。
# ══════════════════════════════════════════════════════════════════

class ExpressionStrategyV2(Base):
    """表达策略 v2（§4.2）：抽象操作 + 保持不变项 + 目标效果假设 + 失败模式。

    与 v1 的本质差异：无 cand_won 质量门（v1 的 success_rate 是偏好胜率，
    不是表达效果证明，不进 v2 身份字段）；三类证据层次**分开记录**
    （§4.3）：scope（范围）/ observation_status（观察）/ effect_status
    （效果）——单部作品只支持该作品的观察，不自动升级（WORK→AUTHOR→
    GENRE→GLOBAL 每级都要独立依据；GLOBAL 本阶段只预留契约不自动授予）。
    版本身份：strategy_key（跨版本稳定自然键）+ version；错误版本经
    retired/superseded 处理，不删除。"""
    __tablename__ = "expression_strategies_v2"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("ESV2"))
    strategy_key: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    abstract_operation: Mapped[str] = mapped_column(Text)      # 抽象操作
    invariants: Mapped[list] = mapped_column(JSON, default=list)  # 保持不变项
    effect_hypothesis: Mapped[str] = mapped_column(Text)       # 目标效果假设
    failure_modes: Mapped[list] = mapped_column(JSON, default=list)  # 失败模式
    status: Mapped[str] = mapped_column(String(20), default="hypothesis")
    source: Mapped[str] = mapped_column(String(120), default="")   # 来源/方法版本
    # v1 血缘（迁移表记录 old ID → 新版本及采用依据，§4.4）
    legacy_strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # §4.3 三分：范围（带具体范围 ID 与依据）
    scope: Mapped[str] = mapped_column(String(12), default="UNCERTAIN")
    scope_ids: Mapped[list] = mapped_column(JSON, default=list)
    scope_basis: Mapped[str] = mapped_column(Text, default="")
    # 观察支持：hypothesis=尚无合格实例 / observed=有已核对实例 /
    # replicated=冻结假设后在独立来源复现（并记反例/缺失）
    observation_status: Mapped[str] = mapped_column(String(12), default="hypothesis")
    # 效果证据：untested / pilot_verified / quality_supported——必须绑定
    # 具体任务、对照、样本单位和适用条件；v1 旧枚举只有这三值，映射保守
    effect_status: Mapped[str] = mapped_column(String(20), default="untested")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class StrategyInstance(Base):
    """策略实例证据（§4.2）：必须能机械核对
    text_version[span_start:span_end] == 存证文本——核对函数在
    app/knowledge.py::verify_instance_span（入库与测试共用同一实现；
    schema 合格不代表推断成立，语义归纳须单独审查）。

    Frame 必须属于同一来源版本；缺失时显式为 null，不伪造关联。
    重复抽取/多模型对同一区间的观察分别保留——统计独立证据时按
    来源区间/根作品聚合（strategy_stats 的职责），不靠重跑加置信度。"""
    __tablename__ = "strategy_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("SI"))
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    strategy_version: Mapped[int] = mapped_column(Integer, default=1)
    work_id: Mapped[str] = mapped_column(String(32))
    segment_id: Mapped[str] = mapped_column(String(32))
    frame_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text_version: Mapped[str] = mapped_column(String(40))
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    evidence_text: Mapped[str] = mapped_column(Text)
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    conditions_observed: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_content: Mapped[str] = mapped_column(Text)
    effect_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    extractor_model: Mapped[str] = mapped_column(String(80))
    reviewer_version: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(12), default="proposed")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class StrategyCondition(Base):
    """策略条件谓词（§4.2）：good_when/bad_when/neutral_when 三态清单。

    谓词允许 true/false/**unknown**（§4.3：缺证据不是假——unknown 显式
    三值，不许把未知折叠成 false）。"""
    __tablename__ = "strategy_conditions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("SC"))
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    strategy_version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(12))
    dimension: Mapped[str] = mapped_column(String(60))
    operator: Mapped[str] = mapped_column(String(20))
    value: Mapped[dict] = mapped_column(JSON)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    predicate_state: Mapped[str] = mapped_column(String(8), default="unknown")
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class StrategyStats(Base):
    """策略统计（§4.2）：按数据快照生成的**可重建投影**——不是第二份真值；
    独立证据按来源区间/根作品聚合（镜像/重切段/重复抽取不计独立复现）。"""
    __tablename__ = "strategy_stats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("SS"))
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    strategy_version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot_at: Mapped[str] = mapped_column(String(32))
    data_fingerprint: Mapped[str] = mapped_column(String(64))
    unique_source_intervals: Mapped[int] = mapped_column(Integer, default=0)
    root_works: Mapped[int] = mapped_column(Integer, default=0)
    known_authors: Mapped[int] = mapped_column(Integer, default=0)
    genres: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    valid: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[int] = mapped_column(Integer, default=0)
    counter_examples: Mapped[int] = mapped_column(Integer, default=0)
    extras: Mapped[dict] = mapped_column(JSON, default=dict)


class StrategyReview(Base):
    """席位评审留痕（K2-CARDS 2026-09-23；真表由主控判定器先建，此处补
    ORM 映射——列型逐列对齐既有 DDL，测试库由本声明建表同构）。
    judge_kind：semantic_card=8 张 legacy 卡 × 3 席独立判定（24 行，
    24/24 证据不支持、0 条 verified）；semantic_card_v2=合并新卡 × 成对
    对照证据的席位判定。**判定≠升格**：本表只留痕；status 只在 ≥2 独立席
    PASS 后按纪律改，禁批量升格。evidence_support/distinct_flag 用 0/1。"""
    __tablename__ = "strategy_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(32), index=True)
    judge_kind: Mapped[str] = mapped_column(String(32))
    reviewer_model: Mapped[str] = mapped_column(String(80))
    verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence_support: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distinct_flag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sufficiency: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class KnowledgeLink(Base):
    """知识边（§4.2）：supports / contradicts / refines / alternative_of——
    绑定双方 ID、版本、证据与创建依据；可指向 Distiller 机制
    （to_kind='distiller_mechanism'），**不复制机制原文**（引用其 id）。"""
    __tablename__ = "knowledge_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("KL"))
    link_kind: Mapped[str] = mapped_column(String(16))
    from_kind: Mapped[str] = mapped_column(String(24))
    from_id: Mapped[str] = mapped_column(String(32))
    from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_kind: Mapped[str] = mapped_column(String(24))
    to_id: Mapped[str] = mapped_column(String(32))
    to_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    basis: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class KnowledgePackage(Base):
    """K3-A 冻结知识包（方案 §6.2 每场冻结；K3-B 在 Runtime prepare 前写入）。

    内容寻址：package_sha256 = sha256(policy 规范化 + 选中策略 id/版本序列
    + 库知识快照指纹)——同策略同快照同 policy 必得同包（幂等）。
    包内容**不含任何原文**（§4.2：Writer 只得到抽象操作、条件、例外和
    不含原文的引用）——evidence 只留 instance id 与 canonical 来源引用。
    K3-A 的 HTTP 只读（GET packages）；写入由 Runtime 冻结流程（K3-B）。"""
    __tablename__ = "knowledge_packages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                   default=lambda: new_id("KPKG"))
    package_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    policy_sha256: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(JSON)          # 规范化后的查询 policy
    selected: Mapped[list] = mapped_column(JSON)        # 选中策略（含版本与分量明细）
    rejected_summary: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64))
    contract_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class ControlledCorruption(Base):
    """受控劣化数据集（总方案 §4.6 / §7 主工作流 B / §8 Minimal Pair）。

    从 Human Anchor 出发，按**单一变量**制造劣化版本：每次只改一个语言变量
    （显式化 / 过度解释 / 情绪直说 / 节奏拉平 …），事实层强制不变，
    语义漂移由**另一个模型**校验（§11.1 SemanticVerifier 口径：只判信息，不判文采）。

    为什么它是本项目最缺的那块：
      · 现有盲评全是"人类原文 vs 模型重建"，两者差**很多**变量，赢了输了都不知道是哪个变量赢的；
        corruption 是**对照组**——除一个变量外其余全同。
      · 它自带已知答案：劣化版一般应当输（§7.5 允许反例 → 反例正是最有信息量的一类）。
        于是它同时是**训练数据**（human=chosen / corruption=rejected 的 DPO pair）
        和**仪器的可判别性标尺**（连单变量劣化都测不出来的评委，不足以支撑任何 κ 结论）。

    候选落库走 `candidates`（experiment_id=劣化实验，prompt_version='corrupt_v1'），
    因此免费继承盲评 / 评委 / 统计管线。**与主池隔离**：`corrupt_v1` 不在
    `BLIND_REVIEW_PROMPT_VERSIONS` 里，随机抽样池永远捞不到它们（回归测试锁定）。
    """

    __tablename__ = "controlled_corruptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("CC"))
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), index=True)
    frame_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    corruption_type: Mapped[str] = mapped_column(String(40), index=True)
    variable: Mapped[str] = mapped_column(String(200), default="")  # 本次唯一改变的变量（可读）
    generator_model: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="corrupt_v1")
    text: Mapped[str] = mapped_column(Text, default="")
    gen_note: Mapped[str] = mapped_column(Text, default="")  # 生成器自述"改了哪一处"（审计单变量承诺）
    n_chars: Mapped[int] = mapped_column(Integer, default=0)
    len_ratio: Mapped[float] = mapped_column(Float, default=0.0)   # 变体/原文 字数比
    drift: Mapped[dict] = mapped_column(JSON, default=dict)        # 校验器原始输出
    drift_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0~1，越高漂移越大
    fact_consistent: Mapped[bool] = mapped_column(Boolean, default=False)
    drift_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_model: Mapped[str] = mapped_column(String(80), default="")
    verify_pv: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(24), default="ok", index=True)
    # ok / rejected_drift / rejected_length / failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class HardCase(Base):
    """硬例库（总方案 §10 工作流 E）。

    进入条件（方案原文）：评委分歧大 / 集霸偏好≠Reward Model / **人类原文被评委大量判输** /
    多模型分布异常 / 语义高分但自然度极低 / 自然度高但语义漂移 / 模型反复失败。

    每条硬例按方案要求带：失败原因、争议点、可能缺失的特征、是否需要人工判断、
    是否需要新增 Ontology、是否需要新增 ExpressionStrategy。
    """

    __tablename__ = "hard_cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("HC"))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    experiment_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)   # 命中的进入条件（可多条，逗号分隔）
    severity: Mapped[float] = mapped_column(Float, default=0.0)  # 严重度（口径见脚本）
    user_verdict: Mapped[str] = mapped_column(String(20), default="")
    judge_votes: Mapped[dict] = mapped_column(JSON, default=dict)  # 各评委站哪边
    n_judges: Mapped[int] = mapped_column(Integer, default=0)
    why: Mapped[str] = mapped_column(Text, default="")            # 失败原因
    dispute: Mapped[str] = mapped_column(Text, default="")        # 争议点
    missing_features: Mapped[list] = mapped_column(JSON, default=list)
    need_human: Mapped[bool] = mapped_column(Boolean, default=False)
    need_ontology: Mapped[bool] = mapped_column(Boolean, default=False)
    need_strategy: Mapped[bool] = mapped_column(Boolean, default=False)
    strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 关联策略
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)

class BenchmarkSet(Base):
    """基准集（总方案 §14）。

    §14 的硬要求：必须存在**完全独立**的 Hidden Benchmark，不得被
    训练 / Prompt 优化 / 策略发现 / 人工调参读取。实现上靠 `Segment.role='benchmark'`：
    基准段的文本不参与任何训练导出（`export_training.py` 已按 role 过滤）。

    条目**冻结文本**（a/b 原文存在 item 上）而不是只存 segment_id：
    语料清洗、切分器升级都会改变 segment 的内容，冻结后同一版基准的分数才可跨时间比较。
    """

    __tablename__ = "benchmark_sets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("BS"))
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(40), index=True)   # corruption_detection / ...
    n_items: Mapped[int] = mapped_column(Integer, default=0)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)      # 构建口径（可复现）
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class BenchmarkItem(Base):
    __tablename__ = "benchmark_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("BI"))
    set_id: Mapped[str] = mapped_column(ForeignKey("benchmark_sets.id"), index=True)
    segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    context: Mapped[str] = mapped_column(Text, default="")      # 冻结的上文
    text_a: Mapped[str] = mapped_column(Text)                   # 冻结的 A（位置随机化结果）
    text_b: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(String(12))             # "A" / "B"（哪边是标准答案）
    meta: Mapped[dict] = mapped_column(JSON, default=dict)      # 类型/变量/来源等
    created_at: Mapped[str] = mapped_column(String(32), default=_now)

    __table_args__ = (Index("ix_benchmark_items_set_kind", "set_id", "kind"),)


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("BR"))
    set_id: Mapped[str] = mapped_column(ForeignKey("benchmark_sets.id"), index=True)
    model: Mapped[str] = mapped_column(String(80), index=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    n_correct: Mapped[int] = mapped_column(Integer, default=0)
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)   # 逐条结果 + 分类型
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)

