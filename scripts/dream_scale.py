"""大规模 Dream：一次提交 N 场 × 双臂，成批自动跑完并汇总**可核的规模证据**。

对应 `docs/plan.md` 第 15 行后置清单「微调、**大规模 Dream**、多进程自动调度与正式发布
后置」中的**大规模 Dream**一块：把「一场一场手动跑」升级为「一次提交、成批跑完并
如实汇总」。调度层整块复用 `scripts/runtime_parallel.py`，本文件不改它任何判据，
也不写第二套提交内核；口径书见 `docs/DREAM_SCALE.md`。

边界（只读消费，不改语义）：

* 每臂 = 一个独立子进程 + 一个独立世界目录 + 一个独立 SQLite 世界库（`runtime_parallel`
  的既有不变量）；父进程从不打开任何一臂的世界库。
* 编译、预算硬闸、幂等、原子提交、outbox 全由 `app/scene_runtime/` 既有单场景内核负责。
* **离线合成通道是默认**：零网络、零真实 token（`tokens_total` 恒为 0）。
* 真模型通道要**两个独立显式开关**且默认关闭：`offline_mode="gateway"`（声明）
  **和** 环境变量 `LG_DREAM_SCALE_LIVE=1`；缺任一 → 整批在启动任何子进程之前拒跑。
  臂子进程里另有内核既有的第三道（`LG_RUNTIME_PARALLEL_LIVE=1`、`LLM_MODE=real`、
  凭据就位），本文件既不复述也不放宽。
* **缺臂即整批判负**：先过 `runtime_parallel.assemble_summary` 的覆盖完整性硬校验，
  再叠一层「每场必须齐 N 臂」的批次校验——少一臂宁可不给汇总，不给「跑了一半当全跑完」。
* **整批调用上限硬闸**：`scenes × arms × max_calls` 必须 ≤ `max_batch_calls`，
  超限整批拒跑（一个子进程都不启动）；跑完再按实际台账复核一次。
* 场与场之间**不共享世界状态**：每场一份独立的 revision 0 世界副本，因此规模证据
  覆盖「批次规模与逐臂隔离」，**不覆盖**连续叙事的连贯性（见口径书 §明确不承诺）。

用法：

    python scripts/dream_scale.py --scenes 10 --out <目录> --max-workers 8
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _entry in (str(ROOT), str(SCRIPTS)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import runtime_parallel as rp  # noqa: E402

SCHEMA = "dream-scale/1"
JOBS_SCHEMA = "dream-scale-jobs/1"
SUMMARY_NAME = "dream-scale-summary.json"
JOBS_NAME = "dream-scale-jobs.json"

LIVE_ENV = "LG_DREAM_SCALE_LIVE"
OFFLINE_MODES = ("synthetic", "gateway")
DEFAULT_OFFLINE_MODE = "synthetic"

DEFAULT_SCENES = 10
MIN_SCENES = 1
MAX_SCENES = 200
DEFAULT_ARMS = ("A", "B")
MIN_ARMS = 1
MAX_ARMS = 4
DEFAULT_BOOK_ID = "dream-scale-offline"

DEFAULT_MAX_WORKERS = 8
DEFAULT_TIMEOUT_S = rp.DEFAULT_TIMEOUT_S
DEFAULT_MAX_BATCH_CALLS = 2000
DEFAULT_ARM_DELAY_S = 0.25
DEFAULT_BUDGET = {"max_calls": 4}

SCENE_OVERRIDE_KEYS = frozenset({"offline", "budget", "arms"})
ARM_OVERRIDE_KEYS = frozenset({"offline", "budget"})
TINY = "·"


class DreamFault(rp.ParallelFault):
    """批次层的稳定拒绝码。

    继承调度层的 `ParallelFault`（同一套 `except` 就能兜住两层拒绝），但语义分开：
    `DreamFault` 一律在**启动任何子进程之前**抛出——批次级问题整批拒跑。
    """


# ── 离线合成世界与场景（确定性；零网络、零真实 token） ──────────────────────

_BEATS = ("交付灯筹", "归还绳索", "记下欠账", "换回钥匙", "点亮岸灯",
          "清点药草", "封存木匣", "划定水位", "分派口粮", "验看灯油")


def _beat(index: int) -> str:
    name = _BEATS[(index - 1) % len(_BEATS)]
    turn = (index - 1) // len(_BEATS) + 1
    return name if turn == 1 else f"{name}·第{turn}轮"


def scene_id(index: int) -> str:
    return f"scene-{index:03d}"


def arm_job_id(index: int, arm: str) -> str:
    return f"{scene_id(index)}-arm{arm}"


def scene_world(book_id: str) -> dict:
    """每场一份独立的 revision 0 世界副本（规模证据不覆盖跨场状态继承）。"""
    return {
        "book_id": book_id, "branch_id": "main", "revision": 0,
        "characters": {"lin": "林穗", "shen": "沈砚"},
        "facts": {
            "ledger.tokens": {"value": 3, "visible_to": ["lin", "shen"]},
            "ledger.handover": {"value": False, "visible_to": ["lin", "shen"]},
            "shen.secret": {"value": "沈砚私下多记了一笔，谁也不知道",
                            "visible_to": ["shen"], "reader_visible": False, "mutable": False},
        },
        "rules": [
            "同一夜、旧渡口的写实世界：无超自然能力，只有林穗与沈砚在场。",
            "只允许计划列出的持续状态变化；不新增消耗品、亲属、交易或过去经历。",
            "钱物只按计划转移；已交出的不退还。",
        ],
    }


def scene_knowledge(book_id: str, index: int) -> dict:
    return {"schema_version": "scene-knowledge/1",
            "package_id": f"ds-knowledge-{index:03d}", "book_id": book_id,
            "source_kind": "empty", "techniques": []}


def scene_plan(book_id: str, index: int) -> dict:
    """第 `index` 场的计划：A/B 双臂共享同一份计划（同题两稿）。"""
    beat = _beat(index)
    return {
        "book_id": book_id, "branch_id": "main", "scene_id": scene_id(index),
        "idempotency_key": f"{scene_id(index)}-v1", "expected_revision": 0, "pov": "lin",
        "goal": f"第{index}场{beat}。林穗要把这一笔记在明面上，沈砚想把它留在账外；"
                f"两人各让一步，本场只发生计划列出的持续状态变化，不开新线索。",
        "style": "第三人称限于林穗；只用对话与动作交代因果；不新增人物、消耗品与过去经历。",
        "min_chars": 200, "max_chars": 1400,
        "events": [{"event_id": f"beat-{index:03d}",
                    "description": f"{beat}：林穗当面交付，沈砚当面记下这一笔，"
                                   f"两人各留一句没说出口的话。",
                    "changes": [{"fact": "ledger.tokens", "before": 3, "after": 2},
                                {"fact": "ledger.handover", "before": False, "after": True}]}],
    }


# ── 参数与护栏（批次级：失败即整批拒跑，一个子进程都不启动） ──────────────

def _check_scenes(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise DreamFault("scenes_not_an_integer")
    try:
        scenes = int(value)
    except (TypeError, ValueError):
        raise DreamFault("scenes_not_an_integer") from None
    if scenes < MIN_SCENES:
        raise DreamFault(f"scenes_below_minimum:{scenes}<{MIN_SCENES}")
    if scenes > MAX_SCENES:
        raise DreamFault(f"scenes_above_cap:{scenes}>{MAX_SCENES}")
    return scenes


def _check_arms(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise DreamFault("arms_not_a_sequence")
    if len(value) < MIN_ARMS:
        raise DreamFault(f"arms_below_minimum:{len(value)}<{MIN_ARMS}")
    if len(value) > MAX_ARMS:
        raise DreamFault(f"arms_above_cap:{len(value)}>{MAX_ARMS}")
    names = [str(item).strip() for item in value]
    for name in names:
        try:
            rp._validate_identifier(name, "arm")
        except ValueError:
            raise DreamFault("arm_invalid:" + name) from None
    if len(set(names)) != len(names):
        raise DreamFault("duplicate_arm_name")
    return names


def _check_timeout(value) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise DreamFault("timeout_not_a_number") from None
    if not timeout > 0 or timeout != timeout:
        raise DreamFault("timeout_not_positive")
    return timeout


def _check_arm_delay(value) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        raise DreamFault("arm_delay_not_a_number") from None
    if not delay >= 0 or delay != delay:
        raise DreamFault("arm_delay_not_positive")
    return delay


def _check_batch_calls(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise DreamFault("max_batch_calls_not_an_integer")
    try:
        cap = int(value)
    except (TypeError, ValueError):
        raise DreamFault("max_batch_calls_not_an_integer") from None
    if cap < 1:
        raise DreamFault(f"max_batch_calls_below_minimum:{cap}<1")
    return cap


def _check_book_id(value) -> str:
    book_id = str(value or "").strip()
    try:
        rp._validate_identifier(book_id, "book_id")
    except ValueError:
        raise DreamFault("book_id_invalid") from None
    return book_id


def _mapping_or_fault(value, field, allowed=None):
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DreamFault(field + "_not_a_mapping")
    if allowed is not None:
        unknown = sorted(set(value) - set(allowed))
        if unknown:
            raise DreamFault("unknown_" + field + "_keys:" + ",".join(unknown))
    return dict(value)


def live_channel_enabled(offline_mode: str) -> bool:
    """两个开关是否都满足（声明 + `LG_DREAM_SCALE_LIVE=1`）。"""
    return str(offline_mode) == "gateway" and os.environ.get(LIVE_ENV) == "1"


def _require_live_opt_in(offline_mode: str, offline_overrides) -> bool:
    """真模型通道的双开关闸：声明 `gateway` 但没开 `LG_DREAM_SCALE_LIVE` → 整批拒跑。

    臂子进程里内核自己还有一道 `LG_RUNTIME_PARALLEL_LIVE=1` 闸与凭据/`LLM_MODE`
    检查；本函数只管批次层这一道，不替内核放宽。
    """
    if str(offline_mode) != "gateway":
        return False
    if os.environ.get(LIVE_ENV) != "1":
        raise DreamFault("gateway_channel_requires_explicit_live_opt_in")
    for key in ("writer_model", "verifier_model"):
        if not str((offline_overrides or {}).get(key) or "").strip():
            raise DreamFault("gateway_channel_requires_model_declaration:" + key)
    return True


def _check_batch_call_cap(*, scenes: int, arm_count: int, budget_doc, cap: int) -> int:
    """整批调用上限硬闸（预检）：结构上限 `场 × 臂 × max_calls` 必须 ≤ 批次上限。

    逐臂 `max_calls` 由内核硬闸执行，所以这个上界是**构造性**的：跑完的台账不可能
    超过它。这里不静默放大上限，只响亮拒跑（费用护栏，意图错位比失败危险）。
    """
    planned = int(scenes) * int(arm_count) * int(budget_doc["max_calls"])
    if planned > cap:
        raise DreamFault(f"batch_call_cap_exceeded:{planned}>{cap}")
    return planned


# ── 批次展开：N 场 × 双臂 → 作业清单 + 场×臂矩阵 ──────────────────────────

def _arm_offline(*, mode, delay_s, overrides, index: int, arm: str, beat: str):
    """逐臂离线通道 spec：双臂共享计划/世界，只在生成通道上不同。

    `delay_s` 走既有合成通道的 `delay_s` 字段——它的 sleep 只在 `behavior="hang"`
    下生效（既有语义，本文件不改），因此用 `hang` + 短延时制造真实重叠窗口，
    让实测并发可核；真故障仍由 `timeout_s` 边界判定，不靠行为名。
    """
    if str(mode) == "gateway":
        spec = {"mode": "gateway"}
    else:
        spec = {"behavior": "hang" if delay_s > 0 else "commit", "delay_s": float(delay_s),
                "text": f"第{index}场{TINY}{arm}臂：{beat}。离线合成正文，无真实模型输出。"}
    spec.update(overrides or {})
    return spec


def build_jobs(*, scenes, arms, book_id, offline_mode=DEFAULT_OFFLINE_MODE,
               arm_delay_s=0.0, offline_overrides=None, scene_overrides=None):
    """展开「N 场 × 双臂」：返回 `(jobs, matrix)`。

    `jobs` 交给调度层（每臂一条作业）；`matrix` 是批次自己的场×臂清单，规模汇总
    按它逐场逐臂核对覆盖率。`scene_overrides` 的键是场号（1 起），值只允许
    `offline` / `budget` / `arms` 三个键：`arms` 再按臂名给 `offline` / `budget`
    ——逐场逐臂调预算或注入故障形态（止损演练、负例）用，不接受其它开口。
    """
    scene_count = _check_scenes(scenes)
    arm_names = _check_arms(arms)
    book = _check_book_id(book_id)
    mode = str(offline_mode)
    batch_overrides = _mapping_or_fault(offline_overrides, "offline_overrides")
    overrides = {}
    for key, value in _mapping_or_fault(scene_overrides, "scene_overrides").items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise DreamFault(f"scene_override_key_invalid:{key}") from None
        if not 1 <= index <= scene_count:
            raise DreamFault(f"scene_override_out_of_range:{index}:not_in_1..{scene_count}")
        overrides[index] = _mapping_or_fault(value, f"scene_override_{index}",
                                             allowed=SCENE_OVERRIDE_KEYS)
    jobs, matrix = [], []
    for index in range(1, scene_count + 1):
        beat = _beat(index)
        plan = scene_plan(book, index)
        world = scene_world(book)
        knowledge = scene_knowledge(book, index)
        per_scene = overrides.get(index, {})
        arm_overrides = {}
        for arm_name, value in _mapping_or_fault(
                per_scene.get("arms"), f"scene_override_{index}_arms").items():
            if str(arm_name) not in arm_names:
                raise DreamFault(f"scene_override_arm_unknown:{index}:{arm_name}")
            arm_overrides[str(arm_name)] = _mapping_or_fault(
                value, f"scene_override_{index}_arm_{arm_name}", allowed=ARM_OVERRIDE_KEYS)
        slots = []
        for arm in arm_names:
            job_id = arm_job_id(index, arm)
            per_arm = arm_overrides.get(arm, {})
            job = {"job_id": job_id, "arm": arm, "book_id": book,
                   "scene_id": scene_id(index), "plan": copy.deepcopy(plan),
                   "knowledge": copy.deepcopy(knowledge), "world": copy.deepcopy(world),
                   "offline": _arm_offline(mode=mode, delay_s=arm_delay_s,
                                           overrides={**batch_overrides,
                                                      **(per_scene.get("offline") or {}),
                                                      **(per_arm.get("offline") or {})},
                                           index=index, arm=arm, beat=beat)}
            job_budget = per_scene.get("budget")
            if per_arm.get("budget") is not None:
                job_budget = {**(job_budget or {}), **per_arm["budget"]}
            if job_budget is not None:
                job["budget"] = dict(job_budget)
            jobs.append(job)
            slots.append({"arm": arm, "job_id": job_id})
        matrix.append({"index": index, "scene_id": scene_id(index),
                       "idempotency_key": plan["idempotency_key"], "arms": slots})
    return jobs, matrix


def _batch_id(book_id: str, jobs, budget_doc) -> str:
    """批次身份 = 作业矩阵 + 预算的摘要：同一矩阵重放即同一批次（幂等的可核凭据）。"""
    from app.scene_runtime.contracts import digest
    return "batch-" + digest({"book_id": book_id, "budget": budget_doc,
                              "jobs": jobs})[:16]


# ── 规模汇总（缺臂即整批判负） ─────────────────────────────────────────

def assemble_scale_summary(base, *, matrix, batch_id, book_id, workdir, wall_ms,
                           max_workers, timeout_s, max_batch_calls, calls_cap_max,
                           offline_mode, arm_names):
    """把调度层汇总收敛成**规模证据**；少一臂直接抛，绝不给「跑了一半」的批次结论。

    两层校验分工：`runtime_parallel.assemble_summary` 已保证逐臂记录不重、不越权、
    不缺；这里再加批次特有的一层——**每场必须齐 `len(arms)` 臂**，且场数必须等于
    提交的场数。预检阶段整批拒跑（一个子进程都没启动）不算「缺臂」，那种情况如实
    记 `not_launched`，绝不替它编造结果。
    """
    records = list(base.get("arms") or [])
    by_job: dict[str, dict] = {}
    for record in records:
        job_id = str(record.get("job_id"))
        if job_id in by_job:
            raise DreamFault("duplicate_arm_record:" + job_id)
        by_job[job_id] = record
    expected = [slot["job_id"] for scene in matrix for slot in scene["arms"]]
    if len(expected) != len(set(expected)):
        raise DreamFault("duplicate_scene_arm_job_id")
    unknown = sorted(set(by_job) - set(expected))
    if unknown:
        raise DreamFault("unsubmitted_arm_record:" + ",".join(unknown))
    missing = [job_id for job_id in expected if job_id not in by_job]
    rejected_at = base.get("rejected_at")
    if missing and not rejected_at:
        raise DreamFault("scene_arm_missing:" + ",".join(missing))

    not_launched_reason = str(base.get("parallel_ok_reason") or "rejected_before_launch")
    rows: list[dict] = []
    failed: list[dict] = []
    scene_rows: list[dict] = []
    for scene in matrix:
        arm_rows = []
        for slot in scene["arms"]:
            record = by_job.get(slot["job_id"])
            if record is None:
                row = {"job_id": slot["job_id"], "scene_id": scene["scene_id"],
                       "arm": slot["arm"], "status": "not_launched", "error": not_launched_reason,
                       "calls": 0, "tokens": 0, "revision": None, "commit_id": None,
                       "text_hash": None, "reused": False, "result_verified": False,
                       "world_dir": None, "db_path": None, "prose_path": None, "pid": None,
                       "exit_code": None, "timed_out": False, "reclaimed": None,
                       "wall_ms": 0, "kernel_ms": None, "stdout_tail": "", "stderr_tail": ""}
            else:
                row = {"job_id": slot["job_id"], "scene_id": str(record.get("scene_id") or scene["scene_id"]),
                       "arm": str(record.get("arm") or slot["arm"]),
                       "status": str(record.get("status")), "error": record.get("error"),
                       "calls": int(record.get("calls") or 0),
                       "tokens": int(record.get("tokens") or 0),
                       "revision": record.get("revision"), "commit_id": record.get("commit_id"),
                       "text_hash": record.get("text_hash"), "reused": bool(record.get("reused")),
                       "result_verified": bool(record.get("result_verified")),
                       "world_dir": record.get("world_dir"), "db_path": record.get("db_path"),
                       "prose_path": record.get("prose_path"), "pid": record.get("pid"),
                       "exit_code": record.get("exit_code"),
                       "timed_out": bool(record.get("timed_out")), "reclaimed": record.get("reclaimed"),
                       "wall_ms": int(record.get("wall_ms") or 0), "kernel_ms": record.get("kernel_ms"),
                       "stdout_tail": str(record.get("stdout_tail") or ""),
                       "stderr_tail": str(record.get("stderr_tail") or "")}
            arm_rows.append(row)
            rows.append(row)
            if row["status"] != "committed":
                failed.append({"scene_id": row["scene_id"], "arm": row["arm"],
                               "job_id": row["job_id"], "status": row["status"],
                               "error": row["error"], "calls": row["calls"]})
        scene_rows.append({"scene_id": scene["scene_id"], "expected_arms": len(scene["arms"]),
                           "committed_arms": sum(1 for r in arm_rows if r["status"] == "committed"),
                           "calls": sum(r["calls"] for r in arm_rows),
                           "scene_ok": all(r["status"] == "committed" for r in arm_rows),
                           "arms": arm_rows})

    submitted = len(expected)
    committed = sum(1 for row in rows if row["status"] == "committed")
    calls_total = sum(row["calls"] for row in rows)
    cap_ok = calls_total <= int(max_batch_calls)
    expected_arms_per_scene = len(arm_names)
    coverage_ok = all(len(scene["arms"]) == expected_arms_per_scene for scene in matrix)
    ok = bool(rows) and coverage_ok and committed == submitted and all(
        scene["scene_ok"] for scene in scene_rows) and cap_ok
    rate = None
    if wall_ms and int(wall_ms) > 0:
        rate = {"scenes_per_minute": round(60.0 * len(scene_rows) * 1000 / int(wall_ms), 3),
                "arms_per_minute": round(60.0 * len(rows) * 1000 / int(wall_ms), 3)}
    return {
        "schema": SCHEMA, "runtime_version": base.get("runtime_version"),
        "base_schema": base.get("schema"), "batch_id": batch_id, "book_id": book_id,
        "workdir": str(workdir), "offline_mode": str(offline_mode),
        "live_channel_enabled": live_channel_enabled(offline_mode),
        "scenes": len(scene_rows), "arms_per_scene": list(arm_names),
        "coverage_ok": coverage_ok,
        "jobs_submitted": submitted, "jobs_committed": committed,
        "jobs_failed": submitted - committed,
        "failed_job_ids": [item["job_id"] for item in failed],
        "failed_arms": failed,
        "scenes_committed": sum(1 for scene in scene_rows if scene["scene_ok"]),
        "calls_total": calls_total,
        "tokens_total": sum(row["tokens"] for row in rows),
        "call_duration_ms_total": int(base.get("call_duration_ms_total") or 0),
        "calls_cap": {"max_batch_calls": int(max_batch_calls),
                      "planned_max": int(calls_cap_max), "spent": calls_total, "ok": cap_ok},
        "reused_jobs": [row["job_id"] for row in rows if row["reused"]],
        "wall_ms": int(wall_ms), "arms_wall_ms": int(base.get("wall_ms") or 0),
        "max_workers": int(max_workers), "timeout_s": float(timeout_s),
        "parallelism_measured": int(base.get("parallelism_measured") or 0),
        "parallel_ok": bool(base.get("parallel_ok")),
        "parallel_ok_reason": base.get("parallel_ok_reason"),
        "concurrency_bound_ok": bool(base.get("concurrency_bound_ok")),
        "launched": int(base.get("launched") or 0),
        "reclaim_problems": list(base.get("reclaim_problems") or []),
        "rejected_at": rejected_at, "rejected_jobs": list(base.get("rejected_jobs") or []),
        "complete": bool(rows) and all(row["result_verified"] for row in rows),
        "ok": ok, "verdict": "accept" if ok else "reject",
        "scene_matrix": scene_rows, "arms": rows,
        "throughput_offline": rate,
        "parallel_summary_path": str(Path(workdir) / rp.SUMMARY_NAME),
        "cost": None, "literary_quality": "not_evaluated",
    }


def require_ok(summary, *, parallel: bool = True):
    """把规模汇总变成可执行判定：任何一场不齐、任何一臂未提交都抛。"""
    if not summary.get("calls_cap", {}).get("ok", False):
        cap = summary.get("calls_cap") or {}
        raise DreamFault(f"batch_call_cap_exceeded_post_run:{cap.get('spent')}>{cap.get('max_batch_calls')}")
    if not summary.get("ok"):
        failed = summary.get("failed_job_ids") or []
        raise DreamFault("dream_scale_rejected:" + ",".join(map(str, failed)))
    if parallel and not summary.get("parallel_ok"):
        raise DreamFault("parallelism_not_met:" + str(summary.get("parallel_ok_reason")))
    if summary.get("reclaim_problems"):
        raise DreamFault("arm_not_reclaimed:" + ",".join(summary["reclaim_problems"]))
    return summary


# ── 入口 ─────────────────────────────────────────────────────────────────

def run_scale(*, out, scenes=DEFAULT_SCENES, arms=DEFAULT_ARMS, book_id=DEFAULT_BOOK_ID,
              max_workers=DEFAULT_MAX_WORKERS, budget=DEFAULT_BUDGET,
              max_batch_calls=DEFAULT_MAX_BATCH_CALLS, timeout_s=DEFAULT_TIMEOUT_S,
              arm_delay_s=DEFAULT_ARM_DELAY_S, offline_mode=DEFAULT_OFFLINE_MODE,
              offline_overrides=None, scene_overrides=None) -> dict:
    """一次提交 N 场 × 双臂，成批跑完并返回规模证据汇总。

    所有批次级护栏（场数/臂名/并发越界/真模型双开关/整批调用上限/真库路径）都在
    **启动任何子进程之前**判定并抛 `DreamFault`；臂级失败（预算耗尽、超时、崩溃）
    只记该臂并把整批判负——两种口径都不许「跑了一半当全跑完」。
    """
    scene_count = _check_scenes(scenes)
    arm_names = _check_arms(arms)
    book = _check_book_id(book_id)
    mode = str(offline_mode or DEFAULT_OFFLINE_MODE)
    if mode not in OFFLINE_MODES:
        raise DreamFault("unknown_offline_mode:" + mode)
    offline_overrides = _mapping_or_fault(offline_overrides, "offline_overrides")
    _require_live_opt_in(mode, offline_overrides)
    workers = rp._check_max_workers(max_workers)
    timeout = _check_timeout(timeout_s)
    delay = _check_arm_delay(arm_delay_s)
    budget_doc = rp._budget_doc(budget)
    cap = _check_batch_calls(max_batch_calls)
    planned = _check_batch_call_cap(scenes=scene_count, arm_count=len(arm_names),
                                    budget_doc=budget_doc, cap=cap)
    jobs, matrix = build_jobs(scenes=scene_count, arms=arm_names, book_id=book,
                              offline_mode=mode, arm_delay_s=delay,
                              offline_overrides=offline_overrides,
                              scene_overrides=scene_overrides)
    root = Path(out).expanduser().resolve()
    rp._refuse_protected(root)                     # 真库护栏：复用既有双向判定
    batch_id = _batch_id(book, jobs, budget_doc)
    started = time.monotonic()
    base = rp.run_parallel(jobs, max_workers=workers, budget=budget_doc,
                           workdir=root, timeout_s=timeout)
    wall_ms = round((time.monotonic() - started) * 1000)
    summary = assemble_scale_summary(base, matrix=matrix, batch_id=batch_id, book_id=book,
                                     workdir=root, wall_ms=wall_ms, max_workers=workers,
                                     timeout_s=timeout, max_batch_calls=cap,
                                     calls_cap_max=planned, offline_mode=mode,
                                     arm_names=arm_names)
    rp._write_json(root / JOBS_NAME, {
        "schema": JOBS_SCHEMA, "batch_id": batch_id, "book_id": book,
        "offline_mode": mode, "live_channel_enabled": live_channel_enabled(mode),
        "scenes": scene_count, "arms_per_scene": list(arm_names), "budget": budget_doc,
        "calls_cap_max": planned, "max_batch_calls": cap, "matrix": matrix, "jobs": jobs})
    rp._write_json(root / SUMMARY_NAME, summary)
    return summary


SUMMARY_PRINT_KEYS = ("ok", "verdict", "complete", "batch_id", "scenes", "arms_per_scene",
                      "coverage_ok", "jobs_submitted", "jobs_committed", "jobs_failed",
                      "failed_job_ids", "calls_total", "tokens_total", "wall_ms",
                      "arms_wall_ms", "max_workers", "parallelism_measured", "parallel_ok",
                      "parallel_ok_reason", "reused_jobs", "workdir")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="大规模 Dream：一次提交 N 场 × 双臂，成批跑完并汇总规模证据。")
    parser.add_argument("--scenes", type=int, default=DEFAULT_SCENES)
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    parser.add_argument("--out", type=Path, required=True, help="批次工作根（汇总与各臂世界目录）")
    parser.add_argument("--book-id", default=DEFAULT_BOOK_ID)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--budget", help="运行级预算合同（JSON 文本，默认 %s）" % json.dumps(DEFAULT_BUDGET))
    parser.add_argument("--max-batch-calls", type=int, default=DEFAULT_MAX_BATCH_CALLS)
    parser.add_argument("--arm-delay-s", type=float, default=DEFAULT_ARM_DELAY_S,
                        help="逐臂合成通道延时（秒）；0 = 不制造重叠窗口")
    parser.add_argument("--offline-mode", choices=OFFLINE_MODES, default=DEFAULT_OFFLINE_MODE)
    parser.add_argument("--offline-json", help="离线通道参数覆盖（JSON 文本）")
    parser.add_argument("--scene-json",
                        help='逐场覆盖（JSON 文本，如 {"3": {"budget": {"max_calls": 4}, '
                             '"arms": {"B": {"offline": {"behavior": "hard"}}}}）')
    args = parser.parse_args(argv)
    budget = json.loads(args.budget) if args.budget else DEFAULT_BUDGET
    summary = run_scale(
        out=args.out, scenes=args.scenes,
        arms=[item for item in str(args.arms).split(",") if item.strip()],
        book_id=args.book_id, max_workers=args.max_workers, budget=budget,
        max_batch_calls=args.max_batch_calls, timeout_s=args.timeout_s,
        arm_delay_s=args.arm_delay_s, offline_mode=args.offline_mode,
        offline_overrides=json.loads(args.offline_json) if args.offline_json else None,
        scene_overrides=json.loads(args.scene_json) if args.scene_json else None)
    print(json.dumps({key: summary[key] for key in SUMMARY_PRINT_KEYS},
                     ensure_ascii=False, sort_keys=True), flush=True)
    try:
        require_ok(summary, parallel=False)
    except DreamFault as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    if not summary["parallel_ok"] or summary["reclaim_problems"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except rp.ParallelFault as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        raise SystemExit(2)
