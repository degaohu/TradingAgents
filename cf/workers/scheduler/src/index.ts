/**
 * Scheduler Worker — runs every minute (see wrangler.toml [triggers]).
 *
 * Reads schedules whose next_run_at <= now(), enqueues jobs for them,
 * then advances next_run_at using their cron expression.
 *
 * ULID generation & cron-next both live in this file to keep the
 * scheduler script self-contained (no cross-worker imports).
 */

interface Env {
  DB: D1Database;
}

interface ScheduleRow {
  id: string;
  user_id: string;
  ticker: string;
  cron_expr: string;
  config_json: string;
}

function nowIso(): string {
  return new Date().toISOString();
}

// ── ULID ─────────────────────────────────────────────────────
const ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
function ulid(): string {
  let ts = Date.now();
  let time = "";
  for (let i = 9; i >= 0; i--) {
    time = ENCODING[ts % 32] + time;
    ts = Math.floor(ts / 32);
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let rand = "";
  for (const b of bytes) rand += ENCODING[b % 32];
  return time + rand;
}

// ── Minimal cron-next ─────────────────────────────────────────
// Supports "*", "N", "*/N", "A-B", comma lists. No named month/day.
function parseField(expr: string, min: number, max: number): number[] {
  const out = new Set<number>();
  for (const part of expr.split(",")) {
    if (part === "*") {
      for (let i = min; i <= max; i++) out.add(i);
      continue;
    }
    const stepMatch = part.match(/^\*\/(\d+)$/);
    if (stepMatch) {
      const step = Number(stepMatch[1]);
      for (let i = min; i <= max; i += step) out.add(i);
      continue;
    }
    const rangeMatch = part.match(/^(\d+)-(\d+)$/);
    if (rangeMatch) {
      const [a, b] = [Number(rangeMatch[1]), Number(rangeMatch[2])];
      for (let i = a; i <= b; i++) out.add(i);
      continue;
    }
    const n = Number(part);
    if (Number.isFinite(n)) out.add(n);
  }
  return [...out].sort((a, b) => a - b);
}

function cronNext(expr: string, after: Date): Date {
  const [m, h, dom, mon, dow] = expr.split(/\s+/);
  const mins = parseField(m, 0, 59);
  const hours = parseField(h, 0, 23);
  const doms = parseField(dom, 1, 31);
  const mons = parseField(mon, 1, 12);
  const dows = parseField(dow, 0, 6);

  const t = new Date(after.getTime() + 60_000); // start from next minute
  t.setUTCSeconds(0, 0);
  for (let guard = 0; guard < 366 * 24 * 60; guard++) {
    if (
      mins.includes(t.getUTCMinutes()) &&
      hours.includes(t.getUTCHours()) &&
      doms.includes(t.getUTCDate()) &&
      mons.includes(t.getUTCMonth() + 1) &&
      dows.includes(t.getUTCDay())
    ) {
      return t;
    }
    t.setTime(t.getTime() + 60_000);
  }
  throw new Error(`no next fire for cron ${expr}`);
}

export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(run(env));
  },
  // Also expose a manual trigger for testing.
  async fetch(_req: Request, env: Env): Promise<Response> {
    await run(env);
    return new Response("scheduler tick");
  },
};

async function run(env: Env): Promise<void> {
  const now = nowIso();
  const due = await env.DB.prepare(
    `SELECT id, user_id, ticker, cron_expr, config_json
       FROM schedules
      WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)
      LIMIT 100`,
  )
    .bind(now)
    .all<ScheduleRow>();
  const rows = due.results ?? [];
  const today = now.slice(0, 10);

  for (const s of rows) {
    const config = JSON.parse(s.config_json) as Record<string, unknown>;
    const jobId = ulid();
    const nextRun = cronNext(s.cron_expr, new Date()).toISOString();
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO jobs (
           id, user_id, ticker, trade_date, status,
           provider, deep_llm, quick_llm, config_json, created_at
         ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)`,
      ).bind(
        jobId,
        s.user_id,
        s.ticker,
        today,
        (config.provider as string) ?? "openai",
        (config.deep_llm as string) ?? "gpt-5.5",
        (config.quick_llm as string) ?? "gpt-5.4-mini",
        s.config_json,
        now,
      ),
      env.DB.prepare(
        `UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?`,
      ).bind(now, nextRun, s.id),
    ]);
  }
}
