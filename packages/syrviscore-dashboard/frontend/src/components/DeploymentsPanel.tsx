import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Undo2 } from "lucide-react";
import {
  getDeployments,
  getInfo,
  rollbackService,
  type DeploymentRecord,
} from "../lib/api";
import { Button, Card, ErrorNote, Spinner } from "./ui";

const ACTION_TONES: Record<string, string> = {
  deploy: "bg-sky-500/15 text-sky-300",
  rollback: "bg-amber-500/15 text-amber-300",
  remove: "bg-rose-500/15 text-rose-300",
  "stack-apply": "bg-violet-500/15 text-violet-300",
};

function ActionBadge({ action }: { action: string }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
        ACTION_TONES[action] ?? "bg-base-700 text-slate-400"
      }`}
    >
      {action}
    </span>
  );
}

function OutcomeDot({ outcome }: { outcome: string }) {
  const tone = outcome === "success" ? "bg-emerald-400" : "bg-rose-500";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
      <span className={`h-2 w-2 rounded-full ${tone}`} />
      {outcome}
    </span>
  );
}

function when(ts?: string) {
  return (ts ?? "").slice(0, 16).replace("T", " ");
}

/** The image cell: transition arrow for upgrades, pin diff for @core records. */
function imageCell(r: DeploymentRecord): ReactNode {
  if (r.tier === "core") {
    const pins = r.pins ?? {};
    const prev = r.previous_pins ?? {};
    const changed = Object.keys(pins).filter((svc) => prev[svc] !== pins[svc]);
    return changed.length ? `pins changed: ${changed.join(", ")}` : "(enabled set changed)";
  }
  const parts: string[] = [];
  if (r.previous_image && r.previous_image !== r.image) parts.push(`${r.previous_image} →`);
  parts.push(r.image ?? "—");
  if (r.rollback_of) parts.push(`(rollback of #${r.rollback_of})`);
  return parts.join(" ");
}

function WorkloadCard({
  name,
  records,
  l2Enabled,
  busy,
  onRollback,
}: {
  name: string;
  records: DeploymentRecord[];
  l2Enabled: boolean;
  busy: string | null;
  onRollback: (name: string, revision: number) => void;
}) {
  const newest = records[0]?.revision;
  const isCore = name === "@core";
  return (
    <Card>
      <div className="flex items-center justify-between border-b border-base-700 px-4 py-3">
        <span className="flex items-center gap-2 text-sm font-semibold text-slate-200">
          <History size={14} className="text-slate-500" />
          {isCore ? "Core stack (@core)" : name}
        </span>
        <span className="text-xs text-slate-500">{records.length} revision(s)</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-slate-500">
              <th className="px-4 py-2 font-medium">Rev</th>
              <th className="px-2 py-2 font-medium">When</th>
              <th className="px-2 py-2 font-medium">Action</th>
              <th className="px-2 py-2 font-medium">Trigger</th>
              <th className="px-2 py-2 font-medium">Outcome</th>
              <th className="px-2 py-2 font-medium">Image</th>
              <th className="px-2 py-2" />
            </tr>
          </thead>
          <tbody>
            {records.map((r) => {
              // A revision is a rollback target when it deployed successfully,
              // is not the newest state, and belongs to an L2 service.
              const canRollback =
                l2Enabled &&
                !isCore &&
                r.revision !== newest &&
                r.outcome === "success" &&
                (r.action === "deploy" || r.action === "rollback");
              return (
                <tr key={r.revision} className="border-t border-base-700/60">
                  <td className="px-4 py-2 font-mono text-xs text-slate-400">#{r.revision}</td>
                  <td className="px-2 py-2 whitespace-nowrap text-xs text-slate-400">
                    {when(r.timestamp)}
                  </td>
                  <td className="px-2 py-2">
                    <ActionBadge action={r.action} />
                  </td>
                  <td className="px-2 py-2 text-xs text-slate-500">{r.trigger}</td>
                  <td className="px-2 py-2">
                    <OutcomeDot outcome={r.outcome} />
                  </td>
                  <td className="max-w-[24rem] truncate px-2 py-2 font-mono text-xs text-slate-400">
                    {imageCell(r)}
                  </td>
                  <td className="px-2 py-2 text-right">
                    {canRollback && (
                      <Button
                        disabled={busy !== null}
                        onClick={() => onRollback(name, r.revision)}
                      >
                        <Undo2 size={12} /> Roll back
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function DeploymentsPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["deployments"],
    queryFn: () => getDeployments(20),
    refetchInterval: 15000,
  });
  const { data: info } = useQuery({ queryKey: ["info"], queryFn: getInfo, refetchInterval: 60000 });
  const l2Enabled = info?.enable_l2_mutations ?? false;
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  async function rollback(name: string, revision: number) {
    if (
      !window.confirm(
        `Roll '${name}' back to revision #${revision}? The prior image is pulled and ` +
          `redeployed; data and secrets are left in place. Note: the next IaC apply ` +
          `re-pins the newer image unless the deployment repo is reverted too.`,
      )
    )
      return;
    setBusy(`${name}:${revision}`);
    setMsg(null);
    try {
      const r = await rollbackService(name, revision);
      setMsg(r.message);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "error");
    } finally {
      setBusy(null);
      qc.invalidateQueries({ queryKey: ["deployments"] });
      qc.invalidateQueries({ queryKey: ["services"] });
    }
  }

  if (isLoading) return <Spinner label="Loading deployment history…" />;
  if (error) return <ErrorNote error={error as Error} />;

  const workloads = data?.workloads ?? {};
  const invalid = data?.invalid ?? [];
  // Services alphabetically, the core stack's own record set last.
  const names = Object.keys(workloads).sort((a, b) =>
    a === "@core" ? 1 : b === "@core" ? -1 : a.localeCompare(b),
  );

  return (
    <div className="space-y-6">
      {msg && <div className="rounded-lg bg-base-800 px-4 py-2 text-sm text-slate-300">{msg}</div>}
      {data?.error && <ErrorNote error={data.error} />}

      {names.map((name) => (
        <WorkloadCard
          key={name}
          name={name}
          records={workloads[name]}
          l2Enabled={l2Enabled}
          busy={busy}
          onRollback={rollback}
        />
      ))}

      {names.length === 0 && !data?.error && (
        <Card>
          <div className="px-4 py-6 text-sm text-slate-500">
            No deployment history yet — revisions appear as services are deployed, updated,
            rolled back, or removed.
          </div>
        </Card>
      )}

      {invalid.length > 0 && (
        <Card>
          <div className="px-4 py-3">
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-300">
              <div className="mb-1 font-semibold">Unreadable deployment records</div>
              {invalid.map((row) => (
                <div key={row.file} className="font-mono">
                  {row.file}: {row.error}
                </div>
              ))}
            </div>
          </div>
        </Card>
      )}

      {!l2Enabled && (
        <div className="text-xs text-slate-500">
          Rollback from the dashboard is disabled (
          <span className="font-mono">ENABLE_L2_MUTATIONS=false</span>). Use{" "}
          <span className="font-mono">sudo syrvis service rollback --to N -- &lt;name&gt;</span>{" "}
          over SSH.
        </div>
      )}
    </div>
  );
}
