# Lifecycle & Host Integration (shutdown/resume, schedule, VMs, doctor/verify, updates & provenance)

## Purpose & role in the system

This subsystem is everything SyrvisCore does **to and with the DSM host over time**, as opposed to the per-service declaration/deploy plane. Five loosely-coupled concerns, all reached through the same "deterministic library + thin adapter" rule:

1. **Instance lifecycle** — derived workload states, transition hooks, the persisted `halted` runstate, and the ordered `shutdown`/`resume` walks (the UPS/reboot path).
2. **Host scheduling** — the delimited managed block in `/etc/crontab`, driven by `config/jobs.d/` declarations plus root-owned scripts materialized from ONE root-configured, commit-pinned git source; plus a strictly read-only census of DSM's *own* Task Scheduler.
3. **VMs** — Synology VMM guests as a declared workload type (`config/vms.d/*.yaml`), adopt-first, never created or deleted.
4. **Health** — `syrvis verify` (read-only report + `--fix` remediation) and the older `syrvis doctor`, sharing one remediation dispatch.
5. **Supply-chain readouts** — report-only container-image update detection (`syrvis updates`), provenance/trust (`syrvis images`), and instance export (`syrvis export`).

The subsystem also owns the two artifacts that make the whole platform survive a reboot: the rootfs-resident rc.d hook `S99syrviscore.sh` and the volume-resident `bin/syrvis-startup.sh`.

## Key modules and files (path — role — approx size)

All under `/Users/kevinteg/code/SyrvisCore/packages/syrviscore/src/syrviscore/` unless noted.

- `lifecycle.py` — derived states, host/container hooks, runstate, `shutdown_instance`/`resume_instance` — 917 lines.
- `schedule.py` — privileged half of jobs.d: pinned clone, manifest verification, script materialize, crontab block splice, `compute_plan`/`apply_schedule`/`sync_from_source`, DSM task census — 1023 lines (**uncommitted, +422/−51**).
- `jobs_d.py` — declarative half: `JobDefinition` (`{schedule, enabled}` only), cron allowlist, managed-block parse + reconcile plan — 287 lines.
- `vms.py` — `VmDefinition`, `SynologyVmmAdapter` (`synowebapi --exec`), `VmManager` (list/status/start/stop/restart/adopt/verdict) — 490 lines.
- `verify.py` — `build_report`, core + L2 drift gathering, `remediate`, the `verify` click command — 483 lines.
- `doctor.py` — legacy print-oriented diagnostics + `--fix` — 380 lines.
- `remediation.py` — the single `fix_action` → privileged op dispatch — 74 lines.
- `update.py` — service-side (legacy) GitHub/SPK version management + `_regenerate_boot_artifacts` — 633 lines.
- `image_updates.py` — OCI tags/list client, flavor-aware newer-tag detection, 6h file cache — 446 lines.
- `image_provenance.py` — cheap/heavy provenance tiers against `image_trust.yaml` — 458 lines.
- `dashboard.py` — deterministic Grafana model generator (`DASHBOARD_SCHEMA_VERSION = 1`) — 747 lines.
- `privileged_ops.py` — `render_boot_script` (S99), `render_startup_script`, `ensure_schedule_block`, `BOOT_HOOK_CONTRACT = 3`.
- `instance_bundle.py` — `export_instance` / `syrvis-instance/v1`.
- Docs: `docs/vms-workload-design.md` (141), `docs/image-provenance-design.md` (114), `docs/backup-restore-design.md` (365).

## How it actually works

### Derived state + hooks

`derive_state(observed, enabled)` maps docker status × declared intent to `running|stopped|failed|unknown`. Key rule: `exited` is `failed` **only if declared enabled**; `restarting`/`dead` are always `failed`. `enabled=None` (intent unknown) suppresses the exited→failed page. `derive_vm_state` mirrors it (`transition` counts as running).

Two hook kinds:
- **Container hooks** — manifest `hooks:` mapping event → a declared `tasks:` argv, valid only for `pre-stop`/`post-start` (`CONTAINER_HOOK_EVENTS`) because those are the only events with a verifiably-running container.
- **Host hooks** — one executable at `$SYRVIS_HOME/hooks.d/<workload|instance>/<event>`. Events: `pre/post-start`, `pre/post-stop`, `pre/post-deploy`, `pre-shutdown`, `post-resume`. Trust check `_hook_is_trusted` walks every component from `hooks.d/` to the leaf: no symlink, owner-ok, `st_mode & 0o022 == 0`, leaf regular + owner-exec, then a `realpath` containment re-check ("belt-and-braces after the symlink-free walk"). Env is scrubbed to a fixed set plus `SYRVIS_HOOK_*`; `SYRVIS_IN_HOOK=1` is a re-entrancy fuse. Timeout 30s, output capped at 16 KiB, appended as JSON lines to `logs/hooks.log`.
- Failure policy: only `ABORTING_EVENTS = {pre-start, pre-deploy}` may abort, never under `force`. "you must always be able to stop."

### Runstate (`data/state/runstate.json`)

Schema `syrvis-runstate/v1`, root-written 0644, atomic (`O_EXCL` temp + `os.replace`):

```json
{"schema":"syrvis-runstate/v1","state":"halted","reason":"ups|reboot|maintenance",
 "at":"2026-08-17T…Z","by":"cli|shutdown|boot|restart","resume_on_boot":true,
 "workloads":[{"name":"…","scope":"service|vm|core","state":"running"}],
 "result":{"ok":true,"failed":[],"forced":[]}}
```

Absent/unreadable == active (fails safe). `guard_not_halted` raises `InstanceHaltedError` (`code = "instance_halted"`) from `DockerManager.start_core_services`/`restart_core_services` and `services_d.apply_reconcile_plan`; only resume/boot pass `allow_halted=True`.

### `syrvis shutdown` — ordering and the reserve-first budget

Order (lifecycle.py:451–707): snapshot running L2/VM/core → **write halted FIRST** (closes the race with a cron reconcile) → instance `pre-shutdown` hook (never aborts) → issue VM ACPI `shutdown` fire-and-forget → stop L2 in ascending `shutdown.priority` bands → wait for VMs, hard-off stragglers → stop core in `CORE_STOP_ORDER = (dashboard, cloudflared, portainer, traefik)` → rewrite runstate with `result`.

Budget constants: `DEFAULT_SHUTDOWN_BUDGET_S = 180`, `DEFAULT_VM_DEADLINE_S = 90`, `CORE_STOP_RESERVE_S = 30`, `STOP_FLOOR_S = 5`, `STORE_BAND = 90`, service defaults `stop_timeout 30` / `priority 50`.

**Reserve-first clamping (design/63 D6, dep:F3)** — stores (`priority >= 90`) always get their full declared grace, *even when the budget is already overspent*; consumers get `min(declared, max(5, remaining − store_tail − vm_drain_remaining − core_reserve))`. The VM claim is `max(windows)`, anchored at the ACPI **issue** time so the drain decays in real time and is a max, not a sum. Reported as `budget: {budget_s, store_reserve_s, vm_reserve_s, core_reserve_s, reserve_s, clamped:[names]}`.

`_wait_vms_off` polls `vms.status(name).power in ("stopped","not_found")` every ≤3s against per-guest absolute deadlines; overdue guests get `poweroff` then a second 5–15s grace window. VM `post-stop` host hooks fire after the wait.

### `syrvis resume`

Core → VMs → L2. Core start is explicit because "graceful stops disarm `restart: unless-stopped`". VM targets are the runstate snapshot, falling back to declared `enabled and autostart`. L2 goes through the ONE reconcile engine (`load_declarations` → `build_reconcile_plan` → `apply_reconcile_plan(trigger="resume", allow_halted=True)`), because declared intent was never touched. `clear_runstate()` runs **after** all tiers are attempted — "a crash mid-resume leaves the instance halted". Boot path: `syrvis reconcile --boot` reads the runstate and either auto-resumes (`resume_on_boot`) or prints the hold.

### The managed crontab block

`jobs.d/<name>.yaml` accepts exactly `{schedule, enabled}`. `command` and `source` are *named* rejections; unknown keys are rejected too. Name regex `^[a-z0-9][a-z0-9_-]{0,63}$`, filename must equal name. Cron spec: exactly 5 fields, charset `[0-9*/,-]` only — "a security allowlist, not a feature-complete parser" — deliberately narrower than crontab (no `@reboot`, no names, no `%`) because the shim char-allowlist blocks `*`/`,`, so the schedule must travel as file content, never argv. Command is derived: `<home>/jobs/<name>`; line = `"{schedule} root {jobs_dir}/{name}"`.

Block markers: `### SYRVISCORE JOBS BEGIN (managed)` / `### SYRVISCORE JOBS END`. `_splice_block` rewrites only that region; DSM's `synoschedtask --run id=N` lines and the `SHELL/PATH/MAILTO` header are preserved verbatim. `write_crontab_atomic` preserves the existing mode.

`compute_plan` (read-only) returns `{changed, actions[{kind:add|update|remove,…}], in_sync, desired, summary, invalid, source, pin, scripts, confs}`.
- `scripts[name] = {present, sha256?|error?}` — lets a deployment verify root-owned scripts over the seam without break-glass.
- `confs[name] = {present, bytes?|error?}` — **deliberately no sha256**: "a digest of a short low-entropy value is a brute-forceable oracle over the seam." Presence answers "did `config set` ever run", which is the question a confless job (exit 0 + a stderr line) otherwise hides.

`apply_schedule` is LOCAL only (no fetch): it drops any enabled job whose script is missing (`skipped[{name,reason}]`) and rewrites the block. This is the self-heal path from the boot startup script (`"$INSTALL/bin/syrvis" schedule apply || true`) and from `verify --fix` → `remediation.apply_fix("schedule_block")` → `privileged_ops.ensure_schedule_block`.

### The pinned source (design/66 — the uncommitted change)

The module docstring now opens with an explicit retraction: the old claim that a compromised operator "can at most re-apply the already-synced, root-vetted set" **"WAS FALSE, and was false for as long as this module has existed"** — the clone took the remote's default-branch HEAD and `materialize_job_script` wrote those bytes `root:root 0755` unread; home-tech's drift detector hashed the NAS copy against the same repo, so "a push made both sides the attacker's and the check went green."

New state file `<home>/config/jobs.pin` (root 0644), one value line: `<40-hex rev> sha256:<64-hex>`, deliberately parsed with the same first-non-comment-line rule as `jobs.source` ("a check that reads a different line from the one the platform reads is fiction" — a bug caught in verify-all on 2026-08-10). Malformed ⇒ `None` ⇒ fail-closed.

`_clone_configured_source(url, rev)`: `git init` + `fetch --depth 1 <url> <rev>` + `checkout --detach FETCH_HEAD`, falling back to a **full** clone + `checkout --detach <rev>` (never a shallow default-branch clone). Then `git rev-parse HEAD` must equal `rev` — "THE GATE. Everything below this line trusts that HEAD == the pin." `GIT_ALLOW_PROTOCOL=https:git:ssh:file`, `GIT_TERMINAL_PROMPT=0`.

`parse_manifest` reads `jobs/MANIFEST.sha256` (plain `sha256sum` format so `sha256sum -c` works) and hard-refuses any malformed/duplicate/out-of-subtree/illegal-name row: "a parser that silently ignores rows it does not understand is a parser an attacker writes rows for." `materialize_job_script(..., expected_sha256=None)` is a **refusal, not a bypass**.

`sync_from_source` has two modes: **bare** (materialize the pin; refuse if unpinned) and **advancing** (`--to` + `--manifest`, both re-verified against the fetched tree; `changed_scripts` names every root-run script the advance would alter, computed *before* any write; `write_pin` runs LAST, only on full success, so a half-applied sync leaves the old pin standing). Seam exposure: new `schedule_sync_pin` Command with `KIND_GIT_REV`/`KIND_SHA256` slots, matching shim predicates `is_gitrev`/`is_sha256` and a sudoers line — justified as granting no new capability, only making the existing one "argument-bearing, journaled, and diffable."

### DSM task census

`dsm_task_census()` shells `/usr/syno/bin/synoschedtask --get` (20s timeout), tolerantly parses `Key: [value]` lines into rows (`_DSM_FIELD_ALIASES` → `id/name/owner/state/type/command/schedule`, everything preserved under `fields`), bounded at 200 tasks / 500 chars per field. **Never raises**; every failure is data. Crucially, non-empty output that parses to zero rows is reported `ok:false` — "treat the census as UNKNOWN, not empty." Rationale (design/20, ops:F20): a DSM task pointing outside the managed block "is invisible to every seam verb… 'the repo declares jobs/login-alert, the NAS ran bin/…, and nothing flagged it' had no automated detector." Strictly read-only by construction — SyrvisCore never creates/edits/deletes a DSM task.

### VMs

`VM_ALLOWED_KEYS` = name, type, backend, guest_name, enabled, critical, autostart, source, resources, passthrough, health, `stop_timeout` (int 5..600, default 90), `description` (≤200, no control chars). `name` uses the service `NAME_RE`; `guest_name` is the raw VMM display name (spaces allowed, ≤64, no control chars) and never appears as operator input — "the CLI/seam never take a raw guest string." Per-file failure isolation, broken file skipped.

Adapter: `synowebapi --exec api=SYNO.Virtualization.API.Guest[.Action] version=1 method=…` with an injectable `run`; JSON-parse + `success` check; power actions limited to `poweron|shutdown|poweroff`. `_VMM_STATUS_MAP` normalizes to running/stopped/transition/unknown; an undeclared-in-VMM guest is `not_found`.

`VmManager.list()` carries `stop_timeout` + `description` explicitly because it doubles as **the shutdown-budget census** — "a reader asking 'what else claims the shutdown window' must not have to open vms.d." `verdict()` returns `{ok, checked, failures:[{name,power}]}`; only critical+enabled+not-running fails. `adopt(guest_name)` writes a declaration from a live guest, deriving `autostart` from `autorun`/`autostart` and `resources` from `vcpu_num`/`ram_size`.

### verify vs doctor

`verify.build_report` → `{smoke, healthy, degraded, checks:[{category,name,passed,message,fixable}], drift, l2_drift}`. Core drift comes from `drift.detect_drift("core", expected_from_compose, actual_from_docker)` plus a `STALE_STATIC` item when `traefik.yml`'s mtime postdates the container's `State.StartedAt` (the "/ping-404 class of failure"). L2 drift is the union of enabled declarations and installed manifests, **minus** shed services (incident 2026-08-16: "fourteen intentionally-stopped services read as fourteen MISSING containers on every poll for a week, which is how a drift signal gets ignored") and **minus** TERMINAL `restart: no` services observed exited/dead (dep:F11) — but an absent container still drifts. Severity: a critical L2 failure ⇒ `healthy=False`; non-critical ⇒ `degraded=True` with exit 0.

`verify --fix` elevates, builds a pre-report, and only remediates if unhealthy ("to avoid needless restarts"): validator fixables via `remediation.apply_fix`, core drift via pull + `up -d`, `STALE_STATIC` via a targeted `restart_traefik_if_running` (because `up -d` won't restart for a bind-mounted file change), then L2 failures via `sm.start`.

`remediation.apply_fix` is the single dispatch for `docker_group`, `user_group:<u>`, `socket_perms`, `symlink`, `startup:<u>`, `boot_script`, `manifest_perms`, `config_tree_perms`, `schedule_block`; unknown actions return `(False, …)` explicitly. Its docstring records why: the audit's H3 finding that "doctor silently ignored `boot_script`/`manifest_perms`" came from duplicated, drifting dispatch.

### Boot artifacts

`render_boot_script` is the single source of truth for `S99syrviscore.sh`, compared byte-for-byte by `SystemValidator.check_boot_script`. `# boot-hook-contract: 3` is embedded so the **manager** (`syrvisctl doctor`, which deliberately does not import the service package) can grade currency from the rootfs alone. Start case, in order: (1) inline seam-shell heal (`sed` `/sbin/nologin` → `/bin/sh` for `syrvis-operator`/`syrvis-reader`), (2) advisory 60s gate on `synocheckshare.service`, (3) reclaim guard for `/volumeN/{syrviscore,<apps-root>}_<N>` directories carrying `.syrviscore-manifest.json` or `apps/`, refusing loudly if the target exists non-empty, (4) trampoline to `bin/syrvis-startup.sh` **with a load-bearing else branch** that alarms via ntfy read from the rootfs cache `/usr/local/etc/syrviscore-boot.env`. Stop case: `timeout 150s "$INSTALL/bin/syrvis" shutdown --reason reboot --json || true`, then delete the macvlan shim, then `exit 0`.

### Updates, provenance, export

`image_updates`: pure `requests` OCI client (bearer challenge, `Link` pagination, `_MAX_PAGES=12 × _PAGE_SIZE=100`, 10s timeouts). `find_newer_tags` matches "flavor" = same v-prefix, same `-suffix`, **and same numeric component count** — so `2.5` never jumps to CalVer `20240101` and `16` never becomes `16.2`. Cache `data/.image-updates-cache.json`, TTL 6h, atomic write, invalidated after any successful reconcile action (`services_d.py:931`) so a just-applied update stops showing as available. Report shape: `{checked_at, count, update_count, images:[{image,kind,name,registry,repository,current,latest,newer[≤5],update_available,truncated?,error?}], cached}`.

`image_provenance`: cheap tier (zero network — trust registry + ref parse + optional local `docker inspect`) always live; heavy tier (manifest annotations for `org.opencontainers.image.base.name`, Hub `tag_last_pushed`, update-available) cached 6h in `data/.image-provenance-cache.json`, only on `--refresh`. Trust = bundled `image_trust.yaml` merged with `$SYRVIS_HOME/trust/image_trust.yaml`, keyed `<registry>/<repository>`. `_score`: `ok` iff publisher ∈ {official, verified, sponsored-oss, trusted-org} **and** digest-pinned **and** no base drift. `_derive_publisher` never guesses "trusted" — `docker.io/library/*` ⇒ official, everything else (including any GHCR org) ⇒ community. `status_summary` runs with `inspect=False` so `syrvis status` stays instant and offline.

`export_instance` emits `{apiVersion:"syrvis-instance/v1", env, stack, declarations}`. Secrets are `****` by default (`is_secret_key` plus any name listed in `TRAEFIK_ACME_DNS_ENV`); inline `environment: ["KEY=VALUE"]` entries in declarations are redacted too. `--reveal-secrets` requires elevation. An unparseable declaration is a **hard error**, because "a snapshot that silently omits a declaration would, on a later restore (declarations are a replace set), DELETE that service's intent."

## Design decisions & their rationale

- **Halt-gate first, clear-last** (lifecycle.py:536, :896) — write `halted` before stopping anything; clear only after every resume tier has been attempted.
- **Reserve-first clamping** (lifecycle.py:411–435) — "a consumer force-killed at 60s is an inconvenience, a postgres force-killed at 5s is a WAL replay or worse." The measured arithmetic that forced it: "store wave 120s + VM ACPI drain 90s + core 30s = 240s of irreducible reserve against a `timeout 150s` rc.d wrapper."
- **VM window anchored at ACPI issue** (lifecycle.py:631–638) — re-anchoring at the wait "would double-count" the drain that overlapped the L2 phase.
- **`stop_timeout` on the VM declaration** (vms.py:93–98) — fnd:F37/dep:F24: "the HAOS guest was an undeclared tenant of a 90s slice."
- **Adopt-first, never create/delete** (`docs/vms-workload-design.md` §2/§4) — "Create-from-image is NOT a reliable, documented API path… libvirt / virsh / qemu are NOT the surface." VMs are excluded from `syrvis updates` and there is no `vm set-image`; delete stays off the seam entirely.
- **Derive-not-declare for jobs** (jobs_d.py:8–23) — command derived from name, source root-configured; the cron spec is validated in Python precisely because it cannot traverse the argv seam.
- **DSM census, not management** (schedule.py:708–725) — the design/20 blind spot gets an enumerator, and nothing more.
- **Report-only image updates** (image_updates.py:9–14) — "A NAS is critical and declaratively managed, so an update is a deliberate act."
- **Trust as a git assertion** (`docs/image-provenance-design.md` §6) — "GHCR gives no reputability signal at all, so our own images can only be asserted"; base-from-label is unreliable so `expected_base` is the foundation and live reads merely validate it.
- **`BOOT_HOOK_CONTRACT` is a capability integer, not a content hash** (privileged_ops.py:47–79) — the `synocheckshare` gate deliberately did **not** bump it, because that would "mark every deployed contract-3 hook STALE and page for a race-narrowing tweak."

## Invariants & contracts

- `data/state/runstate.json` absent ⇒ active. Any bulk bring-up (`start_core_services`, `restart_core_services`, `apply_reconcile_plan`) must call `guard_not_halted`; only resume/boot may pass `allow_halted=True`.
- Shutdown **never** mutates declared intent (`set_intent=False`), so resume/reconcile fully restore the previous set.
- `hooks.d/` and `jobs/` are root-owned trees the operator cannot write; the whole jobs security model rests on that plus the derived command path.
- Only the delimited crontab block is ever rewritten; DSM lines survive byte-identically.
- `jobs.pin` semantics on the wire: `plan["pin"] == null` **means UNPINNED** and "must never be smoothed over" — home-tech's `nas.jobs` / `nas.jobs-pin` grade against it.
- `syrvis-instance/v1` is a replace set; `apply` refuses unknown `apiVersion`.
- `DASHBOARD_SCHEMA_VERSION = 1` is additive-safe; consumers may gate on it.
- Seam contracts in this subsystem: `verify`, `verify_smoke`, `images`, `export` (sudo, read-only), `updates`, `schedule_list`, `schedule_dsm_tasks`, `doctor` (syrvisctl), `shutdown` (`reason` slot enumerated `ups|maintenance`), `resume`, `restart_graceful`, `verify_fix`, `vm_list/status/start/stop/restart`, `schedule_apply`, `schedule_sync`, `schedule_sync_pin`.

## Gaps, debt & sharp edges

- **`reason=reboot` is unreachable from the seam.** The shim predicate is `^(ups|maintenance)$` and `KIND_HALT_REASON` is enumerated the same way, but `HALT_REASONS` and the CLI `Choice` include `reboot`. Only the local rc.d stop case can write that reason. Any tooling that reads `runstate.reason` must handle a value the seam can never produce.
- **The budget arithmetic still doesn't fit the rc.d wrapper.** The stop case bounds `syrvis shutdown` at `timeout 150s` while `DEFAULT_SHUTDOWN_BUDGET_S = 180` and the store reserve is *never* clamped — so a store-heavy instance can be SIGKILLed mid-walk on a DSM reboot. The code comments acknowledge the 240s figure; the source comment says outright "DSM's rc.d-stop timeout is UNVERIFIED here — a real DSM-reboot test must confirm."
- **`elapsed_s` is wrong on overrun.** `elapsed = budget_s - _remaining(deadline)` with `_remaining` floored at 0, so any overrun reports exactly `budget_s`. The one number an operator would use to tune the budget is the one that saturates.
- **No `post-shutdown` instance hook.** `HOST_HOOK_EVENTS` has `pre-shutdown` and `post-resume` only; there is no symmetric "we're down" hook, and no `pre-resume`.
- **Host-hook trust degrades off-root.** `_owner_ok` returns `uid == os.geteuid()` whenever euid ≠ 0. `run_host_hook` is reachable from unprivileged paths (`VmManager._fire_host_hook`, service stop), so in a non-root invocation a hook owned by the invoking user is "trusted." Fine given elevation gates, but the invariant is weaker than the docstring's "only root-authored."
- **`syrvis doctor` is orphaned debt.** It has no `--json`, is not on the seam, prints Unicode glyphs, duplicates validator orchestration that `verify` already owns, and exits 1 on any issue including purely-informational DNS findings. There is no `test_doctor.py` and no `test_verify.py` in the 60-file suite — the two health verbs are the least-tested modules here.
- **`syrvis update` (update.py) is a second, stale version manager.** It downloads `.spk` assets and builds venvs on the *service* side, in direct competition with `syrvisctl` (which downloads wheels and is the documented path), has no backup integration despite `docs/backup-restore-design.md` specifying pre-upgrade backups, and its `rollback` picks "the first version that isn't current" from `list_installed_versions()` rather than a recorded previous. It is still registered in `cli.py:111`.
- **`docs/backup-restore-design.md` is unreconciled with reality** — it is written against `syrvisctl`/`backup.py` in the manager package, and `syrvis update install` never creates the "automatic safety net" backup the design's first principle promises.
- **Docs vs code drift in the VM design.** `docs/vms-workload-design.md` still says "Not yet implemented" and describes a health `probe:` (TCP/HTTP) and reconcile-driven power convergence; `vms.py` validates a `health` mapping but never *runs* a probe, and there is no VM reconcile — `verdict()` only checks power. `syrvis vm adopt` is documented but I see no `vm adopt` seam Command (the design lists six `vm_*` verbs including `vm_adopt`; the registry has five).
- **`docs/image-provenance-design.md` promises fields that don't exist** — `base_eol`/`base_eol_date` (endoflife.date), `signed`/`has_sbom` (Referrers API), `cve_counts` (Trivy), and a `trust_tier`. `eol_product` is parsed from the registry and then never used. The status glyph's third state (`● scan-stale`) has no implementation.
- **Cache TTL vs staleness reporting.** `read_cached()` returns any cached report regardless of age, marked only `cached: true` — the dashboard fast path can render a week-old update report with no staleness signal.
- **`_conf_integrity` scans `<home>/config/<name>.conf` for every declared job**, so a job name colliding with any other file in `config/` (e.g. a future `stack.conf`) would silently report someone else's file's size.
- **Race window in `sync_from_source`**: declarations are installed and stale ones `unlink()`ed *before* scripts are materialized. If materialize fails mid-way, `config/jobs.d` already reflects the new commit while `jobs/` does not; `apply_schedule` then drops those jobs (correct, but the instance is silently unscheduled until someone reads `skipped`).
- **`parse_managed_block` keys on the last `/`-separated token**, so two jobs whose derived paths share a basename (impossible today, but the parser doesn't verify the path prefix matches `jobs_dir`) would collide, and a hand-edited line inside the markers is silently adopted as a managed job.
- **`_wait_vms_off` trusts `vms.status()` exceptions as "keep waiting."** A VMM backend that is hard-down makes every guest ride to its full deadline and then get a `poweroff` that also fails — burning the entire VM reserve on an unreachable API.
- **`resume_instance` clears the runstate even when tiers failed** (documented as deliberate), which means a resume that failed to start core still reports the instance "active" to `syrvis status` — the halted banner disappears while nothing is running.
- **Ownership ambiguity around boot artifacts**: `update.py::_regenerate_boot_artifacts` (service side), `version_manager.activate_version` → `syrvis _regen-boot-hooks` (manager side), `syrvis setup`, and `verify --fix`'s `boot_script` action all write the same S99 file. Four writers, one contract integer.

## Raw material worth citing in the retrospective

- **The retraction**, verbatim (schedule.py:10–16): *"⚠ THE CLAIM THAT USED TO END THIS PARAGRAPH — 'it can at most re-apply the already-synced, root-vetted set' — WAS FALSE, and was false for as long as this module has existed… a push made both sides the attacker's and the check went green."*
- **The shutdown doctrine** (lifecycle.py:419–422): *"a consumer force-killed at 60s is an inconvenience, a postgres force-killed at 5s is a WAL replay or worse."*
- **The measured arithmetic**: 120s stores + 90s VM drain + 30s core = **240s** reserve vs a **150s** rc.d wrapper; defaults 180 / 90 / 30 / floor 5; store band 90, default band 50, default grace 30.
- **The decapitation incident (2026-08-16)** (privileged_ops.py:383–393): *"one rename took out the wrapper, the operator seam, the cron self-heal AND the volume-resident `syrvis-startup.sh`… Nothing alarmed, because the alarm was stored inside the thing it was supposed to alarm about."* → the rootfs boot-env cache and `BOOT_HOOK_CONTRACT = 3`.
- **The load-shed drift lesson** (verify.py:107–112): *"fourteen intentionally-stopped services read as fourteen MISSING containers on every poll for a week, which is how a drift signal gets ignored."*
- **Why no conf hash** (schedule.py:611–615): *"a digest of a short low-entropy value is a brute-forceable oracle over the seam."*
- **Why the manifest parser is strict** (schedule.py:340–342): *"A parser that silently ignores rows it does not understand is a parser an attacker writes rows for."*
- **The census principle** (schedule.py:722–725): *"'the census could not run' and 'there are no other tasks' must never look the same."*
- **The DSM API verdict** (`docs/vms-workload-design.md` §2): *"This is a lifecycle + inventory API, not a provisioning one… SyrvisCore owns the VM's lifecycle + resource envelope; it does not own the VM's birth or its OS version."*
- **The provenance thesis** (`docs/image-provenance-design.md` §6): reputability as *"a curated, diffable git assertion, not an unanswerable live lookup."*
- **The H3 audit finding** (remediation.py:6–10) — duplicated fix dispatch silently dropped `boot_script`/`manifest_perms`; consolidation is the fix.
- Numbers worth quoting: hook timeout 30s / output cap 16 KiB; `MAX_DSM_TASKS = 200`, `MAX_DSM_FIELD_CHARS = 500`, `DSM_TASK_TIMEOUT_S = 20`; `SYNOCHECKSHARE_WAIT_S = 60`; VM `stop_timeout` 5–600 (default 90); image caches 6h, 12 pages × 100 tags, 10s HTTP timeout; `_MAX_PAGES` truncation is surfaced as `truncated: true`.