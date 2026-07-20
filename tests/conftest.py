"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture(autouse=True)
def _isolate_admin_activity_db(tmp_path, monkeypatch):
    """Redirect web/activity.py's SQLite file to a per-test temp path.

    Global and autouse (not just in the web-admin test files) because any
    test that exercises web/routes.py's login or /api/analyze path writes
    through this module — without this, running the suite quietly appends
    fake login/analysis rows to the developer's real
    ``~/.tradingagents/web_admin.db`` (caught the hard way: a local smoke
    test after adding admin activity logging showed test-suite noise —
    ip="testclient" rows — already present in the real file).
    """
    try:
        from web import activity
    except ImportError:
        return  # fastapi/uvicorn (the "web" extra) not installed in this env
    monkeypatch.setattr(activity, "_DB_PATH", str(tmp_path / "web_admin_test.db"))


@pytest.fixture(autouse=True)
def _isolate_history_db(tmp_path, monkeypatch):
    """Redirect web/history.py's SQLite file to a per-test temp path — same
    reasoning as _isolate_admin_activity_db above, for the same reason:
    completed /api/analyze runs in tests must not write into the real
    ~/.tradingagents/web_history.db."""
    try:
        from web import history
    except ImportError:
        return
    monkeypatch.setattr(history, "_DB_PATH", str(tmp_path / "web_history_test.db"))


@pytest.fixture(autouse=True)
def _isolate_quota_db(tmp_path, monkeypatch):
    """Redirect web/quota.py's SQLite file to a per-test temp path — same
    reasoning as the history/activity isolation above."""
    try:
        from web import quota
    except ImportError:
        return
    monkeypatch.setattr(quota, "_DB_PATH", str(tmp_path / "web_quota_test.db"))


@pytest.fixture(autouse=True)
def _isolate_users_db(tmp_path, monkeypatch):
    """Redirect web/users.py's SQLite file to a per-test temp path, and reset
    its one-time seed guard so each test's fresh DB is re-seeded from the
    env-var users (routes._USERS)."""
    try:
        from web import users
    except ImportError:
        return
    monkeypatch.setattr(users, "_DB_PATH", str(tmp_path / "web_users_test.db"))
    monkeypatch.setattr(users, "_seeded_path", None)


@pytest.fixture(autouse=True)
def _isolate_registration_db(tmp_path, monkeypatch):
    """Redirect web/registration.py's SQLite file to a per-test temp path —
    same reasoning as the other isolation fixtures above."""
    try:
        from web import registration
    except ImportError:
        return
    monkeypatch.setattr(registration, "_DB_PATH", str(tmp_path / "web_pending_registrations_test.db"))


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
