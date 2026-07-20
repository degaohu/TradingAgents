"""Unit tests for web/registration.py: pending email-verification storage."""

from __future__ import annotations

import pytest

from web import registration

# Isolation of web.registration's SQLite file is handled globally and
# autouse in tests/conftest.py (_isolate_registration_db).


@pytest.mark.unit
class TestCreateAndConsume:
    def test_create_returns_a_usable_token(self):
        token = registration.create("alice", "alice@example.com", "hashed-pw")
        pending = registration.consume(token)
        assert pending == {"username": "alice", "email": "alice@example.com", "password_hash": "hashed-pw"}

    def test_consume_is_single_use(self):
        token = registration.create("bob", "bob@example.com", "hashed-pw")
        assert registration.consume(token) is not None
        assert registration.consume(token) is None

    def test_consume_unknown_token_returns_none(self):
        assert registration.consume("not-a-real-token") is None

    def test_expired_token_is_rejected_and_removed(self):
        token = registration.create("carol", "carol@example.com", "hashed-pw", ttl_seconds=-1)
        assert registration.consume(token) is None
        # A second consume confirms the expired row was actually deleted,
        # not just skipped this time.
        assert registration.consume(token) is None

    def test_new_registration_for_same_username_invalidates_old_token(self):
        old_token = registration.create("dave", "dave@example.com", "old-hash")
        new_token = registration.create("dave", "dave-new@example.com", "new-hash")
        assert registration.consume(old_token) is None
        assert registration.consume(new_token) == {
            "username": "dave", "email": "dave-new@example.com", "password_hash": "new-hash",
        }

    def test_new_registration_for_same_email_invalidates_old_token(self):
        old_token = registration.create("erin1", "erin@example.com", "old-hash")
        new_token = registration.create("erin2", "erin@example.com", "new-hash")
        assert registration.consume(old_token) is None
        assert registration.consume(new_token) is not None


@pytest.mark.unit
class TestFindByIdentifier:
    def test_finds_by_username(self):
        registration.create("frank", "frank@example.com", "hash")
        found = registration.find_by_identifier("frank")
        assert found == {"username": "frank", "email": "frank@example.com", "password_hash": "hash"}

    def test_finds_by_email(self):
        registration.create("grace", "grace@example.com", "hash")
        found = registration.find_by_identifier("grace@example.com")
        assert found["username"] == "grace"

    def test_unknown_identifier_returns_none(self):
        assert registration.find_by_identifier("nobody-here") is None

    def test_does_not_consume_the_token(self):
        token = registration.create("heidi", "heidi@example.com", "hash")
        registration.find_by_identifier("heidi")
        # Still there — find_by_identifier is a peek, not a consume.
        assert registration.consume(token) is not None
