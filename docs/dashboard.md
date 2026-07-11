# SyrvisCore Dashboard

A live web **observability + safe-management** surface for a SyrvisCore instance.
It is the **third thin adapter** over the deterministic core library — after the
`syrvis` CLI and the MCP server — and the first one that runs *on* the NAS.

Package: `packages/syrviscore-dashboard/` · Image: `ghcr.io/kevinteg/syrviscore-dashboard`

## How it fits

Unlike the MCP server (which shells `syrvis --json` over SSH from the operator's
Mac), the dashboard runs as a **base-tier Docker service** on the NAS and imports
the `syrviscore` library **in-process**. It reuses the same functions the CLI does:

| Concern | Reused from `syrviscore` |
|---|---|
| Core container health + drift | `docker_manager.DockerManager`, `verify.gather_core_drift`, `drift` |
| Redacted config | `config_reader.read_config` (shared with `syrvis config show`) |
| Layer 2 services | `service_manager.ServiceManager` |
| Manifest / versions | `paths` |

Component health that the library doesn't cover is probed directly over the
`proxy` network: Traefik `:8080/ping` + `/api/overview`, Portainer `/api/status`,
Cloudflared `/ready` (via `TUNNEL_METRICS`), and Cloudflare DDNS (public IP vs the
Cloudflare A records). Every probe **degrades gracefully** — an absent or
unreachable component reports `down`/`degraded`/`not_configured`, never a 500.

```
Internet ─▶ Cloudflare Access ─▶ Tunnel ─▶ Traefik ─▶ syrviscore-dashboard
LAN ──────────────────────────────────────▶ Traefik ─▶  (FastAPI + React SPA)
                                                            │ imports syrviscore
                          docker.sock · $SYRVIS_HOME · traefik:8080 · portainer:9000 · cloudflared:20241
```

## Auth (pluggable)

A request is authenticated if it presents **either** a valid Cloudflare Access JWT
**or** a valid local OIDC session. `DASHBOARD_AUTH_MODE` selects which are active:

- `cloudflare` — verify `Cf-Access-Jwt-Assertion` against the team JWKS (`aud`+`iss`).
  Remote, and local-via-hairpin.
- `oidc` — the dashboard is an OIDC client (Auth Code + PKCE); **Synology SSO Server**
  is the default IdP (fully local), any OIDC provider works.
- `both` — accept either. `none` — LAN/dev bypass.

The server fails closed at startup if a selected provider is misconfigured.

## Scope: container-safe management

- **Core** start/stop/restart via the Docker SDK on the `CORE_SERVICES` allowlist
  (no compose binary, no macvlan shim).
- **Layer 2** add/remove/start/stop/update via `ServiceManager`, gated behind
  `ENABLE_L2_MUTATIONS` + the `WITH_L2_TOOLS` image (which adds docker-cli +
  compose + git).
- **Host-root ops** (`setup`, `verify --fix`, macvlan shim, version changes) are
  **never run here** — `/api/system/actions` returns the exact `ssh <target> '…'`
  command to run instead.

## Security

Management requires the docker socket mounted **read-write** — the same authority
as Portainer (effectively host root). Bound the risk with auth on every `/api/*`
route, `no-new-privileges`, the managed-container allowlist, and — for a read-only
posture — mounting the socket `:ro` with `ENABLE_L2_MUTATIONS=false`.

## Develop / build / ship

```bash
# backend (Python 3.10+)
pip install -e "packages/syrviscore" -e "packages/syrviscore-dashboard[dev]"
DASHBOARD_AUTH_MODE=none SYRVIS_HOME=/path python -m syrviscore_dashboard   # :8000
pytest packages/syrviscore-dashboard/tests

# frontend (Node 20+)
cd packages/syrviscore-dashboard/frontend && npm install && npm run dev      # Vite :5173

# image (built from the repo root so it can install the library)
make build-dashboard                 # WITH_L2_TOOLS=true PUSH=1 to enable L2 / push GHCR
```

Released images are pinned in `build/config.yaml` (`docker_images.dashboard`).

## Declaring + bootstrapping the container

Which core-tier containers run is **declared** in `config/stack.yaml` (see
`syrviscore/stack.py`). `traefik` + `portainer` are **primordial** (always on);
`cloudflared`, `dashboard`, and `cloudflare_ddns` are **opt-in**. The compose
generator only emits a service when it's declared enabled.

```bash
# CLI bootstraps the primordial containers the first time
sudo syrvis setup            # writes config/stack.yaml + .env, privileged setup
syrvis start                 # brings up the declared stack (+ macvlan shim)

# then declare + start the dashboard
syrvis stack enable dashboard --subdomain dash
syrvis stack apply           # regenerate docker-compose.yaml from the stack
syrvis start                 # bring the dashboard up

syrvis stack list            # declared vs running, with config hints
```

Once the dashboard is up you use it directly; it can restart core containers, but
the *initial* create + macvlan shim stays with the CLI (host-root, chicken-and-egg).
`config/stack.yaml`:

```yaml
version: 1
services:
  traefik:   { enabled: true }     # primordial
  portainer: { enabled: true }     # primordial
  cloudflared: { enabled: true }
  dashboard: { enabled: true, subdomain: dash }
  cloudflare_ddns: { enabled: false }
```
