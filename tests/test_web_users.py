"""Unit tests for web/users.py: the persistent user/password store."""

from __future__ import annotations

import pytest

from web import users

# Isolation of web.users' SQLite file is handled globally and autouse in
# tests/conftest.py (_isolate_users_db), which also resets the seed guard.

_SEED = {"admin": "pw-admin", "user2": "pw-2", "user3": "pw-3"}
_ADMINS = {"admin"}


@pytest.fixture(autouse=True)
def _seeded():
    users.ensure_seeded(_SEED, _ADMINS)


@pytest.mark.unit
class TestSeedAndVerify:
    def test_seeded_users_can_be_verified(self):
        assert users.verify("admin", "pw-admin") is True
        assert users.verify("user2", "pw-2") is True

    def test_wrong_password_fails(self):
        assert users.verify("admin", "nope") is False

    def test_unknown_user_fails(self):
        assert users.verify("ghost", "whatever") is False

    def test_seeding_is_idempotent_and_does_not_clobber(self):
        users.set_password("user2", "changed")
        users.ensure_seeded(_SEED, _ADMINS)  # a "redeploy" re-runs seeding
        # Re-seed must NOT reset the changed password back to the seed value.
        assert users.verify("user2", "changed") is True
        assert users.verify("user2", "pw-2") is False


@pytest.mark.unit
class TestAdminFlagAndListing:
    def test_admin_flag_reflects_seed(self):
        assert users.is_admin("admin") is True
        assert users.is_admin("user2") is False

    def test_list_and_admin_usernames(self):
        assert set(users.list_usernames()) == {"admin", "user2", "user3"}
        assert users.admin_usernames() == ["admin"]

    def test_all_users_carries_passwords_and_flags(self):
        rows = {u["username"]: u for u in users.all_users()}
        assert rows["admin"]["password"] == "pw-admin"
        assert rows["admin"]["is_admin"] is True
        assert rows["user2"]["is_admin"] is False


@pytest.mark.unit
class TestSetPassword:
    def test_set_password_updates_verification(self):
        assert users.set_password("user2", "brand-new") is True
        assert users.verify("user2", "brand-new") is True
        assert users.get_password("user2") == "brand-new"

    def test_set_password_on_unknown_user_returns_false(self):
        assert users.set_password("ghost", "x") is False
