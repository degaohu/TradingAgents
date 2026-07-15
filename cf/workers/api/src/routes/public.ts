/**
 * Public share endpoints — no auth, only for jobs with is_public=1.
 * URL shape: GET /public/r/:slug → HTML page (or JSON with ?format=json).
 */

import { Hono } from "hono";
import type { AppContext } from "../index";
import { presignReportUrl } from "../lib/r2";

const app = new Hono<AppContext>();

app.get("/r/:slug", async (c) => {
  const slug = c.req.param("slug");
  const row = await c.env.DB.prepare(
    `SELECT j.id, j.ticker, j.trade_date, j.finished_at,
            r.r2_key_final, r.summary_extract, r.decision_action,
            r.decision_price_target, r.sentiment_band
       FROM jobs j
       JOIN reports r ON r.job_id = j.id
      WHERE j.share_slug = ? AND j.is_public = 1`,
  )
    .bind(slug)
    .first<Record<string, unknown>>();
  if (!row) return c.json({ error: "not_found" }, 404);

  const format = c.req.query("format");
  const reportUrl = row.r2_key_final
    ? await presignReportUrl(c.env, row.r2_key_final as string, 300)
    : null;

  if (format === "json") return c.json({ ...row, report_url: reportUrl });

  // Bare-bones HTML wrapper — the SPA will replace this with a nicer page.
  return c.html(
    `<!DOCTYPE html><html lang="zh"><meta charset="utf-8"><title>${row.ticker} · ${row.trade_date}</title>
     <body style="font-family:system-ui;max-width:720px;margin:2rem auto;padding:0 1rem">
       <h1>${row.ticker} — ${row.trade_date}</h1>
       <p><b>Action:</b> ${row.decision_action ?? "—"} ·
          <b>Target:</b> ${row.decision_price_target ?? "—"} ·
          <b>Sentiment:</b> ${row.sentiment_band ?? "—"}</p>
       <hr>
       <p>${row.summary_extract ?? ""}</p>
       ${reportUrl ? `<p><a href="${reportUrl}">Download full report ↗</a></p>` : ""}
     </body></html>`,
  );
});

export default app;
