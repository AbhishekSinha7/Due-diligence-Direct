"""Agent Runtime: asynchronous, durable background execution for the fleet.

A diligence run takes minutes, not milliseconds, so the caller must not have to
sit and wait for it. The runtime accepts a job, returns immediately with a job id,
and executes the graph on a worker thread. Job state is persisted to SQLite, so a
dashboard, a CLI, or an HTTP client can reconnect later and read progress,
per-stage events, the final report, and the trace id that ties it all to telemetry.

Jobs left RUNNING by a process restart are reconciled to INTERRUPTED at startup so
the fleet never reports a stale job as live.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import telemetry

STATE_DIR = Path(os.getenv("FLEET_STATE_DIR", ".fleet"))
JOBS_DB = Path(os.getenv("FLEET_JOBS_DB", str(STATE_DIR / "jobs.db")))
MAX_WORKERS = int(os.getenv("FLEET_RUNTIME_WORKERS", "4"))

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_INTERRUPTED = "INTERRUPTED"

TERMINAL_STATUSES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED}

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_CANCEL_FLAGS: dict[str, threading.Event] = {}
_WRITE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(JOBS_DB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            crn TEXT NOT NULL,
            data_room_path TEXT NOT NULL,
            status TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            trace_id TEXT NOT NULL DEFAULT '',
            events TEXT NOT NULL DEFAULT '[]',
            result TEXT,
            error TEXT
        )
        """
    )
    connection.commit()
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Open, commit, and always close a job-store connection."""

    connection = _connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="fleet-runtime"
            )
    return _EXECUTOR


def reconcile_interrupted_jobs() -> int:
    """Mark jobs left RUNNING by a previous process as INTERRUPTED."""

    with _WRITE_LOCK, _db() as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE status IN (?, ?)",
            (
                STATUS_INTERRUPTED,
                _now(),
                "Process restarted while the job was in flight.",
                STATUS_RUNNING,
                STATUS_QUEUED,
            ),
        )
        return cursor.rowcount


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "crn": row["crn"],
        "data_room_path": row["data_room_path"],
        "status": row["status"],
        "submitted_by": row["submitted_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "trace_id": row["trace_id"],
        "events": json.loads(row["events"] or "[]"),
        "result": json.loads(row["result"]) if row["result"] else None,
        "error": row["error"],
    }


def get_job(job_id: str) -> dict[str, Any] | None:
    with _db() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(limit: int = 25, crn: str | None = None) -> list[dict[str, Any]]:
    with _db() as connection:
        if crn:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE crn = ? ORDER BY created_at DESC LIMIT ?", (crn, limit)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_job(row) for row in rows]


def _update(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with _WRITE_LOCK, _db() as connection:
        connection.execute(
            f"UPDATE jobs SET {assignments} WHERE job_id = ?",
            (*fields.values(), job_id),
        )


def append_event(job_id: str, stage: str, message: str, **attributes: Any) -> dict[str, Any]:
    """Append a progress event so a client can render a live stage timeline."""

    event = {
        "timestamp": _now(),
        "stage": stage,
        "message": message,
        "attributes": attributes,
    }
    with _WRITE_LOCK, _db() as connection:
        row = connection.execute("SELECT events FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        events = json.loads(row["events"] or "[]") if row else []
        events.append(event)
        connection.execute(
            "UPDATE jobs SET events = ? WHERE job_id = ?", (json.dumps(events, default=str), job_id)
        )
    return event


def is_cancelled(job_id: str) -> bool:
    flag = _CANCEL_FLAGS.get(job_id)
    return bool(flag and flag.is_set())


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation. A running job stops at its next stage boundary."""

    job = get_job(job_id)
    if job is None:
        raise KeyError(f"Unknown job {job_id}")
    if job["status"] in TERMINAL_STATUSES:
        return job

    _CANCEL_FLAGS.setdefault(job_id, threading.Event()).set()
    if job["status"] == STATUS_QUEUED:
        _update(job_id, status=STATUS_CANCELLED, finished_at=_now())

    telemetry.audit(
        "runtime.cancel",
        actor=job["submitted_by"],
        resource=f"job://{job_id}",
        decision="cancel",
        severity="WARN",
    )
    return get_job(job_id) or job


class JobCancelled(Exception):
    """Raised inside a worker when cancellation is requested."""


def _run_job(job_id: str, crn: str, data_room_path: str, runner: Callable[..., Any]) -> None:
    with telemetry.agent_span("runtime.job", job_id=job_id, crn=crn) as ids:
        _update(job_id, status=STATUS_RUNNING, started_at=_now(), trace_id=ids["trace_id"])
        telemetry.audit(
            "runtime.start",
            actor="runtime",
            resource=f"job://{job_id}",
            decision="allow",
            attributes={"crn": crn},
        )
        try:
            state = runner(
                crn,
                data_room_path=data_room_path,
                progress=lambda stage, message, **attributes: append_event(
                    job_id, stage, message, **attributes
                ),
                should_cancel=lambda: is_cancelled(job_id),
                job_id=job_id,
            )
            _update(
                job_id,
                status=STATUS_SUCCEEDED,
                finished_at=_now(),
                result=json.dumps(state, default=str),
            )
            telemetry.audit(
                "runtime.finish",
                actor="runtime",
                resource=f"job://{job_id}",
                decision="succeeded",
                attributes={
                    "recommendation": (state.get("red_flag_verdict") or {}).get(
                        "recommendation", "unknown"
                    )
                },
            )
        except JobCancelled:
            _update(job_id, status=STATUS_CANCELLED, finished_at=_now())
            telemetry.audit(
                "runtime.finish",
                actor="runtime",
                resource=f"job://{job_id}",
                decision="cancelled",
                severity="WARN",
            )
        except Exception as exc:
            _update(
                job_id,
                status=STATUS_FAILED,
                finished_at=_now(),
                error=f"{exc.__class__.__name__}: {exc}",
            )
            telemetry.audit(
                "runtime.finish",
                actor="runtime",
                resource=f"job://{job_id}",
                decision="failed",
                severity="ERROR",
                attributes={"error": exc.__class__.__name__},
            )
        finally:
            _CANCEL_FLAGS.pop(job_id, None)


def submit_job(
    crn: str,
    *,
    data_room_path: str = "data_room",
    submitted_by: str = "operator",
    runner: Callable[..., Any] | None = None,
) -> str:
    """Queue a diligence run and return its job id immediately."""

    if runner is None:
        import orchestrator

        runner = orchestrator.run_due_diligence_job

    job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    with _WRITE_LOCK, _db() as connection:
        connection.execute(
            """
            INSERT INTO jobs (job_id, crn, data_room_path, status, submitted_by, created_at, events)
            VALUES (?, ?, ?, ?, ?, ?, '[]')
            """,
            (job_id, crn, data_room_path, STATUS_QUEUED, submitted_by, _now()),
        )

    _CANCEL_FLAGS[job_id] = threading.Event()
    telemetry.audit(
        "runtime.submit",
        actor=submitted_by,
        resource=f"job://{job_id}",
        decision="allow",
        attributes={"crn": crn, "data_room_path": data_room_path},
    )
    _executor().submit(_run_job, job_id, crn, data_room_path, runner)
    return job_id


def wait_for(job_id: str, timeout_seconds: float = 300.0, poll_seconds: float = 0.5) -> dict[str, Any]:
    """Block until a job reaches a terminal state. Used by the CLI and tests."""

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        job = get_job(job_id)
        if job and job["status"] in TERMINAL_STATUSES:
            return job
        time.sleep(poll_seconds)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout_seconds}s")


def fleet_stats() -> dict[str, Any]:
    with _db() as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "workers": MAX_WORKERS,
        "counts": counts,
        "total": sum(counts.values()),
        "in_flight": counts.get(STATUS_RUNNING, 0) + counts.get(STATUS_QUEUED, 0),
    }
