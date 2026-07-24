"""Integration tests for the self-registration + phone-verification flow:
POST /api/register, POST /api/register/verify-code, POST /api/register/resend.

Twilio is never actually called — smsverify.start_verification/
check_verification are monkeypatched with an in-memory fake that mimics
Twilio Verify's behavior (one active code per phone number, wrong code
fails, right code approves and consumes it) closely enough to exercise the
real endpoint logic without a network dependency.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web.routes as routes_module
from web import quota, smsverify, users
from web.jobs import JobRegistry


class _FakeTwilioVerify:
    """In-memory stand-in for Twilio's Verify service: one active code per
    phone number at a time, overwritten by a new start_verification call."""

    def __init__(self):
        self.codes: dict[str, str] = {}
        self.sent_to: list[str] = []
        self._next_code = 100000

    def start(self, phone_e164):
        code = str(self._next_code)
        self._next_code += 1
        self.codes[phone_e164] = code
        self.sent_to.append(phone_e164)

    def check(self, phone_e164, code):
        return self.codes.get(phone_e164) == code


@pytest.fixture()
def fake_twilio(monkeypatch):
    fake = _FakeTwilioVerify()
    monkeypatch.setattr(smsverify, "is_configured", lambda: True)
    monkeypatch.setattr(smsverify, "start_verification", fake.start)
    monkeypatch.setattr(smsverify, "check_verification", fake.check)
    return fake


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(routes_module, "registry", JobRegistry())
    # Rate-limit state is process-global; a dirty bucket from an earlier
    # test would make an unrelated test 429 unpredictably.
    routes_module._rate_limit_hits.clear()
    return TestClient(routes_module.app)


@pytest.mark.unit
class TestRegisterValidation:
    def test_rejects_empty_username(self, client, fake_twilio):
        r = client.post("/api/register", json={"username": "   ", "phone": "4165550001", "password": "longenough1"})
        assert r.status_code == 400

    def test_rejects_username_over_32_chars(self, client, fake_twilio):
        r = client.post("/api/register", json={"username": "a" * 33, "phone": "4165550001", "password": "longenough1"})
        assert r.status_code == 400

    def test_rejects_username_with_slash_or_backslash(self, client, fake_twilio):
        for bad in ["a/b", "a\\b"]:
            r = client.post("/api/register", json={"username": bad, "phone": "4165550001", "password": "longenough1"})
            assert r.status_code == 400, bad

    def test_rejects_username_with_control_characters(self, client, fake_twilio):
        r = client.post("/api/register", json={"username": "a\nb", "phone": "4165550001", "password": "longenough1"})
        assert r.status_code == 400

    def test_accepts_short_and_unicode_usernames(self, client, fake_twilio):
        for i, name in enumerate(["ab", "李雷", "user.name", "user name", "a"]):
            r = client.post("/api/register", json={"username": name, "phone": f"416555002{i}", "password": "longenough1"})
            assert r.status_code == 200, name

    def test_rejects_invalid_phone(self, client, fake_twilio):
        r = client.post("/api/register", json={"username": "validname", "phone": "123", "password": "longenough1"})
        assert r.status_code == 400

    def test_rejects_short_password(self, client, fake_twilio):
        r = client.post("/api/register", json={"username": "validname", "phone": "4165550001", "password": "short"})
        assert r.status_code == 400

    def test_rejects_taken_username(self, client, fake_twilio):
        r = client.post("/api/register", json={
            "username": "admin", "phone": "4165550002", "password": "longenough1",
        })
        assert r.status_code == 409

    def test_rejects_registered_phone(self, client, fake_twilio):
        pw_hash = users.hash_password("whatever123")
        users.create_verified_user("existinguser", pw_hash, "+14165550003")
        r = client.post("/api/register", json={"username": "brandnew", "phone": "4165550003", "password": "longenough1"})
        assert r.status_code == 409

    def test_501_when_twilio_not_configured(self, client, monkeypatch):
        monkeypatch.setattr(smsverify, "is_configured", lambda: False)
        r = client.post("/api/register", json={"username": "someuser", "phone": "4165550004", "password": "longenough1"})
        assert r.status_code == 501

    def test_accepts_various_phone_input_formats(self, client, fake_twilio):
        for i, raw in enumerate(["(416) 555-0010", "416-555-0011", "14165550012", "+1 416 555 0013"]):
            r = client.post("/api/register", json={"username": f"fmtuser{i}", "phone": raw, "password": "longenough1"})
            assert r.status_code == 200, raw


@pytest.mark.unit
class TestRegisterAndVerifyFlow:
    def test_register_starts_verification_and_does_not_create_the_account_yet(self, client, fake_twilio):
        r = client.post("/api/register", json={"username": "pending1", "phone": "4165551001", "password": "longenough1"})
        assert r.status_code == 200
        assert r.json()["phone"] == "+14165551001"
        assert fake_twilio.sent_to == ["+14165551001"]
        assert users.exists("pending1") is False  # not real until verified

    def test_correct_code_creates_the_account_grants_quota_and_logs_in(self, client, fake_twilio):
        client.post("/api/register", json={"username": "newperson", "phone": "4165552001", "password": "mypassword1"})
        code = fake_twilio.codes["+14165552001"]

        r = client.post("/api/register/verify-code", json={"phone": "4165552001", "code": code})
        assert r.status_code == 200
        assert "ta_session" in r.cookies

        assert users.exists("newperson") is True
        assert users.is_admin("newperson") is False
        assert quota.get_remaining("newperson") == routes_module.NEW_USER_BONUS_QUOTA

        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["username"] == "newperson"

    def test_wrong_code_is_rejected_and_does_not_create_the_account(self, client, fake_twilio):
        client.post("/api/register", json={"username": "wrongcode", "phone": "4165553001", "password": "longenough1"})
        r = client.post("/api/register/verify-code", json={"phone": "4165553001", "code": "000000"})
        assert r.status_code == 400
        assert users.exists("wrongcode") is False

    def test_the_new_account_can_log_in_with_the_chosen_password(self, client, fake_twilio):
        client.post("/api/register", json={"username": "loginlater", "phone": "4165554001", "password": "secretpass1"})
        code = fake_twilio.codes["+14165554001"]
        client.post("/api/register/verify-code", json={"phone": "4165554001", "code": code})

        r = client.post("/api/login", json={"username": "loginlater", "password": "secretpass1"})
        assert r.status_code == 200

    def test_code_is_single_use(self, client, fake_twilio):
        client.post("/api/register", json={"username": "onceonly", "phone": "4165555001", "password": "longenough1"})
        code = fake_twilio.codes["+14165555001"]
        client.post("/api/register/verify-code", json={"phone": "4165555001", "code": code})
        r2 = client.post("/api/register/verify-code", json={"phone": "4165555001", "code": code})
        assert r2.status_code == 400

    def test_reregistering_before_verifying_invalidates_the_first_code(self, client, fake_twilio):
        client.post("/api/register", json={"username": "retryuser", "phone": "4165556001", "password": "firstpassword1"})
        first_code = fake_twilio.codes["+14165556001"]
        client.post("/api/register", json={"username": "retryuser", "phone": "4165556001", "password": "secondpassword1"})
        second_code = fake_twilio.codes["+14165556001"]

        assert first_code != second_code
        r1 = client.post("/api/register/verify-code", json={"phone": "4165556001", "code": first_code})
        assert r1.status_code == 400

        r2 = client.post("/api/register/verify-code", json={"phone": "4165556001", "code": second_code})
        assert r2.status_code == 200
        # The second (most recent) password is the one that ended up active.
        assert client.post("/api/login", json={"username": "retryuser", "password": "secondpassword1"}).status_code == 200


@pytest.mark.unit
class TestResendCode:
    def test_resend_triggers_a_new_twilio_send(self, client, fake_twilio):
        client.post("/api/register", json={"username": "resenduser", "phone": "4165557001", "password": "longenough1"})
        assert len(fake_twilio.sent_to) == 1

        r = client.post("/api/register/resend", json={"phone": "4165557001"})
        assert r.status_code == 200
        assert len(fake_twilio.sent_to) == 2

        # The refreshed code from Twilio's second send still verifies successfully.
        code = fake_twilio.codes["+14165557001"]
        assert client.post("/api/register/verify-code", json={"phone": "4165557001", "code": code}).status_code == 200

    def test_resend_for_unknown_phone_returns_generic_ok_without_calling_twilio(self, client, fake_twilio):
        r = client.post("/api/register/resend", json={"phone": "4165559999"})
        assert r.status_code == 200
        assert fake_twilio.sent_to == []


@pytest.mark.unit
class TestRateLimiting:
    def test_register_endpoint_429s_after_too_many_attempts_from_one_ip(self, client, fake_twilio):
        for i in range(5):
            client.post("/api/register", json={"username": f"rluser{i}", "phone": f"416555800{i}", "password": "longenough1"})
        r = client.post("/api/register", json={"username": "rluserlast", "phone": "4165558999", "password": "longenough1"})
        assert r.status_code == 429


@pytest.mark.unit
class TestPublicRoutesBypassAuth:
    def test_register_page_is_reachable_without_a_session(self, client):
        assert client.get("/register").status_code == 200

    def test_verify_code_endpoint_is_reachable_without_a_session(self, client, fake_twilio):
        r = client.post("/api/register/verify-code", json={"phone": "4165550000", "code": "000000"})
        assert r.status_code in (400, 501)  # reachable at all — not 401
