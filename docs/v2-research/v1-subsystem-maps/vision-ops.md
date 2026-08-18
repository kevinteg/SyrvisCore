# The Vision — Operations, Orchestration & Trust (home-tech design corpus, the doctrine SyrvisCore implements)

## Purpose & role in the system

SyrvisCore is the *mechanism*; the home-tech design corpus is the *doctrine*. home-tech is the deployment repo that owns the domain, the DNS, the service catalog and — critically — the operational philosophy that SyrvisCore's generic verbs exist to execute. The boundary is stated repeatedly as design/15: "SyrvisCore ships the **generic mechanism**; home-tech **declares intent**… SyrvisCore never names a homebase job" (20 §4). Every SyrvisCore feature of the 0.5.x line traces to a numbered decision in this corpus, and almost every one traces to an *incident*.

The doctrine, in one line, is design/00's: **declare → reconcile → verify, and drift is loud**. Around that spine the corpus adds four newer axes: (a) *deploy orchestration and velocity control* (60), (b) *dependency-ordered lifecycle* (63), (c) *first-class intent* — device and per-service (65), and (d) a *host agent* that owns "when" while the platform owns "what order" (61). 66 and 70 are the trust-boundary tail: the seam is no longer only about a human operator, it is about an *agent* with a seam key.

## Key modules and files (path — role — approx size)

All under `/Users/kevinteg/code/home-tech/design/`:

- `20-config-management.md` — the origin of GitOps-for-Synology: closing the jobs/data reconcile seams; explicitly rejects Ansible/Salt/NixOS/ArgoCD/Terraform-on-box; the config-render invariant — 255 lines
- `21-l2-service-deployment.md` — the `syrvis deploy` bundle contract (`syrvis-bundle` schema ownership, `deploy-stack` discover→assemble→stream, `deploy.coverage`) — 306 lines
- `22-privileged-l2-tier.md` — `tier: infra`, authorship-gated host-mount allowlist, the docker-socket sharp edge + its 2026-08-16 *availability corollary* — 258 lines
- `25-deployment-lifecycle-adoption.md` — adoption of 0.5.0 revisions/rollback/runstate/`shutdown.priority` bands; D4's band table (20/30/50/70/90) — 245 lines
- `28-ups-graceful-autoshutdown.md` — UPS doctrine: DSM NUT owns detection, SyrvisCore owns ordered flush; Option A rc.d hook; the budget arithmetic amendment — 468 lines
- `55-lan-traefik-posture.md` — `exposure:` is *not* a security control; T1–T6 rulings incl. T5 (hostd split auth) — 210 lines
- `57-ops-console-prominence-and-syrvis-summary.md` — `syrvis-summary/v1`; **D11's seven-word status vocabulary** — 348 lines
- `60-deploy-orchestration-and-velocity-control.md` — the 2026-08-14 incident, R1–R7, change detection, deploy journal D6, §11 backoff/breaker doctrine (normative for 60/61/63) — 717 lines
- `61-host-agent.md` — `syrvis-hostd`: static Go rootfs daemon, checks registry, A–F gate, flight recorder, heal absorption, `job-wrap` — 1320 lines
- `63-service-dependencies-and-ordered-bringup.md` — `depends_on`, graph solver, `syrvis up`, `restart: no` flip, reverse-topo shutdown budgets — 1211 lines
- `65-device-lifecycle-intent.md` — `syrvis-intent/v1`, `drain`/`restore`, boot-converges-to-intent, `shed[]` — 683 lines
- `66-agent-permission-plane.md` — whitelist-not-blacklist for `.claude/settings.local.json` — 77 lines
- `70-env-value-trust-boundary.md` — `.env` value/key hazard rejection at every writer — 89 lines
- `handoffs/design-review-2026-08-16/00-SYNTHESIS.md` + `01-ACTION-ITEMS.md` — nine-reviewer adversarial pass, ~250 findings, seven cross-cutting themes — 12 KB / 33 KB

## How it actually works (mechanisms, data flows, state files & their schemas, algorithms)

**GitOps apply/deploy.** Three declaration planes with distinct completeness (20 §3): `services.d` (declare/reconcile/verify), `jobs.d` (install gap — closed by `schedule sync` from a root-configured `jobs.source` + `apply-jobs` rendering confs via `syrvis config set`), `data.d` (verify gap — closed by `nas.backups`). Deployment is a **projection, never hand-written** (21 §3): `syrvis/stacks/<stack>/deploy.yaml` names `configs[].source/dest` and `secrets[].sops→env`; `scripts/deploy-stack` decrypts sops **in memory**, assembles `{service, configs:[{dest,content}], secretValues:{}}` and streams one stdin doc per service to `sudo -n syrvis deploy -- <name>`. Secrets travel memory → ssh-stdin → env_file 0600, never argv, never a laptop file (design/00 D6). SyrvisCore owns the `syrvis-bundle` *schema* + apply; home-tech owns *discovery + resolve* (21 §3.5).

**Privileged tier.** `tier: ""|"infra"` with `INFRA_HOST_MOUNT_ALLOWLIST` = `/proc`, `/sys`, `/` , `/var/run/docker.sock`, all `:ro`; the trust anchor is **authorship**, `OPERATOR_AUTHORED_PREFIXES = ("services.d:", "deploy:")`, re-asserted inside `deploy_bundle`'s update path (finding N1) because that path bypasses the install chokepoint.

**State files under `$SYRVIS_HOME/data/state/` — one directory, one convention** (60 D6.1):
- `runstate.json` — v1, absence == active, `{reason ∈ ups|reboot|maintenance, resume_on_boot}`
- `intent.json` — `syrvis-intent/v1`: `{schema_version, device: in-service|drained, shed:[{service, reason, since, until?}]}`; **shipped 0.5.15**
- `deploy-journal.json` — `{schema_version, run_id, started_at, finished_at, by, pid, services{name:{state, revision, at, breaker}}, events[]}`; terminal set `{healthy, skipped, failed}` with **`failed` terminal**; 60-minute staleness; unparseable ≠ absent; **shipped 0.5.16**
- `breakers.json` — one row per `{plane ∈ deploy|recovery|agent, context}` with `{state, consecutive_failures, opened_at, last_probe_at, next_probe_at, reason, by}`; **shipped 0.5.16, recording only**
- `bringup.json` / `bringup-pending.json` / `bringup.lock` — **design only** (63 D9/M2)

**The backoff/breaker doctrine (60 §11.1, RATIFIED, normative for 60/61/63):** one curve `delay = min(base × 2^attempt, 10 minutes)` with full jitter, per-context base, universal cap; a breaker after N=3 consecutive failures; half-open probe at the cap; states as *data*, never inferred; the open transition pages **exactly once**; humans reset by intent. Point 6 (the arbitration rule) makes `breakers.json` the single owner, journal/`bringup.json`/hostd `/status.breakers` mirrors, cross-plane suppression, "a close closes all", and `--by <surface>` as a **field** — only `cli:*|seam:*|mcp:*` closes; `hostd|s99|cron` inherit.

**Ordering algorithm (63).** `depends_on: ["name[:readiness]"]` with `started` (default) / `healthy` / `soft`; parse splits the suffix *before* name validation; unknown suffix is an error, never a default. `build_dependency_graph` runs whole-set after per-file load: cycles, unknown targets, self-edges, `MAX_DEPENDS_ON = 12`, `healthy`-onto-checkless all invalidate **only the declaring file**. A hard edge onto a disabled/shed target is a plan-time `blocked` **bucket**, not a validation error (the `opc:F10` amendment — otherwise a partial load-shed would invalidate running consumers and reclassify them `unmanaged`). Plan order is Kahn topological with the *reversed shutdown band* as tie-breaker inside a wave; bring-down is reverse-topo waves with `max_in_flight_stop` 3. Shutdown budget is **reserve-first**: store wave 120 s + VM ACPI 90 s + core 30 s = **240 s irreducible reserve** held back *first*, consumers clamp into the remainder — against today's `timeout 150s` rc.d wrapper, i.e. an effective budget ≤ 135 s.

## Design decisions & their rationale (with citations)

- **No host-CM tool** (20 §2): Ansible/Salt "fight DSM — it owns its own config DB and *regenerates* `/etc/crontab`… And they need **broad root**, which breaks the design/11 least-privilege operator seam."
- **The config-render invariant** (20 §3, "non-negotiable"): a conf value is **inert data** — "never… a **command name**, a **path it later executes**, or an `eval`/`sh -c`/`source` argument."
- **One deploy verb, not N low-level verbs** (21 §2) — "Three calls where one belongs"; and "**No new escalation.** The operator can *already* run arbitrary root code via `service run --image *`."
- **`tier: infra` gated on authorship, not content** (22 §2): "A third-party repo can never escalate itself. This is the load-bearing rule."
- **Bands demoted, graph promoted** (25 D4 amendment / 63 §1.3): design/25 D6's "healthchecks/start_period absorb that" is struck — "The incident showed what 'absorb' costs," and "declaration order" was really `sorted(glob("*.yaml"))`, which starts `docker-health-exporter` before `docker-socket-proxy` and `onyx-api` before its three stores.
- **`restart: no`, not `on-failure`** (63 D4): rejected on determinate moby v24.0.9 evidence — an unclean host stop synthesizes `ExitCode = 255` (`daemon/daemon.go:419-429`) and `restartmanager.ShouldRestart` for `on-failure` "computes `exitCode != 0`… **ignoring `hasBeenManuallyStopped` entirely**", so `on-failure` restores the whole flipped fleet on every power-loss boot.
- **Skip-on-unchanged must not ship without its invalidation half** (60 §3.1 amendment, `dep:F8`) — today's "dumb always-push behaviour repairs [the vector/socket-proxy trap] by accident."
- **Async `up` with fd detach** (63 D3 amendment `opc:F7`): "`setsid` detaches the *session*; it does not close inherited file descriptors" — an SSH forced command holds the channel open, so the async design "silently degrades into the synchronous one it was written to replace."
- **Intent, not inference** (65 §0): "the halt *reason* (why we stopped) is standing in for the device *intent*"; `AUTO_RESUME_REASONS` is "the patch that papers over the conflation" and is **deleted**.
- **Whitelist the agent** (66 §2): "`deny` rules are defense-in-depth for direct spellings, not the primary control"; `Bash(python3 *)` was "arbitrary code execution… pre-approved," and 71 recorded seam calls used spellings none of the seven deny globs matched.
- **Reject, don't quote** (70 §2): "a refused apply… leaves the instance byte-identical and is a loud, local failure; the alternative (a root shell at boot) is not recoverable."

## Invariants & contracts (what other subsystems rely on)

1. **Secrets never on argv, never on laptop disk** — memory → ssh-stdin → 0600 env_file; `deploy` redacts `secretValues` from every echo and error (21 §6/§7).
2. **Server-side re-validation of all operator input** — shim charset-gates argv `name`, CLI re-validates manifest, config dests realpath-confined to `data/<name>/`, env keys via `ENV_KEY_RE` (21 §7); plus 70's `env_value_hazard`/`env_key_hazard`.
3. **One reconcile engine** (63 D7): "every future ordering feature… lands in `build_reconcile_plan`/`apply_reconcile_plan` or it does not land."
4. **Reader enumeration + rollback prohibition** (63 D2): a non-empty `depends_on` is invalid to every reader below the edge schema, so ship order is repo release → NAS active → **dashboard rebuilt/repinned** → then edges; and "while any edge exists in `services.d`, platform rollback below the edge schema is forbidden."
5. **Page ownership** (63 D9 / 61 §7): the *engine* pages a blocked subtree exactly once per bring-up; the *agent* pages only gate refusals, invocation failures and stalls. "One fact, one page."
6. **Exit 2 = `completed-with-blocked` is terminal** — callers (hostd) must never retry it.
7. **UNKNOWN is first-class and never a pass** (61 §3) — mirrored in verify-all and in 57 D11's vocabulary (`ok|warn|fail|unknown|shed|drained|maintenance`), where "the last three are **intent**: amber-with-reason at worst, never red, and never green either."
8. **`budget + issuance overhead ≤ wrapper/transport timeout ≤ measured DSM window`** (63 D6 / 28 §4) — "a budget the transport kills is fiction."
9. **Never wrap `deploy-stack --apply` in `timeout`** (60 §3.3) — the external SIGTERM *is* the 2026-08-14 incident.

## Implemented in SyrvisCore 0.5.x vs still design

**Shipped (verified in the repo):** `deploy` bundle + `deploy.coverage` pattern; `tier: infra` (0.3.21); revisions + `service rollback --to` + runstate + `shutdown --reason`/`resume`/`restart --graceful` (0.5.0–0.5.2); rc.d `stop` flush hook firing `timeout 150s syrvis shutdown --reason reboot` (0.5.2, hardened 0.5.4); boot-hook Docker-race fix (`DOCKER_MAX=600`, retried reconcile, ntfy on failure); `intent.json` with `device` + `shed[]`, `service shed/unshed`, `guard_enable_change`, `guard_bulk_degraded` (0.5.15 — `intent.py`, `guards.py`); reserve-first shutdown clamping, `vms.d`, bundle-digest compare fixing 21 §5's struck idempotence (0.5.15/0.5.16); `depends_on` schema + `build_dependency_graph` + topological ordering, `deploy_journal.py`, `breakers.py` **store only**, `volume_locations` (0.5.16); 70's `.env` writer hardening (0.5.17).

**Design only:** `syrvis up` (async, `--by`, instance `flock`), `bringup.json`/`bringup-pending.json`, per-node readiness gates, blocked-subtree failure policy, the recovery loop, the breaker *engine* (half-open timer, once-per-open page), `drain`/`restore` + the rootfs intent mirror, the `restart: no` flip, the S99 rehoming, the whole of design/61 (no `packages/hostd` exists), design/60 §4's lazy-docker `syrvis doctor` (`doctor.py` is still the old network/cert doctor), §9's `platform docker-restart`, §10's telemetry series, 66's `scripts/seam` + `docs.agent-permissions` check, 70's `parse-dont-source`.

## Gaps, debt & sharp edges

- **The release ledger has already slipped.** 63 §4 declares 0.5.17 = "63 M2 — gates, the instance lock, async `syrvis up`… the recovery loop." The actual 0.5.17 is design/70's `.env` guard. The corpus's own anchor rule ("if the live version is not 0.5.14, re-derive") is now load-bearing and nothing enforces it.
- **Breakers are recorded but never consulted.** `breakers.py`'s own docstring: "Scope at 0.5.16: this module ships the STORE and its arithmetic. The consuming engine… is design/63 M2." Cross-plane suppression, half-open probing and the once-per-open page are all *specified* and *unenforced* — a partially-built safety mechanism reads as a built one on every status surface.
- **G7 is the keystone nobody has measured.** DSM's rc.d-stop window is unverified (the code says so); the arithmetic says ≥240 s reserve against a 150 s wrapper. Until measured, every "ordered graceful flush" claim in 25/28/63 rests on a guard, not a proof — and 28's own risk text names the failure: "a store killed mid-checkpoint" is "worse than useless."
- **The UPS path may not trigger at all.** 28's addendum: the DSM-side toggles (Enable UPS support, Customize time, Restart-after-power-failure) "live in the DSM UI, are represented nowhere in this repo, and no allowlisted read verb exposes them… **Until it is, nothing triggers the flush on a real outage**."
- **The resurrection trap.** 20 §6's amendment: `nas.drift` "compares SETS ONLY," ~14 services live-stopped against one declaring `enabled: false`, and the check's *own remediation text* prints the command that resurrected the shed — twice on 2026-08-16. `--preserve-live-disabled` and `nas.enabled-drift` are amendments, not code.
- **Availability concentration is unpriced by design.** 22's §3 corollary: the socket-proxy is the sole Docker-API path for the container alarm plane *and* vector's log source, carries `critical: true` and **no healthcheck**, and an ~80 s outage "permanently kills vector's `docker_logs` source while vector's own process keeps reporting `running`."
- **Health gating is inoperable for 15 of 39 services** (60 §3.4 amendment) — the M3 healthcheck backfill (victoria-logs, docker-socket-proxy, romm-db, immich-machine-learning) is a prerequisite masquerading as a parallel workstream.
- **Two `doctor`s, one name** (60 §4 note, `opc:F8`) — `syrvis doctor` vs `syrvisctl doctor`, "ruled: no rename now," disambiguation deferred to a runbook FACTS row.
- **Compound hazard across designs**: 28's amendment names it — boot converging to `in-service` plus a git-sourced apply declaring all fourteen shed services enabled means "**the first boot after a git-sourced apply restores the load-shed set, unattended.**"
- **Design 61 is 1320 lines of unbuilt flagship**, gated on ~18 unresolved on-box probes (⚠P1/P7/P8/P12/P14/P17/P18), a first-ever Go toolchain in a Python house, and a `/usr/local` upgrade contract that is "verified, not assumed" with a six-canary harvest pending.
- **The intent mirror is write-only until 0.5.18** (65 D3 amendment) — D2's absolute "drained survives any boot class" is false in the window, closed only by a sentence.

## Raw material worth citing in the retrospective

- Owner's ask, verbatim (63): *"Ideally the container manager only brings up SyrvisCore layer 1 automatically and our agent can then initiate the bring up of the layer 2 services… Then it does the ordered service bring up."*
- Owner's ask (65): *"if the device is supposed to be drained, when it boots back up it stays drained."*
- Owner's ruling (60 §11): *"we should also have an exponential backoff with a max backoff limit of 10 minutes… make sure we provide visibility into when services are in that state."*
- 2026-08-14 timeline: 12:37 `nas-heartbeat` down, 12:55 `alertmanager-watchdog` down, 13:30+ every seam verb hangs at 120 s, 14:13 no self-recovery — and "**§3.1 → the monitoring apply restarts 1 service (vmagent), not 11**."
- The alphabetical-luck table (63 §0): lucky `immich-db < immich-server`; unlucky `onyx-api < onyx-opensearch`, `romm < romm-db`, `vector < victoria-logs`.
- Graph facts: depth 4, ~4 waves; only **6 of 39** declarations touch no edge; **24 of 39** declare a healthcheck.
- 63 §0.1's honesty about derivation: a mechanical scan produced a false positive (`romm-db → romm`) *and* missed two real edges — "a fine lint and a dangerous executor."
- The signed trades (63 D4, "SIGNED 2026-08-16, owner, in-session"): the ≤15-minute recovery SLO and the crash+decapitation trade — "neither is inherited-by-silence."
- 25 §1: "The homebase side has **zero** occurrences of history/rollback/runstate/graceful anywhere" — the platform moved a generation and the deployment repo didn't.
- 21's struck promise: ~~"Idempotent: an unchanged bundle is a no-op"~~ — **STRUCK 2026-08-16 (dep:F10)**.
- 55 §0: `exposure:` is "documented and implemented as *not* a security control (traefik_config.py:551-554) — `internal` and `tunnel` emit byte-identical routers."
- Review synthesis T1: "**No intent vocabulary: the single biggest systemic gap** — found independently by 7 of 9 reviewers."
- 61 §0's thesis sentence: "nothing on the box could answer 'what phase is this machine in, and what is actually true right now?' from outside the failure domain."
- 60 §10.5: the only thing that behaved as designed was the **external Healthchecks.io deadman** — "every rule proposed above lives on the same NAS it watches."