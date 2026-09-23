"""训练导出验收器（审查 A03，2026-09-20；会审 09-21 三轮加固）。

## 为什么需要它

审查实测（09-19 旧导出）：SFT/Rewrite 各 114 行与冻结基准文本重合、
32 行源段未校勘、RM 有 172 组同源同文不同分——源码修复后这些文件
**没有重新生成**，旧文件继续躺在 exports/ 里等着被误拿去训练。

本脚本把「这份导出能不能拿去训练」变成可机检、**防手改**的契约：

1. **隔离**：逐行目标+前文按「忽略空白、≥50 字」口径（审查复算法）
   对全库冻结基准文本哈希重算重合——导出侧排除逻辑失效时这里必须红。
   基准哈希集为空（连错库/基准未冻结）→ 直接拒；**每行的目标字段
   （writer_sft:target / rewrite:output / rm:text）必须非空**——置空/
   占位/半漂移 schema → 拒（三轮 BLOCK 项：空串占位混不进比较数）；
   目标**短于 50 字**的行是合法短文本（真件实测 73/73/48 行），只降
   低隔离复算的覆盖、不入闸——进 manifest 报 rows_target_short，闸的
   是 rows_target_empty（空=导出坏了，短=数据本来就短，两回事）。
2. **冲突消解**（RM）：同源同文不同分的组数必须为 0；分数不可哈希
   （dict/list）→ 拒，不裸炸。
3. **源质量**：逐行回连段 integrity.src_ok，**严格三态**（审查 F-1 收口）：
   JSON true → 通过；JSON false → 判坏；任何非布尔（"true"/"false"/1/0/
   []/{} /null/缺键）→ 未校验。判坏段、未校验段与悬空段（库中查无）
   **都入闸**且都进 manifest——fail-closed，不许一个闸一个抛（三轮 BLOCK
   项；bool("false")==True 的松口径在此废止）。
4. **防手改**：manifest 是旁挂明文，手改 passed=true 挡不住——
   accept_for_training **重跑 verify 的全部纯检查**并与 manifest 的
   sha256/kind/行数/基准哈希数交叉核对；无 manifest（旧导出）、
   sha 不符（重导前旧文件/被改动）、交叉不一致 → 一律拒收；
   verify 的 SystemExit 在 accept 侧转结构化 (False, problems)——
   训练入口对每个文件都能拿到拒收理由，不在第一个坏文件上裸崩。
5. **同源不相加**：union_distinct_sources() 报源段并集 + 两种相加
   口径的虚增量（按行数 / 按源段数）——SFT 与 Rewrite 是同源段两种
   用法，合计只按并集算。

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

# 每类导出参与重合检查的字段：目标 + 前文（审查：「比较范围包括目标与前文」）。
# 契约（三轮会审）：字段名须与 export_training 的行 schema 一致——导出器改键名
# 而验收器不跟，会静默少比。tests 里有源码绊线（改字段名必须两头一起改）。
_KIND_FIELDS = {
    "writer_sft": ("target", "prev1", "prev2"),
    "rewrite": ("output", "context"),
    "rm": ("text", "prev1", "prev2"),
}
# 每行必须产出 ≥MIN_CHARS 可比文本的字段（目标）：置空/占位 = 半漂移，拒
_KIND_REQUIRED = {"writer_sft": "target", "rewrite": "output", "rm": "text"}
_KINDS = {"sft": ("writer_sft", "--sft"), "rewrite": ("rewrite", "--rewrite"),
          "rm": ("rm", "--rm")}


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
    required = _KIND_REQUIRED[kind]
    # 走导出模块的显式接口作废缓存——不戳私有名（改名后静默不生效
    # = 基准哈希陈旧 = 重合漏检）
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
                r = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"第 {i + 1} 行不是合法 JSON：{e}")
            if not isinstance(r, dict):
                raise SystemExit(f"第 {i + 1} 行不是 JSON 对象（拿到 "
                                 f"{type(r).__name__}）——导出损坏，拒收")
            rows.append(r)

    overlap_rows, overlap_examples = 0, []
    missing_key_rows = 0
    n_texts_seen = 0          # 取到字段的文本数（三轮 BLOCK：与可比数分列，
    n_texts_comparable = 0    # ≥MIN_CHARS 规范化后的可比文本数——占位空串混不进）
    rows_target_empty = 0     # 目标字段空/缺失 = 半漂移，入闸
    rows_target_short = 0    # 目标非空但 <50 字 = 合法短文本，只报不闸
    sources = set()
    for i, r in enumerate(rows):
        sid = r.get("segment_id")
        if not sid:
            missing_key_rows += 1
        else:
            sources.add(sid)
        hit = False
        target_nonempty = target_comparable = False
        for fld in fields:
            for t in _texts_of(r.get(fld)):
                n_texts_seen += 1
                norm = "".join(t.split())
                if len(norm) >= MIN_CHARS:
                    n_texts_comparable += 1
                if fld == required and norm:
                    target_nonempty = True
                    if len(norm) >= MIN_CHARS:
                        target_comparable = True
                if len(norm) >= MIN_CHARS and _md5(norm) in bench:
                    hit = True
                    break
            if hit:
                break
        if not target_nonempty:
            rows_target_empty += 1
        elif not target_comparable:
            rows_target_short += 1
        if hit:
            overlap_rows += 1
            if len(overlap_examples) < 10:
                overlap_examples.append({"line_no": i + 1,
                                          "line_id": r.get("id"), "field": fld})
    if n_texts_comparable == 0:
        raise SystemExit(
            f"0 条可比文本（≥{MIN_CHARS} 字）——{kind} 行里字段 {fields} 全空/全短"
            "（导出 schema 漂移？）：比较空转还报 PASS 就是最危险的假绿")

    rm_conflicts, rm_bad_score = 0, 0
    if kind == "rm":
        groups: dict[tuple, set] = defaultdict(set)
        for r in rows:
            score = r.get("score")
            if isinstance(score, (dict, list)):
                rm_bad_score += 1          # 不可哈希的多维分数：计数，不裸炸
                continue
            key = (r.get("segment_id"), "".join((r.get("text") or "").split()))
            groups[key].add(score)
        rm_conflicts = sum(1 for v in groups.values() if len(v) > 1)

    # 源质量：按源段批量回连（in_，不逐段查）。判坏/未校验/悬空三态全入闸——
    # 悬空=导出与库不同源，比未校勘更严重，但同属源质量家族，统一入清单。
    # 严格三态（与 source_check.parse_src_ok 同口径，fail-closed）：
    # JSON true → 通过；JSON false → 判坏；其余一切非布尔（"true"/"false"/
    # 1/0/[]/{}/null/缺键，integrity 非法/非 dict）→ 未校验——
    # 旧 bool("false") 为真的松口径（审查 F-1）在此收口：字符串 "false"
    # 决不能再冒充通过。
    src_ok_n, src_bad, src_unverified, src_missing = 0, 0, 0, 0
    with db.session() as s:
        seg_rows = {sid: integ for sid, integ in
                    s.query(Segment.id, Segment.integrity)
                    .filter(Segment.id.in_(sources)).all()} if sources else {}
    for sid in sources:
        if sid not in seg_rows:
            src_missing += 1
            continue
        integ = seg_rows[sid]
        if isinstance(integ, str):
            try:
                integ = json.loads(integ)
            except Exception:
                integ = None
        v = integ.get("src_ok") if isinstance(integ, dict) else None
        if v is True:
            src_ok_n += 1
        elif v is False:
            src_bad += 1
        else:
            src_unverified += 1
    # 三桶互斥自洽：通过+判坏+未校验+悬空 == 去重后的总源段数
    assert (src_ok_n + src_bad + src_unverified + src_missing
            == len(sources)), "源质量计数口径自洽性破坏（桶重叠/漏计）"

    passed = (overlap_rows == 0 and missing_key_rows == 0
              and rm_conflicts == 0 and rm_bad_score == 0
              and src_bad == 0 and src_unverified == 0   # fail-closed：判坏/未校验>0 即红
              and src_missing == 0
              and rows_target_empty == 0)
    man = {
        "file": str(path), "kind": kind, "sha256": _sha256(path),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_rows": len(rows), "n_distinct_sources": len(sources),
        "n_bench_hashes": len(bench),   # 基准指纹：换小库重算，交叉核对能拦住
        "n_texts_seen": n_texts_seen, "n_texts_comparable": n_texts_comparable,
        "rows_target_empty": rows_target_empty, "rows_target_short": rows_target_short,
        "bench_overlap_rows": overlap_rows,
        "bench_overlap_examples": overlap_examples,
        "rm_conflict_groups": rm_conflicts, "rm_bad_score_rows": rm_bad_score,
        "missing_key_rows": missing_key_rows,
        "src_ok_segments": src_ok_n,
        "src_bad_segments": src_bad,
        "src_unverified_segments": src_unverified,
        "src_missing_segments": src_missing,
        "same_source_note": "SFT 与 Rewrite 同源段是同一批样本的两种用法——"
                            "合计只按 union_distinct_sources 并集算，不许按行数相加",
        "acceptance": {"passed": passed,
                       "rule": "重合=0 且 主键齐全 且（RM）同源同文无多分且分数可哈希 "
                               "且 源段判坏=0 且 未校验源段=0（非严格布尔一律未校验，"
                               "fail-closed）且 悬空源段=0 且 目标字段无空值行"
                               "（目标短于50字只报不闸：rows_target_short）"},
    }
    if write_manifest:
        out = path.with_suffix(".manifest.json")
        out.write_text(json.dumps(man, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"[verify_training_export] 清单已写 {out}")
    return man


def accept_for_training(path_str: str) -> tuple[bool, list[str]]:
    """训练入口契约：只认「manifest 旁挂 + 与文件本体交叉一致 + 当场重算通过」。

    verify 的结构性 SystemExit（坏 JSON/空基准库/空比较等）在这里转成
    (False, problems)——入口对每个文件都拿得到拒收理由，不在第一个
    坏文件上裸崩。手改 manifest 的 passed 位同样挡不住：当场重跑全部
    纯检查并与旁挂清单交叉核对。"""
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
    try:
        fresh = verify(path_str, write_manifest=False)
    except SystemExit as e:
        return False, [f"当场重算结构性失败：{e}"]
    problems = []
    if man.get("sha256") != fresh["sha256"]:
        problems.append("manifest sha256 与文件本体不符（清单过期/文件被改）——拒收")
    if man.get("kind") != fresh["kind"] or man.get("n_rows") != fresh["n_rows"]:
        problems.append("manifest kind/行数与文件不符（备份文件/张冠李戴）——拒收")
    if man.get("n_bench_hashes") != fresh["n_bench_hashes"]:
        problems.append("基准哈希数与 manifest 不符（基准侧被换过）——拒收")
    if not fresh["acceptance"]["passed"]:
        problems.append(f"当场重算未通过：重合 {fresh['bench_overlap_rows']}"
                        f" / 缺键 {fresh['missing_key_rows']}"
                        f" / RM冲突 {fresh['rm_conflict_groups']}"
                        f" / 源判坏段 {fresh['src_bad_segments']}"
                        f" / 未校勘段 {fresh['src_unverified_segments']}"
                        f" / 悬空段 {fresh['src_missing_segments']}"
                        f" / 目标空值行 {fresh['rows_target_empty']}")
    return (not problems), problems


def union_distinct_sources(path_strs: list[str]) -> dict:
    """多文件源段并集 + 两种相加口径的虚增量（「同源不相加」的机器口径）。

    虚增口径分列（三轮会审）：按**行数**相加的虚增（把同一源段的每个
    样本当独立样本）与按**各文件源段数**相加的虚增，名字、算法、文案
    三处一致；合计样本量只认 union_distinct_sources。"""
    union: set = set()
    per: dict[str, dict] = {}
    for p in path_strs:
        rows, n_bad = 0, 0
        sids: set = set()
        with Path(p).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    n_bad += 1
                    continue
                rows += 1
                if isinstance(r, dict) and r.get("segment_id"):
                    sids.add(r["segment_id"])
        per[str(Path(p).name)] = {"n_rows": rows, "n_distinct_sources": len(sids),
                                  "n_bad_lines": n_bad}
        union |= sids
    sum_rows = sum(v["n_rows"] for v in per.values())
    sum_srcs = sum(v["n_distinct_sources"] for v in per.values())
    return {"per_file": per, "union_distinct_sources": len(union),
            "sum_rows": sum_rows,
            "overcount_if_summing_rows": sum_rows - len(union),
            "sum_distinct_sources": sum_srcs,
            "overcount_if_summing_sources": sum_srcs - len(union),
            "n_bad_lines": sum(v["n_bad_lines"] for v in per.values())}


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
