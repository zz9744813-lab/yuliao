"""K4-A 离线预置（零配额，2026-09-22）：三场配对比较的驱动与产物骨架。

四类产物（方案 K4 行结构）：
1. prose——六份正文（3 场 × 2 臂：arm A 用本场 v2 知识包、arm B 空包
   对照）；2. packages——每场 A 臂的包（package id/sha/来源/选中条数；
   **离线路径不落 LG 库**——freeze=False，真跑（--live）才 freeze）；
3. receipts——状态与成本收据（每臂 receipt + usage）；4. failures——
   失败记录（error_type + 摘要，不吞 traceback 根因）。配对分析只做
   **结构性对照**（长度/状态/预算）——「是否有质量收益」单独下结论。

离线口径（会审四轮修正）：**零配额离线跑不改真库**——run_paired 默认
freeze=False（包内容进产物、不写 knowledge_packages）；LG 异常时
rollback 再记失败，不留半成品会话态。

纪律：默认全离线（FxClient；verifier 是自证式夹具——证据门/负例
不在此 e2e 触发，由 paired_cards 卡组覆盖，勿把 e2e 绿读成门全过）；
--live 双闸（CLI flag + 环境变量 K4_ALLOW_LIVE=1）才接 GatewayClient
（LLM_MODE=real + 网关已配）——402 资金墙未拍板前不许真跑；
--out 已存在即拒（不静默覆盖上次实验产物）。

    python scripts/k4_paired_scenes.py              # 离线端到端（fixture）
    python scripts/k4_paired_scenes.py --out <dir>  # 产物落盘（不覆盖）
    python scripts/k4_paired_scenes.py --live --writer-model m1 \
        --verifier-model m2     # 拍板后：K4_ALLOW_LIVE=1 + 本命令即真跑
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import db                                   # noqa: E402
from app.scene_runtime.contracts import (Budget, Change, Fact,  # noqa: E402
                                          KnowledgePackage, PlannedEvent,
                                          ScenePlan, World)
from app.scene_runtime.knowledge_v2 import frozen_package_for_scene  # noqa: E402
from app.scene_runtime.pipeline import SceneRunner     # noqa: E402
from app.scene_runtime.store import Store              # noqa: E402

# 3 场依次消耗一枚钱：rev 按臂内累计口径（每臂独立世界从 0 起）
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
    """离线夹具（零真实调用）。⚠ verifier 自证式：quote 恒等于 payload
    文本、issues 恒空——证据门/负例不在此触发（由卡组覆盖）；「e2e
    全绿」只证结构链路，不证门行为。"""
    models = {"writer": "fx-w", "verifier": "fx-v", "transport": "fixture"}

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            pro = payload["context"].get("knowledge", {}).get("techniques", [])
            tag = f"（用了{len(pro)}条策略）" if pro else "（空包对照）"
            body = {"text": f"林穗把一枚钱放在桌上，又收了回去。{tag}"}
        else:
            changes = [c for ev in payload["plan"]["events"]
                       for c in ev["changes"]]
            body = {"issues": [],
                    "events": [{"event_id": ev["event_id"],
                                "quote": payload["text"]}
                               for ev in payload["plan"]["events"]],
                    "changes": [{"fact": changes[0]["fact"],
                                 "after": changes[0]["after"],
                                 "quote": payload["text"]}]}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 5, "actual_model": "fx",
                "finish_reason": "stop"}


def run_paired(store_factory, client, lg_session, *, live: bool = False,
               freeze: bool = False) -> dict:
    """3 场 × 2 臂 + 四类产物 + 结构性配对分析（不判质量）。

    store_factory() 每臂一次（独立平行世界；臂内 3 场共享该臂世界，
    revision 逐场递增）。同幂等键异输入必冲突（K3-B 契约）→ 两臂 idem
    键各带后缀。freeze 只在真跑（--live）时 True——离线零库写。
    **回滚口径**：freeze_package 逐臂即时 commit，已提交的冻结写不因
    另一臂 rollback 回退（rollback 只丢本臂未提交部分，每臂收据独立）。"""
    four = {"prose": [], "packages": [], "receipts": [], "failures": []}
    stores = {arm: store_factory() for arm in ("A", "B")}
    for (scene_id, rev, before, after, idem) in SCENES:
        for arm in ("A", "B"):
            store = stores[arm]
            plan = build_plan(scene_id, rev, before, after, idem + f"-{arm}")
            try:
                if arm == "A":
                    pkg, meta = frozen_package_for_scene(
                        store, lg_session, plan, freeze=freeze)
                    four["packages"].append(
                        {"scene": scene_id, "arm": arm,
                         "package_id": pkg.package_id,
                         "source_kind": pkg.source_kind,
                         "n_techniques": len(pkg.techniques),
                         "reused": meta.get("reused")})
                else:
                    pkg = KnowledgePackage(
                        package_id=f"empty-{scene_id}", book_id=plan.book_id,
                        source_kind="empty", techniques=[])
                runner = SceneRunner(store, client)
                receipt = runner.run(plan, pkg, Budget())
                usage = store.usage(receipt["job_id"])
                export = store.export(plan.book_id)
                four["prose"].append(
                    {"scene": scene_id, "arm": arm,
                     "text": (export[-1]["text"] if export else ""),
                     "status": receipt["status"]})
                four["receipts"].append(
                    {"scene": scene_id, "arm": arm,
                     "job_id": receipt["job_id"],
                     "usage": {k: usage.get(k) for k in
                               ("calls", "duration_ms", "tokens")
                               if k in (usage or {})},
                     "live": live})
            except Exception as exc:             # noqa: BLE001
                # 回滚口径（会审五轮）：freeze_package 是**逐臂即时 commit**
                # ——已提交的冻结写（含另一臂）不因本臂 rollback 回退；
                # rollback 只丢本臂未提交部分。回滚自身失败是「留半成品」
                # 信号，并进本臂记录（rollback_failed=True，rollback_error=
                # 回滚异常类名——复核人从收据可查回滚为何炸），不 pass 吞
                # （A4：局部标志必须被消费——台账可区分「仅失败」与
                # 「失败且回滚也炸」）。
                rollback_failed = False
                rollback_error = None
                try:
                    lg_session.rollback()
                except Exception as rb:          # noqa: BLE001
                    rollback_failed = True
                    rollback_error = type(rb).__name__
                four["failures"].append(
                    {"scene": scene_id, "arm": arm,
                     "error_type": type(exc).__name__,
                     "error": str(exc)[:300],
                     "rollback_failed": rollback_failed,
                     "rollback_error": rollback_error})
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
    ap.add_argument("--out", default="", help="产物 JSON 输出目录（已存在即拒）")
    ap.add_argument("--live", action="store_true",
                    help="真实调用（拍板后）：K4_ALLOW_LIVE=1 + LLM_MODE=real")
    ap.add_argument("--writer-model", default="")
    ap.add_argument("--verifier-model", default="")
    a = ap.parse_args()
    if a.out and Path(a.out).exists():
        raise SystemExit(f"--out 已存在：{a.out}——不静默覆盖上次实验产物，"
                         "换新目录（方案「新实验目录」纪律）")
    if a.live:
        if os.environ.get("K4_ALLOW_LIVE") != "1":
            raise SystemExit("--live 需要环境变量 K4_ALLOW_LIVE=1（双闸："
                            "402 资金墙未拍板前防误跑烧钱）")
        from app.scene_runtime.client import GatewayClient
        if not (a.writer_model and a.verifier_model):
            raise SystemExit("--live 需要 --writer-model 与 --verifier-model")
        client = GatewayClient(a.writer_model, a.verifier_model)
    else:
        client = FxClient()
    tmp = Path(tempfile.mkdtemp(prefix="k4_worlds_"))
    try:
        def factory():
            factory.n = getattr(factory, "n", 0) + 1
            store = Store(tmp / f"arm{factory.n}" / "k4.sqlite")
            store.create_world(build_world())
            return store
        with db.session() as s:
            four = run_paired(factory, client, s, live=a.live,
                              freeze=a.live)
        analysis = paired_analysis(four)
        out = {"artifacts": four, "analysis": analysis, "live": a.live}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        if a.out:
            d = Path(a.out)
            d.mkdir(parents=True, exist_ok=True)
            (d / "k4_paired.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=1),
                encoding="utf-8")
            print(f"[k4_paired_scenes] 产物已写 {d / 'k4_paired.json'}")
    finally:
        if not a.live:                      # 离线 fixture 世界用后即清；
            shutil.rmtree(tmp, ignore_errors=True)   # --live 留库作收据
    if four["failures"]:
        raise SystemExit(f"有失败臂：{len(four['failures'])} 条（exit 1）")
    print(f"[k4_paired_scenes] PASS：3 场×2 臂全 committed，"
          f"{analysis['n_packages']} 个 A 臂包（freeze={'True' if a.live else 'False'}）")


if __name__ == "__main__":
    main()
