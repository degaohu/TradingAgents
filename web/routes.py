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
import html
import json
import logging
import os
import re
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

from . import activity, history, mailer, quota, registration, users  # noqa: E402
from .decision import build_decision_summary  # noqa: E402
from .jobs import Job, JobAlreadyRunning, JobRegistry  # noqa: E402
from .pipeline import PipelineTracker, build_stage_specs  # noqa: E402
from .polish import generate_polished_report  # noqa: E402
from .version import get_version  # noqa: E402

logger = logging.getLogger("TradingAgentsWebServer")


def get_stock_details(ticker: str) -> dict:
    ticker_upper = ticker.strip().upper()
    
    # Pre-configured lookup dictionary for instant response
    STOCK_DB = {
        "NVDA": {"zh": "英伟达", "en": "NVIDIA Corporation", "abbr": "NVDA"},
        "AAPL": {"zh": "苹果公司", "en": "Apple Inc.", "abbr": "AAPL"},
        "TSLA": {"zh": "特斯拉", "en": "Tesla Inc.", "abbr": "TSLA"},
        "MSFT": {"zh": "微软公司", "en": "Microsoft Corporation", "abbr": "MSFT"},
        "AMZN": {"zh": "亚马逊", "en": "Amazon.com Inc.", "abbr": "AMZN"},
        "GOOG": {"zh": "谷歌公司", "en": "Alphabet Inc.", "abbr": "GOOG"},
        "GOOGL": {"zh": "谷歌公司", "en": "Alphabet Inc.", "abbr": "GOOGL"},
        "ARM": {"zh": "安谋控股", "en": "ARM Holdings plc", "abbr": "ARM"},
        "AVGO": {"zh": "博通公司", "en": "Broadcom Inc.", "abbr": "AVGO"},
        "META": {"zh": "脸书集团", "en": "Meta Platforms Inc.", "abbr": "META"},
        "NFLX": {"zh": "奈飞公司", "en": "Netflix Inc.", "abbr": "NFLX"},
        "AMD": {"zh": "超威半导体", "en": "Advanced Micro Devices", "abbr": "AMD"},
        "INTC": {"zh": "英特尔", "en": "Intel Corporation", "abbr": "INTC"},
        "QCOM": {"zh": "高通公司", "en": "Qualcomm Inc.", "abbr": "QCOM"},
        "ASML": {"zh": "阿斯麦", "en": "ASML Holding N.V.", "abbr": "ASML"},
        "TSM": {"zh": "台积电", "en": "Taiwan Semiconductor Manufacturing", "abbr": "TSM"},
        "BABA": {"zh": "阿里巴巴", "en": "Alibaba Group Holding", "abbr": "BABA"},
        "PDD": {"zh": "拼多多", "en": "PDD Holdings Inc.", "abbr": "PDD"},
        "JD": {"zh": "京东集团", "en": "JD.com Inc.", "abbr": "JD"},
        "TCEHY": {"zh": "腾讯控股", "en": "Tencent Holdings Ltd.", "abbr": "TCEHY"},
        
        # A-shares from user's screenshots and common benchmarks
        "300487.SZ": {"zh": "芒果超媒", "en": "Mango Excellent Media", "abbr": "MGCM"},
        "601088.SS": {"zh": "中国神华", "en": "China Shenhua Energy", "abbr": "ZGSH"},
        "300567.SZ": {"zh": "爱乐达", "en": "Chengdu Aileda Aerospace", "abbr": "ALD"},
        "600519.SS": {"zh": "贵州茅台", "en": "Kweichow Moutai", "abbr": "GZMT"},
        "000858.SZ": {"zh": "五粮液", "en": "Wuliangye Yibin", "abbr": "WLY"},
        "300750.SZ": {"zh": "宁德时代", "en": "CATL", "abbr": "NDSD"},
        "601318.SS": {"zh": "中国平安", "en": "Ping An Insurance", "abbr": "ZGPA"},
        "600036.SS": {"zh": "招商银行", "en": "China Merchants Bank", "abbr": "ZSYH"},
        "000001.SZ": {"zh": "平安银行", "en": "Ping An Bank", "abbr": "PAYH"},
        "600900.SS": {"zh": "长江电力", "en": "China Yangtze Power", "abbr": "CJDL"},
        "000333.SZ": {"zh": "美的集团", "en": "Midea Group", "abbr": "MDJT"},
        "000651.SZ": {"zh": "格力电器", "en": "Gree Electric Appliances", "abbr": "GLDQ"},
        "601166.SS": {"zh": "兴业银行", "en": "Industrial Bank", "abbr": "XYYH"},
        "600030.SS": {"zh": "中信证券", "en": "CITIC Securities", "abbr": "ZXZQ"},
        "002475.SZ": {"zh": "立讯精密", "en": "Luxshare Precision", "abbr": "LXJM"},
        "002594.SZ": {"zh": "比亚迪", "en": "BYD Company", "abbr": "BYD"},
        "601888.SS": {"zh": "中国中免", "en": "China Tourism Duty Free", "abbr": "ZGZM"},
        "600276.SS": {"zh": "恒瑞医药", "en": "Jiangsu Hengrui Medicine", "abbr": "HRYY"},
        "000725.SZ": {"zh": "京东方A", "en": "BOE Technology Group", "abbr": "JDF"},
        "300059.SZ": {"zh": "东方财富", "en": "East Money Information", "abbr": "DFCF"},
    }
    
    # Try matching base symbol
    if ticker_upper in STOCK_DB:
        return STOCK_DB[ticker_upper]
        
    prefix = ticker_upper.split(".")[0]
    if prefix in STOCK_DB:
        return STOCK_DB[prefix]
        
    if ticker_upper.endswith((".SS", ".SZ", ".BJ")):
        return {"zh": f"中国A股 ({prefix})", "en": f"A-Share {ticker_upper}", "abbr": prefix}
    else:
        return {"zh": ticker_upper, "en": ticker_upper, "abbr": ticker_upper}


# ── Multi-user access control ────────────────────────────────────────────────
# Hard-coded user list: username → password. Each user has its own env var
# so distinct accounts actually have distinct credentials — three users
# sharing one password (the previous default) means the admin activity log's
# per-username attribution is meaningless, since anyone who knows the one
# password can sign in as any of the three names.
#
# These are the SEED only. On first run they're inserted into the users DB
# (web/users.py) via ensure_seeded(); after that the DB is the source of
# truth — passwords are managed from the admin panel and persist across
# deploys (changing an env var afterwards no longer overrides a DB value).
_USERS: dict[str, str] = {
    "admin": os.environ.get("TRADINGAGENTS_ADMIN_PASSWORD", "changeme-admin"),
    "user2": os.environ.get("TRADINGAGENTS_USER2_PASSWORD", "changeme-user2"),
    "user3": os.environ.get("TRADINGAGENTS_USER3_PASSWORD", "changeme-user3"),
}
# Admin accounts (seed): these users start with the admin flag set.
_ADMIN_USERS: set[str] = {"admin"}


def _seed_users() -> None:
    """Idempotent: seeds the users DB from _USERS on first call per DB."""
    users.ensure_seeded(_USERS, _ADMIN_USERS)

_SESSION_SECRET = os.environ.get("TRADINGAGENTS_SESSION_SECRET", "tradingagents-secret-key-change-me")
_MAINTENANCE = os.environ.get("TRADINGAGENTS_MAINTENANCE", "").lower() in ("true", "1", "yes")
_SESSION_COOKIE = "ta_session"
_SESSION_TTL = 86400 * 30   # 30 days (persistent "remember me" cookie)

# ── Access log: rolling last 500 entries ─────────────────────────────────────
_access_log: collections.deque = collections.deque(maxlen=500)
_access_lock = threading.Lock()


# ── Live User Sessions tracking ──────────────────────────────────────────────
_live_sessions_lock = threading.Lock()
_live_sessions: dict[str, dict] = {}


def _cleanup_live_sessions() -> None:
    now = time.time()
    with _live_sessions_lock:
        stale = [sid for sid, s in _live_sessions.items() if now - s["last_seen"] > 25]
        for sid in stale:
            _live_sessions.pop(sid, None)


def _client_ip(req: Request) -> str:
    forwarded = req.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() if forwarded else (
        req.client.host if req.client else "unknown"
    )


# ── Self-registration ────────────────────────────────────────────────────────
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# What a freshly-verified self-registration starts with — deliberately
# smaller than quota.DEFAULT_QUOTA (used for admin-created accounts), since
# this reward is meant as a trial, not a full allocation.
NEW_USER_BONUS_QUOTA = int(os.environ.get("TRADINGAGENTS_NEW_USER_BONUS_QUOTA") or 2)

# In-memory sliding-window abuse guard for registration/resend — this is a
# single-process deployment (see JobRegistry's docstring), so an in-memory
# dict is sufficient and resets harmlessly on restart.
_rate_limit_lock = threading.Lock()
_rate_limit_hits: dict[str, list[float]] = {}


def _rate_limited(bucket: str, key: str, max_count: int, window_seconds: float) -> bool:
    """True if `key` (an IP or email) has hit `bucket` more than `max_count`
    times in the trailing `window_seconds`."""
    now = time.time()
    full_key = f"{bucket}:{key}"
    with _rate_limit_lock:
        hits = [t for t in _rate_limit_hits.get(full_key, []) if now - t < window_seconds]
        hits.append(now)
        _rate_limit_hits[full_key] = hits
        return len(hits) > max_count


def _public_base_url(request: Request) -> str:
    """Base URL to build the verification link from. Prefer an explicit env
    var — Railway (and most PaaS reverse proxies) terminate TLS in front of
    this process, so request.url.scheme alone can't be trusted to say
    "https"; X-Forwarded-Proto/Host are the fallback when it's unset."""
    configured = os.environ.get("TRADINGAGENTS_PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _verification_email_html(username: str, verify_url: str) -> str:
    safe_username = html.escape(username)
    return f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    color:#333;line-height:1.6;padding:20px;background-color:#f7f9fa;">
      <div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e1e4e6;border-radius:8px;
      padding:30px;box-shadow:0 4px 12px rgba(0,0,0,0.05);">
        <h1 style="font-size:20px;margin:0 0 16px;color:#111;">■ TradingAgents 账户验证</h1>
        <p>你好 {safe_username}，</p>
        <p>请点击下方按钮验证邮箱并激活您的账户（验证成功后将自动获得
        {NEW_USER_BONUS_QUOTA} 次免费分析额度）：</p>
        <p style="text-align:center;margin:28px 0;">
          <a href="{verify_url}" style="display:inline-block;background:#FF2D55;color:#fff;text-decoration:none;
          padding:12px 28px;border-radius:6px;font-weight:600;">验证邮箱</a>
        </p>
        <p style="font-size:12px;color:#999;">如果按钮无法点击，请复制以下链接到浏览器打开：<br>{verify_url}</p>
        <p style="font-size:12px;color:#999;">此链接 24 小时内有效。如果这不是您本人的操作，请忽略此邮件。</p>
      </div>
    </body></html>
    """


def _log_request(req: Request, status: int, username: str = "") -> None:
    """Append a slim access-log entry (non-blocking)."""
    ip = _client_ip(req)
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
    _seed_users()
    # The session token is hmac(secret, username); find whose it is. (Note:
    # it doesn't depend on the password, so a password reset doesn't
    # invalidate existing sessions — the user just needs the new password on
    # their next sign-in.)
    for username in users.list_usernames():
        if hmac.compare_digest(token, _make_session_token(username)):
            return username
    return None


def _is_authenticated(request: Request) -> bool:
    return _get_current_user(request) is not None


def _is_admin(request: Request) -> bool:
    user = _get_current_user(request)
    return user is not None and users.is_admin(user)


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

    # Always allow: login page, login POST, logout, static assets, PWA meta,
    # heartbeat, and self-registration (form, submit, email verification link,
    # resend) — a brand new visitor has no session yet by definition.
    public = (
        path in (
            "/login", "/api/login", "/api/logout", "/manifest.json", "/sw.js", "/api/heartbeat",
            "/register", "/api/register", "/api/register/resend", "/verify-email",
        ) or
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
    _seed_users()
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not users.verify(username, password):
        activity.log_activity(username or None, "login_failed", ip=_client_ip(request))
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    activity.log_activity(username, "login", ip=_client_ip(request))
    token = _make_session_token(username)
    resp = JSONResponse({"ok": True, "username": username, "is_admin": users.is_admin(username)})
    # "保持登录" (remember me): a persistent cookie that survives browser
    # restarts for 30 days. Unchecked → a session cookie (max_age=None) that
    # the browser drops when it closes.
    remember = bool(body.get("remember", True))
    resp.set_cookie(
        key=_SESSION_COOKIE, value=token,
        max_age=_SESSION_TTL if remember else None,
        httponly=True, samesite="lax", secure=False, path="/",
    )
    return resp


@app.post("/api/logout")
def do_logout(request: Request):
    user = _get_current_user(request)
    if user:
        activity.log_activity(user, "logout", ip=_client_ip(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


# ── Self-registration (email-verified) ───────────────────────────────────────

@app.get("/register", response_class=HTMLResponse, include_in_schema=False)
def register_page():
    return HTMLResponse(_REGISTER_HTML)


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str


@app.post("/api/register")
def register(req: RegisterRequest, request: Request):
    _seed_users()
    ip = _client_ip(request)
    if _rate_limited("register-ip", ip, max_count=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="注册请求过于频繁，请稍后再试。")

    username = req.username.strip().lower()
    email = req.email.strip().lower()
    password = req.password

    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="用户名需为 3-32 位字母、数字、下划线或连字符。")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="请输入有效的邮箱地址。")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码至少需要 8 个字符。")
    if users.exists(username):
        raise HTTPException(status_code=409, detail="用户名已被占用。")
    if users.email_exists(email):
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录。")
    if _rate_limited("register-email", email, max_count=3, window_seconds=3600):
        raise HTTPException(status_code=429, detail="该邮箱的注册请求过于频繁，请稍后再试。")
    if not mailer.is_configured():
        raise HTTPException(status_code=501, detail="邮件发送服务未配置，暂时无法注册，请联系管理员。")

    password_hash = users.hash_password(password)
    token = registration.create(username, email, password_hash)
    verify_url = f"{_public_base_url(request)}/verify-email?token={token}"

    try:
        mailer.send_email(email, "验证您的 TradingAgents 账户", _verification_email_html(username, verify_url))
    except mailer.MailerNotConfigured:
        raise HTTPException(status_code=501, detail="邮件发送服务未配置，暂时无法注册，请联系管理员。")
    except Exception:
        logger.exception("Failed to send verification email to %s", email)
        raise HTTPException(status_code=502, detail="验证邮件发送失败，请稍后重试或使用重新发送功能。")

    activity.log_activity(username, "register_pending", detail=email, ip=ip)
    return {"ok": True, "message": "验证邮件已发送，请查收并点击链接完成注册。"}


class ResendVerificationRequest(BaseModel):
    identifier: str  # username or email


@app.post("/api/register/resend")
def resend_verification(req: ResendVerificationRequest, request: Request):
    ip = _client_ip(request)
    if _rate_limited("resend-ip", ip, max_count=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试。")
    identifier = req.identifier.strip().lower()
    if _rate_limited("resend-identifier", identifier, max_count=3, window_seconds=3600):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试。")

    generic_response = {"ok": True, "message": "如果该邮箱/用户名存在待验证的注册，验证邮件已重新发送。"}
    pending = registration.find_by_identifier(identifier)
    if pending is None:
        # Don't reveal whether a pending registration exists for this identifier.
        return generic_response

    token = registration.create(pending["username"], pending["email"], pending["password_hash"])
    verify_url = f"{_public_base_url(request)}/verify-email?token={token}"
    try:
        mailer.send_email(
            pending["email"], "验证您的 TradingAgents 账户",
            _verification_email_html(pending["username"], verify_url),
        )
    except Exception:
        logger.exception("Failed to resend verification email to %s", pending["email"])
    return generic_response


@app.get("/verify-email", response_class=HTMLResponse, include_in_schema=False)
def verify_email(token: str, request: Request):
    _seed_users()
    pending = registration.consume(token)
    if pending is None:
        return HTMLResponse(_verify_result_html("error", "验证链接无效或已过期，请重新注册。"))

    username = pending["username"]
    email = pending["email"]
    password_hash = pending["password_hash"]

    # Rare race: someone else claimed this username/email while the link sat
    # unused (e.g. an admin created it manually in the meantime).
    if users.exists(username):
        return HTMLResponse(_verify_result_html("error", "该用户名已被占用，请使用其他用户名重新注册。"))
    if users.email_exists(email):
        return HTMLResponse(_verify_result_html("error", "该邮箱已注册，请直接登录。"))

    users.create_verified_user(username, password_hash, email, is_admin=False)
    quota.set_remaining(username, NEW_USER_BONUS_QUOTA)
    activity.log_activity(username, "register_verified", detail=email, ip=_client_ip(request))

    resp = HTMLResponse(_verify_result_html(
        "success", f"验证成功！已为您自动登录，并赠送 {NEW_USER_BONUS_QUOTA} 次免费分析额度。",
    ))
    resp.set_cookie(
        key=_SESSION_COOKIE, value=_make_session_token(username),
        max_age=_SESSION_TTL,
        httponly=True, samesite="lax", secure=False, path="/",
    )
    return resp


@app.get("/api/me")
def whoami(request: Request):
    """Current session identity for the client shell (account bar: username,
    logout, and the admin-panel link shown only when is_admin is true).
    Unlike /api/admin/status, any logged-in user can call this — the auth
    middleware already rejects the request before this handler runs if
    there's no valid session.

    Also carries this user's active job id, if any — the frontend's saved
    job id lives in localStorage (device/browser-specific), so a page
    reload on the *same* device already resumes without this. This field
    is what lets a *different* device/browser the same user logs into
    also discover and reattach to a run they started elsewhere.
    """
    user = _get_current_user(request)
    running = registry.running_job()
    active_job_id = running.id if (running is not None and running.started_by == user) else None
    is_admin = users.is_admin(user) if user else False
    return {
        "username": user,
        "is_admin": is_admin,
        "active_job_id": active_job_id,
        "version": get_version(),
        # Remaining report quota. null = unlimited (admins are exempt).
        "quota": None if (is_admin or not user) else quota.get_remaining(user),
        "quota_low_threshold": quota.LOW_QUOTA_THRESHOLD,
    }


@app.post("/api/heartbeat")
async def api_heartbeat(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "invalid_json"}, status_code=400)

    session_id = data.get("session_id")
    if not session_id:
        return JSONResponse({"status": "error", "message": "missing_session_id"}, status_code=400)

    username = _get_current_user(request) or "Guest"
    ip = _client_ip(request)
    view = data.get("view", "unknown")
    ticker = data.get("ticker", "")
    duration = int(data.get("duration", 0))

    now = time.time()
    with _live_sessions_lock:
        if session_id not in _live_sessions:
            _live_sessions[session_id] = {
                "started_at": now,
                "username": username,
                "ip": ip,
            }
        s = _live_sessions[session_id]
        s["username"] = username
        s["ip"] = ip
        s["view"] = view
        s["ticker"] = ticker
        s["duration"] = duration
        s["last_seen"] = now

    return JSONResponse({"status": "ok"})


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
    _seed_users()
    all_users = users.all_users()
    usernames = [u["username"] for u in all_users]
    admin_set = {u["username"] for u in all_users if u["is_admin"]}
    passwords = {u["username"]: u["password"] for u in all_users}
    emails = {u["username"]: u["email"] for u in all_users}
    remaining = quota.all_remaining(usernames)
    user_summary = activity.user_activity_summary(usernames)
    for row in user_summary:
        name = row["username"]
        # null quota = unlimited (admins are exempt from the report limit)
        row["quota"] = None if name in admin_set else remaining.get(name)
        # Plaintext password — shown only to admins (this endpoint is
        # admin-gated). Deliberate operator choice; see web/users.py. Self-
        # registered accounts are hashed instead (real passwords aren't
        # recoverable from the DB for those) — show a placeholder instead
        # of a raw hash string.
        pw = passwords.get(name)
        row["password"] = "（已加密，无法查看）" if pw and users.is_hashed(pw) else pw
        row["email"] = emails.get(name)

    # Enrich recent activity with IP regions
    recent_activity = activity.recent_activity(100)
    for act in recent_activity:
        act["region"] = activity.get_ip_region(act.get("ip", ""))

    # Enrich top IPs with regions
    top_ips_enriched = []
    for ip, n in top_ips:
        top_ips_enriched.append({
            "ip": ip,
            "count": n,
            "region": activity.get_ip_region(ip)
        })

    # Assemble live active visitors info
    _cleanup_live_sessions()
    live_users_list = []
    now = time.time()
    with _live_sessions_lock:
        for sid, s in _live_sessions.items():
            dwell = int(now - s["started_at"])
            live_users_list.append({
                "username": s["username"],
                "ip": s["ip"],
                "region": activity.get_ip_region(s["ip"]),
                "view": s["view"],
                "ticker": s["ticker"],
                "duration": s["duration"],
                "dwell": dwell,
                "last_seen_ago": int(now - s["last_seen"])
            })

    return {
        "maintenance": os.environ.get("TRADINGAGENTS_MAINTENANCE", "").lower() in ("true", "1", "yes"),
        "current_user": _get_current_user(request),
        "users": usernames,
        "admin_users": list(admin_set),
        "user_summary": user_summary,
        "recent_activity": recent_activity,
        "total_requests": len(log_snapshot),
        "top_ips": top_ips_enriched,
        "recent_requests": [{**e, "region": activity.get_ip_region(e["ip"])} for e in recent],
        "live_users": live_users_list,
        "running_job": {
            "id": job.id,
            "ticker": job.ticker,
            "trade_date": job.trade_date,
            "status": job.status,
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
    activity.log_activity(
        _get_current_user(request), "maintenance_toggle",
        detail="ON" if on else "OFF", ip=_client_ip(request),
    )
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
    activity.log_activity(
        _get_current_user(request), "admin_force_cancel",
        detail=f"{job.ticker} @ {job.trade_date} (job {job.id})", ip=_client_ip(request),
    )
    return {"ok": True, "job_id": job.id}


@app.post("/api/admin/quota")
async def set_user_quota(request: Request):
    """Adjust a user's report quota. Body: {"username": str, "add": int} to
    add/subtract, or {"username": str, "set": int} for an absolute value."""
    if not _is_admin(request):
        raise HTTPException(403)
    body = await request.json()
    username = (body.get("username") or "").strip()
    if not users.exists(username):
        raise HTTPException(status_code=404, detail="unknown user")
    if users.is_admin(username):
        raise HTTPException(status_code=400, detail="admins have unlimited quota")
    if "set" in body:
        new_remaining = quota.set_remaining(username, int(body["set"]))
        detail = f"{username} set to {new_remaining}"
    else:
        delta = int(body.get("add", 0))
        new_remaining = quota.add(username, delta)
        detail = f"{username} {'+' if delta >= 0 else ''}{delta} -> {new_remaining}"
    activity.log_activity(
        _get_current_user(request), "quota_adjust", detail=detail, ip=_client_ip(request),
    )
    return {"username": username, "remaining": new_remaining}


@app.post("/api/admin/password")
async def reset_user_password(request: Request):
    """Set a user's password. Body: {"username": str, "password": str}.
    Takes effect on the user's next sign-in (existing sessions are keyed on
    username, not password, so they aren't forcibly logged out)."""
    if not _is_admin(request):
        raise HTTPException(403)
    body = await request.json()
    username = (body.get("username") or "").strip()
    new_password = body.get("password") or ""
    if not users.exists(username):
        raise HTTPException(status_code=404, detail="unknown user")
    if not new_password:
        raise HTTPException(status_code=400, detail="password must not be empty")
    users.set_password(username, new_password)
    activity.log_activity(
        _get_current_user(request), "password_reset", detail=username, ip=_client_ip(request),
    )
    return {"username": username, "ok": True}


@app.post("/api/admin/users")
async def create_user_route(request: Request):
    """Create a new user. Body: {"username": str, "password": str,
    "is_admin": bool}. Idempotent by username — fails if taken."""
    if not _is_admin(request):
        raise HTTPException(403)
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    make_admin = bool(body.get("is_admin"))
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password must both be non-empty")
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(status_code=400, detail="username must be alphanumeric (plus _ - .)")
    if not users.create_user(username, password, is_admin=make_admin):
        raise HTTPException(status_code=409, detail="username already exists")
    activity.log_activity(
        _get_current_user(request), "user_create",
        detail=f"{username}{' (admin)' if make_admin else ''}",
        ip=_client_ip(request),
    )
    return {"username": username, "is_admin": make_admin, "ok": True}


@app.delete("/api/admin/users/{username}")
def delete_user_route(username: str, request: Request):
    """Delete a user. Refuses to delete the currently-signed-in admin
    (would immediately lock them out of the panel) or the last remaining
    admin (would strand the deployment with no way in)."""
    if not _is_admin(request):
        raise HTTPException(403)
    current = _get_current_user(request)
    if username == current:
        raise HTTPException(status_code=400, detail="cannot delete the currently signed-in account")
    if not users.exists(username):
        raise HTTPException(status_code=404, detail="unknown user")
    if users.is_admin(username) and len(users.admin_usernames()) <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last remaining admin")
    users.delete_user(username)
    activity.log_activity(
        current, "user_delete", detail=username, ip=_client_ip(request),
    )
    return {"username": username, "ok": True}


@app.post("/api/admin/users/{username}/admin")
async def set_user_admin(username: str, request: Request):
    """Toggle a user's admin flag. Body: {"is_admin": bool}. Refuses to
    revoke admin from the last remaining admin, and refuses to demote the
    account making the request."""
    if not _is_admin(request):
        raise HTTPException(403)
    body = await request.json()
    make_admin = bool(body.get("is_admin"))
    current = _get_current_user(request)
    if not users.exists(username):
        raise HTTPException(status_code=404, detail="unknown user")
    if not make_admin:
        if username == current:
            raise HTTPException(status_code=400, detail="cannot demote yourself")
        if users.is_admin(username) and len(users.admin_usernames()) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote the last remaining admin")
    users.set_admin(username, make_admin)
    activity.log_activity(
        current, "user_role_change",
        detail=f"{username} -> {'admin' if make_admin else 'user'}",
        ip=_client_ip(request),
    )
    return {"username": username, "is_admin": make_admin, "ok": True}


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
    """Routes root-logger output into a job's event stream, translating and
    de-duplicating along the way so the UI console reads like a trading
    terminal instead of a raw stderr dump.

    - Drops noisy HTTP-client chatter (``httpx``, ``urllib3``, model SDKs)
      entirely — the pipeline tracker already reports the meaningful
      progress those calls are backing.
    - Rewrites the common *optional* data-source warnings (雪球 403、Reddit
      429、FRED 未配置 …) into a short, human-readable Chinese line and
      shows each such line **only once** per job — the underlying flow
      keeps its own retry/fallback logic, so echoing every retry just
      scares the user.
    - Prefixes every user-facing line with ``HH:MM:SS`` so the console
      feels like a Bloomberg / TradingView log."""

    _NOISY_LOGGERS = (
        "httpx", "httpcore", "urllib3", "openai._base_client",
        "anthropic._base_client", "google.api_core", "google.auth",
        "asyncio", "watchfiles", "chromadb",
    )
    _NOISY_SUBSTRINGS = (
        "HTTP Request:", "HTTP/1.1 200", "HTTP/1.1 201",
    )

    # (match-substring, replacement, level-override, dedupe-key).
    # Substrings are the smallest fragment that identifies the warning so
    # a wide variety of upstream messages collapse to one nice line.
    _REWRITES = (
        ("Xueqiu fetch failed",       "ℹ 雪球数据源暂不可用（未登录/被限流），已跳过",           "info", "xueqiu"),
        ("StockTwits fetch failed",   "ℹ StockTwits 暂不可用，已跳过",                            "info", "stocktwits"),
        ("Reddit RSS 429",            "ℹ Reddit 触发限流，稍后自动重试",                          "info", "reddit-429"),
        ("Reddit RSS fetch failed",   "ℹ Reddit 数据源暂不可用，已跳过",                          "info", "reddit-fail"),
        ("Guba fetch failed",         "ℹ 股吧数据源暂不可用，已跳过",                             "info", "guba"),
        ("Weibo fetch failed",        "ℹ 微博数据源暂不可用，已跳过",                             "info", "weibo"),
        ("Baidu fetch failed",        "ℹ 百度指数暂不可用，已跳过",                               "info", "baidu"),
        ("FRED_API_KEY environment variable is not set",
         "ℹ 未配置 FRED_API_KEY（可选宏观数据源），已跳过",                                       "info", "fred"),
        ("Vendor 'fred' not configured",
         "ℹ 宏观数据源 FRED 未配置，已跳过",                                                     "info", "fred-vendor"),
        ("google_trends unavailable",
         "ℹ Google Trends 暂不可用，已跳过",                                                     "info", "gtrends"),
        ("structured-output invocation failed",
         "⟳ 结构化输出失败，自动回落到自由文本模式重试",                                          "info", "structured-fallback"),
        ("is not in the known model list",
         "",                                                                                     "drop", "model-warn"),
    )

    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self._seen: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if any(name.startswith(p) for p in self._NOISY_LOGGERS):
            return
        msg = record.getMessage()
        if any(s in msg for s in self._NOISY_SUBSTRINGS):
            return

        friendly = None
        for needle, replacement, level, key in self._REWRITES:
            if needle in msg:
                if level == "drop":
                    return
                if key in self._seen:
                    return
                self._seen.add(key)
                friendly = replacement
                break

        ts = time.strftime("%H:%M:%S", time.localtime())
        if friendly is not None:
            out = f"[{ts}] {friendly}"
        else:
            out = f"[{ts}] {msg}"
        with contextlib.suppress(Exception):
            self.job.emit("log", message=out)


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
    details = get_stock_details(job.ticker)
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
        "stock_details": {
            "name_zh": details["zh"],
            "name_en": details["en"],
            "abbr": details["abbr"]
        }
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
        ts = time.strftime("%H:%M:%S", time.localtime())
        if status == "running":
            job.emit("log", message=(f"[{ts}] ▶ 开始 {label}" if use_zh else f"[{ts}] ▶ Started {label}"))
        elif status == "done":
            tail = f" ({elapsed_s}s)" if elapsed_s is not None else ""
            job.emit("log", message=(f"[{ts}] ✔ 完成 {label}{tail}" if use_zh else f"[{ts}] ✔ Finished {label}{tail}"))

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
        history.save_history(job.started_by, job.ticker, job.trade_date, rating, data)
        # A report was actually produced — deduct one from the user's quota
        # (admins are exempt). Only here, so cancels/errors cost nothing.
        if job.started_by and not users.is_admin(job.started_by):
            quota.consume(job.started_by)
        activity.log_activity(
            job.started_by, "analyze_finish",
            detail=f"{job.ticker} @ {job.trade_date} -> {rating}",
        )
    except RunCancelled:
        job.emit("cancelled")
        job.finish("cancelled")
        activity.log_activity(
            job.started_by, "analyze_cancel", detail=f"{job.ticker} @ {job.trade_date}",
        )
    except Exception as exc:
        logger.exception("Job %s failed", job.id)
        job.emit("error", message=str(exc))
        job.finish("error", error=str(exc))
        activity.log_activity(
            job.started_by, "analyze_error", detail=f"{job.ticker} @ {job.trade_date}: {exc}",
        )
    finally:
        root_logger.removeHandler(log_handler)


@app.post("/api/analyze", status_code=202)
def analyze_ticker(req: AnalysisRequest, request: Request):
    _maintenance_check()          # returns 503 if maintenance mode is on
    logger.info("Received analysis request: %s", req)

    # Report quota: non-admins must have a report left. Checked here (before
    # a job is created); the balance is actually deducted on successful
    # completion in _run_job, so cancelled/failed runs don't cost anything.
    user = _get_current_user(request)
    if user and not users.is_admin(user) and quota.get_remaining(user) <= 0:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "quota_exceeded",
                "message": "报告生成次数已用完，请联系管理员充值。/ You've used up your report quota — contact an admin to top up.",
            },
        )

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
    job.started_by = _get_current_user(request)
    activity.log_activity(
        job.started_by, "analyze_start",
        detail=f"{req.ticker} @ {req.trade_date}", ip=_client_ip(request),
    )
    threading.Thread(target=_run_job, args=(job, config), daemon=True).start()
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.snapshot()


@app.get("/api/history")
def get_history(request: Request):
    """This user's past analyses (see web/history.py) — server-side and
    per-user, so the History tab shows the same data regardless of which
    device or browser looks at it.

    We also prepend the *currently running* job (if it belongs to this
    user) as a synthetic entry with ``status="running"`` and ``job_id``
    set, so the History panel reflects live work in progress without
    waiting for it to finish. Only one job can run at a time
    (JobRegistry enforces this), so at most one such entry is injected."""
    user = _get_current_user(request)
    items = history.list_history(user)

    # Enrich each historical item with stock details dynamically
    for it in items:
        details = get_stock_details(it["ticker"])
        it["name_zh"] = details["zh"]
        it["name_en"] = details["en"]
        it["abbr"] = details["abbr"]

    running = registry.running_job()
    if running is not None and running.started_by == user:
        live_key = (running.ticker, running.trade_date)
        # If a prior completed run for the same (ticker, date) exists we
        # collapse it into the live entry rather than showing both — the
        # user re-ran it, the old result is stale until the new one lands.
        items = [it for it in items if (it.get("ticker"), it.get("trade_date")) != live_key]
        details = get_stock_details(running.ticker)
        items.insert(0, {
            "ticker": running.ticker,
            "trade_date": running.trade_date,
            "decision": None,
            "status": "running",
            "job_id": running.id,
            "ts": None,
            "name_zh": details["zh"],
            "name_en": details["en"],
            "abbr": details["abbr"]
        })

    # Mark all historical rows as "done" for a uniform frontend schema.
    for it in items:
        it.setdefault("status", "done")
    return {"items": items}


@app.get("/api/history/{ticker}/{trade_date}")
def get_history_entry(ticker: str, trade_date: str, request: Request):
    user = _get_current_user(request)
    result = history.get_history_result(user, ticker, trade_date)
    if result is None:
        raise HTTPException(status_code=404, detail="history entry not found")
    
    # Inject stock details dynamically so older reports display full metadata
    details = get_stock_details(ticker)
    result["stock_details"] = {
        "name_zh": details["zh"],
        "name_en": details["en"],
        "abbr": details["abbr"]
    }
    return result


@app.post("/api/jobs/{job_id}/polish")
def polish_report(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done" or not job.result:
        raise HTTPException(status_code=409, detail="job has no completed result to polish yet")
    try:
        polished = job.get_or_create_polished_report(lambda: generate_polished_report(job))
        # Update history cache so it is persisted
        if job.started_by and job.result:
            res = history.get_history_result(job.started_by, job.ticker, job.trade_date)
            if res:
                res["polished_markdown"] = polished
                rating = res.get("decision", "HOLD")
                history.save_history(job.started_by, job.ticker, job.trade_date, rating, res)
    except Exception as exc:
        logger.exception("AI polish failed for job %s", job_id)
        raise HTTPException(status_code=502, detail=f"AI polish failed: {exc}") from exc
    return {"polished_markdown": polished}


class EmailReportRequest(BaseModel):
    ticker: str
    trade_date: str
    email: str


@app.post("/api/reports/send-email")
def send_email_report_endpoint(req: EmailReportRequest, request: Request):
    user = _get_current_user(request)
    # Fetch result from history
    result = history.get_history_result(user, req.ticker, req.trade_date)
    if result is None:
        # Check if it matches a running/completed job in the registry that hasn't cleared yet
        found_job = None
        for job in registry.list_jobs():
            if job.ticker == req.ticker and job.trade_date == req.trade_date and job.result:
                found_job = job
                break
        if found_job:
            result = found_job.result
        else:
            raise HTTPException(status_code=404, detail="No analysis report found for this ticker and date.")

    # Send the email!
    if not mailer.is_configured():
        raise HTTPException(
            status_code=501,
            detail="邮件发送服务未配置。请联系系统管理员在服务器端配置 TRADINGAGENTS_SMTP_HOST, TRADINGAGENTS_SMTP_USER, TRADINGAGENTS_SMTP_PASS 环境变量。"
        )

    try:
        # Build HTML content
        # Check if we have polished report
        polished_md = result.get("polished_markdown")
        
        def markdown_to_html(md: str) -> str:
            import re
            md = re.sub(r'^### (.*?)$', r'<h3>\1</h3>', md, flags=re.MULTILINE)
            md = re.sub(r'^## (.*?)$', r'<h2>\1</h2>', md, flags=re.MULTILINE)
            md = re.sub(r'^# (.*?)$', r'<h1>\1</h1>', md, flags=re.MULTILINE)
            md = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', md)
            md = re.sub(r'^\s*-\s*(.*?)$', r'<li>\1</li>', md, flags=re.MULTILINE)
            md = re.sub(r'(<li>.*?</li>)', r'<ul>\1</ul>', md, flags=re.DOTALL)
            md = md.replace('</ul>\n<ul>', '')
            
            paragraphs = md.split('\n\n')
            html_paras = []
            for p in paragraphs:
                p_clean = p.strip()
                if not p_clean:
                    continue
                if p_clean.startswith('<h') or p_clean.startswith('<ul'):
                    html_paras.append(p_clean)
                else:
                    html_paras.append(f"<p>{p_clean.replace('\n', '<br>')}</p>")
            return '\n'.join(html_paras)

        dec_color = "#30D158" if "BUY" in result.get("decision", "HOLD").upper() else ("#FF453A" if "SELL" in result.get("decision", "HOLD").upper() else "#FF9500")
        
        summary = result.get("decision_summary") or {}
        entry = summary.get("entry_price", "--")
        target = summary.get("target_price", "--")
        stop = summary.get("stop_loss", "--")
        position = summary.get("suggested_position", "--")
        horizon = summary.get("time_horizon", "--")
        rr = summary.get("risk_reward_ratio", "--")

        html_body = f"""
        <html>
        <head>
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #333333; line-height: 1.6; padding: 20px; background-color: #f7f9fa; }}
          .container {{ max-width: 680px; margin: 0 auto; background: #ffffff; border: 1px solid #e1e4e6; border-radius: 8px; padding: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
          .header {{ border-bottom: 2px solid #333333; padding-bottom: 20px; margin-bottom: 24px; }}
          .title {{ font-size: 24px; font-weight: 800; margin: 0; color: #111111; letter-spacing: -0.5px; }}
          .subtitle {{ font-size: 13px; color: #666666; margin: 4px 0 0 0; font-family: monospace; }}
          .badge {{ display: inline-block; padding: 6px 14px; border-radius: 4px; font-weight: bold; font-size: 14px; color: #ffffff; background-color: {dec_color}; }}
          .kpi-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
          .kpi-table th, .kpi-table td {{ border: 1px solid #e1e4e6; padding: 10px 12px; text-align: left; }}
          .kpi-table th {{ background-color: #f1f3f5; color: #495057; font-weight: 700; }}
          .section {{ margin-bottom: 30px; border-bottom: 1px solid #f1f3f5; padding-bottom: 20px; }}
          .section-title {{ font-size: 16px; font-weight: 700; color: #1a1a1a; margin-top: 0; margin-bottom: 12px; border-left: 4px solid {dec_color}; padding-left: 10px; }}
          .section-content {{ font-size: 14px; color: #444444; white-space: pre-wrap; }}
          .footer {{ text-align: center; font-size: 11px; color: #999999; margin-top: 40px; border-top: 1px solid #e1e4e6; padding-top: 20px; }}
        </style>
        </head>
        <body>
        <div class="container">
          <div class="header">
            <h1 class="title">■ TradingAgents 量化投资分析报告</h1>
            <p class="subtitle">{req.ticker} | 分析日期：{req.trade_date}</p>
          </div>
          
          <div class="section">
            <h2 class="section-title">最终投资决策</h2>
            <span class="badge">{result.get("decision", "HOLD")}</span>
          </div>
          
          <table class="kpi-table">
            <tr><th>股票代码</th><td>{req.ticker}</td><th>分析日期</th><td>{req.trade_date}</td></tr>
            <tr><th>建议入场价</th><td>{entry}</td><th>风报比 R:R</th><td>{rr}</td></tr>
            <tr><th>止损价</th><td>{stop}</td><th>目标价</th><td>{target}</td></tr>
            <tr><th>建议仓位</th><td>{position}</td><th>时间尺度</th><td>{horizon}</td></tr>
          </table>
        """

        if polished_md:
            html_body += f"""
            <div class="section" style="background-color:#fafbfc;border:1px solid #e1e4e6;padding:20px;border-radius:6px;margin-bottom:30px;">
              <h2 class="section-title" style="border-left-color:#FF2D55;">✨ AI 润色深度研报 (Cohesive AI Report)</h2>
              <div class="section-content" style="font-size:14.5px;color:#2c3e50;">{markdown_to_html(polished_md)}</div>
            </div>
            """

        sections = [
            ("市场与技术面分析", result.get("market_report")),
            ("社交情绪面分析", result.get("sentiment_report")),
            ("宏观与新闻面分析", result.get("news_report")),
            ("财务基本面分析", result.get("fundamentals_report")),
            ("决策经理辩论摘要", result.get("investment_plan")),
            ("交易执行方案", result.get("trader_investment_plan")),
            ("风控分析与建议", result.get("final_trade_decision")),
        ]

        for s_title, s_content in sections:
            if s_content:
                s_html = s_content.replace("\n", "<br>")
                html_body += f"""
                <div class="section">
                  <h2 class="section-title">{s_title}</h2>
                  <div class="section-content">{s_html}</div>
                </div>
                """

        html_body += f"""
          <div class="footer">
            <p>本报告由 TradingAgents 智能体量化系统自动生成。仅供参考，不构成投资建议。</p>
            <p>© 2026 TradingAgents. All rights reserved.</p>
          </div>
        </div>
        </body>
        </html>
        """

        mailer.send_email(req.email, f"【TradingAgents】{req.ticker} 量化分析研报 ({req.trade_date})", html_body)
        return {"status": "success", "message": "Email sent successfully"}
    except Exception as exc:
        logger.exception("Failed to send email to %s for report %s", req.email, req.ticker)
        raise HTTPException(status_code=502, detail=f"Email sending failed: {exc}")


class PolishReportRequest(BaseModel):
    ticker: str
    trade_date: str


@app.post("/api/reports/polish")
def polish_historical_report(req: PolishReportRequest, request: Request):
    user = _get_current_user(request)
    result = history.get_history_result(user, req.ticker, req.trade_date)
    if result is None:
        raise HTTPException(status_code=404, detail="history entry not found")
        
    # Check if polished markdown is already generated and cached
    polished = result.get("polished_markdown")
    if polished:
        return {"polished_markdown": polished}

    # Otherwise, generate it!
    # Get active LLM config
    config = _get_default_config()
    try:
        from tradingagents.llm_clients import create_llm_client
        from web.polish import _build_polish_prompt, generate_polished_report
        
        # Build client config using defaults
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("deep_think_llm", "gpt-5.5"),
            base_url=config.get("backend_url"),
        )
        llm = client.get_llm()
        prompt = _build_polish_prompt(result, config.get("output_language", "English"))
        response = llm.invoke(prompt)
        polished = response.content

        # Cache it back to history!
        result["polished_markdown"] = polished
        rating = result.get("decision", "HOLD")
        history.save_history(user, req.ticker, req.trade_date, rating, result)
        
        return {"polished_markdown": polished}
    except Exception as exc:
        logger.exception("AI polish failed for historical report %s", req.ticker)
        raise HTTPException(status_code=502, detail=f"AI polish failed: {exc}") from exc


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
def get_price_history(ticker: str, date: str, range: str = "6M"):
    """OHLCV history around a trade date, for the Lightweight-Charts overlay.

    ``range`` accepts ``1M`` / ``3M`` / ``6M`` / ``1Y`` / ``2Y`` — the pane
    lets the user flip between windows without a new server round-trip
    would need a longer one. Response shape:

        {"status": "success",
         "prices":  [{date, close}, ...],            # kept for existing consumers
         "candles": [{time, open, high, low, close, volume}, ...]}
    """
    import datetime

    import yfinance as yf
    from dateutil.relativedelta import relativedelta

    from tradingagents.dataflows.stockstats_utils import yf_retry
    from tradingagents.dataflows.symbol_utils import normalize_symbol

    _RANGE_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "2Y": 24}
    months = _RANGE_MONTHS.get((range or "6M").upper(), 6)

    try:
        dt = datetime.datetime.strptime(date, "%Y-%m-%d")
        # Bias toward historical context: 80% of the window is before the
        # trade date, 20% after — so the chart shows what the analysts saw
        # plus a bit of the immediate aftermath for post-hoc checking.
        pre_days = int(months * 30 * 0.8)
        post_days = int(months * 30 * 0.2)
        start_dt = dt - relativedelta(days=pre_days)
        end_dt = dt + relativedelta(days=post_days)

        start_str = start_dt.strftime("%Y-%m-%d")
        end_inclusive_str = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")

        canonical = normalize_symbol(ticker)
        yf_ticker = yf.Ticker(canonical)
        data = yf_retry(lambda: yf_ticker.history(start=start_str, end=end_inclusive_str))

        if data.empty:
            return {"status": "error", "message": f"No data found for {ticker} in range {start_str} to {date}"}

        if data.index.tz is not None:
            data.index = data.index.tz_localize(None)

        prices = []
        candles = []
        for index, row in data.iterrows():
            date_str = index.strftime("%Y-%m-%d")
            close_v = round(float(row["Close"]), 4)
            prices.append({"date": date_str, "close": close_v})
            # Fall back to close for any OHLC field that's missing —
            # some mocked/synthetic sources only provide Close (see test).
            def _pick(key, default):
                if key in row.index and not pd_isnan(row[key]):
                    return round(float(row[key]), 4)
                return default
            candles.append({
                "time": date_str,
                "open":  _pick("Open",  close_v),
                "high":  _pick("High",  close_v),
                "low":   _pick("Low",   close_v),
                "close": close_v,
                "volume": int(row["Volume"]) if ("Volume" in row.index and not pd_isnan(row["Volume"])) else 0,
            })

        return {
            "status": "success",
            "symbol": canonical,
            "trade_date": date,
            "range": range.upper(),
            "prices": prices,
            "candles": candles,
        }
    except Exception as e:
        logger.error(f"Error fetching price history for {ticker}: {e}")
        return {"status": "error", "message": str(e)}


def pd_isnan(v) -> bool:
    """Tiny NaN check that avoids importing pandas at module load time."""
    try:
        return v is None or (isinstance(v, float) and v != v)
    except Exception:
        return False


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
<title>登录</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#080A0C;color:#E6EDF3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#101418;border:1px solid #212830;border-radius:12px;padding:36px;width:360px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
  label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:#8B949E;display:block;margin-bottom:6px}
  .field{margin-bottom:14px}
  input[type=text],input[type=password]{width:100%;background:#161B22;border:1px solid #212830;border-radius:6px;color:#E6EDF3;padding:10px 12px;font-size:14px;outline:none;transition:border-color .2s}
  input:focus{border-color:#B98029}
  .remember{display:flex;align-items:center;gap:8px;margin-top:16px;color:#8B949E;font-size:13px;font-weight:500;text-transform:none;letter-spacing:normal;cursor:pointer}
  .remember input{width:15px;height:15px;accent-color:#FF2D55;cursor:pointer}
  button{width:100%;margin-top:20px;background:#FF2D55;border:none;border-radius:6px;color:#fff;font-size:14px;font-weight:600;padding:11px;cursor:pointer;transition:opacity .15s}
  button:hover{opacity:.9}
  .err{color:#FF453A;font-size:13px;margin-top:12px;display:none}
  .altlink{text-align:center;margin-top:18px;font-size:13px;color:#8B949E}
  .altlink a{color:#FF2D55;text-decoration:none;font-weight:600}
  .altlink a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="card">
  <div class="field">
    <label for="un">用户名</label>
    <input id="un" type="text" autocomplete="username" autofocus>
  </div>
  <div class="field">
    <label for="pw">密码</label>
    <input id="pw" type="password" autocomplete="current-password">
  </div>
  <label class="remember"><input id="remember" type="checkbox" checked>保持登录</label>
  <button onclick="login()">进入工作站</button>
  <div class="err" id="err">用户名或密码错误，请重试</div>
  <div class="altlink">还没有账号？<a href="/register">立即注册</a>（新用户送 2 次免费分析）</div>
</div>
<script>
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
document.getElementById('un').addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('pw').focus()});
async function login(){
  const un=document.getElementById('un').value.trim();
  const pw=document.getElementById('pw').value;
  const remember=document.getElementById('remember').checked;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:un,password:pw,remember:remember})});
  if(r.ok){location.href='/';}
  else{const e=document.getElementById('err');e.style.display='block';}
}
</script>
</body></html>"""


_REGISTER_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>注册</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#080A0C;color:#E6EDF3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#101418;border:1px solid #212830;border-radius:12px;padding:36px;width:380px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
  h1{font-size:16px;margin-bottom:6px}
  .sub{font-size:12px;color:#8B949E;margin-bottom:22px}
  label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:#8B949E;display:block;margin-bottom:6px}
  .field{margin-bottom:14px}
  .hint{font-size:11px;color:#5c6672;margin-top:4px}
  input[type=text],input[type=email],input[type=password]{width:100%;background:#161B22;border:1px solid #212830;border-radius:6px;color:#E6EDF3;padding:10px 12px;font-size:14px;outline:none;transition:border-color .2s}
  input:focus{border-color:#B98029}
  button{width:100%;margin-top:8px;background:#FF2D55;border:none;border-radius:6px;color:#fff;font-size:14px;font-weight:600;padding:11px;cursor:pointer;transition:opacity .15s}
  button:hover{opacity:.9}
  button:disabled{opacity:.5;cursor:default}
  .err{color:#FF453A;font-size:13px;margin-top:12px;display:none}
  .ok{color:#30D158;font-size:13px;margin-top:12px;display:none;line-height:1.5}
  .altlink{text-align:center;margin-top:18px;font-size:13px;color:#8B949E}
  .altlink a{color:#FF2D55;text-decoration:none;font-weight:600}
  .altlink a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="card">
  <h1>创建账号</h1>
  <div class="sub">注册即送 2 次免费分析额度，验证邮箱后自动登录。</div>
  <div class="field">
    <label for="un">用户名</label>
    <input id="un" type="text" autocomplete="username" autofocus>
    <div class="hint">3-32 位字母、数字、下划线或连字符</div>
  </div>
  <div class="field">
    <label for="em">邮箱</label>
    <input id="em" type="email" autocomplete="email">
  </div>
  <div class="field">
    <label for="pw">密码</label>
    <input id="pw" type="password" autocomplete="new-password">
    <div class="hint">至少 8 个字符</div>
  </div>
  <button id="submitBtn" onclick="doRegister()">注册</button>
  <div class="err" id="err"></div>
  <div class="ok" id="ok"></div>
  <div class="altlink">已有账号？<a href="/login">去登录</a></div>
</div>
<script>
async function doRegister(){
  const un=document.getElementById('un').value.trim();
  const em=document.getElementById('em').value.trim();
  const pw=document.getElementById('pw').value;
  const errEl=document.getElementById('err'), okEl=document.getElementById('ok'), btn=document.getElementById('submitBtn');
  errEl.style.display='none'; okEl.style.display='none';
  btn.disabled=true;
  try{
    const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:un,email:em,password:pw})});
    const data=await r.json();
    if(r.ok){
      okEl.textContent=data.message||'验证邮件已发送，请查收。';
      okEl.style.display='block';
    }else{
      errEl.textContent=(data.detail)||'注册失败，请重试。';
      errEl.style.display='block';
      btn.disabled=false;
    }
  }catch(e){
    errEl.textContent='网络错误，请重试。';
    errEl.style.display='block';
    btn.disabled=false;
  }
}
</script>
</body></html>"""


def _verify_result_html(status: str, message: str) -> str:
    color = "#30D158" if status == "success" else "#FF453A"
    icon = "✓" if status == "success" else "✕"
    cta = '<a href="/">进入工作站</a>' if status == "success" else '<a href="/register">重新注册</a>'
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>邮箱验证</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#080A0C;color:#E6EDF3;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
  .card{{background:#101418;border:1px solid #212830;border-radius:12px;padding:40px;width:380px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.6)}}
  .icon{{font-size:40px;color:{color};margin-bottom:16px}}
  .msg{{font-size:14px;line-height:1.6;margin-bottom:24px}}
  a{{display:inline-block;background:#FF2D55;color:#fff;text-decoration:none;padding:11px 28px;border-radius:6px;font-weight:600;font-size:14px}}
  a:hover{{opacity:.9}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{icon}</div>
  <div class="msg">{html.escape(message)}</div>
  {cta}
</div>
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
  .btn.amber:hover{background:rgba(185,128,41,.25)}
  .btn.ok{background:rgba(63,185,80,.15);border-color:rgba(63,185,80,.35);color:#3FB950}
  .btn.ok:hover{background:rgba(63,185,80,.25)}
  .btn:disabled{opacity:.4;cursor:not-allowed;filter:grayscale(.5)}
  .btn:disabled:hover{background:#161B22}
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
  <h2>用户</h2>
  <div class="user-create" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;padding:12px;background:#0d1117;border:1px solid #212830;border-radius:6px">
    <input id="new-username" placeholder="新用户名（字母/数字/_-.）" style="flex:1;min-width:160px;padding:7px 10px;background:#161B22;border:1px solid #212830;border-radius:5px;color:#E6EDF3;font-size:13px">
    <input id="new-password" placeholder="密码" type="text" style="flex:1;min-width:140px;padding:7px 10px;background:#161B22;border:1px solid #212830;border-radius:5px;color:#E6EDF3;font-size:13px">
    <label style="display:flex;align-items:center;gap:6px;color:#C7CDD5;font-size:13px;padding:0 4px">
      <input type="checkbox" id="new-is-admin" style="accent-color:#B98029"> 管理员
    </label>
    <button class="btn ok" style="padding:7px 16px" onclick="createUser()">＋ 新增用户</button>
  </div>
  <table>
    <thead><tr><th>用户名</th><th>角色</th><th>密码</th><th>剩余次数</th><th>额度操作</th><th>最近活动</th><th>登录次数</th><th>分析次数</th><th>用户操作</th></tr></thead>
    <tbody id="users-tbody"></tbody>
  </table>
</div>

<div class="card">
  <h2>在线用户 (Live Users)</h2>
  <table>
    <thead><tr><th>用户</th><th>IP 地区</th><th>当前操作</th><th>查看标的</th><th>已停留时长</th><th>状态</th></tr></thead>
    <tbody id="live-users-tbody"></tbody>
  </table>
</div>

<div class="card">
  <h2>正在运行的任务</h2>
  <div id="job-info" style="color:#8B949E;font-size:13px">暂无运行中的任务</div>
</div>

<div class="card">
  <h2>操作日志</h2>
  <table>
    <thead><tr><th>时间</th><th>用户</th><th>操作</th><th>详情</th><th>IP 地区</th></tr></thead>
    <tbody id="activity-tbody"></tbody>
  </table>
</div>

<div class="card">
  <h2>访问最多的 IP（前 20）</h2>
  <table><thead><tr><th>IP</th><th>归属地区</th><th>请求数</th></tr></thead><tbody id="ip-tbody"></tbody></table>
</div>

<div class="card">
  <h2>最近 50 条原始请求</h2>
  <table>
    <thead><tr><th>时间</th><th>用户</th><th>IP 地区</th><th>方法</th><th>路径</th><th>状态</th></tr></thead>
    <tbody id="log-tbody"></tbody>
  </table>
</div>

<script>
let maintenanceOn = false;

function esc(s){
  const d = document.createElement('div');
  d.textContent = (s === null || s === undefined) ? '' : String(s);
  return d.innerHTML;
}

const ACTION_LABELS = {
  login: '登录成功', login_failed: '登录失败', logout: '登出',
  analyze_start: '发起分析', analyze_finish: '分析完成',
  analyze_cancel: '分析已取消', analyze_error: '分析出错',
  maintenance_toggle: '切换维护模式', admin_force_cancel: '管理员强制终止任务',
  quota_adjust: '调整报告额度', password_reset: '重置密码',
  user_create: '新增用户', user_delete: '删除用户', user_role_change: '修改角色',
  register_pending: '注册待验证', register_verified: '注册验证通过',
};
const ACTION_CLASS = {
  login: 'ok', analyze_finish: 'ok', quota_adjust: 'ok', user_create: 'ok', register_verified: 'ok',
  login_failed: 'err', analyze_error: 'err', admin_force_cancel: 'err', user_delete: 'err',
  analyze_cancel: 'warn', maintenance_toggle: 'warn', password_reset: 'warn', user_role_change: 'warn',
  register_pending: 'warn',
};

function relTime(iso){
  if(!iso) return '从未';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms/60000);
  if(m < 1) return '刚刚';
  if(m < 60) return m + ' 分钟前';
  const h = Math.floor(m/60);
  if(h < 24) return h + ' 小时前';
  return Math.floor(h/24) + ' 天前';
}

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

  // Users table: per-user last-seen / login count / analyze count.
  const adminSet = new Set(d.admin_users||[]);
  document.getElementById('users-tbody').innerHTML =
    (d.user_summary||[]).map(u=>{
      const isAdmin = adminSet.has(u.username);
      let quotaCell, actionCell;
      if(isAdmin || u.quota===null || u.quota===undefined){
        quotaCell = '<span style="color:#8B949E">∞ 无限制</span>';
        actionCell = '<span style="color:#484F58">—</span>';
      } else {
        const q = u.quota;
        const color = q<=0 ? '#FF453A' : (q<=3 ? '#B98029' : '#3FB950');
        const warn = q<=0 ? ' ⚠ 已用完' : (q<=3 ? ' ⚠ 偏低' : '');
        quotaCell = `<strong style="color:${color}">${q}</strong><span style="color:${color};font-size:11px">${warn}</span>`;
        const uname = esc(u.username);
        actionCell = `<input type="number" id="q-${uname}" value="10" min="1" style="width:56px;padding:5px 6px;background:#161B22;border:1px solid #212830;border-radius:5px;color:#E6EDF3;font-size:12px">`
          + ` <button class="btn" style="padding:5px 12px" onclick="addQuota('${uname}')">充值</button>`;
      }
      const uname = esc(u.username);
      const pwCell = `<code style="font-family:monospace;color:#E6EDF3;background:#161B22;padding:2px 7px;border-radius:5px">${esc(u.password)||'—'}</code>`
        + ` <button class="btn" style="padding:4px 9px;font-size:12px" onclick="resetPw('${uname}')" title="重置密码">改</button>`;
      const isCurrent = u.username === d.current_user;
      const roleBtnLabel = isAdmin ? '降级为普通用户' : '升为管理员';
      const roleBtnClass = isAdmin ? 'btn amber' : 'btn ok';
      const roleBtnDisabled = isCurrent ? ' disabled title="不能修改自己的角色"' : '';
      const delBtnDisabled = isCurrent ? ' disabled title="不能删除当前登录账号"' : '';
      const userOps = `
        <button class="${roleBtnClass}" style="padding:4px 9px;font-size:11.5px;margin-right:4px"
                onclick="toggleRole('${uname}', ${!isAdmin})"${roleBtnDisabled}>${roleBtnLabel}</button>
        <button class="btn danger" style="padding:4px 9px;font-size:11.5px"
                onclick="deleteUser('${uname}')"${delBtnDisabled}>删除</button>
      `;
      return `<tr>
        <td>${esc(u.username)}${isCurrent?' <span style="color:#8B949E">(当前)</span>':''}</td>
        <td>${isAdmin?'<span class="badge warn">管理员 👑</span>':'<span class="badge ok">普通用户</span>'}</td>
        <td>${pwCell}</td>
        <td>${quotaCell}</td>
        <td>${actionCell}</td>
        <td>${relTime(u.last_seen)}</td>
        <td>${u.login_count}</td>
        <td>${u.analyze_count}</td>
        <td>${userOps}</td>
      </tr>`;
    }).join('');

  // Activity log: human-readable action feed (login/analyze/admin actions).
  document.getElementById('activity-tbody').innerHTML =
    (d.recent_activity||[]).map(e=>{
      const cls = ACTION_CLASS[e.action] || '';
      const label = ACTION_LABELS[e.action] || e.action;
      const reg = e.region ? `<span style="font-size:11px;opacity:0.6;display:block">${esc(e.region)}</span>` : '';
      return `<tr>
        <td>${esc(e.ts)}</td>
        <td style="color:#B98029;font-weight:500">${esc(e.username)||'—'}</td>
        <td>${cls?`<span class="badge ${cls}">${esc(label)}</span>`:esc(label)}</td>
        <td>${esc(e.detail)||''}</td>
        <td>${esc(e.ip)||''}${reg}</td>
      </tr>`;
    }).join('');

  // Live active visitors tracker binding
  document.getElementById('live-users-tbody').innerHTML =
    (d.live_users||[]).map(x=>{
      let viewLabel = x.view;
      if(x.view === 'welcome') viewLabel = '🎯 配置参数中';
      else if(x.view === 'running') viewLabel = '⏳ 正在进行量化分析';
      else if(x.view === 'reader') viewLabel = '📖 深度阅读社论报告';
      else if(x.view === 'dashboard') viewLabel = '📊 浏览仪表盘指标';

      const lastSeen = x.last_seen_ago <= 5 ? '在线 🟢' : `${x.last_seen_ago}秒前`;

      const durationMin = Math.floor(x.duration / 60);
      const durationSec = x.duration % 60;
      const durationStr = durationMin > 0 ? `${durationMin}分${durationSec}秒` : `${durationSec}秒`;

      const reg = x.region ? `<span style="font-size:11px;opacity:0.6;display:block">${esc(x.region)}</span>` : '';
      return `<tr>
        <td style="color:#3FB950;font-weight:600">${esc(x.username)}</td>
        <td>${esc(x.ip)}${reg}</td>
        <td><span class="badge ok">${esc(viewLabel)}</span></td>
        <td style="font-family:monospace;font-weight:600">${esc(x.ticker)||'—'}</td>
        <td>${durationStr}</td>
        <td><span style="color:#30D158;font-weight:500">${lastSeen}</span></td>
      </tr>`;
    }).join('') || '<tr><td colspan="6" style="color:#8B949E;text-align:center;padding:12px">暂无在线活跃用户</td></tr>';

  // Maintenance button text
  document.getElementById('btn-maintenance').textContent =
    d.maintenance ? '关闭维护模式 ✓' : '开启维护模式';
  document.getElementById('btn-maintenance').className =
    d.maintenance ? 'btn danger' : 'btn amber';

  // Running job
  const ji = document.getElementById('job-info');
  if(d.running_job){
    ji.innerHTML = `<span class="badge ok">运行中</span>&nbsp;
      <strong>${esc(d.running_job.ticker)}</strong> &nbsp;
      ${esc(d.running_job.trade_date)} &nbsp;
      <code style="font-size:11px;color:#8B949E">${esc(d.running_job.id)}</code>`;
  } else {
    ji.textContent = '暂无运行中的任务';
  }

  // IPs
  document.getElementById('ip-tbody').innerHTML =
    d.top_ips.map(x=>`<tr><td>${esc(x.ip)}</td><td>${esc(x.region)||'未知'}</td><td>${x.count}</td></tr>`).join('');

  // Log
  document.getElementById('log-tbody').innerHTML =
    d.recent_requests.map(e=>{
      const sc = e.status;
      const cls = sc>=500?'err':sc>=400?'warn':'ok';
      const reg = e.region ? `<span style="font-size:11px;opacity:0.6;display:block">${esc(e.region)}</span>` : '';
      return `<tr>
        <td>${esc(e.ts)}</td>
        <td style="color:#B98029;font-weight:500">${esc(e.user)||'—'}</td>
        <td>${esc(e.ip)}${reg}</td><td>${esc(e.method)}</td>
        <td style="font-family:monospace">${esc(e.path)}</td>
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

async function addQuota(username){
  const input = document.getElementById('q-'+username);
  const n = parseInt(input && input.value, 10);
  if(!n || n<=0){ alert('请输入要充值的次数'); return; }
  const r = await fetch('/api/admin/quota', {method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({username, add:n})});
  if(r.ok){ const d = await r.json(); load(); }
  else { alert('充值失败'); }
}

async function resetPw(username){
  const pw = prompt('为用户 "'+username+'" 设置新密码：');
  if(pw===null) return;               // cancelled
  if(!pw.trim()){ alert('密码不能为空'); return; }
  const r = await fetch('/api/admin/password', {method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({username, password:pw})});
  if(r.ok){ alert('已更新 '+username+' 的密码'); load(); }
  else { alert('修改失败'); }
}

async function createUser(){
  const u  = document.getElementById('new-username').value.trim();
  const pw = document.getElementById('new-password').value;
  const admin = document.getElementById('new-is-admin').checked;
  if(!u || !pw){ alert('用户名和密码都不能为空'); return; }
  const r = await fetch('/api/admin/users', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:u, password:pw, is_admin:admin})});
  if(r.ok){
    document.getElementById('new-username').value = '';
    document.getElementById('new-password').value = '';
    document.getElementById('new-is-admin').checked = false;
    load();
  } else {
    let msg = '创建失败';
    try { const e = await r.json(); if(e.detail) msg += '：' + e.detail; } catch(_){}
    alert(msg);
  }
}

async function deleteUser(username){
  if(!confirm('确认删除用户 "'+username+'"？此操作不可撤销（历史/活动日志会保留）。')) return;
  const r = await fetch('/api/admin/users/'+encodeURIComponent(username), {method:'DELETE'});
  if(r.ok){ load(); }
  else {
    let msg = '删除失败';
    try { const e = await r.json(); if(e.detail) msg += '：' + e.detail; } catch(_){}
    alert(msg);
  }
}

async function toggleRole(username, makeAdmin){
  const label = makeAdmin ? '设为管理员' : '降级为普通用户';
  if(!confirm('确认将 "'+username+'" '+label+'？')) return;
  const r = await fetch('/api/admin/users/'+encodeURIComponent(username)+'/admin',
    {method:'POST', headers:{'Content-Type':'application/json'},
     body:JSON.stringify({is_admin: makeAdmin})});
  if(r.ok){ load(); }
  else {
    let msg = '修改失败';
    try { const e = await r.json(); if(e.detail) msg += '：' + e.detail; } catch(_){}
    alert(msg);
  }
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
