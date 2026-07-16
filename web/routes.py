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
import collections
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time

import requests

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
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

# ── Multi-user access control ────────────────────────────────────────────────
# Hard-coded user list: username → password.
# To add/remove users, edit _USERS below and redeploy.
# To change the shared password update _USERS and redeploy (no env var needed).
_USERS: dict[str, str] = {
    "user1": os.environ.get("TRADINGAGENTS_USER_PASSWORD", "123321"),
    "user2": os.environ.get("TRADINGAGENTS_USER_PASSWORD", "123321"),
    "user3": os.environ.get("TRADINGAGENTS_USER_PASSWORD", "123321"),
}
# Admin accounts: these users can access /admin.
_ADMIN_USERS: set[str] = {"user1"}

_SESSION_SECRET = os.environ.get("TRADINGAGENTS_SESSION_SECRET", "tradingagents-secret-key-change-me")
_MAINTENANCE = os.environ.get("TRADINGAGENTS_MAINTENANCE", "").lower() in ("true", "1", "yes")
_SESSION_COOKIE = "ta_session"
_SESSION_TTL = 86400 * 7   # 7 days

# ── Access log: rolling last 500 entries ─────────────────────────────────────
_access_log: collections.deque = collections.deque(maxlen=500)
_access_lock = threading.Lock()


def _log_request(req: Request, status: int, username: str = "") -> None:
    """Append a slim access-log entry (non-blocking)."""
    forwarded = req.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        req.client.host if req.client else "unknown"
    )
    entry = {
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "ip": ip,
        "user": username,
        "method": req.method,
        "path": req.url.path,
        "status": status,
        "ua": req.headers.get("user-agent", "")[:80],
    }
    with _access_lock:
        _access_log.appendleft(entry)


def _make_session_token(username: str) -> str:
    """Generate a signed token for the given username."""
    return hmac.new(_SESSION_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()


def _get_current_user(request: Request) -> str | None:
    """Return the logged-in username, or None if not authenticated."""
    token = request.cookies.get(_SESSION_COOKIE, "")
    if not token:
        return None
    # Try each known user
    for username in _USERS:
        if hmac.compare_digest(token, _make_session_token(username)):
            return username
    return None


def _is_authenticated(request: Request) -> bool:
    return _get_current_user(request) is not None


def _is_admin(request: Request) -> bool:
    user = _get_current_user(request)
    return user is not None and user in _ADMIN_USERS


def _maintenance_check():
    """Raise 503 if maintenance mode is active (refreshes from env each call)."""
    m = os.environ.get("TRADINGAGENTS_MAINTENANCE", "").lower() in ("true", "1", "yes")
    if m:
        raise HTTPException(status_code=503, detail={
            "error": "maintenance",
            "message": "系统维护中，请稍后再试 / System under maintenance."
        })

# The dashboard doesn't yet expose analyst selection (WEB_FRONTEND_PLAN.md
# P3 3.2) — this matches TradingAgentsGraph's own default so behavior is
# unchanged from before the job model.
_DEFAULT_ANALYSTS = ("market", "social", "news", "fundamentals")

app = FastAPI(title="TradingAgents Web Dashboard")

# ── Auth middleware ──────────────────────────────────────────────────────────
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Always allow: login page, login POST, logout, static assets.
    public = (
        path in ("/login", "/api/login", "/api/logout") or
        path.startswith("/assets/") or
        path.endswith((".js", ".css", ".ico", ".png", ".jpg", ".svg", ".woff2"))
    )
    if public:
        response = await call_next(request)
        _log_request(request, response.status_code)
        return response

    # Require login for everything else.
    user = _get_current_user(request)
    if not user:
        if path.startswith("/api/"):
            _log_request(request, 401)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Redirect browser to login page
        _log_request(request, 302)
        return RedirectResponse(url="/login", status_code=302)

    response = await call_next(request)
    _log_request(request, response.status_code, username=user or "")
    return response

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


# ── Login / Logout ───────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse(_LOGIN_HTML)


@app.post("/api/login")
async def do_login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    correct_password = _USERS.get(username)
    if not correct_password or not hmac.compare_digest(password, correct_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = _make_session_token(username)
    resp = JSONResponse({"ok": True, "username": username, "is_admin": username in _ADMIN_USERS})
    resp.set_cookie(
        key=_SESSION_COOKIE, value=token,
        max_age=_SESSION_TTL, httponly=True, samesite="lax", secure=False,
        path="/",
    )
    return resp


@app.post("/api/logout")
def do_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


# ── Admin panel ──────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_panel(request: Request):
    if not _is_admin(request):
        if not _is_authenticated(request):
            return RedirectResponse("/login")
        # logged in but not admin
        return HTMLResponse("<html><body style='background:#080A0C;color:#FF453A;font-family:sans-serif;padding:40px'>"
                            "<h2>⛔ 无权限</h2><p>当前账号没有管理员权限。</p>"
                            "<p><a href='/' style='color:#0A84FF'>返回首页</a></p></body></html>", status_code=403)
    user = _get_current_user(request)
    return HTMLResponse(_ADMIN_HTML.replace("__CURRENT_USER__", user or ""))


@app.get("/api/admin/status")
def admin_status(request: Request):
    """Summary metrics for the admin dashboard."""
    if not _is_admin(request):
        raise HTTPException(403)
    with _access_lock:
        log_snapshot = list(_access_log)

    # IP summary
    ip_counts: dict[str, int] = {}
    for e in log_snapshot:
        ip = e["ip"]
        ip_counts[ip] = ip_counts.get(ip, 0) + 1
    top_ips = sorted(ip_counts.items(), key=lambda x: -x[1])[:20]

    # Recent requests (latest 50)
    recent = log_snapshot[:50]

    job = registry.running_job()
    return {
        "maintenance": os.environ.get("TRADINGAGENTS_MAINTENANCE", "").lower() in ("true", "1", "yes"),
        "current_user": _get_current_user(request),
        "users": list(_USERS.keys()),
        "admin_users": list(_ADMIN_USERS),
        "total_requests": len(log_snapshot),
        "top_ips": [{"ip": ip, "count": n} for ip, n in top_ips],
        "recent_requests": recent,
        "running_job": {
            "id": job.id,
            "ticker": job.ticker,
            "trade_date": job.trade_date,
            "status": job.status(),
        } if job else None,
    }


@app.post("/api/admin/maintenance")
async def set_maintenance(request: Request):
    """Toggle maintenance mode. Body: {\"on\": true|false}"""
    if not _is_admin(request):
        raise HTTPException(403)
    body = await request.json()
    on = bool(body.get("on", True))
    os.environ["TRADINGAGENTS_MAINTENANCE"] = "true" if on else "false"
    logger.warning("Maintenance mode %s by admin", "ON" if on else "OFF")
    return {"maintenance": on}


@app.post("/api/admin/cancel")
def admin_cancel_job(request: Request):
    """Force-cancel the currently running job."""
    if not _is_admin(request):
        raise HTTPException(401)
    job = registry.running_job()
    if not job:
        return {"ok": False, "message": "没有正在运行的任务"}
    job.cancel_requested.set()
    logger.warning("Admin force-cancelled job %s", job.id)
    return {"ok": True, "job_id": job.id}


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
    _maintenance_check()          # returns 503 if maintenance mode is on
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
        # Skip Eastmoney entirely for pure-ASCII-letter queries — its
        # suggest endpoint matches on pinyin initials, so `ar` returns
        # 奥瑞德 (ARD) / 奥锐特 (ART) / 奥瑞金 (ARJ) etc., which is pure
        # noise when the user is typing an English ticker like AAPL. Only
        # digits (A-share numeric codes) or CJK characters warrant it.
        has_cjk = any("一" <= ch <= "鿿" for ch in q)
        use_east = q.isdigit() or has_cjk
        if use_east:
            return await asyncio.gather(
                asyncio.to_thread(_yahoo),
                asyncio.to_thread(_eastmoney),
            )
        return [await asyncio.to_thread(_yahoo), []]

    yahoo, east = asyncio.run(_gather())

    seen: set[str] = set()
    merged: list[dict] = []
    # Eastmoney wins when the query is numeric or CJK (A-share / HK by
    # code, or Chinese company name); Yahoo wins for Latin tickers.
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


# ── Inline HTML templates ────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingAgents · 登录</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#080A0C;color:#E6EDF3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#101418;border:1px solid #212830;border-radius:12px;padding:40px 36px;width:360px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
  h1{font-size:18px;font-weight:600;letter-spacing:-.02em;margin-bottom:6px;display:flex;align-items:center;gap:10px}
  .logo{width:22px;height:22px;background:#14151A;border-radius:6px;display:inline-flex;align-items:center;justify-content:center}
  .dot{width:8px;height:8px;border-radius:50%;background:#B98029}
  p{color:#8B949E;font-size:13px;margin-bottom:28px}
  label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:#8B949E;display:block;margin-bottom:6px}
  .field{margin-bottom:14px}
  input{width:100%;background:#161B22;border:1px solid #212830;border-radius:6px;color:#E6EDF3;padding:10px 12px;font-size:14px;outline:none;transition:border-color .2s}
  input:focus{border-color:#B98029}
  button{width:100%;margin-top:20px;background:#FF2D55;border:none;border-radius:6px;color:#fff;font-size:14px;font-weight:600;padding:11px;cursor:pointer;transition:opacity .15s}
  button:hover{opacity:.9}
  .err{color:#FF453A;font-size:13px;margin-top:12px;display:none}
</style>
</head>
<body>
<div class="card">
  <h1><span class="logo"><span class="dot"></span></span>TradingAgents</h1>
  <p>请输入用户名和密码</p>
  <div class="field">
    <label for="un">用户名</label>
    <input id="un" type="text" placeholder="user1" autocomplete="username">
  </div>
  <div class="field">
    <label for="pw">密码</label>
    <input id="pw" type="password" placeholder="••••••••" autocomplete="current-password">
  </div>
  <button onclick="login()">进入工作站</button>
  <div class="err" id="err">用户名或密码错误，请重试</div>
</div>
<script>
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
document.getElementById('un').addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('pw').focus()});
async function login(){
  const un=document.getElementById('un').value.trim();
  const pw=document.getElementById('pw').value;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:un,password:pw})});
  if(r.ok){location.href='/';}
  else{const e=document.getElementById('err');e.style.display='block';}
}
</script>
</body></html>"""


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingAgents · 管理后台</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#080A0C;color:#E6EDF3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;padding:24px}
  h1{font-size:20px;font-weight:600;margin-bottom:4px}
  .sub{color:#8B949E;font-size:13px;margin-bottom:24px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;margin-bottom:24px}
  .kpi{background:#101418;border:1px solid #212830;border-radius:10px;padding:16px 18px}
  .kpi .lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:#8B949E;font-weight:600;margin-bottom:8px}
  .kpi .val{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
  .card{background:#101418;border:1px solid #212830;border-radius:10px;padding:18px 20px;margin-bottom:16px}
  .card h2{font-size:13px;font-weight:600;margin-bottom:12px;color:#E6EDF3}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{color:#8B949E;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;text-align:left;padding:6px 8px;border-bottom:1px solid #212830}
  td{padding:7px 8px;border-bottom:1px solid rgba(255,255,255,.04);font-variant-numeric:tabular-nums}
  tr:hover td{background:rgba(255,255,255,.02)}
  .badge{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:500}
  .ok{background:rgba(47,125,90,.2);color:#30D158}
  .warn{background:rgba(255,149,0,.15);color:#FF9500}
  .err{background:rgba(255,69,58,.15);color:#FF453A}
  .actions{display:flex;gap:10px;margin-bottom:24px;flex-wrap:wrap}
  .btn{padding:8px 16px;border-radius:6px;border:1px solid #212830;background:#161B22;color:#E6EDF3;font-size:13px;font-weight:500;cursor:pointer;transition:background .15s}
  .btn:hover{background:#1c2128}
  .btn.danger{background:rgba(255,69,58,.15);border-color:rgba(255,69,58,.35);color:#FF453A}
  .btn.danger:hover{background:rgba(255,69,58,.25)}
  .btn.amber{background:rgba(185,128,41,.15);border-color:rgba(185,128,41,.35);color:#B98029}
  .status-dot{width:8px;height:8px;border-radius:50%;background:#30D158;box-shadow:0 0 0 3px rgba(47,125,90,.2)}
  .status-dot.off{background:#FF453A;box-shadow:0 0 0 3px rgba(255,69,58,.2)}
</style>
</head>
<body>
<h1>🛡 管理后台</h1>
<p class="sub" id="sub-time">加载中…</p>

<div class="actions">
  <button class="btn" onclick="location.href='/'">← 返回工作站</button>
  <button class="btn amber" id="btn-maintenance" onclick="toggleMaintenance()">开启维护模式</button>
  <button class="btn danger" onclick="cancelJob()">强制终止当前任务</button>
  <button class="btn" onclick="logout()">退出登录</button>
</div>

<div class="grid" id="kpi-grid"></div>

<div class="card">
  <h2>正在运行的任务</h2>
  <div id="job-info" style="color:#8B949E;font-size:13px">暂无运行中的任务</div>
</div>

<div class="card">
  <h2>访问最多的 IP（前 20）</h2>
  <table><thead><tr><th>IP</th><th>请求数</th></tr></thead><tbody id="ip-tbody"></tbody></table>
</div>

<div class="card">
  <h2>最近 50 条请求</h2>
  <table>
    <thead><tr><th>时间</th><th>用户</th><th>IP</th><th>方法</th><th>路径</th><th>状态</th></tr></thead>
    <tbody id="log-tbody"></tbody>
  </table>
</div>

<script>
let maintenanceOn = false;

async function load(){
  const r = await fetch('/api/admin/status');
  if(!r.ok){ document.body.innerHTML='<p style="color:#FF453A;padding:40px">无权限</p>'; return; }
  const d = await r.json();
  maintenanceOn = d.maintenance;

  document.getElementById('sub-time').textContent =
    '当前用户：' + (d.current_user||'?') + ' · 刷新于 ' + new Date().toLocaleTimeString('zh-CN');

  // KPI
  document.getElementById('kpi-grid').innerHTML = `
    <div class="kpi"><div class="lbl">访问总数</div><div class="val">${d.total_requests}</div></div>
    <div class="kpi"><div class="lbl">独立 IP</div><div class="val">${d.top_ips.length}</div></div>
    <div class="kpi"><div class="lbl">用户数</div><div class="val">${(d.users||[]).length}</div></div>
    <div class="kpi"><div class="lbl">维护模式</div><div class="val" style="font-size:16px;margin-top:4px">
      <span class="status-dot ${d.maintenance?'off':''}"></span></div></div>`;

  // Users list
  const usersGrid = document.getElementById('users-grid') || (() => {
    const g = document.createElement('div'); g.id='users-grid';
    g.style.cssText='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px';
    document.getElementById('kpi-grid').insertAdjacentElement('afterend', g);
    return g;
  })();
  usersGrid.innerHTML = (d.users||[]).map(u=>`
    <span style="padding:4px 10px;border-radius:999px;font-size:12px;
      background:${(d.admin_users||[]).includes(u)?'rgba(185,128,41,.2)':'rgba(255,255,255,.05)'};
      border:1px solid ${(d.admin_users||[]).includes(u)?'rgba(185,128,41,.4)':'rgba(255,255,255,.08)'};
      color:${(d.admin_users||[]).includes(u)?'#FFB84D':'#C7CDD5'}">
      ${u}${(d.admin_users||[]).includes(u)?' 👑':''}
    </span>`).join('');

  // Maintenance button text
  document.getElementById('btn-maintenance').textContent =
    d.maintenance ? '关闭维护模式 ✓' : '开启维护模式';
  document.getElementById('btn-maintenance').className =
    d.maintenance ? 'btn danger' : 'btn amber';

  // Running job
  const ji = document.getElementById('job-info');
  if(d.running_job){
    ji.innerHTML = `<span class="badge ok">运行中</span>&nbsp;
      <strong>${d.running_job.ticker}</strong> &nbsp;
      ${d.running_job.trade_date} &nbsp;
      <code style="font-size:11px;color:#8B949E">${d.running_job.id}</code>`;
  } else {
    ji.textContent = '暂无运行中的任务';
  }

  // IPs
  document.getElementById('ip-tbody').innerHTML =
    d.top_ips.map(x=>`<tr><td>${x.ip}</td><td>${x.count}</td></tr>`).join('');

  // Log
  document.getElementById('log-tbody').innerHTML =
    d.recent_requests.map(e=>{
      const sc = e.status;
      const cls = sc>=500?'err':sc>=400?'warn':'ok';
      return `<tr>
        <td>${e.ts}</td>
        <td style="color:#B98029;font-weight:500">${e.user||'—'}</td>
        <td>${e.ip}</td><td>${e.method}</td>
        <td style="font-family:monospace">${e.path}</td>
        <td><span class="badge ${cls}">${e.status}</span></td>
      </tr>`;
    }).join('');
}

async function toggleMaintenance(){
  const on = !maintenanceOn;
  if(on && !confirm('确认开启维护模式？开启后所有分析请求将返回 503。')) return;
  await fetch('/api/admin/maintenance', {method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({on})});
  load();
}

async function cancelJob(){
  if(!confirm('确认强制终止当前正在运行的分析任务？')) return;
  const r = await fetch('/api/admin/cancel', {method:'POST'});
  const d = await r.json();
  alert(d.ok ? '任务已终止' : d.message);
  load();
}

async function logout(){
  await fetch('/api/logout',{method:'POST'});
  location.href='/login';
}

load();
setInterval(load, 10000);  // auto-refresh every 10s
</script>
</body></html>"""


# Mount static files at root — must come last so /api/* routes above take
# precedence over StaticFiles' catch-all.
_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
