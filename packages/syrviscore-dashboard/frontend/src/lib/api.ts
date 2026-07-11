// Typed client for the dashboard backend. Same-origin fetch so the Cloudflare
// Access cookie / local session cookie ride along automatically.

export type Status = "ok" | "degraded" | "down" | "not_configured";
export type Overall = "ok" | "degraded" | "down";

export interface ProbeResult {
  component: string;
  status: Status;
  detail: string;
  latency_ms: number | null;
  extra: Record<string, unknown>;
}

export interface HealthSnapshot {
  generated_at: string;
  overall: Overall;
  healthy: boolean;
  components: Record<string, ProbeResult>;
}

export interface ServiceItem {
  service?: string;
  name?: string;
  status?: string;
  uptime?: string;
  image?: string;
  version?: string;
  url?: string;
  description?: string;
}

export interface ServicesResponse {
  core: { items: ServiceItem[]; error?: string };
  layer2: { items: ServiceItem[]; error?: string };
}

export interface RedactedConfig {
  install_path?: string | null;
  active_version?: string | null;
  domain: string;
  env_path?: string | null;
  values: Record<string, string>;
  enabled_components: Record<string, boolean>;
}

export interface SshAction {
  id: string;
  title: string;
  description: string;
  ssh_command: string;
  why_privileged: string;
}

export interface Me {
  email?: string | null;
  sub?: string | null;
  name?: string | null;
  via: string;
}

export interface Info {
  dashboard_version: string;
  install_path?: string | null;
  active_version?: string | null;
  setup_complete?: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, { credentials: "same-origin", ...init });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export const getHealth = () => api<HealthSnapshot>("/api/health");
export const getServices = () => api<ServicesResponse>("/api/services");
export const getConfig = () => api<RedactedConfig>("/api/config");
export const getInfo = () => api<Info>("/api/info");
export const getMe = () => api<Me>("/api/me");
export const getSystemActions = () => api<{ actions: SshAction[] }>("/api/system/actions");

export const coreAction = (service: string, action: string) =>
  api<{ ok: boolean; message: string }>(`/api/core/${service}/${action}`, { method: "POST" });

export const serviceAction = (name: string, action: string) =>
  api<{ ok: boolean; message: string }>(`/api/services/${name}/${action}`, { method: "POST" });

export const removeService = (name: string, purge: boolean) =>
  api<{ ok: boolean; message: string }>(
    `/api/services/${encodeURIComponent(name)}?purge=${purge}`,
    { method: "DELETE" },
  );

export const addService = (source: string, start: boolean) =>
  api<{ ok: boolean; message: string }>(`/api/services`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source, start }),
  });

export interface LinkItem {
  name: string;
  url: string;
  description?: string;
  category: string;
}
export interface LinksResponse {
  domain: string | null;
  links: LinkItem[];
}
export interface Updates {
  current: string | null;
  latest: string | null;
  update_available: boolean;
  dashboard_version?: string;
  error?: string;
}

export const getLinks = () => api<LinksResponse>("/api/links");
export const getUpdates = () => api<Updates>("/api/updates");

export type RouteKind = "core" | "synology" | "service";
export type Exposure = "internal" | "tunnel";
export type RouteHealth = "ok" | "degraded" | "down" | "unknown";

export interface RouteReachability {
  status: RouteHealth;
  http_code: number | null;
  detail: string;
}

export interface RouteEntry {
  service: string;
  kind: RouteKind;
  subdomain: string;
  hostname: string;
  exposure: Exposure;
  enabled: boolean;
  access_required: boolean;
  managed: boolean;
  router_present: boolean;
  reachability: RouteReachability;
}

export interface RoutesResponse {
  domain: string | null;
  traefik_ip: string | null;
  traefik_api_ok: boolean;
  entries: RouteEntry[];
  error?: string;
  note?: string;
}

export const getRoutes = () => api<RoutesResponse>("/api/routes");
