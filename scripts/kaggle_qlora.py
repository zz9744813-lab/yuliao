"""G7 训练通道：Kaggle QLoRA 模板（SFT + 可选 LoRA-DPO）—— 任务 T-D2。

超参理由与踩坑清单见 `docs/kaggle-qlora-notebook.md`；数据由
`scripts/kaggle_package.py` 打成 `lg_qlora_v1`（Kaggle dataset）。

设计约束都来自仓库现状，不是通用模板话术：

* 1442 train / 356 val（SFT）、70 对（DPO，`weak` 全为真）→ **只训 adapter**，
  7B 4bit + LoRA。这个量级全参必欠拟合（`docs/training-feasibility-20260919.md` §2）。
* prompt 是"语义帧 → 人类原文"：帧要点中位 ~1.4k 字、target 中位 80 字
  → 默认 `max_length=1536`，且 **只学 completion**（prompt 全 -100 掩码），
  否则 loss 被帧 JSON 吃掉，模型学不到写。
* `eval_corrupt_pairs.jsonl` 不进任何训练阶段：`chosen=人类原文` 的方向已被裁定
  否掉（HANDOVER §0.5⑧）。模板只认 sft_*/dpo_train/negatives。
* T4（sm75）没有 bf16、没有 FlashAttention-2、没有 tf32 → 自动落 fp16 + sdpa。
  精度/注意力后端都按 `nvidia-smi` 的 compute_cap 现场决定，不靠猜。
* 本机没有 GPU 也不装训练栈：`--dry-run` 路径**全程不 import torch**，
  只校验数据口径、模板拼接、长度分布和硬件画像。

用法（Kaggle Notebook 第一个 cell）：

    !pip install -q -U "transformers>=4.44,<5" "peft>=0.11,<0.16" \\
        "trl>=0.9,<0.12" "bitsandbytes>=0.43" "accelerate>=0.33" "datasets>=2.20"
    !python /kaggle/working/kaggle_qlora.py --stage both --data-dir /kaggle/input

产物目录：SFT → <out-dir>/adapter，DPO → <out-dir>/dpo_adapter，对照样本 →
<out-dir>/samples.jsonl。跨 session 接着 DPO 必须显式 --sft-adapter <out-dir>/adapter，
否则会在基座上另起一个全新 LoRA；只重出样本用 --stage samples。

本地自检：

    python scripts/kaggle_qlora.py --dry-run --data-dir data/exports/kaggle
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

PKG_NAME = "lg_qlora_v1"
SFT_KEYS = ("id", "instruction", "input", "output", "context_prev1", "context_prev2")
DPO_KEYS = ("id", "prompt", "chosen", "rejected", "weak", "label_source",
            "prompt_segment_sft_split")
# frame_schema.json 实测到 character_state.intent / .intention 两种键并存（导出侧字段漂移）
# → 模板必须都认，否则那批样本的意图静默丢失。
INTENT_KEYS = ("intention", "intent")
DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def log(msg: str) -> None:
    print(f"[qlora] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[qlora] 中止：{msg}", file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------- 数据装载（stdlib）

def find_package(explicit: str | None) -> Path:
    """在 Kaggle 和本地都定位到 lg_qlora_v1 目录。"""
    cands: list[Path] = []
    if explicit:
        cands += [Path(explicit) / PKG_NAME, Path(explicit)]
    for root in (Path("/kaggle/input"), Path.cwd() / "data" / "exports"):
        if root.is_dir():
            cands.append(root / PKG_NAME)
            for p in sorted(root.iterdir()):
                if p.is_dir():
                    cands += [p / PKG_NAME, p]
    for c in cands:
        if (c / "sft_train.jsonl").is_file():
            return c
    die("找不到 lg_qlora_v1/sft_train.jsonl。Kaggle 侧请 Attach 数据集后传 "
        "--data-dir /kaggle/input/<slug>；本地先跑 scripts/kaggle_package.py")


def read_jsonl(path: Path, required: tuple[str, ...]) -> list[dict]:
    if not path.is_file():
        die(f"缺文件 {path}")
    rows, bad = [], 0
    with path.open(encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            missing = [k for k in required if k not in r]
            if missing:
                bad += 1
                if bad <= 3:
                    log(f"  跳过 {path.name}:{ln}，缺字段 {missing}")
                continue
            rows.append(r)
    if bad:
        log(f"  {path.name}: {bad} 行字段不齐/坏 JSON 已跳过")
    return rows


def txt(v) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return "；".join(x for x in (txt(i) for i in v) if x)
    if isinstance(v, dict):
        return "；".join(f"{k}={txt(x)}" for k, x in sorted(v.items()) if txt(x))
    if v is None or isinstance(v, bool):
        return "" if v is None else str(v)
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def frame_points(row: dict) -> str:
    """把语义帧压成模板要点（字段名全部来自 frame_schema.json 的实测并集）。"""
    try:
        frame = json.loads(row.get("instruction") or "{}")
    except json.JSONDecodeError:
        frame = {}
    if not isinstance(frame, dict):
        frame = {}
    out: list[str] = []
    for key in ("event", "intention", "reader_effect", "observation_focus",
                "information_focus", "dialogue_intent", "rhythm", "pov"):
        if txt(frame.get(key)):
            out.append(f"- {key}：{txt(frame[key])}")
    facts = [f for f in frame.get("facts") or [] if isinstance(f, dict)]
    if facts:
        out.append("- facts（必须表达）：" + "；".join(
            f"{f.get('statement', '')}[{f.get('certainty', '')}]" for f in facts))
    cs = frame.get("character_state")
    if isinstance(cs, dict):
        bits = [f"{k}={txt(v)}" for k, v in sorted(cs.items())
                if (k in INTENT_KEYS or k in ("visible_emotion", "goal", "knows",
                                              "does_not_know")) and txt(v)]
        if bits:
            out.append("- character_state：" + "；".join(bits))
    for key, label in (("reader_should_infer", "应让读者推断"),
                       ("must_not_state", "不许直说")):
        if txt(frame.get(key)):
            out.append(f"- {key}（{label}）：{txt(frame[key])}")
    ec = frame.get("expression_constraints")
    if isinstance(ec, dict):
        bits = [f"{k}={txt(v)}" for k, v in sorted(ec.items()) if txt(v)]
        if bits:
            out.append("- expression_constraints：" + "；".join(bits))
    for key in ("beats", "pauses", "dialogue"):
        if txt(frame.get(key)):
            out.append(f"- {key}：{txt(frame[key])}")
    if txt(frame.get("emotion_intensity")):
        out.append(f"- emotion_intensity：{txt(frame['emotion_intensity'])}")
    hints = row.get("strategy_hint") or []
    if hints:
        out.append("- strategy_hint（可选风格提示）：" + "；".join(
            str(h) for h in hints[:4]))
    return "\n".join(out) or "- （帧为空，仅按前文续写）"


def build_sft_prompt(row: dict) -> str:
    prev = "\n".join(x for x in (row.get("context_prev2"), row.get("context_prev1"))
                     if x and str(x).strip())
    parts = [
        "你是中文网文作者。按给定的语义帧续写下一段正文。",
        "facts 必须都表达；must_not_state 里的信息只能让读者自己推断，不许直说。",
        "遵守 expression_constraints。只输出正文：不要标题、不要解释、不要复述本提示。",
    ]
    if prev:
        parts.append("【前文】\n" + prev)
    parts.append("【语义帧】\n" + frame_points(row))
    return "\n\n".join(parts) + "\n\n【正文】\n"


FRAME_SEG = re.compile(r"【语义帧（必须表达 / 不许直说）】\n(.*?)\n\n【任务】", re.S)
PREV_SEG = re.compile(r"【前文】\n(.*?)(?:\n\n【|$)", re.S)


def dpo_prompt(row: dict) -> str:
    """包里 dpo 行的 prompt 是打包时拼好的；这里重拼成与 SFT **同一个外壳**，
    否则 SFT→DPO 两阶段模板不一致，adapter 会被推向另一种分布。"""
    src = row["prompt"]
    m = FRAME_SEG.search(src)
    frame_json = m.group(1) if m else None
    p = PREV_SEG.search(src)
    prev = p.group(1).strip().splitlines() if p else []
    prev2 = prev[0] if len(prev) >= 2 else ""
    prev1 = prev[1] if len(prev) >= 2 else (prev[0] if prev else "")
    return build_sft_prompt({"instruction": frame_json, "context_prev2": prev2,
                            "context_prev1": prev1, "strategy_hint": []})


def build_dpo_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "prompt": dpo_prompt(r),
            "chosen": r["chosen"],
            "rejected": r["rejected"],
            "weak": bool(r.get("weak")),
            "label_source": r.get("label_source"),
            "split_tag": r.get("prompt_segment_sft_split"),
        })
    return out


# ---------------------------------------------------------------- 硬件画像

def gpu_profile() -> dict:
    prof = {"names": [], "cc": None, "total_gb": 0.0, "free_gb": 0.0}
    try:
        q = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.free,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20)
        for line in q.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if q.returncode != 0 or len(parts) < 4:
                continue
            prof["names"].append(parts[0])
            prof["total_gb"] += float(parts[1]) / 1024.0
            prof["free_gb"] += float(parts[2]) / 1024.0
            prof["cc"] = float(parts[3])
    except Exception as e:  # noqa: BLE001 —— 探测失败绝不让训练挂
        log(f"nvidia-smi 探测失败（退回文档默认）：{type(e).__name__}: {e}")
    return prof


def hardware_plan(prof: dict, args) -> dict:
    cc = prof.get("cc")
    ampere = cc is not None and cc >= 8.0
    precision = args.precision or ("bf16" if ampere else "fp16")
    if args.fp16:
        precision = "fp16"
    attn = args.attn
    if attn == "auto":
        attn = "sdpa"  # FA2 需要额外装 wheel，auto 不赌
    plan = {
        "gpus": prof.get("names") or ["<未探到 GPU>"],
        "compute_cap": cc,
        "vram_total_gb": round(prof.get("total_gb") or 0.0, 1),
        "precision": precision,
        "attn_implementation": attn,
        "tf32": bool(ampere and args.tf32),
        "ddp": (f"{len(prof['names'])}×DDP：数据并行，吞吐 ≈1.6-1.8×，"
                "单卡显存占用不变（不是 ZeRO，不会减半）"
                if len(prof.get("names") or []) > 1 else "单卡"),
        "notes": [],
    }
    if cc is None:
        plan["notes"].append("没探到 GPU：--dry-run 之外的路径不要在无卡机上跑")
    elif cc < 7.0:
        plan["notes"].append(f"compute_cap {cc} 低于 bitsandbytes 支持线（sm70+）→ 4bit 不可用")
    elif cc < 8.0:
        plan["notes"].append(
            f"sm{str(cc).replace('.', '')}（T4 级）：无 bf16 / 无 FA2 / 无 tf32 "
            "→ fp16 + sdpa。fp16 若 NaN，先确认不是把 paged optimizer 关掉了")
    else:
        plan["notes"].append("Ampere+：bf16 可用；装好 flash-attn 后可改 --attn flash_attention_2")
    if prof.get("total_gb") and prof["total_gb"] < 15.0:
        plan["notes"].append("总显存 <15G：建议 --max-length 1024 并保留 CPU offload")
    return plan


# ---------------------------------------------------------------- dry-run（无 torch）

def est_tokens(s: str) -> int:
    return max(1, math.ceil(len(s) / 1.5))


def pct(xs, p) -> int:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def do_dry_run(pkg: Path, args) -> int:
    log(f"数据包 {pkg}")
    tr = read_jsonl(pkg / "sft_train.jsonl", SFT_KEYS)
    va = read_jsonl(pkg / "sft_val.jsonl", SFT_KEYS)
    raw_dpo = read_jsonl(pkg / "dpo_train.jsonl", DPO_KEYS)
    dpo = build_dpo_rows(raw_dpo)
    if not tr:
        die("sft_train.jsonl 没有可用行")
    log(f"SFT train/val = {len(tr)}/{len(va)} · DPO = {len(dpo)}")

    leak = {r["id"] for r in tr} & {r["id"] for r in va}
    pt, tg, combo = [], [], []
    for r in tr:
        a = est_tokens(build_sft_prompt(r))
        b = est_tokens(r["output"])
        pt.append(a)
        tg.append(b)
        combo.append(a + b)
    over = sum(1 for c in combo if c > args.max_length)
    dropped = sum(1 for c in pt if c >= args.max_length)
    log(f"prompt token p50/p99 = {pct(pt, .5)}/{pct(pt, .99)}")
    log(f"target token p50/p99 = {pct(tg, .5)}/{pct(tg, .99)}")
    log(f"max_length={args.max_length}：prompt+target 超长会被截 "
        f"{over} 行（{over / len(combo):.1%}）；"
        f"prompt 单独就吃满、target 全被挤掉 → 丢 {dropped} 行（{dropped / len(pt):.1%}）")
    log(f"train/val id 交集（必须 0）= {len(leak)}")
    if leak:
        die("切分泄漏，回去跑 python scripts/kaggle_package.py --verify")
    log(f"DPO prompt 段落落 SFT 侧分布 = "
        f"{dict(Counter(r['split_tag'] for r in dpo))}"
        f"（--dpo-drop-val-overlap 会剔掉 val 那批）")
    log(f"DPO 无帧 prompt（退化为纯前文续写）= "
        f"{sum(1 for r in dpo if '帧为空' in r['prompt'])}")
    spe = max(1, args.batch_size * args.grad_accum)
    log(f"步数估算：epochs={args.epochs} 有效 batch={spe} → "
        f"steps/epoch ≈ {math.ceil(len(tr) / spe)}，"
        f"总 ≈ {math.ceil(len(tr) / spe) * args.epochs}")
    print("\n--- 样例：SFT 拼接后送入模型的 prompt（前 700 字）---")
    print(build_sft_prompt(tr[0])[:700])
    print("--- 该样例 label 侧（人类原文，模型要学的部分）---")
    print(tr[0]["output"])
    if dpo:
        print("--- 样例：DPO 重拼后的 prompt 外壳（前 350 字）---")
        print(dpo[0]["prompt"][:350])
        print("--- chosen / rejected 各前 60 字 ---")
        print("chosen :", dpo[0]["chosen"][:60])
        print("rejected:", dpo[0]["rejected"][:60])
    print("--- 硬件画像（只读探测，不训练）---")
    print(json.dumps(hardware_plan(gpu_profile(), args), ensure_ascii=False, indent=2))
    log("--dry-run 结束：全程未 import torch，未写任何文件")
    return 0


# ---------------------------------------------------------------- 训练（Kaggle 侧）

def bnb_config(args):
    import torch
    from transformers import BitsAndBytesConfig
    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=(torch.bfloat16 if args.precision == "bf16"
                                else torch.float16))


def load_base(args, path: str | None = None):
    """4bit 载入基座；attn 后端不可用时退化重试一次（Kaggle 上 flash-attn 常缺）。

    device_map 必须跟着 LOCAL_RANK 走：写死 {"": 0} 会让 accelerate/DDP 的两个进程
    挤在同一张卡上，第二张 T4 白给。
    """
    import os
    from transformers import AutoModelForCausalLM, AutoTokenizer
    plan = hardware_plan(gpu_profile(), args)
    rank = int(os.environ.get("LOCAL_RANK", os.environ.get("PMI_RANK", 0)) or 0)
    tok = AutoTokenizer.from_pretrained(path or args.model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tok.padding_side = "right"  # 配 collate 里的 position_ids 修正
    kwargs = {"quantization_config": bnb_config(args), "device_map": {"": rank},
              "trust_remote_code": True}
    if args.attn != "off":
        kwargs["attn_implementation"] = plan["attn_implementation"]
    try:
        model = AutoModelForCausalLM.from_pretrained(path or args.model, **kwargs)
    except (ImportError, RuntimeError, ValueError) as e:
        if "attn_implementation" not in kwargs:
            raise
        log(f"attn={kwargs['attn_implementation']} 不可用（{type(e).__name__}）→ 退回默认")
        kwargs.pop("attn_implementation")
        model = AutoModelForCausalLM.from_pretrained(path or args.model, **kwargs)
    model.config.use_cache = False  # 与 gradient checkpointing 互斥
    model.config.pad_token_id = tok.pad_token_id
    return tok, model


def lora_config(args):
    from peft import LoraConfig
    return LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha,
                      lora_dropout=args.lora_dropout, bias="none",
                      task_type="CAUSAL_LM", target_modules=LORA_TARGETS)


def encode_sft(tok, row: dict, args) -> dict:
    prompt = build_sft_prompt(row)
    if args.template == "chat":
        try:
            prompt = tok.apply_chat_template(
                [{"role": "user", "content": prompt}],
                add_generation_prompt=True, tokenize=False)
        except Exception:  # noqa: BLE001 —— 老 tokenizer 没有 chat template
            pass
    p = tok(prompt, add_special_tokens=False)["input_ids"]
    o = tok(row["output"] + tok.eos_token, add_special_tokens=False)["input_ids"]
    ids, labels = (p + o)[:args.max_length], ([-100] * len(p) + o)[:args.max_length]
    if all(x == -100 for x in labels):
        return {}  # prompt 挤掉了全部 target：留着只会教模型输出 pad
    return {"input_ids": ids, "labels": labels, "attention_mask": [1] * len(ids)}


def make_collator(tok, args):
    class Collator:
        def __call__(self, features):
            import torch
            pad = tok.pad_token_id
            m = max(len(f["input_ids"]) for f in features)
            ids, lab, am, pos = [], [], [], []
            for f in features:
                n = m - len(f["input_ids"])
                ids.append(f["input_ids"] + [pad] * n)
                lab.append(f["labels"] + [-100] * n)
                am.append(f["attention_mask"] + [0] * n)
                # pad 的位置全压到 0：-100 标签仍进 RoPE，不修会污染注意力
                pos.append(list(range(len(f["input_ids"]))) + [0] * n)
            return {"input_ids": torch.tensor(ids),
                    "attention_mask": torch.tensor(am),
                    "labels": torch.tensor(lab),
                    "position_ids": torch.tensor(pos)}
    return Collator()


def final_loss(trainer) -> float | None:
    for entry in reversed(trainer.state.log_history):
        if "loss" in entry:
            return float(entry["loss"])
    return None


def train_sft(args) -> Path:
    import torch.utils.data  # noqa: F401 —— 确保子模块可用
    from peft import get_peft_model, prepare_model_for_kbit_training
    from transformers import Trainer, TrainingArguments

    pkg = find_package(args.data_dir)
    rows = read_jsonl(pkg / "sft_train.jsonl", SFT_KEYS)
    vrows = read_jsonl(pkg / "sft_val.jsonl", SFT_KEYS)
    tok, model = load_base(args)
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    peft_model = get_peft_model(model, lora_config(args))
    peft_model.print_trainable_parameters()
    log(f"LoRA r={args.lora_r} α={args.lora_alpha} dropout={args.lora_dropout} "
        f"targets={len(LORA_TARGETS)}×linear")

    feats = [e for e in (encode_sft(tok, r, args) for r in rows) if e]
    vfeats = [e for e in (encode_sft(tok, r, args) for r in vrows) if e]
    log(f"tokenize：train {len(feats)}/{len(rows)}（丢 {len(rows) - len(feats)}）· "
        f"val {len(vfeats)}/{len(vrows)}")

    targs = TrainingArguments(
        output_dir=args.out_dir,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        weight_decay=0.0,
        fp16=args.precision == "fp16",
        bf16=args.precision == "bf16",
        tf32=args.tf32,
        optim="paged_adamw_8bit",
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_grad_norm=args.max_grad_norm,
        logging_steps=10,
        eval_strategy=("steps" if vfeats else "no"),
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,
        save_safetensors=False,  # 坑：bnb 4bit + peft + safetensors 融合加载会崩
        report_to=[],
        dataloader_num_workers=2,
        seed=args.seed,
    )
    trainer = Trainer(model=peft_model, args=targs,
                      train_dataset=_as_dataset(feats),
                      eval_dataset=(_as_dataset(vfeats) if vfeats else None),
                      data_collator=make_collator(tok, args))
    resume = args.resume if (args.resume and not args.no_resume) else None
    trainer.train(resume_from_checkpoint=resume)
    adapter = Path(args.out_dir) / "adapter"
    peft_model.save_pretrained(adapter, safe_serialization=False)
    tok.save_pretrained(adapter)
    write_meta(adapter.parent / "run_sft.json",
               {"stage": "sft", "model": args.model, "template": args.template,
                "train_rows": len(feats), "dropped_rows": len(rows) - len(feats),
                "val_rows": len(vfeats), "max_length": args.max_length,
                "epochs": args.epochs, "lr": args.lr,
                "lora_r": args.lora_r, "precision": args.precision,
                "final_loss": final_loss(trainer), "package_dir": str(pkg)})
    log(f"SFT adapter → {adapter}")
    return adapter


def _as_dataset(feats: list[dict]):
    import torch.utils.data as tu

    class DS(tu.Dataset):
        def __len__(self):
            return len(feats)

        def __getitem__(self, i):
            return feats[i]
    return DS()


def write_meta(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def train_dpo(args, adapter: Path | None) -> None:
    from datasets import Dataset as HFDataset
    from peft import PeftModel
    from trl import DPOConfig, DPOTrainer

    pkg = find_package(args.data_dir)
    rows = build_dpo_rows(read_jsonl(pkg / "dpo_train.jsonl", DPO_KEYS))
    if args.dpo_drop_val_overlap:
        keep = [r for r in rows if r["split_tag"] != "val"]
        log(f"DPO 剔除 prompt 段落落在 sft_val 的 {len(rows) - len(keep)} 对 → {len(keep)}")
        rows = keep
    if not rows:
        die("DPO 集为空")

    base_path = args.dpo_merged or args.model
    tok, model = load_base(args, path=base_path)
    # model 是 is_trainable 的 PeftModel 时，TRL 靠"摘掉 adapter"当 reference，
    # 不再复制一份 4bit 权重 —— 这是 4bit DPO 能塞进 T4 的关键，所以 ref_model=None。
    from peft import get_peft_model, prepare_model_for_kbit_training
    if adapter and Path(adapter).is_dir():
        target = PeftModel.from_pretrained(model, str(adapter), is_trainable=True)
    else:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
        target = get_peft_model(model, lora_config(args))
    ref_model = None
    target.config.use_cache = False

    targs = DPOConfig(
        output_dir=str(Path(args.out_dir) / "dpo"),
        per_device_train_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.dpo_epochs,
        learning_rate=args.dpo_lr,
        lr_scheduler_type="cosine",
        fp16=args.precision == "fp16",
        bf16=args.precision == "bf16",
        optim="paged_adamw_8bit",
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_prompt_length=args.max_length // 2,
        max_length=args.max_length,
        beta=args.dpo_beta,
        max_grad_norm=args.max_grad_norm,
        logging_steps=5,
        save_strategy="epoch",
        save_safetensors=False,
        report_to=[],
        remove_unused_columns=False,
        seed=args.seed,
    )
    data = HFDataset.from_list([{"prompt": r["prompt"], "chosen": r["chosen"],
                               "rejected": r["rejected"]} for r in rows])
    trainer = DPOTrainer(model=target, ref_model=ref_model, args=targs,
                         train_dataset=data, tokenizer=tok,
                         data_collator=make_dpo_pad_collator(tok))
    trainer.train()
    out = Path(args.out_dir) / "dpo_adapter"
    target.save_pretrained(out, safe_serialization=False)
    tok.save_pretrained(out)
    write_meta(out.parent / "run_dpo.json",
               {"stage": "dpo", "pairs": len(rows),
                "beta": args.dpo_beta, "epochs": args.dpo_epochs,
                "lr": args.dpo_lr,
                "drop_val_overlap": args.dpo_drop_val_overlap,
                "from_adapter": str(adapter) if adapter else None,
                "base": base_path})
    log(f"DPO adapter → {out}")


def make_dpo_pad_collator(tok):
    """TRL 的 collator 会顺手截掉一批 completion（已知的无意义截断）→ 这里只补不截。

    列名是 chosen/rejected 两套：input_ids_chosen / labels_rejected ……
    labels_* 必须用 -100 补，用 pad_token_id 补等于把 pad 当成长度惩罚的靶子。
    """
    class Collator:
        def __call__(self, features):
            import torch
            pad = tok.pad_token_id
            out = {}
            for k in features[0]:
                seqs = [list(f[k]) for f in features]
                m = max(len(s) for s in seqs)
                fill = -100 if "labels" in k else pad
                out[k] = torch.tensor([[fill] * (m - len(s)) + s for s in seqs])
            return out
    return Collator()


def generate_samples(args, adapter: Path) -> None:
    """训完立刻跑：一条样本并排 human / base / tuned 三块，风格纠偏要能肉眼看出来。

    两遍走（先未训基座、放掉显存再挂 adapter）：一份 4bit 7B 约 3.9GB，
    同时驻两份纯属给自己加 OOM 风险，而这两列又不需要同时在场。
    """
    import torch
    from peft import PeftModel

    pkg = find_package(args.data_dir)
    rows = read_jsonl(pkg / "sft_val.jsonl", SFT_KEYS)[:args.n_samples]
    prompts = [build_sft_prompt(r) for r in rows]
    got: dict[str, dict[str, str]] = {r["id"]: {} for r in rows}
    for name, with_adapter in (("base", False), ("tuned", True)):
        tok, mdl = load_base(args)
        if with_adapter:
            mdl = PeftModel.from_pretrained(mdl, str(adapter))
        mdl.config.use_cache = True  # load_base 为训练关掉了 KV cache；生成不开=每步重算全序列
        tok.padding_side = "left"  # 批量生成的标准做法
        for r, prompt in zip(rows, prompts):
            enc = tok(prompt, return_tensors="pt")
            enc = {k: v.to(mdl.device) for k, v in enc.items()}
            with torch.no_grad():
                ids = mdl.generate(
                    **enc, max_new_tokens=args.gen_max_tokens,
                    do_sample=True, temperature=args.gen_temperature,
                    top_p=0.95, pad_token_id=tok.pad_token_id)
            got[r["id"]][name] = tok.decode(
                ids[0][enc["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        del mdl, tok
        torch.cuda.empty_cache()
    out = Path(args.out_dir) / "samples.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            cols = got[r["id"]]
            f.write(json.dumps({"id": r["id"], "work": r.get("work"),
                                "human": r["output"], **cols,
                                "hit_max_tokens": len(cols["tuned"]) >= args.gen_max_tokens * 2},
                               ensure_ascii=False) + "\n")
    log(f"三块对照样本 → {out}（{len(rows)} 条；tuned=挂载 {adapter}）")


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["sft", "dpo", "both", "samples"], default="sft")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--data-dir", default=None,
                    help="含 lg_qlora_v1/ 的目录；Kaggle 用 /kaggle/input/<slug>")
    ap.add_argument("--out-dir", default=("/kaggle/working"
                                          if Path("/kaggle/working").is_dir()
                                          else "out/qlora"))
    ap.add_argument("--template", choices=["plain", "chat"], default="chat")
    ap.add_argument("--max-length", type=int, default=1536)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--max-grad-norm", type=float, default=0.3)
    ap.add_argument("--eval-steps", type=int, default=50)
    ap.add_argument("--save-steps", type=int, default=100)
    ap.add_argument("--lora-r", type=int, default=64)
    ap.add_argument("--lora-alpha", type=int, default=128)
    ap.add_argument("--lora-dropout", type=float, default=0.05)
    ap.add_argument("--precision", choices=["bf16", "fp16"], default=None,
                    help="缺省按 compute_cap 自动选")
    ap.add_argument("--fp16", action="store_true", help="强制 fp16（T4 或对照实验）")
    ap.add_argument("--tf32", action="store_true", help="仅 Ampere+ 生效")
    ap.add_argument("--attn", default="auto",
                    choices=["auto", "sdpa", "eager", "flash_attention_2", "off"])
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--dpo-epochs", type=int, default=1)
    ap.add_argument("--dpo-lr", type=float, default=5e-6)
    ap.add_argument("--dpo-beta", type=float, default=0.1)
    ap.add_argument("--dpo-merged", default=None,
                    help="DPO 基座=merge_and_unload 后重新 4bit 量化的目录（省 ~3.9GB）")
    ap.add_argument("--sft-adapter", default=None,
                    help="SFT 产出的 adapter 目录。单独跑 --stage dpo（换了 session）必须传，"
                         "否则会在基座上另起一个全新 LoRA，而不是接着 SFT 训")
    ap.add_argument("--dpo-drop-val-overlap", action="store_true",
                    help="剔除 prompt 段落落在 sft_val 的偏好对")
    ap.add_argument("--n-samples", type=int, default=12)
    ap.add_argument("--gen-max-tokens", type=int, default=256)
    ap.add_argument("--gen-temperature", type=float, default=0.8)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--resume", default=None, help="checkpoint 目录；缺省从头训")
    ap.add_argument("--dry-run", action="store_true",
                    help="只校验数据/模板/长度/硬件画像，不 import torch")
    return ap


def stack_available() -> tuple[bool, str]:
    try:
        import torch  # noqa: F401
        import peft  # noqa: F401
        import bitsandbytes  # noqa: F401
        import transformers  # noqa: F401
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    pkg = find_package(args.data_dir)
    if args.dry_run:
        return do_dry_run(pkg, args)
    ok, why = stack_available()
    if not ok:
        die(f"训练栈不齐（{why}）。本模板只在 Kaggle/有卡机器上跑；"
            "本地自检请用 --dry-run")
    plan = hardware_plan(gpu_profile(), args)
    args.precision = plan["precision"]
    log(f"硬件 {plan['gpus']} cc={plan['compute_cap']} 精度={args.precision} "
        f"attn={plan['attn_implementation']} tf32={plan['tf32']} {plan['ddp']}")
    for note in plan["notes"]:
        log(f"  · {note}")
    log(f"数据 {pkg} · 模型 {args.model} · 输出 {args.out_dir}")
    adapter = None
    if args.sft_adapter:
        if not Path(args.sft_adapter).is_dir():
            die(f"--sft-adapter 指向的目录不存在：{args.sft_adapter}"
                "（SFT 产物应在 <out-dir>/adapter）")
        adapter = Path(args.sft_adapter)
    elif args.stage == "dpo":
        log("警告：--stage dpo 未带 --sft-adapter → 在基座上全新起 LoRA，"
            "不是接着 SFT 训。想接着训请传 <out-dir>/adapter")
    if args.stage == "samples":
        if not adapter:
            die("--stage samples 必须带 --sft-adapter，指向要评的那个 adapter 目录")
        log(f"只出样：tuned = {adapter}")
        generate_samples(args, adapter)
        return 0
    if args.stage in ("sft", "both"):
        adapter = train_sft(args)
    if args.stage in ("dpo", "both"):
        train_dpo(args, adapter)
        adapter = Path(args.out_dir) / "dpo_adapter"
    if adapter:
        generate_samples(args, adapter)
    log(f"完成。记得 Kernel → Save Version 才能留住产物：{args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
