# Claude Rules for SyrvisCore

## Project Overview

SyrvisCore is a self-hosted infrastructure platform for Synology NAS that packages Traefik (reverse proxy), Portainer (container management), and Cloudflared (tunnel) into a single SPK package. The project uses modern Python packaging with a CLI built on Click.

**Current Phase:** MVP (Phase 1) - Focus on build system, basic CLI commands, SPK structure, and installation scripts.

## Key Information

| Item | Value |
|------|-------|
| Python Package | `syrviscore` |
| CLI Command | `syrvis` |
| Version File | `src/syrviscore/__version__.py` |
| Target Platform | Synology DSM 7.0+ |
| Installation Path | `/volume1/docker/syrviscore/` |
| Service Account | `syrvis-bot` |
| Python Version | 3.8+ (3.11 recommended) |

## Getting Started

### Prerequisites

- **pyenv** - Python version management
- **pyenv-virtualenv** - Virtual environment plugin for pyenv

### Environment Setup

```bash
# Install Python 3.11 via pyenv (if not already installed)
pyenv install 3.11

# Create a virtual environment for this project
pyenv virtualenv 3.11 syrviscore

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

## Project Structure

```
SyrvisCore/
├── src/syrviscore/          # Main Python package
│   ├── cli.py               # CLI entry point (Click)
│   ├── paths.py             # SYRVIS_HOME management
│   ├── docker_manager.py    # Docker SDK & compose
│   ├── compose.py           # Docker Compose generation
│   ├── traefik_config.py    # Traefik config generation
│   ├── setup.py             # Privileged setup operations
│   ├── doctor.py            # Installation diagnostics
│   └── privileged_ops.py    # Root/privilege operations
├── tests/                   # Pytest tests
├── build-tools/             # SPK build utilities
├── spk/                     # Synology package structure
│   ├── INFO                 # SPK metadata
│   ├── scripts/             # Lifecycle scripts (sh)
│   ├── conf/                # DSM configuration
│   └── WIZARD_UIFILES/      # Installation wizard UI
├── build/                   # Generated configs
│   └── config.yaml          # Docker image versions ONLY
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

### Testing

- Framework: pytest
- Location: `tests/` directory
- Multi-version: tox (Python 3.8-3.11)
- Mirror source structure in tests

```bash
pytest              # Run all tests
pytest -v           # Verbose
pytest --cov        # With coverage
tox                 # All Python versions
```

## SPK Scripts

### Requirements

- Written in **POSIX shell (sh)**, NOT bash
- Must be executable (`chmod +x`)
- Use structured logging (see below)
- Scripts: `preinst`, `postinst`, `preuninst`, `postuninst`, `preupgrade`, `postupgrade`, `start-stop-status`

### Logging Standard

```sh
SCRIPT_NAME="preinst"
LOG_FILE="/tmp/syrviscore-install.log"

log_msg() {
    LEVEL="$1"
    MSG="$2"
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    LOG_LINE="[$LEVEL] [$TIMESTAMP] [$SCRIPT_NAME] $MSG"

    if [ "$LEVEL" = "ERROR" ]; then
        echo "$LOG_LINE" >&2
    else
        echo "$LOG_LINE"
    fi

    echo "$LOG_LINE" >> "$LOG_FILE" 2>/dev/null || true
}

log_info() { log_msg "INFO" "$1"; }
log_warn() { log_msg "WARN" "$1"; }
log_error() { log_msg "ERROR" "$1"; }
```

### Logging Rules

- Start with context (script name, working directory, key env vars)
- Use step numbering: `[X/Y] Step description...`
- Log all operations with results
- End with summary and log file location
- **Never log passwords/tokens in full** - use `${token:+<set>}`

## CLI Development

### Adding a Command

1. Add function to `src/syrviscore/cli.py` with `@cli.command()` decorator
2. Use Click decorators for options/arguments
3. Add docstring for help text
4. Test with `syrvis <command>`

### Command Structure

```
syrvis
├── setup              # Privileged setup wizard
├── doctor             # Diagnostics and self-healing
├── compose generate   # Generate docker-compose.yaml
├── core start|stop|restart|status|logs
└── config generate-traefik
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

## Resources

- Design Doc: `docs/design-doc.md`
- SPK Guide: `docs/spk-installation-guide.md`
- Build Tools: `build-tools/README.md`
- Click docs: https://click.palletsprojects.com/
- Python packaging: https://packaging.python.org/
