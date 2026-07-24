import { useQuery } from "@tanstack/react-query";
import { ArrowUpCircle, CheckCircle2, Package } from "lucide-react";
import { getUpdates, type ImageUpdate } from "../lib/api";
import { Card } from "./ui";

/**
 * Updates card — the SyrvisCore platform release plus container-image updates.
 * Report-only: it never triggers a pull. Applying an update is a deliberate,
 * declarative act (`syrvis service set-image` for L2; a new release for core).
 */
export function UpdatesCard() {
  const { data } = useQuery({
    queryKey: ["updates"],
    queryFn: getUpdates,
    staleTime: 3_600_000,
  });
  if (!data) return null;

  const version = data.version ?? data;
  const images = data.images?.images ?? [];
  const withUpdate = images.filter((i) => i.update_available);
  const errored = images.filter((i) => i.error);
  const platformUpdate = version.update_available;

  // Nothing to show at all → hide the card entirely.
  if (!platformUpdate && !images.length) return null;

  return (
    <Card className="p-4">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        <Package size={14} /> Updates
      </div>

      {platformUpdate && (
        <a
          href="https://github.com/kevinteg/SyrvisCore/releases"
          target="_blank"
          rel="noreferrer"
          className="mb-3 flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200 transition hover:border-amber-400"
        >
          <ArrowUpCircle size={16} className="shrink-0" />
          <span>
            SyrvisCore <b>{version.latest}</b> available — you&rsquo;re on {version.current}
          </span>
        </a>
      )}

      {withUpdate.length > 0 ? (
        <ul className="space-y-1.5">
          {withUpdate.map((i: ImageUpdate) => (
            <li key={i.image} className="flex items-center justify-between gap-3 text-sm">
              <span className="truncate font-medium text-slate-100">{i.name}</span>
              <span className="shrink-0 font-mono text-xs text-slate-400">
                {i.current} <span className="text-accent">→ {i.latest}</span>
              </span>
            </li>
          ))}
        </ul>
      ) : images.length > 0 ? (
        <div className="flex items-center gap-2 text-sm text-slate-400">
          <CheckCircle2 size={16} className="text-emerald-400" />
          All {images.length} images up to date{data.images?.cached ? " (cached)" : ""}
        </div>
      ) : null}

      {errored.length > 0 && (
        <div className="mt-2 text-xs text-slate-500">
          {errored.length} image(s) could not be checked
        </div>
      )}
    </Card>
  );
}
