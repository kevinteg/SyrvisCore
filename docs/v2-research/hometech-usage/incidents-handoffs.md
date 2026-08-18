# Incidents & handoffs (usage under fire + wished-for verbs)

## What this slice is

The incident corpus is where home-tech's use of SyrvisCore is least idealized: one full incident record (`wiki/incidents/2026-08-16-cold-boot-share-collision.md`, 293 lines, with an appended adversarial critique and a `## Recurrences` table), the five runbooks that incident *manufactured* (`seam-dead-after-boot.md`, `post-boot-verification.md`, `syrviscore-share-collision.md`, `degraded-operations.md`, `share-rename-window.md`), the 2026-08-14 self-inflicted deploy outage recorded as `design/60` §0, the `/incident` + `/deploy` skills, the numbered trap ledger `wiki/resources/agent-traps.md` (T1–T15), and the 2026-08-16 adversarial design review (`design/handoffs/design-review-2026-08-16/`, 293 findings, of which the `agentic-response.md` lens is entirely about verb usage under fire). `MONITORING-HANDOFF.md`, `OPUS-HANDOFF.md` and `FABLE-HANDOFF.md` are historical session records; `reflections/` is empty.

The slice's central fact: **on 2026-08-16 the seam and the platform were the casualty, not the instrument.** Every rung of the response ladder is therefore written around what the seam *cannot* answer, and the wished-for verbs are unusually explicit because someone had to work without them for 50 minutes and then again for 3 hours.

## Verb-usage inventory (exhaustive)

Channel key: **SEAM** = `ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- '[sudo -n] /volume4/syrviscore/bin/syrvis <verb> --json'`; **READER** = `ssh syrvis-reader -- '<cmd>'` (unprivileged, never pages); **BG** = break-glass `cerebrate@ds` (pages the owner per login); **MCP**; **DASH** = HTTP through the Traefik macvlan `192.168.8.4`; **LOCAL** = on-NAS root cron / rootfs.

### A. The 2026-08-16 incident timeline itself (`incident:53-104`)

| verb / argv | channel | caller | cadence | data | what the caller did with it |
|---|---|---|---|---|---|
| `syrvis shutdown --reason maintenance` | SEAM (sudo) | `incident:59`, `ecc-dimm-slot-isolation.md:15` | pre-halt, manual | JSON: 0 failed, 0 forced | proceeded to power-off + DIMM swap |
| `syrvis status --json` | SEAM read (no sudo) | `incident:91` | first probe, then repeatedly | denied → later OK | denial *misread* as shell-revert; runstate later used as G0 |
| reader SSH probe | READER | `incident:91` | ~05:05 | `Permission denied` | should have been the discriminator (both identities denied ⇒ R3); wasn't |
| `curl -sk https://192.168.8.3:5001/`, `:5000`, `ping` | LAN, not seam | `incident:93` | 05:15–05:20 | 200/200, 9 ms | *green lie* — DSM lives on the rootfs and survives the rename |
| `syrvis history --json -- vector\|immich-db\|docker-socket-proxy\|onyx-background` | SEAM read | `incident:94`, `:69` | wave 2, ~05:43 | per-revision `location:` | **the single read that broke wave 2 open** — `location: /volume6` produced the volume6 hypothesis |
| `syrvis logs -n N -- <6 services>` | SEAM read | `incident:95` | wave 2 | container logs | read the postgres "uninitialized… password not specified" refusal → predicted self-heal |
| `syrvis verify --json` | SEAM read | `incident:95` | once | platform self-audit | added nothing — 22 validators, none enumerates a volume root (`incident:217-219`) |
| dash `GET /api/services` | DASH | `incident:96` | sweeps | restart counts | the truthful read plane; used *ad hoc*, not as the acceptance gate — the review's [medium] finding |
| ntfy topic polls; background watchers (seam poll ×3, fleet poll) | out-of-band | `incident:96-97` | 10-min watcher | pages | reclassified as **failure #1**, not a win (`incident:129-133`) |
| `syrvis resume --json` | SEAM (sudo, mutating) | `incident:98`, `:67` | once, ~05:30 | "core + all 37 clean" | **caused the second wave** — convergence onto an absent root = create |
| `syrvis service stop -- onyx-opensearch` + `service start` | SEAM (sudo ×2) | `incident:99`, `:72` | once | — | worked, but wrote declared intent twice; now forbidden (`syrviscore-share-collision.md:95-101`) |
| `head -2 /proc/meminfo`, `ls -d /volume*`, `df -h`, `cat /proc/mdstat`, `ls /volume4 /volume5/syrviscore /volume6` | BG | `incident:100-102` | owner, 05:20 | text | **the owner found root cause with plain `ls` before the agent did** |
| `/volume4/syrviscore_1/bin/syrvis status` | BG | `incident:102` | once | version resolves at the renamed path | proved RENAMED ≠ uninstalled |
| `sudo find /volume6/syrviscore -type f` | BG | `incident:103`, `:70` | gate before `mv` | 4 `secrets.env`, 0 data | the response's best move — authorized the destructive `mv` |
| `sudo mv` ×4 | BG | `incident:104` | both waves | — | the repair; **no seam verb exists for it** |
| `sudo /volume4/syrviscore/jobs/seam-selfheal` | BG (script inside `$SYRVIS_HOME`) | `incident:66`, `:104` | once | — | restored `/etc/passwd` shells → seam back |

Tally: **3 mutating seam calls, ~13 seam/dash reads, 9 break-glass shell commands** (4 of them `mv`) — and the repair itself was 100% break-glass.

### B. The 2026-08-14 deploy outage (`design/60` §0)

`deploy-stack monitoring --apply` wrapped in `timeout 500`, then `timeout 550`, then unwrapped (12:35 / 12:45 / 13:00); `deploy-stack onyx --only onyx-background --apply` → "compose up timed out" (13:26); from 13:30 **every seam verb hangs at 120 s, including read-only ones** because the CLI builds a docker client at startup regardless of verb. Result: `nas-heartbeat` and `alertmanager-watchdog` left *stopped*, monitoring dark ~1.5 h, and `restart: unless-stopped` structurally unable to recover them — only `syrvis reconcile`. Now trap **T3** (`agent-traps.md:74-86`) and the `deploy-stack` interlock row (`index.md:188`).

### C. Codified incident ladders (post-incident, now the mandated argv)

| step | verb / argv | channel | source |
|---|---|---|---|
| R2 "ONE reader round trip" | `ssh syrvis-reader -- 'ls -d /volume*/syrviscore*; ls -l /volume4/syrviscore/current; grep -E "^(syrvis-operator\|syrvis-reader):" /etc/passwd'` | READER | `seam-dead-after-boot.md:96-102` |
| R2 repo-side | `./.venv/bin/python scripts/verify-all data.file-plane nas.accounts nas.jobs` | READER-backed | `:112` |
| R4 pre-canned block (7 commands, none touching `$SYRVIS_HOME`) | `ls -d /volume*/syrviscore*` · `ls /volume4 /volume6` · `synoshare --get syrviscore` · `/proc/mdstat` · `df -h` · `/proc/meminfo` · `grep /etc/passwd` | BG, ONE session | `:154-164` |
| G0 | `syrvis status --json` → `runstate`, or MCP `status` | SEAM read / MCP | `post-boot-verification.md:64` |
| G1 | `/proc/meminfo`, `/proc/mdstat`, `df -h` | READER or BG | `:77-81` |
| G2 | `ls -d /volume*/syrviscore*` (+ `grep '^location:' syrvis/services.d/*.yaml` to re-derive roots) | READER | `:95-121` |
| G3 | `grep /etc/passwd`; `id syrvis-operator` (group `docker`) | READER | `:160-161` |
| G4 | `sudo -n … syrvis status --json` | SEAM | `:176-178` |
| G5 | `sudo -n … syrvis resume --json` | SEAM (mutating) | `:191-194` |
| G6 (a)(b)(c)(d) | dash `/api/services` restart counts · `find /volume{4,5,6}/syrviscore -name secrets.env -size 0` · two verify-all slices · hold 5 min for one vmalert cycle | DASH + READER + laptop | `:226-264`; now one invocation `./scripts/accept` |
| repair | `sudo mv …_1 → …`; `sudo jobs/seam-selfheal`; then `syrvis service recreate -- <name>` | BG then SEAM | `syrviscore-share-collision.md:82-101` |
| degraded read ladder (rungs 0–8) | `cat config/maintenance-state.yaml` → `service list --json` (`intent`/`shed_reason`) → `status --json` runstate → breakers ⏳ → `bringup.json` ⏳ → `deploy-journal.json` ⏳ → dash `/api/services` → `/proc/mdstat` → verify-all/Grafana | mixed | `degraded-operations.md:36-44` |

### D. Window execution (incident-adjacent, same verbs under pressure)

Share-rename window (`share-rename-window.md:29-73`): `./scripts/apply-instance --apply` (writes `SYRVIS_APPS_ROOT_NAME` into `.env` — must precede the move) → SEAM `shutdown --reason maintenance --json` → **owner DSM UI rename + one cerebrate paste** (`sudo mv /volume6/syrviscore …`, `ls -d`, append the `*/10 boot-integrity` crontab line, `systemctl restart crond`) → SEAM `syrvis verify` (`check_apps_root`, `check_home_collision`) → SEAM `resume` → `service list --json` filtered on `location` → `service recreate` per service → `./scripts/accept`.

Window close (`window-close-2026-08-24.md:247-310`): read `service list --json` (no sudo) → `sudo -n … service unshed -- <name> --json` ×14, unrolled (T14: loop bodies fail in the harness) → `./scripts/accept --quick` as the pre-recreate interlock → `sudo -n … service recreate -- <name> --json` in five dependency waves → `verify-all nas.expected-down nas.enabled-drift nas.drift`. Rollback = `service shed --reason … --until <ISO>`; platform rollback = `syrvisctl activate 0.5.16`.

### E. MCP + local-cron channels in the incident context

MCP (`.mcp.json`, server `syrviscore`): `status`, `deployment_history`, `reconcile_plan`, `service_rollback` (two-call HMAC handshake) — `deploy/SKILL.md:36-44, 88-100`. LOCAL: `jobs/seam-selfheal` (`*/5` root cron, inside the tree it heals); `jobs/nas-heartbeat` with an `rc=127` branch that fail-pings "syrvis binary missing at /volume4/syrviscore/bin/syrvis" — *structurally unreachable*, because cron could not exec the script containing it (`incident:172`); `rootfs/boot-integrity` (`*/10`, rootfs, asserts no `_N` sibling, `bin/syrvis` executable, `current` resolves, managed crontab block present, pings a dedicated Healthchecks dead-man); `/usr/local/etc/rc.d/S99syrviscore.sh` → `syrvis-startup.sh` (0.5.12 added the reclaim guard, inline `/etc/passwd` seam heal, and an ntfy `else` branch reading `/usr/local/etc/syrviscore-boot.env`).

## Interaction patterns

1. **Verbs are argv-literal, and the shape is a trap surface.** Absolute path mandatory on every verb; read verbs take **no** `sudo` except `export` ("a sudo read verb"); `stack apply` takes no `--json`; `schedule list` needs sudo; a bare `syrvis …` returns "not on allowlist", which reads exactly like a stale shim (T7, `seam-dead-after-boot.md:32-33`). HANDOFF.md item 18 still carries an unresolved "seam `logs` verb argv mystery (worked once, rejected elsewhere)".
2. **Client-side orchestration is the norm.** Dependency ordering for the 14-service bring-back is a hand-written five-wave table in a runbook, executed one `service recreate` per SSH round trip, with the *list resolved at execution* from `service list --json` because any list typed into a runbook goes stale.
3. **Acceptance is assembled from three planes** because no single verb can attest: dash HTTP for `RestartCount`, reader `find` for zero-byte `secrets.env`, laptop `verify-all` slices, plus a wall-clock hold for one vmalert cycle. Four bespoke acceptance lists were written in one week before `scripts/accept` consolidated them.
4. **The declared-intent layer was invented client-side first** (`config/maintenance-state.yaml` with `device_intent`, `windows[].forbidden_verbs[]`, `shed[]`) and only afterwards became platform truth (`intent.json`, 0.5.15). The repo file is now explicitly a **mirror**; a row cleared only in the repo changes nothing on the box.
5. **Break-glass is batched, not rationed** — the R4 block is pre-canned precisely because each `cerebrate` login pages the owner (T15), so the cost model is per-*session*, not per-command.

## Workarounds & missing verbs (the negative space)

- **The seam has no filesystem verbs** — stated in four places, with the full verb set enumerated as prose: `apply backup_list check_updates export images info profile_list reconcile restart resume service_catalog service_list stack_apply stack_hostnames start status stop verify verify_fix verify_smoke versions_list vm_list` (`immich-nvme-flip.md:63-68`). Every `ls`, `mv`, `find`, rsync step in every relocation and repair runbook is therefore an owner root shell.
- **R2's one reader round trip is literally a specification for an API call**: three facts (volume-root census, `current` symlink target, seam-account shells) that no verb returns, obtained by shelling as a second identity. R4 is the same spec with four host-physics facts added.
- **No `docker exec` verb** → out-of-band `amtool silence add` is a cerebrate session (`external-watchdog-setup.md:634`). **No container-remove verb** → `docker rm -f` on a zombie is explicitly owner-only, "no seam verb… no way to reach it through the operator seam" (`docker-health-troubleshooting.md:88-92`). **No `service restart` verb** at the time → `immich-restore.md:134` hand-rolls "stop → start, one name per call". **No ad-hoc `schedule run`** (`authentik-standup.md:151`). **No metrics-push verb** → `maintenance-mode`'s window metric prints SKIP by construction (`window-close:232-234`). **No verb enumerates crontab entries outside the managed block** — the gap that let a DSM task point elsewhere unnoticed (partially closed by `schedule dsm-tasks`).
- **Verbs that lied, forcing substitute read planes**: `syrvis status` blind to crash loops (T13) → dash `/api/services`; `resume`'s own "all 37 clean" → `scripts/accept`; `syrvis verify`'s 22 validators, none of which looks at a volume root, and all of which need the install dir that moved.
- **`syrvisctl doctor` exists only because nothing could answer without a resolvable home** — and its name now collides with `syrvis doctor` badly enough to need a FACTS row at 3 a.m. (`seam-dead-after-boot.md:36`).
- **Rootfs artifacts are outside the seam**: `boot-integrity` had to be installed by owner paste to `/usr/local/bin` ("third time is the charm" on the crontab line), and the deployed copy went stale against the `syrviscore-apps_N` pattern the same week.
- **Lockstep coupling as friction**: any library change touching the edge schema requires dashboard rebuild + digest repin + release, or a stale dashboard reclassifies running services `unmanaged`.
- **Two channels disagreeing with no arbiter**: breaker state claimed by three planes with two reset verbs and no union view (`00-SYNTHESIS.md:113-116`).

## Observations for a v2 agent design

1. **Ship the census as a verb.** `ls -d /volume*/syrviscore*` is the single most-cited command in the corpus — G2, R2, R4 step 1, `syrviscore-share-collision.md` step 0, T5, T6, the autonomy matrix. A v2 agent must expose a **roots/integrity read that works with no resolvable home**, returning declared-vs-actual roots, collision siblings under *every* configured segment name, `current` resolution, seam-account shells, and array/volume physics — i.e. R4's whole block as one typed response. That deletes the majority of break-glass pages in this corpus.
2. **Convergence must be transactional and stat-aware.** "A convergence verb is a WRITE; absent means create" is the repo's loudest rule and it is a *client-side* rule enforced by prose. v2 should refuse-by-default: preflight the declared roots, high-water-mark app homes, and never materialize an `env_file` for an installed service.
3. **Interlocks belong in the agent, not the runbook.** home-tech maintains a ten-row interlock table and a failure-class autonomy matrix in markdown. Each row is a machine-checkable precondition (roots present, no open shed, `mdstat` quiet, secrets non-empty, deploy not externally signalled). `guard_enable_change` / `guard_bulk_degraded` proved the shape works — generalize it, and make `--force` always journaled.
4. **Truthful status is a v2 requirement, not a nicety.** `restart_count`, `StartedAt`, health, `flapping`, `blocked_by`, `shed_reason` must come from the same call the operator already makes — home-tech only reaches around to the dashboard's HTTP API because `status` was blind.
5. **Reads must survive the failure they diagnose.** Two separate incidents ended with the diagnostic plane co-fated: dockerd wedged ⇒ *read-only* verbs hang 120 s; a root rename ⇒ wrapper, cron healers and boot hook all dead. A Go host agent should keep a rootfs-resident, docker-free read path with hard per-verb deadlines and a distinguishable *denied* vs *hung* vs *degraded* return.
6. **Give bulk operations a server-side plan.** The five-wave `unshed` + `recreate` sequence, "stores before consumers, resolve the list at execution", is exactly a dependency-ordered batch the device should compute and execute, reporting per-item outcomes — not 14 sequential SSH round trips driven by a markdown table.
7. **Make acceptance a verb.** `scripts/accept` is a client-side composite of dash HTTP, a reader `find`, two verify-all slices, and a timed hold. A v2 `accept`/`attest` verb returning that bundle would let the closing gate live where the facts are.
8. **Keep the deliberate boundary explicit.** "No filesystem verbs" is documented as a *deliberate* trust boundary, not an oversight — so v2 should widen the **read** surface aggressively (census, find-by-predicate, digest/size attestation) while keeping arbitrary writes owner-only, and treat "the owner had to open a session" as the metric to minimize.