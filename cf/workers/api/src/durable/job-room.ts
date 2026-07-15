/**
 * JobRoom — a Durable Object per job, in charge of:
 *   * accepting `POST /publish` from /internal chunk handlers
 *   * fanning events out to every browser subscribed via `GET /subscribe`
 *
 * Why a DO: SSE requires a long-lived connection, but a single Worker
 * request can't wait to broadcast to N unrelated other requests. DOs
 * pin a single JS object in one location that all subscribers connect
 * to, and it forwards each `publish` to every subscriber.
 *
 * Backfill: we retain the last 200 events in memory so a late-connecting
 * client can replay from an `event_id` cursor.
 */

interface JobEvent {
  event_id: number;
  event_type: string;
  payload: unknown;
  ts: number;
}

const HISTORY_CAP = 200;

export class JobRoom implements DurableObject {
  private history: JobEvent[] = [];
  private counter = 0;
  private subscribers = new Set<{ write: (ev: JobEvent) => void; close: () => void }>();

  constructor(private state: DurableObjectState, _env: unknown) {}

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/subscribe") return this.handleSubscribe(req);
    if (url.pathname === "/publish") return this.handlePublish(req);
    return new Response("not found", { status: 404 });
  }

  private async handlePublish(req: Request): Promise<Response> {
    const body = (await req.json().catch(() => null)) as {
      event_type?: string;
      payload?: unknown;
    } | null;
    if (!body?.event_type) return Response.json({ error: "missing_event_type" }, { status: 400 });
    this.counter += 1;
    const event: JobEvent = {
      event_id: this.counter,
      event_type: body.event_type,
      payload: body.payload ?? null,
      ts: Date.now(),
    };
    this.history.push(event);
    if (this.history.length > HISTORY_CAP) this.history.shift();
    for (const sub of this.subscribers) {
      try {
        sub.write(event);
      } catch {
        this.subscribers.delete(sub);
      }
    }
    // On terminal events, close subscribers so the browser sees end-of-stream.
    if (event.event_type === "finish") {
      for (const sub of this.subscribers) sub.close();
      this.subscribers.clear();
    }
    return Response.json({ ok: true });
  }

  private handleSubscribe(req: Request): Response {
    const url = new URL(req.url);
    const since = Number(url.searchParams.get("since") ?? 0);
    const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
    const writer = writable.getWriter();
    const encoder = new TextEncoder();

    const write = (ev: JobEvent) => {
      const chunk = `id: ${ev.event_id}\nevent: ${ev.event_type}\ndata: ${JSON.stringify(ev.payload)}\n\n`;
      void writer.write(encoder.encode(chunk));
    };
    const close = () => {
      void writer.close();
    };
    const sub = { write, close };
    this.subscribers.add(sub);

    // Replay any events past the `since` cursor.
    for (const ev of this.history) if (ev.event_id > since) write(ev);

    // If the client disconnects, drop them from the fanout set.
    req.signal.addEventListener("abort", () => {
      this.subscribers.delete(sub);
      close();
    });

    return new Response(readable, {
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
      },
    });
  }
}
