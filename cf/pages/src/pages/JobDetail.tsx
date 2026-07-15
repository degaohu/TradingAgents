import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type Job } from "@/lib/api";

interface JobDetailResponse {
  job: Job & Record<string, unknown>;
  report_url: string | null;
}

interface StageEvent {
  event_id: number;
  event_type: string;
  data: unknown;
}

export default function JobDetail() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<JobDetailResponse | null>(null);
  const [events, setEvents] = useState<StageEvent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  // Initial fetch
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    api<JobDetailResponse>(`/api/jobs/${id}`)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setErr(String(e)));
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Live SSE while running
  useEffect(() => {
    if (!id || !data?.job) return;
    if (data.job.status !== "queued" && data.job.status !== "running") return;
    const es = new EventSource(`/api/jobs/${id}/events`);
    esRef.current = es;
    const onMsg = (ev: MessageEvent) => {
      const parsed = safeParse(ev.data);
      setEvents((prev) => [
        ...prev,
        { event_id: Number(ev.lastEventId) || 0, event_type: ev.type, data: parsed },
      ]);
      if (ev.type === "finish") {
        es.close();
        // Refetch to pull report_url + full metadata.
        api<JobDetailResponse>(`/api/jobs/${id}`).then(setData).catch(console.error);
      }
    };
    ["stage", "chunk", "finish", "message"].forEach((t) => es.addEventListener(t, onMsg as EventListener));
    es.onerror = () => es.close();
    return () => {
      es.close();
    };
  }, [id, data?.job?.status]);

  if (err) return <div className="text-neg">{err}</div>;
  if (!data) return <div className="text-ink-3">加载中…</div>;

  const j = data.job;
  return (
    <div>
      <div className="flex items-baseline gap-3 mb-4">
        <h1 className="font-display text-2xl font-semibold tracking-tight">{j.ticker}</h1>
        <span className="text-ink-3 font-mono text-[13px]">· {j.trade_date}</span>
        <span
          className={`ml-auto text-[11px] px-2 py-0.5 rounded ${
            j.status === "done"
              ? "bg-pos-tint text-pos"
              : j.status === "failed"
              ? "bg-neg-tint text-neg"
              : "bg-amber-tint text-amber-ink"
          }`}
        >
          {j.status}
        </span>
      </div>

      <div className="text-ink-3 text-[11.5px] font-mono mb-6">{j.id}</div>

      {events.length > 0 && (
        <div className="border border-line rounded-lg bg-white p-4 mb-6">
          <div className="text-[11px] uppercase tracking-wider font-semibold text-ink-3 mb-2">
            实时进度
          </div>
          <ol className="space-y-1 font-mono text-[12px]">
            {events.map((e, i) => (
              <li key={i} className="text-ink-2">
                <span className="text-ink-4">#{e.event_id}</span>{" "}
                <span className="text-amber-ink">{e.event_type}</span>{" "}
                <span>{typeof e.data === "string" ? e.data : JSON.stringify(e.data)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {data.report_url && (
        <a
          href={data.report_url}
          className="inline-block px-3 py-1.5 rounded-md bg-ink text-white text-[13px]"
        >
          下载完整报告
        </a>
      )}
    </div>
  );
}

function safeParse(x: string): unknown {
  try {
    return JSON.parse(x);
  } catch {
    return x;
  }
}
