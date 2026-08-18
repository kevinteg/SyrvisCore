# Project history & documentation corpus

## Purpose & role in the system

This "subsystem" is the project's memory: the design documents, archived analyses, release-note blocks and commit history that explain *why* SyrvisCore looks the way it does. It matters because SyrvisCore is a system that was **rewritten twice under duress** — once because DSM's privilege model made the original design impossible, once because an audit found every safety-critical mechanism broken — and almost every odd-looking invariant in the code today is a scar from a specific dated incident. The corpus is also load-bearing operationally: `docs/seam-contract.md` is a cross-repo API contract consumed by another repository (`home-tech`), and the repo-root `CLAUDE.md` functions as the de-facto changelog (there is no `CHANGELOG.md`).

## Key modules and files (path — role — approx size)

**Current / load-bearing**
- `CLAUDE.md` (repo root) — house rules + the *only* per-release notes; the "0.5.16" block is a full release narrative. ~400 lines.
- `docs/seam-contract.md` — the cross-repo integration surface (verb classes, `syrvis-instance/v1`, `syrvis-bundle/v1`, hostnames report v1). 210 lines, touched 2026-08-16.
- `docs/cli-syrvis.md` — 618 lines, tracks the real CLI (updated 2026-08-16).
- `docs/deployments.md` — 359 lines, the 0.5.0 deployment/rollback/shutdown system.
- `docs/wiki/` — 8-page engineering handbook (architecture, primordial substrate, networking, split DNS, L2 services, DR, schema reference), born 2026-07-11 (`013bf68`).
- `docs/release-0.6-plan.md` — 93 lines, the explicit deferral list (drafted 2026-08-11).
- `docs/service-declaration-roadmap.md`, `docs/dashboard.md`, `docs/backup-restore-design.md`, `docs/mcp-design.md`, `docs/image-provenance-design.md`, `docs/vms-workload-design.md`, `docs/service-loading-design.md`, `docs/home-tech-provisioning-requirement.md` — per-feature design docs, each a snapshot of one decision point.

**Explicitly historical**
- `docs/v2-design.md` — 138 lines, carries a `(SUPERSEDED)` banner: *"Status: historical… Kept for the rationale trail; do not implement from this document."*
- `docs/archives/` — 11 files, ~4,000 lines: `code-audit-2026-07.md` (148 l), `architecture-proposal-v2.md` (437 l), `design-doc-update-1.md` (651 l), `file-structure.md` (561 l), `dsm-wizard-guide.md` (404 l), `spk-scripts-analysis.md` (309 l), `chat-import-notes-2026-07.md` (226 l), `wizard-fixes-summary.md` (182 l), `PHASE3-NONSECURITY-NOTES.md` (50 l), `mcp-security-review-2026-07.md` (58 l), `nas-cleanup.md` (87 l).

**Stale but not marked stale** (see Gaps): `docs/design-doc.md`, `docs/dev-guide.md`, `docs/cli-syrvisctl.md`, `docs/spk-installation-guide.md`, `docs/spk-troubleshooting.md`.

## How it actually works — the arc, reconstructed

### v1 (2025-11-29 → 2025-12-26): Cline-authored, NAS-as-integration-test

The first ~90 commits are prefixed `Cline:` — an AI pair-programmer driving a single-package CLI plus an SPK. `docs/archives/design-doc-update-1.md` (repo date 2025-11-30) records the one v1 decision that survived everything: **macvlan**. DSM's nginx holds 80/443 and the Application Portal refuses reverse-proxy rules on 443; four alternatives were tried and rejected (Portal proxy, editing nginx.conf, bridge on 8080/8443, double reverse proxy). Traefik gets its own LAN IP. That doc also records `ovs_eth0` (Synology uses Open vSwitch), the Docker Hub API bug (`ordering=-last_updated` returns *oldest* first — sort client-side), and the split of build-time config (`build/config.yaml` = image tags only) from install-time config (`.env` = secrets **and** network).

`docs/archives/chat-import-notes-2026-07.md` is the distilled packaging pain: an SPK is an **uncompressed** outer tar containing `INFO`, `package.tgz`, and `scripts/` as a **directory**. Error taxonomy learned by repeated installs on the real box: **263** (outer archive gzipped), **261** (missing mandatory `start-stop-status`), **313** (`scripts` packed as a tar, so DSM can't chmod inside it), **276** (privilege).

**276 is the finding that caused everything downstream.** With `conf/privilege` `run-as: package`, DSM 7 runs *every* lifecycle script as the unprivileged package user (observed `syrvis-bot`, UID 203102) — "never root, even when installed via Package Center UI, even when the admin ran `sudo synopkg install`." So `synogroup`, `chown /var/run/docker.sock`, writes to `/usr/local/etc/rc.d/`, symlinks in `/usr/local/bin/` all fail inside install scripts. The corollary: **the SPK is a bootstrapper only**, and root work happens later via a self-elevating CLI. A hybrid SPK+Ansible design and a Chef-style approach were floated and explicitly rejected (Ansible isn't on DSM; the CLI is already Python) — recorded so it "isn't re-litigated."

The DSM wizard branch is the corpus's clearest dead end: `dsm-wizard-guide.md` + `wizard-fixes-summary.md` document five wizard steps, HTML simplification, JSON validation in the build script — and the audit then notes `WIZARD_UIFILES/` "is maintained but never packaged (no `install_wizard` directive; `build-spk.sh` doesn't copy it)". The wizard was polished for a code path that never shipped.

v1 ended on **2025-12-26**, when v0.1.2 through v0.1.21 shipped in a single day. The audit's verdict on this is the sharpest line in the corpus: *"releases v0.1.2–v0.1.9 shipped within hours of each other on 2025-12-26, each fixing what the previous broke on the production NAS — the NAS was the integration test."*

Note the v3 split-package architecture (`9aaca78`, `0a47bba`) actually landed **2025-12-25**, inside v1's numbering. "v3" is an *architecture* label, not a version line: v1/v2/v3 in this project name the architecture, while 0.x names the release.

### The gap and the audit (2026-07-09)

Six months of dormancy, then 173 commits in July 2026. It opens with `docs/archives/code-audit-2026-07.md`, a four-area audit whose verdicts are:

| Area | Verdict |
|---|---|
| manager | **Rewrite internals; keep CLI surface + version/symlink architecture** |
| service | **Refactor; do not rewrite** |
| SPK/build | **Overhaul scripts; keep SPK format knowledge** |
| tests/CI | **Rebuild the process first** |

Concrete findings acted on: manager **C1** (no checksum on downloaded wheels, pip-installed as root → release-asset compromise = root RCE), **C2** (path traversal in `restore`: `config/../../../etc/cron.d/evil` passes the `startswith("config/")` guard), **C3** (reinstall rmtrees the version dir *before* locating the wheel), **H1** (`syrvisctl uninstall "../.."` resolves to volume root and is `rmtree`d as root — "lethal once MCP passes model-generated arguments"), **H2** (non-atomic `current` symlink switch — "this is the core of the *instant rollback* promise"). Service **C1** (attacker-controlled service `name` used unsanitized as a path component; `name: ../../../../usr/local/etc/rc.d/S99evil` writes into boot-hook territory as root) and **C2** (arbitrary host mounts — "container escape by YAML").

Process findings: *"CI has failed on every run for months without gating anything"* (installs a root `pyproject.toml` whose `src/` disappeared at the monorepo split; matrix tested 3.9–3.11, not DSM's 3.8). *"The DSM simulation simulates success, not DSM"* — `SimulationOperations` returned unconditional `True`; `tests/dsm_sim.py` (394 lines) had zero consumers. And the damning counterfactual: *"Would the suite have caught the real NAS failures? **No**."*

`docs/v2-design.md` is the response, and its §2 operating principles are still the project's constitution: declarative intent only; **deterministic core, thin adapters** ("anything Claude can do, `ssh nas && syrvis …` can do"); machine-readable everything (`--json` + typed errors, prompts only in the CLI adapter, all bypassable); integrity chain (sha256, staged-then-swap, never destroy-then-download); verify is first-class. §5 redesigns elevation (`os.execv` is incompatible with MCP — it replaces the process): the MCP never elevates; **"the enumerated [sudoers] list *is* the security boundary, and it's auditable."** §6 keeps the SPK explicitly because *"its primary justification is disaster recovery… This makes the SPK's reliability a DR requirement, not a convenience"*, and adds the tarball dev loop whose single enabling primitive was `syrvisctl install --wheel`.

Phases 0–5 all executed within ~36 hours (2026-07-09 → 07-10). Phase 5 was red-teamed the same day: `mcp-security-review-2026-07.md` — 23 candidate findings, 17 verified, **10 confirmed, all fixed**, MCP suite 159 → 180. Highlights: F3 (shim `exec $cmd` unquoted → word-split/glob re-injection; rewritten to a precise argv matcher with charset whitelist + `set -f` + exact-argc), F1/F2 (git-URL allowlist **failed open**; now fail-closed at config load), F7 (nonce check-then-add race), F8 (tokens replayable across restart; fixed with a per-process random salt in the signing key).

### 0.2 → 0.5.17: the release cadence

68 tags exist, but **the last tag is `v0.5.7` (2026-08-11)** — 0.5.8 through 0.5.17 are untagged, per CLAUDE.md's gate: *"Do not tag until the release chain runs: the dashboard image must be rebuilt + repinned…"*

- **0.2.x** (2026-07-10) — "mark the v2 line"; dashboard born (FastAPI+React, third adapter); Traefik DNS-01; SHA256SUMS on releases.
- **0.3.0–0.3.7** (07-11) — exposure model (`internal`/`tunnel`), image-first `service run`, `stack hostnames`, `config/stack.yaml`, `services.d` declarative loading in three phases, typed-error CLI.
- **0.3.8–0.3.22** (07-17 → 07-20) — the operator-seam build-out: scheduled jobs, `secret set`, `config set`, `deploy` bundle, `command:` field, `tier: infra`. Four of these are permission fixes for the seam account (0.3.9/0.3.10/0.3.11).
- **0.4.0–0.4.7** (07-24/25) — seam registry relocated into the platform (`syrviscore.seam`), `syrvis apply`, profiles, tasks, one Traefik routing mechanism (file provider everywhere), image updates + `export`, `vms.d`, image provenance; the fviolence→owned `docker-state-exporter` supply-chain swap; digest-pinned core images + Renovate `pinDigests`.
- **0.5.0/0.5.1** (07-27/28) — deployment system (revisions, `service rollback`, lifecycle hooks, `shutdown --reason ups`/`resume`); per-service app location on any DSM volume.
- **0.5.2–0.5.4** (07-30/31) — boot durability. Root cause recorded verbatim: *"the DEPLOYED `syrvis-startup.sh` had drifted years behind the code… because activate never regenerated it and no validator caught the drift."*
- **0.5.5–0.5.8** (08-10 → 08-14) — durable shim ifcfg (silences a DSM SystemHealth poller flood, incident 2026-08-10); change-aware Traefik reload; `/api/summary`; fileplane volumes + raw `ports:`.
- **0.5.9–0.5.16** (all on **2026-08-16**) — the rename incident: *"A DSM cold boot renamed the platform's plain volume roots to `syrviscore_1`, killing the wrapper, seam, both self-healers and — via a resume that scaffolded the vanished roots — four containers' env."* Seven hardening changes, then six follow-ups in the same day, including 0.5.12 (`RestartCount` read from `attrs["State"]` — *"a location Docker has never populated"* — so flapping detection was inert in production; the fake fixture had fabricated the field) and 0.5.13 (a permanently-armed collision check turned the `verify --smoke` dead-man red: *"A liveness gate only goes red for states someone can fix"*).
- **0.5.17** (08-17, current branch `security-review-2026-08-17`) — `.env` write-time guard: *"a value with a shell metacharacter is root RCE at next boot, and a hazardous KEY NAME (PATH/LD_PRELOAD/PYTHONPATH) is a reboot-free root RCE."*

### What 0.6 explicitly defers

`docs/release-0.6-plan.md` (2026-08-11) is the honest deferral list: (1) `logging:` schema key — **every container today has unbounded logs**; (2) Traefik logs to stdout; (3) `syrvis-run-job` — *"All 12 managed jobs currently discard output entirely"*; (4) `user:` field replacing the blanket `0o777` that *"the code itself apologizes for"* (`service_manager.py:974`); (5) share/user declare verbs (a **parked patch**, `parked/syrvis-share-user-declare.patch`, 14 files, "1102-tests-green when parked"); (6) managed config dirs; (9) exporter memory series — *"the memory plane is completely blind today"*; (11) `verify --smoke` unreadable-config check that makes `healthy:false` a permanent seam artifact.

## Design decisions & their rationale (as recorded)

| Decision | Where recorded | Rationale |
|---|---|---|
| macvlan, Traefik owns a LAN IP | `design-doc-update-1.md` "Critical Architecture Change" | DSM nginx owns 80/443; four alternatives failed |
| SPK = unprivileged bootstrapper only | `chat-import-notes` §2 | DSM Error 276 / package-user model |
| Keep the SPK anyway | `v2-design.md` §6 | *"its primary justification is disaster recovery"* |
| Split packages (manager immutable, service versioned) | audit §1 verdict | architecture judged sound; only internals rewritten |
| Deterministic core, thin adapters | `v2-design.md` §2.3 | no operation exists only via MCP |
| MCP never self-elevates; enumerated sudoers | `v2-design.md` §5 | `os.execv` replaces the process; the list *is* the boundary |
| Reject absolute host mounts wholesale (no `/etc/localtime` allowlist) | `PHASE3-NONSECURITY-NOTES.md` #7 | *"Left strict on purpose"* |
| Ansible / DSM Task Scheduler rejected | `chat-import-notes` §4 | external dep absent on DSM; *"a dead end on purpose"* |
| Delete success-simulating sim mode | audit §4 / `v2-design.md` §10 | replaced by a command-transcript fake |
| Last-segment-only version bumps | `CLAUDE.md` | 0.5.15 → 0.5.16, features included |

## Invariants & contracts other subsystems rely on

- **`docs/seam-contract.md` is the only public surface.** *"Everything you may rely on is here; anything not here is an internal you must not depend on."* Both the sudoers file and the forced-command shim are **generated** from `syrviscore.seam.registry`, with a committed drift test.
- **`CLAUDE.md` is the changelog.** Any retrospective of 0.5.x must read it; git tags stop at 0.5.7.
- **Cross-repo gating rules live in prose.** CLAUDE.md: the dashboard image must be rebuilt/repinned before any `depends_on` reaches a real `services.d`, and *"while any edge exists, platform rollback below 0.5.16 is forbidden."*
- **Two version lines.** Manager `0.3.6` vs service `0.5.17`; the manager comment notes it *"is SPK-installed, so this needs an SPK reinstall to reach a NAS — unlike the service package."*
- **Boot-hook contract integer** pinned across packages by a test (`MIN_BOOT_HOOK_CONTRACT`, now 3).

## Gaps, debt & sharp edges

1. **`docs/design-doc.md` claims "Version 3.0 / Status: Implemented" and is materially wrong.** It lists 9 `syrvis` commands (reality: ~40 across `stack`/`service`/`schedule`/`vm`/`profile`/seam verbs), documents `syrvisctl migrate` (removed 2026-07-09; no longer in `cli.py`), and says SPK *"Creates global symlink to `/usr/local/bin/syrvisctl`"* (removed in `efbae56`, "use PATH instead of symlinks"). It is the first doc a newcomer reads.
2. **Four docs untouched since 2025-12-26** — `dev-guide.md`, `cli-syrvisctl.md`, `spk-installation-guide.md`, `spk-troubleshooting.md`. `dev-guide.md` still instructs `pyenv activate syrviscore` (CLAUDE.md: no activation, `.python-version` does it), `make dev-install` (superseded by `make env`), and documents the DSM simulation the audit ordered deleted — while `make sim-setup`/`test-sim` targets and `tests/dsm-sim/` still exist, unreferenced by the 2026 test suite.
3. **The real design corpus lives in another repo.** Commit messages cite `design/13, 20, 21, 22, 26, 28, 37, 44, 52, 53, 60, 63, 64, 70` — all in `~/code/home-tech`. SyrvisCore's history is **not reconstructible from this repo alone**; a reader hitting "design/63 D2 as amended, `opc:F10`" in CLAUDE.md has no local referent.
4. **No CHANGELOG, and release notes only exist for the version currently in flight.** CLAUDE.md carries a rich 0.5.16 block; 0.5.0–0.5.15 narratives exist only in commit bodies.
5. **The 2026-08-16 burst rhymes with 2025-12-26.** Eight versions in one day, four of them fixing the previous one's fix (0.5.10 and 0.5.11 are pure dashboard repins; 0.5.13 fixes a check shipped hours earlier that broke the dead-man alarm). The difference is real (1,100+ tests vs none), but "the NAS is the integration test" is still structurally true for boot/rename/DSM-interaction paths.
6. **Untagged drift.** Ten service versions past the last tag, with the tag gate expressed as prose in CLAUDE.md rather than enforced by CI.
7. **Branch clutter.** Six stale local branches (`feat/config-set-verb`, `feat/infra-tier`, `feat/l2-command-field`, `feat/l2-deploy-bundle`, `feat/share-user-declare`, `seam-cleanup`) plus a parked patch outside version control's normal flow.
8. **Audit findings never explicitly closed.** `PHASE3-NONSECURITY-NOTES.md` deferred M1/M5/M6/L5/L3 to "the follow-up model"; M1/M5 were later fixed (0.5.2, `3664736`) but nothing updates that document, so a reader can't tell what remains open. Same for the audit's own Medium/Low lists.
9. **Test blind spots the corpus itself names**: the MCP review's accepted residuals (a stolen operator key is inherently powerful; the reserved-core-name list is *"a hand-maintained frozen list"*; raw remote stderr is surfaced unredacted); and `PHASE3-NONSECURITY-NOTES.md`'s admission that the doctor fixers, add-rollback and password change *"do not yet have dedicated tests."*
10. **Fixture-shaped bugs are a proven class, not a one-off.** 0.5.12: `_FakeContainer` fabricated `State.RestartCount`, so a field Docker never populates passed 1,100 tests and shipped inert to production.

## Raw material worth citing in the retrospective

- *"The NAS **was** the integration test."* — `code-audit-2026-07.md`, on v0.1.2–v0.1.9 shipping within hours on 2025-12-26.
- *"The DSM simulation simulates success, not DSM."* — audit §4.
- *"Would the suite have caught the real NAS failures? **No**."* — audit §4.
- *"DSM 7 runs every lifecycle script as the unprivileged package user… never root, even when the admin ran `sudo synopkg install`."* — `chat-import-notes` §2 (Error 276).
- *"The enumerated list **is** the security boundary, and it's auditable."* — `v2-design.md` §5.
- *"Anything Claude can do, `ssh nas && syrvis …` can do."* — `v2-design.md` §2.3.
- *"This makes the SPK's reliability a DR requirement, not a convenience."* — `v2-design.md` §6.
- *"the DEPLOYED `syrvis-startup.sh` had drifted years behind the code… no validator caught the drift."* — 0.5.2 commit `8dc2fb8`.
- *"A liveness gate only goes red for states someone can fix."* — 0.5.13 commit `e2f8ce5`.
- *"a location Docker has never populated"* — 0.5.12 commit `24968db`.
- *"the blanket `0o777` the code itself apologizes for"* — `release-0.6-plan.md` #4.
- **Numbers:** 286 commits (29 in 2025-11, 59 in 2025-12, **173 in 2026-07**, 25 in 2026-08); 68 tags, last `v0.5.7`; manager audited at 2,793 lines / 8 files / **zero tests**; service package today 45 modules / ~27,600 lines; test suites 1,290 (service) + 213 (MCP) + 108 (dashboard) test functions; MCP red team 23 → 17 → **10 confirmed, all fixed**; SPK error codes **263 / 261 / 313 / 276**; Python **3.8.12** for DSM parity.
- **Timeline:** 2025-11-29 first commit · 2025-11-30 macvlan decision · 2025-12-25 v0.1.0 + v3 split-package · 2025-12-26 v0.1.2–v0.1.21 (one day) + Layer 2 · *6-month dormancy* · 2026-07-09 audit + v2-design + Phases 0–3 · 2026-07-10 Phases 4–5 (verify engine, MCP, red team) + dashboard · 2026-07-11 exposure model + `services.d` + wiki · 2026-07-20 seam verbs + infra tier · 2026-07-24 seam registry + profiles + 0.4.x · 2026-07-27 deployment system (0.5.0) · 2026-07-30 boot-resume proven on the NAS · 2026-08-11 0.6 plan drafted · 2026-08-16 rename incident → 0.5.9–0.5.16 · 2026-08-17 security review P6 (0.5.17).