# SyrvisCore Design Document

**Version:** 3.0
**Status:** Implemented

## Overview

SyrvisCore is a self-hosted infrastructure platform for Synology NAS that provides Traefik (reverse proxy), Portainer (container management), and Cloudflared (tunnel). It uses a split-package architecture with separate manager and service components.

**Package Names:**
- Manager Package: `syrviscore-manager`
- Service Package: `syrviscore`
- Manager CLI: `syrvisctl`
- Service CLI: `syrvis`
- SPK filename: `syrviscore-{version}-noarch.spk`

## Architecture

### Split-Package Design (v3)

The architecture separates concerns between two packages:

| Package | CLI | Location | Update Method | Purpose |
|---------|-----|----------|---------------|---------|
| `syrviscore-manager` | `syrvisctl` | SPK install dir | SPK reinstall (rare) | Version management |
| `syrviscore` | `syrvis` | Per-version venv | `syrvisctl install` (frequent) | Docker services |

**Key Principles:**

1. **SPK installs manager only** - Lightweight, immutable, rarely updated
2. **Manager installs service** - Downloads from GitHub releases
3. **One venv per version** - Clean isolation for service CLI
4. **Instant rollback** - Symlink switch
5. **Offline installation** - SPK bundles all dependencies

### System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Internet                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Cloudflare Edge │
                    │  (DNS + Proxy)  │
                    └────────┬────────┘
                             │ Cloudflare Tunnel
                             │ (encrypted, outbound-only)
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│                      Synology NAS                                 │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                    SyrvisCore (Layer 1)                     │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │  │
│  │  │ Cloudflared  │──│   Traefik    │  │   Portainer     │  │  │
│  │  │   (tunnel)   │  │ (reverse     │  │  (container     │  │  │
│  │  │   optional   │  │  proxy)      │  │   management)   │  │  │
│  │  └──────────────┘  └──────────────┘  └─────────────────┘  │  │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

### Directory Structure

```
/var/packages/syrviscore/target/      # SPK install (IMMUTABLE)
├── venv/bin/syrvisctl                 # Manager CLI
├── wheels/                            # Bundled Python dependencies
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

## Installation Flow

The installation follows a staged approach:

```
1. Install SPK          → Manager CLI (syrvisctl) installed
                           └─ All dependencies bundled, no network needed

2. syrvisctl install    → Service package downloaded from GitHub
                           └─ Creates venv, installs syrvis CLI

3. syrvis setup         → Interactive configuration
                           └─ Docker permissions, generate compose files

4. syrvis start         → Docker services running
```

### SPK Installation

The SPK package:
- Installs manager CLI (`syrvisctl`) to a virtual environment
- Bundles all Python dependencies (no network required)
- Creates global symlink to `/usr/local/bin/syrvisctl`
- Does NOT start any services or require configuration

### Service Installation

`syrvisctl install` handles:
- Download service wheel from GitHub releases
- Create version-specific virtual environment
- Install `syrvis` CLI
- Update `current` symlink

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

## Configuration

### Environment Variables

The system uses environment variables set during installation:

| Variable | Source | Description |
|----------|--------|-------------|
| `SYNOPKG_PKGDEST` | DSM | SPK installation directory |
| `SYRVIS_HOME` | Wizard | Service data directory (e.g., `/volume1/docker/syrviscore`) |
| `PACKAGE_NAME` | DSM | Always `syrviscore` |

### Manifest File

The manifest (`.syrviscore-manifest.json`) tracks installation state:

```json
{
  "schema_version": 3,
  "active_version": "0.2.0",
  "install_path": "/volume1/docker/syrviscore",
  "setup_complete": true,
  "versions": {
    "0.1.0": { "status": "available", "installed_at": "..." },
    "0.2.0": { "status": "active", "installed_at": "...", "activated_at": "..." }
  }
}
```

## Versioning

### Version Management

- Each service version installs to `versions/{version}/`
- Active version linked via `current` symlink
- Rollback changes symlink without reinstallation
- Old versions kept for rollback (configurable retention)

### Semantic Versioning

| Type | Example | When |
|------|---------|------|
| MAJOR | 2.0.0 | Breaking config changes |
| MINOR | 1.1.0 | Component upgrades, new features |
| PATCH | 1.0.1 | Bug fixes, security updates |

Manager and service can have different versions.

## Build System

### Monorepo Structure

```
SyrvisCore/
├── packages/
│   ├── syrviscore-manager/           # Manager package
│   │   ├── pyproject.toml
│   │   └── src/syrviscore_manager/
│   └── syrviscore/                   # Service package
│       ├── pyproject.toml
│       └── src/syrviscore/
├── spk/                              # SPK packaging
├── build-tools/                      # Build scripts
│   ├── build-manager.sh              # Build manager wheel + deps
│   ├── build-service.sh              # Build service wheel
│   └── build-spk.sh                  # Build SPK
└── build/config.yaml                 # Docker image versions
```

### Dependency Bundling

The SPK bundles all Python dependencies for offline installation:

1. `build-manager.sh` downloads wheels for target platform (Linux x86_64)
2. `build-spk.sh` bundles wheels into `package/wheels/`
3. `postinst` installs from bundled wheels (no pip download)

```bash
# Build downloads dependencies
pip download --dest dist/manager-deps/ \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 \
    syrviscore_manager-*.whl

# Install uses bundled wheels
pip install --no-index --find-links "$WHEELS_DIR" "$WHEEL"
```

## Security

### Secrets Management

- Secrets stored in `/volume1/secrets/` (encrypted folder recommended)
- Configuration in `.env` (never committed to git)
- TLS certificates symlinked from secrets directory

### Permissions

| File/Dir | Permissions | Notes |
|----------|-------------|-------|
| ACME certs | 0600 | SSL certificates |
| Config files | 0644 | docker-compose, traefik.yml |
| Scripts | 0755 | SPK scripts, wrapper |
| Docker socket | 0660 | Group-readable |

### Privilege Model

- SPK installation: Unprivileged (venv creation)
- `syrvis setup`: Self-elevating (prompts for sudo when needed)
- Docker operations: Via docker group membership

## Core Components

| Service | Purpose | Required |
|---------|---------|----------|
| Traefik v3 | Reverse proxy, SSL termination | Yes |
| Portainer CE | Container management UI | Yes |
| Cloudflared | Cloudflare Tunnel | Optional |

All Docker images use pinned version tags (no `:latest`).

## Related Documentation

- [SPK Installation Guide](spk-installation-guide.md)
- [CLI Reference - syrvisctl](cli-syrvisctl.md)
- [CLI Reference - syrvis](cli-syrvis.md)
- [Development Guide](dev-guide.md)
- [Build Tools](../build-tools/README.md)
