# SyrvisCore Code & Test Audit — July 2026

Four-area audit (manager package, service package, SPK/build tooling, tests/process) performed 2026-07-09 to decide the shape of the v2 effort. Companion document: `docs/v2-design.md`.

## Verdict summary

| Area | Verdict | One-line reason |
|---|---|---|
| `packages/syrviscore-manager` | **Rewrite internals; keep CLI surface + version/symlink architecture** | Every safety-critical mechanism (download integrity, symlink switch, restore, input validation) is flawed; zero tests; nothing machine-readable for MCP |
| `packages/syrviscore` | **Refactor; do not rewrite** | Architecture is sound (provider pattern, validators, generators); targeted fixes for two critical Layer 2 security holes and the elevation path |
| SPK scripts + build tools | **Overhaul scripts; keep SPK format knowledge** | Uninstall path is guaranteed-broken under the declared privilege model; build not reproducible; wizard and Phase-2 installer are unshipped ghosts |
| Tests + CI | **Rebuild the process first** | CI red on every run for months; the one SPK test is dead code; DSM "simulation" simulates success; 4 of 21 modules covered, manager at zero |

The owner's distrust of the code and testing process is confirmed. The strongest evidence is historical: releases v0.1.2–v0.1.9 shipped within hours of each other on 2025-12-26, each fixing what the previous broke on the production NAS — the NAS *was* the integration test.

---

## 1. Manager package (`syrvisctl`)

2,793 lines across 8 files. Zero unit tests (nothing in `tests/` imports `syrviscore_manager`).

### Critical

- **C1 — No integrity verification of downloaded wheels, pip-installed as root.** `downloader.py:135-172` fetches from GitHub release `browser_download_url` with no checksum/signature/size limit; `version_manager.py:127-140` pip-installs it (root after self-elevation, `cli.py:188-190`), resolving transitive deps from PyPI unpinned. Release-asset compromise = root code execution on the NAS. Also `requests==2.31.0` pin predates CVE-2024-35195.
- **C2 — Path traversal in backup restore = arbitrary root file write.** `backup.py:396-429` guards extraction only with `member.name.startswith("config/")`; `config/../../../etc/cron.d/evil` passes. `syrvisctl restore` is documented for disaster recovery from arbitrary archives and runs elevated.
- **C3 — Reinstall deletes the existing version before downloading the replacement.** `version_manager.py:309-319` rmtrees the version dir (possibly the active one) before the wheel is even located (`:329-339`). A failed download leaves a dangling `current` symlink and no recovery path.

### High

- **H1 — Unvalidated version strings reach `rmtree`.** No version-format validation anywhere; `syrvisctl uninstall "../.."` resolves through `paths.py:229-231` to the volume root and is deleted as root (`version_manager.py:186`). Lethal once MCP passes model-generated arguments.
- **H2 — Non-atomic `current` symlink switch.** `paths.py:322-338` does `unlink()` then `symlink_to()` — crash window with no `current` at all, concurrent-activate race, and `IsADirectoryError` bricking if `current` is ever a real dir. This is the core of the "instant rollback" promise.
- **H3 — Restore ignores its own `install_path`** for the symlink/manifest steps (`backup.py:440-443` call ambient `get_syrvis_home()`); the CLI sets `SYRVIS_HOME` only *after* restore (`cli.py:934-938`). Restoring to a non-standard path can silently mutate a *different* installation.
- **H4 — Rollback/restore can report success while producing a non-runnable install.** Wheel caching swallows all exceptions (`version_manager.py:51-71`); restore skips or discards venv-install failures (`backup.py:431-437,471-487`) and flips the symlink anyway.
- **H5 — Rollback overwrites live config with no pre-rollback snapshot and non-atomic extraction** (`version_manager.py:376-411`, `backup.py:396-429`); `run_syrvis_stop` swallows all errors (`cli.py:437-446`).
- **H6 — Secrets in backups are world-readable; restore chmods `.env` to 0755.** Archive created 0644 containing acme.json/Cloudflared creds (`backup.py:246,267-295`); restore's suffix-guessing chmod (`backup.py:426-427`) matches every extensionless file including dotfiles.

### Medium (abridged)

- **M1** Manifest vs symlink: two unsynchronized sources of truth; non-atomic manifest writes (`manifest.py:64-72`); `uninstall`'s active-version guard reads only the manifest.
- **M2** Un-suppressible interactive prompts (`version_manager.py:314` ignores `-y`; rollback/cleanup have no `-y` at all) — blocks automation/MCP/cron.
- **M3** sudo re-exec (`cli.py:58-72`) loses pre-elevation state, re-prompts, fails without TTY.
- **M4** Downloader collapses every failure (rate-limit 403, DNS, TLS, missing release) into `None` → one generic message (`downloader.py:49-85`). GitHub's 60 req/hr unauthenticated limit makes this routine.
- **M5** Volume/home detection fragility: hardcoded `/volume1..9` scan, `volumeUSB1` matches, env-var strategy trusts any existing dir without a manifest, multi-volume ambiguity resolved silently (`paths.py:101-147`).
- **M6** `run_syrvis_clean` returns success unconditionally (`cli.py:114-121`).
- **M7** Version parsing collapses non-numeric tags to `(0,0,0)`, duplicated in four places.
- **M8** `cleanup` off-by-one with no active version; TOCTOU between listing and deletion; no sudo check yet prints "Cleanup complete" on failures.
- **M9** Raw tracebacks when no installation exists (`cli.py:234,303`); dead exception handlers elsewhere.
- **M10** `migrate` is a stub that flips `schema_version` and writes a wrapper — no actual migration.

### Low (abridged)

~10 dead functions across all modules (including `get_previous_version` — rollback never uses it); two divergent wheel-install code paths (`version_manager.py:74-159` vs `backup.py:448-487`, the latter using bare `python3` from PATH); 20-release pagination limit in `get_latest_release`.

### MCP-readiness

None. No `--json` anywhere; `click.echo`/`click.confirm` welded into the library layer; everything returns `bool` + prints; `os.environ` mutation as configuration; no locking; no error taxonomy. The rewrite shape is prescribed in `docs/v2-design.md` §4.

---

## 2. Service package (`syrvis`)

15 files; the largest modules (`validators.py` 1139, `setup.py` 896, `cli.py` 875, `privileged_ops.py` 872) have **zero** test coverage.

### Critical

- **C1 — Path traversal via attacker-controlled service `name`.** A third-party `syrvis-service.yaml` fully controls `name`/`container_name`, used unsanitized as path components in `service_manager.py:100,142,231` and `traefik_config.py:409` (`shutil.move` target, data dir, compose filename, Traefik-watched config filename). `service_schema.py:85-110` validates nothing. `name: ../../../../usr/local/etc/rc.d/S99evil` writes into boot-hook territory — as root, since `service add` self-elevates.
- **C2 — Arbitrary host mounts, no volume/capability policy.** `service.volumes` passes straight into generated compose (`service_manager.py:206-224`): `/:/host:rw`, docker.sock mounts, `../../` escapes from the relative-path prefix — all accepted. Combined with root elevation this is container escape by YAML. Any `http://`/`git@` source is accepted with no allowlist (`service_manager.py:48-57`).

### High

- **H2 — Elevation drops `SYRVIS_HOME`, breaking every `service` subcommand.** `privilege.py:67-69` re-execs the venv console script (bypassing the `bin/syrvis` wrapper that sets the var) under sudo `env_reset`; `ServiceManager.__init__` hard-requires the var with no fallback (`service_manager.py:31-35`). `sudo syrvis service add/remove/start/stop/update` fail immediately after elevation.
- **H3 — `doctor --fix` silently ignores two of its advertised fixes.** `apply_fixes` (`doctor.py:230-275`) has no branch for `boot_script` (`validators.py:1056`) or `manifest_perms` (`validators.py:409`) despite both being flagged fixable — a missing boot hook is reported fixable and never fixed.

### Medium (abridged)

- **M1** Boot-time startup script recomputes `SHIM_IP`, ignoring the configured value (`privileged_ops.py:326-347`) — different network state after reboot vs `syrvis start`.
- **M2** `docker-compose` (v1) for core services vs `docker compose` (v2) for Layer 2 — one of the two silently fails per host.
- **M3** All Layer 2 services share one implicit compose project (no `-p`, `service_manager.py:248-249`).
- **M4** Failed `service add` leaves partial state; retry reports "already installed".
- **M5** `doctor` resolves the wrong user under sudo (`validators.py:498-501`) — checks root's docker-group membership instead of the invoking user's.
- **M6** `setup_completed_at` written, `setup_date` read — doctor always shows "unknown".
- **M7** Portainer password minimum 8 chars vs Portainer CE's required 12 — first-run init fails.

### Low (abridged)

Duplicate elevation implementations (`setup.py:31-40` vs `privilege.py`); Traefik dashboard `api.insecure: true` on :8080 unauthenticated; dead volume-cleanup code targeting volumes that never exist; fragile `.env` export in the startup script leaking the Cloudflare token; string `>` version comparison in `paths.py:433`.

### Worth keeping

`privileged_ops.py` provider pattern, `validators.py` (typed `CheckResult`/`ValidationReport` — essentially a `verify` engine already), `compose.py`/`traefik_config.py` generators, versioned `paths.py` model.

---

## 3. SPK scripts and build tooling

### Privilege-model contradictions (High)

`spk/conf/privilege` runs all lifecycle scripts as the unprivileged package user, but:

- **postuninst is guaranteed to die mid-cleanup**: `set -e` + privileged operations (`rm` of rc.d file, `chown /var/run/docker.sock`) abort on first real file; everything after silently never runs.
- **postuninst removes the wrong boot hook** (`S99syrviscore-docker.sh` at `postuninst:42`; setup creates `S99syrviscore.sh` at `privileged_ops.py:363`) and would set the Docker socket to **root:root 0666 (world-writable)** — flagged in the project's own `docs/archives/spk-scripts-analysis.md:291-294` and never fixed.
- **Broken cross-script contract**: postuninst reads `/tmp/syrviscore_install_dir` which only pre**upgrade** writes — plain uninstalls skip data handling; a stale file could target the wrong directory.
- **preuninst runs `syrvis stop` as the package user** — self-elevation can't work without a TTY; no timeout, so a hung docker call blocks Package Center indefinitely.

### Other findings

- **preinst validates the wrong directory** (`${pkgwizard_volume:-/volume1}/docker` — the legacy path, never used by v3) and can hard-fail valid installs.
- **DSM 7.2+ dependency naming**: `spk/INFO` depends on `Docker`/`pkgctl-Docker`; DSM 7.2+ ships `ContainerManager`. Verify against the actual DSM version.
- **Ghost artifacts**: `WIZARD_UIFILES/` is maintained but never packaged (no `install_wizard` directive; `build-spk.sh` doesn't copy it); `spk/package/bin/setup-privileges.py` isn't shipped yet `preinst:122` tells users to run it. The "local installer" of record is the current architecture itself: SPK demoted to bootstrapper, everything real done by the self-elevating CLI chain (`spk/INFO:4` documents this).
- **`trap ... ERR` is not POSIX sh** (`postinst:82`, `postupgrade:77`) — works only because DSM's sh is bash.
- **postupgrade deletes the manager venv before proving the replacement installs** — a pip failure leaves no working `syrvisctl` and no rollback.
- **Volume scans hardcoded to `/volume1..5`** in preupgrade/preuninst/start-stop-status (manager Python scans 1..9 — inconsistent, both wrong for `/volume10+`).
- **build-manager.sh silent host-platform fallback** (`:92-97`): if the platform-pinned `pip download` fails, it silently bundles macOS/arm64 wheels; postinst's offline `--no-index` install then fails on the NAS with an opaque pip error — exactly the historical failure class.
- **Builds are not reproducible**: transitive deps unpinned (same tag → different SPK on different days), `tar -czf` with no `--sort/--owner/--mtime/SOURCE_DATE_EPOCH`, no `COPYFILE_DISABLE=1` on macOS (AppleDouble `._*` leakage), `build-spk.sh:109` sed-mutates `spk/INFO` in the source tree, version truth in four places (spk/INFO, service pyproject+`__version__`, manager pyproject+`__version__`, dead root pyproject).
- **The Makefile build path has been broken since the monorepo split**: `Makefile:31` reads nonexistent `src/syrviscore/__version__.py` → empty `VERSION`; `make build-wheel` builds the stale root `pyproject.toml` (0.1.12, `where=["src"]` → no packages). Only the direct scripts (`build-manager.sh` → `build-spk.sh`; `build-service.sh` → `release-service.sh`) work.
- **No integrity chain**: `release-service.sh` uploads no checksums; nothing verifies downloads (pairs with manager C1).
- **Worth keeping**: offline-wheel bundling design, `validate-spk.sh` (encodes the hard-won error-263/313 lessons), permission normalization in `build-spk.sh:209-247`, pip-log capture.

### Tarball dev-loop feasibility (the key result)

**Nothing about the working system requires Package Center except the DSM UI entry and dependency check.** An admin SSH user with sudo can reproduce 100% of a working install. Blockers/requirements:

1. `syrvisctl install` has no local-wheel path — only GitHub (`cli.py:128-134`, `downloader.py`). **Adding `syrvisctl install --wheel <file>` is the single change that makes the loop trivial.** (Interim: a small Python shim calling `version_manager.install_version()` + `activate_version()` directly — they're cleanly separable.)
2. Bootstrap must replicate only postinst's steps: venv + offline pip install of the manager wheel + profile snippet (or full paths). `sudo syrvis setup` already automates everything else — don't reimplement in shell.
3. **Use a distinct `SYRVIS_HOME` for dev** (env var is honored): the SPK scripts discover installs by scanning for `.syrviscore-manifest.json`, so a dev install at the production path would confuse upgrade/uninstall logic.
4. Ship `bootstrap.sh --clean` teardown so the loop is `make tarball && scp && ssh 'tar xf … && ./bootstrap.sh'`.

---

## 4. Tests and process

- **CI has failed on every run for months without gating anything**: `.github/workflows/test.yml:28` installs the stale root `pyproject.toml` (`where=["src"]`, dir gone since the monorepo migration) — tests can't import. Lint targets the same nonexistent `src/`. Matrix tests 3.9–3.11, not the 3.8 that DSM actually runs.
- **The only SPK test is dead**: `tests/test_spk_installation.sh:112-115` exits reading a pre-monorepo path, and past that asserts the pre-v3 architecture (wizard `.env`, `.env.template` in package.tgz).
- **The DSM simulation simulates success, not DSM**: `SimulationOperations` (`privileged_ops.py:528-687`) returns unconditional `True` for group membership, socket perms, macvlan shim. The mock `synogroup`/`synopkg` binaries are never invoked by Python (the factory routes sim mode away from `DsmOperations`, the only caller); `tests/dsm_sim.py` (394 lines) has zero consumers and no conftest registers its fixture.
- **Over-mocked docker tests**: `test_docker_manager.py:105-141` mock `docker.from_env` and `subprocess.run`, asserting only that a substring appears in argv — they pass with docker absent or the command wrong.
- **Coverage**: meaningful tests exist only for `compose.py`, `traefik_config.py`, `paths.py` (service), and the file-generation half of `docker_manager.py`. Manager package: zero. `cli.py` (×2), `setup.py`, `validators.py`, `privileged_ops.py`, `service_manager.py`, `backup.py`: zero.
- **Would the suite have caught the real NAS failures? No** — everything that broke (elevation, synogroup, macvlan `ip` sequence, boot hook, offline pip) lives in code paths no test executes, or is replaced by unconditional success in sim mode.

### Test capabilities v2 must build (priority order)

1. Green, **gating** CI on the monorepo layout, running **Python 3.8.12** (DSM's runtime).
2. `CliRunner` contract tests for every `syrvis`/`syrvisctl` command (exit codes, `--json` output, error messages, non-interactive paths) — doubles as the MCP contract layer.
3. A **command-transcript fake** for `DsmOperations` (inject a `run_command` seam; assert exact argv sequences incl. the 4-step macvlan ordering and rollback-on-failure) instead of return-True simulation.
4. **SPK lifecycle test in a Linux container in CI**: build SPK → extract → run scripts as unprivileged user with `--no-index` (the production pip branch) → assert venv/profile/logs.
5. Hermetic manager tests: local/`file://` release source so install→activate→rollback→backup/restore run against wheels from the current tree (backup/restore is disaster-recovery code with zero tests).
6. Golden-file tests for generated compose/traefik configs against a committed canonical `config.yaml`, validated with `docker compose config`.
7. `syrvis doctor --smoke`: non-destructive post-install assertion suite runnable over SSH — codifies what the v0.1.2–v0.1.9 fix-release churn did manually.
8. Delete or wire up the orphans (dead SPK test, unused simulator, mock bins) so coverage is legible.
