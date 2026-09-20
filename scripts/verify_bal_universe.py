"""长度平衡集宇宙核验 —— bal-v2 建集后必须真跑（监督 16:32 / qwen 席 09-20 BLOCK 项）。

## 为什么需要它

build_length_balanced 的 l/s_experiments 过滤是**建集时**的宇宙承诺；
「建出来的集是否真的纯净」要独立核验，不许拿建集函数自己的返回值当证据。
首建集 BS-fe40d3fd9b1a 实测 59.5% 行来自旧实验——正是没有这一步的代价。

核验口径（逐题，不用聚合自查）：
1. 每题按 answer 定人类侧，按文本长度定方向（S=人类更短 / L=人类更长）；
2. 变体文本回连 ControlledCorruption（segment_id + text 双键），取行的
   experiment_id——同段同文本多行且实验不同 → ambiguous，宁可报错不猜；
3. L 侧（或 --s-experiments 时 S 侧）有任何一行落在预期宇宙外 → exit 1，
   列出题号与实验 id；EQ（两侧等长）与回连失败也计为 mismatch。

## 用法

    python scripts/verify_bal_universe.py --set BS-5543d4b7ac4c
    python scripts/verify_bal_universe.py --set BS-xxx --json out/verify.json

预期宇宙默认读集合 spec 的 l_universe/s_universe；--l-experiments 可显式覆盖。
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


def verify_set(set_id: str, l_experiments=None, s_experiments=None) -> dict:
    with db.session() as s:
        st = s.get(BenchmarkSet, set_id)
        if st is None:
            raise SystemExit(f"集合不存在：{set_id}")
        spec = st.spec or {}
        l_expect = (tuple(l_experiments) if l_experiments is not None
                    else (tuple(spec["l_universe"])
                          if isinstance(spec.get("l_universe"), list) else None))
        s_expect = (tuple(s_experiments) if s_experiments is not None
                    else (tuple(spec["s_universe"])
                          if isinstance(spec.get("s_universe"), list) else None))
        items = s.query(BenchmarkItem).filter_by(set_id=set_id).all()
        per_side: Counter = Counter()
        mismatch: list[dict] = []
        for it in items:
            human = (it.text_a or "") if it.answer == "A" else (it.text_b or "")
            variant = (it.text_b or "") if it.answer == "A" else (it.text_a or "")
            if len(human) == len(variant):
                direction = "EQ"
            else:
                direction = "S" if len(human) < len(variant) else "L"
            ccs = (s.query(ControlledCorruption)
                   .filter_by(segment_id=it.segment_id, text=variant).all())
            exps = sorted({cc.experiment_id for cc in ccs})
            if not exps:
                mismatch.append({"item": it.id, "direction": direction,
                                 "reason": "variant_not_linked"})
                per_side[(direction, "NOT-FOUND")] += 1
                continue
            if len(exps) > 1:
                mismatch.append({"item": it.id, "direction": direction,
                                 "reason": "ambiguous_link", "exps": exps})
            exp = exps[0]
            per_side[(direction, exp)] += 1
            expect = l_expect if direction == "L" else s_expect
            if direction in ("L", "S") and expect is not None and exp not in expect:
                mismatch.append({"item": it.id, "direction": direction,
                                 "exp": exp, "reason": "outside_universe"})
        counts = {f"{d}/{e}": n for (d, e), n in sorted(per_side.items())}
        return {"set_id": set_id, "name": st.name, "n_items": len(items),
                "l_universe_expected": list(l_expect) if l_expect else "all",
                "s_universe_expected": list(s_expect) if s_expect else "all",
                "per_direction_experiment": counts,
                "n_mismatch": len(mismatch), "mismatch": mismatch[:50]}


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
    lex = tuple(x for x in args.l_experiments.split(",") if x) \
        if args.l_experiments is not None else None
    sex = tuple(x for x in args.s_experiments.split(",") if x) \
        if args.s_experiments is not None else None
    out = verify_set(args.set, l_experiments=lex, s_experiments=sex)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[verify_bal_universe] 证据已写 {args.json}")
    if out["n_mismatch"]:
        raise SystemExit(f"宇宙不纯：{out['n_mismatch']} 题落在预期宇宙外/回连失败"
                         "（exit 1）")
    print("[verify_bal_universe] 宇宙纯净：0 mismatch")


if __name__ == "__main__":
    main()
