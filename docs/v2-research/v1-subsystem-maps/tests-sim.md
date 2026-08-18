# Test architecture, DSM simulation & CI

## Purpose & role in the system

SyrvisCore has no staging environment. The only real target is one production Synology NAS whose failure mode is "the homebase is decapitated" (incident 2026-08-16). The test suite is therefore doing two jobs at once: (1) it is the *only* pre-flight gate before a wheel is shipped to a live NAS, and (2) it is the institutional memory of every incident — a large fraction of test modules open with a docstring naming the incident or design clause they pin (`test_vanished_app_home.py`, `test_wrapper_fallback.py`, `test_intent_shed.py`, `test_flapping.py`, `test_boot_reclaim_guard.py`, `test_deploy_journal.py`). The strategy is *hermetic-library testing*: the deterministic library layer is tested exhaustively in `tmp_path` at ~1500 tests in 25 s, while everything that requires root, DSM, or Docker is either dependency-injected, string-asserted against a rendered artifact, or simply untested.

## Key modules and files (path — role — approx size)

- `tests/` — 56 pytest modules + 2 bash scripts, 1507 collected tests, `1507 passed in ~25s`, 2874 assertions
- `tests/conftest.py` — 63 lines. The *entire* shared fixture surface: `stamp_install_root()` + an `install_root` factory fixture. Nothing else is shared.
- `tests/dsm-sim/` — `setup-sim.sh`, `activate.sh`, `deactivate.sh`, `reset-sim.sh`, `bin/synopkg`, `bin/synogroup`, `state/{docker-status.txt,installed-packages.json,docker-group-members.txt}`, plus a committed stale `root/etc/TZ` and `root/tmp/spk-extract/**` (a July SPK extraction leaked into git)
- `tests/test_sim_workflow.sh` (7.7 KB) / `tests/test_version_management.sh` (6.5 KB) — the only consumers of the simulator; both last touched 2026-07-09, neither runs in CI
- `.github/workflows/test.yml` (9 KB) — 5 jobs: `test`, `mcp`, `dashboard`, `dashboard-image`, `dev-loop`
- `.github/workflows/dashboard-image.yml`, `docker-state-exporter-image.yml` — GHCR image builds
- `Makefile` — dual-env orchestration (`env`/`env-modern`, `check`/`check-modern`, `sim-*`, `test-sim`, `test-versions`)
- `pyproject.toml` (root) — 14 lines; `[tool.pytest.ini_options] testpaths = ["tests"]` is the single most consequential line in the test architecture (see Gaps)
- `packages/syrviscore-mcp/tests/` — 15 files, **423 tests**, own `conftest.py` (`NASConfig` factory + `FakeRunner` that still builds real argv/tokens)
- `packages/syrviscore-dashboard/tests/` — 16 files, **108 tests**, `conftest.py` with an autouse `_no_real_docker` fixture, a `syrvis_home` fixture and a FastAPI `TestClient`
- `packages/syrviscore/tests/` — 4 files, **91 tests** (`test_fileplane`, `test_write_secret`, `test_write_config`, `test_domain_override`) — **collected by nothing**

## How it actually works

**The DSM simulation model.** Two env vars drive it. `DSM_SIM_ACTIVE=1` + `DSM_SIM_ROOT=<dir>` are read in exactly four places:

- `paths.is_simulation_mode()` / `get_sim_root()` / `resolve_volume_root()` — maps `/volume6` → `<sim_root>/volume6` ("Kept module-level so tests can monkeypatch it directly", `paths.py:108`)
- `paths.is_mounted_volume()` — in sim mode the `os.path.ismount` requirement is waived, but a *symlinked* volume root is rejected in every mode ("sim mode must not reopen that hole, adversarial review #9")
- `privilege.is_root()` — returns `True` unconditionally under sim
- `privileged_ops.get_system_operations()` — returns `SimulationOperations(sim_root)` instead of `DsmOperations`; raises `PrivilegedOpsError("DSM_SIM_ACTIVE=1 but DSM_SIM_ROOT not set")` if the root is missing

`SimulationOperations` (privileged_ops.py:1122–1281) stubs *out* the dangerous half (docker group, socket perms, macvlan shim → `"Macvlan shim skipped (simulation mode)"`) and *reimplements* the filesystem half against the sim root (global symlink at `<sim>/usr/local/bin/syrvis`, startup script, `<sim>/usr/local/etc/rc.d/S99syrviscore.sh` — but a **different, simplified script body** than `DsmOperations` renders).

What the shell simulator actually mocks is tiny: `synopkg` (status/is_onoff/start/stop/install/uninstall/list/version — Docker status backed by a text file) and `synogroup` (`--get/--member/--memberadd/--memberdel` backed by `docker-group-members.txt`). **Nothing else.** There is no mock for `synocheckshare`, `synoschedtask`, `synowebapi`, `crontab`, `ip`, or `docker`; `setup-sim.sh` symlinks the *real* `/var/run/docker.sock` into the sim root if present.

**The real fake surface is Python dependency injection, not the simulator.** Only 8 of 56 modules touch `DSM_SIM_*` at all (`test_paths`, `test_home_collision`, `test_apps_root_segment`, `test_volume_locations`, plus three that *delete* the vars for isolation). The mechanisms used instead:

- **Injected runners**: `schedule.dsm_task_census(binary=…, run=…)` — the `synoschedtask` census is tested with a `_fake_runner` returning canned DSM text, plus explicit non-happy paths (missing tool → `available: False`; rc=13 permission error; unparseable output → `ok False, "did not recognise"`; a runner that raises `OSError`; `MAX_DSM_TASKS`/`MAX_DSM_FIELD_CHARS` bounds). `vms` uses the same shape for `synowebapi`. `guards.guard_bulk_degraded(..., mdstat_path=…)` reads a fabricated `/proc/mdstat`.
- **Function-level redirection where a constant would not work**: `test_scheduled_jobs.monkey_crontab()` replaces `schedule.read_crontab`/`write_crontab_atomic` wholesale, with the reason documented: "read_crontab/write_crontab_atomic bind CRONTAB_PATH as a default argument at definition time, so reassigning the module constant is not enough".
- **Argv-dispatching fakes**: `test_macvlan_shim._FakeIp` models `ip link/addr/route` with mutable state so reconciliation (stale SHIM_IP, wrong route dev, DSM's log-flooding `ifcfg` stub) is asserted on resulting argv sequences.
- **Instance-method surgery at the Docker boundary**: `test_deploy_bundle._manager()` builds a real `ServiceManager(syrvis_home=tmp_path)` then sets `mgr._reload_traefik = lambda: None` and `mgr._start_service = lambda name, cp: (start_ok, …)`. Everything below that line (manifest writes, 0644 configs, 0600 `secrets.env`, compose generation, atomic rollback) is real filesystem work; everything above is fiction.
- **Real Docker SDK mocking** only in `test_docker_manager.py` (19 `MagicMock`, 13 `patch`), with an autouse fixture pinning `compose_cmd._cached = ["docker-compose"]`.
- **Real shell execution** in three modules: `test_boot_reclaim_guard`, `test_wrapper_fallback`, `test_apps_root_segment`. These take the *shipped rendered artifact* (`privileged_ops.render_boot_script`, `manager.paths.create_syrvis_wrapper`), substitute `/volume[0-9]*` → `<tmp_path>/volume[0-9]*`, drop the `/etc/passwd` seam-heal loop (it "would sed the developer's real /etc/passwd and fail on BSD sed"), and run it with `sh`. That is the only place where a shipped shell script's *behaviour* — not its text — is proven. Every such module also asserts `sh -n` parse validity.

**CLI adapter testing** uses `click.testing.CliRunner` in 20 modules, with `privilege.ensure_elevated` monkeypatched to a no-op and `SYRVIS_HOME` pointed at a `stamp_install_root(tmp_path)` tree. Stdin-bundle verbs (`apply`, `deploy`) are driven by `CliRunner().invoke(cli, argv, input=json_doc)` and assert both the success envelope and the redaction rule (`assert "oldvalue" not in r.output and "newvalue" not in r.output`).

**State-file contract tests are the strongest layer.** `test_deploy_journal.py` (27 tests) pins `data/state/deploy-journal.json` clause by clause: canonical path (`assert path.parent == breakers.get_breakers_path(home).parent`), `schema_version` unknown ⇒ `{"state": "unknown", "act": False}`, unparseable ⇒ `unknown` never `absent`, `TERMINAL_STATES == {"started","healthy","skipped","failed"}` vs `NON_TERMINAL_STATES == {"pending","stopping","starting"}`, `STALE_AFTER_S == 3600`, dead-PID staleness, "a stale journal REFUSES NOTHING", `MAX_EVENTS` bounding, atomic write with no `.tmp` leftovers and mode 644. `test_breakers.py` (30 tests) does the same for `data/state/breakers.json`: required row fields, garbage store ⇒ all-closed, per-row drop of malformed entries, refusal to overwrite `schema_version: 99`, the capped curve (`MAX_BACKOFF_S == 600`, jitter always in `[1,600]`, `backoff_seconds(10**6)` doesn't explode), open-transition pages exactly once, cross-plane suppression, "a close closes all", and `closes_breakers("hostd"/"s99"/"cron"/None) is False`.

**CI matrix** (`test.yml`):

| job | runner | python | scope |
|---|---|---|---|
| `test` | ubuntu-22.04 (pinned: "Python 3.8 … is unavailable on newer runners") | 3.8 | `pytest tests/`, `black --check` + `ruff check` over `manager/src`, `syrviscore/src`, `tests` |
| `mcp` | ubuntu-latest | 3.12 | `pytest packages/syrviscore-mcp/tests` (incl. real `visudo -cf` when present), lint, `python -m syrviscore_mcp.deploy.gen check packages/syrviscore-mcp/deploy` |
| `dashboard` | ubuntu-latest | 3.12 + Node 20 | `pytest packages/syrviscore-dashboard/tests`, lint, `npm ci && npm run build` (= `tsc --noEmit && vite build`) |
| `dashboard-image` | ubuntu-latest | — | buildx build; push to GHCR only on `main`/tag/dispatch |
| `dev-loop` | ubuntu-22.04 | 3.8 | build devkit tarball → `bootstrap.sh --home … --yes` → verify `current` symlink + `syrvis --version` + `syrvisctl list --json` → **DR: backup, copy off-box, `rm -rf` the home, `restore`, re-verify** → `bootstrap.sh --clean` |

`BLACK_VERSION: "24.8.0"` is pinned repo-wide with the rationale in-file: 24.8.0 is the last release installable on 3.8, so without the pin the two 3.12 jobs would float and "the repo [would be] gated by two formatters that drift apart on Black's release schedule".

`make check` = lint + format-check + test on 3.8; `make check-modern` = `test-mcp` + `test-dashboard` on the parallel `syrviscore-modern` (3.12.7) pyenv virtualenv selected via `PYENV_VERSION=` rather than activation. `LINT_DIRS` is deliberately wider than the 3.8 test scope because "Black + Ruff are static analyzers (they parse, never import)".

## Design decisions & their rationale

- **`.python-version` as the interpreter contract.** "That single file is what makes the interpreter deterministic; without it, a bare `python3` falls through to the pyenv *global*" (CLAUDE.md). It is what lets CI, `make`, and agents share one interpreter with no activation step.
- **Two Python environments, never one matrix.** mcp/dashboard "target modern Python (>=3.10; fastmcp/fastapi ship no 3.8 wheels) and run as their own CI jobs on 3.12 — never in the 3.8 SPK matrix" (Makefile).
- **Hermeticity over fidelity.** `test_manager_core.py`: "No network, no real venv/pip: the venv backend is replaced with a fake that writes marker files, so install/activate/rollback/backup/restore run against real filesystem state in tmp_path in milliseconds." The `dev-loop` job exists precisely to buy back the fidelity that fake costs — it is the one place a real venv, real pip, and a real restore run.
- **Conftest as incident documentation.** The 40-line conftest docstring exists to explain why `stamp_install_root` exists: after the 2026-08-16 cold boot, `paths.get_syrvis_home()` requires a self-identifying `.syrviscore-manifest.json`, "so a test pointing `SYRVIS_HOME` at a bare tmpdir was asserting against a state that cannot legitimately occur… Tests that WANT the rejection (`tests/test_paths.py`) deliberately do not use this."
- **Executing rendered shell rather than trusting assertions.** `test_boot_reclaim_guard.py`: "The reclaim guard is exercised by actually RUNNING the rendered shell against a fake volume tree; assertions alone would not prove a shell script works."
- **Drift bindings as tests.** `test_seam_registry_drift.py` is 2 assertions binding `registry.STACK_SERVICES == stack.ALL_SERVICES`; `test_seam_sync.TestProvisionPolicy::test_policy_path_matches_manager_constant` binds the provision script's `$STATE_DIR/seam-policy.json` to `seam_sync.SEAM_POLICY_PATH` because "a silent divergence would make load_policy() always return None, so auto-sync would silently skip on every activate/rollback." The MCP `gen check` step is the same idea for generated sudoers/shim artifacts.
- **Security tests treat declarations as attacker input.** `test_service_security.py` (86 tests, 1451 lines) and `test_deploy_bundle.py` (63 tests) both open by stating the trust boundary; `test_scheduled_jobs.py` enumerates its invariants (a) `source` key rejected, (b) `command` key rejected/derived, (c) fail-closed root source, … (g) DSM crontab lines preserved.

## Invariants & contracts other subsystems rely on

- `SYRVIS_HOME` must name a real install root (self-identifying manifest); `conftest.stamp_install_root` is the sanctioned way to fabricate one and is imported by name from three suites (`from conftest import stamp_install_root`).
- Sim mode is *additive permission* only: it waives mountpoint-ness and root-ness, never the symlinked-volume-root refusal.
- `data/state/` is one directory with one convention (journal + breakers assert a shared parent), atomic writes, 0644, no `.tmp` residue.
- The 3.8 job's lint scope defines what "the repo is formatted" means; `make check` is deliberately wider so a green local check cannot precede a red mcp/dashboard job.
- `pytest packages/syrviscore-mcp/tests` and `pytest packages/syrviscore-dashboard/tests` must be invoked as **separate processes** (see Gaps).
- Test names/docstrings are the retro record: `design/60 §5 D6`, `design/63 M1/D1/D2`, `opc:F1/F2/F4/F5/F10`, `incident 2026-08-16`, `design/21`, `design/12`, `design/37 §4 Phase 1`.

## Gaps, debt & sharp edges

1. **91 tests are orphaned.** Root `pyproject.toml` sets `testpaths = ["tests"]`, `make test` runs `$(TESTS_DIR)`, and CI runs `pytest tests/`. `packages/syrviscore/tests/` (fileplane/shares registry, `write_secret`, `write_config`, `domain_override`) is therefore run by **nothing**. It is also outside `LINT_DIRS` and outside CI's `black --check` path. Verified: they pass (91 in 0.16 s) but `black --check` reports "1 file would be reformatted" and `ruff` reports 3 errors on that directory today. Adding them raises `shares_registry` coverage 68% → 87% and `service_schema` 89% → 93%.
2. **The DSM simulator is effectively dead.** Every file in `tests/dsm-sim/` and both shell tests date to 2026-07-09; the codebase is 2026-08-17. Neither `test-sim` nor `test-versions` runs in CI. `tests/dsm-sim/root/` in git contains only leftovers of a July run (`etc/TZ`, `tmp/spk-extract/` with `package.tgz` and two PNGs) because `.gitignore`'s `var/` rule hid the interesting half. `test_sim_workflow.sh` step 8 is network-conditional (`curl … api.github.com`). The simulator's `SimulationOperations.ensure_boot_script` writes a *different* S99 body than production renders, so nothing about the real boot hook is validated there.
3. **No SPK is built or validated in CI.** There is no `build-spk`/`validate-spk` job; `build-tools/validate-spk.sh` runs only when a human types `make validate`. The SPK scripts (`preinst`/`postinst`/`postupgrade`/`start-stop-status`) have no `sh -n`, no shellcheck, and no test — the one artifact DSM actually installs.
4. **Concurrency is untested and, in the service package, unimplemented.** The manager has `locking.hold_lock` (non-blocking `flock`) with exactly one same-process test (`TestLocking::test_concurrent_mutation_refused`); no cross-process test. The service package contains **no `flock` at all** — cron `schedule apply`, the S99 boot hook, `syrvis reconcile`, the seam's `apply`/`deploy`, and the dashboard can interleave against `config/services.d`, `data/state/*.json`, and `/etc/crontab` with only advisory journal/breaker state between them. `deployments._write_record` documents a lock-free `os.link` revision claim; no test races it.
5. **Failure injection is nearly absent.** Across 56 modules there are ~10 occurrences of `side_effect`/`raise OSError` (6 files). No test simulates ENOSPC on an atomic write, a truncated write, a killed process mid-deploy, a read-only volume, a SIGTERM during `shutdown`, or a partially applied bundle beyond the one rollback path.
6. **13% of assertions are string containment in rendered artifacts** (383 of 2874) — sudoers text, provision scripts, boot hooks, wrapper scripts. Several assert *ordering by string index* (`start_case.index("SEAM_USER") < start_case.index(trampoline)`). This pins semantics that are otherwise unobservable, but it is refactor-brittle and it is plumbing, not behaviour, wherever no execution test backs it. The clearest case: the advisory `synocheckshare.service` phase gate is asserted only as "the substring appears before the glob" — the wait loop, the `sleep 2` cadence, and the timeout branch never execute (on both macOS and CI the loop exits on iteration 0).
7. **Docker is never exercised.** `_start_service`/`_reload_traefik` are stubbed at the instance level, `docker_manager` is `MagicMock`ed (63% covered), the dashboard forces `DOCKER_HOST=unix:///nonexistent/…`. Nothing verifies that a generated compose file is one Docker will accept, that `up -d --force-recreate` actually re-bakes env, or that health checks behave — precisely the behaviours whose absence caused the 2026-08-16 recreate/restart incident.
8. **Coverage blind spots, measured** (`--cov` over `tests/`, total 68% of 13891 statements): `doctor.py` **9%**, `setup.py` **11%**, `update.py` **11%**, `privileged_ops.py` **43%**, `validators.py` **46%**, `cli.py` **48%**, `manager/cli.py` **56%**, `image_provenance.py` **56%**, `downloader.py` **57%**, `docker_manager.py` **63%**, `version_manager.py` **66%**, `shares_registry.py` **68%**, `seam/gen.py` **69%**, `verify.py` **71%**. `syrvis setup` — the verb every install runs first — is essentially untested (5 tests in `test_setup_env.py`, covering only `.env` idempotency). No coverage threshold is enforced anywhere; `make test-cov` exists and nothing calls it.
9. **The three suites cannot be collected together.** Both `packages/*/tests/` carry `__init__.py` and are both named `tests`, so a single invocation dies with `ImportPathMismatchError: ('tests.conftest', …mcp/tests/conftest.py, …dashboard/tests/conftest.py)`. Basenames also collide across suites (`test_drift.py`, `test_deploy*.py`). CI hides this by using separate jobs.
10. **No frontend tests.** `packages/syrviscore-dashboard/frontend/package.json` has no `test` script and zero `*.test.*` files; the CI job runs `tsc --noEmit && vite build` only. The dashboard is the reader whose stale schema can reclassify running services as `unmanaged` (the design/63 D2 gate) — that classification logic is only tested on the Python side.
11. **Workflow drift.** `test.yml` was migrated to Node-24-native action majors (`checkout@v5`, `setup-python@v6`, `docker/*` v4/v7) with a long comment; `dashboard-image.yml` and `docker-state-exporter-image.yml` are still on `actions/checkout@v4`.
12. **Nothing gates the release chain.** CLAUDE.md's "Do not tag until … the dashboard image must be rebuilt + repinned" and "platform rollback below 0.5.16 is forbidden" are prose; no test or workflow enforces either.
13. **Test-suite/working-tree coupling.** The suite currently under measurement includes uncommitted changes (`schedule.py` +467 lines, `service_manager.py` +191, and three test files) — the jobs-pin/manifest work of design/66 is being written test-first but the gate has not yet seen it on a branch build.

## Raw material worth citing in the retrospective

- **Numbers:** 1507 tests / 25 s / 68% coverage (13891 statements) on 3.8; +423 mcp, +108 dashboard, +91 orphaned = 2129 written, **2038 actually gated**. 56 pytest modules, 2874 assertions, 1256 `tmp_path` uses, 20 `CliRunner` modules, 6 `skipif`s total, **0 xfails**. Slowest test 1.23 s.
- Root `pyproject.toml`, entire pytest config: `testpaths = ["tests"]`.
- `env: BLACK_VERSION: "24.8.0"` — "the last release that installs on Python 3.8… (26.x already reformats source 24.8.0 accepts)".
- `runs-on: ubuntu-22.04` — "Python 3.8 (the DSM runtime) is unavailable on newer runners".
- `dev-loop` DR step: `SYRVIS_HOME="$HOME_DIR" "$CTL" backup create` → `rm -rf "$HOME_DIR"` ("bare-metal: the installation is gone") → `restore` → `test -L "$HOME_DIR/current"`.
- `test_boot_reclaim_guard.py`: "The failure alarm was stored inside the thing it was supposed to alarm about."
- `test_wrapper_fallback.py`: `assert "Run 'syrvisctl install'" not in result.stderr` — "the old advice must be gone: it is the destructive one."
- `test_deploy_journal.py`: "Absent means no run; unparseable means I cannot tell" — different verdicts, and conflating them is what D6 clause 2 forbids."
- `test_breakers.py`: `assert breakers.MAX_BACKOFF_S == 600` — "Doctrine (design/60 §11), not a tuning knob"; `closes_breakers("s99") is False` — "Fails SAFE toward NOT resetting."
- `test_scheduled_jobs.py:304`: "read_crontab/write_crontab_atomic bind CRONTAB_PATH as a default argument at definition time, so reassigning the module constant is not enough."
- `paths.is_mounted_volume`: "sim mode must not reopen that hole, adversarial review #9."
- `test_deploy_bundle.py::test_failed_update_blast_radius_is_documented_behavior` — "Pin it so the documented limitation is intentional + regression-guarded" (a test whose purpose is to freeze a known deficiency).
- Live-today evidence of the orphan gap: `black --check packages/syrviscore/tests` → "1 file would be reformatted"; `ruff check` → "Found 3 errors."