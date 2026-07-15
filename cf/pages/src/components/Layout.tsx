import { Link, Outlet, useLocation } from "react-router-dom";
import { clsx } from "clsx";

const NAV = [
  { to: "/", label: "报告" },
  { to: "/new", label: "新建分析", accent: true },
  { to: "/schedules", label: "定时任务" },
  { to: "/billing", label: "账户" },
];

export default function Layout() {
  const loc = useLocation();
  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-line bg-paper/80 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto flex items-center gap-6 px-6 h-12">
          <Link to="/" className="font-display font-semibold tracking-tight text-[15px] flex items-center gap-2">
            <span className="w-5 h-5 rounded-md bg-ink relative inline-flex items-center justify-center">
              <span className="w-2 h-2 rounded-full bg-amber" />
            </span>
            TradingAgents
          </Link>
          <nav className="flex items-center gap-1 text-[13px]">
            {NAV.map((n) => {
              const active = loc.pathname === n.to;
              return (
                <Link
                  key={n.to}
                  to={n.to}
                  className={clsx(
                    "px-3 py-1.5 rounded-md transition",
                    active
                      ? "bg-white text-ink shadow-sm ring-1 ring-line"
                      : n.accent
                      ? "text-amber-ink hover:bg-amber-tint"
                      : "text-ink-2 hover:bg-paper-2 hover:text-ink",
                  )}
                >
                  {n.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto text-[11.5px] text-ink-3 font-mono">v0 · skeleton</div>
        </div>
      </header>
      <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
