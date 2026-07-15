/**
 * Webhook receivers. Only Stripe for MVP.
 * Signature verification skipped in skeleton — wired in week 3.
 */

import { Hono } from "hono";
import type { AppContext } from "../index";

const app = new Hono<AppContext>();

app.post("/stripe", async (c) => {
  if (c.env.STRIPE_ENABLED !== "true") return c.text("stripe_disabled", 501);
  // TODO week 3: verify Stripe-Signature header and dispatch by event type.
  return c.text("ok");
});

export default app;
