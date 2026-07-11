import type { ReactNode } from "react";
import { Boxes, Cloud, Globe, Network, Server, Settings2, type LucideIcon } from "lucide-react";
import { ProbeResult } from "../lib/api";
import { StatusPill } from "./StatusPill";

const META: Record<string, { title: string; Icon: LucideIcon }> = {
  core: { title: "Core stack", Icon: Server },
  traefik: { title: "Traefik", Icon: Network },
  portainer: { title: "Portainer", Icon: Boxes },
  cloudflared: { title: "Cloudflare Tunnel", Icon: Cloud },
  cloudflare_ddns: { title: "Cloudflare DDNS", Icon: Globe },
  config: { title: "Configuration", Icon: Settings2 },
};

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-xs">
      <span className="text-slate-500">{label}</span>
      <span className="truncate font-mono text-slate-300">{value}</span>
    </div>
  );
}

function Extra({ component, extra }: { component: string; extra: Record<string, unknown> }) {
  const e = extra ?? {};
  if (component === "core") {
    const containers = (e.containers ?? {}) as Record<string, { status?: string; image?: string }>;
    const drift = e.drift as { in_sync?: boolean } | null;
    return (
      <div className="space-y-1">
        {Object.entries(containers).map(([svc, info]) => (
          <div key={svc} className="flex items-center justify-between text-xs">
            <span className="flex items-center gap-1.5">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  info.status === "running" ? "bg-emerald-400" : "bg-rose-500"
                }`}
              />
              {svc}
            </span>
            <span className="font-mono text-slate-500">{info.status ?? "?"}</span>
          </div>
        ))}
        {drift && drift.in_sync === false && (
          <div className="mt-1 rounded bg-amber-500/10 px-2 py-1 text-xs text-amber-300">
            drift: containers don't match compose
          </div>
        )}
      </div>
    );
  }
  if (component === "traefik") {
    const routers = (e.routers ?? {}) as { total?: number };
    const names = (e.router_names ?? []) as string[];
    return (
      <div className="space-y-1">
        <Row label="routers" value={routers.total ?? names.length ?? "—"} />
        {names.length > 0 && (
          <div className="truncate font-mono text-xs text-slate-500">{names.join(", ")}</div>
        )}
      </div>
    );
  }
  if (component === "portainer") {
    return <Row label="version" value={(e.version as string) ?? "—"} />;
  }
  if (component === "cloudflared") {
    return <Row label="edge connections" value={String(e.readyConnections ?? "—")} />;
  }
  if (component === "cloudflare_ddns") {
    const records = (e.records ?? []) as { name: string; record_ip?: string; in_sync?: boolean }[];
    return (
      <div className="space-y-1">
        <Row label="public IP" value={(e.public_ip as string) ?? "—"} />
        {records.map((r) => (
          <div key={r.name} className="flex items-center justify-between text-xs">
            <span className="truncate">{r.name}</span>
            <span className={`font-mono ${r.in_sync ? "text-emerald-300" : "text-amber-300"}`}>
              {r.record_ip ?? "—"}
            </span>
          </div>
        ))}
      </div>
    );
  }
  if (component === "config") {
    const enabled = (e.enabled ?? []) as string[];
    return (
      <div className="space-y-1">
        <Row label="domain" value={(e.domain as string) || "unset"} />
        <Row label="enabled" value={enabled.length ? enabled.join(", ") : "none"} />
      </div>
    );
  }
  return null;
}

export function ComponentCard({ probe }: { probe: ProbeResult }) {
  const meta = META[probe.component] ?? { title: probe.component, Icon: Server };
  const { Icon } = meta;
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-base-700 bg-base-800 p-4 shadow-sm transition hover:border-base-600">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2.5">
          <span className="rounded-lg bg-base-700 p-2 text-slate-300">
            <Icon size={18} />
          </span>
          <div>
            <div className="font-semibold text-slate-100">{meta.title}</div>
            {probe.latency_ms != null && (
              <div className="text-xs text-slate-500">{probe.latency_ms} ms</div>
            )}
          </div>
        </div>
        <StatusPill status={probe.status} />
      </div>
      <p className="text-sm text-slate-400">{probe.detail || "—"}</p>
      <div className="border-t border-base-700 pt-2">
        <Extra component={probe.component} extra={probe.extra} />
      </div>
    </div>
  );
}
