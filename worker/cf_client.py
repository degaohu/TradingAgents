"""Signed HTTP client for calling the Cloudflare API worker's
``/internal/*`` routes.

Every request is HMAC-SHA256 signed with ``CF_INTERNAL_TOKEN`` so the
API worker can trust it. The signature scheme mirrors what
``cf/workers/api/src/lib/internal-auth.ts`` verifies:

    message   = f"{method}\\n{path}\\n{timestamp}\\n{body_sha256}"
    signature = hex(HMAC_SHA256(secret, message))

Headers:
    X-TA-Timestamp   unix seconds
    X-TA-Signature   hex-encoded HMAC
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .config import WorkerConfig

logger = logging.getLogger(__name__)


class CFClient:
    def __init__(self, cfg: WorkerConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.headers["User-Agent"] = f"tradingagents-worker/{cfg.worker_id}"

    def _sign(self, method: str, path: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{method}\n{path}\n{ts}\n{body_hash}"
        sig = hmac.new(
            self._cfg.cf_internal_token.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {"X-TA-Timestamp": ts, "X-TA-Signature": sig}

    def _request(
        self,
        method: str,
        path: str,
        json_body: Any | None = None,
        timeout: float = 30.0,
    ) -> Any:
        url = f"{self._cfg.cf_api_base}{path}"
        body_bytes = b""
        headers = {"content-type": "application/json"}
        if json_body is not None:
            body_bytes = json.dumps(json_body, separators=(",", ":")).encode()
        headers.update(self._sign(method, urlparse(url).path, body_bytes))
        resp = self._session.request(
            method,
            url,
            data=body_bytes if body_bytes else None,
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code >= 500:
            logger.warning("CF API %s %s → %s: %s", method, path, resp.status_code, resp.text[:200])
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    # ── High-level helpers ─────────────────────────────────

    def claim_job(self) -> dict | None:
        """POST /internal/jobs/claim → next queued job or None."""
        try:
            res = self._request("POST", "/internal/jobs/claim", {"worker_id": self._cfg.worker_id})
        except requests.RequestException as exc:
            logger.warning("claim_job failed: %s", exc)
            return None
        return (res or {}).get("job")

    def post_chunk(self, job_id: str, event_type: str, payload: Any) -> None:
        self._request(
            "POST",
            f"/internal/jobs/{job_id}/chunk",
            {"event_type": event_type, "payload": payload},
            timeout=10.0,
        )

    def finish_job(self, job_id: str, report: dict) -> None:
        self._request(
            "POST",
            f"/internal/jobs/{job_id}/finish",
            {"status": "done", "report": report},
            timeout=30.0,
        )

    def fail_job(self, job_id: str, error_message: str) -> None:
        self._request(
            "POST",
            f"/internal/jobs/{job_id}/finish",
            {"status": "failed", "error_message": error_message},
            timeout=30.0,
        )

    def is_cancelled(self, job_id: str) -> bool:
        try:
            res = self._request("GET", f"/internal/jobs/{job_id}/cancel-flag")
        except requests.RequestException:
            return False
        return bool((res or {}).get("cancelled"))
