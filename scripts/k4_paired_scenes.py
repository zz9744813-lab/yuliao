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
JSON，未登记/空包即非零退出；内置场景仍是合成夹具，故真实作品场景卡与预算合同接入前
即使 K2/K3 通过也不放行。当前 strategy_reviews 缺证据指纹及审查→晋升
审计绑定；现在逐张核当前 K2 快照、双席调用收据及晋升/事后放行链接，任一缺失
仍按 semantic_review_unverifiable 拒绝真跑。--live 路径叠加同一道闸（仅 LLM_MODE=real 时生效，
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

审计对账链消费侧（2026-09-26，docs/K4收据世界目录_20260926.md）：逐条收据的
worlds_dir + job_id 才是「这一臂产物出自哪个世界目录」的权威口径——k4_accept_report
按它反查该臂的 arm*/k4.sqlite（只读），旧收据缺键时退回历史顶层键口径并在报告
标注来源；已清理的目录显式标 cleaned=true，不伪装成「仍在」。

真实场景包通路（2026-09-30，docs/K4_REAL_SCENE_PLAN.md）：`real_scene_plan_unverified`
不再是写死常量。`--scene-bundle` 现在认两种包（按顶层 `schema_version` 判别，行为各自独立）：

① **legacy 离线候选包**（无 `schema_version`，即既有 `check_offline_scene_bundle` 口径：
   `status=offline_candidate_not_canon_approved` + `unresolved_canon` 非空）——行为与
   改动前**逐字不变**：仅 `--preflight` 结构检查、仍不放行闸、仍不许进 `--live`。
② **真实场景包** `k4-real-scene-pack/1`（`--emit-scene-pack` 生成，见下）——逐场用
   `app.scene_runtime.contracts` 的 World/ScenePlan/Budget/validate_plan 校验
   （结构/事件/可观察事实 before-after/修订顺序/POV 与角色可见性），绑定已登记
   book_id 与 A 臂知识策略 `build_a_arm_policy(book_id)`（逐字比对），把**逐臂逐场**
   预算合同（调用/修稿/输出/输入/超时上限）冻结进包，并自带 self_sha256；
   `--expected-pack-sha256` 与 self_sha256 不符即拒（防换包冒充）。

闸判据（`preflight_world` 单一来源，--preflight 与 --live 复用同一 ready）：
包通过校验 ∧ 世界已登记 ∧ A 臂包非空 ∧（既有）语义审查收据可核验 ∧ 预算合同齐
⇒ ready=True，world_reason 给逐项可核对读数；任一缺失仍拒并给具体原因码。
**不传 `--scene-bundle` 时 ready 恒 False、world_reason 仍以 `real_scene_plan_unverified`
开头**（合成夹具路径的拒绝行为逐字不变）。--live 过闸后按包内世界/场景卡执行
（每臂独立世界从包的 rev0 世界起、逐场预算取自合同），live 收据逐条记 scene_pack_sha256。

    python scripts/k4_paired_scenes.py --emit-scene-pack <pack.json> --book-id <id>
        # 生成三场真实场景包（来源见 REAL_SCENE_CARD_SOURCE），打印其 self_sha256
    python scripts/k4_paired_scenes.py --preflight --book-id <id> \
        --scene-bundle <pack.json> --expected-pack-sha256 <self_sha256>
"""
from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import db                                   # noqa: E402
from app.scene_runtime.contracts import (Budget, Change, Fact,  # noqa: E402
                                          Identifier, KnowledgePackage,
                                          PlannedEvent, RuntimeFault,
                                          ScenePlan, World, canonical, digest,
                                          validate_plan)
from app.scene_runtime.knowledge_v2 import frozen_package_for_scene  # noqa: E402
from app.scene_runtime.offline_scene_bundle import (  # noqa: E402
    SAFE_PLAN_FAULTS, SceneBundleError, check_offline_scene_bundle)
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


# ── 真实作品场景包（k4-real-scene-pack/1）─────────────────────────────────
# 为什么另立一种包而不是改 legacy 候选包：legacy 包自带
# status=offline_candidate_not_canon_approved 与非空 unresolved_canon——它**自己声明**
# 「不是正典、未决 canon 未清」，因此永远只能当结构参考。真实场景包必须能声明
# 「这三场范围内 canon 已闭」并携带逐臂逐场预算合同，否则闸无从判断。

REAL_SCENE_PACK_SCHEMA = "k4-real-scene-pack/1"
SCENE_PACK_ARMS = ("A", "B")
MAX_SCENE_PACK_BYTES = 1024 * 1024

# 三场场景卡转录来源（仓库内既有素材，只读一次，转录后固化在此）：
#   F:/Hermes/team/K4_ZHUTIAN_OFFLINE_SCENES_20260928.json（5812 字节，
#   sha256=8263a6fb…9ec85e，自称 production_pack_sha256=22317fda…c5376）。
# 该草案自己声明 offline_candidate_not_canon_approved，故本包只声明**三场范围内**
# canon 已闭（见 canon_status），三条未决项逐条搬进 deferred_canon 明示不提前结算。
REAL_SCENE_CARD_SOURCE = {
    "title": "诸天红颜录",
    "origin_ref": ("F:/Hermes/team/"
                   "K4_ZHUTIAN_OFFLINE_SCENES_20260928.json"),
    "origin_sha256": ("8263a6fb14a44d1a9e3918bb2be2ada8c5ba2a9dd92c8ec8d5"
                      "71edb60e9ec85e"),
    "origin_declared_production_pack_sha256": (
        "22317fda09be34991bbc10315908e44d7aa7a5c178dfc6b2ebd4b2948ffc5376"),
    "scene_card_basis": "docs/K4_世界目录收据_20260924.md",
    "canon_scope_note": (
        "第一章开头三场的范围内 canon 已闭：只推进「确认幸存者→划定可核实救援"
        "范围」「拒绝永久归附→提出十二时辰临时条款」「自列首位担责者→停在待签」。"
        "签署资格、独立见证人与公开复议时间属本章后文，见 deferred_canon；"
        "本包不代签、不让临时契生效、不宣称救援完成。"),
    "deferred_canon": [
        "现有章合同未指定救援区内谁有资格代表受影响居民签署；本候选不代签、"
        "不让临时契生效",
        "谁担任独立见证人、公开复议何时举行，须在正式场景卡定稿前确认",
        "顾砚舟的告别记忆损失和泊界城坐标暴露属于后续真正接锚时的代价，"
        "本三场不提前结算",
    ],
    "origin_book_id": "zhutian-hongyanlu",
    "chapter_contract": 1,
    "scope": ("第一章开头三场；只推进确认幸存者、拒绝永久归附、"
              "提出临时救援条款，不宣称本章救援已完成"),
    "world": {
        "book_id": "zhutian-hongyanlu",
        "branch_id": "main",
        "revision": 0,
        "characters": {"CHAR-GU": "顾砚舟", "CHAR-NING": "宁照霜",
                       "CHAR-HAN": "韩灯"},
        "facts": {
            "survivor_signal": {"value": "unconfirmed", "visible_to":
                                 ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                                 "reader_visible": True, "mutable": True},
            "rescue_scope": {"value": "unset", "visible_to":
                             ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                             "reader_visible": True, "mutable": True},
            "permanent_accession": {"value": "not_decided", "visible_to":
                                    ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                                    "reader_visible": True, "mutable": True},
            "temporary_terms": {"value": "absent", "visible_to":
                                ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                                "reader_visible": True, "mutable": True},
            "first_liability": {"value": "unassigned", "visible_to":
                                ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                                "reader_visible": True, "mutable": True},
            "affected_community_signature": {"value": "missing", "visible_to":
                                             ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                                             "reader_visible": True,
                                             "mutable": False},
            "anchor_link": {"value": "inactive", "visible_to":
                            ["CHAR-GU", "CHAR-NING", "CHAR-HAN"],
                            "reader_visible": True, "mutable": False},
        },
        "rules": [
            "界契不得由在场者代替缺席的受影响居民作永久同意。",
            "灾难中的十二时辰临时契到期必须公开复议；签署资格未核实时不得使"
            "界锚生效。",
            "空白录一次只可改写一条可定位因果连接，实际改写要支付不可回收的"
            "记忆代价。",
            "界锚不是领土所有权凭证，救援范围与政治归属必须分开。",
        ],
    },
    "plans": [
        {"book_id": "zhutian-hongyanlu", "branch_id": "main",
         "scene_id": "zhl-c1-s1", "idempotency_key": "zhl-c1-s1-k4-v1",
         "expected_revision": 0, "pov": "CHAR-GU",
         "goal": "在界壁继续破裂前确认仍有生命反应的城区，只划定可核实的救援"
                 "范围，不替全城许诺归附。",
         "style": "冷峻、短促、以可观察行动和有限感官推进；避免解释世界规则的"
                  "长段旁白。",
         "events": [
             {"event_id": "signal-confirmed",
              "description": "顾砚舟用现有观测手段确认一片城区仍有幸存者，"
                             "但无法推断整座霜阙的意愿。",
              "changes": [
                  {"fact": "survivor_signal", "before": "unconfirmed",
                   "after": "confirmed"},
                  {"fact": "rescue_scope", "before": "unset",
                   "after": "confirmed_zone_only"}]}],
         "min_chars": 450, "max_chars": 1200},
        {"book_id": "zhutian-hongyanlu", "branch_id": "main",
         "scene_id": "zhl-c1-s2", "idempotency_key": "zhl-c1-s2-k4-v1",
         "expected_revision": 1, "pov": "CHAR-NING",
         "goal": "拒绝把眼前救援解释成霜阙永久归附，同时留下可谈判的临时救援"
                 "入口。",
         "style": "让宁照霜通过军令、质询和让步表达立场；不以主角内心解释替她作"
                  "决定。",
         "events": [
             {"event_id": "annexation-refused",
              "description": "宁照霜明确拒绝永久归附，顾砚舟改提出十二时辰临时"
                             "条款。",
              "changes": [
                  {"fact": "permanent_accession", "before": "not_decided",
                   "after": "refused"},
                  {"fact": "temporary_terms", "before": "absent",
                   "after": "proposed_12h"}]}],
         "min_chars": 450, "max_chars": 1200},
        {"book_id": "zhutian-hongyanlu", "branch_id": "main",
         "scene_id": "zhl-c1-s3", "idempotency_key": "zhl-c1-s3-k4-v1",
         "expected_revision": 2, "pov": "CHAR-GU",
         "goal": "把自己列为临时救援的首位担责者，公开指出受影响居民签署资格"
                 "仍缺，停在待签状态。",
         "style": "代价以人物承担的具体责任呈现；不把提案写成已经生效的界契或"
                  "已完成的救援。",
         "events": [
             {"event_id": "liability-offered",
              "description": "顾砚舟在待签条款中记录自身首位责任，宁照霜要求"
                             "找出有资格代表受影响居民的人。",
              "changes": [
                  {"fact": "first_liability", "before": "unassigned",
                   "after": "gu_offered"},
                  {"fact": "temporary_terms", "before": "proposed_12h",
                   "after": "awaiting_affected_signature"}]}],
         "min_chars": 450, "max_chars": 1200},
    ],
    # 逐臂逐场预算合同（两臂同上限：A 臂带知识包、B 臂空包对照，调用预算对齐
    # 才可比；数值取自草案 runtime_budget_draft=6/3000/600，其余用 Budget 默认
    # 值：max_rewrites=2 / max_input_chars=24000 / style_feedback=false）。
    "budget_contract_limits": {
        "max_calls": 6, "max_rewrites": 2, "max_output_tokens": 3000,
        "max_input_chars": 24000, "max_elapsed_seconds": 600,
        "style_feedback": False},
}


class ScenePackError(ValueError):
    """真实场景包的稳定拒绝码；绝不携带剧情正文。"""


class _PackStrict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _PackSource(_PackStrict):
    title: str = Field(min_length=1)
    origin_ref: str = Field(min_length=1)
    origin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin_declared_production_pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pack_file_checked: bool
    origin_book_id: Identifier
    chapter_contract: int = Field(ge=1)
    scope: str = Field(min_length=1)
    scene_card_basis: str = Field(min_length=1)


class _PackArmLimits(_PackStrict):
    """逐臂逐场上限；**不设界**——域校验唯一来源是 contracts.Budget 本身
    （max_calls 2..20 / max_rewrites 0..2 / max_output_tokens 100..8000 /
    max_input_chars 100..100000 / max_elapsed_seconds 1..3600），
    越界由 Budget 构造抛 ValidationError → pack_budget_out_of_contract。"""
    max_calls: int
    max_rewrites: int
    max_output_tokens: int
    max_input_chars: int
    max_elapsed_seconds: int
    style_feedback: bool


class _PackBudgetEntry(_PackStrict):
    arm: Literal["A", "B"]
    scene_id: Identifier
    limits: _PackArmLimits


class _PackBudgetContract(_PackStrict):
    arms: list[Literal["A", "B"]] = Field(min_length=1)
    entries: list[_PackBudgetEntry] = Field(min_length=1, max_length=200)


class RealScenePack(_PackStrict):
    schema_version: Literal["k4-real-scene-pack/1"]
    book_id: Identifier
    branch_id: Identifier = "main"
    canon_status: Literal["scoped_pack_canon_closed"]
    canon_scope_note: str = Field(min_length=1)
    deferred_canon: list[str] = Field(default_factory=list)
    source: _PackSource
    world: World
    plans: list[ScenePlan] = Field(min_length=1, max_length=10)
    a_arm_policy: dict[str, object]
    budget_contract: _PackBudgetContract
    self_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


# 本文件带 `from __future__ import annotations`，注解是字符串；pydantic 解析
# Literal/Annotated 需要命名空间，而回归钉常用 importlib 直接 exec 本文件
# （不先登记 sys.modules）——此时按模块命名空间解析会失败。故在此就地按本模块
# 全局解析：Literal / Identifier / World / ScenePlan 都已在上方导入。
RealScenePack.model_rebuild(_types_namespace=globals())



def pack_self_sha256(payload: dict) -> str:
    """包自摘要口径：`self_sha256` 键剔除后，按 contracts.canonical（键序无关、
    无空白、ensure_ascii=False）取 SHA-256。与文件字节摘要（artifact_sha256）
    分开记账，两者都在报告里给，复核人可任选其一核对。"""
    return digest({k: v for k, v in payload.items() if k != "self_sha256"})


def build_real_scene_pack(book_id: str, *, budget_limits: dict | None = None,
                          source_scene: dict | None = None) -> dict:
    """构造三场真实场景包（纯函数，离线、零库、零模型调用）。

    book_id 是登记台账里的世界 id（work_sources.work_id）；草案自带的
    origin_book_id 与之不同时**显式改写**并由 book_id_remapped 标记（复核人
    可在报告里看到改写事实，不靠猜）。a_arm_policy 逐字取
    build_a_arm_policy(book_id)——包与 K3 只读查询入口因此绑定死：查询策略一改，
    包立即失效（pack_a_arm_policy_mismatch），不会出现「包按旧策略、闸按新策略」。
    """
    # **深拷贝**源卡：调用方（含回归负例）改包内任一 events/changes/facts 都不许
    # 反写 REAL_SCENE_CARD_SOURCE——否则第一个负例就把全局源永久污染，后续
    # 每一次 build 都带着上一例的篡改（实测：anchor_link 一行让全文件后半红）。
    src = copy.deepcopy(source_scene if source_scene is not None
                        else REAL_SCENE_CARD_SOURCE)
    limits = dict(budget_limits if budget_limits is not None
                  else src["budget_contract_limits"])
    plans = []
    for raw in src["plans"]:
        plan = dict(raw)
        plan["book_id"] = book_id
        plans.append(plan)
    world = dict(src["world"])
    world["book_id"] = book_id
    scene_ids = [p["scene_id"] for p in plans]
    entries = [{"arm": arm, "scene_id": sid, "limits": dict(limits)}
               for arm in SCENE_PACK_ARMS for sid in scene_ids]
    payload = {
        "schema_version": REAL_SCENE_PACK_SCHEMA,
        "book_id": book_id,
        "branch_id": src["world"]["branch_id"],
        "canon_status": "scoped_pack_canon_closed",
        "canon_scope_note": src["canon_scope_note"],
        "deferred_canon": list(src["deferred_canon"]),
        "source": {
            "title": src["title"],
            "origin_ref": src["origin_ref"],
            "origin_sha256": src["origin_sha256"],
            "origin_declared_production_pack_sha256":
                src["origin_declared_production_pack_sha256"],
            "source_pack_file_checked": False,
            "origin_book_id": src["origin_book_id"],
            "chapter_contract": src["chapter_contract"],
            "scope": src["scope"],
            "scene_card_basis": src["scene_card_basis"],
        },
        "world": world,
        "plans": plans,
        "a_arm_policy": build_a_arm_policy(book_id),
        "budget_contract": {"arms": list(SCENE_PACK_ARMS), "entries": entries},
    }
    payload["self_sha256"] = pack_self_sha256(payload)
    return payload


def _unique_pack_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_pack_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _read_pack(path) -> dict:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_SCENE_PACK_BYTES + 1)
    except (OSError, TypeError, ValueError) as exc:
        raise ScenePackError("pack_unreadable") from exc
    if not raw or len(raw) > MAX_SCENE_PACK_BYTES:
        raise ScenePackError("pack_size_invalid")
    try:
        return json.loads(raw.decode("utf-8"),
                          object_pairs_hook=_unique_pack_pairs,
                          parse_constant=_reject_pack_constant)
    except (UnicodeError, ValueError) as exc:
        raise ScenePackError("pack_json_invalid") from exc


def scene_bundle_kind(path) -> str:
    """判别 `--scene-bundle` 给的是哪一类包：legacy 离线候选 / real 真实场景包。

    判据只看顶层 `schema_version` 键**是否存在**：legacy 包的 _Bundle 是
    extra="forbid"，出现该键只会是 schema 错——所以「有该键但取值不对」判为
    real，让真实通路的 pack_schema_invalid 报错，而不是掉进 legacy 的
    bundle_schema_invalid（错码误导复核人）。"""
    payload = _read_pack(path)
    if not isinstance(payload, dict):
        raise ScenePackError("pack_json_invalid")
    return ("real" if "schema_version" in payload else "legacy")


def _pack_budget_contract(pack: RealScenePack,
                          scene_ids: list[str]) -> dict:
    """逐臂逐场预算合同 → {(arm, scene_id): Budget}；缺一臂/缺一场即拒。"""
    declared = list(pack.budget_contract.arms)
    if (sorted(set(declared)) != sorted(SCENE_PACK_ARMS) or
            len(declared) != len(SCENE_PACK_ARMS)):
        raise ScenePackError("pack_budget_contract_incomplete:arms=" +
                             ",".join(declared))
    seen: dict = {}
    for entry in pack.budget_contract.entries:
        key = (entry.arm, entry.scene_id)
        if key in seen:
            raise ScenePackError("pack_budget_contract_duplicate:" +
                                 f"{key[0]}:{key[1]}")
        seen[key] = entry
    missing = [f"{arm}:{sid}" for arm in SCENE_PACK_ARMS for sid in scene_ids
               if (arm, sid) not in seen]
    if missing:
        raise ScenePackError("pack_budget_contract_incomplete:missing=" +
                             ",".join(sorted(missing)))
    extra = sorted(f"{arm}:{sid}" for (arm, sid) in seen
                   if sid not in scene_ids)
    if extra:
        raise ScenePackError("pack_budget_contract_extra:" +
                             ",".join(extra))
    out = {}
    for (arm, sid), entry in sorted(seen.items()):
        try:
            out[(arm, sid)] = Budget(**entry.limits.model_dump())
        except ValidationError:
            raise ScenePackError(
                f"pack_budget_out_of_contract:{arm}:{sid}") from None
    return out


def _replay_scene_pack(pack: RealScenePack) -> World:
    """逐场 validate_plan + 推进世界：结构/事件/可观察事实 before-after/修订顺序/
    POV 与角色可见性逐条机械校验。knowledge 只作 scope 载体（A 臂技巧内容离线
    不可知，K3 查询在 freeze 时才发生）——包绑的是**策略**不是技巧。"""
    world = World.model_validate(pack.world.model_dump(mode="json"))
    scope_only = KnowledgePackage(package_id="pack-scope-only",
                                 book_id=pack.book_id,
                                 source_kind="knowledge_query_v2",
                                 techniques=[])
    for plan in pack.plans:
        try:
            validate_plan(plan, world, scope_only)
        except RuntimeFault as exc:
            code = str(exc)
            raise ScenePackError(
                "pack_plan_invalid:" + plan.scene_id + ":" +
                (code if code in SAFE_PLAN_FAULTS else "unknown")) from None
        try:
            state = world.model_dump(mode="json")
            for event in plan.events:
                for change in event.changes:
                    state["facts"][change.fact]["value"] = change.after
            state["revision"] += 1
            world = World.model_validate(state)
        except (KeyError, TypeError, ValueError):
            raise ScenePackError("pack_replay_invalid:" +
                                 plan.scene_id) from None
    return world


def load_real_scene_pack(path) -> RealScenePack:
    """读 + 结构校验 + 自摘要复核（离线、只读文件）；通过即返回模型对象。

    执行侧（世界/场景卡/预算合同）消费这个对象，闸侧消费
    check_real_scene_pack 的 JSON 报告——两者同源同文件同一次口径。"""
    payload = _read_pack(path)
    if not isinstance(payload, dict):
        raise ScenePackError("pack_json_invalid")
    try:
        pack = RealScenePack.model_validate(payload)
    except ValidationError:
        raise ScenePackError("pack_schema_invalid") from None
    if pack.self_sha256 != pack_self_sha256(payload):
        raise ScenePackError("pack_self_sha256_mismatch")
    return pack


def check_real_scene_pack(path, *, expected_pack_sha256: str,
                          book_id: str, scene_count: int) -> dict:
    """真实场景包零库零模型校验（离线、只读文件）。

    返回报告：包自身结构/绑定/预算合同的读数。`live_ready` 恒 False——
    **包本身从不授权真跑**，放行只由 preflight_world 的闸决定（登记世界 +
    A 臂非空 + 审查收据 + 预算合同齐）。"""
    if (not isinstance(expected_pack_sha256, str) or
            len(expected_pack_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in expected_pack_sha256)):
        raise ScenePackError("pack_expected_sha256_invalid")
    pack = load_real_scene_pack(path)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    computed = pack_self_sha256(payload)
    if pack.self_sha256 != expected_pack_sha256:
        raise ScenePackError("pack_expected_sha256_mismatch")
    if pack.book_id != book_id:
        raise ScenePackError("pack_book_id_mismatch")
    scene_ids = [plan.scene_id for plan in pack.plans]
    idem = [plan.idempotency_key for plan in pack.plans]
    if len(scene_ids) != len(set(scene_ids)) or len(idem) != len(set(idem)):
        raise ScenePackError("pack_scene_identity_duplicate")
    if len(scene_ids) != scene_count:
        raise ScenePackError(f"pack_scene_count_mismatch:{len(scene_ids)}"
                             f"!={scene_count}")
    if any(plan.book_id != pack.book_id or
           plan.branch_id != pack.branch_id for plan in pack.plans):
        raise ScenePackError("pack_scene_scope_mismatch")
    policy = build_a_arm_policy(book_id)
    if canonical(pack.a_arm_policy) != canonical(policy):
        raise ScenePackError("pack_a_arm_policy_mismatch")
    budgets = _pack_budget_contract(pack, scene_ids)
    final_world = _replay_scene_pack(pack)
    first_limits = budgets[(SCENE_PACK_ARMS[0], scene_ids[0])].model_dump()
    return {
        "kind": "real_scene_pack",
        "schema_version": pack.schema_version,
        "pack_valid": True,
        "live_ready": False,
        "live_ready_reason": ("pack_alone_never_authorizes_live:registered_"
                              "world+nonempty_a_arm+review_chain_decide"),
        "model_calls": 0,
        "db_registration_checked": False,
        "book_id": pack.book_id,
        "branch_id": pack.branch_id,
        "origin_book_id": pack.source.origin_book_id,
        "book_id_remapped": pack.source.origin_book_id != pack.book_id,
        "scene_ids": scene_ids,
        "scene_count": len(scene_ids),
        "pov_by_scene": {plan.scene_id: plan.pov for plan in pack.plans},
        "facts_by_scene": {plan.scene_id: [change.fact for event in plan.events
                                           for change in event.changes]
                           for plan in pack.plans},
        "characters": sorted(pack.world.characters),
        "initial_world_sha256": digest(pack.world),
        "final_world_sha256": digest(final_world),
        "plans_sha256": digest([plan.model_dump(mode="json")
                                for plan in pack.plans]),
        "artifact_sha256": hashlib.sha256(
            Path(path).read_bytes()).hexdigest(),
        "self_sha256": computed,
        "declared_self_sha256": pack.self_sha256,
        "expected_pack_sha256_matched": True,
        "a_arm_policy": {"policy_sha256": digest(policy),
                         "declared_policy_sha256": digest(pack.a_arm_policy),
                         "bound": True,
                         "policy_source": ("build_a_arm_policy(book_id)"
                                           "（K3 只读查询唯一入口；其"
                                           " semantic_requirements 取自内置"
                                           " s1 夹具计划，见 "
                                           "docs/K4_REAL_SCENE_PLAN.md"
                                           " 已知限制）")},
        "budget_contract": {
            "arms": list(pack.budget_contract.arms),
            "scene_ids": scene_ids,
            "entries": len(budgets),
            "complete": True,
            "effective_limits": first_limits},
        "canon": {"status": pack.canon_status,
                  "scope_note": pack.canon_scope_note,
                  "deferred_canon": list(pack.deferred_canon)},
        "source_pack_file_checked": pack.source.source_pack_file_checked,
        "source": {"title": pack.source.title,
                   "origin_ref": pack.source.origin_ref,
                   "origin_sha256": pack.source.origin_sha256,
                   "chapter_contract": pack.source.chapter_contract,
                   "scope": pack.source.scope,
                   "scene_card_basis": pack.source.scene_card_basis},
    }


def preflight_world(book_id: str, s, *, scene_pack: dict | None = None) -> dict:
    """只读预检（零生成调用、零库写）：
    - 查 work_sources 是否登记该 book_id；
    - 调 K3 只读 query_knowledge 判断该世界 A 臂包是否非空；
    - （可选）消费 --scene-bundle 真实场景包报告（check_real_scene_pack 的产物）。
    返回可核对 dict：book_id / registered / k3_status / selected_ids /
    n_techniques / empty_reason / review_status / review_reason /
    knowledge_ready / pack_ok / pack_reason / world_reason / ready。
    不抛异常、不退出——退出决策交给调用方。

    这是闸，不是提示：未登记、A 臂空或当次 selected 策略缺可核验的
    当前证据语义审查收据，调用方（--preflight / --live）均非零退出。
    旧 strategy_reviews 没有版本、证据指纹及审查→晋升审计绑定，不能放行；
    新收据链逐张重算当前证据与实际模型身份，最终冻结仍在 Writer 入场重核。

    真实场景包判据（2026-09-30，docs/K4_REAL_SCENE_PLAN.md）——**四读数缺一即拒**，
    且 world_reason 逐项给码，绝不静默放行：
      ① pack_ok：包过校验（结构/绑定/预算合同/self_sha256 与 CLI 钉的哈希一致）；
      ② registered：世界在 work_sources 有登记行；
      ③ A 臂包非空：k3_status=matched 且 selected 非空；
      ④ 语义审查收据可核验（review_status=verified）——沿用既有 knowledge_ready，
         本件**不放宽**：宁可继续拒，不为凑 ready=true 降低既有门。
    scene_pack=None（未传 --scene-bundle）⇒ 恒拒，world_reason 以
    real_scene_plan_unverified 开头——合成夹具路径的拒绝行为逐字不变。
    """
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
    review_status = "not_applicable"
    review_reason = None
    manifest = []
    if selected_ids:
        from app.semantic_admission import approved_selected
        from app.semantic_approval import ApprovalError
        from app.promotion_audits import PromotionAuditSchemaError
        from app.semantic_receipts import ReceiptSchemaError
        try:
            manifest = approved_selected(s, selected)
            review_status = "verified"
        except (ApprovalError, PromotionAuditSchemaError,
                ReceiptSchemaError) as exc:
            review_status = "semantic_review_unverifiable"
            review_reason = "semantic_review_unverifiable:" + str(exc)
    knowledge_ready = (registered and k3_status == "matched" and
                       bool(selected_ids) and review_status == "verified")
    # 场景包闸（不是提示）：四读数缺一即拒。pack_ok 只认校验器给的 pack_valid，
    # 不看任何调用方自报——防「报告里写个 ready 就放行」。
    pack_ok = bool(scene_pack and scene_pack.get("pack_valid"))
    pack_reason = None
    blocking = []
    if scene_pack is None:
        blocking.append("no_scene_pack:未传 --scene-bundle，内置世界/场景仍是"
                        "合成夹具（林穗/三枚钱），缺真实作品场景卡与逐臂预算"
                        "合同")
    if scene_pack is not None and not pack_ok:
        pack_reason = scene_pack.get("reason_code") or "pack_invalid"
        blocking.append("scene_pack_rejected:" + str(pack_reason))
    if not registered:
        blocking.append("world_not_registered")
    if not selected_ids:
        blocking.append("empty_package")
    if review_status != "verified":
        blocking.append("semantic_review_unverifiable")
    ready = pack_ok and not blocking
    if ready:
        contract = scene_pack["budget_contract"]
        limits = contract["effective_limits"]
        world_reason = (
            "real_scene_plan_pack_verified:"
            f"pack_sha256={scene_pack['self_sha256'][:12]};"
            f"artifact_sha256={scene_pack['artifact_sha256'][:12]};"
            f"scenes={scene_pack['scene_count']}"
            f"[{','.join(scene_pack['scene_ids'])}];"
            f"world_sha256={scene_pack['initial_world_sha256'][:12]}→"
            f"{scene_pack['final_world_sha256'][:12]};"
            f"budget_contract=arms[{','.join(contract['arms'])}]"
            f"×{scene_pack['scene_count']}={contract['entries']}条;"
            f"max_calls={limits['max_calls']},"
            f"max_rewrites={limits['max_rewrites']},"
            f"max_elapsed_seconds={limits['max_elapsed_seconds']};"
            f"a_arm_policy={scene_pack['a_arm_policy']['policy_sha256'][:12]};"
            f"registered={registered};k3_status={k3_status};"
            f"a_arm_techniques={n_techniques};"
            f"review_status={review_status}")
    else:
        world_reason = "real_scene_plan_unverified:" + ";".join(blocking)
    return {"book_id": book_id, "registered": registered,
            "k3_status": k3_status, "selected_ids": selected_ids,
            "n_techniques": n_techniques, "empty_reason": empty_reason,
            "review_status": review_status, "review_reason": review_reason,
            "approval_manifest": manifest,
            "knowledge_ready": knowledge_ready,
            "pack_ok": pack_ok, "pack_reason": pack_reason,
            "pack_sha256": (scene_pack or {}).get("self_sha256"),
            "world_reason": world_reason,
            "ready": ready}


def _preflight_with_pack(book_id: str, s, scene_pack: dict | None) -> dict:
    """--preflight 与 --live 共用的唯一闸入口（同一 preflight_world、同一判据）。

    无场景包时按两位置参调用——保持既有调用形态（含只接受 (book_id, session)
    的替换实现）逐字不变；有包时才带 scene_pack 关键字。"""
    if scene_pack is None:
        return preflight_world(book_id, s)
    return preflight_world(book_id, s, scene_pack=scene_pack)


def fit_fixture_text(text: str, plan) -> str:
    """把夹具正文对齐到计划的长度带 min_chars..max_chars。

    默认合成场景（min_chars=1/max_chars=500）下这段**逐字不改**——正文仍是
    原来那句「林穗把一枚钱放在桌上，又收了回去。」。真实场景包按小说场景
    口径给 450..1200，夹具若不落在带内，validate_review 立刻报
    text_length_outside_plan，Writer 修稿额度耗尽——那测的是夹具长度不是闸。
    补白用确定性重复句，零真实调用、零随机。
    """
    data = plan if isinstance(plan, dict) else plan.model_dump()
    lo = int(data.get("min_chars") or 1)
    hi = int(data.get("max_chars") or lo)
    if len(text) > hi:
        return text[:hi]
    if len(text) < lo:
        filler = "他把能确认的与不能确认的分开记下，然后等下一个可核实的时刻。"
        while len(text) < lo:
            text += filler
        text = text[:lo]
    return text


class FxClient:
    """离线夹具（零真实调用）。⚠ verifier 自证式：quote 恒等于 payload
    文本、issues 恒空——证据门/负例不在此触发（由卡组覆盖）；「e2e
    全绿」只证结构链路，不证门行为。"""
    models = {"writer": "fx-w", "verifier": "fx-v", "transport": "fixture"}

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        if role == "writer":
            context = payload["context"]
            pro = context.get("knowledge", {}).get("techniques", [])
            tag = f"（用了{len(pro)}条策略）" if pro else "（空包对照）"
            body = {"text": fit_fixture_text(
                f"林穗把一枚钱放在桌上，又收了回去。{tag}",
                context.get("plan") or {})}
        else:
            changes = [c for ev in payload["plan"]["events"]
                       for c in ev["changes"]]
            body = {"issues": [],
                    "events": [{"event_id": ev["event_id"],
                                "quote": payload["text"]}
                               for ev in payload["plan"]["events"]],
                    # 逐条覆盖计划里的**全部**变化：只报第一条会让
                    # state_patch_not_authorized_by_plan（多变化场景卡）触发
                    # 核验返修并耗尽——那是夹具缺陷，不是场景卡的。
                    "changes": [{"fact": c["fact"], "after": c["after"],
                                 "quote": payload["text"]}
                                for c in changes]}
        return {"text": json.dumps(body, ensure_ascii=False),
                "tokens_in": 10, "tokens_out": 5, "actual_model": "fx",
                "finish_reason": "stop"}


def gateway_host_from_url(url) -> str:
    """收据 gateway_host 口径（孤儿裁定 #13 收口，2026-09-26）：取
    urllib.parse.urlsplit 的 hostname（含有效端口时保留 host:port）——
    **绝不携带 userinfo**。旧 split("//") 形态下
    LG_GATEWAY_BASE_URL=http://u:p@host:3000/v1 会把 u:p@host:3000 原样
    落进收据（凭据进入产物面）。空/不可解析（无 netloc、非法端口等）
    按既有口径落空串，不抛异常。"""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname
        if not host:
            return ""
        if parts.port is not None:
            return f"{host}:{parts.port}"
        return host
    except ValueError:
        return ""


def model_identity_fields(usage: dict) -> dict:
    """live 收据的模型身份诚实字段（任务 2026-10-01 A3）。

    背景：litellm 会静默换上游（实测 491 次调用 42 次请求≠实际，8.6%），
    而 K4/K5 异模型门与 K2 两席按**请求**模型对判定——不核实的话收据里
    的模型身份可能是假的。口径：
    · models（既有键）语义不变 = 请求模型，绝不为凑门改指向实际值；
    · models_actual = calls 台账里的实际服务模型；同角色逐次出现多个不同
      实际模型（= 逐次换皮）时如实列成有序清单，不取其一冒充全部；
    · model_identity_ok = **每一笔**带请求侧记录的调用都 actual==requested；
      无请求侧记录（非网关通道，如离线夹具）⇒ 无法证明身份 ⇒ False
      （fail-closed，诚实原则优先于让字段变绿）。
    消费侧复核（docs/MODEL_IDENTITY_AND_RETRY.md）：门的异模型前提应以
    models_actual 的两个值互异 + model_identity_ok==True 共同成立。"""
    attempts = (usage or {}).get("attempts", [])

    def actual_for(role_prefix):
        seen = sorted({a.get("actual_model") for a in attempts
                       if str(a.get("stage", "")).startswith(role_prefix + ".")
                       and a.get("actual_model")})
        if not seen:
            return None
        return seen[0] if len(seen) == 1 else seen

    checked = [a for a in attempts if a.get("requested_model") is not None]
    identity_ok = bool(checked) and all(
        a.get("actual_model") == a.get("requested_model") for a in checked)
    return {"models_actual": {"writer": actual_for("writer"),
                              "verifier": actual_for("verifier")},
            "model_identity_ok": identity_ok}


def run_paired(store_factory, client, lg_session, *, live: bool = False,
               freeze: bool = False, n_scenes: int = 3,
               book_id: str = "WK-K4",
               channel_changed: bool = False,
               worlds_dir: str | None = None,
               worlds_created_at: str | None = None,
               budget_calls: int | None = None,
               scene_plans: list | None = None,
               budget_contract: dict | None = None) -> dict:
    """N 场（默认 3=SCENES；扩展场派生见 scenes_for）× 2 臂 + 四类产物
    （prose/packages/receipts/failures）+ skipped（断臂后未执行的后续场
    单列，不进 failures——C2/止损台账不被连锁幻影污染，2026-09-23 会审
    整改）+ 结构性配对分析（不判质量）。

    store_factory() 每臂一次（独立平行世界；臂内各场共享该臂世界，
    revision 逐场递增）。同幂等键异输入必冲突（K3-B 契约）→ 两臂 idem
    键各带后缀。freeze 只在真跑（--live）时 True——离线零库写。
    **回滚口径**：freeze_package 逐臂即时 commit，已提交的冻结写不因
    另一臂 rollback 回退（rollback 只丢本臂未提交部分，每臂收据独立）。

    scene_plans / budget_contract（2026-09-30 真实场景包通路）：两者缺省
    ⇒ **逐字走合成夹具路径**（build_plan + Budget()，与改动前完全一致）。
    给了 scene_plans（真实场景包 plans，顺序即场序）⇒ 每场直接用包内
    ScenePlan（幂等键逐字保留，不加臂后缀——两臂各有独立世界库，不会撞键），
    逐场预算取自 budget_contract[(arm, scene_id)]（包内冻结的逐臂合同，
    仍经 contracts.Budget 校验域）。CLI 的 --budget-calls 与包内
    max_calls 冲突时抛错——合同优先，不静默取其一。

    worlds_dir / worlds_created_at：本次跑的世界目录（main() 里
    tempfile.mkdtemp(prefix="k4_worlds_") 自建）及其创建时刻（ISO8601
    UTC）。仅 live 消费——审计非阻断项收口（2026-09-25）：live 收据逐条
    记 worlds_dir/worlds_dir_exists/created_at/worlds_dir_cleaned，缺参即
    拒跑（不带无收据的真跑）；离线（live=False）不消费、收据字段逐字
    不变。

    消费侧对账（审计非阻断项第二段，2026-09-26，docs/K4收据世界目录_
    20260926.md）：k4_accept_report 不再靠「产物顶层 worlds_dir + 工厂序
    arm1=A/arm2=B」猜臂—世界目录映射，改为**以逐条收据的 worlds_dir 为准**
    并用收据 job_id 在该目录的 arm*/k4.sqlite 里只读反查定位该臂世界库；
    旧收据（缺 worlds_dir 键）退回历史口径并在报告里标明来源，绝不崩、
    绝不假装知道。收据里 worlds_dir_cleaned 恒显式落值（已清理即 true）。"""
    if live:
        if worlds_dir is None or worlds_created_at is None:
            raise ValueError("run_paired: live 跑必须记 worlds_dir 与 "
                             "worlds_created_at——无收据地址不许起真跑"
                             "（审计非阻断项收口 2026-09-25）")
        worlds_dir_abs = str(Path(worlds_dir).resolve())
    if scene_plans is not None and len(scene_plans) != n_scenes:
        raise ValueError(f"run_paired: scene_plans 场数 {len(scene_plans)} "
                         f"与 n_scenes {n_scenes} 不一致——包场数已由"
                         f"check_real_scene_pack 钉死，不许运行期再漂移")
    four = {"prose": [], "packages": [], "receipts": [], "failures": [],
            "skipped": []}
    # 预算可配置（gui-k4-s2-contract 最小修复，2026-09-25）：
    # budget_calls=None ⇒ Budget() 无参构造——**与现状逐字一致**（默认
    # max_calls=6，contracts.py:103）；显式传参 ⇒ Budget(max_calls=N)
    # （N 仍受契约 ge=2/le=20 约束，默认闸不许放宽指默认值不变，
    # 上限也只收不放宽）。生效值逐条写进收据（budget_calls 键）。
    # 真实场景包在场时，逐场逐臂预算**一律**取自包内合同（budget_calls=None
    # 时的 Budget() 默认值不参与），且与 CLI 显式值冲突即拒。
    if budget_contract is not None:
        contract_budgets = dict(budget_contract)
    else:
        contract_budgets = {}
    default_budget = Budget() if budget_calls is None \
        else Budget(max_calls=budget_calls)

    def scene_budget(arm, scene_id):
        if not contract_budgets:
            return default_budget
        budget = contract_budgets[(arm, scene_id)]
        if budget_calls is not None and budget.max_calls != budget_calls:
            raise ValueError(
                f"run_paired: --budget-calls={budget_calls} 与场景包内 "
                f"{arm}:{scene_id} 的 max_calls={budget.max_calls} 冲突——"
                f"逐臂预算合同以包为准，不静默取其一")
        return budget
    stores = {arm: store_factory() for arm in ("A", "B")}
    failed_at = {"A": None, "B": None}     # 本臂首个失败场（None=未断）
    for sp in (scene_plans if scene_plans is not None
               else scenes_for(n_scenes)):
        scene_id = sp.scene_id if isinstance(sp, ScenePlan) else sp["scene_id"]
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
            if isinstance(sp, ScenePlan):
                plan = sp
            else:
                plan = build_plan(scene_id, sp["rev"], sp["before"],
                                  sp["after"], sp["idem"] + f"-{arm}",
                                  fact=sp["fact"], goal=sp["goal"],
                                  desc=sp["desc"], book_id=book_id)
            budget = scene_budget(arm, scene_id)
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
                engine = (lg_session.get_bind()
                          if pkg.source_kind == "knowledge_query_v2" and
                          pkg.techniques else None)
                runner = SceneRunner(store, client, lg_engine=engine)
                receipt = runner.run(plan, pkg, budget)
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
                    gw_host = gateway_host_from_url(_cfg.GATEWAY_BASE_URL)
                v_attempts = [{"stage": a["stage"],
                               "model": a.get("requested_model"),
                               "status": a["status"],
                               # 离线收据逐字不变（键集钉死见 tests/
                               # test_k4_worlds_dir_receipt.py）：实际服务
                               # 模型只在 live 侧逐次留痕。
                               **({"model_actual": a.get("actual_model")}
                                  if live else {})}
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
                       "budget_calls": budget.max_calls,
                       "live": live,
                       # C5 口径（证据 §3.3）：换通道真跑必须自报 channel_changed
                       "channel_changed": channel_changed,
                       "gateway_host": gw_host,
                       "models": {"writer": client.models.get("writer"),
                                  "verifier": client.models.get("verifier")},
                       "retried": bool(u.get("verifier_invalid_retries")
                                       or u.get("contract_retries")),
                       "verifier_attempts": v_attempts}
                if live:
                    # 模型身份诚实化（任务 2026-10-01 A3）：models=请求语义
                    # 不变；models_actual/model_identity_ok 从 calls 台账取
                    # 实际服务模型——只为 live 收据加键（离线键集逐字钉死）。
                    rec.update(model_identity_fields(u))
                    rec["usage"]["contract_retries"] = u.get("contract_retries", 0)
                if live:
                    # 与顶层产物、世界库和计划绑定同一本书，供验收侧逐臂核对。
                    rec["book_id"] = plan.book_id
                    # 审计非阻断项收口（2026-09-25）：live 收据逐条记产物
                    # 世界目录（绝对路径）、写收据时目录是否仍在、目录创建
                    # 时刻（ISO8601 UTC）——事后从收据即可定位 arm*/k4.sqlite
                    # 对账产物。离线收据不加键：默认路径输出逐字不变。
                    rec["worlds_dir"] = worlds_dir_abs
                    rec["worlds_dir_exists"] = Path(worlds_dir_abs).exists()
                    rec["created_at"] = worlds_created_at
                    # worlds_dir_cleaned 在此写时即落 False（写收据这一刻
                    # 目录必在，谎报不得）——补 2026-09-26 消费侧缺口：作为
                    # 库被直接调用（不经 main）时收据也恒带这四键，消费侧
                    # （k4_accept_report）不靠「有没有这键」猜清理状态。main
                    # 收口处仍逐条复核写 False（见下，正常收口留库不删）。
                    rec["worlds_dir_cleaned"] = False
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
                failure = {"scene": scene_id, "arm": arm,
                           "error_type": type(exc).__name__,
                           "error": str(exc)[:300],
                           "rollback_failed": rollback_failed,
                           "rollback_error": rollback_error}
                # 预算耗尽诊断（gui-k4-s2-contract，2026-09-25）：给
                # k4_accept_report 的机械判据补硬证据——实际调用数/生效上限/
                # 返修调用数（.contract1/.state1/.retry 段）逐项落账，从
                # 本臂世界库（临时产物库，只读 SELECT）取数；非预算类失败
                # 记录逐字不变。**不改变判定**：预算耗尽仍按失败记账，
                # 绝不改判通过。
                err_head = str(exc).split(":", 1)[0]
                if err_head in ("call_budget_exhausted",
                                "rewrite_budget_exhausted"):
                    diag = {"max_calls": budget.max_calls,
                            "configured": budget_calls is not None}
                    try:
                        with store.connection() as wdb:
                            jrow = wdb.execute(
                                "SELECT id FROM jobs WHERE idem=?",
                                (plan.idempotency_key,)).fetchone()
                            if jrow is not None:
                                rows = wdb.execute(
                                    "SELECT stage,status FROM calls WHERE "
                                    "job=?", (jrow[0],)).fetchall()
                                diag["actual_calls"] = len(rows)
                                diag["failed_calls"] = sum(
                                    1 for _, st in rows if st != "succeeded")
                                diag["repair_calls"] = sum(
                                    1 for st, _ in rows
                                    if st.endswith((".contract1", ".state1",
                                                   ".retry")))
                    except Exception:              # noqa: BLE001
                        diag["store_unavailable"] = True
                    failure["budget_diag"] = diag
                four["failures"].append(failure)
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
                    help="只读预检（零生成调用、零库写）：查登记、非空包与"
                         "可核验语义审查收据；任一缺失 → 非零退出")
    ap.add_argument("--book-id", default="WK-K4",
                    help="世界 id（默认 WK-K4，行为逐字不变）；须为有登记、"
                         "可匹配的真实试点世界（如 production_nonbenchmark_* 源）")
    ap.add_argument("--scene-bundle", default="",
                    help="场景 JSON。legacy 离线候选包（无 schema_version）："
                         "仅 --preflight 检查结构，不放行 --live（行为不变）；"
                         "真实场景包 k4-real-scene-pack/1（--emit-scene-pack "
                         "生成）：逐场校验并接 --preflight/--live 闸")
    ap.add_argument("--expected-pack-sha256", default="",
                    help="独立校验的作品包 SHA-256；使用 --scene-bundle 时必填。"
                         "legacy 包＝包内声明的 production_pack_sha256；真实场景"
                         "包＝包内 self_sha256（键序无关的规范摘要，口径见 "
                         "check_real_scene_pack）")
    ap.add_argument("--emit-scene-pack", default="",
                    dest="emit_scene_pack",
                    help="生成三场真实场景包到该 JSON 路径并打印其 self_sha256"
                         "（--expected-pack-sha256 要用的值）；纯离线、零库、零"
                         "模型调用。已存在即拒（不覆盖）")
    ap.add_argument("--writer-model", default="")
    ap.add_argument("--verifier-model", default="")
    ap.add_argument("--channel-changed", action="store_true",
                    dest="channel_changed",
                    help="换通道真跑必报（C5 口径，证据 §3.3）：收据与产物"
                         "标 channel_changed=true；同通道基线跑不带")
    ap.add_argument("--budget-calls", type=int, default=None,
                    dest="budget_calls",
                    help="每场每臂的 call 预算上限（默认缺省=6，与现状逐字"
                         "一致；显式传参须在契约域 2..20 内，生效值进收据"
                         "budget_calls 键——gui-k4-s2-contract）。带真实场景包"
                         "时逐场预算以包内逐臂合同为准，与本值冲突即拒")
    a = ap.parse_args()
    if a.scenes < 1:
        raise SystemExit("--scenes 须为 ≥1 的整数")
    if a.budget_calls is not None and not (2 <= a.budget_calls <= 20):
        raise SystemExit("--budget-calls 须在契约域 [2, 20] 内"
                         f"（得到 {a.budget_calls}——与 Budget.max_calls 的 "
                         "ge=2/le=20 同口径；默认缺省=6 不变）")
    if a.out and Path(a.out).exists():
        raise SystemExit(f"--out 已存在：{a.out}——不静默覆盖上次实验产物，"
                         "换新目录（方案「新实验目录」纪律）")
    # 场景包生成（独立模式）：零库、零模型调用，只写一个 JSON 文件。
    if a.emit_scene_pack:
        if a.preflight or a.live:
            raise SystemExit("--emit-scene-pack 是独立生成模式，不与 "
                             "--preflight/--live 同用")
        target = Path(a.emit_scene_pack)
        if target.exists():
            raise SystemExit(f"--emit-scene-pack 目标已存在：{target}"
                             "——不覆盖既有包（换新路径，防拿旧包冒充）")
        payload = build_real_scene_pack(a.book_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        print(f"[k4_paired_scenes] 真实场景包已写 {target}")
        print(json.dumps({"book_id": payload["book_id"],
                          "scene_ids": [p["scene_id"]
                                        for p in payload["plans"]],
                          "scene_count": len(payload["plans"]),
                          "self_sha256": payload["self_sha256"],
                          "origin_book_id": payload["source"][
                              "origin_book_id"],
                          "book_id_remapped": payload["source"][
                              "origin_book_id"] != payload["book_id"],
                          "a_arm_policy_sha256": digest(payload[
                              "a_arm_policy"]),
                          "budget_contract_arms": payload[
                              "budget_contract"]["arms"],
                          "deferred_canon": payload["deferred_canon"]},
                         ensure_ascii=False, indent=1))
        return
    bundle_report = None
    pack_report = None
    pack_world = None
    pack_plans = None
    pack_budgets = None
    if a.scene_bundle:
        if not a.expected_pack_sha256:
            raise SystemExit("--scene-bundle 需要 --expected-pack-sha256")
        try:
            kind = scene_bundle_kind(a.scene_bundle)
        except ScenePackError as exc:
            raise SystemExit("[scene-bundle] 拒绝：" + str(exc)) from exc
        if kind == "legacy":
            # 离线候选包：行为与改动前逐字不变（仅结构检查、不放行闸、
            # 不许进 --live——它自己声明 canon 未闭且无逐臂预算合同）。
            if not a.preflight:
                raise SystemExit("--scene-bundle 仅供 --preflight；当前运行器"
                                 "仍用合成世界，不能把候选包当作真实运行输入")
            try:
                bundle_report = check_offline_scene_bundle(
                    a.scene_bundle,
                    expected_pack_sha256=a.expected_pack_sha256)
            except SceneBundleError as exc:
                raise SystemExit("[scene-bundle] 拒绝：" + str(exc)) from exc
            if (bundle_report["book_id"] != a.book_id or
                    bundle_report["scene_count"] != a.scenes):
                raise SystemExit("[scene-bundle] book_id 或场数与 CLI 不一致")
        else:
            # 真实场景包：接 --preflight/--live 同一道闸。
            if not (a.preflight or a.live):
                raise SystemExit("真实场景包（--scene-bundle）只接 --preflight/"
                                 "--live 的场景包闸；离线跑仍用内置合成夹具")
            try:
                pack_report = check_real_scene_pack(
                    a.scene_bundle,
                    expected_pack_sha256=a.expected_pack_sha256,
                    book_id=a.book_id, scene_count=a.scenes)
                # 同一文件、同一口径再取执行侧输入（世界/场景卡/预算合同）——
                # 报告给复核人，模型对象给 run_paired，二者不可能来自两个包。
                pack_model = load_real_scene_pack(a.scene_bundle)
            except ScenePackError as exc:
                raise SystemExit("[scene-pack] 拒绝：" + str(exc)) from exc
            pack_world = pack_model.world
            pack_plans = list(pack_model.plans)
            pack_budgets = _pack_budget_contract(
                pack_model, pack_report["scene_ids"])
    # === 只读预检（独立模式）：零生成调用、零库写，打印可核对 JSON ===
    if a.preflight:
        with db.session() as s:
            pre = _preflight_with_pack(a.book_id, s, pack_report)
        if bundle_report is not None:
            pre["scene_bundle"] = bundle_report
        if pack_report is not None:
            # 逐场读数随闸一起打印（复核人不用再猜包里有什么）
            pre["scene_pack"] = pack_report
        print(json.dumps(pre, ensure_ascii=False, indent=1))
        if not pre["ready"]:
            raise SystemExit(
                f"[preflight] 拒绝（非零退出）："
                f"{pre['empty_reason'] or pre['review_reason'] or pre['world_reason']} "
                f"（book_id={a.book_id}, k3_status={pre['k3_status']}）")
        print(f"[preflight] 通过：book_id={a.book_id} 已登记，A 臂包非空"
              f"（n_techniques={pre['n_techniques']}）；{pre['world_reason']}")
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
            # 世界未登记、A 臂空、语义审查收据不可核验或场景包未过校验即拒绝，
            # 零真实调用。**live 复用 --preflight 的同一个 ready**（同一函数、
            # 同一判据），不另写一套。
            if getattr(_cfg, "LLM_MODE", "mock") == "real":
                with db.session() as s:
                    pre = _preflight_with_pack(a.book_id, s, pack_report)
                if not pre["ready"]:
                    raise SystemExit(
                        f"[preflight] 拒绝 --live 起跑（非零退出，零真实调用）："
                        f"{pre['empty_reason'] or pre['review_reason'] or pre['world_reason']}"
                        f"（book_id={a.book_id}, "
                        f"k3_status={pre['k3_status']}）")
                # 未带真实场景包 ⇒ 本次要跑的仍是内置 build_world/build_plan
                # （林穗／三枚钱合成夹具）：登记 book_id 与 K2/K3 全绿都不能把
                # 合成剧情变成真实试点，故**保留原拒绝**（行为与改动前一致）。
                # 这不是另立一套判据：pack_report is None 与闸里 pack_ok=False
                # 是同一个事实，只是把它挡在客户端构造之前，零真实调用。
                # 仅在会发起真实调用时生效（mock 模式不烧钱，见上）。
                if pack_report is None:
                    raise SystemExit(
                        "[preflight] real_scene_plan_unverified:当前 K4 世界和"
                        "场景仍为合成夹具（内置 build_world/build_plan），缺"
                        "真实作品场景卡与逐臂预算合同；零真实调用")
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
                # 每臂一个独立世界：包在场时用**包内 rev0 世界**（真实角色与
                # 事实），否则逐字走合成夹具世界（build_world）。
                store.create_world(pack_world if pack_world is not None
                                   else build_world(a.book_id))
                return store
            with db.session() as s:
                four = run_paired(factory, client, s, live=a.live,
                                  freeze=a.live, n_scenes=a.scenes,
                                  book_id=a.book_id,
                                  channel_changed=a.channel_changed,
                                  worlds_dir=str(tmp),
                                  worlds_created_at=worlds_created_at,
                                  budget_calls=a.budget_calls,
                                  scene_plans=pack_plans,
                                  budget_contract=pack_budgets)
            analysis = paired_analysis(
                four,
                ([{"scene_id": p.scene_id} for p in pack_plans]
                 if pack_plans is not None else scenes_for(a.scenes)))
            if a.live:
                # worlds_dir_cleaned 语义（审计非阻断项收口）：走到这里即
                # 正常收口——live 留库作收据（tokens 对账要读这里的
                # arm*/k4.sqlite），不清理，记 False。异常/中断且收据未落盘
                # 的分支在 finally 清理（记 stderr，收据本就无从谈起）。
                for r in four["receipts"]:
                    r["worlds_dir_cleaned"] = False
                    if pack_report is not None:
                        # 这次真跑按哪份场景卡/预算合同跑的——逐条落账，
                        # 消费侧不必猜（无包时不加此键，离线收据键集不变）。
                        r["scene_pack_sha256"] = pack_report["self_sha256"]
                        r["scene_pack_arms"] = pack_report[
                            "budget_contract"]["arms"]
            # worlds_dir 记入产物（审计非阻断项收口）：live 留库作收据——
            # tokens 对账（收据之和 vs calls 表之和）要读这里的 arm*/k4.sqlite
            out = {"artifacts": four, "analysis": analysis, "live": a.live,
                   "channel_changed": a.channel_changed,
                   "worlds_dir": str(tmp)}
            if a.live:
                out["book_id"] = a.book_id
            if pack_report is not None:
                out["scene_pack"] = {
                    "sha256": pack_report["self_sha256"],
                    "scene_ids": pack_report["scene_ids"],
                    "budget_contract_arms": pack_report[
                        "budget_contract"]["arms"],
                    "book_id_remapped": pack_report["book_id_remapped"]}
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
