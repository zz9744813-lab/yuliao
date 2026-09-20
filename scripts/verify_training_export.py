"""训练导出验收器（审查 A03，2026-09-20；会审 09-21 二轮加固）。

## 为什么需要它

审查实测（09-19 旧导出）：SFT/Rewrite 各 114 行与冻结基准文本重合、
32 行源段未校勘、RM 有 172 组同源同文不同分——源码修复后这些文件
**没有重新生成**，旧文件继续躺在 exports/ 里等着被误拿去训练。

本脚本把「这份导出能不能拿去训练」变成可机检、**防手改**的契约：

1. **隔离**：逐行目标+前文按「忽略空白、≥50 字」口径（审查复算法）
   对全库冻结基准文本哈希重算重合——导出侧排除逻辑失效时这里必须红。
   基准哈希集为空（连错库/基准未冻结）→ 直接拒，不做恒绿检查；
   实际参与比较的文本数计入 manifest，字段名漂移导致比较空转 → 拒。
2. **冲突消解**（RM）：同源同文不同分的组数必须为 0（导出侧
   _dedupe_rm_rows 优先级规则的独立复核）；
3. **源质量**：逐行回连段 integrity.src_ok，未过闸的**段数**进
   验收判据（>0 即拒）——「只报不闸」会让未校勘段混进训练还亮绿；
4. **防手改**：manifest 是旁挂明文，手改 passed=true 挡不住——
   accept_for_training **重跑 verify 的全部纯检查**并与 manifest
   的 sha256/kind/行数交叉核对；无 manifest（旧导出）、sha 不符
   （重导前旧文件/被改动）、交叉不一致 → 一律拒收；
5. **同源不相加**：union_distinct_sources() 给出多文件的源段并集——
   SFT 与 Rewrite 是同源段两种用法，合计只按并集数算，函数与测试
   钉住这条规则，不靠口头约定。

## 用法

    python scripts/verify_training_export.py --file data/exports/writer_sft_v3.jsonl
    # 旁挂生成 writer_sft_v3.manifest.json（x.jsonl → x.manifest.json）；
    # 验收不过 exit 1。训练侧入口见 scripts/train_entry.py（只认验收过）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
    raise SystemExit(f"认不出导出类型（文件名须以 writer_sft/rewrite/rm 开头）：{path.name}"
                     "——备份文件别拿来验收；accept 侧还会与 manifest 交叉核对 kind")


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


def _md5(text: str) -> str:
    # usedforsecurity=False：FIPS 受限环境下 md5 默认禁用，验收器是非安全用途
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path_str: str, write_manifest: bool = True) -> dict:
    """重算全部检查项并（默认）旁挂写 manifest。

    write_manifest=False 供 accept_for_training 重跑纯检查用——
    训练入口不信旁挂明文里的 passed 位，只信当场重算的结果。"""
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")
    kind = _kind_of(path)
    fields = _KIND_FIELDS[kind]
    # 走导出模块的显式接口作废缓存——不戳私有名（会审 09-21：改名后
    # 静默不生效 = 基准哈希陈旧 = 重合漏检）
    ET.reset_bench_cache()
    bench = ET._bench_hashes()
    if not bench:
        raise SystemExit("基准哈希集为空（连错库 / 基准未冻结）——隔离检查"
                         "无从算起，宁可拒也不许恒绿假 PASS")

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
    n_texts_compared = 0
    sources = set()
    for i, r in enumerate(rows):
        sid = r.get("segment_id")
        if not sid:
            missing_key_rows += 1
        else:
            sources.add(sid)
        hit = False
        for fld in fields:
            for t in _texts_of(r.get(fld)):
                n_texts_compared += 1
                norm = "".join(t.split())
                if len(norm) >= MIN_CHARS and _md5(norm) in bench:
                    hit = True
                    break
            if hit:
                break
        if hit:
            overlap_rows += 1
            if len(overlap_examples) < 10:
                overlap_examples.append({"line_no": i + 1,
                                          "line_id": r.get("id"), "field": fld})
    if n_texts_compared == 0:
        raise SystemExit(
            f"0 条文本参与重合比较——{kind} 行里找不到字段 {fields}"
            "（导出 schema 漂移？）：比较空转还报 PASS 就是最危险的假绿")

    rm_conflicts = 0
    if kind == "rm":
        groups: dict[tuple, set] = defaultdict(set)
        for r in rows:
            key = (r.get("segment_id"), "".join((r.get("text") or "").split()))
            groups[key].add(r.get("score"))
        rm_conflicts = sum(1 for v in groups.values() if len(v) > 1)

    # 源质量：按源段批量回连（1672 段不许逐段 1672 查），integrity 兼容
    # str 与 dict（JSON 列在 ORM 侧已解析）；未过闸段数 >0 即拒收
    src_unverified, src_missing = 0, 0
    with db.session() as s:
        segs = {seg.id: seg for seg in
                s.query(Segment).filter(Segment.id.in_(sources)).all()} \
            if sources else {}
    for sid in sources:
        seg = segs.get(sid)
        if seg is None:
            src_missing += 1
            continue
        integ = seg.integrity
        if isinstance(integ, str):
            try:
                integ = json.loads(integ)
            except Exception:
                integ = None
        ok = bool((integ or {}).get("src_ok")) if isinstance(integ, dict) else False
        if not ok:
            src_unverified += 1
    if src_missing:
        raise SystemExit(f"{src_missing} 个源段在库里查无（行引用悬空段）——"
                         "导出与库不同源，拒收")

    passed = (overlap_rows == 0 and missing_key_rows == 0
              and rm_conflicts == 0 and src_unverified == 0)
    man = {
        "file": str(path), "kind": kind, "sha256": _sha256(path),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_rows": len(rows), "n_distinct_sources": len(sources),
        "n_texts_compared": n_texts_compared,
        "bench_overlap_rows": overlap_rows,
        "bench_overlap_examples": overlap_examples,
        "rm_conflict_groups": rm_conflicts,
        "missing_key_rows": missing_key_rows,
        "src_unverified_segments": src_unverified,
        "same_source_note": "SFT 与 Rewrite 同源段是同一批样本的两种用法——"
                            "合计只按 union_distinct_sources 并集算，不许按行数相加",
        "acceptance": {"passed": passed,
                       "rule": "重合=0 且 主键齐全 且（RM）同源同文无多分 "
                               "且 未校勘源段=0 且 比较文本>0"},
    }
    if write_manifest:
        out = path.with_suffix(".manifest.json")
        out.write_text(json.dumps(man, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"[verify_training_export] 清单已写 {out}")
    return man


def accept_for_training(path_str: str) -> tuple[bool, list[str]]:
    """训练入口契约：只认「manifest 旁挂 + 与文件本体交叉一致 + 当场重算通过」。

    手改 manifest 的 passed 位挡不住——这里重跑 verify 的全部纯检查
    （write_manifest=False）并与旁挂清单的 sha256/kind/行数交叉核对；
    拒收：无 manifest（旧导出/手生成）、文件缺失、sha 不符（重导前旧
    文件/被改动）、交叉不一致、当场重算未过。"""
    path = Path(path_str)
    if not path.exists():
        return False, [f"导出文件不存在：{path}"]
    mf = path.with_suffix(".manifest.json")
    if not mf.exists():
        return False, [f"无验收清单 {mf.name}——旧版/未走验收器的导出一律拒收"]
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        return False, [f"manifest 解析失败：{e}"]
    problems = []
    fresh = verify(path_str, write_manifest=False)   # 只信当场重算
    if man.get("sha256") != fresh["sha256"]:
        problems.append("manifest sha256 与文件本体不符（清单过期/文件被改）——拒收")
    if man.get("kind") != fresh["kind"] or man.get("n_rows") != fresh["n_rows"]:
        problems.append("manifest kind/行数与文件不符（备份文件/张冠李戴）——拒收")
    if not fresh["acceptance"]["passed"]:
        problems.append(f"当场重算未通过：重合 {fresh['bench_overlap_rows']}"
                        f" / 缺键 {fresh['missing_key_rows']}"
                        f" / RM冲突 {fresh['rm_conflict_groups']}"
                        f" / 未校勘段 {fresh['src_unverified_segments']}")
    return (not problems), problems


def union_distinct_sources(path_strs: list[str]) -> dict:
    """多文件源段并集（「同源不相加」的机器口径）。

    SFT 与 Rewrite 是同源段的两种用法：合计样本量按本函数返回的
    union 数算；按行数相加会把同一源段重复计为独立样本。"""
    union, per = set(), {}
    for p in path_strs:
        rows = []
        with Path(p).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        sids = {r.get("segment_id") for r in rows if r.get("segment_id")}
        per[str(Path(p).name)] = len(sids)
        union |= sids
    return {"per_file": per, "union_distinct_sources": len(union),
            "sum_rows_would_overcount_by": sum(per.values()) - len(union)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="待验收的导出 jsonl")
    ap.add_argument("--union", default="",
                    help="可选：逗号分隔多文件，报同源并集（SFT×Rewrite 相加口径）")
    args = ap.parse_args()
    man = verify(args.file)
    print(json.dumps(man, ensure_ascii=False, indent=1))
    ok, problems = accept_for_training(args.file)
    print(f"[verify_training_export] 训练入口验收：{'接受' if ok else '拒绝'}")
    for p in problems:
        print(f"  - {p}")
    if args.union:
        files = [x for x in args.union.split(",") if x]
        print(json.dumps(union_distinct_sources(files), ensure_ascii=False, indent=1))
    if not man["acceptance"]["passed"] or not ok:
        raise SystemExit("验收未通过（exit 1）")
    print("[verify_training_export] PASS")


if __name__ == "__main__":
    main()
