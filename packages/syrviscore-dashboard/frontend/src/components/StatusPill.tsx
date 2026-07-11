export const STATUS_META: Record<
  string,
  { label: string; dot: string; text: string; ring: string }
> = {
  ok: { label: "Healthy", dot: "bg-emerald-400", text: "text-emerald-300", ring: "ring-emerald-500/30" },
  degraded: { label: "Degraded", dot: "bg-amber-400", text: "text-amber-300", ring: "ring-amber-500/30" },
  down: { label: "Down", dot: "bg-rose-500", text: "text-rose-300", ring: "ring-rose-500/30" },
  not_configured: {
    label: "Not configured",
    dot: "bg-slate-500",
    text: "text-slate-400",
    ring: "ring-slate-500/20",
  },
};

export function statusMeta(status: string) {
  return STATUS_META[status] ?? STATUS_META.not_configured;
}

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const m = statusMeta(status);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full bg-base-800 px-2.5 py-1 text-xs font-medium ring-1 ${m.ring} ${m.text}`}
    >
      <span className={`h-2 w-2 rounded-full ${m.dot} ${status === "ok" ? "animate-pulse" : ""}`} />
      {label ?? m.label}
    </span>
  );
}
