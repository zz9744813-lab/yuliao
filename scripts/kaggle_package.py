"""G7 训练通道：把已有 SFT/DPO 导出打包成可上传 Kaggle 的产物（T-D2）。

**只读**现有数据文件，只写自己名下的新路径：

    data/exports/kaggle/lg_qlora_v1/   暂存目录（Kaggle 直接上传这一层）
    data/exports/language_genome_qlora_v1.tar.gz
    data/exports/summary.json

输入全部来自 `scripts/export_training.py` 已经产出的 jsonl（字段名以其真实产出为准，
本脚本不发明字段）：

    writer_sft_v3.jsonl     id/prev2/prev1/frame{...}/target/target_side/quality/
                            strategy_hint[]/work/segment_id/frame_id/pv/provenance
    rewrite_v2.jsonl        id/instruction/output/context{prev2,prev1}/frame/...
    ai_ranking_v1.jsonl     id/segment_id/chosen/rejected/chosen_model/rejected_model/
                            label_source/weak/votes/n_valid_judges/pair_seed/pv
    corrupt_dpo_v1.jsonl    id/prev2/prev1/frame/chosen/rejected/corruption_type/...
    corrupt_dpo_strict.jsonl  同上（集霸亲裁，目前 1 对）
    corrupt_negatives_v2.jsonl id/failure_text/failure_variable/failure_mode/
                            source_human/context_prev1/context_prev2/drift{...}/...
    rm_v1.jsonl             id/work/segment_id/candidate_id/pv/model/prev2/prev1/
                            text/score/label_source/weak/suspect/side/...

三条从 HANDOVER 带来的纪律，打包时硬编码执行：
  1. `corrupt_dpo_v1` 的 `chosen=人类原文` 方向已被裁定否掉（§0.5⑧）→
     只作为 **评测仪器** 放进 `eval_/`，不进 `dpo_train`。
  2. `ai_ranking_v1` 是 AI-vs-AI 相对排序、`weak` 全为真 → 进 dpo_train 但每行带
     `label_source`/`weak`，训练时按标签强度可选过滤。
  3. 切分不许按行随机：相邻段的 `prev1/prev2` 与彼此的 `target` 是同一串文本
     （实测 64 行命中，最长链 7），行级切分会把验证集泄漏进训练集 →
     先按"文本重叠"做并查集连通块，**整块**分配 train/val。

用法：
    python scripts/kaggle_package.py                      # 打包 + 写 summary
    python scripts/kaggle_package.py --val-ratio 0.2 --seed 20260919
    python scripts/kaggle_package.py --verify             # 只核对 sha256，不写任何东西
    python scripts/kaggle_package.py --dry-run            # 只打印计划与统计
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "data" / "exports"
PKG = "lg_qlora_v1"
STAGE_DIR = EXPORTS / "kaggle" / PKG
TARBALL = EXPORTS / "language_genome_qlora_v1.tar.gz"
SUMMARY = EXPORTS / "summary.json"

# 来源文件 → 角色（readonly：本脚本绝不写这些路径）
SOURCES = {
    "rewrite_v2.jsonl": "SFT 指令形态（instruction=帧要点 / output=人类原文）",
    "writer_sft_v3.jsonl": "SFT 帧形态（带 frame/prev1/prev2/strategy_hint）",
    "ai_ranking_v1.jsonl": "AI-vs-AI 自然度排序（weak 标签）",
    "corrupt_dpo_strict.jsonl": "集霸亲裁偏好对",
    "corrupt_dpo_v1.jsonl": "受控劣化对照对（方向已否 → 只当评测仪器）",
    "corrupt_negatives_v2.jsonl": "负面模式库（只标「不该这么写」）",
    "rm_v1.jsonl": "奖励模型打分数据（三来源标注）",
}

# 本脚本允许写出的文件名（其它一律拒绝）
OWNED_OUTPUTS = {
    "sft_train.jsonl", "sft_val.jsonl", "dpo_train.jsonl", "negatives.jsonl",
    "eval_corrupt_pairs.jsonl", "rm_scores.jsonl", "frame_schema.json",
    "manifest.json", "README.md", "metaData.json",
}
TOKEN_HEURISTIC = "chars/1.5（估算，中文 Qwen BPE 的经验值；真值以 tokenizer 为准）"
_CACHE: dict[str, list[dict]] = {}


# ---------------------------------------------------------------- 基础设施

def die(msg: str) -> None:
    print(f"[kaggle_package] 中止：{msg}", file=sys.stderr)
    raise SystemExit(2)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rows(name: str) -> list[dict]:
    path = EXPORTS / name
    if not path.is_file():
        die(f"缺少输入 {path}（先跑 scripts/export_training.py 对应口径）")
    if name in _CACHE:
        return _CACHE[name]
    rows = []
    with path.open(encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                die(f"{name}:{ln} 不是合法 JSON：{e}")
    _CACHE[name] = rows
    return rows


def assert_owned(path: Path) -> None:
    """护栏：只允许写自己声明过的产物，防止误伤别人的文件。"""
    if path.name not in OWNED_OUTPUTS and path.name not in {TARBALL.name, SUMMARY.name}:
        die(f"拒绝写未声明的产物 {path}")
    resolved = str(path.resolve())
    if not resolved.startswith(str(EXPORTS.resolve())):
        die(f"拒绝写到 data/exports 之外：{path}")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    assert_owned(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def write_text(path: Path, text: str) -> None:
    assert_owned(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def field_union(rows: list[dict]) -> list[str]:
    keys: set[str] = set()
    for r in rows:
        keys |= set(r)
    return sorted(keys)


def frame_schema(rows: list[dict]) -> dict:
    """从真实数据里取 frame 的字段并集 + 类型，不手写契约。"""
    types: dict[str, set[str]] = defaultdict(set)

    def walk(node, prefix=""):
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            key = f"{prefix}{k}"
            types[key].add("null" if v is None else type(v).__name__)
            if isinstance(v, dict):
                walk(v, key + ".")
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                walk(v[0], key + "[].")

    for r in rows:
        walk(r.get("frame"))
    return {
        "source": "rewrite_v2.jsonl / writer_sft_v3.jsonl 的 frame 字段并集（实测）",
        "fields": {k: sorted(v) for k, v in sorted(types.items())},
    }


# ---------------------------------------------------------------- 切分（防泄漏）

def norm(s) -> str:
    return "".join(str(s or "").split())


def prev_of(r: dict, which: str) -> str:
    """SFT 行里上下文键叫 context_prev1/2；原始导出叫 prev1/prev2 —— 两种都认。"""
    return norm(r.get(which)) or norm(r.get(f"context_{which}"))


def grouped_split(rows: list[dict], val_ratio: float, seed: int):
    """按「文本重叠连通块」整块切 train/val。

    两条边：A.prev1/prev2 与 B.target 是同一段文字（邻段链）；同 segment_id 天然同块。
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_text: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        rid = str(r["id"])
        find(rid)
        by_text[norm(r.get("target") or r.get("output"))].append(rid)
    links = 0
    for r in rows:
        rid = str(r["id"])
        union(rid, f"seg::{r.get('segment_id')}")
        for k in ("prev1", "prev2"):
            for other in by_text.get(prev_of(r, k), []):
                if other != rid and prev_of(r, k):
                    union(other, rid)
                    links += 1

    comps: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        comps[find(str(r["id"]))].append(r)

    # 块内总字符量决定其权重；按 (hash(seed+root), root) 稳定打乱，逐块填 val
    ordered = sorted(comps.items(), key=lambda kv: (
        hashlib.sha256(f"{seed}::{kv[0]}".encode()).hexdigest(), kv[0]))
    total_chars = sum(len(json.dumps(r, ensure_ascii=False)) for r in rows)
    val, train, acc = [], [], 0
    for root, members in ordered:
        size = sum(len(json.dumps(r, ensure_ascii=False)) for r in members)
        if acc / total_chars < val_ratio:
            val.extend(members)
            acc += size
        else:
            train.extend(members)
    train.sort(key=lambda r: str(r["id"]))
    val.sort(key=lambda r: str(r["id"]))
    stats = {
        "method": "union-find over (segment_id | prev1==target | prev2==target)，整块分配",
        "n_link_edges": links,
        "n_components": len(comps),
        "max_component_rows": max(len(v) for v in comps.values()),
        "seed": seed,
        "val_ratio_target": val_ratio,
        "val_ratio_actual_rows": round(len(val) / max(1, len(rows)), 4),
        "val_ratio_actual_chars": round(acc / max(1, total_chars), 4),
    }
    return train, val, stats


# ---------------------------------------------------------------- 行构造

def sft_row(rw: dict, sft: dict | None) -> dict:
    """SFT 行：保留 rewrite_v2 的 instruction/output 原文，补真实上下文。"""
    out = {
        "id": rw["id"],
        "instruction": rw["instruction"],
        "input": "",
        "output": rw["output"],
        "context_prev2": rw["context"].get("prev2", ""),
        "context_prev1": rw["context"].get("prev1", ""),
        "work": rw.get("work"),
        "segment_id": rw.get("segment_id"),
        "frame_id": rw.get("frame_id"),
        "granularity": rw.get("granularity"),
        "quality": rw.get("quality"),
        "prompt_version": rw.get("pv"),
        "provenance": rw.get("provenance"),
    }
    if sft is not None:
        out["strategy_hint"] = sft.get("strategy_hint") or []
        out["target_side"] = sft.get("target_side")
    return out


def prompt_text(prev2: str, prev1: str, instruction: str | None) -> str:
    parts = ["【前文】", (prev2 or "").strip(), (prev1 or "").strip(), ""]
    if instruction:
        parts += ["【语义帧（必须表达 / 不许直说）】", instruction, ""]
    parts += ["【任务】按帧写出下一段中文正文，只输出正文本身。", ""]
    return "\n".join(parts)


def dpo_row(air: dict, prompt_src: dict | None, strict: bool,
            seg_split: dict[str, str] | None = None) -> dict | None:
    """DPO 行需要 prompt；ai_ranking 本身不带上下文 → 按 segment_id 回连帧导出。

    strict 行（corrupt_dpo_strict）的 frame 为 null，只有 prev1/prev2 →
    prompt 允许退化为「纯前文续写」，但必须标 `prompt_has_frame=false`。
    回连不上、且无任何上下文的直接丢弃并计数（宁可少，不造 prompt）。
    """
    if strict:
        prev1, prev2 = (air.get("prev1") or ""), (air.get("prev2") or "")
        instruction = (json.dumps(air["frame"], ensure_ascii=False, sort_keys=True)
                       if air.get("frame") else None)
    else:
        if prompt_src is None:
            return None
        prev1 = prompt_src["context"].get("prev1", "")
        prev2 = prompt_src["context"].get("prev2", "")
        instruction = prompt_src.get("instruction")
    if not instruction and not (prev1.strip() or prev2.strip()):
        return None
    seg = str(air.get("segment_id"))
    return {
        "id": air["id"],
        "prompt": prompt_text(prev2, prev1, instruction),
        "chosen": air["chosen"],
        "rejected": air["rejected"],
        "label_source": air.get("label_source", "user_verdict_strict"),
        "weak": air.get("weak", False),
        "chosen_model": air.get("chosen_model"),
        "rejected_model": air.get("rejected_model"),
        "n_valid_judges": air.get("n_valid_judges"),
        "pair_seed": air.get("pair_seed"),
        "segment_id": air.get("segment_id"),
        "prompt_version": air.get("pv"),
        "prompt_has_frame": bool(instruction),
        "prompt_segment_sft_split": (seg_split or {}).get(seg, "unknown"),
    }


def eval_pair_row(r: dict) -> dict:
    """corrupt_dpo 只做仪器：把"不许当训练方向"写进每一行，防下游手滑。"""
    return {
        "id": r["id"],
        "prompt_context_prev2": r.get("prev2", ""),
        "prompt_context_prev1": r.get("prev1", ""),
        "human_text": r.get("chosen"),
        "corrupted_text": r.get("rejected"),
        "corruption_type": r.get("corruption_type"),
        "corruption_variable": r.get("corruption_variable"),
        "generator": r.get("generator"),
        "verifier": r.get("verifier"),
        "drift": r.get("drift"),
        "len_ratio": r.get("len_ratio"),
        "judge_votes": r.get("judge_votes"),
        "suspect": r.get("suspect"),
        "user_verdict": r.get("user_verdict", ""),
        "quality": r.get("quality"),
        "work": r.get("work"),
        "segment_id": r.get("segment_id"),
        "candidate_id": r.get("candidate_id"),
        "provenance": r.get("provenance"),
        "usable_for_training": False,
        "why": "chosen=人类原文 的默认方向已被裁定否掉（HANDOVER §0.5⑧：24 题里集霸只判 4 题人类胜）",
    }


def negatives_row(r: dict) -> dict:
    return {
        "id": r["id"],
        "failure_mode": r.get("failure_mode"),
        "failure_variable": r.get("failure_variable"),
        "failure_text": r.get("failure_text"),
        "source_human": r.get("source_human"),
        "context_prev2": r.get("context_prev2", ""),
        "context_prev1": r.get("context_prev1", ""),
        "drift": r.get("drift"),
        "provenance": r.get("provenance"),
        "prompt_version": r.get("pv"),
        "usage": "约束性负例：可拼进 SFT prompt 的「不该这么写」段，或做评判参考",
    }


def rm_row(r: dict) -> dict:
    return {
        "id": r["id"],
        "text": r.get("text"),
        "score": r.get("score"),
        "label_source": r.get("label_source"),
        "weak": r.get("weak"),
        "suspect": r.get("suspect"),
        "side": r.get("side"),
        "model": r.get("model"),
        "work": r.get("work"),
        "segment_id": r.get("segment_id"),
        "candidate_id": r.get("candidate_id"),
        "corruption_type": r.get("corruption_type") or None,
        "provenance": r.get("provenance"),
    }


# ---------------------------------------------------------------- 统计

def char_stats(rows: list[dict], key: str) -> dict:
    vals = sorted(len(str(r.get(key) or "")) for r in rows)
    if not vals:
        return {"n": 0}

    def pct(p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    est = [int(round(v / 1.5)) for v in vals]
    return {
        "n": len(vals),
        "chars_min": vals[0],
        "chars_p50": pct(0.5),
        "chars_p90": pct(0.9),
        "chars_p99": pct(0.99),
        "chars_max": vals[-1],
        "est_tokens_p50": est[len(est) // 2],
        "est_tokens_p99": est[int(len(est) * 0.99)],
        "est_tokens_max": est[-1],
        "est_tokens_method": TOKEN_HEURISTIC,
    }


def build_tarball(stage: Path, tar_path: Path) -> None:
    members = sorted(p for p in stage.rglob("*") if p.is_file())
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for p in members:
                tf.add(p, arcname=f"{PKG}/{p.relative_to(stage)}", recursive=False)
    tar_path.write_bytes(buf.getvalue())


# ---------------------------------------------------------------- 主流程

README = """# Language Genome QLoRA 数据包（{pkg}）

来源：`F:\\agi\\language-genome\\data\\exports\\`，由 `scripts/kaggle_package.py` 打包。
生成时间（UTC）：{created}

## 文件与用法

| 文件 | 条数 | 用法 |
|---|---|---|
| `sft_train.jsonl` | {n_sft_tr} | QLoRA SFT 主粮（帧要点 → 人类原文，quality=L1） |
| `sft_val.jsonl` | {n_sft_va} | 同分布 held-out，按邻段链整块切分（防 prev/target 泄漏） |
| `dpo_train.jsonl` | {n_dpo} | LoRA-DPO（AI-vs-AI 自然度排序，全部 `weak=true`；含集霸亲裁 {n_strict} 对） |
| `negatives.jsonl` | {n_neg} | 负面模式库：只标「不该这么写」，可拼进 prompt |
| `eval_corrupt_pairs.jsonl` | {n_eval} | **只读仪器**，`usable_for_training=false`，别拿去训 |
| `rm_scores.jsonl` | {n_rm} | 奖励模型/评点数据（三来源，`weak` 标强度） |
| `frame_schema.json` | — | SemanticFrame 真实字段并集（从数据实测，非手写） |
| `manifest.json` | — | 来源、条数、字段、切分口径、sha256 |

字段契约见 `manifest.json` 的 `outputs[*].fields`，与 jsonl 实际键一一对应。

## 纪律（打包时按 HANDOVER 执行，改数据前请先读）

1. `eval_corrupt_pairs.jsonl` 的 `chosen=人类原文 / rejected=受控劣化` 方向
   **已被裁定否掉**（HANDOVER §0.5⑧：24 题里集霸只判 4 题人类胜、11 题两边都不好）。
   拿它训 DPO = 教模型模仿他不认可的文本。它存在的意义是：每对都知道哪边是原文，
   可以脱离人工量出评委/模型的判别力。
2. `dpo_train.jsonl` 的标签全是**弱标签**：AI-vs-AI 自然度排序，每条 2 评委多数决
   （`moonshotai/kimi-k3` + `agnes-3.0-flash`，deepseek 弃权 18 条已排除），`weak` 全为真。
   评委对 AI 措辞本身有 77% 正偏好（控制臂读数 0.231）——
   所以预期产出是"风格纠偏"，不是"能力提升"，每轮必须过集霸人工盲评关卡。
3. `dpo_train.jsonl` 里有 {n_dpo_val_overlap} 行的 prompt 段落在 `sft_val.jsonl` 中
   （字段 `prompt_segment_sft_split`）。做「SFT → DPO」两阶段时，若要拿 sft_val 当
   早停/评估集，请先按该字段过滤，否则 DPO 阶段的读数被污染。
4. 数据只来自 4 本网文（{works}）。跨域泛化没有证据，别外推结论。

## 版权 / 公开性（上传前必须处理）

`output`、`source_human`、`context_prev*`、`human_text` 是已出版网文的**原文片段**。
Kaggle 数据集默认公开 = 二次分发。**`metaData.json` 已置 `isPrivate: true`**；
若要公开需集霸就版权口径先拍板。`instruction` 侧是本项目自己抽取的语义帧，
不含原文；但输出侧含，所以整包按"含受版权保护文本"处理。

## 上传

```bash
# 一次性：Kaggle API token 放 ~/.kaggle/kaggle.json 并 chmod 600，
# 并把 metaData.json 里的 REPLACE_WITH_KAGGLE_USERNAME 换成真实用户名
kaggle datasets create -p .                              # 首次建集
kaggle datasets version -p . -m "rebuild from export"    # 后续更新
kaggle datasets files <username>/language-genome-qlora-v1 # 核对
```

**上传解包后的目录，不要传 `.tar.gz`** —— Kaggle 不自动解 tar.gz；包里那份
只用于归档/搬运。网页端等价操作：Datasets → Create New Dataset → 整目录拖入。
"""

META_TEMPLATE = {
    "title": "Language Genome QLoRA v1",
    "id": "REPLACE_WITH_KAGGLE_USERNAME/language-genome-qlora-v1",
    "isPrivate": True,
    "description": (
        "SFT (SemanticFrame -> human text) + weak AI-vs-AI preference pairs + "
        "negative-mode library, for QLoRA on a 7B Chinese base. "
        "Output sides contain copyrighted novel excerpts: keep private."),
    "keywords": ["nlp", "chinese", "fine-tuning", "lora", "dataset"],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--val-ratio", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--stage-dir", default=str(STAGE_DIR))
    ap.add_argument("--tarball", default=str(TARBALL))
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    ap.add_argument("--verify", action="store_true",
                    help="只按现有 summary.json 核对产物 sha256，不写任何东西")
    args = ap.parse_args()

    stage = Path(args.stage_dir)
    tar_path = Path(args.tarball)

    if args.verify:
        verify(SUMMARY, EXPORTS)
        return

    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rw_rows = load_rows("rewrite_v2.jsonl")
    sft_rows = load_rows("writer_sft_v3.jsonl")
    air_rows = load_rows("ai_ranking_v1.jsonl")
    strict_rows = load_rows("corrupt_dpo_strict.jsonl")
    corrupt_rows = load_rows("corrupt_dpo_v1.jsonl")
    neg_rows = load_rows("corrupt_negatives_v2.jsonl")
    rm_rows = load_rows("rm_v1.jsonl")

    by_id = {str(r["id"]): r for r in sft_rows}
    sft_all = [sft_row(r, by_id.get(str(r["id"]))) for r in rw_rows]
    train, val, split_stats = grouped_split(sft_all, args.val_ratio, args.seed)

    seg_split: dict[str, str] = {}
    for r in train:
        seg_split[str(r.get("segment_id"))] = "train"
    for r in val:
        seg_split[str(r.get("segment_id"))] = "val"

    rw_by_seg = {}
    for r in rw_rows:
        rw_by_seg.setdefault(str(r.get("segment_id")), r)

    dpo, dpo_dropped = [], 0
    for r in air_rows:
        row = dpo_row(r, rw_by_seg.get(str(r.get("segment_id"))), strict=False,
                      seg_split=seg_split)
        if row is None:
            dpo_dropped += 1
            continue
        dpo.append(row)
    dpo_dropped_strict = 0
    for r in strict_rows:
        row = dpo_row(r, None, strict=True, seg_split=seg_split)
        if row is None:
            dpo_dropped_strict += 1
            continue
        dpo.append(row)
    dpo.sort(key=lambda x: str(x["id"]))

    eval_rows = [eval_pair_row(r) for r in corrupt_rows]
    neg_out = [negatives_row(r) for r in neg_rows]
    rm_out = [rm_row(r) for r in rm_rows]

    works = sorted({str(r.get("work")) for r in rw_rows if r.get("work")})
    outputs = [
        {"file": "sft_train.jsonl", "role": "SFT 训练", "rows": len(train),
         "fields": field_union(train),
         "size_bytes": None, "sha256": None,
         "stats": {"by_work": dict(sorted(Counter(str(r.get("work")) for r in train).items())),
                   "length_output": char_stats(train, "output"),
                   "length_instruction": char_stats(train, "instruction")}},
        {"file": "sft_val.jsonl", "role": "SFT held-out（只算 loss / 抽样人评）",
         "rows": len(val), "fields": field_union(val),
         "size_bytes": None, "sha256": None,
         "stats": {"by_work": dict(sorted(Counter(str(r.get("work")) for r in val).items())),
                   "length_output": char_stats(val, "output")}},
        {"file": "dpo_train.jsonl", "role": "LoRA-DPO（弱标签；量小，预期=风格纠偏）",
         "rows": len(dpo), "fields": field_union(dpo),
         "size_bytes": None, "sha256": None,
         "stats": {"n_weak": sum(1 for r in dpo if r.get("weak")),
                   "n_without_frame_in_prompt": sum(1 for r in dpo if not r.get("prompt_has_frame")),
                   "by_label_source": dict(sorted(Counter(str(r.get("label_source")) for r in dpo).items())),
                   "prompt_segment_also_in_sft_val": sum(
                       1 for r in dpo if r.get("prompt_segment_sft_split") == "val"),
                   "length_chosen_chars": char_stats(dpo, "chosen"),
                   "length_prompt": char_stats(dpo, "prompt")}},
        {"file": "negatives.jsonl", "role": "负面模式库（约束性负例）",
         "rows": len(neg_out), "fields": field_union(neg_out),
         "size_bytes": None, "sha256": None,
         "stats": {"by_failure_mode": dict(sorted(Counter(str(r.get("failure_mode")) for r in neg_out).items()))}},
        {"file": "eval_corrupt_pairs.jsonl", "role": "只读评测仪器（usable_for_training=false）",
         "rows": len(eval_rows), "fields": field_union(eval_rows),
         "size_bytes": None, "sha256": None,
         "stats": {"by_corruption_type": len({r.get("corruption_type") for r in eval_rows}),
                   "n_suspect": sum(1 for r in eval_rows if r.get("suspect"))}},
        {"file": "rm_scores.jsonl", "role": "RM/评点数据",
         "rows": len(rm_out), "fields": field_union(rm_out),
         "size_bytes": None, "sha256": None,
         "stats": {"by_label_source": dict(sorted(Counter(str(r.get("label_source")) for r in rm_out).items())),
                   "n_weak": sum(1 for r in rm_out if r.get("weak"))}},
        {"file": "frame_schema.json", "role": "SemanticFrame 字段实测并集",
         "rows": None, "fields": None, "size_bytes": None, "sha256": None, "stats": {}},
        {"file": "manifest.json", "role": "本清单的机读版", "rows": None,
         "fields": None, "size_bytes": None, "sha256": None, "stats": {}},
        {"file": "README.md", "role": "随包说明（纪律 + 版权）", "rows": None,
         "fields": None, "size_bytes": None, "sha256": None, "stats": {}},
        {"file": "metaData.json", "role": "Kaggle 数据集元数据（isPrivate=true）",
         "rows": None, "fields": None, "size_bytes": None, "sha256": None, "stats": {}},
    ]

    manifest = {
        "package": PKG,
        "created_utc": created,
        "generator_script": "scripts/kaggle_package.py",
        "upstream_scripts": ["scripts/export_training.py"],
        "repo": str(ROOT),
        "source_files": {
            name: {"role": role, "rows": len(load_rows(name)),
                   "path": str(EXPORTS / name),
                   "size_bytes": (EXPORTS / name).stat().st_size,
                   "sha256": sha256_file(EXPORTS / name)}
            for name, role in SOURCES.items()},
        "split": split_stats,
        "dpo_join_dropped": {
            "ai_ranking_no_prompt_context": dpo_dropped,
            "strict_unjoinable": dpo_dropped_strict,
            "note": "ai_ranking 不带上下文，按 segment_id 回连 rewrite_v2；回连不上就丢，不造 prompt",
        },
        "dpo_prompt_overlap_with_sft_val": sum(
            1 for r in dpo if r.get("prompt_segment_sft_split") == "val"),
        "disciplines": [
            "corrupt_dpo_v1 (chosen=human) 方向已被裁定否掉 → 只进 eval_，不进 dpo_train",
            "ai_ranking 全 weak → dpo 行带 label_source/weak，训练侧可按需过滤",
            "train/val 按邻段链连通块整块切分，避免 prev/target 同文泄漏",
            "dpo 行的 prompt 段可能与 sft_val 重合 → 每行带 prompt_segment_sft_split 供过滤",
            "数据仅出自 4 本网文，跨域泛化无证据",
            "含已出版网文原文片段 → metaData 置 isPrivate=true；公开需集霸拍版权口径",
        ],
        "token_estimate_method": TOKEN_HEURISTIC,
        "works": works,
    }

    fs = frame_schema(rw_rows)

    if args.dry_run:
        print(json.dumps({
            "package": PKG, "split": split_stats,
            "rows": {o["file"]: o["rows"] for o in outputs},
            "dpo_join_dropped": manifest["dpo_join_dropped"],
            "sft_output_len": outputs[0]["stats"]["length_output"],
            "sft_instruction_len": outputs[0]["stats"]["length_instruction"],
            "dpo_prompt_len": outputs[2]["stats"]["length_prompt"],
            "works": works,
        }, ensure_ascii=False, indent=2))
        print("[dry-run] 未写任何文件")
        return

    write_jsonl(stage / "sft_train.jsonl", train)
    write_jsonl(stage / "sft_val.jsonl", val)
    write_jsonl(stage / "dpo_train.jsonl", dpo)
    write_jsonl(stage / "negatives.jsonl", neg_out)
    write_jsonl(stage / "eval_corrupt_pairs.jsonl", eval_rows)
    write_jsonl(stage / "rm_scores.jsonl", rm_out)
    write_text(stage / "frame_schema.json",
               json.dumps(fs, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_text(stage / "manifest.json",
               json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    write_text(stage / "README.md", README.format(
        pkg=PKG, created=created, n_sft_tr=len(train), n_sft_va=len(val),
        n_dpo=len(dpo), n_strict=len(strict_rows), n_neg=len(neg_out),
        n_eval=len(eval_rows), n_rm=len(rm_out), works="、".join(works),
        n_dpo_val_overlap=sum(1 for r in dpo
                              if r.get("prompt_segment_sft_split") == "val")))
    meta = dict(META_TEMPLATE)
    meta["resources"] = [{"path": o["file"]} for o in outputs if o["file"] != "metaData.json"]
    write_text(stage / "metaData.json",
               json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    build_tarball(stage, tar_path)

    for o in outputs:
        p = stage / o["file"]
        o["size_bytes"] = p.stat().st_size
        o["sha256"] = sha256_file(p)

    summary = {
        "task": "G7 训练通道：Kaggle 打包 + QLoRA 模板 (T-D2)",
        "created_utc": created,
        "generator_script": "scripts/kaggle_package.py",
        "regenerate_command": "python scripts/kaggle_package.py --val-ratio {} --seed {}".format(
            args.val_ratio, args.seed),
        "verify_command": "python scripts/kaggle_package.py --verify",
        "source_scripts": ["scripts/export_training.py"],
        "staging_dir": str(stage),
        "tarball": {
            "path": str(tar_path),
            "size_bytes": tar_path.stat().st_size,
            "sha256": sha256_file(tar_path),
            "members": [o["file"] for o in outputs],
        },
        "package_root": PKG,
        "rows_total": {"sft": len(sft_all), "dpo": len(dpo), "negatives": len(neg_out),
                       "eval_only_corrupt_pairs": len(eval_rows), "rm": len(rm_out)},
        "split": split_stats,
        "dpo_join_dropped": manifest["dpo_join_dropped"],
        "dpo_prompt_overlap_with_sft_val": manifest["dpo_prompt_overlap_with_sft_val"],
        "disciplines": manifest["disciplines"],
        "token_estimate_method": TOKEN_HEURISTIC,
        "sources": manifest["source_files"],
        "outputs": outputs,
        "git_note": "data/ 在 .gitignore 内（仓库政策：数据产物不进版本库）→ "
                    "本包与 summary.json 只落盘，不 git add；"
                    "进版本库的只有 scripts/kaggle_package.py、scripts/kaggle_qlora.py、"
                    "docs/kaggle-qlora-notebook.md",
    }
    assert_owned(SUMMARY)
    write_text(SUMMARY, json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    print(f"[kaggle_package] 暂存目录 → {stage}")
    print(f"[kaggle_package] tarball → {tar_path} ({tar_path.stat().st_size/1e6:.2f} MB)")
    print(f"[kaggle_package]   sha256 {summary['tarball']['sha256'][:16]}…")
    print(f"[kaggle_package] summary → {SUMMARY}")
    print(f"[kaggle_package] SFT train/val = {len(train)}/{len(val)}（{split_stats['method']}）")
    print(f"[kaggle_package] DPO = {len(dpo)}（丢弃无上下文 {dpo_dropped}）· "
          f"negatives = {len(neg_out)} · eval-only = {len(eval_rows)} · rm = {len(rm_out)}")


def verify(summary_path: Path, base: Path) -> None:
    if not summary_path.is_file():
        die(f"没有 {summary_path}，先不带 --verify 跑一次")
    s = json.loads(summary_path.read_text(encoding="utf-8"))
    bad = 0
    tar_path = Path(s["tarball"]["path"])
    if not tar_path.is_file():
        print(f"  MISS {tar_path}")
        bad += 1
    elif sha256_file(tar_path) != s["tarball"]["sha256"]:
        print(f"  DIFF {tar_path}")
        bad += 1
    else:
        print(f"  OK   {tar_path.name} {tar_path.stat().st_size/1e6:.2f} MB")
    for o in s["outputs"]:
        p = Path(s["staging_dir"]) / o["file"]
        if not p.is_file():
            print(f"  MISS {p}")
            bad += 1
            continue
        if sha256_file(p) != o["sha256"]:
            print(f"  DIFF {o['file']}（内容已变，需重打包）")
            bad += 1
        elif p.stat().st_size != o["size_bytes"]:
            print(f"  DIFF {o['file']} 大小不符")
            bad += 1
        else:
            print(f"  OK   {o['file']:<26} rows={o['rows']} {o['size_bytes']/1e6:.2f} MB")
    for name, info in s.get("sources", {}).items():
        p = base / name
        ok = p.is_file() and sha256_file(p) == info["sha256"]
        print(f"  {'OK  ' if ok else 'STALE'} (源) {name}")
        bad += 0 if ok else 1
    bad += check_leak(Path(s["staging_dir"]))
    print(f"[verify] {'全部一致' if bad == 0 else str(bad) + ' 项不符'}")
    raise SystemExit(1 if bad else 0)


def check_leak(stage: Path) -> int:
    """核心卖点必须机器可核：train/val 之间不许有邻段链或同文。"""
    try:
        tr = [json.loads(l) for l in (stage / "sft_train.jsonl").open(encoding="utf-8") if l.strip()]
        va = [json.loads(l) for l in (stage / "sft_val.jsonl").open(encoding="utf-8") if l.strip()]
    except OSError as e:
        print(f"  MISS 无法读切分文件：{e}")
        return 1
    vt = {norm(r.get("output")) for r in va}
    vs = {str(r.get("segment_id")) for r in va}
    leaks = sum(1 for r in tr
                if prev_of(r, "prev1") in vt or prev_of(r, "prev2") in vt
                or str(r.get("segment_id")) in vs)
    shared_seg = len({str(r.get("segment_id")) for r in tr} & vs)
    dup_text = len({t for t in map(lambda r: norm(r.get("output")), tr)} & vt)
    ok = leaks == 0 and shared_seg == 0 and dup_text == 0
    print(f"  {'OK  ' if ok else 'LEAK'} 切分隔离 train={len(tr)}/val={len(va)}："
          f"链上泄漏 {leaks} · 共享 segment {shared_seg} · 同文跨集 {dup_text}")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
