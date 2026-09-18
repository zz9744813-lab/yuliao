# SemanticFrame Calibration Report v1（Phase 1 总装）

- 日期：2026-09-12　实验：`EXP-0911-B82D`　语料：琼明神女录（精校）`WK-6e5d2623`
- 冻结声明：报告完成前语料与 Candidate 规模冻结（30 段 / 336 候选 / 单本）。
- 评委矩阵：kimi-k3（judge v1）、deepseek-v4.1-flash（judge v2）、GLM-5.3 Flash 窗口盲评（人工级参照）。
- 配套文件：`data/reports/EXP-0911-B82D/calibration_report.md`（自动报告）、
  `docs/window-judge-2026-09-12.md`（窗口评委记录）、`phase1_metrics.json`（全部数字）。

---

## 一、主问题的回答：L 粒度进甜区（带三条限定）

| 粒度 | 保真率 | 表达自由 | 对抗泄漏 p90 | 判定 |
|---|---|---|---|---|
| **L** | **0.923** | 0.953 | 0.623（<0.65 阈） | ✅ 甜区 |
| M | 0.829 | 0.965 | 0.509 | ✅ 甜区（保真弱一档） |
| S | 0.701 | 0.977 | 0.455 | ❌ 保真不足 |

**结论：最细骨架（L）同时拿到高保真与高自由——"骨架越细锁死表达"的担忧不成立。**
**（措辞冻结：L 为 Corpus-01 上的 provisional sweet spot——n=30、单作品、单题材、单作者，
不写成"最佳粒度"；跨语料复现是 Phase 1.5 的门。）**
静态泄漏三层全 ≈0.002，说明 prompt 层防复制有效；只有对抗还原层抓到尾部泄漏（max 0.71–0.84，
经查主要是 Frame 内专名被强模型回填，属设计内信息传递；entity-normalized 口径见 Phase 1.5）。

三条限定：
1. M 粒度覆盖 24/30（首轮 kimi 超时损毁后补跑修复），非满配；
2. judge v1（kimi）给 human 的自然度分存在系统性压低（见 §六）；
3. 候选中位 87 字，per_k 类确定性指标只作方向参考。

## 二、Segment Integrity（任务一）

六指标全确定性 Python（`app/segment_integrity.py`）：quote / antecedent / dialogue /
scene_boundary / context_dependency / truncation_risk。

- 回填 14,124 段；全书自然度合格率 **58%**；**1,456 段以孤悬闭引号开头**（跨段引语被切）。
- 实验 30 段中 **20 段（67%）合格**；不合格主因：引号跨段（dlg=0）、truncation、连接词开场。
- 已接入：自然度校准抽样（window_judge prepare-nat）与报告 human 侧聚合按 eligible 过滤。
- 事故记录：首次回填因 ORM 未定义 `integrity` 列被静默丢弃——**"先迁移、后回填、提交后必须验库"**。

## 三、Context Ablation（任务二）——本轮最重要的方法论发现

窗口评委盲评（30 题 = 10 段 × 3 模式，全部 artifact_risk 组，已排除前批见过的文本）：

| 上下文模式 | human 胜率 |
|---|---|
| segment_only | 1/10 |
| prev1_current | **7/10** |
| prev2_current_next1 | 6/10 |

**机制**：候选是"段生"的（从 Frame 独立生成的自足小段），human 是"章生"的（从连续文本切出，
指代/引语/场景靠前后文撑着）。孤立评审结构性偏袒候选——上下文一恢复，human 的连续性优势
立刻显现（候选往往冗余地重建场景，打断语流）。

**规程修订（强制）**：今后一切 human vs candidate 的对照评审必须供给 ≥prev1 上下文；
`app/context_ablation.py` 已提供三种窗口模式与盲评基建。

## 四、同段 S/M/L 三 Frame（任务三）

extract stage 天然满足"同段三粒度"：primary frame 覆盖 L 30/30、M 24/30、S 30/30
（M 的 6 段缺口 = 首轮 kimi 超时，补跑后仍失败，已列入待重试）。双抽取器（kimi+deepseek）存证。

## 五、Frame Leakage 五层（任务四）

char6 / word3 / rare / adversarial / embedding：

- 静态三层 mean ≈ 0.002–0.008，超阈 0；
- adversarial：L p90 0.623 / M 0.509 / S 0.455；max 0.71–0.84（专名回填，见 §一）；
- **embedding 层诚实降级**：网关无 embedding 模型（探测 3 个候选均 404），以
  char-bigram hashing 代理落地（256 维余弦）。它只是词汇分布相似度，**不参与近重复判定**，
  报告中单独标注 backend。真语义向量待接入本地/云端 embedding 服务。

## 六、自然度七维（任务六）——把"Human 5.95 < AI"解剖开

deepseek 全量表（29 human / 332 cand）+ kimi 部分复现（29/21）+ GLM 窗口盲评（10+10）：

| 维度 | human | cand | 差 |
|---|---|---|---|
| local_fluency | 6.55 | 7.88 | -1.33 |
| novelistic_naturalness | 6.07 | 7.14 | -1.07 |
| contextual_fit | 6.55 | 7.14 | -0.59 |
| narrative_efficiency | 6.45 | 7.27 | -0.82 |
| **implicitness** | **4.59** | **6.27** | **-1.68** |
| **voice_authenticity** | **4.86** | **5.64** | **-0.78** |
| over_polish（反向轴） | 4.10 | **5.12** | +1.02 |

三个评委一致的方向 + over_polish 反向轴给出的自洽解释：

1. **倒挂不是评分错误，是成分问题**：差距集中在 implicitness（贴情绪标签）与 voice（通用腔）——
   窗口评委的质性观察完全吻合（"心中愤愤不平"、"场面的香艳淫糜随之攀升"）。
2. **over_polish 轴有效**：候选同时"更光滑"且"更 AI 腔"，说明七维量表能区分
   "流畅"与"过度打磨"，单分自然度做不到这一点。
3. integrity eligible-only 的 human 均值几乎不变（6.55→6.32）——切段伪影影响的是
   A/B 偏好（§三），而七维差距是真实的行文成分差异。两个效应都存在，不互相抵消。

## 七、Human Anchor 三重锚（任务七）

`works.anchors` 已写入：**semantic_anchor**（高置信：命题密度 5~14/段，S/M/L 齐备）、
**quality_anchor**（Phase 1.5 起多维拆分：local_expression=low / contextual_fit=**high** /
narrative_efficiency=medium_high / voice=uncertain / implicitness=low / overall=uncertain——
孤立段弱 + 上下文嵌合强同时成立，不再整本降级；正式拆分为
local_quality_anchor / contextual_quality_anchor / style_quality_anchor 三字段）、
**style_anchor**（mid：det 指纹在短段上噪声大，仅作方向）。

## 八、Active Review Queue 300（任务八）

300 条（284 pending）。构成：human_upset 142、judge_disagreement 68、novel_pattern 90+、
random_baseline 13（种子保底防极端化）、judge_uncertainty 6、over_polish 6、abstain 1。
幂等修复：refresh 重算不再丢失 random_baseline 标签（回归测试覆盖）。

## 九、Train/Benchmark 近重复检测（任务九）

`app/near_dup.py`：exact / char-6gram Jaccard / MinHash(128) / SimHash64 / embedding（代理）。
自检：自身判重 ✓、无关段判净 ✓。**基准隔离**：琼明 200 段已标 `role=benchmark`
（永不进训练采样；create_experiment 已接入 train_sampling_pool）。
阈值（保守）：ngram/minhash ≥0.15、simhash 汉明 ≤12、真向量 ≥0.85 一票判重。

## 十、确定性残差引擎（任务五）

审计结论：`metrics_det / segment_integrity / leakage(静态层) / near_dup` 全部零 LLM；
LLM 仅用于四件语义工作：Frame 抽取、重建、对抗还原、命题/语义残差/评审。
补充 3 个对话体关键指标（exclaim_ratio / question_ratio / ellipsis_per_k），
336 候选的 ResidualDet 已确定性重算。

## 十一、仪器事故登记（对抗性自查）

| 事故 | 后果 | 修复 |
|---|---|---|
| nat 批次 qid 带 H/C 前缀 | 盲性破功，整批作废 | qid 改中性 T{i}-0/1 |
| ORM 缺列时回填静默丢弃 | integrity 全库丢失 | 先迁移后回填 + 提交后验库 |
| refresh 覆盖 random_baseline | 队列刷新不幂等 | 标签保留逻辑 + 回归测试 |
| 章节标题黏连进段（"第七十二章…"） | 上下文与 naturalness 受染 | 已用 integrity 的 scene/ctx 维度标记，切分器 v2 待做 |
| 源书"章/掌"错字被候选模仿 | 候选继承源文错字 | 无害但须知情；Frame 抽取时应做字形规范化（待办） |

## 十二、总结论与下一步（决定权在作者）

1. **SemanticFrame 路线在 L 粒度上通过了本轮可证伪检验**（保真 0.92 / 自由 0.95 / 泄漏可控），
   不触发推翻条款；
2. 但 **quality_anchor 不可用**：本书行文的 implicitness/voice 显著弱于生成端，
   它只能当 semantic_anchor + style_anchor 用；
3. **评审规程改版**：human vs candidate 一律带 ≥1 段上文（§三）；自然度一律七维（§六）；
4. 放量前置条件（按你"报告前禁扩"的命令，全部待批）：
   ① 你本人盲评队列 top50 定 naturalness 的锚；② 补齐 M 粒度 6 段；③ 换/加强文风
   quality_anchor 书；④ 切分器 v2（引号闭合感知 + 章节标题剥离）后重切全库。
