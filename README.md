# Language Genome — SemanticFrame Calibration Lab

> 下一阶段建设以 [知识化调整方案（2026-09-20）](docs/Language_Genome_知识化调整方案_20260920.md) 为入口：保留实验基础，推进带条件与证据的表达知识及 Runtime 自动消费。**定位更新（2026-09-21，K1-A/K1-B 落地）**：v2 契约层已进库——来源登记（work_sources/authors/genres + 一键对账 verify_work_registry.py）与五核心结构（expression_strategies_v2 / strategy_instances / strategy_conditions / strategy_stats / knowledge_links）+ 知识包版本协商契约（app/knowledge.py）；v1 八条聚类策略已按保守口径转为**待验证假设**（success_rate 不复制不重解释，scripts/migrate_strategies_v2.py）。实例抽取与自动消费（K2/K3）依赖外部模型通道——当前卡 deepseek 402 资金墙，待拍板（docs/HANDOVER.md）。下文保留现有实验台的说明和命令。

> 不是完整系统，是**第一个关键科学实验**：
> 证明"语义可以被压缩成一个既足够约束意义、又不过度约束表达的中间表示"。

## 现在要答的问题

一个 SemanticFrame 应该保留多少信息，才能：

| 指标 | 太低 | 太高 |
|---|---|---|
| Semantic Sufficiency（语义充分） | 骨架太抽象，各家重建的"意思"都不一样 | 越高越好 |
| Expression Freedom（表达自由） | 写法被骨架钉死 | 越高越好 |
| Source Leakage（原文泄漏） | 越低越好 | Frame 变成 paraphrase |
| Adversarial Reconstruction | 盲眼强模型猜不回原句 | 能猜回原文=泄漏 |

甜区 = **语义约束很强 + 语言约束很弱**。

## 架构（刻意很薄）

```
inbox/fixtures → corpus(works/segments)
      ↓ 1~10 句切分，不截句中
Frame-S / M / L（双模型独立抽取，Pydantic 校验 + 一次修复重试）
      ↓
泄漏四层半：char6 / word3 / rare-idf / adversarial(强模型盲还源) (+embedding 预留)
      ↓
多模型重建（模型 × 温度 × 采样，候选匿名 X####）
      ↓
Residual：① 确定性指标差（Python 直接算，零 token）
          ② 语义残差（LLM：missing/added/contradicted/越界/潜台词）
      ↓
三 Judge：Semantic / Naturalness(盲评, Human 也盲评) / Adversarial（匿名猜 AI，自爆偏差）
      ↓
Active Human Review 队列（按信息量排序，不是随机 1%）
      ↓
Calibration Report v1 → data/reports/<EXP>/
```

工程原则：
- SQLite 起步（WAL），`LG_DATABASE_URL` 一行切 Postgres
- Job 表跑 stage 链（pending/running/completed/failed/retry），幂等可重入
- 全部 prompt 版本化入库；全部 LLM 调用记 token/延迟/状态
- 中转站单价未知 → cost 一律 None，只记 token（沿用 novel-distiller 约定）

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env   # 填 LG_GATEWAY_BASE_URL / LG_GATEWAY_API_KEY

# ① 不联网的全流程冒烟（内置两篇原创测试文本）
python scripts/run_calibration.py --mock --use-fixtures --segments 8

# ② 把自己小说扔 data/corpus_inbox/ 然后真跑
python scripts/run_calibration.py --segments 60

# ②b 直接按绝对路径导入一本书、只从它采样（评委模型避开重建家族）
python scripts/run_calibration.py --file "F:\小说\gem\绿\长篇\【精校】琼明神女录.txt" \
    --work 琼明 --segments 30 \
    --models "deepseek-v4.1-flash,z-ai/glm-5.3" \
    --judge-models "moonshotai/kimi-k3" --samples 1 --concurrency 6

# ③ Web 控制台（实验/语料/盲评/用量 四页签，无构建依赖）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787   # → http://127.0.0.1:8787/
```

## 已知边界（v1 明确不做）

- 双抽**只并存不合并**：主抽取器结果进下游，次抽取器留作对照；自动 Semantic Merge 后置
- 判价未知，只记 token
- jieba 可选；无则字级 bigram 兜底
- Human Anchor 语料→自己放进 `data/corpus_inbox/`；distiller 适配器只读过闸，
  但那里现在只有验收测试书，**不算 Human Anchor**
- 真大规模跑之前记得看报告第七节 token 账单再扩量

## 关键文件

- `app/frames_schema.py`  Frame S/M/L 契约（改字段=改这个文件+prompt 版本）
- `app/leakage.py`        泄漏四层半
- `app/metrics_det.py`    确定性残差指标（AI 味词表在里面）
- `app/report.py`         报告聚合口径
- `docs/calibration-design.md`  设计决议记录
- `docs/adversarial-review-2026-09-11.md`  代码级对抗性审查（7×P0 修复 + 回归测试）
