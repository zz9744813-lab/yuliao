"""多进程自动调度：让已有的单场景内核**并行**跑多场景 / 多臂。

对应 `docs/plan.md` 第 15 行后置清单「微调、大规模 Dream、多进程自动调度与正式发布
后置」中的**多进程自动调度**一块（微调与大规模 Dream 不在本文件范围，见
`docs/RUNTIME_PARALLEL.md`）。

并行边界（本文件**只读调用** `app/scene_runtime/`，不改 `app/` 任何文件）：

* 每臂 = 一个**独立子进程** + 一个**独立工作目录** + 一个**独立 SQLite 世界库**；
  进程之间不共享任何连接（父进程从不打开任何一臂的世界库）。
* 编译、核验、预算、幂等、原子提交、outbox 全部由既有单场景内核
  （`SceneRunner` + `Store`）负责——**本文件不另写一套提交内核**。
* 硬闸沿用内核同码：每臂 `max_calls` 超限即 `call_budget_exhausted`。
* 单臂失败不外溢：逐臂独立进程 + 逐臂终态，退出时逐臂汇总。
* 离线合成 writer/verifier：零真实模型调用、零远端网关、零真库写。

进程模型选型（`subprocess` 而非 `multiprocessing`，理由见上）：

1. **超时回收必须是硬回收**。`multiprocessing.Process.terminate()` 在 POSIX 上只
   发 SIGTERM，可被子进程捕获或忽略，「超时必被回收」就不是保证；`Popen.kill()`
   + `communicate()` 在任何平台都是强杀并 wait 到进程句柄。
2. **离线替身不必可 pickle**。合成客户端由子进程按声明式 spec 就地构造，测试替身
   与生产实现不会走两条不同代码路径（`multiprocessing` 只能传可 pickle 的对象/工厂）。
3. **连接独立是构造性的**。子进程是全新解释器，SQLite 连接天然独立，不用依赖
   「spawn 不继承 fd」这类平台细节（POSIX 的 fork 反而会继承父进程连接与 WAL 锁）。
   代价：每臂一次解释器启动（Windows 上约 0.2–0.4s），已在 `docs/RUNTIME_PARALLEL.md`
   记入成本口径。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "runtime-parallel/1"
ARM_SCHEMA = "runtime-parallel-arm/1"
SUMMARY_NAME = "parallel-summary.json"
SPEC_NAME = "job.json"
RESULT_NAME = "result.json"
LOCK_NAME = "arm.lock"
PROSE_NAME = "prose.md"
DB_NAME = "runtime.sqlite"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_WORKERS = 4
MIN_CONCURRENCY = 1
TAIL_CHARS = 400
SCRIPT = Path(__file__).resolve()
OFFLINE_BEHAVIORS = ("commit", "hard", "hang", "crash")
JOB_KEYS = frozenset({
    "job_id", "book_id", "scene_id", "arm", "branch_id", "idempotency_key",
    "world_dir", "db_name", "plan", "knowledge", "world", "budget", "offline",
})
IDENTIFIER_HINT = "^[a-zA-Z0-9_.-]{1,100}$"


class ParallelFault(RuntimeError):
    """稳定的调度层拒绝码（不含上游响应原文或密钥）。"""


# ── 文件与锁 ────────────────────────────────────────────────────────────

def _write_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _lock_backend():
    if os.name == "nt":
        import msvcrt
        return "msvcrt", msvcrt
    try:
        import fcntl
    except ImportError:
        return None, None
    return "fcntl", fcntl


def _hold_arm_lock(path):
    """臂进程存活期间独占目录锁；被强杀时由内核释放——父进程据此证明已回收。"""
    backend, mod = _lock_backend()
    stream = open(path, "a+b")
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    if backend == "msvcrt":
        mod.locking(stream.fileno(), mod.LK_LOCK, 1)
    elif backend == "fcntl":
        mod.flock(stream.fileno(), mod.LOCK_EX)
    return stream


def _arm_lock_free(path) -> bool | None:
    """锁不存在 ⇒ 没有任何进程在持锁；能独占取得 ⇒ 原持锁进程已死。"""
    backend, mod = _lock_backend()
    path = Path(path)
    if not path.exists():
        return True
    if backend is None:
        return None
    try:
        stream = open(path, "a+b")
    except OSError:
        return False
    try:
        stream.seek(0)
        try:
            if backend == "msvcrt":
                mod.locking(stream.fileno(), mod.LK_NBLCK, 1)
                mod.locking(stream.fileno(), mod.LK_UNLCK, 1)
            else:
                mod.flock(stream.fileno(), mod.LOCK_EX | mod.LOCK_NB)
                mod.flock(stream.fileno(), mod.LOCK_UN)
        except OSError:
            return False
        return True
    finally:
        stream.close()


# ── 离线合成通道（零网络 / 零 token / 确定性） ──────────────────────────

class SyntheticClient:
    """离线合成 writer/verifier：不连网关、不产生真实 token、不读任何凭据。

    `behavior` 只控制**故障形态**，正例一律 `commit`：writer 给一段确定性正文，
    verifier 逐条引原文并照抄计划里的 `changes`——机械闸全过，于是每臂正好
    2 次调用（writer.0 + verifier.0）即提交。

    · `hard`  —— verifier 恒报 hard issue：烧满 `max_rewrites` 后如实失败；
    · `hang`  —— 每次回复前睡 `delay_s`：交给父进程超时回收；
    · `crash` —— 直接 `os._exit`：不走异常处理、不落结果文件（按缺臂处理）。
    """

    models = {"writer": "offline-synthetic-writer/1",
              "verifier": "offline-synthetic-verifier/1",
              "transport": "offline-synthetic/1"}

    def __init__(self, *, behavior="commit", delay_s=0.0, text=None):
        if behavior not in OFFLINE_BEHAVIORS:
            raise ParallelFault("unknown_offline_behavior")
        self.behavior = behavior
        self.delay_s = max(0.0, float(delay_s or 0.0))
        self.text = text
        self.calls = 0

    def invoke(self, *, role, system, payload, max_tokens, timeout):
        from app.scene_runtime.contracts import canonical
        self.calls += 1
        if self.behavior == "hang":
            time.sleep(self.delay_s)
        if role == "writer":
            result = {"text": self._draft(payload)}
        else:
            plan = payload["plan"]
            quote = payload["text"]
            result = {
                "issues": ([{"kind": "hard",
                             "description": "离线合成注入：恒报一个 hard 问题，烧满修稿额度后如实失败",
                             "quote": quote}] if self.behavior == "hard" else []),
                "events": [{"event_id": e["event_id"], "quote": quote} for e in plan["events"]],
                "changes": [{"fact": c["fact"], "after": c["after"], "quote": quote}
                            for e in plan["events"] for c in e["changes"]],
            }
        if self.behavior == "crash":
            os._exit(97)
        return {"text": canonical(result), "requested_model": self.models[role],
                "actual_model": self.models[role], "tokens_in": None, "tokens_out": None,
                "finish_reason": "stop", "provider_request_id": None}

    def _draft(self, payload) -> str:
        plan = payload.get("plan") or payload["context"]["plan"]
        low, high = int(plan["min_chars"]), int(plan["max_chars"])
        base = self.text or "".join(str(e["description"]) for e in plan["events"]) or str(plan["goal"])
        filler = "离线合成正文，无真实模型输出。"
        if len(base) < low:
            base = base + filler * ((low - len(base)) // len(filler) + 1)
        return base[:high]


def _build_client(spec):
    """按声明式 spec 在**子进程内**构造客户端；live 通道需两个独立显式开关。"""
    from app.scene_runtime.contracts import RuntimeFault
    mode = str((spec or {}).get("mode", "synthetic"))
    if mode == "gateway":
        if os.environ.get("LG_RUNTIME_PARALLEL_LIVE") != "1":
            raise RuntimeFault("gateway_channel_requires_explicit_live_opt_in")
        from app.scene_runtime.client import GatewayClient
        return GatewayClient(str(spec.get("writer_model")), str(spec.get("verifier_model")))
    if mode != "synthetic":
        raise RuntimeFault("unknown_client_mode")
    return SyntheticClient(behavior=(spec or {}).get("behavior", "commit"),
                           delay_s=(spec or {}).get("delay_s", 0.0),
                           text=(spec or {}).get("text"))


# ── 作业规范化与预检 ────────────────────────────────────────────────────

def _validate_identifier(value, field) -> None:
    from pydantic import TypeAdapter, ValidationError
    from app.scene_runtime.contracts import Identifier
    try:
        TypeAdapter(Identifier).validate_python(value)
    except ValidationError:
        raise ValueError(f"{field}_invalid") from None


def _budget_doc(budget):
    from pydantic import ValidationError
    from app.scene_runtime.contracts import Budget
    if budget is None:
        return Budget().model_dump()
    if isinstance(budget, Budget):
        return budget.model_dump()
    if not isinstance(budget, Mapping):
        raise ParallelFault("budget_not_a_mapping")
    data = dict(budget)
    unknown = sorted(set(data) - set(Budget.model_fields))
    if unknown:
        raise ParallelFault("unknown_budget_keys:" + ",".join(unknown))
    try:
        return Budget.model_validate(data).model_dump()
    except ValidationError as exc:
        raise ParallelFault("budget_contract_invalid") from exc


def _protected_paths():
    """只读真库所在目录：调度层拒写（默认研究库目录 + LG_DATABASE_URL 指向的库）。"""
    from app import config
    out = [Path(config.ROOT) / "data"]
    url = (os.environ.get("LG_DATABASE_URL") or config.DATABASE_URL or "").strip()
    if url.startswith("sqlite:///"):
        out.append(Path(url[len("sqlite:///"):]).parent)
    return [p.resolve() for p in out]


def _refuse_protected(path: Path) -> None:
    """拒写真库所在的目录树，两个方向都拒：目录本身、以及它的父目录。

    往真库目录里写臂目录（`data/arms/...`）和把真库目录当工作根（`--out data`）
    同样会把运行产物混进研究库，两个方向都必须响亮拒绝。
    """
    target = Path(path).resolve()
    for protected in _protected_paths():
        if target == protected or protected in target.parents or target in protected.parents:
            raise ParallelFault("workdir_conflicts_research_database")


def _normalize_job(job, index, root: Path, budget_doc):
    if not isinstance(job, Mapping):
        raise ValueError("job_not_a_mapping")
    unknown = sorted(set(job) - JOB_KEYS)
    if unknown:
        raise ValueError("unknown_job_keys:" + ",".join(unknown))
    job_id = str(job.get("job_id") or "").strip()
    _validate_identifier(job_id, "job_id")
    for key in ("plan", "knowledge", "world"):
        if not isinstance(job.get(key), Mapping):
            raise ValueError(f"job_{key}_missing")
    plan = job["plan"]
    scene_id = str(job.get("scene_id") or plan.get("scene_id") or "").strip()
    book_id = str(job.get("book_id") or plan.get("book_id") or "").strip()
    branch_id = str(job.get("branch_id") or plan.get("branch_id") or "main").strip()
    idem = str(job.get("idempotency_key") or plan.get("idempotency_key") or "").strip()
    arm = str(job.get("arm") or "A").strip()
    for field, value in (("scene_id", scene_id), ("book_id", book_id),
                         ("branch_id", branch_id), ("idempotency_key", idem), ("arm", arm)):
        _validate_identifier(value, field)
    declared = job.get("world_dir")
    world_dir = (Path(declared) if declared else root / job_id)
    if not world_dir.is_absolute():
        world_dir = root / world_dir
    world_dir = world_dir.resolve()
    _refuse_protected(world_dir)
    db_name = str(job.get("db_name") or DB_NAME).strip()
    if db_name != Path(db_name).name or not db_name.lower().endswith(".sqlite"):
        raise ValueError("db_name_must_be_a_plain_sqlite_filename")
    budget = dict(budget_doc)
    extra = job.get("budget")
    if extra is not None:
        from app.scene_runtime.contracts import Budget
        if not isinstance(extra, Mapping):
            raise ValueError("job_budget_not_a_mapping")
        unknown_budget = sorted(set(extra) - set(Budget.model_fields))
        if unknown_budget:
            raise ValueError("unknown_budget_keys:" + ",".join(unknown_budget))
        budget.update(extra)
    offline = job.get("offline")
    if offline is None:
        offline = {}
    if not isinstance(offline, Mapping):
        raise ValueError("job_offline_not_a_mapping")
    return {
        "index": index,
        "job_id": job_id, "arm": arm, "book_id": book_id, "scene_id": scene_id,
        "branch_id": branch_id, "idempotency_key": idem,
        "world_dir": str(world_dir), "world_key": os.path.normcase(str(world_dir)),
        "db_name": db_name, "db_path": str(world_dir / db_name),
        "plan": dict(plan), "knowledge": dict(job["knowledge"]), "world": dict(job["world"]),
        "budget": budget, "offline": dict(offline),
    }


def _preflight(jobs, root: Path, budget_doc):
    """返回 (specs, problems)。

    `specs` 覆盖**每个**提交的作业（规范化失败的是占位 spec），`problems` 非空即
    整轮拒跑：跨臂冲突（重名 job_id、同一世界目录）与本臂规格错误都属于「提交
    级」问题——共享世界目录意味着「每臂独立世界库」这条不变量已被打破，无法判断
    哪一臂拥有它，因此不启动任何子进程，而不是让两臂去争同一条 SQLite。
    """
    specs, problems = [], []
    for index, job in enumerate(jobs):
        try:
            specs.append(_normalize_job(job, index, root, budget_doc))
        except (ValueError, ParallelFault) as exc:
            job_id = ""
            if isinstance(job, Mapping) and job.get("job_id"):
                job_id = str(job["job_id"])
            specs.append({"job_id": job_id or f"job-{index}", "_invalid": True})
            problems.append({"index": index, "job_id": job_id or f"job-{index}",
                             "error": str(exc) or type(exc).__name__})
    owners = {}
    for spec in specs:
        if spec.get("_invalid"):
            continue
        if spec["job_id"] in owners:
            problems.append({"index": spec["index"], "job_id": spec["job_id"],
                             "error": "duplicate_job_id:" + spec["job_id"]})
        owners[spec["job_id"]] = spec["world_key"]
    holders_by_dir = {}
    for spec in specs:
        if not spec.get("_invalid"):
            holders_by_dir.setdefault(spec["world_key"], []).append(spec)
    for key, holders in sorted(holders_by_dir.items()):
        if len(holders) > 1:
            problems.append({"world_dir": key,
                             "job_ids": [h["job_id"] for h in holders],
                             "index": [h["index"] for h in holders],
                             "error": "world_dir_contended:" + key})
    return specs, problems


# ── 臂执行（子进程入口） ────────────────────────────────────────────────

def _execute_arm(spec, world_dir: Path):
    from app.scene_runtime.contracts import (Budget, KnowledgePackage, RuntimeFault,
                                             ScenePlan, World)
    from app.scene_runtime.pipeline import SceneRunner
    from app.scene_runtime.store import Store
    plan = ScenePlan.model_validate(spec["plan"])
    knowledge = KnowledgePackage.model_validate(spec["knowledge"])
    world = World.model_validate(spec["world"])
    budget = Budget.model_validate(spec["budget"])
    if (plan.book_id, plan.branch_id) != (world.book_id, world.branch_id):
        raise RuntimeFault("arm_world_scope_conflict")
    store = Store(world_dir / spec.get("db_name", DB_NAME))
    store.create_world(world)
    receipt = SceneRunner(store, _build_client(spec.get("offline"))).run(plan, knowledge, budget)
    job_id = receipt["job_id"]
    usage = store.usage(job_id) or {}
    projected = store.project()
    audit = store.audit(plan.book_id)
    scene = next((s for s in store.export(plan.book_id) if s["job_id"] == job_id), None)
    prose_path = world_dir / PROSE_NAME
    prose_path.write_text((scene or {}).get("text", ""), encoding="utf-8")
    out = {
        "job_id": spec["job_id"], "arm": spec["arm"], "book_id": plan.book_id,
        "branch_id": plan.branch_id, "scene_id": plan.scene_id,
        "world_dir": str(world_dir), "db_path": spec.get("db_path", str(world_dir / DB_NAME)),
        "kernel_job_id": job_id, "commit_id": receipt.get("commit_id"),
        "revision": receipt.get("revision"), "status": str(receipt.get("status")),
        "reused": bool(receipt.get("reused")),
        "calls": int(usage.get("calls", 0)), "tokens": int(usage.get("tokens", 0)),
        "call_duration_ms": int(usage.get("duration_ms", 0)),
        "verifier_invalid_retries": int(usage.get("verifier_invalid_retries", 0)),
        "projections": projected, "pending_projections": audit["pending_projections"],
        "audit_ok": bool(audit["ok"]), "audit": audit,
        "text_hash": receipt.get("text_hash"), "prose_path": str(prose_path),
        "error": None, "result_verified": True,
    }
    if not audit["ok"]:
        out["status"] = "canon_audit_failed:" + ",".join(audit["errors"])
        out["error"] = out["status"]
        out["result_verified"] = False
    return out


def _spent_after_failure(spec, world_dir: Path):
    """失败臂**已烧掉的**调用照实计入汇总（止损台账不许把失败臂记成 0 调用）。

    复用内核的 `Store.usage` 聚合口径，只读查一次 `jobs` 表定位本臂的冻结 job；
    定位不到（连世界库都没建成）就如实返回空，不猜。
    """
    from app.scene_runtime.store import Store
    db_path = world_dir / str(spec.get("db_name") or DB_NAME)
    if not db_path.exists():
        return {}
    try:
        store = Store(db_path)
        with store.connection() as db:
            row = db.execute("SELECT id FROM jobs WHERE book=? AND branch=? AND idem=?",
                             (spec.get("book_id"), spec.get("branch_id"),
                              spec.get("idempotency_key"))).fetchone()
        if row is None:
            return {}
        kernel_job_id = str(row[0])
        usage = store.usage(kernel_job_id) or {}
    except Exception:  # noqa: BLE001 —— 记账失败不许盖掉真正的失败原因
        return {}
    return {"kernel_job_id": kernel_job_id, "calls": int(usage.get("calls", 0)),
            "tokens": int(usage.get("tokens", 0)),
            "call_duration_ms": int(usage.get("duration_ms", 0)),
            "verifier_invalid_retries": int(usage.get("verifier_invalid_retries", 0))}


def run_arm(spec_path, result_path):
    """一个臂 = 一个世界目录 + 一个 SQLite 世界库 + 一次单场景内核跑。

    无论成功失败都落一份 `result.json`（原子写），父进程据此逐臂汇总；只有被强杀
    的臂才会缺这份文件——那正是「缺臂」信号，父进程不得替它编造结果。
    """
    from pydantic import ValidationError
    from app.scene_runtime.contracts import RuntimeFault
    spec = _read_json(spec_path) if spec_path else {}
    world_dir = Path(str(spec.get("world_dir") or "."))
    started = time.monotonic()
    record = {"schema": ARM_SCHEMA, "job_id": str(spec.get("job_id") or ""), "pid": os.getpid(),
              "kernel_job_id": None, "commit_id": None, "revision": None, "status": "refused",
              "reused": False, "calls": 0, "tokens": 0, "call_duration_ms": 0,
              "verifier_invalid_retries": 0, "projections": 0, "pending_projections": None,
              "audit_ok": None, "audit": None, "text_hash": None, "prose_path": None,
              "kernel_ms": 0, "error": None, "result_verified": False}
    lock = None
    try:
        world_dir.mkdir(parents=True, exist_ok=True)
        lock = _hold_arm_lock(world_dir / LOCK_NAME)
        record.update(_execute_arm(spec, world_dir))
    except RuntimeFault as exc:
        code = str(exc) or type(exc).__name__
        record.update(status=code, error=code, **_spent_after_failure(spec, world_dir))
    except ValidationError as exc:
        record.update(status="spec_invalid:ValidationError",
                      error=f"spec_invalid:ValidationError: {str(exc)[:TAIL_CHARS]}")
    except Exception as exc:  # noqa: BLE001 —— 单臂失败不外溢
        record.update(status=f"internal_failure:{type(exc).__name__}",
                      error=f"{type(exc).__name__}: {str(exc)[:TAIL_CHARS]}")
    finally:
        if lock is not None:
            lock.close()
        record["kernel_ms"] = round((time.monotonic() - started) * 1000)
        _write_json(result_path or (world_dir / RESULT_NAME), record)
    status = str(record["status"])
    if status == "committed":
        return 0
    return 3 if status.startswith(("spec_invalid", "internal_failure")) else 2


# ── 臂启动（父进程侧） ─────────────────────────────────────────────────

def _arm_reclaimed(world_dir: Path, timed_out: bool):
    if not timed_out:
        return None
    return _arm_lock_free(world_dir / LOCK_NAME)


def _launch_arm(spec, timeout_s: float):
    """启动一个臂子进程并把它收敛成一条记录（**永不抛异常**）。"""
    world_dir = Path(spec["world_dir"])
    result_path = world_dir / RESULT_NAME
    record = {"job_id": spec["job_id"], "arm": spec["arm"], "book_id": spec["book_id"],
              "scene_id": spec["scene_id"], "world_dir": str(world_dir),
              "db_path": spec["db_path"], "pid": None, "exit_code": None,
              "timed_out": False, "reclaimed": None, "started_at": None, "ended_at": None,
              "wall_ms": 0, "duration_ms": 0, "kernel_ms": None, "status": "missing",
              "calls": 0, "tokens": 0, "call_duration_ms": 0, "reused": False,
              "result_verified": False, "error": None, "stdout_tail": "", "stderr_tail": ""}
    proc = None
    try:
        world_dir.mkdir(parents=True, exist_ok=True)
        if result_path.exists():
            result_path.unlink()
        spec_path = world_dir / SPEC_NAME
        _write_json(spec_path, {k: v for k, v in spec.items()
                                if k not in ("world_key", "index")})
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        cmd = [sys.executable, "-X", "utf8", str(SCRIPT), "--run-arm", str(spec_path),
               "--result", str(result_path)]
        started = time.time()
        record["started_at"] = started
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace")
        record["pid"] = proc.pid
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
            proc.kill()
            out, err = proc.communicate()
        ended = time.time()
        record["ended_at"] = ended
        record["exit_code"] = proc.returncode
        record["wall_ms"] = record["duration_ms"] = int((ended - started) * 1000)
        record["reclaimed"] = _arm_reclaimed(world_dir, record["timed_out"])
        record["stdout_tail"] = (out or "")[-TAIL_CHARS:]
        record["stderr_tail"] = (err or "")[-TAIL_CHARS:]
        child, reason = _read_arm_result(result_path, spec["job_id"])
        if child is None:
            record["status"] = "timeout" if record["timed_out"] else "missing"
            record["error"] = reason
        elif (record["exit_code"] == 0) != (child.get("status") == "committed"):
            record["status"] = "missing"
            record["error"] = "arm_result_exit_code_disagreement"
        else:
            record.update(child)
    except Exception as exc:  # noqa: BLE001 —— 启动失败也只记进这一臂
        record["status"] = "missing"
        record["error"] = f"launch_failure:{type(exc).__name__}: {str(exc)[:TAIL_CHARS]}"
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
    return record


def _read_arm_result(result_path: Path, job_id: str):
    if not result_path.exists():
        return None, "arm_result_file_absent"
    try:
        child = _read_json(result_path)
    except (OSError, ValueError):
        return None, "arm_result_unreadable"
    if not isinstance(child, Mapping) or child.get("schema") != ARM_SCHEMA:
        return None, "arm_result_schema_invalid"
    if str(child.get("job_id") or "") != str(job_id):
        return None, "arm_result_identity_mismatch"
    return dict(child), None


def _launch_all(specs, workers: int, timeout_s: float):
    """线程池只做「等子进程」这一件事；池宽就是并发上界（构造性约束）。"""
    records = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_launch_arm, spec, timeout_s) for spec in specs]
        for future in futures:
            records.append(future.result())
    return records


# ── 汇总 ───────────────────────────────────────────────────────────────

def _peak_concurrency(arms):
    """扫线求实测峰值并发臂数（同刻结束先于开始，避免把交接算成重叠）。"""
    points = []
    for arm in arms:
        start, end = arm.get("started_at"), arm.get("ended_at")
        if start is None or end is None:
            continue
        points.append((float(start), 1))
        points.append((float(end), -1))
    points.sort(key=lambda item: (item[0], item[1]))
    current = peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


def _parallel_verdict(measured: int, max_workers: int, submitted: int):
    expected = min(max_workers, submitted)
    if expected <= 1:
        ok = measured <= 1
        return ok, f"serial_by_request:measured={measured},expected<=1"
    if measured > max_workers:
        return False, f"concurrency_bound_violated:measured={measured},cap={max_workers}"
    ok = measured >= 2
    return ok, f"measured={measured},expected={expected},floor=2"


def assemble_summary(*, submitted, records, wall_ms, max_workers, timeout_s,
                     workdir, rejected_jobs=None, rejected_at=None):
    """把逐臂记录收敛成一份**覆盖完整**的汇总；缺臂即拒绝出结果。

    少一臂的汇总比崩溃更危险：调用方会把「只跑了一半」读成「全跑完了」。所以这里
    对覆盖率做硬校验——重复记录、没提交过的记录、缺失的臂都直接抛
    `ParallelFault`，宁可不给汇总。
    """
    from app.scene_runtime import RUNTIME_VERSION
    order = [str(job_id) for job_id in submitted]
    index: dict[str, dict] = {}
    for record in records:
        job_id = str(record.get("job_id"))
        if job_id in index:
            raise ParallelFault("duplicate_arm_record:" + job_id)
        index[job_id] = dict(record)
    unknown = sorted(set(index) - set(order))
    if unknown:
        raise ParallelFault("unsubmitted_arm_record:" + ",".join(unknown))
    missing = [job_id for job_id in order if job_id not in index]
    if missing:
        raise ParallelFault("arm_result_missing:" + ",".join(missing))
    arms = [index[job_id] for job_id in order]
    measured = _peak_concurrency(arms)
    parallel_ok, parallel_reason = _parallel_verdict(measured, max_workers, len(arms))
    if rejected_at == "preflight":
        parallel_ok, parallel_reason = False, "rejected_before_launch"
    committed = [a for a in arms if a.get("status") == "committed"]
    complete = bool(arms) and all(a.get("result_verified") is True for a in arms)
    ok = bool(arms) and len(committed) == len(arms)
    reclaim_problems = [a["job_id"] for a in arms
                        if a.get("timed_out") and a.get("reclaimed") is False]
    return {
        "schema": SCHEMA, "runtime_version": RUNTIME_VERSION, "workdir": str(workdir),
        "wall_ms": int(wall_ms), "max_workers": int(max_workers), "timeout_s": float(timeout_s),
        "jobs_submitted": len(arms), "jobs_committed": len(committed),
        "jobs_failed": len(arms) - len(committed),
        "failed_job_ids": [a["job_id"] for a in arms if a.get("status") != "committed"],
        "launched": sum(1 for a in arms if a.get("pid")),
        "complete": complete, "ok": ok, "verdict": "accept" if ok else "reject",
        "calls_total": sum(int(a.get("calls") or 0) for a in arms),
        "tokens_total": sum(int(a.get("tokens") or 0) for a in arms),
        "call_duration_ms_total": sum(int(a.get("call_duration_ms") or 0) for a in arms),
        "parallelism_measured": measured, "parallel_ok": parallel_ok,
        "parallel_ok_reason": parallel_reason,
        "concurrency_bound_ok": measured <= int(max_workers),
        "reclaim_problems": reclaim_problems,
        "rejected_at": rejected_at, "rejected_jobs": rejected_jobs or [],
        "arms": arms, "cost": None, "literary_quality": "not_evaluated",
    }


def require_ok(summary, *, parallel: bool = True):
    """把汇总变成**可执行**的判定：任何一个臂没提交就抛，绝不放过整体成功。"""
    if not summary.get("ok"):
        failed = summary.get("failed_job_ids") or []
        raise ParallelFault("parallel_run_rejected:" + ",".join(map(str, failed)))
    if parallel and not summary.get("parallel_ok"):
        raise ParallelFault("parallelism_not_met:" + str(summary.get("parallel_ok_reason")))
    if summary.get("reclaim_problems"):
        raise ParallelFault("arm_not_reclaimed:" + ",".join(summary["reclaim_problems"]))
    return summary


# ── 入口 ───────────────────────────────────────────────────────────────

def _check_max_workers(value) -> int:
    from app import limits
    if isinstance(value, bool):
        raise ParallelFault("max_workers_not_an_integer")
    try:
        workers = int(value)
    except (TypeError, ValueError):
        raise ParallelFault("max_workers_not_an_integer") from None
    if workers < MIN_CONCURRENCY:
        raise ParallelFault(f"max_workers_below_minimum:{workers}<{MIN_CONCURRENCY}")
    if workers > limits.MAX_CONCURRENCY:
        raise ParallelFault(f"max_workers_above_cap:{workers}>{limits.MAX_CONCURRENCY}")
    return workers


def _rejected_record(spec, reason):
    return {"job_id": spec["job_id"], "arm": spec.get("arm"), "book_id": spec.get("book_id"),
            "scene_id": spec.get("scene_id"), "world_dir": spec.get("world_dir"),
            "db_path": spec.get("db_path"), "pid": None, "exit_code": None,
            "timed_out": False, "reclaimed": None, "started_at": None, "ended_at": None,
            "wall_ms": 0, "duration_ms": 0, "kernel_ms": None, "status": "rejected",
            "calls": 0, "tokens": 0, "call_duration_ms": 0, "reused": False,
            "result_verified": False, "error": reason, "stdout_tail": "", "stderr_tail": ""}


def run_parallel(jobs, *, max_workers, budget, workdir, timeout_s=DEFAULT_TIMEOUT_S) -> dict:
    """并行跑一批场景/臂，返回逐臂汇总。

    * `jobs` —— 每项 = 一条场景/臂任务：`job_id`、`plan`/`knowledge`/`world` 契约载荷、
      可选 `arm`/`scene_id`/`world_dir`/`budget`/`offline`。
    * `max_workers` —— 并发臂数上界；上界只取 `app.limits.MAX_CONCURRENCY` 单一真源，
      越界**响亮报错**（不静默 clamp：并发是费用/限速护栏，意图错位比失败危险）。
    * `budget` —— 运行级默认预算合同（逐臂可覆盖），硬闸由单场景内核执行。
    * `workdir` —— 汇总与各臂世界的根；拒写真库路径。
    * `timeout_s` —— 单臂墙钟上限，超时强杀并回收。

    两级失败口径：**提交级**（重名 job_id、同一世界目录、规格错误）整轮拒跑，一个
    子进程都不启动；**臂级**（预算耗尽、契约故障、超时、崩溃）只记该臂，其它臂照跑。
    拒跑汇总的 `arms` 按去重后的 `job_id` 列出（重名无法逐条表达），逐条问题以
    `rejected_jobs` 为准（每条带 `index` 指出提交清单里的位置）。
    """
    if isinstance(jobs, (str, bytes)) or not isinstance(jobs, (list, tuple)):
        raise ParallelFault("jobs_not_a_sequence")
    if not jobs:
        raise ParallelFault("jobs_empty")
    workers = _check_max_workers(max_workers)
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        raise ParallelFault("timeout_not_a_number") from None
    if not timeout > 0 or timeout != timeout:
        raise ParallelFault("timeout_not_positive")
    budget_doc = _budget_doc(budget)
    root = Path(workdir).expanduser().resolve()
    _refuse_protected(root)
    specs, problems = _preflight(list(jobs), root, budget_doc)
    if problems:
        reason = problems[0]["error"]
        submitted, records, seen = [], [], set()
        for spec in specs:
            if spec["job_id"] in seen:
                continue
            seen.add(spec["job_id"])
            submitted.append(spec["job_id"])
            records.append(_rejected_record(spec, reason))
        summary = assemble_summary(submitted=submitted, records=records,
                                   wall_ms=0, max_workers=workers, timeout_s=timeout,
                                   workdir=root, rejected_jobs=problems,
                                   rejected_at="preflight")
        _write_json(root / SUMMARY_NAME, summary)
        return summary
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    records = _launch_all(specs, workers, timeout)
    wall_ms = round((time.monotonic() - started) * 1000)
    summary = assemble_summary(submitted=[s["job_id"] for s in specs], records=records,
                               wall_ms=wall_ms, max_workers=workers, timeout_s=timeout,
                               workdir=root)
    _write_json(root / SUMMARY_NAME, summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="多进程自动调度：并行跑多场景/多臂。")
    parser.add_argument("--jobs", type=Path, help="作业清单 JSON（列表或 {budget, jobs}）")
    parser.add_argument("--out", type=Path, help="汇总与各臂工作目录的根")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--budget", help="运行级默认预算合同（JSON 文本）")
    parser.add_argument("--run-arm", type=Path, help="内部：子进程跑单个臂")
    parser.add_argument("--result", type=Path, help="内部：子进程结果文件")
    args = parser.parse_args(argv)
    if args.run_arm is not None:
        return run_arm(args.run_arm, args.result)
    if args.jobs is None or args.out is None:
        parser.error("--jobs 与 --out 必须成对给出（--run-arm/--result 是子进程内部入口）")
    doc = _read_json(args.jobs)
    if isinstance(doc, Mapping) and "jobs" in doc:
        jobs, budget = list(doc["jobs"]), doc.get("budget")
    else:
        jobs, budget = list(doc), None
    if args.budget:
        budget = json.loads(args.budget)
    summary = run_parallel(jobs, max_workers=args.max_workers, budget=budget,
                           workdir=args.out, timeout_s=args.timeout_s)
    print(json.dumps({k: summary[k] for k in (
        "ok", "verdict", "complete", "jobs_submitted", "jobs_committed", "jobs_failed",
        "failed_job_ids", "calls_total", "wall_ms", "max_workers", "parallel_ok",
        "parallel_ok_reason", "parallelism_measured", "workdir")},
        ensure_ascii=False, sort_keys=True), flush=True)
    try:
        require_ok(summary, parallel=False)
    except ParallelFault as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        return 1
    return 0 if summary["parallel_ok"] and not summary["reclaim_problems"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ParallelFault as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
        raise SystemExit(2)
