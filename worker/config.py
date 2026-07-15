"""Worker daemon configuration from environment variables.

All settings that could plausibly differ between local dev and prod
come from the environment. Nothing here talks to the network — this
module is safe to import from anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerConfig:
    # ── API ──────────────────────────────────────────────────
    # Base URL of the Cloudflare Workers API (Hono).
    #   dev:  http://127.0.0.1:8787
    #   prod: https://tradingagents-api.<subdomain>.workers.dev
    cf_api_base: str

    # HMAC shared secret with the API worker (`CF_INTERNAL_TOKEN` there).
    cf_internal_token: str

    # ── R2 (S3-compatible) ────────────────────────────────────
    # If set, the worker writes report bodies to R2 directly instead
    # of streaming them through the Workers API (saves bandwidth on
    # Workers and avoids their 100 MB request-body cap).
    r2_account_id: str | None
    r2_access_key_id: str | None
    r2_secret_access_key: str | None
    r2_bucket: str

    # ── Runtime ──────────────────────────────────────────────
    worker_id: str
    poll_interval_seconds: float
    max_concurrent_jobs: int
    log_level: str


def load_config() -> WorkerConfig:
    def req(k: str) -> str:
        v = os.environ.get(k)
        if not v:
            raise RuntimeError(f"missing required env var {k}")
        return v

    return WorkerConfig(
        cf_api_base=req("CF_API_BASE").rstrip("/"),
        cf_internal_token=req("CF_INTERNAL_TOKEN"),
        r2_account_id=os.environ.get("R2_ACCOUNT_ID"),
        r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
        r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
        r2_bucket=os.environ.get("R2_BUCKET", "tradingagents-reports"),
        worker_id=os.environ.get("WORKER_ID", "worker-1"),
        poll_interval_seconds=float(os.environ.get("POLL_INTERVAL_SECONDS", "3.0")),
        max_concurrent_jobs=int(os.environ.get("MAX_CONCURRENT_JOBS", "2")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
