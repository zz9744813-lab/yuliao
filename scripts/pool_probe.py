"""模型池存活探针（会审 89f779e R8 待办落地，2026-09-23）。

背景（主控证据 §1）：池内残留死名（minimaxai/minimax-m3 HTTP 410 已 EOL）与
慢通道（moonshotai/kimi-k3 实测 98.4s）——真跑批量前不探活，随机选中死名
即整批复现超时故障。require_models 的池名单检查只能查「在册」，查不出
「在册但叫不醒」。

两档口径：
- 默认（无 --live）：只做 require_models 池名单预检——**零调用**，报在册
  与池外名；**池外名非空 → exit 2**（预检闸语义，与 require_models 同口径
  ——调用方不必解析 stdout 才能拦死名，9e02916 会审建议项）；
- --live + 双闸（POOL_PROBE_ALLOW_LIVE=1 + LG_LLM_MODE=real）：每模型一次
  最小真探（max_tokens=1，temperature=0，purpose=pool_probe——A05 记账），
  报 {model, ok|error, latency_ms, tokens}，超 --slow-ms（默认 30000）标
  slow。live 探针持 live_lock（R6：与 pytest/其他 live 互斥）。

退出码（会审 794521f 整改：真 exit 2，不再用 SystemExit(str)——那实际退 1）：
- 0 = 无阻断（默认档预检通过）
- 2 = 预检发现池外名（阻断；原因打印到 stderr 后 raise SystemExit(2)）

verdict 判定表（live 档，契约）：
- ok:   调用未抛异常，且 status == "ok"，且 error 为空，且 latency_ms <= slow_ms
- slow: 调用未抛异常，且 status == "ok"，且 error 为空，但 latency_ms > slow_ms（慎选）
- dead: 调用抛异常，**或软失败**（未抛异常但 status != "ok" / error 非空）——
  在册但叫不醒、以错误响应收场的死名不得判 ok（R8 要防的正是要这个场景）

用法：
    python scripts/pool_probe.py --models deepseek-v4.1-flash,z-ai/glm-5.3
    POOL_PROBE_ALLOW_LIVE=1 python scripts/pool_probe.py --models a,b --live
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import gateway                                     # noqa: E402
from app.config import LLM_MODE                             # noqa: E402

SLOW_MS_DEFAULT = 30_000


def probe_one(model: str) -> dict:
    """单模型一次最小真探（max_tokens=1）。异常不炸——探针的本职就是
    把「叫不醒」如实报出来。软失败（未抛异常但 status != "ok" / error
    非空）同样算叫不醒：ok=False，verdict 判 dead。"""
    try:
        r = gateway.chat(model=model, system="存活探针",
                         user="1", purpose="pool_probe", temperature=0.0,
                         max_tokens=1)
        alive = (getattr(r, "status", None) == "ok"
                 and not getattr(r, "error", None))
        return {"model": model, "ok": alive, "latency_ms": r.latency_ms,
                "tokens": r.tokens_in + r.tokens_out,
                "status": r.status, "error": r.error}
    except Exception as exc:                                # noqa: BLE001
        return {"model": model, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:200]}


def run_probe(models: list[str], *, slow_ms: int = SLOW_MS_DEFAULT) -> dict:
    rows = [probe_one(m) for m in models]
    for r in rows:
        r["verdict"] = ("ok" if r["ok"] and r["latency_ms"] <= slow_ms
                        else "slow" if r["ok"] else "dead")
    return {"probed": len(rows),
            "verdicts": {r["model"]: r["verdict"] for r in rows},
            "rows": rows,
            "note": "真跑批量前跑：dead 不得入选，slow 慎选（探针即 R8 闸）"}


def main() -> None:
    ap = argparse.ArgumentParser(description="模型池存活探针（R8）")
    ap.add_argument("--models", required=True,
                    help="逗号分隔模型名列表")
    ap.add_argument("--live", action="store_true",
                    help="最小真探（每模型 1 token）：需环境变量 "
                         "POOL_PROBE_ALLOW_LIVE=1（双闸）")
    ap.add_argument("--slow-ms", type=int, default=SLOW_MS_DEFAULT,
                    dest="slow_ms")
    a = ap.parse_args()
    models = [x.strip() for x in a.models.split(",") if x.strip()]
    if not models:
        raise SystemExit("--models 为空")
    if not a.live:
        # 默认档：只做池名单预检（零调用），池外名非空 → exit 2（预检闸）
        from preflight_models import preflight_block
        blocked = preflight_block(models, source="pool_probe")
        print(json.dumps({"mode": "preflight_only", "probed": 0,
                          "models": models,
                          "blocked": blocked,
                          "note": "零调用口径；存活真探须 --live + "
                                  "POOL_PROBE_ALLOW_LIVE=1"},
                         ensure_ascii=False, indent=1))
        if blocked:
            # 真 exit 2（SystemExit(str) 实际退 1，会审 794521f 整改）；
            # 原因照旧打印，与 require_models 同口径
            print(f"[预检失败] 池外名：{blocked}——拒绝放行"
                  "（预检闸语义，exit 2）", file=sys.stderr)
            raise SystemExit(2)
        return
    if os.environ.get("POOL_PROBE_ALLOW_LIVE") != "1":
        raise SystemExit("--live 需要环境变量 POOL_PROBE_ALLOW_LIVE=1（双闸）")
    if LLM_MODE != "real":
        raise SystemExit(f"--live 需要 LG_LLM_MODE=real（当前 {LLM_MODE}）")
    from app.live_guard import live_lock
    with live_lock("pool_probe"):        # R6：live 互斥
        print(json.dumps(run_probe(models, slow_ms=a.slow_ms),
                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
