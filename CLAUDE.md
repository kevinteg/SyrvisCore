# Claude Rules for SyrvisCore

## Project Overview

SyrvisCore is a self-hosted infrastructure platform for Synology NAS that packages Traefik (reverse proxy), Portainer (container management), and Cloudflared (tunnel) into a single SPK package. The project uses modern Python packaging with a CLI built on Click.

**Current Phase:** MVP (Phase 1) - Focus on build system, basic CLI commands, SPK structure, and installation scripts.

**Architecture:** v2 - Versioned directory structure with CLI-driven setup (no wizard dependency).

## Key Information

| Item | Value |
|------|-------|
| Python Package | `syrviscore` |
| CLI Command | `syrvis` |
| Version File | `src/syrviscore/__version__.py` |
| Target Platform | Synology DSM 7.0+ |
| Installation Path | `/volume1/docker/syrviscore/` |
| Service Account | `syrvis-bot` |
| Python Version | 3.8.12 (matches Synology DSM) |

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

# Install the package in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify installation
syrvis --version
```

### Running Tests

**Always run tests before committing changes:**

```bash
# Activate virtualenv first
pyenv activate syrviscore

# Run all tests
make test

# Run tests with verbose output
pytest -v

# Run tests with coverage report
make test-cov

# Run a specific test file
pytest tests/test_cli.py -v

# Run tests across all supported Python versions
tox
```

### Development Workflow

```bash
# 1. Activate environment
pyenv activate syrviscore

# 2. Make changes to code

# 3. Format and lint
make format
make lint

# 4. Run tests
make test

# 5. Full validation (lint + test + build)
make all
```

## Architecture v2: Versioned Directory Structure

```
/volume1/docker/syrviscore/
├── current -> versions/0.1.0/     # Symlink to active version
├── versions/
│   ├── 0.0.1/                     # Previous version (rollback target)
│   │   ├── cli/venv/              # Python venv for this version
│   │   └── build/config.yaml      # Docker versions for this version
│   └── 0.1.0/                     # Current active version
│       ├── cli/venv/
│       └── build/config.yaml
├── config/                        # Shared configuration
│   ├── .env                       # User configuration
│   ├── docker-compose.yaml        # Generated compose file
│   └── traefik/                   # Traefik config
├── data/                          # Persistent data (never deleted)
│   ├── traefik/
│   ├── portainer/
│   └── cloudflared/
└── .syrviscore-manifest.json      # Installation manifest
```

### Key Principles

1. **SPK does minimum** - Only installs Python venv, no configuration
2. **CLI does everything** - Setup, config, updates, rollback
3. **Versioned installs** - Each version in separate directory
4. **Instant rollback** - Just change a symlink
5. **No wizard dependency** - DSM wizard was unreliable

## CLI Commands

### Core Commands

```bash
syrvis setup                   # Interactive setup with self-elevation
syrvis status                  # Show service status
syrvis start                   # Start all services
syrvis stop                    # Stop all services
syrvis restart                 # Restart all services
syrvis logs [service] [-f]     # View logs
syrvis doctor [--fix]          # Diagnose and fix issues
```

### Update Commands

```bash
syrvis update check            # Check for updates
syrvis update download [ver]   # Download update (don't install)
syrvis update install [ver]    # Install update (with backup)
syrvis update rollback         # Rollback to previous version
syrvis update list             # List installed versions
syrvis update cleanup          # Remove old versions (keep 2)
```

### Configuration Commands

```bash
syrvis config show             # Show current configuration
syrvis compose generate        # Generate docker-compose.yaml
syrvis config generate-traefik # Generate Traefik config
```

## Project Structure

```
SyrvisCore/
├── src/syrviscore/          # Main Python package
│   ├── cli.py               # CLI entry point (Click)
│   ├── setup.py             # Setup command with self-elevation
│   ├── update.py            # Update/rollback commands
│   ├── paths.py             # Versioned path management
│   ├── doctor.py            # Installation diagnostics
│   ├── docker_manager.py    # Docker SDK & compose
│   ├── compose.py           # Docker Compose generation
│   ├── traefik_config.py    # Traefik config generation
│   └── privileged_ops.py    # Root/privilege operations
├── tests/                   # Pytest tests
├── build-tools/             # SPK build utilities
├── spk/                     # Synology package structure
│   ├── INFO                 # SPK metadata
│   ├── scripts/             # Lifecycle scripts (sh)
│   └── conf/                # DSM configuration
├── docs/                    # Documentation
├── pyproject.toml           # Package config & dependencies
├── Makefile                 # Build automation
└── tox.ini                  # Multi-env testing
```

## Development Rules

### Python Packaging

- **Use `pyproject.toml` exclusively** - No `requirements.txt` or `setup.py`
- Dependencies: `[project.dependencies]`
- Dev dependencies: `[project.optional-dependencies.dev]`
- Install: `pip install -e .` or `pip install -e ".[dev]"`
- Never use `sudo pip install` - use venv

### Code Style

- **Formatter:** Black (line length: 100)
- **Linter:** Ruff
- **Type hints:** Encouraged but not required for MVP
- **Docstrings:** Google style for public functions
- **Naming:** `snake_case` for Python files and functions

### File Extensions

- YAML files: `.yaml` (not `.yml`)
- GitHub Actions: `.yml` (follows GitHub convention)

### Version Management

- Single source of truth: `src/syrviscore/__version__.py`
- Follow semantic versioning (MAJOR.MINOR.PATCH)
- Never hardcode version strings elsewhere

### Build System

- `build/config.yaml` contains ONLY Docker image versions
- Python dependencies managed via `pyproject.toml`, NOT `build/config.yaml`
- Generated artifacts go in `dist/` (SPK) or `build/` (intermediate)
- Use Makefile targets for common operations

### Key Makefile Targets

```bash
make dev-install    # Install with dev dependencies
make test           # Run pytest
make test-cov       # Run with coverage
make lint           # Run ruff
make format         # Format with black
make build-spk      # Build complete SPK
make validate       # Validate SPK structure
make all            # lint + test + build-spk
```

## SPK Scripts

### Requirements

- Written in **POSIX shell (sh)**, NOT bash
- Must be executable (`chmod +x`)
- Use structured logging
- Scripts: `preinst`, `postinst`, `preuninst`, `postuninst`, `preupgrade`, `postupgrade`

### SPK Installation Flow

1. **postinst** - Creates versioned directory, installs venv, creates symlink
2. User runs `syrvis setup` - Prompts for config, sets up Docker permissions
3. **postupgrade** - Installs new version, preserves old for rollback

### Logging Standard

```sh
log_info() { log_msg "INFO" "$1"; }
log_error() { log_msg "ERROR" "$1"; }
```

## Security

- Secrets go in `/volume1/secrets/` on Synology
- Use `.env` files locally (never commit)
- Provide `.env.template` as reference
- File permissions: ACME certs `0600`, configs `0644`, scripts `0755`

## Git Practices

- Atomic, well-described commits
- **DO commit:** `build/config.yaml` (versioned Docker tags)
- **DON'T commit:** `.env`, `venv/`, `__pycache__/`, `*.spk`, `dist/`

## What to Avoid

- Using `requirements.txt` or `setup.py`
- Putting Python packages in `build/config.yaml`
- Hardcoding version strings
- Using `:latest` Docker tags
- Committing sensitive data
- Mixing tabs and spaces (use spaces)
- Relying on DSM wizard (it's unreliable)

## External Dependencies

| Service | Purpose | Notes |
|---------|---------|-------|
| Traefik v3 | Reverse proxy | SSL termination, Let's Encrypt |
| Portainer CE | Container management | Web UI |
| Cloudflared | Tunnel | Optional, Cloudflare integration |

All Docker images use specific version tags (no `:latest`).

## Design Principles

- **CLI-first** - No web service (Flask/FastAPI/Django)
- **Single-node** - Docker Compose orchestration, not Kubernetes
- **Simple over complex** - Minimal viable solution first
- **Community-friendly** - No hardcoded personal info, configuration-driven
- **Self-elevating** - CLI prompts for sudo when needed

## Resources

- Design Doc: `docs/design-doc.md`
- Architecture Proposal: `docs/architecture-proposal-v2.md`
- SPK Guide: `docs/spk-installation-guide.md`
- Build Tools: `build-tools/README.md`
- Click docs: https://click.palletsprojects.com/
- Python packaging: https://packaging.python.org/
