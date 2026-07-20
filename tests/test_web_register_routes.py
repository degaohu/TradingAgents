"""Integration tests for the self-registration + email-verification flow:
POST /api/register, GET /verify-email, POST /api/register/resend.

SMTP is never actually exercised — mailer.send_email is monkeypatched to
capture what would have been sent instead of hitting a real server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web.routes as routes_module
from web import mailer, quota, registration, users
from web.jobs import JobRegistry


@pytest.fixture()
def sent_emails(monkeypatch):
    """Replaces mailer.send_email with a recorder and reports SMTP as
    configured, so /api/register doesn't 501 in tests."""
    calls = []

    def _fake_send(to, subject, html_body):
        calls.append({"to": to, "subject": subject, "html": html_body})

    monkeypatch.setattr(mailer, "send_email", _fake_send)
    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    return calls


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(routes_module, "registry", JobRegistry())
    # Rate-limit state is process-global; a dirty bucket from an earlier
    # test would make an unrelated test 429 unpredictably.
    routes_module._rate_limit_hits.clear()
    return TestClient(routes_module.app)


def _extract_token(html_body: str) -> str:
    """Pull the token= query param out of the verify-email link embedded
    in the fake-sent email HTML."""
    marker = "token="
    start = html_body.index(marker) + len(marker)
    end = start
    while html_body[end] not in ('"', "'", "<", " ", "\n"):
        end += 1
    return html_body[start:end]


@pytest.mark.unit
class TestRegisterValidation:
    def test_rejects_short_username(self, client, sent_emails):
        r = client.post("/api/register", json={"username": "ab", "email": "a@b.com", "password": "longenough1"})
        assert r.status_code == 400

    def test_rejects_invalid_email(self, client, sent_emails):
        r = client.post("/api/register", json={"username": "validname", "email": "not-an-email", "password": "longenough1"})
        assert r.status_code == 400

    def test_rejects_short_password(self, client, sent_emails):
        r = client.post("/api/register", json={"username": "validname", "email": "a@b.com", "password": "short"})
        assert r.status_code == 400

    def test_rejects_taken_username(self, client, sent_emails):
        r = client.post("/api/register", json={
            "username": "admin", "email": "new@example.com", "password": "longenough1",
        })
        assert r.status_code == 409

    def test_rejects_registered_email(self, client, sent_emails):
        pw_hash = users.hash_password("whatever123")
        users.create_verified_user("existinguser", pw_hash, "taken@example.com")
        r = client.post("/api/register", json={"username": "brandnew", "email": "taken@example.com", "password": "longenough1"})
        assert r.status_code == 409

    def test_501_when_mailer_not_configured(self, client, monkeypatch):
        monkeypatch.setattr(mailer, "is_configured", lambda: False)
        r = client.post("/api/register", json={"username": "someuser", "email": "some@example.com", "password": "longenough1"})
        assert r.status_code == 501


@pytest.mark.unit
class TestRegisterAndVerifyFlow:
    def test_register_sends_a_verification_email_and_does_not_create_the_account_yet(self, client, sent_emails):
        r = client.post("/api/register", json={"username": "pending1", "email": "pending1@example.com", "password": "longenough1"})
        assert r.status_code == 200
        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == "pending1@example.com"
        assert users.exists("pending1") is False  # not real until verified

    def test_clicking_the_verify_link_creates_the_account_grants_quota_and_logs_in(self, client, sent_emails):
        client.post("/api/register", json={"username": "newperson", "email": "newperson@example.com", "password": "mypassword1"})
        token = _extract_token(sent_emails[0]["html"])

        r = client.get(f"/verify-email?token={token}", follow_redirects=False)
        assert r.status_code == 200
        assert "验证成功" in r.text
        assert "ta_session" in r.cookies

        assert users.exists("newperson") is True
        assert users.is_admin("newperson") is False
        assert quota.get_remaining("newperson") == routes_module.NEW_USER_BONUS_QUOTA

        # The auto-set session cookie actually authenticates subsequent requests.
        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["username"] == "newperson"

    def test_the_new_account_can_log_in_with_the_chosen_password(self, client, sent_emails):
        client.post("/api/register", json={"username": "loginlater", "email": "loginlater@example.com", "password": "secretpass1"})
        token = _extract_token(sent_emails[0]["html"])
        client.get(f"/verify-email?token={token}")

        r = client.post("/api/login", json={"username": "loginlater", "password": "secretpass1"})
        assert r.status_code == 200

    def test_verify_email_token_is_single_use(self, client, sent_emails):
        client.post("/api/register", json={"username": "onceonly", "email": "onceonly@example.com", "password": "longenough1"})
        token = _extract_token(sent_emails[0]["html"])
        client.get(f"/verify-email?token={token}")
        r2 = client.get(f"/verify-email?token={token}")
        assert "无效或已过期" in r2.text

    def test_invalid_token_shows_error_page(self, client):
        r = client.get("/verify-email?token=totally-made-up")
        assert r.status_code == 200
        assert "无效或已过期" in r.text

    def test_reregistering_before_verifying_invalidates_the_first_link(self, client, sent_emails):
        client.post("/api/register", json={"username": "retryuser", "email": "retryuser@example.com", "password": "firstpassword1"})
        first_token = _extract_token(sent_emails[0]["html"])
        client.post("/api/register", json={"username": "retryuser", "email": "retryuser@example.com", "password": "secondpassword1"})
        second_token = _extract_token(sent_emails[1]["html"])

        assert first_token != second_token
        r1 = client.get(f"/verify-email?token={first_token}")
        assert "无效或已过期" in r1.text

        r2 = client.get(f"/verify-email?token={second_token}")
        assert "验证成功" in r2.text
        # The second (most recent) password is the one that ended up active.
        assert client.post("/api/login", json={"username": "retryuser", "password": "secondpassword1"}).status_code == 200


@pytest.mark.unit
class TestResendVerification:
    def test_resend_reissues_a_working_token(self, client, sent_emails):
        client.post("/api/register", json={"username": "resenduser", "email": "resend@example.com", "password": "longenough1"})
        first_token = _extract_token(sent_emails[0]["html"])

        r = client.post("/api/register/resend", json={"identifier": "resend@example.com"})
        assert r.status_code == 200
        assert len(sent_emails) == 2
        new_token = _extract_token(sent_emails[1]["html"])

        assert client.get(f"/verify-email?token={first_token}").text.__contains__("无效或已过期")
        assert "验证成功" in client.get(f"/verify-email?token={new_token}").text

    def test_resend_for_unknown_identifier_returns_generic_ok_without_sending(self, client, sent_emails):
        r = client.post("/api/register/resend", json={"identifier": "nobody@example.com"})
        assert r.status_code == 200
        assert len(sent_emails) == 0


@pytest.mark.unit
class TestRateLimiting:
    def test_register_endpoint_429s_after_too_many_attempts_from_one_ip(self, client, sent_emails):
        for i in range(5):
            client.post("/api/register", json={"username": f"rluser{i}", "email": f"rl{i}@example.com", "password": "longenough1"})
        r = client.post("/api/register", json={"username": "rluserlast", "email": "rllast@example.com", "password": "longenough1"})
        assert r.status_code == 429


@pytest.mark.unit
class TestPublicRoutesBypassAuth:
    def test_register_page_and_api_are_reachable_without_a_session(self, client):
        assert client.get("/register").status_code == 200

    def test_verify_email_is_reachable_without_a_session(self, client):
        r = client.get("/verify-email?token=whatever")
        assert r.status_code == 200
