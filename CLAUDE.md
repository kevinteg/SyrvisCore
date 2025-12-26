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
| Installation Path | `/volume1/docker/syrviscore/` |
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
└── syrviscore_manager-*.whl

/volumeX/docker/syrviscore/            # SYRVIS_HOME (managed by syrvisctl)
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
└── build/config.yaml                 # Docker image versions
```

## Getting Started

### Prerequisites

- **pyenv** - Python version management
- **pyenv-virtualenv** - Virtual environment plugin for pyenv

### Environment Setup

```bash
# Install Python 3.8.12 via pyenv (matches Synology NAS)
pyenv install 3.8.12

# Create a virtual environment for this project
pyenv virtualenv 3.8.12 syrviscore

# Activate the virtual environment
pyenv activate syrviscore

# Install both packages in editable mode
pip install -e "packages/syrviscore-manager[dev]"
pip install -e "packages/syrviscore[dev]"

# Verify installation
syrvisctl --version
syrvis --version
```

### Running Tests

```bash
# Activate virtualenv first
pyenv activate syrviscore

# Run all tests
make test

# Run tests with verbose output
pytest -v

# Run a specific test file
pytest tests/test_cli.py -v
```

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
syrvisctl uninstall <version> # Remove a service version
syrvisctl list                # List installed versions
syrvisctl activate <version>  # Switch active version
syrvisctl rollback            # Rollback to previous version
syrvisctl check               # Check for updates
syrvisctl info                # Show installation info
syrvisctl cleanup [--keep N]  # Remove old versions
syrvisctl migrate             # Migrate from legacy installation
```

### syrvis (Service)

```bash
syrvis setup                  # Interactive setup with self-elevation
syrvis status                 # Show service status
syrvis start                  # Start all services
syrvis stop                   # Stop all services
syrvis restart                # Restart all services
syrvis logs [service] [-f]    # View logs
syrvis doctor [--fix]         # Diagnose and fix issues
syrvis config show            # Show current configuration
syrvis compose generate       # Generate docker-compose.yaml
```

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

### Build System

- `build/config.yaml` contains Docker image versions (bundled with service)
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
- **DO commit:** `build/config.yaml` (versioned Docker tags), `packages/`
- **DON'T commit:** `.env`, `venv/`, `__pycache__/`, `*.spk`, `dist/`

## External Dependencies

| Service | Purpose | Notes |
|---------|---------|-------|
| Traefik v3 | Reverse proxy | SSL termination, Let's Encrypt |
| Portainer CE | Container management | Web UI |
| Cloudflared | Tunnel | Optional, Cloudflare integration |

All Docker images use specific version tags (no `:latest`).

## Design Principles

- **CLI-first** - No web service
- **Split packages** - Manager (immutable) vs Service (updatable)
- **Single-node** - Docker Compose orchestration
- **Simple over complex** - Minimal viable solution first
- **Self-elevating** - CLI prompts for sudo when needed

## Systems Engineering Best Practices

### Reproducibility

All operations must be reproducible and automated. **Never require manual out-of-band privileged operations.**

**Bad:**
```bash
# Don't do this - requires manual sudo
sudo ln -sf /path/to/cmd /usr/local/bin/cmd
sudo chown root:root /some/file
```

**Good:**
```bash
# Use PATH or environment variables
source /var/packages/syrviscore/target/syrviscore.profile
# Or use the full path
/var/packages/syrviscore/target/venv/bin/syrvisctl
```

### Command Access

SPK scripts run as an unprivileged package user. To make commands accessible:

1. **Create a profile snippet** in the package directory that users can source
2. **Document the full path** to the command
3. **Never attempt to write to system directories** like `/usr/local/bin`

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
| `SYRVIS_HOME` | Service data directory | `/volumeX/docker/syrviscore` |
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
