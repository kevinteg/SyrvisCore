import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Copy, Check, Terminal } from "lucide-react";
import { getSystemActions } from "../lib/api";
import { Card, Spinner } from "./ui";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className="shrink-0 rounded p-1.5 text-slate-400 hover:bg-base-700 hover:text-slate-200"
      title="Copy command"
    >
      {copied ? <Check size={14} className="text-emerald-400" /> : <Copy size={14} />}
    </button>
  );
}

export function SystemActions() {
  const { data, isLoading } = useQuery({ queryKey: ["system-actions"], queryFn: getSystemActions });
  if (isLoading) return <Spinner />;

  return (
    <Card>
      <div className="flex items-center gap-2 border-b border-base-700 px-4 py-3 text-sm font-semibold text-slate-200">
        <Terminal size={15} /> Privileged actions (run over SSH)
      </div>
      <p className="px-4 pt-3 text-xs text-slate-500">
        These need host root and are not run from the dashboard — copy and run them on the NAS.
      </p>
      <div className="space-y-3 p-4">
        {data?.actions.map((a) => (
          <div key={a.id} className="rounded-lg border border-base-700 bg-base-900 p-3">
            <div className="text-sm font-medium text-slate-200">{a.title}</div>
            <div className="mt-0.5 text-xs text-slate-500">{a.description}</div>
            <div className="mt-2 flex items-center gap-2 rounded bg-base-800 px-3 py-2">
              <code className="flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs text-accent">
                {a.ssh_command}
              </code>
              <CopyButton text={a.ssh_command} />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
