"""base vs tuned 同提示词逐条对比（finetune_min.py 的下游一棒）。

## 为什么单独一个脚本

「训完到底变没变」不能靠看 loss：loss 降的是**这批样本**上的似然，
不等于文本变好，也不等于风格变了。这里把口径钉死成**同提示词、同解码参数、
同随机种子**的逐条对照，让"变好/没变/变坏"肉眼可判：

* 同一个 model 实例、同一个 prompt 张量，**先在 `disable_adapter()` 里跑 base、
  再摘掉屏蔽跑 tuned**——两侧只差 LoRA 权重，连基座权重的浮点抖动都排除了；
* 解码固定贪心（`do_sample=False`、`num_beams=1`；`temperature=0` 记在
  `compare.json` 的 decode 段里，`generate` 不接受这个键，见 `generate_kwargs`）；
* 提示词来自**库里的新段**——凡在 `train_pairs.jsonl` 里出现过的
  `segment_id`（train 与 eval 两侧都算）一律排除，不拿训过的句子自证。

产物：`compare.json`（逐条 + 环境 + 口径）与 `compare.md`（可读版）。

    python scripts/finetune_compare.py --adapter <out>/adapter \
        --db <库> --train-pairs <out>/train_pairs.jsonl \
        --out-dir <out>/compare --n-prompts 6

**这份对照不承诺质量提升**：样本 ≤2000、基座 ≤1.5B、步数 ≤300，
它只是"管线真跑通了"的证据（见 `docs/FINETUNE_LOCAL.md` 的边界段）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import finetune_min as FM  # noqa: E402

DEFAULT_MODEL = FM.DEFAULT_MODEL
DEFAULT_OUT = Path("F:/lg_runs/finetune_min/compare")

# 解码口径（全脚本唯一出处，别处不许再传别的一套）
DECODE = {
    "temperature": 0.0,      # 0 = 贪心，不是"低温采样"
    "do_sample": False,
    "num_beams": 1,
    "top_p": 1.0,
    "top_k": 0,
    "repetition_penalty": 1.0,
}


def log(msg: str) -> None:
    print(f"[ftcmp] {msg}", flush=True)


def die(msg: str, code: int = 2) -> None:
    print(f"[ftcmp][FATAL] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def generate_kwargs(max_new_tokens: int) -> dict:
    """真正传给 `generate` 的参数。

    `temperature` / `top_p` / `top_k` 在 `do_sample=False` 下不参与计算，
    且新版 transformers 会校验它们（非采样模式下给非法值直接抛），
    所以只传贪心相关键；采样口径仍原样记进 `compare.json` 的 decode 段，
    免得"看起来没锁死"。
    """
    return {"do_sample": DECODE["do_sample"], "num_beams": DECODE["num_beams"],
            "max_new_tokens": max_new_tokens}


REWRITE_EVAL_INSTRUCTION = (
    "下面这段文字有 AI 味。把它改回自然的中文：保留原意与事实，"
    "不要添加新信息，不要写解释，只输出改后的正文。\n原句：{bad}"
)
FRAME_EVAL_INSTRUCTION = (
    "按下面的要点写一段中文小说正文，只写正文，不要解释、不要标题。\n"
    "事件：{event}\n人物意图：{intention}\n读者感受：{effect}"
)


def build_eval_prompts(con, exclude_segments: set[str], n_prompts: int,
                       seed: int = 20260930,
                       exclude_bench: bool = True) -> list[dict]:
    """从库里挑 n_prompts 条**没被训过**的段当提示词（确定性：固定 seed 洗牌）。

    一半"去 AI 味改写"（拿劣化版当输入、人类原文当参考答案），一半"帧→正文"
    （拿帧要点当指令）；两种模板都只用库里现成的 pair，不现场编造文本。
    """
    if n_prompts < 1:
        raise ValueError(f"n_prompts 必须 ≥1，收到 {n_prompts}")
    bench = FM.bench_text_hashes(con) if exclude_bench else set()
    rewrite: list[dict] = []
    if FM.table_exists(con, "controlled_corruptions"):
        for row in con.execute(
            "select cc.corruption_type, cc.variable, cc.text bad_text, "
            "s.id segment_id, s.work_id, s.text from controlled_corruptions cc "
            "join segments s on s.id=cc.segment_id "
            "where cc.status='ok' and cc.fact_consistent=1 and cc.drift_ok=1"
        ):
            rewrite.append({
                "kind": "rewrite_deai",
                "segment_id": row["segment_id"],
                "work_id": row["work_id"],
                "instruction": REWRITE_EVAL_INSTRUCTION.format(
                    bad=(row["bad_text"] or "").strip()),
                "reference": (row["text"] or "").strip(),
                "meta": {"corruption_type": row["corruption_type"],
                         "variable": row["variable"]},
            })
    frame: list[dict] = []
    if FM.table_exists(con, "frames"):
        for row in con.execute(
            "select f.payload, s.id segment_id, s.work_id, s.text "
            "from frames f join segments s on s.id=f.segment_id "
            "where f.status='ok'"
        ):
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except (TypeError, ValueError):
                continue
            instruction = FM._frame_instruction(payload)
            if not instruction:
                continue
            frame.append({
                "kind": "frame_to_text",
                "segment_id": row["segment_id"],
                "work_id": row["work_id"],
                "instruction": instruction,
                "reference": (row["text"] or "").strip(),
                "meta": {},
            })

    def _usable(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            if r["segment_id"] in exclude_segments:
                continue
            if not FM._target_ok(r["reference"], bench):
                continue
            if r["kind"] == "rewrite_deai" and (
                    not r["instruction"] or len(r["instruction"]) < 40):
                continue
            out.append(r)
        return out

    rewrite, frame = _usable(rewrite), _usable(frame)
    rng = random.Random(seed)
    rng.shuffle(rewrite)
    rng.shuffle(frame)
    picked: list[dict] = []
    i = j = 0
    while len(picked) < n_prompts and (i < len(rewrite) or j < len(frame)):
        if len(picked) % 2 == 0 and i < len(rewrite):
            picked.append(rewrite[i]); i += 1
        elif j < len(frame):
            picked.append(frame[j]); j += 1
        elif i < len(rewrite):
            picked.append(rewrite[i]); i += 1
    return picked[:n_prompts]


def adapter_fingerprint(adapter_dir: Path) -> dict:
    """adapter 目录指纹：文件清单 + 每个文件的 sha256（换权重必须换指纹）。"""
    p = Path(adapter_dir)
    if not p.exists():
        die(f"adapter 目录不存在：{p}")
    files = {}
    for f in sorted(p.rglob("*")):
        if f.is_file():
            files[str(f.relative_to(p))] = hashlib.sha256(
                f.read_bytes()).hexdigest()[:16]
    return {"path": str(p), "file_count": len(files), "files": files}


def render_markdown(payload: dict) -> str:
    """compare.md。纯函数：给定 payload 出文本（验收用例直接断渲染结果）。"""
    lines = ["# base vs tuned 同提示词对照", ""]
    dec = payload["decode"]
    lines.append(f"- 基座：`{payload['model']}`")
    lines.append(f"- adapter：`{payload['adapter']['path']}`"
                 f"（{payload['adapter']['file_count']} 个文件，"
                 f"指纹 `{payload['adapter_fingerprint']}`）")
    lines.append(f"- 解码：`do_sample={dec['do_sample']}`、`num_beams={dec['num_beams']}`、"
                 f"`temperature={dec['temperature']}`、`max_new_tokens={dec['max_new_tokens']}`"
                 f"、seed={payload['seed']}")
    lines.append(f"- device={payload['device']} dtype={payload['dtype']} "
                 f"耗时 {payload['elapsed_sec']}s")
    lines.append(f"- 提示词 {payload['n_prompts']} 条，全部来自库中新段"
                 f"（已排除 train_pairs.jsonl 里的 {payload['excluded_segment_ids']} 个段 id）")
    lines.append("")
    lines.append("> 本表**不承诺质量提升**：≤1.5B 基座 / ≤2000 样本 / ≤300 步的量级，"
                 "只能证明管线真跑通。人类原文只作 `reference` 参考，不是标准答案。")
    lines.append("")
    lines.append(f"逐条差异摘要：base 与 tuned 文本完全相同的 "
                 f"{payload['summary']['identical']}/{payload['n_prompts']} 条；"
                 f"长度中位数 base={payload['summary']['base_chars_median']} / "
                 f"tuned={payload['summary']['tuned_chars_median']}。")
    lines.append("")
    for item in payload["items"]:
        lines += [
            f"## {item['index']}. `{item['segment_id']}`"
            f"（{item['kind']}，work={item['work_id']}）",
            "",
            "**提示词**",
            "",
            "```text",
            item["prompt_text"],
            "```",
            "",
            "**base**",
            "",
            "```text",
            item["base_text"],
            "```",
            "",
            "**tuned**",
            "",
            "```text",
            item["tuned_text"],
            "```",
            "",
            f"**reference（人类原文）**：{item['reference']}",
            "",
            f"- 相同：{'是' if item['identical'] else '否'}；"
            f"base {len(item['base_text'])} 字 / tuned {len(item['tuned_text'])} 字 / "
            f"reference {len(item['reference'])} 字",
            "",
        ]
    return "\n".join(lines) + "\n"


def _median(xs: list[int]) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    n = len(s)
    return int(s[n // 2]) if n % 2 else int((s[n // 2 - 1] + s[n // 2]) / 2)


def compare(adapter_dir: Path, out_dir: Path, db: Path, train_pairs: Path,
            model: str = DEFAULT_MODEL, n_prompts: int = 6,
            max_new_tokens: int = 96, seed: int = 20260930,
            device: str = "auto", cache_root: Path = FM.DEFAULT_CACHE_ROOT,
            max_len: int = 512, exclude_bench: bool = True) -> dict:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    FM.apply_cache_env(FM.cache_env(cache_root))
    adapter_dir = Path(adapter_dir)
    out_dir = FM.guard_out_dir(out_dir)
    fingerprint = adapter_fingerprint(adapter_dir)
    log(f"adapter 指纹 {fingerprint['file_count']} 文件 "
        f"{hashlib.sha256(json.dumps(fingerprint['files'], sort_keys=True).encode()).hexdigest()[:16]}")

    exclude: set[str] = set()
    if train_pairs and Path(train_pairs).exists():
        for row in FM.read_pairs_jsonl(Path(train_pairs)):
            if row.get("segment_id"):
                exclude.add(row["segment_id"])
    log(f"排除已训段 id：{len(exclude)}")

    con = FM.open_db_ro(db)
    try:
        prompts = build_eval_prompts(con, exclude, n_prompts, seed=seed,
                                     exclude_bench=exclude_bench)
    finally:
        con.close()
    if not prompts:
        die("库里挑不出可用提示词（段都被训过/被基准排除/长度不合格？）")
    log(f"提示词 {len(prompts)} 条："
        f"{ {k: sum(1 for p in prompts if p['kind'] == k) for k in {p['kind'] for p in prompts}} }")

    cfg = FM.TrainConfig(device=device, dtype="auto", max_len=max_len)
    dev, dtype_name = FM.resolve_device_dtype(cfg, torch)
    FM.set_seed(seed, torch)
    dtype = FM.torch_dtype(dtype_name, torch)

    tok_src = adapter_dir if (adapter_dir / "tokenizer_config.json").exists() else None
    tok = AutoTokenizer.from_pretrained(str(tok_src) if tok_src else model)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    log(f"device={dev} dtype={dtype_name} tokenizer={tok_src or model}")

    base = FM._load_base(model, dtype, dev, torch)
    base.config.use_cache = True
    tmodel = PeftModel.from_pretrained(base, str(adapter_dir))
    tmodel.eval()

    items: list[dict] = []
    t0 = time.perf_counter()
    for i, p in enumerate(prompts):
        prompt_text, mode = FM.build_prompt(tok, p["instruction"])
        ids = tok(prompt_text, return_tensors="pt",
                  add_special_tokens=False)["input_ids"].to(dev)
        gk = generate_kwargs(max_new_tokens)
        with torch.no_grad():
            with tmodel.disable_adapter():
                base_out = tmodel.generate(input_ids=ids, **gk)
            tuned_out = tmodel.generate(input_ids=ids, **gk)
        b_txt = tok.decode(base_out[0][ids.shape[1]:], skip_special_tokens=True)
        t_txt = tok.decode(tuned_out[0][ids.shape[1]:], skip_special_tokens=True)
        items.append({
            "index": i + 1, "kind": p["kind"], "segment_id": p["segment_id"],
            "work_id": p["work_id"], "prompt_mode": mode,
            "prompt_text": prompt_text, "instruction": p["instruction"],
            "base_text": b_txt, "tuned_text": t_txt,
            "reference": p["reference"],
            "identical": b_txt == t_txt, "meta": p["meta"],
        })
        log(f"[{i + 1}/{len(prompts)}] {p['kind']} {p['segment_id']} "
            f"identical={b_txt == t_txt} base={len(b_txt)}字 tuned={len(t_txt)}字")
    elapsed = round(time.perf_counter() - t0, 2)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": "scripts/finetune_compare.py",
        "model": model,
        "adapter": {"path": str(adapter_dir), "file_count": fingerprint["file_count"]},
        "adapter_fingerprint": hashlib.sha256(
            json.dumps(fingerprint["files"], sort_keys=True).encode()).hexdigest()[:16],
        "decode": dict(DECODE, max_new_tokens=max_new_tokens),
        "generate_kwargs": generate_kwargs(max_new_tokens),
        "seed": seed,
        "device": dev,
        "dtype": dtype_name,
        "elapsed_sec": elapsed,
        "n_prompts": len(items),
        "excluded_segment_ids": len(exclude),
        "env": FM.environment_snapshot(torch, dev, dtype_name),
        "summary": {
            "identical": sum(1 for x in items if x["identical"]),
            "differ": sum(1 for x in items if not x["identical"]),
            "base_chars_median": _median([len(x["base_text"]) for x in items]),
            "tuned_chars_median": _median([len(x["tuned_text"]) for x in items]),
            "by_kind": {k: sum(1 for x in items if x["kind"] == k)
                        for k in sorted({x["kind"] for x in items})},
        },
        "disclaimer": ("不承诺质量提升：小基座小样本的量级只能作为管线证据；"
                       "reference 是人类原文，仅供参照。"),
        "items": items,
    }
    FM.write_json(out_dir / "compare.json", payload)
    (out_dir / "compare.md").write_text(render_markdown(payload), encoding="utf-8")
    log(f"写出 {out_dir / 'compare.json'} 与 compare.md"
        f"（identical {payload['summary']['identical']}/{len(items)}）")
    return payload


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="base vs tuned 同提示词逐条对比（固定贪心解码）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--adapter", type=Path, required=True, help="LoRA adapter 目录")
    p.add_argument("--base-model", dest="model", default=DEFAULT_MODEL)
    p.add_argument("--db", type=Path, default=FM.DEFAULT_DB)
    p.add_argument("--train-pairs", type=Path, default=None,
                   help="finetune_min 写出的 train_pairs.jsonl（用来排除已训段）")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--cache-root", type=Path, default=FM.DEFAULT_CACHE_ROOT)
    p.add_argument("--n-prompts", type=int, default=6)
    p.add_argument("--max-new-tokens", type=int, default=96)
    p.add_argument("--max-len", type=int, default=512,
                   help="提示词编码上限（超长直接截）")
    p.add_argument("--seed", type=int, default=FM.TrainConfig.seed)
    p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    p.add_argument("--no-exclude-bench", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    FM._utf8_stdout()
    args = build_parser().parse_args(argv)
    if args.n_prompts < 1:
        die("--n-prompts 必须 ≥1")
    if args.max_new_tokens < 1:
        die("--max-new-tokens 必须 ≥1")
    payload = compare(
        adapter_dir=args.adapter, out_dir=args.out_dir, db=args.db,
        train_pairs=args.train_pairs, model=args.model,
        n_prompts=args.n_prompts, max_new_tokens=args.max_new_tokens,
        seed=args.seed, device=args.device, cache_root=args.cache_root,
        max_len=args.max_len, exclude_bench=not args.no_exclude_bench)
    s = payload["summary"]
    log(f"摘要：identical={s['identical']} differ={s['differ']} "
        f"base 中位 {s['base_chars_median']} 字 / tuned 中位 {s['tuned_chars_median']} 字")
    log(payload["disclaimer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())