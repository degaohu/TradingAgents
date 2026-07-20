"""Twilio Verify API client for phone-number verification codes.

Uses Twilio's Verify service (https://www.twilio.com/docs/verify/api) via
plain HTTP (requests is already a dependency) rather than the twilio SDK,
to avoid a new dependency for two API calls. Twilio owns the one-time
code itself — generation, ~10 minute expiry, and delivery — this module
only starts and checks a verification.

Configured via TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and
TWILIO_VERIFY_SERVICE_SID (the Service SID from a Verify Service created in
the Twilio console, starts with "VA").
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("TradingAgentsWebServer")

_API_BASE = "https://verify.twilio.com/v2"


class TwilioNotConfigured(RuntimeError):
    """Raised when TWILIO_ACCOUNT_SID/AUTH_TOKEN/VERIFY_SERVICE_SID aren't all set."""


def is_configured() -> bool:
    return bool(
        os.environ.get("TWILIO_ACCOUNT_SID")
        and os.environ.get("TWILIO_AUTH_TOKEN")
        and os.environ.get("TWILIO_VERIFY_SERVICE_SID")
    )


def _auth_and_service() -> tuple[tuple[str, str], str]:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    service_sid = os.environ.get("TWILIO_VERIFY_SERVICE_SID")
    if not account_sid or not auth_token or not service_sid:
        raise TwilioNotConfigured(
            "短信验证服务未配置。请联系系统管理员在服务器端配置 "
            "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VERIFY_SERVICE_SID 环境变量。"
        )
    return (account_sid, auth_token), service_sid


def start_verification(phone_e164: str) -> None:
    """Trigger Twilio to text a one-time code to this number. Raises
    TwilioNotConfigured, or requests.HTTPError on an API-level failure
    (invalid number, Twilio account/billing issue, etc.)."""
    auth, service_sid = _auth_and_service()
    resp = requests.post(
        f"{_API_BASE}/Services/{service_sid}/Verifications",
        data={"To": phone_e164, "Channel": "sms"},
        auth=auth,
        timeout=10.0,
    )
    if not resp.ok:
        logger.error("Twilio Verify start error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()


def check_verification(phone_e164: str, code: str) -> bool:
    """True if `code` is currently valid for this number. A wrong code is
    NOT an error — Twilio reports it as a normal (non-"approved") status,
    reflected here as a plain False return. Raises TwilioNotConfigured or
    requests.HTTPError only for actual transport/auth failures."""
    auth, service_sid = _auth_and_service()
    resp = requests.post(
        f"{_API_BASE}/Services/{service_sid}/VerificationCheck",
        data={"To": phone_e164, "Code": code},
        auth=auth,
        timeout=10.0,
    )
    if resp.status_code == 404:
        # No pending verification for this number (expired, never started,
        # or already consumed) — same outcome as a wrong code from the
        # caller's perspective.
        return False
    if not resp.ok:
        logger.error("Twilio Verify check error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json().get("status") == "approved"
