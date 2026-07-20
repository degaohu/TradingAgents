"""Unit tests for web/registration.py: pending phone-verification storage."""

from __future__ import annotations

import pytest

from web import registration

# Isolation of web.registration's SQLite file is handled globally and
# autouse in tests/conftest.py (_isolate_registration_db).


@pytest.mark.unit
class TestCreateGetConsume:
    def test_create_then_get_returns_the_pending_row(self):
        registration.create("alice", "+14165550001", "hashed-pw")
        pending = registration.get("+14165550001")
        assert pending == {"username": "alice", "phone": "+14165550001", "password_hash": "hashed-pw"}

    def test_get_does_not_consume(self):
        registration.create("alice", "+14165550001", "hashed-pw")
        registration.get("+14165550001")
        assert registration.get("+14165550001") is not None

    def test_consume_is_single_use(self):
        registration.create("bob", "+14165550002", "hashed-pw")
        assert registration.consume("+14165550002") is not None
        assert registration.consume("+14165550002") is None

    def test_get_and_consume_unknown_phone_return_none(self):
        assert registration.get("+14165559999") is None
        assert registration.consume("+14165559999") is None

    def test_expired_registration_is_rejected_and_removed(self):
        registration.create("carol", "+14165550003", "hashed-pw", ttl_seconds=-1)
        assert registration.get("+14165550003") is None
        assert registration.consume("+14165550003") is None

    def test_new_registration_for_same_username_replaces_old_pending_phone(self):
        registration.create("dave", "+14165550004", "old-hash")
        registration.create("dave", "+14165550005", "new-hash")
        assert registration.get("+14165550004") is None
        assert registration.get("+14165550005") == {
            "username": "dave", "phone": "+14165550005", "password_hash": "new-hash",
        }

    def test_new_registration_for_same_phone_replaces_old_pending_username(self):
        registration.create("erin1", "+14165550006", "old-hash")
        registration.create("erin2", "+14165550006", "new-hash")
        assert registration.get("+14165550006") == {
            "username": "erin2", "phone": "+14165550006", "password_hash": "new-hash",
        }
