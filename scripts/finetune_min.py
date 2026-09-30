"""本机微调最小闭环（Runtime 后置项「微调」，2026-09-30）。

## 这件东西是什么

`docs/HANDOVER.md` §8 记的是「本机无 GPU、也没有微调管线 ⇒ §53 六条成功标准
一条都测不了」。**现在本机有卡**（RTX 3050 Laptop, 4096 MiB），但可用显存只有
2~3 GiB——**7B 级 QLoRA 在本机跑不了**，那条路仍然归 `docs/kaggle-qlora-notebook.md`。

所以本脚本只做一件事：**把「有管线、能真训练、能出前后对比」做成可复现的真跑证据**，
并且把规模上限写死在配置和日志里（小基座 ≤1.5B，样本 ≤2000 条，步数 ≤300）。
**它不是 §53 六条标准的证据**，也不承诺质量提升——见 `docs/FINETUNE_LOCAL.md`。

## 数据从哪来（项目产物，不现编）

只读 `language_genome.db`，两种「风格化指令对」，目标文本**一律是人类原文**
（与 `docs/training-feasibility-20260919.md` §1 的口径一致：目标=人类原文）：

* 模板 A `frame_to_text`：`frames.payload` 的要点（事件/意图/读者感受）→ 段原文；
* 模板 B `rewrite_deai`：AI 劣化版（`controlled_corruptions.text`）+ 病灶说明
  （`variable`）→ 同一段的**人类原文**（学「把 AI 味改回去」）；
* 模板 C `ai_to_human`：`candidates.text`（本项目 AI 侧真实产物）→ 人类原文。
  `candidates` 没有 fact_consistent 字段，所以自己加一道**内容重合闸**：
  二元组 Jaccard < `MIN_OVERLAP` 的候选不进（漂移过的候选会教模型编事实）。

入池闸门照抄仓库既有纪律，不另立口径：

* `integrity.src_ok` 必须是 JSON 布尔 `true`（`k2_extract_backfill.src_ok_state`
  的三态镜像；未校验/字符串 "false"/非字典一律不过，fail-closed）；
* `segments.role == 'benchmark'` 排除（`export_training.py` 同口径）；
* 基准文本重合按忽略空白哈希排除（`export_training._bench_hashes` 同口径：
  a/b 侧无长度下限，context 及其 `\n\n` 组成段 ≥50 字才算）；
* txt 水印伪影（拼音替换 / 站点水印）按 `make_random_batch.looks_watermarked`
  同口径排除——只污染人类侧，留着等于教模型学乱码；
* 目标文本长度落在 `[MIN_TARGET_CHARS, MAX_TARGET_CHARS]`，超长样本会让 0.5B
  基座在 4 GiB 卡上 OOM，长样本直接不进。

最后留 `--eval-size` 条**不参与训练**做 held-out，训练前后各评一次 loss
（`train_log.json` 的 `eval_loss_before/after`）——这比"训练 loss 下降"更难自欺。

## 缓存与落盘纪律（硬边界）

* `HF_HOME` / `TORCH_HOME` / `PIP_CACHE_DIR` 一律指到 `F:/Hermes/cache/...`
  （`--cache-root` 可改，但 `guard_path` 会拒 C 盘）；
* `--out-dir` **必须**在 F:/G: 盘且**不得在检出内**——产物（adapter、
  `train_log.json`、`train_pairs.jsonl`）不进版本库，也不留在 worktree 里；
* `--dry-run` **全程不 import torch / transformers**（同 `kaggle_qlora.py`），
  只校验数据口径、模板拼接和配置，验收脚本 `tests/test_finetune_min.py`
  在没装训练栈的 venv 里也跑得动。

## 用法

数据 + 训练（GPU）：

    python scripts/finetune_min.py --stage all --db <库> \
        --out-dir F:/lg_runs/ft01 --model Qwen/Qwen2.5-0.5B-Instruct \
        --steps 60 --max-pairs 800 --micro-batch 1 --grad-accum 8

显存不够时的 CPU 兜底（同一脚本，步数可以降到 ≥20）：

    python scripts/finetune_min.py --stage all --device cpu --steps 20 \
        --out-dir F:/lg_runs/ft02 --max-len 192

不装训练栈也能验数据与配置：

    python scripts/finetune_min.py --stage data --dry-run --db <库> \
        --out-dir F:/lg_runs/ft03 --max-pairs 200
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:            # 裸 SQL 读 integrity 紧凑码要 import app.*
    sys.path.insert(0, str(ROOT))

DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
DEFAULT_DB = ROOT / "data" / "language_genome.db"
DEFAULT_CACHE_ROOT = Path("F:/Hermes/cache/hf")

# 规模上限写死在代码里，不靠"使用者自觉"——本机 4 GiB 卡的物理边界。
MAX_PAIRS_HARD_CAP = 2000      # 任务书：≤2000 条
MAX_STEPS_HARD_CAP = 300       # 任务书：默认 ≤300 步
MAX_MODEL_PARAM_HINT = 1.5     # B：建议基座 ≤1.5B

ALLOWED_DRIVES = frozenset({"F:", "G:"})
MAX_LEN_BOUNDS = (32, 1024)
EVAL_SIZE_BOUNDS = (1, 256)

MIN_TARGET_CHARS = 20
MAX_TARGET_CHARS = 400
# 候选文本没有 fact_consistent 字段 → 用二元组 Jaccard 兜住"事实漂移"：
# 与人类原文重合太低的对子不进训练（实测中位数 0.74、p10 0.39）。
MIN_OVERLAP = 0.30

# 与 kaggle_qlora.py 同款（Qwen/Llama 系注意力+MLP 投影名）
LORA_TARGETS = ("q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj")
# frame_schema.json 实测到 intention / intent 两种键并存（导出侧字段漂移）
INTENT_KEYS = ("intention", "intent")

# ── 语料水印伪影（抄 make_random_batch.py 同口径，2026-09-17 定的规则）────
# 盗版 txt 把随机汉字换成拼音、夹站点水印；只污染人类侧那一列。
_LATIN = re.compile(r"[A-Za-z]")
_ACCENT = re.compile(r"[āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]")
_WM_PUNCT = re.compile(r"[一-鿿][~{}]|[~{}][一-鿿]")

TEMPLATE_A = "frame_to_text"
TEMPLATE_B = "rewrite_deai"
TEMPLATE_C = "ai_to_human"
ALL_TEMPLATES = (TEMPLATE_A, TEMPLATE_B, TEMPLATE_C)


def log(msg: str) -> None:
    print(f"[ftmin] {msg}", flush=True)


def die(msg: str, code: int = 2) -> None:
    print(f"[ftmin][FATAL] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _utf8_stdout() -> None:
    """Windows 控制台中文兜底（同 strategy_evidence_audit.py）。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


# ── 落盘/缓存纪律 ──────────────────────────────────────────────────

def cache_env(cache_root: Path | str = DEFAULT_CACHE_ROOT,
              pip_cache: Path | str = "F:/Hermes/cache/pip") -> dict[str, str]:
    """训练栈要用的环境变量。**一律指到 F:/G:**，绝不让 huggingface/torch
    往 C:\\Users\\...\\.cache 里写。"""
    root = Path(cache_root)
    guard_path(root, what="cache-root")
    pip = Path(pip_cache)
    guard_path(pip, what="pip-cache")
    hub = root / "hub"
    return {
        "HF_HOME": str(root),
        "HF_HUB_CACHE": str(hub),
        "TRANSFORMERS_CACHE": str(hub),
        "TORCH_HOME": str(Path("F:/Hermes/cache/torch")),
        "PIP_CACHE_DIR": str(pip),
        "TOKENIZERS_PARALLELISM": "false",
    }


def apply_cache_env(env: dict[str, str] | None = None) -> dict[str, str]:
    env = env or cache_env()
    os.environ.update(env)
    return env


def drive_of(path: Path) -> str:
    drive = os.path.splitdrive(str(Path(path).resolve()))[0]
    return drive.upper()


def guard_path(path: Path | str, what: str = "path") -> Path:
    """硬闸：路径必须在 F:/G: 盘，且**不得落在检出内**。

    两条纪律各挡一类事故：
    ① 「绝不落 C 盘」——模型/数据/缓存/产物一律 F: 或 G:；
    ② 产物不进版本库——`--out-dir` 指向检出内会在 worktree 里留下未跟踪文件
       （本仓库 .gitignore 只挡 `data/`、`*.db` 这类，挡不住任意 jsonl/adapter）。
    """
    p = Path(path)
    drive = drive_of(p)
    if drive not in ALLOWED_DRIVES:
        raise ValueError(
            f"{what} 必须落在 F:/G: 盘（硬边界：禁止写 C 盘），收到 {p}（盘符 {drive or '无'}）")
    try:
        inside = p.resolve().is_relative_to(ROOT.resolve())
    except (OSError, ValueError):
        inside = False
    if inside:
        raise ValueError(
            f"{what} 不得落在检出内（产物不进版本库）：{p}（检出根 {ROOT}）")
    return p


def guard_out_dir(path: Path | str) -> Path:
    return guard_path(path, what="--out-dir")


# ── 文本/基准隔离 ──────────────────────────────────────────────────

def norm_text(t: str | None) -> str:
    return "".join((t or "").split())


def content_hash(t: str | None) -> str:
    return hashlib.md5(norm_text(t).encode("utf-8")).hexdigest()


def bigram_jaccard(a: str | None, b: str | None, n: int = 2) -> float:
    """二元组 Jaccard：估"两段讲的是不是同一件事"，给无 fact 标记的候选兜底。"""
    ta, tb = norm_text(a), norm_text(b)
    if len(ta) < n or len(tb) < n:
        return 0.0
    ga = {ta[i:i + n] for i in range(len(ta) - n + 1)}
    gb = {tb[i:i + n] for i in range(len(tb) - n + 1)}
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


def looks_watermarked(text: str) -> bool:
    """与 make_random_batch.looks_watermarked 同口径（规则抄自那里的注释）。"""
    t = text or ""
    return bool(_ACCENT.search(t)) or len(_LATIN.findall(t)) >= 2 \
        or bool(_WM_PUNCT.search(t))


def integrity_src_ok_state(integrity: str | None) -> bool | None:
    """`integrity.src_ok` 三态：True / False / None（未校验）。

    fail-closed 语义同 `k2_extract_backfill.src_ok_state` + `app.source_check`：
    缺键、值非布尔（含字符串 "false"）、integrity 非 JSON、解析失败 → None（不过闸）。

    先过 `app.segment_integrity.canonical_json` 再解 JSON——库里 2026-09-30 起
    并存**紧凑编码**行（`i1:…`，`CompactIntegrity` 读侧就是先还原再给读者）。
    裸 SQL 拿到的正是紧凑串：不还原就在 `json.loads` 处判"坏 JSON"，
    与"键缺失=未校验"结果同为 None 但理由不同（`k2_extract_backfill.integrity_dict`
    的注释点名过这条静默歧义）。紧凑码只编 8 个基键、不含 src_ok，
    所以还原后依旧不过闸——本函数的作用是把"为什么不过"写对，不放宽口径。
    """
    if not integrity:
        return None
    raw = integrity
    try:
        from app.segment_integrity import canonical_json
        raw = canonical_json(integrity)
    except Exception:                            # app 不在路径上：按原样解，口径不放宽
        pass
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    v = payload.get("src_ok")
    return v if isinstance(v, bool) else None


def open_db_ro(db_path: Path | str) -> sqlite3.Connection:
    """只读开库。生产库 52 GiB 且被 live 侧持有，**绝不能被写**。"""
    p = Path(db_path)
    if not p.exists():
        die(f"库不存在：{p}（用 --db 指定 language_genome.db）")
    uri = f"file:{p.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def bench_text_hashes(con: sqlite3.Connection) -> set[str]:
    """基准冻结文本的内容哈希集（口径抄 `export_training._bench_hashes`）。

    a/b 是判别题两侧本体：**无长度下限**；context 及其 `\n\n` 组成段 ≥50 字才算。
    空集不静默放行——调用方 `extract_pairs` 见到空集会在日志里标出来。
    """
    hs: set[str] = set()

    def add_any(t: str | None) -> None:
        if t:
            hs.add(content_hash(t))

    def add_long(t: str | None) -> None:
        if len(norm_text(t)) >= 50:
            hs.add(content_hash(t))

    if not table_exists(con, "benchmark_items"):
        return hs
    for row in con.execute("select text_a, text_b, context from benchmark_items"):
        add_any(row["text_a"])
        add_any(row["text_b"])
        add_long(row["context"])
        for piece in re.split(r"\n\s*\n", row["context"] or ""):
            add_long(piece)
    return hs


# ── 指令对构造 ────────────────────────────────────────────────────

def _target_ok(text: str | None, bench: set[str]) -> bool:
    t = (text or "").strip()
    if not (MIN_TARGET_CHARS <= len(t) <= MAX_TARGET_CHARS):
        return False
    if looks_watermarked(t):
        return False
    if bench and content_hash(t) in bench:
        return False
    return True


def _frame_instruction(payload: dict) -> str | None:
    """帧要点 → 指令。要点缺失就不成对（宁可少一条，不要编）。"""
    if not isinstance(payload, dict):
        return None
    event = (payload.get("event") or "").strip()
    intention = ""
    for k in INTENT_KEYS:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            intention = v.strip()
            break
    effect = (payload.get("reader_effect") or "").strip()
    if not event and not intention and not effect:
        return None
    lines = ["按下面的要点写一段中文小说正文，只写正文，不要解释、不要标题。"]
    if event:
        lines.append(f"事件：{event}")
    if intention:
        lines.append(f"人物意图：{intention}")
    if effect:
        lines.append(f"读者感受：{effect}")
    return "\n".join(lines)


REWRITE_INSTRUCTION = (
    "下面这段文字有 AI 味{pin}。把它改回自然的中文：保留原意与事实，"
    "不要添加新信息，不要写解释，只输出改后的正文。{pin}\n原句：{bad}"
)


AI_TO_HUMAN_INSTRUCTION = (
    "下面这段是机器写的，读着有 AI 味。把它改回自然的中文：保留原意与事实，"
    "不要添加新信息，不要写解释，只输出改后的正文。\n机器稿：{ai}"
)


def _rewrite_instruction(bad_text: str, variable: str) -> str:
    desc = (variable or "").strip()
    pin = f"（{desc}）" if desc else ""
    return REWRITE_INSTRUCTION.format(pin=pin, bad=bad_text)


def extract_pairs(con: sqlite3.Connection, limit: int = 800, seed: int = 20260930,
                  exclude_bench: bool = True,
                  eval_size: int = 32) -> tuple[list[dict], dict]:
    """从项目产物抽 ≤limit 条风格化指令对；尾部 eval_size 条划成 held-out。

    返回 (pairs, stats)。每条 pair：
      {id, split, template, instruction, output, segment_id, work_id,
       source, meta}
    确定性：同一 (limit, seed, exclude_bench, eval_size) 必得同一结果——
    打散用固定 `random.Random(seed)`，不碰全局 random。
    """
    if limit < 1:
        raise ValueError(f"limit 必须 ≥1，收到 {limit}")
    if limit > MAX_PAIRS_HARD_CAP:
        raise ValueError(
            f"limit 超过硬上限 {MAX_PAIRS_HARD_CAP}（任务书：≤2000 条），收到 {limit}")
    if not (EVAL_SIZE_BOUNDS[0] <= eval_size <= min(EVAL_SIZE_BOUNDS[1], limit - 1)):
        raise ValueError(
            f"eval_size 必须落在 [{EVAL_SIZE_BOUNDS[0]}, "
            f"{min(EVAL_SIZE_BOUNDS[1], limit - 1)}]，收到 {eval_size}")

    bench = bench_text_hashes(con) if exclude_bench else set()
    stats: dict = {
        "limit": limit, "seed": seed, "exclude_bench": exclude_bench,
        "bench_hash_count": len(bench),
        "seen": {}, "kept": {TEMPLATE_A: 0, TEMPLATE_B: 0},
        "skipped": {"src_ok_not_true": 0, "benchmark_role": 0, "target_len": 0,
                    "watermark": 0, "bench_overlap": 0, "no_instruction": 0,
                    "dup_segment": 0, "low_overlap": 0},
    }

    def _bump(bucket: str) -> None:
        stats["skipped"][bucket] += 1

    def _admissible(row) -> bool:
        if (row["role"] or "") == "benchmark":
            _bump("benchmark_role")
            return False
        if integrity_src_ok_state(row["integrity"]) is not True:
            _bump("src_ok_not_true")
            return False
        text = (row["text"] or "").strip()
        if not (MIN_TARGET_CHARS <= len(text) <= MAX_TARGET_CHARS):
            _bump("target_len")
            return False
        if looks_watermarked(text):
            _bump("watermark")
            return False
        if bench and content_hash(text) in bench:
            _bump("bench_overlap")
            return False
        return True

    a_rows: list[dict] = []
    if table_exists(con, "frames"):
        for row in con.execute(
            "select f.id frame_id, f.payload, s.id segment_id, s.work_id, s.text, "
            "s.integrity, s.role from frames f join segments s on s.id=f.segment_id "
            "where f.status='ok'"
        ):
            stats["seen"][TEMPLATE_A] = stats["seen"].get(TEMPLATE_A, 0) + 1
            if not _admissible(row):
                continue
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except (TypeError, ValueError):
                payload = {}
            instruction = _frame_instruction(payload)
            if not instruction:
                _bump("no_instruction")
                continue
            a_rows.append({
                "id": f"FT-{row['frame_id']}",
                "template": TEMPLATE_A,
                "instruction": instruction,
                "output": (row["text"] or "").strip(),
                "segment_id": row["segment_id"],
                "work_id": row["work_id"],
                "source": "frames.payload → segments.text",
                "meta": {"corruption_type": None, "variable": None},
            })

    b_rows: list[dict] = []
    if table_exists(con, "controlled_corruptions"):
        for row in con.execute(
            "select cc.id cc_id, cc.corruption_type, cc.variable, "
            "cc.text bad_text, s.id segment_id, s.work_id, s.text, s.integrity, "
            "s.role from controlled_corruptions cc "
            "join segments s on s.id=cc.segment_id "
            "where cc.status='ok' and cc.fact_consistent=1 and cc.drift_ok=1"
        ):
            stats["seen"][TEMPLATE_B] = stats["seen"].get(TEMPLATE_B, 0) + 1
            if not _admissible(row):
                continue
            bad = (row["bad_text"] or "").strip()
            if not bad or content_hash(bad) == content_hash(row["text"]):
                _bump("no_instruction")
                continue
            b_rows.append({
                "id": f"FT-{row['cc_id']}",
                "template": TEMPLATE_B,
                "instruction": _rewrite_instruction(
                    bad, row["variable"]),
                "output": (row["text"] or "").strip(),
                "segment_id": row["segment_id"],
                "work_id": row["work_id"],
                "source": "controlled_corruptions.text → segments.text",
                "meta": {"corruption_type": row["corruption_type"],
                         "variable": row["variable"]},
            })

    # 模板 B/C 全量先取（它们是"去 AI 味"这条项目主线信号），余量给 A
    c_rows: list[dict] = []
    if table_exists(con, "candidates"):
        for row in con.execute(
            "select c.id cand_id, c.text ai_text, c.model, s.id segment_id, "
            "s.work_id, s.text, s.integrity, s.role from candidates c "
            "join segments s on s.id=c.segment_id where c.status='ok'"
        ):
            stats["seen"][TEMPLATE_C] = stats["seen"].get(TEMPLATE_C, 0) + 1
            if not _admissible(row):
                continue
            ai = (row["ai_text"] or "").strip()
            human = (row["text"] or "").strip()
            if not ai or content_hash(ai) == content_hash(human):
                _bump("no_instruction")
                continue
            overlap = bigram_jaccard(ai, human)
            if overlap < MIN_OVERLAP:
                _bump("low_overlap")
                continue
            c_rows.append({
                "id": f"FT-{row['cand_id']}",
                "template": TEMPLATE_C,
                "instruction": AI_TO_HUMAN_INSTRUCTION.format(ai=ai),
                "output": human,
                "segment_id": row["segment_id"],
                "work_id": row["work_id"],
                "source": "candidates.text → segments.text",
                "meta": {"generator_model": row["model"],
                         "overlap_jaccard": round(overlap, 4)},
            })

    picked: list[dict] = []
    used_segments: set[str] = set()
    for row in b_rows + c_rows + a_rows:
        if len(picked) >= limit:
            break
        if row["segment_id"] in used_segments:
            stats["skipped"]["dup_segment"] += 1
            continue
        used_segments.add(row["segment_id"])
        picked.append(row)
        stats["kept"][row["template"]] = stats["kept"].get(row["template"], 0) + 1

    random.Random(seed).shuffle(picked)
    for i, row in enumerate(picked):
        row["split"] = "eval" if i < eval_size else "train"
    stats["pairs_total"] = len(picked)
    stats["train"] = sum(1 for r in picked if r["split"] == "train")
    stats["eval"] = sum(1 for r in picked if r["split"] == "eval")
    stats["pool"] = {TEMPLATE_A: len(a_rows), TEMPLATE_B: len(b_rows),
                     TEMPLATE_C: len(c_rows)}
    return picked, stats


# ── 训练配置 ──────────────────────────────────────────────────────

@dataclass
class TrainConfig:
    model: str = DEFAULT_MODEL
    out_dir: Path = Path("F:/lg_runs/finetune_min")
    db: Path = DEFAULT_DB
    cache_root: Path = DEFAULT_CACHE_ROOT
    steps: int = 60
    lr: float = 2e-4
    micro_batch: int = 1
    grad_accum: int = 8
    max_len: int = 256
    max_pairs: int = 800
    eval_size: int = 32
    seed: int = 20260930
    device: str = "auto"
    dtype: str = "auto"
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    warmup_ratio: float = 0.1
    weight_decay: float = 0.0
    grad_clip: float = 1.0
    log_every: int = 1
    gradient_checkpointing: bool = True

    def as_dict(self) -> dict:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}


def validate_config(cfg: TrainConfig) -> TrainConfig:
    """纯逻辑校验，**不 import torch**（--dry-run 与验收用例都靠它）。

    拒绝的每一条都对应一次真事故或一次硬边界，不是洁癖：
    steps>300 / max_pairs>2000 是任务书写死的规模上限；
    CPU+fp16 会让训练在中途炸（fp16 算子在 CPU 上大面积缺失）；
    dtype/device 拼错只能等到 import 后才炸，不如当场响。
    """
    if not (1 <= cfg.steps <= MAX_STEPS_HARD_CAP):
        raise ValueError(
            f"steps 必须落在 [1, {MAX_STEPS_HARD_CAP}]（任务书上限），收到 {cfg.steps}")
    if cfg.micro_batch < 1:
        raise ValueError(f"micro_batch 必须 ≥1，收到 {cfg.micro_batch}")
    if cfg.grad_accum < 1:
        raise ValueError(f"grad_accum 必须 ≥1，收到 {cfg.grad_accum}")
    if not (MAX_LEN_BOUNDS[0] <= cfg.max_len <= MAX_LEN_BOUNDS[1]):
        raise ValueError(
            f"max_len 必须落在 {list(MAX_LEN_BOUNDS)}，收到 {cfg.max_len}")
    if not (0.0 < cfg.lr <= 1.0):
        raise ValueError(f"lr 必须落在 (0, 1]，收到 {cfg.lr}")
    if not (1 <= cfg.max_pairs <= MAX_PAIRS_HARD_CAP):
        raise ValueError(
            f"max_pairs 必须落在 [1, {MAX_PAIRS_HARD_CAP}]（任务书：≤2000 条），"
            f"收到 {cfg.max_pairs}")
    if not (EVAL_SIZE_BOUNDS[0] <= cfg.eval_size <= cfg.max_pairs - 1):
        raise ValueError(
            f"eval_size 必须落在 [{EVAL_SIZE_BOUNDS[0]}, {cfg.max_pairs - 1}]，"
            f"收到 {cfg.eval_size}")
    if cfg.lora_r < 1 or cfg.lora_alpha < 1:
        raise ValueError(
            f"lora_r/lora_alpha 必须 ≥1，收到 {cfg.lora_r}/{cfg.lora_alpha}")
    if not (0.0 <= cfg.warmup_ratio <= 0.5):
        raise ValueError(
            f"warmup_ratio 必须落在 [0, 0.5]，收到 {cfg.warmup_ratio}")
    if cfg.grad_clip < 0:
        raise ValueError(f"grad_clip 必须 ≥0，收到 {cfg.grad_clip}")
    if cfg.log_every < 1:
        raise ValueError(f"log_every 必须 ≥1，收到 {cfg.log_every}")
    if cfg.device not in ("auto", "cpu", "cuda"):
        raise ValueError(f"device 只能是 auto/cpu/cuda，收到 {cfg.device!r}")
    if cfg.dtype not in ("auto", "fp32", "fp16", "bf16"):
        raise ValueError(f"dtype 只能是 auto/fp32/fp16/bf16，收到 {cfg.dtype!r}")
    if cfg.device == "cpu" and cfg.dtype == "fp16":
        raise ValueError("CPU 上不要 fp16（CPU 端 fp16 算子大面积缺失，会训到一半炸）")
    if not cfg.model.strip():
        raise ValueError("model 不能为空")
    guard_out_dir(cfg.out_dir)
    guard_path(cfg.cache_root, what="--cache-root")
    return cfg


def resolve_device_dtype(cfg: TrainConfig, torch_mod) -> tuple[str, str]:
    """auto 的落法写死在这里，别处不许再猜：cuda 有 bf16 → bf16（免 GradScaler），
    否则 fp16；CPU → fp32。返回 (device, dtype 名)。"""
    device = cfg.device
    if device == "auto":
        device = "cuda" if torch_mod.cuda.is_available() else "cpu"
    dtype = cfg.dtype
    if dtype == "auto":
        if device == "cpu":
            dtype = "fp32"
        elif torch_mod.cuda.is_bf16_supported():
            dtype = "bf16"
        else:
            dtype = "fp16"
    if device == "cpu" and dtype == "fp16":
        raise ValueError("CPU 上不要 fp16")
    return device, dtype


def torch_dtype(name: str, torch_mod):
    return {"fp32": torch_mod.float32, "fp16": torch_mod.float16,
            "bf16": torch_mod.bfloat16}[name]


# ── 编码（只学 completion，prompt 全 -100）─────────────────────────

def fallback_prompt(instruction: str) -> str:
    return f"### 指令\n{instruction}\n### 回答\n"


def build_prompt(tok, instruction: str) -> tuple[str, str]:
    """返回 (prompt_text, 模式)。基座没 chat template 时走纯文本模板。

    两种模式都记进 train_log.json——训练/对比两侧必须同一种，
    否则 tuned 的"变好"可能只是模板对不上。
    """
    try:
        text = tok.apply_chat_template(
            [{"role": "user", "content": instruction}],
            tokenize=False, add_generation_prompt=True)
        if text:
            return text, "chat_template"
    except Exception:      # 缺 template / 老版 tokenizer
        pass
    return fallback_prompt(instruction), "fallback_text"


def encode_pair(tok, pair: dict, max_len: int) -> tuple[list[int], list[int]]:
    """→ (input_ids, labels)。labels 只保留 completion 段（prompt 全 -100）。

    截断时**砍 prompt 头、保 completion 尾**：要学的是输出侧，
    把输出砍掉等于这一轮白跑。
    """
    prompt, _ = build_prompt(tok, pair["instruction"])
    eos = tok.eos_token or ""
    full = prompt + pair["output"] + eos
    pid = tok(prompt, add_special_tokens=False)["input_ids"]
    fid = tok(full, add_special_tokens=False)["input_ids"]
    if not fid:
        return [], []
    keep_from = max(0, len(fid) - max_len)
    fid = fid[keep_from:]
    prompt_kept = max(0, min(len(pid), len(fid)) - keep_from)
    labels = [-100] * prompt_kept + fid[prompt_kept:]
    return fid, labels


def make_batches(items: list[dict], tok, max_len: int, batch_size: int
                 ) -> list[dict]:
    """成批 + **左侧** padding（padding 位在 labels 里同样补 -100，不学 pad）。

    pad 放在开头而不是结尾：生成侧惯例，训练侧靠 `attention_mask` 复位
    `position_ids`（transformers 在 `position_ids=None` 且有 mask 时按
    `mask.cumsum-1` 造位置），右 pad 会让短样本的最后一个真 token 学到
    一片 pad 的相对位置。
    """
    pad_id = tok.pad_token_id
    if pad_id is None:
        pad_id = tok.eos_token_id or 0
    out = []
    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        ids = [c["input_ids"] for c in chunk]
        labs = [c["labels"] for c in chunk]
        width = max(len(x) for x in ids)
        out.append({
            "input_ids": [[pad_id] * (width - len(x)) + x for x in ids],
            "labels": [[-100] * (width - len(x)) + x for x in labs],
            "attention_mask": [[0] * (width - len(x)) + [1] * len(x) for x in ids],
        })
    return out


# ── 产物落盘 ──────────────────────────────────────────────────────

def write_pairs_jsonl(path: Path, pairs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in pairs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def read_pairs_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


TRAIN_LOG_KEYS = ("model", "steps", "device", "dtype", "seed", "losses",
                  "eval_loss_before", "eval_loss_after", "elapsed_sec",
                  "peak_vram_mib", "train_pairs", "eval_pairs")


def write_train_log(out_dir: Path, payload: dict) -> Path:
    """train_log.json —— 缺关键键当场 ValueError（写半截的日志等于没日志）。"""
    missing = [k for k in TRAIN_LOG_KEYS if k not in payload]
    if missing:
        raise ValueError(f"train_log 缺关键字段：{missing}")
    if not isinstance(payload["losses"], list) or not payload["losses"]:
        raise ValueError("train_log.losses 必须是非空序列（没序列 = 没训练证据）")
    return write_json(Path(out_dir) / "train_log.json", payload)


def environment_snapshot(torch_mod=None, device: str = "", dtype: str = "") -> dict:
    env = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "device": device,
        "dtype": dtype,
        "cache_env": {k: os.environ.get(k) for k in
                      ("HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE",
                       "TORCH_HOME", "PIP_CACHE_DIR")},
    }
    if torch_mod is None:
        return env
    env["torch"] = getattr(torch_mod, "__version__", "?")
    env["cuda_available"] = bool(torch_mod.cuda.is_available())
    if env["cuda_available"]:
        env["cuda_runtime"] = torch_mod.version.cuda
        env["gpu_name"] = torch_mod.cuda.get_device_name(0)
        try:
            free, total = torch_mod.cuda.mem_get_info(0)
            env["gpu_total_mib"] = int(total / 2 ** 20)
            env["gpu_free_mib_at_start"] = int(free / 2 ** 20)
        except Exception:                       # 非 CUDA 平台/驱动不给就算了
            pass
        env["bf16_supported"] = bool(torch_mod.cuda.is_bf16_supported())
    try:
        import transformers
        env["transformers"] = transformers.__version__
    except Exception:
        env["transformers"] = None
    try:
        import peft
        env["peft"] = peft.__version__
    except Exception:
        env["peft"] = None
    return env


# ── 训练（真跑；torch 全部懒加载）─────────────────────────────────

def _import_torch():
    try:
        import torch
    except ImportError as e:
        die("没装 torch：本机训练栈装在 F:/G: 盘专用 venv 里，"
            "例如 G:/lg_ft/venv/Scripts/python.exe（--dry-run 不需要 torch）"
            f"（原始错误：{e}）")
    return torch


def set_seed(seed: int, torch_mod) -> None:
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import numpy as np
        np.random.seed(seed)
    except Exception:
        pass
    torch_mod.manual_seed(seed)
    if torch_mod.cuda.is_available():
        torch_mod.cuda.manual_seed_all(seed)


def _load_base(model_name: str, dtype, device: str, torch_mod):
    from transformers import AutoModelForCausalLM
    kw = {"dtype": dtype}                    # transformers>=5
    try:
        model = AutoModelForCausalLM.from_pretrained(model_name, **kw)
    except TypeError:                        # 老版只认 torch_dtype
        model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=dtype)
    return model.to(device)


def train(cfg: TrainConfig, pairs: list[dict]) -> dict:
    """真训练。返回 train_log payload（`write_train_log` 直接吃）。

    手写训练循环而不是 `Trainer`：loss 序列逐 optimizer step 精确记账，
    且不绑 transformers 某一版的 Trainer 参数名（本机 venv 里 transformers
    是 5.x，`evaluation_strategy` 之类早已改名）。
    """
    torch_mod = _import_torch()
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoTokenizer

    apply_cache_env(cache_env(cfg.cache_root))
    device, dtype_name = resolve_device_dtype(cfg, torch_mod)
    dtype = torch_dtype(dtype_name, torch_mod)
    set_seed(cfg.seed, torch_mod)

    env = environment_snapshot(torch_mod, device, dtype_name)
    log(f"device={device} dtype={dtype_name} model={cfg.model}")
    log(f"环境：{json.dumps({k: env.get(k) for k in ('python','torch','transformers','peft','gpu_name','gpu_total_mib','gpu_free_mib_at_start')}, ensure_ascii=False)}")

    train_rows = [r for r in pairs if r["split"] == "train"]
    eval_rows = [r for r in pairs if r["split"] == "eval"]
    if not train_rows:
        die("没有训练样本（pairs 全被划进 eval？）")

    tok = AutoTokenizer.from_pretrained(cfg.model)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    encoded, dropped = [], 0
    for row in train_rows:
        ids, labels = encode_pair(tok, row, cfg.max_len)
        if not ids or all(x == -100 for x in labels):
            dropped += 1
            continue
        encoded.append({"input_ids": ids, "labels": labels, "id": row["id"]})
    if not encoded:
        die("全部样本编码后没有可学 token（max_len 太小？）")
    eval_encoded, eval_dropped = [], 0
    for row in eval_rows:
        ids, labels = encode_pair(tok, row, cfg.max_len)
        if ids and not all(x == -100 for x in labels):
            eval_encoded.append({"input_ids": ids, "labels": labels})
    eval_dropped = len(eval_rows) - len(eval_encoded)
    _, template_mode = build_prompt(tok, train_rows[0]["instruction"])
    log(f"prompt 模式={template_mode} 训练样本={len(encoded)}"
        f"（编码丢弃 {dropped}）held-out={len(eval_encoded)}"
        f"（丢弃 {eval_dropped}）max_len={cfg.max_len}")

    model = _load_base(cfg.model, dtype, device, torch_mod)
    model.config.use_cache = False
    if cfg.gradient_checkpointing:
        model.gradient_checkpointing_enable()
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
    lora = LoraConfig(task_type=TaskType.CAUSAL_LM, r=cfg.lora_r,
                      lora_alpha=cfg.lora_alpha, lora_dropout=cfg.lora_dropout,
                      target_modules=list(LORA_TARGETS), bias="none")
    model = get_peft_model(model, lora)
    trainable = [p for p in model.parameters() if p.requires_grad]
    n_trainable = sum(p.numel() for p in trainable)
    n_total = sum(p.numel() for p in model.parameters())
    log(f"LoRA r={cfg.lora_r} alpha={cfg.lora_alpha} "
        f"可训练参数 {n_trainable:,}/{n_total:,}（{n_trainable / n_total:.4%}）")
    model.train()

    def _eval_loss() -> float | None:
        if not eval_encoded:
            return None
        model.eval()
        batches = make_batches(eval_encoded, tok, cfg.max_len, cfg.micro_batch)
        tot, n = 0.0, 0
        with torch_mod.no_grad():
            for b in batches:
                out = model(input_ids=torch_mod.tensor(b["input_ids"], device=device),
                            attention_mask=torch_mod.tensor(b["attention_mask"],
                                                            device=device),
                            labels=torch_mod.tensor(b["labels"], device=device))
                ntok = max(1, sum(1 for row in b["labels"] for v in row if v != -100))
                tot += float(out.loss) * ntok
                n += ntok
        model.train()
        return tot / n if n else None

    opt = torch_mod.optim.AdamW(trainable, lr=cfg.lr,
                                weight_decay=cfg.weight_decay)
    warmup = max(1, int(cfg.steps * cfg.warmup_ratio))

    def _lr_lambda(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        remain = max(1, cfg.steps - warmup)
        return max(0.0, (cfg.steps - step) / remain)

    sched = torch_mod.optim.lr_scheduler.LambdaLR(opt, _lr_lambda)
    scaler = None
    if dtype_name == "fp16":
        try:
            scaler = torch_mod.amp.GradScaler("cuda", enabled=True)
        except (AttributeError, TypeError):
            scaler = torch_mod.cuda.amp.GradScaler(enabled=True)

    eval_before = _eval_loss()
    log(f"held-out 初始 loss = {eval_before}")

    rng = random.Random(cfg.seed)
    pool = list(encoded)
    cursor = 0
    losses: list[float] = []
    tokens_seen = 0
    samples_seen = 0
    step_times: list[float] = []
    t0 = time.perf_counter()
    def _next_micro() -> list[dict]:
        """从池里取下一份 micro-batch；取空就重洗（不硬切 epoch 边界）。"""
        nonlocal cursor
        if cursor + cfg.micro_batch > len(pool):
            rng.shuffle(pool)
            cursor = 0
        micro = pool[cursor:cursor + cfg.micro_batch]
        cursor += cfg.micro_batch
        return micro

    try:
        for step in range(cfg.steps):
            ts = time.perf_counter()
            opt.zero_grad(set_to_none=True)
            micro_losses: list[float] = []
            # 真·梯度累积：一个 optimizer step 内跑 grad_accum 份 micro-batch，
            # 各自 /grad_accum 后把梯度累到同一份 .grad 上，最后只 step 一次。
            # （早先的版本只取一份 micro-batch 再把 loss 除以 grad_accum——
            #  那等于把 lr 偷偷除以 grad_accum，不是累积，是缩水。）
            for _ in range(cfg.grad_accum):
                micro = _next_micro()
                for b in make_batches(micro, tok, cfg.max_len, cfg.micro_batch):
                    ids_t = torch_mod.tensor(b["input_ids"], device=device)
                    lab_t = torch_mod.tensor(b["labels"], device=device)
                    am_t = torch_mod.tensor(b["attention_mask"], device=device)
                    out = model(input_ids=ids_t, attention_mask=am_t, labels=lab_t)
                    loss = out.loss / cfg.grad_accum
                    if scaler is not None:
                        scaler.scale(loss).backward()
                    else:
                        loss.backward()
                    micro_losses.append(float(out.loss.detach()))
                    tokens_seen += int((lab_t != -100).sum())
                    samples_seen += ids_t.shape[0]
            if not micro_losses:
                die("一个 optimizer step 里一份样本都没取到（pool 为空？）")
            if cfg.grad_clip > 0:
                if scaler is not None:
                    scaler.unscale_(opt)
                torch_mod.nn.utils.clip_grad_norm_(trainable, cfg.grad_clip)
            if scaler is not None:
                scaler.step(opt)
                scaler.update()
            else:
                opt.step()
            sched.step()
            step_loss = sum(micro_losses) / len(micro_losses)
            losses.append(round(step_loss, 6))
            step_times.append(round(time.perf_counter() - ts, 3))
            if (step + 1) % cfg.log_every == 0 or step == 0:
                log(f"step {step + 1}/{cfg.steps} loss={step_loss:.4f} "
                    f"lr={sched.get_last_lr()[0]:.2e} "
                    f"{step_times[-1]:.2f}s")
    except torch_mod.cuda.OutOfMemoryError as e:
        die(f"CUDA OOM（显存不够）：{e}\n"
            "对策（按代价从小到大）：--device cpu / --max-len 128 / "
            "--max-pairs 200 / --steps 20。**不要硬撞显存**。")
    elapsed = time.perf_counter() - t0
    eval_after = _eval_loss()
    peak = None
    if device == "cuda":
        torch_mod.cuda.synchronize()
        peak = int(torch_mod.cuda.max_memory_allocated() / 2 ** 20)
    log(f"held-out 结束 loss = {eval_after} 用时 {elapsed:.1f}s"
        + (f" 峰值显存 {peak} MiB" if peak else ""))

    adapter_dir = Path(cfg.out_dir) / "adapter"
    model.save_pretrained(str(adapter_dir))
    tok.save_pretrained(str(adapter_dir))

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": "scripts/finetune_min.py",
        "config": cfg.as_dict(),
        "env": env,
        "prompt_mode": template_mode,
        "template_mix": {
            t: sum(1 for r in train_rows if r["template"] == t)
            for t in ALL_TEMPLATES
        },
        "model": cfg.model,
        "steps": cfg.steps,
        "seed": cfg.seed,
        "device": device,
        "dtype": dtype_name,
        "train_pairs": len(encoded),
        "eval_pairs": len(eval_encoded),
        "dropped_pairs": {"train": dropped, "eval": eval_dropped},
        "lora": {"r": cfg.lora_r, "alpha": cfg.lora_alpha,
                 "dropout": cfg.lora_dropout,
                 "targets": list(LORA_TARGETS),
                 "trainable_params": n_trainable, "total_params": n_total},
        "optim": {"lr": cfg.lr, "micro_batch": cfg.micro_batch,
                  "grad_accum": cfg.grad_accum, "grad_clip": cfg.grad_clip,
                  "warmup_ratio": cfg.warmup_ratio,
                  "max_len": cfg.max_len, "grad_scaler": scaler is not None},
        "losses": losses,
        "loss_first": losses[0],
        "loss_last": losses[-1],
        "loss_mean_first10": round(sum(losses[:10]) / max(1, len(losses[:10])), 6),
        "loss_mean_last10": round(sum(losses[-10:]) / max(1, len(losses[-10:])), 6),
        "eval_loss_before": eval_before,
        "eval_loss_after": eval_after,
        "elapsed_sec": round(elapsed, 2),
        "sec_per_step": round(elapsed / max(1, cfg.steps), 3),
        "step_times_sec": step_times,
        "peak_vram_mib": peak,
        "samples_seen": samples_seen,
        "tokens_seen": tokens_seen,
        "adapter_dir": str(adapter_dir),
        "adapter_files": sorted(p.name for p in adapter_dir.iterdir()),
        "scale_ceiling": {
            "max_model_params_b": MAX_MODEL_PARAM_HINT,
            "max_pairs": MAX_PAIRS_HARD_CAP,
            "max_steps": MAX_STEPS_HARD_CAP,
            "note": "本机 4 GiB 显存下限；7B~14B QLoRA 走 docs/kaggle-qlora-notebook.md",
        },
    }
    write_train_log(Path(cfg.out_dir), payload)
    write_json(Path(cfg.out_dir) / "run_env.json", env)
    return payload


# ── CLI ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="本机微调最小闭环（小基座 LoRA，产物只落 F:/G:）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--stage", choices=("data", "train", "all"), default="all",
                   help="data=只抽数据；train=只训（读已有 train_pairs.jsonl）；all=两者")
    p.add_argument("--dry-run", action="store_true",
                   help="只校验数据与配置，全程不 import torch")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--out-dir", type=Path, default=TrainConfig.out_dir)
    p.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    p.add_argument("--steps", type=int, default=TrainConfig.steps)
    p.add_argument("--lr", type=float, default=TrainConfig.lr)
    p.add_argument("--micro-batch", type=int, default=TrainConfig.micro_batch)
    p.add_argument("--grad-accum", type=int, default=TrainConfig.grad_accum)
    p.add_argument("--max-len", type=int, default=TrainConfig.max_len)
    p.add_argument("--max-pairs", type=int, default=TrainConfig.max_pairs)
    p.add_argument("--eval-size", type=int, default=TrainConfig.eval_size)
    p.add_argument("--seed", type=int, default=TrainConfig.seed)
    p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    p.add_argument("--dtype", choices=("auto", "fp32", "fp16", "bf16"), default="auto")
    p.add_argument("--lora-r", type=int, default=TrainConfig.lora_r)
    p.add_argument("--lora-alpha", type=int, default=TrainConfig.lora_alpha)
    p.add_argument("--lora-dropout", type=float, default=TrainConfig.lora_dropout)
    p.add_argument("--warmup-ratio", type=float, default=TrainConfig.warmup_ratio)
    p.add_argument("--weight-decay", type=float, default=TrainConfig.weight_decay)
    p.add_argument("--grad-clip", type=float, default=TrainConfig.grad_clip)
    p.add_argument("--log-every", type=int, default=TrainConfig.log_every)
    p.add_argument("--no-grad-checkpointing", action="store_true")
    p.add_argument("--no-exclude-bench", action="store_true",
                   help="不排除基准文本（默认排除；打开会污染基准，实验用）")
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    cfg = TrainConfig(
        model=args.model, out_dir=args.out_dir, db=args.db,
        cache_root=args.cache_root, steps=args.steps, lr=args.lr,
        micro_batch=args.micro_batch, grad_accum=args.grad_accum,
        max_len=args.max_len, max_pairs=args.max_pairs,
        eval_size=args.eval_size, seed=args.seed, device=args.device,
        dtype=args.dtype, lora_r=args.lora_r, lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout, warmup_ratio=args.warmup_ratio,
        weight_decay=args.weight_decay, grad_clip=args.grad_clip,
        log_every=args.log_every,
        gradient_checkpointing=not args.no_grad_checkpointing)
    try:
        cfg = validate_config(cfg)
    except ValueError as e:
        die(str(e))
    log(f"out-dir={cfg.out_dir}（F:/G: 盘、检出外）cache={cfg.cache_root}")

    pairs_path = Path(cfg.out_dir) / "train_pairs.jsonl"
    if args.stage in ("data", "all"):
        con = open_db_ro(cfg.db)
        try:
            pairs, stats = extract_pairs(
                con, limit=cfg.max_pairs, seed=cfg.seed,
                exclude_bench=not args.no_exclude_bench,
                eval_size=cfg.eval_size)
        finally:
            con.close()
        write_pairs_jsonl(pairs_path, pairs)
        log(f"数据：{json.dumps(stats, ensure_ascii=False)}")
        log(f"写出 {pairs_path}（{len(pairs)} 条）")
        for row in pairs[:2]:
            log(f"样例[{row['template']}/{row['split']}] {row['id']}："
                f"指令={row['instruction'][:60]}… 目标={row['output'][:40]}…")

    if args.dry_run:
        log("dry-run：到此为止（不 import torch、不训练）")
        return 0

    if args.stage == "data":
        return 0

    if args.stage == "train" and not pairs_path.exists():
        die(f"--stage train 需要先有 {pairs_path}（先跑 --stage data）")
    pairs = read_pairs_jsonl(pairs_path)
    log(f"读入 {pairs_path}：{len(pairs)} 条")
    payload = train(cfg, pairs)
    log(f"train_log 首 loss={payload['loss_first']} 末 loss={payload['loss_last']} "
        f"held-out {payload['eval_loss_before']} → {payload['eval_loss_after']}")
    log(f"adapter 落盘：{payload['adapter_dir']}")
    for name in payload["adapter_files"]:
        log(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())