# Deployments: history, rollback, lifecycle, and graceful shutdown

> Status: **implemented** (2026-07-27). Positions the managed stacks as a
> deployment system: every workload change is a recorded *deployment revision*,
> rollback is a first-class verb, transitions fire hooks, and the whole
> instance can gracefully halt/resume (the UPS-on-battery driver).

## 1. Concepts

| Concept | What it is | Where it lives |
|---|---|---|
| Deployment record | One immutable JSON per workload change (image, env names, volumes, routing, trigger, outcome) | `$SYRVIS_HOME/data/deployments/<workload>/<NNNN>.json` |
| Revision | Monotonic per-workload number; rollback targets a revision, Helm-style | record field |
| Lifecycle state | DERIVED, never stored: `running` / `stopped` / `failed` / `unknown` | `lifecycle.derive_state()` |
| Transition hook | Code that runs at a lifecycle event (container-exec or host script) | manifest `hooks:` / `$SYRVIS_HOME/hooks.d/` |
| Instance runstate | ONE persisted instance state: `active` (no file) or `halted` | `$SYRVIS_HOME/data/state/runstate.json` |

## 2. Deployment records (`syrviscore.deployments`)

Every mutation that changes what a workload runs appends one record:

- **Layer 2** — `service add/run`, catalog installs, `service update`,
  `service set-image`, `service rollback`, `service remove`, `deploy` (bundle),
  and every reconcile/converge `add`/`replace`/`prune_remove|purge`. All
  materializations funnel through ONE choke point
  (`ServiceManager._install_from_definition`), so a reconcile REPLACE records a
  single deploy with the image transition (`previous_image → image`), never a
  remove+add pair. `deploy_bundle` suppresses the inner record and records once
  at its own end (after configs/secrets land and the container starts).
- **Core tier** — `stack apply` (and the converge path) writes an instance
  record under the reserved workload id **`@core`** whenever the pin set or
  enabled set actually changed (`pins`, `previous_pins`, `core_enabled`).

Record shape (`syrvis-deployment/v1`), service tier:

```json
{
  "schema": "syrvis-deployment/v1", "revision": 4, "workload": "cyberquill",
  "tier": "service", "timestamp": "2026-07-27T12:34:56Z",
  "action": "deploy | rollback | remove", "trigger": "cli | reconcile | converge | deploy | resume",
  "outcome": "success | failed", "detail": "…",
  "image": "ghcr.io/acme/cyberquill:1.5.0", "image_digest": "sha256:… (iff pinned by digest)",
  "previous_image": "ghcr.io/acme/cyberquill:1.4.0", "version": "1.5.0",
  "exposure": "tunnel", "hostname": "bbq.example.com", "ports": [8300],
  "env_names": ["API_KEY", "MODE"], "env_file": "secrets.env",
  "volumes": [{"source": "media", "target": "/data", "mode": "rw", "kind": "service-data"}],
  "config_checksums": {"config.yaml": "sha256:…"},
  "source_url": "deploy:cyberquill", "tier_selector": "",
  "rollback_of": null,
  "manifest": { "…full ServiceDefinition snapshot…" }
}
```

Rules that must hold:

- **Best-effort writes.** A history-write failure never fails a deployment
  (same discipline as the services.d dual-write).
- **Secrets.** `env_names` carries KEYS only; the full `manifest` snapshot
  (needed for rollback fidelity) keeps inline `KEY=VALUE`, so a record carrying
  inline env is written `0640` + the services.d shared group (mirroring
  installed manifests). `syrvis history` ALWAYS masks inline env values; only
  the rollback path reads the raw file. `env_file` contents are never stored.
- **Atomic numbering.** Content goes to an `O_EXCL` temp file, then
  `os.link(tmp, NNNN.json)` claims the number atomically (retry on collision).
- **Retention.** Only the newest 50 records per workload are kept; revision
  numbers keep climbing (a trimmed revision is no longer a rollback target).
- **Failed rollouts are history too** — recorded when they leave changed state
  (a torn-down update, a failed restart on a new pin). A fully-rolled-back
  fresh install or an aborted pull records nothing (no state change).

## 3. History + rollback verbs

```bash
syrvis history [WORKLOAD] [--json] [--limit N] [--revision R]  # read-only, redacted
sudo syrvis service rollback NAME [--to REV] [-y]              # redeploy a prior revision
```

Rollback mirrors the proven `set-image` shape: resolve the target revision
(default: the newest earlier *successful* deploy/rollback), re-validate the
stored manifest through the FULL `ServiceDefinition.from_dict` trust boundary
(a tampered record cannot smuggle an unaudited key; `tier: infra` is
re-authorized via the stored `source_url`), pull the target image FIRST (an
unpullable tag leaves the service untouched), swap manifest + compose +
Traefik, dual-write the services.d declaration (CONTENT only — the operator's
current `enabled`/`critical` is preserved), restart, and record a NEW revision
with `rollback_of`. Data dir, env_file secrets, and materialized configs are
untouched. Git-sourced services are refused (roll back via the repo).

**GitOps semantics:** rollback is operator break-glass. The next IaC apply /
reconcile that still declares the newer image will redeploy it — exactly like
`kubectl rollout undo` before the repo is reverted. Make it durable by
reverting the deployment repo too; both movements appear in history.

## 4. Lifecycle states + transition hooks

States are derived (`lifecycle.derive_state(observed, enabled)`): `running`;
`restarting`/`dead` → `failed`; `exited` → `failed` only when declared
enabled (a deliberately-stopped service is `stopped`); else `unknown`. VMs map
analogously from VMM power states.

### Events

| Event | Container hook | Host hook | Aborts on failure? |
|---|---|---|---|
| `pre-start` | — | ✓ | ✓ (unless `force`) |
| `post-start` | ✓ | ✓ | never |
| `pre-stop` | ✓ (the DB-quiesce path) | ✓ | never — stop must always work |
| `post-stop` | — | ✓ | never |
| `pre-deploy` | — | ✓ | ✓ (unless forced path) |
| `post-deploy` | — | ✓ | never |
| `pre-shutdown` / `post-resume` | — | ✓ (instance scope) | never |

Firing is 1:1 at the public verb: deploy verbs (set-image, update, bundle,
rollback, reconcile add/replace) fire ONLY `pre-deploy`/`post-deploy` — the
reconcile-REPLACE inner remove passes `fire_hooks=False`, so a replace never
fires a spurious stop quiesce. Hook failures inside a reconcile stay
per-service isolated. A hook that shells back into `syrvis` is a no-op
(`SYRVIS_IN_HOOK=1` re-entrancy fuse).

### Container hooks (declared; the DB-checkpoint path)

```yaml
tasks:
  checkpoint: { command: ["psql", "-c", "CHECKPOINT"] }
hooks:
  pre-stop: checkpoint     # only pre-stop / post-start accepted
```

`hooks:` selects WHICH schema-audited `tasks:` argv runs at WHICH event — it
can never supply code, and it runs `docker exec` under the container's own
confinement. 30s default timeout (`run_task` keeps its own 300s).

### Host hooks (drop-in; root-authored)

One executable per event: `$SYRVIS_HOME/hooks.d/<workload>/<event>` and
`hooks.d/instance/<event>`. A host hook is arbitrary root code — same trust
class as `jobs/` scripts (derive-not-declare): it runs ONLY if the leaf and
EVERY parent directory up to `hooks.d/` are root-owned, not group/world
writable, and the leaf is a non-symlink regular executable. Anything else is
skipped with a warning (fail-open — the control is that an unprivileged
operator cannot WRITE such a file, not runtime blocking). Execution: clean
env (`SYRVIS_HOOK_EVENT/SCOPE/WORKLOAD/REASON`, context like
`SYRVIS_HOOK_IMAGE`), cwd `$SYRVIS_HOME`, stdin `/dev/null`, 30s timeout,
output captured to `logs/hooks.log` (16 KiB cap). `chmod -x` is the off
switch.

### Per-service graceful-stop knobs

```yaml
shutdown:
  stop_timeout: 120   # SIGTERM grace seconds (5..300, default 30) — DBs set 120+
  priority: 90        # instance-shutdown band 0..100 (lower stops first; DBs high)
```

`stop_timeout` is also emitted into the generated compose as
`stop_grace_period`, so the grace holds on EVERY stop path including the
daemon's own stop at OS poweroff. VMs declare `stop_timeout` (5..600, default
90) in `vms.d/` — the ACPI wait deadline.

## 5. Instance runstate + graceful shutdown (the UPS path)

```bash
sudo syrvis shutdown --reason ups [--timeout 180] [--vm-deadline 90] [--hold] [--json]
sudo syrvis resume [--json]
sudo syrvis restart --graceful [--json]
syrvis status          # shows an INSTANCE HALTED banner; --json carries runstate
```

`syrvis shutdown` sequence (budget-clamped, nothing aborts on one failure):

1. Snapshot running workloads, then **write `runstate.json` (halted) FIRST** —
   the gate closes the race where a cron reconcile restarts just-stopped
   containers (reconcile plans a `start` for any enabled+exited service).
2. Instance `pre-shutdown` host hook (log-only).
3. Issue VM ACPI shutdown (fire-and-forget — guests drain in parallel with
   the container stops; VMs dominate wall-clock).
4. Stop L2 services in ascending `shutdown.priority` bands: per service,
   `pre-stop` hooks (container quiesce, then host) → `compose stop -t <grace>`
   → `post-stop`. Declared intent (`enabled` flags) is NEVER touched.
5. Wait for VMs to power off (deadline-clamped); force-off stragglers.
6. Stop core per-container with Traefik LAST (ingress dies last).
7. Refresh runstate with the outcome. Exit 0 = clean, 2 = degraded (listed).

**Halted gate:** `services_d.apply_reconcile_plan` and
`DockerManager.start_core_services/restart_core_services` refuse while halted
(`InstanceHaltedError`, code `instance_halted`) — enforced at library
chokepoints so the CLI, MCP, dashboard, and estate are all covered. Planning
(`--dry-run`) stays allowed. Targeted `service start` is deliberately not
gated (one workload can be brought up without un-halting).

**Boot matrix** (`syrvis reconcile --boot`, the existing S99 line — unchanged,
no reprovision needed):

| runstate | boot behavior |
|---|---|
| absent (active) | normal best-effort reconcile (today's behavior) |
| halted, `reason: ups` | AUTO-RESUME: clear + start core + power on VMs + reconcile L2. Required explicitly — a graceful stop disarms `restart: unless-stopped`, so Docker will NOT bring core back by itself. |
| halted, `reason: maintenance` (or `--hold`) | stay down; log; exit 0. Survives reboots until `syrvis resume`. |

`syrvis resume` order: core first (routing substrate + macvlan shim), then
snapshot VMs (else declared enabled+autostart), then L2 via the ONE reconcile
engine (`trigger: resume`), then the instance `post-resume` hook. A resume
killed mid-flight leaves the instance **halted** (runstate clears only at the
end; `lifecycle.resume_instance`) — recovery is re-running `syrvis resume`. A
resume that *completes* with failures (exit 2) leaves it active-but-partial;
recover with `syrvis start` + `sudo syrvis reconcile`.

### UPS integration (home-tech)

The estate's NUT low-battery hook makes ONE seam call and then proceeds to
host poweroff on its own timer regardless of exit code:

```sh
ssh -F <ssh_config> syrvis-nas sudo -n syrvis shutdown --reason ups --json
```

Budget math: internal default 180s < seam `timeout_s` 240 < NUT shutdown
timer. Trigger at a battery threshold leaving ≥ 4 minutes. SyrvisCore never
powers off the host — that stays NUT/DSM's job.

## 6. Seam + MCP surface

New registry commands (sudoers + shim regenerated; drift-tested):

| id | argv | class |
|---|---|---|
| `deployment_history` | `syrvis history [--json] [-- <name>]` | read, no sudo |
| `service_rollback` | `sudo syrvis service rollback --to <rev> -y -- <name>` | destructive (token); `--to` is REQUIRED over the seam |
| `shutdown` | `sudo syrvis shutdown --reason <ups\|maintenance> --json` | converge, token-free (unattended NUT must fire it; reversible) |
| `resume` | `sudo syrvis resume --json` | converge |
| `restart_graceful` | `sudo syrvis restart --graceful --json` | converge |

MCP tools mirror these: `deployment_history` (read-only), `service_rollback`
(two-call confirmation handshake), `shutdown`/`resume`/`restart_graceful`
(idempotent hints). The `reason` and `revision` slots are validated three
times: CLI Choice, MCP validator, shim predicate.

The dashboard (third adapter) surfaces the same, in-process: a **Deploys** tab
(`GET /api/deployments`, redacted at the source) with per-revision rollback
(`POST /api/services/{name}/rollback`, gated by `ENABLE_L2_MUTATIONS` like the
other compose-shelling L2 verbs), a cross-tab **halted banner** driven by
`GET /api/runstate`, and `shutdown`/`resume` as SSH-hint actions (the
container must not stop the instance it runs inside).

## 7. Files

| Path | Purpose | Perms |
|---|---|---|
| `data/deployments/<workload>/NNNN.json` | deployment records | 0644 (0640+group with inline env) |
| `data/state/runstate.json` | halted record (absent == active) | root 0644, names only |
| `hooks.d/<workload>/<event>`, `hooks.d/instance/<event>` | host hooks | root 0755, trust-checked |
| `logs/hooks.log` | structured hook outcomes | appended, 16 KiB/run cap |
