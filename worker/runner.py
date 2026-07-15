"""Run a single job.

Wraps :func:`tradingagents.graph.trading_graph.TradingAgentsGraph.propagate`
and turns its chunk stream into HTTP POSTs to the API worker, plus a
final R2 upload with the finished report.

Kept deliberately independent from the polling daemon so it can be
called from CLI / tests without any queue involvement.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from .cf_client import CFClient
from .r2_writer import R2Writer

logger = logging.getLogger(__name__)


@dataclass
class JobSpec:
    id: str
    user_id: str
    ticker: str
    trade_date: str
    provider: str
    deep_llm: str
    quick_llm: str
    config_json: str

    @classmethod
    def from_row(cls, row: dict) -> "JobSpec":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            ticker=row["ticker"],
            trade_date=row["trade_date"],
            provider=row["provider"],
            deep_llm=row["deep_llm"],
            quick_llm=row["quick_llm"],
            config_json=row["config_json"],
        )


def _build_config(spec: JobSpec) -> dict[str, Any]:
    """Merge DEFAULT_CONFIG with the per-job overrides from D1."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["llm_provider"] = spec.provider
    cfg["deep_think_llm"] = spec.deep_llm
    cfg["quick_think_llm"] = spec.quick_llm
    try:
        overrides = json.loads(spec.config_json) or {}
    except json.JSONDecodeError:
        overrides = {}
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


# ── Report post-processing ──────────────────────────────────
# Minimal extraction — full decision.py logic lives in web/ still and
# can be imported unchanged. This shim keeps runner self-contained
# even if the web module gets moved later.


def _extract_summary(final_report: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", final_report).strip()
    return text[:limit]


def _extract_decision(final_report: str) -> dict[str, Any]:
    """Best-effort scrape of the trader/PM decision block.

    Cheap regex — the definitive parser is ``web/decision.py`` which we
    should call once that module is imported by the worker. For now we
    just look for BUY/HOLD/SELL and one price line.
    """
    action_match = re.search(r"\b(BUY|HOLD|SELL)\b", final_report)
    target_match = re.search(r"(?:price[_ ]target|target[_ ]price)[^\d\-]*([\d.]+)", final_report, re.I)
    stop_match = re.search(r"stop[_ ]loss[^\d\-]*([\d.]+)", final_report, re.I)
    return {
        "action": action_match.group(1) if action_match else None,
        "price_target": float(target_match.group(1)) if target_match else None,
        "stop_loss": float(stop_match.group(1)) if stop_match else None,
    }


# ── Main entrypoint ──────────────────────────────────────────


def run_job(spec: JobSpec, cf: CFClient, r2: R2Writer | None) -> None:
    """Run a single job to completion, streaming progress + finalizing.

    Any exception here is caught and reported to CF as a failed job; we
    never propagate up to the daemon loop, which would kill the worker.
    """
    logger.info("job %s: starting %s @ %s", spec.id, spec.ticker, spec.trade_date)
    cf.post_chunk(spec.id, "stage", {"stage": "starting"})

    def on_chunk(chunk: dict[str, Any]) -> None:
        try:
            cf.post_chunk(spec.id, "chunk", chunk)
        except Exception as exc:  # noqa: BLE001 — never crash job on telemetry
            logger.debug("post_chunk failed (ignored): %s", exc)

    def should_cancel() -> bool:
        return cf.is_cancelled(spec.id)

    try:
        cfg = _build_config(spec)
        ta = TradingAgentsGraph(config=cfg)
        state = ta.propagate(
            spec.ticker,
            spec.trade_date,
            on_chunk=on_chunk,
            should_cancel=should_cancel,
        )
    except Exception as exc:
        logger.exception("job %s failed: %s", spec.id, exc)
        cf.fail_job(spec.id, f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        return

    final_report = state.get("final_report") or state.get("trader_investment_plan") or ""
    if not final_report:
        cf.fail_job(spec.id, "empty final report")
        return

    # Persist to R2.
    if r2 is None:
        r2_key_final = None
        logger.warning("no R2 writer configured — skipping upload")
    else:
        r2_key_final = f"reports/{spec.id}/final.md"
        r2.put_markdown(r2_key_final, final_report)

    decision = _extract_decision(final_report)
    summary = _extract_summary(final_report)

    report_payload = {
        "r2_key_final": r2_key_final,
        "summary_extract": summary,
        "decision_action": decision["action"],
        "decision_price_target": decision["price_target"],
        "decision_stop_loss": decision["stop_loss"],
        # Sentiment fields are best-effort; leave None if not present.
        "sentiment_band": (state.get("sentiment_report") or {}).get("overall_band") if isinstance(state.get("sentiment_report"), dict) else None,
        "sentiment_score": (state.get("sentiment_report") or {}).get("overall_score") if isinstance(state.get("sentiment_report"), dict) else None,
        "sentiment_confidence": (state.get("sentiment_report") or {}).get("confidence") if isinstance(state.get("sentiment_report"), dict) else None,
    }
    cf.finish_job(spec.id, report_payload)
    logger.info("job %s: done", spec.id)
