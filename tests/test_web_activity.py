"""Unit tests for web/activity.py: the persistent admin activity log."""

from __future__ import annotations

import pytest

from web import activity

# Isolation of web.activity's SQLite file from the real
# ~/.tradingagents/web_admin.db is handled globally and autouse in
# tests/conftest.py (_isolate_admin_activity_db) — every test here already
# runs against a per-test temp DB.


@pytest.mark.unit
class TestLogAndRecentActivity:
    def test_round_trip_newest_first(self):
        activity.log_activity("user1", "login", ip="1.1.1.1")
        activity.log_activity("user1", "analyze_start", detail="AAPL @ 2026-06-01")
        events = activity.recent_activity(10)
        assert [e["action"] for e in events] == ["analyze_start", "login"]
        assert events[0]["detail"] == "AAPL @ 2026-06-01"
        assert events[1]["ip"] == "1.1.1.1"

    def test_limit_is_respected(self):
        for i in range(5):
            activity.log_activity("user1", "login", detail=str(i))
        assert len(activity.recent_activity(3)) == 3

    def test_never_raises_on_backend_failure(self, monkeypatch):
        def _raise():
            raise OSError("disk full")

        monkeypatch.setattr(activity, "_connect", _raise)
        activity.log_activity("user1", "login")  # must not raise

    def test_persists_across_separate_calls(self):
        # Each call opens its own connection (no shared state) — confirm
        # writes from one call are visible to a later call.
        activity.log_activity("user2", "logout")
        again = activity.recent_activity(5)
        assert len(again) == 1


@pytest.mark.unit
class TestUserActivitySummary:
    def test_reports_last_seen_and_counts_per_user(self):
        activity.log_activity("user1", "login")
        activity.log_activity("user1", "analyze_start", detail="AAPL @ 2026-06-01")
        activity.log_activity("user1", "analyze_start", detail="TLRY @ 2026-06-02")
        activity.log_activity("user2", "login")

        summary = {row["username"]: row for row in activity.user_activity_summary(["user1", "user2", "user3"])}
        assert summary["user1"]["login_count"] == 1
        assert summary["user1"]["analyze_count"] == 2
        assert summary["user1"]["last_seen"] is not None
        assert summary["user2"]["login_count"] == 1
        assert summary["user2"]["analyze_count"] == 0

    def test_user_with_no_activity_gets_zeros_and_none(self):
        activity.log_activity("user1", "login")
        summary = {row["username"]: row for row in activity.user_activity_summary(["user1", "user3"])}
        assert summary["user3"]["last_seen"] is None
        assert summary["user3"]["login_count"] == 0
        assert summary["user3"]["analyze_count"] == 0

    def test_preserves_requested_username_order(self):
        summary = activity.user_activity_summary(["user3", "user1", "user2"])
        assert [row["username"] for row in summary] == ["user3", "user1", "user2"]
