import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowUpCircle, Wifi, WifiOff } from "lucide-react";
import { getMe, getUpdates, type Overall } from "../lib/api";
import { StatusPill } from "./StatusPill";

export interface Tab {
  id: string;
  label: string;
}

export function Header({
  overall,
  live,
  tabs,
  tab,
  setTab,
}: {
  overall?: Overall;
  live: boolean;
  tabs: Tab[];
  tab: string;
  setTab: (id: string) => void;
}) {
  const { data: me } = useQuery({ queryKey: ["me"], queryFn: getMe });
  const { data: upd } = useQuery({
    queryKey: ["updates"],
    queryFn: getUpdates,
    staleTime: 3_600_000,
  });

  return (
    <header className="sticky top-0 z-10 border-b border-base-700 bg-base-900/80 backdrop-blur">
      <div className="mx-auto flex max-w-6xl flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/20 text-accent">
            <Activity size={18} />
          </div>
          <div>
            <div className="font-semibold leading-none text-slate-100">SyrvisCore</div>
            <div className="text-xs text-slate-500">infrastructure dashboard</div>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {upd?.update_available && (
            <a
              href="https://github.com/kevinteg/SyrvisCore/releases"
              target="_blank"
              rel="noreferrer"
              title={`SyrvisCore ${upd.latest} available — you're on ${upd.current}`}
              className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-1 text-xs font-medium text-amber-300 transition hover:bg-amber-500/25"
            >
              <ArrowUpCircle size={13} /> {upd.latest}
            </a>
          )}
          {overall && <StatusPill status={overall} />}
          <span title={live ? "live (SSE)" : "polling"}>
            {live ? (
              <Wifi size={16} className="text-emerald-400" />
            ) : (
              <WifiOff size={16} className="text-slate-500" />
            )}
          </span>
          {me?.email && me.via !== "none" && (
            <span className="hidden text-xs text-slate-400 sm:inline">{me.email}</span>
          )}
        </div>
      </div>
      <nav className="mx-auto flex max-w-6xl gap-1 overflow-x-auto px-4">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`shrink-0 border-b-2 px-3 py-2 text-sm transition ${
              tab === t.id
                ? "border-accent text-slate-100"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>
    </header>
  );
}
