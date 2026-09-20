"""训练入口（审查 A03 契约接线，2026-09-21）。

## 为什么需要它

「训练入口必须拒绝未经此验收的旧版本」——会审指出：验收函数写成工具
函数、没人调用 = 可被绕过。本脚本就是那个**入口**：任何 SFT/Rewrite/RM
数据要进训练，必须从这里走 accept_for_training；拒绝即非零退出，
不产生任何训练产物。每个参数位还校验文件 kind（--sft 必须真是
writer_sft 导出，张冠李戴拒收）。

## 边界（诚实声明）

实际训练流程仍然后置（未实现，见训练可行性文档）——本入口当前**只做
验收门**：验收通过打印各文件摘要后退出 0；任何一项拒绝即退出 1。
「不可绕过」的真正强制点在训练流程实现时必须以本入口为先决——
这是**已记账的未闭环风险**，不是已闭环的事实。

## 用法

    python scripts/train_entry.py --sft data/exports/writer_sft_v3.jsonl \
        --rewrite data/exports/rewrite_v2.jsonl --rm data/exports/rm_v1.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_training_export as VT              # noqa: E402

# 参数位 → 必须的导出类型（张冠李戴拒收）：train_entry 的 --sft 传 rm 文件
# 会被 verify 认成 rm——本表在验收前先对上 kind。
_ARG_KIND = {"--sft": "writer_sft", "--rewrite": "rewrite", "--rm": "rm"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sft", default="", help="SFT 导出（writer_sft_*.jsonl）")
    ap.add_argument("--rewrite", default="", help="Rewrite 导出（rewrite_*.jsonl）")
    ap.add_argument("--rm", default="", help="RM 导出（rm_*.jsonl）")
    args = ap.parse_args()
    files = [x for x in (args.sft, args.rewrite, args.rm) if x]
    if not files:
        raise SystemExit("什么都没给：--sft/--rewrite/--rm 至少其一")

    all_ok = True
    for flag, f in (("--sft", args.sft), ("--rewrite", args.rewrite), ("--rm", args.rm)):
        if not f:
            continue
        try:
            kind = VT._kind_of(Path(f))
        except SystemExit as e:
            print(f"[FAIL] {f}\n    - {e}")
            all_ok = False
            continue
        if kind != _ARG_KIND[flag]:
            print(f"[FAIL] {f}\n    - {flag} 必须是 {_ARG_KIND[flag]} 导出，"
                  f"实为 {kind}——张冠李戴拒收")
            all_ok = False
            continue
        ok, problems = VT.accept_for_training(f)
        print(f"{'[PASS]' if ok else '[FAIL]'} {f}")
        for p in problems:
            print(f"    - {p}")
        all_ok &= ok

    # 同源不相加：SFT 与 Rewrite 都给时，样本量按源段并集报
    if args.sft and args.rewrite:
        try:
            u = VT.union_distinct_sources([args.sft, args.rewrite])
            s = Path(args.sft).name
            r = Path(args.rewrite).name
            print(f"同源口径：SFT {u['per_file'][s]['n_distinct_sources']} 源段 × "
                  f"Rewrite {u['per_file'][r]['n_distinct_sources']} 源段"
                  f" → 并集 {u['union_distinct_sources']}"
                  f"（按行数相加虚增 {u['overcount_if_summing_rows']}、"
                  f"按各文件源段数相加虚增 {u['overcount_if_summing_sources']}，"
                  "合计只认并集）")
        except SystemExit as e:
            print(f"[FAIL] 同源并集计算失败：{e}")
            all_ok = False

    if not all_ok:
        raise SystemExit("训练入口验收：拒绝（exit 1）——先跑 "
                         "scripts/verify_training_export.py 生成合格 manifest")
    print("训练入口验收：全部通过。实际训练流程后置——本入口当前只做验收门。")


if __name__ == "__main__":
    main()
