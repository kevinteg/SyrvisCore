# SyrvisCore Architecture Proposal v2

## Problem Statement

The current architecture has several issues discovered during DSM 7.0 testing:

1. **Wizard Not Working**: DSM wizard cannot reliably prompt for installation variables
2. **Privilege Confusion**: Unclear separation between what SPK scripts can do vs CLI
3. **No Rollback**: Updates overwrite previous installation with no easy rollback
4. **Complex Two-Phase**: Users must remember to run `sudo syrvis setup` after installation

## Proposed Solution

### Core Principle: SPK Does Minimum, CLI Does Everything

```
SPK Installation (Unprivileged, Automatic)
├── Install Python venv with syrviscore package
├── Create basic directory structure
└── That's it - no configuration, no wizard dependencies

CLI Tool (Interactive, Handles Everything)
├── Initial setup (privileged operations)
├── Configuration (prompts for all settings)
├── Service management (start/stop/status)
├── Updates (download, install, rollback)
└── Diagnostics (doctor command)
```

### Versioned Installation Structure

```
/volume1/docker/syrviscore/
├── current -> versions/0.1.0/           # Symlink to active version
├── versions/
│   ├── 0.0.1/                           # Previous version (rollback target)
│   │   ├── cli/venv/
│   │   ├── build/config.yaml
│   │   └── .version-manifest.json
│   ├── 0.1.0/                           # Current active version
│   │   ├── cli/venv/
│   │   ├── build/config.yaml
│   │   └── .version-manifest.json
│   └── 0.2.0/                           # Downloaded, not yet activated
│       └── ...
├── data/                                # Persistent data (never deleted)
│   ├── traefik/
│   ├── portainer/
│   └── cloudflared/
├── config/                              # User configuration (preserved across versions)
│   ├── .env
│   ├── docker-compose.yaml
│   └── traefik/
│       ├── traefik.yml
│       └── dynamic.yml
└── .syrviscore-manifest.json            # Root manifest (tracks active version, history)
```

## Phase 1: SPK Installation (Unprivileged)

### What SPK Does

| Operation | Privilege | Notes |
|-----------|-----------|-------|
| Create `/volume1/docker/syrviscore/` | Unprivileged | Uses default volume or env var |
| Create `versions/{version}/` directory | Unprivileged | Version from SPK INFO |
| Install Python venv | Unprivileged | `python3 -m venv` |
| Install syrviscore wheel | Unprivileged | `pip install *.whl` |
| Copy build/config.yaml | Unprivileged | Docker image versions |
| Create version manifest | Unprivileged | Track this version's metadata |
| Create `current` symlink | Unprivileged | Point to new version |
| Create CLI wrapper script | Unprivileged | In `/var/packages/syrviscore/target/` |

### What SPK Does NOT Do

- No wizard prompts (wizard is unreliable)
- No configuration generation
- No Docker operations
- No privileged operations
- No service startup

### SPK Scripts Simplified

**preinst**: Only checks Python 3 exists
**postinst**: Installs venv, creates version directory, symlink
**preupgrade**: Records current version for potential rollback
**postupgrade**: Installs new version, updates symlink (keeps old version)
**preuninst**: Warns user about data preservation
**postuninst**: Cleanup (privileged, run by DSM as root)

## Phase 2: CLI Setup (Interactive)

After SPK installation, user runs:

```bash
syrvis setup
```

This single command handles everything:

### Setup Flow

```
$ syrvis setup

SyrvisCore Setup
================

[1/7] Checking prerequisites...
  ✓ Python 3.8.12
  ✓ Docker installed
  ✓ Docker daemon running

[2/7] Privilege check...
  ⚠ Some operations require root privileges.

  Run with sudo? [Y/n]: y
  [sudo] password for admin:

[3/7] Docker access setup...
  Creating docker group... done
  Adding admin to docker group... done
  Setting socket permissions... done

[4/7] Configuration...

  Domain name: example.com
  ACME email for Let's Encrypt: admin@example.com

  Network Configuration:
    Network interface [eth0]:
    Network subnet [192.168.1.0/24]:
    Gateway IP [192.168.1.1]:
    Traefik IP (dedicated macvlan IP): 192.168.1.100

  Cloudflare Tunnel (optional):
    Enable Cloudflare Tunnel? [y/N]: n

[5/7] Generating configuration files...
  ✓ /volume1/docker/syrviscore/config/.env
  ✓ /volume1/docker/syrviscore/config/docker-compose.yaml
  ✓ /volume1/docker/syrviscore/config/traefik/traefik.yml
  ✓ /volume1/docker/syrviscore/config/traefik/dynamic.yml

[6/7] Creating system integration...
  ✓ Global symlink: /usr/local/bin/syrvis
  ✓ Startup script: /usr/local/etc/rc.d/S99syrviscore.sh

[7/7] Starting services...
  Pulling Docker images...
  Starting containers...

  ✓ traefik: running (192.168.1.100:443)
  ✓ portainer: running

Setup complete!

Access your services:
  Traefik:   https://traefik.example.com
  Portainer: https://portainer.example.com

Run 'syrvis status' to check service health.
```

### Privilege Escalation Strategy

The CLI uses a **self-elevating** pattern:

```python
def setup():
    if needs_privileged_operations() and not is_root():
        # Re-execute self with sudo
        print("Some operations require root privileges.")
        if confirm("Run with sudo?"):
            os.execvp("sudo", ["sudo", sys.argv[0]] + sys.argv[1:])
        else:
            print("Skipping privileged operations. Run 'sudo syrvis setup' later.")
            # Continue with unprivileged operations only
```

## CLI Commands

### Core Commands

```
syrvis setup [--non-interactive]     # Initial setup (interactive by default)
syrvis status                        # Show service status
syrvis start                         # Start all services
syrvis stop                          # Stop all services
syrvis restart                       # Restart all services
syrvis logs [service] [-f]           # View logs
syrvis doctor [--fix]                # Diagnose and fix issues
```

### Configuration Commands

```
syrvis config show                   # Show current configuration
syrvis config edit                   # Edit .env in $EDITOR
syrvis config regenerate             # Regenerate docker-compose from .env
syrvis config validate               # Validate configuration
```

### Update Commands

```
syrvis update check                  # Check for updates
syrvis update download [version]     # Download update (don't install)
syrvis update install [version]      # Install update (with backup)
syrvis update rollback               # Rollback to previous version
syrvis update list                   # List installed versions
syrvis update cleanup [--keep=2]     # Remove old versions
```

## Update Flow

### Download and Install

```
$ syrvis update check
Current version: 0.1.0
Latest version:  0.2.0

Changes in 0.2.0:
  - Traefik v3.2.0 → v3.3.0
  - Bug fixes

$ syrvis update install 0.2.0

[1/5] Downloading syrviscore-0.2.0.spk...
  ████████████████████ 100%

[2/5] Verifying package...
  ✓ Checksum valid
  ✓ Signature valid

[3/5] Installing to versions/0.2.0/...
  ✓ Extracted package
  ✓ Created venv
  ✓ Installed CLI

[4/5] Stopping services...
  ✓ Containers stopped

[5/5] Activating version 0.2.0...
  ✓ Updated symlink: current -> versions/0.2.0/
  ✓ Pulling new Docker images...
  ✓ Starting services...

Update complete!

Previous version 0.1.0 preserved for rollback.
Run 'syrvis update rollback' if needed.
```

### Rollback

```
$ syrvis update rollback

Current version: 0.2.0
Rollback to:     0.1.0

This will:
  - Stop current services
  - Switch to version 0.1.0
  - Start services with previous Docker images

Continue? [y/N]: y

[1/3] Stopping services...
[2/3] Switching version...
  ✓ Updated symlink: current -> versions/0.1.0/
[3/3] Starting services...

Rollback complete. Now running version 0.1.0.
```

## Configuration Persistence

### What's Version-Specific

```
versions/{version}/
├── cli/venv/              # Python environment for this version
├── build/config.yaml      # Docker image versions for this version
└── .version-manifest.json # Metadata for this version
```

### What's Shared Across Versions

```
config/
├── .env                   # User configuration (domain, email, IPs)
├── docker-compose.yaml    # Generated from .env + version's config.yaml
└── traefik/               # Traefik configuration
    ├── traefik.yml
    └── dynamic.yml

data/
├── traefik/acme.json      # Let's Encrypt certificates
├── portainer/             # Portainer data
└── cloudflared/           # Tunnel credentials
```

### Configuration Regeneration on Update

When updating, the CLI:

1. Reads existing `config/.env` (user settings)
2. Reads new version's `build/config.yaml` (Docker images)
3. Regenerates `docker-compose.yaml` with new images + existing settings
4. Preserves all user data in `data/`

## Root Manifest Structure

**File:** `/volume1/docker/syrviscore/.syrviscore-manifest.json`

```json
{
  "schema_version": 2,
  "active_version": "0.2.0",
  "install_path": "/volume1/docker/syrviscore",
  "setup_complete": true,
  "setup_completed_at": "2024-12-25T10:30:00Z",
  "versions": {
    "0.1.0": {
      "installed_at": "2024-12-20T08:00:00Z",
      "status": "available",
      "spk_path": "versions/0.1.0/syrviscore-0.1.0.spk"
    },
    "0.2.0": {
      "installed_at": "2024-12-25T10:00:00Z",
      "activated_at": "2024-12-25T10:30:00Z",
      "status": "active",
      "spk_path": "versions/0.2.0/syrviscore-0.2.0.spk"
    }
  },
  "update_history": [
    {
      "from": "0.1.0",
      "to": "0.2.0",
      "timestamp": "2024-12-25T10:30:00Z",
      "type": "upgrade"
    }
  ],
  "privileged_setup": {
    "docker_group_created": true,
    "user_added_to_docker": "admin",
    "global_symlink": "/usr/local/bin/syrvis",
    "startup_script": "/usr/local/etc/rc.d/S99syrviscore.sh"
  }
}
```

## Migration Path

### For Existing Installations

```
$ syrvis migrate

Detected existing installation at /volume1/docker/syrviscore/
Version: 0.0.1 (legacy layout)

This will migrate to the new versioned layout:
  - Current installation → versions/0.0.1/
  - Configuration → config/
  - Data preserved in data/

Continue? [y/N]: y

[1/4] Creating new directory structure...
[2/4] Moving CLI to versions/0.0.1/...
[3/4] Moving configuration to config/...
[4/4] Creating symlinks...

Migration complete!
```

## Benefits of This Architecture

### 1. Simpler SPK Installation
- No wizard dependencies
- No configuration during install
- Just installs the CLI tool

### 2. Interactive Setup
- User runs `syrvis setup` when ready
- All prompts in one place
- Can re-run to change settings

### 3. Safe Updates
- Each version in separate directory
- Rollback is instant (symlink change)
- Old versions preserved until cleanup

### 4. Clear Separation
- SPK: Unprivileged, minimal, reliable
- CLI: All features, interactive, handles privileges

### 5. Better UX
- Single command to set up: `syrvis setup`
- Single command to update: `syrvis update install`
- Single command to rollback: `syrvis update rollback`

## Implementation Plan

### Phase 1: Core Infrastructure
1. Implement versioned directory structure
2. Simplify SPK scripts (remove wizard dependencies)
3. Implement `syrvis setup` with interactive prompts
4. Implement `syrvis status/start/stop/restart/logs`

### Phase 2: Update System
1. Implement `syrvis update check/download/install`
2. Implement `syrvis update rollback`
3. Implement `syrvis update list/cleanup`
4. Add GitHub release integration

### Phase 3: Polish
1. Implement `syrvis migrate` for existing installations
2. Improve `syrvis doctor` for new layout
3. Add configuration validation
4. Documentation updates

## Questions for Review

1. **Version Retention**: How many old versions should we keep by default? (Proposed: 2)

2. **Config Location**: Should config stay in `config/` or at root level for easier access?

3. **Startup Behavior**: Should `syrvis setup` automatically start services, or require explicit `syrvis start`?

4. **Privilege Model**: Should we prompt for sudo during setup, or require user to run `sudo syrvis setup`?

5. **Update Source**: GitHub releases only, or support custom update URLs?
