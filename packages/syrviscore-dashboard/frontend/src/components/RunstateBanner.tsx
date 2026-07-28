import { useQuery } from "@tanstack/react-query";
import { PauseCircle } from "lucide-react";
import { getRunstate } from "../lib/api";

/** A cross-tab banner when the instance is intentionally halted (graceful
 * shutdown). Without it, a halted instance's exited containers read as an
 * outage. */
export function RunstateBanner() {
  const { data } = useQuery({
    queryKey: ["runstate"],
    queryFn: getRunstate,
    refetchInterval: 10000,
  });
  if (!data || data.state !== "halted") return null;

  const since = (data.at ?? "").slice(0, 16).replace("T", " ");
  return (
    <div className="mb-4 flex items-start gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
      <PauseCircle size={18} className="mt-0.5 shrink-0" />
      <div>
        <div className="font-semibold">
          Instance halted ({data.reason ?? "unknown"}, since {since})
        </div>
        <div className="text-xs text-amber-300/80">
          Services are intentionally stopped —{" "}
          {data.resume_on_boot
            ? "they resume automatically on the next boot"
            : "they stay down until resumed"}
          . Run <span className="font-mono">sudo syrvis resume</span> over SSH to bring
          everything back.
        </div>
      </div>
    </div>
  );
}
