import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Plus, RefreshCw, RotateCw, Square, Trash2 } from "lucide-react";
import {
  addService,
  coreAction,
  getServices,
  removeService,
  serviceAction,
  type ServiceItem,
} from "../lib/api";
import { Button, Card, ErrorNote, Spinner } from "./ui";

function dot(status?: string) {
  if (status === "running") return "bg-emerald-400";
  if (status === "exited" || status === "stopped") return "bg-rose-500";
  return "bg-slate-500";
}

export function ServicesPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["services"],
    queryFn: getServices,
    refetchInterval: 8000,
  });
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [gitUrl, setGitUrl] = useState("");

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
          return row(
            name,
            s.image,
            s.status,
            <>
              <Button disabled={busy !== null} onClick={() => act(name + ":start", () => coreAction(name, "start"))}>
                <Play size={13} /> Start
              </Button>
              <Button disabled={busy !== null} onClick={() => act(name + ":restart", () => coreAction(name, "restart"))}>
                <RotateCw size={13} /> Restart
              </Button>
              <Button variant="ghost" disabled={busy !== null} onClick={() => act(name + ":stop", () => coreAction(name, "stop"))}>
                <Square size={13} /> Stop
              </Button>
            </>,
          );
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
          return row(
            name,
            s.image ?? s.version,
            s.status,
            <>
              <Button disabled={busy !== null} onClick={() => act(name + ":start", () => serviceAction(name, "start"))}>
                <Play size={13} /> Start
              </Button>
              <Button disabled={busy !== null} onClick={() => act(name + ":restart", () => serviceAction(name, "restart"))}>
                <RotateCw size={13} /> Restart
              </Button>
              <Button disabled={busy !== null} onClick={() => act(name + ":update", () => serviceAction(name, "update"))}>
                <RefreshCw size={13} /> Update
              </Button>
              <Button variant="danger" disabled={busy !== null} onClick={() => act(name + ":remove", () => removeService(name, false))}>
                <Trash2 size={13} /> Remove
              </Button>
            </>,
          );
        })}
        {layer2.length === 0 && (
          <div className="px-4 py-3 text-sm text-slate-500">No Layer 2 services installed.</div>
        )}
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
      </Card>
    </div>
  );
}
