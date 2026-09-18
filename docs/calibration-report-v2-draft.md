# SemanticFrame Calibration Report v2 — Cross-Corpus Replication（草稿 v0.3）

- 状态：**数据生成中**（四语料 B0/C/D 候选管线后台运行）
- 前置报告：`docs/calibration-report-v1.md`（Corpus-01 定标）
- 语料矩阵：

| Corpus | 作品 | 锚角色 | v2 可用段 | 合格率 |
|---|---|---|---|---|
| 01 | 琼明神女录 | 对照组（糙而连续） | 20,533 | 91.2% |
| 02 | 将夜（猫腻） | 质量锚 | 68,626 | 86.8% |
| 03 | 凡人修仙传（忘语） | 叙事效率锚 | 154,809 | 93.7% |
| 04 | 斗罗大陆（唐家三少） | 目标风格锚 | 37,863 | 86.4% |

---

## 一、可冻结结论（跨实验已复现，不再讨论）

1. SemanticFrame 路线可行；**L = provisional sweet spot**（Corpus-01：保真 0.923 / 自由 0.953 / raw 泄漏 p90 0.623 / **entity-norm p90 0.590**）；
2. **孤立段评审有严重仪器偏差**（上下文消融：human 1/10 → 7/10）——评审协议必须带 ≥prev1 上文；
3. 单一 Naturalness 无效；七维量表（implicitness/voice 分离度最高，over_polish 反向轴有效）；
4. Human ≠ Quality：锚体系已拆 local/contextual/style 三维；
5. **LLM Judge 不能当偏好真理源**：Human Prediction Matrix——**修正后** kimi 0.480（n=98）、
   deepseek 0.564（n=39），均抛硬币水平；0.70 Gate 未达，adversarial-vs-preference 口径错位已记录。
   ⚠ 2026-09-14 口径勘误：`scripts/judge_matrix.py` 原实现 `judge_says_human_won = (guess_hit_ai is False)`
   方向写反（评委认出 AI 却被记为"站 candidate"），此前流传的 kimi 0.462 / deepseek 0.436 系该错误口径产物；
   修正后 kimi 0.480 / deepseek 0.564。已加回归测试锁死方向（`tests/test_judge_matrix.py`）。
   **结论不变**：两种口径都在 0.5 附近，Judge Gate 仍未过；但 deepseek 由"最差"变为"最好"，排名翻转。
6. Residual 分层成立（确定性引擎零 LLM + 语义残差 LLM）。

## 二、2×2 因子首读（EXP-0913-BA19，琼明 v2，n=10，窗口评委盲评）

偏好排序（全部带上下文）：**C(Context+Frame) 7 > B0(Frame-only) 6 > Human 4 > D(Context-only) 2**。

- **Frame 的核心价值 = 语义锚定/防漂移**：D 的败局全是语义劫持（村民乱入/误读对话/插入起源/替角色答题/违背力竭）；
- D 只在"纯文笔局"赢 → **Adaptive Frame 假说获得首个实证**：剧情段必须 Frame，氛围段可放宽；
- `Previous Prose + SemanticFrame → Next Prose` 为第一代 Writer 接口首选。
- 口径：n=10 冒烟；跨语料复现 = 第三节数据。

## 三、Entity-normalized Leakage（EXP-0911-B82D，84 frames，1.8 实体/帧）

| 粒度 | raw p50/p90 | norm p50/p90 | Gate(0.65) |
|---|---|---|---|
| S | 0.317/0.467 | 0.291/0.467 | ✅ |
| M | 0.310/0.509 | 0.312/0.571 | ✅ |
| **L** | 0.447/0.623 | **0.391/0.590** | ✅ |

**归因修正**：实体回填只解释泄漏 ~12%（p50），大头是 Frame 描述性内容的可复述度
→ 防泄漏优化方向 = Frame 抽取的措辞约束，不是摘实体。

## 四、跨语料复现（数据生成中）

四实验：琼明 `EXP-0914-C812` / 将夜 `EXP-0914-FF7B` / 凡人 `EXP-0914-EE18` / 斗罗 `EXP-0914-6DD3`
（各 50 合格 v2 段，S/M/L 双抽取器）。

### 4.1 抽取层（已完，gap-fill 后终值待更新）

| Corpus | 段合格率 | S 可用 | M 可用（gap-fill 前） | L 可用 | 抽取合计可用 |
|---|---|---|---|---|---|
| 琼明 C812 | 91.2% | 50/50 | 45+2f（95.7%） | 48+1 | **95.0%**（ok=190 repaired=95 failed=15，gap-fill 已完） |
| 将夜 FF7B | 86.8% | 50/50 | 37+13f（74.0%） | 50/50 | gap-fill 中 |
| 凡人 EE18 | 93.7% | 50/50 | 43+7f（86.0%） | 50/50 | 待 gap-fill |
| 斗罗 6DD3 | 86.4% | 50/50 | 46+4f（92.0%） | 50/50 | 待 gap-fill |

**发现（M-gap 跨语料复现 + 根因闭环，2026-09-14）**：S 与 L 可用率四语料全部 ≈100%，M 在所有语料都是最脆粒度
（74%–96%）。M 缺口是**粒度级问题**（抽取契约/粒度定义），不是语料特异问题 →
与 Phase 1.5 指令"M-gap 要查根因而非重试"一致：重试只修尾部，根因在 M 契约。

**M-gap 根因（四语料 74 个失败 M/L 实测，全部可归因）**：
1. **V1 抽取模板缺 M 的「输出 schema」JSON 块**（S/L 都有）→ 模型自造字段名
   facts[].`fact`(43 行)/`content`(14 行)，内容本身合格；
2. **prompt 教模型「拿不准就留 null」**，但 pydantic 对显式 null 不走 default_factory
   → `does_not_know: null` 等整行被拒；
3. **字符串冒充列表**（分号拼接 `reader_should_infer` 等，22 行）；
4. 单键外壳 `{"frame_m": {...}}`（4 行）。
修复三层：`validate_frame` 前置 `normalize_frame_payload` 契约宽容层（只收实测别名，
有损改写不做）；抽取 prompt 升 **extract_v2**（M 补 schema 块 + 数组/单角色/字段名三钉）；
存量救活脚本 `scripts/rescue_frames.py`（**48 帧确定性翻 repaired，零 API**）。
残余 11 帧不救：raw 截断（9，待 gap-fill/V2 重抽）+ character_state 按角色拆数组
（语义有损，V2 预防）。回归测试 7 项入 pytest（38 全绿）。

斗罗 L-frame 抽查（用户指定目标风格）：facts 带 certainty/source、must_not_state、
expression_constraints 全部成立且贴合原文 → 抽取器跨域泛化成立。

### 4.2 候选与盲评（2026-09-14 早更新）

- 后台管线 `scripts/corpus_matrix_full.py`：每语料 ~50 L-frames × 2 模型 × {B0,C,D} ≈ 300 候选；
- 盲评构建器 `scripts/factorial_blind_multicorpus.py` 已就绪并通过冒烟
  （ba19 基准：36 项结构/映射/判定翻译全部验证通过；pytest 31 项全绿）。
- **生成进度（09-14 08:40，计 300/1200 条 ok）**：C812 154 / EE18 57 / FF7B 53 / 6DD3 39。
  并行方案：`scripts/gen_cand_one.py`（单实验切片驱动，幂等键 (eid,frame,model,pv) 逐条重查，
  anon_label 加 run_tag 防跨进程撞号）×4 进程 + 主串行进程相向而行；WAL 并发写零锁死。
  预计余额度窗口内再产出 ~200-300 条，剩余缺口由主管线继续。
- **entity-norm 泄漏（§六，琼明 90 primary frame 已跑）**：char6_norm mean 0.0017（max 0.042）、
  word3_norm mean 0.0126、adversarial_entity mean 0.341（raw 口径对照：char6 0.0021 /
  word3 0.0053 / adversarial 0.30，n=116）。双口径结论一致（provisional，琼明单语料）：
  Frame 逐字泄漏≈0，对抗性复述约 1/3，entity 归一化不改变量级。

### 4.3 窗口评委 2×2 读数（provisional，冒烟口径，评委=会话内窗口评委）

两批同日判定，判定文件与 .map 隔离（评委判完前未读 map），结果落 `data/blind/factorial_*_result.json`：

1. **BA19 扩批**（琼明旧实验 B82D，n=29 段 × {HB,HC,HD,CD} = 116 项）：
   HB B0 24:5 ｜ HC C 26:3 ｜ HD D 17:12 ｜ CD C15:D14。
   模型拆分：deepseek 侧 human 8:43，muse 侧 human 12:24。
   与 09-13 n=10 首读排序（C>B0>Human>D）方向一致。
2. **四语料冒烟批**（琼明8/将夜7/凡人8/斗罗6 段 × 4 对比 = 116 项）：
   汇总 HB 候选 21:8 ｜ HC 21:8 ｜ HD D 15:14 ｜ **CD D 20:9**。
   分语料：HB 候选:human = 琼明 5:3、将夜 3:4、凡人 7:1、斗罗 6:0；
   CD = 琼明 4:4、将夜 2:5、凡人 0:8、斗罗 3:3。

读数（全部 provisional：单评委、n=6-8/语料、冒烟不外推）：

- **候选 vs Human 的差距随语料质量线分层**：将夜/琼明 human 对候选有来有回，
  凡人/斗罗 human 被碾压 → 与"Human≠Quality、质量锚语料 Human 更耐打"假设一致，
  支持 Corpus 按功能角色（质量锚/效率锚/风格锚）分工的既有设计。
- **D（context-only）对 human 勉强过半**（15:14），琼明上反输 human（3:5）→ D 最弱复现。
- **CD 汇总 D 20:9 反超 C**，与 EXP-0913 首读"Frame 防漂移"方向相反（BA19 批则打平 15:14）。
  Frame 相对 context-only 的净效应在冒烟批**未被复现**。两种候选解释：
  ① 上下文已富（prev2 完整场景）时 Frame 约束可能压低文采；② 冒烟批各语料 C/D 文本
  模型配比不均。**在扩到 30-50 段/语料且用户双判复核前，不更新任何 Frame 净效应结论。**
- 仪器发现：① 拒答文本混入候选（BA19 HD 侧 1 例、琼明 2 例，模型对胁迫/未成年情节拒绝续写），
  建议 refusal 检测单独归类，不与正常文本混算胜率；② H 侧 txt 水印伪影
  （拼音替代字 róu/sè、OCR 错字如"车得越来越浓郁"）系统性拉低 human 侧观感，
  判定已按"内容优先于错字"口径处理，但需在跨语料对比中记一笔。

待填栏位：
- [x] 各语料 2×2 偏好读数（冒烟版已填，见 4.3；30-50 段扩批与用户双判复核待做）
- [ ] 各语料 S/M/L 保真率（L 是否仍最优）
- [ ] 各语料表达自由度
- [ ] 各语料 entity-norm 泄漏 p90
- [ ] 跨语料稳定 det 残差指纹（哪些 AI 偏移跨模型跨作品恒定）

## 五、Judge 体系（待用户补充）

- 双判 10 对**作废**（原定 GLM 对照；2026-09-14 glm 系模型下架，批次 `batch_dual10` 已退役）；
  preference-task 口径的 Judge 重测待做；
- 当前盲评批次改为 `batch_r15`（15 题，单实验 EXP-0911-B82D，构成 9 upset / 5 disagreement / 1 novel）；
- Matrix 出全前，任何 LLM Judge 不得自动晋升偏好真理源。

## 六、Phase 2 Gate 核验表

| 条件 | 现状 |
|---|---|
| 跨 ≥4 类语料 | ✅ 四语料就位 |
| Semantic Fidelity ≥0.90 | Corpus-01 L=0.923 ✅，跨语料待验 |
| Expression Freedom ≥0.90 | Corpus-01 L=0.953 ✅，跨语料待验 |
| Entity-norm 泄漏 p90 <0.65 | Corpus-01 L=0.590 ✅，跨语料待验 |
| Segment usable ≥0.80 | v2 切分 86-94% ✅ |
| 某 Judge 与用户 agreement ≥0.70 | ❌ 且**该 Gate 表述本身有缺陷**：应改看 κ。修掉"Judge 未供上下文"的仪器错误后（v3），kimi agreement 0.505 / κ +0.097、deepseek 0.453 / +0.012 —— 响应偏差由 0.91–0.93 降到 0.68–0.71，但**信号依然很弱**。建议 Gate 改为 **κ ≥0.40** + 随机抽样批（详见 phase1.5-plan.md「仪器事故：Judge 空手判」） |
| Context protocol 固定 | ✅（prev2+prev1 起步，阶梯已定义） |
| Quality Anchor 体系跑通 | ✅（local/contextual/style 三维） |
| Benchmark near-dup 清理 | ✅（四语料各 100-200 段隔离） |

**结论（草稿）**：Gate 八项中六项已过或达标，Judge 项未过（等双判补充 + 口径修正），
跨语料复现项数据生成中。两项齐 → 正式进入 Phase 2（Expression Genome Discovery）。
