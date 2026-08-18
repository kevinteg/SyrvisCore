# SERVICE CORE — configuration model, compose generation, core stack, networking/exposure, setup

## Purpose & role in the system

This is the layer that turns *declared intent* into *a running Docker Compose instance on a Synology NAS*. It answers four questions deterministically:

1. **Where am I installed?** (`paths.py` — SYRVIS_HOME resolution, versioned layout, manifest)
2. **What should run?** (`stack.yaml` for the core tier; `config/services.d` for Layer 2)
3. **What does the runtime look like?** (`compose.py` → `config/docker-compose.yaml`; `traefik_config.py` → `data/traefik/*`)
4. **What must the *outside world* do for this to be reachable?** (`exposure.py` + `hostnames.py` → the `stack hostnames` DNS-intent report, which SyrvisCore never acts on)

Everything else in the repo (CLI, MCP, dashboard) is an adapter over these modules — the "deterministic core, thin adapters" rule stated in `docs/wiki/01-architecture-overview.md`: *"Anything an adapter can do, `ssh nas && syrvis …` can do. Library modules raise typed errors and never print; `cli.py` is the only presentation layer."*

## Key modules and files (path — role — approx size)

All under `packages/syrviscore/src/syrviscore/` unless noted.

| File | Role | LOC |
|---|---|---|
| `paths.py` | SYRVIS_HOME resolution + volume auto-detect, versioned layout, manifest R/W, `.env` scalar reader, `.env` write-time hazard predicates, apps-root segment | 910 |
| `compose.py` | `DEFAULT_DOCKER_IMAGES` pins, `ComposeGenerator` (core tier only), network model | 622 |
| `stack.py` | `config/stack.yaml` model: primordial vs optional core services + settings | 177 |
| `traefik_config.py` | Static + dynamic Traefik config, `SYNOLOGY_SERVICES` / `PRIMORDIAL_UIS` catalogs, `ServiceTraefikConfig` (per-L2 file provider) | 702 |
| `config_reader.py` | The single redaction-aware `.env` reader (`read_config`) shared by CLI + dashboard | 127 |
| `exposure.py` | The two-value vocabulary `internal` / `tunnel` + `normalize()` | 56 |
| `hostnames.py` | `build_report()` — the versioned external-state (DNS/tunnel) report | 182 |
| `profiles.py` + `profile_data/monitoring/*.yml` | Curated service sets + seeded generic configs | 130 |
| `catalog.py` + `catalog_templates/*.yaml` | Bundled + site-local L2 templates, validated through the service schema | 116 |
| `setup.py` | Interactive setup, `.env`/`stack.yaml` generation, privileged phase orchestration | 1053 |
| `docker_manager.py` | Compose driver for the core project, `write_traefik_config_files` (single writer), macvlan shim ensure, status | 758 |
| `privileged_ops.py` (adjacent) | `render_startup_script`, `render_boot_script` (`BOOT_HOOK_CONTRACT = 3`), `render_shim_ifcfg`, boot-env cache | 1591 |
| `compose_cmd.py` / `_format.py` / `errors.py` / `__compat__.py` | compose v1/v2 probe; ASCII glyph+row helpers; `SyrvisError` base; `MIN_MANAGER_VERSION` | 50/71/32/15 |

## How it actually works

### SYRVIS_HOME resolution (`paths.get_syrvis_home`)

Four strategies, in order:

1. `$SYRVIS_HOME` — **content-checked** via `_is_install_manifest` (manifest exists, parses, carries `install_path`/`schema_version`/`versions`). Failure raises `SyrvisHomeError` with the collision-recovery text ("check `ls -d /volume*/syrviscore*` for a 'syrviscore_1' sibling … Do NOT run 'syrvisctl install'").
2. `/volume1/syrviscore` — checked with `_is_install_root` (manifest **plus** `Path(install_path).resolve() == candidate.resolve()`).
3. `/volume2..9/syrviscore` — same strict check.
4. Walk up from `__file__` looking for `.syrviscore-manifest.json`.

The asymmetry is deliberate and documented inline: strategy 1 uses the *content* check only because the dashboard container bind-mounts the real tree at `/syrvis` while the manifest records the host path — the strict identity check there "emptied the dashboard's entire Layer-2 list, silently, on the 0.5.9 image" (paths.py:224-230). Strategies 2–3 keep identity because a stray restore under a location root must never win auto-detection.

Manifest shape (`create_manifest`): `{schema_version: 3, active_version, install_path, setup_complete, created_at, versions: {<v>: {installed_at, status}}, update_history: [{from,to,timestamp,type}], privileged_setup: {}}`. Written atomically (`mkstemp` + `os.chmod(0644)` + `os.replace`) so a crash can't truncate it; 0644 so doctor reads it without sudo.

Simulation: `DSM_SIM_ACTIVE=1` + `DSM_SIM_ROOT` remap `/volumeN` via `resolve_volume_root`; `is_mounted_volume` rejects a **symlinked** volume root in *every* mode ("sim mode must not reopen that hole, adversarial review #9") and only waives the `os.path.ismount` requirement under simulation.

**Apps-root segment**: `SYRVIS_APPS_ROOT_NAME` (default `PACKAGE_NAME = "syrviscore"`) is the `X` in `<volume>/X/apps/<name>`. Resolution: process env → `<home>/config/.env` via `read_env_value` → default. Validated by `_APPS_ROOT_NAME_RE = ^[A-Za-z0-9][A-Za-z0-9._-]*$` and explicitly **refuses a trailing `_<digits>`** — "that is the suffix DSM hangs on a collision-renamed volume root". Set-but-invalid raises; unreadable `.env` falls back (the 0600-file-vs-`syrvis-reader` case).

### The `.env` model and its write-time guard

`config/.env` (0600) is the only secrets/network surface. `.env.template` at repo root documents the schema. Keys: `SYRVIS_HOME`, `SYRVIS_DATA_DIR`, `SYRVIS_APPS_ROOT_NAME`, `NETWORK_{INTERFACE,SUBNET,GATEWAY}`, `TRAEFIK_IP`, `SHIM_IP`, `NAS_IP`, `SYNOLOGY_*_ENABLED`, `DOMAIN`, `ACME_EMAIL`, `CLOUDFLARE_DNS_API_TOKEN`, `CLOUDFLARE_TUNNEL_TOKEN`, `DASHBOARD_*`, `SSH_TARGET`, `CLOUDFLARE_ACCESS_*`, `OIDC_*`, `TRAEFIK_*`, `TZ`.

**design/70 P6** (paths.py:391-468): the root boot hook does `set -a; . .env`, so a hazardous value is root RCE at boot and a hazardous *key* (`PATH`, `LD_PRELOAD`, `BASH_ENV`, `PYTHONPATH`, …) is **reboot-free** root RCE through the CLI's `load_dotenv(override=True)`. `env_value_hazard` rejects `[$`\\;&|<>()\n\r\t]` and leading/trailing whitespace; `env_key_hazard` rejects non-`[A-Za-z_][A-Za-z0-9_]*` and a frozenset of 13 loader-special names; `env_file_hazards` scans rendered text. `setup.generate_env_file` refuses to write on any finding (fail-closed, file untouched).

`_preserve_existing_values` restores any key the freshly rendered file blanked (`KEY=`) from the prior file — the reason unmanaged secrets (DNS-01/tunnel tokens, OIDC secret) survive a setup re-run.

### Image pinning and the release `config.yaml` override

`DEFAULT_DOCKER_IMAGES` in `compose.py` is the committed source of truth, **digest-pinned** (`repo:tag@sha256:…`) so a re-pushed tag can't silently change a core image. Precedence in `ComposeGenerator.load_config()`: explicit `config_path` > active version's `versions/<v>/build/config.yaml` (attached as a release asset, copied in by `syrvisctl install`) > built-ins. A config file lacking `docker_images` raises `ValueError("Invalid config: missing docker_images section")`.

The traefik entry carries a ~15-line rationale for the v3.6→v3.7 line move (v3.6 security EOL 2026-08-16; v3.6.5 carried "17 (10 high)" advisories incl. CVE-2026-71324 and CVE-2026-27141), noting the migration surface is empty because SyrvisCore is *file-provider only, one middleware, exact-Host routers, no tls.options, no http3*. The dashboard pin is versioned independently and its tag **must equal the dashboard package `__version__`** — asserted by `tests/test_compose.py::TestImagePinLockstep`.

### Compose generation — what is and isn't emitted

`generate_compose(stack=None)` loads `stack.yaml`, reads and validates network config from env (`NETWORK_INTERFACE/SUBNET/GATEWAY`, `TRAEFIK_IP`; `ipaddress` checks gateway and Traefik IP are *inside* the subnet), then emits:

- **Always**: `traefik` (macvlan `ipv4_address: TRAEFIK_IP` + `proxy`, `stop_grace_period: 30s`, `no-new-privileges`, four bind mounts, env = `TZ` + `CF_DNS_API_TOKEN=${CLOUDFLARE_DNS_API_TOKEN:-}` plus any names listed in `TRAEFIK_ACME_DNS_ENV`), `portainer` (`proxy`, socket `:ro`, optional `--admin-password-file`).
- **Conditionally**: `cloudflared` when `stack.is_enabled("cloudflared")` (`TUNNEL_TOKEN`, `TUNNEL_METRICS=0.0.0.0:<metrics_port|20241>`, no `ports:`), `syrviscore-dashboard` when enabled (socket/data/services mounts `:ro` unless `management: true`).
- **Networks**: `syrvis-macvlan` (driver macvlan, `driver_opts.parent`, ipam subnet+gateway) and `proxy` (bridge, `name: proxy`).

Deliberately **never emitted**: any Traefik label (routing for *every* tier is file-provider), any docker-socket mount on Traefik ("Traefik holds no host-level authority at all"), any published port for cloudflared's metrics, any Layer-2 service (L2 lives in its own compose project `syrvis-<name>` — `service_manager._project_name`), and `depends_on` (never rendered into a generated compose file even though 0.5.16 reintroduced it as *orchestration* intent).

Written 0640 root:docker so the docker-group operator can read it ("the compose carries no secrets — those live in `.env`, which stays 0600"). Bind paths are relative (`../data/traefik/...`) and resolve against the compose file's directory (`config/`).

`CLOUDFLARED_METRICS_PORT = 20241` is a documented contract with two out-of-band readers: the dashboard's `/ready` probe (`CLOUDFLARED_URL` rendered from the *same* declared value so probe and listener can't drift) and a deployment repo's vmagent scrape (`cloudflared_tunnel_ha_connections == 0` while the container is UP is "the tunnel failure that a container-lifecycle alert structurally cannot see").

### The core stack (`stack.yaml`)

```yaml
version: 1
services:
  traefik:     {enabled: true}      # primordial
  portainer:   {enabled: true}      # primordial
  cloudflared: {enabled: false}
  dashboard:   {enabled: false, subdomain: dash}
```
`PRIMORDIAL = ("traefik","portainer")`, `OPTIONAL = ("cloudflared","dashboard")`, `CONTAINER_NAME` maps dashboard→`syrviscore-dashboard`. `from_dict` force-enables primordial "regardless of the file"; `set_enabled` raises `StackError` on unknown name or on disabling a primordial. Absent file → `infer_stack_from_env()` (cloudflared **on**, preserving pre-stack behaviour); corrupt YAML → `StackError` — and `_core_service_routes` deliberately lets that raise, because "a routing regen that silently dropped the dashboard router here would restart Traefik and de-route a running dashboard with no error surfaced".

`_regenerate_compose()` (cli.py:2375) is the real apply: load `.env` → generate compose → `write_traefik_config_files` → `restart_traefik_if_running()` on any change → `remove_disabled_core_containers()` (exact-name matches of OPTIONAL only — narrower than `--remove-orphans`) → invalidate image-update cache → record a `@core` deployment revision iff the pin set or enabled set actually changed.

### Traefik configuration

**Static** (`data/traefik/traefik.yml`, 0644): `api.dashboard+insecure` on :8080, `ping: {}`, entrypoints web/websecure with `lifeCycle.graceTimeOut: "20s"` (must stay below the 30s `stop_grace_period` or "Traefik dies in `panic(\"Timeout while stopping traefik\")` instead of exiting 0"), `providers.file{directory: /config, watch: true}`, logs, and a `letsencrypt` ACME resolver whose challenge is DNS-01 when `CLOUDFLARE_DNS_API_TOKEN` / `TRAEFIK_ACME_DNS_ENV` / `TRAEFIK_ACME_CHALLENGE=dns`, else HTTP-01. Every operator-settable value is regex-guarded against YAML injection: provider `^[a-z0-9-]+$`, resolvers `[0-9A-Fa-f:.\[\]]+:\d+`, CA server `https?://[^\s'"]+`. Optional `metrics.prometheus` block via `TRAEFIK_METRICS_PROMETHEUS=true`.

**Dynamic** (`data/traefik/config/dynamic.yml`): Traefik's own UI (`api@internal` at `traefik.<domain>`), the `https-redirect` middleware, core routes from `_core_service_routes` (portainer → `http://portainer:9000`; `syrvis-dashboard` → `http://syrviscore-dashboard:8000`, prefixed so it can't collide with the `dashboard` router), and Synology passthrough routers/services generated from `SYNOLOGY_SERVICES` (photos/dsm/drive/audio/video/webdav → `<proto>://<backend_ip>:<port>` with `serversTransport: insecure-skip-verify@file`). `backend_ip = SHIM_IP or NAS_IP` — documented as working only because "DSM system services … bind 0.0.0.0".

**Per-L2** (`data/traefik/config/dynamic/<name>.yaml`): `ServiceTraefikConfig.write_config` renders the same HTTP-redirect + TLS router pair and is **change-aware** — it returns `(path, changed)` and removes a stale file when a service becomes unrouted, so "a content-identical redeploy never bounces the edge proxy". `exposure` is explicitly *not* consumed here.

`docker_manager.write_traefik_config_files()` is the single writer, only touching a file whose bytes actually change — because the stale-static drift check compares mtime against Traefik's `StartedAt` and a no-op rewrite "flipped the dashboard to degraded" live. It returns "restart needed", and callers pair it with `restart_traefik_if_running()` since the file-provider watch "does not fire reliably on Synology bind mounts".

### Networking / macvlan shim

Traefik owns a dedicated LAN IP on a macvlan network so it can bind :80/:443 beside DSM's nginx. Because macvlan containers cannot reach their own host, `privileged_ops.ensure_macvlan_shim(interface, traefik_ip, shim_ip)` creates host interface `syrvis-shim` at `SHIM_IP` (default `TRAEFIK_IP + 1`) with `ip addr add <shim>/32` and `ip route add <traefik_ip>/32 dev syrvis-shim`. It is idempotent with **drift detection**: if the existing shim's address doesn't match, it tears down and rebuilds; if only the route is missing, it re-adds. Every success path also reconciles `/etc/sysconfig/network-scripts/ifcfg-syrvis-shim` (`render_shim_ifcfg`: DEVICE/BOOTPROTO=static/ONBOOT=no/IPADDR/NETMASK=255.255.255.255) — written because DSM's health poller otherwise logs `SystemHealth.cpp:87 Failed to get interface: [syrvis-shim]` every ~60s. `ONBOOT=no` is load-bearing: SyrvisCore owns the lifecycle.

### Exposure and the hostnames report

`exposure.py` is 56 lines of vocabulary: `INTERNAL`/`TUNNEL`, `DEFAULT = internal`, `normalize()`. `hostnames.build_report()` (`REPORT_VERSION = 1`) enumerates four sources — primordial UIs, the optional dashboard, enabled Synology services (`SYNOLOGY_<KEY>_EXPOSURE` overrides), and every L2 service (with per-service `domain` override) — and emits per host `{service, kind, subdomain, hostname, exposure, enabled, access_required, record}` where `record` is `{type: A, target: TRAEFIK_IP, proxied: false}` for internal and `{type: CNAME, target: null, proxied: true, note: "Cloudflare Tunnel public hostname + Access policy"}` for tunnel. The doc is blunt about the limit: *"Exposure is declared intent, not routing enforcement… A LAN client that points a `tunnel` hostname at `TRAEFIK_IP` reaches the service **without** Cloudflare Access"* (04-split-dns.md:51-57).

### Setup, self-elevation, boot persistence

`syrvis setup` is a 7-step click command: prereqs → privilege check (`privilege.self_elevate` re-execs `sudo SYRVIS_HOME=… python argv`, because sudo's `env_reset` strips it and re-execing the console script bypasses the `bin/syrvis` wrapper) → detect install → interactive config (domain validated against `DOMAIN_RE`; Portainer password ≥12 chars because "Portainer CE 2.x rejects admin passwords shorter than 12 chars at first-run init") → privileged setup (7 sub-steps: verify docker, docker group, user in group, socket perms, `/usr/local/bin/syrvis` symlink, startup script, boot script) → generate `.env` / `stack.yaml` / `.portainer-password` / traefik configs / compose + manifest update → start.

Boot persistence is the project's stated rule ("setup must leave a system that works after a reboot with no manual steps"). Two artifacts, both rendered by pure functions shared with the content-aware validators so drift is *detected*, and written by `_write_script_if_changed` (atomic, returns created/updated/unchanged):

- `$SYRVIS_HOME/bin/syrvis-startup.sh` — ordered: (1) seam shell heal, (2) source `.env`, (3) macvlan shim + ifcfg, (4) poll `docker info` up to 600s, (5) socket chown + `synogroup --member docker <user> syrvis-operator`, (6) `syrvis reconcile --boot` ×3 with logging, (7) `syrvis schedule apply`, (8) ntfy on failure. Order is documented as the fix for the 2026-07-30 boot-resume race.
- `/usr/local/etc/rc.d/S99syrviscore.sh` — rootfs-resident, carries `# boot-hook-contract: 3` so `syrvisctl doctor` can read currency from the rootfs alone without importing this package. Its `start)` case inlines the seam heal, an **advisory** 60s `synocheckshare.service` phase gate, and the **reclaim guard** that renames `/volumeN/<name>_<N>` back when it carries a manifest or `apps/` dir — refusing loudly and paging when the target exists non-empty — then trampolines into the startup script with a load-bearing `else` branch ("without it a missing … startup script was a SILENT no-op, which is how the 2026-08-16 decapitation produced zero pages for ~50 minutes"). `stop)` runs `timeout 150s syrvis shutdown --reason reboot` then deletes the shim. `/usr/local/etc/syrviscore-boot.env` (0600) caches `NTFY_URL` + `SYRVIS_APPS_ROOT_NAME` on the rootfs because "the alarm was stored inside the thing it was supposed to alarm about".

### Profiles and catalog

`profiles.PROFILES` currently has one entry, `monitoring` (7 members: victoria-metrics, vmagent, vmalert, alertmanager, node-exporter, docker-socket-proxy, docker-health-exporter). `enable_profile` resolves each through `catalog.resolve` (full schema validation), writes `services.d` declarations, and seeds three generic configs from `profile_data/monitoring/` — **never overwriting** an existing declaration or config. The stated boundary: platform owns member set + pinned images + generic defaults (null alert receiver, scrape-self); deployment owns receivers/dashboards. The profile path also exists as a *privilege* mechanism: infra-tier members carry enumerated read-only host mounts (`/proc`, `/sys`, `/`, the docker socket) that "`service run` can never grant".

`catalog.resolve` merges bundled `catalog_templates/*.yaml` with site-local `$SYRVIS_HOME/catalog/*.yaml` (site wins), validates through `ServiceDefinition.from_dict`, and refuses a template whose `name` field differs from its filename.

## Design decisions & their rationale

- **File-provider-only Traefik** — `traefik_config.generate_traefik_static_config` docstring: *"ONE routing mechanism for every tier … and Traefik never needs the docker socket"*. This is also what makes the v3.7 migration surface empty.
- **Digest-pinned core images, never `:latest`** — compose.py:26-30; a release-attached `config.yaml` is the channel for shipping image bumps without a code change.
- **Dashboard versioned independently of the service** (owner decision 2026-07-31) with a pin==`__version__` lockstep test; the CLAUDE.md 0.5.16 note adds the *reader-enumeration gate*: the dashboard image must be repinned before any `depends_on` lands, or "a stale dashboard would mark every edge-carrying declaration invalid".
- **Exposure as declaration, not action** — exposure.py:18-21: *"Keeping exposure a declaration keeps SyrvisCore generic: it never needs Cloudflare API access."* 04-split-dns.md §"Why keep DNS out of SyrvisCore?" adds: no secrets/account bindings, open-sourceable, machine-readable contract.
- **Fail-loud SYRVIS_HOME** (paths.py:209-241) after the 2026-08-16 incident: an unguarded env var produced "a clean, empty, ACTIVE homebase that had converged nothing".
- **`.env` hazard guard fail-closed** — "Rejecting is a loud, local failure; the alternative (a root shell at boot) is not recoverable."
- **Boot-hook contract integer not bumped for the synocheckshare gate** — bumping "would mark every deployed contract-3 hook STALE and page for a race-narrowing tweak".
- **`services.d` owned by the setup user, not root** (setup.py:606-611) — "user ownership is what lets an IaC repo (home-tech) push declarations with plain `rsync` + then `sudo syrvis reconcile`".
- **Graceful-timeout ladder** 20s (Traefik lifeCycle) < 30s (compose stop_grace / `_reload_traefik`) — equal deadlines lose the drain race.

## Invariants & contracts

1. `.syrviscore-manifest.json` with `install_path` == its own root identifies an install root (manager and service both depend on it; `MANIFEST_SCHEMA_VERSION = 3` must match `syrviscore_manager.manifest`).
2. `PACKAGE_NAME = "syrviscore"` is baked into the wrapper, sudoers and SPK — **not** configurable; only the apps-root segment is.
3. Primordial services are always enabled regardless of `stack.yaml`; `CONTAINER_NAME` is the only container-name mapping.
4. `SYNOLOGY_SERVICES` and `PRIMORDIAL_UIS` are *the* catalogs — compose/traefik generators, `hostnames.build_report`, validators and the dashboard's links endpoint all derive from them ("Do not redefine service→subdomain mappings elsewhere").
5. `stack hostnames --json` is a versioned cross-repo contract (`REPORT_VERSION = 1`; bump only on breaking shape change) that home-tech reconciles.
6. Every static-config write goes through `write_traefik_config_files` and *must* be paired with a restart on change.
7. Compose pin tag for `dashboard` == dashboard package `__version__` (test-enforced).
8. Boot hook text is rendered by `render_boot_script`/`render_startup_script`; the validators re-render and compare, so writer and validator can never disagree. `# boot-hook-contract: N` is readable from the rootfs by the manager.
9. `SyrvisError.to_dict()` → `{"error", "code"}` is the adapter/JSON envelope; library code never prints.
10. `__compat__.MIN_MANAGER_VERSION = "0.2.0"` gates `syrvisctl activate`.

## Gaps, debt & sharp edges

- **`hostnames.build_report` can raise, contradicting its own docstring.** Sections 3 (Synology) and 4 (L2) are wrapped in `try/except`; sections 1 (primordial UIs) and 2 (dashboard) are **not**. A `stack.yaml` carrying `exposure: publik` on traefik/portainer/dashboard makes `exposure_mod.normalize` raise `ValueError` straight out of a function documented "Never raises" — breaking the `--json` error contract for the seam consumer. No test covers it (`tests/test_hostnames.py` only exercises the unresolvable-home path).
- **Untyped errors in the core generators.** `compose.py` never imports `SyrvisError`; missing network vars and bad subnet/gateway raise bare `ValueError`. Same for `traefik_config.get_domain_from_env` and `ServiceTraefikConfig.__init__` (`ValueError("SYRVIS_HOME environment variable not set")`). These bypass the `code`-bearing taxonomy the MCP/dashboard adapters gate on.
- **Five different `.env` parsers.** `paths.read_env_value`, `paths.env_file_hazards`, `validators.parse_env_file`, `setup._parse_env_file`, and an inline loop in `traefik_config.get_domain_from_env` — plus `dotenv.load_dotenv` for the real thing. Quote-stripping and `export ` handling differ between them.
- **Inconsistent home resolution inside compose/traefik.** `_generate_portainer_service` does `Path(os.environ.get("SYRVIS_HOME",""))/"config"/".portainer-password"` — with the var unset this becomes the *relative* path `config/.portainer-password`, so a file in cwd would flip the password-file branch. `ServiceTraefikConfig` also reads the raw env var rather than `paths.get_syrvis_home()`.
- **Wiki drift vs code.** `02-primordial-substrate.md` still documents a `cloudflare_ddns` optional stack service (removed from `stack.OPTIONAL`) and claims Traefik uses "the Docker provider (labels on containers like Portainer/the dashboard) *and* the file provider" — the code is file-provider only and mounts no socket into Traefik. `docs/design-doc.md` (v3.0, "Implemented") predates stack.yaml, exposure, profiles, the seam and the dashboard entirely; its CLI table is missing ~20 verbs.
- **Setup cannot enable the dashboard.** `write_stack_file` keys off `config.get("dashboard_enabled")`, which `prompt_configuration` never sets, and its docstring still mentions DDNS. Non-interactive setup also never populates `cloudflare_dns_api_token` or `portainer_password`.
- **Migration default can crash-loop the tunnel.** With no `stack.yaml`, `infer_stack_from_env()` enables cloudflared; compose then emits `TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}` with no default, so an unset token yields a compose interpolation warning and a restarting container.
- **Compose is generated up to twice per save.** `generate_and_save` → `save_compose` (which calls `generate_compose`) → `generate_compose` again; each call re-loads `stack.yaml`. And the emitted file still carries the obsolete top-level `version: "3.8"` key that Compose V2 warns about.
- **`_traefik_acme_env` name filter is weak**: `name.replace("_","").isalnum()` accepts non-ASCII identifiers, and forwarded names are never checked against the `.env` key-hazard set.
- **`remove_disabled_core_containers` is silent and best-effort** — an unreadable `stack.yaml` or unreachable daemon returns `[]`, so a "disabled" optional service can keep running with no signal.
- **Race/ordering fragility that is acknowledged but unverified**: the S99 `stop)` case is bounded by `timeout 150s` but the comment says "DSM's rc.d-stop timeout is UNVERIFIED here — a real DSM-reboot test must confirm the whole stop case completes in time before this graceful flush is trusted."
- **Test blind spots**: no test drives `syrvis setup` end-to-end (only `test_setup_env.py` for `.env` safety); nothing asserts the hostnames never-raises contract; nothing covers `stack.yaml` settings validation (arbitrary keys are accepted and silently round-tripped into the file by `to_dict`); `_cloudflared_metrics_port`'s silent fallback on a bad value means a typo is invisible except in the rendered compose.
- **Stale comment in the L2 compose generator** ("depends_on is rejected at schema-validation time") now contradicts 0.5.16, which made `depends_on` a real orchestration key — the *behaviour* (never emitted) is still correct, the *reason* is not.

## Raw material worth citing in the retrospective

- paths.py:220 — *"a clean, empty, ACTIVE homebase that had converged nothing… saying so by name is what turns a 50-minute outage into one `ls`."*
- paths.py:406 — *"Rejecting is a loud, local failure; the alternative (a root shell at boot) is not recoverable."*
- privileged_ops.py:391 — *"Nothing alarmed, because the alarm was stored inside the thing it was supposed to alarm about."*
- privileged_ops.py:64 — `BOOT_HOOK_CONTRACT = 3`; `SYNOCHECKSHARE_WAIT_S = 60` deliberately *not* a bump.
- compose.py:36 — v3.6.5 carried "17 (10 high)" advisories; v3.7.10 zero. `CLOUDFLARED_METRICS_PORT = 20241`, "WHY 0.0.0.0 IS SAFE HERE".
- traefik_config.py:206 — 20s `graceTimeOut` vs 30s stop, or `panic("Timeout while stopping traefik")`.
- docker_manager.py:88 — a no-op rewrite "flipped the dashboard to degraded" (live).
- exposure.py:18 — *"Keeping exposure a declaration keeps SyrvisCore generic: it never needs Cloudflare API access."*
- 04-split-dns.md:51 — *"Exposure is declared intent, not routing enforcement."*
- 01-architecture-overview.md:65 — *"Anything an adapter can do, `ssh nas && syrvis …` can do."*
- Numbers: `MANIFEST_SCHEMA_VERSION = 3`, `STACK_SCHEMA_VERSION = 1`, `REPORT_VERSION = 1`, `MIN_MANAGER_VERSION = "0.2.0"`, 600s Docker-ready poll, 3× reconcile retry at 15s, 150s shutdown bound, 12-char Portainer password floor, 0600 `.env`/`acme.json`/boot-env, 0640 compose, 0644 stack/traefik configs, 0755 scripts.