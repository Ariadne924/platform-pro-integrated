"""Process-local ML job registry for the first research vertical slice."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_ML_JOBS = 20
TERMINAL_STATUSES = {"done", "error", "cancelled"}


@dataclass
class MLJob:
    job_id: str
    signature: str
    request: dict[str, Any]
    status: str = "queued"
    progress: float = 0.0
    stage: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    experiment_id: str | None = None
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


_ml_jobs: dict[str, MLJob] = {}


def job_signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_ml_job(payload: dict[str, Any]) -> tuple[MLJob, bool]:
    """Create a job or reuse an identical active/completed research result."""
    signature = job_signature(payload)
    for job in _ml_jobs.values():
        if job.signature == signature and job.status in {"queued", "running", "done"}:
            return job, False
    _evict_ml_jobs()
    job = MLJob(
        job_id=uuid.uuid4().hex[:12],
        signature=signature,
        request=payload,
    )
    _ml_jobs[job.job_id] = job
    record_ml_event(job, "queued", "机器学习研究任务已排队", progress=0.0)
    return job, True


def get_ml_job(job_id: str) -> MLJob | None:
    return _ml_jobs.get(job_id)


def record_ml_event(
    job: MLJob,
    kind: str,
    message: str,
    *,
    progress: float | None = None,
) -> None:
    job.updated_at = time.time()
    if progress is not None:
        job.progress = float(min(1.0, max(0.0, progress)))
    job.events.append(
        {"ts": job.updated_at, "kind": kind, "message": message, "progress": job.progress}
    )
    if len(job.events) > 200:
        del job.events[:-200]


def request_ml_job_cancel(job: MLJob) -> bool:
    if job.status in TERMINAL_STATUSES:
        return False
    job.cancel_requested = True
    job.stage = "cancelling"
    record_ml_event(job, "cancelling", "已请求取消；当前安全阶段结束后停止")
    return True


def ml_job_snapshot(job: MLJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "signature": job.signature,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "events": list(job.events),
        "result": job.result,
        "error": job.error,
        "experiment_id": job.experiment_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _evict_ml_jobs() -> None:
    if len(_ml_jobs) < MAX_ML_JOBS:
        return
    terminal = [job for job in _ml_jobs.values() if job.status in TERMINAL_STATUSES]
    if terminal:
        oldest = min(terminal, key=lambda job: job.updated_at)
        _ml_jobs.pop(oldest.job_id, None)
