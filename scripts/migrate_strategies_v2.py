"""K1-B v1→v2 策略迁移（知识化方案 §4.4 迁移纪律 + §2 决策「旧 8 条策略
只转成待验证假设」）：v1 expression_strategies → v2 hypothesis 行。

保守映射（逐字段声明，不许静默重解释）：
- abstract_operation ← v1 name+description（聚类归纳的操作描述）；
- failure_modes ← v1 avoid（该避免的写法）；
- effect_hypothesis ← **未定**——v1 没有效果假设字段，不造一个；
- status/observation_status = hypothesis（方案明令：不自动转成 observed）；
- effect_status = untested——v1 的 success_rate 是**偏好胜率**，不是表达
  效果证明，「不覆盖、不删除、不静默重解释 success_rate」（§4.4）；
- scope = UNCERTAIN（v1 无范围依据）；
- legacy_strategy_id 记 v1 id（迁移表：old ID → 新版本及采用依据）。

幂等：同 legacy_strategy_id 已迁移则跳过。v1 表零改动。
回滚：v2 五张表为纯新增，回滚 = DROP 五张表（tests/test_knowledge_v2.py
钉死 v1 不受影响）。

    python scripts/migrate_strategies_v2.py --dry-run
    python scripts/migrate_strategies_v2.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                     # noqa: E402
from app.models import ExpressionStrategy, ExpressionStrategyV2  # noqa: E402

ADOPTION_BASIS = ("v1 聚类产物转待验证假设（方案 §2：旧策略只转成 hypothesis，"
                  "不自动转成 observed；success_rate 是偏好胜率，不作为表达"
                  "效果证明复制或重解释）")


def migrate(dry_run: bool = False) -> list[dict]:
    out: list[dict] = []
    with db.session() as s:
        done = {r.legacy_strategy_id for r in
                s.query(ExpressionStrategyV2).all()
                if r.legacy_strategy_id}
        for v1 in s.query(ExpressionStrategy).all():
            if v1.id in done:
                out.append({"legacy_id": v1.id, "action": "skip",
                            "note": "已迁移（幂等）"})
                continue
            row = {
                "legacy_id": v1.id, "action": "seed",
                "strategy_key": f"legacy:{v1.name}",
                "abstract_operation": f"{v1.name}——{v1.description or ''}".strip(),
                "failure_modes": list(v1.avoid or []),
                "observation_status": "hypothesis",
                "effect_status": "untested",
                "scope": "UNCERTAIN",
            }
            out.append(row)
            if not dry_run:
                s.add(ExpressionStrategyV2(
                    strategy_key=row["strategy_key"], version=1,
                    abstract_operation=row["abstract_operation"],
                    invariants=list(v1.recommended or []),
                    effect_hypothesis="未定——v1 聚类产物无效果假设字段",
                    failure_modes=row["failure_modes"],
                    status="hypothesis",
                    source=f"legacy_v1:{v1.method or 'cluster'}",
                    legacy_strategy_id=v1.id,
                    scope="UNCERTAIN", scope_ids=[], scope_basis=ADOPTION_BASIS,
                    observation_status="hypothesis", effect_status="untested"))
        if not dry_run:
            s.commit()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db.init_db()
    rows = migrate(dry_run=args.dry_run)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    n_seed = sum(1 for r in rows if r["action"] == "seed")
    print(f"[migrate_strategies_v2] {'将转' if args.dry_run else '已转'} "
          f"{n_seed} 条 v1 → v2 待验证假设（hypothesis；"
          "success_rate 不复制不重解释）")


if __name__ == "__main__":
    main()
