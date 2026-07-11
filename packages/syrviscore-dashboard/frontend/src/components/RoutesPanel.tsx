import { useQuery } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { getRoutes, type RouteEntry } from "../lib/api";
import { Button, Card, ErrorNote, Spinner } from "./ui";
import { StatusPill, statusMeta } from "./StatusPill";

const KIND_BADGE: Record<string, { label: string; className: string }> = {
  core: { label: "Core", className: "bg-accent/15 text-accent" },
  synology: { label: "Synology", className: "bg-violet-500/15 text-violet-300" },
  service: { label: "Service", className: "bg-emerald-500/15 text-emerald-300" },
};

const EXPOSURE_BADGE: Record<string, { label: string; className: string }> = {
  internal: { label: "LAN", className: "bg-sky-500/15 text-sky-300" },
  tunnel: { label: "Tunnel", className: "bg-amber-500/15 text-amber-300" },
};

function Badge({ label, className }: { label: string; className: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${className}`}
    >
      {label}
    </span>
  );
}

function reachabilityLabel(r: RouteEntry) {
  const { status, http_code } = r.reachability;
  const base =
    status === "unknown" ? "Unknown" : status === "ok" ? "Reachable" : statusMeta(status).label;
  return http_code != null ? `${base} · ${http_code}` : base;
}

function RouteRow({ r }: { r: RouteEntry }) {
  const kind = KIND_BADGE[r.kind] ?? KIND_BADGE.service;
  const exposure = EXPOSURE_BADGE[r.exposure] ?? EXPOSURE_BADGE.internal;
  const reachable = r.reachability.status === "ok";

  return (
    <tr className="border-b border-base-700 last:border-0">
      <td className="px-4 py-3">
        {reachable ? (
          <a
            href={`https://${r.hostname}`}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-medium text-accent hover:underline"
          >
            {r.hostname}
            <ExternalLink size={12} className="opacity-60" />
          </a>
        ) : (
          <span className="font-medium text-slate-200">{r.hostname}</span>
        )}
        <div className="text-[11px] text-slate-500">
          {r.service}
          {!r.enabled && " · disabled"}
          {r.kind === "synology" && (
            <span className="text-slate-400"> · routed via Syrvis — not managed</span>
          )}
        </div>
      </td>
      <td className="px-4 py-3">
        <Badge label={kind.label} className={kind.className} />
      </td>
      <td className="px-4 py-3">
        <Badge label={exposure.label} className={exposure.className} />
      </td>
      <td className="px-4 py-3">
        <span title={r.reachability.detail}>
          <StatusPill status={r.reachability.status} label={reachabilityLabel(r)} />
        </span>
      </td>
    </tr>
  );
}

export function RoutesPanel() {
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["routes"],
    queryFn: getRoutes,
  });

  if (isLoading) return <Spinner label="Probing routes through Traefik…" />;
  if (error) return <ErrorNote error={error as Error} />;

  const entries = data?.entries ?? [];

  return (
    <div className="space-y-4">
      {data?.error && <ErrorNote error={data.error} />}

      <Card>
        <div className="flex flex-col gap-2 border-b border-base-700 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="text-sm font-semibold text-slate-200">Routes</div>
            <div className="text-xs text-slate-500">
              Every hostname routed through Traefik by this instance
              {data?.domain && <span className="font-mono"> · {data.domain}</span>}
            </div>
          </div>
          <div className="flex items-center gap-3">
            {data && !data.traefik_api_ok && (
              <span className="text-xs text-amber-300" title={data.note}>
                Traefik API unreachable — route state unknown
              </span>
            )}
            <Button disabled={isFetching} onClick={() => refetch()}>
              <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} /> Refresh
            </Button>
          </div>
        </div>

        {entries.length === 0 ? (
          <div className="px-4 py-3 text-sm text-slate-500">No routed hostnames found.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="px-4 py-2 font-medium">Hostname</th>
                  <th className="px-4 py-2 font-medium">Kind</th>
                  <th className="px-4 py-2 font-medium">Exposure</th>
                  <th className="px-4 py-2 font-medium">Reachability</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((r) => (
                  <RouteRow key={r.hostname} r={r} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
