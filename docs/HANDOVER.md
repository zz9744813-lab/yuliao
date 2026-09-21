# Language Genome — 交接文档

> **2026-09-20 23:14 基线的后续调整**：下一阶段先读 [知识化调整方案](Language_Genome_知识化调整方案_20260920.md)，近期顺序以其 K0–K5 为准；旧三场与计划第 1–7 项保留。本轮只写计划和核实：LG 40 项定向测试通过，但 A01 旧客户端仍可误译；A02 工作区修复原样例通过，旧指标待重验；Distiller A08 三个竞争回归通过。bal-v2 当前为 180 题 / 95 个源段，快照时尚无完成运行落库；不得沿用旧缺陷数量或将 v2 知识能力标为已实现。

> 2026-09-20 Codex：长篇总方案已定稿，单场景恢复与连续三场 CLI 试点完成（含 Codex 复核，第一场未重跑）；508 项测试通过，调用 / 收据 / 验收边界见 [Runtime 交接](runtime-handover-20260920.md) 与 [执行计划](plan.md)。

> 2026-09-20 18:10 起全面审查：仍有 8 项 P1 / 3 项 P2，含盲评映射、训练分组、调用漏账、租约竞争及测试污染真实游标；另 1 项测试夹具问题审查期间已修复。最新 LG 593 passed / 1 项模型下载连接失败，Distiller 214 passed。原三场收据保留，详见 [全面审查报告](project-audit-20260920-1810.md)，修复前不扩为无人值守长篇或开训。

> 交接时间：2026-09-18 · 交接人：ZCode（肉包之后一任）
> 目标读者：接手本项目的智能体
>
> **先读这一页，再动手。** 本项目有大量"看起来该做、但已被实测否掉"的路线，
> 以及一批会让脚本**静默出错**的环境坑。跳过本文直接开工会重复踩坑。
>
> ⭐ **如果你是接手者，先读 §0.5（2026-09-18 交接快照）**——里面是当前目标、今天的
> 实验结论、模型通道、以及"哪些假设已被推翻"。**目标在 2026-09-18 被集霸改过一次**，
> 不读会做错方向。

---

## 0. 一句话定位

> 🧭 **项目进度对照总方案（`docs/Language_Genome_完整工程方案.md` §50 的 14 项任务）**
>
> | 任务 | 状态（2026-09-18） |
> |---|---|
> | 1 基础工程 / 2 Corpus / 3 SemanticFrame / 4 Reconstruction / 5 Residual Analyzer / 8 Preference Lab | ✅ |
> | 9 Strategy Atlas（工作流 D） | ✅ `scripts/strategy_discovery.py` + `expression_strategies` |
> | 10 Hard Case（工作流 E） | ✅ `scripts/hard_case_mining.py` + `hard_cases` |
> | **6 Controlled Corruption（工作流 B）** | ✅ `scripts/controlled_corruption.py`（18 类单变量劣化 + 1 控制臂）；**且已用集霸裁定验过方向**，见 §0.5④ |
> | **11 Benchmark（§14）** | ✅（可建部分，T5）corruption 检测 171 题 + **按类型子基准 19 集合 214 题** + **nat-v1 自然度 201 题**（控制臂已排除）；其余子基准待解锁清单见 `benchmark_build.py` docstring 状态表 |
> | 13 Training Export（§42） | ✅（T6）SFT（`--from-frames`，**不需要人工标签**）· DPO（`--pairs`，默认方向**已被裁定否掉**）/ 严格 1（`--strict`）· 负面库 · **RM 1316**（`--rm`，正负比 0.953）· **Rewrite 845**（`--rewrite`）；摘要见 `data/exports/*_summary.json` |
> | 7 Judge Arena | ⚠️ 评委**已证不可靠**：κ −0.04~+0.11，且**控制臂显示四家共享同一偏差**（§0.5③） |
> | 12 Experiment Engine | ✅ `app/engine.py` + `scripts/run_experiment.py`（阶段状态机，可续跑/幂等/失败传播）|
> | 14 Observability | ✅ `app/observability.py`（纯函数聚合）+ `GET /llm/stats?hours=24&exp=` + `scripts/observability_report.py`；窗口内 0 条显式 n=0，ISO 时间窗按字符串比较（tests/test_observability.py 钉住）|
>
> 📌 **§0.5 是本次交接新增的核心章节**，其余章节是 09-16 版本的历史积累（仍然有效）。
>
> ⚠ **集霸 2026-09-17 明确纠正过**：不要继续泡在 Phase 1.5 的"测量"里（评审判不判得准），
> **按总方案推进建设**。判别任何新工作的标准是：**它是否在补 §50 任务清单里空缺的那几项。**
> 📌 终报在 `docs/final-report.md`（Phase 1.5 的测量结论，已收官）。

> ⛔ **改 `app/static/index.html` 之前必读：`tests/test_frontend_invariants.py`**
> 前端没有构建产物也没有类型检查，"某个功能被顺手删掉"没有任何机制能发现。
> 2026-09-16 的 UI 重写就删掉了**跨实验取题逻辑**（`batchMulti`），后果是实的：
> 跨语料批 `x50` 判完第一个实验的 3 题后，页面显示「本批已全部判定」，**实际还有 39 题**。
> 现有 7 项静态断言把它们钉住（跨实验路由 / 上文口径 / 批注块标记 / `MARK_KINDS` /
> DOM id / 反锚定锁定）。**删任何一条 → pytest 立刻红**，那是有意的。

> 📌 **2026-09-17：集霸选定"用现有数据出终报"，终报已产出 → `docs/final-report.md`**
> （`python scripts/final_report.py --out docs/final-report.md`；评委分**全覆盖**，
> 且已扩到**四家五个模型**：kimi-k3 / deepseek-v4.1 / muse-spark-1.3 / agnes-3.0 / **gemini-3.8**）。
> 核心数字：**最好一格 kimi v4 κ = +0.112**（按段落聚类 CI [+0.010, +0.214]）；
> 五个模型 κ 全部落在 **−0.04 ~ +0.11**，**全部低于「恒定答 human」基线 0.735**。
> ⚠ **Google 模型有内容审查**：gemini 在 69 题里拒答 9 题（13%，色情文本触发
> Generative AI Prohibited Use policy），n 只有 60 —— 本项目语料上 Google 模型**部分不可用**。
> ⚠ 样本补满后 κ 反而更低（126 条 +0.145 → 230 条 +0.112）：原先的"略高于 0"
> 有一部分是早期批次缺分造成的。
> **人工盲评已停止**（集霸："太折磨了"）。

不是完整系统，是**第一个关键科学实验**：证明"语义可以被压缩成一个
既足够约束意义、又不过度约束表达的中间表示"（SemanticFrame）。

当前实际焦点已经收敛到其中一个子问题：

> **能不能用一个 LLM 评委稳定复现集霸（作者本人）对"哪种写法更好"的判断？**

答案是**目前不能**，而且原因已查清（见 §7、§8）。

> 🔄 **2026-09-18 把"为什么不能"查得更准了**（见 §0.5③）：不是评委读不出差异——
> 单变量劣化它们能认出 86%；而是它们有一个**方向性的偏差**（控制臂上 77% 偏 AI 那版），
> 而集霸在同样的题上 **0/8** 选 AI 那版。**这是口味相反，不是能力不足。**
> 另外，集霸在 24 题里只有 4 题判"人类原文胜"（11 题"两边都不好"）——
> **"人类原文 = 好"这条前提本身也不成立**，这是接手后第一个要面对的。

**2026-09-16 补充（h30 判完后）**：不止"评委不行"这一半确定了，
**"靠打分模型挑题补样本"这一半也证伪了**——承诺命中 0.696 的收割段只打出 3/21。
于是原定的"补到少数类 100 条再重测"**没有可行的补法**，需要集霸在 §8 的三条路里拍板。

---

## 0.5 交接快照（2026-09-18）

### ① 目标被集霸改过一次（**先读这条，不读会做错方向**）

原目标隐含"找到文字的定性标准 / 复现集霸偏好"。2026-09-18 集霸原话：

> 「因为文字独有的复杂性其实你很难找到一个定性的标准，我觉得可以适当的降低要求，
>   **只要不是出现很明显的 ai 味道就行了**」

→ 目标从"正面标准"改成**否定式判据：拦掉明显的 AI 味**。
影响：**不要**再去追"什么算好"的定性标准；把力气花在"AI 味能不能测出来"。

### ② 当前数字（可复现：`python scripts/final_report.py` / 各脚本自带 `--report`）

| | 数量 |
|---|---|
| 人类段落（已切分 v2） | 295,955 |
| 源完整性已查 / 判坏 | 975 / 558 |
| L 主帧 | 469（`EXP-0918-SCALE*` 在继续扩） |
| 候选（可盲评） | 2,675 |
| 受控劣化 ok | 524 |
| 集霸判定 | 282 |
| 评委判定 | 8,690 |
| 基准条目 / 基准运行 | 171 / 5 |
| 训练导出 | SFT 219（自动）· DPO 244（方向未经裁定）· 负面库 108 |

### ③ 今天最有分量的实验结果：**两个假设被推翻**

集霸判完 corr24 全部 24 题（人类原文 vs 单变量劣化版）：

| | 集霸 | 评委（同题） |
|---|---|---|
| **控制臂 8 题**（两边内容相同、只换说法） | 原文 3 / 打平 2 / 都不好 3 / **AI 那版 0** | **AI 那版 ≈77%** |
| 劣化对照 16 题 | 原文 1 / 打平 4 / 都不好 8 / 改坏版 3 | 原文多数（86%） |

1. **"评委偏好 AI 腔"不是他的口味**：控制臂上评委 77% 选 AI 那版，他 **0/8**。
   → 低 κ 不是噪声，是**方向相反**。
2. **"人类原文 = 好"在这些语料上只有 17% 成立**（24 题里他只有 4 题判原文胜；
   11 题"两边都不好"）。**他不认可这批原文本身。**
   → 后果：DPO 不能默认 `human=chosen`（244 对里只有 1 对经他背书），
   已加 `--strict`；劣化集的正确用法是**负面模式库**（`corrupt_negatives_v1.jsonl`）。
3. **gold standard 现在是最大的未决问题**（比数据量更根本）。三个方向见 §8 末尾。

### ④ AI 味检测器：三次失败 + 一条**片段级**做出来了

| 做法 | 对他的 63 条批注 | 构造劣化对（标签独立） |
|---|---|---|
| 规则层 v1（模板词表） | 段级 20.8% | 31.5% |
| 规则层 v2（照批注结构重写） | 段级 7.5% | — |
| LLM 绝对打分 | **更低**（0.372 vs 0.454） | 72.1% |
| 段落级向量+LR | AUC 0.443（低于随机） | — |
| **片段级向量+LR** | **AUC 0.710** | **75.9%** |

**适用边界**（别过度声称）：AI **自由续写** 75.0%、AI **紧约束改写** 52.0%。
即：它抓"AI 自己组织语言"的那种味，对"照原文重写"不报警。
产物：`scripts/flavor_span.py`（`--train` / `--eval-pairs` / `--text`），
模型 `data/models/flavor_span_v1.npz`。**样本只有 55 正例，AUC 区间 ±0.1，别当稳定仪器。**

**为什么段落级全败**（这条诊断比失败本身有用）：这些**人类原文本身就满是**
AI 味词表里的东西（`凤毛麟角`/`心中一紧`/四字格）——它们是中文网文。
→ 表层统计只能分出"**网文味**"，分不出"**AI 味**"。参照文本与被测文本表层同质时，
表层检测器原理上无效。

### ⑤ 模型通道（集霸 09-18 指定换代）

| 通道 | 模型 id | 实测 | 约束 |
|---|---|---|---|
| 中转网关 | `moonshotai/kimi-k3` / `deepseek/deepseek-v4.1-flash` / `z-ai/glm-5.3` / `agnes-3.0-flash` | 13~180s | glm 很慢（判分 180s、校验 280s） |
| 本机 CLI | `agy/gemini-3.8-flash-high` | 33s | **必须挂 127.0.0.1:2080 代理** |
| 本机 CLI | `qoder/Qwen3.8-Flash` | 12s | **不要挂代理**；免费档 |
| 本机 CLI | `wb/hy4-preview-f` | 18s | **WorkBuddy AI GUI 必须开着** |

- **停用 `meta/muse-spark-1.3`**（集霸指令）；`glm-5.3` 裸名已 503，必须写 `z-ai/glm-5.3`。
- 三条本机 CLI 全是**单账号共享额度 → 必须串行**：统一走
  `app.gateway.is_serial_model()` / `split_models()`，别自己判断前缀。
- 网关的 `/models` 列表**会骗人**（列着已下线/无额度的）：`minimax-m3` 已 410、
  `agnes-2.5-pro` 403、`glm-5.3-flash` 也 261s。**实测为准，登记表见 `data/model_registry.json`**。

### ⑥ 基准排行榜（§14，171 题 corruption 检测，`benchmark_runs` 表）

| 模型 | 答对率 |
|---|---|
| **agy/gemini-3.8-flash-high** | **0.912** |
| moonshotai/kimi-k3 | 0.819 |
| deepseek/deepseek-v4.1-flash | 0.817 |
| z-ai/glm-5.3 | 0.793 |

（表里另有一条 deepseek 0.958 是 **24 题的旧子集**，不可与 171 题直接比。）

### ⑦ 现在在跑的（交接时未见完，接手者先看这些日志）

| 作业 | 日志 | 说明 |
|---|---|---|
| 语料扩产第二批 300 段 | `data/_dbg/scale2.log` | 规则清洗 → LLM 源校勘 → 抽 L 帧 |
| Qoder / WB 通道基准 | `data/_dbg/bench_qoder.log` / `bench_wb2.log` | 串行跑 171 题 |

产物都在 `F:` 盘：`data/exports/`（训练数据）、`data/models/`（检测器）、
`data/_dbg/`（日志）、`data/model_registry.json`（模型登记）。

## 0.7a 2026-09-19 军师退回轮（gpt-6-astra 只读核仓，两处定性被推翻）

军师核仓 BLOCK 退回全部落地（逐项 commit + 会审门放行 + 推送）：

| # | 级别 | 退回内容 | 处置 |
|---|---|---|---|
| 1 | P0 | 字表工具用修人名顺手放行 src_ok（387 段被误翻可用） | `2b91e8f` 撤销逻辑，段重置未校勘后由他人用可用通道重校（现 249 ok/132 bad/5 待查） |
| 2 | P0 | 令牌轮换未验收 + serve 脚本明文打印令牌 | `39a876c` 运行态验收（新令牌 302/新 cookie 200/旧值 401）+ 链接只写 0600 文件 |
| 3 | P1 | 两个高分没超过"只选较短"长度基线（nat 0.944/hvai 0.872） | `8df9f50` falsify 增长度基线必检——**四 pass 全降 weak**，"读得出差异"退回 provisional |
| 4 | P1 | 只报已答 acc 是选择性汇报 | 同上：全题有效成功率入表（hvai 0.787、deepseek nat 0.796） |
| 5 | P1 | 训练导出内容级泄漏（4 条输出=hvai 原文、7 处上下文同文） | `7c1419f`/`4031b10` 内容级哈希隔离 + 回归 |
| 6 | P1 | RM 172 组分数冲突静默共存；SFT 1798 实覆盖 1396 段、32 条无 src_ok；负面库 103 条仅 7 源段 | `e70e0cf`+`09803e5`+`afec071`+`0187d63`+`2b75b82`：src_ok 必须 True、冲突按 user_verdict>变量级>弱标 保一条 + 确定性 tie-break、summary 全部如实（覆盖段数/集中度/丢弃数） |
| 7 | P2 | gold standard 主线转 (a) | `302df36` goldpick 16 段待指认清单（含 4 正常反例）+ 入口 docs/goldpick-候选清单-v1.md；**(b) 降级弱标签试验** |
| 8 | P2 | 源文本优先找干净版本 | `7bb7c20` 策略+验证协议入库；字表归一化降级为局部修补 |
| 9 | P2 | 数百条先验证训练管线 | `7bb7c20` SFT runbook（主从端点预注册 + 推理配置四项锁定——会审二轮意见一并落地） |

**经验（留档）**：会审门（pre-push 钩子）拦下了 3 次推送、每轮意见都实打实——
绕过它等于把核仓员的眼睛蒙上。heredoc 写含 `
` 的补丁在本机会吃掉转义
（本日 3 次事故），含转义的编辑一律用 Edit 工具。

### 0.7b bal-v2 长度方向重生成管线落地（2026-09-20，`7f87fc3`）

按 `docs/proposal-length-balanced-regen-20260920.md`（三轮会审定稿）**规格测试先行**实现：
TYPE_LEN_SPEC（6 压缩型强制 L 窗 [0.60,0.92] 闭区间，膨胀型/控制臂 any；
L上界<S下界模块断言防重叠）+ 压缩型 prompt 60%~90% 指令 + judge_verify
越窗 → rejected_length + classify_llm_failure/batch_llm_health（503=0 批次
先决判据）+ final_report.gate_bal_reading（先最新 split 再集内最高，
半成品集不顶替）。**全量 580 例全绿**（基线 553 + 27 项规格新测）。
剩余项：bal-v2 L 侧重生成放量 → bal-v2 建集 → benchmark_run + falsify → 档一读数接任。

**2026-09-20 下午更新（产线定论，`c974385`）**：pilot 三轮跑完——
· 2 产型（SUBTEXT_ERASE/LITERARY_OVERWRITE，deepseek 67%）入产线；
  EXP-BAL2-PROD（150 段×2 类=300 变体）在跑，503=0；
· 4 类（ABSTRACT_SUMMARY/RHYTHM_FLATTEN/EMOTION_LABEL/DIALOGUE_EXPOSITION）
  两轮全灭 + kimi 诊断也不压（要么 ratio 1.05~1.46 越窗要么生成失败）
  → **类型问题非模型问题**，记「本代模型不可产出」；
· 三态语义：校验窗全保留（6 类 L 窗——不可产出类的不压缩样本被
  len_window 拒，删键=any=污染）；调度侧 UNPRODUCTIVE_TYPES 排除
  （显式请求也跳过）；历史样本全为 rejected_* 状态、零 ok 入库。
产线限制（诚实声明）：bal-v2 的 L 侧只有 2 个类型构成——S/L 对照存在
类型混杂（L 侧=LITERARY_OVERWRITE+SUBTEXT_ERASE），N5 分层读数解读
时必须带类型注记。

**「本代模型不可产出」清单与复核条件**（c974385 定论，依据提案 §1.5
预注册协议两轮上限）：
· 不可产出 4 类：ABSTRACT_SUMMARY / RHYTHM_FLATTEN / EMOTION_LABEL /
  DIALOGUE_EXPOSITION——round-1 0/3×4、round-2 放宽 +0.05 后 0/4×4、
  kimi 换生成器同样失败（EXP-BAL2-L1/L2/DIAG 三轮证据，503=0）。
  是**类型问题**（该变量天然不压缩或压缩即病句），不是单模型问题。
· 排除的实现是三态：调度 UNPRODUCTIVE_TYPES 跳过 / 校验 EXCLUDED_WINDOW
  哨兵显式拒收（excluded_type 理由）/ TYPE_LEN_SPEC 显式 None 键
  （与漏填可区分）。None 不承担「排除」语义——any（不约束）与排除是两个哨兵。
· **复核条件**（满足其一可重开这 4 类）：① 出现更强的生成模型
  （可通过 20 对 pilot 复验）；② 修改类型规格本身（如放宽压缩下限）——
  属规格变更，须按提案 §3 重新预注册。

**2026-09-20 监督整改（采样宇宙污染修复 + bal-v2-prod 干净重建）**：
监督实测（非自述）：bal-v2 首建集 BS-fe40d3fd9b1a 的 200 题只落 93 段，
其中 17 段 119 题（59.5%）的合格劣化行全部来自旧实验——role='benchmark'
是持久单调标记，`--exp` 只限新标记，不限池内历史行。修复（2a-2e）：
`build_length_balanced` 增**按侧行宇宙过滤**（l_experiments/s_experiments，
按行 experiment_id 过滤各自一侧）+ 同名守卫（重名拒绝/`--replace` 删旧建新并报
replaced）+ spec 记实测宇宙与两侧真实库存（l/s_universe + l/s_stock_measured，
不写口号）+ split CLI 加固（`--split-benchmark 0` 与 None 分开、marked==0 且
already==0 非零退出、--exp 透传）+ 测试卫生（链测试 monkeypatch spy、_UNIQ、
共享库池卫生 teardown 夹具——teardown 删净本文件 seed 的行并回滚被 split
标记的外段 role）+ 旧实验排除回归测试（混池按 exp 建集不得捞旧行）。
旧集处置：BS-fe40d3fd9b1a 的三评委跑分在 kimi 完成 123/600 后终止（runner
已杀，额度消耗审计见台账 F:\Hermes\team\gui_report.md 2026-09-20 17:00/18:24
行；llm_calls purpose=benchmark 留存），集与 200 条目已删除——该集读数
作废，不得引用；删除不可逆、无备份，追溯依赖上述台账行与残留 llm_calls。
**bal-v2-prod 干净重建：BS-5543d4b7ac4c，180 题 = S 90 + L 90**（seed
20260925，per_side 90）。实测宇宙：L 侧行宇宙 = **纯 EXP-BAL2-PROD**
（实测库存 93 行；核验=scripts/verify_bal_universe.py 逐题按变体文本回连
ControlledCorruption。重跑：`"$PY" scripts/verify_bal_universe.py --set
BS-5543d4b7ac4c --json <任意路径>.json`——证据目录 outputs/ 在仓库外
（gitignore），文件可由该命令一键再生；09-20 实测证据
outputs/bal2-prod-verify/BS-5543d4b7ac4c.json：
L/EXP-BAL2-PROD 90、S/EXP-0918-BENCH 57、S/EXP-0918-CORR 33、0 mismatch）；S 侧未限定（结构事实：窗口化产线只产 L 方向，PROD 无
S 库存，纯 PROD 平衡集不可行），S 侧 90 题来自 legacy EXP-0918-BENCH（57）+
EXP-0918-CORR（33），spec 已声明 s_universe=all、s_stock_measured=195。
解读注记（继承产线声明）：S/L 对照带类型混杂——L 侧全部由
LITERARY_OVERWRITE+SUBTEXT_ERASE 两型构成，S 侧为旧代多型；档一读数
必须带此注记。qwen 席会审 BLOCK 修复（同日第二轮）：dry-run 报
name_conflict/on_conflict（预演可见冲突）；replace 在新集 spec 留
replaced={set_id,n_items} 删旧痕迹（role='benchmark' 不回收——持久单调
标记、段可被多集共享，行级宇宙过滤是兜底层）；k=0 拒建空集（空集曾让
3 条测试假阴性通过，已补两侧 seed）；新增 scripts/verify_bal_universe.py
（建集函数自证不算数：逐题回连、mismatch exit 1、--json 落证据）；
链测试池夹具快照缩到可翻转集（role 空白段），teardown 只回滚被翻外段，
不再两次全表物化。**两席 BLOCK 第二轮修复（同日第三笔）**：replace 改
事务内删旧、删旧与建新同一 commit（建新失败 → 回滚 → 旧集幸存，k=0 守卫
前移到删除之前，回归=replace+库存不足旧集原样保留）；replaced.n_items
改用 delete() rowcount 并随 spec 携带 prior（链式 replace 追溯不断链）；
verify_bal_universe 硬化：只核 length_balanced 且非空集、spec 宇宙键坏
值报错不降级、两侧 all 报「无从核验」、answer≠A/B 与 EQ 计 mismatch、
ambiguous/not-linked 只分型不猜行、大清单截断显式标
mismatch_truncated、--l-experiments 空串拒绝；池夹具 teardown 改查
role=='benchmark' 小集 + Python 求交（不下发大 IN 参数），加漂移哨兵
（split 若开始改标非空 role 段必须红）。
**档一读数（2026-09-20 深夜，BS-5543d4b7ac4c = bal-v2-prod 干净宇宙）**：
三评委全报（180 题 = S 90 + L 90，逐题回连核验 L 侧 100% EXP-BAL2-PROD、
0 旧行；task=detection，pv=bench_task_v1，540/540 调用 0 失败）：
· qoder/Qwen3.8-Flash **0.839**（151/180；段 bootstrap CI [0.792,0.882]；
  置换 p=0.0000/95 段；短/长分层 0.84/0.83；留一波动 0.006）→ **达标**
  （≥0.80 预注册线，超出 +0.039；诚实注记：CI 下界 0.792 微低于线，
  点估计过线）；
· moonshotai/kimi-k3 **0.828**（CI [0.765,0.883]；p=0.0000；短/长
  0.83/0.82；留一 0.006）→ **达标**；
· agnes-3.0-flash **0.728**（CI [0.663,0.789]；p=0.0000；短/长 0.72/0.73；
  留一 0.045）→ 未达 0.80 线。
N4 长度基线 **0.500**：三家全部显著超过（75:14 / 74:15 / 66:25，p=0.000）；
短/长分层均衡 → 读数**没有搭长度便车**——与长度混淆时代（nat-v1 0.944 等）
的读数性质不同，方向识别信号成立。位置注记：pickA 0.64~0.67、答案 A 率
0.51，轻度位置偏好，未吞掉信号。**预注册达标判定：档一（方向识别）
2/3 评委过线（qoder/kimi），agnes 未过**。类型混杂注记（必读）：L 侧
全部由 LITERARY_OVERWRITE+SUBTEXT_ERASE 两型构成、S 侧 legacy 多型——
S/L 对照带类型混杂，解读必须携带本注记；每类型分层的读数留待
类型子基准扩展。falsify 全套 N0~N5 三家全 pass（表见
outputs/bal2-prod-verify/watcher.log）。final_report 档一节已改吃
set_name 并按 0.80 线判达标/未达标（旧实现写死 bal-v1 + 无条件未达标）。

**审查 A01 已修（同日晚，跑分进行中插入）**：盲评页重出题覆盖旧页 A/B 含义
（污染最贵的用户偏好标签）——每次端题落一行不可变 ReviewPresentation
（排列 human_first/上下文口径/两侧文本指纹），响应带 presentation_id，
提交绑定它按呈现当时排列解读；旧客户端回退该题最近持久化呈现（跨重启/
多 worker），409 只对 A01 落库前的无呈现历史题生效。前端取题/改判/重端
三处携带 pid，human_verdict 落 presentation_id 审计指针。回归 7 测
（同题双页相反排列按各自呈现解读、pid 错题拒 400/不存在 404、重启回退、
批注 target 随绑定呈现、冻结行指纹、改判重启续用持久呈现、无呈现历史
题仍记原始 A/B）。**审查 A02 已修（同日）**：flavor_span 数据集 groups
与 texts 错位（pos/neg 交错追加 vs pos+neg 重排）→ 按题分组折失效。
修复为 (text, y, rid) 记录一次成行统一拆列 + train() 折隔离断言
（同题跨训练/测试两侧即 SystemExit）+ 回归 2 测（逐题正负例计数对齐、
错位数据必被断言红出）。**修复后已重训重验**（23:14 基线注记的
「旧指标待重验」即此项）：AUC 0.794（各折 0.781/0.796/0.743/0.781/
0.867），长度基线 0.500，165 样本（正 55/负 110），劣化对外部验证
52.9% 劣化版更高——此为本口径有效读数，旧 AUC 不再引用。
**审查 A03 已修（同日深夜；会审二轮加固）**：训练导出重导 + 验收器 +
训练入口。旧导出（09-19 文件）实测 SFT/Rewrite 各 114 行与基准文本
重合（**内容级口径**：忽略空白、≥50 字、目标+前文对全库冻结基准哈希）、
32 行源未校勘、RM 172 组同源同文不同分。处置：①现行代码重导三件套
——writer_sft_v3（2037 行/1672 源段）、rewrite_v2（2037/1672）、
rm_v1（573/150）。**口径换算**：导出侧跳过的「基准段 873」是**段级**
role 闸（含已判改判段），审查的「114 行重合」是**内容级**复算——
两个不是同一口径；重导后内容级重合复算=0（验收器管这个数）；②新增
scripts/verify_training_export.py：内容级重合逐行重算（含 dict 形态
前文）、RM 同源同文多分组数、主键缺失、源质量（integrity.src_ok
批量回连）、参与比较文本数；旁挂 x.manifest.json（x.jsonl →
x.manifest.json，sha256+计数）；③**训练入口接线 scripts/train_entry.py**
——验收函数不再是可绕过的工具：SFT/Rewrite/RM 从这里进，任一拒绝即
非零退出（实际训练流程后置，入口当前只做验收门）；④accept_for_training
**防手改**：重跑纯检查并与 manifest 的 sha256/kind/行数交叉核对——
手改 passed 位 / 备份冒名 / 改动过的文件一律拒收；⑤三个宁可拒不恒绿：
基准哈希集为空（连错库）→ 拒；0 条文本参与比较（schema 漂移）→ 拒；
源质量**闸**而不只是报（未校勘段>0 → 验收红）；⑥同源不相加机械口径：
union_distinct_sources()——SFT 1672 源段 × Rewrite 1672 源段 →
**并集 1672**（train_entry 实跑：按行数相加虚增 2402、按各文件源段数
相加虚增 1672，两种口径分列，合计只认并集）。三件重导出验收全 PASS
（重合 0/冲突 0/缺键 0/未校勘 0/悬空 0/目标空值 0/比较文本
6111/6111/1719；目标短于 50 字的行 73/73/48 属合法短文本，只报不闸），
manifest 落 data/exports/（gitignore 外置，可重跑验收器再生）。
**会审三轮加固（同日）**：取到文本数与可比文本数分列——目标字段
**空值行**入闸（半漂移：置空占位不许混进比较数），目标**短而非空**
只报不闸（空=导出坏了，短=数据本来就短，两回事；真件实测 73 行短
目标曾被误闸，现 0 误伤）；悬空源段（库中查无）从裸抛改为入闸入
manifest（源质量一个家族一套口径）；accept 把 verify 的结构性
SystemExit 转成 (False, problems)——入口对每个文件都拿得到拒收
理由；manifest 增基准指纹 n_bench_hashes 并入交叉核对（换小库
重算骗不过）；RM 分数不可哈希计数拒收不裸炸；非 dict 行拒收；
train_entry 改 ASCII 标记（win32 cp936 下 ✓ 会 UnicodeEncodeError）
+ 参数位 kind 校验（--sft 传 rm 文件=张冠李戴拒收）+ 入口措辞降级为
「未闭环风险已记账」（真正强制点在训练流程实现时接入）。回归 11 测
（含 schema 绊线：验收器字段名与导出器源码必须同步）。
**审查 A04 已修（同日凌晨）**：评审游标路径从仓库硬编码改为
config.DATA_DIR 派生——旧 `_CURSOR_FILE` 直指仓库 data/serve_cursor.json，
测试只隔离了 LG_DATA_DIR 与数据库、管不住这条路径：test_review_batch
的 unlink 与取题写入全打在真实文件上，正式游标被测试批次键反复污染
（18:10 审查证据 9 键中 3 个确认测试键；次日实测 15 键全为已知测试
批次名 c41/pr1-5/rj1-8/rj3b——含本日 10 轮全量测试持续写入的真实
污染记录），多轮测试还反复销毁历史内容。**如实记录损失评估**：
真实用户批次键已不可恢复（无 git 追踪、无内容备份，审查证据只存
sha256）；但游标只是**轮换起点**，真实进度在 review_items
（status=done，完好），_pick_next 固定列表跳过已判——下次从 0 扫、
跳过已判、从首个待判继续，无判定数据损失。处置：删除仅含测试键的
正式文件；新增 tests/test_cursor_isolation.py 2 测钉死派生契约与
禁回硬编码；验收=全量 630 例前后正式文件不变（实测 ABSENT→ABSENT）。
**审查 A05 已修（2026-09-21 上午）**：网关重试不再吞账。事故（审查
MockTransport 复现）：两次上游请求合计 45 token，账上只留一条成功
15 token——可重试 HTTP / 空正文续试 / 传输异常都在重试环内静默
continue，只有最终结果落一行；实验引擎又把 llm_calls 当唯一费用/
失败账本 → 失败率被低估、成本少记。修复（监督 08:25 口径）：①每次
派发**先预留**（status=dispatched 行，进程中途崩掉该尝试也留痕）、
每次返回**单独结算**（本尝试 usage/延迟/状态/错误）；②LlmCall 增
logical_call_id + attempt_no 两列（_migrate 只增列），账本分列
「逻辑调用」（n_logical，按 lcid 分组，历史行 NULL→每行自成一组）
与「HTTP 尝试」（n，逐行）两个口径，observability._block 同步报双
口径；③空正文那趟已烧的 usage 必须入账（回归复现 45 token 全额）；
④未知费用保持 None 不冒充 0；⑤单发路径（桥接/mock/_record）每行
自成逻辑调用（attempt_no=0）。回归 8 测钉死监督验收口径
「N 次 HTTP 尝试 = N 条尝试记录、失败那次同样留痕」（503→ok 两行、
空正文重试两行 30+15、传输异常两行、全军覆没 MAX_RETRIES 行、
预留→结算单元、cost=None、单发 lcid、报表双口径 n=2/n_logical=1）。
test_model_pool 的「账本记真名」契约随记账点从 _record 移钉 _reserve。
全量 638 例全绿（630→638 只增不减）。
**审查 A06 已修（2026-09-21 上午，接 A05）**：非空截断输出不再当成功。
事故：网关读了 finish_reason 却只拒绝空正文——finish_reason=length 且
正文非空时照样返回 status=ok（审查复现：返回「尚未写完的半句」，
调用状态成功）——自由文本候选可能以完整样本身份进重建与后续评审。
修复：①完成原因白名单 OK_FINISH_REASONS={stop, end_turn,
stop_sequence, None}（None 显式接受：部分中转成功时不回 finish_reason，
空正文另有 P0 闸）；②length/content_filter/tool_calls 等非白名单完成
方式，正文非空也一律记**非完整产物**（本趟 usage 由 A05 结算入账，
error 带 finish_reason）→ 抛错，绝不以 ok 落库；③有上限恢复：
length 走既有预算加倍重试（≤MAX_RETRIES、≤8192），不可恢复的完成
方式（content_filter 等）立即失败不空转烧钱。回归 5 测（监督口径：
不许只覆盖「截断且空文本」）：半句+length 拒收留痕（截断趟 usage
12/8 入账）→ 加预算重试成功；MAX_RETRIES 趟全截断→抛错且每趟留痕；
content_filter 一次即止（第二发是白烧钱）；白名单 end_turn/None 放行；
截断且空文本走 P0 空容闸不回归。全量 643 例全绿（638→643 只增不减）。
**审查 A07 已修（2026-09-21 上午）**：同一实验并发运行不再重复执行
阶段。事故（审查复现口径）：两个线程对同一实验同一阶段调用
engine.run，屏障控制顺序后**阶段体执行 2 次、两边都返回成功**——
API「检查 running」与后台线程「写 running」不在一个原子操作内，
CLI 与 API 同时启动、双请求竞争都读到「阶段尚未完成」；真实阶段
则重复生成与计费。修复（监督口径）：①**数据库条件更新领取执行权**
（_claim_run：原子 UPDATE 只有把 status 翻成 running 的那一个赢，
run_owner/run_claimed_at 凭据随领取写入，Experiment 增两列+
_migrate）；②**提交阶段结果前校验持有权**（run_owner 须仍是自己
token——中途被夺权的 runner 立即停止提交，finally 收尾同样校验，
绝不替新主人写 done/failed）；③api.py 快路径注记明示「先查后启
不是闸」，真闸在 run 内的领取事务（UI 禁用按钮不能替代）；④卡死
恢复显式：release_run / CLI --release（无自动 TTL——PROD 级数小时
长跑中途被误抢=A07 换姿势重演，接管必须人为）。回归 6 测：并发
双线程屏障起跑→阶段体恰好 1 次（旧事故=2 次）、输家拿
already_running；赢家在跑时第二 run 直接输；夺权后下一阶段即停、
status 不被旧 runner 收尾；卡死 release 后可重领；冻结在领取前拒
（status 不翻 running）；已 done 阶段跳过幂等不回归。全量 649 例
全绿（643→649 只增不减）。
审查剩余：1 项 P1 / 3 项 P2 排队中。

## 0.6 接手者第一天照这个做

```bash
cd F:/agi/language-genome
PY="F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe"   # ⚠ 用项目自己的解释器
                                                                     # （PATH 上的 python 可能是别的 venv，没有 sqlalchemy）

"$PY" -m pytest -q                      # 全量测试（300+ 项，必须全绿；改动前先跑一遍存基线）
"$PY" scripts/controlled_corruption.py --report      # 劣化数据集 + 可判别性标尺
"$PY" scripts/benchmark_run.py --scan                # 基准排行榜
"$PY" scripts/clean_text.py --report                 # 清洗进度
"$PY" scripts/source_check.py --scan                 # 源完整性进度
bash scripts/serve_remote.sh                         # 起评审台（集霸批改用；URL 每次重启会变，令牌不变）
```

**动手前必须知道的三件事**（都写在 §0.5）：
1. 目标已被改成"**拦掉明显的 AI 味**"，别再追定性标准；
2. `human=chosen` 这条默认方向**已被他的裁定否掉**；
3. 本机有三个 CLI 通道（agy/Qoder/WB），**必须串行**，代理要求各不相同。

**不要碰的东西**：`app/static/index.html`（先读 `tests/test_frontend_invariants.py`）、
`BLIND_REVIEW_PROMPT_VERSIONS` 的抽样池语义（隔离靠它）、以及 §7 列过的已否路线。

## 0.7 2026-09-19 白班交付（Hermes 调度队列 T1–T8 完成）

夜间守夜 Hermes + ZCode agent 交付 T1/T4/T6（`c715851` / `7d1f342` / `7862666`），
白班接续 T2/T3/T5/T7/T8。**每项全量 pytest 全绿后独立提交**，逐项记录在
`F:\Hermes\team\gui_report.md`：

| 任务 | 提交 | 交付物 | 证据 |
|---|---|---|---|
| T1 任务12 实验引擎 | `c715851` | `app/engine.py` 状态机（plan→source_check→extract→reconstruct→residual→judge→report；可续跑/幂等/失败传播）+ `scripts/run_experiment.py` | `tests/test_engine.py` |
| T2 只读控制台 API | `0f19c0f` | `GET /console`（索引）+ `/console/<模块>`（§18 导航的 16 模块；白名单分发，未知 404）。`app/console.py` **全部只读**（回归钉住：全端点扫描前后全表行数逐表相等）；settings 显式白名单，令牌/密钥不进任何响应 | `tests/test_console.py`（20 项） |
| T3 第二界面评审台 | `21729b0` | `/console/ui`——16 模块外壳：导航由索引驱动（不硬编码）、GET-only、读数不落 localStorage、取数失败显形。**index.html 一字未动**，其不变量测试原样全绿 | `tests/test_console_page.py`（5 项）+ 浏览器实测截图 |
| T4 任务14 可观测 | `7d1f342` | `app/observability.py`（纯函数聚合）+ `GET /llm/stats?hours=` 窗口口径 + `scripts/observability_report.py` | `tests/test_observability.py` |
| T5 任务11 子基准 | `178fd63` | **corruption_type**：19 个按类型冻结集合（214 题，类型一等公民，可单跑/回归对比）；**naturalness_pair**：nat-v1（201 题，问"哪边更自然"不问身份；控制臂 NEUTRAL_PARAPHRASE 不进答案键）。三种 kind 共用同一套闸门（src_ok/非病句/基准段）+ 同种子同位置可复现；runner 加 `--task naturalness`（NAT 模板），未知 task 响亮报错。**其余 §14 子基准待解锁**（human_vs_ai 缺基准段重建候选；Semantic Fidelity 等 6 项需构题器+可验证答案键；Preference Prediction / Reconstruction Quality 需基准段上的集霸裁定）——不装假仪器 | `docs/benchmark-subs-20260919.md`、`tests/test_benchmark_subs.py`、`benchmark_build.py` docstring 状态表 |
| T6 任务13 RM/Rewrite | `7862666` | `--rm`：**RM 1316 条**（pos/neg 0.953；来源 user_verdict 362 / corruption_variable 190 / judge_majority 764 弱标；基准段隔离 266、控制臂排除 5）；`--rewrite`：**845 条**（instruction=帧要点 / output=人类原文） | `data/exports/rm_v1_summary.json`、`rewrite_v1_summary.json`、`tests/test_export_rm_rewrite.py` |
| T7 硬 Gate 探路 | `d848fb7` | `scripts/judge_debias_probe.py`（只读）：corr24 数据面上对四家评委做 raw/位置基线/恒定human/反转投票/map偏移校正（控制臂转移矩阵+拉普拉斯平滑）五种变换，必报四样。**结论：不采信任何过线声明**——agnes 原始 1.000(n=5) 置换 p=0.0998 不显著；map 校正在劣化对照 16 题 0/4 全灭（N3 未过）→ 评委方向偏差在现有数据上**不可校正**，gold standard 仍是第一瓶颈（与 §0.5③ 一致） | `docs/judge-debias-report-20260919.md` |
| T8 交付文档 | 本节 | 本节 + 任务表 11/13 行更新 + §12 待办表第 5 项更新 | — |

**顺手修的（不占任务号）**：
- `test_engine` 偶发红测加固（`8ab21e6`）：归账断言的 ">0" 隐含"采样段没校勘过"前提，
  全库采样撞上别的测试写好的 integrity 时会静默归零——显式清空选中段 integrity 使前提自足。
- 桥接子进程不弹控制台黑框（`e88da91`，朱十一要求；F:/Hermes/scripts 不可用时静默回退）。

⚠ **未跟踪文件 `app/static/polish_kimi.css` 来历不明**（非本轮 agent 所建，09:4x 出现在
工作区）——未动、未提交，等集霸认领或删除。

📌 **集霸拍板的 4 项本日未动**（gold standard / 训练通道 / 换干净源文本 / 令牌轮换）——只记录不实现。

**评审台新入口**：`<站点>/console/ui?t=<令牌>` —— 第二界面只读控制台（16 模块仪表），
`?t=` 首次换 cookie 后即可去掉；**判题仍在第一界面 `/`**，写接口不经过第二界面。

## 0.75 2026-09-19 午后（队列后高价值工作，续 §0.7）

队列完成后接续做的（每步见 `F:\Hermes\team\gui_report.md` 台账）：

1. **§14 硬闸**（`e9fb653`）：`make_random_batch` 建批前硬拦 role='benchmark' 段上的
   候选——此前只靠"别把基准实验指给建批脚本"的自觉；拦下量显式打印。
2. **EXP-BENCH-RECON**（`scripts/bench_recon_setup.py` 装配，引擎全绿跑完）：
   718 个基准段补齐 source_check / L 帧 / 命题 / 自由重建候选（0 失败；
   deepseek-v4.1-flash 单模型，重建 temperature=0.7）。
   ⚠ 段级校勘发现坏源随规模上升：基准段 src_ok=False 从 17 涨到 ~173（~24%），
   与交接 §7.7 的 21% 一致——闸门在正常工作。
   ⚠ 命题分解模型加 `LG_PROPOSITION_MODEL` 环境钩子（`residual_sem.py`，照
   LG_SOURCE_MODEL 先例；kimi ~2.6 次/分要 4.6 小时 → deepseek ~13 次/分）。
   本批命题 23 条由 kimi 完成、其余 deepseek——按行记录 provenance，可追溯。
3. **hvai-v1 子基准建成**（`8c46d07` 构建器 + 本日数据）：BS-95651478c6dd，
   **545 题**（545 段 × 1 重建候选；173 段被 src_ok 闸门排除）——§14 的
   Human-vs-AI Discrimination 从"待解锁"变"已建"。与 corruption_detection 的
   差别：那边是"按单变量改坏的原文"，这边是"模型从帧自由写的"。
4. **nat-v1 自然度子基准三模型读数**（第一份跨模型数据，**provisional**）：
   qoder **0.915**（184/201）· kimi **0.910**（183/201）· deepseek **0.879**
   （160/182，19 题未答）。最弱类型都是"文学腔"方向（NARRATOR_JUDGMENT 0.71 /
   LITERARY_OVERWRITE 0.75 / PARALLELISM_OVERUSE 0.75），与 §7.5 一致。
   **读法**：与控制臂"评委 77% 偏 AI 版"并存 → 评委**读得出**自然度（~0.9），
   但**偏好**仍然反向——两条轴分开，现在有了数字。
   ⚠ 别过度声称：n 聚在 15 个段落（段级聚类，非独立样本）；答案键来自构造；
   未做否掉检验；单集合单版本。
5. **否掉检验工具**（`4395a9e`）：`scripts/benchmark_falsify.py` 对子基准跑分做
   四项检验（段级符号翻转置换 / 位置偏差 / 留一类型敏感性 / 未答率）。
   **nat-v1 三模型全部 pass**（qoder 0.915 / kimi 0.910 / deepseek 0.879；
   置换 p≈0、留一波动 ≤0.01、无位置偏差）——自然度轴读数从 provisional 升为
   通过否掉检验的结论（仍受 15 段聚类上限约束）。
6. **负面库重导 v2**（`23b3140`）：发现 `corrupt_negatives_v1.jsonl`（108 条）的
   生产者从未入库且早于病句重查、没过坏源闸。口径 code 化为
   `export_training.py --negatives`（与 DPO 对同款 drift_ok 总闸；老行
   fact_consistent=0 是缺省不是判否）。v2 = **103 条 18 类**，隔离审计
   基准 266 / 坏源 150 / 控制臂 5 全部拦下。下游一律用 v2，v1 弃用。
7. **语料扩产第三批**：EXP-0919-SCALE，挑 300 段 → 校勘 287 ok / 70 判坏
   （判坏率 ~24%，与 §7.7 一致）→ **+222 L 帧**（L 主帧总数 ~1400+）。
8. **训练导出刷新**（15:10）：扩产两批的 222+ 新 L 帧灌进训练口径——
   `--rewrite --ver v2` = **1798 对**（v1 845）、`--from-frames --ver v3` = **1798 条**
   （v1 219）；隔离审计：基准 746 / 坏源 68-93 / 番外 18 / 夹具 8 全部拦下。
   下游一律用 rewrite_v2 / writer_sft_v3，旧版本弃用。
9. **子基准全天读数汇总**（⚠ 军师 P1-3 长度基线退回后**大幅下修**）：
   · 长度基线（"只选较短文本"）：nat-v1 **0.944**、hvai-v1 **0.872**——两个
     集合上简单规则都**不低于**模型读数（劣化侧普遍更长，"选短"≈白拿分）。
   · nat-v1：qoder 0.915 / kimi 0.910 / agnes 0.910 / deepseek 0.879（已答口径）
     全部**不显著优于长度基线**（配对 p=0.11~0.48）→ 四模型判定由 pass **降为 weak**。
   · hvai-v1 全量：qoder 已答 0.827（429/519）、全题有效 0.787（26 题未答，全在
     《琼明》）、长度基线 0.872 且显著压过模型（75:53, p=0.063）→ **weak**。
   · 修正后的读法：模型在"认出人类侧"上**还有超出长度的真信号，但被长度混淆
     严重污染**——自然度/身份两轴的定量结论全部退回 provisional，任何"评委
     读得出 X"的说法必须先过长度基线。AI-vs-AI 排序（ai_ranking_v1）的标签
     仪器同受此质疑：chosen 侧 deepseek 72% 可能部分是长度效应，**v2 必须先
     在 AI-vs-AI 对上测长度基线**再谈放量。
10. **语料扩产第四批**：EXP-0919-SCALE2，+209 L 帧（今日两批共 +431；
    L 主帧 ~1600，距"几千"目标过半）。
    ⚠ **2026-09-20 修正（P0 复盘证伪本条初判）**：第五批 0 帧的真因是
    `deepseek/deepseek-v4.1-flash` 已下线（503 model_not_found 全线死，
    源校勘 0/300 通过 → 0 帧），**不是池子耗尽**——初判把模型事故误归因为
    数据枯竭。修复后第五批重跑落地 **202 帧 / 503=0**
    （EXP-0919-SCALE3，`data/_dbg/scale_0920b_20260920.log`）；pick(5000)
    四作品各 1250 段均衡、corpus v2 零混入。三条"续产路"（新作品源 /
    min_chars 40 / 重切分）**均不再必要**，扩产按需直跑。
    ✅ **corpus v2 已产出**（T-CORPUS-V2，2026-09-20）：斗罗/将夜各建新 Work
    （106,489 段 1:1 镜像，text=TYPO_MAP 修复后文本；v1 原样保留），
    映射 data/exports/corpus_v2_map.jsonl；入库闸门 app/typo_map 扫描
    add_work 只记 note 不改文本。
    ■ **corpus v2 口径（会审①收口 U0c，2026-09-20）**：
      · **v2 段永不参与基准评选，基准只在 v1 侧维护。** v2 是 v1 的 TYPO_MAP
        修复副本，同一内容在库里存在两份——任何评选/入池/gold 指认只认 v1 那一本。
      · 权威标记 = **`Work.v2_of`（存来源 v1 Work.id）**，由 v1 work.id 派生，
        是 build 的幂等键；标题后缀「（corpus v2）」只是人类可读 + 回填前历史行的
        兼容通道。**幂等与追溯不认标题**（旧实现按 Work.title 查，同名多 Work 会
        被第一本的 v2 顶掉、静默不产出）。
      · 排除已实装（判定统一走 `app.models.exclude_corpus_v2_segments` /
        `is_corpus_v2_work`，不再各处硬编码字面量）：`app/near_dup`
        （train_sampling_pool + split_benchmark）、`scripts/scale_corpus.pick`、
        `scripts/goldpick_build`、`scripts/export_training`（剔除原因 `corpus_v2`）、
        `scripts/benchmark_build`（role 闸之外的第二道血缘闸）。
        回归：`tests/test_corpus_v2_isolation.py`（7 条）+ `tests/test_corpus_fix_v2.py`。
      · **存量收口**：`python scripts/corpus_fix_v2.py --backfill`（默认 dry-run
        只报告）→ 加 `--apply` 写库：按段上的 `corpus_v2_source` 锚反查 v1 work
        补 `v2_of`，并把 v2 段继承来的 role 显式清零（v1 侧一律不动）。
        **2026-09-20 已对生产库执行**（写前备份 `F:\agi\_bak\language_genome_20260920_0456.db`）：
        v2 段 `role='benchmark'` **224 → 0**（将夜 109 + 斗罗 115），全库 benchmark
        **966 → 742**（−224，v1 侧 109/115 一根没动），两本 v2 Work 的 `v2_of`
        由 null 回填为 `WK-a052258c` / `WK-8e8e0459284d`，`unresolved_lineage=[]`
        （全部靠锚反查唯一命中，无需猜测）。审计行写入两本 v2 Work 的 note。
        复核口径：v2 侧真实同文镜像段 ≈218 段 / 216 种文本（另有 1 个退化的
        2 字段「……」在 v2 侧复现 3284 次，把 DISTINCT 计数撑到 3502，不是内容重复）
        ——数量与"224 个继承 role 的段"基本吻合，证实污染就是 v1 基准的 1:1 复制。
      · ⚠ **仍未收口的下游（在途 P0 文件，需协调后再动）**：
        `scripts/controlled_corruption.py` 的选池（`Segment.role.is_(None)`，约 659 行）
        与基准划定（约 731/1309 行 `role = "benchmark"`）不带 v2 排除——这是
        v2 重新被标成基准段的**真复发路径**；`scripts/ai_ranking_build.py:81`
        的 role 池同理（目前只是间接暴露：v2 段上没有候选）。补一行
        `.filter(exclude_corpus_v2_segments())` 即可收口。
      · 建批侧纪律不变：v1/v2 同文并存，实验用 `work_ids` 明确选边。
11. **评审台令牌已轮换**（2026-09-19 集霸授权代行）：新令牌在
    `data/review_token.txt`（chmod 600，不外发）；旧令牌已在 1 个日志文件脱敏
    （`<TOKEN-REDACTED>`）并经远程模拟验证作废（401）；服务已重启，
    **集霸手机上的旧链接已失效**——新带令牌链接看 `data/_dbg/serve_rotate_20260919.log`。
    ⚠ 新令牌同时出现在本次会话记录与 serve 日志里（轮换脚本必然打印）；
    如需彻底离线保管，可再轮换一次并跳过终端输出。
12. **训练可行性评估产出**（集霸授权项 4，本轮不拍通道）：
    `docs/training-feasibility-20260919.md`——数据盘点（SFT 1798 已过千、偏好信号
    走负面库+AI 排序双轨绕开被否方向）、QLoRA 7B~14B 为唯一合理档位、
    单次训+评迭代 ≈ ¥10~35（4090 行情）、§53 六条标准 3/6 已可全自动测、
    1/5 号依赖集霸人工盲评、4 号 Implicitness 缺构题器。
13. **字表归一化修复完成**（授权项 2，`ca74785` + 本条）：斗罗/将夜系统性错字
    438 处修复（千雪→千仞雪 390、吴天→昊天 两书 47、了天斗罗 1），387 段翻回
    池。**前后对照（同源 40 对，qoder）：0.700 → 0.900（Δ=+20pp，
    Fisher p=0.048）**——错字对仪器读数的影响被定量；斗罗历史读数偏低部分
    归因于此（provisional）。⚠ 初版"387 段翻回可用池"经军师 P0 退回**已撤销**：
    字表工具不许碰 integrity（修人名≠无缺句），相关段重置未校勘后由
    source_check LLM 重判。详见 `docs/typo-normalization-20260919.md`。
    复跑：`scripts/normalize_typos.py --scan/--apply/--report`。
14. **授权代行四项（2026-09-19 晚）**：
    · **项1 gold standard 双轨**：(c) 负面库 v2 已是主线（103 条，`23b3140`）；
      (b) AI-vs-AI 相对排序上线——`scripts/ai_ranking_build.py`（自然度轴三评委
      多数决、weak 标签、断点续跑、`LG_RANKING_JUDGES` 可覆盖；deepseek 网关
      晚间连败后降级 kimi+agnes 双评委），产物 `ai_ranking_v1.jsonl`
      **105 对**（132 对尝试、45 对评委分歧弃权；全部双评委一致票 n_valid=2）。
      ⚠ **已知偏向（必读）**：chosen 侧 deepseek 占 72%（76/105）——评委轴认为
      deepseek 文风更自然。DPO 用它训练会把模型往 deepseek 风格拉，不完全是
      "更不像 AI"。缓解（v2 待做）：配对按模型均衡采样；或先在 hvai-v1 上
      复验排序仪器再放量。数据全带 weak 标签与逐票留痕，可审计。
    · **项2 字表归一化**：见上条 13（`ca74785`/`fef07a0`，0.700→0.900）。
    · **项3 令牌轮换**：见上条 11（`0fc6935`）。
    · **项4 训练可行性评估**：`docs/training-feasibility-20260919.md`（`8322819`）。
15. **运维**：评审台服务 8787 曾掉线，10:00 已重启（`data/_dbg/serve_20260919_1000.log`，
   令牌不变，隧道 URL 会变）。后台管线曾于 10:13 集体停摆 ~5 分钟（两进程同时
   无调用流、无子进程，疑似网关/代理抖动）——按纪律杀掉重启后恢复；重启的作业
    都幂等。

## 0.8 2026-09-19 晚间 · 前端优化（websrc 接通，集霸批准方案）

**背景**：`websrc/` 16 页由 Hermes 于 19:29 入库（`b0b02f9`），入库时是**死页**——
① `api.py` 未 mount，浏览器访问不到；② 10/16 页 fetch 的 slug 与 `console.MODULE_ORDER`
不一致（页面写 `/console/overview`，API 叫 `dashboard`；`frames`→`semantic-lab`；
`arena`→`reconstruction-arena`；`residual`→`expression-residual`；`strategy`→`strategy-atlas`；
`judges`→`judge-arena`；`preference`→`preference-lab`；`hardcase`→`hard-cases`；
`benchmark`→`benchmarks`；`training`→`training-data`）；③ 页面直接读 `data.stats`，
而 API 返回 `{module,title,data}` 信封，缺解包层；④ 页面期望统一 `{stats,rows}`，
而 `console.py` 16 模块各有各的真实键，形状不符 → 即使接通也只能渲染空态。

**集霸批准的三项决策**：①**A** 视觉走金棕统一（弃用 websrc 原 GitHub Dark 蓝
`#58a6ff`，对齐盲评台石墨底 + 金棕体系）；②**A** `console.html` 旧外壳降级为重定向
（代码保留不删）；③**A** 盲评台 `index.html` 头部加一个"研究台"入口 `<a>`（其余零改动）。

**方案与坑点**：见 `docs/frontend-plan-2026-09-19.md`（五阶段 P1~P5 + 8 条坑点 + 硬边界）。
关键边界：**`app/console.py` 与 16 模块返回 shape 一字不动**（`test_console_each_module_shapes`
钉住），契约对齐全部在 websrc 侧完成；写操作仍全部留在第一界面，研究台保持 GET-only。

**对新入口的影响**：`app/access.py` 是全路径 `@app.middleware("http")`，`/lab/*`
自动继承令牌闸，无需额外配置；但 `_GATE_HTML` 的 `onsubmit` 原先把 `?t=` 换 cookie 后
硬跳回 `/?t=`，从研究台被拦时会跳到盲评台 —— 已改为跳回**当前路径**（首页行为不变）。

### §0.8 执行结果（当晚 P1–P5 全部完成）

**新入口**：`/lab/overview.html`（首页 `/lab` 自动跳此）。
- 16 页全部接通真实数据；`/lab/_shared/{tokens.css,base.css,lg.js}` 为共享层；
  `/lab/_shared/selfcheck.html` 是 **16 页运行时体检页**（在 iframe 里真实加载并读回渲染结果，
  改完共享层或页面开一次即可，一屏看 16 页）。
- `/console/ui` 旧外壳 **302 → /lab/overview.html**（`console.html` 文件保留，
  其 4 项静态契约测试仍在跑；`test_console_ui_route_serves_page_no_store` 已按新契约演进为
  `test_console_ui_route_redirects_to_lab`）。
- 盲评台 `index.html` 头部新增一个 `<a id="lab-entry">研究台 ↗</a>`（其余一字未动，
  不变量测试原样全绿）。

**关键判断修正**：websrc 并非"通用键并集表格"——每页已有定制可视化（arena 候选对照、
judges 记分板、preference 胜率分布、strategy Wilson 区间）。故**保留各页 renderer**，
只抽令牌 / 组件样式 / 工具函数 / 导航 / 信封解包，未推倒重来。

**实测抓到的真 bug**：`frames.html` 写了 `lk.over_0.6_by_layer` —— 属性名以数字开头是非法 JS 语法，
整块 renderer 静默不执行（导航都建不出来）。**用方括号访问**修正。
据此补两条防线：`tests/test_websrc_contract.py` 里的 `node --check` 语法校验（每页内联脚本 + `lg.js`），
以及上面那页浏览器自检 —— 这是"没有构建工具时"能拿到的最低成本编译检查。

**未闭合缺口（未用假数据顶替）**：`/console/*` 是聚合口径，两页原设计要明细而没有数据源 ——
`reconstruction-arena`（对阵记录 + 候选正文并排）与 `semantic-lab`（Frame 清单 + 筛选器）
本次改为等价的分布视图，页内写明边界。**注意** `test_console_each_module_shapes` 用的是
`need <= set(data)` 子集断言 —— 后端加字段不会破坏测试，故若要让这两页恢复原设计，
扩 API 是安全路径，但属范围决策，**留待集霸拍板**。

**测试**：全量 **405 项全绿**（新增 `tests/test_websrc_contract.py` 18 项）；
16 页路由 16/16 通过 + no-store；目录穿越 404；浏览器自检 16/16 通过；
深/浅双主题截图核对（`data/_dbg/lab-shots/`）。详见 `docs/frontend-plan-2026-09-19.md` §5。

## 1. 交接时点状态

### 实验

- 单实验：`EXP-0911-B82D`，已冻结（`config.frozen=True`，`run_experiment` 拒绝重跑）
- 单一语料：《琼明神女录》（跨 ≥4 语料是 Phase 2 的要求，尚未做）
- 🚨 **整个实验只有 30 个人类段落**（2026-09-16 查清，此前一直没意识到）：
  411 个可盲评候选**全部**是这 30 段的复述；未判池 219 个候选也只覆盖这 30 段
  （每段 6–14 个候选）。**所有批次（first50 / r25 / s30 / h30 / r50）判的都是同一批段落**。
  后果：同一个段落会被反复端出来（集霸 2026-09-16 就是为此发问："怎么段落没变"），
  且**共享段落的判定之间不独立** —— 任何把 n 当独立样本的 CI 都偏乐观；
  这一条同时解释了历史上多次出现的"阈值不稳、CI 很宽却仍显显著"的现象。
  想拿到真正新的人类文本，只能加段落/语料（Phase 2 的跨语料线程），
  **在同一个 30 段上加候选是加不出新人类文本的**。
- 🟢 **好消息：新段落早就有现成的**（2026-09-16 查明）——`EXP-0914-*` 四个跨语料实验
  在 09-14 就导入并生成过候选，只是**从未被集霸盲评过**（只被窗口评委判过）：

  | 实验 | 语料 | 段落 | 有候选的段落 | 可盲评候选 |
  |---|---|---|---|---|
  | EXP-0914-C812 | 琼明神女录 | 50 | 50 | 218 |
  | EXP-0914-FF7B | 将夜 | 50 | 50 | 200 |
  | EXP-0914-EE18 | 凡人修仙传 | 50 | 50 | 200 |
  | EXP-0914-6DD3 | 斗罗大陆 | 50 | 8 | 29 |
  | EXP-0913-BA19 | 琼明（2×2 因子） | 30 | 29 | 115 |

  **与 B82D 的 30 段零重叠**。已按"一段一题 + 人类段 ≥40 字"建成三个盲评批
  （`jy` 32 / `fr` 31 / `qm` 24），见 §12。
- 切分：`segmenter_v2`，seg usable = **0.91**（Gate 要求 0.80 ✓）

### 标注

| 项 | 值 |
|---|---|
| 已判（`review_items.status='done'`） | **223 条**（B82D 192 + mix30 30 + 将夜单条 1） |
| 其中白名单内 + 二选一 | **216 条**（human 156 / candidate 60）。另 4 `both_bad` + 2 `tie` + 1 映射丢失不可用 |
| 带噪点批注 | **30 题 / 36 处** |
| B82D 待判池 | **149 条**（有 det 指标、文本可用） |

⚠ **统计批注时不要用 SQL `LIKE '%annotations%'`** —— 空数组 `[]` 也会命中，
会把 16 题误报成 44 题（我写本文时就这么错过一次）。
必须解析 JSON 后判 `len(annotations) > 0`。

### 评测结果（这是最关键的数字）

**干净留出集（r25 真留出，16 题）**：v4 + kimi，AUC 0.678 / κ +0.217
**干净优先集（62 题）**：v4 + kimi，AUC 0.674，CI [0.523, 0.803]
**同子集折外（91 题）**：核心 4 计数特征，agreement **0.673** / κ +0.233
**h30（30 题，2026-09-16）**：v4 + kimi，agreement 0.500 / κ **+0.164**；
⚠ 本批恒定答 human 基线 agreement = **0.867**，把所有评委碾压。

**mix30（跨语料 30 题，2026-09-16）—— 基础率被掰正后的复测**：
用户判 human 比例 **0.556**（不再偏斜）；n=27 可用（2 tie + 1 映射丢失已排除）。

| 口径 | agreement | κ | 挑 candidate | 判 candidate 时精度 |
|---|---|---|---|---|
| kimi v3 | 0.593 | **+0.208** | 0.630 | 0.529 |
| kimi v4 | 0.556 | +0.115 | 0.519 | 0.500 |
| deepseek v3 | 0.519 | +0.093 | 0.778 | 0.476 |
| deepseek v4 | 0.556 | +0.129 | 0.593 | 0.500 |

**这是本轮最重要的**：v4+kimi 在偏斜基础率下（h30）κ +0.164，在**掰正到 0.556/0.444**
（对 κ 最有利的条件）后仍是 **+0.115**，agreement 0.556 = 恒定答 human 基线（0.556），
四个配置**全部低于**位置基线（永远选 A = 0.630）。
→ **「κ 低是因为基础率太偏」这个解释被排除**：换成均衡基础率，评委依然没有可用信号。
⚠ 混合批的**加权 κ 无意义**（三个语料入样概率不同，加权还原不出真实总体），只读原始 κ。

**Gate 要求 agreement ≥ 0.70 → 未过（且现在证据更强）。**

### 采样：新段落的候选胜率显著更高（mix30，2026-09-16）

> ⚠ **2026-09-17 修正（nq50 判完 26 条后）**：见本节末尾的「同书对照终判」——
> 下面这个"新段落候选胜率高"的结论**主要来自语料效应，不是"新段落"**。

| 语料 | 候选胜 | Wilson 95% CI | 人胜 |
|---|---|---|---|
| 凡人（EE18） | **7/9 = 0.778** | [0.45, 0.94] | 2 |
| 琼明-C812（新段） | 4/14 = 0.286 | [0.12, 0.55] | 10 |
| 将夜（FF7B） | 1/4 = 0.250 | [0.05, 0.70] | 3 |
| **合计** | **12/27 = 0.444** | **[0.28, 0.63]** | 15 |
| 对照：B82D 旧 30 段 | 4/30 = **0.133** | [0.05, 0.30] | 26 |

- 池化差异 Fisher 双尾 **p = 0.0168**（显著）；**同书对照**（琼明新 4/14 vs 琼明旧 4/30）
  p = 0.242 **不显著**。
- 两批候选生成器**完全相同**（deepseek-v4.1-flash + muse-spark-1.3，同为
  `reconstruct_v1`/`recon_ctx_v1`）→ **排除"模型文风差异"这个解释**。

→ 诚实的读法：**池化差异显著，但其中相当一部分是语料效应**（凡人的候选就是能赢）。
"旧 30 段是特别不利于候选的样本"这个假设**方向一致但未被证实**（同书 p=0.24）。
要分清"段落抽样"与"语料"，还需要更多语料 × 更多段落。

#### 🔻 同书对照终判（nq50，2026-09-17）——上述假设被否

`nq50` = 琼明**全新 50 段**（同书、同生成器、同建批口径），集霸判了 26 条：

| | 候选胜 | 判定 |
|---|---|---|
| 琼明**旧** 30 段（B82D） | 4/30 = **0.133** |  |
| 琼明**新** 26 段（nq50） | 5/26 = **0.192**，CI [0.09, 0.38] | Fisher 双尾 **p = 0.72 → 无差异** |

→ **"旧 30 段是坏样本"不成立。** 差异来自**语料**：
凡人 **0.778**（7/9）、琼明 0.19–0.29、将夜 0.25。
所以"补样本"应当按语料选（凡人产量最高），而不是靠"换新段落"。

⚠ **nq50 的数据质量**：26 条里 **14 条（54%）**被判成 `tie`/`both_bad`/`cant_judge`
（其它批次 0–10%）。集霸自述"太折磨了"，**疲劳很可能是主因**。这不改变上面的
方向性结论，但意味着这批的**有效二选一只剩 12 条**——引用时必须说明。

**评委侧（nq50，n=12）依旧不行**：kimi v4 κ +0.122 / deepseek v4 +0.087；
agreement 0.42–0.50 vs **恒定答 human 基线 0.917**；四配置全部低于位置基线；
判 candidate 精度 0.11–0.14。**结论与 h30/mix30 一致，且已跨三批复现。**

### 采样：按 score 挑题第二次失败（h30，2026-09-16）

| | 建批承诺 | 实测 | 检验 |
|---|---|---|---|
| 收割段（21 条） | 0.696 | **3/21 = 0.143** | P(≤3) = **2.4e-07** ✗ |
| 整批（30 条） | 0.568 | **4/30 = 0.133** | P(≤4) = **1.1e-06** ✗ |
| 锚点段（9 条） | 0.271 | 1/9 = 0.111 | P(≤1) = 0.253（**判不出来**） |

组内佐证：4 条候选胜的 score 均值 **0.405**，反而**低于**其余 26 条的 0.453；
全批最高分（0.969）那条不是候选胜。→ **打分器在这个池子上没有正向判别力**。

按本批自己的 IPW 池估计 0.128 [0.040, 0.290]，**纯随机抽 30 题预期也是 3.8 条候选胜**，
h30 实际 4 条 —— **本批等价于随机批，30 题只换来 4 条少数类样本**。
对比 s30（旧方案）同样是 4/30 = 0.133：**两次基于打分的挑题以同样方式失败**。

### 测试

**185 项全绿**（2026-09-16 接手时复核：157 原有 + 28 新增）。跑法：

```bash
cd F:/agi/language-genome
F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe -m pytest
```

---

## 2. 五分钟上手

### 环境（务必按这个来）

| 项 | 值 |
|---|---|
| Python | `F:/kelaode/Data/Agents/zqibcc8w9/tools/Python311/python.exe` |
| Shell | Windows 上的 **Git Bash**（不是 Linux，行为差异见 §9） |
| 数据库 | `F:/agi/language-genome/data/language_genome.db`（SQLite，~180MB） |
| 已装库 | numpy、jieba。**没有** torch / sklearn / scipy / transformers |
| 设备约束 | **集霸的设备无法训练模型**（他明确说过）。但本项目的方案**也不需要** —— 全是纯 numpy，见 §8 |

### 起服务（本机 + 局域网 + 外网）

```bash
cd F:/agi/language-genome
bash scripts/serve_remote.sh            # 起了就复用
bash scripts/serve_remote.sh --restart  # 改完代码用这个
```

脚本会打印本机 / 局域网 / 外网三个入口、访问令牌、以及手机可直接点开的带令牌链接。

- 本机免鉴权；远程需令牌，令牌在 `data/review_token.txt`（持久化，重启不变）
- 外网走 cloudflared 快速隧道，**URL 每次重启都会变**
- 鉴权实现见 `app/access.py`，回归测试见 `tests/test_access_gate.py`

### 盲评页面

```
http://127.0.0.1:8787/?batch=<批次名>
```

集霸会在这个页面上做题，同时可以**划词加批注**（噪点标注）。

---

## 3. 一次完整的批改循环（这是日常主流程）

```bash
# ① 挑题：收割 + 校准锚点 —— ⚠ 2026-09-16 起**先读 §7⑥**：
#    按 score 挑题已被 h30 证伪（连续第二次），**不要再建新批**
python scripts/select_harvest_batch.py --n 30 --tag h31 --dry-run   # 先干跑看预期
python scripts/select_harvest_batch.py --n 30 --tag h31             # 确认后写库

# ② 判题**前**先预跑评委分（省掉判完后的等待；可选但推荐）
python scripts/prerun_batch_judges.py --batch h31 --dry-run   # 先看调用量
python scripts/prerun_batch_judges.py --batch h31             # 复用 heldout_eval.run_one，口径逐位一致

# ③ 请在页面批改（集霸做，约 30 题）
#    http://127.0.0.1:8787/?batch=h31

# ④ 判完后评测（评委分已预跑，秒出报告）
python scripts/heldout_eval.py --batch h31        # 用户 κ（含逆概率加权）
python scripts/noise_report.py --batch h31        # 批注汇总

# ⑤ 选择器自检（承诺值建批时已落盘，直接跑即可）
python scripts/anchor_check.py --batch h31
```

⚠ **② 的预跑通常需要重跑 1–2 次**：deepseek 的 `failed_parse` 实测约 **9%**（r50：10/110），
kimi 是 **0/100**。脚本的覆盖断言会列出缺口并以非零退出，**重跑同一命令即可**——
它会跳过已 ok 的、只补缺口（幂等）。别把非零退出当成"脚本坏了"。
（另注：`cmd | tail` 会把 python 的退出码换成 `tail` 的，那样这个断言就形同虚设。
本项目已两次踩到这个家族的坑。）

### 判完后**必须**看的一件事

`select_harvest_batch.py` 会把题分成 `HARVEST`（按分挑）和 `ANCHOR`（随机抽）。
判完后用 **`anchor_check.py`** 对比。**主检验是收割段**（21 条，有功效）：

- 收割段实测 ≪ 建批承诺率 → **打分器对这个池子没有判别力，不要再用它挑题**
- 收割段 ≈ 承诺 → 承诺未被推翻（注意这只是「没被抓到」）

⚠ **锚点那一侧（9 条）几乎放行一切，不能作为通过依据。** 第一版工具只测了锚点，
h30 判完后据此打印「✓ 框一致，可继续」——**放过了真问题**：承诺 0.696 的收割段
只打出 3/21（P=2.4e-07），而锚点 1/9 照样落在 0.271 的 CI 内。已修，并补了回归测试
（`test_anchor_pass_does_not_override_harvest_failure`）锁死这个错法。

---

## 4. 文件地图

### 核心代码 `app/`

| 文件 | 职责 |
|---|---|
| `api.py` | FastAPI 薄层 + 盲评页面路由 |
| `access.py` | 远程访问门（令牌鉴权、代理识别） |
| `judges.py` | **所有评委口径**。`PROMPT_VARIANTS` 在这里，`PREFERENCE_PROMPT`（v4）也在 |
| `models.py` | SQLAlchemy 模型（Candidate / ReviewItem / JudgeRun / Experiment …） |
| `config.py` | `BLIND_REVIEW_PROMPT_VERSIONS` 白名单、frozen 标记 |
| `context_ablation.py` | `scene_context()` —— 喂给评委的上文，**必须与用户看到的一致** |
| `metrics_det.py` | 确定性计数指标（32 维特征的生产者） |
| `segmenter_v2.py` | 切分 |
| `review.py` | 盲评队列与取题逻辑 |

### 分析脚本 `scripts/`（38 个）

按用途分类，**加粗的是当前主力**：

**评测与诊断**
- **`heldout_eval.py`** —— 留出集评测主入口。支持 `--variants`、`--reverse`、`--batch` 多批次
- **`anchor_check.py`** —— **锚点自检**（2026-09-16 加）：判完后核 ANCHOR 实测命中率
  vs 建批外推基准率 + IPW 池整体率。**§3 那道必做自检的工具，此前不存在**
- **`large_sample_diag.py`** —— 大样本 κ/AUC 诊断
- **`pref_drivers.py`** —— 集霸偏好的确定性特征分析（32 维，纯 numpy 逻辑回归）
- **`selector_cv.py`** / `selector_lift.py` —— 选择器评估（折外 precision@K）
- **`threshold_calibration.py`** —— 阈值校准（折外）
- `defect_compare.py` / `defect_threshold.py` / `defect_robustness.py` —— 缺陷口径（**已被否，见 §7**）
- `interjudge.py` —— 评委间一致度
- `annot_circularity.py` —— 词表循环性检验
- `clean_final.py` —— 干净集三口径终审
- **`fusion_probe.py`** —— 融合实验（**已证无收益**）
- **`noise_report.py`** —— 集霸批注汇总

**建批**
- **`select_harvest_batch.py`** —— **当前主力**：收割 + 校准锚点
- **`prerun_batch_judges.py`** —— 判题**前**预跑评委分（复用 `heldout_eval.run_one`，
  种子只由 cid 决定 → 与评测当场跑逐位一致）
- `make_random_batch.py` —— 纯随机批（无偏，但效率低）
- `make_stratified_batch.py` —— **已失败的方案，见 §7**

### 2026-09-18 新增脚本（本轮）

| 脚本 | 作用 | 关键约束 |
|---|---|---|
| `scripts/controlled_corruption.py` | 工作流 B：18 类单变量劣化 + 控制臂；`--report` 出可判别性标尺 | 单变量硬约束、病句硬拒、源文本必须 `src_ok` |
| `scripts/clean_text.py` | 文本清洗：规则删水印 → LLM 还原拼音 → `--polish` 再扫 | **原文不动**，写 `segments.text_clean`；下游优先读它 |
| `scripts/source_check.py` | 源完整性校勘（缺字/截断/人名不一致）→ `segments.integrity.src_ok` | **没查过 = 不可用**；`KNOWN_TYPOS` 是频次自洽确认过的错字表 |
| `scripts/scale_corpus.py` | 语料扩产：挑新段 → 源校勘 → 抽 L 帧 | 不需要人工；SFT 的目标就是人类原文 |
| `scripts/benchmark_build.py` / `benchmark_run.py` | §14 基准：建集（冻结文本）/ 跑分 / 排行榜 / 回归对比 | 基准段 `role='benchmark'` **不进训练导出** |
| `scripts/flavor_span.py` | **片段级 AI 味检测**（`--train`/`--eval-pairs`/`--text`） | 适用边界见 §0.5④，别当稳定仪器 |
| `scripts/flavor_train.py` / `ai_flavor_eval.py` | 判别器训练 + 评估（段落级失败记录在里面） | 按题分组切折 |
| `app/ai_flavor.py` | 规则层（v1 模板词表 / v2 结构式） | 已证**无效**，保留供接手者对照 |

**训练导出**（`scripts/export_training.py`）现在有四种口径，别混：
`--from-frames`（SFT 219，自动）· `--pairs`（DPO 244，方向**未经裁定**）·
`--pairs --strict`（只留集霸判"原文胜"的，现在只有 1 对）· 负面库（`corrupt_negatives_v1.jsonl`）。

### 文档 `docs/`

| 文件 | 内容 |
|---|---|
| **`phase1.5-plan.md`** | **主文档，955 行**。①–⑨ 节按时间记录了全部实验与结论，**新 agent 必读** |
| `calibration-report-v1.md` | 第一版标定报告 |
| `adversarial-review-2026-09-11.md` / `-09-14.md` | 对抗性审查记录 |
| `calibration-design.md` | 实验设计 |

### 测试 `tests/`（23 个 test 文件，185 项）

关键回归文件：
- `test_access_gate.py` —— 访问门（含"经代理的 127.0.0.1 仍须鉴权"）
- `test_position_diagnostic.py` —— 位置偏差与翻转率诊断
- `test_defect_judge.py` —— 缺陷口径的计数规则
- `test_harvest_selector.py` —— 挑题器的池基准率与锚点不重叠
- `test_anchor_check.py` —— 锚点自检：层不许混算 / 弃权不进分母 / 缺 `w` 不许静默 /
  IPW 必须与手算一致 / **锚点那侧通过不得覆盖收割段的失败**（第一版真犯过的错）
- `test_prerun_batch_judges.py` —— 预跑的纳入条件必须与评测一致；覆盖核对必须能发现缺口
- `test_compile_all.py` —— 全库编译检查（防中文全角引号写进代码）

---

## 5. 核心概念

### 盲评任务

给集霸看同一场景下**同一段语义**的两种写法（A/B 匿名随机），他判哪个更好。
两边之一是**人类原文**，另一是 **LLM 重建的候选**。

`winner_resolved ∈ {human, candidate, equal, both_bad, cant_judge}`
（服务端暗记 `human_was_a`，不上面板，保证匿名）

### 三道 Gate 相关指标

| 指标 | 含义 |
|---|---|
| `agreement` | 评委与集霸判断一致的原始比例 |
| `κ` | 扣除随机一致后的比例。**基础率偏斜时只有 κ 可比** |
| `AUC` | 与阈值无关，测"连续信号是否与用户同向" |

⚠ **汇报一律以 κ 与 AUC 为准。** agreement 会被边际分布一起抬
（实测出现过 agreement 显著、κ 不显著）。

### 必须报的基线

1. **恒定多数类基线**（如"永远答 human"）—— agreement 上限的参照物
2. **位置基线**（"永远选 A"的准确率）—— 评委若低于它，说明是**负信息**
3. **置换零分布** —— 别拿 0.5 当基准，折外挑阈值本身会带来乐观偏差

### 抽样框（本项目最容易出错的概念）

同一个"打分模型"，在**不同抽样框**上含义不同：

| 框 | 定义 | 实测候选胜率 |
|---|---|---|
| 优先队列 | 按 `human_upset` 过采样的"难例" | ~0.31 |
| 自然池 | 随机 / 未按分筛 | ~0.32 |
| 分层批 s30 | 按 score 分层 + 错误先验 | **0.133** |

**跨框搬运先验会彻底失效。** 新增批次时务必确认它的框。

---

## 6. 必须遵守的纪律（10 条）

> 每一条都对应一次真实的事故。**不要因为"看起来合理"就绕过。**

### ① "结果好到需要专门找反证"的结果，先找反证再采信

本轮 9 次分析里，**有 3 次出现了"恰好越过 Gate 线"或"大幅突破"的数字，
3 次都是错的**：

| 出现的好数字 | 真相 |
|---|---|
| v4 挑候选比例恰好 0.500 | 位置偏差掩盖（真值 0.74–0.80） |
| 缺陷口径 AUC **0.897** | 词表过拟合；干净集掉到 0.569 |
| 核心4维 agreement **0.725** | 阈值过拟合；折外 0.673 |

**否掉自己的成本只有一轮；采信后返工的成本是数轮。**

### ② 特征 / 阈值 / 模型选择必须放进 CV 折内

实测：先全量选特征再 CV，AUC **虚高约 0.06**。
折外挑阈值同样会乐观偏差。**任何"挑"的动作都要在折内。**

### ③ 抽样框不能混

见 §5。**评估选择器时，必须用与目标池同框的数据测其增益。**

### ④ 静默失败是本项目最高频故障模式

已经踩过的形态：
- `verdict` 字段有双形态（ORM 是 dict、裸 sqlite 是 TEXT）→ 只认一种会**静默返回 None**
- 函数建了结果对象却忘记 `append` → 静默返回空列表
- 杀掉进程失败但脚本继续 → 验的是**旧代码**
- `curl -w '%{http_code}' || echo 000` → 恒为真，脚本一直谎报"服务在运行"

**统一做法：走 `_as_dict()`；脚本里凡是"取数然后判断"，若取到空必须报错而非继续。**
分析脚本在空数据上静默跑完，**比崩溃糟糕得多**。

### ⑤ 白名单优于黑名单

`BLIND_REVIEW_PROMPT_VERSIONS` 是白名单。忘登记时"少题"可察觉；
黑名单则是"静默混入"不可察觉。

### ⑥ 评委与用户必须吃同一份上下文

共用 `context_ablation.scene_context`。两处各写一份必然漂移，
会造成"用户带上文判、评委空手判"的不对称比较（这是真实事故）。

### ⑦ 改动前先归档

**仓库无 git。** 改 prompt 前先把旧版存进代码（`judges.PROMPT_VARIANTS`），
否则无法回溯对照。

### ⑧ 任务可判别性 > 措辞

大样本（n=99、少数类 36）上"整段哪边更好"的 **AUC ≈ 0.50**，与样本量无关。
**所以不要靠继续调 prompt 措辞去救这条路**，已证无效。

### ⑨ 评估小样本一致性必报四样

κ（不是 agreement）、置换零分布、恒定多数类基线、位置基线。
分层抽样时还要报**逆概率加权 κ 与 n_eff**。

### ⑩ 设备约束

集霸的设备**练不了模型**。凡提方案先检查能否用
「计数特征 + 现成 LLM 分 + 纯 numpy 逻辑回归」实现——本项目全部方案都是这样。

---

## 7. 已否掉的路线（**不要重试**）

### ⑦ 表层特征检测"AI 味"（2026-09-18 全面证伪，**别重试**）

四条路一起否掉，且原因已查清：

| 做法 | 结果 |
|---|---|
| 规则词表 v1（`嘴角勾起`/`心中一紧`/四字格…） | 对他 63 条批注：段级 20.8%（低于抛硬币） |
| 规则词表 v2（照他批注结构重写成"细节+意义解释"结构式） | 段级 **7.5%**，更差 |
| 让 LLM 直接打"像 AI 写的程度" | 对他批注那侧**打分更低**（0.372 vs 0.454） |
| 段落级向量 + 逻辑回归 | AUC **0.443**（低于随机） |

**根因**：这些**人类原文本身就是中文网文**，满是他会圈的"AI 味"词（`凤毛麟角`、
`心中一紧`、四字格）。表层统计只能分出**网文味**，分不出**AI 味**。
→ **凡是"用表层统计判 AI 味"的新方案，先回答"你的参照文本是不是同样带这些痕迹"**，
答不上来就别做。

**活下来的是片段级**（他标的片段 vs 同段等长随机片段，AUC 0.710）——
见 §0.5④。它成功的原因正相反：**不比较整段风格，只问"这一段文字里哪一处最扎眼"**。

### ⑧ 把 `human=chosen` 当默认训练方向（2026-09-18 证伪）

24 题裁定：他只有 4 题判人类原文胜（17%），11 题"两边都不好"。
→ 拿默认方向训 DPO = 教模型模仿他不认可的文本。已加 `--strict` 与 `suspect` 标记。


按被否时间顺序。每条都做过完整实验并有文档记录。

### ① 调 prompt 措辞（v3 → v4 之后再调）

v4 的 rubric 把 AUC 从 0.585 提到 0.674（真实效果，跨三个独立评估稳定）。
但**再往上调没用**：大样本上 v3 的 AUC = **0.504**（完全无信息），
说明瓶颈不在措辞。

### ② 位置偏差校正（正反两序各跑一次再合并）

动机：评委选 A 率 0.74–0.80，位置方差吃掉功效。
结果：**未见可靠提升**（kimi v4 略降、deepseek v4 升、v3 持平），
且弃权使 n 腰斩（46→20~33），CI 反而更宽。
**成本翻倍、收益不确定。代码保留在 `heldout_eval.py --reverse`，但只当诊断工具。**

### ③ 缺陷检测口径（`judge_defect_v1`）

让评委只"指缺陷"（必须引用原文），胜负由计数规则导出。
在集霸做过批注的那两批（`r25` + `s30`，共 46 题）上表现极好：
AUC **0.897**、折外校准 κ **+0.532**、置换检验 p=0.015、30 个种子稳定。

**但在集霸没标注过的优先队列（62 题）上完全不迁移**：AUC **0.569**、校准 κ **−0.117**。
→ **缺陷类型词表是对那 46 题的过拟合。**

⚠ 这里有个容易误读的点：词表是从 46 题中 **16 题的批注**里提炼的，
却在全部 46 题上评估——同一批数据既"出题"又"判卷"，属轻度循环。
**正是因为这个可疑之处才去做了干净集检验，也才发现了不迁移。**
改动已保留在代码里（`DEFECT_KINDS` / `PREFERENCE_PROMPT_DEFECT`），**但不要采用**。

### ④ 特征 + 评委 融合

假设两条路线互补（确定性特征抓句长结构、评委抓文风）。
同子集 n=91 实测：核心4维 AUC 0.617，加评委分后 **Δ = +0.000**，
配对 bootstrap CI [−0.045, +0.048] 不显著。
且**维度越多越差**（5维 0.615 → 36维 0.562）。
→ 不互补，且样本量撑不住更多维度。

### ⑤ 分层抽样建批（`make_stratified_batch.py` 的原用法）

用"优先队列"上估的层先验，去"自然池"抽样（s30）。
预期候选胜 14 条，**实际只有 4 条**（0.133），**反而低于纯随机 0.24**。
→ 当时以为修正版 `select_harvest_batch.py`（掺锚点自校准）能救。**见 ⑥。**

### ⑥ 按 score「收割」挑题（`select_harvest_batch.py`）—— **2026-09-16 h30 证伪**

修正版的设想：70% 按打分模型挑高分 + 30% 池内随机锚点，锚点提供自校准。
h30 判完（30/30）的结果：

| | 建批承诺 | 实测 | 检验 |
|---|---|---|---|
| 收割段（21 条） | 0.696 | **3/21 = 0.143** | P(≤3) = **2.4e-07** |
| 整批（30 条） | 0.568 | **4/30 = 0.133** | P(≤4) = **1.1e-06** |
| 锚点段（9 条） | 0.271 | 1/9 = 0.111 | P(≤1) = 0.253（判不出） |

组内佐证：4 条候选胜的 score 均值 **0.405**，反而**低于**其余 26 条的 0.453；
全批最高分（0.969）那条不是候选胜 → **打分器在这个池子上没有正向判别力**。

按本批 IPW 池估计 0.128 [0.040, 0.290]，**纯随机抽 30 题预期也是 3.8 条**——
h30 等价于随机批。与 s30（4/30 = 0.133）**同数、同方式失败**。

**结论：这条路线不要重试。** 锚点自校准机制的**设计**没错，但它把检验放在
只有 9 条的一侧（功效极低），真正有功效的收割段当时没有落盘承诺可对照——
所以自检工具本身也修了（见 §3 与 `anchor_check.py`）。

**⚠ 还有一个更早就该拦住它的证据**：挑题脚本本来就有「帧检查」——
对比待判池与校准集的 score 分布。**它当时就打印了警告**（两池均值差 >0.06），
然后批次照建。2026-09-16 的干跑显示这个偏差已经很极端：

```
校准集  n=114  均值 0.316   p90 0.650   max 0.976
待判池  n=158  均值 0.237   p90 0.373   max 0.412   ← 池子里已经没有高分题了
```

机制是**撇脂**：每建一个收割批就把池内最高分的 21 条抽走并判掉，校准集却仍由
旧批次构成（分数分布更高）→ 模型的外推率必然失真。
**已把这个警告升级为硬 Gate**：帧检查不通过时**默认拒绝建批**（exit 2），
要建必须显式 `--force`，并提示改用纯随机批（`make_random_batch.py`）。
理由就是本项目的纪律「放量前硬 Gate」——**不拦人的警告等于没有 Gate**。

**更要紧的推论**：池子的候选胜率点估计只有 **0.128**（历史外推是 0.271）。
若属实，「把少数类补到 100」需要 ~500 道题的人工批改，**不可行**。
这正是下一步必须先由集霸拍板的原因（见 §8）。

---

## 7.5 受控劣化数据集（工作流 B，2026-09-18 建成）

**这是什么**：从人类原文出发，按**单一变量**制造劣化版本（18 类 + 1 条控制臂），
事实层冻结、漂移由**另一个模型**校验。它同时是三样东西：
①DPO 训练对（`chosen=人类原文 / rejected=劣化版`，还带"被判定的变量名"）；
②**自带答案的仪器**——每对都知道哪边是原文，所以能脱离集霸量出评委的判别力；
③§14 隐藏基准的一块（corruption 检测）。

**怎么跑**（幂等，可反复跑）：
```bash
python scripts/controlled_corruption.py --n-seg 24 --conc 8          # 生成 + 校验
python scripts/controlled_corruption.py --reverify                   # 换口径后重判被拒的
python scripts/controlled_corruption.py --judge --conc 8             # 评委判「人 vs 劣化版」
python scripts/controlled_corruption.py --report                     # 类型表 + 可判别性标尺
python scripts/controlled_corruption.py --build-batch corr24 --batch-n 24
python scripts/controlled_corruption.py --fresh-benchmark 12         # 纯基准段（§14）
python scripts/export_training.py --pairs --ver v1                   # DPO 对导出
```
**给集霸的入口**：`<站点>/?t=<令牌>&batch=corr24`。

**核心读数（2026-09-18，440 条 × 4 评委 = 1957 判定，详见 phase1.5-plan §⑲/§⑲-1）**：
- 评委**认出人类原文**的比率 = **0.857**（各家 0.75~0.89）；位置基线 0.52。
- **控制臂（中性改写）只有 0.231** → 评委对 **AI 措辞本身有 77% 的正偏好**。
  这是读所有评委数字时必须对照的基线。
- **文学腔 0.513**（唯一被评委"判平"的劣化）——正好是集霸最烦的那个方向。
- 显式化/过度解释/心理解说/情绪直说 = **0.95~0.97**：**评委不是读不出这条轴**
  （推翻 §⑱ 的旧解释：那五次失败是"没有对照组还想问出因果"的问题，不是评委瞎）。

**四条纪律**：
1. `corrupt_v1` 只在 `SERVABLE_PROMPT_VERSIONS` 里，**不在** `BLIND_REVIEW_PROMPT_VERSIONS`：
   劣化版胜率天然≈0，混进随机池会把候选胜率/κ 静默拽偏（回归测试锁定）。
2. 控制臂 `NEUTRAL_PARAPHRASE` **不许进 DPO 导出**（中性改写不是"该被拒绝"的写法）。
3. `Segment.role='benchmark'` 的段**不进训练导出**（§14）。
4. 劣化必须是**表达方式**上的劣化，不是病句：生成 prompt 里"标点照抄 + 不许病句"
   是硬约束，校验器带 `ungrammatical` 字段做审计。改了病句，变量就被污染，
   整条对照数据作废。

## 7.55 模型池变更（2026-09-18 集霸指令）

**停用 `meta/muse-spark-1.3-contributor`**，换成：

| 通道 | 模型 id | 特性 |
|---|---|---|
| 中转网关 | `z-ai/glm-5.3` | 慢（判分 ~180s/次，是重推理模型），质量稳 |
| **本机 CLI（agy）** | `agy/gemini-3.8-flash-high` | 33s/次；**必须挂 2080 代理** |
| **本机 CLI（Qoder）** | `qoder/Qwen3.8-Flash` | 12s/次，**免费档**；**不要挂代理** |
| **本机 CLI（WB 国际版）** | `wb/hy4-preview-f` | 18s/次；**WorkBuddy AI GUI 必须开着** |
| 现有 | `moonshotai/kimi-k3` / `deepseek/deepseek-v4.1-flash` / `agnes-3.0-flash` | — |

三条本机 CLI **全是单账号共享额度 → 必须串行**（`app.gateway.is_serial_model`）。
Qoder 与 WB 是 2026-09-18 集霸要求追加的，已接进 `gateway` 并登记在
`data/model_registry.json`（含"哪条要代理/哪条不能挂/哪条要 GUI"）。

⚠ **命名坑**：网关里现在只认 `z-ai/glm-5.3`；写成 `glm-5.3` 会 503
`model_not_found`（旧记录里是裸名，那是当时还有通道）。

⚠ **串行纪律**：`agy/` 前缀的模型**绝不能进并发池**（本机 CLI 单账号，并发互相挤掉）。
统一走 `app.gateway.is_serial_model()` / `split_models()`，所有跑模型的循环都已按
"并发组 + 串行组"分开执行（`controlled_corruption.py` 判分、`benchmark_run.py`）。
回归测试：`tests/test_model_pool.py`。

历史数据（含 muse 的判定与 κ）**原样保留**——它们是既成事实，不能因为换池子就重写。

## 7.6 原始文本清洗（2026-09-18，集霸要求）

**为什么要做**：集霸原话「先把原始文本的那些拼音广告啥的搞一下不然评个屁啊」。
盗版源把**生僻字换成拼音**、把**站点广告插进句子中间**，实测比例：
将夜 7.0% / 凡人 5.4% / 斗罗 1.4% / 琼明 0.26%。以前只在**抽样时排除**，
但①伪影主要出现在**上文**里（评审台显示的那一段），排除正文挡不住；
②被排除的是整块语料（5~7%），训练数据白扔。

**三步，顺序不能反**：
```bash
python scripts/clean_text.py --rules     # 规则：删站点水印/空括号/行首尾碎片（秒级，全库）
python scripts/clean_text.py --llm       # LLM：按上下文把拼音还原成汉字（分批 10 段/调用）
python scripts/clean_text.py --polish    # 规则再扫一遍（规则表会长，且不能拿原文重跑覆盖 LLM 结果）
python scripts/clean_text.py --report    # 分布
```

**四条纪律**：
1. **原文不动**：清洗结果写 `segments.text_clean`，`text` 保持原样（可审计、可重跑）。
   下游一律优先读 `text_clean`。
2. **拼音靠还原、不靠删**：`白sè` 删成 `白` 是悄悄改写原文（丢字）。规则只删水印，
   拼音交给 LLM 按上下文还原。
3. **A/B 与批注校验必须同源**：`_display_text()` 同时供端出与 `_side_texts`（偏移校验）用。
   给用户看清洗版、却拿原文校验批注偏移 → 所有划词批注的偏移全错位。
4. **上文宁可少给，不许给脏的**：`_ctx_display()` 把清洗后仍是坏文本的段直接丢掉。

**回归**：`tests/test_clean_text.py`（14 项）钉住"只删伪影不动正文"——过度清洗比不清洗更危险。

## 7.7 语料源头缺陷与两道新闸门（2026-09-18）

集霸看到的一屏里其实有**两个**独立毛病，别混为一谈：

**一、我生成的劣化版是病句（我的锅）**
ADVERB_INFLATION 初版指令是"尽量让每个动作都被副词修饰"，产出一片「地」汤：
`不应该地扫描不到` / `不禁地有种` / `大范围地地进行` / `再也没有缓缓地出现过`。
后果是**变量被污染**——读者分辨的是"通不通顺"，不是"副词多不多"，这条对照数据作废。
三道闸门：
1. `judge_verify` 把 `ungrammatical` 从"记录"升级为**硬拒**；
2. 类型定义重写：最多加 3~4 处，且"加了不通顺就一律不加"；
3. **确定性兜底** `mechanical_defect()`：变体比原文多的「地」≥5 个 → 直接拒。
   （LLM 校验器会漏判"语法上说得过去"的，计数不会漏。实测全库只有 11 条命中。）
重查全量 618 条变体，剔除病句/机械劣化 **110** 条。

**二、原始 txt 本身就缺字（语料源的锅）**
- `还有和千仞雪一起逃走的五名强。` ← 掉了「者」
- `千雪的神念突然…` ← 掉了「仞」（同段另一处又写作「千仞雪」）
- `有吴天斗罗护法` ← 「昊天」抄成「吴天」
已核对 `text` 与 `text_clean` 完全一致 → **不是清洗弄丢的**。
原有的清洗器抓不到这类：它只查拉丁/带调拼音/水印，而"掉一个汉字"字面上完全合法。

两道闸门（`scripts/source_check.py`，结果写 `segments.integrity.src_ok`）：
1. **LLM 校勘**（`--run --scope used`）：只判文本是否完好，不评文风；
   实测 265 段里判坏 **55 段（21%）**。
2. **频次自洽规则**（`KNOWN_TYPOS`）：不靠"我觉得该这么写"，靠**语料自己的频次**——
   同一作品里 `昊天斗罗` ×48 / `吴天斗罗` ×1 / `了天斗罗` ×1 → 少数派即抄写错误。
   复现：`select text from segments where text like '%X天斗罗%'`。按此又改判 **386 段**。

**铁律**：`pick_segments` / `build_batch` 一律要求 `src_ok is True`——
**没查过 = 不可用**（宁可少用，也不许把「五名强。」端给集霸）。

## 8. 当前瓶颈与下一步

### 瓶颈一：少数类样本量（**主要瓶颈**）

少数类只有 34 条（同子集 n=91 内）→ AUC 标准误 ≈ **0.083**，
距 0.5 仅 **1.4 个标准误**；阈值在 30 个折划分下 agreement 波动 **0.47–0.74**。

**样本量需求估算**（要把 AUC 0.62 稳定区别于 0.5）：

| 少数类条数 | AUC 标准误 | 距 0.5 |
|---|---|---|
| 34（现状） | 0.083 | 1.4 SE |
| 60 | 0.063 | 1.9 SE |
| **100** | 0.049 | **2.5 SE** |

→ 目标仍是把少数类从 34 补到 ~100。**但补法已断**（见下）。

### 瓶颈二：信号强度

真实最好 AUC ≈ 0.62–0.67，离可用的 0.75+ 有实质距离。

### 补样本的算力账（2026-09-16 二次修订，**mix30 之后**）

原来指望「按 score 收割」以 0.568 命中率补样本，**已被 h30 证伪**（§7⑥）。
退回无偏抽样后，命中率取决于抽哪个池：

| 抽样方式 | 每 30 题的候选胜期望 | 补 66 条少数类需要 |
|---|---|---|
| 按 score 收割（已证伪） | ~17（承诺）→ **实测 4** | 不可能 |
| B82D 旧 30 段（IPW 0.128） | ~3.8 | ~17 批 × 30 题 |
| **跨语料新段落（mix30 实测 0.444）** | **~13** | **~5 批 × 30 题** |

→ **换新段落把补样本从"不可行"拉到"可行但仍有量"**（约 150 题人工批改）。

### 下一步（**等集霸拍板**，2026-09-16 mix30 后修订）

1. **继续扩跨语料段落**（本轮唯一被数据支持的方向）：mix30 的 0.444 与旧段的 0.133
   差异显著，且候选生成器相同。**但仍要分清是"新段落"还是"新语料"**——
   同书对照 p=0.24 不显著，所以下一步应**在同一本书里加更多段落**（琼明还有大量未用段落），
   这是唯一能把"段落抽样"与"语料构成"分开的设计。
2. **承认现状、把已判的 223 条做成诚实终报**：本轮已把"基础率偏斜"这个辩护堵死——
   均衡基础率下 κ 仍只有 +0.09~+0.21。否定性结论 + 仪器已定型，零额外人工成本。
3. **换任务粒度**：不再问"整段哪边更好"（大样本 AUC ≈ 0.50，§6⑧），需先有明确假设。
4. **Gate 已改分档口径（2026-09-20 采纳，`scripts/final_report.py` 输出）**：
   旧单线 `agreement ≥ 0.70` 作废。**档一「方向识别」达标线 ≥ 0.80**——只认
   长度平衡基准（bal-v1）的读数，长度混淆集不得作为达标依据（军师 P1-3）；
   **档二「复现人类口味」无固定线**——按实测报 κ 区间并注明否定性结论
   （+0.09~+0.21、低于恒定答 human 基线，现有数据面上不可达）。

### 长期未做

- 跨 ≥4 语料（Phase 2 Gate 要求，目前只有 1 个语料）
- v2 报告实验矩阵（语料齐后才能跑）

---

### 2026-09-18 修订：瓶颈换人了

上面那套"补少数类样本"的思路**已经不是当前瓶颈**了。今天的实测把问题推到了更前面：

| 旧瓶颈（09-16） | 现状 |
|---|---|
| 评委 κ 上不去 / 少数类样本不足 | **已定性**：评委有方向性偏差（控制臂 77% 偏 AI 那版），
  再补样本也只是把 0.11 的 κ 测得 更准，不会变好 |
| 靠打分模型挑题补样本 | 已证伪（h30 收割段 3/21） |

**现在的瓶颈按优先级**：

1. **gold standard（正面标准）是什么** —— 集霸说原文本身他也不认可（24 题里 11 题"两边都不好"）。
   没有正面标准，"训出来的模型"就没有迁移目标。**这是唯一需要集霸拍板的事**，三个方向：
   - (a) 他指一批**他认可的文本**当正面集（不需要他判题，只需指认）；
   - (b) 用 **AI 输出之间的相对排序**学（不跟人类比，只学"哪些写法更不像 AI"）；
   - (c) 只学**避免哪些写法**（负面库已在做，`corrupt_negatives_v1.jsonl`，零人工成本）。
   → **在集霸拍板前，(c) 是可以独立推进的，别停在那里等。**
2. **数据量**：L 帧 469，目标几千（§42 的训练前提）。扩产流水线 `scale_corpus.py`
   **不需要任何人工**（SFT 的目标文本就是人类原文），可以放手跑。
3. **训练通道**：本机无 GPU、也没有微调管线 → §53 那六条成功标准**一条都测不了**。
   这是"要不要花钱/换机器"的决策，同样属于集霸。

## 9. 陷阱清单

### 环境（Windows + Git Bash，全部实测踩过）

| 坑 | 症状 | 正确做法 |
|---|---|---|
| `pkill -f` 杀不掉 python.exe | **静默失败**，以为重启了实际跑旧代码 | `MSYS_NO_PATHCONV=1 taskkill /F /PID <pid>`，**杀完回查 netstat** |
| `taskkill //F //PID n` | 报"无效参数/选项 - '//F'" | 同上，用 `/F` 单斜杠 + `MSYS_NO_PATHCONV=1` |
| `curl -s -o /dev/null <url>`（无 `-w`） | 返回 exit 23（写错误），把"正常"误判成"没起" | 一律用 `curl -w '%{http_code}'` 取码 |
| `curl -w '%{http_code}' ... \|\| echo 000` | 拼成 `000000`，`!= "000"` 恒真 → 脚本谎报"在运行" | 只取 `-w` 输出，用 `${c:-000}` 兜底 |
| `cd A && cmd &` 里的 `cd` | 不作用于后续命令，相对路径写错地方 | 一律**绝对路径** |
| nohup 后台进程未加 `< /dev/null` | 继承调用方管道，`bash script.sh \| tail` **挂死** | 加 `< /dev/null` |
| `ipconfig` 输出是 GBK | 被 grep 当二进制（"Binary file matches"） | 用 Python 探出口网卡取局域网 IP |
| Windows Python stdout 是 `

` | `cur=$(python -c 'print(n)')` 拿到 `"434
"`，`[ "$cur" -ge 400 ]` **语法失败→条件永假**，等待循环永不退出（2026-09-18 实测：pipeline 卡死 40 分钟） | 别用 shell 比数值；用 `python -c "import sys; sys.exit(0 if n>=400 else 1)"` 返回码 |
| 多个后台脚本写同一个日志文件 | 旧进程持 fd 按旧偏移写，新旧内容交错成乱码，人会据此误判进度 | 每次启动用**带时间戳的新日志**（serve_remote.sh 已有此约定，其他脚本照抄） |
| `heldout_eval.DB` 是写死的生产库路径 | 测试里用裸 sqlite3 连它会**静默读生产数据**（不报错、只给错数：本轮 export 的基准隔离读数恒为 0） | 新脚本一律走 `db.session()`（ORM），不要 `sqlite3.connect(he.DB)` |
| Python 字符串里写中文全角引号 `“”` | 语法错误 | 代码里用 `「」`；`test_compile_all.py` 会抓 |

### 2026-09-18 新增陷阱（全部实测踩过）

| 坑 | 症状 | 正确做法 |
|---|---|---|
| 后台进程继承 stdin 管道 | 本机 CLI 桥接（`wbai_bridge.py` 等）**一次调用都不发**、进程活着、日志无动静（实测静默卡 12 分钟） | `subprocess.run(..., stdin=subprocess.DEVNULL)`；这是 serve_remote.sh 记过的同一条坑 |
| 生成口径升版但**端出白名单**没同步 | 批次里**明明有待判的题**，取题接口说"队列已清空"，集霸以为评完了（实测 12/24 处） | 白名单用**前缀**兜（`config.is_servable_pv`），并加"当前口径必须永远端得出来"的断言 |
| 辅助实验的 `segment_ids` 只建一次 | 复用同一个实验 id 补抽帧时，新段不在列表里 → **一个调用都不发**、报"0/5" | 每次运行都更新 `e.config["segment_ids"]` |
| 抽取在工作线程各自 session 里提交 | 外层 session 数不到刚写的行（打印"抽到 0/8"而库里其实有） | 统计时**另开 session** |
| 测试/脚本用裸 `sqlite3.connect(he.DB)` | `heldout_eval.DB` 是**写死的生产库路径**，测试里会静默读生产数据 | 一律走 `db.session()`（ORM） |
| 用 `kill <pid>` 杀后台作业 | 我本想杀 WB 那条基准，**打到了 Qoder 那条的子进程**上（rc=143），它静默停了一次都不发 | 杀之前用 `ps -W \| awk '$2==<父pid>'` **列出子进程**再杀；或用带时间戳的日志文件名区分 |
| ORM 的 JSON 列已是 dict | 再 `json.loads()` 抛 TypeError，被 `except` 吞掉 → 静默返回 None / 白跑 | 统一用 `_as_dict()`（本项目已踩多次） |
| 跨文件测试耦合 | 新测试造的数据把旧测试的**绝对断言**顶掉（本轮又踩两次） | 断言写**相对值**（从库里现算期望），别写死数字 |
| 网关 `/models` 列表 | 列着已下线（minimax 410）、无额度（agnes-pro 403）的模型 | 用前先**实测一次**；登记表 `data/model_registry.json` |
| 同一批批注的折划分 | 同一道题的两个版本（人/候选）分到不同折 = 泄漏 | **按题分组**切折（`flavor_train.group_folds`） |

### 统计（全部有过真实事故）

1. **`agreement` 会骗人** —— 基础率偏斜时用 κ 与 AUC
2. **`0.5` 不是零分布基准** —— 折外挑阈值会带来乐观偏差，要用置换零分布
3. **折内 vs 折外** —— 任何"挑"的动作都要折内；同批挑阈值会虚高
4. **分层先验不能跨框搬** —— 见 §5、§7⑤
5. **位置偏差** —— 评委选 A 率 0.74–0.80；诊断代码 `_position_diagnostic`
6. **少数类主导方差** —— n=46 时少数类仅 5 条，κ 的 CI 宽到无判别力
7. **循环性** —— 若某个口径的定义来自某批数据，就不要在该批上评估它
8. **段落聚类，n 不是独立样本数**（2026-09-16 集霸发问后查清）—— 整个实验只有
   **30 个人类段落**，所以"n=183 条判定"实际是 30 段的反复对照。共享段落的判定
   （同人类文本、同上文）**不独立**：按 n 算的 CI 偏乐观，凡涉及人类文本的结论
   都要按**段级**聚类看（或至少报一句"有效样本量受 30 段上限约束"）。
   工具侧暂未做聚类校正——**报数时手动说明**。

### 数据（语料本身的问题 —— 2026-09-17 集霸截图发现）

**txt 水印伪影**：盗版 txt 会把随机汉字换成拼音，有的还夹站点水印。
| 语料 | 被污染的段落 | 举例 |
|---|---|---|
| 琼明（B82D/C812/nq50） | **0 / 130** | 精校版，干净 |
| 将夜 | 3 / 50 | `朱红sè的大门`（应是 朱红色） |
| 凡人 | 3 / 50 | `此nv`（此女）、`敬畏之sè`（之色） |
| 斗罗 | 2 / 50 | `小-说-t-xt-天.堂` |

⚠ **它只污染人类那一侧**：候选是从语义帧重建的，乱码被自然消掉（实测候选侧污染 0/50）。
所以这类题是**不公平比较**——集霸会因为乱码判候选赢，与文笔无关。
这也可能是 09-14 冒烟批里"凡人/斗罗候选碾压人类"的部分原因。

**已加永久防线**：`make_random_batch.looks_watermarked()` + 建批**默认排除**伪影段
（`--keep-dirty-text` 可关，供专门研究该效应时用）。实测已剔除 x50 里的 6 题。
排查命令：`select text from segments` 后过 `looks_watermarked`，或见 `tests/test_random_batch.py`
里的真阳性样例。

**番外区（2026-09-17 集霸定的策略：只排番外，其余照抽）**：集霸反馈"内容基本都是琼明的
番外篇、价值不大"。查证：**成因是内容类型而非位置**——琼明是色情小说，其合格段落里
性描写占比很高，番外更是清一色（他的 nq50 已判 5 题里 4 题是性描写段、位置都在正文中段）。
番外恰好也落在书末，两件事叠在一起造成"靠后"的错觉。

- **检测**：`make_random_batch.extras_start()` —— 以「番外」开头的标题段定界。
  琼明 15 个这样的段，最早 ordinal **18308 / 20533**（末 10.8%，与作者自述位置一致）；
  将夜/凡人/斗罗均无番外。
- **已默认排除**（`--keep-extras` 可关），并已从 `nq50` 撤掉 6 道**未判**的番外题
  （46 → 40 题）；`mix30` 里已判的 2 道保留（有效数据）。
- ⚠ **内容类型不解释评委失败**：按露骨度把 221 条判定劈两半重算，
  低露骨组 kimi v4 κ +0.211 / deepseek v4 +0.145，高露骨组 +0.099 / +0.142 ——
  **两边都低**，没有"叙事段就判得准"的证据。该检验有混淆（露骨度与语料高度相关、
  两组 n 不等 38 vs 80），只能否掉"题材能救评委"，不能当正面证据。

**抽样位置偏差（2026-09-17 集霸问"怎么都是靠后的内容"）**：不是随机抽的问题，是
**"剩下的池子"偏移**——之前用掉的段落（B82D/BA19/C812 共 54 段）几乎没碰书的最后 10%
（末位十分位仅 **1%**，预期 10%），于是剩下的可抽池在末尾富集，新抽的样本跟着偏后
（nq50：末两位十分位各 **14%**）。单看统计量未达显著（n=50 时每层噪声 ±4%），但方向真实。
**已加 `--stratify-position`**：按全书位置的十分位轮流取段，覆盖全书成为构造性保证。
实测同一池子 n=20：纯随机留下**整整 3 个十分位一条都没有**；分层则每层恰好 2 条。
⚠ **旧批次（nq50/x50/mix30）仍是纯随机样本**——它们无偏，只是没做均衡；要均衡得重建。

### 代码

1. **`verdict` 双形态** —— 统一走 `scripts/heldout_eval.py::_as_dict()`
2. **`reasons` 是 JSON 文本** —— 裸 sqlite 拿到的是 str，必须 `json.loads`
3. **`review_items.subject_id` 指向 candidate id**（不是 segment id）
4. **新增/修改 prompt 必须登记进 `PROMPT_VARIANTS`**，否则 `heldout_eval` 看不到
5. **批次名必须单实验** —— `review/next` 按 `experiment_id` 过滤

---

## 10. 数据字典

### `candidates`

| 列 | 说明 |
|---|---|
| `id` | `CND-xxxxxxxx` |
| `segment_id` | 对应的 `segments.id`（人类原文段） |
| `text` | 重建出的候选文本 |
| `prompt_version` | 只信白名单 `BLIND_REVIEW_PROMPT_VERSIONS`（`reconstruct_v1` / `recon_ctx_v1`） |
| `status` | `ok` / `failed` |

### `review_items`

| 列 | 说明 |
|---|---|
| `subject_id` | → `candidates.id` |
| `status` | `pending` / `done` |
| `reasons` | **JSON 数组**（文本）。含 `batch_<tag>`、`stratum:<名>`、`w:<入样概率>`、`score:<分>` |
| `human_verdict` | **JSON**。含 `winner_resolved`、`annotations[]`（噪点批注） |
| `reviewed_at` | 判定时间（用于"建批前/后"过滤） |

### `judge_runs`

| 列 | 说明 |
|---|---|
| `subject_id` | → `candidates.id` |
| `judge_kind` | `preference` / `naturalness` / `semantic` / `adversarial` |
| `prompt_version` | 口径标识（`judge_preference_v3` / `_v4` / `_heldout` / `_rev`） |
| `verdict` | **JSON，双形态**（ORM 已解析为 dict；裸 sqlite 是 TEXT） |
| `abstain` | 评委拒绝表态（**不进 agreement 分母**） |

### `residuals_det`

| 列 | 说明 |
|---|---|
| `candidate_id` | → `candidates.id` |
| `deltas` | **JSON**，32 维确定性指标之差（候选 − 人类） |

**打分模型用的 4 个核心特征**（跨折稳定入选）：
`n_sentences`、`long_sent_ratio`、`punct_！_per_k`、`sent_len_mean`

---

## 11. 术语表

| 术语 | 含义 |
|---|---|
| **候选** | LLM 从 SemanticFrame 重建出的文本（vs 人类原文） |
| **候选胜** | 集霸判定"候选写得比人类好"—— 本项目的**少数类** |
| **口径 / variant** | 一套评委 prompt（v3 / v4 / defect） |
| **留出集** | 未参与 rubric 推导的题（r25 真留出 16 题；s30 30 题） |
| **优先队列** | 按"评委把人类误判为 AI"等条件过采样的难例队列 |
| **自然池** | 随机抽样，未按分或按难度筛过 |
| **收割 / 锚点** | 收割=按分挑高分题（提命中率）；锚点=池内随机抽（自校准） |
| **翻转率** | 正反两序给出相反内容方向的比例。纯内容=0.00 / 纯位置=1.00 / 随机≈2p(1−p) |
| **IPW** | 逆概率加权，用入样概率 `w` 还原总体 κ |

---

## 12. 交接时的遗留事项

### ⭐ 2026-09-18 交接时的待办（按优先级）

| # | 事项 | 谁来做 | 说明 |
|---|---|---|---|
| 1 | **定正面标准**（gold standard） | **集霸拍板** | 三条路见 §8 修订版；他未定之前，(c) 负面库路线可独立推进 |
| 2 | 语料扩产继续推 | 接手 agent | `scale_corpus.py --run N`，**零人工**；目标 L 帧几千 |
| 3 | corr24 的 24 条裁定灌进训练导出 | 接手 agent | 已加 `--strict`；目前只有 1 对被他背书 → 数据规模取决于第 1 项 |
| 4 | 训练通道（本机无 GPU / 无微调管线） | **集霸决定** | §53 六条成功标准**一条都测不了**，卡在这里 |
| 5 | 基准扩到其余 11 项子基准 | 接手 agent | 【T5 部分完成 2026-09-19】corruption_type（19 集合）+ naturalness_pair（nat-v1）已建；其余待解锁（human_vs_ai 缺基准段重建候选等，见 `benchmark_build.py` docstring 状态表与 `docs/benchmark-subs-20260919.md`） |
| 6 | Experiment Engine（任务 12） | 接手 agent | 纯工程，`experiments` 表在但无引擎 |
| 7 | 源文本换更干净的版本 | **集霸决定** | 斗罗 txt 系统性缺字（`千雪`应为`千仞雪` 347 段、`吴天`应为`昊天`） |
| 8 | 评审台令牌轮换 | **集霸决定** | 曾在日志里明文出现过，我已脱敏但**未轮换**（轮换会让他手上的链接失效） |

### 集霸当前的评审入口

`<站点>/?t=<令牌>&batch=corr24`（24 题**全部已判完**）。
下一批要请他判什么，取决于第 1 项（正面标准）怎么定。


### 🎯 mix30 已判完（30/30，2026-09-16）—— 结论见 §1

| 项 | 值 |
|---|---|
| 批次标签 | `mix30`（入口 `?batch=mix30`） |
| 构成 | 凡人(EE18) 10 / 将夜(FF7B) 5 / 琼明(C812) 15 |
| 状态 | **30/30 已判**；评委分 **120/120 `status=ok`**（预跑 2 轮补齐） |
| 子标签 | 各题另带 `batch_fr10` / `batch_jy5` / `batch_qm15`，语料构成可追溯 |
| 结果 | 候选胜 **12/27 = 0.444**（vs B82D 旧段 0.133）；评委 κ 仍只有 +0.09~+0.21 —— 见 §1 |
| 数据质量 | 1 条因**我重启服务**导致 A/B 映射丢失（存成原始 `B`），已排除；这是我的操作造成的 |

**为「一起出」新增的跨实验支持（此前一批只能属于一个实验）**：
- 后端：新增 `GET /review/next?batch=X`（不限实验），`_pick_next()` 抽取；原路径不动。
- 前端：`batchMulti` —— `/review/batch/{batch}` 的 `experiments` 多于一个时走新端点。
- 分析端：`run_one` 按**候选自己的实验**写 `JudgeRun`；`load_items`/`collect`/`noise_report`
  在跨实验批上**不加实验过滤**（池化），逐语料用 `--exp`。
- ⚠ 池化时**加权 κ 无意义**（三个语料入样概率不同），工具现在会显式警告。

**🩹 顺手修掉的取题缺陷（会在集霸眼皮底下静默吞题）**：`review_next` 原本在
**pending 列表**上取模轮换，列表随判定变短 → 游标后面的题被判掉时，游标处的题被整段跳过。
实测：mix30 判了 18 题后，**5 道将夜一次都没被端出过**。已改为游标走**整批固定列表**
（含已判）+ 跳过已判 = 每道题恰好出一次。

### 新改的工具（2026-09-17 下半场：判题成本）

| 改动 | 说明 |
|---|---|
| **上文默认只给近段** | `_serve_payload(..., ctx_scope="near")`；`?ctx=near\|scene` 切换。实测单题要读 **≈4100 字，其中上文 3970 字（97%）**，A/B 两段仅 137 字 → near 之后降到 **~200 字**。响应带 `context`（按 scope）+ `context_full`（恒为全场景）供前端展开 |
| **UI 上下文档位** | 默认显示近段，按钮「看全场景」一键切；`ctx_mode` 记成 `near1/scene58` 供分析区分 |
| ⚠ **纪律** | §6⑥：评委与用户必须吃同一份上下文 → **新采集轮次要用 `?ctx=near` 跑评委**（现有 254 条判定是 `scene` 口径）|
| 🩹 **修复：跨实验取题被 UI 重写覆盖** | `batchMulti` 逻辑在 2026-09-16 的 UI 重写中被删掉，导致跨语料批走单实验路径 → **第一个实验的题判完就误报「本批已全部判定」**（x50 实测：斗罗 3 题判完即显示完成，实际还有 39 题）。已补回并加回归测试（`test_review_batch.py`）|

### 其它遗留

- [x] 批次 `h30`、`mix30` **均已判完**；`r50` 停在 9/41（旧材料，不必回头续判）
- [ ] **不要再建收割批**（`select_harvest_batch.py`）—— 两次失败，且帧检查现已硬拦
- [~] **安全**：旧令牌曾明文留在 4 个日志文件里（已抹成 `<TOKEN-REDACTED>`），
      且多次被粘进对话 → 建议**轮换令牌**（需重启，隧道 URL 也会变）。**集霸未拍板，未轮换。**
- [ ] 服务在跑；**2026-09-16 重启过两次**（新端点 + 取题修复；隧道 URL 与令牌均未变）。
      ⚠ 进程内 A/B 映射会随重启清空 → 已端出未判的题提交会 409，**刷新页面即可**
      （mix30 就有 1 条因此变成不可用的原始 `B`）

### 新改的工具（2026-09-16）

| 脚本 | 新增能力 |
|---|---|
| `app/api.py` | `GET /review/next?batch=`（跨实验取题）；`_pick_next()` 改为固定列表轮换（不吞题） |
| `app/static/index.html` | `batchMulti`：跨实验批走新端点 |
| `make_random_batch.py` | `--pool {all,unjudged}`、`--by {candidate,segment}`、`--min-human-chars` |
| `heldout_eval.py` | `--exp`；混合批自动池化 + 加权 κ 警告；空批次明确报错；末尾报覆盖段落数 |
| `anchor_check.py` | `--exp`；主检验改为收割段承诺（见 §3） |
| `prerun_batch_judges.py` | `--exp`；混合批按候选各自实验写入；覆盖回读断言 |
| `noise_report.py` | 同上一族修复：原写死 B82D，跨语料批会静默报「已判 0 条」 |
| `select_harvest_batch.py` | 帧检查升级为**硬 Gate**；建批承诺落盘 |

### 想快速建立全局认知，按这个顺序读

1. `docs/phase1.5-plan.md` §①–⑫（尤其 ⑥⑦⑧⑨ 的结论、⑩ 的仪器缺口、⑪ h30 终判、⑫ 转向跨语料）
2. 本文档 §1（尤其"只有 30 个人类段落"）§5 §6 §7（尤其 ⑥）§8 §12
3. `scripts/heldout_eval.py` 的 docstring
4. `scripts/anchor_check.py` 的 docstring（讲了两次「自检放过真问题」）
5. `scripts/select_harvest_batch.py` 的 docstring（讲了 s30 的教训与修正）
6. `tests/test_position_diagnostic.py` 的注释（讲了"静默 None"这一族故障）

---

## 附：本项目最值得保留的一条经验

本轮（2026-09-16）做了 9 项分析，其中 **3 次出现"恰好越过 Gate 线"或"大幅突破"的数字，
3 次都被自己否掉**：

| 好数字 | 证伪方式 | 真相 |
|---|---|---|
| 挑候选比例恰好 0.500 | 查原始 A/B 分布 | 位置偏差 0.74–0.80 |
| 缺陷口径 AUC 0.897 | 换到未标注集 | 0.569（词表过拟合） |
| 核心4维 agreement 0.725 | 折外挑阈值 | 0.673，且波动 0.47–0.74 |

**所以：任何显著优于此前瓶颈的结果，先设计一个"最可能否掉它"的检验，再决定是否采信。**

这不是悲观，是省时间。三次否掉自己的成本各只有一轮；而任何一个被采信的错误结论，
都会让后续数轮建立在错误前提上——**那比"没发现"更糟**。
