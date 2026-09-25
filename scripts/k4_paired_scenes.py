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
（LLM_MODE=real + 网关已配）——402 资金墙未拍板前不许真跑；R6 互斥
守卫前置于任何客户端构造/模型解析（锁在 ⇒ 立刻拒，与环境配置无关）；
--out 已存在即拒（不静默覆盖上次实验产物）。

    python scripts/k4_paired_scenes.py              # 离线端到端（fixture，默认 3 场）
    python scripts/k4_paired_scenes.py --out <dir>  # 产物落盘（不覆盖）
    python scripts/k4_paired_scenes.py --scenes 10 --out <dir>
                                                   # 10 场离线预演（零配额）
    python scripts/k4_paired_scenes.py --live --writer-model m1 \
        --verifier-model m2     # 拍板后：K4_ALLOW_LIVE=1 + 本命令即真跑

审计整改（P0 主线第 2 条，2026-09-24）：世界可配（--book-id，默认 WK-K4 行为
不变）；新增**只读预检** `--preflight`（零生成调用、零库写）——查 work_sources
是否登记该 book_id、并调 K3 只读 query_knowledge 判断 A 臂包是否非空，打印可核对
JSON，未登记/空包即非零退出。--live 路径叠加同一道闸（仅 LLM_MODE=real 时生效，
mock 不烧钱故跳过以保双闸测试）：过闸才许起真实调用，否则拒绝起跑、零真实调用。
禁止用未登记 WK-K4 虚构场景把空包对照当真跑证据。

    python scripts/k4_paired_scenes.py --preflight \
        --book-id WK-dc90993434e9   # 只读预检：WK-K4 默认也行
    python scripts/k4_paired_scenes.py --live --book-id WK-dc90993434e9 \
        --writer-model m1 --verifier-model m2   # 过闸才真跑

审计非阻断项收口（2026-09-25，docs/K4_世界目录收据_20260924.md）：live 收据
（four["receipts"] 逐条）补记 worlds_dir（产物世界目录，绝对路径）+
worlds_dir_exists + created_at（目录创建时刻 ISO8601 UTC）+ worlds_dir_cleaned
（正常收口留库=False 供 tokens 对账；异常/中断且收据未落盘时保守清理本次自建的
临时目录=True——清理只作用于本次 mkdtemp 自建目录，绝不碰他处）。
离线（非 live）路径的收据字段与产物 JSON 逐字不变。
"""
from __future__ import annotations

import argparse
import datetime
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


def scenes_for(n: int) -> list:
    """--scenes N 的场景派生（纯函数，可离线核验）。
    n<=3 恒等于 SCENES 前缀（默认 3 场口径零改动）；n>3 按序扩展：
    s4 起世界转入 received 逐场 +1（coins 已在 s3 耗尽，received 初始 0），
    每场 before 与世界实际状态精确匹配——扩展场无虚假前置条件。"""
    if n < 1:
        raise ValueError(f"scenes_for: n>=1 required, got {n}")
    specs = [{"scene_id": s, "rev": r, "before": b, "after": a, "idem": i,
              "fact": "coins", "goal": f"支付一枚钱（{s}）",
              "desc": "支付一枚钱"}
             for (s, r, b, a, i) in SCENES[:min(n, len(SCENES))]]
    for i in range(len(SCENES) + 1, n + 1):
        specs.append({"scene_id": f"s{i}", "rev": i - 1,
                      "before": i - 4, "after": i - 3,
                      "idem": f"k4-scene-{i}", "fact": "received",
                      "goal": f"收到一枚钱（s{i}）", "desc": "收到一枚钱"})
    return specs


def build_world(book_id: str = "WK-K4") -> World:
    return World(book_id=book_id, revision=0, characters={"lin": "林穗"},
                 facts={"coins": Fact(value=3, visible_to=["lin"]),
                        "received": Fact(value=0, visible_to=["lin"])},
                 rules=[])


def build_plan(scene_id, revision, before, after, idem, *,
               fact="coins", goal=None, desc=None,
               book_id: str = "WK-K4") -> ScenePlan:
    return ScenePlan(book_id=book_id, scene_id=scene_id,
                     idempotency_key=idem, expected_revision=revision,
                     pov="lin",
                     goal=goal if goal is not None else f"支付一枚钱（{scene_id}）",
                     style="简洁",
                     min_chars=1, max_chars=500,
                     events=[PlannedEvent(event_id="pay",
                              description=desc if desc is not None else "支付一枚钱",
                              changes=[Change(fact=fact, before=before,
                                             after=after)])])


def build_a_arm_policy(book_id: str) -> dict:
    """A 臂知识包 policy——与 app/scene_runtime/knowledge_v2.frozen_package_for_scene
    逐字一致的 policy 构造（knowledge_v2 不可改，这里照抄唯一入口；book_id 仅入
    参 echo，query_knowledge 为全库只读查询，不按 book_id 收窄——故「A 臂非空」
    取决于 work_sources 登记 + 全库有 verified 策略两条）。plan_sha256 在
    query_knowledge 侧不参与过滤，预检无需复算 digest。"""
    from app.knowledge import PACKAGE_CONTRACT_VERSION
    s0 = SCENES[0]
    plan = build_plan(s0[0], s0[1], s0[2], s0[3], s0[4], book_id=book_id)
    return {"contract_version": PACKAGE_CONTRACT_VERSION,
            "book_id": plan.book_id, "branch_id": plan.branch_id,
            "scene_id": plan.scene_id,
            "semantic_requirements": {"goal": plan.goal, "pov": plan.pov,
                                     "style": plan.style},
            "limits": {"context_items": 3}}


def preflight_world(book_id: str, s) -> dict:
    """只读预检（零生成调用、零库写）：
    - 查 work_sources 是否登记该 book_id；
    - 调 K3 只读 query_knowledge 判断该世界 A 臂包是否非空；
    返回可核对 dict：book_id / registered / k3_status / selected_ids /
    n_techniques / empty_reason。不抛异常、不退出——退出决策交给调用方。

    这是闸，不是提示：registered=False 或 k3_status!="matched" 即「A 臂空」，
    调用方（--preflight / --live）必须据此非零退出，绝不允许把空包对照当真跑
    证据（审计 P0 主线第 2 条禁的动作）。"""
    from app import knowledge_query as kq
    from app.models import WorkSource, ExpressionStrategyV2
    from app.knowledge import PACKAGE_CONTRACT_VERSION
    registered = s.query(WorkSource).filter(
        WorkSource.work_id == book_id).first() is not None
    resp = kq.query_knowledge(build_a_arm_policy(book_id), s)
    selected = resp.get("selected") or []
    selected_ids = [e.get("strategy_id") for e in selected]
    k3_status = resp.get("status")
    n_techniques = len(selected)
    empty_reason = None
    if not registered:
        empty_reason = (f"world_not_registered:work_sources 无 book_id="
                        f"{book_id} 的登记行（K4 必须先用有登记、可匹配的真实"
                        f"试点世界，禁止用未登记 WK-K4 虚构场景当真跑证据）")
    elif k3_status != "matched":
        # 真实根因：全库策略状态分布（eligible_statuses 只认 verified）
        dist = {}
        for (st,) in s.query(ExpressionStrategyV2.status).all():
            dist[st] = dist.get(st, 0) + 1
        verified = dist.get("verified", 0)
        empty_reason = (f"empty_package:k3_status={k3_status}；全库策略状态"
                        f"分布={dist}，verified={verified}（eligible_statuses "
                        f"只认 verified ⇒ A 臂知识包恒空，无合格证据可进包）")
    return {"book_id": book_id, "registered": registered,
            "k3_status": k3_status, "selected_ids": selected_ids,
            "n_techniques": n_techniques, "empty_reason": empty_reason}


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
               freeze: bool = False, n_scenes: int = 3,
               channel_changed: bool = False,
               worlds_dir: str | None = None,
               worlds_created_at: str | None = None) -> dict:
    """N 场（默认 3=SCENES；扩展场派生见 scenes_for）× 2 臂 + 四类产物
    （prose/packages/receipts/failures）+ skipped（断臂后未执行的后续场
    单列，不进 failures——C2/止损台账不被连锁幻影污染，2026-09-23 会审
    整改）+ 结构性配对分析（不判质量）。

    store_factory() 每臂一次（独立平行世界；臂内各场共享该臂世界，
    revision 逐场递增）。同幂等键异输入必冲突（K3-B 契约）→ 两臂 idem
    键各带后缀。freeze 只在真跑（--live）时 True——离线零库写。
    **回滚口径**：freeze_package 逐臂即时 commit，已提交的冻结写不因
    另一臂 rollback 回退（rollback 只丢本臂未提交部分，每臂收据独立）。

    worlds_dir / worlds_created_at：本次跑的世界目录（main() 里
    tempfile.mkdtemp(prefix="k4_worlds_") 自建）及其创建时刻（ISO8601
    UTC）。仅 live 消费——审计非阻断项收口（2026-09-25）：live 收据逐条
    记 worlds_dir/worlds_dir_exists/created_at，缺参即拒跑（不带无收据的
    真跑）；离线（live=False）不消费、收据字段逐字不变。"""
    if live:
        if worlds_dir is None or worlds_created_at is None:
            raise ValueError("run_paired: live 跑必须记 worlds_dir 与 "
                             "worlds_created_at——无收据地址不许起真跑"
                             "（审计非阻断项收口 2026-09-25）")
        worlds_dir_abs = str(Path(worlds_dir).resolve())
    four = {"prose": [], "packages": [], "receipts": [], "failures": [],
            "skipped": []}
    stores = {arm: store_factory() for arm in ("A", "B")}
    failed_at = {"A": None, "B": None}     # 本臂首个失败场（None=未断）
    for sp in scenes_for(n_scenes):
        scene_id = sp["scene_id"]
        for arm in ("A", "B"):
            if failed_at[arm] is not None:
                # 会审整改（89f779e BLOCK）：SCENES 的 revision 阶梯按「前场
                # 成功推进」硬编码——前场失败后本臂世界停在旧 revision，后续
                # 场必然 world_revision_conflict（首轮真跑实测：6 条失败里 4
                # 条是这类连锁幻影）。故断臂即停：后续场**不执行、不烧调用**，
                # 单列 skipped（failures 只记真实独立失败，C2/止损台账不被
                # 幻影污染）；另一臂独立世界不受影响。
                four["skipped"].append(
                    {"scene": scene_id, "arm": arm,
                     "skipped_after": failed_at[arm],
                     "reason": "前场失败，本臂世界未推进，本场景未执行"})
                continue
            store = stores[arm]
            plan = build_plan(scene_id, sp["rev"], sp["before"], sp["after"],
                              sp["idem"] + f"-{arm}", fact=sp["fact"],
                              goal=sp["goal"], desc=sp["desc"])
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
                # usage 三键恒在（tokens 缺记=0：fixture 零真实消耗如实
                # 记 0；live 走网关实账）——K5-A §6 止损命令不因缺键空转
                u = usage or {}
                # 通道/模型/重试留痕（主控 K4-健壮化取证件 2026-09-23）：
                # 收据写清 writer/verifier 请求模型与网关主机；verifier
                # 无效重试（stage+'.retry'）逐次列明——两次尝试的通道名由
                # 此可核（实际路由被校准网关隐藏，记请求侧，client.py 口径）。
                gw_host = ""
                if live:
                    from app import config as _cfg
                    gw_host = (_cfg.GATEWAY_BASE_URL or ""
                               ).split("//")[-1].split("/")[0]
                v_attempts = [{"stage": a["stage"],
                               "model": a.get("requested_model"),
                               "status": a["status"]}
                              for a in u.get("attempts", [])
                              if str(a.get("stage", "")
                                     ).startswith("verifier")]
                rec = {"scene": scene_id, "arm": arm,
                       "job_id": receipt["job_id"],
                       "usage": {"calls": u.get("calls", 0),
                                 "duration_ms": u.get("duration_ms", 0),
                                 "tokens": u.get("tokens", 0),
                                 "verifier_invalid_retries":
                                     u.get("verifier_invalid_retries", 0)},
                       "live": live,
                       # C5 口径（证据 §3.3）：换通道真跑必须自报 channel_changed
                       "channel_changed": channel_changed,
                       "gateway_host": gw_host,
                       "models": {"writer": client.models.get("writer"),
                                  "verifier": client.models.get("verifier")},
                       "retried": bool(u.get("verifier_invalid_retries")),
                       "verifier_attempts": v_attempts}
                if live:
                    # 审计非阻断项收口（2026-09-25）：live 收据逐条记产物
                    # 世界目录（绝对路径）、写收据时目录是否仍在、目录创建
                    # 时刻（ISO8601 UTC）——事后从收据即可定位 arm*/k4.sqlite
                    # 对账产物。离线收据不加键：默认路径输出逐字不变。
                    rec["worlds_dir"] = worlds_dir_abs
                    rec["worlds_dir_exists"] = Path(worlds_dir_abs).exists()
                    rec["created_at"] = worlds_created_at
                four["receipts"].append(rec)
            except Exception as exc:             # noqa: BLE001
                failed_at[arm] = scene_id       # 断臂标记：本臂后续场 skip
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


def paired_analysis(four: dict, specs=None) -> dict:
    """结构性配对对照（只列事实——质量收益按方案 K4 单独下结论）。
    specs 缺省=SCENES（三场既有口径）；10 场跑传 scenes_for(10)。"""
    specs = specs if specs is not None else SCENES
    by = {(p["scene"], p["arm"]): p for p in four["prose"]}
    rows = []
    for sp in specs:
        scene_id = sp["scene_id"] if isinstance(sp, dict) else sp[0]
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
    ap.add_argument("--scenes", type=int, default=3,
                    help="场数（默认 3=既有三场口径不动；10 场扩展用 "
                         "--scenes 10，派生规则见 scenes_for）")
    ap.add_argument("--live", action="store_true",
                    help="真实调用（拍板后）：K4_ALLOW_LIVE=1 + LLM_MODE=real")
    ap.add_argument("--preflight", action="store_true",
                    help="只读预检（零生成调用、零库写）：查 book_id 是否在 "
                         "work_sources 登记、并调 K3 只读 query_knowledge 判断"
                         "该世界 A 臂包是否非空；未登记或空包 → 非零退出")
    ap.add_argument("--book-id", default="WK-K4",
                    help="世界 id（默认 WK-K4，行为逐字不变）；须为有登记、"
                         "可匹配的真实试点世界（如 production_nonbenchmark_* 源）")
    ap.add_argument("--writer-model", default="")
    ap.add_argument("--verifier-model", default="")
    ap.add_argument("--channel-changed", action="store_true",
                    dest="channel_changed",
                    help="换通道真跑必报（C5 口径，证据 §3.3）：收据与产物"
                         "标 channel_changed=true；同通道基线跑不带")
    a = ap.parse_args()
    if a.scenes < 1:
        raise SystemExit("--scenes 须为 ≥1 的整数")
    if a.out and Path(a.out).exists():
        raise SystemExit(f"--out 已存在：{a.out}——不静默覆盖上次实验产物，"
                         "换新目录（方案「新实验目录」纪律）")
    # === 只读预检（独立模式）：零生成调用、零库写，打印可核对 JSON ===
    if a.preflight:
        with db.session() as s:
            pre = preflight_world(a.book_id, s)
        print(json.dumps(pre, ensure_ascii=False, indent=1))
        if not pre["registered"] or pre["k3_status"] != "matched":
            raise SystemExit(
                f"[preflight] 拒绝（非零退出）：{pre['empty_reason']} "
                f"（book_id={a.book_id}, k3_status={pre['k3_status']}）")
        print(f"[preflight] 通过：book_id={a.book_id} 已登记，A 臂包非空"
              f"（n_techniques={pre['n_techniques']}）")
        return
    if a.live:
        if os.environ.get("K4_ALLOW_LIVE") != "1":
            raise SystemExit("--live 需要环境变量 K4_ALLOW_LIVE=1（双闸："
                            "402 资金墙未拍板前防误跑烧钱）")
        if not (a.writer_model and a.verifier_model):
            raise SystemExit("--live 需要 --writer-model 与 --verifier-model")
    import contextlib
    from app.live_guard import live_lock
    # R6 守卫：live 实跑与全量 pytest / 其他 live 互斥（锁文件原子创建）。
    # 顺序钉（2026-09-23 回归）：互斥检查必须前置于任何 GatewayClient 构造/
    # 模型解析——旧顺序在未配网关的环境里构造即抛 RuntimeFault
    # (gateway_not_configured)，抢掉守卫的 SystemExit，互斥成环境依赖巧合。
    with (live_lock("k4_paired_scenes") if a.live
          else contextlib.nullcontext()):
        if a.live:
            from app import config as _cfg
            # 预检闸仅在「确实会发起真实调用」时生效（LLM_MODE=real）：
            # mock 环境下 GatewayClient 本就拒构（RuntimeFault），不会烧钱，
            # 故跳过预检以免破坏离线/双闸测试；真实试点侧必须过闸才能起跑——
            # 世界未登记 或 A 臂空（无 verified 策略）即拒绝，零真实调用。
            if getattr(_cfg, "LLM_MODE", "mock") == "real":
                with db.session() as s:
                    pre = preflight_world(a.book_id, s)
                if not pre["registered"] or pre["k3_status"] != "matched":
                    raise SystemExit(
                        f"[preflight] 拒绝 --live 起跑（非零退出，零真实调用）："
                        f"{pre['empty_reason']}（book_id={a.book_id}, "
                        f"k3_status={pre['k3_status']}）")
            from app.scene_runtime.client import GatewayClient
            client = GatewayClient(a.writer_model, a.verifier_model)
        else:
            client = FxClient()
        tmp = Path(tempfile.mkdtemp(prefix="k4_worlds_"))
        # 世界目录创建时刻（ISO8601 UTC，Z 后缀）——随 live 收据逐条落账
        worlds_created_at = datetime.datetime.now(
            datetime.timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")
        receipted = False        # 产物（含 worlds_dir 收据）是否已打印/落盘
        try:
            def factory():
                factory.n = getattr(factory, "n", 0) + 1
                store = Store(tmp / f"arm{factory.n}" / "k4.sqlite")
                store.create_world(build_world())
                return store
            with db.session() as s:
                four = run_paired(factory, client, s, live=a.live,
                                  freeze=a.live, n_scenes=a.scenes,
                                  channel_changed=a.channel_changed,
                                  worlds_dir=str(tmp),
                                  worlds_created_at=worlds_created_at)
            analysis = paired_analysis(four, scenes_for(a.scenes))
            if a.live:
                # worlds_dir_cleaned 语义（审计非阻断项收口）：走到这里即
                # 正常收口——live 留库作收据（tokens 对账要读这里的
                # arm*/k4.sqlite），不清理，记 False。异常/中断且收据未落盘
                # 的分支在 finally 清理（记 stderr，收据本就无从谈起）。
                for r in four["receipts"]:
                    r["worlds_dir_cleaned"] = False
            # worlds_dir 记入产物（审计非阻断项收口）：live 留库作收据——
            # tokens 对账（收据之和 vs calls 表之和）要读这里的 arm*/k4.sqlite
            out = {"artifacts": four, "analysis": analysis, "live": a.live,
                   "channel_changed": a.channel_changed,
                   "worlds_dir": str(tmp)}
            print(json.dumps(out, ensure_ascii=False, indent=1))
            receipted = True       # 收据已对外可见（stdout 即记录）——此后
                                   # 世界目录被收据引用，任何异常都不再清理
            if a.out:
                d = Path(a.out)
                d.mkdir(parents=True, exist_ok=True)
                (d / "k4_paired.json").write_text(
                    json.dumps(out, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                print(f"[k4_paired_scenes] 产物已写 {d / 'k4_paired.json'}")
        finally:
            if not a.live:                  # 离线 fixture 世界用后即清；
                shutil.rmtree(tmp, ignore_errors=True)   # --live 留库作收据
            elif not receipted:
                # 泄漏路径收口（2026-09-25）：live 异常/中断且收据从未落盘
                # ——本次自建的临时世界目录不会被任何收据引用，留着即泄漏。
                # 保守口径：只删本次 mkdtemp 自建的 tmp（O_EXCL 唯一目录），
                # 绝不删他处目录；收据已落盘（receipted=True）时留库不删。
                shutil.rmtree(tmp, ignore_errors=True)
                print(f"[k4_paired_scenes] live 中断且收据未落盘：已清理本次"
                      f"自建临时世界目录 {tmp}（worlds_dir_cleaned=true，"
                      f"仅限本次 mkdtemp 自建目录）", file=sys.stderr)
        if four["failures"]:
            raise SystemExit(
                f"有失败臂：{len(four['failures'])} 条（另有 "
                f"{len(four['skipped'])} 条 skipped_after_failure 未执行）"
                "（exit 1）")
    print(f"[k4_paired_scenes] PASS：3 场×2 臂全 committed，"
          f"{analysis['n_packages']} 个 A 臂包（freeze={'True' if a.live else 'False'}）")


if __name__ == "__main__":
    main()
