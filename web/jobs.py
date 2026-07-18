"""In-memory job registry for the web dashboard.

One analysis run = one ``Job``. The worker thread pushes progress as a flat,
ordered event log (``Job.emit``); the SSE endpoint replays that log from any
event id, so a page refresh mid-run reconnects and rebuilds full progress
instead of losing it. Everything lives in process memory (no cross-restart
persistence) — several logged-in users can share this dashboard, but only
one analysis runs at a time (see ``JobRegistry``).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# How long a job with zero attached SSE listeners is given to reconnect
# (e.g. a page refresh, a browser tab closed and reopened later, a flaky
# network blip causing EventSource to redial) before it's treated as
# abandoned and cancelled. Long enough that closing the app for a while —
# locking the phone, switching apps, a longer errand — doesn't nuke an
# in-progress analysis; short enough that a genuinely forgotten-about job
# doesn't keep burning LLM API calls unattended indefinitely.
_ABANDON_GRACE_SECONDS = 1800.0

_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})


class JobAlreadyRunning(Exception):
    """Raised by ``JobRegistry.create`` when a job is already in flight."""

    def __init__(self, job: Job):
        super().__init__(f"job {job.id} is already running")
        self.job = job


@dataclass
class Job:
    id: str
    ticker: str
    trade_date: str
    status: str = "running"  # running -> done | error | cancelled
    created_at: float = field(default_factory=time.monotonic)
    result: dict[str, Any] | None = None
    error: str | None = None
    config: dict[str, Any] | None = None  # this run's config; reused for the AI-polish pass
    started_by: str | None = None  # username that submitted this job; for the admin activity log
    polished_report: str | None = None  # cached AI-polished report (see routes.py's /polish)

    def __post_init__(self) -> None:
        self.cancel_requested = threading.Event()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._events: list[dict[str, Any]] = []
        self._next_id = 1
        self._listener_count = 0
        self._abandon_timer: threading.Timer | None = None
        self._polish_lock = threading.Lock()

    def get_or_create_polished_report(self, compute_fn) -> str:
        """Single-flight the (slow, LLM-backed) polish pass.

        A dedicated lock rather than ``self._lock``: this holds for the
        entire LLM call, and ``self._lock`` also guards fast, frequent
        operations (``emit``, ``snapshot``) that shouldn't stall behind it.
        Double-checked so a second concurrent request (e.g. an impatient
        double-click) waits for and reuses the first call's result instead
        of paying for the LLM call twice.
        """
        if self.polished_report is not None:
            return self.polished_report
        with self._polish_lock:
            if self.polished_report is None:
                self.polished_report = compute_fn()
            return self.polished_report

    def is_finished(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def emit(self, type_: str, **fields: Any) -> None:
        with self._cond:
            event = {"id": self._next_id, "type": type_, **fields}
            self._next_id += 1
            self._events.append(event)
            self._cond.notify_all()

    def finish(self, status: str, *, result: dict | None = None, error: str | None = None) -> None:
        with self._cond:
            self.status = status
            self.result = result
            self.error = error
            self._cond.notify_all()

    def wait_for_events_after(self, after_id: int, timeout: float) -> list[dict[str, Any]]:
        """Block up to ``timeout`` seconds for events past ``after_id``.

        Returns immediately (empty list) once the job is finished and every
        event has already been delivered — the caller's loop treats that as
        "stream over".
        """
        with self._cond:
            pending = [e for e in self._events if e["id"] > after_id]
            if pending or self.is_finished():
                return pending
            self._cond.wait(timeout=timeout)
            return [e for e in self._events if e["id"] > after_id]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "ticker": self.ticker,
                "trade_date": self.trade_date,
                "status": self.status,
                "result": self.result,
                "error": self.error,
            }

    # -- listener bookkeeping: cancel a run abandoned by every client -------

    def listener_connected(self) -> None:
        with self._lock:
            self._listener_count += 1
            if self._abandon_timer is not None:
                self._abandon_timer.cancel()
                self._abandon_timer = None

    def listener_disconnected(self) -> None:
        with self._lock:
            self._listener_count = max(0, self._listener_count - 1)
            if self._listener_count == 0 and not self.is_finished():
                timer = threading.Timer(_ABANDON_GRACE_SECONDS, self._cancel_if_still_abandoned)
                timer.daemon = True
                self._abandon_timer = timer
                timer.start()

    def _cancel_if_still_abandoned(self) -> None:
        with self._lock:
            if self._listener_count == 0 and not self.is_finished():
                self.cancel_requested.set()


class JobRegistry:
    """Tracks all jobs for the process lifetime and enforces "one running job"."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._current_id: str | None = None

    def create(self, ticker: str, trade_date: str) -> Job:
        with self._lock:
            current = self._jobs.get(self._current_id) if self._current_id else None
            if current is not None and not current.is_finished():
                raise JobAlreadyRunning(current)
            job = Job(id=str(uuid.uuid4()), ticker=ticker, trade_date=trade_date)
            self._jobs[job.id] = job
            self._current_id = job.id
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def current(self) -> Job | None:
        with self._lock:
            if self._current_id is None:
                return None
            return self._jobs.get(self._current_id)

    def running_job(self) -> Job | None:
        """Return the currently running job, or None."""
        job = self.current()
        if job and not job.is_finished():
            return job
        return None
