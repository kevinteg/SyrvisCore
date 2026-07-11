# syrviscore-dashboard

A live web **observability + safe-management** dashboard for a SyrvisCore instance.
It runs as a base-tier Docker service (alongside Traefik / Portainer / Cloudflared)
and imports the `syrviscore` library **in-process** — the third thin adapter over the
deterministic core, after the `syrvis` CLI and the MCP server.

- **See:** live health for the core stack, Portainer, Traefik, Cloudflared, and
  Cloudflare DDNS; drift; redacted config; streamed logs.
- **Manage (container-safe):** start/stop/restart core containers and Layer 2
  services. Host-root operations (setup, `verify --fix`, macvlan shim, version
  changes) are **not** run here — the API returns the exact `ssh <target> '…'`
  command to run instead.
- **Auth (pluggable):** Cloudflare Access (remote + hairpin) **or** a local OIDC
  session (Synology SSO Server by default; any OIDC IdP). `none` for LAN/dev.

## Layout

```
src/syrviscore_dashboard/   FastAPI backend (app factory, probes, aggregator, api, auth)
frontend/                   React + Vite + TypeScript + Tailwind SPA
Dockerfile                  multi-stage: node build SPA -> python runtime
```

## Local development

```bash
# backend (Python 3.10+), auth disabled, pointed at a SyrvisCore home
pip install -e "packages/syrviscore[dev]" -e "packages/syrviscore-dashboard[dev]"
DASHBOARD_AUTH_MODE=none SYRVIS_HOME=/path/to/syrviscore \
  python -m syrviscore_dashboard      # serves on :8000

# frontend (Node 20+)
cd packages/syrviscore-dashboard/frontend
npm install && npm run dev            # Vite dev server proxies /api -> :8000
```

## Security note

Management requires the Docker socket mounted **read-write** (the same trust level
as Portainer — effectively host root). Mount it `:ro` and leave
`ENABLE_L2_MUTATIONS=false` for a read-only deployment.
