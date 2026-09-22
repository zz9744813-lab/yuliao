"""K4-A 离线预置（零配额，2026-09-22）：三场配对比较的驱动与产物骨架。

四类产物（方案 K4 行结构）：
1. prose——六份正文（3 场 × 2 臂：arm A 用本场冻结 v2 知识包、
   arm B 用空包对照）；2. packages——每场的冻结包（含 package id/
   sha/来源与选中清单，不落库——真跑时经 freeze_package 落库）；
3. receipts——状态与成本收据（每臂的 receipt + usage：调用数/时长/
   token）；4. failures——失败记录（每臂的 RuntimeFault 原文）。
配对分析只做**结构性对照**（长度/状态/预算）——「是否有质量收益」
单独下结论，此处不判质量。

纪律：默认全离线（FixtureClient）；--live 显式开关才接
GatewayClient（LLM_MODE=real + 网关已配，402 资金墙未拍板前不许
真跑）；新实验目录新 Store（不动旧 3 场收据）。

    python scripts/k4_paired_scenes.py              # 离线端到端（fixture）
    python scripts/k4_paired_scenes.py --out <dir> --json
    python scripts/k4_paired_scenes.py --live --writer-model m1 --verifier-model m2   # 拍板后真跑
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.scene_runtime.contracts import (Budget, Change, Fact,  # noqa: E402
                                          KnowledgePackage, PlannedEvent,
                                          ScenePlan, World)
from app.scene_runtime.knowledge_v2 import frozen_package_for_scene  # noqa: E402
from app.scene_runtime.pipeline import SceneRunner  # noqa: E402
from app.scene_runtime.store import Store  # noqa: E402

# 3 场依次消耗一枚钱：每场独立 idem 键，revision 递增
SCENES = [("s1", 0, 3, 2, "k4-scene-1"),
          ("s2", 1, 2, 1, "k4-scene-2"),
          ("s3", 2, 1, 0, "k4-scene-3")]


def build_world() -> World:
    return World(book_id="WK-K4", revision=0, characters={"lin": "林穗"},
                facts={"coins": Fact(value=3, visible_to=["lin"]),
                       "received": Fact(value=0, visible_to=["lin"])},
                rules=[])


def build_plan(scene_id, revision, before, after, idem) -> ScenePlan:
    return ScenePlan(book_id="WK-K4", scene_id=scene_id,
                     idempotency_key=idem, expected_revision=revision,
                     pov="lin", goal=f"支付一枚钱（{scene_id}）", style="简洁",
                     min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay",
                              description="支付一枚钱",
                              changes=[Change(fact="coins", before=before,
                                             after=after)])])


class FxClient:
    """离线夹具（零真实调用）：生成可读伪正文并核验一致。"""
    models = {"writer": "fx-w", "verifier": "fx-v", "transport": "fixture"}

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            pro = payload["context"].get("knowledge", {}).get("techniques", [])
            tag = f"（用了{len(pro)}条策略）" if pro else "（空包对照）"
            body = {"text": f"林穗把一枚钱放在桌上，又收了回去。{tag}"}
        else:
            after = [c["after"] for ev in payload["plan"]["events"]
                     for c in ev["changes"]][0]
            body = {"issues": [],
                    "events": [{"event_id": ev["event_id"],
                                "quote": payload["text"]}
                               for ev in payload["plan"]["events"]],
                    "changes": [{"fact": "coins", "after": after,
                                 "quote": payload["text"]}]}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 5, "actual_model": "fx",
                "finish_reason": "stop"}


def run_paired(store: Store, client, lg_session, *, live: bool = False) -> dict:
    """3 场 × 2 臂 + 四类产物 + 结构性配对分析（不判质量）。"""
    four = {"prose": [], "packages": [], "receipts": [], "failures": []}
    for (scene_id, rev, before, after, idem) in SCENES:
        plan = build_plan(scene_id, rev, before, after, idem)
        for arm in ("A", "B"):
            try:
                if arm == "A":
                    pkg, meta = frozen_package_for_scene(
                        store, lg_session, plan)
                    four["packages"].append(
                        {"scene": scene_id, "arm": arm,
                         "package_id": pkg.package_id,
                         "source_kind": pkg.source_kind,
                         "n_techniques": len(pkg.techniques),
                         "reused": meta.get("reused")})
                else:
                    pkg = KnowledgePackage(package_id="empty",
                                           book_id=plan.book_id,
                                           source_kind="empty",
                                           techniques=[])
                runner = SceneRunner(store, client)
                receipt = runner.run(plan, pkg, Budget())
                usage = runner.store.usage(receipt["job_id"])
                four["prose"].append(
                    {"scene": scene_id, "arm": arm,
                     "text": (store.export(plan.book_id)[-1]["text"]
                              if store.export(plan.book_id) else ""),
                     "status": receipt["status"]})
                four["receipts"].append(
                    {"scene": scene_id, "arm": arm,
                     "job_id": receipt["job_id"],
                     "usage": {k: usage.get(k) for k in
                               ("calls", "duration_ms", "tokens") if k in
                               (usage or {})},
                     "live": live})
            except Exception as exc:             # noqa: BLE001
                four["failures"].append({"scene": scene_id, "arm": arm,
                                         "error": str(exc)[:300]})
    return four


def paired_analysis(four: dict) -> dict:
    """结构性配对对照（只列事实——质量收益按方案 K4 单独下结论）。"""
    by = {(p["scene"], p["arm"]): p for p in four["prose"]}
    rows = []
    for (scene_id, *_rest) in SCENES:
        a, b = by.get((scene_id, "A")), by.get((scene_id, "B"))
        rows.append({
            "scene": scene_id,
            "a_len": len(a["text"]) if a else None,
            "b_len": len(b["text"]) if b else None,
            "a_status": a["status"] if a else "failed",
            "b_status": b["status"] if b else "failed",
            "a_pkg": next((p["n_techniques"] for p in four["packages"]
                           if p["scene"] == scene_id and p["arm"] == "A"),
                          None)})
    return {"rows": rows, "n_failures": len(four["failures"]),
            "n_packages": len(four["packages"]),
            "quality_verdict": "单独下结论——本分析不判质量（方案 K4）"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="", help="产物 JSON 输出目录")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="真实调用（拍板后）：GatewayClient，LLM_MODE=real")
    ap.add_argument("--writer-model", default="")
    ap.add_argument("--verifier-model", default="")
    a = ap.parse_args()
    if a.live:
        from app.scene_runtime.client import GatewayClient
        if not (a.writer_model and a.verifier_model):
            raise SystemExit("--live 需要 --writer-model 与 --verifier-model")
        client = GatewayClient(a.writer_model, a.verifier_model)
    else:
        client = FxClient()
    import tempfile
    store = Store(Path(tempfile.mkdtemp(prefix="k4_")) / "k4.sqlite")
    store.create_world(build_world())
    from app import db
    with db.session() as s:
        four = run_paired(store, client, s, live=a.live)
    analysis = paired_analysis(four)
    out = {"artifacts": four, "analysis": analysis, "live": a.live}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.out:
        d = Path(a.out); d.mkdir(parents=True, exist_ok=True)
        (d / "k4_paired.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[k4_paired_scenes] 产物已写 {d / 'k4_paired.json'}")
    if four["failures"]:
        raise SystemExit(f"有失败臂：{len(four['failures'])} 条（exit 1）")
    print(f"[k4_paired_scenes] PASS：3 场×2 臂全 committed，"
          f"{analysis['n_packages']} 个冻结包")


if __name__ == "__main__":
    main()
