# 本机微调最小闭环（FINETUNE_LOCAL）

> Runtime 后置项「微调」。分支 `task/finetune-local`，基线 `75ac752`。
> 逐字真跑证据见 `DELIVERY_finetune_local.md`。

## 1. 这件东西是什么，不是什么

`docs/HANDOVER.md` §8 记的是「本机无 GPU、也没有微调管线 ⇒ §53 六条成功标准
一条都测不了」。**现在本机有卡**（RTX 3050 Laptop, 4096 MiB），但可用显存只有
2~3 GiB，且常被别的进程占走一部分——**7B 级 QLoRA 在本机跑不了**，那条路仍然
归 `docs/kaggle-qlora-notebook.md`。

所以本件只做一件事：**把「有管线、能真训练、能出前后对比」做成可复现的真跑证据**，
并把规模上限写死在代码和日志里（小基座 ≤1.5B、样本 ≤2000 条、步数 ≤300）。

**它不是 §53 六条标准的证据**，也不承诺质量提升：样本量太小、基座太小、步数太少，
结论只能当「管线真跑通了」的证据，不能当「模型写小说变好了」的证据。

## 2. 两个脚本

### `scripts/finetune_min.py` —— 抽数据 + 真训练（LoRA）

* **数据**（只读 `language_genome.db`，不现编）：三种「风格化指令对」，
  目标文本一律是人类原文：
  * `frame_to_text`：`frames.payload` 要点（事件/意图/读者感受）→ 段原文；
  * `rewrite_deai`：AI 劣化版（`controlled_corruptions.text`）+ 病灶说明 → 同段人类原文；
  * `ai_to_human`：`candidates.text`（本项目 AI 侧真实产物）→ 人类原文，
    用二元组 Jaccard `< MIN_OVERLAP(0.30)` 自加一道内容重合闸（候选无 fact 标记）。
  入池闸门照抄仓库既有纪律：`integrity.src_ok` 必须是 JSON 布尔 `true`（fail-closed，
  先过 `app.segment_integrity.canonical_json` 还原紧凑码）、`segments.role=='benchmark'`
  排除、基准文本按忽略空白哈希排除、txt 水印伪影排除、目标长度 `[20, 400]`。
  尾部 `--eval-size` 条划成 held-out，**不参与训练**，训练前后各评一次 loss。
* **训练**：手写优化循环（不用 `Trainer`，逐 optimizer step 精确记 loss），
  `peft` LoRA（r/alpha/dropout 可配），真·梯度累积（一个 step 内跑 `grad_accum` 份
  micro-batch、各自 `/grad_accum` 反传到同一份 `.grad`、只 step 一次），
  梯度检查点、micro-batch=1、固定种子、warmup+线性衰减、grad-clip。
  device/dtype 走 `resolve_device_dtype`：cuda 有 bf16 → bf16（免 GradScaler），
  否则 fp16；CPU → fp32（且 `validate_config` 直接拒 CPU+fp16）。
* **产物**（全落 F:/G:，`guard_path` 拒 C 盘、拒落在检出内）：`adapter/`
  （LoRA 权重 + tokenizer）、`train_log.json`（loss 序列/首末/前后 10 步均值、
  held-out 前后 loss、步数、耗时、峰值显存、环境快照）、`train_pairs.jsonl`、
  `run_env.json`。
* **`--dry-run`** 全程不 import torch，只验数据口径与配置——没装训练栈的 venv 也跑得动
  （验收用例靠这条）。

### `scripts/finetune_compare.py` —— base vs tuned 同提示词逐条对照

「训完到底变没变」不靠看 loss。口径钉死成**同提示词、同解码参数、同随机种子**：

* 同一个 `PeftModel` 实例、同一个 prompt 张量，先在 `disable_adapter()` 里跑 base、
  再摘掉屏蔽跑 tuned——两侧**只差 LoRA 权重**，连基座浮点抖动都排除；
* 解码固定贪心：`do_sample=False`、`num_beams=1`、`temperature=0` 记进
  `compare.json` 的 `decode` 段（`generate` 不吃 `temperature`，实际只传
  `generate_kwargs` 里的贪心键）；
* 提示词来自库里**没被训过**的新段（凡 `train_pairs.jsonl` 里出现过的 `segment_id`
  一律排除），一半改写、一半帧→正文；
* 产物：`compare.json`（逐条 + adapter 指纹 + 口径 + 环境）与 `compare.md`（可读版）。

## 3. 落盘/缓存硬纪律

* `HF_HOME`/`HF_HUB_CACHE`/`TRANSFORMERS_CACHE`/`TORCH_HOME`/`PIP_CACHE_DIR`
  一律由 `cache_env()` 指到 F:/G:（`--cache-root` 可改；本次实测用 `G:/Hermes/cache/hf`）；
* `--out-dir` 必须在 F:/G: 盘且**不得落在检出内**：产物不进版本库、不留在 worktree；
* 训练栈装在检出外的独立 venv（本次：`G:/lg_ft/venv`），**验收 venv 不装 torch**
  （装了反而可能污染其它用例），验收靠 `--dry-run` 与纯逻辑用例。

## 4. 规模上限（写死在代码里，不靠自觉）

`MAX_PAIRS_HARD_CAP=2000`、`MAX_STEPS_HARD_CAP=300`、`MAX_MODEL_PARAM_HINT=1.5`（B）。
`validate_config` 与 `extract_pairs` 会当场拒越界值。本机 4 GiB 卡的物理边界决定了
这些上限；**7B~14B 的 QLoRA 走 `docs/kaggle-qlora-notebook.md`**，本件不覆盖。

## 5. 已知限制（诚实）

* 不承诺质量提升：本次 held-out loss 有下降、base/tuned 逐条不同，但样本 ≤60、
  基座 0.5B、40 步，只能当**管线证据**，不能当「风格变好」的结论；
* 对比里 tuned 文本普遍更短——这是长度分布漂移，不是质量信号，脚本与交付都如实标注；
* 显存被别的进程挤占时可能退化：先退 `--max-len`/`--max-pairs`/`--steps`，
  再退 `--device cpu`（同脚本，步数可降到 ≥20），**不许硬撞显存**。
