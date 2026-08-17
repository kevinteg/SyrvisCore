# Claude Rules for SyrvisCore

## Project Overview

SyrvisCore is a self-hosted infrastructure platform for Synology NAS that packages Traefik (reverse proxy), Portainer (container management), and Cloudflared (tunnel). The project uses a split-package architecture with separate manager and service components.

**Current Phase:** MVP (Phase 1) - Focus on build system, basic CLI commands, SPK structure, and installation scripts.

**Architecture:** v3 - Split packages with `syrvisctl` (manager) and `syrvis` (service).

## Key Information

| Item | Value |
|------|-------|
| Manager Package | `syrviscore-manager` |
| Service Package | `syrviscore` |
| Manager CLI | `syrvisctl` |
| Service CLI | `syrvis` |
| Target Platform | Synology DSM 7.0+ |
| Installation Path | `/volumeX/syrviscore/` (auto-detected from package volume) |
| Python Version | 3.8.12 (matches Synology DSM) |

## Architecture: Split Packages

### Two Packages

| Package | CLI | Location | Update Method | Purpose |
|---------|-----|----------|---------------|---------|
| `syrviscore-manager` | `syrvisctl` | SPK install dir | SPK reinstall (rare) | Version management |
| `syrviscore` | `syrvis` | Per-version venv | `syrvisctl install` (frequent) | Docker services |

### Directory Structure

```
/var/packages/syrviscore/target/      # SPK install (IMMUTABLE)
├── venv/bin/syrvisctl                 # Manager CLI
├── syrviscore_manager-*.whl
└── syrviscore.profile                 # Source this to add to PATH

/volumeX/syrviscore/                   # SYRVIS_HOME (auto-detected from package volume)
├── current -> versions/0.2.0          # Symlink to active version
├── versions/
│   ├── 0.1.0/cli/venv/bin/syrvis      # Previous version
│   └── 0.2.0/cli/venv/bin/syrvis      # Active version
├── config/                            # Shared configuration
│   ├── .env
│   └── docker-compose.yaml
├── data/                              # Persistent data
├── bin/syrvis                         # Wrapper script
└── .syrviscore-manifest.json
```

### Key Principles

1. **SPK installs manager only** - Lightweight, immutable
2. **Manager installs service** - Downloads from GitHub releases
3. **One venv per version** - Clean isolation
4. **Instant rollback** - Symlink switch
5. **Manager rarely updates** - Only new features require SPK reinstall

## Monorepo Structure

```
SyrvisCore/
├── packages/
│   ├── syrviscore-manager/           # Manager package (SPK)
│   │   ├── pyproject.toml
│   │   └── src/syrviscore_manager/
│   │       ├── cli.py                # syrvisctl entry point
│   │       ├── version_manager.py    # Install/activate/rollback
│   │       ├── downloader.py         # GitHub release downloads
│   │       └── manifest.py           # Manifest management
│   │
│   └── syrviscore/                   # Service package
│       ├── pyproject.toml
│       └── src/syrviscore/
│           ├── cli.py                # syrvis entry point
│           ├── setup.py              # Interactive setup
│           ├── docker_manager.py     # Container management
│           └── ...
│
├── spk/                              # SPK (manager only)
├── build-tools/
│   ├── build-manager.sh              # Build manager wheel
│   ├── build-service.sh              # Build service wheel
│   ├── build-spk.sh                  # Build SPK (manager only)
│   └── release-service.sh            # GitHub release for service
├── tests/                            # Pytest tests
└── build/                            # scratch build output (config.yaml is generated, not committed)
```

## Getting Started

### Prerequisites

- **pyenv** - Python version management
- **pyenv-virtualenv** - Virtual environment plugin for pyenv

### Environment Setup

The interpreter is pinned by the committed **`.python-version`** (the `syrviscore`
pyenv virtualenv, Python 3.8.12 — matches Synology DSM). Because that file selects
the env, pyenv's shims resolve `python3` / `pytest` / `make` targets to it
**automatically from this directory** — no manual `pyenv activate`, and it works in
non-interactive shells (CI, agents, `make`). That single file is what makes the
interpreter deterministic; without it, a bare `python3` falls through to the pyenv
*global* (e.g. a Homebrew Python with no dev deps).

One command bootstraps everything — it creates the pyenv virtualenv only if it's
missing, then installs both packages editable with dev deps:

```bash
make env
```

Prereq: `pyenv` + `pyenv-virtualenv` (`brew install pyenv pyenv-virtualenv`).
`make env` is idempotent — re-running it on an existing env just refreshes the
editable install. Verify with `syrvisctl --version` and `syrvis --version`.

### Running Tests

No activation step — `.python-version` selects the interpreter:

```bash
make test                                       # full suite (python3 -m pytest via the pinned env)
make check                                      # lint + test
python3 -m pytest tests/test_seam_sync.py -q    # a single file
```

If `python3 --version` from the repo root isn't `3.8.12`, either you're not in the
repo root (`.python-version` is per-directory) or the env is missing — run `make env`.

### Building Packages

```bash
# Build manager wheel
./build-tools/build-manager.sh

# Build service wheel
./build-tools/build-service.sh

# Build SPK (includes manager only)
./build-tools/build-spk.sh

# Create GitHub release for service
./build-tools/release-service.sh
```

## CLI Commands

### syrvisctl (Manager)

```bash
syrvisctl install [version]   # Download and install service from GitHub
syrvisctl install --wheel F   # Install from a local wheel (dev loop, no network)
syrvisctl uninstall <version> # Remove a service version
syrvisctl list [--json]       # List installed versions
syrvisctl activate <version>  # Switch active version
syrvisctl rollback [version]  # Rollback to previous version (full restore)
syrvisctl check [--json]      # Check for updates
syrvisctl info [--json]       # Show installation info
syrvisctl doctor [--json]     # Diagnose from the DSM rootfs with NO resolvable
                              # home: boot-hook presence/currency, a
                              # /volume*/syrviscore* census naming any DSM
                              # collision rename, and the seam accounts' shells.
                              # The verb for "the seam is dead after a reboot".
syrvisctl cleanup [--keep N]  # Remove old versions
syrvisctl backup <cmd>        # Backup management (list/create/cleanup)
syrvisctl restore [file]      # Restore from backup (disaster recovery)
syrvisctl seam sync|status    # Regenerate operator shim+sudoers from the active
                              # version (activate/rollback do this automatically
                              # when provisioned with auto_seam_update)
```

All prompts are bypassable with `-y`; read commands support `--json`
(the contract the MCP server layer builds on). Library modules raise typed
`SyrvisError` exceptions and never print — `cli.py` is a thin shell.
See `docs/v2-design.md` for the v2 architecture and phase plan.

### syrvis (Service)

```bash
syrvis setup                  # Interactive setup with self-elevation
syrvis status                 # Show service status (+ halted banner/runstate)
syrvis start                  # Start all services
syrvis stop                   # Stop all services
syrvis restart [--graceful]   # Restart core (or the whole instance, gracefully)
syrvis logs [service] [-f]    # View logs
syrvis doctor [--fix]         # Diagnose and fix issues
syrvis config show            # Show current configuration
syrvis compose generate       # Generate docker-compose.yaml

# Deployment system (see docs/deployments.md)
syrvis history [workload]     # Deployment revisions (image/env-names/volumes; redacted)
syrvis service rollback NAME [--to REV]  # Redeploy a prior revision (records rollback_of)
syrvis shutdown --reason ups|maintenance # Graceful instance halt (hooks, stop grace, VMs)
syrvis resume                 # Bring a halted instance back (core -> VMs -> L2)

# Core stack (declarative core-tier: config/stack.yaml)
syrvis stack list             # Show declared core services + running state
syrvis stack enable <svc>     # Enable a core service (--subdomain, --exposure)
syrvis stack disable <svc>    # Disable an optional core service
syrvis stack apply            # Regenerate docker-compose from the stack
syrvis stack hostnames        # Required external DNS/tunnel state (--json)

# Layer 2 Services (user-installable containers)
syrvis service add <git-url>  # Add service from git repo (--subdomain, --exposure)
syrvis service run <name>     # Run image-first: --image, --exposure, --port, --env
syrvis service remove <name>  # Remove a service
syrvis service list           # List installed services
syrvis service start <name>   # Start a service
syrvis service stop <name>    # Stop a service (EPHEMERAL intent: writes
                              # enabled: false — the next GitOps `apply`
                              # overwrites it from the repo)
syrvis service shed --reason R [--until D] -- <name>
                              # DURABLE intent: records {reason, since, until}
                              # in data/state/intent.json (OUTSIDE the
                              # declaration set, so it survives apply/deploy/
                              # reconcile/resume/boot) and stops the container
                              # without touching the declaration. Reconcile
                              # treats shed as an enabled:false overlay; start/
                              # recreate refuse; deploy lands bits but does not
                              # start. The verb for a load-shed.
syrvis service unshed <name>  # Lift the shed (starts nothing — reconcile does)
syrvis service recreate <name>  # Replace the container (up -d --force-recreate),
                              # writing NO declared intent. The only verb that
                              # re-reads a changed env_file — Docker bakes env in
                              # at container CREATE time, so `restart` cannot.
syrvis service update <name>  # Update from git repo
syrvis service task --task T -- <name>  # Run a DECLARED one-shot task (tasks: block)
syrvis service set-image --image REF -- <name>  # Re-pin an L2 image + redeploy (declarative update)
syrvis service catalog        # Bundled catalog templates (--json)

# Scheduled jobs (OPTIONAL; dormant with an empty config/jobs.d)
syrvis schedule list          # Declared jobs + the managed crontab block (--json;
                              # plan.scripts = per-job presence+sha256,
                              # plan.confs = per-job conf presence+size)
syrvis schedule dsm-tasks     # READ-ONLY census of DSM's OWN Task Scheduler
                              # (synoschedtask --get). SyrvisCore never creates,
                              # edits or deletes a DSM task — this only reports
                              # what ELSE is scheduled on the box, the gap that
                              # let a task point outside the managed block
                              # unnoticed (design/20).
sudo syrvis schedule apply    # LOCAL reconcile of the managed crontab block
sudo syrvis schedule sync     # Clone the root-configured source, install + apply

# VMs (config/vms.d/*.yaml; Synology VMM — adopt-first, never created here)
syrvis vm list                # Declared VMs + live power (--json); carries
                              # stop_timeout (the VM tier's declared claim on the
                              # shutdown budget) and description
syrvis vm status|start|stop|restart NAME

# Updates + export
syrvis updates [--json --refresh]  # Available container-image updates (report-only)
syrvis export [--json --reveal-secrets]  # Snapshot instance as syrvis-instance/v1 (redacted)

# Profiles (platform-curated service sets)
syrvis profile list           # Available profiles (--json)
syrvis profile enable <name>  # Declare a profile's services + seed default configs

# Operator-seam bundle verbs (stdin-only; secrets never on argv)
syrvis apply                  # syrvis-instance/v1: .env + stack.yaml + services.d
                              # (--dry-run, --allow-secret-change; see docs/seam-contract.md)
syrvis deploy <name>          # syrvis-bundle/v1: one service's manifest+configs+secrets
syrvis secret set <name>      # Write a service's env_file from stdin
syrvis config set <name>      # Write a declared job's conf from stdin
```

## Service Exposure (internal vs tunnel)

Every routed service declares an `exposure`: `internal` (LAN-only; Traefik + a
DNS-01 cert — the only external step is a LAN DNS record → Traefik) or `tunnel`
(remote via the Cloudflare Tunnel + Access). SyrvisCore routes both identically;
exposure is *declared intent* consumed by `syrvis stack hostnames`, which reports
the concrete record each host needs. SyrvisCore never touches DNS or the Cloudflare
API — a deployment repo (e.g. home-tech) reconciles the reported state via its own
MCP tooling. The repo stays generic: no domain, IPs, accounts, or service catalog.

## Installation Flow

1. **Install SPK** - Installs manager (`syrvisctl`) to `/var/packages/syrviscore/target/`
2. **Run `syrvisctl install`** - Downloads and installs service from GitHub
3. **Run `syrvis setup`** - Interactive configuration, Docker permissions
4. **Run `syrvis start`** - Start Docker services

## Development Rules

### Python Packaging

- **Use `pyproject.toml` exclusively** - No `requirements.txt` or `setup.py`
- Dependencies: `[project.dependencies]`
- Dev dependencies: `[project.optional-dependencies.dev]`
- Never use `sudo pip install` - use venv

### Code Style

- **Formatter:** Black (line length: 100)
- **Linter:** Ruff
- **Type hints:** Encouraged but not required for MVP
- **Docstrings:** Google style for public functions

### Version Management

- Manager version: `packages/syrviscore-manager/src/syrviscore_manager/__version__.py`
- Service version: `packages/syrviscore/src/syrviscore/__version__.py`
- Follow semantic versioning (MAJOR.MINOR.PATCH)
- Manager and service can have different versions
- **Bump the LAST SEGMENT only** (0.5.15 → 0.5.16), features included.

#### 0.5.16 — deploy & lifecycle robustness (in progress)

The 2026-08-16 review's deploy-plane package. Landed here (SC-B):

- `deploy_bundle` reads the recorded config/secret digests back, so a
  byte-identical redeploy no longer force-recreates a secrets-bearing service
  (design/60 G1); records gain `secrets_checksum`
- reserve-first shutdown clamping — stores keep their declared grace, consumers
  clamp into what remains (design/63 D6); VM windows anchored at the ACPI issue
- reconcile orders bring-up by REVERSED shutdown bands (interim until 63 M1's
  `depends_on` graph) and classes an exited `restart: no` service TERMINAL
- `vms.d` gains `description`; `vm list` reports the budget census fields
- seam read verb `schedule dsm-tasks` (DSM Task Scheduler census)
- `syrvisctl install` refuses while a collision-renamed platform root exists
- S99 reclaim guard gains a bounded, advisory `synocheckshare` phase gate
  (deliberately NOT a `BOOT_HOOK_CONTRACT` bump — see the constant's comment)

Landed here (SC-C) — design/63 M1, design/60 §5 D6 + §11.1 point 6, design/37
Phase 1:

- **`depends_on` is a real key again** — orchestration-level, string entries
  `name[:readiness]` with `started` (default) / `healthy` / `soft`
  (design/63 D1). The old blanket reject was right for the COMPOSE meaning and
  is superseded for the ORCHESTRATION one; nothing is ever emitted into a
  generated compose file. Parse splits the suffix BEFORE name validation, and an
  unknown suffix is an error, never a default.
- **Whole-set graph validation** (`services_d.build_dependency_graph`, run from
  `load_declarations`): cycles, unknown targets and `healthy`-onto-a-checkless-
  target invalidate the DECLARING file only — isolation preserved. A hard edge
  onto a **disabled/shed/invalid** target is a plan-time `blocked` BUCKET, never
  a validation error and never a failure (design/63 D2 as amended, `opc:F10`):
  a deliberate load-shed must not fail every hourly reconcile for its dependants.
- **Topological plan ordering**, with SC-B's reversed-band key as the
  TIE-BREAKER inside each wave (one sort, not two). No edges anywhere ⇒
  byte-identical to the band-only interim.
- **`data/state/deploy-journal.json`** — `schema_version` (unknown ⇒ report
  `unknown` and refuse to act; unparseable is NOT absent), terminal set
  `{started, healthy, skipped, failed}` with `failed` TERMINAL, the 60-minute
  staleness rule (a stale journal annotates, never refuses), atomic writes,
  bounded events. Written from the deploy path; `started` never `healthy`,
  because 0.5.16 verifies no health.
- **`data/state/breakers.json`** — the ONE durable breaker store, one row per
  `{plane, context}`, the only place a count lives. Cross-plane suppression, "a
  close closes all", the capped jittered curve, and `by` as a FIELD (only
  `cli:`/`seam:`/`mcp:` close; `hostd`/`s99`/`cron` inherit). The journal's
  `breaker:` blocks are MIRRORS. `--force` on `guard_bulk_degraded` closes the
  breakers in its scope. **Recording only** — the skip/page/half-open ENGINE is
  design/63 M2.
- **`volume_locations:`** (design/37 §4 Phase 1, unblocking design/64 D7) —
  per-named-volume placement: `{<declared volume>: /volume<N>}` binds that
  volume from `<override>/<apps-root>/apps/<name>/data/<vol>` while the app home
  stays put. Per-override mount check on every materialize/start path,
  containment assertions, a per-VOLUME change refusal, purge coverage, and the
  override named in a comment in the generated compose.

Do not tag until the release chain runs: the **dashboard image must be rebuilt +
repinned** with the edge schema before any `depends_on` lands in a real
`services.d` (design/63 D2's reader-enumeration gate — a stale dashboard would
mark every edge-carrying declaration invalid and reclassify its running service
`unmanaged`). And while any edge exists, **platform rollback below 0.5.16 is
forbidden.**

### Build System

- Docker image versions are pinned in `DEFAULT_DOCKER_IMAGES` (`packages/syrviscore/src/syrviscore/compose.py`) — the committed source of truth. A release may attach a `config.yaml` asset, which `syrvisctl install` bundles into the version tree (`versions/<v>/build/config.yaml`) and which then overrides the built-in pins; `build-tools/select-docker-versions.py` keeps both in sync.
- Manager SPK is minimal (~20KB wheel)
- Service wheel includes all dependencies

## SPK Scripts

### Requirements

- Written in **POSIX shell (sh)**, NOT bash
- Must be executable (`chmod +x`)
- Only handles manager installation

### Synology Environment Notes

- **Synology DSM uses full GNU coreutils**, NOT BusyBox (at least on x86_64 models)
- Standard GNU tools available: `sed`, `awk`, `grep`, etc.
- Do NOT assume limited/BusyBox implementations

### SPK Installation Flow

1. **postinst** - Creates manager venv, installs manager wheel, creates profile snippet
2. User sources profile: `source /var/packages/syrviscore/target/syrviscore.profile`
3. User runs `syrvisctl install` - Downloads and installs service
4. User runs `syrvis setup` - Configures services
5. **postupgrade** - Updates manager venv, updates profile snippet

## Security

- Secrets go in `/volume1/secrets/` on Synology
- Use `.env` files locally (never commit)
- File permissions: ACME certs `0600`, configs `0644`, scripts `0755`

## Git Practices

- Atomic, well-described commits
- **DO commit:** `packages/` (image pins live in compose.py's `DEFAULT_DOCKER_IMAGES`)
- **DON'T commit:** `.env`, `venv/`, `__pycache__/`, `*.spk`, `dist/`

## External Dependencies

| Service | Purpose | Notes |
|---------|---------|-------|
| Traefik v3 | Reverse proxy | SSL termination, Let's Encrypt |
| Portainer CE | Container management | Web UI |
| Cloudflared | Tunnel | Optional, Cloudflare integration |

All Docker images use specific version tags (no `:latest`).

## Design Principles

- **Deterministic core, thin adapters** - One tested library layer does the work; the
  `syrvis` CLI, the MCP server (`packages/syrviscore-mcp`), and the web dashboard
  (`packages/syrviscore-dashboard`, a base-tier container — see `docs/dashboard.md`) are all
  thin adapters over it. Anything an adapter can do, `ssh nas && syrvis …` can do.
- **Split packages** - Manager (immutable) vs Service (updatable)
- **Single-node** - Docker Compose orchestration
- **Simple over complex** - Minimal viable solution first
- **Self-elevating** - CLI prompts for sudo when needed

## Systems Engineering Best Practices

### Reproducibility (CRITICAL)

**All operations must be reproducible and automated.** This is a core design principle:

1. **Never require manual out-of-band operations** - If `syrvis setup` creates something, it must create everything needed. Users should never need to manually run commands after setup.

2. **Boot persistence must be automatic** - If a service needs to run at boot, the setup process must install the boot hook. Don't document "run this manually after reboot."

3. **Self-contained installation** - Running `syrvis setup` should result in a fully working system that survives reboots without additional intervention.

**Bad:**
```bash
# Don't do this - requires manual intervention
sudo ln -sf /path/to/cmd /usr/local/bin/cmd
# Or documenting: "Add to Task Scheduler manually"
# Or: "Run this script after each reboot"
```

**Good:**
```bash
# Setup installs everything including boot hooks
sudo syrvis setup
# After reboot, everything works automatically
```

### Boot Persistence

The installation creates two scripts for boot persistence:

| Script | Location | Purpose |
|--------|----------|---------|
| Startup script | `$SYRVIS_HOME/bin/syrvis-startup.sh` | Creates macvlan shim, sets Docker permissions |
| Boot hook | `/usr/local/etc/rc.d/S99syrviscore.sh` | Calls startup script on boot |

Both are created automatically by `syrvis setup`. The boot hook ensures services work after Synology reboots.

### Command Access

SPK scripts run as an unprivileged package user. To make commands accessible:

1. **Create a profile snippet** in the package directory that users can source
2. **Document the full path** to the command
3. **Use self-elevation** - CLI commands should detect when they need sudo and re-execute themselves

The package creates `/var/packages/syrviscore/target/syrviscore.profile` which users can source to add `syrvisctl` to their PATH.

### Privilege Separation

| Operation | Runs As | Can Write To |
|-----------|---------|--------------|
| SPK install scripts | Package user (`syrviscore`) | `$SYNOPKG_PKGDEST` only |
| CLI commands | User who invokes | Depends on user |
| Docker operations | User in `docker` group | Docker socket |

### Environment Variables

Use environment variables for configuration, not hardcoded paths:

| Variable | Purpose | Default |
|----------|---------|---------|
| `SYNOPKG_PKGDEST` | SPK installation directory | `/var/packages/syrviscore/target` |
| `SYRVIS_HOME` | Service data directory | `/volumeX/syrviscore` |
| `DSM_SIM_ACTIVE` | Simulation mode flag | `0` |
| `DSM_SIM_ROOT` | Simulation root path | (unset) |

### Logging

All operations must log to deterministic locations:

| Log File | Purpose |
|----------|---------|
| `/tmp/syrviscore-install.log` | SPK installation log |
| `/tmp/syrviscore-pip.log` | Pip installation output |
| `$SYRVIS_HOME/logs/` | Runtime service logs |

## Resources

- Design Doc: `docs/design-doc.md`
- Architecture Proposal: `docs/architecture-proposal-v2.md`
- SPK Guide: `docs/spk-installation-guide.md`
- Build Tools: `build-tools/README.md`
