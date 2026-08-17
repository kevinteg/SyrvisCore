# The SyrvisCore Seam Contract

This document is the integration surface between a SyrvisCore instance and a
deployment repo (the estate-specific repo that owns the domain, DNS, Cloudflare
account, and service catalog — SyrvisCore itself stays generic). If you are
building or maintaining a deployment repo, everything you may rely on is here;
anything not here is an internal you must not depend on.

Three artifacts make up the contract:

1. **The operator seam** — the enumerated verb set a deployment may invoke.
2. **The wire formats** — `syrvis-instance/v1`, `syrvis-bundle/v1`, and the
   hostnames report (v1) streamed across that seam.
3. **The division of labor** — what SyrvisCore owns, what the deployment owns,
   and what deliberately never crosses the seam.

---

## 1. The operator seam

All routine management happens as a dedicated least-privilege NAS account
(default `syrvis-operator`) whose SSH key is locked to a forced-command shim
and whose sudo rights are an enumerated NOPASSWD allowlist. Both artifacts are
**generated** from the platform's verb registry (`syrviscore.seam.registry`),
so the enforcement boundary and the runtime argv can never drift (a committed
drift test asserts it).

- **Provisioning (once, as root):**
  `python -m syrviscore.seam.gen provision --home <SYRVIS_HOME> --pubkey <key.pub> [--from <cidr>] [--no-auto-seam-update]`
  renders a self-contained POSIX script to run on the NAS. It creates the
  operator, installs the sudoers policy + shim + key, records a rollback
  script, and writes the **seam policy**
  (`/var/log/syrviscore-mcp-provision/seam-policy.json`, root-held).
- **Lifecycle (automatic):** with `auto_seam_update` on (the default),
  `syrvisctl activate` and `syrvisctl rollback` re-render the sudoers + shim
  from the newly active service version — new verbs arrive with the release
  that implements them; a rollback narrows the seam back. The trust anchor is
  therefore the release channel plus the root-held policy file. Provision with
  `--no-auto-seam-update` to pin the boundary instead and update it on demand
  with `syrvisctl seam sync` (`syrvisctl seam status --json` reports drift).
- **Clients:** the MCP server (`syrviscore-mcp`, runs on an operator machine,
  brokers verbs as typed tools with a two-call confirmation handshake for
  destructive ones) and plain scripts invoking
  `ssh <operator-target> 'sudo -n <verb> ...'` directly. Both are *generated
  consumers* of the registry — a deployment never invents an argv shape.

### Verb classes

| Class | Examples | Notes |
|---|---|---|
| Read (no sudo) | `status`, `verify`, `service list`, `stack hostnames`, `service catalog`, `profile list`, `updates`, `logs`, `history` | All support `--json`; `updates` queries registries (report-only); `history` is always env-redacted |
| Read (sudo, side-effect-free) | `reconcile --dry-run`, `schedule list`, `schedule dsm-tasks`, `apply --dry-run`, `export`, `vm list/status` | sudo only to read 0600 config or a root-only DSM tool; `export` is always redacted over the seam. `schedule dsm-tasks` enumerates DSM's OWN Task Scheduler (`synoschedtask --get`) — SyrvisCore never writes it |
| Converge (sudo) | `start/stop/restart`, `shutdown`, `resume`, `restart --graceful`, `stack apply`, `reconcile`, `reconcile --force`, `stack enable/disable`, `profile enable`, `service declare/adopt/run/add/start/stop/shed/unshed/update/task`, `syrvisctl install`, `backup create` | Idempotent intent/lifecycle. `shutdown`/`resume` are deliberately token-free (reversible; an unattended NUT low-battery hook must be able to fire `shutdown --reason ups`) — and so are `service shed`/`unshed`, so an unattended degradation response can declare a load-shed with no human in the loop |
| Destructive (sudo + confirmation token via MCP) | `reconcile --prune`, `service remove`, `service set-image`, `service rollback --to N`, `activate`, `rollback`, `uninstall`, `cleanup`, `backup cleanup`, `schedule apply/sync` | Two-call handshake; `service rollback` requires an EXPLICIT `--to` over the seam |
| Stdin writers (sudo; **script-only, never MCP tools**) | `apply`, `deploy`, `secret set`, `config set` | Payload arrives on stdin ONLY — secrets never touch argv/ps/logs, and never transit an LLM context |

### Deliberately NOT on the seam

- `syrvis setup` (first-boot bootstrap), `syrvisctl restore` (disaster recovery
  must not depend on the thing being recovered), `syrvis doctor`/`clean`/`reset`
  (troubleshooting). These are the break-glass SSH floor — by design.
- Any verb that would put a secret value into MCP tool arguments.

---

## 2. Wire formats

### 2.1 `syrvis stack hostnames --json` — the routing report (version 1)

The report is how a deployment learns the external state each routed hostname
needs. SyrvisCore never touches DNS or the Cloudflare API; the deployment
reads this report and reconciles (LAN DNS records, tunnel public hostnames,
Access policies). **Never re-derive routing truth — read the report.**

```json
{
  "version": 1,
  "domain": "example.com",
  "traefik_ip": "192.168.1.100",
  "entries": [
    {
      "service": "grafana",
      "kind": "core" | "synology" | "service",
      "subdomain": "grafana",
      "hostname": "grafana.example.com",
      "exposure": "internal" | "tunnel",
      "enabled": true,
      "access_required": true,
      "record": {
        "type": "A" | "CNAME",
        "name": "grafana.example.com",
        "target": "192.168.1.100" | null,
        "proxied": false | true,
        "note": "LAN DNS record pointing at Traefik" | "Cloudflare Tunnel public hostname + Access policy"
      }
    }
  ]
}
```

- `internal` → an `A` record on the LAN resolver pointing at `traefik_ip`.
- `tunnel` → a Cloudflare Tunnel public hostname (`CNAME`, target filled in by
  the deployment) plus an Access policy.
- On a config-read failure the report degrades to
  `{"version": 1, "domain": null, "traefik_ip": null, "entries": [], "error": ...}`.
- `version` bumps only on a breaking shape change; additive fields do not.

**Exposure is declared intent, not routing enforcement.** SyrvisCore routes
`internal` and `tunnel` identically at the Traefik layer (same router, same
cert resolver). Concretely: a LAN client that points a `tunnel` hostname at
`traefik_ip` reaches the service **without** Cloudflare Access. This is a
deliberate, documented property — the LAN is inside the trust boundary; Access
gates the *public* path. If your LAN is not trusted, do not rely on Access as
the only gate for a tunnel-exposed service.

### 2.2 `syrvis apply` — the instance bundle (`syrvis-instance/v1`)

The core-tier configuration plane. One JSON document on stdin declares the
runtime `.env`, the core `stack.yaml` enablement, and the complete
`services.d/` declaration set — so a deployment repo never writes files under
`$SYRVIS_HOME/config` itself:

```json
{
  "apiVersion": "syrvis-instance/v1",
  "env":   { "DOMAIN": "example.com", "TRAEFIK_IP": "192.168.1.100", "CLOUDFLARE_TUNNEL_TOKEN": "..." },
  "stack": { "services": { "cloudflared": {"enabled": true}, "dashboard": {"enabled": true, "subdomain": "dash"} } },
  "declarations": { "<name>": { ...full syrvis-service.yaml manifest... } }
}
```

Semantics:

- Every section is optional; present sections are authoritative. `env` is
  whole-file replace; `declarations` is a **replace set** (a declaration absent
  from the bundle is removed from `services.d/` — containers themselves are
  only ever removed by `reconcile --prune`).
- Validation is strict: env keys charset-checked, values single-line; a
  bundle-supplied `SYRVIS_HOME` must equal the real install; unknown stack
  services rejected; primordial services cannot be disabled; every declaration
  runs the full `syrvis-service.yaml` trust boundary.
- Changing an **existing** secret value in `.env` requires
  `--allow-secret-change` (token rotation stays a deliberate act).
- Flipping an **existing** declaration from `enabled: false` to `enabled: true`
  requires `--allow-enable-change` — the same shape, for the same reason
  (0.5.15; home-tech incident 2026-08-16, where a repo apply re-enabled
  fourteen deliberately-stopped services mid-array-rebuild). The refusal names
  every affected service; the override is journaled to `logs/overrides.log`.
  A **shed** service is not refused — its declaration is written
  `enabled: false` regardless of the bundle, reported as `shed_pinned`.
- `--dry-run --json` plans without writing; reports name keys, never values.
  Neither the secret nor the enable guard is enforced on a dry run (nothing is
  written), so a plan can safely preview what it would refuse — `enable_changes`
  and `shed_pinned` are in the report either way.
- Apply only **writes** configuration. Converge afterwards:
  `stack apply` (+ `start`) for the core tier, `reconcile` for L2.

### 2.3 `syrvis deploy` — the service bundle (`syrvis-bundle/v1`)

The Layer 2 per-service plane (design/21): one JSON document on stdin carries a
full service manifest + non-secret config files + secret env values, applied
atomically (install or update; configs 0644, secret configs and the env_file
0600; start last; rollback on failure):

```json
{
  "apiVersion": "syrvis-bundle/v1",
  "service": { ...full syrvis-service.yaml manifest... },
  "configs": [ {"dest": "config/scrape.yml", "content": "...", "secret": false} ],
  "secrets": { "ADMIN_PASSWORD": "..." }
}
```

The argv service name is authoritative — a bundle claiming a different
`service.name` is rejected. Related single-file verbs: `secret set <name>`
(just the env_file) and `config set <name>` (a declared scheduled-job's conf).

---

## 3. Division of labor

| Concern | SyrvisCore | Deployment repo |
|---|---|---|
| Traefik routing, certs (DNS-01), macvlan, compose | ✅ owns | consumes |
| Which services exist, their config + secrets | validates + applies | ✅ owns (bundles) |
| DNS records, tunnel hostnames, Access policies | reports (`stack hostnames`) | ✅ owns (reconciles) |
| Scheduled jobs | derives + runs (`jobs.d`, root-vetted source) | declares + confs |
| Seam enforcement (shim/sudoers) | ✅ generates from the registry | provisions once |
| Backups / restore | ✅ owns (`syrvisctl backup`/`restore`) | schedules + verifies freshness |

Verification loop for a deployment: `verify --json` (health + drift),
`reconcile --dry-run --json` (L2 plan), `apply --dry-run --json` (core plan),
`schedule list --json` (jobs + per-script sha256 + per-conf presence/size),
`schedule dsm-tasks --json` (what ELSE this box schedules), `stack hostnames --json`
(external-state diff), `updates --json` (available container-image updates),
`export --json` (a redacted snapshot of the whole declared instance) — all
seam-readable without a break-glass login.

## 4. Keeping current

- **Platform version** (`syrviscore`/`syrviscore-manager`): `syrvisctl check`
  → `syrvisctl install <ver>` (from GitHub releases; a release ships new
  core-image pins). `syrvisctl activate`/`rollback` re-sync the seam.
- **Container images**: `syrvis updates --json` reports newer *compatible*
  registry tags for every pinned image (core + installed L2), report-only.
  Apply an L2 image update declaratively with `syrvis service set-image
  --image <ref> -- <name>` (re-pins the manifest + declaration, pulls,
  restarts). Core-tier images advance with a platform release. A deployment
  repo can also bump the pin in its `services.d/` manifest and re-`apply`.
- **DNS/tunnel state**: `stack hostnames --json` → the deployment reconciles.
