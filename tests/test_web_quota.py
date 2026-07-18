"""Unit tests for web/quota.py: the per-user report-generation quota."""

from __future__ import annotations

import pytest

from web import quota

# Isolation of web.quota's SQLite file from the real
# ~/.tradingagents/web_quota.db is handled globally and autouse in
# tests/conftest.py (_isolate_quota_db).


@pytest.mark.unit
class TestQuota:
    def test_unseen_user_starts_at_the_default(self):
        assert quota.get_remaining("newbie") == quota.DEFAULT_QUOTA

    def test_consume_decrements_by_one(self):
        start = quota.get_remaining("user2")
        assert quota.consume("user2") is True
        assert quota.get_remaining("user2") == start - 1

    def test_consume_returns_false_and_stays_zero_when_empty(self):
        quota.set_remaining("user2", 0)
        assert quota.consume("user2") is False
        assert quota.get_remaining("user2") == 0

    def test_add_tops_up(self):
        quota.set_remaining("user2", 1)
        assert quota.add("user2", 5) == 6
        assert quota.get_remaining("user2") == 6

    def test_add_negative_clamps_at_zero(self):
        quota.set_remaining("user2", 2)
        assert quota.add("user2", -10) == 0

    def test_set_remaining_is_absolute_and_clamps(self):
        assert quota.set_remaining("user2", 42) == 42
        assert quota.set_remaining("user2", -3) == 0

    def test_all_remaining_reports_each_user(self):
        quota.set_remaining("user2", 4)
        result = quota.all_remaining(["user2", "user3"])
        assert result["user2"] == 4
        assert result["user3"] == quota.DEFAULT_QUOTA  # untouched → default

    def test_balances_are_independent_per_user(self):
        quota.set_remaining("user2", 1)
        quota.consume("user2")
        assert quota.get_remaining("user2") == 0
        assert quota.get_remaining("user3") == quota.DEFAULT_QUOTA
