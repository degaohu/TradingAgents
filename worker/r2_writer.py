"""Cloudflare R2 writer.

R2 speaks S3, so boto3 works. We use a plain client (not the higher-
level Object Manager) because uploads are one-shot Markdown blobs,
not multi-part streams.

Object layout in the bucket:

    reports/{job_id}/final.md       — raw agent report
    reports/{job_id}/polished.md    — polished/LLM-synthesized version
    reports/{job_id}/chunks/*.md    — intermediate per-agent artifacts
"""

from __future__ import annotations

import logging
from typing import Iterable

import boto3
from botocore.client import Config

from .config import WorkerConfig

logger = logging.getLogger(__name__)


class R2Writer:
    def __init__(self, cfg: WorkerConfig) -> None:
        if not (cfg.r2_account_id and cfg.r2_access_key_id and cfg.r2_secret_access_key):
            raise RuntimeError("R2 credentials missing; set R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY")
        self._bucket = cfg.r2_bucket
        endpoint = f"https://{cfg.r2_account_id}.r2.cloudflarestorage.com"
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=cfg.r2_access_key_id,
            aws_secret_access_key=cfg.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def put_markdown(self, key: str, body: str) -> str:
        """Upload a Markdown blob under ``key``. Returns the same key."""
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        logger.debug("r2 put %s (%d bytes)", key, len(body))
        return key

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``. Returns the number deleted."""
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[dict] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                keys.append({"Key": obj["Key"]})
        if not keys:
            return 0
        # Delete up to 1000 objects per request (S3 API cap).
        deleted = 0
        for batch in _chunks(keys, 1000):
            resp = self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": batch, "Quiet": True},
            )
            deleted += len(resp.get("Deleted", []) or [])
        logger.info("r2 delete_prefix %s → %d objects", prefix, deleted)
        return deleted


def _chunks(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i : i + n]
