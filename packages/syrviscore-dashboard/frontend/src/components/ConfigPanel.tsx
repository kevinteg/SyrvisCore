import { useQuery } from "@tanstack/react-query";
import { getConfig } from "../lib/api";
import { Card, ErrorNote, Spinner } from "./ui";
import { SystemActions } from "./SystemActions";

export function ConfigPanel() {
  const { data, isLoading, error } = useQuery({ queryKey: ["config"], queryFn: getConfig });
  if (isLoading) return <Spinner label="Loading config…" />;
  if (error) return <ErrorNote error={error as Error} />;

  const enabled = Object.entries(data?.enabled_components ?? {});
  const values = Object.entries(data?.values ?? {});

  return (
    <div className="space-y-6">
      <Card className="p-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <Meta label="Domain" value={data?.domain || "unset"} />
          <Meta label="Install path" value={data?.install_path ?? "—"} />
          <Meta label="Active version" value={data?.active_version ?? "—"} />
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {enabled.map(([name, on]) => (
            <span
              key={name}
              className={`rounded-full px-2.5 py-1 text-xs font-medium ${
                on ? "bg-emerald-500/15 text-emerald-300" : "bg-base-700 text-slate-500"
              }`}
            >
              {name}
            </span>
          ))}
        </div>
      </Card>

      <Card>
        <div className="border-b border-base-700 px-4 py-3 text-sm font-semibold text-slate-200">
          Configuration (secrets redacted)
        </div>
        <div className="divide-y divide-base-700">
          {values.map(([k, v]) => (
            <div key={k} className="flex items-center justify-between gap-4 px-4 py-2 text-sm">
              <span className="font-mono text-slate-400">{k}</span>
              <span
                className={`truncate font-mono ${v === "****" ? "text-amber-400/70" : "text-slate-200"}`}
              >
                {v || <span className="text-slate-600">(empty)</span>}
              </span>
            </div>
          ))}
          {values.length === 0 && (
            <div className="px-4 py-3 text-sm text-slate-500">No configuration found.</div>
          )}
        </div>
      </Card>

      <SystemActions />
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="truncate font-mono text-sm text-slate-200">{value}</div>
    </div>
  );
}
