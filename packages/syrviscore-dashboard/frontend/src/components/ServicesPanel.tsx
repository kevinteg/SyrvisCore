import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart3, ExternalLink, Play, Plus, RefreshCw, RotateCw, Square, Trash2 } from "lucide-react";
import {
  addService,
  coreAction,
  getDeclarations,
  getInfo,
  getServices,
  metricsUrl,
  removeService,
  serviceAction,
  type DeclarationItem,
  type ServiceItem,
} from "../lib/api";
import { StatusPill } from "./StatusPill";
import { ActionMenu, Button, Card, ErrorNote, Spinner, type MenuItem } from "./ui";

function dot(status?: string) {
  if (status === "running") return "bg-emerald-400";
  if (status === "exited" || status === "stopped") return "bg-rose-500";
  return "bg-slate-500";
}

const BADGE_TONES = {
  slate: "bg-base-700 text-slate-400",
  sky: "bg-sky-500/15 text-sky-300",
  amber: "bg-amber-500/15 text-amber-300",
  rose: "bg-rose-500/15 text-rose-300",
} as const;

function Badge({ tone = "slate", children }: { tone?: keyof typeof BADGE_TONES; children: ReactNode }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  );
}

// Drift pill: in_sync green, pending_* amber (named after the plan action),
// unmanaged/disabled gray — mirrors the /api/declarations `state` field.
function driftPill(state: string) {
  if (state === "in_sync") return <StatusPill status="ok" label="In sync" />;
  if (state.startsWith("pending_"))
    return <StatusPill status="degraded" label={`Pending: ${state.slice("pending_".length)}`} />;
  if (state === "disabled") return <StatusPill status="not_configured" label="Disabled" />;
  return <StatusPill status="not_configured" label="Unmanaged" />;
}

function DeclarationsCard() {
  const { data } = useQuery({
    queryKey: ["declarations"],
    queryFn: getDeclarations,
    refetchInterval: 15000,
  });
  if (!data) return null;

  const items = data.services ?? [];
  const invalid = data.invalid ?? [];

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-base-700 px-4 py-3">
        <span className="text-sm font-semibold text-slate-200">Declared services (services.d)</span>
        {data.summary && (
          <span className="text-xs text-slate-500">
            {data.summary.declared} declared · {data.summary.total_actions} pending
          </span>
        )}
      </div>
      {data.error && (
        <div className="px-4 py-3">
          <ErrorNote error={data.error} />
        </div>
      )}
      {items.map((d: DeclarationItem) => (
        <div
          key={d.name}
          className="flex flex-col gap-2 border-b border-base-700 px-4 py-3 last:border-0 sm:flex-row sm:items-center sm:justify-between"
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`h-2 w-2 shrink-0 rounded-full ${dot(d.status)}`} />
              <span className="font-medium text-slate-100">{d.name}</span>
              {d.declared ? <Badge tone="sky">Declared</Badge> : <Badge>Unmanaged</Badge>}
              {d.enabled === false && <Badge tone="amber">Disabled</Badge>}
              {d.critical && <Badge tone="rose">Critical</Badge>}
              <span className="text-xs text-slate-500">{d.status}</span>
            </div>
            {(d.image || d.subdomain) && (
              <div className="truncate pl-4 font-mono text-xs text-slate-500">
                {d.image ?? ""}
                {d.subdomain && ` → ${d.subdomain}${d.exposure ? ` (${d.exposure})` : ""}`}
              </div>
            )}
          </div>
          <div className="shrink-0 pl-4 sm:pl-0">{driftPill(d.state)}</div>
        </div>
      ))}
      {items.length === 0 && !data.error && (
        <div className="px-4 py-3 text-sm text-slate-500">No services declared.</div>
      )}
      {invalid.length > 0 && (
        <div className="border-t border-base-700 px-4 py-3">
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-300">
            <div className="mb-1 font-semibold">Invalid declaration files</div>
            {invalid.map((row) => (
              <div key={row.file} className="font-mono">
                {row.file}: {row.error}
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

export function ServicesPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["services"],
    queryFn: getServices,
    refetchInterval: 8000,
  });
  const { data: info } = useQuery({ queryKey: ["info"], queryFn: getInfo, refetchInterval: 60000 });
  const l2Enabled = info?.enable_l2_mutations ?? false;
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [gitUrl, setGitUrl] = useState("");

  // Open (Traefik URL) + Metrics (Grafana deep-link) — shown whenever available,
  // independent of mutation permissions since they only navigate.
  const linkItems = (name: string, url?: string): MenuItem[] => {
    const items: MenuItem[] = [];
    if (url) items.push({ kind: "link", label: "Open", icon: <ExternalLink size={13} />, href: url });
    const m = metricsUrl(info, name);
    if (m) items.push({ kind: "link", label: "Metrics", icon: <BarChart3 size={13} />, href: m });
    return items;
  };

  async function act(key: string, fn: () => Promise<{ ok: boolean; message: string }>) {
    setBusy(key);
    setMsg(null);
    try {
      const r = await fn();
      setMsg(r.message);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "error");
    } finally {
      setBusy(null);
      qc.invalidateQueries({ queryKey: ["services"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    }
  }

  if (isLoading) return <Spinner label="Loading services…" />;
  if (error) return <ErrorNote error={error as Error} />;

  const core = data?.core.items ?? [];
  const layer2 = data?.layer2.items ?? [];

  const row = (name: string, image: string | undefined, status: string | undefined, actions: ReactNode) => (
    <div
      key={name}
      className="flex flex-col gap-2 border-b border-base-700 px-4 py-3 last:border-0 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 shrink-0 rounded-full ${dot(status)}`} />
          <span className="font-medium text-slate-100">{name}</span>
          <span className="text-xs text-slate-500">{status ?? ""}</span>
        </div>
        {image && <div className="truncate pl-4 font-mono text-xs text-slate-500">{image}</div>}
      </div>
      <div className="flex flex-wrap gap-1.5 pl-4 sm:pl-0">{actions}</div>
    </div>
  );

  return (
    <div className="space-y-6">
      {msg && <div className="rounded-lg bg-base-800 px-4 py-2 text-sm text-slate-300">{msg}</div>}

      <Card>
        <div className="border-b border-base-700 px-4 py-3 text-sm font-semibold text-slate-200">
          Core stack
        </div>
        {data?.core.error && <div className="px-4 py-3"><ErrorNote error={data.core.error} /></div>}
        {core.map((s: ServiceItem) => {
          const name = s.service ?? s.name ?? "?";
          // Core lifecycle (Docker SDK) is always available — it is NOT gated by
          // ENABLE_L2_MUTATIONS (that only covers compose/git-shelling L2 verbs).
          const items: MenuItem[] = [
            ...linkItems(name, s.url),
            { kind: "action", label: "Start", icon: <Play size={13} />, disabled: busy !== null, onClick: () => act(name + ":start", () => coreAction(name, "start")) },
            { kind: "action", label: "Restart", icon: <RotateCw size={13} />, disabled: busy !== null, onClick: () => act(name + ":restart", () => coreAction(name, "restart")) },
            { kind: "action", label: "Stop", icon: <Square size={13} />, disabled: busy !== null, onClick: () => act(name + ":stop", () => coreAction(name, "stop")) },
          ];
          return row(name, s.image, s.status, <ActionMenu items={items} disabled={busy !== null} />);
        })}
        {core.length === 0 && !data?.core.error && (
          <div className="px-4 py-3 text-sm text-slate-500">No core containers found.</div>
        )}
      </Card>

      <Card>
        <div className="border-b border-base-700 px-4 py-3 text-sm font-semibold text-slate-200">
          Layer 2 services
        </div>
        {layer2.map((s: ServiceItem) => {
          const name = s.name ?? "?";
          // L2 mutations (start/stop/restart/update/remove) shell compose+git and
          // are gated by ENABLE_L2_MUTATIONS. When disabled they are ABSENT from
          // the menu (never a button that 403s), with a note explaining why.
          const items: MenuItem[] = [...linkItems(name, s.url)];
          if (l2Enabled) {
            items.push(
              { kind: "action", label: "Start", icon: <Play size={13} />, disabled: busy !== null, onClick: () => act(name + ":start", () => serviceAction(name, "start")) },
              { kind: "action", label: "Restart", icon: <RotateCw size={13} />, disabled: busy !== null, onClick: () => act(name + ":restart", () => serviceAction(name, "restart")) },
              { kind: "action", label: "Stop", icon: <Square size={13} />, disabled: busy !== null, onClick: () => act(name + ":stop", () => serviceAction(name, "stop")) },
              { kind: "action", label: "Update", icon: <RefreshCw size={13} />, disabled: busy !== null, onClick: () => act(name + ":update", () => serviceAction(name, "update")) },
              { kind: "action", label: "Remove", icon: <Trash2 size={13} />, danger: true, disabled: busy !== null, onClick: () => act(name + ":remove", () => removeService(name, false)) },
            );
          } else {
            items.push({ kind: "note", label: "Management disabled (read-only)" });
          }
          return row(name, s.image ?? s.version, s.status, <ActionMenu items={items} disabled={busy !== null} />);
        })}
        {layer2.length === 0 && (
          <div className="px-4 py-3 text-sm text-slate-500">No Layer 2 services installed.</div>
        )}
        {l2Enabled ? (
          <div className="flex flex-col gap-2 border-t border-base-700 px-4 py-3 sm:flex-row">
            <input
              value={gitUrl}
              onChange={(e) => setGitUrl(e.target.value)}
              placeholder="https://github.com/you/syrvis-service.git"
              className="flex-1 rounded-lg border border-base-600 bg-base-900 px-3 py-1.5 text-sm text-slate-200 outline-none focus:border-accent"
            />
            <Button
              disabled={busy !== null || !gitUrl}
              onClick={() => act("add", () => addService(gitUrl, true)).then(() => setGitUrl(""))}
            >
              <Plus size={13} /> Add service
            </Button>
          </div>
        ) : (
          <div className="border-t border-base-700 px-4 py-3 text-xs text-slate-500">
            Layer 2 management is disabled (<span className="font-mono">ENABLE_L2_MUTATIONS=false</span>). Use{" "}
            <span className="font-mono">syrvis service …</span> over SSH to add or change services.
          </div>
        )}
      </Card>

      <DeclarationsCard />
    </div>
  );
}
