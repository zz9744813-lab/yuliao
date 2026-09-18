# Language Genome：中文小说语言学习与优化系统完整工程方案

> 目标：构建一套长期可演化的中文小说语言研究、数据沉淀、评测、训练与生成系统。  
> 核心研究对象仅聚焦于：**语义、语句、语感、表达策略、潜台词、节奏、自然度与文体一致性**。  
> 暂不承担完整人物塑造、剧情设计、世界观推演等上层叙事任务。  
> 系统最终必须能够回答：**“在相同语义与上下文条件下，人类作者为什么这样写，而 AI 为什么常常不会这样写？”**

---

# 0. 总体要求

本系统不得做成简单的“AI 去味审核器”，也不得以“模型打分高”作为最终质量标准。

系统必须围绕以下闭环建立：

```text
Human Corpus
    ↓
Semantic Distillation
    ↓
隐藏原文
    ↓
Multi-model Reconstruction
    ↓
揭晓原文
    ↓
Contrastive Analysis
    ↓
Expression Residual
    ↓
Pattern Discovery
    ↓
Preference Dataset
    ↓
Reward Model / Verifier
    ↓
Writer / Rewriter
    ↓
再次挑战隐藏 Human Corpus
```

系统的长期资产必须包括：

1. Human Anchor Corpus
2. SemanticFrame 数据集
3. Human ↔ AI 表达差异数据
4. Expression Residual 数据
5. Controlled Corruption 数据
6. Pairwise Preference 数据
7. Rewrite 数据
8. Hard Case 数据
9. Language Error Ontology
10. Expression Strategy Library
11. Literary Reward Model 训练集
12. Writer / Rewriter 训练集
13. Hidden Benchmark
14. 模型能力矩阵
15. Prompt / Model / Dataset / Experiment 版本链
16. 所有实验的可复现结果

任何生成结果都必须可追溯到：

```text
source
dataset_version
semantic_frame_version
prompt_version
model
model_version
temperature
seed
workflow_version
judge_version
experiment_id
timestamp
```

---

# 1. 核心设计原则

## 1.1 Human Anchor 永远高于 Synthetic Data

Human Anchor 必须作为不可替代的真实参照，不允许被后续 AI 数据覆盖。

要求：

```text
Human Anchor = immutable
AI Synthetic = append-only
```

Human Anchor 只能增加、纠错、补充元数据，不能被 AI 生成文本替换。

---

## 1.2 先研究“表达差异”，再研究“谁更好”

系统不得直接把任务简化成：

```text
Human = 好
AI = 坏
```

第一步必须分析：

```text
相同语义条件下：

Human 使用了什么表达策略？
AI 使用了什么表达策略？
二者的差异发生在哪一层？
```

差异分析至少覆盖：

- 信息显露程度
- 心理显式化程度
- 行为承载语义比例
- 对话承载语义比例
- 句法复杂度
- 词汇抽象度
- 修饰密度
- 时序编码
- 因果连接方式
- 情绪命名方式
- 潜台词强度
- 留白程度
- 节奏
- 句群结构
- 段落推进方式
- 叙述距离
- POV 贴合程度

---

## 1.3 语义与语言实现必须分离

禁止直接：

```text
剧情 → 正文
```

必须存在中间表示：

```text
Narrative Intent
    ↓
SemanticFrame
    ↓
Expression Strategy
    ↓
Sentence / Paragraph Realization
```

SemanticFrame 必须足够表达“意思”，但不能泄漏人类原句的具体措辞。

---

## 1.4 不允许一个 Judge 决定一切

不得设计唯一“文学大师 Judge”。

所有评价必须解耦。

至少独立存在：

```text
SemanticVerifier
NaturalnessVerifier
PragmaticVerifier
RhythmVerifier
DialogueVerifier
StyleVerifier
ClicheDetector
AI-Likeness Detector
AdversarialVerifier
HumanPreferenceModel
```

任何 Judge 都必须有：

- version
- calibration set
- agreement rate
- bias report
- failure cases
- confidence
- abstain / uncertain 能力

---

## 1.5 工作流是骨架，模型只是节点

模型不得掌控全局流程。

整体必须由 Workflow Engine 控制：

```text
Workflow
├─ 数据采样
├─ SemanticFrame 抽取
├─ Frame 校验
├─ 原文隐藏
├─ 候选生成
├─ 匿名化
├─ 对照分析
├─ Judge
├─ Preference
├─ Hard Case Mining
├─ 数据入库
├─ Benchmark
└─ 实验统计
```

LLM 只能承担局部智能节点。

---

## 1.6 Token 应用于扩大“搜索空间”，不是无限审稿

预算充足时，优先增加：

- 语义样本数量
- 多模型数量
- 每个模型候选数量
- 采样温度与表达策略数量
- Controlled Corruption 类型
- 对照实验数量
- Hard Case 数量
- Benchmark 数量
- Human Preference 数量

禁止主要通过“同一段文字反复改 20 次”消耗 Token。

---

# 2. 系统总体架构

```text
┌──────────────────────────────────────────────┐
│               Web Frontend                   │
│ React + TypeScript                           │
└──────────────────────┬───────────────────────┘
                       ↓
┌──────────────────────────────────────────────┐
│                FastAPI API                   │
│ Auth / Dataset / Experiment / Workflow       │
└──────────────────────┬───────────────────────┘
                       ↓
┌──────────────────────────────────────────────┐
│              Workflow Engine                 │
│ 推荐：Temporal                               │
│ 长任务 / 重试 / 分支 / 状态持久化           │
└──────────────┬───────────────┬───────────────┘
               ↓               ↓
      ┌────────────────┐  ┌──────────────────┐
      │ Worker Pool A  │  │ Worker Pool B    │
      │ CPU / Data     │  │ LLM / Judge      │
      └────────────────┘  └──────────────────┘
               ↓               ↓
┌──────────────────────────────────────────────┐
│              Model Gateway                   │
│ OpenAI-compatible unified provider layer     │
└──────────────────────┬───────────────────────┘
                       ↓
 DeepSeek / Muse / GPT / Claude / Gemini / Qwen / GLM

┌──────────────────────────────────────────────┐
│ PostgreSQL                                   │
│ metadata / experiments / results / versions  │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│ Object Storage                               │
│ raw corpus / large JSONL / training data     │
│ local disk initially, S3/MinIO later         │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│ Redis                                        │
│ cache / lightweight queue / rate limit       │
└──────────────────────────────────────────────┘
```

---

# 3. 推荐技术栈

## 3.1 后端

```text
Python 3.12+
FastAPI
SQLAlchemy 2.x
Pydantic v2
PostgreSQL 16+
Temporal Python SDK
Redis
Alembic
httpx
orjson
polars
pyarrow
```

数据分析优先使用：

```text
Polars
DuckDB
PyArrow
```

不要把百万级实验数据全部塞进 pandas。

---

## 3.2 前端

```text
React
TypeScript
Vite
TanStack Router
TanStack Query
Zustand
React Flow
ECharts
Monaco Editor
Tailwind CSS
shadcn/ui
```

---

## 3.3 数据格式

训练和交换格式：

```text
JSONL
Parquet
Arrow
```

数据库只保存索引、元数据、状态、关键结构。

超大候选文本、训练集、Benchmark 快照使用：

```text
Parquet / JSONL + Object Storage
```

---

# 4. 核心数据对象

# 4.1 HumanSource

```json
{
  "source_id": "HS_000001",
  "work_id": "WORK_001",
  "author": "",
  "title": "",
  "genre": "",
  "subgenre": "",
  "era": "",
  "license_status": "",
  "source_type": "human",
  "text_span": "",
  "chapter": "",
  "paragraph_index": 0,
  "pov": "",
  "quality_tags": [],
  "created_at": ""
}
```

要求：

- HumanSource 不可被 AI 自动覆盖
- 必须保存原始出处和位置
- 必须支持完整回查
- 必须支持删除与版权隔离
- 必须记录是否允许用于训练

---

# 4.2 SemanticFrame

SemanticFrame 是整个系统最重要的数据结构之一。

建议至少包含：

```yaml
semantic_frame_id:

surface_event:
  actor:
  action:
  target:
  object:
  location:
  time:

facts:
  - statement:
    certainty:
    source:

character_state:
  visible_emotion:
  hidden_emotion:
  goal:
  short_term_intention:
  long_term_intention:
  knowledge:
  beliefs:
  uncertainty:

reader_state:
  must_know:
  should_infer:
  must_not_know:
  ambiguity_to_preserve:

pragmatics:
  subtext:
  social_pressure:
  relationship_distance:
  power_relation:
  politeness_level:
  deception:
  concealment:

expression_constraints:
  explicitness_target:
  psychological_explanation_allowed:
  dialogue_allowed:
  action_allowed:
  narration_distance:
  pov:
  tense:
  rhythm_target:

semantic_invariants:
  - 不允许改变的事实

semantic_flexibility:
  - 允许语言层自由处理的部分

forbidden_leakage:
  - 不允许直接说出的信息
```

SemanticFrame 必须经过：

```text
Extractor
↓
Verifier
↓
Leakage Check
↓
Human Original Similarity Check
```

如果 Frame 中出现过多原文措辞，需要自动降级或重新抽取。

---

# 4.3 Candidate

```json
{
  "candidate_id": "",
  "semantic_frame_id": "",
  "generator_model": "",
  "model_version": "",
  "prompt_version": "",
  "temperature": 0.8,
  "seed": 0,
  "strategy_id": "",
  "text": "",
  "token_input": 0,
  "token_output": 0,
  "latency_ms": 0,
  "cost": 0
}
```

---

# 4.4 ExpressionStrategy

建立独立表达策略层。

示例：

```yaml
strategy_id: ES_0012
name: 动作暗示认知
description:
conditions:
  - 情绪强度低
  - 不希望直接解释心理
  - 读者可从动作推断
recommended:
  - 使用动作时序
  - 使用停顿
  - 使用行为选择
avoid:
  - 直接说“他意识到”
examples:
counter_examples:
success_rate:
genre_distribution:
```

系统必须允许自动发现新策略，而不是只允许人工定义。

---

# 4.5 ExpressionResidual

用于记录 Human 与 Candidate 在相同语义条件下的差异。

```yaml
residual_id:

human_text:
candidate_text:

differences:
  explicitness:
  psychological_labeling:
  action_semantics:
  dialogue_semantics:
  syntactic_complexity:
  modifier_density:
  abstraction_level:
  temporal_encoding:
  causal_encoding:
  rhythm:
  redundancy:
  subtext:
  pov_distance:
  narrative_distance:

analysis:
  human_choices:
  ai_choices:
  lost_information:
  added_information:
  semantic_drift:
  likely_reason:

confidence:
```

---

# 4.6 ControlledCorruption

从 Human Anchor 自动制造可控劣化版本。

类型至少包括：

```text
EXPLICITIZE
OVER_EXPLAIN
EMOTION_LABEL
PSYCHOLOGY_LABEL
LITERARY_OVERWRITE
ADJECTIVE_INFLATION
ADVERB_INFLATION
LOGIC_CONNECTOR_INFLATION
REDUNDANCY
PARALLELISM_OVERUSE
ABSTRACT_SUMMARY
MICRO_EXPRESSION_TEMPLATE
DIALOGUE_EXPOSITION
POV_DRIFT
SEMANTIC_OVERCOMPLETION
RHYTHM_FLATTEN
SUBTEXT_ERASE
```

每次劣化只能改变一个或少数变量，方便做控制实验。

---

# 4.7 PreferencePair

```json
{
  "pair_id": "",
  "semantic_frame_id": "",
  "candidate_a": "",
  "candidate_b": "",
  "winner": "A",
  "preference_source": "human|judge|rm|hybrid",
  "dimensions": {
    "semantic": 0,
    "naturalness": 0,
    "implicitness": 0,
    "rhythm": 0,
    "style": 0,
    "overall": 0
  },
  "confidence": 0,
  "reason_codes": [],
  "judge_disagreement": 0
}
```

---

# 5. 数据库表

至少建立：

```text
human_sources
human_segments
semantic_frames
semantic_frame_versions
expression_strategies
candidate_generations
candidate_batches
controlled_corruptions
expression_residuals
judge_runs
judge_scores
pairwise_preferences
human_preferences
hard_cases
benchmark_sets
benchmark_items
benchmark_runs
model_registry
model_versions
prompt_registry
prompt_versions
workflow_versions
experiments
experiment_variants
experiment_runs
experiment_results
training_exports
reward_models
writer_models
system_events
cost_records
```

所有表必须有：

```text
created_at
updated_at
version
source/provenance
```

---

# 6. 主工作流 A：Human Reconstruction

这是系统最核心的工作流。

## Step 1：Human Segment 采样

从 Human Corpus 抽取：

```text
1～10句
```

优先保持：

- 完整语义单元
- 完整动作
- 完整对话轮次
- 完整微场景

不得随意从句中间截断。

---

## Step 2：SemanticFrame 抽取

至少使用两个不同模型独立抽取。

然后执行：

```text
Frame A
Frame B
↓
Semantic Merge
↓
Semantic Verifier
```

如果关键事实冲突，则进入：

```text
Hard Frame Review
```

---

## Step 3：Frame Leakage 检查

必须检测：

- 是否复制原文词序
- 是否保留独特措辞
- 是否保留罕见短语
- 是否已经接近 paraphrase
- 是否可以通过 Frame 直接还原原句

若泄漏过高：

```text
重新抽取
```

---

## Step 4：隐藏 Human Original

从后续 Generator 输入中彻底删除人类原文。

后续模型只允许看到：

```text
SemanticFrame
Context Metadata
Style Constraints
```

---

## Step 5：Multi-model Reconstruction

默认候选策略：

```text
8 个模型
×
8 个采样
=
64 candidates / frame
```

可按实验调整。

推荐覆盖：

```text
DeepSeek
Muse
Claude
GPT
Gemini
Qwen
GLM
其他开放模型
```

每个模型必须至少覆盖：

```text
temperature:
0.3
0.6
0.9
1.1
```

同时要求部分候选：

- 不指定表达策略
- 指定不同 ExpressionStrategy
- 指定不同显隐程度
- 指定不同句长节奏

---

## Step 6：匿名化

Judge 不得看到：

```text
模型名称
模型供应商
prompt
是否 Human
是否 Rewrite
```

统一转为：

```text
Candidate X1
Candidate X2
...
```

顺序随机化。

---

## Step 7：Reveal Human

候选全部生成完成后才允许 Human Original 进入对照分析。

---

## Step 8：Contrastive Analysis

不先问“谁好”。

必须先抽取：

```text
Human 与 AI 的表达差异
```

每个 Candidate 至少生成一份结构化 residual。

---

## Step 9：Preference Evaluation

评估：

```text
Human vs AI
AI vs AI
Human vs ControlledCorruption
```

需要支持：

```text
A/B
Ranking
Best-of-N
Tournament
```

---

## Step 10：Pattern Mining

批量统计：

```text
相同语义条件下
Human 选择了什么
AI 选择了什么
```

形成：

```text
Expression Distribution Gap
```

---

# 7. 主工作流 B：Controlled Corruption

Human 文本自动制造劣化版本。

示例：

```text
Human:
他把茶喝完，才起身。
```

生成：

```text
Explicit:
他虽然想离开，却不愿表现得慌张，于是把茶喝完后才起身。

Literary:
他将杯中残茶缓缓饮尽，这才不疾不徐地起身。

Emotional:
他神色平静地喝完茶，从容起身。

Explanation:
他这样做，是为了不让别人看出自己的退意。
```

要求：

1. 每次只改变一个主要变量
2. 保持核心语义尽量不变
3. 自动验证是否产生语义漂移
4. 自动生成 Preference Pair
5. 必须允许反例：某些 corruption 在特定语境下可能并非劣化

---

# 8. 主工作流 C：Minimal Pair 实验

目标：找语言变量的因果影响。

例如只改变：

```text
显式心理解释
```

生成：

```text
V0: 无心理解释
V1: 轻度心理解释
V2: 中度心理解释
V3: 高度心理解释
```

其他变量保持尽量一致。

对每组运行：

```text
Human Preference
Naturalness
Semantic Fidelity
Implicitness
Rhythm
```

最终建立：

```text
Variable → Quality Response Curve
```

---

# 9. 主工作流 D：Expression Strategy Discovery

从大量 Human / AI residual 中自动聚类。

步骤：

```text
Residual Embedding
↓
Clustering
↓
Cluster Summarization
↓
Strategy Candidate
↓
Human / Strong Judge Review
↓
ExpressionStrategy 入库
```

系统需要自动发现：

```text
动作暗示
对话回避
反应延迟
时序压缩
环境代理
轻度心理
认知直述
省略
节奏断裂
反问替代解释
```

不得预设只有固定策略。

---

# 10. 主工作流 E：Hard Case Mining

进入 Hard Case 的条件：

```text
Judge disagreement > threshold
Human preference ≠ Reward Model
Human original 被 Judge 大量判输
多模型结果分布异常
语义评分高但自然度极低
自然度高但语义发生漂移
模型反复失败
```

Hard Case 必须单独进入：

```text
hard_cases
```

并拥有：

- 失败原因
- 当前解释
- 争议点
- 可能缺失的特征
- 是否需要人工判断
- 是否需要新增 Ontology
- 是否需要新增 ExpressionStrategy

---

# 11. Judge / Verifier 体系

# 11.1 SemanticVerifier

只负责：

- 事实一致性
- 信息增加
- 信息丢失
- 语义漂移
- 指代
- 因果
- 时序
- 人物知道/不知道什么

禁止评价文采。

---

# 11.2 NaturalnessVerifier

负责：

- 中文自然度
- 是否像成熟作者自然表达
- 句式是否僵硬
- 机械解释
- 模板化表达
- 书面腔
- AI 常见过度完整

---

# 11.3 PragmaticVerifier

负责：

- 潜台词
- 留白
- 语用
- 社会关系
- 礼貌
- 权力关系
- 言外之意
- 信息隐藏

---

# 11.4 RhythmVerifier

负责：

- 长短句
- 停顿
- 标点
- 句群节奏
- 段落呼吸
- 叙述与对白切换
- 重复结构

---

# 11.5 DialogueVerifier

负责：

- 对白自然度
- 是否信息倾倒
- 人物是否“为了告诉读者而说话”
- 回答是否过度完整
- 对话是否缺乏互动性

---

# 11.6 StyleVerifier

负责：

- 文体一致性
- 叙述距离
- POV
- 时代感
- 题材适配
- 风格漂移

---

# 11.7 AdversarialVerifier

专门攻击其他 Judge。

任务：

```text
找出为何当前评分可能是错的
找反例
判断是否存在长度偏好
判断是否偏好华丽表达
判断是否偏好解释完整
判断是否有模型家族偏好
```

---

# 12. Judge 可靠性评估

每个 Judge 必须维护：

```text
human_agreement
pairwise_accuracy
calibration_error
false_positive
false_negative
genre_bias
length_bias
style_bias
model_family_bias
```

低可靠 Judge 不允许参与高权重决策。

---

# 13. Preference Aggregator

禁止简单平均。

输入：

```text
多个 Judge 分数
Judge 历史可靠性
Judge confidence
Human preference
样本类型
题材
```

输出：

```text
final preference probability
```

后期可以训练专门 Aggregator。

---

# 14. Benchmark 体系

必须存在完全独立的 Hidden Benchmark。

不得被：

```text
训练
Prompt 优化
策略发现
人工调参
```

直接读取。

Benchmark 至少包含：

```text
Semantic Fidelity
Naturalness
Implicitness
Pragmatics
Dialogue
Rhythm
Style
Human-vs-AI Discrimination
Human Preference Prediction
Reconstruction Quality
Controlled Corruption Detection
Hard Case
```

---

# 15. Human Preference

系统必须支持人工盲评。

前端只显示：

```text
A
B
```

不显示：

- 模型
- Human / AI
- Prompt
- 分数

用户操作：

```text
A 更好
B 更好
差不多
都不好
无法判断
```

并允许选原因：

```text
更自然
更简洁
更有余味
信息更准确
节奏更好
对话更真实
更符合文体
更像真人
```

---

# 16. Experiment Engine

所有修改必须通过 Experiment。

示例：

```yaml
experiment_id: EXP-000184

hypothesis:
  reader_should_infer 字段可以减少显式心理解释

control:
  semantic_frame_v6

variant:
  semantic_frame_v7

dataset:
  hidden_set_v3

models:
  - muse
  - deepseek

samples:
  10000

metrics:
  semantic_fidelity:
  naturalness:
  implicitness:
  human_preference:

status:
```

---

# 17. 实验必须支持的类型

```text
A/B
A/B/C
Factorial
Ablation
Cross-model
Cross-prompt
Cross-style
Cross-genre
Temperature sweep
Strategy sweep
Judge ablation
Frame ablation
Human-vs-AI
Before-vs-after training
```

---

# 18. 前端总体设计

前端不是“日志面板”，而是研究工作台。

导航：

```text
Dashboard
Corpus
Semantic Lab
Reconstruction Arena
Expression Residual
Strategy Atlas
Judge Arena
Preference Lab
Hard Cases
Benchmarks
Experiments
Models
Training Data
Workflow
Observability
Settings
```

---

# 19. Dashboard

首页只展示真正有决策价值的信息。

## 顶部

```text
Human Anchors
Semantic Frames
Candidates
Preference Pairs
Hard Cases
Active Experiments
Current Best Writer
Current Best Reward Model
```

## 中部

四块：

### Quality

```text
Semantic Fidelity
Naturalness
Human Preference Win Rate
AI-Likeness
```

### Data Growth

```text
新增 Frame
新增 Pair
新增 Residual
新增 Hard Case
```

### Model Leaderboard

```text
模型
Human Win Rate
Naturalness
Semantic Fidelity
Cost / 1K
```

### Risk

```text
Judge disagreement spike
Benchmark regression
Semantic drift spike
Model API failure
Dataset imbalance
```

---

# 20. Corpus 页面

左侧：

```text
Works
Authors
Genres
Sources
License
Quality
```

主区域：

```text
作品列表
段落数量
已处理比例
SemanticFrame 数量
实验次数
```

点击进入：

```text
原文
段落切分
元数据
Frame
Reconstruction History
Residual
Preference
```

---

# 21. Semantic Lab

这是最核心页面之一。

布局：

```text
┌──────────────┬──────────────────┬──────────────────┐
│ Human Text   │ SemanticFrame    │ Validation       │
│              │                  │                  │
│ 原文         │ 结构化语义       │ 泄漏/事实/缺失   │
└──────────────┴──────────────────┴──────────────────┘
```

功能：

- 自动抽取
- 多模型对照
- diff
- Frame 版本切换
- 字段级修改
- Leakage Highlight
- semantic invariants
- reader_should_infer
- must_not_know

---

# 22. Reconstruction Arena

顶部选择：

```text
Frame
Models
Candidates per model
Temperature
Strategies
```

主区域：

卡片式匿名候选：

```text
Candidate X17
Candidate X23
Candidate X41
```

默认隐藏模型来源。

支持：

```text
Reveal Human
Reveal Models
Run Judges
Create Tournament
```

Human Reveal 前必须明显提示：

```text
Human Original Hidden
```

---

# 23. Expression Residual 页面

左右对照：

```text
Human
Candidate
```

中央高亮：

```text
增加的信息
删除的信息
显式化
抽象化
修饰
心理标签
时序差异
句法差异
```

底部：

```text
Residual JSON
Difference Tags
Strategy Classification
```

---

# 24. Strategy Atlas

用来展示表达策略。

可以按：

```text
策略
题材
POV
情绪强度
关系距离
场景类型
Human Win Rate
AI 使用率
```

筛选。

每个策略页面：

```text
定义
适用条件
不适用条件
Human 示例
AI 示例
反例
成功率
相关策略
常见误用
```

---

# 25. Judge Arena

展示同一 Pair 的多 Judge 结果。

```text
Semantic       A
Naturalness    B
Pragmatics     B
Rhythm         A
Style          Equal
Adversarial    Uncertain
```

必须展示：

```text
confidence
历史 human agreement
bias flags
```

不能只展示最终分数。

---

# 26. Preference Lab

核心是盲评。

模式：

```text
A/B
Ranking
Tournament
Best-of-N
```

可开启：

```text
Human-only mode
AI Judge mode
Mixed mode
```

Human-only 模式必须隐藏所有 AI 评分。

---

# 27. Hard Cases

每个 Hard Case 显示：

```text
争议原因
所有候选
Human
Judge disagreement
可能缺失特征
历史处理
相关案例
```

按钮：

```text
重新抽 Frame
增加候选
增加 Judge
标记 Ontology 缺陷
创建新实验
加入 Benchmark
```

---

# 28. Benchmarks

显示：

```text
Benchmark Version
Samples
Coverage
Leakage Risk
Last Run
Best Model
Regression
```

每次模型/Prompt/Frame 改动都必须可跑 Benchmark。

---

# 29. Experiments

每个 Experiment 页面：

```text
Hypothesis
Control
Variant
Dataset
Models
Prompts
Sample Size
Metrics
Cost
Status
```

结果必须显示：

```text
Effect Size
Confidence Interval
Human Preference
Semantic Delta
Naturalness Delta
Cost Delta
```

禁止只给：

```text
+3.4%
```

---

# 30. Models

模型管理：

```text
Provider
Model ID
Context
Input Price
Output Price
Cache Price
Rate Limit
Status
Capability
```

能力矩阵：

```text
Semantic Extraction
Writing
Naturalness
Judging
Adversarial
Rewrite
Dialogue
```

---

# 31. Training Data

显示：

```text
SFT Samples
Preference Pairs
Reward Model Samples
Rewrite Samples
Hard Cases
```

可导出：

```text
JSONL
Parquet
DPO
RM
SFT
```

导出必须记录 dataset version。

---

# 32. Workflow 页面

使用 React Flow。

展示 DAG：

```text
Corpus
  ↓
Semantic
  ↓
Leakage
  ↓
Reconstruct
  ↓
Residual
  ↓
Judge
  ↓
Preference
  ↓
Hard Case
  ↓
Dataset
```

节点状态：

```text
queued
running
completed
failed
retrying
paused
```

支持：

```text
Retry
Pause
Resume
Cancel
Clone Experiment
```

---

# 33. Observability

需要：

```text
API success rate
Provider latency
Token usage
Cost
Workflow duration
Failure rate
Retry count
Judge disagreement
Semantic failure
```

模型故障必须记录：

```text
provider
model
status
error
request_id
retry
fallback
```

---

# 34. API 设计

主要 API：

```text
/api/corpus
/api/segments
/api/semantic-frames
/api/candidates
/api/reconstructions
/api/residuals
/api/judges
/api/preferences
/api/hard-cases
/api/strategies
/api/benchmarks
/api/experiments
/api/models
/api/workflows
/api/training-data
/api/metrics
```

所有列表接口支持：

```text
filter
sort
pagination
search
version
```

---

# 35. Model Gateway

统一 OpenAI-compatible 接口。

职责：

```text
模型路由
API Key
并发
Rate Limit
Retry
Streaming
Cost
Token Accounting
Fallback
Provider Health
```

禁止业务模块直接写死 Provider。

---

# 36. 模型路由

不同任务允许不同模型：

```text
Semantic Extraction:
强推理模型

Candidate Generation:
多模型池

Residual:
强分析模型

Naturalness:
专门 Judge

Adversarial:
与主 Judge 不同家族

Cheap Batch:
低价高速模型
```

---

# 37. 防止模型能力限制污染数据

系统必须强制：

## 多模型异构

不得长期使用：

```text
同一模型
生成 + 审核 + 改写
```

---

## Human Anchor

所有高价值规则都必须能回到真实文本。

---

## Disagreement Mining

Judge 越不一致，样本价值越高。

---

## Hidden Benchmark

任何“自称变强”都必须通过隐藏测试。

---

## Human Checkpoint

定期抽样：

```text
1%
```

进入真实人工盲评。

比例可调整。

---

# 38. 模型能力天花板处理

如果所有模型都生成不好：

```text
Generator Ceiling
```

需要：

- 增加模型多样性
- 增加采样温度
- 增加表达策略
- 使用 Human 示例训练
- 后期训练 Writer

如果 Judge 不可靠：

```text
Verifier Ceiling
```

需要：

- Human Preference
- 专门 RM
- Judge calibration
- Adversarial verifier

---

# 39. 自动学习规则

系统不得直接把：

```text
“缓缓”不好
```

存为规则。

必须存条件规则：

```yaml
pattern:
  psychological_explicitness

bad_when:
  - 信息已可以从动作推断
  - 情绪强度低
  - POV 贴近

acceptable_when:
  - 认知变化本身是剧情事件
  - 信息必须明确
```

---

# 40. 训练路线

训练不是第一天做，但系统从一开始就为训练准备。

推荐顺序：

```text
Stage 1
Prompt + Workflow

Stage 2
Preference Dataset

Stage 3
Reward Model

Stage 4
Rewrite Model SFT

Stage 5
Writer SFT

Stage 6
DPO

Stage 7
Verifier-guided RL / GRPO

Stage 8
Personal / Genre Reward Model
```

---

# 41. Reward Model

至少拆成：

```text
Semantic RM
Naturalness RM
Style RM
Human Preference RM
```

后期可训练：

```text
Unified Literary RM
```

但必须保留单项模型用于诊断。

---

# 42. Writer 训练数据

Writer 输入：

```text
SemanticFrame
ExpressionStrategy
StyleProfile
Context
```

输出：

```text
Sentence / Paragraph
```

不要训练：

```text
Prompt → 整章小说
```

初期重点让模型学习：

```text
Semantic → Expression
```

---

# 43. Personal Preference

系统必须允许建立：

```text
Global Quality
Genre Preference
Project Preference
User Preference
```

最终评分应支持：

```text
Universal Language Score
+
Genre Score
+
Personal Preference Score
```

避免所有文本被统一成一种“标准作文腔”。

---

# 44. 数据质量分级

每条数据有等级：

```text
L0 Raw
L1 Auto Extracted
L2 Multi-model Verified
L3 Judge Agreement
L4 Human Checked
L5 Benchmark Grade
```

训练时允许指定：

```text
minimum_quality_level
```

---

# 45. 自动停止规则

避免无意义烧 Token。

例如：

```text
Judge agreement > 0.95
且 confidence > 0.9
→ 不再增加 Judge
```

```text
Rewrite 连续 3 轮无提升
→ Stop
```

```text
候选已覆盖 95% strategy clusters
→ 停止继续采样
```

---

# 46. 并发策略

建议：

```text
Candidate Generation:
高并发

Strong Judge:
低并发

Human Evaluation:
异步

Benchmark:
独立队列
```

每个 Provider 独立 Rate Limit。

---

# 47. 本机部署

当前设备适合：

```text
FastAPI
PostgreSQL
Redis
Temporal client/worker
React
DuckDB
Polars
API 调用
数据处理
```

不建议本地：

```text
7B+ Reward Model 训练
大规模 LoRA
GRPO
```

---

# 48. GPU 训练

未来数据达到：

```text
Preference Pair > 500K
SemanticFrame > 100K
Hidden Benchmark 稳定
```

再租：

```text
4090 / 5090
A100 80GB
H100
H200
```

进行训练。

---

# 49. 推荐项目目录

```text
language-genome/
├─ backend/
│  ├─ api/
│  ├─ models/
│  ├─ schemas/
│  ├─ services/
│  ├─ workflows/
│  ├─ workers/
│  ├─ judges/
│  ├─ generators/
│  ├─ analyzers/
│  ├─ experiments/
│  └─ gateway/
│
├─ frontend/
│  ├─ pages/
│  ├─ components/
│  ├─ features/
│  ├─ stores/
│  ├─ api/
│  └─ charts/
│
├─ data/
│  ├─ corpus/
│  ├─ benchmarks/
│  ├─ training/
│  ├─ exports/
│  └─ cache/
│
├─ prompts/
│  ├─ semantic/
│  ├─ generation/
│  ├─ residual/
│  ├─ judges/
│  └─ adversarial/
│
├─ experiments/
├─ configs/
├─ scripts/
├─ docs/
└─ tests/
```

---

# 50. 开发任务清单

## 任务 1：基础工程

完成：

```text
FastAPI
React
PostgreSQL
Redis
Workflow
Model Gateway
Auth
Config
Logging
```

验收：

- 前后端能启动
- DB migration 可运行
- 能调用至少 2 个模型
- 模型调用记录 Token/Cost/Latency

---

## 任务 2：Corpus

完成：

- 文本导入
- Work / Chapter / Paragraph
- Segment 切分
- 元数据
- Human Anchor 标记

验收：

- 可定位回原文
- 可筛选
- 可导出
- 不允许 AI 覆盖 Human

---

## 任务 3：SemanticFrame

完成：

- Schema
- Extractor
- Multi-model merge
- Verifier
- Leakage detector
- Version

验收：

- 可从 Human Segment 自动抽 Frame
- 可人工编辑
- 有 diff
- 有泄漏分

---

## 任务 4：Reconstruction Arena

完成：

- 多模型候选
- 多采样
- 匿名化
- Human Hidden
- Reveal

验收：

- 一个 Frame 可生成至少 64 Candidate
- Human Reveal 前模型看不到原文

---

## 任务 5：Residual Analyzer

完成：

- Human vs Candidate
- 结构化差异
- 高亮
- Residual 存储

验收：

- 每个候选都有 residual
- 可聚合统计

---

## 任务 6：Controlled Corruption

完成至少 15 类 corruption。

验收：

- 可批量生成
- 可验证语义
- 可自动形成 Pair

---

## 任务 7：Judge Arena

完成全部基础 Judge。

验收：

- Judge 独立
- 隐藏来源
- 支持 abstain
- 保存 confidence
- 记录 agreement

---

## 任务 8：Preference Lab

完成：

- A/B
- Ranking
- Tournament
- Human Blind Review
- Judge Review

---

## 任务 9：Strategy Atlas

完成：

- 策略发现
- 策略聚类
- 策略库
- Human/AI 分布

---

## 任务 10：Hard Case

完成：

- 自动进入
- 分类
- 重新实验
- 加入 Benchmark

---

## 任务 11：Benchmark

完成：

- Hidden Set
- Benchmark Runner
- Regression
- Leaderboard

---

## 任务 12：Experiment Engine

完成：

- Hypothesis
- Control
- Variant
- Dataset
- Metrics
- Result
- Reproduce

---

## 任务 13：Training Export

支持：

```text
SFT
DPO
Reward Model
Rewrite
```

---

## 任务 14：Observability

完成：

- Token
- Cost
- Latency
- Provider
- Workflow
- Error
- Judge disagreement
- Benchmark regression

---

# 51. 自动验收标准

系统整体完成后必须满足：

### 数据

- 所有核心数据有 version
- 所有结果可追溯
- Human Anchor 不可被 AI 覆盖

### 工作流

- 失败可恢复
- 支持并发
- 支持 Pause / Resume / Retry
- 支持条件分支

### 实验

- 同实验可以重跑
- 控制变量可锁定
- 输出显著性与效果量

### 前端

- 不依赖日志阅读
- 所有核心对象有独立页面
- 所有实验可视化
- 所有 Human/AI 对照可直观看到

### 质量

- 不允许仅依赖单 Judge
- 必须存在 Hidden Benchmark
- 必须存在 Human Preference
- 必须存在 Hard Case

---

# 52. 系统最终要回答的问题

系统必须逐步能够定量回答：

```text
AI 为什么喜欢解释心理？
AI 为什么喜欢把隐含信息说完整？
AI 为什么句子经常过于工整？
什么情况下直接心理描写是好的？
什么情况下行为表达优于心理表达？
不同题材的句群节奏有什么区别？
Human 与 AI 的表达策略分布差多少？
不同模型的“AI味”是否不同？
哪些表达策略最适合某种语义条件？
```

---

# 53. 最终成功标准

本项目成功不是因为：

```text
“AI味检测 95 分”
```

而是因为能够证明：

```text
在同样 SemanticFrame 条件下：

系统训练后的 Writer
相较基础模型：

Human Preference ↑
Semantic Fidelity 不下降
Naturalness ↑
Implicitness ↑
Judge-Human Agreement ↑
Hidden Benchmark ↑
```

并且：

```text
模型能够生成过去基础模型很少生成、
但人类作者高频采用的表达策略。
```

---

# 54. 禁止事项

禁止：

1. 单一模型完成生成、审核、改写、最终判定
2. 把禁词表当核心
3. 把“更简洁”自动等同于“更好”
4. 把“更隐晦”自动等同于“更好”
5. 把 Human 文本默认判定为绝对最优
6. 用训练集调 Hidden Benchmark
7. 让 AI 合成数据逐渐取代 Human Anchor
8. 没有实验就修改核心规则
9. 只记录最终分数，不记录过程
10. 同一段无限 Rewrite 以消耗 Token
11. 不记录 Prompt / Model / Dataset 版本
12. 把前端做成日志查看器

---

# 55. 建议的第一批大规模实验

## EXP-001

问题：

```text
AI 是否显著更偏好显式心理解释？
```

方法：

```text
10000 Human Frames
×
8 Models
×
8 Candidates
```

统计 Human / AI expression distribution。

---

## EXP-002

问题：

```text
SemanticFrame 是否降低语义漂移？
```

对比：

```text
直接续写
vs
SemanticFrame → Expression
```

---

## EXP-003

问题：

```text
Human Anchor 与 AI Candidate 的差异主要集中在哪些维度？
```

运行百万级 residual 分析。

---

## EXP-004

问题：

```text
Controlled Explicitness 对 Human Preference 的影响是否存在最优区间？
```

---

## EXP-005

问题：

```text
不同模型是否拥有稳定的表达偏差？
```

建立 Model Language Fingerprint。

---

## EXP-006

问题：

```text
多 Judge 是否真的比最佳单 Judge 更接近 Human？
```

---

## EXP-007

问题：

```text
Judge 数量增加到多少开始收益递减？
```

---

## EXP-008

问题：

```text
Rewrite 到第几轮开始出现质量退化？
```

---

## EXP-009

问题：

```text
Human Original 在匿名情况下是否稳定击败 AI？
```

如果不能：

```text
分析为什么。
```

---

## EXP-010

问题：

```text
能否仅利用 residual + preference 数据训练一个小型 Naturalness RM，
并超过通用 LLM Judge？
```

---

# 56. 长期产物

最终项目应沉淀：

```text
Language Genome Dataset
Expression Strategy Atlas
Chinese Literary Error Ontology
SemanticFrame Corpus
Expression Residual Corpus
Human-AI Contrast Dataset
Controlled Corruption Dataset
Preference Dataset
Hard Case Dataset
Hidden Benchmark
Naturalness Reward Model
Semantic Reward Model
Literary Reward Model
Writer Model
Rewriter Model
```

---

# 57. 一句话总纲

本项目不追求“让 AI 学会几个去 AI 味技巧”。

真正要建立的是：

> **一个能够通过人类真实文本、语义重建、表达对照、差异分析、偏好学习和持续实验，逐步学习“中文小说中某个意思应该如何被自然表达”的长期语言研究与训练系统。**

系统的最终价值，不是某一代模型生成出来的一篇文章，而是：

> **将“人类为什么这样写”沉淀为可验证、可比较、可训练、可演化的数据与模型能力。**
