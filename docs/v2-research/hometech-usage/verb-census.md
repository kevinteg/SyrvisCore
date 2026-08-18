# Exhaustive verb census (grep-driven, whole repo)

## What this slice is

A mechanical census of every reference to a SyrvisCore verb anywhere in `/Users/kevinteg/code/home-tech` (excluding `.git/` and `site/`), normalized against the platform's own seam registry — `SyrvisCore/packages/syrviscore/src/syrviscore/seam/registry.py`, which enumerates **exactly 77 `Command` rows** and is the single generator of the sudoers file, the forced-command shim allowlist, and the MCP tool surface. Counts were produced by walking every non-binary file, matching `(syrvis|syrvisctl|{SYRVIS_*}|{WRAPPER}) <subcommand tokens>`, and assigning each hit to the most specific registry variant by the flags present on the line.

**Methodological caveat that matters for the retrospective:** raw grep *overstates* doc traffic and *understates* execution. Most hits are prose (design docs, runbooks, review handoffs). Conversely, every mature caller wraps the seam in a helper — `verify-all:_seam_nas(verb, sudo=)`, `apply-instance:seam(remote_cmd, stdin=)`, `recreate-located:seam(remote, sudo=)` — so the executed argv is assembled from a *fragment* (`"vm list --json"`) with no binary token, and grep never sees it. The executable inventory below is therefore hand-verified from the call sites, not from counts.

Channel taxonomy used: **SEAM** (`ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- '[sudo -n] /volume4/syrviscore/bin/syrvis …'`), **LOCAL-CRON** (on-NAS root, `$SYRVIS_HOME/bin/syrvis` or the SPK `syrvisctl`), **READER** (`ssh syrvis-reader`, raw shell, *outside* the registry), **MCP**, **BREAK-GLASS** (`ssh ds` / `cerebrate@ds`), **DASH-HTTP**.

## Verb-usage inventory (exhaustive)

### A. Executed over the SEAM from Mac-resident scripts (the real surface — 17 distinct argv shapes)

| # | argv shape | caller (file:line) | cadence | data | consumption |
|---|---|---|---|---|---|
| 1 | `stack hostnames --json` (no sudo) | `scripts/verify-all:1699` (`check_nas_reach`) | every `verify-all` sweep + `--unattended` cron | JSON read | reachability only; PASS/INFO |
| 2 | `status --json` (no sudo) | `scripts/verify-all:3269` (`_seam_status`, **memoized once per sweep**) | per sweep | JSON read | `runstate` feeds `nas.runstate`, `nas.version`, `nas.monitoring` gate (`:3289,:3419,:7459,:7585`) |
| 3 | `status --json` (no sudo) | `scripts/apply-instance:280` (`require_active_runstate`) | per `--converge` | JSON read | hard exit if `runstate.state == halted`, pointing at `resume` |
| 4 | `sudo -n … schedule list --json` | `scripts/verify-all:3320` | per sweep | JSON read | managed-crontab block presence + `plan.in_sync` |
| 5 | `service list --json` (no sudo) | `scripts/verify-all:3368`; `scripts/apply-immich-secrets:96`; `scripts/recreate-located:83` | sweep / per secret push / per recreate run | JSON read | membership grading; "is this service deployed yet"; the running-set the recreate plan filters |
| 6 | `logs -n 50 -- vmagent` (no sudo) | `scripts/verify-all:3464` | per sweep | text read | greps for push failures |
| 7 | `sudo -n … updates --json` | `scripts/verify-all:3496` (90 s timeout) | per sweep | JSON read | `update_count` → INFO, report-only; **explicitly refuses to apply** (`:3482` "NOT `syrvis service set-image`") |
| 8 | `sudo -n … export --json` | `scripts/verify-all:3539` (`nas.drift`); `scripts/apply-instance:135` (`live_disabled`) | per sweep / per apply | JSON read | declaration-name set vs `syrvis/services.d/*.yaml` (39 files); and the **`enabled:false` preservation overlay** |
| 9 | `vm list --json` (**no sudo — bug**) | `scripts/verify-all:4336` | per sweep | JSON read | registry row `vm_list` is `sudo=True`; the shim cannot match this argv, so the check permanently degrades to `INFO "unreachable"` and prints a note blaming an unshipped `vms.d` feature |
| 10 | `verify --json` (no sudo, 90 s) | `scripts/verify-all:7201` | per sweep | JSON + **exit-code semantics** (`:7204` "EXITS 1 WHEN UNHEALTHY — that is the point") | per-check rollup; `fixable` flag surfaces `verify --fix` as the *suggested* action, never auto-run |
| 11 | `sudo -n … apply [--dry-run\|--allow-secret-change\|--allow-enable-change] --json` **+ stdin bundle** | `scripts/apply-instance:296` | manual, per config change | `syrvis-instance/v1` JSON on stdin (`.env` + `stack.yaml` + full services.d replace-set) | prints report; banners added/removed `.env` keys; surfaces `guard_enable_change` refusal **verbatim** |
| 12 | `sudo -n … stack apply` | `scripts/apply-instance:389` | `--converge` only | exit-code gate | fail → exit |
| 13 | `sudo -n … reconcile --json -y` | `scripts/apply-instance:400` | `--converge` only | JSON + exit code | fail → exit with "do not reach for `--force` before reading it" |
| 14 | `history --json` (no sudo) | `scripts/deploy-stack:257` (`fetch_history`) | per deploy failure/timeout | JSON read | picks `max(revision)` where `action ∈ {deploy,rollback} ∧ outcome == success` → prints a **rollback hint string**, never executes it |
| 15 | `sudo -n … deploy [--force] -- <name>` **+ stdin bundle** | `scripts/deploy-stack:470` | per-deploy, one call per service, serial | `syrvis-bundle/v1` (manifest + configs + sops-decrypted secret *values*) on stdin | exit-code gate; refusal text echoed verbatim (`guard_bulk_degraded`); `TimeoutExpired` is deliberately **not** stringified (argv would leak the bundle) |
| 16 | `sudo -n … secret set -- <svc>` **+ stdin** | `scripts/apply-immich-secrets:127` | manual | env_file on stdin | exit-code gate |
| 17 | `sudo -n … config set -- <name>` **+ stdin** | `scripts/apply-jobs:177` | manual, per job conf (17 `.conf.tmpl`) | rendered conf on stdin | exit-code gate; prints the verb's own line |
| 18 | `sudo -n <volume>/syrviscore/bin/syrvis stack hostnames --json` (**sudo variant**, `stack_hostnames_full`) | `scripts/lan-dns-plan:93` | per DNS plan | JSON read → cached to disk | needs `domain` + `traefik_ip`, which the non-sudo variant returns null for |
| 19 | `dashboard generate --all --json` (no sudo) | `scripts/gen-syrvis-dashboard:68` | manual / `--check` drift gate | JSON read | writes `syrvis/stacks/monitoring/config/grafana/dashboards-syrvis/*.json`; asserts `__syrviscore.generated` marker on every entry |
| 20 | `sudo -n … service recreate -- <name>` | `scripts/recreate-located:212` | maintenance-window loop | exit-code gate | serial, **stops on first failure**, then points at `history --json` and `./scripts/accept` |

### B. Executed LOCALLY on the NAS as root (managed crontab, materialized by `schedule sync`)

Only **two of 30 job scripts invoke a SyrvisCore verb at all**:

| argv | caller | cadence | consumption |
|---|---|---|---|
| `timeout 120 /volume4/syrviscore/bin/syrvis verify --smoke --json` | `jobs/nas-heartbeat:65-67` | per `nas-heartbeat` tick | **exit code only** → Healthchecks.io ping vs `/fail` |
| `/var/packages/syrviscore/target/venv/bin/syrvisctl backup create -o "$final"` | `jobs/syrviscore-backup:255` | nightly | exit code + artifact existence + size sanity vs previous archive |

Note the `-o` flag: the registry's `backup_create` row takes **no arguments**, so this shape exists only locally and is unreachable over the seam.

The other **28 jobs bypass the CLI entirely**: 26 read `$SYRVIS_HOME` paths directly (`config/services.d/*.yaml` as *text*, `data/state/intent.json`, `data/job-push/`) and 23 shell straight to `/usr/local/bin/docker`. `jobs/intent-metrics:136` states the doctrine explicitly: *"Not `syrvis service list --json`: this job runs as root from cron INSIDE the box and must keep working when the CLI cannot (a half-installed version, a `current` symlink that does not resolve)."*

### C. Channels that are not the seam

- **READER** (`ssh syrvis-reader`, raw shell, zero registry coverage): `verify-all:2707` (backup `find` probe), `:3063` (file-plane probe across volumes/shares), `:6525` (`/etc/passwd` shell audit); `scripts/accept:273`; `scripts/backup_drill.py:427`; `scripts/secure-share` (`grep -Fq … /proc/mounts`).
- **DASH-HTTP**: `scripts/accept:107` → `GET https://dash.konsume.org/api/services` (curl, `--resolve` pinned to `traefik_ip`) for `restart_count`; `worker/api.js:548` → `GET https://syrvis.konsume.org/api/summary` behind a path-scoped CF Access service token.
- **BREAK-GLASS**: 11 `cerebrate` references in `scripts/`, 34 in `wiki/`, 57 in `design/`. Executable break-glass survives in `scripts/secure-share` (interactive sudo password prompt) and in runbooks: `immich-restore.md` (~15 `ssh ds "$DOCKER exec …"` psql lines), `access-hardening.md`, `tls-certificates.md`, `boot-integrity-gate.md` (`scp`), `jobs-pin-cutover.md:125`, `window-close-2026-08-24.md:659` ("costs one `cerebrate@ds` login, which pages. Batch it or skip it").
- **MCP**: referenced as an *alternative* ("seam/MCP") ~30 times and by tool name in `thoughts/` audits; `.mcp.json` wires it; **no `mcp__syrviscore__*` call appears in any executable path**.

### D. Master frequency (registry row × total mentions × executable sites)

Top by total mentions: `schedule_sync` 228 (21 exec-file mentions, 0 executed), `install` 80, `service_list` 72, `verify` 71, `deploy` 60, `deployment_history` 59, `resume` 58, `status` 156, `logs` 145, `shutdown` 51, `service_stop` 51, `doctor` 48, `apply` 47, `schedule_list` 43, `reconcile_plan` 42, `activate` 38, `backup_create` 35.

**ZERO usage anywhere in the repo (9 registry rows):** `stop`, `stack_disable`, `service_task`, `service_update`, `service_declare`, `service_adopt`, `vm_start`, `vm_stop`, `vm_restart`.

**Doc-only, single mention, never executed (12 rows):** `service_catalog`, `profile_list`, `versions_list`, `check_updates`, `info`, `backup_list`, `vm_status`, `stack_enable`, `reconcile_force`, `verify_fix_smoke`, `deploy_force` (reachable via `deploy-stack --force` but unused), `apply_enable_change`.

**Doc-heavy but never executed by any script or job (13 rows):** `shutdown` (51), `restart_graceful`, `service_start`, `service_recreate` as a *runbook* step, `service_rollback` (printed as a hint string only), `activate`, `rollback`, `uninstall`, `cleanup`/`cleanup_preview`, `backup_cleanup`, `service_remove`, `profile_enable`, `service_run`/`service_add`.

**Verbs referenced that do not exist in the registry:** `syrvis share declare|list`, `syrvis user declare` (design/42 D6, design/51 D5 — "BUILD 3, in flight"), `syrvisctl restore` (4 references, DR-only), `syrvisctl seam sync|status` (SPK-local, never seam-reachable), and `syrvis up` (design/63/65 — the *future* consolidated bring-up verb, 12 references).

**Argv-shape variants observed:** `service rollback --to {rev} -y -- {name}`; `schedule sync --to <40-hex> --manifest sha256:<64-hex> --json` (plus a deliberate all-zeros *negative* proof in `jobs-pin-cutover.md:94` and `window-close:383`); `service shed --reason R [--until D] -- name`; `logs -n 50 -- name`; `shutdown --reason ups|maintenance`; `apply` × 4 flag combinations; `deploy [--force]`; `cleanup --keep 3 [--dry-run]`.

## Interaction patterns

1. **Bundles are built entirely client-side, in memory, and pushed on stdin.** Three of the four stdin writers have a script: `apply-instance` (whole-instance replace-set), `deploy-stack` (per-service), `apply-jobs` (per-conf), `apply-immich-secrets` (per-secret). All resolve sops → memory → ssh stdin, never argv, never a file. `deploy-stack` refuses to print `TimeoutExpired` because its `str()` includes argv.
2. **Read-then-decide, always on the client.** `apply-instance` reads `export --json` to compute a `live_disabled` overlay before building the bundle — home-tech re-implements intent preservation because pushing a git replace-set twice resurrected a deliberate 14-service load-shed on 2026-08-16. `recreate-located` reads `service list --json`, unions the shed set from `config/maintenance-state.yaml` *and* the NAS's `intent.json`, sorts stores-before-consumers by **name suffix heuristic** (`-db`/`-valkey`/`-redis`), and loops serially.
3. **Orchestration is a client-side for-loop with client-side ordering.** No caller asks the platform for a plan. `deploy-stack` streams one bundle per service in declaration order and carries a hand-written comment about the alerting pair having to go last after a 32-minute outage.
4. **Results are consumed three ways only:** parsed JSON (13 sites), bare exit code (8 sites), or *verbatim stderr echo* when a server-side guard refuses. The third is a deliberate pattern — three separate scripts document that paraphrasing a `guard_*` refusal destroys its entire value.
5. **Memoization and budget awareness are hand-coded.** `verify-all` memoizes one `status --json` for four consumers; `recreate-located` hard-codes the registry's own `service_recreate` timeout budget; `deploy-stack` bans wrapping the verb in `timeout` because a killed deploy leaves containers stopped.

## Workarounds & missing verbs (the negative space)

- **`schedule sync` has 228 mentions and zero scripted callers.** The most-discussed verb in the repo is invoked only by a human or an agent typing it over the seam. `apply-jobs` explicitly divides labour and takes the *other* half. Nothing reconciles the pin.
- **`jobs/seam-selfheal` rewrites `/etc/passwd` as root every 5 minutes** because DSM reverts `syrvis-operator`/`syrvis-reader` to `nologin` on boot and on any user-db write. The seam's own liveness is maintained by a repo-supplied shell script. There is no verb for "assert the seam accounts".
- **28 of 30 cron jobs refuse the CLI on principle** and parse `config/services.d/*.yaml` with a regex (DSM's `/usr/bin/python3` has no PyYAML) plus `docker ps`. There is no on-box, dependency-free, CLI-independent state read.
- **`syrvis status` is structurally blind to `restart_count`**, so `scripts/accept` reads the dashboard's `/api/services` instead — and after `DASHBOARD_AUTH_MODE=oidc` landed, that 401'd, forcing a *second* fallback through VictoriaMetrics (`docker_container_restart_count`). Two workarounds stacked on one missing field.
- **`jobs.d/hourly-reconcile.yaml` is a declaration with no script**, shipped `enabled: false` behind three hand-written arming preconditions. The repo's own note: "`jobs.d/` carried 24 declarations and NOT ONE of them reconciles… the homebase's self-healing story is 'someone notices'."
- **`vm list --json` is called without `sudo -n`** in `verify-all:4336` against a `sudo=True` registry row — a silent permanent UNKNOWN, mis-narrated as an unshipped feature. The read/write sudo split is called out as "real and subtle" in three separate runbooks and `seam-dead-after-boot.md:32` notes `export` breaks the naive rule.
- **`service rollback` is never executed** — `deploy-stack:453` computes the exact command and prints it, plus a caveat that rollback is GitOps-ephemeral and the repo must be reverted too.
- **Whole-declaration transport is missing**: `FABLE-HANDOFF.md:131` (P4) notes `service_declare` carries only six scalars, so anything with `env_file`/`volumes`/`healthcheck` must go through `apply`'s bundle — which is why `service_declare` has zero usage.
- **`secret_set` had no MCP tool** (`thoughts/…2026-07-19-estate-adversarial-review.md:44`), reachable only via raw SSH, outside MCP validation and audit — with a noted wrapper-path drift (`/volume4` vs `/volume1`).
- **No `share`/`user` verbs**: `syrvis/shares.d/` (39 services, a `shares.d/schema.md`, `data.d/`, `volumes.yaml`) is declared in git and reconciled *by hand-runbook*, pending "BUILD 3".
- **DR still needs break-glass**: `immich-restore.md` runs ~15 `ssh ds "$DOCKER exec -i immich_postgres psql …"` lines; `secure-share` prompts for cerebrate's sudo password. Both page the owner by design.

## Observations for a v2 agent design

1. **The registry is 77 rows; the repo executes 20 argv shapes.** Roughly a quarter of the surface carries all the traffic, ~30% is doc-only, and 9 rows are dead. A consolidated agent should be sized to the *executed* set plus the three whole-plane gaps (shares/users, schedule pin reconcile, seam self-assertion), not to CLI parity.
2. **Ship a plan/apply pair, not primitives.** Every serious caller re-implements the same loop: read state → subtract shed/disabled → order by a local heuristic → iterate serially → stop on first failure → point at history. `recreate-located`'s stores-first-by-name-suffix and `deploy-stack`'s alerting-pair-last are dependency graphs written in comments. A single `plan`/`apply` with server-side topological ordering (design/63 M1's `depends_on`, and `syrvis up`) deletes three client-side orchestrators.
3. **Make intent a first-class read, not an inference.** `apply-instance` performs an `export --json` round-trip purely to avoid resurrecting a load-shed, and `recreate-located` unions two shed sources. The agent should expose "effective desired state after all overlays" as one read so no client ever computes it.
4. **Kill the sudo/no-sudo argv split.** It has produced a live silent-degradation bug (`vm list`), three runbook warnings, and one documented exception (`export`). A v2 agent should present one transport where privilege is a server-side property of the verb.
5. **Serve on-box consumers too.** 28 cron jobs deliberately route around the CLI for availability reasons that are correct. A Go agent that is always-resident and answers a local socket with a stable JSON state document — readable when `current` is a dangling symlink — converts 26 text-parsing jobs into one query, and would let `hourly-reconcile` finally exist.
6. **Surface the fields the incidents needed.** `restart_count` cost two fallback paths and a 15-minute detection gap. Acceptance-shaped reads (restart deltas, per-service health transitions, zero-byte secret detection) belong in the agent, not in curl-against-the-dashboard.
7. **Preserve the two properties the callers depend on**: stdin-only secret transport (never argv), and verbatim guard refusals that *name what blocked them*. Both are load-bearing and independently documented in four scripts.
8. **Own the self-heal.** Boot-time shell reversion, jobs-pin reconcile, and crontab-block assertion are all "the platform's job, done by a repo script running as root". A device-lifecycle agent is exactly the right home for them.