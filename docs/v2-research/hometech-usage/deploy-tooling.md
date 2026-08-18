# Deploy & apply tooling (the write path)

## What this slice is

Four "appliers" and three supporting scripts on the operator's Mac that turn git-declared intent in `/Users/kevinteg/code/home-tech` into NAS state, exclusively over the **operator seam** (`ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- '<argv>'`). Each applier owns one plane and one seam verb:

| script | plane | seam write verb | bundle schema |
|---|---|---|---|
| `scripts/apply-instance` | whole instance: `.env` + `stack.yaml` + the complete `services.d` set | `apply` | `syrvis-instance/v1` |
| `scripts/deploy-stack` | one L2 service's manifest + configs + secrets | `deploy` | `syrvis-bundle/v1` |
| `scripts/apply-jobs` | a declared cron job's conf | `config set` | raw text on stdin |
| `scripts/apply-immich-secrets` | one service's `env_file` | `secret set` | raw `KEY=value` on stdin |

All four are the same shape: **sops → memory → ssh stdin, never argv, never a laptop file, never a log** (design/00 D6). Supporting cast: `recreate-located` (a non-declarative repair loop over `service recreate`), `gen-syrvis-dashboard` (a seam *read* that writes repo files), `accept` (the post-write acceptance sweep, mutation-free), `validate-intent` (offline schema gate), `bootstrap` (workstation only — it explicitly refuses to run on the NAS), `immich-quiesced` (an app-plane pre-stop gate that touches no syrvis verb).

## Verb-usage inventory

**`scripts/deploy-stack`** (per-deploy; called by hand, by `wiki/runbooks/*`, and by the `deploy` skill):

| verb / argv | channel | caller | data | result use |
|---|---|---|---|---|
| `sudo -n /volume4/syrviscore/bin/syrvis deploy [--force] -- <name>` | seam write | `deploy_one`, deploy-stack:470–476 | **stdin**: `{"service": <manifest>, "configs": [{dest, content, secret?}], "secrets": {ENV: value}}` | exit-code gate; non-zero ⇒ `die()` printing the server's refusal **verbatim** + a `rollback_hint()` lookup. `TimeoutExpired` at `HOMEBASE_DEPLOY_TIMEOUT_S`=600 is its own failure class ("dockerd wedged; only `reconcile` recovers") |
| `/volume4/syrviscore/bin/syrvis history --json` (no sudo) | seam read | `fetch_history`, :256; `rollback_hint`, :440 | JSON `{"workloads": {name: [records]}}`, 30 s timeout | prints `rev N recorded (prev → new)` per service and synthesizes the exact `service rollback --to REV -y -- <name>` argv. Failure is swallowed (`return None`) — never fails a deploy |
| `POST https://grafana.konsume.org/api/annotations` | dashboard/edge HTTP | `post_deploy_annotations`, :383 | Bearer + CF-Access service-token headers, JSON body | **disarmed**: requires `HOMEBASE_GRAFANA_ANNOTATE=1` + `GRAFANA_SA_TOKEN`; ships dark because Grafana is OIDC-only and behind Access |

**`scripts/apply-instance`** (per intent change; runbooks `rs1221-standup.md:268`, `share-rename-window.md:16/33`, `synology-photos-sunset.md:46`):

- `sudo -n … export --json` — seam **sudo read**, `live_disabled()`:135 → `declarations{}`, used to build the `enabled:false` overlay. Unreadable ⇒ `({}, reason)` and a printed warning that the preservation guarantee did not run.
- `sudo -n … apply --dry-run [--allow-enable-change] --json` — always, stdin bundle. The guard flag rides both plan and write "or the plan is not a plan."
- `sudo -n … apply [--allow-secret-change][--allow-enable-change] --json` — only with `--apply`. Non-JSON output ⇒ exit with the raw server text (the `guard_enable_change` refusal *names the blocking services*, which is its whole value). JSON report is pretty-printed; `env.added`/`env.removed` are re-surfaced as a banner (design/70 P6).
- `… status --json` (non-sudo read) — `require_active_runstate()`:280, **only on `--converge`**; `runstate.state == "halted"` ⇒ refuse up front rather than half-converging.
- `sudo -n … stack apply` — converge step 1, exit-code gated.
- `sudo -n … reconcile --json -y` — converge step 2. **`reconcile --force` is deliberately not wired to a flag** ("a converge during a degraded array belongs in the window record, not on a laptop command line").

Flags in practice: `--allow-secret-change` for deliberate token rotation only; `--allow-enable-change` implies `--force-enable` locally (one switch, both sides); `--preserve-live-disabled` is an accepted **no-op** so a runbook can state the default it relies on; `-y` appears only inside the reconcile argv.

**`scripts/apply-jobs`** — `sudo -n … config set -- <name>` per job with a `jobs.d/<name>.conf.tmpl` (:176). Content is the rendered conf (comments/blanks stripped, `${TOKEN}` substituted via a per-token `SOPS_KEY_MAP` spanning five sops files). Result: exit-code gate; stdout is scrubbed of the OpenSSH post-quantum banner before printing. `--check` is an **offline** CI gate (`.github/workflows/verify.yml:114`) validating 5-field cron strings and boolean `enabled` with no SSH.

**`scripts/apply-immich-secrets`** — `… service list --json` (non-sudo read, `ConnectTimeout=6 BatchMode=yes`) as a stand-in for the retired cerebrate `test -d` that paged the owner; then `sudo -n … secret set -- <svc>` with `VAR=<sops value>\n` on stdin, for three targets. Largely **superseded**: `syrvis/stacks/immich/deploy.yaml` folds the same two secret refs into the bundle, but is annotated "NOT yet cut over" since 2026-07-31.

**`scripts/recreate-located`** — `… service list --json` (read, 45 s) then `sudo -n … service recreate -- <name>` serially (:212, timeout 330 s = the registry's 300 s + 30). Skips: not-listed, not-`running`, and shed (union of `config/maintenance-state.yaml` + NAS `intent.json` via `shed_reason`/`intent` fields on the list rows). Stops on first failure or timeout, and always ends by pointing at `./scripts/accept`. Cadence: manual, inside `share-rename-window.md` step 5.

**`scripts/gen-syrvis-dashboard`** — `… dashboard generate --all --json` (non-sudo read, 30 s). Validates every entry carries the `__syrviscore.generated` marker, injects a homebase-local `Maintenance` annotation lane, writes `<uid>.json` into `syrvis/stacks/monitoring/config/grafana/dashboards-syrvis/` and prunes stale managed files. `--check` is a drift gate returning 1.

**`scripts/accept`** — mutation-free, but it is where results are consumed. Three non-seam channels: (1) dashboard HTTPS `GET /api/services` with `curl --resolve <host>:443:<traefik_ip>`; (2) fallback `POST https://<traefik_ip>/api/ds/query` with `Host: grafana.konsume.org` + `GRAFANA_SA_TOKEN` for `docker_container_restart_count` (added the evening the dashboard went OIDC and started 401ing this sweep); (3) **raw shell over the `syrvis-reader` ssh alias** — a `for d in …; find "$d" -name secrets.env -size 0` probe with an `__SWEPT__` sentinel. Plus `verify-all --json` over eight explicitly named checks. Exit codes 0/1/2/**3=BLIND**.

**`immich-quiesced`** — app-plane only: sops-extracted `immich_family_api_key` → `GET /api/jobs`; exit 1 while any queue has `active` or `waiting`. The gate before any verb that stops `immich-db`.

## Interaction patterns

**Bundle assembly is entirely client-side.** `deploy-stack` discovers `syrvis/stacks/*/deploy.yaml`, asserts `stack:` matches the directory, joins each service key to `syrvis/services.d/<name>.yaml` (asserting `name:` matches), reads each `configs[].source` relative to the stack dir, expands `configs_glob` (sorted, **never secret**), and re-implements SyrvisCore's `_SECRET_MAX_BYTES = 65536` cap laptop-side so an oversize config fails in dry-run rather than mid-sweep on the live alerting path. `secret: true` configs carry `${VAR}` placeholders rendered from the stack's `secrets_source` **only at `--apply`**, with a convenience derivation (`X_URL` → `X_SERVER` + `X_TOPIC`).

**Ordering is a client-side convention with a hardcoded exception.** Declaration order in `deploy.yaml` is deploy order (stores before apps — see `romm`'s numbered comments), except that a full run stable-partitions `alertmanager` + `ntfy-alertmanager` to the very end, back-to-back (`ALERTING_PAIR_LAST`) after a 32-minute undelivered-alerts window on 2026-08-11. `--only` is exempt: the operator's typed order wins. `recreate-located` invents its own ordering — stores first by name suffix (`-db`, `-valkey`, `-redis`, `-postgres`, `-pg`, `-opensearch`).

**Change detection does not exist client-side.** There is no digest compare, no `--resume`, no `--since`. Every `--apply` streams every non-staged service's full bundle; the only scoping is `--only <a,b>`. Failure semantics are "abort at the first non-zero rc, remaining services untouched" — resumption is the operator re-deriving an `--only` list. The runbooks compensate with doctrine: "always `--only` what changed", "a deploy of unchanged bits is a NO-OP" (`.claude/skills/deploy/SKILL.md`).

**Staging is a git act.** `deploy-stack` reads each `services.d` file itself and skips `enabled: false` services ("the design/62 unpoller trap"), overridable only with `--include-staged`. `apply-instance` does the mirror-image thing: it *reads live state* (`export --json`) and overlays `enabled: false` onto git's declaration set, cross-referencing `config/maintenance-state.yaml` for a shed reason and flagging undeclared divergences. Both are client re-implementations of server guards (`guard_enable_change`, `guard_bulk_degraded`) that are deliberately redundant "and fail in the same direction."

**Result consumption is print-and-hand-off.** No applier parses `deploy`'s JSON beyond exit code; the revision story is a *separate* `history --json` read after the loop. Every script ends by naming the next human step: `./scripts/accept`, `verify-all nas.monitoring nas.drift`, "restart X to pick up the env_file", "hold ≥5 min for one vmalert cycle."

## Workarounds & missing verbs

- **No filesystem verb.** The seam has none, so `accept` uses a *fourth* identity (`syrvis-reader`) running raw `find` over a hand-derived root list; the deploy skill's stated remedy for anything harder is "ask for ONE batched cerebrate session."
- **No config prune.** A bundle only writes; SyrvisCore never removes a config that left the bundle. The monitoring `deploy.yaml` KILL LIST documents seven boards that survive in the live console until an **owner hand-deletes files on the NAS**, and the retire-the-SyrvisCore-folder plan's step 3 is explicitly "the one part no deploy can do for you."
- **The 64 KiB per-config cap** forced `rules-critical.yml` into five files, split `nas.json` into `io-array.json`, and left the generator's own `syrvis-overview.json` (127 KB, 64,056 bytes minified) permanently un-deployable in `dashboards-syrvis/attic/` — `configs_glob` was retired rather than fixed.
- **Mode is a one-way door.** `_place_config` refuses to overwrite an existing 0600 file with a 0644 one, and dry-run never renders secret content, so `secret: true` mistakes surface only at `--apply` on live paths. The `alertmanager` block is 25 lines of prose standing in for a missing "what uid will read this" check.
- **No `depends_on`/health**: `ALERTING_PAIR_LAST`, `_STORE_SUFFIXES`, and "declaration order = deploy order" are three separate hand-rolled substitutes; the journal records `started`, never `healthy`.
- **No timeout affordance**: `DEPLOY_TIMEOUT_S` exists *because* wrapping the script in `timeout(1)` caused a 1.5-hour self-inflicted outage (agent-traps T3).
- **Whole-file `.env` replace** means a key must be declared in `render/nas_env.py` or the next apply destroys it (`NTFY_URL` silently disarmed the boot-failure notifier), and a secret whose sops home is the "wrong" file makes apply-instance hard-fail by design.
- **Transport noise**: three scripts independently strip the OpenSSH post-quantum banner or parse "from the first `{`".
- **Six copies** of the same `SEAM_SSH_CONFIG`/`SEAM_TARGET`/`SYRVIS_WRAPPER` constants, `sops_get`, ssh runner and JSON-hardening code, with divergent timeouts (30/45/60/300/600 s, some unbounded).
- **Dead/blocked features**: Grafana deploy annotations complete but disarmed on two credential blockers; `apply-immich-secrets` superseded-but-live; `secret set` has no companion "now re-read it" verb — that gap is why `service recreate` (0.5.14) went uncalled by anything until `recreate-located` was written on 2026-08-16.

## Observations for a v2 agent design

1. **One transport, one client.** A single typed device client (bundle build, stream, exit-code + JSON contract, timeouts, banner-free framing) would delete six divergent copies and normalize the 30/45/60/300/600 s timeout zoo into declared per-verb budgets the agent publishes.
2. **Make the agent own ordering.** `depends_on` with readiness, plus reversed-band stop ordering, would retire `ALERTING_PAIR_LAST`, `_STORE_SUFFIXES`, and "declaration order = deploy order" — three client heuristics that each encode one incident.
3. **Server-side change detection with a plan the client can read.** A `plan` verb returning per-service `{unchanged | config-changed | secret-changed | image-changed}` would give `--only` a computed answer, make `--resume` trivial, and end the "I redeployed it" non-repair.
4. **Prune must be a first-class outcome.** Declared config sets should converge, with removals reported and gated — not left as an owner hand-delete.
5. **Size, mode, and mount-target validation belong server-side, at plan time.** The 64 KiB cap, the 0600-vs-container-uid trap, and "does this `dest` land where a mount reads" (which *nothing* validates today) are all pre-flight facts the agent has and the client guesses at.
6. **Intent must be mergeable, not replaced.** The whole reason `apply-instance` reads `export --json` first is that `apply` is a replace set with no per-key merge; durable intent (shed) plus field-level ownership would remove the client-side overlay entirely.
7. **Give the agent a scoped filesystem/attest surface** (zero-byte `secrets.env`, app-home presence, root census) so acceptance stops needing a second SSH identity and a raw `find`.
8. **Deploy should emit events, not require a follow-up read.** Revision, previous/new image, and an annotation-ready record on the deploy response would collapse `deploy` → `history --json` → print into one round trip, and give the disarmed Grafana lane a credential-free path.
9. **Keep verbatim refusals.** Both appliers go out of their way to print server guard text unmodified because the named services *are* the value; a v2 agent should return refusals as structured `{guard, blocking: [...], override_flag}` so clients can render them without re-deriving.