"""Unit tests for web/jobs.py: Job event log, listener bookkeeping, JobRegistry."""

from __future__ import annotations

import threading
import time

import pytest

from web.jobs import Job, JobAlreadyRunning, JobRegistry


@pytest.mark.unit
class TestJobEventLog:
    def test_emit_assigns_increasing_ids(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        job.emit("log", message="a")
        job.emit("log", message="b")
        events = job.wait_for_events_after(0, timeout=0.01)
        assert [e["id"] for e in events] == [1, 2]

    def test_wait_for_events_after_only_returns_newer(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        job.emit("log", message="a")
        job.emit("log", message="b")
        events = job.wait_for_events_after(1, timeout=0.01)
        assert [e["id"] for e in events] == [2]

    def test_wait_for_events_after_wakes_on_new_event(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        result = {}

        def waiter():
            result["events"] = job.wait_for_events_after(0, timeout=5.0)

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)  # let the waiter block inside Condition.wait
        job.emit("log", message="hello")
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert [e["message"] for e in result["events"]] == ["hello"]

    def test_finished_job_with_no_new_events_returns_empty_immediately(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        job.emit("log", message="a")
        job.finish("done", result={"x": 1})
        start = time.monotonic()
        events = job.wait_for_events_after(1, timeout=5.0)
        assert time.monotonic() - start < 1.0  # must not block the full timeout
        assert events == []

    def test_snapshot_reflects_result_and_status(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        assert job.snapshot()["status"] == "running"
        job.finish("done", result={"decision": "Buy"})
        snap = job.snapshot()
        assert snap["status"] == "done"
        assert snap["result"] == {"decision": "Buy"}
        assert snap["error"] is None


@pytest.mark.unit
class TestJobAbandonment:
    def test_reconnect_within_grace_period_cancels_the_timer(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        job.listener_connected()
        job.listener_disconnected()
        assert job._abandon_timer is not None
        job.listener_connected()
        assert job._abandon_timer is None
        assert not job.cancel_requested.is_set()

    def test_abandonment_past_grace_period_requests_cancel(self, monkeypatch):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        monkeypatch.setattr("web.jobs._ABANDON_GRACE_SECONDS", 0.05)
        job.listener_connected()
        job.listener_disconnected()
        time.sleep(0.2)
        assert job.cancel_requested.is_set()

    def test_finished_job_never_schedules_abandon_timer(self):
        job = Job(id="1", ticker="NVDA", trade_date="2026-01-15")
        job.finish("done", result={})
        job.listener_connected()
        job.listener_disconnected()
        assert job._abandon_timer is None


@pytest.mark.unit
class TestJobRegistry:
    def test_create_then_running_second_create_raises(self):
        registry = JobRegistry()
        registry.create("NVDA", "2026-01-15")
        with pytest.raises(JobAlreadyRunning):
            registry.create("TSLA", "2026-01-16")

    def test_create_after_finish_succeeds(self):
        registry = JobRegistry()
        first = registry.create("NVDA", "2026-01-15")
        first.finish("done", result={})
        second = registry.create("TSLA", "2026-01-16")
        assert second.id != first.id
        assert registry.current() is second

    def test_get_returns_none_for_unknown_id(self):
        registry = JobRegistry()
        assert registry.get("does-not-exist") is None

    def test_already_running_exception_carries_the_current_job(self):
        registry = JobRegistry()
        first = registry.create("NVDA", "2026-01-15")
        with pytest.raises(JobAlreadyRunning) as excinfo:
            registry.create("TSLA", "2026-01-16")
        assert excinfo.value.job is first
