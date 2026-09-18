"""数据表定义。面向 Calibration Lab 第一阶段，保持薄而完整；全部带 created_at 审计字段。

粒度枚举：frame.granularity ∈ {S, M, L}
候选匿名化：Judge prompt 只见 candidate.anon_label，模型名只存在 DB。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON  # SQLite/Postgres 均可用 JSON 普通列
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .ids import new_id


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


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="created")  # created/running/done/failed
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)



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

