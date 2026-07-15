"""Long-running polling daemon.

Every ``poll_interval_seconds``, asks the API worker for the next
queued job (atomic CLAIM), runs it via :mod:`worker.runner`, and
loops. Optional CLI ``--once`` mode runs a synthetic job locally for
end-to-end tests without any queue.

Concurrency: for MVP the daemon is single-threaded — one job at a
time per process. To scale, run multiple systemd instances on the
same VPS (each with a different ``WORKER_ID``); the CLAIM query is
atomic so they won't step on each other.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Any

from .cf_client import CFClient
from .config import WorkerConfig, load_config
from .r2_writer import R2Writer
from .runner import JobSpec, run_job

logger = logging.getLogger(__name__)


class Daemon:
    def __init__(self, cfg: WorkerConfig) -> None:
        self._cfg = cfg
        self._cf = CFClient(cfg)
        self._r2 = R2Writer(cfg) if cfg.r2_access_key_id else None
        self._stopping = False

    def stop(self, *_: Any) -> None:
        logger.info("stop signal received; finishing current work and exiting")
        self._stopping = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self._stopping:
            try:
                row = self._cf.claim_job()
            except Exception as exc:
                logger.warning("claim failed, backing off: %s", exc)
                self._sleep(min(30.0, self._cfg.poll_interval_seconds * 3))
                continue

            if not row:
                self._sleep(self._cfg.poll_interval_seconds)
                continue

            spec = JobSpec.from_row(row)
            try:
                run_job(spec, self._cf, self._r2)
            except Exception:
                # run_job handles its own failures; any leakage here is a bug.
                logger.exception("run_job leaked exception for %s", spec.id)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stopping:
            time.sleep(min(0.5, end - time.monotonic()))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="TradingAgents VPS worker daemon")
    parser.add_argument("--once", action="store_true", help="Run a single ad-hoc job then exit")
    parser.add_argument("--ticker", help="Ticker (with --once)")
    parser.add_argument("--trade-date", help="YYYY-MM-DD (with --once)")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--deep-llm", default="gpt-5.5")
    parser.add_argument("--quick-llm", default="gpt-5.4-mini")
    args = parser.parse_args()

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    if args.once:
        if not (args.ticker and args.trade_date):
            parser.error("--once requires --ticker and --trade-date")
        cf = CFClient(cfg)
        r2 = R2Writer(cfg) if cfg.r2_access_key_id else None
        spec = JobSpec(
            id=f"adhoc-{int(time.time())}",
            user_id="cli",
            ticker=args.ticker,
            trade_date=args.trade_date,
            provider=args.provider,
            deep_llm=args.deep_llm,
            quick_llm=args.quick_llm,
            config_json="{}",
        )
        run_job(spec, cf, r2)
        return 0

    Daemon(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
