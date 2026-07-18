"""Integration tests for the admin panel: login/logout activity logging,
admin-only gating, and the /api/admin/status payload (users, activity,
running-job status)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import web.routes as routes_module
from web import activity
from web.jobs import JobRegistry

from .test_web_routes import _FakeGraphFactory, _read_sse_events

# Isolation of web.activity's SQLite file from the real
# ~/.tradingagents/web_admin.db is handled globally and autouse in
# tests/conftest.py (_isolate_admin_activity_db).


@pytest.fixture()
def raw_client(monkeypatch):
    """Unauthenticated client — for exercising the login flow itself."""
    monkeypatch.setattr(routes_module, "registry", JobRegistry())
    return TestClient(routes_module.app)


def _login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


@pytest.mark.unit
class TestLoginLogout:
    def test_successful_login_sets_cookie_and_logs_activity(self, raw_client):
        resp = _login(raw_client, "admin", routes_module._USERS["admin"])
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True
        assert "ta_session" in resp.cookies

        events = activity.recent_activity(10)
        assert events[0]["action"] == "login"
        assert events[0]["username"] == "admin"

    def test_non_admin_user_login_reports_is_admin_false(self, raw_client):
        resp = _login(raw_client, "user2", routes_module._USERS["user2"])
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is False

    def test_wrong_password_returns_401_and_logs_login_failed(self, raw_client):
        resp = _login(raw_client, "admin", "definitely-wrong")
        assert resp.status_code == 401
        events = activity.recent_activity(10)
        assert events[0]["action"] == "login_failed"
        assert events[0]["username"] == "admin"

    def test_unknown_username_returns_401(self, raw_client):
        resp = _login(raw_client, "not-a-real-user", "whatever")
        assert resp.status_code == 401

    def test_logout_logs_activity_for_the_current_user(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        resp = raw_client.post("/api/logout")
        assert resp.status_code == 200
        events = activity.recent_activity(10)
        assert events[0]["action"] == "logout"
        assert events[0]["username"] == "admin"

    def test_unauthenticated_api_request_returns_401(self, raw_client):
        resp = raw_client.get("/api/jobs/does-not-exist")
        assert resp.status_code == 401

    def test_unauthenticated_browser_request_redirects_to_login(self, raw_client):
        resp = raw_client.get("/dashboard-page-that-does-not-exist", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"


@pytest.mark.unit
class TestVersionDisplay:
    """The app version (from pyproject.toml via web/version.py) is surfaced
    through /api/me for the in-app account bar. It is deliberately NOT on the
    login page — that page shows nothing to unauthenticated visitors."""

    def test_login_page_does_not_leak_version_or_admin_hint(self, raw_client):
        from web.version import get_version

        html = raw_client.get("/login").text
        assert get_version() not in html
        assert "__VERSION__" not in html
        assert 'placeholder="admin"' not in html

    def test_whoami_reports_the_version_matching_pyproject(self, raw_client):
        from web.version import get_version

        _login(raw_client, "admin", routes_module._USERS["admin"])
        assert raw_client.get("/api/me").json()["version"] == get_version()

    def test_login_remember_me_sets_persistent_cookie(self, raw_client):
        resp = raw_client.post(
            "/api/login",
            json={"username": "admin", "password": routes_module._USERS["admin"], "remember": True},
        )
        assert resp.status_code == 200
        assert "max-age" in resp.headers.get("set-cookie", "").lower()

    def test_login_without_remember_uses_session_cookie(self, raw_client):
        resp = raw_client.post(
            "/api/login",
            json={"username": "admin", "password": routes_module._USERS["admin"], "remember": False},
        )
        assert resp.status_code == 200
        assert "max-age" not in resp.headers.get("set-cookie", "").lower()


@pytest.mark.unit
class TestWhoAmI:
    """/api/me — the client shell's account bar reads this to decide whether
    to show the admin-panel link, unlike /api/admin/status which 403s for
    non-admins."""

    def test_admin_user_sees_is_admin_true(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        data = raw_client.get("/api/me").json()
        assert data["username"] == "admin"
        assert data["is_admin"] is True
        assert data["active_job_id"] is None
        assert isinstance(data["version"], str) and data["version"]

    def test_regular_user_sees_is_admin_false(self, raw_client):
        _login(raw_client, "user2", routes_module._USERS["user2"])
        data = raw_client.get("/api/me").json()
        assert data["username"] == "user2"
        assert data["is_admin"] is False
        assert data["active_job_id"] is None

    def test_active_job_id_is_only_reported_to_the_user_who_started_it(self, raw_client, monkeypatch):
        factory = _FakeGraphFactory(cancel_after_first_chunk=True)
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", factory.build())
        _login(raw_client, "user2", routes_module._USERS["user2"])
        resp = raw_client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = resp.json()["job_id"]
        assert factory.first_chunk_emitted.wait(timeout=5.0)

        # The user who started it sees the job id (e.g. reopening on another device).
        assert raw_client.get("/api/me").json()["active_job_id"] == job_id

        # A different logged-in user does not.
        _login(raw_client, "admin", routes_module._USERS["admin"])
        assert raw_client.get("/api/me").json()["active_job_id"] is None

        raw_client.post(f"/api/jobs/{job_id}/cancel")

    def test_unauthenticated_request_gets_401_same_as_any_api_route(self, raw_client):
        resp = raw_client.get("/api/me")
        assert resp.status_code == 401


@pytest.mark.unit
class TestAdminGating:
    def test_non_admin_user_gets_403_on_admin_status(self, raw_client):
        _login(raw_client, "user2", routes_module._USERS["user2"])
        resp = raw_client.get("/api/admin/status")
        assert resp.status_code == 403

    def test_admin_user_gets_200_on_admin_status(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        resp = raw_client.get("/api/admin/status")
        assert resp.status_code == 200

    def test_non_admin_cannot_toggle_maintenance(self, raw_client):
        _login(raw_client, "user2", routes_module._USERS["user2"])
        resp = raw_client.post("/api/admin/maintenance", json={"on": True})
        assert resp.status_code == 403


@pytest.mark.unit
class TestUserManagement:
    def test_admin_status_exposes_plaintext_passwords(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        rows = {u["username"]: u for u in raw_client.get("/api/admin/status").json()["user_summary"]}
        assert rows["user2"]["password"] == routes_module._USERS["user2"]
        assert rows["admin"]["password"] == routes_module._USERS["admin"]

    def test_admin_can_reset_a_users_password(self, raw_client):
        from web import users

        _login(raw_client, "admin", routes_module._USERS["admin"])
        resp = raw_client.post("/api/admin/password", json={"username": "user2", "password": "fresh-pw"})
        assert resp.status_code == 200
        assert users.verify("user2", "fresh-pw") is True
        # And the old password no longer works for logging in.
        assert _login(raw_client, "user2", routes_module._USERS["user2"]).status_code == 401
        assert _login(raw_client, "user2", "fresh-pw").status_code == 200

    def test_reset_rejects_empty_password_and_unknown_user(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        assert raw_client.post("/api/admin/password", json={"username": "user2", "password": ""}).status_code == 400
        assert raw_client.post("/api/admin/password", json={"username": "ghost", "password": "x"}).status_code == 404

    def test_non_admin_cannot_reset_passwords(self, raw_client):
        _login(raw_client, "user2", routes_module._USERS["user2"])
        resp = raw_client.post("/api/admin/password", json={"username": "user3", "password": "x"})
        assert resp.status_code == 403


@pytest.mark.unit
class TestAdminStatusPayload:
    def test_no_running_job_does_not_crash_and_reports_null(self, raw_client):
        # Regression guard: admin_status used to call job.status() (a plain
        # str attribute, not a method) whenever a job was running, which
        # raised TypeError. This covers the no-job branch...
        _login(raw_client, "admin", routes_module._USERS["admin"])
        resp = raw_client.get("/api/admin/status")
        assert resp.status_code == 200
        assert resp.json()["running_job"] is None

    def test_running_job_status_field_is_a_plain_string_not_a_crash(self, raw_client):
        # ...and this covers the actual regression: a running job must not
        # crash admin_status when serializing its status.
        job = routes_module.registry.create("AAPL", "2026-06-01")
        _login(raw_client, "admin", routes_module._USERS["admin"])
        resp = raw_client.get("/api/admin/status")
        assert resp.status_code == 200
        assert resp.json()["running_job"]["status"] == job.status
        assert isinstance(resp.json()["running_job"]["status"], str)

    def test_user_summary_and_recent_activity_present(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        data = raw_client.get("/api/admin/status").json()
        usernames = {row["username"] for row in data["user_summary"]}
        assert usernames == {"admin", "user2", "user3"}
        assert any(e["action"] == "login" for e in data["recent_activity"])

    def test_maintenance_toggle_is_logged(self, raw_client):
        _login(raw_client, "admin", routes_module._USERS["admin"])
        raw_client.post("/api/admin/maintenance", json={"on": True})
        raw_client.post("/api/admin/maintenance", json={"on": False})  # leave it off for other tests
        data = raw_client.get("/api/admin/status").json()
        toggles = [e for e in data["recent_activity"] if e["action"] == "maintenance_toggle"]
        assert len(toggles) == 2
        assert toggles[0]["detail"] == "OFF"
        assert toggles[1]["detail"] == "ON"


@pytest.mark.unit
class TestAnalysisActivityAttribution:
    def test_analyze_start_and_finish_are_attributed_to_the_submitting_user(self, raw_client, monkeypatch):
        monkeypatch.setattr(routes_module, "TradingAgentsGraph", _FakeGraphFactory().build())
        _login(raw_client, "user2", routes_module._USERS["user2"])

        resp = raw_client.post("/api/analyze", json={"ticker": "NVDA", "trade_date": "2026-01-15"})
        job_id = resp.json()["job_id"]
        _read_sse_events(raw_client, job_id)

        _login(raw_client, "admin", routes_module._USERS["admin"])  # switch to the admin to read status
        data = raw_client.get("/api/admin/status").json()
        actions = {(e["username"], e["action"]) for e in data["recent_activity"]}
        assert ("user2", "analyze_start") in actions
        assert ("user2", "analyze_finish") in actions

        user2_summary = next(u for u in data["user_summary"] if u["username"] == "user2")
        assert user2_summary["analyze_count"] == 1
