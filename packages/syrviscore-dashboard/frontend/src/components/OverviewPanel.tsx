import type { HealthSnapshot } from "../lib/api";
import { ComponentCard } from "./ComponentCard";
import { ErrorNote, Spinner } from "./ui";

const ORDER = ["core", "traefik", "portainer", "cloudflared", "cloudflare_ddns", "config"];

export function OverviewPanel({
  snapshot,
  isLoading,
  error,
}: {
  snapshot?: HealthSnapshot;
  isLoading: boolean;
  error: Error | null;
}) {
  if (isLoading && !snapshot) return <Spinner label="Probing components…" />;
  if (error && !snapshot) return <ErrorNote error={error} />;

  const components = snapshot?.components ?? {};
  const keys = [
    ...ORDER.filter((k) => components[k]),
    ...Object.keys(components).filter((k) => !ORDER.includes(k)),
  ];

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {keys.map((k) => (
        <ComponentCard key={k} probe={components[k]} />
      ))}
    </div>
  );
}
