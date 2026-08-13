"""In-memory registry for long-running web jobs (batch evaluation).

Follows the live-runtime pattern (``routes/live.py``): a background task is
spawned with ``asyncio.create_task`` and the frontend polls a GET endpoint
until the job reaches a terminal state.  Jobs live only in this process — a
single uvicorn worker, matching how ``state.py`` singletons are held.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

MAX_JOBS = 10
MAX_EVENTS = 500


@dataclass
class BatchJob:
    job_id: str
    status: str = "running"  # running | done | error
    events: list[dict] = field(default_factory=list)  # {ts, kind, message}
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    # Monotonic download totals. Kept separate from ``events`` because the
    # event ring is trimmed at MAX_EVENTS — the panel's cumulative "已下载"
    # counter must not shrink when old fetch_done events fall off the window.
    download: dict = field(default_factory=lambda: {
        "fetches": 0,
        "rows": 0,
        "bytes": 0,
        "elapsed": 0.0,
    })


_jobs: dict[str, BatchJob] = {}


def create_batch_job() -> BatchJob:
    """Create a running job, evicting oldest finished jobs past MAX_JOBS."""
    _evict()
    job = BatchJob(job_id=uuid.uuid4().hex[:12])
    _jobs[job.job_id] = job
    return job


def get_batch_job(job_id: str) -> BatchJob | None:
    return _jobs.get(job_id)


def record_job_event(job: BatchJob, kind: str, message: str, payload: dict | None = None) -> None:
    """Append one progress event, keeping structured fields for the frontend.

    ``payload`` carries the raw pipeline event (rows/elapsed/bytes/...) so the
    panel can compute download speed instead of parsing the human message.
    """
    ev: dict = {"ts": time.time(), "kind": kind, "message": message}
    if payload:
        ev.update({k: v for k, v in payload.items() if k != "kind"})
    job.events.append(ev)
    if len(job.events) > MAX_EVENTS:
        del job.events[: len(job.events) - MAX_EVENTS]
    # Accumulate running download totals so the panel can show a monotonic
    # "已下载" even after the event ring starts evicting old fetch_done rows.
    if kind == "fetch_done" and payload:
        d = job.download
        d["fetches"] += 1
        d["rows"] += int(payload.get("rows", 0) or 0)
        d["bytes"] += int(payload.get("bytes", 0) or 0)
        d["elapsed"] += float(payload.get("elapsed", 0) or 0.0)


def _evict() -> None:
    if len(_jobs) < MAX_JOBS:
        return
    finished = [j for j in _jobs.values() if j.status != "running"]
    if finished:
        oldest = min(finished, key=lambda j: j.created_at)
        _jobs.pop(oldest.job_id, None)
