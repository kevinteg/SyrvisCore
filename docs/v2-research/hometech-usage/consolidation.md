# Verb-surface consolidation

## The v1 surface as actually used

**The registry is 77 rows; home-tech executes about twenty argv shapes.** The seam registry (`syrviscore.seam.registry`, 77 `Command` rows, 18 slot kinds) is the single generator of the sudoers file, the forced-command shim allowlist and the MCP tool surface. Against it, the verb census finds ~20 executed argv shapes from Mac-resident scripts (its own header says 17 — the discrepancy is itself a datum about counting flag-variants as verbs), two locally-executed shapes on the NAS, and a large doc-only tail. Nine registry rows have **zero usage anywhere in the repo** (`stop`, `stack_disable`, `service_task`, `service_update`, `service_declare`, `service_adopt`, `vm_start`, `vm_stop`, `vm_restart`); twelve more are single-mention doc-only (`service_catalog`, `profile_list`, `versions_list`, `check_updates`, `info`, `backup_list`, `vm_status`, `stack_enable`, `reconcile_force`, `verify_fix_smoke`, `deploy_force`, `apply_enable_change`); thirteen are doc-heavy but never executed by any script or job (`shutdown` at 51 mentions, `restart_graceful`, `service_start`, `service_rollback` — computed and *printed*, never run — `activate`, `rollback`, `uninstall`, `cleanup`, `backup_cleanup`, `service_remove`, `profile_enable`, `service_run`/`service_add`). Roughly a quarter of the surface carries all the traffic. (One conflict worth recording: the census scores `service_task` at zero, while the declaration tree carries `romm-db.reinit-datadir` and it was invoked on 2026-08-15 — i.e. its single lifetime use was an incident-day human, not a caller.)

**By channel.** Five channels are live, and only two are load-bearing for automation. The **seam** (`ssh -F …/ssh_config syrvis-nas -- '[sudo -n] /volume4/syrviscore/bin/syrvis <verb> --json'`) carries every scripted read and every write. The **reader identity** (`ssh syrvis-reader -- '<raw shell>'`) exists *entirely* because the seam has no filesystem verbs: `verify-all`'s backup-freshness probe, the file-plane probe, the `/etc/passwd` audit, `accept`'s zero-byte-`secrets.env` sweep, `backup_drill.py`, `secure-share`. **Dashboard HTTP** exists for exactly one missing field: `restart_count`. **MCP** — 50 tools, 17 read-only, 19 mutating-untokened, 13 HMAC-confirmed plus `install` — appears in `.mcp.json`, in three skills, and in **no executable path in the repo**; five read-only tools are pre-approved and the rest prompt. **Break-glass** (`cerebrate@ds`) pages the owner on every login and is where every `mv`, `rsync`, `docker exec`, `docker rm`, `docker inspect`, `/etc/passwd` repair and rootfs install lives.

**By cadence.** Daily: one LaunchAgent → `monitor-tick` → `verify-all --json --unattended`, ~9 SSH round trips serving 20 seam-bound checks out of 83 (`CHECK_NEEDS`: 47 pure / 20 seam / 12 cloud-creds / 4 LAN), with three memoized readers (`_seam_status`, `_schedule_list`, `_service_list`) feeding ten checks. Every 3–15 minutes: 25 enabled cron jobs on the NAS — of which **exactly two invoke a SyrvisCore binary** (`nas-heartbeat` → `verify --smoke --json`, exit code only; `syrviscore-backup` → `syrvisctl backup create -o …`, a shape the registry cannot express and which is therefore unreachable over the seam). The other 28 bypass the CLI on principle: 26 read `$SYRVIS_HOME` paths as text, 23 shell straight to `/usr/local/bin/docker`. Per-change: `deploy-stack`, `apply-instance`, `apply-jobs`. Incident-only: `logs`, `history`, `resume`, `recreate`, `shed`/`unshed`, `rollback`.

**Argv-shape variance is the transport's dominant defect.** The read/sudo split is not derivable from the verb name (`export` is a sudo read; `stack hostnames` exists in both forms and the non-sudo one returns `domain`/`traefik_ip` as null), and it has produced a live silent bug: `verify-all:4336` calls `vm list --json` without `sudo` against a `sudo=True` row, so the shim cannot match, the check degrades permanently to `INFO "unreachable"`, and its note blames an unshipped feature. Three runbooks carry the split as a warning; `seam-dead-after-boot.md:32` notes `export` breaks the naive rule. Absolute path is mandatory (a bare `syrvis …` returns "not on allowlist", which reads exactly like a stale shim); `--json` is part of the matched argv, not a formatting choice. The permission plane's two allowlisted seam rules both spell a *read* with `sudo -n`, contradicting the repo's own FACTS row, and the measurement says they **never fired once** — "nobody spells it the same way twice." 4,338 mined Bash calls, 4.7% auto-approved, 134 walks around the deny list, 87 `timeout N ssh …` wraps of a seam the deploy doctrine forbids wrapping.

**The client-side orchestration burden is the real surface.** Nothing asks the platform for a plan. `deploy-stack` assembles `syrvis-bundle/v1` entirely laptop-side (manifest + config sources + sops-decrypted values), re-implements SyrvisCore's `_SECRET_MAX_BYTES = 65536` as its own constant so dry-run fails instead of a half-completed apply, streams one bundle per service serially in declaration order, stops at the first non-zero rc, then makes a *second* round trip to `history --json` purely to print a revision and synthesize a rollback argv it never executes. Ordering is three uncoordinated hand-rolled mechanisms: reversed `shutdown.priority` bands (the platform's only signal), "declaration order = deploy order" in `deploy.yaml`, and `ALERTING_PAIR_LAST` — a hardcoded stable-partition in the client born from a 32-minute undelivered-alerts window. `recreate-located` invents a fourth: stores-first by name suffix (`-db`, `-valkey`, `-redis`, …). `apply-instance` reads `export --json` *before every write* solely to overlay live `enabled:false` back onto git's replace-set, because pushing the set twice resurrected a deliberate 14-service load-shed. `verify-all` hand-codes memoization as a seam budget and classifies "I could not answer" with **two regexes over summary prose**, gated by a build-breaking meta-check (`docs.unknown-coverage`) that exists to keep the prose honest. `scripts/accept` fuses three planes (dashboard HTTPS, a VictoriaMetrics fallback added the evening OIDC landed, a reader `find`) plus a wall-clock hold, and was written only after four bespoke acceptance lists in one week. Six copies of the same ssh-runner/sops/JSON-hardening code carry divergent timeouts (30/45/60/300/600 s, some unbounded); three independently strip the OpenSSH post-quantum banner. Runbooks carry the rest: 62 files, a five-wave 14-service recreate table, G0–G6 and R0–R4 ladders, per-name loops unrolled because `service stop` takes one name.

## Consolidation map

Dispositions: **(a)** declarative intent write the agent converges · **(b)** field in one rich status/query API · **(c)** surviving imperative agent method · **(d)** deleted/absorbed.

| v1 verb / family (as used) | v2 disposition | Notes |
|---|---|---|
| `status --json` | (b) | `Get`. Plus the fields it was blind to: `restart_count`, health, `flapping`, `StartedAt`, `blocked_by`, `shed_reason` — the absence of the first cost two stacked workarounds |
| `service list --json` | (b) | `Get`; must carry the *effective* state after intent overlays so no client recomputes it |
| `export --json` | (b) | `Get(declarations)`; field-level merge in `Declare` removes its pre-write role entirely |
| `verify --json` / `--smoke` | (b) | Checks become facts with `answered/observed_at/unreachable_reason`; payload-first, exit code only about transport |
| `history --json` | (b) + `Events` | Revision facts are queryable; the post-deploy round trip is absorbed by `Declare`'s response |
| `schedule list --json`, `schedule dsm-tasks` | (b) | One census; `plan.confs` published rather than proxied through a metric |
| `stack hostnames --json` (both variants) | (b) | The sudo/non-sudo split dies with argv |
| `updates --json`, `version`, `info`, `versions list`, `check updates`, `backup list`, `catalog`, `profile list`, `cleanup preview` | (b) | All doc-only or report-only today; fields, not verbs |
| `vm list` / `vm status` | (b) | Ends `nas.vms` as a designed-in standing UNKNOWN |
| `reconcile --dry-run` / `reconcile_plan` / `apply --dry-run` | (c) `Plan` | Purity split is a kept principle; must return per-item `{unchanged\|config-changed\|secret-changed\|image-changed\|blocked}`, prune set, topological order, and limit pre-flight |
| `logs -n N -- <svc>` | (c) `Logs` | Unbounded byte stream + follow; payload must be protocol-labelled untrusted |
| **stdin bundle: `apply`** | (a) `Declare` | Merge, not replace-set; transactional; live-disabled preservation becomes a server property |
| **stdin bundle: `deploy <name>`** | (a) `Declare` | Same method, narrower scope; configs converge *with prune* (seven retired Grafana boards still serve from disk today) |
| **stdin bundle: `secret set`** | (a) `Declare` | Spec-hash convergence makes the missing "now re-read it" verb unnecessary |
| **stdin bundle: `config set`** | (a) `Declare` | Job confs are part of the declared set, not a second channel |
| `stack apply` | (d) | Absorbed by `Declare` + the loop |
| `reconcile [-y]`, `reconcile --force`, `reconcile_prune` | (c) `Converge` | Survivor: "run the loop now and block for per-item outcomes." `--force` is a field, journaled |
| `service start` / `stop` | (a) `SetIntent` | `stop` = shed with `reason: operator`; ends the ephemeral-vs-durable split that resurrected 14 services |
| `service shed` / `unshed` | (a) `SetIntent` | Already the right shape; generalized to windows, accepted alerts, hardware watches |
| `service recreate` | (d) | Absorbed: direct Docker API + spec-hash comparison makes recreate-on-content-change automatic. `recreate-located` disappears with it |
| `restart`, `restart --graceful`, instance `start` | (d) | Absorbed by device intent + `Converge` |
| `shutdown --reason ups\|maintenance`, `resume` | (a) `SetIntent(device: drained\|in-service)` | design/65; boot converges to intent, which is what the UPS hook and G5 both want |
| `service rollback --to REV` | (a) `SetIntent` | Never executed today — only printed. Becomes a pinned-revision intent write |
| `service set-image` | (a) or (d) | `verify-all` explicitly refuses it in favour of a git pin bump; keep as an intent write, expect near-zero use |
| `service update`, `declare`, `adopt`, `add`, `run`, `remove` | (d) → (a) | Zero/near-zero usage because they cannot carry a whole declaration; absorbed by `Declare` (the P4 six-scalar gap dies) |
| `service task --task` | (c) `RunTask` | Genuine one-shot: declared name, exit code + output, not convergent |
| `schedule sync [--to --manifest]`, `schedule apply` | (a) `SetIntent(jobs pin)` | 228 mentions, zero scripted callers, nothing reconciles the pin. As intent, the loop reconciles it |
| `dashboard generate --all` | (d) | Dashboard backend folds into the agent; generated boards become a published artifact |
| `verify --fix` | (d) | Absorbed into converge + declared heals |
| `syrvisctl install / activate / rollback / uninstall` | (a) `SetIntent(version pin)` | Download → verify sha256 → symlink swap is convergence over a file |
| `syrvisctl list / check / info` | (b) | Fields |
| `syrvisctl cleanup [--keep N]` | (a) | Retention becomes a declared field |
| `syrvisctl backup create / list / cleanup` | (a) + (c) | Schedule + retention declared; off-cadence run via `RunTask`; `list` is a field. The `-o` shape that only works locally disappears |
| `syrvisctl restore` | floor | Stays off the socket: "disaster recovery must not depend on the thing being recovered" — now served by the rescue binary |
| `syrvisctl doctor` | (c) `Rescue` | Not a socket method: a binary mode that works with the daemon dead and no resolvable home. Non-negotiable, and it ends the `syrvis doctor` / `syrvisctl doctor` name collision at 3 a.m. |
| `syrvisctl seam sync / status` | (d) **deleted** | No generated shim, no sudoers pair, no auto-sync fail-open, no drift test — the forced command is the agent's own subcommand |
| Token-gated destructive family (13 MCP tools) | (c) `Confirm` + confirm field | The handshake moves *into the agent*, so the seam spelling of `schedule sync` stops being ungated while the MCP spelling is |
| Break-glass floor (`setup`, `--purge`, VM create/delete, `docker exec/rm/inspect`, `mv`/`rsync`, `/etc/passwd`, rootfs installs) | floor, with the **read** half migrating | Deliberate boundary kept for writes; every *read* it currently forces (`ls -d /volume*/syrviscore*`, `find … secrets.env -size 0`, `du`, `/proc/mdstat`, `df`) becomes `Census`/`Attest` |
| `syrvis-reader` raw shell (no registry coverage) | (d) | Deleted as an identity: `Census` + `Attest` cover all four probes |
| Dashboard `GET /api/services` for `RestartCount` | (d) | Deleted: the field is in `Get` |
| Missing: metrics push | (d) | Absorbed by the job runner + `Emit`; retires 13 verbatim copies of `vm_push` and `jobs/_vm-push.sh` |
| Missing: `share` / `user` declare (BUILD 3) | (a) | The 74-file registry (`shares.d`, `data.d`, `backup-tasks.d`, `volumes.yaml`) the platform cannot see today |
| Missing: `service move --to /volumeN` | (a) | `volume_locations` intent write + a long-running operation observed via `Get`/`Events` |

## The residual imperative core

Thirteen socket methods plus one rescue mode. Everything above lands here.

1. **`Get(scope, fields) → Snapshot`** — the single rich read: runstate, services (with `restart_count`, health, `flapping`, `blocked_by`, `shed_reason`, effective intent), declarations, schedule plan, VMs, updates, versions, breakers, journal, capabilities/limits. One timestamp, per-fact `{answered, observed_at, unreachable_reason}`.
2. **`Census() → DeviceCensus`** — roots and collision siblings under every configured segment name, `current` resolution, seam-account shells, docker-group membership, `/proc/mdstat`, `df`, `meminfo`. Separate from `Get` *because its whole job is surviving `Get`'s failure domain*; docker-free, no resolvable home required.
3. **`Attest(since) → AcceptanceReport`** — file-plane facts under declared roots (zero-byte `secrets.env`, app-home presence, sizes/digests), restart deltas since a timestamp, health transitions.
4. **`Plan(scope) → Plan`** — pure, topologically ordered, with prune set and limit pre-flight.
5. **`Declare(bundle, opts) → Result`** — the one transactional write for everything file-shaped: instance, service, configs, secrets, job confs, jobs pin, shares. Merge semantics, dry-run flag, override ids, confirm token, and a response that already carries the revision.
6. **`SetIntent(targets, intent) → Result`** — device intent (`in-service|drained`), shed/unshed (= stop/start), enable/disable, pinned revision, platform version pin, retention, maintenance windows.
7. **`Converge(scope, opts) → ConvergeResult`** — run the loop now, block, return per-item outcomes.
8. **`RunTask(kind, name, args) → TaskResult`** — declared one-shots: service tasks, job runs, backup-now.
9. **`Logs(selector, opts) → stream`**.
10. **`Events(since) → stream`** — journal, deploy, breaker, heal, boot.
11. **`Emit(samples)`** — local sink for job metrics; and the agent's own outward heartbeat push.
12. **`Confirm(challenge) → token`** — two-call handshake owned by the device, inherited by every channel.
13. **`Capabilities() → Schema`** — verb table, capability class per verb, limits (the 64 KiB cap), per-verb deadlines, API version.
14. **`Rescue`** *(binary mode, not a socket method)* — `doctor` made permanent.

**Caller mapping.**

- **`deploy-stack`** → `Plan(stack)` → `Declare(bundle, dry_run)` → `Declare(bundle)`. Revision and annotation ride the response; ordering, `--resume`, and `--only`-as-computed-answer come from `Plan`. Gone: `history` round trip, `ALERTING_PAIR_LAST`, `_STORE_SUFFIXES`, the 65536 constant, the timeout zoo.
- **`apply-instance`** → `Declare(instance, merge)` → `Converge(all)`. Gone: the `export --json` pre-read, the live-disabled overlay, `require_active_runstate` (the agent refuses and names the reason), the `stack apply` + `reconcile` two-step.
- **`apply-jobs`** → `Declare(jobs)` → `Converge(schedule)`; offline `--check` stays as a git-side lint.
- **`verify-all` seam slice** → `Get` + `Census` + `Attest` — three calls replacing ~9 round trips, one instant instead of several.
- **`monitor-tick`** → `Get` + `Census`; blackout becomes a count of unanswered facts, not a regex over prose. Liveness rides `Emit` outward.
- **`accept`** → `Attest(since)` + `Events(since)`.
- **runbooks**: G0–G6 → `Census` → `Get` → `SetIntent(in-service)` → `Converge` → `Attest`. Window-close → `SetIntent(unshed × N)` + `Converge` + `Attest` (one call, not 14 unrolled round trips in a five-wave prose table). App-move → `Declare(volume_locations)` + `Converge` + `Events`.
- **NUT hook** → `SetIntent(device: drained, reason: ups)`; boot converges to intent.
- **boot** → the agent's own loop; `Rescue` and the rootfs gate assert it exists.
- **jobs plane** → local `Get` (replacing 26 text-parsing jobs), `Emit` (replacing 13 `vm_push` copies), agent-run scheduling.
- **MCP** → generated from `Capabilities()`; the 50-of-77 gap becomes structurally impossible.

## What the consumer deletes

- **`scripts/recreate-located`** — deleted entirely. Its plan, shed filter, store-first heuristic and budget are `Converge`.
- **`scripts/apply-immich-secrets`** — deleted (already superseded; `Declare` finishes it).
- **`scripts/gen-syrvis-dashboard`** — deleted with the dashboard image and its lockstep repin gate.
- **`scripts/deploy-stack`** — loses `ALERTING_PAIR_LAST`, `_STORE_SUFFIXES`, `SEAM_CONFIG_MAX_BYTES`, `fetch_history`/`rollback_hint`, `HOMEBASE_DEPLOY_TIMEOUT_S`, the banner strip, and the disarmed Grafana annotation lane. Keeps repo→bundle assembly and sops decryption.
- **`scripts/apply-instance`** — loses `live_disabled()`, `require_active_runstate()`, `--preserve-live-disabled`/`--force-enable` plumbing, and the two-step converge.
- **`scripts/verify-all`** — deletes `_seam_nas(sudo=)`, three memo helpers, `_ssh_reader()` and all four raw probes, `_is_unknown()`/`_unknown_cloud()`, `UNREACHABLE_SUMMARIES`, `docs.unknown-coverage`, `nas.job-confs` plane 2 and `config/job-confs.yaml`, the `SELFCHECK_PRIVILEGE_BLIND` allow-list, and the `nas.status-vs-metrics` dashboard/VM detour. `nas.vms` becomes a real check.
- **`scripts/accept`** — the dashboard-HTTP leg, the VictoriaMetrics fallback and the reader `find` all delete; what remains is `Attest` + exit-code mapping.
- **`scripts/monitor-tick`** — `push_liveness` SKIP resolves; `TWIN_COVERED` shrinks. The off-box two-port pre-flight and digit-masked signatures **stay** — the alarm must not live inside the thing it alarms about.
- **`scripts/maintenance-mode`** — byte-preserving YAML surgery and `push_window_metric` delete; `config/maintenance-state.yaml` stops being a hand-maintained mirror and becomes `SetIntent`.
- **`jobs/`** — `_vm-push.sh` and 13 copied blocks plus the `intent.vm-push-block` check delete; `intent-metrics`' regex YAML parse and `onyx-db-backup`'s `intent.json` grep become local `Get`; five `last-run.log` hacks and the never-built `job-wrap` become the agent's runner; `seam-selfheal` deletes (no login shell needed); `hourly-reconcile` deletes (the loop is the agent); `host-sysctls` becomes a declared field; `nas-heartbeat` becomes an outward `Emit`.
- **`rootfs/boot-integrity`** — **kept and shrunk**: assert the binary is present, resolvable and scheduled. It must not be absorbed.
- **`config/agent-permissions.yaml` + `.claude/settings.local.json`** — generated from `Capabilities()`; one prefix rule covers routine traffic, and the unbuilt `scripts/seam` is never built.
- **Six duplicated ssh/sops/JSON-hardening blocks** — one generated client.