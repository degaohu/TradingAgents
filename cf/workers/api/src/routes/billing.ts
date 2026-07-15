/**
 * /api/billing/* — Stripe subscriptions + admin bypass.
 * Wired in MVP week 3; skeleton returns 501 until STRIPE_ENABLED=true.
 */

import { Hono } from "hono";
import type { AppContext } from "../index";

const app = new Hono<AppContext>();

app.get("/status", async (c) => {
  const user = c.get("user");
  const sub = await c.env.DB.prepare(
    "SELECT status, current_period_end, plan FROM subscriptions WHERE user_id = ?",
  )
    .bind(user.id)
    .first();
  const u = await c.env.DB.prepare(
    "SELECT plan, free_quota_remaining, is_admin FROM users WHERE id = ?",
  )
    .bind(user.id)
    .first<{ plan: string; free_quota_remaining: number; is_admin: number }>();
  return c.json({
    plan: u?.plan ?? "free",
    is_admin: u?.is_admin === 1,
    free_quota_remaining: u?.free_quota_remaining ?? 0,
    subscription: sub ?? null,
    stripe_enabled: c.env.STRIPE_ENABLED === "true",
  });
});

app.post("/checkout", (c) => {
  if (c.env.STRIPE_ENABLED !== "true") {
    return c.json({ error: "stripe_disabled" }, 501);
  }
  // TODO week 3: create Stripe Checkout Session.
  return c.json({ error: "not_implemented" }, 501);
});

app.post("/admin/grant", async (c) => {
  const user = c.get("user");
  if (!user.isAdmin) return c.json({ error: "forbidden" }, 403);
  const body = (await c.req.json().catch(() => null)) as {
    target_email: string;
    plan?: string;
    credits?: number;
  } | null;
  if (!body?.target_email) return c.json({ error: "missing_target_email" }, 400);
  const plan = body.plan ?? "admin_bypass";
  await c.env.DB.prepare(
    `UPDATE users SET plan = ?, free_quota_remaining = ? WHERE email = ?`,
  )
    .bind(plan, body.credits ?? 9999, body.target_email)
    .run();
  return c.json({ ok: true });
});

export default app;
