"""长度平衡集宇宙核验 —— bal-v2 建集后必须真跑（监督 16:32 / 两席会审 09-20 二轮 BLOCK 项）。

## 为什么需要它

build_length_balanced 的 l/s_experiments 过滤是**建集时**的宇宙承诺；
「建出来的集是否真的纯净」要独立核验，不许拿建集函数自己的返回值当证据。
首建集 BS-fe40d3fd9b1a 实测 59.5% 行来自旧实验——正是没有这一步的代价。

核验口径（逐题，不用聚合自查；宁可报错不静默降级）：
1. 集合必须是 kind=length_balanced 且非空——传错 id / 空集一律拒绝，
   不给「0 mismatch / 纯净」这种假绿；
2. 预期宇宙默认读集合 spec 的 l_universe/s_universe：键存在但既不是
   "all" 也不是 list → 显式报错（不是降级成 all 让核验变 no-op）；
   两侧都未限定 → 报错要求显式传参（「all 对 all」无从核验）；
3. 每题按 answer 定人类侧——answer 不是 A/B → bad_answer；按文本长度定
   方向，EQ（两侧等长）→ mismatch（说明建集侧与核验侧的方向判定不同源）；
4. 变体文本回连 ControlledCorruption（segment_id + text 双键）：回连失败
   → variant_not_linked；同段同文本多实验 → ambiguous_link；这两种
   **只分型记录、不做宇宙判定**（拿任意一行继续判与「不猜」矛盾）；
5. L/S 任一侧行落在预期宇宙外 → outside_universe；mismatch 清单超 50 条
   截断时 JSON 里标 mismatch_truncated=true，不当完整清单卖。

## 用法

    python scripts/verify_bal_universe.py --set BS-5543d4b7ac4c
    python scripts/verify_bal_universe.py --set BS-xxx --json out/verify.json

显式 --l-experiments/--s-experiments 覆盖 spec 默认（空串=拒绝：空宇宙
没有核验意义）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                  # noqa: E402
from app.models import BenchmarkItem, BenchmarkSet  # noqa: E402
from app.models import ControlledCorruption          # noqa: E402


def _spec_universe(spec: dict, key: str, explicit) -> tuple | None:
    """读预期宇宙：显式传参优先；否则读 spec。返回 None=该侧未限定。

    spec 值为 "all"（builder 的未限定哨兵）→ None；list → tuple；
    其他类型（键名漂移/手写坏值）→ 报错，绝不静默降级成 all。"""
    if explicit is not None:
        return tuple(explicit)
    val = (spec or {}).get(key)
    if val is None or val == "all":
        return None
    if not isinstance(val, list):
        raise SystemExit(
            f"spec.{key} 存在但既不是 'all' 也不是 list（{type(val).__name__}）："
            "宇宙声明解析失败，拒绝降级成 all 继续核验（那会变 no-op）。")
    return tuple(val)


def verify_set(set_id: str, l_experiments=None, s_experiments=None) -> dict:
    with db.session() as s:
        st = s.get(BenchmarkSet, set_id)
        if st is None:
            raise SystemExit(f"集合不存在：{set_id}")
        if st.kind != "length_balanced":
            raise SystemExit(f"{set_id}（{st.name}）kind={st.kind}，"
                             "不是长度平衡集——本脚本只核验 bal 集，传错 id 不给绿。")
        l_expect = _spec_universe(st.spec, "l_universe", l_experiments)
        s_expect = _spec_universe(st.spec, "s_universe", s_experiments)
        if l_expect is None and s_expect is None:
            raise SystemExit(
                f"{set_id}（{st.name}）两侧宇宙均未限定（all 对 all），无从核验——"
                "显式传 --l-experiments/--s-experiments，或建集时先限定宇宙。")
        items = s.query(BenchmarkItem).filter_by(set_id=set_id).all()
        if not items:
            raise SystemExit(f"{set_id}（{st.name}）是空集——空集的 0 mismatch "
                             "没有判别力（builder 已拒建空集，出现即口径漂移）。")
        per_side: Counter = Counter()
        mismatch: list[dict] = []

        def flag(item, direction, reason, **extra):
            mismatch.append({"item": item.id, "direction": direction,
                             "reason": reason, **extra})

        for it in items:
            if it.answer not in ("A", "B"):
                flag(it, "?", "bad_answer", answer=it.answer)
                per_side[("BAD_ANSWER", str(it.answer))] += 1
                continue
            human = (it.text_a or "") if it.answer == "A" else (it.text_b or "")
            variant = (it.text_b or "") if it.answer == "A" else (it.text_a or "")
            if len(human) == len(variant):
                direction = "EQ"
            else:
                direction = "S" if len(human) < len(variant) else "L"
            ccs = (s.query(ControlledCorruption)
                   .filter_by(segment_id=it.segment_id, text=variant).all())
            exps = sorted({cc.experiment_id for cc in ccs}, key=str)
            if not exps:
                flag(it, direction, "variant_not_linked")
                per_side[(direction, "NOT-FOUND")] += 1
                continue
            if len(exps) > 1:
                flag(it, direction, "ambiguous_link", exps=exps)
                per_side[(direction, "AMBIGUOUS")] += 1
                continue               # 宁可报错，不拿任意一行继续猜
            exp = exps[0]
            per_side[(direction, exp)] += 1
            if direction == "EQ":
                flag(it, "EQ", "eq_length")
                continue
            expect = l_expect if direction == "L" else s_expect
            if expect is not None and exp not in expect:
                flag(it, direction, "outside_universe", exp=exp)
        counts = {f"{d}/{e}": n for (d, e), n in sorted(per_side.items())}
        shown = mismatch[:50]
        return {"set_id": set_id, "name": st.name, "n_items": len(items),
                "l_universe_expected": list(l_expect) if l_expect else "all",
                "s_universe_expected": list(s_expect) if s_expect else "all",
                "per_direction_experiment": counts,
                "n_mismatch": len(mismatch), "mismatch": shown,
                "mismatch_truncated": len(mismatch) > len(shown)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", required=True, help="BenchmarkSet id（BS-…）")
    ap.add_argument("--l-experiments", default=None,
                    help="L 侧预期宇宙，逗号分隔（默认读 spec.l_universe）")
    ap.add_argument("--s-experiments", default=None,
                    help="S 侧预期宇宙，逗号分隔（默认读 spec.s_universe）")
    ap.add_argument("--json", default=None, help="证据 JSON 输出路径")
    args = ap.parse_args()
    db.init_db()

    def _parse(val, name) -> tuple | None:
        if val is None:
            return None
        parts = tuple(x for x in val.split(",") if x)
        if not parts:
            raise SystemExit(f"--{name}-experiments 传了空串：空宇宙没有核验"
                             "意义（与不传=不限定的默认语义必须分开）。")
        return parts

    lex = _parse(args.l_experiments, "l")
    sex = _parse(args.s_experiments, "s")
    out = verify_set(args.set, l_experiments=lex, s_experiments=sex)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[verify_bal_universe] 证据已写 {args.json}")
    if out["n_mismatch"]:
        raise SystemExit(f"宇宙不纯：{out['n_mismatch']} 题异常"
                         f"（清单截断：{out['mismatch_truncated']}；exit 1）")
    print("[verify_bal_universe] 宇宙纯净：0 mismatch")


if __name__ == "__main__":
    main()
