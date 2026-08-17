# syrvis CLI Reference

`syrvis` is the service CLI for SyrvisCore. It manages Docker services (Traefik, Portainer, Cloudflared), configuration, and diagnostics.

## Installation

The `syrvis` command is installed when you run `syrvisctl install`. Each version has its own virtual environment.

**Location:** `$SYRVIS_HOME/bin/syrvis` (wrapper script)

## Commands

### syrvis setup

Run interactive setup to configure SyrvisCore.

```bash
syrvis setup
```

**What it does:**
1. Checks prerequisites (Python, Docker)
2. Prompts for sudo if privileged operations needed
3. Creates/updates docker group membership
4. Sets Docker socket permissions
5. Generates configuration files
6. Creates startup scripts

**Options:**
```bash
syrvis setup --non-interactive  # Use defaults, no prompts
```

**Notes:**
- Safe to re-run to reconfigure
- Self-elevates with sudo when needed

---

### syrvis status

Display status of all services.

```bash
syrvis status
```

**Output:**
```
SyrvisCore Status
=================
traefik:     running  (192.168.1.100)
portainer:   running
cloudflared: disabled

Uptime: 5 days, 3 hours
```

---

### syrvis start

Start all services.

```bash
syrvis start [SERVICE]
```

**Arguments:**
- `SERVICE` - (Optional) Specific service to start

**Examples:**
```bash
# Start all services
syrvis start

# Start specific service
syrvis start traefik
```

---

### syrvis stop

Stop all services.

```bash
syrvis stop [SERVICE]
```

**Arguments:**
- `SERVICE` - (Optional) Specific service to stop

**Examples:**
```bash
# Stop all services
syrvis stop

# Stop specific service
syrvis stop cloudflared
```

---

### syrvis restart

Restart all services.

```bash
syrvis restart [--graceful] [--json]
```

**Options:**
- `--graceful` - Full-instance graceful restart: ordered stop of every managed
  workload (transition hooks + per-service stop grace, VMs included), then an
  ordered bring-up. Without it: core-only force-recreate.

**Examples:**
```bash
# Core force-recreate (applies compose + static-config changes)
sudo syrvis restart

# Graceful everything (DBs get their stop grace, hooks fire)
sudo syrvis restart --graceful
```

---

### syrvis history

Show deployment history: per-workload revisions with image transitions, env
var NAMES (values always redacted), volumes, exposure, trigger, and outcome.
The core tier records under the reserved workload id `@core`.

```bash
syrvis history [WORKLOAD] [--json] [--limit N] [--revision R]
```

**Examples:**
```bash
syrvis history                        # every workload
syrvis history cyberquill --limit 5   # one service, newest 5
syrvis history cyberquill --revision 3 --json   # one revision in full
```

---

### syrvis service rollback

Roll a service back to a prior deployment revision (Helm-style: the restore is
recorded as a NEW revision with `rollback_of`). Restores the target revision's
manifest through the full trust boundary, pulls the target image first (an
unpullable tag leaves the service untouched), and redeploys. Data and secrets
are left in place; the operator's current enabled/critical is preserved.
Git-sourced services are refused (roll back via the repo + `service update`).

```bash
sudo syrvis service rollback NAME [--to REV] [-y]
```

Note: rollback is GitOps-ephemeral — the next IaC apply/reconcile that still
declares the newer image will redeploy it. Revert the deployment repo too to
make it durable.

---

### syrvis shutdown

Gracefully stop every managed workload and halt the instance — the
UPS-on-battery verb. Fires the instance pre-shutdown hook, issues VM ACPI
shutdown (guests drain in parallel), stops Layer 2 services in
`shutdown.priority` order (pre-stop hooks quiesce databases, then each gets
its `shutdown.stop_timeout` grace), waits for VMs (force-off stragglers), and
stops the core stack with Traefik last. Declared intent is untouched.

```bash
sudo syrvis shutdown [--reason ups|maintenance] [--timeout N] [--vm-deadline N] [--hold] [--json]
```

- `--reason ups` auto-resumes on the next boot (power returned);
  `maintenance` (default) stays down until `syrvis resume`.
- Exit 0 = clean; 2 = completed degraded (failures listed in the report).
- While halted, `status` shows a banner and bulk bring-up verbs
  (`start`, `restart`, `reconcile`) refuse until `resume`. Config-only verbs
  (`stack apply`, declarations) still work — they start nothing.

---

### syrvis resume

Bring a halted instance back: core stack first, then VMs, then Layer 2 via the
reconcile engine. Clears the halted state; a no-op when the instance is
active.

```bash
sudo syrvis resume [--json]
```

---

### syrvis logs

View service logs.

```bash
syrvis logs [SERVICE] [OPTIONS]
```

**Arguments:**
- `SERVICE` - (Optional) Service name (traefik, portainer, cloudflared)

**Options:**
- `-f, --follow` - Follow log output
- `-n, --tail N` - Show last N lines (default: 100)
- `--since TIME` - Show logs since timestamp

**Examples:**
```bash
# All logs
syrvis logs

# Specific service
syrvis logs traefik

# Follow logs
syrvis logs -f traefik

# Last 50 lines
syrvis logs -n 50 portainer

# Logs since time
syrvis logs --since "2024-12-25T10:00:00"
```

---

### syrvis doctor

Diagnose and optionally fix common issues.

```bash
syrvis doctor [OPTIONS]
```

**Options:**
- `--fix` - Attempt to automatically fix issues

**Output:**
```
SyrvisCore Diagnostics
======================

[PASS] Python version: 3.8.12
[PASS] Docker installed
[PASS] Docker running
[WARN] User not in docker group
[PASS] Docker socket exists
[FAIL] Docker socket not accessible

Issues found: 2
Run 'syrvis doctor --fix' to attempt automatic fixes.
```

**Examples:**
```bash
# Diagnose only
syrvis doctor

# Diagnose and fix
syrvis doctor --fix
```

---

### syrvis config show

Display current configuration.

```bash
syrvis config show
```

**Output:**
```
SyrvisCore Configuration
========================
Domain:      example.com
ACME Email:  admin@example.com
Traefik IP:  192.168.1.100
Subnet:      192.168.1.0/24
Gateway:     192.168.1.1
Cloudflare:  disabled
```

---

### syrvis compose generate

Generate or regenerate docker-compose.yaml.

```bash
syrvis compose generate
```

**What it does:**
1. Reads `.env` configuration
2. Reads Docker image versions from manifest
3. Generates `docker-compose.yaml`

**Notes:**
- Safe to re-run after configuration changes
- Backup is created before overwriting

---

### syrvis --version

Display version information.

```bash
syrvis --version
```

**Output:**
```
syrvis, version 0.2.0
```

---

### syrvis --help

Display help information.

```bash
syrvis --help
syrvis status --help
```

## Layer 2 services & declarative loading

The `service` group manages user-installed containers; `reconcile` converges the
instance to the declarations in `config/services.d/` (one validated
`syrvis-service.yaml` per file — see docs/wiki/05-layer2-services.md and
docs/service-loading-design.md).

```bash
# Imperative (each of these also authors the services.d declaration):
syrvis service run gollum                         # from the catalog
syrvis service run app --image ghcr.io/o/app:1.0  # image-first
syrvis service add https://github.com/o/repo.git  # git manifest
syrvis service list [--json]
syrvis service start|stop|update|remove NAME      # stop/start flip `enabled`
syrvis service shed --reason R [--until D] NAME   # DURABLE "deliberately down"
syrvis service unshed NAME                        # lift it (starts nothing)
syrvis service catalog [--json]                   # vetted templates

# Declarative:
syrvis service declare NAME --image IMG [--subdomain S] [--exposure internal|tunnel]
                     [--port N] [--enabled BOOL] [--critical BOOL] [--json]
                                                  # author intent only — applies nothing
syrvis service adopt NAME | --all [--json]        # existing install -> declaration
sudo syrvis reconcile [--dry-run] [--json] [--strict] [--force]
                      [--prune stop|remove|purge] [-y]
                                                  # converge to services.d
```

### Shed — durable "deliberately down" (0.5.15)

`service stop` is EPHEMERAL intent: it writes `enabled: false` into the
declaration, which is exactly the file the next GitOps `syrvis apply`
overwrites from the repo. That is right for a five-minute stop and wrong for a
five-day load-shed — it is how fourteen deliberately-stopped services were
resurrected mid-array-rebuild (home-tech incident 2026-08-16).

`service shed` is DURABLE intent. It records `{service, reason, since, until}`
in `data/state/intent.json` — outside the declaration set, so nothing in the
bundle path can address it — and stops the container without touching the
declaration. The composition rule every consumer implements: **a workload runs
iff the device is `in-service` AND the service is `enabled` AND it is not
shed.** While shed:

| surface | behavior |
|---|---|
| `reconcile` / `resume` | plans it exactly as `enabled: false`; a shed service found RUNNING is stopped; reported in its own `shed` bucket, never `disabled` |
| `apply` (instance bundle) | pins the written declaration `enabled: false` whatever the bundle says (reported as `shed_pinned`) |
| `deploy` (service bundle) | lands manifest/configs/secrets, then does **not** start — staging fixed bits on a down service is the point |
| `service start` / `service recreate` | refuse, naming the reason and the lift verb |
| `verify` / L2 drift | not drift |
| `service list --json` | `intent: shed` + `shed_reason`/`shed_since`/`shed_until` |
| `status --json` | an `intent` block: `device`, `shed[]`, `shed_count`, `shed_reasons`, `shed_expired` |

`service unshed` removes the row and starts nothing — the declaration
underneath was never touched, so `reconcile` is the one bring-up path.

### Guards (0.5.15)

Two refusals, both overridable, both journaling the override to
`logs/overrides.log`:

- **`guard_enable_change`** — `syrvis apply` refuses, BY NAME, to flip any
  service from declared-off to declared-on. Override: `--allow-enable-change`.
  (A shed service is not refused; it is pinned off — see above.) Not enforced
  on `--dry-run`, which writes nothing and reports the names either way.
- **`guard_bulk_degraded`** — `reconcile` (with work to do) and `deploy` refuse
  while `/proc/mdstat` shows a resync/recovery/reshape/check, naming the array
  and its progress. Override: `--force`. `reconcile --dry-run` and
  `reconcile --boot` are never blocked — planning is free and boot recovery
  must proceed.

Reconcile semantics: every declaration file loads independently (a broken file
marks only itself invalid — but any invalid file fails the run, since corrupted
intent must never pass silently); every service converges independently; only a
`critical: true` service's failure (or `--strict`) is otherwise fatal. Installed
services with no declaration are reported `unmanaged` and never touched without
an explicit `--prune`. The boot hook runs `reconcile --boot` (always exit 0,
never prunes) after waiting for the Docker daemon.

`syrvis verify` honors the same severity: a failing critical service is
UNHEALTHY (exit 1); non-critical failures report DEGRADED (exit 0).

## Services

The following services are managed by `syrvis`:

| Service | Description | Required |
|---------|-------------|----------|
| `traefik` | Reverse proxy with SSL | Yes |
| `portainer` | Container management UI | Yes |
| `cloudflared` | Cloudflare Tunnel | No |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SYRVIS_HOME` | Service data directory |
| `DOCKER_HOST` | (Optional) Docker socket location |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Invalid arguments |
| 3 | Service not found |
| 4 | Docker error |
| 5 | Permission denied |
| 6 | Configuration error |

## Files

| File | Description |
|------|-------------|
| `$SYRVIS_HOME/config/.env` | Configuration file |
| `$SYRVIS_HOME/config/docker-compose.yaml` | Generated compose file |
| `$SYRVIS_HOME/config/traefik/` | Traefik configuration |
| `$SYRVIS_HOME/data/` | Persistent service data |
| `$SYRVIS_HOME/bin/syrvis` | Wrapper script |

## Configuration

Configuration is stored in `$SYRVIS_HOME/config/.env`:

```bash
# Domain configuration
DOMAIN=example.com
ACME_EMAIL=admin@example.com

# Network configuration
NETWORK_INTERFACE=ovs_eth0
NETWORK_SUBNET=192.168.1.0/24
GATEWAY_IP=192.168.1.1
TRAEFIK_IP=192.168.1.100

# Optional: Cloudflare
CLOUDFLARE_TUNNEL_TOKEN=
```

## See Also

- [syrvisctl CLI Reference](cli-syrvisctl.md) - Manager CLI documentation
- [SPK Installation Guide](spk-installation-guide.md) - Installation instructions
- [Design Document](design-doc.md) - Architecture overview
