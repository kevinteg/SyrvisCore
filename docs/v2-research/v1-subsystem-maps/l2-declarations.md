# Layer-2 Service Declarations — schema, validation, dependency graph, intent overlay

## Purpose & role in the system

`config/services.d/` is the *interface* of SyrvisCore's Layer-2 plane: one validated `syrvis-service.yaml` per file, filename ≡ `name:`. Everything else is an author or a consumer. `docs/service-loading-design.md` §1 states the thesis: "the file collection is the substrate; every explicit interaction is sugar that writes those files… One mechanism, four front doors" (home-tech IaC via rsync, the CLI, the dashboard, the MCP).

The schema is simultaneously the *trust boundary* for third-party repositories. `service_schema.py:7-13`: "A syrvis-service.yaml is attacker-controlled input that ends up as filesystem paths… and as a docker-compose file that root starts. Every field is therefore strictly validated here — names are constrained to a safe charset, host mounts are restricted to the service's own data directory, and unknown keys are rejected outright."

Layered on top of the declaration set are three planes that are deliberately *not* declarations: the durable intent overlay (`data/state/intent.json` — shed), the whole-set dependency graph (`depends_on`), and the refusal guards (`guards.py`).

## Key modules and files (path — role — approx size)

- `packages/syrviscore/src/syrviscore/service_schema.py` — the per-file schema, the trust boundary, `ServiceDefinition`, every sub-validator, `parse_dependency_entry`, `dump_definition` — 1,466 lines / 64 KB
- `packages/syrviscore/src/syrviscore/services_d.py` — loading with per-file isolation, `build_dependency_graph`, `dependency_blocked`, `build_reconcile_plan` / `apply_reconcile_plan`, `verdict`, declaration writers — 1,096 lines / 50 KB
- `packages/syrviscore/src/syrviscore/intent.py` — `intent.json` read/validate/write, shed/unshed/expired/summary, `log_override` — 376 lines / 14 KB
- `packages/syrviscore/src/syrviscore/guards.py` — `guard_enable_change`, `enable_changes`, `parse_mdstat`/`busy_arrays`, `guard_bulk_degraded`, `close_breakers_in_scope` — 276 lines / 11 KB
- `packages/syrviscore/src/syrviscore/shares_registry.py` — `shares.d/` loader + `resolve_fileplane` — 139 lines / 6 KB
- `packages/syrviscore/src/syrviscore/validators.py` — *not* declaration validation; installation/docker/config/network health checks (`check_home_collision`, `check_apps_root` are the declaration-adjacent ones) — 1,647 lines / 63 KB
- Consumers: `service_manager.py` (materialize/compose-gen/`_service_paths`/`_app_home`/`_volume_home`, shed refusals), `instance_bundle.py:_plan_declarations` (the `apply` seam), `converge.py` (`stack apply --from` projection onto services.d), `bundle.py` (`deploy` one service), `syrviscore-dashboard/api/declarations.py` (tolerant read).
- Docs: `docs/service-loading-design.md`, `docs/service-declaration-roadmap.md`, `docs/wiki/05-layer2-services.md`, `docs/wiki/07-service-schema-reference.md`; `examples/syrvis-homeassistant/`.
- Tests: `tests/test_services_d.py` (835), `test_depends_on.py` (527), `test_intent_shed.py` (691), `test_volume_locations.py` (457), `test_service_security.py` (1457), `test_vanished_app_home.py`, `test_fileplane.py`, `test_instance_apply.py`.

## How it actually works

### The schema (allowlist-of-keys)

`ALLOWED_TOP_LEVEL_KEYS` (`service_schema.py:73-146`) is the whole surface: `name, version, image, description, author, homepage, container_name, traefik, environment, command, env_file, volumes, ports, networks, depends_on, config_templates, restart, healthcheck, resources, enabled, critical, tier, location, volume_locations, tasks, hooks, shutdown, dashboard`. Anything else raises `ServiceValidationError` (`code = "service_invalid"`, also a `ValueError` for back-compat). Load-bearing rules:

- **Names**: `NAME_RE = ^[a-z0-9][a-z0-9_-]{0,63}$`, plus `RESERVED_NAMES = {traefik, portainer, cloudflared, proxy, syrvis-macvlan, deployments, state}`. The last two exist because a service's data dir is `data/<name>`, so `deployments`/`state` would overlay platform subtrees and `remove --purge` would rmtree the platform's records.
- **Images**: `_validate_image` requires a tag or `@sha256:<64 hex>`; `:latest` and untagged rejected ("house rule: no floating images").
- **traefik block**: `{enabled, subdomain (single DNS label), domain (≥2 labels, per-service zone override), port 1-65535, middlewares, exposure}`; `exposure ∈ {internal, tunnel}` (`exposure.py`) is *declared intent only* — routing is identical; it drives `stack hostnames`.
- **volumes**: three admitted forms — named volume, relative host path (resolved under the service data root), and `fileplane=<share>[/<sub>]:<mount>:<mode>`. Refused everywhere: `$` interpolation, `..`, absolute host paths, `~`, `docker.sock`, modes ≠ ro/rw. The `infra` tier (`ALLOWED_TIERS = {"", "infra"}`) unlocks exactly `INFRA_HOST_MOUNTS = {/proc, /sys, /, /var/run/docker.sock}`, each forced `:ro`.
- **ports**: `"H:C[/proto]"`, normalized to `host:container/proto`; the non-HTTP escape hatch (Syncthing :22000 was the concrete need — roadmap phase 5).
- **command / tasks**: exec-form argv only, non-empty strings, no `$`. `_validate_command`'s docstring is the rationale: it "runs INSIDE the container under the same confinement… so it grants no authority the image did not already have… in the same benign class as `environment:`". `tasks` ≤ `MAX_TASKS = 16`; `hooks` (≤8) maps `pre-stop|post-start` → a *declared* task name, so hooks select *which audited argv runs when*, never code.
- **shutdown**: `{stop_timeout 5-300, priority 0-100}` — the stop band, consumed by lifecycle and by reconcile's ordering tie-break.
- **dashboard**: explicitly *not* a trust boundary (`service_schema.py:163-175`) — presentation metadata rendered into Grafana JSON, so `$` is uniquely permitted; bounded counts/lengths and no control chars.
- **env_file**: a data-dir-relative path, installed 0600.

`ServiceDefinition.__post_init__` defaults `container_name = name` and always appends `proxy` to `networks`. `dump_definition(service, path, include_orchestration)` is the ONE writer: installed manifests strip `enabled`/`critical`; declarations keep them; a file with inline `environment:` is chmod 0600, else 0644.

### `depends_on` — orchestration edges, never compose

Entry form is a string `name[:readiness]` with `started` (default) / `healthy` / `soft`; `DEPENDS_ON_HARD = {started, healthy}`; `MAX_DEPENDS_ON = 12`. Two halves:

- **Per file** (`_validate_depends_on`): shape, per-edge parse, bound, self-edge, duplicate-target. Entries are stored *verbatim* so the operator's spelling round-trips; `dependency_edges()` is the single reader.
- **Parse order is load-bearing** (`parse_dependency_entry`, `service_schema.py:531-560`): the `:readiness` suffix is split *before* `validate_service_name`, "because `romm-db:healthy` fails the name charset as written — the old dead-code loop behind the reject branch would have refused every readiness edge on its first day." An unknown suffix (`:helthy`) is an ERROR, never a default.

**Whole set** — `services_d.build_dependency_graph(declarations, invalid)` returns `{"edges": {name: [{"on","readiness"}]}, "errors": {name: why}, "depths": {name:int}, "waves": [[name]], "invalid_targets": [...]}`. Four rules:

1. **cycle** → error on every member, message names the ring (`a -> b -> a`). Detected over *all* edges including `soft`, because a soft edge still orders.
2. **unknown target** → error on the dependant: "an edge onto a service that does not exist is a typo, not a deliberately-down dependency."
3. **`healthy` onto a checkless target** → error, never a silent downgrade (`dep:F9`): "2026-08-16 was a lesson in checks that report what they cannot see."
4. **hard edge onto disabled/shed/invalid** → NOT an error; a plan-time `blocked` bucket.

`_find_cycles` is an iterative tri-color DFS (comment: "the declaration set is attacker-adjacent input and a stack overflow is not an error message"). `_dependency_depths` is longest-path with an iteration cap of `len(nodes)+1`; ascending depth is both the topological order and the wave number.

`_apply_graph_validation` short-circuits with one `any(service.depends_on …)` so an edgeless instance pays nothing, then moves graph-invalid names out of `valid` into `invalid` as `{"file": "<name>.yaml", "error": …}` — isolation identical to a syntax error.

### Per-file validation isolation

`load_declarations(home, tolerant=False)` globs `services.d/*.yaml` sorted, and for each: `yaml.safe_load` → must be a mapping → optional tolerant key-drop → `ServiceDefinition.from_dict` → `service.name == path.stem`. Any exception appends `{"file", "error"}` and continues (`services_d.py:113-114`). `tolerant=True` drops unknown top-level keys and is for READ-ONLY consumers only (dashboard `api/declarations.py`, `manager.list`, `cli.py:586`) so an older image-baked reader can display a newer declaration; "The strict default is for the deploy/reconcile path, which must NEVER silently ignore an unaudited key — that rejection is the trust boundary."

### The reconcile plan

`build_reconcile_plan(manager, declarations, invalid, prune=None)` is pure/read-only. It:

1. reads `intent.shed_map(home)`; loads `_installed_manifests` (a service dir with no/unloadable manifest → `None`, so a matching declaration plans a REPLACE rather than an ADD that would refuse forever);
2. solves the graph once and computes `dependency_blocked(declarations, graph, shed)`;
3. **FLOOR CHECK**: `if prune is None and installed and not declarations: raise ReconcileError` — "0 declarations but N installed service(s)… an empty config is never a reason to act on a populated instance." Scoped to the ambient path because `--prune` is an explicit instruction;
4. classifies each declared service: shed-or-disabled → `stop` if alive, else `shed`/`disabled` bucket; then the vanished-home pre-flight → `blocked` ACTION; then `add` / `replace` (content diff via `_content_dict`, orchestration keys stripped so flipping `critical` never replaces a container) / `terminal` bucket (`restart: no` + status ∈ `{exited, dead}`) / `start` (also when `is_service_flapping`) / `in_sync`;
5. post-pass withdraws bring-up actions for `blocked_by_dependency`;
6. sorts with `_action_order_key`.

Plan shape: `{changed, actions[], in_sync[], disabled[], shed[], terminal[], blocked[], unmanaged[], invalid[], graph:{depths,waves,edges}, summary:{declared,invalid,shed,terminal,blocked,total_actions,destructive}}`.

`_action_order_key` returns `(0, -depth, band, name)` for stop kinds and `(1, depth, -band, name)` for bring-up kinds, `(2,0,0,name)` for `blocked`. One sort, two orderings: topological primary, shutdown band (`shutdown.priority`, default 50) as intra-wave tie-break. Its docstring records the interim's evidence: "Alphabetical starts docker-health-exporter before docker-socket-proxy (its only data path), immich-legal-server before immich-legal-redis, and onyx-api before all three of its stores — and 15 of 39 services have no healthcheck, so 'healthchecks absorb that' was a hope, not a mechanism."

`apply_reconcile_plan` calls `lifecycle.guard_not_halted`, then executes per action with try/except isolation; `replace` = `_location_change_refusal` / `_volume_location_change_refusal` pre-check, then `remove(keep_declaration=True, fire_hooks=False)` + `install_declaration(preserve_data_on_rollback=True, previous_image=…)`; any success invalidates the image-updates cache. `verdict(plan, results, strict)`: an invalid file is fatal by default ("a truncated critical service's file has no readable `critical` flag, so criticality cannot exempt it"); a failed action is fatal only for `critical: true`.

### The intent overlay

`$SYRVIS_HOME/data/state/intent.json`, atomic (O_EXCL temp + `os.replace`), 0644:

```json
{"schema_version": 1, "device": "in-service",
 "shed": [{"service":"onyx-api","reason":"md6-resync","since":"2026-08-16T12:00:00Z","until":"2026-08-24","by":"seam"}]}
```

`SHED_REASON_RE = ^[a-z0-9][a-z0-9._-]{0,63}$` (a token, not prose — it rides argv through the seam's charset-allowlisting shim and becomes a Prometheus label). `SHED_UNTIL_RE` accepts `YYYY-MM-DD` or `…Thh:mm[:ss]Z`; `expired()` compares strings, deliberately, because both shapes are zero-padded and lexicographically ordered. Reads never raise (absent/garbage → `{"device":"in-service","shed":[]}`) and drop bad rows individually; writes validate strictly. Re-shedding preserves the original `since`.

The composition rule is one sentence (`intent.py:20-27`): *a workload runs iff the device is `in-service` AND the service is `enabled` AND the service is not shed.* Consumers: the planner (shed → `enabled: false` overlay, own bucket), `write_declaration_from_install` (shed pins `enabled: false` over any incoming manifest), `instance_bundle._plan_declarations` (`shed_pinned` report rows), `ServiceManager._shed_refusal` (imperative `start`/`recreate` refuse; `stop` never guarded), `deploy_bundle` (lands bits, journals `STATE_SKIPPED`, "shed — not started"), and `_intent_columns` (`intent ∈ shed|enabled|disabled|undeclared`, shed outranks the flag).

### Guards

`guard_enable_change(services, allow, home, action)` refuses by name; `enable_changes(incoming, live, shed)` computes off→on transitions, excluding shed (handled more strongly upstream by the pin). `instance_bundle._live_enabled` reads the raw YAML rather than `ServiceDefinition` so an unparseable declaration can still say "I am turned off" — "A guard that fails OPEN on an unreadable file would be no guard at all."

`guard_bulk_degraded(action, force, home, mdstat_path, services, by)` parses `/proc/mdstat` (`BUSY_OPERATIONS = resync|recovery|reshape|check|repair`) and refuses without `--force`. Unreadable mdstat → `[]` ("Unreadable must mean 'no evidence of a rebuild', never 'assume one'"). `--force` also closes breakers in scope, but `close_breakers_in_scope` fails safe on `by`: only `cli:`/`seam:`/`mcp:` close; `hostd`/`s99`/`cron` inherit (`opc:F2`). Every override writes a JSON line to `logs/overrides.log` via `intent.log_override` — best-effort, never a blocker.

### Shares registry

`$SYRVIS_HOME/shares.d/*.yaml` → `{id (defaults to stem), share_name, volume, class, writers}` → `ShareDeclaration(share_id, root=<volume>/<share_name>, share_class, writers)`. Malformed declarations *raise* rather than skip ("a registry that silently drops entries would turn a typo into a phantom 'share not declared' error somewhere else"); duplicate ids raise. `resolve_fileplane` refuses undeclared shares and refuses `rw` on a `class: resting` share unless the service is in `writers:` — "file-plane rw is sanctioned by the share declaration, not the service."

### `volume_locations`

`{<declared volume source>: /volume<N>}`. Five parse-time refusals (`_validate_volume_locations`): unknown key; semantic slots `config/logs/secrets` and infra host mounts unoverridable; value must re-match `LOCATION_RE`; overlapping/nesting keys; and an override *requires* `location:`. At runtime `ServiceManager._volume_home` → `<override>/<apps-root>/apps/<name>`, `_service_paths` stores it under a reserved `volume:` prefix with containment assertions, `_generate_compose_file` re-checks `paths.is_mounted_volume` per override on *every* materialize/start path and writes a `# volume_locations: <vol> -> /volumeN` comment into the compose file. `_volume_location_change_refusal` refuses a per-volume placement change while that volume's current dir is non-empty (nested binds *shadow*, they don't merge).

## Design decisions & their rationale

- **Per-file over a monolith** (`service-loading-design.md` §5): "partial validity is all-or-nothing (one typo blocks the whole set — precisely what the non-blocking requirement forbids)".
- **Orchestration keys live only in declarations** (§6 phase 1): materialized manifests strip them so older/rollback versions still parse, and "a git repo can never declare itself `critical` or toggle its own enablement".
- **`depends_on` reinterpreted, not un-rejected** (`service_schema.py:190-196`): "That reject was right for the COMPOSE meaning and is superseded here for the ORCHESTRATION meaning… `_generate_compose_file` still never emits them."
- **`blocked` bucket ≠ validation error** (design/63 D2 as amended, `opc:F10`): as first written, a hard edge onto a disabled target invalidated the file, "which meant a partial load-shed made a RUNNING service's declaration invalid — reclassifying it `unmanaged` on the operator's own deliberate action… And because 'which services are disabled' flips under any apply (`opc:F9`), validity itself would have been non-deterministic."
- **Intent outside the declaration set** (`intent.py:12-19`): "the flag that says 'this service is deliberately down' lived in exactly the file the next apply overwrites — which is how fourteen load-shed services could be resurrected, mid-array-resync, by a runbook whose own text said they would stay down (incident 2026-08-16)."
- **`unshed` starts nothing** — "one bring-up path, not two."
- **Overridable guards + mandatory evidence** (`guards.py:28-31`): "the refusal and the bypass cost the same one line of evidence."
- **`terminal` is a bucket, not an action**: "an action would report as a failure on every pass… a terminal service is not a failure — it is a service with nothing owed."
- **Absent ≠ create for stateful apps** (`_vanished_home_refusal`): "Absent means CREATE is the correct default for a declarative engine; it is the wrong default for a stateful app whose data is the thing being converged."

## Invariants & contracts

1. **Filename ≡ `name:`**; violation invalidates only that file.
2. **One bad file never poisons the set** — load, converge and health isolation are all structural.
3. **Unknown top-level key ⇒ reject** on every write path; tolerant drop is READ-ONLY.
4. **`depends_on` never reaches compose.** Any generated `compose/<name>.yaml` is `depends_on`-free (asserted in `test_depends_on.py:125`).
5. **No edges anywhere ⇒ byte-identical plan ordering** to the band-only interim.
6. **Shed outranks `enabled:`** everywhere; nothing in the bundle/apply/deploy path can address `intent.json`.
7. **`installed-but-undeclared` is never touched** without an explicit prune policy; destructive prune actions carry `destructive: true` for caller-side gating.
8. **Orchestration-only diffs never replace a container** (`_content_dict`).
9. **`location:`/`tier: infra` are authorship-gated** — `location` by `OPERATOR_AUTHORED_PREFIXES = ("services.d:", "deploy:")`; `tier: infra` was moved (design/66) to a root-held `DEFAULT_INFRA_SERVICES` / `<home>/config/infra-allow` name+image-repo grant, because the prefix test on a streamed bundle "used to test the very string deploy_bundle had just written — a tautology that could not fail."
10. **Plan is pure**; only `apply_reconcile_plan` is halted-guarded.
11. `shares.d` is read-only to the engine — it never creates or chmods file-plane paths.

## Gaps, debt & sharp edges

- **Documentation is materially stale.** `docs/wiki/07-service-schema-reference.md:185` still says "**`depends_on`** — *rejected*"; `wiki/05:321` repeats it; the roadmap's phase-1 table lists "`depends_on` reject ✅" as a feature. Neither wiki page documents `depends_on`, `volume_locations`, `location`, `tier`, `shutdown`, `hooks`, `ports`, `dashboard`, or `shed`. The reference's top-level table omits ~10 of 28 allowed keys. `service_manager.py:1621` still carries the stale comment "NB: depends_on is rejected at schema-validation time".
- **`ServiceDefinition.__post_init__` bypasses validation.** Constructing the dataclass directly (tests, `copy.copy` in `write_declaration_from_install`/`_plan_declarations`) skips `from_dict` entirely; `dependency_edges()` re-parses and can raise from a hand-built object at graph-build time, where it is swallowed into `errors` rather than surfacing as a parse error.
- **The graph is solved twice on the reconcile path** — once in `load_declarations._apply_graph_validation`, once in `build_reconcile_plan`. Documented as deliberate ("re-solving is cheap"), but the two calls can see different `invalid` lists depending on the caller, so `invalid_targets` (and hence `blocked` vs `errors`) is caller-dependent for hand-built declaration dicts.
- **`blocked` overloading.** `plan["blocked"]` (a bucket, not a failure) and `actions[kind=="blocked"]` (the vanished-home safety refusal, which grades `ok=False`) share a word. The docstring flags it as "load-bearing", which is an admission that the naming is a trap for adapters.
- **`healthy` is validated but not enforced.** M1 is ordering-only; the journal records `started`, "never `healthy`, because 0.5.16 verifies no health." A declaration can therefore assert a health gate the engine silently does not apply — the exact class of "checks that report what they cannot see" the same module refuses elsewhere.
- **Read/write asymmetry in intent.** `read_intent` accepts *any* `service` string (no `NAME_RE` check) and any `reason`/`until`; `validate_intent` enforces both. A hand-edited `intent.json` with a bogus reason reads fine and shed-blocks a service, but the next `shed`/`unshed` write of an unrelated row raises `IntentError` on the whole document.
- **Shed rows are unbounded and never garbage-collected.** `expired()` reports lapsed rows but nothing acts on them; a shed row for a service whose declaration was deleted persists forever and still blocks dependants via `dependency_blocked`.
- **TOCTOU / concurrency.** `intent.json` writes are atomic per-write but read-modify-write is not locked — two concurrent `shed` calls lose one row. `services.d` has no locking at all: `instance_bundle._write_declarations` (unlink + atomic write) races an in-flight `reconcile` that already loaded declarations; the plan then applies against a set that no longer exists on disk.
- **`_content_dict` diff is YAML-dict equality**, so key-order-insensitive but value-type-sensitive (`version: 1.0` vs `"1.0"` round-trips through `str(data["version"])` — fine — but a `restart` default present vs absent is normalized only because `to_dict` always emits it). Any future optional key that `to_dict` emits conditionally becomes a silent replace trigger.
- **Ownership blur between `enabled:` and shed.** Three mechanisms now hold a service down (`enabled: false`, shed, `restart: no` + terminal); the `restart: no` comment itself says "Three mechanisms, one field, cancelling out." `set_declared_enabled` (imperative stop) writes ephemeral intent that the next apply overwrites — the CLAUDE.md notes this explicitly, but the CLI verb `service stop` does not distinguish itself from `shed` in name.
- **`validators.py` is misnamed** for this subsystem: it validates the *installation*, not declarations. Its declaration-adjacent checks (`check_home_collision`, `check_apps_root`) encode a standing accepted risk as `passed=True` with a `⚠ ARMED` message — a deliberate but fragile compromise ("CheckResult is binary… encoding a standing fact as passed=False put the smoke tier… permanently red within hours").
- **Reader-enumeration gate is manual.** CLAUDE.md: the dashboard image "must be rebuilt + repinned with the edge schema before any `depends_on` lands in a real `services.d`", and "platform rollback below 0.5.16 is forbidden" while any edge exists. Nothing in code enforces either; `tolerant=True` mitigates unknown *keys* but `depends_on` is a *known* key with new semantics, so an old strict reader parses it and then rejects it.
- **Test blind spots**: no test exercises concurrent apply/reconcile; `shares_registry` has no per-file isolation (one malformed share file raises for the whole registry, unlike `services.d`) and no test asserts that asymmetry is intended; `expired()` has no consumer to test.

## Raw material worth citing in the retrospective

- "the file collection is the substrate; every explicit interaction is sugar that writes those files… One mechanism, four front doors." (`service-loading-design.md` §1)
- "a workload runs iff the device is `in-service` AND the service is `enabled` AND the service is not shed." (`intent.py`)
- "fourteen load-shed services could be resurrected, mid-array-resync, by a runbook whose own text said they would stay down (incident 2026-08-16)."
- "an edge onto a service that does not exist is a typo, not a deliberately-down dependency." (`services_d.py:205`)
- "never a silent downgrade to `started`… 2026-08-16 was a lesson in checks that report what they cannot see." (`build_dependency_graph`, `dep:F9`)
- "Absent means CREATE is the correct default for a declarative engine; it is the wrong default for a stateful app whose data is the thing being converged." (`_vanished_home_refusal`)
- "0 declarations but N installed service(s) — refusing to reconcile against an empty config/services.d." (floor check)
- "the refusal and the bypass cost the same one line of evidence." (`guards.py`)
- "15 of 39 services have no healthcheck, so 'healthchecks absorb that' was a hope, not a mechanism." (`_action_order_key`)
- Numbers: `MAX_DEPENDS_ON = 12`, `MAX_TASKS = 16`, `MAX_HOOKS = 8`, `MAX_DASHBOARD_PANELS = 12`, `SHUTDOWN_STOP_TIMEOUT_RANGE = (5, 300)`, `SHUTDOWN_PRIORITY_RANGE = (0, 100)` default 50, 28 allowed top-level keys, 7 reserved names, 4 `INFRA_HOST_MOUNTS`, 3 readiness classes, 3 prune policies, 4 non-action buckets (`disabled`/`shed`/`terminal`/`blocked`), intent `schema_version: 1`, 60-minute journal staleness rule.
- Decision names to cite: design/63 D1 (readiness classes), D2 as amended / `opc:F10` (blocked bucket), D3 (wave narration), D6 (edge beats band), D7 (one planner), M1 (validation + ordering only), M2 (the gating engine, unbuilt); design/65 D8 (shed overlay pulled forward); design/37 §4 Phase 1 (`volume_locations`); design/26 (`location:`); design/22 + design/66 (infra tier grant moved to a root-held list); design/60 §11.1 points 5-6 (one breaker reset path); `dep:F11`, `dep:F23`, `opc:F2`, `opc:F9`.