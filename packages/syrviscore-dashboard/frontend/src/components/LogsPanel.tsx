import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getServices } from "../lib/api";
import { Card, Spinner } from "./ui";

export function LogsPanel() {
  const { data } = useQuery({ queryKey: ["services"], queryFn: getServices });

  const services = useMemo(() => {
    const names = [
      ...(data?.core.items ?? []).map((s) => s.service ?? s.name),
      ...(data?.layer2.items ?? []).map((s) => s.name),
      "syrviscore-dashboard",
    ].filter((n): n is string => Boolean(n));
    return Array.from(new Set(names));
  }, [data]);

  const [selected, setSelected] = useState("");
  const [lines, setLines] = useState<string[]>([]);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!selected) return;
    setLines([]);
    const es = new EventSource(`/api/logs/${encodeURIComponent(selected)}?stream=true&tail=200`);
    es.addEventListener("log", (e) =>
      setLines((prev) => [...prev, (e as MessageEvent).data].slice(-2000)),
    );
    return () => es.close();
  }, [selected]);

  useEffect(() => {
    boxRef.current?.scrollTo(0, boxRef.current.scrollHeight);
  }, [lines]);

  return (
    <Card className="flex h-[70vh] flex-col">
      <div className="flex items-center gap-2 border-b border-base-700 p-3">
        <label className="text-sm text-slate-400">Service</label>
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          className="rounded-lg border border-base-600 bg-base-900 px-3 py-1.5 text-sm text-slate-200 outline-none focus:border-accent"
        >
          <option value="">Select…</option>
          {services.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>
      <div
        ref={boxRef}
        className="scroll-thin flex-1 overflow-auto whitespace-pre-wrap bg-base-900 p-3 font-mono text-xs leading-relaxed text-slate-300"
      >
        {!selected && <div className="text-slate-500">Pick a service to stream its logs.</div>}
        {selected && lines.length === 0 && <Spinner label="Streaming…" />}
        {lines.map((l, i) => (
          <div key={i}>{l}</div>
        ))}
      </div>
    </Card>
  );
}
