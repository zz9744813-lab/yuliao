# Calibration Lab 设计决议（2026-09-11）

## 为什么先做这个

Language Genome 所有下游几千万条数据都建立在 SemanticFrame 上。
如果 Frame 把"语义"和"表达"分不开，后面全是系统性污染。
所以第一刀不是搭平台，是回答一个可证伪的问题：

> **Frame 能不能同时做到 （a) 语义还原度高  (b) 表达自由度高  (c) 原文泄漏低？**

## 已锁决议

### R1. 三粒度并行竞争，不预设赢家
Frame-S（粗）/ Frame-M（中）/ Frame-L（细）。同一段各抽一份，交给实验投票。

### R2. 泄漏必须四层半，不允许 LLM 自评独断
char6 / word3 / rare-idf 是确定性底线（零成本、可复现）；
**对抗还原是裁决**：盲眼强模型只看 Frame 能不能复原原句。
embedding 相似预留但当前网关不支持，先记 skipped。

### R3. 语义充分性走"原子命题"，不走"像不像"
Human 原文 → 命题列表（fact/infer/atmosphere）。
判 candidate：逐命题 hit / missing / contradicted / altered。
禁止直接让 LLM 打"语义 92 分"这种不可复核的数。

### R4. 表达自由度有确定口径
同 Frame 的多候选之间算 1 − Jaccard(char5) 平均距离 + 长度离散度。
**fidelity↑ + freedom↑ 同时成立，Frame 才算站住。**

### R5. Human 也进盲评
naturalness judge 对 Human 原文和 candidate 一视同仁。
adversarial 对 Human/AI 做匿名 A/B（A/B 位随机，身份只进库）。
"Human 被判得像 AI"是最有信息量的事件，单独记 human_upset。

### R6. 人只看高信息量样本
review 队列优先级 = judge 分歧 + human_upset + 低置信 + 指标新奇度。
不随机抽 1%。

### R7. 引擎最少
SQLite(WAL) + Python 线程池 + DB job 表。
Temporal / Redis / Postgres 都不装；`LG_DATABASE_URL` 留好切换口。
任务公约：幂等（每 stage 跳过已存在产物），中断续跑不重复花钱。

### R8. 双抽不合并
那两个抽取器出的 Frame 都存，只先用主抽取器进下游。
自动语义合并是真问题，留给下一个实验（先有这个实验的数据再谈合并）。

### R9. 成本口径
中转站单价未知 → 只记 token，不编美元/人民币。报告第七节按用途汇总 token。

## 参数默认值的理由

| 参数 | 值 | 理由 |
|---|---|---|
| temperatures | 0.5 / 0.9 | 一稳一放，够看倾向；四个温度档留给规模实验 |
| samples_per_pair | 2 | 同温复测，看采样方差 |
| adversarial_k | 8 | 取 max(sim) 的稳健性下限；16 更稳但贵一倍 |
| judge_models | recon[0]（起步） | 第一刀求通路；正式实验须换家族 |

## 拿到报告后怎么读

1. 先看第二节泄漏四层：**adversarial p90 是唯一硬否决线**（>0.65 的粒度直接出局）
2. 再看第三节保真率：语义 hit_rate < 0.8 的粒度 = 骨架丢了意思
3. 再看第四节自由度：pairwise_dist 高还伴随 fidelity 高 = 甜区
4. 第六节 Δ 指标是"AI 味"的定量种子：哪个维度全体模型整齐偏移，哪个维度就是残留高发区

## 明确不做（这次）

- 前端（看库+报告 md 就够）
- Reward Model 训练（数据不到）
- Benchmark Hidden Set（先让 Frame 成立）
- Postgres 实际落库（凭据未在手上，SQLite WAL 先顶着）
