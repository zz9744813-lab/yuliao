"""极简 Job 队列：都在 DB 里，支持 pending/running/completed/failed + 重试 + 退避。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import session
from .models import Job


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def enqueue(s: Session, kind: str, payload: dict, *, max_attempts: int = 3) -> Job:
    j = Job(kind=kind, payload=payload, max_attempts=max_attempts)
    s.add(j)
    s.flush()
    return j


def _claim_one(s: Session, exp_id: str | None = None) -> Job | None:
    now = _now()
    q = (
        s.query(Job)
        .filter(Job.status.in_(["pending", "retry"]))
        .filter((Job.run_after.is_(None)) | (Job.run_after <= now))
    )
    if exp_id is not None:
        # 旧实现按 created_at 全局领单：别的实验的 stage 会被本实验的 pump 领走、
        # handler 因 experiment_id 不匹配直接 return，pump 却把它标成 completed——
        # 即"帮别人把活标干了"。
        q = q.filter(Job.payload["experiment_id"].as_string() == exp_id)
    j = q.order_by(Job.created_at).first()
    if not j:
        return None
    j.status = "running"
    j.attempts = (j.attempts or 0) + 1
    s.commit()
    return j


def complete(s: Session, job_id: str) -> None:
    j = s.get(Job, job_id)
    if j:
        j.status = "completed"
        s.commit()


def fail(s: Session, job_id: str, err: str) -> None:
    j = s.get(Job, job_id)
    if not j:
        return
    if j.attempts >= j.max_attempts:
        j.status = "failed"
        j.last_error = err[:500]
    else:
        j.status = "retry"
        # 退避：5s、30s、120s
        backoff = [5, 30, 120][min(j.attempts - 1, 2)]
        j.run_after = (datetime.utcnow() + timedelta(seconds=backoff)).isoformat(timespec="seconds") + "Z"
        j.last_error = err[:500]
    s.commit()


def pump(handler, *, limit: int = 10_000, idle_sleep: float = 0.5,
         experiment_id: str | None = None) -> dict:
    """worker 主循环：claim → handler(job) → complete/fail。返回统计。"""
    done = failed = 0
    for _ in range(limit):
        with session() as s:
            job = _claim_one(s, experiment_id)
        if not job:
            break
        try:
            handler(job)
        except Exception as e:  # noqa: BLE001
            with session() as s:
                fail(s, job.id, f"{type(e).__name__}: {e}")
            failed += 1
        else:
            with session() as s:
                complete(s, job.id)
            done += 1
        if idle_sleep:
            time.sleep(0)  # 目前串行 pump，不睡；保留接口
    return {"done": done, "failed": failed}
