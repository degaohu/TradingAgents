/**
 * TradingAgents API — Hono on Cloudflare Workers.
 *
 * Split into two auth realms:
 *   /api/*      — user-facing, requires Cloudflare Access JWT
 *   /internal/* — VPS worker daemon → Workers, HMAC via CF_INTERNAL_TOKEN
 *   /public/*   — unauthenticated (share links)
 *   /webhooks/* — Stripe etc., signature-verified per-route
 */

import { Hono } from "hono";
import { cors } from "hono/cors";

import { requireAccessJwt, type AccessUser } from "./lib/access";
import { requireInternalHmac } from "./lib/internal-auth";
import { upsertUserByEmail } from "./lib/users";

import jobsRoutes from "./routes/jobs";
import schedulesRoutes from "./routes/schedules";
import billingRoutes from "./routes/billing";
import internalRoutes from "./routes/internal";
import publicRoutes from "./routes/public";
import webhookRoutes from "./routes/webhooks";

export interface Env {
  DB: D1Database;
  REPORTS: R2Bucket;
  SESSIONS: KVNamespace;
  JOB_ROOMS: DurableObjectNamespace;

  // vars
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_AUD: string;
  FREE_QUOTA_INITIAL: string;
  STRIPE_ENABLED: string;

  // secrets
  CF_INTERNAL_TOKEN: string;
  R2_ACCOUNT_ID: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  STRIPE_SECRET_KEY?: string;
  STRIPE_WEBHOOK_SECRET?: string;
}

export type AppContext = {
  Bindings: Env;
  Variables: {
    user: AccessUser;
  };
};

const app = new Hono<AppContext>();

app.use("*", cors({ origin: "*", credentials: true }));

app.get("/healthz", (c) => c.text("ok"));

// ── User-facing (Cloudflare Access) ────────────────────────
app.use("/api/*", async (c, next) => {
  const claims = await requireAccessJwt(c);
  if (!claims) return c.json({ error: "unauthorized" }, 401);
  const user = await upsertUserByEmail(c.env, claims.email, claims.name);
  c.set("user", { id: user.id, email: user.email, isAdmin: user.is_admin === 1 });
  await next();
});
app.route("/api/jobs", jobsRoutes);
app.route("/api/schedules", schedulesRoutes);
app.route("/api/billing", billingRoutes);

// ── VPS worker → Workers (HMAC) ────────────────────────────
app.use("/internal/*", requireInternalHmac);
app.route("/internal", internalRoutes);

// ── Public share links ─────────────────────────────────────
app.route("/public", publicRoutes);

// ── Webhooks ───────────────────────────────────────────────
app.route("/webhooks", webhookRoutes);

app.notFound((c) => c.json({ error: "not_found" }, 404));
app.onError((err, c) => {
  console.error("unhandled", err);
  return c.json({ error: "internal", message: String(err) }, 500);
});

// Re-export the Durable Object class so wrangler can bind it if the
// same script hosts it (we deploy it separately; this line is a no-op
// for that setup but keeps single-script deploys possible).
export { JobRoom } from "./durable/job-room";

export default app;
