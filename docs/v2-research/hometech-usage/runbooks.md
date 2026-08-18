# RUNBOOKS (manual operator verb usage)

## What this slice is

`~/code/home-tech/wiki/runbooks/` — 62 markdown files that are the *human/agent* execution surface for the homebase. Unlike the cron plane (declared, materialized on the NAS) or the MCP plane (typed tools), these are prose procedures that tell an operator or an agent exactly which argv to type, on which channel, in which order, with which precondition. They are the densest record of what SyrvisCore's verb surface actually *is* in practice, because every gap in it shows up here as a break-glass step, a hand-rolled loop, or a warning box.

Four channels appear, and the runbooks classify them explicitly (`index.md` §FACTS, `seam-dead-after-boot.md` §FACTS):

| channel | shape | pages owner? |
|---|---|---|
| **SEAM** (operator) | `ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- '[sudo -n] /volume4/syrviscore/bin/syrvis <verb> --json'` | no |
| **READER** (scoped read identity) | `ssh syrvis-reader -- '<arbitrary read-only shell>'` | no |
| **BREAK-GLASS** | `ssh cerebrate@ds` / `ssh -t ds` | **yes, every login, even `ls`** |
| **DASHBOARD HTTP** | `curl -sk --resolve dash.konsume.org:443:192.168.8.4 https://dash.konsume.org/api/services` | no |
| (MCP) | referenced as an equivalent for `status` / the `/deploy` skill's `resume` row only | no |

Three invariants are repeated in nearly every file because each has cost an incident: **read verbs take NO `sudo`, write verbs take `sudo -n`**; **every verb needs the absolute `/volume4/syrviscore/bin/syrvis` path** (a bare `syrvis …` → `syrvis-mcp-shim: command not on allowlist`, which reads like a stale shim); and **`--json` is part of the allowlisted argv**, not a formatting choice.

## Verb-usage inventory

### Seam reads (no sudo)

| verb / argv | caller (file) | cadence | data flow → use |
|---|---|---|---|
| `status --json` | post-boot-verification G0/G4; seam-dead R0; degraded-ops rung 2; rs1221-standup:41,93,243; rs1221-migration-day:75,143,157,297; unifi-dropin-day:105,469,529 | per-boot, per-window, manual | JSON → `runstate` (`halted`/`active`), version resolution. Explicitly **not** trusted for health (blind to crash loops) |
| `service list --json` | degraded-ops rung 1; window-close §3.2/§3.3; share-rename-window step 5; data-plane-relocation §3–§7; onyx-restore §2; deploy-tunnel-service step 11; app-move | manual, always "resolve the list at execution" | JSON → `intent`/`shed_reason`/`location`/state. Runbooks forbid working from a list typed in the runbook |
| `history --json [-- <svc>]` | data-plane-relocation §3.5,§4,§5,§6,§7.3; immich-restore:62; rs1221-migration-day:78,325; window-close §7 rollback | per-move, per-deploy | JSON → newest revision's `"location"` field is the **authoritative confirmation a `location:` flip took**; also the rev id fed to `service rollback --to` |
| `logs [-n N] -- <svc>` | docker-health-troubleshooting §2/§4; window-close §6.4/§7 (V3); socket-proxy-acl §1; external-watchdog:428; host-sysctls:97; data-plane-relocation §3.5 | incident-only | text → cadence analysis (wedged vs slow inspect), `grep -c ' 403 '` as a pass/fail gate, template-fallback detection |
| `verify --json` / `verify --smoke --json` | data-plane-relocation §4,§6,§7.3; docker-health §4; share-rename-window step 4 (`check_apps_root`, `check_home_collision`) | post-change | exit/JSON gate |
| `schedule list --json` | schedule-cutover step 2; window-close §5/§6.5; cloudflare-gate-verification:129 | post-sync | JSON → managed crontab block, one line per job |
| `stack hostnames --json` | rs1221-migration-day:79,330; unifi-dropin-day:106 | migration baseline | JSON diffed against a baseline |
| `version --json` | window-close §4.2; env-value-safety:186 | per-release | string equality gate (`0.5.17`) |
| `reconcile --dry-run --json` | rs1221-migration-day:77; degraded-ops ("what you may do") | pre-flight | plan only; the only converge-family verb allowed while bulk-degraded |
| `schedule dsm-tasks` | jobs-pin-cutover §5 (drift-again branch) | incident-only | census of DSM's own tasks |

### Seam writes (`sudo -n`)

| verb / argv | caller | cadence | notes |
|---|---|---|---|
| `resume --json` | post-boot-verification **G5**; ecc-dimm §post-boot step 2; share-rename-window step 5; rs1221-migration-day:140,298; syrviscore-share-collision step 2 | per-boot after a halt | **The gated write.** Unreachable until G0–G4 pass. Exit 2 ⇒ `service start` + `reconcile` |
| `shutdown --reason maintenance --json` | share-rename-window step 1; ecc-dimm timeline; rs1221-standup:110; rs1221-migration-day:129,185 | per-window | halt holds across reboots by design |
| `shutdown --reason ups --json` | rs1221-standup:335 — wired into DSM's pre-Safe-Mode/NUT path | per power event | budget 180 s < seam timeout 240 s < NUT timer; auto-resumes at boot |
| `restart --graceful --json` | immich-restore:143; onyx-restore §1 note | manual | bring-up half does *not* honour bands |
| `service stop -- <name>` | data-plane-relocation ×10; app-move step 2; immich/onyx-restore; external-watchdog tests A/B/C; onyx-nvme-flip | per-move, per-drill | **one name per call**; a multi-name form is a usage error that stops nothing. Writes a live-only `enabled:false` |
| `service start -- <name>` | onyx-restore §1; immich-restore:140; external-watchdog:525,546 | per-restore | forbidden in every post-rename context (stored compose = stale binds) |
| `service recreate -- <name>` | syrviscore-share-collision step 4; window-close §3.3 (14×, five waves) + §7.3 (vector); share-rename-window steps 5/7; socket-proxy-acl:106; degraded-ops close step 2 | per-window, post-deploy | the only verb that re-reads a changed `env_file`; writes no declared intent |
| `service unshed -- <name> --json` | window-close §3.2 (14×) | window day | lifts durable intent; starts nothing |
| `service shed --reason R --until D -- <name>` | window-close §3.3 rollback; degraded-ops; index interlocks | incident | durable intent with expiry |
| `service rollback --to <rev> -y -- <name>` | immich-restore:65; deploy-tunnel-service step 8; socket-proxy-acl:154; window-close §7 | incident | GitOps-ephemeral — repo must be reverted too |
| `reconcile --json -y` | rs1221-standup:252; rs1221-migration-day:159,309; onyx-nvme-flip §B.5 | recovery | the only recovery from a killed `deploy-stack` |
| `start` (instance) | rs1221-standup:251,270 | recovery | recreates the macvlan net |
| `schedule sync --json` (bare) | schedule-cutover step 1; alerting-integrity:21; external-watchdog:114,121,484; host-sysctls:32; authentik-standup:141; window-close §5.5 | per job-change | post-0.5.17 = re-materialize the *pinned* commit; `changed_scripts: []` + `pin_written: false` is the proof the remediation path can't install a push |
| `schedule sync --to <40hex> --manifest sha256:<64hex> --json` | jobs-pin-cutover §3/§4.2; window-close §5.2 (zeros smoke test), §5.3 | per pin advance | **read `changed_scripts` before accepting** — a name you didn't touch = you are installing someone else's root code |
| `schedule apply --json` | schedule-cutover step 4 | after DSM task edits | `changed:false / in_sync:[…]` is the assurance |
| `deploy` (stdin bundle) | operator-shim-reprovision verify (`echo '{}' | … syrvis deploy -- probe`); driven by `scripts/deploy-stack --apply` | per-deploy | stdin `syrvis-bundle/v1` |
| `apply` (stdin bundle) | driven by `scripts/apply-instance --apply [--converge]` — bbq-app-standup Phase 3/5, share-rename-window step 1, window-close §4.4 | per instance change | `syrvis-instance/v1`; `_render_env` rewrites `config/.env` **whole** |
| `config set -- <job>` | schedule-cutover §Job confs; driven by `scripts/apply-jobs --apply` | per conf change | sops → memory → ssh-stdin, never a file on disk |
| `secret set -- <name>` | referenced in operator-shim-reprovision's verb history | rare | stdin |

### Reader-identity reads (the "seam has no filesystem verbs" channel)

`ls -d /volume*/syrviscore*` (the roots census — seam-dead R2, post-boot G2, share-collision step 0, share-rename step 3); `ls -l /volume4/syrviscore/current`; `grep -E '^(syrvis-operator|syrvis-reader):' /etc/passwd`; `cat /proc/mdstat`; `df -h`; `head -2 /proc/meminfo`; `find /volume*/syrviscore* -name secrets.env -size 0`; `du -sh <app home>`; `ls -d /sys/fs/cgroup/memory/docker/<id>*`; `cat /proc/sys/vm/overcommit_memory`; `find /volume4/syrviscore/backups/immich-db …`. Cadence: every boot, every degraded-ops rung 7, every acceptance sweep.

### Break-glass (`cerebrate@ds`) — and the seam gap each one evidences

| step | runbook | seam gap |
|---|---|---|
| `mv /volume4/syrviscore_1 /volume4/syrviscore`; `mv` scaffold aside | syrviscore-share-collision 1/3; share-rename-window 3 | no filesystem verbs |
| `sudo /volume4/syrviscore/jobs/seam-selfheal` | share-collision 1 | no `seam heal` verb reachable when the seam is denied |
| `mkdir -p` + `rsync -aHAX --numeric-ids` + `mv OLD OLD.pre-move` | app-move 3–4; data-plane-relocation R1–R4/R2b; immich-nvme-flip 4–5; onyx-nvme-flip | no data-move verb; the `location`-change refusal has no sanctioned bypass |
| `docker exec -i <pg> psql … -v ON_ERROR_STOP=1` | onyx-restore §1; immich-restore §A/§B/§D | `docker exec` is not allowlisted — DB restore is *structurally* break-glass |
| `docker ps -a --no-trunc`; `docker rm -f <id>` | docker-health-troubleshooting §3 | no container-removal verb; explicitly "nothing in this repo may run it" |
| `docker inspect -f …` for Mounts / proxy IP | app-move 7; immich-nvme-flip 9; window-close §7 optional | no inspect verb |
| `sed -i 's#…:/sbin/nologin$#…:/bin/sh#' /etc/passwd` | seam-dead R4.2; reader-identity §3(a); nas-human-accounts:95 | no identity-repair verb (and the healer lives inside the failure domain) |
| `install -m0755 rootfs/boot-integrity /usr/local/bin/`; write `/usr/local/etc/boot-integrity.conf` 0600; append a TAB-separated `/etc/crontab` line **outside** the managed block | boot-integrity-gate §3.2–3.3 | the rootfs is not a plane `syrvis` writes; `schedule sync` would delete an in-block line |
| `scp provision.sh` → `sudo sh /tmp/provision.sh --dry-run` → apply | operator-shim-reprovision | **the seam cannot widen its own boundary** |
| `syrvisctl seam status` / `seam sync` from `/var/packages/syrviscore/target/venv/bin/` + 3× `grep -c` proofs + `visudo -cf` | jobs-pin-cutover §4 owner block; window-close §5.2 | `syrvisctl` is not on the seam at all |
| write `/volume4/syrviscore/config/jobs.source` (0644 root:root); root→GitHub deploy key | schedule-cutover §prereqs | deliberate root-only writes; "no CLI sets it" |
| `curl` the socket-proxy container IP for a 403 proof | window-close §7 optional | proxy publishes no host port and the seam runs only `syrvis` verbs |
| `rm -rf *.pre-move` cleanup session | data-plane-relocation §9; HANDOFF item 8 | — |

### Laptop-side wrappers that drive the seam
`scripts/deploy-stack <stack> [--only a,b] [--apply]` (dry-run is the default; **never wrapped in `timeout`**; `--only` is one comma-separated flag — repeating it is argparse last-wins), `scripts/apply-instance [--apply] [--converge]`, `scripts/apply-jobs <job> --apply`, `scripts/accept [--quick]`, `scripts/verify-all <slices>`, `scripts/maintenance-mode start|end --force|status`, `scripts/jobs_manifest.py --write|--print-pin`, `scripts/cloud`, `scripts/cf`, `scripts/backup-drill`, `scripts/secure-share`, `scripts/gen-syrvis-dashboard`, `render/console_static.py`, `render/data.py index`.

## Interaction patterns

**Bundles are built entirely laptop-side.** `deploy-stack` resolves repo config files + sops secrets into a `syrvis-bundle/v1` and streams it over ssh stdin to `syrvis deploy`; `apply-instance` renders `.env` + `stack.yaml` + every `services.d/*.yaml` into `syrvis-instance/v1` for `syrvis apply`. Secrets are decrypted in memory and never land on the NAS as plaintext. Window-close §10 records that a *raw seam* `syrvis deploy-stack --apply` (as one runbook wrote it) is wrong precisely because it skips this laptop-side resolution — the seam verb cannot see the repo.

**Results are consumed three ways:** JSON field extraction (`runstate`, `intent`, `location` inside a `history` revision, `changed_scripts`, `pin_written`), exit-code gating (`verify-all`: 0/1/2/**3 = BLIND**), and *deliberate distrust* — the recurring rule "a verb's own success report is not verification when the reporter is known blind" forces a second, independent surface (dashboard `RestartCount`, a downstream counter delta, a held vmalert cycle).

**Orchestration is client-side.** Stop/start ordering, store-before-consumer waves, the five-wave 14-service recreate, per-name loops (unrolled because `service stop` takes one name), the R0→R4 and G0→G6 ladders, and the whole app-move dance are sequenced in prose by the operator. The platform contributes only the stop-side `shutdown: {priority, stop_timeout}` bands; `resume`/`restart --graceful` start L2 "in declaration order, not the bands", which every restore runbook calls out as the reason to hand-order starts.

## Workarounds & missing verbs (the negative space)

1. **No filesystem verbs, stated as a boundary and worked around everywhere.** `immich-nvme-flip.md` even enumerates the full allowed set. Consequence: the roots census, the zero-byte-`secrets.env` sweep, `du`/`find` gates and every `mv`/`rsync` go to the reader or to cerebrate. `syrvis-reader` exists *only* because of this gap.
2. **No `docker exec` / `docker rm` / `docker inspect`** ⇒ DB restore, zombie clearing and mount verification are all break-glass by construction.
3. **No self-diagnosis without a resolvable home** — mitigated late by `syrvisctl doctor` (design/61), whose existence `seam-dead-after-boot.md` flags as confusing (`syrvis doctor` ≠ `syrvisctl doctor`).
4. **The seam cannot widen itself** ⇒ a whole runbook (`operator-shim-reprovision.md`) exists for boundary changes, plus the owner cerebrate block in `jobs-pin-cutover.md`.
5. **`service stop` conflates "stop" with "declare disabled"** — the entire shed/unshed feature (0.5.15) is the fix, but runbooks still carry the old `stop`+`start` idiom with "do NOT copy that" warnings (share-collision step 4).
6. **No batch/multi-name form** for stop/start/recreate/unshed; loops are unrolled by hand ("`for` loop bodies fail in this sandbox").
7. **No metrics-push verb** — `maintenance-mode end` prints SKIP for the window metric "by construction".
8. **No boot-time gate verb** ⇒ `rootfs/boot-integrity` is a hand-installed POSIX-sh script with its own hand-installed conf and a crontab line deliberately outside the managed block.
9. **Registry brittleness as a workaround generator:** `verify --json` "isn't an allowlisted seam argv; use `schedule apply`/`list` instead" (schedule-cutover) while later runbooks call `verify --json` freely — the allowlist changed under the prose.
10. **Reads that cannot reach the target grade as INFO/UNKNOWN and score zero** — flagged in five files as "UNKNOWN is not a pass"; `scripts/accept` exit 3 = BLIND is the local patch.
11. **Sequences that beg to be one verb:** post-boot G0–G6; the app-move seven-step dance; the shed-set close (read list → N× unshed → N× recreate in waves → accept); acceptance itself (`scripts/accept` was written after *four* hand-rolled acceptance lists in one week); seam re-provision (gen → scp → dry-run → apply → verify → rollback.sh); the jobs-pin advance (manifest → commit → push → print-pin → `sync --to` → record → verify).

## Observations for a v2 agent design

- **Give the agent a filesystem/topology read plane.** A single `device census --json` (roots + collision siblings + `current` symlink + declared-vs-present app homes + zero-byte secrets + shells + docker group + `/proc/mdstat` + `df`) would replace the reader identity, the R2/R4 blocks, G1/G2, and half of `scripts/accept`. It must run from the rootfs with no resolvable home — that requirement is non-negotiable and already learned.
- **Make convergence refuse-on-absent, not create-on-absent.** The single most-cited fact in this slice ("a convergence verb is a WRITE, and an absent declared root is treated as *create*") is the root cause of the 2026-08-16 second wave. A v2 agent should treat a missing declared root as a hard error requiring an explicit `--adopt`/`--scaffold`.
- **Ship gated composite verbs.** `boot-verify --then-resume`, `service move --to /volumeN`, `window close`, and `accept` should be device-side, journaled, and idempotent — every one of them exists today only as prose plus a laptop script, and each has drifted between runbooks (window-close §10 lists eight such conflicts, including three different monitoring-deploy shapes).
- **Report what the platform is currently blind to.** `RestartCount` (forcing a dashboard HTTP call), health-vs-`running`, and flapping must be first-class in the agent's status, or every runbook keeps carrying a "do not trust this verb" box.
- **Batch and order natively.** Accept a set of names with declared dependency/band ordering for stop, start and recreate; expose the topological plan as a dry-run. Client-side wave tables in prose are how a 14-service bring-up goes wrong.
- **Keep the argv boundary but make it forgiving.** The absolute-path requirement, the `--json` argv match and the sudo/no-sudo split have each produced misdiagnoses that sent operators into the wrong runbook. A v2 agent speaking a typed protocol (rather than allowlisted argv) removes an entire class of "looks exactly like a stale shim" failures — while preserving the *deliberate* boundaries (no filesystem, no arbitrary exec, root-only `jobs.source`, no self-widening).
- **Preserve the durable-intent model and expiries.** `shed`/`unshed` with `{reason, since, until}` outside the declaration set is the one design here everyone leans on; extend it to windows, accepted alerts and hardware watches so `config/maintenance-state.yaml` stops being a hand-maintained mirror of the device's own record.