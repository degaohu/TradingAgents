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


@pytest.mark.unit
class TestPasswordHashing:
    """Self-registered accounts store a hashed password (web/registration.py
    passes create_verified_user() an already-hashed value); operator-managed
    accounts (seed, admin panel) stay plaintext. verify() must transparently
    support both formats in the same table."""

    def test_hash_password_round_trips_through_verify(self):
        pw_hash = users.hash_password("correct horse battery staple")
        assert users.create_verified_user("newbie", pw_hash, "newbie@example.com") is True
        assert users.verify("newbie", "correct horse battery staple") is True
        assert users.verify("newbie", "wrong password") is False

    def test_hash_password_produces_self_describing_format(self):
        pw_hash = users.hash_password("hello")
        assert users.is_hashed(pw_hash) is True
        assert pw_hash.startswith("pbkdf2_sha256$")

    def test_legacy_plaintext_rows_are_not_flagged_as_hashed(self):
        assert users.is_hashed(users.get_password("admin")) is False

    def test_plaintext_and_hashed_rows_coexist_and_both_verify(self):
        pw_hash = users.hash_password("s3cur3-passw0rd")
        users.create_verified_user("hasheduser", pw_hash, "hashed@example.com")
        assert users.verify("admin", "pw-admin") is True          # legacy plaintext
        assert users.verify("hasheduser", "s3cur3-passw0rd") is True  # hashed


@pytest.mark.unit
class TestCreateVerifiedUser:
    def test_creates_non_admin_by_default(self):
        pw_hash = users.hash_password("whatever123")
        assert users.create_verified_user("freshuser", pw_hash, "fresh@example.com") is True
        assert users.is_admin("freshuser") is False
        assert users.exists("freshuser") is True

    def test_rejects_duplicate_username(self):
        pw_hash = users.hash_password("whatever123")
        assert users.create_verified_user("dupe", pw_hash, "dupe1@example.com") is True
        assert users.create_verified_user("dupe", pw_hash, "dupe2@example.com") is False

    def test_rejects_missing_fields(self):
        assert users.create_verified_user("", "hash", "a@b.com") is False
        assert users.create_verified_user("user", "", "a@b.com") is False
        assert users.create_verified_user("user", "hash", "") is False


@pytest.mark.unit
class TestEmail:
    def test_email_exists_is_case_insensitive(self):
        pw_hash = users.hash_password("whatever123")
        users.create_verified_user("emailuser", pw_hash, "Someone@Example.com")
        assert users.email_exists("someone@example.com") is True
        assert users.email_exists("SOMEONE@EXAMPLE.COM") is True
        assert users.email_exists("nobody@example.com") is False

    def test_all_users_carries_email_and_defaults_to_none_for_seed_users(self):
        rows = {u["username"]: u for u in users.all_users()}
        assert rows["admin"]["email"] is None
        pw_hash = users.hash_password("whatever123")
        users.create_verified_user("withemail", pw_hash, "with@example.com")
        rows = {u["username"]: u for u in users.all_users()}
        assert rows["withemail"]["email"] == "with@example.com"
