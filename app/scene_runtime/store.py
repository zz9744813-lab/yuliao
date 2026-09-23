"""Single-host SQLite pilot: atomic canon commits, durable attempts and outbox.

Does not open or migrate the existing LG research database. No external publication.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from . import RUNTIME_VERSION
from .contracts import (Budget, Issue, KnowledgePackage, Review, RuntimeFault, ScenePlan,
                        World, canonical, digest, validate_plan, validate_review)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "runtime_meta" not in tables:
                raise RuntimeFault("refuse_non_runtime_database")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_meta(version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS branches(
                  book TEXT, branch TEXT, kind TEXT NOT NULL, revision INTEGER NOT NULL,
                  initial_world TEXT NOT NULL, world TEXT NOT NULL,
                  PRIMARY KEY(book,branch));
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, book TEXT, branch TEXT, scene TEXT,
                  idem TEXT, request_hash TEXT NOT NULL, request TEXT NOT NULL,
                  context TEXT NOT NULL, status TEXT NOT NULL, verified TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(book,branch,idem),
                  FOREIGN KEY(book,branch) REFERENCES branches(book,branch));
                CREATE TABLE IF NOT EXISTS calls(
                  job TEXT, stage TEXT, request_hash TEXT NOT NULL, status TEXT NOT NULL,
                  request TEXT NOT NULL, response TEXT, error TEXT,
                  started_at TEXT NOT NULL, duration_ms INTEGER,
                  PRIMARY KEY(job,stage), FOREIGN KEY(job) REFERENCES jobs(id));
                CREATE TABLE IF NOT EXISTS commits(
                  id TEXT PRIMARY KEY, job TEXT UNIQUE, book TEXT, branch TEXT, scene TEXT,
                  base_revision INTEGER NOT NULL, revision INTEGER NOT NULL,
                  text TEXT NOT NULL, text_hash TEXT NOT NULL, events TEXT NOT NULL,
                  patch TEXT NOT NULL, context_hash TEXT NOT NULL, receipt TEXT NOT NULL,
                  UNIQUE(book,branch,scene), UNIQUE(book,branch,revision),
                  FOREIGN KEY(job) REFERENCES jobs(id));
                CREATE TABLE IF NOT EXISTS outbox(
                  commit_id TEXT PRIMARY KEY, processed INTEGER NOT NULL DEFAULT 0,
                  FOREIGN KEY(commit_id) REFERENCES commits(id));
                CREATE TABLE IF NOT EXISTS memories(
                  commit_id TEXT PRIMARY KEY, book TEXT, branch TEXT, revision INTEGER,
                  payload TEXT NOT NULL, FOREIGN KEY(commit_id) REFERENCES commits(id));
                CREATE TABLE IF NOT EXISTS confirmed_issues(
                  job TEXT, text_hash TEXT, issue_hash TEXT, payload TEXT NOT NULL,
                  created_at TEXT NOT NULL, PRIMARY KEY(job,text_hash,issue_hash),
                  FOREIGN KEY(job) REFERENCES jobs(id));
                CREATE TABLE IF NOT EXISTS review_decisions(
                  job TEXT, stage TEXT, text_hash TEXT, issue_hash TEXT,
                  payload TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(job,stage,text_hash,issue_hash),
                  FOREIGN KEY(job,stage) REFERENCES calls(job,stage));
            """)
            row = db.execute("SELECT version FROM runtime_meta").fetchone()
            if row and row[0] != RUNTIME_VERSION:
                raise RuntimeFault("unsupported_runtime_schema")
            if not row:
                db.execute("INSERT INTO runtime_meta VALUES(?)", (RUNTIME_VERSION,))

    @contextmanager
    def connection(self, *, transaction=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if transaction:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if transaction:
                db.commit()
        except BaseException:
            if transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def create_world(self, world: World, *, kind="canon"):
        if kind not in {"canon", "simulation"} or world.revision != 0:
            raise RuntimeFault("invalid_initial_world")
        if (world.branch_id == "main") != (kind == "canon"):
            raise RuntimeFault("branch_kind_conflict")
        with self.connection(transaction=True) as db:
            old = db.execute("SELECT initial_world,kind FROM branches WHERE book=? AND branch=?",
                             (world.book_id, world.branch_id)).fetchone()
            if old:
                if old[0] != canonical(world) or old[1] != kind:
                    raise RuntimeFault("world_initialization_conflict")
                return
            db.execute("INSERT INTO branches VALUES(?,?,?,?,?,?)",
                       (world.book_id, world.branch_id, kind, 0, canonical(world), canonical(world)))

    def snapshot(self, book, branch="main") -> World:
        with self.connection() as db:
            row = db.execute("SELECT world FROM branches WHERE book=? AND branch=?", (book, branch)).fetchone()
        if row is None:
            raise RuntimeFault("world_not_found")
        return World.model_validate(json.loads(row[0]))

    def job(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeFault("job_not_found")
        return dict(row)

    def prepare(self, plan: ScenePlan, knowledge: KnowledgePackage, budget: Budget, models: dict):
        request = {"plan": plan.model_dump(), "knowledge": knowledge.model_dump(),
                   "budget": budget.model_dump(), "models": models, "runtime": RUNTIME_VERSION}
        request_hash = digest(request)
        job_id = "scene-" + digest([plan.book_id, plan.branch_id, plan.idempotency_key])[:24]
        with self.connection(transaction=True) as db:
            old = db.execute("SELECT request_hash FROM jobs WHERE id=?", (job_id,)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise RuntimeFault("idempotency_input_conflict")
                return job_id
            row = db.execute("SELECT kind,world FROM branches WHERE book=? AND branch=?",
                             (plan.book_id, plan.branch_id)).fetchone()
            if row is None:
                raise RuntimeFault("world_not_found")
            if row[0] != "canon":
                raise RuntimeFault("simulation_cannot_commit")
            world = World.model_validate(json.loads(row[1]))
            validate_plan(plan, world, knowledge)
            # Knowledge is frozen before compilation. No secret world values in Writer view.
            visible = {k: f.model_dump() for k, f in world.facts.items() if plan.pov in f.visible_to}
            previous = db.execute("SELECT scene,text,text_hash FROM commits WHERE book=? AND branch=? "
                                  "ORDER BY revision DESC LIMIT 2", (plan.book_id, plan.branch_id)).fetchall()
            # Prose may have another POV. Only include prior text for the same POV.
            recent = []
            for item in reversed(previous):
                prior = db.execute("SELECT request FROM jobs WHERE id=(SELECT job FROM commits "
                                   "WHERE book=? AND branch=? AND scene=?)",
                                   (plan.book_id, plan.branch_id, item[0])).fetchone()
                if json.loads(prior[0])["plan"]["pov"] == plan.pov:
                    recent.append(dict(item))
            context = {"runtime": RUNTIME_VERSION, "verifier_context_version": 2, "plan": plan.model_dump(),
                       "knowledge": knowledge.model_dump(), "world_revision": world.revision,
                       "characters": world.characters, "facts": visible, "rules": world.rules,
                       "recent_committed_scenes": recent}
            if len(canonical(context)) > budget.max_input_chars:
                raise RuntimeFault("context_budget_exceeded")
            stamp = utcnow()
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, plan.book_id, plan.branch_id, plan.scene_id, plan.idempotency_key,
                        request_hash, canonical(request), canonical(context), "prepared", None, stamp, stamp))
        return job_id

    def reserve_call(self, job_id, stage, payload):
        """Atomic reservation. Any in-flight/unknown request blocks automatic redispatch."""
        with self.connection(transaction=True) as db:
            old = db.execute("SELECT * FROM calls WHERE job=? AND stage=?", (job_id, stage)).fetchone()
            if old:
                if old["request_hash"] != digest(payload):
                    raise RuntimeFault("call_input_conflict")
                if old["status"] == "succeeded":
                    return json.loads(old["response"])
                raise RuntimeFault("call_requires_reconciliation:" + old["status"])
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] not in {"prepared", "running"}:
                raise RuntimeFault("job_not_callable")
            state = db.execute("SELECT revision FROM branches WHERE book=? AND branch=?",
                               (job["book"], job["branch"])).fetchone()
            request = json.loads(job["request"])
            if state[0] != request["plan"]["expected_revision"]:
                raise RuntimeFault("world_revision_conflict")
            if db.execute("SELECT 1 FROM calls WHERE job=? AND status IN ('dispatched','unknown')", (job_id,)).fetchone():
                raise RuntimeFault("call_requires_reconciliation:unknown")
            count, duration = db.execute("SELECT COUNT(*),COALESCE(SUM(duration_ms),0) FROM calls WHERE job=?", (job_id,)).fetchone()
            budget = Budget.model_validate(request["budget"])
            if count >= budget.max_calls or duration >= budget.max_elapsed_seconds * 1000:
                raise RuntimeFault("call_budget_exhausted")
            if len(payload["system"]) + len(canonical(payload["input"])) > budget.max_input_chars:
                raise RuntimeFault("context_budget_exceeded")
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?)",
                       (job_id, stage, digest(payload), "dispatched", canonical(payload), None, None, utcnow(), None))
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (utcnow(), job_id))
        return None

    def finish_call(self, job, stage, *, response=None, error=None, unknown=False, duration_ms=0):
        status = "unknown" if unknown else "failed" if error else "succeeded"
        with self.connection(transaction=True) as db:
            changed = db.execute("UPDATE calls SET response=?,error=?,status=?,duration_ms=? "
                                 "WHERE job=? AND stage=? AND status='dispatched'",
                                 (canonical(response) if response is not None else None, error, status,
                                  duration_ms, job, stage)).rowcount
            if changed != 1:
                raise RuntimeFault("call_completion_conflict")

    def usage(self, job):
        with self.connection() as db:
            rows = db.execute("SELECT stage,status,response,duration_ms,error FROM calls WHERE job=? ORDER BY rowid", (job,)).fetchall()
        results = [dict(r) for r in rows]
        for r in results:
            if r["response"]:
                reply = json.loads(r.pop("response"))
                r.update({k: reply.get(k) for k in ("requested_model", "actual_model", "tokens_in", "tokens_out", "finish_reason")})
        # 2026-09-23 主控取证件：tokens 聚合缺位=真跑收据恒 0（K5-A 成本模型
        # 与 §6 止损命令都指它）。只聚合**成功调用**的网关实账 tokens；失败
        # 调用的上游计费本侧不可见（网关账 llm_calls 是交叉核对侧）。
        # verifier_invalid_retries=以 stage+'.retry' 落表的重试数（收据区分
        # verifier_invalid_retry 与真 hard issue 的依据）。
        tokens = sum((r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
                     for r in results)
        retries = sum(1 for r in results if str(r["stage"]).endswith(".retry"))
        return {"calls": len(rows), "duration_ms": sum(r["duration_ms"] or 0 for r in rows),
                "tokens": tokens, "verifier_invalid_retries": retries,
                "cost": None, "attempts": results}

    def add_confirmed_issue(self, job_id, text: str, issue: Issue):
        """Local operator feedback; never authorizes a different plan or canon."""
        if issue.quote not in text:
            raise RuntimeFault("operator_evidence_not_in_text")
        with self.connection(transaction=True) as db:
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job[0] == "committed":
                raise RuntimeFault("cannot_annotate_committed_or_missing_job")
            rows = db.execute("SELECT response FROM calls WHERE job=? AND stage LIKE 'writer.%' AND status='succeeded'", (job_id,)).fetchall()
            from .pipeline import parse_result
            from .contracts import Draft
            if not any(parse_result(json.loads(r[0])["text"], Draft).text == text for r in rows):
                raise RuntimeFault("operator_issue_unknown_artifact")
            db.execute("INSERT OR IGNORE INTO confirmed_issues VALUES(?,?,?,?,?)",
                       (job_id, digest(text), digest(issue), canonical(issue), utcnow()))
            # An already verified draft with a newly confirmed issue needs rechecking.
            db.execute("UPDATE jobs SET status='running',verified=NULL,updated_at=? WHERE id=?", (utcnow(), job_id))

    def confirmed_issues(self, job_id, text_hash):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM confirmed_issues WHERE job=? AND text_hash=? ORDER BY issue_hash", (job_id, text_hash)).fetchall()
        return [Issue.model_validate(json.loads(r[0])) for r in rows]

    def dismiss_review_issue(self, job_id, stage, issue: Issue, *, reviewer, reason,
                             evidence_commit_id, evidence_quote):
        """Trusted local operator adjudication, never a model-call capability.

        Binds one recorded model issue to one draft and a literal prior canon
        citation. Does not edit prose, plans, patches, budgets or original replies.
        The operator must establish semantic relevance; substring checks cannot.
        """
        if not all(isinstance(v, str) and v.strip() for v in
                   (reviewer, reason, evidence_commit_id, evidence_quote)):
            raise RuntimeFault("review_decision_requires_evidence")
        with self.connection(transaction=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] == "committed":
                raise RuntimeFault("cannot_annotate_committed_or_missing_job")
            call = db.execute("SELECT * FROM calls WHERE job=? AND stage=?", (job_id, stage)).fetchone()
            if not stage.startswith("verifier.") or call is None or call["status"] != "succeeded":
                raise RuntimeFault("review_decision_unknown_review")
            from .pipeline import align_quotes, parse_result
            text = json.loads(call["request"])["input"]["text"]
            review = align_quotes(text, parse_result(json.loads(call["response"])["text"], Review))
            if not any(digest(i) == digest(issue) for i in review.issues):
                raise RuntimeFault("review_decision_unknown_issue")
            source = db.execute("SELECT * FROM commits WHERE id=?", (evidence_commit_id,)).fetchone()
            plan = json.loads(job["request"])["plan"]
            if (source is None or (source["book"], source["branch"]) != (job["book"], job["branch"])
                    or source["revision"] > plan["expected_revision"]
                    or evidence_quote not in source["text"]):
                raise RuntimeFault("review_decision_invalid_canon_evidence")
            payload = {"decision": "false_positive", "reviewer": reviewer, "reason": reason,
                       "stage": stage, "text_hash": digest(text), "issue_hash": digest(issue),
                       "issue": issue.model_dump(), "evidence_commit_id": source["id"],
                       "evidence_text_hash": source["text_hash"], "evidence_quote": evidence_quote}
            previous = db.execute("SELECT payload FROM review_decisions WHERE job=? AND stage=? AND text_hash=? AND issue_hash=?",
                                  (job_id, stage, digest(text), digest(issue))).fetchone()
            if previous and previous[0] != canonical(payload):
                raise RuntimeFault("review_decision_conflict")
            db.execute("INSERT OR IGNORE INTO review_decisions VALUES(?,?,?,?,?,?)",
                       (job_id, stage, digest(text), digest(issue), canonical(payload), utcnow()))

    def review_decisions(self, job_id, text_hash):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM review_decisions WHERE job=? AND text_hash=? ORDER BY stage,issue_hash",
                              (job_id, text_hash)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def apply_review_decisions(self, job_id, stage, text, review: Review):
        dismissed = {d["issue_hash"] for d in self.review_decisions(job_id, digest(text)) if d["stage"] == stage}
        result = review.model_copy(deep=True)
        result.issues = [i for i in result.issues if digest(i) not in dismissed]
        return result

    def mark_verified(self, job_id, text, review: Review):
        job = self.job(job_id)
        plan = ScenePlan.model_validate(json.loads(job["request"])["plan"])
        errors = validate_review(plan, text, review)
        if errors:
            raise RuntimeFault("verification_failed:" + ",".join(errors))
        if any(i.kind == "hard" for i in self.confirmed_issues(job_id, digest(text))):
            raise RuntimeFault("unresolved_operator_issue")
        verified = {"text": text, "text_hash": digest(text), "review": review.model_dump(),
                    "review_decisions": self.review_decisions(job_id, digest(text)),
                    "context_hash": digest(json.loads(job["context"]))}
        with self.connection(transaction=True) as db:
            self._check_final_budget(db, job_id)
            count = db.execute("UPDATE jobs SET status='verified',verified=?,updated_at=? "
                               "WHERE id=? AND status IN ('prepared','running')",
                               (canonical(verified), utcnow(), job_id)).rowcount
            if count != 1:
                raise RuntimeFault("verification_state_conflict")

    def _check_final_budget(self, db, job_id):
        request = json.loads(db.execute("SELECT request FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
        budget = Budget.model_validate(request["budget"])
        rows = db.execute("SELECT status,duration_ms,response FROM calls WHERE job=?", (job_id,)).fetchall()
        # 2026-09-23 主控取证件修正：终闸挡**未解决**调用（dispatched=在飞/
        # unknown=结果未知）；status='failed'（错误已落账——如 verifier 无效
        # 判定被 stage+'.retry' 补上）是已解决态，不挡验证。行数预算仍算
        # failed 行（上游确实烧了 token，不静默放宽）。
        if any(r["status"] in ("dispatched", "unknown") for r in rows):
            raise RuntimeFault("unresolved_call_at_verification")
        if len(rows) > budget.max_calls or sum(r["duration_ms"] or 0 for r in rows) > budget.max_elapsed_seconds * 1000:
            raise RuntimeFault("completed_call_exceeded_budget")
        for row in rows:
            if row["status"] != "succeeded":
                continue                    # failed 行无 response 可核
            reported = json.loads(row["response"]).get("tokens_out")
            if reported is not None and reported > budget.max_output_tokens:
                raise RuntimeFault("provider_exceeded_output_limit")

    def receipt(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT receipt FROM commits WHERE job=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self, job_id, *, fault=None):
        """The only canon write path. `fault` supports transaction failure injection."""
        with self.connection(transaction=True) as db:
            old = db.execute("SELECT receipt FROM commits WHERE job=?", (job_id,)).fetchone()
            if old:
                return json.loads(old[0])
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "verified":
                raise RuntimeFault("unverified_scene")
            self._check_final_budget(db, job_id)
            req = json.loads(job["request"])
            plan = ScenePlan.model_validate(req["plan"])
            row = db.execute("SELECT kind,world FROM branches WHERE book=? AND branch=?", (plan.book_id, plan.branch_id)).fetchone()
            if row[0] != "canon":
                raise RuntimeFault("simulation_cannot_commit")
            world = World.model_validate(json.loads(row[1]))
            validate_plan(plan, world, KnowledgePackage.model_validate(req["knowledge"]))
            result = json.loads(job["verified"])
            if db.execute("SELECT 1 FROM confirmed_issues WHERE job=? AND text_hash=? AND json_extract(payload,'$.kind')='hard'",
                          (job_id, result["text_hash"])).fetchone():
                raise RuntimeFault("unresolved_operator_issue")
            if result["text_hash"] != digest(result["text"]) or result["context_hash"] != digest(json.loads(job["context"])):
                raise RuntimeFault("verified_artifact_changed")
            review = Review.model_validate(result["review"])
            if validate_review(plan, result["text"], review):
                raise RuntimeFault("verification_receipt_invalid")
            evidence = {c.fact: c.quote for c in review.changes}
            patch = []
            for event in plan.events:
                for c in event.changes:
                    world.facts[c.fact].value = c.after
                    patch.append({**c.model_dump(), "event_id": event.event_id, "quote": evidence[c.fact]})
            world.revision += 1
            commit_id = "commit-" + job_id.removeprefix("scene-")
            receipt = {"commit_id": commit_id, "job_id": job_id, "book_id": plan.book_id,
                       "branch_id": plan.branch_id, "scene_id": plan.scene_id,
                       "base_revision": plan.expected_revision, "revision": world.revision,
                       "text_hash": result["text_hash"], "context_hash": result["context_hash"],
                       "status": "committed", "created_at": utcnow()}
            db.execute("INSERT INTO commits VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (commit_id, job_id, plan.book_id, plan.branch_id, plan.scene_id,
                        plan.expected_revision, world.revision, result["text"], result["text_hash"],
                        canonical([e.model_dump() for e in review.events]),
                        canonical(patch), result["context_hash"], canonical(receipt)))
            if fault:
                fault("after_scene_insert")
            db.execute("UPDATE branches SET revision=?,world=? WHERE book=? AND branch=? AND revision=?",
                       (world.revision, canonical(world), plan.book_id, plan.branch_id, plan.expected_revision))
            if fault:
                fault("after_world_update")
            db.execute("INSERT INTO outbox(commit_id) VALUES(?)", (commit_id,))
            db.execute("UPDATE jobs SET status='committed',updated_at=? WHERE id=?", (utcnow(), job_id))
        return receipt

    def project(self, *, fault=None):
        with self.connection(transaction=True) as db:
            rows = db.execute("SELECT c.* FROM commits c JOIN outbox o ON o.commit_id=c.id WHERE o.processed=0 ORDER BY c.revision").fetchall()
            for r in rows:
                payload = {"events": json.loads(r["events"]), "changes": json.loads(r["patch"]), "text_hash": r["text_hash"]}
                db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?)",
                           (r["id"], r["book"], r["branch"], r["revision"], canonical(payload)))
                if fault:
                    fault("after_projection_write")
                db.execute("UPDATE outbox SET processed=1 WHERE commit_id=?", (r["id"],))
        return len(rows)

    def _audit_commit_consistency(self, db, r) -> list[str]:
        """A09（审查 20260920-1810）：事件/收据/上下文/投影的逐项交叉核验。

        旧审计只查正文哈希、补丁证据与状态重放——把事件引文换成不存在的
        文字、把收据 text_hash 改成全零，audit 仍 ok=true（审查在三场运行
        库副本上隔离复现）。本检查又被用作代码升级前的正史检查，漏过损坏
        的收据或事件等于给坏账盖章。故逐项校验：
        · 事件：quote 必须在正文里逐字存在（event_quote）；event_id 必须
          出自冻结计划且计划事件全覆盖（event_plan_ref / event_plan_coverage）；
        · 补丁：每条的 event_id 必须出自冻结计划（patch_event_ref）；
        · 收据：与权威提交行逐字段对齐（receipt_fields），text_hash /
          context_hash 与提交行一致（receipt_hash——审查改全零复现）；
        · 上下文：提交行 context_hash 必须仍是该 job 冻结上下文的指纹
          （context_hash）；
        · 投影：outbox 必在（outbox_missing）；已处理则 memories 载荷必须
          与提交行投影一致（projection_payload / projection_missing），
          记忆已写而 outbox 未标是半写态（projection_state）。"""
        out: list[str] = []
        job = db.execute("SELECT * FROM jobs WHERE id=?", (r["job"],)).fetchone()
        if job is None:
            return ["job_missing"]
        try:
            req = json.loads(job["request"])
            plan = ScenePlan.model_validate(req["plan"])
        except Exception:
            return ["plan_unreadable"]
        plan_ids = {e.event_id for e in plan.events}
        events = json.loads(r["events"])
        for ev in events:
            if ev.get("event_id") not in plan_ids:
                out.append("event_plan_ref")
            if (ev.get("quote") or "") not in r["text"]:
                out.append("event_quote")
        if {ev.get("event_id") for ev in events} != plan_ids:
            out.append("event_plan_coverage")
        for c in json.loads(r["patch"]):
            if c.get("event_id") not in plan_ids:
                out.append("patch_event_ref")
        try:
            receipt = json.loads(r["receipt"])
        except Exception:
            return out + ["receipt_unreadable"]
        if (receipt.get("commit_id") != r["id"] or receipt.get("job_id") != r["job"]
                or receipt.get("book_id") != r["book"]
                or receipt.get("branch_id") != r["branch"]
                or receipt.get("scene_id") != r["scene"]
                or receipt.get("base_revision") != r["base_revision"]
                or receipt.get("revision") != r["revision"]
                or receipt.get("status") != "committed"):
            out.append("receipt_fields")
        if receipt.get("text_hash") != r["text_hash"] \
                or receipt.get("context_hash") != r["context_hash"]:
            out.append("receipt_hash")
        try:
            ctx = json.loads(job["context"])
        except Exception:
            return out + ["context_unreadable"]
        if digest(ctx) != r["context_hash"]:
            out.append("context_hash")
        ob = db.execute("SELECT processed FROM outbox WHERE commit_id=?", (r["id"],)).fetchone()
        if ob is None:
            out.append("outbox_missing")
        else:
            m = db.execute("SELECT revision,payload FROM memories WHERE commit_id=?",
                            (r["id"],)).fetchone()
            if ob["processed"] == 1 and m is None:
                out.append("projection_missing")
            elif m is not None:
                if ob["processed"] == 0:
                    out.append("projection_state")
                payload = {"events": json.loads(r["events"]),
                           "changes": json.loads(r["patch"]),
                           "text_hash": r["text_hash"]}
                if canonical(payload) != m["payload"] or m["revision"] != r["revision"]:
                    out.append("projection_payload")
        return out

    def audit(self, book, branch="main"):
        with self.connection(transaction=True) as db:
            b = db.execute("SELECT * FROM branches WHERE book=? AND branch=?", (book, branch)).fetchone()
            if b is None:
                raise RuntimeFault("world_not_found")
            rows = db.execute("SELECT * FROM commits WHERE book=? AND branch=? ORDER BY revision", (book, branch)).fetchall()
            world = World.model_validate(json.loads(b["initial_world"]))
            errors = []
            for r in rows:
                if r["base_revision"] != world.revision or r["revision"] != world.revision+1:
                    errors.append("revision_chain")
                if digest(r["text"]) != r["text_hash"]:
                    errors.append("text_hash")
                for c in json.loads(r["patch"]):
                    if canonical(world.facts[c["fact"]].value) != canonical(c["before"]) or c["quote"] not in r["text"]:
                        errors.append("patch_evidence")
                    world.facts[c["fact"]].value = c["after"]
                world.revision = r["revision"]
                errors.extend(self._audit_commit_consistency(db, r))
            if canonical(world) != b["world"] or world.revision != b["revision"]:
                errors.append("world_replay")
            pending = db.execute("SELECT COUNT(*) FROM outbox o JOIN commits c ON c.id=o.commit_id WHERE c.book=? AND c.branch=? AND o.processed=0", (book, branch)).fetchone()[0]
            return {"book_id": book, "branch_id": branch, "commits": len(rows), "revision": world.revision,
                    "pending_projections": pending, "errors": errors, "ok": not errors}

    def export(self, book, branch="main"):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM commits WHERE book=? AND branch=? ORDER BY revision", (book, branch)).fetchall()
        return [{**json.loads(r["receipt"]), "text": r["text"], "events": json.loads(r["events"]),
                 "patch": json.loads(r["patch"]), "usage": self.usage(r["job"]),
                 "review_decisions": self.review_decisions(r["job"], r["text_hash"])} for r in rows]
