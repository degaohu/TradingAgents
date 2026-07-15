# TradingAgents Cloudflare stack

Frontend (Pages) + API (Workers + D1 + R2) + scheduler (Cron Trigger)
+ SSE broadcast (Durable Object). The heavy Python worker runs on a
VPS and reaches these Workers over a Cloudflare Tunnel — see
[`../worker/README.md`](../worker/README.md).

## Layout

```
cf/
├── schema/                # D1 schema migrations
│   └── 001_init.sql
├── workers/
│   ├── api/               # Hono API (routes user-facing + internal)
│   ├── scheduler/         # Cron Trigger: scans schedules table
│   └── job-room/          # Durable Object: SSE fanout per job
└── pages/                 # React SPA (Vite + Tailwind + shadcn/ui)
```

## Prerequisites (one-off)

1. Cloudflare account, add a Zone (domain) if you want a custom
   subdomain — not required for `.workers.dev` / `.pages.dev`.
2. `npm i -g wrangler` and `wrangler login`.
3. Create resources:
   ```bash
   wrangler d1   create tradingagents-db
   wrangler r2 bucket create tradingagents-reports
   wrangler kv:namespace  create SESSIONS
   wrangler queues create job-events   # optional; can start without
   ```
   Copy each ID into the corresponding `wrangler.toml` binding.
4. Apply schema:
   ```bash
   wrangler d1 execute tradingagents-db --file=cf/schema/001_init.sql --remote
   ```

## Deploy order

```bash
# API
cd cf/workers/api        && wrangler deploy
# Scheduler
cd cf/workers/scheduler  && wrangler deploy
# Durable Object (job-room)
cd cf/workers/job-room   && wrangler deploy
# SPA
cd cf/pages && npm ci && npm run build && wrangler pages deploy dist
```

## Cloudflare Access (auth)

Zero-code SSO. In the Cloudflare dashboard:

1. Zero Trust → Access → Applications → Add a **Self-hosted** app.
2. Application domain: your Pages URL (e.g. `tradingagents.pages.dev`).
3. Add a policy: allow specific email domains or a hard-coded list.
4. Access will inject `Cf-Access-Jwt-Assertion` header into every
   request; the API Worker verifies it and does user upsert on first
   sight.

The `/internal/*` routes on the API Worker are **not** protected by
Access — they're called by the VPS worker daemon and use HMAC
authentication with a shared secret (`CF_INTERNAL_TOKEN`).

## Environment / secrets

Set via `wrangler secret put NAME` on each worker. See individual
`wrangler.toml` files for the required list.
