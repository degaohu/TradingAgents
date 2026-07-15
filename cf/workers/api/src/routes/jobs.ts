/**
 * /api/jobs — user-facing job routes.
 *
 * POST   /api/jobs              submit a new analysis
 * GET    /api/jobs              list current user's jobs (with q, tag, status filters)
 * GET    /api/jobs/:id          job detail + presigned R2 URL for the report
 * POST   /api/jobs/:id/cancel   request cancellation (worker polls this flag)
 * POST   /api/jobs/:id/publish  mint a public share slug
 * DELETE /api/jobs/:id          delete job + associated R2 objects
 *
 * SSE streaming lives in a Durable Object (see ../durable/job-room.ts);
 * this file just exposes /api/jobs/:id/events which forwards to it.
 */

import { Hono } from "hono";
import type { AppContext } from "../index";
import { nowIso, ulid } from "../lib/ids";
import { decrementQuota } from "../lib/users";
import { presignReportUrl } from "../lib/r2";

const app = new Hono<AppContext>();

interface CreateJobBody {
  ticker: string;
  trade_date: string;
  provider: string;
  deep_llm: string;
  quick_llm: string;
  max_debate_rounds?: number;
  config?: Record<string, unknown>;
}

app.post("/", async (c) => {
  const user = c.get("user");
  const body = (await c.req.json().catch(() => null)) as CreateJobBody | null;
  if (!body?.ticker || !body?.trade_date || !body?.provider) {
    return c.json({ error: "missing_required_fields" }, 400);
  }

  const ok = await decrementQuota(c.env, user.id);
  if (!ok) return c.json({ error: "quota_exhausted", upgrade_url: "/billing" }, 402);

  const id = ulid();
  const config = { ...(body.config ?? {}), max_debate_rounds: body.max_debate_rounds ?? 1 };
  await c.env.DB.prepare(
    `INSERT INTO jobs (
       id, user_id, ticker, trade_date, status,
       provider, deep_llm, quick_llm, config_json, created_at
     ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)`,
  )
    .bind(
      id,
      user.id,
      body.ticker.toUpperCase(),
      body.trade_date,
      body.provider,
      body.deep_llm,
      body.quick_llm,
      JSON.stringify(config),
      nowIso(),
    )
    .run();
  return c.json({ job_id: id, status: "queued" }, 202);
});

app.get("/", async (c) => {
  const user = c.get("user");
  const q = c.req.query("q");
  const status = c.req.query("status");
  const limit = Math.min(Number(c.req.query("limit") ?? 50), 200);
  const offset = Number(c.req.query("offset") ?? 0);

  // Note: for `q`, we route through FTS5 in a later iteration.
  //       Skeleton just does a LIKE on ticker.
  const where: string[] = ["user_id = ?"];
  const params: unknown[] = [user.id];
  if (status) {
    where.push("status = ?");
    params.push(status);
  }
  if (q) {
    where.push("ticker LIKE ?");
    params.push(`%${q.toUpperCase()}%`);
  }
  const rows = await c.env.DB.prepare(
    `SELECT id, ticker, trade_date, status, created_at, finished_at, error_message
       FROM jobs
      WHERE ${where.join(" AND ")}
      ORDER BY created_at DESC
      LIMIT ? OFFSET ?`,
  )
    .bind(...params, limit, offset)
    .all();
  return c.json({ jobs: rows.results ?? [] });
});

app.get("/:id", async (c) => {
  const user = c.get("user");
  const id = c.req.param("id");
  const job = await c.env.DB.prepare(
    `SELECT j.*, r.r2_key_final, r.r2_key_polished, r.summary_extract,
            r.decision_action, r.decision_price_target, r.decision_stop_loss,
            r.sentiment_band, r.sentiment_score
       FROM jobs j LEFT JOIN reports r ON r.job_id = j.id
      WHERE j.id = ? AND j.user_id = ?`,
  )
    .bind(id, user.id)
    .first<Record<string, unknown>>();
  if (!job) return c.json({ error: "not_found" }, 404);

  let report_url: string | null = null;
  if (job.r2_key_final) {
    report_url = await presignReportUrl(c.env, job.r2_key_final as string, 300);
  }
  return c.json({ job, report_url });
});

app.post("/:id/cancel", async (c) => {
  const user = c.get("user");
  const id = c.req.param("id");
  const res = await c.env.DB.prepare(
    `UPDATE jobs SET status = 'cancelled'
      WHERE id = ? AND user_id = ? AND status IN ('queued', 'running')`,
  )
    .bind(id, user.id)
    .run();
  if ((res.meta?.changes ?? 0) === 0) return c.json({ error: "not_cancellable" }, 409);
  return c.json({ ok: true });
});

app.post("/:id/publish", async (c) => {
  const user = c.get("user");
  const id = c.req.param("id");
  const slug = ulid().toLowerCase().slice(0, 12);
  const res = await c.env.DB.prepare(
    `UPDATE jobs SET is_public = 1, share_slug = ?
      WHERE id = ? AND user_id = ? AND status = 'done'`,
  )
    .bind(slug, id, user.id)
    .run();
  if ((res.meta?.changes ?? 0) === 0) return c.json({ error: "not_publishable" }, 409);
  return c.json({ share_slug: slug, share_url: `/r/${slug}` });
});

app.delete("/:id", async (c) => {
  const user = c.get("user");
  const id = c.req.param("id");
  const job = await c.env.DB.prepare(
    "SELECT id FROM jobs WHERE id = ? AND user_id = ?",
  )
    .bind(id, user.id)
    .first();
  if (!job) return c.json({ error: "not_found" }, 404);

  // Delete R2 objects then D1 rows (FK ON DELETE CASCADE cleans reports).
  const rpt = await c.env.DB.prepare(
    "SELECT r2_key_final, r2_key_polished FROM reports WHERE job_id = ?",
  )
    .bind(id)
    .first<{ r2_key_final: string; r2_key_polished: string | null }>();
  if (rpt?.r2_key_final) await c.env.REPORTS.delete(rpt.r2_key_final);
  if (rpt?.r2_key_polished) await c.env.REPORTS.delete(rpt.r2_key_polished);
  await c.env.DB.prepare("DELETE FROM jobs WHERE id = ?").bind(id).run();
  return c.json({ ok: true });
});

// SSE forward to the Durable Object.
app.get("/:id/events", async (c) => {
  const user = c.get("user");
  const id = c.req.param("id");
  const owned = await c.env.DB.prepare(
    "SELECT 1 FROM jobs WHERE id = ? AND user_id = ?",
  )
    .bind(id, user.id)
    .first();
  if (!owned) return c.json({ error: "not_found" }, 404);
  const stub = c.env.JOB_ROOMS.get(c.env.JOB_ROOMS.idFromName(id));
  return stub.fetch(new Request(`https://do/subscribe?job_id=${id}`, { method: "GET" }));
});

export default app;
