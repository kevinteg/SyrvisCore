import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getHealth, HealthSnapshot } from "./api";

// Live health: react-query provides the initial fetch + a polling fallback, while
// an SSE EventSource pushes updates into the query cache and disables polling
// while the stream is connected.
export function useHealthStream() {
  const qc = useQueryClient();
  const [live, setLive] = useState(false);

  const query = useQuery<HealthSnapshot>({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: live ? false : 5000,
  });

  useEffect(() => {
    const es = new EventSource("/api/events");
    es.addEventListener("health", (e) => {
      try {
        qc.setQueryData(["health"], JSON.parse((e as MessageEvent).data));
        setLive(true);
      } catch {
        /* ignore malformed frame */
      }
    });
    es.onerror = () => setLive(false);
    return () => es.close();
  }, [qc]);

  return {
    snapshot: query.data,
    live,
    isLoading: query.isLoading,
    error: query.error as Error | null,
  };
}
