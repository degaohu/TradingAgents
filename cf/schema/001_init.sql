-- TradingAgents production database schema for Cloudflare D1.
-- Applied via: wrangler d1 execute tradingagents-db --file=cf/schema/001_init.sql
--
-- D1 is SQLite serverless. Notes:
--   * All timestamps are ISO 8601 strings in UTC. Cloudflare Workers cannot
--     rely on machine local time; use `new Date().toISOString()`.
--   * Booleans stored as INTEGER (0/1) per SQLite convention.
--   * Foreign keys are enforced (D1 has FKs on by default from mid-2024 onward).

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────
-- Users: created lazily on first authenticated request when we
-- see a Cloudflare Access JWT for a previously-unknown email.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
  id                     TEXT PRIMARY KEY,               -- ULID
  email                  TEXT NOT NULL UNIQUE,
  name                   TEXT,
  created_at             TEXT NOT NULL,
  plan                   TEXT NOT NULL DEFAULT 'free',   -- 'free' | 'pro' | 'admin_bypass'
  free_quota_remaining   INTEGER NOT NULL DEFAULT 5,
  stripe_customer_id     TEXT,
  is_admin               INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- ─────────────────────────────────────────────────────────────
-- Jobs: one row per analysis request.
-- Report bodies live in R2 (see reports table for R2 keys).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
  id             TEXT PRIMARY KEY,               -- ULID
  user_id        TEXT NOT NULL REFERENCES users(id),
  ticker         TEXT NOT NULL,
  trade_date     TEXT NOT NULL,                  -- YYYY-MM-DD
  status         TEXT NOT NULL,                  -- queued | running | done | failed | cancelled
  provider       TEXT NOT NULL,                  -- openai | deepseek | anthropic | ...
  deep_llm       TEXT NOT NULL,
  quick_llm      TEXT NOT NULL,
  config_json    TEXT NOT NULL,                  -- entire config snapshot
  created_at     TEXT NOT NULL,
  started_at     TEXT,
  finished_at    TEXT,
  error_message  TEXT,
  worker_id      TEXT,                           -- which VPS worker claimed it
  is_public      INTEGER NOT NULL DEFAULT 0,
  share_slug     TEXT UNIQUE                     -- for /r/<slug> public URLs
);

CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_ticker_date ON jobs(ticker, trade_date);

-- ─────────────────────────────────────────────────────────────
-- Reports: one row per finished job. Metadata + FTS-indexed
-- excerpt. Full markdown lives in R2 to keep D1 small.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reports (
  job_id                 TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  r2_key_final           TEXT NOT NULL,           -- raw final report
  r2_key_polished        TEXT,                    -- optional polished version
  decision_action        TEXT,                    -- BUY | HOLD | SELL
  decision_entry_price   REAL,
  decision_price_target  REAL,
  decision_stop_loss     REAL,
  decision_upside_pct    REAL,
  decision_downside_pct  REAL,
  sentiment_band         TEXT,                    -- Bullish | Bearish | Neutral | Mixed
  sentiment_score        REAL,
  sentiment_confidence   TEXT,                    -- low | medium | high
  summary_extract        TEXT NOT NULL,           -- first ~500 chars for list view
  created_at             TEXT NOT NULL
);

-- ─────────────────────────────────────────────────────────────
-- Full-text search over report content.
-- FTS5 with unicode61 + ngram tokenizer (ngram covers CJK where
-- unicode61 falls short).
-- ─────────────────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(
  job_id UNINDEXED,
  ticker,
  ticker_name,
  summary,
  full_body,
  tokenize = 'unicode61 remove_diacritics 2'
);

-- ─────────────────────────────────────────────────────────────
-- Tags / favorites (per-user).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tags (
  id       TEXT PRIMARY KEY,
  user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name     TEXT NOT NULL,
  color    TEXT,
  UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS job_tags (
  job_id  TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  tag_id  TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (job_id, tag_id)
);

CREATE TABLE IF NOT EXISTS favorites (
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (user_id, job_id)
);

-- ─────────────────────────────────────────────────────────────
-- Scheduled tasks: cron-triggered runs.
-- Cron Worker scans this table every minute.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedules (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  cron_expr     TEXT NOT NULL,                  -- standard 5-field cron
  ticker        TEXT NOT NULL,
  config_json   TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  last_run_at   TEXT,
  next_run_at   TEXT,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON schedules(next_run_at) WHERE enabled = 1;

-- ─────────────────────────────────────────────────────────────
-- Billing.
-- Admin bypass: set users.plan = 'admin_bypass' to skip all
-- quota checks without needing a Stripe subscription.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
  user_id              TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  stripe_sub_id        TEXT NOT NULL UNIQUE,
  status               TEXT NOT NULL,            -- active | canceled | past_due | trialing
  current_period_end   TEXT NOT NULL,
  plan                 TEXT NOT NULL,            -- price_id or plan name
  updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id        TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  cost_credits  INTEGER NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_user_created ON usage_events(user_id, created_at DESC);

-- ─────────────────────────────────────────────────────────────
-- Job queue: the VPS worker polls this table for `queued` jobs.
-- Simpler than a real queue for MVP; a claim uses an UPDATE
-- with a WHERE status='queued' condition (D1 supports SQLite's
-- atomic single-row UPDATE semantics via `RETURNING`).
-- ─────────────────────────────────────────────────────────────
-- (No separate table; we use jobs.status = 'queued' as the queue.)
