# Kaggle QLoRA 7B 训练手册（G7 / T-D2）

> 任务：把 `data/exports/` 里已有的 SFT/DPO 数据打成可上传的 Kaggle 产物，并给一份
> **不依赖本机训练栈** 的 QLoRA 模板（`scripts/kaggle_qlora.py`）。
> 本手册写清：数据账本 → 上传 → 环境 → 显存/步数数学 → 超参理由 → 2×T4 取舍 →
> 跑通判据 → 踩坑清单 → 产物回收。
>
> 阅读约定：**每个数字标来源**。标 〔实测〕= 本机可复现（命令在 §12）；
> 标 〔估算〕= 按公式推的，没上机；标 〔上机确认〕= Kaggle 会变、开跑前先看页面。
> 没标的全是废话，可以跳过。

---

## 0. 一句话结论

**单张 T4 就够跑完整条链**（4bit 基座 + LoRA r=64 + 只学 completion），
数据量是 1442 条训练样本〔实测〕，**瓶颈不是显存，是标签质量和 wall-clock**。
双 T4 只买吞吐，不买显存；除非要把一次实验压进一个 session，否则别开。

真正需要人盯的是 §7 的判据：**这次训练是一次证伪实验，不是"练个能写的模型"**。
基线（7B 零样本走同一 prompt）必须一起跑，否则 §7 的数没有意义。

---

## 1. 产物账本

打包脚本：`scripts/kaggle_package.py`（只读 `data/exports/*.jsonl`，只写自己声明的文件）。

```
data/exports/kaggle/lg_qlora_v1/     ← 上传这个目录（不要传 tar.gz，见 §3.1）
data/exports/language_genome_qlora_v1.tar.gz   ← 备份/传阅用
data/exports/summary.json            ← 机器可读账本
```

| 文件 | 角色 | 行数 | 字节 | sha256（前 12） |
|---|---|---:|---:|---|
| `sft_train.jsonl` | SFT 训练 | 1442 | 6,132,706 | `58fe4ac3344d` |
| `sft_val.jsonl` | SFT held-out | 356 | 1,529,141 | `b471572fd621` |
| `dpo_train.jsonl` | LoRA-DPO（弱标签） | 70 | 242,597 | `5a8ae69deb1a` |
| `negatives.jsonl` | 负面模式库（不进训练） | 103 | 171,493 | `2fa6e15f512c` |
| `eval_corrupt_pairs.jsonl` | **只读评测仪器** | 244 | 437,630 | `c7ad1c622a40` |
| `rm_scores.jsonl` | RM 打分料（本模板不用） | 1316 | 853,644 | `89f3cb36d32c` |
| `frame_schema.json` | SemanticFrame 字段并集 | — | — | `e8be8a551b81` |
| `manifest.json` / `README.md` / `metaData.json` | 元数据 | — | — | `20b028a3e695` / `752fe781400e` / `88830b974ef7` |

整包：`language_genome_qlora_v1.tar.gz` = 2,106,862 B，
sha256 `1b68cec857f0735980c9ce6dff9e4170c26e0f3ac5fdd82ee5c6dd01b25d91ce`，
生成于 `2026-09-19T15:07:14Z`〔实测〕。

**本包钉的是打包那一刻的源文件**：`rm_v1.jsonl` 的 mtime 早于 P1-6（RM 分数冲突
规则）落地，`rm_v1_summary.json` 里没有 `n_conflict_dropped`〔实测〕。等源被
`export_training.py` 重生成后，`--verify` 会报 MISMATCH —— 那不是 bug，是在提醒你
按 §11 第 1 条重跑打包，否则 Kaggle 上那份数据与仓库当前口径不一致。

来源是 4 本已出版网文（凡人修仙传 / 将夜 / 斗罗大陆 / 琼明神女录），train 侧分布
241 / 374 / 390 / 437，val 侧 40 / 93 / 81 / 142〔实测〕。
**跨域泛化没有任何证据**，这一点在 §7 的判据里要还债。

字段名以 `manifest.json` 的 `files[].fields` 为准，或本机跑：

```bash
python - <<'EOF'
import json,collections
p="data/exports/kaggle/lg_qlora_v1/"
for f in ("sft_train","dpo_train","negatives"):
    r=json.loads(open(p+f+".jsonl",encoding="utf-8").readline())
    print(f, sorted(r))
EOF
```

### 1.1 为什么 train/val 不能按行随机切

相邻段落的 `prev1/prev2` 就是上一条样本的 `target`。按行切 → val 的前文出现在
train 的标签里，loss 会虚低到没法看。所以打包时做**连通块整块切分**：

* 并查集建边：同一 `segment_id`、`prev1 == 他人的 target`、`prev2 == 他人的 target`；
* 结果：91 条链边 / 1349 个连通块 / 最大块 7 行，按块 hash 排序后贪心填 val〔实测〕；
* 实际 val 占比 rows 0.198、chars 0.200（目标 0.2），seed 20260919〔实测〕；
* 自检断言：链上泄漏 0 · 共享 segment 0 · 同文跨集 0（`--verify` 每次重算）。

```bash
python scripts/kaggle_package.py --verify     # 重算全部 sha256 + 泄漏审计〔实测：全部一致〕
```

### 1.2 DPO 侧的两条硬口径（别改）

1. `corrupt_dpo_v1`（244 对，`chosen=人类原文`）**不进训练**。方向已被裁定否掉
   （`docs/HANDOVER.md` §0.5⑧：24 次裁决里只有 4 次人类原文胜出，11 次两边都差），
   它只作为评测仪器随包发，每行带 `usable_for_training: false` 和一个 `why`。
2. `ai_ranking_v1` 的 105 对里 **36 对回连不上上下文，直接丢掉**，不造 prompt〔实测〕；
   留下的 69 对 + 1 对集霸亲裁 = **70 对**，全部带 `weak` / `label_source`。
   其中 12 对的 prompt 段落落在 `sft_val` 侧〔实测〕→ 模板给 `--dpo-drop-val-overlap` 开关。

70 对撑不起"偏好对齐"，只够做一次**风格纠偏**（§7.3 的判据按这个尺度写）。

---

## 2. 环境决策：开卡之前先看这三件事

1. **精度**：T4 = sm75，**没有 bf16、没有 FlashAttention-2、没有 tf32**。模板按
   `nvidia-smi` 的 `compute_cap` 现场决定，`<8.0` 自动落 fp16 + sdpa〔实测：探测逻辑可跑，见 §12〕。
2. **下载量**：QLoRA 必须从 fp16 权重现场量化 → 要拉 ~15 GB〔估算：7.62B×2B〕。
   量化后常驻 ~3.9 GB。所以磁盘和缓存配额是先决条件，不是显存。
   把 `HF_HOME` 指到一个确定的目录，别让它落到镜像默认位置后才发现装不下（§3.2）。
3. **wall-clock**：4 epoch ≈ 364 优化步〔实测：91 步/epoch，见 §4〕。T4 上 QLoRA 的
   吞吐经验值 **±2× 不确定**。别一上来 `--epochs 4`：**先跑 30 步测吞吐**（§3.4），
   再决定是一次 session 跑完还是靠 checkpoint 跨 session 续。

Kaggle 的 GPU 型号、每周配额、session 上限、磁盘额度都是会变的产品参数〔上机确认〕。
开跑前看 **Notebook 右侧 Settings → Accelerator** 和页面顶部的 quota 提示，
不要照抄任何手册（包括这份）。

---

## 3. 完整步骤（可直接抄）

### 3.1 上传数据集

**传解压后的目录，不要传 .tar.gz** —— Kaggle 不解 tar.gz，notebook 里你会拿到一个
压缩包而不是 jsonl。

```bash
# 本机（Windows/git-bash）：包已在 data/exports/kaggle/lg_qlora_v1/
cd /f/agi/language-genome/data/exports/kaggle/lg_qlora_v1

# 1) 把里面的 REPLACE_WITH_KAGGLE_USERNAME 换成你的 Kaggle 用户名（两行都要换）
sed -i 's/REPLACE_WITH_KAGGLE_USERNAME/你的用户名/g' metaData.json README.md

# 2) ~/.kaggle/kaggle.json 放 API token，权限收紧
chmod 600 ~/.kaggle/kaggle.json

# 3) 首次创建 / 之后出新版本
kaggle datasets create  -p .                 # 读同目录 metaData.json
kaggle datasets version -p . -m "G7 v1: 组件级切分 + 泄漏审计"

# 4) 验证挂载视角
kaggle datasets files <用户名>/language-genome-qlora-v1
```

metaData 里 **`isPrivate: true` 是有意的**〔实测：模板就这么写〕：output 侧是已出版
网文的原文片段，公开上传等于再分发。要改公开需要集霸拍版权口径（`manifest.json`
的 `disciplines` 里也写了这条）。

网页路径也行：**Create → Datasets → Upload files**，把 `lg_qlora_v1/` 里的文件
（含 `metaData.json`）整包拖进去，Visibility 选 Private。

### 3.2 Notebook 设置与安装

Settings：
* **Accelerator = NVIDIA Tesla T4**（2×T4 见 §6）
* **Internet = ON**（否则 pip 和 HF 下载全挂，报错通常长这样：
  `Cannot connect to host huggingface.co:443`）
* Language = Python

第一个 cell（**装完必须 Restart & Run Clear，bnb 换 `.so` 不重启会报
`ImportError: libcudart.so`**）：

```python
# cell 1：环境与版本地板（未在本机验证；这是"起点"，不是"正确版本"）〔上机确认〕
import os
os.environ.setdefault("HF_HOME", "/kaggle/working/hf-cache")   # 别把 15GB 权重扔进 home
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
!pip install -q -U "transformers>=4.44,<5" "peft>=0.11,<0.16" \
    "trl>=0.9,<0.12" "bitsandbytes>=0.43" "accelerate>=0.33" "datasets>=2.20"
```

```python
# cell 2：把模板拷进 working 并先做口径自检（不 import torch，几秒出结果）
import shutil, glob
src = glob.glob("/kaggle/input/**/kaggle_qlora.py", recursive=True) \
    or glob.glob("/mnt/data/**/kaggle_qlora.py", recursive=True)
if src:
    shutil.copy(src[0], "/kaggle/working/kaggle_qlora.py")
!python /kaggle/working/kaggle_qlora.py --dry-run --data-dir /kaggle/input
```

`--dry-run` 会打印：prompt/target 长度分位、超长截断行数、train/val id 交集（必须 0）、
DPO prompt 落 train/val 的分布、无帧 prompt 条数、硬件画像。**这四类数对不上就不要继续**
（§1 的表就是它该给出的样子）。

数据集挂载点：`/kaggle/input/<dataset-slug>/`（只读）。模板用 `find_package()`
在 `/kaggle/input/*` 下找 `lg_qlora_v1`，所以传成 dataset 或直接传 output 都能认。

### 3.3 训练

```python
# cell 3：SFT（单卡）。产物固定落在 <out-dir>/adapter
!python /kaggle/working/kaggle_qlora.py \
    --stage sft \
    --data-dir /kaggle/input \
    --out-dir /kaggle/working \
    --model Qwen/Qwen2.5-7B-Instruct \
    --max-length 1536 --epochs 4 \
    --lr 2e-4 --batch-size 1 --grad-accum 16 \
    --lora-r 64 --lora-alpha 128 --lora-dropout 0.05 \
    --eval-steps 50 --save-steps 100
```

```python
# cell 4：接 DPO。跨 session 时必须显式指 --sft-adapter，
# 否则脚本会在基座上另起一个全新 LoRA（它会打警告，但不会替你停下来）
!python /kaggle/working/kaggle_qlora.py \
    --stage dpo --data-dir /kaggle/input --out-dir /kaggle/working \
    --sft-adapter /kaggle/working/adapter \
    --dpo-epochs 1 --dpo-lr 5e-6 --dpo-beta 0.1 --dpo-drop-val-overlap
```

`--stage both` 一条命令串起来（SFT → DPO → 自动出样）。
`--dpo-merged` 走"先 merge 再重新 4bit 量化"的路子（省 ~3.9 GB，代价是多一次
merge/quantize，见 §5.3）。DPO 产物落在 `<out-dir>/dpo_adapter`。

### 3.4 第一个 30 步该看什么

用 cell 3 的完整命令，只把两个数改掉：`--epochs 1 --save-steps 10 --eval-steps 10`。

盯三件事：

1. **每步秒数**：364 步 × 每步秒数 = 总时长。超过 session 上限就提前规划续训（§9.11）。
2. **loss 是否在 20-40 步内明显下降**：不降先看 §9.4（checkpointing + LoRA 梯度断链）。
3. **`nvidia-smi` 显存占用**：应该在 6-8 GB 量级〔估算，§5.1 有算法〕。
   如果 >12 GB，说明有东西没按预期走（多半是 `use_cache=True` 或没启用 gradient checkpointing）。

### 3.5 采样对比（关键，别省）

```python
# 出样 = 两遍走：先未训基座、放掉显存、再挂 adapter（不同时驻两份 4bit 7B）
!python /kaggle/working/kaggle_qlora.py --stage samples --data-dir /kaggle/input \
    --sft-adapter /kaggle/working/dpo_adapter \
    --n-samples 12 --gen-max-tokens 256 --gen-temperature 0.8
```

评 SFT 就指 `/kaggle/working/adapter`，评 DPO 就指 `/kaggle/working/dpo_adapter`
（`--stage both` / `--stage dpo` 跑完会自动用当轮最新 adapter 出一次样）。

产出 `/kaggle/working/samples.jsonl`，每行 `{id, human, base, tuned}`：
人类原文 / 基座零样本 / 挂了 adapter 的同一个模型。**这三列就是 §7.2 的评测物料**。
`base` 列走的是同一套 prompt 外壳，所以差异归因于 adapter，不归因于提示词。

---

## 4. 步数与数据量（全部可复算）

| 量 | 值 | 来源 |
|---|---:|---|
| SFT train / val | 1442 / 356 | 〔实测〕`summary.json` |
| 有效 batch | 16（bs 1 × accum 16） | 命令行 |
| steps / epoch | ≈ 91 | ⌈1442/16⌉〔实测：模板打印〕 |
| 4 epoch 总步数 | ≈ 364 | 91×4 |
| prompt tokens p50/p99 | 875 / 1215 | 〔实测：`--dry-run`〕 |
| target tokens p50/p99 | 54 / 158 | 〔实测：`--dry-run`〕 |
| `max_length=1536` 截断 | 4 行（0.3%） | 〔实测：`--dry-run`〕 |
| prompt 吃满、target 被挤光 → 丢行 | 3 行（0.2%） | 〔实测：`--dry-run`〕 |
| DPO 对数 | 70（1  epoch ≈ 4 步 @ accum 16） | 〔实测〕 |

token 口径提醒：`summary.json` 里的 `est_tokens_*` 是 **chars/1.5 的估算**，
不是 tokenizer 真值〔实测：方法就这么标注的〕。真值以上面 `--dry-run` 打印的
tokenizer 分位数为准 —— 那才是 `max_length` 该不该调的依据。

---

## 5. 显存数学（每一项给公式）

### 5.1 单卡 T4 16GB，QLoRA 7B，seq 1536，bs 1

| 组成 | 估算 | 算法 / 依据 |
|---|---:|---|
| 4bit 权重 | ≈ 3.9 GB | 7.62B × 0.5 B（NF4 双量化把 block-wise scale 也量化，省掉的是 fp16 那 2B/参数）〔估算〕 |
| LoRA 可训参数 | 161.5 M（2.1%） | 每层 q/o 各 64×(3584+3584)=458,752；k/v 各 64×(3584+512)=262,144；gate/up/down 各 64×(3584+18944)=1,441,792 → 5,767,168/层 × 28 层〔估算，按 Qwen2.5-7B 形状〕 |
| LoRA 参数 + 梯度 + 优化器态 | ≈ 1.0 GB | fp16 参数 0.32 + 梯度 0.32 + paged_adamw_8bit 两个状态 ≈0.32〔估算〕 |
| 检查点下的激活 | ≈ 0.35 GB 常驻 | 1536×3584×2B×29 个存储点〔估算，公式给你，真值看 nvidia-smi〕 |
| 反量化峰值 + 临时缓冲 | 1 – 3 GB | 逐层把 4bit 反量化成 fp16 才能算 matmul；这一项最不确定〔估算〕 |
| **合计** | **≈ 6 – 8 GB** | 16GB 卡有较多余量〔估算 → §3.4 用 nvidia-smi 核实〕 |

结论：**T4 16G 跑 7B QLoRA 不需要 offload，也不需要双卡**。
真要吃紧，按顺序砍：`--max-length 1024` → `--lora-r 32` → 只挂 q/o_proj。

### 5.2 为什么这套数据必须"只学 completion"

target 中位 54 token、prompt 中位 875 token〔实测〕。若不做掩码，loss 里约
94% 落在**固定的帧 JSON** 上 —— 那是输入不是输出，模型会把算力全花在背诵帧格式上，
学不到写。模板里 `encode_sft()` 把 prompt 全标 -100，且**整个 target 被截光时丢行
并计数**（宁可少 3 条，不要喂一条没有标签的样本）。

同时 `make_collator()` 显式造 `position_ids`（pad 位给 0）。右 padding + 默认
`attention_mask.cumsum` 的位置编码会让 pad 参与 RoPE，短样本的位置被拉长 →
推理时续写节奏崩。这两处是同类模板最常见的静默错误。

### 5.3 DPO 为什么单基座就够

TRL 的 LoRA-DPO 可以把**同一份 4bit 基座 + adapter 开/关**当作 policy 与 reference，
不需要第二份模型〔模板就这么设 `ref_model=None`〕。省下的就是那 ~3.9 GB。
代价：参考模型是 adapter 关闭时的 4bit 基座，与"fp16 原始基座"有微小数值差 ——
在 70 对、1 epoch 的尺度上可忽略，但**别拿这个配置去发论文式地报 DPO 指标**。

`--dpo-merged` 是另一条路，但**脚本不替你 merge**：它只是"DPO 基座换到这个目录"。
想走这条路，自己先产一份（多花一次磁盘和时间，换来 ref 与 policy 起点严格一致）：

```python
# 想要 ref=policy 起点时再跑；否则用默认的单基座 + adapter toggle 就够
import torch
from transformers import AutoModelForCausalLM
from peft import PeftModel

m = AutoModelForCausalLM.from_pretrained(
    "Qwen/Qwen2.5-7B-Instruct", torch_dtype=torch.float16, device_map="auto")
m = PeftModel.from_pretrained(m, "/kaggle/working/adapter").merge_and_unload()
m.save_pretrained("/kaggle/working/merged-fp16")     # ~15GB：先确认磁盘/内存吃得下
# 然后直接 --dpo-merged /kaggle/working/merged-fp16  （载入时由 bnb 现量化成 4bit）
```

默认路径（不传 `--dpo-merged`）省掉这整段，也是本手册推荐的走法。

---

## 6. 2×T4：值不值

**买吞吐，不买显存。** DDP 是两个进程各持一份完整模型，每份显存不变；
只把 batch 拆开并行，理想加速 1.6–1.8×（QLoRA 的反量化开销不可并行）。

要开：

```python
!pip install -q -U "transformers>=4.44,<5" "peft>=0.11,<0.16" "trl>=0.9,<0.12" \
    "bitsandbytes>=0.43" "accelerate>=0.33" "datasets>=2.20"
!accelerate config     # 交互式；或直接下一行
!accelerate launch --multi_gpu --num_processes 2 \
    /kaggle/working/kaggle_qlora.py --stage sft --grad-accum 8 ...
```

三条硬约束：

1. **`device_map` 必须跟着 `LOCAL_RANK` 走**，否则两个进程都落在 `cuda:0`，
   第二张卡空转、第一张 OOM。模板里 `load_base()` 已按
   `LOCAL_RANK / PMI_RANK` 取卡〔实测：代码如此，未双卡跑过〕。
2. **有效 batch 变了**：2 卡 × accum 16 = 32，lr 要跟着调
   （经验：√2 上抬到 ~2.8e-4）〔估算〕。或者把 `--grad-accum` 降到 8 保持等价。
3. notebook 里跑多进程，日志交错、`Ctrl+C` 难清干净；**跨 session 续训时 checkpoint
   只由 rank0 写**，恢复要确认目录完整。

**建议：第一轮别开双卡。** 先用单卡跑通并测出每步秒数，再决定要不要为吞吐付复杂度。

---

## 7. 跑通判据（先定判据，再开跑）

这次训练的目的不是"得到一个会写网文的模型"，是给项目一个**可用的下限**，
并且**尽可能否掉"微调有用"这个结论**（HANDOVER 收尾那条纪律）。

### 7.1 工程判据（不过就别谈效果）

1. `--dry-run` 四项数与 §1/§4 对得上；train/val id 交集 = 0〔实测已成立，重跑要复核〕。
2. 训练能收敛到 `eval_loss` 下降后走平，无 NaN、无 OOM。
3. `adapter_model` 目录能被 `PeftModel.from_pretrained` 加载，且加载后
   `samples.jsonl` 的输出**与加载前不同**（否则 adapter 根本没生效——这是真踩过的坑，§9.6）。

### 7.2 效果判据（对基线，人评）

从 `samples.jsonl` 抽 30 条，盲评（打乱 base/tuned 标签、双评）：

| 指标 | 通过线 | 不通过说明什么 |
|---|---|---|
| tuned 优于 base 的比例 | ≥ 60%（净胜 ≥ 15pp） | 低于 50% = 微调把模型练坏了，直接回退 |
| 帧覆盖度（facts 是否都表达） | tuned 不比 base 差 | 变差 = 模型学会"抄帧"而不是"按帧写"（§9.9） |
| 重复/口癖率 | 不高于 base | 变高 = 过拟合到 4 本书的表层风格，`--epochs` 减 |

净胜 < 10pp 视为**无信号**（这个数据量的合理结果），不要把它读成"微调没用"，
只读成"这个量级的这个数据不足以产生可测收益"。

### 7.3 DPO 判据（单独一档）

70 对〔实测〕不够谈对齐。判据只有一个：**有没有变坏**。
`--dpo-epochs 1 --dpo-lr 5e-6` 之后重跑 §7.2 的 30 条，若 tuned-dpo 不优于 tuned-sft，
就停在 SFT，把 DPO 记为"待数据"。**不要加 epoch 去救** —— 70 对上多跑一轮基本等于背题。

---

## 8. 超参一览（值 / 为什么 / 什么时候改）

| 参数 | 默认 | 理由 | 什么时候改 |
|---|---|---|---|
| `--model` | `Qwen/Qwen2.5-7B-Instruct` | 中文底座稳、T4 可跑 4bit；14B 在 16G 上没余量〔估算〕 | 换底座时 `--template` 要跟着换 |
| `--template` | `chat` | 帧→正文是指令式任务 | 底座无 instruct 时用 `plain` |
| `--max-length` | 1536 | prompt p99=1215 + target p99=158 ≈ 1400，只截 0.3%〔实测〕 | 想更快：1024（截得更多，先跑 `--dry-run` 看行数） |
| `--epochs` | 4 | 1442 条小数据，2-4 epoch 常见甜区〔估算，非本项目实测〕 | loss 走平后过拟合（eval 回升）就取拐点 |
| `--lr` | 2e-4 | QLoRA + LoRA r=64 的经验起点〔估算〕 | 不稳/NaN：降到 1e-4；无变化：先怀疑数据不是 lr |
| `--batch-size` × `--grad-accum` | 1 × 16 = 16 | 小数据要低噪声梯度 | 2 卡时降到 8 保持等价 |
| `--max-grad-norm` | 0.3 | 4bit + 小 batch 梯度尖峰多，硬截更稳〔估算〕 | 基本不用动 |
| `--lora-r` / `--alpha` | 64 / 128（2r） | 全 7 个线性模块 + r=64 = 2.1% 可训参数（§5.1） | 显存紧或过拟合：`--lora-r 16 --lora-alpha 32` |
| `--lora-dropout` | 0.05 | 小数据留一点正则 | 欠拟合可置 0 |
| `--precision` | 自动（sm75→fp16） | §2 | 只在 Ampere+ 试 bf16 |
| `--attn` | 自动（sdpa） | T4 无 FA2 | 只有 sm80+ 才 `flash_attention_2` |
| `--eval-steps` / `--save-steps` | 50 / 100 | 364 步的尺度〔实测推算〕 | session 上限紧就 `--save-steps 25` |
| `--dpo-lr` | 5e-6 | DPO 比 SFT 低 1–2 个数量级；70 对更保守 | 无变化也不抬，见 §7.3 |
| `--dpo-beta` | 0.1 | 标准起点 | 想更保守：0.3 |
| `--seed` | 42 | 全链路固定 | 换 seed 重跑才算复现 |

**默认值里只有"为什么是这个值"，没有"它对这批数据已被验证有效"。**
第一个实验该做的事：单卡、默认超参、`--epochs 1`，拿到吞吐和曲线形状，再谈调参。

---

## 9. 踩坑清单（按会撞上的顺序）

1. **Internet 没开** → `pip` 或 HF 下载失败。症状是网络错误而非 CUDA 错误，别往驱动上找。
2. **传了 .tar.gz** → `/kaggle/input/<slug>/` 下是个压缩包，`find_package()` 找不到
   `lg_qlora_v1`。传解压后的目录（§3.1）。
3. **装完 bitsandbytes 没重启 kernel** → `ImportError: libcudart.so` 或
   `bitsandbytes ... not supported`。Restart & Run All。
4. **gradient checkpointing + LoRA 梯度断链** → 日志出现
   `None of the inputs has requires_grad`、loss 几乎不降。模板两处已处理：
   `prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)` 挂 input-grad
   hook，`TrainingArguments` 里 `gradient_checkpointing_kwargs={"use_reentrant": False}`。
   **改模板加新模块时别把这两处拆掉**（`--stage dpo` 从零起 LoRA 时同样调了前者）。
5. **`use_cache` 与 checkpointing 互斥** → 训练路径显式 `model.config.use_cache = False`；
   出样路径 `generate_samples()` 再开回 `True`（不开就是每步重算全序列）。
   自己加生成代码时记得这一步。
6. **adapter 存了但加载不回来 / 加载后输出不变** → 已知组合坑：bnb 4bit + PEFT 的
   fused 层存 safetensors 会静默丢量化元数据。模板用 `save_safetensors=False`。
   **验收必须做 §7.1 第 3 条**，别只看文件存在。
7. **T4 上开 bf16** → 要么直接报错要么慢到不可用。`--fp16` 强制。
8. **T4 上装 FlashAttention-2** → `requires compute capability >= 8.0`。用 sdpa。
9. **帧 JSON 被模型背下来** → 输出里冒出 `- facts（必须表达）：` 这类帧文本。
   根因是掩码漏了（§5.2）或 `prompt_version` 变了；先确认 `encode_sft()` 的 -100 区间。
10. **中文引号/书名号被切分异常** → `--template plain` 时 tokenizer 可能把
    `「」《》` 切成多 token，长 prompt 白白变长。真值看 `--dry-run` 的分位数。
11. **session 到点被杀** → 前功尽弃。`--save-steps` 要小于 session 剩余时间对应的步数
    （§3.4 测出每步秒数后换算），下一个 session 用 `--resume /kaggle/working/.../checkpoint-NNN`。
12. **保存 output 时把 HF 缓存一起存了** → `/kaggle/working` 塞进 15GB 权重，导出失败。
    `HF_HOME` 指到独立子目录，保存时只 `Save Version` 需要的文件，或训完
    `!rm -rf /kaggle/working/hf-cache` 再保存。
13. **DPO 没接着 SFT 训** → `--stage dpo` 单独跑时不带 `--sft-adapter` 会在基座上
    另起一个全新 LoRA（脚本会打警告，但流程照走）。跨 session 必须
    `--sft-adapter /kaggle/working/adapter`。注意 `--resume` 是**另一个东西**：
    它只给 SFT 阶段做 `resume_from_checkpoint`，不承担"找 adapter"的职责。
14. **多卡时第二张卡空转** → `device_map` 没跟 `LOCAL_RANK`（§6 第 1 条）。
    用 `accelerate launch`，不要在 notebook 里手工 `CUDA_VISIBLE_DEVICES` 混着设。
15. **把 `eval_corrupt_pairs.jsonl` 当训练数据用了** → 这条是**项目层面的坑**，不是
    工程坑：`chosen=人类原文` 的方向已被裁定否掉（§1.2）。模板不读这个文件；
    若你手工加进 TRL 数据集，等于把否掉的结论重新灌回模型。

---

## 10. 产物回收

训练结束后 `/kaggle/working/` 下期望看到：

```
adapter/                       # SFT LoRA：adapter_config.json + adapter_model.bin（几十~几百 MB）
checkpoint-<step>/             # SFT 断点（save_total_limit=3，只留最后 3 个）+ trainer_state.json
run_sft.json                   # 本次 SFT 的口径快照：模型/精度/lora_r/epochs/lr/丢了几行
dpo/  dpo_adapter/  run_dpo.json   # 若跑了 DPO
samples.jsonl                  # human/base/tuned 三列 → §7.2 的评测物料
```

必做三件：
1. 下载 `samples.jsonl` 交人工盲评（**这份比 loss 重要**）；
2. 把 `trainer_state.json` 的 eval 曲线和净胜率写进
   `docs/`，并同步 `docs/HANDOVER.md` §53 的成败判据状态；
3. adapter 传 Kaggle Model（或 HF private repo），记录
   `数据集 slug + 数据集版本号 + 命令行 + seed`，缺一不能复现。

**注意：本包数据与 `summary.json` 不进版本库**（仓库政策：`data/` 在 `.gitignore`，
数据产物不是代码）。要交接产物请走 Kaggle / 网盘，并在提交说明里写 sha256。

---

## 11. 换数据 / 换底座时要做的三件事

1. **重打包**：`python scripts/kaggle_package.py --val-ratio 0.2 --seed <新种子>`，
   然后 `python scripts/kaggle_package.py --verify`。切分口径变了 → `summary.json`
   的 `split` 字段和每行 sha256 都会变，评测结论不能与 v1 直接比。
2. **对齐 prompt 版本**：`prompt_version` 是随包发的常量，改 `build_sft_prompt()`
   等于换分布，DPO 侧 `dpo_prompt()` 会同步重拼外壳（模板刻意保证 SFT/DPO 同一外壳）。
3. **重跑 `--dry-run` 并更新 §4 的表**：长度分位决定 `--max-length`，
   步数决定 `--save-steps` 和是否分 session。

---

## 12. 本机可复现的自检（不装训练栈、不跑训练）

```bash
cd /f/agi/language-genome

# 1) 语法（新脚本必须过）
python -c "compile(open('scripts/kaggle_package.py',encoding='utf-8').read(),'scripts/kaggle_package.py','exec')"
python -c "compile(open('scripts/kaggle_qlora.py',encoding='utf-8').read(),'scripts/kaggle_qlora.py','exec')"

# 2) 打包与账本一致性 + 泄漏审计
python scripts/kaggle_package.py --verify

# 3) 训练口径自检（不 import torch；不写任何文件）
python scripts/kaggle_qlora.py --dry-run --data-dir data/exports/kaggle
```

三条都应退出码 0，且第 3 条打印的分位数与 §4 一致。
本手册里的数就是这三条命令的输出，不是从任何旧文档抄来的。
