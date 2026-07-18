"""Unit tests for web/history.py: the persistent per-user analysis history."""

from __future__ import annotations

import pytest

from web import history

# Isolation of web.history's SQLite file from the real
# ~/.tradingagents/web_history.db is handled globally and autouse in
# tests/conftest.py (_isolate_history_db) — every test here already runs
# against a per-test temp DB.


@pytest.mark.unit
class TestSaveAndListHistory:
    def test_round_trip_newest_first(self):
        history.save_history("user1", "AAPL", "2026-06-01", "Buy", {"decision": "Buy"})
        history.save_history("user1", "TLRY", "2026-06-02", "Hold", {"decision": "Hold"})
        items = history.list_history("user1")
        assert [i["ticker"] for i in items] == ["TLRY", "AAPL"]

    def test_history_is_scoped_per_user(self):
        history.save_history("user1", "AAPL", "2026-06-01", "Buy", {})
        history.save_history("user2", "TSLA", "2026-06-01", "Sell", {})
        assert [i["ticker"] for i in history.list_history("user1")] == ["AAPL"]
        assert [i["ticker"] for i in history.list_history("user2")] == ["TSLA"]

    def test_rerunning_same_ticker_and_date_replaces_and_bumps_to_most_recent(self):
        history.save_history("user1", "AAPL", "2026-06-01", "Buy", {"decision": "Buy"})
        history.save_history("user1", "TLRY", "2026-06-02", "Hold", {"decision": "Hold"})
        history.save_history("user1", "AAPL", "2026-06-01", "Sell", {"decision": "Sell"})

        items = history.list_history("user1")
        assert [i["ticker"] for i in items] == ["AAPL", "TLRY"]  # AAPL bumped to top
        assert items[0]["decision"] == "Sell"  # updated, not duplicated

    def test_limit_is_respected(self):
        for i in range(5):
            history.save_history("user1", f"T{i}", "2026-06-01", "Hold", {})
        assert len(history.list_history("user1", limit=3)) == 3

    def test_never_raises_on_backend_failure(self, monkeypatch):
        def _raise():
            raise OSError("disk full")

        monkeypatch.setattr(history, "_connect", _raise)
        history.save_history("user1", "AAPL", "2026-06-01", "Buy", {})  # must not raise


@pytest.mark.unit
class TestGetHistoryResult:
    def test_returns_the_full_stored_payload(self):
        payload = {"decision": "Buy", "market_report": "## Market\nBullish."}
        history.save_history("user1", "AAPL", "2026-06-01", "Buy", payload)
        assert history.get_history_result("user1", "AAPL", "2026-06-01") == payload

    def test_returns_none_for_unknown_entry(self):
        assert history.get_history_result("user1", "NOPE", "2026-01-01") is None

    def test_scoped_per_user_even_for_the_same_ticker_and_date(self):
        history.save_history("user1", "AAPL", "2026-06-01", "Buy", {"who": "user1"})
        history.save_history("user2", "AAPL", "2026-06-01", "Sell", {"who": "user2"})
        assert history.get_history_result("user1", "AAPL", "2026-06-01") == {"who": "user1"}
        assert history.get_history_result("user2", "AAPL", "2026-06-01") == {"who": "user2"}
