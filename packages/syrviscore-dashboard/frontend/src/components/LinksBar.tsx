import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { getLinks, type LinkItem } from "../lib/api";
import { Card } from "./ui";

const ORDER = ["primordial", "synology", "service"];

export function LinksBar() {
  const { data } = useQuery({ queryKey: ["links"], queryFn: getLinks, staleTime: 60_000 });
  const links = data?.links ?? [];
  if (!links.length) return null;

  const sorted = [...links].sort(
    (a, b) => ORDER.indexOf(a.category) - ORDER.indexOf(b.category),
  );

  return (
    <Card className="p-4">
      <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Quick links
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {sorted.map((l: LinkItem) => (
          <a
            key={l.url}
            href={l.url}
            target="_blank"
            rel="noreferrer"
            className="group flex items-center justify-between gap-2 rounded-lg border border-base-700 bg-base-900 px-3 py-2 transition hover:border-accent"
          >
            <div className="min-w-0">
              <div className="truncate text-sm font-medium text-slate-100">{l.name}</div>
              {l.description && (
                <div className="truncate text-xs text-slate-500">{l.description}</div>
              )}
            </div>
            <ExternalLink
              size={14}
              className="shrink-0 text-slate-500 transition group-hover:text-accent"
            />
          </a>
        ))}
      </div>
    </Card>
  );
}
