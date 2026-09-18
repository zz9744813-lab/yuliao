"""评委间一致度诊断（2026-09-16）。

回答一个此前没量化过的问题：**两个独立评委之间能对上多少？**

为什么重要：这是"单个评委对用户能做到的上限"的参照物。
若两个独立的强模型彼此只有勉强过半的一致度，说明这个任务对它们接近抛硬币，
那么它们对用户的 κ 也就不可能高——**问题在任务/信号，不在 prompt 措辞**。

用法：
    python scripts/interjudge.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import heldout_eval as HE  # noqa: E402


def _kpair(pairs: list[tuple[int, int]]) -> float:
    """两把尺的 κ（pairs 是 (judgeA, judgeB) 的 0/1）。"""
    n = len(pairs)
    if not n:
        return float("nan")
    ag = sum(1 for x, y in pairs if x == y) / n
    p1 = sum(x for x, _ in pairs) / n
    p2 = sum(y for _, y in pairs) / n
    pe = p1 * p2 + (1 - p1) * (1 - p2)
    return (ag - pe) / (1 - pe) if pe < 1 else float("nan")


def main() -> None:
    con = sqlite3.connect(HE.DB)
    con.row_factory = sqlite3.Row
    u25 = {x["cid"]: (1 if x["user"] == "human" else 0)
           for x in HE.load_items(True, "r25", HE.BATCH_EPOCH)}
    u30 = {x["cid"]: (1 if x["user"] == "human" else 0)
           for x in HE.load_items(True, "s30", None)}
    user = {**u25, **u30}
    cids = list(user)
    print(f"合并留出 n={len(cids)}（r25 {len(u25)} + s30 {len(u30)}）")
    print(f"人胜率 = {np.mean(list(user.values())):.3f}\n")

    print("=== 评委之间的一致度（同一序）vs 各自对用户 ===")
    hdr = f"{'口径':6s}{'双方有效n':>10s}{'模型间一致':>12s}{'模型间κ':>10s}"
    hdr += f"{'kimi对用户κ':>14s}{'deepseek对用户κ':>18s}"
    print(hdr)
    for v in ("v3", "v4"):
        base = HE.PROMPT_VARIANTS[v][1]
        km, ds, pair = [], [], []
        for c in cids:
            a = HE._read_verdict(c, HE.JUDGES[0], base, con)
            b = HE._read_verdict(c, HE.JUDGES[1], base, con)
            if a is not None:
                km.append((user[c], 1 if a == "human" else 0))
            if b is not None:
                ds.append((user[c], 1 if b == "human" else 0))
            if a is not None and b is not None:
                pair.append(((1 if a == "human" else 0), (1 if b == "human" else 0)))
        ag = sum(1 for x, y in pair if x == y) / len(pair) if pair else float("nan")
        print(f"{v:6s}{len(pair):10d}{ag:12.3f}{_kpair(pair):+10.3f}"
              f"{HE._kappa(km):+14.3f}{HE._kappa(ds):+18.3f}")

    # 两序合并后的一致性（若两序真的去掉了位置噪声，模型间一致应上升）
    print("\n=== 两序合并后的模型间一致 ===")
    for v in ("v3", "v4"):
        base = HE.PROMPT_VARIANTS[v][1]
        pair = []
        for c in cids:
            a = HE._two_order_verdict(c, HE.JUDGES[0], base, con)
            b = HE._two_order_verdict(c, HE.JUDGES[1], base, con)
            if a is not None and b is not None:
                pair.append(((1 if a == "human" else 0), (1 if b == "human" else 0)))
        ag = sum(1 for x, y in pair if x == y) / len(pair) if pair else float("nan")
        print(f"  {v}: n={len(pair)}  一致={ag:.3f}  κ={_kpair(pair):+.3f}")

    print("\n【怎么读】")
    print("  · 模型间一致 ≈ 0.5 且 κ ≈ 0 → 两个强模型在这个任务上接近抛硬币，")
    print("    说明**信号弱/任务难**，继续调 prompt 措辞收益有限。")
    print("  · 若模型间一致高但各自对用户都低 → 它们共享同一套「非用户」的偏好，")
    print("    属于口径问题，值得继续对齐（这是 v3→v4 那条路）。")
    print("  · 两序合并后模型间一致若明显上升 → 位置噪声确实是共同噪声源。")
    con.close()


if __name__ == "__main__":
    main()
