"""FastAPI app for the TradingAgents web dashboard.

Analysis runs as a background ``Job`` (see ``jobs.py``): ``POST /api/analyze``
returns immediately with a job id, progress streams over SSE
(``GET /api/jobs/{id}/events``), and ``GET /api/jobs/{id}`` lets a refreshed
page recover status without replaying the whole event log itself. Only one
job runs at a time — a single-user local dashboard has no need for
concurrent runs, and sharing one root logger across simultaneous runs would
interleave their log streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading

import requests

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure TradingAgents packages can be imported regardless of cwd.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.graph.trading_graph import RunCancelled, TradingAgentsGraph  # noqa: E402

from .decision import build_decision_summary  # noqa: E402
from .jobs import Job, JobAlreadyRunning, JobRegistry  # noqa: E402
from .pipeline import PipelineTracker, build_stage_specs  # noqa: E402
from .polish import generate_polished_report  # noqa: E402

logger = logging.getLogger("TradingAgentsWebServer")

# The dashboard doesn't yet expose analyst selection (WEB_FRONTEND_PLAN.md
# P3 3.2) — this matches TradingAgentsGraph's own default so behavior is
# unchanged from before the job model.
_DEFAULT_ANALYSTS = ("market", "social", "news", "fundamentals")

app = FastAPI(title="TradingAgents Web Dashboard")

# CORS is opt-in and off by default: the dashboard's static files are served
# by this same app, so same-origin requests need no CORS headers at all.
# Only set TRADINGAGENTS_WEB_CORS_ORIGINS (comma-separated) when the frontend
# is genuinely hosted elsewhere (e.g. a separate dev server).
_cors_origins = [o.strip() for o in os.environ.get("TRADINGAGENTS_WEB_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

registry = JobRegistry()


class AnalysisRequest(BaseModel):
    ticker: str
    trade_date: str
    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    max_debate_rounds: int | None = None
    checkpoint_enabled: bool | None = None
    output_language: str | None = None


class _JobLogHandler(logging.Handler):
    """Routes root-logger output into a job's event stream (mirrors the
    previous ``QueueHandler``, retargeted at ``Job.emit`` instead of a
    per-request ``queue.Queue``).

    Filters out the noisy HTTP client chatter (``httpx``, ``urllib3``,
    ``openai`` etc.) that just repeats "POST … 200 OK" for every model
    call — the dashboard's stage tracker already reports meaningful
    progress, so those lines only clutter the console."""

    _NOISY_LOGGERS = (
        "httpx", "httpcore", "urllib3", "openai._base_client",
        "anthropic._base_client", "google.api_core", "google.auth",
        "asyncio", "watchfiles", "chromadb",
    )
    _NOISY_SUBSTRINGS = (
        "HTTP Request:", "HTTP/1.1 200", "HTTP/1.1 201",
    )

    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        # Drop noise before formatting so we don't spend cycles on it.
        name = record.name or ""
        if any(name.startswith(p) for p in self._NOISY_LOGGERS):
            return
        msg = record.getMessage()
        if any(s in msg for s in self._NOISY_SUBSTRINGS):
            return
        with contextlib.suppress(Exception):
            self.job.emit("log", message=self.format(record))


def _build_config(req: AnalysisRequest) -> dict:
    config = DEFAULT_CONFIG.copy()
    if req.llm_provider:
        config["llm_provider"] = req.llm_provider
    if req.deep_think_llm:
        config["deep_think_llm"] = req.deep_think_llm
    if req.quick_think_llm:
        config["quick_think_llm"] = req.quick_think_llm
    if req.max_debate_rounds is not None:
        config["max_debate_rounds"] = req.max_debate_rounds
    if req.checkpoint_enabled is not None:
        config["checkpoint_enabled"] = req.checkpoint_enabled
    if req.output_language:
        config["output_language"] = req.output_language
    return config


def _build_result_payload(job: Job, final_state: dict, rating: str) -> dict:
    return {
        "ticker": job.ticker,
        "trade_date": job.trade_date,
        "decision": rating,
        "company_of_interest": final_state.get("company_of_interest", job.ticker),
        "market_report": final_state.get("market_report", ""),
        "sentiment_report": final_state.get("sentiment_report", ""),
        "news_report": final_state.get("news_report", ""),
        "fundamentals_report": final_state.get("fundamentals_report", ""),
        "investment_plan": final_state.get("investment_plan", ""),
        "final_trade_decision": final_state.get("final_trade_decision", ""),
        "trader_investment_plan": final_state.get("trader_investment_plan", ""),
        "investment_debate_state": final_state.get("investment_debate_state", {}),
        "risk_debate_state": final_state.get("risk_debate_state", {}),
        "decision_summary": build_decision_summary(final_state, rating),
    }


def _run_job(job: Job, config: dict) -> None:
    log_handler = _JobLogHandler(job)
    log_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    job.emit("topology", stages=build_stage_specs(_DEFAULT_ANALYSTS))
    # Human-readable stage transitions -> user-facing log stream.
    _STAGE_LOG_EN = {
        "market": "Technical & Market Analyst",
        "social": "Sentiment Analyst",
        "news":   "News & Macro Analyst",
        "fundamentals": "Fundamentals Analyst",
        "research_debate":  "Bull vs. Bear Debate",
        "research_manager": "Research Manager",
        "trader":           "Trader",
        "risk_debate":      "Risk Team Debate",
        "portfolio_manager": "Portfolio Manager",
    }
    _STAGE_LOG_ZH = {
        "market": "技术 · 市场分析师",
        "social": "情绪分析师",
        "news":   "新闻 · 宏观分析师",
        "fundamentals": "基本面分析师",
        "research_debate":  "多空研究员辩论",
        "research_manager": "研究经理裁决",
        "trader":           "交易员方案",
        "risk_debate":      "风险团队辩论",
        "portfolio_manager": "投资组合经理签批",
    }
    lang = (config.get("output_language") or "").lower()
    use_zh = lang.startswith("chinese") or lang.startswith("中")
    _STAGE_LOG = _STAGE_LOG_ZH if use_zh else _STAGE_LOG_EN

    def _on_stage_event(stage_id: str, status: str, elapsed_s, reports):
        job.emit("stage", stage_id=stage_id, status=status, elapsed_s=elapsed_s, reports=reports)
        label = _STAGE_LOG.get(stage_id, stage_id)
        if status == "running":
            job.emit("log", message=(f"▶ 开始 {label}" if use_zh else f"▶ Started {label}"))
        elif status == "done":
            tail = f" ({elapsed_s}s)" if elapsed_s is not None else ""
            job.emit("log", message=(f"✔ 完成 {label}{tail}" if use_zh else f"✔ Finished {label}{tail}"))

    tracker = PipelineTracker(_DEFAULT_ANALYSTS, on_event=_on_stage_event)

    try:
        ta = TradingAgentsGraph(_DEFAULT_ANALYSTS, debug=True, config=config)
        final_state, rating = ta.propagate(
            job.ticker, job.trade_date,
            on_chunk=tracker.update,
            should_cancel=job.cancel_requested.is_set,
        )
        data = _build_result_payload(job, final_state, rating)
        # Emit before finish(): wait_for_events_after() short-circuits once
        # is_finished() is true, even with nothing pending — flipping the
        # order would let a poll land between the two calls and return an
        # empty list, ending the SSE stream one event before the result
        # ever went out.
        job.emit("result", data=data)
        job.finish("done", result=data)
    except RunCancelled:
        job.emit("cancelled")
        job.finish("cancelled")
    except Exception as exc:
        logger.exception("Job %s failed", job.id)
        job.emit("error", message=str(exc))
        job.finish("error", error=str(exc))
    finally:
        root_logger.removeHandler(log_handler)


@app.post("/api/analyze", status_code=202)
def analyze_ticker(req: AnalysisRequest):
    logger.info("Received analysis request: %s", req)
    try:
        job = registry.create(req.ticker, req.trade_date)
    except JobAlreadyRunning as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "An analysis is already running.",
                "job_id": exc.job.id,
                "ticker": exc.job.ticker,
                "trade_date": exc.job.trade_date,
            },
        ) from exc

    config = _build_config(req)
    job.config = config  # kept for the AI-polish pass (reuses the same provider/model)
    threading.Thread(target=_run_job, args=(job, config), daemon=True).start()
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.snapshot()


@app.post("/api/jobs/{job_id}/polish")
def polish_report(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done" or not job.result:
        raise HTTPException(status_code=409, detail="job has no completed result to polish yet")
    try:
        polished = job.get_or_create_polished_report(lambda: generate_polished_report(job))
    except Exception as exc:
        logger.exception("AI polish failed for job %s", job_id)
        raise HTTPException(status_code=502, detail=f"AI polish failed: {exc}") from exc
    return {"polished_markdown": polished}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, response: Response):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.is_finished():
        response.status_code = 200
        return {"status": job.status, "message": "job already finished"}
    job.cancel_requested.set()
    response.status_code = 202
    return {"status": "cancelling"}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    last_event_id = int(request.headers.get("last-event-id") or 0)

    async def gen():
        after = last_event_id
        job.listener_connected()
        try:
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(job.wait_for_events_after, after, 1.0)
                for event in events:
                    after = event["id"]
                    yield f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
                if events:
                    continue
                if job.is_finished():
                    return
                yield ": keep-alive\n\n"
        finally:
            job.listener_disconnected()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/price/{ticker}")
def get_price_history(ticker: str, date: str):
    """Fetch price history for an interval around trade date using yfinance."""
    import datetime
    from dateutil.relativedelta import relativedelta
    import yfinance as yf
    from tradingagents.dataflows.symbol_utils import normalize_symbol
    from tradingagents.dataflows.stockstats_utils import yf_retry

    try:
        dt = datetime.datetime.strptime(date, "%Y-%m-%d")
        start_dt = dt - relativedelta(days=90)
        end_dt = dt + relativedelta(days=90)
        
        start_str = start_dt.strftime("%Y-%m-%d")
        end_inclusive_str = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")

        canonical = normalize_symbol(ticker)
        yf_ticker = yf.Ticker(canonical)
        
        # Fetch price history with retry
        data = yf_retry(lambda: yf_ticker.history(start=start_str, end=end_inclusive_str))
        
        if data.empty:
            return {"status": "error", "message": f"No data found for {ticker} in range {start_str} to {date}"}
            
        if data.index.tz is not None:
            data.index = data.index.tz_localize(None)
            
        prices = []
        for index, row in data.iterrows():
            prices.append({
                "date": index.strftime("%Y-%m-%d"),
                "close": round(float(row["Close"]), 2)
            })
            
        return {"status": "success", "prices": prices}
    except Exception as e:
        logger.error(f"Error fetching price history for {ticker}: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/ticker-search")
def ticker_search(q: str, limit: int = 15):
    """Aggregated ticker autosuggest — Yahoo (US/global) + Eastmoney (A-share).

    Called directly from the browser via same-origin fetch, so no CORS
    fuss. Runs upstream calls in threads to avoid blocking; both are
    best-effort and any single failure is swallowed.
    """
    q = (q or "").strip()
    if not q:
        return {"items": []}

    def _http_json(url: str, params: dict, headers: dict | None = None, timeout: float = 3.0):
        h = {"User-Agent": "Mozilla/5.0"}
        if headers:
            h.update(headers)
        r = requests.get(url, params=params, headers=h, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _yahoo() -> list[dict]:
        try:
            data = _http_json(
                "https://query1.finance.yahoo.com/v1/finance/search",
                {"q": q, "quotesCount": 12, "newsCount": 0},
            )
        except Exception:
            return []
        # Keep the useful long-tail — equities, ETFs, mutual funds,
        # indices, crypto pairs. Drop only currency/futures/options noise.
        KEEP = {"EQUITY", "ETF", "MUTUALFUND", "INDEX", "CRYPTOCURRENCY", None}
        out = []
        for x in data.get("quotes", []):
            sym = x.get("symbol")
            if not sym or x.get("quoteType") not in KEEP:
                continue
            out.append({
                "symbol": sym,
                "name": x.get("shortname") or x.get("longname") or "",
                "exch": x.get("exchDisp") or x.get("exchange") or "",
            })
        return out

    def _eastmoney() -> list[dict]:
        # Eastmoney's public suggest endpoint. Covers A-shares, HK stocks,
        # US-listed names, ETFs, LOFs, and indices under a single call —
        # the trick is passing a broad `type` mask and mapping MktNum +
        # SecurityType ourselves. `token` is the public web-app token.
        try:
            data = _http_json(
                "https://searchapi.eastmoney.com/api/suggest/get",
                {
                    "input": q,
                    # 14 = 沪深A, 4 = 港股, 5 = 美股, 3 = 指数, 2 = 基金/ETF/LOF.
                    # Comma-joined mask returns them all in one round-trip.
                    "type": "14,4,5,3,2",
                    "count": "15",
                    "token": "D43BF722C8E33BDC906FB84D85E326E8",
                },
                headers={"Referer": "https://www.eastmoney.com/"},
            )
        except Exception:
            return []

        # MktNum -> (yahoo suffix, exchange label) for the "obvious" cases.
        # HK stocks need zero-padding to 4 digits (Yahoo uses "0700.HK",
        # not "700.HK"); US names have no suffix and we pass the symbol
        # through as-is.
        MKT_TABLE = {
            "1":   (".SS", "SSE"),      # 上交所
            "0":   (".SZ", "SZSE"),     # 深交所 (default; sub-classified below)
            "116": (".HK", "HKEX"),     # 港交所
            "105": ("",    "NASDAQ"),   # 纳斯达克
            "106": ("",    "NYSE"),     # 纽交所
            "107": ("",    "AMEX"),     # AMEX
            "153": ("",    "LSE"),      # 伦交所
        }

        out: list[dict] = []
        for x in (data.get("QuotationCodeTable", {}) or {}).get("Data", []) or []:
            code = (x.get("Code") or "").strip()
            market = (x.get("MktNum") or "").strip()
            name = x.get("Name") or ""
            typ = x.get("SecurityTypeName") or ""
            if not code:
                continue

            mapping = MKT_TABLE.get(market)
            if not mapping:
                continue
            suffix, exch = mapping

            if market == "116":
                # HK: pad to 4 digits.
                code = code.zfill(4)
            elif market == "0":
                # Deep-market sub-classification: 8/4=北交所, 300=创业板,
                # 15/16=深市 ETF/LOF, otherwise 主板/中小.
                if code.startswith(("8", "4")):
                    suffix, exch = ".BJ", "BSE"
                elif code.startswith("300") or code.startswith("301"):
                    exch = "ChiNext"
                elif code.startswith(("15", "16")):
                    exch = "SZ-Fund"
            elif market == "1":
                # 沪市: 688 科创板, 5xxxxx ETF/LOF.
                if code.startswith("688"):
                    exch = "SSE-STAR"
                elif code.startswith("5"):
                    exch = "SH-Fund"

            # OTC funds (SecurityTypeName == "基金", no market code) are
            # skipped — they aren't tradable through the trading pipeline.
            if typ == "基金" and market not in {"0", "1"}:
                continue

            out.append({
                "symbol": f"{code}{suffix}",
                "name": f"{name} · {typ}" if typ else name,
                "exch": exch,
            })
        return out

    async def _gather():
        return await asyncio.gather(
            asyncio.to_thread(_yahoo),
            asyncio.to_thread(_eastmoney),
        )

    yahoo, east = asyncio.run(_gather())

    seen: set[str] = set()
    merged: list[dict] = []
    # Eastmoney is the stronger source for A-share/HK numeric codes and
    # any CJK query; Yahoo wins for Latin tickers (US/EU/crypto/ETF).
    prefer_east = q.isdigit() or any("一" <= ch <= "鿿" for ch in q)
    order = (east, yahoo) if prefer_east else (yahoo, east)
    for group in order:
        for it in group:
            sym = it["symbol"]
            if sym in seen:
                continue
            seen.add(sym)
            merged.append(it)
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break
    return {"items": merged}


# Mount static files at root — must come last so /api/* routes above take
# precedence over StaticFiles' catch-all.
_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
