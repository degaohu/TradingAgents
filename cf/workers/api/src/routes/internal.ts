/**
 * /internal/* — VPS worker daemon → Workers API.
 *
 * These endpoints let the Python worker:
 *   1. Claim the next queued job (atomic).
 *   2. POST progress chunks (fanned out to browser SSE).
 *   3. POST final report (writes reports row, updates job.status).
 *   4. POST error / cancel-check.
 */

import { Hono } from "hono";
import type { AppContext } from "../index";
import { nowIso } from "../lib/ids";

const app = new Hono<AppContext>();

/**
 * POST /internal/jobs/claim
 * Body: { worker_id: string, ticker_hint?: string }
 * Response: { job } or { job: null } when nothing to do.
 *
 * Atomicity: we UPDATE...WHERE status='queued' ORDER BY created_at LIMIT 1
 * with RETURNING so only one worker wins each row. D1 executes the whole
 * statement transactionally.
 */
app.post("/jobs/claim", async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as { worker_id?: string };
  const workerId = body.worker_id ?? "unknown";
  const res = await c.env.DB.prepare(
    `UPDATE jobs
        SET status = 'running',
            worker_id = ?,
            started_at = ?
      WHERE id = (
        SELECT id FROM jobs WHERE status = 'queued'
        ORDER BY created_at ASC LIMIT 1
      )
      RETURNING *`,
  )
    .bind(workerId, nowIso())
    .first<Record<string, unknown>>();
  return c.json({ job: res ?? null });
});

/**
 * POST /internal/jobs/:id/chunk
 * Body: { event_id, event_type, payload }
 * Fans the event out to any SSE subscribers via the JobRoom DO.
 * Not persisted — SSE is best-effort; the final report is what matters.
 */
app.post("/jobs/:id/chunk", async (c) => {
  const id = c.req.param("id");
  const body = await c.req.json().catch(() => null);
  if (!body) return c.json({ error: "bad_body" }, 400);
  const stub = c.env.JOB_ROOMS.get(c.env.JOB_ROOMS.idFromName(id));
  await stub.fetch(
    new Request("https://do/publish", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
  return c.json({ ok: true });
});

/**
 * POST /internal/jobs/:id/finish
 * Body: {
 *   status: 'done' | 'failed',
 *   error_message?: string,
 *   report: {
 *     r2_key_final, r2_key_polished, summary_extract,
 *     decision_action, decision_price_target, decision_stop_loss,
 *     decision_entry_price, decision_upside_pct, decision_downside_pct,
 *     sentiment_band, sentiment_score, sentiment_confidence
 *   }
 * }
 */
app.post("/jobs/:id/finish", async (c) => {
  const id = c.req.param("id");
  const body = (await c.req.json().catch(() => null)) as {
    status: "done" | "failed";
    error_message?: string;
    report?: Record<string, unknown>;
  } | null;
  if (!body?.status) return c.json({ error: "bad_body" }, 400);

  const finishedAt = nowIso();

  if (body.status === "failed") {
    await c.env.DB.prepare(
      `UPDATE jobs SET status = 'failed', finished_at = ?, error_message = ? WHERE id = ?`,
    )
      .bind(finishedAt, body.error_message ?? "unknown", id)
      .run();
    // Optional: refund quota on failure. Left to policy.
    const stub = c.env.JOB_ROOMS.get(c.env.JOB_ROOMS.idFromName(id));
    await stub.fetch(
      new Request("https://do/publish", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ event_type: "finish", payload: { status: "failed" } }),
      }),
    );
    return c.json({ ok: true });
  }

  const r = body.report ?? {};
  await c.env.DB.batch([
    c.env.DB.prepare(
      `INSERT INTO reports (
         job_id, r2_key_final, r2_key_polished, summary_extract,
         decision_action, decision_entry_price, decision_price_target,
         decision_stop_loss, decision_upside_pct, decision_downside_pct,
         sentiment_band, sentiment_score, sentiment_confidence, created_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      id,
      r.r2_key_final,
      r.r2_key_polished ?? null,
      r.summary_extract ?? "",
      r.decision_action ?? null,
      r.decision_entry_price ?? null,
      r.decision_price_target ?? null,
      r.decision_stop_loss ?? null,
      r.decision_upside_pct ?? null,
      r.decision_downside_pct ?? null,
      r.sentiment_band ?? null,
      r.sentiment_score ?? null,
      r.sentiment_confidence ?? null,
      finishedAt,
    ),
    c.env.DB.prepare(
      `UPDATE jobs SET status = 'done', finished_at = ? WHERE id = ?`,
    ).bind(finishedAt, id),
  ]);

  // Publish to any live SSE subscribers so the UI flips to "done".
  const stub = c.env.JOB_ROOMS.get(c.env.JOB_ROOMS.idFromName(id));
  await stub.fetch(
    new Request("https://do/publish", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ event_type: "finish", payload: { status: "done" } }),
    }),
  );
  return c.json({ ok: true });
});

/**
 * GET /internal/jobs/:id/cancel-flag
 * Cheap poll so the worker can bail mid-run if the user pressed cancel.
 */
app.get("/jobs/:id/cancel-flag", async (c) => {
  const id = c.req.param("id");
  const row = await c.env.DB.prepare(
    "SELECT status FROM jobs WHERE id = ?",
  )
    .bind(id)
    .first<{ status: string }>();
  return c.json({ cancelled: row?.status === "cancelled" });
});

export default app;
