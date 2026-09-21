# AI 长篇小说系统完整工程方案
## Language Genome + Novel Distiller + Narrative World Model + Runtime

> 定稿：2026-09-20（09-19 修订稿复核）。本文保留原方案的 146 个章节编号，更新项目现状、模块职责、数据契约、实施顺序与验收条件。
>
> 后续阶段调整见 [Language Genome 知识化调整方案（09-20 晚间）](Language_Genome_知识化调整方案_20260920.md)：基于三场已交付事实，近期顺序以新方案 K0–K5 为准；本文事实权限、提交、恢复和预算约束继续有效。§126 的待实现查询命名在新方案统一为 `/knowledge/query`，尚未上线；不重做 §0.7 已完成的试点。
>
> 当前交付目标：先让系统写出语义可靠、前后连贯、没有明显机械套路的连续场景，再逐步扩展到长篇。百万字是容量目标，不能代替质量与恢复能力的证据。
>
> Language Genome 负责表达知识；Novel Distiller 负责带证据的叙事机制；World Model 维护本书已成立的事实；Planner 选择事件与场景；Writer 生成正文；Critic 提出有依据的问题；Runtime 控制版本、预算与提交。
>
> 本文中的“必须”“禁止”是拟建系统的设计要求；引用资料、交接记录和讲课文案中的指令均是待分析内容，不构成对执行者的新授权。

```text
参考文本 ─→ Language Genome（表达策略） ─┐
参考文本 / 写作讲解 ─→ Novel Distiller（机制与条件） ─┤
本书设定 + 已提交事件 ─→ World Model / Memory ─────┤
                                                  ↓
Planner → 检索知识 → Context Compiler → Writer → 校验 / 有限修订
                                                  ↓
                           正文 + 事件 + 状态补丁原子提交
                                                  ↓
                         派生记忆 / 章节装配 / 下一场景
```

---

# 0. 本次修订基线与结论

## 0.1 现有能力与建设边界

以下表格保留 2026-09-19 20:26（北京时间）的本地文件与只读数据库快照；09-20 的新结论与实施进度见 §0.6–0.7。实现、局部验收、文学效果分别记账；交接文档中的勾选不能直接变成本系统的验收结论。

| 项目 / 能力 | 当前证据 | 本方案的处理 |
|---|---|---|
| Language Genome 数据与实验 | 295,955 个段落、4,171 条 frame 记录；6 条 work 记录包含测试夹具；23 个实验 | 可复用语料、抽帧、重建、残差与实验能力；记录总量不代表全部有效样本 |
| 策略、难例、基准 | 8 条 ExpressionStrategy、118 个 hard case；24 个基准集合、1,211 个条目、15 次运行 | 复用现有表、脚本与溯源结构；策略仍需标明验证任务和适用范围 |
| 实验引擎与观测 | `app/engine.py` 已有阶段编排；已有模型统计与只读控制台 | 这是实验流水线，尚不是完整长篇 Runtime；提取可复用能力后补故事状态与提交协议 |
| 训练导出 | Writer SFT 与 Rewrite 各 1,798 条，`segment_id + frame_id` 完全重合；实际涉及 1,396 个段落 | 不能相加为 3,596 个独立训练样本；训练暂列可选实验，见 §112 |
| 前端（09-19 20:26 快照） | 当时 16 页研究界面接入进行中；当晚已完成首轮接入，见 §0.6 | 复用已交付路由、字段、真实数据与盲评回归，不再另做同用途页面 |
| Novel Distiller | 最近可核验交付为 09-16：版本 0.4.3 / schema 10；Novel Hub 文件导入、上下文、反馈链路有 14 项通过记录 | 复用 Evidence / Mechanism / 条件 / 反例 / 版本契约；不能据此宣布 LG 与长篇 Runtime 已打通 |
| Distiller 的效果边界 | 检索留出集 13/20（65%），低于原定 85%；全书关系验证与写作收益证据不足 | 可接为实验性知识源，保留无结果与低置信回退；暂不作为生成质量的必要前置门 |
| 长篇系统 | 当前 LG API 未实现本文设计的 `/genome/query`、世界状态和整场景闭环接口 | §126–134 均是待实现契约；Dream、场景提交、长篇记忆与作品工作台不能标记为已上线 |

运行中的导出与任务会继续变化；`ai_ranking_v1` 已在 09-19 21:40 生成 105 对弱标签，不能使用旧的计划条数。历史 SFT / Rewrite / RM 文件尚未按晚间修复重新导出。统计口径与快照见 [本次证据](../../outputs/novel-plan-review-20260919/latest-evidence.json)。

## 0.2 当前实验可以说明什么

| 观察 | 能支持的结论 | 不能推出的结论 |
|---|---|---|
| `nat-v1` 201 题来自 15 个源段落；两次完整运行约 91% | 对构造答案键有较高命中，但未超过长度基线（见 §0.6） | 201 个独立场景均有效；能替用户审美；长篇正文已改善 |
| `hvai-v1` 545 题；一次运行答出 519 题，其中 429 题识别来源正确 | 已答题身份识别率 429/519；全体分母为 429/545，另列 26 题缺失 | 人类来源必然更好；能识别来源就能评判质量 |
| `corr24` 用户判定为“两边都不好”11、打平6、原文胜4、候选胜3 | 当前“原文恒优”和默认 DPO 方向缺少支持 | 所有原文差、所有模型口味必然相反，或所有题材都无解 |
| 去偏报告的有效小样本可能得到高准确率 | 需要保留缺失、全体分母及不确定性 | 用筛掉未答题后的满分宣布评委合格 |
| 专名纠错前后同 40 对，身份识别由 28/40 到 36/40 | 来源噪声可能影响本任务，需要版本化清洗 | 文字质量显著提高；把配对样本当独立两组做显著性结论 |

**实施决策：工程建设与审美研究分开推进。** 延续已记录的方向：减少明显问题、推进系统建设，不再要求用户持续做大批盲评，也不等待“万能审美评委”出现。已确认的语义矛盾可以阻断；尚未验证的风格判断只给可定位的建议。不能把“没有可靠审美裁判”解释为“无需做正文验收”。

## 0.3 猫神资料的采用方式

资料来自 Obsidian 的 [猫神写作_全部文案](F:/6/Documents/黑曜石/日记/写作技巧/猫神写作_全部文案.md)，共 116 条、同一讲述者来源。本次核对全目录并重点阅读大纲、期待感、节奏、反转、描写、对话、拆书等相关篇目，未逐条核听音视频。

这些文案是**技巧假设来源**，不是 116 份独立有效性证据。保留原始转写；订正 ASR 错字、分离广告与方法主张，并记录来源定位及修订版本。标题中的“必火”“赚钱”、平台或收益断言不进入质量规则。相互矛盾的开篇建议按题材与读者契约保留条件，不能拼成全局硬约束。

本次吸收的实质增量是：读者期待账、人物目标驱动的对话、场景转折及后果、按重要性分配叙述篇幅，以及带边界的删改操作。分层大纲、场景卡、拆解后重构等原方案已有能力继续复用。具体候选见 §27.1 与 §103。

## 0.4 状态用语与依据

- `implemented`：代码 / 数据存在；`contract_verified`：接口与失败路径有证据；`pilot_verified`：限定任务试用通过；`quality_supported`：与基线比较后，在明确范围内有质量收益证据。四者不得互换。
- 对当前状态优先核对仓库代码、带时间的运行记录、数据库快照和数据导出；交接文档中的旧数字按日期处理。
- 同一结论必须记录任务、样本独立单位、缺失、版本、适用范围及尚未验证的部分。

本地依据：[LG 交接](HANDOVER.md)、[前端接入计划](frontend-plan-2026-09-19.md)、[训练可行性记录](training-feasibility-20260919.md)、[去偏报告](judge-debias-report-20260919.md)、[子基准记录](benchmark-subs-20260919.md)、[纠错记录](typo-normalization-20260919.md)、[Distiller 交付记录](../../novel-distiller/持续交付进度.md)。它们是证据入口，不自动覆盖本文的结论边界。

## 0.5 先交付什么

第一交付物是：在固定世界快照上，完成一场景的计划、知识检索、正文、校验、有限修订和原子提交；中断可恢复，重复请求不重复改变世界。随后扩至 3 场景、10 场景和更长连续运行。

猫神策略先选择对话目标、期待推进、重复解释删改三个独立小试点。Dream、多读者讨论、微调与大规模自动学习后置。详细顺序和验收见 §140–144。

---

## 0.6 09-20 最终核对补充

1. **长度混淆**：最新交接与 `benchmark_falsify.py` 增加“只选较短文本”基线：nat-v1 为 0.944，hvai-v1 为 0.872，均高于上述模型点估计。当前高分不足以证明超出长度线索的辨别能力；相关结论保持 weak / provisional。p=0.063 不能表述为常用 0.05 水平下显著。
2. **导出文件与导出器版本不同步**：代码已补来源完整性三态、内容级隔离、同文冲突消解、确定性选样与覆盖统计；只读核查时 `writer_sft_v3` / `rewrite_v2` / `rm_v1` 仍是修复前文件。训练前必须重导并核对 manifest，不能把“修好脚本”写成“旧文件已干净”。负面库 103 条仅覆盖 7 个源段落，需披露集中度。
3. **前端更新**：09-19 晚间已完成 `/lab` 的 16 页接入、共享样式、旧入口重定向与既有盲评入口保留，交接有静态测试和运行时体检记录。Arena 与 Frame 页当前为聚合分布，明细能力仍有边界。S0 不重复建设已交付内容。
4. **边界统一**：Dream 默认关闭且只写模拟空间；Critic 的 Issue 不授予改设定权限；全流程有调用、输入、输出、时长与修订上限。Unknown 上游结果阻止自动重派，直到显式核对。
5. **本阶段实施入口**：[执行计划](plan.md) 与 [逐步交接](runtime-handover-20260920.md)。从独立 SQLite 上的 Scene Commit、固定知识包和 CLI 场景流水线开始。设计 API 仍属待实现；测试 / 示例通过只说明相应契约和限定场景通过，不宣称整个系统或文学质量已验收。

补充快照：[09-20 源数据核对](../../outputs/novel-runtime-20260920/source-snapshot.json)。历史数字均保留日期，不覆盖原始实验。

## 0.7 09-20 单场景与连续三场交付

已实现本机 CLI 试点：明确场景卡 → 只读 LG 策略快照 → 冻结上下文 → 真实 Writer / Verifier → 有限修订 → 独立 SQLite 的正文 / 事件 / 状态原子提交 → outbox。三场原创样例顺序完成，世界 revision=3；第一场既有收据原样保留，重新执行三场仍只保留 14 次调用。全仓 508 项测试通过，其中 Runtime 35 项。实现入口和命令见 [试点说明](scene-runtime-pilot.md)；[正文、收据与验收](../../outputs/novel-runtime-20260920/accepted-pilot-final/acceptance.md)包含调用、token、耗时、代码哈希及恢复证据。

结果限定为 **含 Codex 复核的 pilot_verified**：三处正文事实问题修正后提交；核验模型另有五条误报，经逐条引文裁定处理，原始意见未删除。未变化事实被误列补丁、核验理由与 hard 标签矛盾，说明当前核验器仍不能无人值守放行。它只提出意见，不能因此增加预算、修改设定或绕过批准的变化。

每场保留 6 次调用、2 次修稿及输入 / 输出 / 请求时长上限，故障恢复不重置。金额缺少可靠计价与账单，记为 null，不能声称货币硬上限或实际费用已核实。HTTP API、自动卷级规划、Distiller 实时桥接、多进程调度、长期运行和文学质量均未随此勾选；微调与大规模 Dream 继续后置。下一步先检验核验契约在新样例上的准确性及所需复核量，再扩展场景数。

---

# 1. 总体目标

构建可长期写作、可回溯、可恢复、可控制成本的 AI 长篇小说系统。长期支持多卷、多人物、多势力、多线剧情、伏笔与关系演化，容量逐步覆盖 50 万至 500 万字及千章级作品。

近期先交付连续场景的真实闭环。优先满足：

1. **故事成立**：事实、时间、人物知识和事件因果不自相矛盾。
2. **读者有所得**：场景带来信息、选择、关系或局势的有效变化；安静场景可以用于消化后果与建立人物。
3. **表达可用**：减少重复解释、无功能铺陈、机械排比等具体问题，保留题材与个人风格。
4. **工程可靠**：正文与状态一致、可中断恢复、可审计、预算有界。

长篇规模、单场景表现和长期一致性分别验收。系统生成了很多字、所有测试通过、模型评分高，都不能单独证明小说好看。

---

# 2. 系统总架构

三种知识必须分开：

| 层 | 管什么 | 不能做什么 |
|---|---|---|
| Language Genome | 语义约束下的表达策略、风格、失败模式、局部操作 | 决定本书事实；以原文来源充当好坏标签 |
| Novel Distiller | 带证据的叙事机制、触发条件、反例、迁移边界 | 把参考作品的人物、事件或台词搬成本书正史 |
| World Model / Canon | 本书已经成立的事件、实体状态、规则与信息分布 | 把模拟、计划、读者猜想直接写成已发生事实 |

```text
作者目标 / 读者契约 → Planner ← 本书权威状态快照
                         ↓
             查询 Genome + Distiller + 记忆
                         ↓
                 Context Compiler
                         ↓
                  Writer 草稿
                         ↓
       硬约束校验 + 适用 Critic → 有限修订 / 退回计划
                         ↓
       状态补丁核对 → Scene Commit（正文 + 事件 + 状态）
                         ↓
              Outbox → 派生记忆 / 图谱 / 章节
```

Runtime 包围整个流程。Dream 是 Planner 的可选支路；Knowledge Query 在 Context Compile 之前完成。权威状态只由 §56 的提交事务改变。

---

# 3. 工程拆分

保留 8 个核心逻辑域与 6 个附属域：

```text
01_language_genome  02_world_model  03_planning  04_context
05_writing         06_review       07_memory    08_runtime
09_model_governance 10_benchmark 11_observability 12_frontend
13_data_pipeline   14_research_lab
```

逻辑域不等于 14 个服务或 14 个 Agent。初期以少量进程、明确模块与版本化契约实现，按实测瓶颈拆分。

Novel Distiller 作为独立知识提供方接入；优先复用其证据、机制与审查数据模型。已有 LG 引擎、网关、观测、实验台和 Novel Hub 的接口资产先做适配审计，避免重建同类能力。

---

# 4. Language Genome

Language Genome 是表达知识层，负责“语义约束下怎样表达”。Corpus、清洗、来源完整性、分段、SemanticFrame、重建、残差、Strategy、Failure、Hard Case、跨语料验证继续沿用 [LG 独立方案](Language_Genome_完整工程方案.md)。

截至本次修订，策略发现、难例挖掘、受控劣化、子基准、训练导出、实验引擎和观测已有实现。下一步聚焦可消费的知识契约、策略适用条件及场景闭环，不再把这些模块列为从零开发。

Frame 的事实约束与表达偏好分开。叙述显隐、心理描写、句长、比喻等允许继承项目设置或为 `unknown`；不得在抽帧阶段用“低显露、禁止心理解释”等全局默认值偷偷限制所有文体。具体落地需核对当前 schema、提示词与重建器的实际生效行为。

---

# 5. Language Genome 对 Writer 的最终输出

通过版本化 Knowledge API 或离线只读包输出，结构示意：

```yaml
language_knowledge_package:
  schema_version: genome-package/1
  package_id: null
  snapshot_id: null
  query_hash: null
  corpus_split: runtime_reference
  semantic_matches: []
  recommended_strategies: []
  alternative_strategies: []
  style_profiles: []
  failure_patterns: []
  hard_case_refs: []
  example_refs: []
  source_versions: []
  evidence_status: hypothesis
  uncertainty: []
  excluded_reasons: []
```

每条策略包含条件、操作、保持不变的语义、反例、证据来源、版本与适用范围。未校准的可信度用证据等级与不确定项表示，不填凭空生成的小数。

Writer 不扫描整个 Corpus。默认提供抽象规则或改写后的小例子；确有用途的原文片段须经过使用范围、长度、来源隔离与防复制检查。盲测来源、答案和标签不得进入知识包。包内容与查询结果均保存哈希，运行中不悄悄刷新。无匹配时返回可观察的空包，允许基线写作。

---

# 6. Narrative World Model

World Model 负责本书世界、人物、关系、资源、事件、因果、伏笔和冲突的状态。它也提供预测所需的只读快照，但模拟结果与事实必须物理或逻辑隔离。

所有状态、事件和派生数据都带 `book_id`、`branch_id`、`revision`、来源事件和证据引用。明确四种性质：`canon`（已提交事实）、`plan`（拟定目标）、`hypothesis`（推测）、`simulation`（模拟）。

区分世界真相、角色相信什么、角色实际知道什么、读者已看到什么。角色的错误信念可以成立于角色知识层，不能覆盖世界真相。每次生成读取一个固定 revision，不能混合多个时点的状态。

---

# 7. World State

统一状态结构示意：

```yaml
world_state:
  book_id: null
  branch_id: main
  revision: 0
  source_commit_id: null
  story_time: null
  location: null
  active_characters: []
  active_factions: []
  active_conflicts: []
  unresolved_threads: []
  resources: {}
  relationships: []
  secrets: []
  power_state: {}
  political_state: {}
  economic_state: {}
  social_state: {}
  physical_state: {}
  epistemic_state:
    world_truth: []
    known_by_character: {}
    exposed_to_reader: []
```

未知与空值有明确语义，不能被抽取器当作否定。每个事实可追溯到提交事件、正文证据或作者设定；硬世界规则单独版本化。

---

# 8. Character State

每个角色必须维护独立状态。

```yaml
character_state:
  character_id:
  location:
  physical_state:
  emotional_state:
  goals:
  short_term_goals:
  long_term_goals:
  beliefs:
  knowledge:
  false_beliefs:
  secrets:
  fears:
  desires:
  obligations:
  relationships:
  attitude_to_others:
  resources:
  injuries:
  power:
  social_status:
  unresolved_internal_conflicts:
  behavioral_tendencies:
```

---

# 9. Relationship State

关系有方向、有语境、有证据，不是单个静态标签或好感度总分。

```yaml
relationship:
  book_id: null
  branch_id: main
  revision: 0
  from_character: null
  to_character: null
  trust: null
  affection: null
  fear: null
  hostility: null
  dependency: null
  debt: []
  power_balance: null
  knowledge_asymmetry: []
  intimacy: null
  unresolved_conflict: []
  public_relation: null
  private_relation: null
  trend: unknown
  evidence_event_ids: []
```

`A → B` 不自动等于 `B → A`。数值量表须有锚点；没有证据时保留未知。关系跃迁要有触发行为、选择或后果，不能由一次情绪形容词自动升级。

---

# 10. Event Model

事件与状态转移是故事推进的基本单位，章节是阅读与发布单位。

```yaml
event:
  event_id: null
  book_id: null
  branch_id: main
  base_revision: null
  scene_id: null
  status: proposed
  story_time: null
  type: null
  actors: []
  target: null
  location: null
  preconditions: []
  trigger: null
  actions: []
  immediate_effects: []
  delayed_effect_hypotheses: []
  knowledge_changes: []
  relationship_changes: []
  resource_changes: []
  causal_parents: []
  evidence_spans: []
  source_text_hash: null
```

抽取的事件先为 proposed；核对正文、前置条件与状态补丁后才能随场景提交。尚未发生的延迟效果仍是义务、预测或待触发事件，不提前计入实际资源与人物状态。

---

# 11. 因果图

图中的边明确区分 `causes`、`enables`、`motivates`、`reveals`、`temporally_precedes` 与 `hypothesized`。时间相邻不自动等于因果。

重大事件至少能追踪前置条件、行为主体、选择与影响。角色行动应受目标、知识、代价和可用资源约束。证据不足时记录多个因果假设。

已提交事件的严格时间先后关系不得成环；叙述顺序可以倒叙。概念、关系和动机网络不一概强制为 DAG。反转改变读者的解释时，保留旧解释与揭示依据，不能抹掉先前已经成立的事实。

---

# 12. Thread / Plot Thread

长期剧情必须作为 Thread 管理。

```yaml
plot_thread:
  thread_id:
  type:
  origin_event:
  goal:
  current_state:
  tension:
  participants:
  dependencies:
  unresolved_questions:
  expected_payoffs:
  possible_outcomes:
  status:
```

类型：

```text
main_plot
character_arc
relationship
mystery
revenge
political
romance
power_growth
resource
foreshadowing
secret
```

---

# 13. Foreshadowing 与读者期待账

伏笔记录 `setup_event`、可见性、角色 / 读者已知范围、预期回收类型、回收窗口、关联线与状态。只有正文提交且读者已看到，才从 `planned` 变成 `seeded`；只有回收事件提交，才变成 `paid_off`。

在伏笔之外增加 `expectation_ledger`，覆盖承诺、问题、期限、危险与关系期待：

```yaml
expectation:
  id: null
  scope: scene
  promise_or_question: null
  established_by: []
  visible_to_reader: false
  stakes: null
  payoff_window: null
  progress_events: []
  resolution_event: null
  status: planned
  abandonment_reason: null
```

`scope` 可为 scene、arc、book。检查是否有可感知的推进、回报、合理延迟或明确放弃，而非要求每章制造新悬念。日常、抒情、余波场景也可兑现人物理解与情绪期待；没有适用期待项时允许为空。

该账本支持节奏审查，但“记录了期待”不等于“读者真的期待”，效果另行验证。

---

# 14. Narrative Arc

章节之上使用 Arc。

```yaml
arc:
  arc_id:
  objective:
  entry_state:
  exit_state:
  central_conflict:
  escalation:
  turning_points:
  climax:
  payoff:
  character_changes:
  world_changes:
  unresolved_threads:
```

Arc 可以跨：

- 多场景
- 多事件
- 多章节

不得以“固定 10 章一个 Arc”作为规则。

---

# 15. Dream Engine

Dream 是可选的前瞻模拟器，用于比较关键选择可能带来的后果，不负责批量写完整章节。

输入为固定 World Snapshot 与候选干预；输出为隔离分支上的事件、状态变化、风险和不确定项。模拟记录必须带 `simulation_id`、父快照、分支、深度、模型与预算。

Dream 没有正史写权限。Planner 可以采纳一条未来作为计划，之后仍须经过 Writer、核验和场景提交才能成立。

初期默认关闭；只对重大不可逆选择、跨线冲突或可明确描述的不确定性启用。先用少分支、短滚动验证收益，再扩大规模。

---

# 16. Dream 的基本单位

Dream 不按章节计算。

按：

```text
因果传播链
```

终止条件包括：

- 主要影响已传播完成
- 冲突形成稳定新状态
- 进入新的高层决策节点
- 不确定性过高
- 分支价值明显下降
- 已出现主要 payoff
- 出现不可逆状态
- 进入下一个 Arc

---

# 17. Dream Candidate

```yaml
dream_candidate:
  intervention:
  rationale:
  expected_immediate_effect:
  uncertainty:
  novelty:
  risk:
```

例如：

```text
角色公开质问
角色暂时隐忍
第三方介入
秘密提前曝光
资源突然损失
关系暂时破裂
敌人误判
主角主动放弃
```

---

# 18. Dream Rollout

每条 Rollout 记录：

```yaml
rollout:
  branch_id:
  initial_state:
  interventions:
  simulated_events:
  state_transitions:
  turning_points:
  emergent_threads:
  dead_ends:
  opportunities:
  uncertainty:
  terminal_state:
```

不得生成完整小说正文。

---

# 19. Dream Evaluation

先排除违反世界硬规则、角色已知信息或资源边界的候选，再比较因果连贯、人物动机、后续选择空间、伏笔回收机会、重复风险和读者契约匹配。

可以用 Pareto Front 保留不同优势路线，但应限制候选数，并给出可核查的利弊与证据。各维度不是天然可靠的评分器；无校准的模型总分不自动决定路线。

Dream 的主要对照是同预算的普通规划：是否减少后续矛盾、返工和死路，是否保留更多可用路线。模拟越多、评分越高都不等于收益越大。

---

# 20. Dream 多样性

对不同路线比较事件、状态、策略和冲突的重合，必要时去除等价分支。多样性是选择空间的诊断，不强制添加新的打分器或为求不同破坏人物与世界规则。分支数和滚动深度始终受 §90 的预算限制。

---

# 21. Prediction Error

必须分开三类比较：

| 比较 | 用途 | 不能当作什么 |
|---|---|---|
| 计划 / Dream 与系统按该计划生成的正文 | 检查执行偏差、遗漏与状态抽取 | 对独立未来的预测正确性 |
| 在隐藏后文、冻结输入时预测参考故事的后续，再揭示后文 | 测试特定语料上的预测与校准 | 本书读者必然喜欢该路线 |
| 生成后收到的真实读者或作者反馈 | 检查某个偏好 / 理解 / 体验假设 | 世界事实或所有读者的通用规律 |

`prediction_result` 记录 `evaluation_kind`、预先冻结的预测、独立结果来源、是否受本次计划影响、误差、证据与适用范围。

系统自己计划、自己实现、自己打高分的闭环只能证明执行一致，不能作为世界模型“学会预测”的证据。没有独立结果时不自动提高置信度。

---

# 22. World Model Learning

先学习可审计的规则与经验，不以训练神经网络为前提。经验卡记录：条件、观察到的行为、来源事件、反例、适用人物 / 项目、版本与验证状态。

```yaml
lesson:
  condition: [public_humiliation, authority_present]
  observed_behavior: [suppress_direct_conflict, retaliate_later]
  scope: character
  source_event_ids: []
  counter_examples: []
  evidence_status: hypothesis
  calibrated_probability: null
  revision: 1
```

示意卡没有证据时仍是 hypothesis。经验先成为候选；通过适用性和回归检查后才影响建议。更新规则不能改写已经提交的事实，不能把同一次生成的反复自评当成新增独立样本。

---

# 23. Planner

Planner 分层。

```text
Book Planner
Volume Planner
Arc Planner
Sequence Planner
Scene Planner
Beat Planner
```

---

# 24. Book Planner

Book Planner 维护作品定位、读者契约、主问题、主要人物弧线、长期目标、世界硬规则、叙事视角和内容边界。

读者契约说明目标体验、题材承诺、允许的表达范围与不希望反复出现的套路。作者明确偏好优先于讲师建议、通用题材模板与模型默认风格。

远期只固定少量支柱事件与结束条件，保留可选路线；不能把千章写成不可修改的详细清单。里程碑变更记录影响范围，已发布内容变更须走独立修订流程。

---

# 25. Volume Planner

每卷定义：

```yaml
volume:
  objective:
  starting_state:
  ending_state:
  main_conflict:
  major_arcs:
  character_targets:
  world_changes:
  key_reveals:
  climax:
  unresolved_threads:
```

---

# 26. Arc Planner

输入固定 World Snapshot、卷目标、未解决剧情线、人物目标和可选 Dream 结果。输出 Arc 目标、主要冲突、转折、期望状态变化与退出条件。

Dream 未启用时正常规划；期望状态只能写入计划命名空间。

---

# 27. Scene Planner

Scene 是 Writer 的直接上游。场景卡同时描述事实约束与阅读作用，示意：

```yaml
scene_plan:
  book_id: null
  branch_id: main
  scene_id: null
  base_revision: null
  purpose: null
  function: null
  pov: null
  location: null
  participants: []
  participant_goals: {}
  entry_state: {}
  desired_exit_state: {}
  obstacle: null
  tactics_and_responses: []
  turning_choice: null
  cost_or_consequence: null
  information_to_reveal: []
  information_to_hide: []
  reader_expectation_before: []
  reader_expectation_after: []
  relationship_shift: null
  emotional_shift: null
  required_events: []
  forbidden_events: []
  foreshadowing: []
  payoff: []
  pacing: null
  tone: null
  technique_refs: []
```

字段按场景功能适用，允许 `not_applicable` 及理由。过渡与余波场景不必强行冲突或反转；但应知道保留它对人物、信息或阅读节奏有何作用。desired_exit_state 是目标，不能冒充已经发生。

## 27.1 猫神技巧的十个候选操作

来源定位均指 [Obsidian 合集](F:/6/Documents/黑曜石/日记/写作技巧/猫神写作_全部文案.md) 的对应标题 / 原始行号；位置是本次审阅快照，入库时应加内容哈希。所有条目初始为 hypothesis。

| ID / 候选 | 落到哪里、怎样操作 | 适用边界 / 反例 | 小试点如何验收 | 来源 |
|---|---|---|---|---|
| M01 期待推进 | Planner 为既有承诺登记本场推进、延期代价或回报 | 不要求每章悬崖结尾；抒情场景可兑现理解 | 读者能指出在等什么、获得什么；没有新增欠账失控 | 《小说最强3招期待感写法》L691；《小说想要挣钱就必须搞明白期待感》L275 |
| M02 目标—阻碍—选择 | ScenePlan 给关键人物独立目标、阻碍、选择与持续后果 | 不将全部人物变成机械任务执行者 | 行动动机能从本书事实解释；删除选择会改变结果 | 《4招让你写出：真正有用的小说大纲》L428 |
| M03 多线交汇 | Planner 对齐故事时间、资源约束和线间影响，安排有因果作用的交汇点 | 不为“多线”频繁切镜头；不泄露角色未知信息 | 交换两线顺序是否产生事实错误；每次切换是否有阅读作用 | 《5招让你的多线剧情》L439 |
| M04 对话作为行动 | 给双方不同意图，以试探、回避、争取、拒绝及反应推进 | 真诚直说、说明事实也可以有效，不强塞潜台词 | 保持信息与人设；至少一方选择、关系或信息状态有变化 | 《小说对话写的很假很低级》L1014 |
| M05 视角选择细节 | Writer 用当前人物可感知且与目标相关的细节替换泛泛描述 | 不擅自增加物件、背景真相；必要概述可保留 | 没有新增事实与视角越界；细节承担识别、行动或情绪作用 | 《4招让你拥有最牛的小说文笔》L904 |
| M06 删除重复解释 | Rewriter 只删已被动作、台词或上文表达过的重复心理结论 | 新信息、人物自欺、特殊叙述腔与必要心理过程不删 | 编辑前后信息等价；不靠同时增删情节取得优势 | 《3招精简小说文笔》L959 |
| M07 对话标记与节拍 | 在说话人清晰时减少冗余标签；必要时加入已有场景动作 | 多人对话保留归属；“说”“道”不是禁词 | 读者能辨认说话人；动作不挤占对话或捏造状态变化 | 《写网文怎样避免说和道》L915 |
| M08 注意力分配 | 给关键选择、转折和代价更多叙述空间，压缩低作用经过 | 慢不等于差；用字数比不能代替节奏判断 | 同一事件链下关键变化更易理解，无机械填充 | 《4招让你彻底学会小说的节奏感》L669 |
| M09 余波与过渡 | 用后果处理、关系反应、小目标或自然省略连接事件 | 不为留人硬塞事故；不允许高潮后状态自动复原 | 前场代价继续影响下一场；过渡有用且预算受控 | 《6招让你小说平淡期也能留住读者》L636 |
| M10 有依据的反转 | 从已有线索改变解释、选择或局势，登记提示与回收 | 不靠临时新规则救场；不把反转次数当质量 | 回看有证据、角色知识合理、后果持续存在 | 《4招神级反转》L680 |

每次小试点只启用一个操作，固定语义、模型、上下文和预算，同时记录副作用。优先做 M04、M01、M06；能支持工程正确性但没有读者反馈时，仅标为 pilot，不宣称审美收益。

“过滤词”文章的演示同时涉及增写和改写，不能直接视为单变量删词金标；“拆书—遮原文—重构”已经对应 LG 的既有流程，不另建重复模块。

---

# 28. Beat Planner

复杂场景允许拆 Beat。

```yaml
beat:
  objective:
  action:
  reaction:
  information_change:
  tension_change:
  state_change:
```

Beat 不是强制每场使用。

---

# 29. Planner 与 Dream 的关系

Planner 先从世界快照、读者契约、未完成承诺和人物目标形成可执行计划。

普通场景直接进入知识检索与编译；重大分岔按 §90 触发有预算的 Dream，然后比较路线、选择计划。Dream 超时或无有效候选时，记录原因并使用仍满足硬约束的普通规划；若前置事实本身不足，则暂停该场景解决缺口。

采纳的是计划，不是模拟事实。不能因“未来可能有效”跳过世界约束和正文证据。

---

# 30. Context Compiler

Context Compiler 将固定版本的 World、Plan、Memory、Genome、Distiller、项目风格与约束编译为角色所需的输入。

先完成检索，再编译；产物是可重放的 Context Package，而非临时拼接后丢弃的提示词。每个字段标明来源、版本、裁剪结果、消费角色与可见性。

角色知识与读者曝光状态决定 Writer 的叙述权限。审查器可在独立权限下读取隐藏事实检查越界；给 Writer 的隐藏限制尽量转换为禁用事项，避免完整未来答案泄漏。

---

# 31. Context Package

```yaml
context_package:
  package_id: null
  book_id: null
  branch_id: main
  scene_id: null
  world_revision: null
  plan_revision: null
  role_view: writer
  canon_constraints: []
  character_knowledge: {}
  reader_exposure: []
  scene_plan: {}
  recent_committed_scenes: []
  relevant_memory: []
  genome_package_ref: null
  distiller_package_ref: null
  style_profile_ref: null
  source_manifest: []
  source_hashes: {}
  compiler_version: null
  prompt_version: null
  token_budget: {}
  omissions: []
  content_hash: null
```

manifest 必须能证明本次使用了哪些版本、为何入选、排除了什么。缓存命中不得混用另一作品、分支、读者曝光阶段或世界 revision。

---

# 32. Context 分层

必须分：

```text
Permanent
Long-term
Arc
Scene
Immediate
Task
```

禁止把整个小说正文一直塞入上下文。

---

# 33. Context Budget

按实际模型与路由能力分配输入、输出预留和工具开销，不把宣传窗口长度当成有效理解长度。

编译优先级：世界与人物硬约束 → 本场计划和必要前情 → 相关记忆与证据 → 适用技巧 → 可选例子。优先裁剪无关或重复材料；硬约束放不下时必须重新规划输入或返回明确失败。

记录 token 实测 / 估算方法、输入实际用量、输出预留、裁剪项和回退方案。同一场景知识操作先限制为少数几项，避免大包技巧互相争夺注意力。

---

# 34. Context Inspector

前端必须能查看：

```text
本次 Writer 到底收到了什么
哪些来源被注入
哪些被排除
每部分 token
为什么选择这些信息
```

---

# 35. Writer

Writer 在场景计划、角色可见信息和世界约束内生成正文，允许选择句法、视角内细节与自然对话。

不改变关键事件、角色能力、资源、地点关系或隐藏事实。未入账的附带细节若会影响后续，必须形成新事实提案并经过核对；不允许靠补写设定自动解决剧情问题。

允许不影响正史的表达性细节，但不得把这种自由扩展为因果豁免。信息不足时返回具体缺口；普通表达选择不必反复让作者确认。

---

# 36. Writer 输入

```yaml
writer_input:
  scene_plan:
  context_package:
  semantic_targets:
  language_strategies:
  style_profile:
  genre_profile:
  hard_constraints:
  soft_preferences:
```

---

# 37. Writer 输出

Writer 输出正文与可核对的结构化声明：

```yaml
writer_result:
  run_id: null
  context_package_id: null
  draft_text: null
  draft_hash: null
  claimed_events: []
  proposed_new_facts: []
  claimed_strategy_refs: []
  unresolved_gaps: []
```

事件、策略使用和状态变化均为自报提案，不能直接入账。独立核对器以正文、Context Package 与源证据确认；不要求或保存模型隐藏思维过程。

---

# 38. Writer 多阶段生成

默认用一次场景生成建立基线，随后做硬约束检查。按具体 Issue 触发局部修订，而非每场固定执行扩写、润色、去味、重写等多轮调用。

对白、描写、节奏等专门处理只在有明确问题时启用。记录修改前后文本、保留的不变量、解决的问题及新产生的问题。达到预算或修订上限后按 §96 退出，避免反复润色损失人物声音。

---

# 39. Writer Strategy

Compiler 从知识包中选择适用的主要、辅助、备选策略与反模式，也允许明确的空包基线。Writer 不另发无预算检索。

输出 `claimed_strategy_refs` 仅是使用声明，必须由文本证据核对，不等于策略生效或确有质量收益。

---

# 40. StyleProfile

StyleProfile 描述本书、题材、视角与场景功能的表达倾向，不复制某位作者，也不充当全局正确答案。

维度保留句长及变化、对话与动作密度、心理直接度、显隐程度、描写、意象、说明、叙述距离、节奏、词汇复杂度、修饰、幽默与情绪强度。

每项记录范围或倾向、来源、作用范围、优先级与版本，允许 unknown / inherit。场景可以有受约束的偏离，例如高潮与余波采用不同节奏。

不得默认“越短越好”“心理描写越少越好”“比喻越少越好”；作者明确选择优先。StyleProfile 与 SemanticFrame 的事实字段分开存储、分别验收。

---

# 41. GenreProfile

GenreProfile 管题材倾向。

例如：

```text
玄幻
仙侠
都市
武侠
悬疑
历史
恋爱
科幻
```

包括：

- 常见节奏
- 常见冲突
- 常见场景
- 信息密度
- 读者预期
- 战力展示方式
- 关系推进方式
- 转场习惯
- 高风险套路

---

# 42. Writer 防污染

按 §51 拆分元信息泄漏防护和软风格诊断；ReferenceCopyGuard 独立处理参考复制。实现中可以合并 MetaComment / PromptLeak 等确定性扫描，不要求为每个名称新建 Agent。

既有 AILeakJudge 可作为兼容名称，但不能把来源身份判分接成强制风格否决。

---

# 43. ReferenceCopyGuard

对正文与本次检索参考检查异常长的连续重合、罕见表达、专有设定和连续情节的可疑迁移，保存可定位的匹配证据。

长段原文重合等明确问题可以阻断。语义相似、常见词组和题材惯例只能触发复核，不能凭统一相似度阈值断言复制，更不能把“像人类”当作无复制证明。

语料使用范围与训练可用性在数据入口记录，未明确可用于训练的素材不随导出自动取得训练资格。防复制检查与来源身份识别、审美评价是不同任务。

---

# 44. Critic 系统

Critic 是职责划分，不要求每场启动多个独立 Agent。先执行确定性契约、状态与引用检查，再调用适用的语义、连贯、人物、因果、节奏和语言评审。

分为两类输出：

- **可证实的约束问题**：引用正文与权威状态，确认后阻断提交，例如死去的人物无解释出现、资源重复消耗、角色知道尚未获知的秘密。
- **需要判断的风格建议**：指出具体句段、作用与副作用；未经验证不作为自动否决器，例如解释过多、句式单调、节奏拖沓。

评委数量、讨论一致或自称信心高都不能替代证据。多个角色可以由同一模型承担，但要保留输入隔离、职责和实际路由记录。

---

# 45. Semantic Critic

检查：

- Scene Plan 是否完成
- 事件是否缺失
- 是否新增未授权事实
- 是否语义漂移
- 是否逻辑跳跃

---

# 46. Continuity Critic

检查：

- 人物位置
- 时间
- 道具
- 伤势
- 关系
- 已知信息
- 战力
- 身份
- 服装 / 外观连续性
- 世界规则

---

# 47. Character Critic

检查：

```text
人物行为是否符合当前状态
人物是否突然降智
人物目标是否突然变化
人物是否知道不该知道的信息
人物语言风格是否漂移
```

---

# 48. Causal Critic

检查：

```text
事件是否有前因
结果是否过度
转折是否无依据
冲突是否强行制造
解决是否机械降神
```

---

# 49. Pacing Critic

检查：

- 信息释放
- 冲突密度
- 场景长度
- 重复
- 拖沓
- 跳跃
- 高潮堆叠
- 缺乏缓冲

---

# 50. Language Critic

检查局部表达问题：重复传递同一信息、无功能的情绪总结、套式转折、机械排比、说话人不明、视角漂移、动作链断裂，以及不符合本场 StyleProfile 的明显问题。

每条意见必须给出正文位置、问题机制、上下文依据和最小修订建议，并标明“确定性问题”或“风格假设”。必要心理描写、直说、概述、对话标签、长句本身不构成错误。

优先复用 LG 的 Failure Pattern 与猫神候选操作，保留反例。不能重新把缺陷词计数包装成“个人偏好评分”；已有失败路线与当前样本限制见交接与终报。

---

# 51. AILeakJudge 与风格诊断

拆成两个独立职责：

1. `MetaLeakGuard`：检查正文中的系统提示、工具痕迹、任务解释、残留 JSON、与小说无关的自我说明等。匹配必须结合上下文，角色在故事里谈 AI 并不自动构成泄漏。
2. `StyleDiagnostic`：按具体局部机制提示机械表达，仅作为有证据的修改建议，直到该规则在相应场景上获得验证。

“能区分人写 / AI 写”、训练集中的劣化检出率和模型声称的 AI 概率，均不能用作实际写作质量阈值。没有可靠的通用“AI 味分数”；产品应展示具体问题及修订效果，不展示虚假的精确程度。

---

# 52. Critic 输出

```yaml
issue:
  issue_id: null
  artifact_id: null
  text_hash: null
  span_start: null
  span_end: null
  offset_unit: unicode_codepoint
  rule_id: null
  rule_version: null
  kind: null
  severity: minor
  evidence_refs: []
  explanation: null
  expected_invariant: null
  proposed_fix: null
  remedy_stage: rewrite
  status: proposed
  resolution_evidence: []
```

偏移量用 0 起始、左闭右开的 Unicode code point，前端转换到自身索引单位。无具体文本位置的问题引用 scene / plan / state 字段路径。修稿后重新定位或失效旧 Issue，不能沿用旧偏移。

状态经过 proposed → confirmed → resolved，误报进入 rejected 并记录理由。严重度按影响判定；不能因为模型用了强烈措辞就提升等级。

---

# 53. Rewriter

Rewriter 不允许随意重写整章。

优先级：

```text
局部修复
↓
段落修复
↓
场景修复
↓
整场重写
```

只有结构性问题才允许整场重写。

---

# 54. Rewriter 输入

```text
Original Text
+
Issues
+
Scene Plan
+
Relevant Context
+
Language Strategies
+
Forbidden Changes
```

---

# 55. Rewriter 约束

Rewriter 只解决已选择的 Issue，并保留 Context Package 中的事实、视角、人物目标与已批准场景计划。每次修改产出变更范围和不变量核对结果。

Critic 提出的问题不等于修改世界设定的授权。需要改变事件、人物选择、能力或已成立事实时，退回 Planner 形成新的计划版本；涉及已提交正史的修订走显式 canon revision 流程。

修改后复查受影响的语义与连续性约束。未被批准的纯风格偏好不能覆盖作者要求，不能为解决一个问题无边界重写整场。

---

# 56. Continuity 与 Scene Commit

Continuity 从最终候选正文抽取事件与状态变化，再与入场快照、计划和证据核对。抽取结果是提案，不能边抽取边更新世界。

**正文、事件日志和权威状态补丁必须一次提交。** 协议：

1. 冻结待提交文本、上下文和计划版本，核验哈希；生成带证据的 `StatePatch`。
2. 对每个变化检查实体、前置条件、角色知识、时间、资源守恒或世界特有规则。补丁包含旧值 / 新值，无法确认的事实保持 unresolved。
3. 使用 `(book_id, branch_id, expected_revision)` 做并发校验。当前 revision 已变化时返回冲突，重新编译与验证，不强行覆盖。
4. 在一个数据库事务中写入 scene commit、正文引用 / 内容、事件、状态补丁、世界新 revision 和 outbox 消息；任何一步失败全部回滚。
5. 事务成功后，outbox 驱动摘要、向量索引、图谱、章节装配等派生更新；重复消费不重复生效。

```yaml
state_patch:
  book_id: null
  branch_id: main
  scene_id: null
  expected_revision: null
  final_text_hash: null
  operations: []  # 每项含路径、旧值、新值、事件和正文证据
  verifier_result_ref: null
  idempotency_key: null
```

大正文若放外部文件，先写入不可变内容寻址存储并确认可读，再在事务中登记哈希引用；失败遗留文件作为未引用对象清理。不能先更新世界后保存正文。

Writer、Critic、Dream、抽取器无权直接写 canon。已提交场景修改采用新版本及补偿 / 重建流程，保留事件链，不原地悄改旧账。

---

# 57. Memory System

保留 Task、Working、Long-term、Permanent 四类记忆，但记忆是带来源的检索材料，不能另造一套与 World Model 竞争的事实库。

已提交事件与作者设定是权威依据；摘要、向量索引、图谱和角色概况是可重建投影。所有记忆带 book、branch、source revision、有效时间、实体引用与来源证据。

重要性与新近度只决定检索优先级，不能让重要性低的伤势、承诺、资源消耗等事实从正史消失。

---

# 58. Task Memory

只保存当前场景、核验、修订所需的结构化决定、产物引用和简短理由，不保存或索取模型隐藏推理过程。

临时缓存完成后可清理；恢复所需的冻结输入、版本、调用台账和提交收据按审计策略保留。

---

# 59. Working Memory

当前 Arc 使用。

包括：

- 最近事件
- 活跃冲突
- 当前关系状态
- 当前目标
- 当前伏笔

---

# 60. Long-term Memory

跨 Arc。

包括：

- 人物长期行为
- 关系演化
- 世界重大事件
- 长期伏笔
- 长期承诺
- 重要资源变化
- 未解决秘密

---

# 61. Permanent Memory

永久规则。

包括：

- 世界观硬规则
- 角色基础设定
- 禁止修改信息
- 核心身份
- 物理规则
- 战力体系
- 固定历史

---

# 62. Memory 写入规则

只有作者明确设定或通过 Scene Commit 的正文事实进入正式记忆。草稿、未采纳计划、Dream 和 Reader Agent 猜测进入各自命名空间，不被 canon 检索默认返回。

派生记忆由 outbox 异步生成，记录源 revision 与构建版本。摘要可以省略细节，但不能改变否定、人物归属、时间或知识边界；关键事实回链权威记录。

读取时先按作品、分支、时点、角色权限过滤，再做语义检索与排序。索引落后时使用权威状态和已提交事件补齐；不能静默使用旧摘要冒充最新状态。

---

# 63. Memory 冲突

冲突必须记录双方内容、源事件、版本、时间和影响范围，不靠“哪条 confidence 更高”覆盖。

先区分真实矛盾、不同时间状态、角色误解、叙述不可靠和已经批准的设定修订。涉及硬事实时回到 canon / event log 核对；无法消解则暂停依赖该事实的场景。

冲突解决以新的修订记录生效，投影随后重建；不得删除旧证据来制造一致。

---

# 64. Knowledge Graph

维护：

```text
Character
Faction
Location
Item
Event
Secret
Relationship
Thread
Foreshadow
Rule
Resource
```

边：

```text
knows
owns
hates
loves
owes
serves
controls
located_at
caused
witnessed
suspects
targets
protects
betrayed
```

---

# 65. Book State

整个项目维护：

```yaml
book_state:
  current_volume:
  current_arc:
  current_time:
  global_threads:
  global_conflicts:
  major_secrets:
  active_factions:
  unresolved_promises:
  structural_risks:
```

---

# 66. Chapter 作为阅读与发布层

故事事实按 Event / Scene 管理；Chapter 负责阅读组织、阶段回报、连载节奏与发布版本。

章节装配选择连续且已提交的场景，遵守故事时间、因果、视角转换和读者信息顺序。可以切分、合并或调整合法的叙述顺序，但不能按字数任意剪断选择与后果。

章节结尾应服务本书读者契约，可以是兑现、余韵、问题或悬念，不强制每章悬崖。补写连接文字若包含新事件或事实，同样走生成、核验与提交流程。

---

# 67. Scene Boundary

自动识别：

- 地点变化
- 时间跳转
- POV 切换
- 核心参与者变化
- 核心冲突变化
- 叙事功能变化

---

# 68. Reader Agents

Reader Agents 作为可选的阅读探针，模拟不同关注点：因果理解、人物动机、关系、悬念、节奏或信息负担。

先独立回答可定位的问题，例如“这场谁想得到什么”“哪句话让你改变对人物的理解”“你认为下一步在等待什么”，再提出体验假设。它们可以帮助发现遗漏，不能代表真实读者偏好或作者本人。

初期不把多读者讨论设为每场必经环节。只在抽样审查、重要节点或已有问题的定位中启用，并计算增量调用成本。

---

# 69. Reader Discussion

先冻结各 Reader Agent 的独立意见，再允许讨论分歧。保存讨论前后变化及引用证据。

不能用互相说服后的多数一致当作独立多评委一致性。讨论只形成待核查问题和可能解释；事实交给权威状态核验，偏好保留分歧。

---

# 70. Human Feedback

作者可以在计划、场景、整章或阅读中随时反馈，不限定为最后一步，也不把持续大批盲评设为使用系统的前提。

优先复用自然发生的操作：保留 / 删除某段、亲自修改、退回原因、指出最不喜欢的一处。必要时提供低频、短小、可跳过的对照；允许打平、两边都不好、不确定与不适用。

反馈用于当前任务改进和作者偏好记录。缺少真实反馈时，可以交付工程闭环并使用软诊断，但必须保留“审美收益尚未证明”的结论边界。

---

# 71. Feedback Memory

```yaml
reader_preference_pattern:
  preference_id: null
  source_feedback_ids: []
  reader_id: null
  project_id: null
  condition: null
  observed_response: null
  scope: project
  counter_examples: []
  evidence_status: observed
  revision: 1
```

scope 可为 reader、project、genre；只有跨范围证据充分时才提出 global 候选。一次删句不自动形成永久禁词，作者的新明确要求可修订旧偏好。个人口味、规范约束和世界事实分开保存。

---

# 72. Feedback 不进入 Language Genome 核心真值

Language Genome 保持通用知识。

Reader Preference 属于：

```text
Project Preference Layer
```

---

# 73. Model Governance

模型注册表统一保存 provider、路由标识、实际模型、支持的任务、上下文 / 输出限制、工具与结构化输出能力、可用状态、价格来源和角色验收记录。

模型列表中出现不等于可用，需有对应任务的真实调用证据。代码、配置和输出均记录请求模型与实际返回模型；未知时明确标记，不能用别名掩盖替换。

已停用模型仅保留历史实验引用，不写回活跃绑定或 fallback。当前网关和本地通道按实际配置选择，不将本次交接中的模型名固化为长期标准。共享通道的串行或并发限制由网关资源锁统一管理。

---

# 74. Agent Model Binding

每个 Agent 可绑定：

```text
Primary Model
Fallback Model
Emergency Model
```

允许锁死。

禁止静默换模型。

---

# 75. Fallback

Fallback 按任务能力、预算和项目约束显式配置，记录原因、原模型、实际模型与提示词 / 参数变化。

先区分：发送前失败、已发送但结果未知、收到无效结果、明确可重试错误。已派发请求超时不等于未收费或未执行；先查 provider 回执与调用台账，不盲目重发。

语义核验不能悄悄替换成纯词表检查；结构化输出失败不得把半截 JSON 当完成。备用路线也须满足该角色契约，不能通过降低门槛让状态变绿。

---

# 76. Streaming

Writer / Research 等长输出支持 streaming。

结构化任务：

默认完整 JSON 返回。

---

# 77. Cost Governance

每次调用记录实际模型、输入 / 输出 token、计费来源及版本、重试、等待、耗时、缓存、费用估计和最终结算（若可获得）。

未知单价或费用用 `null / unknown`，不能记作 0。价格未确定时仍可用调用数、token、运行时长和并发上限控制消耗。

场景成本包含规划、Dream、检索模型调用、生成、评审、修订与派生处理。以成功提交场景的总成本统计，同时单列失败与返工，不只展示 Writer 一次调用的价格。

---

# 78. Budget

支持 task、scene、arc、daily、monthly 上限。首期至少冻结 `max_calls`、`max_input_chars`（保守输入代理）、`max_output_tokens`、`max_elapsed_seconds`、`max_rewrites` 与有限重规划次数；实际 token 可获得时同时记账。

调用前预留预算，检查点与失败调用也计入尝试额度；每次 HTTP 请求使用剩余时长内的超时。不明价格以调用 / token / 时间限额约束，费用保持 unknown，不能声称已实现精确金额封顶。

超限停止或显式省略可选阶段。硬约束与提交核验不得因预算不足跳过；修订次数和预算在恢复后延续，不重置。

---

# 79. 模型角色建议

先用现有可用通道完成单场景闭环，再按角色试验替换。规划看因果与约束，Writer 看目标文体与语义保持，Verifier 看具体错误检出与误报，摘要看事实保真。

不把“贵模型负责所有审查”或“便宜模型一定适合摘要”当架构规则。每次替换使用小型角色任务集、真实路由、失败样本与成本记录验收；不把一次模型排名推广到全部写作任务。

---

# 80. 本地设备策略

本机优先承担文本与数据库处理、检索、编排、前端、缓存和确定性校验。模型推理或训练的位置由实际硬件、可用通道、数据规模和任务时延决定。

CPU、内存、GPU / 显存、磁盘与并发能力在执行前只读探测；不把旧文档中的设备描述当作当前事实。先测代表性小任务的耗时和成本，再判断本机、现有远端或租用算力是否合适。

微调不是运行长篇系统的前提。训练方案、模型尺寸和报价必须另行核实，不由“样本超过一千”自动触发。

---

# 81. 数据库

现有 LG 继续使用其当前数据库与迁移体系，不为方案对齐而整体搬迁。Distiller 保留独立资产库，通过契约交付知识。

Runtime 的权威状态存储必须支持事务、唯一约束、乐观并发与可重放事件。单进程试点可以用 SQLite 验证业务闭环；需要多个写进程或正式并发时，再用 PostgreSQL 验证隔离、锁和冲突处理。

向量库、图数据库、Redis 都是按需组件，不是最小闭环的安装清单。语义检索和派生缓存不能绕过权威数据库的版本与访问范围。

---

# 82. 核心表

按阶段增量建立，以下为逻辑实体，不要求首期全部创建：

```text
books / volumes / arcs / scenes / scene_versions / scene_commits
events / state_patches / world_snapshots / canon_revisions
characters / character_states / relationships / factions / locations / items
threads / foreshadows / expectations / reader_exposure
dream_sessions / dream_branches / predictions / prediction_results
memories / memory_conflicts / projection_versions
context_packages / knowledge_queries / knowledge_snapshots
writer_runs / critic_runs / issues / rewrite_runs / reader_reviews
model_runs / call_attempts / jobs / checkpoints / outbox_events
```

核心唯一键、book / branch 隔离和版本约束由数据库强制。仅在提示词里要求不重复，不算工程保证。

---

# 83. Language Genome 独立表

现有数据库实际使用 `works`、`segments`、`frames`、`expression_strategies`、`hard_cases`、`benchmark_sets`、`benchmark_items`、`benchmark_runs` 等表。

SemanticFrame、StrategyInstance、FailurePattern 是逻辑概念，不能把概念名写成已经存在的物理表。新增 Runtime 使用独立命名空间与迁移，知识提供方业务表保持其所有权。

---

# 84. Workflow Runtime

逻辑任务状态：

```text
pending → running → completed
             ├→ retry_wait
             ├→ paused / blocked
             ├→ failed
             └→ cancelled
```

模型调用另有 `prepared / dispatched / succeeded / failed / interrupted_unknown` 状态；任务已取消不代表上游调用已停止。保留账单与迟到结果的归属，禁止迟到结果越过版本检查自动提交。

任务领取采用租约、心跳与 fencing token，避免失联旧 worker 恢复后继续写入。completed 表示该阶段契约成立，必须区分草稿完成、核验完成、场景已提交与作品已发布。

---

# 85. Job

```yaml
job:
  id: null
  type: null
  parent_job_id: null
  book_id: null
  branch_id: main
  scene_id: null
  expected_revision: null
  stage: null
  status: pending
  input_hash: null
  output_ref: null
  idempotency_key: null
  lease_owner: null
  lease_expires_at: null
  fencing_token: null
  attempt_count: 0
  max_attempts: null
  checkpoint_ref: null
  budget_ref: null
  error_code: null
  created_at: null
  updated_at: null
```

长正文和上下文用不可变 artifact 引用传递；审计日志不输出凭据。执行时冻结所需版本，不能仅凭当前全局配置恢复旧任务。

---

# 86. 幂等

任务去重键至少涵盖 book、branch、scene、stage、输入哈希、世界 revision 和实现 / 提示词版本。相同幂等键但不同输入必须报冲突。

数据库使用唯一约束保障 scene commit 与状态补丁至多生效一次；outbox 与派生索引用事件 ID / revision 去重。

外部模型调用通常不能承诺 exactly-once。将调用请求与尝试单独记账；有 provider 幂等能力时使用，没有时显式记录结果未知与重试决策，避免把数据库幂等误写成上游绝不重复计费。

---

# 87. Checkpoint

在计划冻结、上下文编译、有效模型结果落盘、Issue 确认、最终文本冻结和事务提交后设置检查点。

检查点记录输入 / 输出哈希、世界 revision、计划与知识包版本、模型实际路由、预算余量及当前阶段。恢复前验证 artifact 存在且未改变。

部分流式输出只能用于预览，不是可恢复的已验收正文。提交后恢复应返回原收据并补做派生任务，不重新生成已提交场景。

---

# 88. Failure Recovery

失败先分类处理：

| 失败 | 恢复方式 |
|---|---|
| 确定性输入 / schema 错误 | 修正输入或实现，不重复请求同样的错误 |
| 模型临时不可用 | 在剩余预算和重试上限内按治理策略重试 / 切换 |
| 已发送、结果未知 | 核对调用记录与上游回执，保留 unknown；不自动假定未执行 |
| 状态 revision 冲突 | 重新读取状态、编译上下文并复核受影响产物 |
| 正文不满足硬约束 | 局部修订或退回规划；超过限额进入明确的 blocked / failed |
| 提交事务中断 | 回滚；以同一幂等键重试，返回唯一提交结果 |
| 派生索引失败 | 保留已提交正文与事实，重放 outbox；标明索引落后 |

恢复能力通过故障注入验收，不以“有 retry 字段”代替。

---

# 89. Pipeline

正式执行顺序：

```text
1. 冻结 Book / Branch / World Snapshot / 读者契约与预算
2. Planner 生成或更新 ScenePlan
3. 可选 Dream → 选择路线 → 冻结计划版本
4. 查询 Genome / Distiller / 相关记忆，保存知识快照
5. Context Compiler 编译并验明权限、版本与预算
6. Writer 生成草稿，保存不可变 artifact
7. 硬约束校验 + 按需 Critic，确认具体 Issue
8. 在预算内局部修订；结构性问题退回 Planner
9. 对最终文本复核语义、连续性与已确认 Issue
10. 抽取事件与 StatePatch，核对证据和 expected_revision
11. 原子提交正文、事件、状态变化与 outbox
12. 更新派生记忆 / 图谱 / 章节候选，记录投影版本
13. 可选读者探针 / 自然反馈，保留质量结论边界
14. 按依赖与预算进入下一场景
```

可选阶段在运行清单中显式标记 skipped / reason；硬约束和提交协议不能因预算不足而跳过。§142–143 复用本流程，不另设相反执行顺序。

---

# 90. Dream Trigger

仅在计划中明确出现以下情况时考虑 Dream：重大不可逆决定、多条剧情线争用同一资源或时点、某条路线可能造成难以恢复的死路、需要比较几种不同代价。

首期建议上限为 3 条候选、每条 2–3 步事件滚动；这是成本起点，后续按收益证据调整。常规过渡、局部表达和信息已充分的场景不触发。

不能仅因模型自报低 confidence 就无限加推演。需要列出不确定事实、候选分歧和停止条件。

---

# 91. Deep Dream

Deep Dream 用于卷级或关键 Arc 节点的专项规划实验，明确任务和资源额度后运行，不作为每章的固定费用。

继续满足分支隔离、只读正史、输入快照与调用预算。对照普通规划，检验是否减少后续返工或结构死路；没有可测收益时保留简单路线。

---

# 92. Anti-Loop

系统必须检测：

- 同类冲突重复
- 同类场景重复
- 相同套路重复
- 相同 Strategy 重复
- 相同角色反应重复
- 相同 payoff 重复

---

# 93. Novelty Memory

记录近期：

```text
冲突类型
场景类型
表达策略
转折类型
关系推进方式
```

用于防重复。

---

# 94. Continuity Gate

以最终文本与固定世界快照检查：实体与时空、角色知识、能力与资源、事件前置条件、关系跃迁、已建立承诺、叙述曝光和计划必需事件。

只有确认且有证据的硬约束问题阻断提交。发现含混时要求定位或重新抽取，不把评委一句“感觉不合理”当事实。

新 revision 到来、正文改变或计划重排后，复核相关依赖；旧版本上的 pass 不能直接放行新产物。

---

# 95. Issue Severity

| 等级 | 定义与处理 |
|---|---|
| blocker | 作品 / 分支污染、状态提交不一致、明确违反不可变世界规则等；必须解决 |
| major | 改变关键事件、人物知识、因果或理解的已确认问题；修订或重规划 |
| minor | 局部清晰度和表达问题；按收益及剩余预算修订 |
| suggestion | 尚未验证的风格偏好、替代路线；默认不阻断 |

严重度与证据状态分开。尚未确认的 blocker 候选先核查，不靠反复重写“碰运气”消除。

---

# 96. Rewrite Loop

首期默认最多 2 轮局部修订、1 次重规划，并同时受场景总预算约束；这些是可配置运行上限，不是质量定律。

每轮记录本轮目标、文本差异、已解决 / 新增 Issue、语义变化和成本。没有进展、问题来回反转或评委分歧持续时停止自动润色，保留最佳有效版本与明确原因。

确认的硬问题未解决则不提交；仅剩风格建议时可按项目策略保留可用稿。不得在“通不过就重写”的循环中无限调用，也不把超时自动当作 pass。

---

# 97. Chapter Assembly

Chapter Builder 消费已提交场景及其 revision，按叙述计划装配章节。检查时间、视角、阅读曝光、承诺推进与场景接缝，并记录成员顺序和正文哈希。

连接性文字若只做格式处理无需模型；若新增内容，生成单独补段并经过同样的语义与状态检查。不能在装配时偷偷引入新事实。

章节可处于 assembled、reviewed、ready_to_publish、published 等独立状态。生成或提交场景不自动发布作品；发布目标及权限由产品单独控制。

---

# 98. Long-form Planning

长篇不提前写死几千章。

采用：

```text
Stable Long-term Objectives
+
Flexible Mid-term Arcs
+
Dynamic Scene Planning
```

越远：

越抽象。

越近：

越具体。

---

# 99. Planning Horizon

```text
Book Horizon
Volume Horizon
Arc Horizon
Near Scene Horizon
Immediate Beat Horizon
```

禁止把远期计划写得和当前场景一样细。

---

# 100. Uncertainty

所有预测 / 计划允许：

```text
UNKNOWN
MULTIPLE
LOW_CONFIDENCE
```

不得强行确定。

---

# 101. Hidden State

人物真实意图未知时保留多种假设，每条记录支持证据、反证、适用时点与来源。未校准的概率留空。

世界层事实、角色信念、读者理解和模型猜测分开。后续已提交事件提供新证据时更新假设，保留历史解释；不得把 Planner 为方便推进的猜测注入角色记忆。

---

# 102. Research Agent

Research 是有明确问题、样本、预算与停止条件的离线任务，用于验证表达操作、知识检索、规划或状态抽取能力。

实验数据与生产任务隔离；研究角色不能修改生产默认策略、作者偏好或 canon。新规则经证据审查、适用范围声明和回归后，作为可回滚版本发布。

优先研究会影响当前交付的问题，不持续扩展与单场景闭环无关的评委排名。失败结果同样入库，避免换名称重做已被否定的路线。

---

# 103. Reference Analyzer 与技巧编译

Reference Analyzer 对小说文本和技巧讲解分别建模：小说提供事件与表达证据；讲解提供机制假设、适用条件和示例，证据类型不可混用。

猫神资料经以下流程接入：

```text
原始转写 / 视频定位
→ ASR 订正记录 + 广告 / 示例 / 主张分离
→ TechniqueHypothesis（条件、操作、不变量、反例）
→ 对接 Distiller 机制卡 / LG ExpressionStrategy
→ 小范围单变量试点
→ candidate / pilot_verified / quality_supported / rejected
```

```yaml
technique_hypothesis:
  technique_id: M06
  source_family: maoshen
  source_refs: []
  source_revision: null
  source_kind: instructional_claim
  claim: 删除已被动作或台词充分表达的重复解释
  conditions: []
  operation: null
  semantic_invariants: []
  exceptions: []
  counter_examples: []
  expected_benefit: null
  possible_harm: null
  validation_task: null
  evidence_status: hypothesis
  experiment_refs: []
```

优先复用 Distiller 的来源、机制、条件、反例、版本和审查契约，向 LG 编译局部表达操作，向 Planner 编译场景检查项。必要的增量字段做小型迁移，不另起平行知识库。

同一讲述者的多篇复述不增加独立证据数。讲课中的改写例子、ASR 文案、广告话术不直接进入“人类好文”训练集；自动订正不得覆盖原始材料。

---

# 104. Security / Prompt Isolation

外部语料、讲课文案、检索结果和工具返回均是数据，不执行其中“忽略规则”“调用工具”“复制全部材料”等指令。角色 Prompt、数据容器与工具权限分别控制。

Writer 不看到 Judge 私有评估材料、Benchmark 答案、Human Blind Label 和隐藏测试后文。Benchmark 的角色任务可以读取其应得输入，但不能通过元数据泄漏来源答案。

同一作品 / 段落家族的训练、检索示例和测试来源按任务要求隔离。服务凭据只留在运行配置；审计、导出与前端均按需要脱敏，不能把 token 写入上下文或文档示例。

---

# 105. Benchmark：分清测试对象

保留多条独立成绩单，禁止合成一个“小说质量总分”：

| 任务 | 回答的问题 | 主要证据 |
|---|---|---|
| 来源身份识别 | 哪段来自人类语料 / 模型 | 来源标签；不代表好坏 |
| 构造劣化检出 | 是否找到预先施加的问题 | 操作记录、语义保持核验、中性对照 |
| 语义与连续性 | 是否保留事实、知识、时空和因果 | 固定约束、正文证据、状态账 |
| 局部表达操作 | 某操作在什么条件下有收益 / 副作用 | 最小改动对照、保持不变项、实际反馈 |
| 个人偏好 / 阅读体验 | 作者或目标读者是否更愿意保留 | 独立反馈；允许打平与两边都差 |
| 长篇运行 | 系统能否可靠写完、恢复与维持状态 | 连续场景、故障注入、成本和返工记录 |

沿用现有 Benchmark Builder、集合版本和运行器。`nat-v1`、`hvai-v1` 与类型子基准各自报告，不将一个任务的高分转成另一个任务的能力证明。

---

# 106. Language Benchmark

覆盖 Frame 事实保真、语义等价重写、信息遗漏 / 增写、视角、对话归属和局部操作的适用性。人类原文可以是信息参照，但不自动成为偏好优胜者。

猫神策略使用最小改动对照：例如删重复解释时不同时添加比喻、缩短情节和改变角色态度。加入中性改写、边界反例与“不应修改”样本，检查误报和副作用。

清洗引起文本变化时生成新的数据版本与 benchmark manifest。原始和清洗版本、同段不同候选属于同一来源家族，不跨 train / validation / hidden test 泄漏。

---

# 107. World Model Benchmark

覆盖实体归属、时间先后、角色知识与误解、资源 / 伤势 / 物品持有、关系变化、伏笔状态、隐含前置条件和新事实提案。

除正确样本外，加入缺失证据、同名人物、跨分支事实、陈旧 revision、矛盾抽取及重复事件。验收既看错误检出，也看有效文本的误拒。

硬事实通过可追溯证据核对；含混情绪或人物意图允许 unknown，不强求每个字段都有答案。

---

# 108. Planner Benchmark

以固定世界状态和读者契约比较计划：行动是否符合人物目标与知识、是否具备前置条件、是否产生持续后果、是否推进适用期待、是否保留后续空间。

对比普通规划与增加机制卡 / Dream 的规划，保持总调用预算可比。计划描述得更长、冲突更多、评委打分更高都不是独立收益。

结构问题要能回链到计划字段和后续失败事件，统计实际返工、死路与重复情节，不只评第一眼的精彩程度。

---

# 109. Writer Benchmark

固定 ScenePlan、World Snapshot、上下文版本与预算，分别比较基线、单条表达策略和机制卡组合。

先比较事实保持、信息分配、视角、人物声音与具体问题；再收集低负担的真实保留 / 修改反馈。长度、角色、题材和场景功能差异需要分层报告。

没有人类偏好证据时，结果名称只能是约束通过率、问题检出 / 修复率等，不命名为“审美胜率”。一个评委既生成标签又验收其训练产物时，必须另有独立评价来源。

---

# 110. Long-term Benchmark

按 3 场景 → 10 场景 → 100 场景逐级扩展，记录人物 / 时间 / 资源 / 信息连续性、未完成期待、重复情节、返工、成本与人工介入。

至少包含一次跨场景延迟后果、一次角色信息不对称、一次伏笔回收，以及中断恢复、重复提交、状态冲突与派生索引失败。恢复后核对正文与 canon 的一致性。

更大字数与千章容量单独做存储、查询、恢复及长距离回链验证。不要把许多互不相关的短场景累加为已经通过长篇验收。

---

# 111. Prediction Benchmark

独立未来预测遵守 §21：先冻结输入与预测，再揭示未参与生成的后续证据。按作品 / 事件链留出，防止检索把答案带入上下文。

本系统依照自身计划生成的“预测命中率”只归入计划执行一致性。独立预测没有证据时保持未验证，不阻塞最小写作闭环。

---

# 112. Experiment Discipline 与训练条件

每个会影响效果结论的实验记录 Hypothesis、Control、Variant、Metric、Result、Decision，注明输入 / 提示词 / 模型 / 路由 / 数据版本、预算和停止条件。普通格式调整无需文学实验。

## 112.1 报告口径

- 同时列出分配总数、有效回答、拒答、错误与缺失；缺失不能静默从通过率分母移除。
- 统计独立单位通常是源段落、场景、作品或来源家族；同一段的多种劣化、多个模型输出与多次评判不是新的独立文本。小量作品的外推限制单列。
- 对同一批样本的前后变化使用配对分析；按源段落 / 作品聚类计算区间。样本很少时优先展示原始计数和不确定性。
- 先规定标签含义和指标；来源身份、自然度、质量与个人偏好不混用。允许不确定、打平、两边都不好。
- 规则从一个集合发现，不能只在同一集合宣布有效；按作品、段落家族与近邻泄漏风险做分割和去重。
- 评委出现位置、长度或来源偏差时，使用交换位置、隐藏来源与对照复核；小样本“未显著”不等于排除了某种偏差。

现有材料已经显示标签与任务边界的重要性。LLM 评委偏差研究可作为设计依据，不能将其他任务的结论直接迁移为本项目成绩：参见 [MT-Bench 评委研究](https://arxiv.org/abs/2306.05685)。局部反馈可借鉴 [细粒度反馈研究](https://arxiv.org/abs/2306.01693)，但中文长篇收益仍是待验证假设。

## 112.2 当前训练资产的真实含义

| 导出 | 09-19 核验结果 | 使用边界 |
|---|---|---|
| `writer_sft_v3` / `rewrite_v2` | 各 1,798 行；1,798 个 frame 对完全重合，源段落 1,396 个 | 两种格式的同源监督；不是翻倍数据，也非全部人工认可好文 |
| `rm_v1` | 1,316 行：user_verdict 362、judge_majority 764、corruption_variable 190 | 多种标签来源的文本记录，不是 1,316 次独立人工偏好；弱标签单列 |
| `corrupt_negatives_v2` | 103 条 | 劣化负例，需保留操作与语义漂移检查，不用于证明自然正例质量 |
| `corrupt_dpo_v1` | 244 对 | 默认 chosen 方向缺少用户支持；严格筛选极少，不得直接当高质量偏好库 |
| `ai_ranking_v1` | 09-19 21:40 已有 105 对 | 多评委共识仍是弱标签，需先核对长度与模型来源偏差 |

训练导出能力已经存在；**先可运行，后证明训练有收益**。开启 SFT / LoRA / DPO 等实验前至少满足：使用范围允许；来源 / 标签可追溯；训练验证按来源家族隔离；目标能力明确；有独立基线与回归；样本质量抽查和漂移控制完成；硬件、时间与费用已实测或可靠估算。

不设“达到 1,000 条就值得训练”的通用门槛，不把模型原文身份当 chosen 标签，不把对评委拟合更好当作者更喜欢。训练是可选分支，不能延迟 World / Context / Commit 的工程闭环。

---

# 113. Frontend 总体

复用现有 LG 研究工作台，保持两种用途边界：

- **研究与诊断**：当前 `/lab/*` 新页面、`/console/*` 只读 API 和根路径盲评；接入真实数据、统一外观和状态反馈，完成既有功能回归。
- **作品生产**：后续 Book Workspace 提供计划、场景、正文、Issue、状态提交和发布准备；这些是新增能力，不能用静态页面冒充已打通。

当前已完成首轮接入；后续按 [09-19 前端接入计划](frontend-plan-2026-09-19.md) 完成路由、十个 slug 对齐、接口 envelope 与字段形状对齐。保留跨实验取题、上下文范围、批注和反锚定行为；结合 `tests/test_frontend_invariants.py` 做针对性验证。

作品主界面优先展示“当前稿、哪里有问题、下一步、耗时 / 预算”。Context、模型路由、状态补丁等放诊断区按需展开，不把工程术语变成作者每次必须填写的表单。

---

# 114. Dashboard

研究 Dashboard 显示真实数据量、任务状态、失败与缺失、策略证据等级、预算和数据更新时间。n=0 显示无数据，不伪造 0% 或 100%。

作品 Dashboard 显示当前场景 / 章节、已提交进度、尚未解决的问题、阻塞原因与下一步。区分草稿完成、场景已提交、索引落后与章节可发布。

所有完成比例必须能回到原始计数和口径，不能把 pending 任务算进已完成，也不使用单一 AI 味分数代表质量。

---

# 115. Books

两层导航：

```text
Bookshelf
→ Book Workspace
```

---

# 116. Book Workspace

以场景列表与正文为中心，提供计划 / 正文对照、人物与世界状态摘要、可定位 Issue、局部修改差异、版本历史和恢复入口。

默认只展示作者当前需要决定的内容。知识策略、事件图、状态补丁、Context manifest 与调用记录按需展开。

预览稿、已核验稿、已提交稿和发布版本采用清楚的状态标识。界面上的“接受改动”对应明确的文本版本和影响范围，不能暗中提交另一个 revision。

---

# 117. Dream UI

在关键路线比较时显示候选的选择、代价、前置条件、风险与后续空间，并标明“模拟，尚未发生”。

采用某条路线只更新计划版本，界面不得把模拟事件显示为作品事实。保留输入快照、分支和预算；普通写作时隐藏未启用的 Dream 面板。

---

# 118. Context Inspector UI

显示每次 Agent：

```text
Injected
Excluded
Source
Token
Reason
Version
```

---

# 119. Model UI

显示：

- Provider
- Model
- Health
- Cost
- Latency
- Fail Rate
- Role Binding
- Fallback
- Context
- Streaming
- JSON 能力

---

# 120. Task UI

主视图显示作品、场景、当前阶段、已确认问题、预算余量与下一步；进度分母来自本次实际启用的阶段，不固定写成 12 步。

详细调用、版本和错误放可展开诊断区。失败、结果未知、等待恢复与已提交有不同状态，不用持续转圈掩盖阻塞。

---

# 121. Observability

复用已有 LG 模型统计与实验观测；Runtime 增加 book / branch / scene / stage / attempt / commit 的关联 ID。

记录队列等待、有效生成时长、token 与费用、重试 / fallback、未知结果、Issue 解决与误报、提交冲突、outbox 积压、投影延迟及人工介入。

每个状态可回链输入输出哈希、版本和错误码；日志默认只存必要元数据，原文与敏感配置按权限查看。发布“成功率”同时报告样本数和缺失，避免用纯函数统计通过替代真实运行验证。

---

# 122. 数据备份

分别定义权威数据库、原始语料与修订、不可变正文、知识快照和配置版本的恢复策略。派生索引可重建，不必与权威数据使用同一种备份频率。

活跃 SQLite 使用一致性备份机制，不能只复制正在写入的主 db 文件并遗漏 WAL。恢复演练核对提交日志、正文哈希、世界 revision 和 outbox 进度。

明确保留周期、恢复点与恢复时间目标；以实际恢复成功为证据，不以“备份文件存在”作为通过。

---

# 123. Versioning

版本覆盖 Prompt、角色、实际模型 / 路由、Compiler、World / Frame / Strategy schema、Planner、Writer、Critic、Benchmark、原始 / 清洗文本、知识包和训练导出。

每次运行冻结 manifest：代码 revision（含必要工作区差异摘要）、数据快照、参数、随机种子（可用时）、各阶段版本、输入输出哈希和模型实际响应标识。结果可追溯不等于外部模型逐字可复现。

升级不能让运行中的任务混用新旧版本。数据迁移保留来源对应关系与回滚 / 重建方法；已发布文本与 canon 的变更各自留痕。

---

# 124. 项目仓库与复用边界

当前以 `F:/agi/language-genome` 为主项目，`F:/agi/novel-distiller` 为叙事机制知识源；`F:/agi/novel-hub` 的现有导入、上下文、反馈与写作流程作为可复用资产审计。

先核对真实路由、数据模型、版本、权限和故障行为，再决定适配还是独立实现。已有 Distiller → Novel Hub 链路通过，不代表 Hub 全部功能健康，也不代表 LG → Runtime 已接通。

长篇 Runtime 是明确逻辑边界。若复用现有宿主会导致状态所有权混乱，再创建独立 `novel-runtime`；不把新仓库、新前端、新网关和新数据库作为开工前提。本次方案修订不表示这些组件已经创建。

共享的是契约与经过核验的基础能力，不跨项目直接改写对方业务表。

---

# 125. Language Genome / Distiller 接入方式

Runtime 通过可版本固定的 API 或离线包消费知识。允许按哈希缓存本次使用的包与必要证据，避免服务更新使同一次写作输入漂移；不复制整套业务库充当新真值。

LG 提供表达策略；Distiller 提供叙事机制、条件与反例。二者统一到包外壳，但保留各自证据语义和来源等级。

当前 `/corpus/import-distiller` 只说明已有语料适配能力，不能代替机制查询、策略编译、版本协商与 Runtime 回执的完整集成。首期用固定离线包证明消费契约，再连接实时 API。

无可用知识时记录 empty / unavailable / unsupported，并可走明确的基线生成模式；世界事实缺失不能用知识回退掩盖。

---

# 126. Writer Bridge：待实现契约

以下接口是目标设计，当前不能按已存在调用：

```text
GET  /genome/capabilities
POST /genome/query
GET  /knowledge/packages/{package_id}
```

查询包含目标任务、场景功能、语义约束、项目风格、允许的数据范围、所需 schema 和包版本。响应为 §5 的知识包，区分 compatible、empty、unsupported、unavailable；不能用空数组伪装服务错误。

Bridge 适配既有 ExpressionStrategy / Hard Case 与 Distiller 契约，验证条件、反例、证据、来源隔离和版本可追溯。测试必须覆盖无匹配、旧版本、错误 book scope、缺失证据与服务不可用。

角色职责：检索器选择可能适用知识，Compiler 决定进入上下文的内容，Writer 不取得跨语料扫描权限。

---

# 127. World Model API：待实现契约

```text
GET  /books/{book_id}/branches/{branch_id}/world?revision=...
GET  /books/{book_id}/branches/{branch_id}/events
POST /books/{book_id}/branches/{branch_id}/state-proposals/validate
POST /books/{book_id}/branches/{branch_id}/scene-commits
```

所有后续 API 共用 book / branch 作用域、调用权限、请求 ID、schema 版本及可定位的错误响应。写操作带 idempotency_key 与 expected_revision；状态冲突返回明确冲突码。

普通 Agent 只能提交提案；只有 Runtime Commit 路径具有正式写权限。禁止暴露可绕过正文证据与版本检查的通用 `/world/update`。

---

# 128. Planner API：待实现契约

在 §127 的作用域下提供 Book / Volume / Arc / Scene 计划查询、生成与修订。响应带计划版本、源世界 revision、依赖与未解决缺口。

重规划不能悄悄覆盖已经提交的场景；返回受影响的未提交计划范围。远期计划允许多个路线和 unknown，不能为了 schema 齐全强造事实。

---

# 129. Dream API：待实现契约

提供创建模拟任务、读取分支、比较候选和停止任务接口。输入固定快照、干预与预算；输出含 simulation 标识、候选状态、证据和成本。

采纳接口只生成新的计划版本，不调用 canon commit。所有 Dream 数据与正式记忆隔离。

---

# 130. Writer API：待实现契约

根据冻结 Context Package 创建写作任务，提供进度、预览和最终 artifact 查询。结果符合 §37，并返回实际模型、输入哈希与状态。

Writer API 不负责发布，也不直接修改人物、世界或记忆。预览流中断与完整有效结果使用不同状态。

---

# 131. Review API：待实现契约

针对指定文本哈希、计划版本与世界快照创建核验任务；Issue 遵守 §52。区分硬约束核验、风格诊断和读者探针，分别展示结果。

确认、驳回与解决 Issue 都保留证据。正文变化后旧结论标记过期或重新核对，不允许对新版文本沿用旧通过收据。

---

# 132. Memory API：待实现契约

提供按 book / branch / 时点 / 角色权限检索、查看来源、重建投影和查询投影进度的接口。

Memory 不提供给普通 Writer 任意写入永久事实的接口。派生写入来自已提交 outbox；作者新增设定走有审计的 canon revision。

---

# 133. Full Scene Request

请求结构示意（实际调用时必填字段不得为 null）：

```yaml
scene_request:
  request_id: null
  idempotency_key: null
  book_id: null
  branch_id: main
  scene_id: null
  expected_world_revision: null
  plan_revision: null
  goal: null
  mode: draft
  knowledge_mode: optional
  dream_policy: off
  budget_policy_ref: null
  style_profile_ref: null
  output_policy_ref: null
```

服务端可以根据作者已选项目设置补足常规参数，但受理后必须冻结并回显有效配置。`draft` 返回候选稿；只有具备相应权限的 `generate_and_commit` 才可执行 §56。发布使用独立入口。

同一请求不同输入报冲突；没有预算策略、世界版本或必要约束时返回具体缺口，不无限猜测重试。

---

# 134. Pipeline Result

```yaml
scene_result:
  request_id: null
  scene_id: null
  stage_status: draft
  final_artifact_ref: null
  text_hash: null
  world_revision_before: null
  world_revision_after: null
  plan_ref: null
  context_package_ref: null
  knowledge_package_refs: []
  dream_ref: null
  issue_refs: []
  verification_receipt_ref: null
  proposed_state_patch_ref: null
  commit_receipt_ref: null
  projection_status: pending
  model_run_refs: []
  budget_usage: {}
  cost: null
  unresolved: []
  next_action: null
```

只有 commit_receipt 存在且事务成立时，才返回 committed 与新世界 revision。draft、verified、committed、published 含义不同；正文生成完不自动等于章节可发布。

---

# 135. 自动运行

自动运行按已授权的作品目标、范围、预算和停止条件推进；每次选择下一个满足依赖的场景，不把“持续写作”解释为无限调用。

在已确认硬问题未解决、关键事实冲突、预算耗尽或上游结果未知时安全停止，并保存恢复点。普通局部修订在已设置规则内自动完成，无需反复请求作者确认。

停止操作先冻结新任务领取，处理在途结果与事务边界，再返回准确状态。不能因用户点停止就留下半个世界状态，也不能把停止误报为成功完成。

---

# 136. Chief Agent

Chief 是任务分派、预算与阶段验收职责，优先由确定性状态机执行。只有规划分歧、研究决策或异常解释需要模型辅助。

Chief 根据 artifact、版本、Issue、预算与收据推进，不采信下游一句“全部完成”。同一执行者可以承担多个角色，但产出与核验职责仍有明确边界。

本方案描述未来系统的角色，不要求当前任务自动启动多代理，也不赋予任意外部服务操作权限。

---

# 137. Chief Decision

必须基于：

```text
state
metrics
issues
budget
```

不能凭自然语言随意决定。

---

# 138. Agent 通信

使用结构化 envelope 传递状态、版本、输入输出引用与错误，必要的理由和问题解释可以用简短自然语言。

```json
{
  "schema_version": "agent-envelope/1",
  "task_id": null,
  "book_id": null,
  "branch_id": "main",
  "stage": null,
  "input_refs": [],
  "result_refs": [],
  "status": "pending",
  "evidence_refs": [],
  "issues": [],
  "uncertainty": [],
  "error": null
}
```

不靠长篇对话维持事实账，不传无来源的精确 confidence，不在消息中复制凭据、整本语料或隐藏推理。

---

# 139. 禁止静默降级

任何：

```text
模型切换
上下文裁剪
忽略错误
省略 Stage
跳过 Critic
```

必须记录。

---

# 140. 关键 Gate 与验收证据

工程门与效果门分开：前者决定能否安全运行，后者决定能否宣称某策略改善阅读质量。

| Gate | 放行证据 | 未通过时 |
|---|---|---|
| G0 契约与来源 | 必填字段、版本协商、来源引用、空结果 / 失败区分；训练 / 测试不泄漏 | 修适配或使用明确基线；不能捏造知识 |
| G1 状态与权限 | 正文 / 事件 / 状态同事务；重复请求、过期 revision、跨作品 / 分支污染测试通过 | 不放行正式提交 |
| G2 单场景 | 固定快照可完成 §89；确认的硬 Issue 为零；正文、补丁和收据可对账 | 有限修订或退回计划 |
| G3 恢复与成本 | 中断、已发送未知结果、outbox 失败、重复执行均有正确恢复；预算能停止 | 保留试验模式，不宣称稳定无人值守 |
| G4 连续场景 | 3 → 10 → 100 场景逐级通过，跨场景后果与角色知识正确；记录人工介入 | 保持当前规模，修已复现的问题 |
| G5 策略效果 | 独立对照、无语义损失、适用范围与反例明确；审美结论有真实反馈 | 留作 hypothesis / pilot，不设全局硬门 |
| G6 发布准备 | 章节版本、成员场景、正文哈希、发布目标与产品权限一致 | 保留已提交草稿，不自动发布 |

验收包至少含运行 manifest、代表性正文、Issue / 修订差异、状态补丁、提交收据、失败恢复记录与成本摘要。单测绿、页面截图或模型高分只能证明对应一部分。

---

# 141. 现阶段开发优先顺序

以现有 LG 建设成果为起点，按交付链排优先级：

| 阶段 | 建设内容 | 可验收交付 | 不作为前置条件 |
|---|---|---|---|
| S0 资产与契约接通 | 复用已接入前端并核对边界；审计 LG / Distiller / Hub；定义知识包和只读适配 | 真实数据可见、原盲评行为保留、固定知识包可消费 | 新网关、新研究前端、重复建设实验引擎 |
| S1 故事最小内核 | Book / Scene / World Snapshot / Event / StatePatch；幂等、版本冲突、Scene Commit 与 outbox | G1；用确定性样例验证一次提交和失败回滚 | 大量训练数据、Dream、向量数据库 |
| S2 单场景闭环 | Planner、检索、Compiler、Writer、硬校验、有限修订、抽取与提交 | G0–G3；能对账的一场景正文与状态 | 可靠万能审美评委、1 万条 frame、持续人工盲评 |
| S3 连续写作 | 3 → 10 场景；人物知识、期待账、余波、章节装配、投影重建；再扩到 100 场景 | G4；真实恢复与成本报告 | 全自动美学评分和全面模型微调 |
| S4 有边界的增强 | 先试 M04 / M01 / M06；机制卡、Reader 探针、关键节点 Dream 分别做消融 | 可说明增量收益及代价的策略版本 | 一次启用全部技巧、多 Agent 必开 |
| S5 长篇与可选训练 | 长距离回链、卷级规划、容量与稳定性；数据成熟后独立训练实验 | 长篇质量 / 容量分开报告；训练有独立回归 | 单凭字数宣告完成或单凭样本量启动训练 |

研究工作可以并行于工程，但不能长期占用全部产能。出现具体生产错误时优先修复；没有新的决策价值时停止追加同类评委测量。

**下一个开发任务包**应具体到：版本化 ScenePlan / WorldSnapshot / StatePatch schema、单事务 commit 与幂等键、固定知识包适配、单场景 manifest 与故障恢复验收。先把这条链做成，再扩数量。

---

# 142. 最小正式闭环

使用 §89 的顺序，默认不启用 Dream 与 Reader Discussion：

```text
作者目标 + 固定世界快照
→ ScenePlan
→ 查询 / 固定知识包
→ Context Compile
→ Draft
→ 硬约束核验 + 必要局部修订
→ 最终文本的事件 / StatePatch 核验
→ 原子 Scene Commit
→ 派生记忆 + 可恢复收据
```

完成定义：一份可读正文、相符的事件和状态、零未解决的已确认硬问题、明确成本与缺失、可重复请求和中断恢复。风格建议可保留，不能伪称文学效果已经证明。

---

# 143. 完整正式闭环

在最小闭环上按需加入卷 / Arc 规划、版本化 Distiller 机制知识、关键节点 Dream、期待与伏笔回收、章节装配、Reader 探针、自然反馈与候选策略实验。

所有增强沿用相同的事实权限、上下文快照、提交协议、预算和证据门；不得形成另一条先更新记忆再保存正文的旁路。

反馈推动的是待验证规则或明确的作者偏好；独立预测结果才进入预测校准。新策略经版本化放行后作用于未来运行，不悄悄改写历史结果。

---

# 144. 分阶段验收清单

以下是未来验收项，不代表本次文档修订已经实现：

**最小可用系统（S0–S2）**

- [ ] 现有研究前端路由、数据与盲评关键行为有针对性回归证据。
- [ ] LG / Distiller 知识契约可消费；来源、版本、空包与服务失败可区分。
- [ ] 同场景重复请求只产生一次有效 commit；相同键不同输入被拒绝。
- [ ] 过期 revision 与跨 book / branch 访问被拒绝，不产生部分状态。
- [ ] 最终正文、事件、StatePatch、世界 revision 和提交收据逐项可对账。
- [ ] Context 的角色知识与读者曝光边界有效，硬约束未被静默裁剪。
- [ ] 已确认硬问题未解决时不会提交；风格建议与硬阻断清楚区分。
- [ ] 在模型超时、提交中断和 outbox 失败下恢复正确，预算达到上限能停止。

**连续写作（S3）**

- [ ] 3 场景与 10 场景分别有连续性、成本、返工与介入报告；通过后再扩 100 场景。
- [ ] 角色信息不对称、延迟代价、关系演化和伏笔回收均有正文及状态证据。
- [ ] 派生记忆损坏或落后可重建，读者曝光顺序与章节成员版本一致。
- [ ] 场景提交、章节审阅、发布准备与发布状态没有混淆。

**增强与规模（S4–S5）**

- [ ] 每项技巧 / Dream 增强有独立基线、适用条件、反例、成本和失败样本。
- [ ] 来源身份识别、构造劣化检出、个人偏好与实际正文收益分开报告。
- [ ] 训练数据来源与用途明确，去重 / 留出 / 标签质量通过，训练收益有独立验证。
- [ ] 长篇存储、查询、回链和恢复能力实测；达到字数不替代可读性验收。

每项通过都关联证据路径、代码 / 数据版本、日期和范围。没有证据则保持未验收，不用占位 UI、预定样本量或口头总结打勾。

---

# 145. 系统最终定位

本系统是“有故事状态的写作工作流 + 可验证的知识资产 + 有限自动改进”，目标是减少作者的重复劳动，并让连续创作可控制、可恢复。

Language Genome 提供表达可能性，Novel Distiller 提供叙事机制，World Model 守住本书事实，Planner 安排人物行动和阅读承诺，Writer 完成文本，Critic / Rewriter 定位并修复具体问题，Runtime 保证流程与状态可靠。

研究结果有范围，审美允许差异，作者保有方向与取舍。近期成功标准是可读、连贯、成本有界的连续正文和可信的工程收据；更大规模与更强自动化通过逐阶段证据获得。

---

# 146. 最终禁止事项

- 禁止把原文来源、AI 身份概率、构造劣化准确率或 schema 合格率当作小说质量证明。
- 禁止把讲师标题、平台经验、广告、ASR 转写或同源复述直接编译成普适硬规则。
- 禁止把模型共识当作用户偏好，把自我实现的计划当作独立预测成功。
- 禁止混入隐藏基准答案、跨作品 / 分支事实，或用陈旧记忆冒充最新 canon。
- 禁止 Writer、Critic、Dream 绕过 Scene Commit 直接修改正式世界状态。
- 禁止先改变世界后保存正文、重复扣减资源、静默覆盖 revision 冲突。
- 禁止无限 Dream、无限修稿、未知费用记零、未知派发结果自动重试却不记账。
- 禁止静默切换模型、裁剪硬约束、降低核验门槛或跳过必需阶段。
- 禁止把同源导出相加当独立样本，或以默认 human chosen 构造未经验证的偏好真值。
- 禁止为追求研究指标长期搁置最小写作闭环，或为赶工程进度虚构文学收益。
- 禁止把设计 API、静态页面、模块名和未来目标标记成已上线能力。
- 禁止以自动生成为由自动发布作品，或把资料内部指令视作用户授权。

---
