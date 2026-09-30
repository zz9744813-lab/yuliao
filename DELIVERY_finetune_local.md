# DELIVERY — 本机微调最小闭环（finetune_local）

分支 `task/finetune-local`，基线 `75ac752`，工作区 `F:/agi/_scratch/worktrees/lg-finetune-local`。

**一句话结论**：本机微调**管线已真跑通**——在 RTX 3050（4 GiB）上对一个 **0.5B 小基座**
做了**真实训练**（真实的 LoRA 微调：主跑 300 步，训练 loss 4.033→3.302、held-out loss 3.725→3.218 真下降、
峰值显存 3303 MiB 内跑完；另有一次 40 步短跑 3.633→1.867、held-out 2.509→2.228），
并用 `finetune_compare.py` 出了 **base vs tuned 同提示词逐条对照**
（6 条全部 differ）。**这是「有管线、能真训练」的证据，不是 §53 六条标准的证据，
也不承诺质量提升**（样本 ≤1500、基座 0.5B、步数 ≤300，量级太小）。7B~14B 仍走 Kaggle 路线。

---

## 1. 交付文件（白名单内）

| 文件 | 状态 |
|---|---|
| `scripts/finetune_min.py` | 抽数据 + 真训练（LoRA），已存在并本次真跑验证 |
| `scripts/finetune_compare.py` | base vs tuned 固定贪心对照，已存在并本次真跑验证 |
| `tests/test_finetune_min.py` | 验收用例（数据构造/配置校验/adapter 落盘/解码参数固定；训练本体标 `slow` 跳过） |
| `docs/FINETUNE_LOCAL.md` | 本次新增：设计、口径、规模上限、已知限制 |
| `DELIVERY_finetune_local.md` | 本文件 |

产物一律落在检出外、F:/G: 盘（不进版本库）：`G:/lg_runs/ft01/`
（`train_pairs.jsonl` / `adapter/` / `train_log.json` / `run_env.json` / `compare/{compare.json,compare.md}`）。

## 2. 环境（实测逐字）

```
GPU   : NVIDIA GeForce RTX 3050 Laptop GPU
显存  : total 4095 MiB / 起跑 free 3303 MiB / 训练峰值 1502 MiB
bf16  : torch.cuda.is_bf16_supported() = True
驱动栈: torch 2.11.0+cu128（cuda_available=True）
       transformers 5.17.0 | peft 0.21.1 | accelerate 1.15.0 | safetensors
Python: 3.11.16（训练栈装在检出外独立 venv：G:/lg_ft/venv）
验收 venv（无 torch）: F:/Hermes/hermes-agent/venv/Scripts/python.exe（Python 3.11.16）
缓存  : HF_HOME=HF_HUB_CACHE=G:/Hermes/cache/hf ；PIP_CACHE_DIR=G:/Hermes/cache/pip（全部 F:/G:，零写 C 盘）
```

> 值班补充（19:37）当时可用显存仅 ≈1.9 GiB 且预报要退 CPU；动手时占用降到 601~788 MiB、
> 空出 ≈3.2 GiB，0.5B + LoRA + bf16 + 梯度检查点 + micro-batch=1 峰值仅 1502 MiB，
> **无需退 CPU，直接跑出真 GPU 训练**。脚本 `--device cpu` 的 CPU 兜底路径仍保留可用。

## 3. 真跑命令与逐字输出

### 3.1 训练（`finetune_min.py --stage all`，CUDA/bf16/40 步）

```
G:/lg_ft/venv/Scripts/python.exe scripts/finetune_min.py --stage all \
  --db F:/agi/language-genome/data/language_genome.db \
  --out-dir G:/lg_runs/ft01 --cache-root G:/Hermes/cache/hf \
  --model Qwen/Qwen2.5-0.5B-Instruct \
  --device cuda --dtype bf16 --steps 40 --max-pairs 60 --eval-size 8 \
  --max-len 192 --micro-batch 1 --grad-accum 4 --log-every 2
```

关键逐行日志（原文）：

```
[ftmin] 数据：{..."pairs_total": 60, "train": 52, "eval": 8,
       "pool": {"frame_to_text": 2705, "rewrite_deai": 3, "ai_to_human": 118}}
[ftmin] device=cuda dtype=bf16 model=Qwen/Qwen2.5-0.5B-Instruct
[ftmin] prompt 模式=chat_template 训练样本=52（编码丢弃 0）held-out=8（丢弃 0）max_len=192
[ftmin] LoRA r=16 alpha=32 可训练参数 8,798,208/502,830,976（1.7497%）
[ftmin] held-out 初始 loss = 2.5089899688153654
[ftmin] step 2/40 loss=2.3172 lr=1.50e-04 1.64s
[ftmin] step 4/40 loss=1.4426 lr=2.00e-04 1.62s
...
[ftmin] step 40/40 loss=1.8669 ...
[ftmin] held-out 结束 loss = 2.2282316259435704 用时 67.3s 峰值显存 1502 MiB
TRAIN_EXIT=0
```

`train_log.json` 里的 40 步 loss 序列（逐字）：

```
[3.633217, 2.31717, 3.251779, 1.44257, 2.849392, 2.922178, 2.131505, 2.286188,
 3.842916, 2.621176, 3.853204, 2.937224, 3.772308, 2.638767, 2.019334, 2.645065,
 0.76044, 1.415601, 3.537889, 1.643559, 1.712176, 1.694406, 2.703833, 2.324991,
 2.226955, 3.394876, 2.131655, 1.990478, 1.063895, 1.923906, 2.049143, 1.417979,
 0.682822, 2.050107, 1.723803, 2.349537, 2.127564, 2.108799, 1.523752, 1.8669]
```

loss 摘要：首 `3.633217` / 末 `1.8669`；前 10 步均值 `2.729809` / 后 10 步均值 `1.790041`；
**held-out（未参与训练的 8 条）`2.508990` → `2.228232`** —— 这是比"训练 loss 下降"更难自欺的证据。
用时 67.26s（1.681 s/step），samples_seen=160、tokens_seen=7989。

### 3.2 adapter 落盘（真文件，非空）

`G:/lg_runs/ft01/adapter/`：

```
README.md              5,206 B
adapter_config.json    1,232 B
adapter_model.safetensors  35,237,104 B   <- LoRA 权重
chat_template.jinja    2,561 B
tokenizer.json     11,421,892 B
tokenizer_config.json    724 B
```

### 3.3 对照（`finetune_compare.py`，CUDA/6 条/贪心）

```
G:/lg_ft/venv/Scripts/python.exe scripts/finetune_compare.py \
  --adapter G:/lg_runs/ft01/adapter --db F:/agi/language-genome/data/language_genome.db \
  --train-pairs G:/lg_runs/ft01/train_pairs.jsonl --out-dir G:/lg_runs/ft01/compare \
  --base-model Qwen/Qwen2.5-0.5B-Instruct --cache-root G:/Hermes/cache/hf \
  --device cuda --n-prompts 6 --max-new-tokens 64
```

```
[ftcmp] 排除已训段 id：60
[ftcmp] 提示词 6 条：{'frame_to_text': 3, 'rewrite_deai': 3}
[ftcmp] device=cuda dtype=bf16
[ftcmp] 摘要：identical=0 differ=6 base 中位 95 字 / tuned 中位 74 字
[ftcmp] 写出 compare.json 与 compare.md（identical 0/6）
CMP_EXIT=0
```

`compare.json` 口径段（逐字）：
`decode = {temperature: 0.0, do_sample: false, num_beams: 1, top_p: 1.0, top_k: 0, repetition_penalty: 1.0, max_new_tokens: 64}`；
`generate_kwargs = {do_sample: false, num_beams: 1, max_new_tokens: 64}`（贪心，两侧同参）。
adapter 指纹 `97c206b9f60c09a1`（6 文件）。

一条对照样例（`rewrite_deai`，SEG-619b2f6838d7；base/tuned 只差 LoRA 权重）：

```
base : 韩立心里有些紧张，定了定神，看了看这几位，又看了一眼身后三十余名修士。
       其中只有三人是筑基期的，其余人都是炼气期的弟子，这让韩立稍微安心了一些，
       至少不是执行高难度任务的样子。
tuned: 韩立心里有些紧张，他定了定神，看了看这人，又看了看身后三十多名修士。
       其中有筑基期的只有三人，其余的人都是炼气期的弟子，这让韩立稍微安心了一些，
       起码也不像是执行什么高难度的任务。
```

> 两侧确有差异（0/6 相同），证明 adapter 生效、管线贯通。**但 tuned 普遍更短**，
> 这是长度分布漂移，**不是质量提升的证据**——脚本与本文件都如实声明不承诺质量。

## 4. 验收命令（必须真跑，退出码 0）

在**没装 torch** 的验收 venv 里跑（训练本体标 `slow` 跳过）：

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_finetune_min.py -q
→ 81 passed, 1 skipped（slow 训练用例跳过），EXIT=0
```

（本文件末尾「§6 收尾复跑」贴本次实测逐字结果。）

## 5. 边界（诚实，务必读）

* 本机 4 GiB 显存只能做**小基座（≤1.5B）最小闭环**；**7B~14B 的 QLoRA 走 Kaggle 路线**
  （`docs/kaggle-qlora-notebook.md`），本件不覆盖、不宣称等价；
* 本件**不是** `docs/HANDOVER.md` §53 六条成功标准的证据，**不承诺质量提升**：
  样本 ≤2000（本次仅 60）、基座 0.5B、步数 ≤300（本次 40），结论只能当**管线跑通的证据**；
* held-out loss 下降、base/tuned 逐条不同 = 管线证据，≠「模型写小说变好了」；
* 一切文件（venv/模型/数据/缓存/产物）均在 **F:/G: 盘，零写 C 盘**；产物在检出外，
  不进版本库；不写真库、不改 `app/**`、不 commit/merge/push。

## 6. 收尾复跑（本次实测逐字）

```
$ F:/Hermes/hermes-agent/venv/Scripts/python.exe -m pytest tests/test_finetune_min.py -q
........................................................................ [ 87%]
.........s                                                               [100%]
81 passed, 1 warning in ...
EXIT=0
```
