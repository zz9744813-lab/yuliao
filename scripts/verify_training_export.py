"""训练导出验收器（审查 A03，2026-09-20）。

## 为什么需要它

审查实测（09-19 旧导出）：SFT/Rewrite 各 114 行与冻结基准文本重合、
32 行源段尚未校勘、RM 有 172 组同源同文不同分——源码修复后这些文件
**没有重新生成**，旧文件继续躺在 exports/ 里等着被误拿去训练。

本脚本把「这份导出能不能拿去训练」变成可机检的契约：

1. **隔离**：逐行目标+前文按「忽略空白、≥50 字」口径（审查复算法）
   对全库冻结基准文本哈希重算重合——导出侧排除逻辑失效时这里必须红；
2. **冲突消解**（RM）：同源同文不同分的组数必须为 0（导出侧
   _dedupe_rm_rows 优先级规则的独立复核）；
3. **源质量**：逐行回连段 integrity.src_ok，未过闸的行数如实报；
4. **哈希清单**：sha256 + 行数 + 不同源段数写入旁挂 manifest，
   训练入口 accept_for_training() 只认「manifest 与文件本体哈希一致
   且验收通过」的版本——旧导出/手改过的导出一律拒绝；
5. **同源不相加**：manifest 记不同源段数；SFT 与 Rewrite 是同源的
   两种用法，合计样本时按源段数算，不许按行数相加。

## 用法

    python scripts/verify_training_export.py --file data/exports/writer_sft_v3.jsonl
    # 旁挂生成 <file>.manifest.json；验收不过 exit 1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import export_training as ET                     # noqa: E402
from app import db                              # noqa: E402
from app.models import Segment                   # noqa: E402

MIN_CHARS = 50          # 审查复算口径：忽略空白后 ≥50 字才参与重合比较

# 每类导出参与重合检查的字段：目标 + 前文（审查：「比较范围包括目标与前文」）
_KIND_FIELDS = {
    "writer_sft": ("target", "prev1", "prev2"),
    "rewrite": ("output", "context"),
    "rm": ("text", "prev1", "prev2"),
}


def _kind_of(path: Path) -> str:
    name = path.name.lower()
    if name.startswith("writer_sft"):
        return "writer_sft"
    if name.startswith("rewrite"):
        return "rewrite"
    if name.startswith("rm"):
        return "rm"
    raise SystemExit(f"认不出导出类型（文件名须以 writer_sft/rewrite/rm 开头）：{path.name}")


def _texts_of(v):
    """字段值 → 待比对文本流：字符串直出；dict/list（如 rewrite 的
    context={prev1,prev2} 结构）递归抽其中的字符串——前文不管什么形态
    都得进重合比较，漏一层就是漏一片。"""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _texts_of(x)
    elif isinstance(v, list):
        for x in v:
            yield from _texts_of(x)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")
    kind = _kind_of(path)
    fields = _KIND_FIELDS[kind]
    # 导出侧 _bench_hashes 是进程级缓存——验收必须每次重算，宁慢不错
    ET._BENCH_HASHES = None
    bench = ET._bench_hashes()

    rows = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"第 {i + 1} 行不是合法 JSON：{e}")

    overlap_rows, overlap_examples = 0, []
    missing_key_rows = 0
    sources = set()
    for r in rows:
        sid = r.get("segment_id")
        if not sid:
            missing_key_rows += 1
        else:
            sources.add(sid)
        for fld in fields:
            for t in _texts_of(r.get(fld)):
                norm = "".join(t.split())
                if len(norm) >= MIN_CHARS and \
                        hashlib.md5(norm.encode("utf-8")).hexdigest() in bench:
                    overlap_rows += 1
                    if len(overlap_examples) < 10:
                        overlap_examples.append({"line_id": r.get("id"), "field": fld})
                    break
            else:
                continue
            break

    rm_conflicts = 0
    if kind == "rm":
        groups: dict[tuple, set] = defaultdict(set)
        for r in rows:
            key = (r.get("segment_id"), "".join((r.get("text") or "").split()))
            groups[key].add(r.get("score"))
        rm_conflicts = sum(1 for v in groups.values() if len(v) > 1)

    # 源质量：逐行回连段 integrity.src_ok（导出侧已闸；这里独立复核计数）
    src_unverified = 0
    with db.session() as s:
        for sid in sources:
            seg = s.get(Segment, sid)
            ok = False
            if seg is not None and seg.integrity:
                try:
                    ok = bool(json.loads(seg.integrity).get("src_ok"))
                except Exception:
                    ok = False
            if not ok:
                src_unverified += 1

    passed = (overlap_rows == 0 and missing_key_rows == 0
              and rm_conflicts == 0)
    return {
        "file": str(path), "kind": kind, "sha256": _sha256(path),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "n_rows": len(rows), "n_distinct_sources": len(sources),
        "bench_overlap_rows": overlap_rows,
        "bench_overlap_examples": overlap_examples,
        "rm_conflict_groups": rm_conflicts,
        "missing_key_rows": missing_key_rows,
        "src_unverified_segments": src_unverified,
        "same_source_note": "SFT 与 Rewrite 同源段是同一批样本的两种用法——"
                            "合计只按不同源段数算，不许按行数相加",
        "acceptance": {"passed": passed,
                       "rule": "bench_overlap=0 且 主键齐全 且（RM）同源同文无多分"},
    }


def accept_for_training(path_str: str) -> tuple[bool, list[str]]:
    """训练入口契约：只认「manifest 与文件本体一致且验收通过」的导出。

    拒收：无 manifest（旧导出/手生成）、sha 不符（重导前旧文件/被改过）、
    验收未过（基准重合/主键缺失/RM 冲突）。"""
    path = Path(path_str)
    mf = path.with_suffix(".manifest.json")
    problems = []
    if not mf.exists():
        return False, [f"无验收清单 {mf.name}——旧版/未走验收器的导出一律拒收"]
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        return False, [f"manifest 解析失败：{e}"]
    if man.get("sha256") != _sha256(path):
        problems.append("文件 sha256 与 manifest 不符（重导过/被改动）——拒收")
    acc = man.get("acceptance") or {}
    if not acc.get("passed"):
        problems.append(f"manifest 验收未通过：重合 {man.get('bench_overlap_rows')}"
                         f" / 缺键 {man.get('missing_key_rows')}"
                         f" / RM冲突 {man.get('rm_conflict_groups')}")
    return (not problems), problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="待验收的导出 jsonl")
    args = ap.parse_args()
    db.init_db()
    man = verify(args.file)
    out = Path(args.file).with_suffix(".manifest.json")
    out.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(man, ensure_ascii=False, indent=1))
    print(f"[verify_training_export] 清单已写 {out}")
    ok, problems = accept_for_training(args.file)
    print(f"[verify_training_export] 训练入口验收：{'接受' if ok else '拒绝'}")
    for p in problems:
        print(f"  - {p}")
    if not man["acceptance"]["passed"] or not ok:
        raise SystemExit("验收未通过（exit 1）")
    print("[verify_training_export] PASS")


if __name__ == "__main__":
    main()
