# The Deployment Plane — bundles, revisions, journal, breakers, convergence, drift

## Purpose & role in the system

The deployment plane is the part of SyrvisCore that turns *declared intent* into *running containers*, and records what it did. It has four layers that a retrospective should keep distinct:

1. **Transport** — two stdin-only bundle schemas (`syrvis-instance/v1` for the core tier + whole declaration set, `syrvis-bundle/v1` for one L2 service) that let the deployment repo (home-tech) push resolved state over the operator seam without ever writing a file under `$SYRVIS_HOME` itself.
2. **Materialization** — `ServiceManager`, the 3.6 k-line choke point where a validated `ServiceDefinition` becomes a manifest, a compose file, a Traefik dynamic config, bind-mount dirs, an env_file, and a `docker compose up -d`.
3. **Convergence** — the `services.d` reconcile engine (plan/apply split, topological ordering, per-service failure isolation), reachable via `syrvis reconcile`, `stack apply --from`, boot, and resume.
4. **Memory** — three append/replace state surfaces: per-workload immutable deployment records (`data/deployments/`), the per-run deploy journal (`data/state/deploy-journal.json`), and the durable circuit-breaker store (`data/state/breakers.json`).

Everything above is library code; the CLI, MCP server and dashboard are adapters. Current version: `0.5.17` (`packages/syrviscore/src/syrviscore/__version__.py`).

## Key modules and files (path — role — approx size)

All under `/Users/kevinteg/code/SyrvisCore/`:

| Path | Role | Lines |
|---|---|---|
| `packages/syrviscore/src/syrviscore/service_manager.py` | the materialization choke point: install/replace/remove/start/stop/recreate/update/set-image/rollback/run_task/write_secret/write_config/deploy_bundle | 3651 |
| `packages/syrviscore/src/syrviscore/services_d.py` | the ONE reconcile engine: load, dependency graph, plan, apply, verdict | 1096 |
| `packages/syrviscore/src/syrviscore/deployments.py` | revision records: write/read/redact/trim/rollback-target resolution | 604 |
| `packages/syrviscore/src/syrviscore/instance_bundle.py` | `syrvis-instance/v1` parse + `apply_instance_bundle` + `export_instance` | 661 |
| `packages/syrviscore/src/syrviscore/bundle.py` | `syrvis-bundle/v1` parse/validation only (no I/O) | 234 |
| `packages/syrviscore/src/syrviscore/breakers.py` | the durable breaker store + curve + suppression + reset | 572 |
| `packages/syrviscore/src/syrviscore/deploy_journal.py` | the deploy journal store + the single `journal_status()` verdict | 480 |
| `packages/syrviscore/src/syrviscore/converge.py` | desired-state doc → projection onto services.d → the same engine | 387 |
| `packages/syrviscore/src/syrviscore/drift.py` | pure compose-vs-docker diff (separate from reconcile) | 257 |
| `packages/syrviscore/src/syrviscore/guards.py` | `guard_enable_change`, `guard_bulk_degraded`, `close_breakers_in_scope` | 277 |
| `packages/syrviscore/src/syrviscore/intent.py` | `intent.json` shed/device intent — the overlay outside the declaration set | 376 |
| `docs/deployments.md`, `docs/seam-contract.md` | the normative prose | 359 / ~230 |

Tests: `tests/test_deploy_bundle.py` (972), `test_services_d.py` (835), `test_depends_on.py` (527), `test_instance_apply.py` (520), `test_deployments.py` (482), `test_breakers.py` (286), `test_deploy_journal.py` (243), `test_converge.py` (253).

## How it actually works

### `syrvis apply` — the instance bundle, end to end

`cli.py:read_json_stdin` reads stdin with a hard **1 MiB byte cap** (`len(raw.encode(...)) > max_bytes`, deliberately bytes not chars), parses JSON, then `InstanceBundle.from_dict` validates strictly: unknown top-level keys rejected; `apiVersion` must equal `syrvis-instance/v1`; at least one of `env`/`stack`/`declarations`.

- **env**: keys must match `^[A-Z][A-Z0-9_]*$`, then pass `paths.env_key_hazard` (rejects `PATH`, `LD_PRELOAD`, `PYTHONPATH` — they would poison `os.environ` via the CLI's `load_dotenv(override=True)`) and `paths.env_value_hazard` (rejects `$ \` \ ; & | < > ( )` because the root boot hook *sources* `.env` as bash). Values are `.strip()`ed once — because both `parse_env_file` and python-dotenv strip, an unstripped value could never compare equal on re-apply and would report as "changed" forever. `DOMAIN` is mandatory. 64 KiB section cap.
- **stack**: only `stack_mod.ALL_SERVICES`; primordial services cannot be disabled.
- **declarations**: each manifest runs the **full strict `ServiceDefinition.from_dict` trust boundary** (never the tolerant reader), 64 KiB each, and `service.name` must equal the mapping key.

`apply_instance_bundle` is a plan-then-write split. Guards live in the plan:
- `_plan_env` refuses a `SYRVIS_HOME` that doesn't match the real install; refuses to change/drop an *existing non-empty* secret (`is_secret_key`) without `--allow-secret-change` — **except on `--dry-run`**, deliberately, so a rotation can be previewed over the seam.
- `_plan_declarations` implements the two intent protections born from the 2026-08-16 incident: (1) **shed overlay** — any name in `intent.json`'s shed set is written `enabled: false` regardless of the bundle, reported as `shed_pinned`; (2) **enable guard** — every *other* off→on flip is collected by `guards.enable_changes` and refused by `guard_enable_change` unless `--allow-enable-change`, with the override journaled to `logs/overrides.log`. `_live_enabled` reads the raw YAML rather than `ServiceDefinition`, on purpose: "a guard that fails OPEN on an unreadable file would be no guard at all."

Writes are ordered env → stack → declarations, each individually atomic (`_atomic_write`: `mkstemp` in the target dir → chmod → optional chown → `os.replace`). `.env` is 0600 with the previous owner preserved; declarations are 0600 when they carry inline `environment`, else 0644. Declarations are a **replace set** — anything on disk not in the bundle is unlinked, and a failed unlink raises rather than reporting false convergence. `export_instance` is the inverse, redacting secret values to `****` and **failing loud** on an unparseable declaration ("a snapshot that silently omits a declaration would, on a later restore, DELETE that service's intent").

### `syrvis deploy` — the service bundle, end to end

`DeployBundle.from_dict` validates: dests relative with no `..`, no duplicate dests, dest ≠ the declared `env_file` ("env secrets go in the 'secrets' section"), 64 KiB per config; secret keys must `ENV_KEY_RE.fullmatch` (fullmatch not match, because `$` matches before a trailing newline), values must be strings with no `\n`/`\r` (line injection into the env_file), and secrets without a declared `env_file` are rejected. **Values are never echoed in an error.** The CLI then rejects a bundle whose `service.name` differs from the argv target, and runs `guards.guard_bulk_degraded("deploy", …)` which reads `/proc/mdstat` and refuses while any array is resync/recovery/reshape/check/repair unless `--force`.

`ServiceManager.deploy_bundle` (`service_manager.py:3169`) then:
1. Re-validates the name as a path component; reads the shed flag.
2. Stamps `source_url = "deploy:<name>"` — explicitly demoted to a **provenance label**, not an authority: the old design/22 gate "tested the very string `deploy_bundle` had just written — a tautology that could not fail." The real gate is `_infra_privilege_refusal`, a root-held name→image-repo list (`DEFAULT_INFRA_SERVICES` in code, overridable by root-owned `<home>/config/infra-allow`), run on *both* the fresh and update fork.
3. Fires `pre-deploy` (the one abortable hook).
4. **Computes the change set BEFORE any write** (`_bundle_change_set`) — "the writes below are what would destroy the evidence."
5. Writes the journal `starting` event.
6. Fresh → `install_declaration(start=False, record=False, fire_hooks=False)`; update → location/volume-location immutability refusals, `write_declaration_from_install` (orchestration-preserving), manifest rewrite, Traefik rewrite, app-home repair.
7. Places configs (0644, or 0600 when `secret: true`, with a **downgrade guard**: never overwrite an existing 0600 file with a 0644 write), then the env_file via `write_secret` (0600, `O_EXCL` temp → fsync → `os.replace` → chmod).
8. Compose regen, then **start LAST** — or, if shed, deliberately *not* started ("staging fixed bits on a service that must stay down is exactly what an operator wants mid-degradation").
9. The post-`up` fix-up, which is the design/60 G1 rule.

### The byte-identical-redeploy rule (design/60 G1)

`docker compose up -d` compares the compose **spec**, not the content behind it. The fix-up therefore differs by artifact:

- changed **secrets** → `_recreate_containers` (`up -d --force-recreate`, 300 s) — because Docker bakes env at container CREATE time;
- changed **configs** only → `compose restart` (90 s) — a bind-mounted file is re-read on process restart;
- **neither changed** → nothing.

`_bundle_change_set` compares the bundle's own bytes against `deployments.last_materialized_digests`, which returns the **newest** record (not newest-successful, and that is argued explicitly: a failed update also tears the container down, so the retry's `up -d` recreates from the files on disk). It is **FAIL-CHANGED**: no history, a pre-0.5.16 record, a `remove` record, an unreadable record, or a dest the record never digested all read as *changed*. Only the bundle's own dests are compared, so `config_templates` rendered outside the bundle don't make every templated service permanently changed. `_bundle_secret_body` is the single encoding shared by the writer and the digester — "if the two ever diverge, every deploy looks unchanged (a rotated secret silently not taking effect, which is not [harmless])."

### Revision records

`data/deployments/<workload>/NNNN.json`, schema `syrvis-deployment/v1`. Numbering is claimed atomically: content goes into an `O_EXCL` temp, then `os.link(tmp, "{:04d}.json")` claims the number; `FileExistsError` → increment and retry, up to 1000 times. Perms: 0644, or **0640 + the services.d shared gid** whenever `env_names` *or* `secrets_checksum` is present — "a digest of a SHORT low-entropy secret is a confirmation oracle." Retention: newest **50** per workload; revision numbers keep climbing past it, so a trimmed revision is no longer a rollback target. `load_history` masks every inline env value; only rollback reads raw. Corrupt files are isolated into an `invalid[]` list. Instance-level core applies use the reserved workload id `@core` (the `@` can never collide with a service name).

`resolve_rollback_target` requires action ∈ `{deploy, rollback}`, `outcome == "success"`, and a dict `manifest`. `ServiceManager.rollback` re-validates the stored manifest through the full trust boundary, restores `source_url` from the record, refuses git-sourced services, refuses a location change over non-empty data, refuses a hostname now owned by another service, pulls the target image **first**, then swaps manifest+compose+Traefik, dual-writes CONTENT only (the operator's `enabled`/`critical` survive), restarts, and records a new revision with `rollback_of`. Failed rollouts are recorded too, because "a rollback target picker needs to see the failed rollout."

### The deploy journal

`data/state/deploy-journal.json`, `schema_version: 1`. Four normative clauses, implemented once in `journal_status()`:

1. canonical absolute path from `SYRVIS_HOME` (beside `runstate.json`, `intent.json`, `breakers.json`);
2. an unrecognised or unparseable file is **`unknown` + `act: False`**, never `absent` — "absent means 'no run'; unparseable means 'I cannot tell', and those are different verdicts";
3. terminal set `{started, healthy, skipped, failed}` (failed IS terminal); non-terminal `{pending, stopping, starting}`; in-flight iff any row non-terminal AND no `finished_at`;
4. staleness at **3600 s** or a dead pid (`os.kill(pid,0)`; `PermissionError` counts as alive) → **annotate, never refuse**; only a *fresh* in-flight journal whose set `intersects()` the caller's refuses.

Writes are `O_EXCL` temp → fsync → `os.replace` → 0644, with `events` trimmed to the newest **500**. `record_event` writes *before* each transition, opens a run implicitly if none is open, and closes the run (`finished_at`) as soon as every row is terminal. Every deploy records `started` (or `skipped` when shed) and never `healthy` — "at 0.5.16 there is no health gating at all, so every success records `started`: nothing was verified." The per-row `breaker:` block is a **mirror** rendered from `breakers.json`.

### The breaker store

`data/state/breakers.json`, one row per `{plane, context}` with `plane ∈ {deploy, recovery, agent}`, contexts `deploy-svc:<name>` / `node:<name>`. Constants: threshold 3, base 30 s, cap **600 s**, ±20 % jitter, `MAX_ROWS` 512 (closed rows dropped first — "a closed breaker is the absence of news"). `backoff_seconds` bounds the exponent at 20 steps before shifting ("2\*\*(large int) is a memory bomb"). `record_failure` returns `(row, opened_transition)` where the boolean is true only on the *transition* into open — "the open transition pages exactly once." `should_attempt` implements the once-ruled half-open (`opc:F5`): before `next_probe_at` → skip; the first attempt after it *is* the probe. `suppressed_by` gives cross-plane suppression; `close_service`/`close_scope` give "a close closes all". `closes_breakers(by)` fails safe — only `cli:`/`seam:`/`mcp:` close; `hostd`/`s99`/`cron`/absent inherit. `read_breakers` degrades to `[]` on anything unreadable; `write_breakers` **refuses** to overwrite a file whose `schema_version` it doesn't recognise (the rollback door).

### Reconcile ordering, TERMINAL, blocked

`build_reconcile_plan` emits actions `add | replace | start | stop | blocked | prune_{stop,remove,purge}` and four **non-action buckets**: `disabled` (git says off), `shed` (intent.json says off), `terminal` (a `restart: no` service whose status ∈ `{exited, dead}`), `blocked` (a hard `depends_on` edge onto a disabled/shed/invalid target). None is drift. A **floor check** refuses when `prune is None and installed and not declarations` — "an empty config is never a reason to act on a populated instance."

Ordering is one sort with a four-tuple key (`_action_order_key`): stop kinds `(0, -depth, band, name)`, bring-up `(1, depth, -band, name)`, report rows `(2,0,0,name)`. Depth is the dependency graph's longest path; the shutdown band (`shutdown.priority`, default 50) is the **tie-breaker inside a wave**, so an instance with no edges sorts byte-identically to the pre-graph interim. Where edge and band disagree the edge wins. `_find_cycles` is iterative DFS ("the declaration set is attacker-adjacent input and a stack overflow is not an error message").

`apply_reconcile_plan` runs `lifecycle.guard_not_halted` first, isolates every action in its own try, handles `blocked` **first** so it can never fall through into add/start, and implements REPLACE as `remove(keep_declaration=True, fire_hooks=False)` + `install_declaration(preserve_data_on_rollback=True)` so history shows ONE deploy with `previous_image → image`.

## Design decisions & their rationale

- **One choke point for materialization.** `deployments.py:9-16`: "All materializations funnel through `ServiceManager._install_from_definition`; a reconcile REPLACE records ONE deploy, never delete+create."
- **Best-effort memory.** History, journal, breaker writes all catch `Exception` and return `None`/`False`. "A history-write failure must NEVER fail a deployment."
- **Fail-changed over fail-unchanged** (G1). "the worst outcome of a gap is today's behavior (an unnecessary recreate), never a rotated secret that silently never reached the container."
- **`by` is a field, not a doctrine** (`opc:F2`) — "intent cannot be inferred from the verb, because hostd, the S99 fallback and `restore` all fire the same `syrvis up`."
- **Shed lives outside the declaration set** (`intent.py:12-19`) — the 2026-08-16 resurrection of fourteen services happened because "the flag that says 'this service is deliberately down' lived in exactly the file the next apply overwrites."
- **`blocked` is a bucket, not a failure** (design/63 D2 as amended, `opc:F10`) — "a deliberate 14-service load-shed must not fail every hourly reconcile for every dependant."
- **`restart: no` + exited is TERMINAL** (`services_d.py:706-729`) — "Three mechanisms, one field, cancelling out."
- **`recreate` vs `restart`** (`service_manager.py:2349`) — `stop` is an intent verb that writes `enabled: false`, so using stop+start to re-bake env "is an operational trap hiding inside a repair procedure."
- **Flapping inferred from one sample** (`is_flapping`) — "an engine that has to remember a previous observation to notice a crash loop cannot notice one across a restart of itself."
- **`installed` gate on env_file scaffolding** — the unconditional `touch()` "re-created FOUR empty secrets.env files… three Postgres containers came one entrypoint check away from initdb'ing empty clusters over the real databases."
- **Deliberately not blue/green** — `deploy_bundle`'s docstring names the asymmetric blast radius: a failed UPDATE takes the service down and leaves the NEW manifest.

## Invariants & contracts

- Deployment records are immutable, monotonically numbered, and never carry a secret VALUE or an env_file body — only `env_names`, `config_checksums`, `secrets_checksum`.
- `journal_status()` is the single verdict function; no consumer re-derives staleness. `breakers.json` is the only place a count lives; every other breaker display is a mirror.
- `services.d/` is the declared set; the directory IS the intent. `apply` replaces it; containers are only removed by `reconcile --prune`.
- `_get_service_status` returns Docker's raw string (`running`/`exited`/`created`/`paused`/…), `"stopped"` for NotFound, `"unknown"` for no daemon — "every caller and the whole seam contract depend on it."
- Secrets transit stdin only, never argv/env/logs (`seam-contract.md` §"Stdin writers … never MCP tools").
- Plan functions are pure and side-effect free; dry-run is safe by construction.
- `tier: infra` is granted by a root-held list bound to an image repository, checked at *every* admission point (install, deploy_bundle, set_image).

## Gaps, debt & sharp edges

- **The journal and breakers are write-only.** Grep confirms zero in-tree callers of `journal_status`, `intersects`, `should_attempt`, `suppressed_by`, or `breakers.summary` outside `guards.close_breakers_in_scope`, the journal's own mirror, and tests. `status --json`, `doctor`, hostd and the dashboard consume none of it. The doctrine is fully written and half unwired (M2).
- **Journal leak on early-return refusals.** `record_event(STARTING)` fires at `service_manager.py:3256`, but the update branch has ≥6 `return False` paths after it (location refusal, `LOCATION_RE`, unmounted volume, declaration write failure, Traefik failure, slot containment) and the fresh branch returns on `install_declaration` failure — none writes a terminal event. Each leaves a permanently in-flight journal that goes stale after 60 minutes and then annotates every later bring-up.
- **The journal is effectively single-service.** `begin_run` is never called with more than one name by shipping code; `record_event` replaces the whole file per deploy, so a multi-service `deploy-stack` loop leaves only the last service's run. And because the run auto-closes once every row is terminal, the "fresh in-flight intersects → refuse" path is reachable only if a deploy is killed between the two events.
- **`state: "half-open"` is never written.** It is defined, normalized and honored by `suppressed_by`, but half-openness is derived by `should_attempt`, contradicting the module's own "named states … carried as DATA. A reader never infers a breaker from a count."
- **Breaker read/write asymmetry is silent.** Unknown `schema_version` → `read_breakers` returns `[]` and `write_breakers` raises; `_record_deploy_breaker` swallows the raise. Counting silently stops with no operator signal.
- **No locking anywhere in the plane.** `breakers._put`, the journal, the declaration set and the compose/manifest tree are all unguarded read-modify-write. Two concurrent deploys of the same service, or a cron reconcile racing a seam deploy, lose counts and interleave writes. The only atomicity is per-file (`os.replace` / `os.link`).
- **The enable guard exists on only one of two declaration writers.** `instance_bundle._plan_declarations` enforces `guard_enable_change`; `converge.apply_plan`'s `declare`/`declare_update` path applies only the shed overlay. `stack apply --from` can therefore flip declared-off → declared-on unguarded.
- **Replace-set removal has no guard.** An accidental omission from a bundle silently deletes a declaration; the running container becomes `unmanaged` and is never stopped or reported as a failure.
- **`apply_instance_bundle` is not transactional across sections.** A crash after the env write leaves a partial declaration set; recovery is documented as "re-run apply."
- **Two independent drift notions.** `drift.py` diffs the compose file against Docker; the reconcile planner diffs declarations against installed manifests. They can disagree, and `drift.py` knows nothing about flapping or shed (shed filtering is bolted on in `verify.py:108`). `_normalize_image`'s final `if "/" not in …: return ref` branch returns `ref` on both paths — dead code.
- **Planner cost.** `build_reconcile_plan` makes two Docker SDK round-trips per declared service (`_get_service_status` + `is_service_flapping`, the latter re-fetching `attrs`), unbatched, on every hourly reconcile of a 39-service fleet.
- **`update()` (git path) never records a failed rollout** and does not consult the breaker or journal at all — it is the one deploy verb outside the 0.5.16 instrumentation.
- **Retention interacts with rollback silently.** At 50 records, a rollback target simply disappears with no warning; `history --limit` won't reveal it either.
- **`guard_bulk_degraded` hard-codes `by="cli:force"`**, so a seam- or MCP-driven `--force` still reads as CLI operator intent and closes breakers — the very inference `opc:F2` was written to prevent.

## Raw material worth citing in the retrospective

- design/60 **G1**: "a no-op apply of an unmodified stack restarts zero containers" — and the mechanism that broke it: "every push of an unmodified stack force-recreated every secrets-bearing service — the platform violating the caller's no-op contract from the inside."
- "**a digest of a SHORT low-entropy secret is a confirmation oracle**" — why `secrets_checksum` records go 0640.
- "*Absent* means 'no run'; *unparseable* means 'I cannot tell'. Different verdicts." (D6 clause 2)
- "A stale journal that can hold unattended bring-up hostage forever is the failure mode this clause exists to forbid." (60 min, `STALE_AFTER_S = 3600`)
- "a per-plane store gave one sick service two independent open breakers, each counting the same failures, each half-open probing, each paging once for the same fact, and each cleared by a *different* verb with neither admitting the other existed."
- "the cap is doctrine (design/60 §11), not a tuning knob: a half-open probe every ten minutes is the slowest anything in this platform is allowed to re-try." (600 s, ±20 %, threshold 3, base 30 s)
- "Three mechanisms, one field, cancelling out." (`restart: no` vs `enabled:` vs reconcile)
- "three Postgres containers came one entrypoint check away from initdb'ing empty clusters over the real databases." (the env_file `installed` gate)
- "the gate below used to test the very string `deploy_bundle` had just written — a tautology that could not fail." (design/66, `tier: infra`)
- Numbers: 1 MiB stdin cap; 64 KiB per config/secret/declaration section; 50 records/workload; 500 journal events; 512 breaker rows; 3651-line `service_manager.py`; 39 services on the live fleet, **15 of 39 with no healthcheck**; 120 s flap window; timeouts 120 s `up`, 300 s force-recreate, 900 s pre-pull, 300 s task, 30 s hook.