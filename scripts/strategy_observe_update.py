"""观察态迁移（K2 收尾件，2026-09-23）：把「有已核对实例」的策略的
observation_status 从 hypothesis 机械翻到 observed——**只动事实层**。

纪律：
- 只动 observation_status（hypothesis→observed），**绝不碰 status 字段**
  （hypothesis→verified 是语义审查判定=拍板项，本工具无权）；
- 判据机械：该策略 strategy_instances 里 status='verified' 的行数 ≥ 1
  （证据即实例本身，可复查：SELECT strategy_id, COUNT(*) ... GROUP BY）；
- 幂等：已是 observed/replicated 的不动；0 实例的 hypothesis 不动；
- 默认 --dry-run 零库写（打印 would_update + 每策略计数）；--apply 才写。

用法：
    python scripts/strategy_observe_update.py            # 预演（零库写）
    python scripts/strategy_observe_update.py --apply    # 落迁移（写真库）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                    # noqa: E402
from app.models import ExpressionStrategyV2, StrategyInstance  # noqa: E402


def collect(s) -> dict:
    """每策略 verified 实例计数 + 当前观察态（确定性排序）。"""
    counts: dict[str, int] = {}
    for r in (s.query(StrategyInstance.strategy_id, StrategyInstance.status)
              .all()):
        if r.status == "verified":
            counts[r.strategy_id] = counts.get(r.strategy_id, 0) + 1
    rows = []
    for st in (s.query(ExpressionStrategyV2)
               .order_by(ExpressionStrategyV2.strategy_key,
                         ExpressionStrategyV2.version).all()):
        rows.append({"id": st.id, "strategy_key": st.strategy_key,
                     "version": st.version, "status": st.status,
                     "observation_status": st.observation_status,
                     "verified_instances": counts.get(st.id, 0)})
    return {"rows": rows}


def run(apply: bool) -> dict:
    with db.session() as s:
        data = collect(s)
        would, untouched = [], []
        for r in data["rows"]:
            if (r["observation_status"] == "hypothesis"
                    and r["verified_instances"] >= 1):
                would.append(r)
            else:
                untouched.append(r)
        out = {"mode": "apply" if apply else "dry_run",
               "would_update": len(would),
               "detail": [{"strategy_key": r["strategy_key"],
                           "verified_instances": r["verified_instances"]}
                          for r in would],
               "untouched": len(untouched),
               "note": "只动 observation_status（hypothesis→observed）；"
                       "status（hypothesis→verified）是语义审查拍板项，"
                       "本工具绝不触碰"}
        if apply:
            for r in would:
                st = s.get(ExpressionStrategyV2, r["id"])
                st.observation_status = "observed"
            s.commit()
            out["applied"] = len(would)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="观察态迁移（hypothesis→observed，"
                                              "仅事实层，不碰 status）")
    ap.add_argument("--apply", action="store_true",
                    help="落迁移（写库）；缺省=dry-run 零库写")
    a = ap.parse_args()
    db.init_db()
    print(json.dumps(run(a.apply), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
