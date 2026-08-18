# syrvisd — device lifecycle manager and deployment orchestrator (v2 design)

Grounded in the eight slice inventories, `docs/v1-retrospective.md` §4.7/§8/§9, and home-tech designs 60/61/63/65.

---

## 1. The lifecycle model

### 1.1 One axis, not three

v1 holds a service down three ways — `enabled: false` in the declaration, an ephemeral live-only `enabled:false` written by `service stop`, and a durable `shed` row in `intent.json` — and the code's own comment reads *"Three mechanisms, one field, cancelling out."* The 2026-08-16 shed resurrection is that sentence executing: `apply-instance` pushed git's declaration set, the fourteen deliberately-stopped services came back mid-array-rebuild, and the fix landed client-side as a `live_disabled()` overlay computed from `export --json`.

v2 has **one suppression mechanism: the drain**. A drain is a durable, scoped, reasoned record that lives in the agent's state store and *never* in the declaration set. `enabled:` stays in the file and means only "this service is part of the declared fleet." `stop` is sugar for a service-scoped drain with `reason: operator`. `shutdown --reason maintenance` is sugar for a device-scoped drain with `on_boot: hold`. `halted` stops being a separate runstate.

```json
{
  "id": "drn-20260816T0507-mdresync",
  "scope": {"kind": "volume", "selector": "/volume5"},
  "class": "hardware",            // hardware|maintenance|power|operator|automatic
  "reason": "md resync on volume5 array",
  "by": "seam:kevin",
  "since": "2026-08-16T05:07:11Z",
  "until": "2026-08-24T00:00:00Z",
  "state": "drained",             // draining|drained|restoring|expired|released
  "policy": {
    "dependants": "refuse",       // cascade|strand|refuse
    "deploys": "land",            // land|refuse
    "floor": "default",           // default|none
    "on_expiry": "hold",          // hold|restore
    "on_boot": "hold"             // hold|converge
  },
  "seq": 4417
}
```

### 1.2 Drain scopes, grounded

| kind | selector | resolved membership | real event |
|---|---|---|---|
| `device` | — | everything except the floor (§2.3) | UPS (`shutdown --reason ups`, NUT path), maintenance windows: share-rename 2026-08-16, ECC DIMM slot isolation, rs1221 standup |
| `volume` | `/volume5` | every service whose *effective* storage touches it: `location:`, `volume_locations:`, `fileplane:` binds, bind sources under that volume | md resync; `onyx-nvme-flip`; `immich-nvme-flip`; the 14-service shed of 2026-08-16 (11 on `/volume5`, 4 on `/volume6`) |
| `plane` | `monitoring`, `container`, `routing`, `storage` | services carrying that plane label + derived membership | 2026-08-14 (container plane: dockerd wedged, every verb hung 120 s); monitoring-stack redeploys |
| `set` | `stack:onyx`, `stack:immich`, or a label selector | stack membership from the declaration tree | onyx restore, immich restore, `immich-quiesced` windows |
| `service` | `onyx-opensearch` | one | today's `shed`/`stop` |

**Membership resolves at evaluation time, never at declaration time.** Every runbook already says this — *"resolve the list at execution; any list typed into a runbook goes stale"* — and `recreate-located` reimplements it with a `service list --json` read plus a name-suffix heuristic. Making it a property of the agent is what turns the 2026-08-16 hand-derived 11-to-17-name list into a query. Volume membership in particular is the read that cost a break-glass session: G2's `ls -d /volume*/syrviscore*` plus `grep '^location:' syrvis/services.d/*.yaml`.

### 1.3 Composition and precedence

Drains are a **set**, not a flag. Multiple drains overlap freely; a member is drained if *any* active drain covers it. Release is per-drain, so lifting the maintenance window does not lift the hardware drain underneath it — the exact failure mode of `config/maintenance-state.yaml`, where a row cleared only in the repo changes nothing and a row cleared on the box leaves the repo lying.

The effective-intent lattice is total and evaluated every tick, highest first:

1. **rescue** — agent degraded, no resolvable home, or the rootfs mirror disagrees with the volume store → all converge writes refuse, reads still answer.
2. **device drain (`on_boot: hold`)** → stop all but the floor.
3. **scope drains** (volume ∪ plane ∪ set ∪ service).
4. **declared `enabled: false`.**
5. **breaker open** → suppress *start* attempts; never stops a running container.
6. **blocked** — a hard `depends_on` edge onto a target that is disabled, drained, or invalid → `blocked` bucket, never a validation failure (design/63 D2 as amended: a deliberate load-shed must not fail every hourly reconcile for its dependants).
7. otherwise → should_run.

The rule that makes this safe is one sentence: **a desired-state push can change levels 4 and 6; it can never change levels 2 and 3.** Bundles carry declarations; drains are written only by intent methods. `apply-instance`'s live-disabled overlay, `deploy-stack`'s "skip `enabled: false`" reimplementation, and `guard_enable_change` all become unnecessary — the guard's *refusal text*, which names the blocking services, survives as structured data (§5.4).

### 1.4 Durability and boot

State lives in `state.db` (SQLite, WAL, single writer, real migration ladder) under `$SYRVIS_HOME`. Every drain mutation *also* writes `/usr/local/etc/syrvisd/intent.json` — rootfs, 0600, monotonic `seq`. On 2026-08-16 the volume-resident everything (wrapper, `current` symlink, S99 trampoline target, all 24 cron jobs, the self-healer) died in one rename; the rootfs boot-env cache and `rootfs/boot-integrity` were the only survivors, and both had to be hand-installed. The mirror is that lesson as a first-class artifact.

Boot sequence: agent starts from the rootfs binary → reads the rootfs mirror → **refuses to converge until it can prove the declared roots exist and are the declared roots**. A missing declared root is a hard error requiring explicit `--adopt`, never a create. This is the single loudest fact in the incident corpus: *"a convergence verb is a WRITE, and an absent declared root is treated as create"* — the mechanism that recreated four containers against an empty scaffold, wrote four zero-byte `secrets.env`, and produced six crash loops in wave two. Then, and only then, `boot converges to intent` (design/65): drains with `on_boot: hold` are re-asserted before anything starts, so a reboot during a maintenance window does not resurrect the window's contents.

If the mirror and the DB disagree, newest `seq` wins **and the disagreement is an event**, never a silent pick.

### 1.5 Expiry semantics

`until` is a **review deadline, not a timer**. Default `on_expiry: hold` for `class: hardware` and `power`; `restore` is opt-in and permitted only for `maintenance` and `operator`. An md-resync drain auto-expiring at 03:00 and starting fourteen services onto a still-degraded array is the shed resurrection with a clock instead of a laptop. An expired-and-held drain surfaces as `state: expired`, pages exactly once, and appears in `/status` as an open decision.

Garbage collection is real: a drain whose selector resolves to zero members for N days is reported and releasable; v1's shed rows are never GC'd, and a shed row for a deleted declaration blocks its dependants forever (retro §6.7).

---

## 2. Drain semantics

### 2.1 What draining does

`Drain.Declare` computes a plan, then executes it as a server-side job:

1. **Resolve membership** at plan time; report it. The set is part of the plan the operator confirms — no more five-wave tables in prose.
2. **Run declared pre-stop gates.** `immich-quiesced` (poll `/api/jobs`, refuse while any queue has `active` or `waiting`) becomes `drain_gate:` on the service manifest — an exec/HTTP probe with a bounded wait, run and journaled by the agent. Today it is a laptop script the operator has to remember before any verb that stops `immich-db`.
3. **Stop in reverse-topological order** over `depends_on`, with `shutdown.priority` as the *tie-breaker inside a wave* — derived, not parallel. This retires all three v1 ordering mechanisms at once: `ALERTING_PAIR_LAST` (a client stable-partition born from a 32-minute undelivered-alerts window), `_STORE_SUFFIXES` (`-db`/`-valkey`/`-redis` name matching), and "declaration order = deploy order."
4. **Budget reserve-first.** Stores keep their declared grace; consumers clamp into what remains. For a `device` drain the agent refuses to start when the measured reserve exceeds the *proven* transport window — v1's numbers are 240 s irreducible against an rc.d `stop)` wrapper bounded at `timeout 150s` with the comment "UNVERIFIED here." The agent measures its own window once at install (a timed no-op flush) and stores it; volume/plane/set drains are not rc.d-bounded and get their full declared grace.
5. **Journal every transition**, with `elapsed_s` unsaturated (v1 clamps it at the budget — the one number an operator would tune with).

### 2.2 Dependants outside the scope

Declared per drain, defaulted per edge kind:

- `refuse` (default for hard `healthy` edges) — plan-time refusal naming the outside dependants and the flag that would proceed.
- `cascade` — pull dependants into the scope; the expansion is reported *before* execution and recorded on the drain.
- `strand` (default for `soft` edges) — leave them up; they show as `blocked` with `blocked_by` naming the drained target, which is a state, not an alarm.

### 2.3 The observability floor

The floor is the set that must survive every non-device drain. It is **declared**, not hardcoded: `floor: true` on a manifest, plus everything at `tier: infra`. Default membership from the live fleet: the agent itself, `docker-socket-proxy` and `docker-health-exporter` and `ntfy-alertmanager` (the three `critical: true` services), `node-exporter`, `victoria-metrics`, `vmagent`, `vmalert`, `alertmanager`, `vector`. That is also `nas.monitoring`'s thirteen hard-coded collector names, moved from a laptop check into the device.

Three rules:

- A scope drain **never** stops a floor member. If a floor member is in the resolved set — `victoria-metrics` homed on `/volume5` during an md resync is the live case — the agent **refuses and names the conflict**. `--accept-blind` proceeds, records a blind window on the drain, and pages once. Today eleven `needs: seam` checks go blind together and the tally scores INFO as zero and prints clean; a blind window must be a declared object, not an emergent silence.
- A device drain stops the floor **last**, and only after emitting a terminal event out-of-band: ntfy, the Healthchecks dead-man, and the rootfs flight recorder. *"The alarm must not live inside the thing it alarms about."*
- The agent's own read path never depends on a floor member. Reads are answerable with dockerd wedged (§4.4).

### 2.4 Rehydration

`Drain.Release` writes intent and **starts nothing directly** — v1's `unshed` made exactly this choice and it is right. The convergence loop plans the restore: forward-topological, wave-gated on declared readiness (`started` / `healthy` / `soft`), breaker-consulted, journaled. The plan is readable before it runs.

This is what collapses `window-close-2026-08-24`'s §3.2–§3.3: read `service list --json`, then fourteen unrolled `service unshed` calls, then `./scripts/accept --quick`, then fourteen `service recreate` calls in five hand-authored dependency waves. In v2 that is `Drain.Release(id)` → `Watch(job)`.

### 2.5 Partial drain × deploys

Generalize v1's shed+deploy behavior into a rule: **a deploy into a drained scope lands bits and does not start.** The revision records `applied: true, started: false, held_by: <drain-id>`; the journal event is `skipped` with `reason: drained`. On release, the held revisions are precisely the set the restore wave brings up.

The agent decides *restart vs recreate* by spec-hash comparison, so `service recreate` disappears as a verb. This is not cosmetic: Docker bakes env at container CREATE time, `restart` cannot re-read a changed `env_file`, and that gap is why `secret set` had no companion verb for months, why `service recreate` (0.5.14) went uncalled by anything until `recreate-located` was written on 2026-08-16, and why `appenv-metrics` exists at all. When the env digest changes, the agent recreates; when only the image tag moved, it recreates; when nothing changed, it does nothing — G1 ("a no-op apply restarts zero containers") as a property of content addressing rather than a hand-built comparison.

`deploys: refuse` is available per drain for the case where landing bits is itself unsafe (a drained volume that is unmounted).

---

## 3. The deployment orchestrator

### 3.1 What replaces client-side deploy-stack

Today the client owns everything: bundle assembly, ordering, the 64 KiB cap (hand-copied from `_SECRET_MAX_BYTES`), the staging filter, the alerting-pair exception, the serial loop, "abort at first non-zero rc," and the follow-up `history --json` read. There is **no change detection client-side at all** — every `--apply` streams every non-staged service's full bundle, and the only scoping is a typed `--only`.

v2: one write method, `Deploy.Plan` / `Deploy.Apply`, over one bundle shape, with `target ∈ {instance, stack:<name>, service:<name>}`. The bundle carries declarations, configs, secrets, and `.env` as **content-addressed items**:

```json
{"apiVersion":"syrvis-bundle/v2","target":{"kind":"stack","name":"monitoring"},
 "declarations":{"vmagent":{...}},
 "items":[{"service":"vmagent","dest":"config/scrape.yml","mode":"0640","uid_hint":"nobody",
           "sha256":"…","size":41230,"secret":true,"content":"…"}],
 "env":{"NTFY_URL":"…"},
 "prune":{"configs":true},
 "policy":{"max_parallel":3,"on_failure":"continue-independent","health_gate":"strict"}}
```

### 3.2 Change detection is server-side

The agent computes `spec_digest = H(declaration ⊕ resolved image ref ⊕ config digests ⊕ secret digests ⊕ volume bindings ⊕ agent schema version)` and returns per service one of `new | unchanged | declaration-changed | config-changed | secret-changed | image-changed | removed`. That gives `--only` a *computed* answer, makes `--resume` trivial (re-plan; unchanged is unchanged), and ends the "I redeployed it" non-repair. It also fixes G1's v1 shape, where the digest read-back had to be bolted on so that a byte-identical redeploy of a secrets-bearing service stopped force-recreating.

### 3.3 Plan is pure, complete, and validating

Plan renders secrets. v1's dry-run deliberately omits them, which means secret-rendering failures surface *only at `--apply`, on the live alerting path* — the sharpest edge in the declarations slice. The agent holds rendered content in memory, validates, and discards; the plan response reports digests and sizes, never content.

Plan-time validations the client currently guesses at or cannot do:

- **Size** against the agent-published limit (`Meta.Limits` — one query, no duplicated constant). `rules-critical.yml` has been split five times and `syrvis-overview.json` is permanently un-deployable at 127 KB; a published limit at least makes the split a computed decision.
- **`dest` against declared mounts** — *"deploy-stack checks size and renderability, not whether a dest lands where a mount reads; a mismatch deploys green and serves nothing."* Nothing validates this today.
- **Mode vs container uid** — the 0600-root file that crash-loops a `USER nobody` image, and `_place_config`'s one-way 0600→0644 refusal. 25 lines of prose in `alertmanager`'s block stand in for this check.
- **Prune set** — the declared config set converges. Seven retired Grafana boards serve from disk today because *"a bundle only WRITES,"* and the remedy in the KILL LIST is an owner hand-delete. Prune is reported in the plan and gated by `prune: true`.
- **Graph validity** — cycles, unknown targets, `healthy` onto a checkless target; invalidating only the declaring file (per-file isolation preserved).

### 3.4 Execution: waves, gates, velocity

The apply is a **server-side job with an id**. This is the direct fix for 2026-08-14: the operator wrapped `deploy-stack --apply` in `timeout 500`, the client died mid-`compose up`, `nas-heartbeat` and `alertmanager-watchdog` were left stopped, `restart: unless-stopped` was structurally unable to recover them, and monitoring went dark ~1.5 h. In v2, **client disconnect is not abort.** A client may request `Job.Cancel`, which is journaled and unwinds to a consistent set; it may not kill the orchestrator. The `HOMEBASE_DEPLOY_TIMEOUT_S` constant and trap T3 both dissolve.

Waves: topological, band tie-broken, `max_parallel` bounded. Health gating per readiness kind — and `healthy` means an observed healthcheck transition, so the journal can finally record `healthy` rather than only `started`.

Velocity: the breaker **engine**, not just the store. One row per `{plane, context}`, the capped jittered curve (base×2ⁿ, 10-min cap, full jitter, open at 3, half-open probe at the cap), cross-plane suppression, "a close closes all," `by` as a field so `hostd`/`s99`/`cron` inherit and only `cli:`/`seam:`/`mcp:` close. The open transition pages exactly once. `--force` closes breakers in scope and is journaled with the actor.

Rollout policy per apply: `on_failure ∈ {halt, continue-independent, rollback-service, rollback-stack}`. Default `halt` for instance targets; **`continue-independent` for stack targets** — the 2026-08-14 casualties were unrelated subtrees, and an independent subtree should not die of someone else's failure. `canary: [names]` runs a named subset first and gates the rest on it.

### 3.5 Rollback

Revisions store content digests plus blobs in a content-addressed store, so `Deploy.Rollback(target, to)` re-applies through the same wave machinery without the client repo. `rollback_of` is recorded. Retention never deletes a revision that is a live service's current rollback target — v1 silently expires targets at 50. The response names the git-side follow-up, because rollback remains GitOps-ephemeral and the repo must be reverted too.

### 3.6 The client round-trip

`Deploy.Plan` → operator reads a typed diff → `Deploy.Apply` → `Deploy.Watch(job)` streams events → terminal `JobResult` carries, per service: outcome, revision, previous → new image, config/secret digests, prune list, wave, elapsed, breaker state, and an **annotation-ready record**. That collapses `deploy` → `history --json` → print into one round trip and gives the disarmed Grafana annotation lane a credential-free path (the agent emits, no laptop Bearer token, no CF-Access service token).

---

## 4. Rich status and events

### 4.1 `/status` — the axes

One read answers every rung of `degraded-operations`' ladder 0–8 and every gate of `post-boot-verification` G0–G4.

```json
{"schema_version":2,"observed_at":"2026-08-18T04:12:03Z","agent":{"version":"2.0.3","build":"…","mode":"normal|rescue","uptime_s":91233},
 "device":{"phase":"in-service","phase_since":"…","intent":"drained","intent_source":"drn-20260816T0507-mdresync"},
 "host":{"roots":[{"path":"/volume4/syrviscore","declared":true,"kind":"home"},
                  {"path":"/volume6/syrviscore_1","declared":false,"kind":"collision-sibling"}],
         "current_symlink":{"target":"versions/0.5.17","resolves":true},
         "array":{"state":"resync","pct":41.2,"device":"md3"},
         "volumes":[{"path":"/volume5","mounted":true,"free_pct":18}],
         "boot_hook":{"present":true,"current":true,"contract":7},
         "seam_accounts":[{"user":"syrvis-operator","shell":"/bin/sh","groups":["docker"]}],
         "answered":true},
 "drains":[{"id":"drn-…","scope":{"kind":"volume","selector":"/volume5"},"state":"drained",
            "members":11,"held_revisions":3,"until":"2026-08-24T00:00:00Z","expired":false}],
 "planes":{"monitoring":{"floor_intact":true,"blind_windows":[]},
           "container":{"dockerd":"responsive","last_probe_ms":38}},
 "services":[ /* §4.2 */ ],
 "breakers":[{"plane":"deploy","context":"stack:onyx","open":false,"failures":1,"next_probe_at":null}],
 "jobs":{"installed":25,"block_in_sync":true,"pin":"a1b2…","last_sync":"…"},
 "blind":[{"fact":"vm.power","unreachable_reason":"synowebapi timeout","since":"…"}]}
```

### 4.2 "Why is X not running", in one read

Every service row carries the full explanation tuple shown in §1.3's lattice order, with `reason.code` drawn from a closed enum: `running | starting | drained | disabled | blocked | breaker-open | terminal | crash-loop | image-missing | secret-empty | volume-unmounted | unmanaged | unknown`.

```json
{"name":"onyx-background","stack":"onyx","plane":"apps","home":"/volume5/…/onyx-background",
 "declared":{"enabled":true,"file":"services.d/onyx-background.yaml","spec_digest":"sha256:9f…"},
 "effective_intent":"should_stop",
 "reason":{"code":"drained","drain_id":"drn-20260816T0507-mdresync",
           "scope":{"kind":"volume","selector":"/volume5"},"since":"…","until":"…"},
 "observed":{"state":"exited","exit_code":0,"health":"none","restart_count":7,
             "started_at":"…","spec_digest_running":"sha256:1c…","drift":"config-changed",
             "flapping":false,"answered":true},
 "pending":{"revision":48,"applied":true,"started":false,"held_by":"drn-20260816T0507-mdresync"},
 "blocked_by":[],"depends_on":[{"name":"onyx-relational-db","readiness":"healthy","satisfied":false}],
 "breaker":{"open":false,"failures":0},
 "secrets":{"env_file":"secrets.env","bytes":0,"keys":0,"warning":"empty"}}
```

Three v1 blind spots close here by construction. `restart_count` is first-class — today `syrvis status` reads the raw Docker string, a crash-looping `restart: unless-stopped` container reads `running` (trap T13), and `scripts/accept` reaches around to the dashboard's `/api/services`, which then 401'd when the dashboard went OIDC, forcing a *second* fallback through a VictoriaMetrics PromQL query. `secrets.bytes == 0` is reported directly, retiring the `syrvis-reader` `find … -size 0` probe and the entire `appenv-metrics` job. And **every fact carries `answered` + `unreachable_reason` + `observed_at`** — the typed replacement for `verify-all`'s two regexes over summary *prose*, its `docs.unknown-coverage` meta-check, and INFO-scored-as-zero.

### 4.3 `/events` — streaming, paging, dedup

Append-only, monotonic `seq`, cursor-paged, bounded by count and age, mirrored (last N) to a rootfs flight recorder readable in rescue mode.

```json
{"seq":88214,"ts":"…","kind":"service.state_change","subject":"service:vmagent","job_id":"job-…",
 "severity":"warn","reason":{"code":"crash-loop","detail":"restart_count 3→7 in 300s"},
 "dedup_key":"service:vmagent|crash-loop","first_seen":"…","last_seen":"…","count":4,
 "payload":{"restart_count":7}}
```

Contract:
- **Transitions only.** A steady state emits no events; `count` on the dedup row increments.
- **`dedup_key` is digit-masked** at the protocol level — `monitor-tick`'s `mask_digits` promoted out of the client, so a countdown is not a new problem while 3→30 is.
- **`Watch(from_seq)`** streams; the response header carries `resume_from` and, if the cursor fell off the ring, **`gap: true`**. A consumer must be able to learn that it missed something — v1 has no such signal anywhere.
- **Severity is advisory; drained is never `warn`.** *"Shed is not unhealthy — an amber that never clears is an amber nobody reads."*
- **Untrusted payloads are labelled.** Container log lines, image names, alert bodies carry `trust: "untrusted"` at the protocol level; the fencing that exists only in the unbuilt `scripts/seam` becomes a field.

### 4.4 Reads survive the failure they diagnose

Two incidents ended with the diagnostic plane co-fated. On 2026-08-14 every seam verb — including read-only ones — hung at 120 s because the CLI built a Docker client at startup regardless of verb. On 2026-08-16 the rename took the wrapper, the healer, the boot hook and all 24 jobs together.

So: the status/events path is served from the agent's in-memory model and the rootfs mirror, with **hard per-verb deadlines** and a Docker probe that is a *bounded, cached, partial* collection (the 2026-08-10 exporter-blinding lesson: one sick container must never blind the sensor for the healthy ones). Docker unreachable yields `answered: false, unreachable_reason: "docker: deadline"` on the container facts and full answers on everything else. `denied`, `hung` and `degraded` are distinguishable return conditions, never one timeout.

---

## 5. API sketch

Transport: length-framed JSON over a `root:operator` 0660 UNIX socket. The seam is `syrvisd seam-proxy` as the forced command — **no argv on the wire at all**, generalizing what `apply`/`deploy`/`secret set` already do. That retires the shim charset allowlist (which deformed `schedule apply` into taking no cron argv because `*` and `,` cannot pass), the 18 slot kinds, the sudoers globs looser than the shim, the sudo/no-sudo argv split (which silently degraded `vm list` to a permanent UNKNOWN in `verify-all:4336`), and the three duplicated `/etc/passwd` heals — a forced command that is the agent's own subcommand needs no login shell, so `jobs/seam-selfheal` rewriting `/etc/passwd` every five minutes stops existing.

Every method declares a **capability class**: `read | converge | destructive | rescue`. The class is queryable (`Meta.Methods`), so the allowlist, the deny floor, the MCP tool set, the CLI tree and the permission drift check are all *generated* from one table rather than hand-mirrored across `config/agent-permissions.yaml`, `registry.py`, and three skills.

| method | class | request → response |
|---|---|---|
| `Status.Get{sections[]}` | read | → §4.1 |
| `Events.Watch{from_seq, kinds[]}` | read | → stream of §4.3 |
| `Meta.Methods{}` / `Meta.Limits{}` | read | → method table w/ classes; `{config_max_bytes, secret_max_bytes, per_verb_deadline_s}` |
| `Census.Get{}` | read | roots + collision siblings under every configured apps-root segment, `current` resolution, seam shells, `/proc/mdstat`, `df`, meminfo — R4's whole break-glass block as one typed response, answerable in rescue mode |
| `Fs.Stat{paths[]}` / `Fs.FindEmptySecrets{}` / `Fs.Digest{paths[]}` | read | scoped to declared roots; retires the `syrvis-reader` identity |
| `Declarations.Get{}` / `.Validate{bundle}` | read | effective desired state *after all overlays* — one read, so no client recomputes it |
| `Deploy.Plan{bundle}` | read | per-service change class, prune set, wave order, validation findings |
| `Deploy.Apply{bundle, policy, plan_digest}` | converge | → `{job_id}` |
| `Deploy.Watch{job_id}` / `Job.List{}` / `Job.Cancel{job_id}` | read / converge | |
| `Deploy.Revisions{service}` / `Deploy.Rollback{target, to, confirm}` | read / destructive | |
| `Drain.Declare{scope, class, reason, until, policy, confirm?}` | converge | → `{drain_id, plan}`; device scope requires confirm |
| `Drain.Amend{id, until, policy}` / `Drain.Release{id}` / `Drain.List{}` | converge / read | |
| `Service.Converge{name, force_recreate}` | converge | request to reconverge now; writes **no** intent — this is `recreate`/`restart` folded into one, journaled |
| `Config.SetSecret{service, env}` / `Config.SetJobConf{job, content}` | converge | stdin-shaped, secrets never on argv |
| `Schedule.Plan{}` / `Schedule.Sync{to, manifest, confirm}` / `Schedule.Run{job}` / `Schedule.Logs{job, n}` / `Dsm.Tasks{}` | read / destructive / converge / read / read | `Sync` is destructive *on every channel*, closing the asymmetry where MCP token-gates it and the seam spelling — the one the documentation teaches — does not |
| `Vm.List{}` / `Vm.Power{name, action, confirm}` | read / converge | |
| `Attest.Accept{since_job}` | read | restart deltas, zero-byte secrets, floor coverage, held-revision set, one-cycle hold — `scripts/accept` as a device verb |
| `Version.Activate{v, confirm}` / `Backup.*` | destructive | |

**Imperative residue.** `start` and `stop` are gone as verbs; `stop` is `Drain.Declare(scope=service)`. What remains genuinely imperative is `Service.Converge`, `Schedule.Run`, `Vm.Power`, `Job.Cancel`, and the version/backup admin set. Each is journaled with `by`.

**Confirmation.** `Confirm{method, args}` → `{token, plan_digest, expires_at}`; the token is bound to the plan digest, so any state change between mint and use voids it. The check lives **in the agent**, so MCP, CLI, dashboard and the seam all inherit it. `shutdown`/`resume`/`drain` at `class: power` stay deliberately token-free so an unattended NUT hook or a degradation response needs no human. Overrides remain distinct methods with distinct audit lines — *"two overrides in one call is two decisions in one."*

**Refusals are structured**, and they keep naming names: `{"error":"guard","guard":"bulk_degraded","blocking":["onyx-background","vector"],"override":"force","evidence_required":true,"recovery":"Drain.List then release drn-… or pass force"}`. Clients render it; nobody paraphrases it.

---

## 6. Migration notes

| consumer | today | v2 |
|---|---|---|
| `scripts/deploy-stack` | client bundle build, ordering, cap, serial loop, `history` follow-up | `Deploy.Plan` → `Deploy.Apply` → `Watch`. Deletes `ALERTING_PAIR_LAST`, `SEAM_CONFIG_MAX_BYTES`, `_SECRET_MAX_BYTES` copy, `HOMEBASE_DEPLOY_TIMEOUT_S`, the staged-service filter |
| `scripts/apply-instance` | `export --json` overlay + `apply` + `stack apply` + `reconcile` | `Deploy.Apply{target: instance}`. The `live_disabled()` overlay is deleted outright — drains are not in the declaration set |
| `scripts/apply-jobs` | `config set` per job | `Config.SetJobConf`; conf presence appears in `Status` (retiring `config/job-confs.yaml`'s entire indirect metric plane) |
| `scripts/apply-immich-secrets` | `secret set` ×3 | folded into the stack bundle; the script retires (already "superseded but live") |
| `scripts/recreate-located` | list → filter shed → name-suffix order → serial recreate | `Drain.Release` or `Deploy.Apply`; the whole script retires |
| `scripts/accept` | dashboard HTTPS + reader `find` + verify-all slices + timed hold, exit 3 = BLIND | `Attest.Accept` + a much thinner local gate; the `syrvis-reader` identity and the dashboard detour retire |
| `scripts/verify-all` | 4 memoized seam reads → 10 checks; regex UNKNOWN classifier | one `Status.Get{sections}` per sweep, atomic and single-timestamped; `answered:false` replaces `_is_unknown()` and `docs.unknown-coverage` |
| `scripts/monitor-tick` | `seam_blackout()` synthesis, two-port TCP pre-flight, client dedup | `Events.Watch` with protocol dedup; the blackout synthesis stays (it must survive the agent's death) |
| `scripts/maintenance-mode` | writes `config/maintenance-state.yaml`, device never reads it | `Drain.Declare`; the YAML becomes a **generated mirror** of `Drain.List`, checked for drift, never authored |
| the 28 cron jobs | regex-parse `services.d`, grep `intent.json`, 13× copied `vm_push` | local socket `Status.Get` / `Drain.List` (works when `current` dangles — that is the whole reason they refuse the CLI today) + `Telemetry.Push` on the agent, retiring the `docker exec victoria-metrics` transport |
| `jobs.d/hourly-reconcile` | declaration with no script, disabled | becomes real: the agent's own level-triggered loop, drain-aware by construction. An hourly reconcile stops being an hourly resurrect |
| MCP | 50 hand-written tools over 77 registry rows | generated from `Meta.Methods` |
| dashboard | container bundling its own library copy | backend folds into the agent; the image-lockstep failure class (two silent regressions in three releases) disappears |
| runbooks | prose interlocks, five-wave tables | preconditions become agent refusals that name themselves; `forbidden_verbs[]` becomes enforced rather than reported-by-three-consumers-and-enforced-by-none |

### Transition invariants

1. **The Python side never writes the new state store.** It calls the agent. This is the only thing holding the dual-writer window closed.
2. **No verb migrates without its incident tests ported.** The ~2,000 gated tests are the design record; losing them is the only irreversible cost.
3. **The rescue path ships before the agent owns anything.** Rootfs-resident, zero volume dependency, answering `Census.Get` and `Status.Get` with no resolvable home. The platform's worst outage came from trusting a volume path.
4. **Intent is imported once, then owned.** `intent.json`'s shed rows lower into service-scoped drains at first start; `config/maintenance-state.yaml` inverts from source to generated mirror on the same day, not later.
5. **Every drain must lower to v1 shed rows during the window**, and **platform rollback below the agent is forbidden while any non-service-scoped drain is active** — the same reader-enumeration gate that guards `depends_on`, applied to scopes.
6. **Unknown schema ⇒ report unknown and refuse to act**, never treat as absent. The journal already rules this; it becomes the whole protocol's rule.
7. **CI builds the SPK and the binary from day one.** The artifact DSM actually installs has never been gated, and `rootfs/boot-integrity` — a check that does not share fate with the thing it checks — stays hand-installed on the rootfs, outside the managed block, permanently. Whatever v2 self-heals, something outside v2 must still assert that v2 is present, resolvable and scheduled.