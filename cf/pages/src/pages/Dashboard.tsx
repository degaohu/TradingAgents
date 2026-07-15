import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Job } from "@/lib/api";

const STATUS_LABEL: Record<Job["status"], string> = {
  queued: "排队中",
  running: "分析中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export default function Dashboard() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const params = new URLSearchParams();
        if (q) params.set("q", q);
        const res = await api<{ jobs: Job[] }>(`/api/jobs?${params}`);
        if (!cancelled) setJobs(res.jobs);
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [q]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight">分析报告</h1>
          <p className="text-ink-3 text-[13px] mt-1">
            所有你跑过的分析。点击进入查看完整报告和交易结论。
          </p>
        </div>
        <Link
          to="/new"
          className="px-3 py-1.5 rounded-md bg-ink text-white text-[13px] font-medium shadow-sm hover:bg-black"
        >
          + 新建分析
        </Link>
      </div>

      <input
        type="text"
        placeholder="按股票代码搜索…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        className="w-full mb-4 px-3 py-2 rounded-md border border-line bg-white text-[13px] focus:outline-none focus:ring-2 focus:ring-amber-tint focus:border-amber"
      />

      {err && <div className="text-neg text-sm mb-4">{err}</div>}

      {!jobs && !err && <div className="text-ink-3 text-sm">加载中…</div>}
      {jobs && jobs.length === 0 && (
        <div className="text-ink-3 text-sm border border-dashed border-line rounded-lg p-8 text-center">
          还没有任何分析报告。<Link to="/new" className="text-amber-ink underline">开始第一次分析</Link>。
        </div>
      )}

      {jobs && jobs.length > 0 && (
        <div className="border border-line rounded-lg overflow-hidden bg-white">
          {jobs.map((j) => (
            <Link
              key={j.id}
              to={`/jobs/${j.id}`}
              className="grid grid-cols-[1fr_120px_120px_100px] gap-4 items-center px-4 py-3 border-b border-line last:border-0 hover:bg-paper-2 transition"
            >
              <div>
                <div className="font-medium text-[13.5px]">{j.ticker}</div>
                <div className="text-ink-3 text-[11px] font-mono">{j.id}</div>
              </div>
              <div className="text-[12px] font-mono text-ink-2">{j.trade_date}</div>
              <div className="text-[12px] text-ink-3">
                {new Date(j.created_at).toLocaleString("zh-CN")}
              </div>
              <div>
                <span
                  className={`text-[11px] px-2 py-0.5 rounded ${
                    j.status === "done"
                      ? "bg-pos-tint text-pos"
                      : j.status === "failed"
                      ? "bg-neg-tint text-neg"
                      : j.status === "cancelled"
                      ? "bg-paper-2 text-ink-3"
                      : "bg-amber-tint text-amber-ink"
                  }`}
                >
                  {STATUS_LABEL[j.status]}
                </span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
