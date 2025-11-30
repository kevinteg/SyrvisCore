# SyrvisCore Design Document

**Version:** 1.0.0-draft  
**Date:** 2024-11-29  
**Status:** Design Complete - Ready for Implementation

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Configuration](#3-configuration)
4. [Installation & Upgrade Flow](#4-installation--upgrade-flow)
5. [Build System](#5-build-system)
6. [CLI Design](#6-cli-design)
7. [Version Management](#7-version-management)
8. [Python Package](#8-python-package)
9. [Security & Secrets](#9-security--secrets)
10. [Disaster Recovery](#10-disaster-recovery)
11. [Future Enhancements](#11-future-enhancements)
12. [Implementation Roadmap](#12-implementation-roadmap)

---

## 1. Project Overview

### 1.1 Identity & Naming

**Project Name:** Syrvis  
**Pronunciation:** "SER-vis" (service)

**Component Hierarchy:**
- **SyrvisCore** - Layer 1 infrastructure platform (this SPK package)
- **SyrvisStack** - Layer 2 application containers (future: individual service repositories)

**Terminology:**
- **Core** - Infrastructure containers (Traefik, Portainer, Cloudflared)
- **Stacks** - Application containers (Home Assistant, Wiki, etc.)

**Package Names:**
- Python package: `syrviscore`
- CLI command: `syrvis`
- SPK filename: `syrviscore-{version}.spk`

### 1.2 Design Philosophy

**Opinionated Infrastructure:**
- Traefik and Portainer are required (no opt-out)
- Cloudflared is optional (users may have alternative external access)
- Sensible defaults over configuration options
- Convention over configuration where possible

**Community-First Design:**
- No hardcoded personal details
- Configuration-driven from day one
- Clear documentation for newcomers
- Shareable without forking

**Self-Contained Platform:**
- Brings own Python environment (venv)
- Pinned dependency versions
- No reliance on system packages
- Survives DSM updates

**Version Immutability:**
- Each SyrvisCore version = tested stack snapshot
- Explicit version pinning for all components
- Reproducible builds
- Safe rollback capability

### 1.3 Target Audience

**Primary:** Technical home users managing self-hosted services on Synology NAS

**Skill Level:**
- Comfortable with SSH and command line
- Basic understanding of Docker concepts
- Familiar with YAML configuration files
- Not necessarily developers

**Use Cases:**
- Home automation (Home Assistant)
- Personal wiki/documentation
- Development environments (Weaviate, databases)
- Media services
- IoT management

### 1.4 Goals

**Must Have:**
1. Single SPK installation provides complete reverse proxy + container management platform
2. External access via Cloudflare Tunnel (no port forwarding, no static IP required)
3. Split DNS support (same URLs work internally and externally)
4. CLI-driven management and monitoring
5. Version pinning with safe upgrade/rollback
6. Disaster recovery from Hyper Backup
7. Clear path for non-experts to install and maintain

**Should Have:**
1. Local build tools for version discovery and SPK creation
2. Manifest-based transparency (know exactly what's installed)
3. Layer 2 stack deployment via Portainer
4. Healthcheck integration (external monitoring ready)
5. Comprehensive logging

**Nice to Have:**
1. Web UI dashboard
2. Synology DSM integration (widgets, notifications)
3. Auto-update notifications
4. SyrvisStack catalog/marketplace

### 1.5 Non-Goals

**Explicitly Out of Scope:**
1. Kubernetes/orchestration complexity
2. Multi-node deployments
3. High-availability/clustering
4. Built-in backup solutions (use Synology Hyper Backup)
5. VPN server functionality (use Cloudflare Tunnel or other solutions)
6. Certificate management beyond Cloudflare Origin Certificates
7. Email server or similar complex services

---

## 2. Architecture

### 2.1 System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        Internet                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ Cloudflare Edge │
                    │  (DNS + Proxy)  │
                    └────────┬────────┘
                             │
                             │ Cloudflare Tunnel
                             │ (encrypted, outbound-only)
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│                      Synology NAS                                 │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                    SyrvisCore (Layer 1)                     │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │  │
│  │  │ Cloudflared  │  │   Traefik    │  │   Portainer     │  │  │
│  │  │   (tunnel)   │─>│ (reverse     │  │  (container     │  │  │
│  │  │              │  │  proxy +     │  │   management)   │  │  │
│  │  │              │  │  SSL term)   │  │                 │  │  │
│  │  └──────────────┘  └──────┬───────┘  └─────────────────┘  │  │
│  └────────────────────────────┼──────────────────────────────┘  │
│                                │                                  │
│  ┌────────────────────────────▼──────────────────────────────┐  │
│  │               SyrvisStacks (Layer 2)                       │  │
│  │  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐  │  │
│  │  │   Home     │  │     Wiki     │  │    Weaviate      │  │  │
│  │  │ Assistant  │  │              │  │    (dev env)     │  │  │
│  │  └────────────┘  └──────────────┘  └──────────────────┘  │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘

Internal Network:
  Local DNS Override: *.yourdomain.com → 192.168.1.x (Traefik)
  Direct routing, bypasses Cloudflare entirely
```

### 2.2 Component Relationships

**SyrvisCore Components:**

1. **Traefik** (Required)
   - Reverse proxy for all HTTP/HTTPS traffic
   - SSL termination with Cloudflare Origin Certificate
   - Auto-discovery of containers via Docker labels
   - Provides routing to Portainer, Cloudflared metrics, and all stacks
   
2. **Portainer** (Required)
   - Container management UI
   - Git-based stack deployment
   - Manages SyrvisStack deployments
   - Webhook support for auto-updates
   
3. **Cloudflared** (Optional)
   - Cloudflare Tunnel agent
   - Outbound-only connection (no port forwarding)
   - Encrypted tunnel to Cloudflare Edge
   - Routes external traffic to Traefik

**Dependencies:**
```
Cloudflared (optional) ─→ Traefik (required) ─→ Portainer (required)
                              │
                              ├─→ Stack 1
                              ├─→ Stack 2
                              └─→ Stack N
```

**Network Flow:**

**External Access:**
```
User → Cloudflare DNS (CNAME) → Cloudflare Edge (HTTPS)
  → Cloudflare Tunnel (encrypted) → Traefik (HTTPS, Origin Cert)
  → Stack (HTTP, internal)
```

**Internal Access:**
```
User → Local DNS Override → Traefik (HTTPS, Origin Cert)
  → Stack (HTTP, internal)
```

### 2.3 File Structure

```
/volume1/docker/syrviscore/              # Core installation directory
├── syrviscore-config.yaml               # USER CREATES - Required before install
├── .syrviscore-manifest.json            # GENERATED - Installation metadata
├── docker-compose.yaml                  # GENERATED - From SPK
├── .env                                 # USER CREATES/EDITS - Secrets & tokens
├── .env.template                        # GENERATED - Reference template
│
├── core/                                # Core component configurations
│   ├── traefik/
│   │   ├── traefik.yaml                 # Static configuration
│   │   ├── dynamic/                     # Dynamic configuration
│   │   │   └── tls.yaml                 # TLS/certificate config
│   │   └── certs/                       # Symlinks to /volume1/secrets
│   │       ├── cloudflare-origin.crt → /volume1/secrets/cloudflare-origin.crt
│   │       └── cloudflare-origin.key → /volume1/secrets/cloudflare-origin.key
│   │
│   ├── cloudflared/                     # Optional component
│   │   ├── config.yaml                  # Tunnel configuration
│   │   └── credentials.json → /volume1/secrets/cloudflared-credentials.json
│   │
│   └── portainer/
│       └── (data managed by Docker volume)
│
├── cli/                                 # SyrvisCore CLI (Python package)
│   ├── venv/                            # Python virtual environment
│   ├── src/
│   │   └── syrviscore/                  # Main package
│   ├── setup.py                         # Python package metadata
│   └── pyproject.toml                   # Modern Python config
│
├── logs/                                # Centralized logs (symlinks to volumes)
│   ├── traefik/
│   ├── cloudflared/
│   └── portainer/
│
└── versions/                            # Downloaded SPK versions (for rollback)
    ├── syrviscore-1.0.0.spk
    └── syrviscore-1.1.0.spk             # Current installed version

/volume1/docker/stacks/                  # SyrvisStack applications
├── homeassistant/
│   ├── docker-compose.yaml
│   ├── stack.json                       # Stack metadata
│   └── config/
├── wiki/
└── weaviate-dev/

/volume1/secrets/                        # Encrypted folder (user-managed)
├── cloudflare-origin.crt
├── cloudflare-origin.key
└── cloudflared-credentials.json
```

**Directory Conventions:**

- **User-Created Files:** `syrviscore-config.yaml`, `.env`, secrets
- **Generated Files:** `.syrviscore-manifest.json`, `docker-compose.yaml`, `.env.template`
- **Preserved on Upgrade:** User files, logs, versions cache
- **Replaced on Upgrade:** Generated files, core configurations

### 2.4 Data Flow

**Installation Data Flow:**
```
User creates syrviscore-config.yaml
  ↓
SPK postinst reads config
  ↓
Generates docker-compose.yaml (from config + build-config.yaml)
  ↓
Generates .syrviscore-manifest.json
  ↓
Creates directory structure
  ↓
Installs Python CLI in venv
  ↓
Waits for user to configure secrets (.env)
  ↓
User starts via Package Center
  ↓
Docker Compose starts core containers
```

**Runtime Data Flow:**
```
External Request → Cloudflare Tunnel → Traefik → Stack Container
Internal Request → Local DNS → Traefik → Stack Container
CLI Query → Docker API / Traefik API / Portainer API → Display
```

### 2.5 Persistence & State

**Docker Volumes (persist across upgrades/restarts):**
- `syrviscore-traefik-logs`
- `syrviscore-cloudflared-logs`
- `syrviscore-portainer-data`
- `syrviscore-portainer-logs`

**Filesystem Persistence:**
- `/volume1/docker/syrviscore/` (entire directory backed up by Hyper Backup)
- `/volume1/secrets/` (encrypted folder, backed up separately)

**Ephemeral (recreated on restart):**
- Container state
- Network bridges
- Temporary files in containers

---

## 3. Configuration

### 3.1 syrviscore-config.yaml

**Purpose:** User-facing configuration file, required before SPK installation.

**Location:** `/volume1/docker/syrviscore/syrviscore-config.yaml`

**Schema:**

```yaml
# SyrvisCore Configuration
# Required before SPK installation

# Your domain name (used for all service URLs)
# Example: homelab.com results in traefik.homelab.com, portainer.homelab.com
domain: yourdomain.com

# Core components to enable
core:
  traefik: enabled        # Reverse proxy + SSL termination (REQUIRED)
  portainer: enabled      # Container management UI (REQUIRED)
  cloudflared: optional   # Cloudflare Tunnel for external access (OPTIONAL)

# Optional: Custom paths (defaults shown)
paths:
  core_root: /volume1/docker/syrviscore      # Core installation directory
  stacks_root: /volume1/docker/stacks        # Where stacks are deployed
  secrets: /volume1/secrets                  # Secrets/certificates location
```

**Validation Rules:**

1. **domain:** 
   - Required
   - Must be valid FQDN format
   - No protocol prefix (https://)
   - No trailing slash

2. **core.traefik:**
   - Must be "enabled"
   - Cannot be disabled (opinionated design)

3. **core.portainer:**
   - Must be "enabled"
   - Cannot be disabled (opinionated design)

4. **core.cloudflared:**
   - Can be "enabled" or "optional"
   - If "optional", tunnel containers not created but config preserved

5. **paths:**
   - Must be absolute paths
   - Must exist or be creatable
   - No spaces in paths (Docker limitation)

**Example Minimal Config:**

```yaml
domain: example.com

core:
  traefik: enabled
  portainer: enabled
  cloudflared: optional
```

**Example Custom Paths:**

```yaml
domain: homelab.local

core:
  traefik: enabled
  portainer: enabled
  cloudflared: optional

paths:
  core_root: /volume2/docker/syrviscore  # Using second volume
  stacks_root: /volume2/docker/stacks
  secrets: /volume2/secrets
```

### 3.2 .syrviscore-manifest.json

**Purpose:** System-generated metadata about the installation. Used by CLI and upgrade scripts.

**Location:** `/volume1/docker/syrviscore/.syrviscore-manifest.json`

**Schema:**

```json
{
  "platform": {
    "version": "1.0.0",
    "release_date": "2024-11-29",
    "installed_at": "2024-11-29T10:30:00Z",
    "mode": "fresh_install"
  },
  "spk": {
    "filename": "syrviscore-1.0.0.spk",
    "download_url": "https://github.com/you/syrviscore/releases/download/v1.0.0/syrviscore-1.0.0.spk",
    "sha256": "abc123..."
  },
  "git": {
    "repo": "https://github.com/you/syrviscore.git",
    "commit": "a1b2c3d4",
    "tag": "v1.0.0"
  },
  "components": {
    "traefik": {
      "image": "traefik:v3.2.0",
      "digest": "sha256:1234abcd...",
      "enabled": true
    },
    "cloudflared": {
      "image": "cloudflare/cloudflared:2024.11.0",
      "digest": "sha256:5678efgh...",
      "enabled": false
    },
    "portainer": {
      "image": "portainer/portainer-ce:2.21.4-alpine",
      "digest": "sha256:9012ijkl...",
      "enabled": true
    }
  },
  "config": {
    "domain": "yourdomain.com",
    "paths": {
      "core_root": "/volume1/docker/syrviscore",
      "stacks_root": "/volume1/docker/stacks",
      "secrets": "/volume1/secrets"
    }
  },
  "compatibility": {
    "dsm_min_version": "7.0",
    "docker_min_version": "20.10"
  },
  "secrets_required": [
    "/volume1/secrets/cloudflare-origin.crt",
    "/volume1/secrets/cloudflare-origin.key",
    "/volume1/secrets/cloudflared-credentials.json"
  ],
  "rollback": {
    "previous_version": "0.9.0",
    "available": true,
    "spk_path": "/volume1/docker/syrviscore/versions/syrviscore-0.9.0.spk"
  }
}
```

**Usage:**
- CLI reads this to display current version, component status
- Upgrade scripts check compatibility
- Rollback uses previous_version reference
- DR recovery uses git/spk references to download correct version

### 3.3 .env File

**Purpose:** Environment variables for secrets and runtime configuration.

**Location:** `/volume1/docker/syrviscore/.env`

**Not in Git:** This file contains secrets and should never be committed.

**Template (.env.template):**

```bash
# SyrvisCore Environment Variables
# Copy to .env and fill in your values

# Portainer API Token (generate after first login)
PORTAINER_API_TOKEN=

# Healthchecks.io API Key (optional, for external monitoring)
HEALTHCHECKS_API_KEY=

# ntfy Topic (optional, for notifications)
NTFY_TOPIC=

# Timezone (used by all containers)
TZ=America/Los_Angeles
```

**Actual .env (user-created):**

```bash
PORTAINER_API_TOKEN=ptr_xxxxxxxxxxxxxxxxxxxxxxx
HEALTHCHECKS_API_KEY=abc123def456
NTFY_TOPIC=my-syrviscore-alerts
TZ=America/Los_Angeles
```

**Secrets Not in .env:**
- TLS certificates (in `/volume1/secrets/`, symlinked)
- Cloudflare tunnel credentials (in `/volume1/secrets/`, symlinked)

**Rationale:** Separation of configuration (environment vars) from secrets (files).

### 3.4 docker-compose.yaml

**Purpose:** Container definitions for SyrvisCore components.

**Location:** `/volume1/docker/syrviscore/docker-compose.yaml`

**Generated By:** SPK postinst script based on `build/config.yaml` + `syrviscore-config.yaml`

**High-Level Structure:**

```yaml
version: '3.8'

networks:
  syrviscore:
    name: syrviscore
    driver: bridge

volumes:
  syrviscore-traefik-logs: {}
  syrviscore-cloudflared-logs: {}
  syrviscore-portainer-data: {}
  syrviscore-portainer-logs: {}

services:
  traefik:
    image: traefik:v3.2.0  # Pinned version from build/config.yaml
    container_name: syrviscore-traefik
    # ... configuration details
    
  cloudflared:  # Only included if enabled in syrviscore-config.yaml
    image: cloudflare/cloudflared:2024.11.0
    container_name: syrviscore-cloudflared
    # ... configuration details
    
  portainer:
    image: portainer/portainer-ce:2.21.4-alpine
    container_name: syrviscore-portainer
    # ... configuration details
```

**Notes:**
- Exact image versions pinned during build
- Service definitions conditional on syrviscore-config.yaml
- Labels include platform metadata for introspection
- Healthchecks defined for all services

---

## 4. Installation & Upgrade Flow

### 4.1 Fresh Installation

**Prerequisites:**
1. Synology NAS running DSM 7.0+
2. Docker package installed from Package Center
3. SSH access (for configuration setup)

**Installation Steps:**

**Step 1: Prepare Configuration**

```bash
# SSH into Synology
ssh admin@nas.local

# Create SyrvisCore directory
sudo mkdir -p /volume1/docker/syrviscore
cd /volume1/docker/syrviscore

# Create configuration file
sudo nano syrviscore-config.yaml
```

Minimal config:
```yaml
domain: yourdomain.com

core:
  traefik: enabled
  portainer: enabled
  cloudflared: optional
```

**Step 2: Download SPK**

```bash
# Download from GitHub Releases
wget https://github.com/you/syrviscore/releases/download/v1.0.0/syrviscore-1.0.0.spk
```

**Step 3: Install via Package Center**

1. Open DSM → Package Center
2. Click "Manual Install"
3. Browse to downloaded SPK file
4. Click "Install"

**SPK postinst behavior:**

```
Reading configuration: /volume1/docker/syrviscore/syrviscore-config.yaml
✓ Configuration valid

Installing SyrvisCore v1.0.0
  Domain: yourdomain.com
  Components:
    • Traefik: enabled
    • Cloudflared: optional (not configured)
    • Portainer: enabled

Creating directory structure...
Installing Python CLI...
Generating docker-compose.yaml...
Generating manifest...

⚠️  Configuration Required

Before starting SyrvisCore:
  1. Place secrets in /volume1/secrets/:
     - cloudflare-origin.crt
     - cloudflare-origin.key
     - cloudflared-credentials.json (if using tunnel)
  
  2. Create .env file from template:
     cp /volume1/docker/syrviscore/.env.template .env
     nano .env
  
  3. Start SyrvisCore from Package Center

Documentation: https://github.com/you/syrviscore/wiki
```

**Step 4: Configure Secrets**

```bash
# Create secrets folder (encrypted recommended)
sudo mkdir -p /volume1/secrets

# Copy certificates (user provides these)
sudo cp /path/to/cloudflare-origin.crt /volume1/secrets/
sudo cp /path/to/cloudflare-origin.key /volume1/secrets/
sudo cp /path/to/cloudflared-credentials.json /volume1/secrets/

# Create .env from template
cd /volume1/docker/syrviscore
sudo cp .env.template .env
sudo nano .env
```

**Step 5: Start SyrvisCore**

1. Package Center → SyrvisCore → Start

**Expected Outcome:**

```bash
# Verify via CLI
syrvis core status

╔════════════════════════════════════════════════════╗
║  SyrvisCore Status                                 ║
╠════════════════════════════════════════════════════╣
║  Version: 1.0.0                                    ║
║  Domain:  yourdomain.com                           ║
╚════════════════════════════════════════════════════╝

┌────────────────┬──────────┬──────────┬─────────────┐
│ Component      │ Status   │ Health   │ Version     │
├────────────────┼──────────┼──────────┼─────────────┤
│ traefik        │ running  │ healthy  │ v3.2.0      │
│ cloudflared    │ disabled │ n/a      │ n/a         │
│ portainer      │ running  │ healthy  │ 2.21.4      │
└────────────────┴──────────┴──────────┴─────────────┘

Access Points:
  • Traefik Dashboard: https://traefik.yourdomain.com
  • Portainer: https://portainer.yourdomain.com
```

### 4.2 Upgrade Flow

**Scenario:** Upgrading from v1.0.0 to v1.1.0

**Step 1: Check for Updates**

```bash
syrvis version available

Checking GitHub for available versions...

Available Versions:
┌──────────┬────────────┬──────────────┬─────────────────────────────┐
│ Version  │ Status     │ Released     │ SPK Location                │
├──────────┼────────────┼──────────────┼─────────────────────────────┤
│ v1.1.0   │ available  │ 2024-12-01   │ Download from GitHub        │
│ v1.0.0   │ installed  │ 2024-11-29   │ Currently running           │
└──────────┴────────────┴──────────────┴─────────────────────────────┘

To download v1.1.0:
  syrvis version download v1.1.0
  
  OR manually:
  https://github.com/you/syrviscore/releases/download/v1.1.0/syrviscore-1.1.0.spk

To install:
  Package Center → Manual Install → Browse to downloaded SPK
```

**Step 2: Download New Version**

```bash
syrvis version download v1.1.0

Downloading SyrvisCore v1.1.0...
Source: https://github.com/you/syrviscore/releases/download/v1.1.0/syrviscore-1.1.0.spk
Destination: /volume1/docker/syrviscore/versions/syrviscore-1.1.0.spk

✓ Downloaded (15.8 MB)
✓ Checksum verified

Ready to install via Package Center:
  1. Package Center → Manual Install
  2. Browse to: /volume1/docker/syrviscore/versions/syrviscore-1.1.0.spk
  3. Confirm upgrade
```

**Step 3: Install via Package Center**

1. Package Center → Manual Install
2. Browse to `/volume1/docker/syrviscore/versions/syrviscore-1.1.0.spk`
3. Click "Install"
4. Synology prompts: "Upgrade SyrvisCore from v1.0.0 to v1.1.0?"
5. Click "Yes"

**SPK preupgrade behavior:**

```
Creating backup of current configuration...
Backup saved: /volume1/docker/syrviscore-backups/platform-v1.0.0-20241201-103000/

Stopping SyrvisCore containers...
  • traefik: stopped
  • portainer: stopped

Ready for upgrade.
```

**SPK postupgrade behavior:**

```
Upgrading SyrvisCore from v1.0.0 to v1.1.0...

Updating core files:
  • docker-compose.yaml (Traefik v3.2.0 → v3.2.1)
  • core/traefik/traefik.yaml
  • CLI tools

Preserving user files:
  • syrviscore-config.yaml (no changes)
  • .env (preserved)
  • secrets (preserved)
  • logs (preserved)

Updating manifest...
Updating Python CLI dependencies...

✓ Upgrade complete

SyrvisCore will restart automatically.
Changes:
  • Traefik: v3.2.0 → v3.2.1 (security fix)

Verify with: syrvis core status
```

**Step 4: Verify Upgrade**

```bash
syrvis core status

╔════════════════════════════════════════════════════╗
║  SyrvisCore Status                                 ║
╠════════════════════════════════════════════════════╣
║  Version: 1.1.0                                    ║
║  Upgraded from: 1.0.0                              ║
╚════════════════════════════════════════════════════╝

┌────────────────┬──────────┬──────────┬─────────────┐
│ Component      │ Status   │ Health   │ Version     │
├────────────────┼──────────┼──────────┼─────────────┤
│ traefik        │ running  │ healthy  │ v3.2.1 ✓    │
│ cloudflared    │ disabled │ n/a      │ n/a         │
│ portainer      │ running  │ healthy  │ 2.21.4      │
└────────────────┴──────────┴──────────┴─────────────┘
```

### 4.3 Rollback Flow

**Scenario:** v1.1.0 has issues, need to rollback to v1.0.0

**Option A: Using CLI**

```bash
syrvis version rollback

╔════════════════════════════════════════════════════╗
║  SyrvisCore Rollback                               ║
╚════════════════════════════════════════════════════╝

Current version:  v1.1.0
Rollback target:  v1.0.0

This will:
  1. Stop all core components
  2. Restore configuration from v1.0.0
  3. Restart with previous container versions

⚠️  Stacks will not be affected
⚠️  Current config will be backed up

Continue? [y/N]: y

Stopping core components...
Restoring v1.0.0 configuration from backup...
Source: /volume1/docker/syrviscore-backups/platform-v1.0.0-20241201-103000/

Starting core components...
  • traefik: v3.2.1 → v3.2.0
  • portainer: 2.21.4 (no change)

✓ Rollback complete

Verify with: syrvis core status
```

**Option B: Manual (if CLI fails)**

```bash
# Stop containers
cd /volume1/docker/syrviscore
docker-compose down

# Restore from backup
sudo cp -r /volume1/docker/syrviscore-backups/platform-v1.0.0-20241201-103000/* .

# Restart
docker-compose up -d
```

### 4.4 Error Handling

**Missing Configuration:**

```
SPK Installation: syrviscore-1.0.0.spk

❌ Configuration file not found

SyrvisCore requires configuration before installation.

Create: /volume1/docker/syrviscore/syrviscore-config.yaml

Minimal example:
  domain: yourdomain.com
  core:
    traefik: enabled
    portainer: enabled
    cloudflared: optional

Documentation: https://github.com/you/syrviscore/wiki/Configuration

Installation aborted.
```

**Invalid Configuration:**

```
SPK Installation: syrviscore-1.0.0.spk

❌ Configuration validation failed

Errors in /volume1/docker/syrviscore/syrviscore-config.yaml:
  Line 3: 'domain' is required
  Line 7: 'core.traefik' must be 'enabled' (cannot be disabled)

Fix configuration and try again.

Installation aborted.
```

**Missing Secrets:**

```
Starting SyrvisCore...

❌ Required secrets not found

Missing files:
  • /volume1/secrets/cloudflare-origin.crt
  • /volume1/secrets/cloudflare-origin.key

SyrvisCore cannot start without TLS certificates.

See: https://github.com/you/syrviscore/wiki/Secrets

Containers not started.
```

**Docker Not Running:**

```
SPK Installation: syrviscore-1.0.0.spk

❌ Docker is not running

Install and start Docker from Package Center before installing SyrvisCore.

Installation aborted.
```

---

## 5. Build System

### 5.1 Build Philosophy

**Two-Stage Process:**

1. **Stage 1: Version Selection** - Interactive discovery of latest Docker image versions
2. **Stage 2: SPK Creation** - Build SPK from pinned versions in `build/config.yaml`

**Goals:**
- Local development workflow mirrors CI/CD
- Developers can explore version updates before committing
- Production builds are reproducible (pinned versions)
- Version pinning is explicit and auditable

### 5.2 Build Configuration

**build/config.yaml**

**Purpose:** Pinned version configuration for reproducible builds.

**Location:** `build/config.yaml` (in Git repository)

**Schema:**

```yaml
# SyrvisCore Build Configuration
# Pinned component versions for reproducible builds

version: 1.0.0
release_date: 2024-11-29

components:
  traefik:
    version: v3.2.0
    image: traefik:v3.2.0
    # Digest filled during build
    
  cloudflared:
    version: 2024.11.0
    image: cloudflare/cloudflared:2024.11.0
    
  portainer:
    version: 2.21.4-alpine
    image: portainer/portainer-ce:2.21.4-alpine

compatibility:
  dsm_min_version: "7.0"
  docker_min_version: "20.10"
  docker_compose_version: "2.x"

notes: |
  This version includes:
  - Traefik v3.2.0 (stable)
  - Cloudflared 2024.11.0 (tunnel agent)
  - Portainer CE 2.21.4 (container management)
```

### 5.3 Build Tools

**Structure:**

```
build-tools/
├── select-docker-versions    # Interactive version selector
├── build-spk                 # SPK builder
├── validate-config           # Config file validator
├── lib/
│   ├── docker_hub.py         # Docker Hub API client
│   ├── spk_builder.py        # SPK construction
│   ├── version_parser.py     # Semantic versioning
│   └── manifest_generator.py # Manifest creation
├── pyproject.toml            # Build tools package config
└── tox.ini                   # Build automation
```

### 5.4 Version Selection Tool

**Command:** `./build-tools/select-docker-versions`

**Purpose:** Interactively discover and select Docker image versions for inclusion in `build/config.yaml`.

**User Experience:**

```bash
$ ./build-tools/select-docker-versions

╔════════════════════════════════════════════════════╗
║  SyrvisCore - Docker Version Selection             ║
╚════════════════════════════════════════════════════╝

Querying Docker Hub for latest versions...

Current build/config.yaml:
  • Traefik: v3.2.0
  • Cloudflared: 2024.11.0
  • Portainer: 2.21.4-alpine

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Traefik:
  Available versions:
    [1] v3.2.1 (latest, released 2024-11-15) ← RECOMMENDED
    [2] v3.2.0 (current, released 2024-10-01)
    [3] v3.1.5 (released 2024-09-15)
  
  Select: [1]: 1
  ✓ Selected: v3.2.1

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Cloudflared:
  Available versions:
    [1] 2024.12.0 (latest, released 2024-12-01)
    [2] 2024.11.0 (current, released 2024-11-01) ← STABLE
    [3] 2024.10.1 (released 2024-10-15)
  
  Select: [2]: 2
  ✓ Selected: 2024.11.0 (keeping stable version)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Portainer CE:
  Available versions:
    [1] 2.21.5-alpine (latest, released 2024-12-10)
    [2] 2.21.4-alpine (current, released 2024-10-20)
    [3] 2.21.3-alpine (released 2024-09-15)
  
  Select: [2]: 2
  ✓ Selected: 2.21.4-alpine

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Summary of Changes:
  • Traefik: v3.2.0 → v3.2.1 (UPDATE)
  • Cloudflared: 2024.11.0 (NO CHANGE)
  • Portainer: 2.21.4-alpine (NO CHANGE)

Apply changes to build/config.yaml? [y/N]: y

✓ Updated build/config.yaml

Next steps:
  1. Review changes: git diff build/config.yaml
  2. Test locally: ./build-tools/build-spk --version 1.1.0
  3. Commit: git commit -am "Update Traefik to v3.2.1"
  4. Tag for release: git tag v1.1.0 && git push --tags
```

**Features:**
- Queries Docker Hub API for available versions
- Shows release dates to aid decision-making
- Highlights recommended (latest) vs stable versions
- Shows current versions from build/config.yaml
- Interactive selection with defaults
- Validates selections before applying
- Git-friendly output (easy to diff and commit)

### 5.5 SPK Build Tool

**Command:** `./build-tools/build-spk --version <version>`

**Purpose:** Build SPK package from pinned versions in `build/config.yaml`.

**User Experience:**

```bash
$ ./build-tools/build-spk --version 1.1.0

╔════════════════════════════════════════════════════╗
║  SyrvisCore - SPK Build                            ║
╚════════════════════════════════════════════════════╝

Reading: build/config.yaml

Component Versions:
  • Traefik:     v3.2.1
  • Cloudflared: 2024.11.0
  • Portainer:   2.21.4-alpine

Building SPK v1.1.0...

[1/8] Validating configuration files...
  ✓ build/config.yaml
  ✓ core/traefik/traefik.yaml
  ✓ core/cloudflared/config.yaml

[2/8] Generating docker-compose.yaml...
  ✓ Template rendered with pinned versions

[3/8] Pulling Docker images...
  ✓ traefik:v3.2.1 (14.2 MB)
  ✓ cloudflare/cloudflared:2024.11.0 (8.5 MB)
  ✓ portainer/portainer-ce:2.21.4-alpine (12.3 MB)

[4/8] Capturing image digests...
  ✓ traefik@sha256:abc123...
  ✓ cloudflared@sha256:def456...
  ✓ portainer@sha256:ghi789...

[5/8] Generating manifest...
  ✓ .syrviscore-manifest.json

[6/8] Creating package archive...
  ✓ package.tgz (15.2 MB)

[7/8] Building SPK...
  ✓ INFO file
  ✓ Package icons
  ✓ Lifecycle scripts
  ✓ SPK archive

[8/8] Generating checksums...
  ✓ SHA256: abc123def456...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Build complete!

Output: syrviscore-1.1.0.spk (15.8 MB)
SHA256: abc123def456...

Test locally:
  1. Extract package.tgz
  2. cd to extracted directory
  3. docker-compose up -d

Create GitHub release:
  git tag v1.1.0
  git push --tags
  # GitHub Actions will build and release automatically
```

**Build Artifacts:**

```
syrviscore-1.1.0.spk           # Main SPK file
checksums.txt                   # SHA256 checksum
build-log.txt                   # Detailed build log
```

### 5.6 Tox Configuration

**tox.ini**

**Purpose:** Automate build, test, and validation tasks.

```ini
[tox]
envlist = lint, validate, build
skipsdist = True

[testenv:lint]
description = Lint Python code
deps =
    black
    ruff
commands =
    black --check build-tools/ src/
    ruff check build-tools/ src/

[testenv:validate]
description = Validate configuration files
deps =
    pyyaml
    jsonschema
commands =
    python -m build_tools.validate_config build/config.yaml
    python -m build_tools.validate_config syrviscore-config.yaml.example

[testenv:build]
description = Build SPK package
deps =
    pyyaml
    requests
    docker
passenv = VERSION
commands =
    python -m build_tools.build_spk --version {env:VERSION:dev}

[testenv:dev]
description = Development environment
deps =
    black
    ruff
    pytest
    ipython
commands =
    ipython
```

**Usage:**

```bash
# Lint code
tox -e lint

# Validate configs
tox -e validate

# Build SPK
VERSION=1.1.0 tox -e build

# Enter dev environment
tox -e dev
```

### 5.7 GitHub Actions Workflow

**.github/workflows/build-release.yaml**

**Purpose:** Automate SPK builds on Git tag push.

**Trigger:** Push of version tag (e.g., `v1.0.0`)

**High-Level Steps:**

```yaml
name: Build and Release SPK

on:
  push:
    tags:
      - 'v*.*.*'

jobs:
  build:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout code
      
      - name: Extract version from tag
        # v1.0.0 → 1.0.0
      
      - name: Setup Python
      
      - name: Install dependencies
      
      - name: Run linting
        run: tox -e lint
      
      - name: Validate configurations
        run: tox -e validate
      
      - name: Build SPK
        run: VERSION=${{ version }} tox -e build
      
      - name: Generate checksums
      
      - name: Create GitHub Release
        # Upload SPK + checksums
        # Auto-generate release notes from CHANGELOG.md
```

**Developer Workflow:**

```bash
# Local development
git checkout -b update-traefik

# Select new versions
./build-tools/select-docker-versions

# Test build locally
./build-tools/build-spk --version 1.1.0-dev

# Commit changes
git commit -am "Update Traefik to v3.2.1"
git push origin update-traefik

# Merge PR to main
# Then tag for release
git checkout main
git pull
git tag -a v1.1.0 -m "Release v1.1.0 - Traefik security update"
git push origin v1.1.0

# GitHub Actions automatically:
# - Builds SPK
# - Runs tests
# - Creates GitHub Release
# - Uploads artifacts
```

### 5.8 Version Numbering

**Semantic Versioning:** MAJOR.MINOR.PATCH

**Rules:**

- **PATCH (1.0.x):** Security updates, bug fixes, no breaking changes
  - Example: Traefik v3.2.0 → v3.2.1 (security fix)
  
- **MINOR (1.x.0):** Component upgrades, new optional features
  - Example: Cloudflared 2024.11.0 → 2024.12.0
  - Example: Add new optional core component
  
- **MAJOR (x.0.0):** Breaking configuration changes, DSM requirement changes
  - Example: Change configuration schema
  - Example: Require DSM 7.2+

**Pre-release Versions:**

- `1.1.0-beta.1` - Beta testing
- `1.1.0-rc.1` - Release candidate
- `1.1.0` - Stable release

---

## 6. CLI Design

### 6.1 Command Structure

```
syrvis
├── core                    # Core management
│   ├── status              # Show core component status
│   ├── logs <service>      # Tail logs for a service
│   ├── restart <service>   # Restart specific component
│   └── routes              # Show Traefik routing table
│
├── stacks                  # Stack management
│   ├── list                # Show deployed stacks
│   ├── sync                # Sync stack definitions from Git
│   └── deploy <name>       # Deploy a specific stack
│
├── version                 # Version management
│   ├── current             # Show installed version
│   ├── available           # List available versions (GitHub)
│   ├── downloaded          # List locally cached SPKs
│   ├── download <ver>      # Download specific version
│   ├── rollback            # Rollback to previous version
│   └── cleanup             # Remove old cached SPKs
│
├── config                  # Configuration
│   ├── show                # Display current config
│   ├── validate            # Check config syntax
│   └── edit                # Open config in editor
│
└── manifest                # Installation metadata
    └── show                # Display manifest details
```

### 6.2 Command Examples

#### syrvis core status

**Purpose:** Quick health check of core components.

```bash
$ syrvis core status

╔════════════════════════════════════════════════════╗
║  SyrvisCore Status                                 ║
╠════════════════════════════════════════════════════╣
║  Version: 1.0.0                                    ║
║  Domain:  yourdomain.com                           ║
╚════════════════════════════════════════════════════╝

┌────────────────┬──────────┬──────────┬─────────────┐
│ Component      │ Status   │ Health   │ Version     │
├────────────────┼──────────┼──────────┼─────────────┤
│ traefik        │ running  │ healthy  │ v3.2.0      │
│ cloudflared    │ disabled │ n/a      │ n/a         │
│ portainer      │ running  │ healthy  │ 2.21.4      │
└────────────────┴──────────┴──────────┴─────────────┘

Core Metrics:
  • Traefik routes: 12 active
  • Tunnel status: N/A (cloudflared disabled)
  • Stacks deployed: 4

Access Points:
  • Traefik Dashboard: https://traefik.yourdomain.com
  • Portainer: https://portainer.yourdomain.com
```

**With version mismatch:**

```bash
$ syrvis core status

⚠️  VERSION MISMATCH DETECTED

╔════════════════════════════════════════════════════╗
║  SyrvisCore Status                                 ║
╠════════════════════════════════════════════════════╣
║  Version: 1.1.0                                    ║
║  Domain:  yourdomain.com                           ║
╚════════════════════════════════════════════════════╝

┌────────────────┬──────────┬──────────┬─────────────┬─────────────┐
│ Component      │ Status   │ Health   │ Expected    │ Running     │
├────────────────┼──────────┼──────────┼─────────────┼─────────────┤
│ traefik        │ running  │ healthy  │ v3.2.1      │ v3.2.0 ⚠️   │
│ cloudflared    │ disabled │ n/a      │ n/a         │ n/a         │
│ portainer      │ running  │ healthy  │ 2.21.4      │ 2.21.4 ✓    │
└────────────────┴──────────┴──────────┴─────────────┴─────────────┘

Action required:
  Manifest expects Traefik v3.2.1, but v3.2.0 is running.
  
  To fix:
    cd /volume1/docker/syrviscore
    docker-compose pull
    docker-compose up -d
```

#### syrvis core logs

**Purpose:** Stream logs from a core component.

```bash
$ syrvis core logs traefik

Streaming logs from syrviscore-traefik...
(Press Ctrl+C to exit)

2024-11-29T10:30:15Z INF Starting Traefik version=v3.2.0
2024-11-29T10:30:15Z INF Loading configuration file=/etc/traefik/traefik.yaml
2024-11-29T10:30:16Z INF Starting provider Docker
2024-11-29T10:30:16Z INF Discovered service homeassistant@docker
2024-11-29T10:30:16Z INF Discovered service portainer@docker
...
```

**With options:**

```bash
# Tail last 50 lines
syrvis core logs traefik --tail 50

# Follow logs
syrvis core logs traefik --follow

# Show logs since timestamp
syrvis core logs traefik --since 2024-11-29T10:00:00
```

#### syrvis core routes

**Purpose:** Display Traefik routing table.

```bash
$ syrvis core routes

╔════════════════════════════════════════════════════╗
║  Traefik Routing Table                             ║
╚════════════════════════════════════════════════════╝

┌────────────────────────────┬─────────────────────┬────────────┐
│ Hostname                   │ Service             │ Status     │
├────────────────────────────┼─────────────────────┼────────────┤
│ traefik.yourdomain.com     │ api@internal        │ enabled    │
│ portainer.yourdomain.com   │ portainer@docker    │ enabled    │
│ homeassistant.yourdomain.com│ homeassistant@docker│ enabled    │
│ wiki.yourdomain.com        │ wiki@docker         │ enabled    │
└────────────────────────────┴─────────────────────┴────────────┘

Total routes: 4 active
```

#### syrvis stacks list

**Purpose:** Show deployed SyrvisStacks.

```bash
$ syrvis stacks list

╔════════════════════════════════════════════════════╗
║  SyrvisStacks                                      ║
╚════════════════════════════════════════════════════╝

┌─────────────────┬──────────┬────────────────────────────┬─────────┐
│ Stack           │ Status   │ URL                        │ Health  │
├─────────────────┼──────────┼────────────────────────────┼─────────┤
│ homeassistant   │ running  │ homeassistant.yourdomain.com│ healthy │
│ wiki            │ running  │ wiki.yourdomain.com        │ healthy │
│ weaviate-dev    │ stopped  │ weaviate-dev.yourdomain.com│ n/a     │
└─────────────────┴──────────┴────────────────────────────┴─────────┘

Total stacks: 3 deployed (2 running, 1 stopped)
```

#### syrvis version available

**Purpose:** Check for updates on GitHub.

```bash
$ syrvis version available

Checking GitHub for available versions...

Available Versions:
┌──────────┬────────────┬──────────────┬─────────────────────────────┐
│ Version  │ Status     │ Released     │ SPK Location                │
├──────────┼────────────┼──────────────┼─────────────────────────────┤
│ v1.2.0   │ available  │ 2024-12-15   │ Download from GitHub        │
│ v1.1.0   │ downloaded │ 2024-12-01   │ /volume1/.../versions/...   │
│ v1.0.0   │ installed  │ 2024-11-29   │ Currently running           │
└──────────┴────────────┴──────────────┴─────────────────────────────┘

Legend:
  • installed  - Currently running
  • downloaded - Cached locally (ready for install/rollback)
  • available  - Available for download

To download v1.2.0:
  syrvis version download v1.2.0
  
  OR manually:
  https://github.com/you/syrviscore/releases/download/v1.2.0/syrviscore-1.2.0.spk

To install:
  1. Package Center → Manual Install
  2. Browse to downloaded SPK
  3. Confirm upgrade
```

#### syrvis version cleanup

**Purpose:** Remove old cached SPK versions.

```bash
$ syrvis version cleanup

Cached SPK versions:
┌──────────┬────────────┬────────────┬──────────┐
│ Version  │ Status     │ Size       │ Action   │
├──────────┼────────────┼────────────┼──────────┤
│ v1.2.0   │ installed  │ 15.8 MB    │ keep     │
│ v1.1.0   │ previous   │ 15.5 MB    │ keep     │
│ v1.0.0   │ old        │ 15.2 MB    │ remove   │
│ v0.9.0   │ old        │ 14.8 MB    │ remove   │
└──────────┴────────────┴────────────┴──────────┘

Policy: Keep installed + 1 previous version

Remove v1.0.0 and v0.9.0? [y/N]: y

Removing old versions...
  ✓ Removed v1.0.0 (15.2 MB)
  ✓ Removed v0.9.0 (14.8 MB)

Freed 30.0 MB
```

#### syrvis config show

**Purpose:** Display current configuration.

```bash
$ syrvis config show

╔════════════════════════════════════════════════════╗
║  SyrvisCore Configuration                          ║
╚════════════════════════════════════════════════════╝

Source: /volume1/docker/syrviscore/syrviscore-config.yaml

Domain:
  yourdomain.com

Core Components:
  • Traefik:     enabled
  • Portainer:   enabled
  • Cloudflared: optional (not configured)

Paths:
  • Core root:   /volume1/docker/syrviscore
  • Stacks root: /volume1/docker/stacks
  • Secrets:     /volume1/secrets

Edit: syrvis config edit
Validate: syrvis config validate
```

#### syrvis manifest show

**Purpose:** Display installation manifest details.

```bash
$ syrvis manifest show

╔════════════════════════════════════════════════════╗
║  SyrvisCore Installation Manifest                  ║
╚════════════════════════════════════════════════════╝

Platform:
  Version:        1.0.0
  Installed:      2024-11-29 10:30:00 UTC
  Release Date:   2024-11-29
  Install Mode:   fresh_install

Components:
  Traefik:
    Image:   traefik:v3.2.0
    Digest:  sha256:abc123...
    Status:  enabled
  
  Cloudflared:
    Image:   cloudflare/cloudflared:2024.11.0
    Digest:  sha256:def456...
    Status:  disabled
  
  Portainer:
    Image:   portainer/portainer-ce:2.21.4-alpine
    Digest:  sha256:ghi789...
    Status:  enabled

Source:
  Git Repo:   https://github.com/you/syrviscore.git
  Git Tag:    v1.0.0
  Git Commit: a1b2c3d4
  
  SPK Download:
  https://github.com/you/syrviscore/releases/download/v1.0.0/syrviscore-1.0.0.spk

Rollback:
  Previous Version: N/A (fresh install)
  Available:        No
```

### 6.3 Output Formatting

**Design Principles:**

1. **Consistent Tables:** Use box-drawing characters for clarity
2. **Color Coding:** (optional, via `rich` library)
   - Green checkmarks (✓) for healthy/success
   - Yellow warnings (⚠️) for attention needed
   - Red X (❌) for errors
3. **Progressive Disclosure:** Show summary first, details on demand
4. **Machine-Readable:** All commands support `--json` flag for scripting

**Example with --json flag:**

```bash
$ syrvis core status --json

{
  "version": "1.0.0",
  "domain": "yourdomain.com",
  "components": {
    "traefik": {
      "status": "running",
      "health": "healthy",
      "version": "v3.2.0",
      "expected_version": "v3.2.0",
      "match": true
    },
    "cloudflared": {
      "status": "disabled",
      "health": "n/a",
      "version": null,
      "expected_version": null,
      "match": true
    },
    "portainer": {
      "status": "running",
      "health": "healthy",
      "version": "2.21.4",
      "expected_version": "2.21.4",
      "match": true
    }
  },
  "metrics": {
    "traefik_routes": 12,
    "tunnel_status": "disabled",
    "stacks_deployed": 4
  }
}
```

### 6.4 Error Messages

**Design Principles:**

1. **Clear Problem Statement:** What went wrong?
2. **Actionable Solution:** What should the user do?
3. **Context:** Where to find more information?

**Examples:**

**Config file missing:**
```bash
$ syrvis core status

❌ Configuration file not found

SyrvisCore requires a configuration file to operate.

Expected location: /volume1/docker/syrviscore/syrviscore-config.yaml

Create configuration:
  syrvis config edit

Documentation:
  https://github.com/you/syrviscore/wiki/Configuration
```

**Docker not running:**
```bash
$ syrvis core status

❌ Cannot connect to Docker

Ensure Docker is running:
  1. Open Package Center
  2. Start "Docker" package
  3. Run: syrvis core status
```

**Permission denied:**
```bash
$ syrvis core logs traefik

❌ Permission denied

SyrvisCore CLI requires access to Docker socket.

Run with sudo:
  sudo syrvis core logs traefik

Or add user to docker group (one-time setup):
  sudo synogroup --add docker $USER
```

---

## 7. Version Management

### 7.1 Version Pinning Strategy

**Core Principle:** Every SyrvisCore version represents a tested, immutable stack of component versions.

**Guarantees:**

1. Installing SyrvisCore v1.0.0 today or in 2 years results in identical containers
2. Rollback to v1.0.0 restores exact previous state
3. No unexpected updates from upstream images

**Implementation:**

- `build/config.yaml` pins exact image tags (e.g., `traefik:v3.2.0`, not `traefik:latest`)
- Manifest includes image digests for absolute immutability
- Docker Compose file generated during build, not runtime

**Example:**

```yaml
# build/config.yaml (committed to Git)
version: 1.0.0
components:
  traefik:
    version: v3.2.0
    image: traefik:v3.2.0  # Explicit tag, no "latest"
```

### 7.2 Local SPK Cache

**Purpose:** Store downloaded SPK files for quick rollback without re-downloading.

**Location:** `/volume1/docker/syrviscore/versions/`

**Managed By:** CLI automatically caches on download

**Structure:**

```
/volume1/docker/syrviscore/versions/
├── syrviscore-1.0.0.spk
├── syrviscore-1.1.0.spk
└── syrviscore-1.2.0.spk
```

**Automatic Caching:**

```bash
# User downloads new version
syrvis version download v1.2.0

# CLI downloads to cache
# → /volume1/docker/syrviscore/versions/syrviscore-1.2.0.spk

# Package Center install uses cached file
# → Cached version persists for rollback
```

**Manifest Tracks Cache:**

```json
{
  "rollback": {
    "previous_version": "1.1.0",
    "available": true,
    "spk_path": "/volume1/docker/syrviscore/versions/syrviscore-1.1.0.spk"
  }
}
```

### 7.3 Cleanup Policy

**Default Retention:** Keep installed version + 1 previous version

**Rationale:**
- Installed version: Currently running
- Previous version: Immediate rollback target
- Older versions: Can be re-downloaded if needed

**Manual Cleanup:**

```bash
syrvis version cleanup

# Shows what will be removed
# Prompts for confirmation
# Deletes old SPKs
```

**Automatic Cleanup:** (Future enhancement)

Could run automatically:
- After successful upgrade (keep last 2)
- On schedule (monthly cleanup)
- When disk space low

**Override Retention:**

```bash
# Keep more versions
syrvis version cleanup --keep 3

# Remove all except installed
syrvis version cleanup --keep 1

# Dry run (show what would be deleted)
syrvis version cleanup --dry-run
```

### 7.4 Rollback Mechanism

**Two Rollback Scenarios:**

**Scenario A: Rollback Immediately After Upgrade**

```
v1.0.0 → upgrade to v1.1.0 → problems → rollback to v1.0.0
```

**Process:**
1. SPK preupgrade created backup in `/volume1/docker/syrviscore-backups/`
2. `syrvis version rollback` restores from backup
3. Previous SPK in cache, ready to install if needed

**Scenario B: Rollback After Time Has Passed**

```
v1.0.0 → v1.1.0 → v1.2.0 (current) → problems → rollback to v1.1.0
```

**Process:**
1. Check if v1.1.0 SPK in cache
2. If yes: Use cached version
3. If no: Download from GitHub
4. User manually installs via Package Center (downgrade not officially supported)
5. Restore configuration from backup

**CLI Rollback Command:**

```bash
syrvis version rollback

# Interactive prompt shows:
# - Current version
# - Target version (previous from manifest)
# - What will happen
# - Confirmation required
```

**Limitations:**

Synology doesn't officially support package downgrades via Package Center. Workarounds:

1. **Restore from backup** (preferred method)
2. **Uninstall + Reinstall** (preserves data if done carefully)
3. **Manual docker-compose restoration** (advanced users)

**Future Enhancement:** CLI could automate the full rollback process, including SPK reinstallation.

### 7.5 Version Comparison

**Understanding Changes Between Versions:**

```bash
$ syrvis version compare v1.0.0 v1.1.0

Comparing SyrvisCore versions...

Version: v1.0.0 → v1.1.0
Released: 2024-11-29 → 2024-12-01

Component Changes:
┌────────────────┬─────────────┬─────────────┬────────────┐
│ Component      │ v1.0.0      │ v1.1.0      │ Change     │
├────────────────┼─────────────┼─────────────┼────────────┤
│ traefik        │ v3.2.0      │ v3.2.1      │ UPDATED    │
│ cloudflared    │ 2024.11.0   │ 2024.11.0   │ unchanged  │
│ portainer      │ 2.21.4      │ 2.21.4      │ unchanged  │
└────────────────┴─────────────┴─────────────┴────────────┘

Release Notes:
  • Traefik v3.2.1: Security fix for CVE-2024-XXXXX
  
Full changelog:
  https://github.com/you/syrviscore/compare/v1.0.0...v1.1.0
```

---

## 8. Python Package

### 8.1 Package Structure

**Layout: src-based structure (modern Python best practice)**

```
syrviscore/                          # Git repository root
├── src/
│   └── syrviscore/                  # Main Python package
│       ├── __init__.py              # Package initialization
│       ├── __version__.py           # Version singleton
│       ├── cli.py                   # CLI entry point (using Click)
│       ├── core/                    # Core management
│       │   ├── __init__.py
│       │   ├── status.py            # Status checking
│       │   ├── logs.py              # Log streaming
│       │   └── routes.py            # Route inspection
│       ├── stacks/                  # Stack management
│       │   ├── __init__.py
│       │   ├── list.py              # List stacks
│       │   └── sync.py              # Sync from Git
│       ├── version/                 # Version management
│       │   ├── __init__.py
│       │   ├── available.py         # Check GitHub
│       │   ├── download.py          # Download SPKs
│       │   ├── rollback.py          # Rollback logic
│       │   └── cleanup.py           # Cleanup old versions
│       ├── config/                  # Configuration handling
│       │   ├── __init__.py
│       │   ├── loader.py            # Load YAML configs
│       │   └── validator.py         # Validation logic
│       ├── manifest/                # Manifest handling
│       │   ├── __init__.py
│       │   ├── reader.py            # Read manifest
│       │   └── writer.py            # Write manifest
│       └── lib/                     # Shared utilities
│           ├── __init__.py
│           ├── docker_client.py     # Docker API wrapper
│           ├── portainer_api.py     # Portainer API client
│           ├── traefik_api.py       # Traefik API client
│           └── output.py            # Table formatting
│
├── build_tools/                     # Build system (separate)
│   ├── __init__.py
│   ├── select_docker_versions.py   # Version selection tool
│   ├── build_spk.py                 # SPK builder
│   ├── validate_config.py           # Config validator
│   └── lib/
│       ├── __init__.py
│       ├── docker_hub.py            # Docker Hub API
│       ├── spk_builder.py           # SPK construction
│       └── version_parser.py        # Semantic versioning
│
├── tests/                           # Test suite
│   ├── __init__.py
│   ├── test_cli.py
│   ├── test_version.py
│   └── test_config.py
│
├── setup.py                         # Minimal setup.py for compatibility
├── pyproject.toml                   # Modern Python packaging
├── tox.ini                          # Build automation
├── README.md
├── CHANGELOG.md
└── LICENSE
```

### 8.2 pyproject.toml

**Purpose:** Modern Python package configuration (PEP 518, 621).

```toml
[build-system]
requires = ["setuptools>=65.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "syrviscore"
version = "1.0.0"
description = "Self-hosted infrastructure platform for Synology NAS"
readme = "README.md"
requires-python = ">=3.9"
license = {text = "MIT"}
authors = [
    {name = "Your Name", email = "you@example.com"}
]
keywords = ["synology", "docker", "self-hosted", "homelab"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: System Administrators",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
]

dependencies = [
    "click==8.1.7",
    "docker==7.0.0",
    "requests==2.31.0",
    "pyyaml==6.0.1",
    "tabulate==0.9.0",
    "python-dateutil==2.8.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "black>=23.0",
    "ruff>=0.1",
    "ipython>=8.0",
]
build = [
    # Build tool specific dependencies
    "tox>=4.0",
]

[project.urls]
Homepage = "https://github.com/you/syrviscore"
Documentation = "https://github.com/you/syrviscore/wiki"
Repository = "https://github.com/you/syrviscore.git"
Issues = "https://github.com/you/syrviscore/issues"
Changelog = "https://github.com/you/syrviscore/blob/main/CHANGELOG.md"

[project.scripts]
syrvis = "syrviscore.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
syrviscore = ["py.typed"]

[tool.black]
line-length = 100
target-version = ['py39']

[tool.ruff]
line-length = 100
select = ["E", "F", "W", "I", "N"]
ignore = ["E501"]  # Line too long (handled by black)

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

### 8.3 setup.py (Minimal)

**Purpose:** Compatibility shim for older tools.

```python
#!/usr/bin/env python
"""
Minimal setup.py for compatibility.
Configuration is in pyproject.toml.
"""

from setuptools import setup

setup()
```

### 8.4 Dependency Pinning

**Production (CLI installed on NAS):**

Dependencies in `pyproject.toml` are **pinned to exact versions**:

```toml
dependencies = [
    "click==8.1.7",      # Exact version
    "docker==7.0.0",
    "requests==2.31.0",
]
```

**Rationale:**
- Ensures reproducibility
- No surprise breakages from dependency updates
- Tested combination of versions

**Development:**

Optional dependencies allow flexibility:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0",       # Minimum version, allow updates
    "black>=23.0",
]
```

**Updating Dependencies:**

```bash
# Discover available updates
pip list --outdated

# Test with new versions
pip install --upgrade click

# If tests pass, update pyproject.toml with new pinned version
# Then run full test suite before committing
```

### 8.5 Entry Points

**CLI Command:**

Defined in `pyproject.toml`:

```toml
[project.scripts]
syrvis = "syrviscore.cli:main"
```

**Translates to:**

```bash
# User types:
syrvis core status

# Python executes:
python -m syrviscore.cli main core status
```

**CLI Entry Point (src/syrviscore/cli.py):**

```python
#!/usr/bin/env python
"""
SyrvisCore CLI entry point.
"""

import click
from syrviscore import __version__
from syrviscore.core import status, logs, routes
from syrviscore.version import available, download, rollback, cleanup

@click.group()
@click.version_option(version=__version__)
def cli():
    """SyrvisCore - Self-hosted infrastructure for Synology NAS"""
    pass

# Register command groups
cli.add_command(core_group)
cli.add_command(stacks_group)
cli.add_command(version_group)
cli.add_command(config_group)
cli.add_command(manifest_group)

def main():
    """Main entry point"""
    cli()

if __name__ == '__main__':
    main()
```

### 8.6 Installation & Distribution

**Development Installation:**

```bash
# Clone repo
git clone https://github.com/you/syrviscore.git
cd syrviscore

# Install in editable mode
pip install -e .

# With dev dependencies
pip install -e ".[dev]"

# Now `syrvis` command available
syrvis --version
```

**Production Installation (on Synology):**

Done automatically by SPK postinst:

```bash
# SPK postinst creates venv
python3 -m venv /volume1/docker/syrviscore/cli/venv

# Activates venv and installs package
source /volume1/docker/syrviscore/cli/venv/bin/activate
pip install /volume1/docker/syrviscore/cli/

# Creates global symlink
ln -s /volume1/docker/syrviscore/cli/venv/bin/syrvis /usr/local/bin/syrvis
```

**User can then run:**

```bash
syrvis core status
# Uses isolated venv, no system Python pollution
```

---

## 9. Security & Secrets

### 9.1 Secrets Strategy

**Principle:** Secrets never stored in Git, SPK, or configuration files.

**Secrets Location:** `/volume1/secrets/` (encrypted Synology shared folder recommended)

**Required Secrets:**

1. **Cloudflare Origin Certificate** (`cloudflare-origin.crt`)
   - TLS certificate for `*.yourdomain.com`
   - Valid for 15 years
   - Only trusted by Cloudflare (sufficient for tunnel use case)

2. **Cloudflare Origin Key** (`cloudflare-origin.key`)
   - Private key for Origin Certificate
   - Must be kept secure

3. **Cloudflare Tunnel Credentials** (`cloudflared-credentials.json`)
   - Tunnel authentication token
   - Generated via `cloudflared tunnel create`

**Optional Secrets (.env file):**

4. **Portainer API Token**
   - For CLI automation
   - Generated after first Portainer login

5. **Healthchecks.io API Key**
   - For external monitoring integration

6. **ntfy Topic Name**
   - For push notifications

### 9.2 Secrets Handling

**Symlink Strategy:**

SyrvisCore symlinks secrets from `/volume1/secrets/` into core directories:

```bash
# Traefik certificates
/volume1/docker/syrviscore/core/traefik/certs/cloudflare-origin.crt
  → /volume1/secrets/cloudflare-origin.crt

/volume1/docker/syrviscore/core/traefik/certs/cloudflare-origin.key
  → /volume1/secrets/cloudflare-origin.key

# Cloudflared credentials
/volume1/docker/syrviscore/core/cloudflared/credentials.json
  → /volume1/secrets/cloudflared-credentials.json
```

**Benefits:**
- Secrets stored once, used by multiple services
- Easy to update (edit in `/volume1/secrets/`, changes propagate)
- Clear separation (secrets dir can be encrypted, backed up separately)

**SPK postinst creates symlinks:**

```bash
# Check if secrets exist
if [ -f /volume1/secrets/cloudflare-origin.crt ]; then
    ln -sf /volume1/secrets/cloudflare-origin.crt \
           /volume1/docker/syrviscore/core/traefik/certs/
fi
```

**If secrets missing, installation proceeds but containers won't start.**

### 9.3 .env File

**Purpose:** Non-secret environment variables and API tokens.

**Location:** `/volume1/docker/syrviscore/.env`

**Not Symlinked:** This is a unique file, not shared.

**Contents:**

```bash
# Portainer API Token (generate after first login)
PORTAINER_API_TOKEN=ptr_xxxxxxxxxxxxxxxxxxxxxxx

# Healthchecks.io API Key (optional)
HEALTHCHECKS_API_KEY=abc123def456

# ntfy Topic (optional)
NTFY_TOPIC=my-syrviscore-alerts

# Timezone
TZ=America/Los_Angeles
```

**Security Considerations:**

- File permissions: `600` (owner read/write only)
- Not in Git (`.gitignore` includes `.env`)
- Backed up by Hyper Backup (encrypted backup recommended)
- Template (`.env.template`) provided for reference

**Docker Compose loads automatically:**

```yaml
# docker-compose.yaml
services:
  portainer:
    env_file:
      - .env
```

### 9.4 Credential Rotation

**Current Approach:** Manual rotation

**Cloudflare Origin Certificate:**
- 15-year validity, rotation infrequent
- Process: Generate new cert → replace files → restart Traefik

**Cloudflare Tunnel Credentials:**
- No expiration
- Rotation: Create new tunnel → update credentials.json → restart cloudflared

**Portainer API Token:**
- User-controlled expiration
- Rotation: Generate new token in UI → update .env → restart CLI automation

**Future Enhancement:**

Could add CLI commands:

```bash
syrvis secrets rotate cloudflare-cert
syrvis secrets rotate portainer-token
```

### 9.5 Backup Strategy

**Secrets Backup:**

1. **Primary:** Hyper Backup includes `/volume1/secrets/`
   - Encrypted backup to S3
   - Automatic, scheduled

2. **Secondary:** 1Password (manual)
   - Certificate files as attachments
   - API tokens as secure notes
   - Disaster recovery fallback

**Recovery:**

After Hyper Backup restore, secrets automatically restored to `/volume1/secrets/`.

SPK postinst detects existing secrets, creates symlinks, installation continues normally.

---

## 10. Disaster Recovery

### 10.1 DR Philosophy

**Goal:** Restore SyrvisCore from zero to operational in < 1 hour.

**Prerequisites:**
- Hyper Backup to S3 (daily, encrypted)
- SPK version manifest in backup
- Secrets backup (Hyper Backup + 1Password)
- Documentation (this design doc + wiki)

### 10.2 Recovery Scenarios

**Scenario A: NAS hardware failure, total loss**

1. Acquire replacement NAS
2. Install DSM
3. Restore from Hyper Backup
4. Install Docker package
5. Read manifest for SPK version
6. Download and install SPK
7. Start SyrvisCore
8. Verify via CLI

**Estimated time:** 30-60 minutes (mostly Hyper Backup restore)

**Scenario B: Corrupt SyrvisCore installation**

1. Uninstall SyrvisCore SPK
2. Configuration preserved in `/volume1/docker/syrviscore/`
3. Reinstall same SPK version
4. Start SyrvisCore
5. Verify

**Estimated time:** 5-10 minutes

**Scenario C: Bad upgrade, need rollback**

1. Check previous version in manifest
2. Download previous SPK (or use cached version)
3. Restore from pre-upgrade backup
4. Verify

**Estimated time:** 10-15 minutes

### 10.3 Manifest-Based Recovery

**Key Insight:** `.syrviscore-manifest.json` contains everything needed to reconstruct the installation.

**Manifest includes:**
- Exact SPK version installed
- GitHub download URL for that version
- Git commit hash (for source inspection)
- Component versions (Docker images)
- Configuration snapshot

**Recovery Process:**

```bash
# After Hyper Backup restore
cat /volume1/docker/syrviscore/.syrviscore-manifest.json

# Extract SPK download URL
jq -r '.spk.download_url' .syrviscore-manifest.json

# Download exact version
wget <URL>

# Install via Package Center
# Done
```

**Automation Opportunity:**

Could create `syrvis disaster-recover` command:

```bash
syrvis disaster-recover

Reading manifest...
Detected installation: v1.0.0
Downloading SPK from GitHub...
✓ Ready for installation

Next steps:
  1. Package Center → Manual Install
  2. Upload: /tmp/syrviscore-1.0.0.spk
```

### 10.4 DR Runbook

**Separate document to be created:** `DR-RUNBOOK.md`

**Contents (outline):**
1. Prerequisites checklist
2. Step-by-step recovery procedure
3. Verification steps
4. Common issues and troubleshooting
5. Emergency contacts (if shared with others)

**Keep updated with each major version release.**

---

## 11. Future Enhancements

### 11.1 Web UI Dashboard

**Vision:** `https://syrvis.yourdomain.com` → Dashboard showing core + stacks status

**Features:**
- Visual core component status (green/yellow/red)
- Stack list with quick start/stop
- Recent logs viewer
- Version update notifications
- Quick links to Traefik, Portainer

**Technology:** React artifact (leveraging existing Traefik routing)

**Priority:** Medium (CLI is sufficient for MVP)

### 11.2 Synology DSM Integration

**Widget:** Show SyrvisCore status on DSM desktop

**Package Center Integration:**
- Richer package details page
- Resource usage graphs
- Direct links to services

**Notifications:** Send DSM notifications for updates, errors

**Priority:** Low (nice to have, but SPK already integrates reasonably)

### 11.3 Auto-Update Notifications

**Concept:** Proactive notifications when new versions available

**Implementation:**
- Daily cron job checks GitHub for releases
- Sends ntfy notification if new version found
- Includes changelog snippet
- Link to download SPK

**Configuration:**

```yaml
# syrviscore-config.yaml
notifications:
  update_checks: enabled
  ntfy_topic: my-syrviscore-alerts
```

**Priority:** Low (manual check via CLI is fine for MVP)

### 11.4 SyrvisStack Catalog

**Vision:** Curated catalog of pre-configured stacks

**Features:**
- Browse available stacks (Home Assistant, Wiki, Weaviate, etc.)
- One-click deployment via Portainer
- Version compatibility checking
- Community contributions

**Structure:**

```
syrvisstack-catalog/  (separate Git repo)
├── homeassistant/
│   ├── docker-compose.yaml
│   ├── stack.json
│   └── README.md
├── wiki/
└── weaviate/
```

**Integration:**

```bash
syrvis stacks browse
syrvis stacks deploy homeassistant
```

**Priority:** Medium (useful for community adoption)

### 11.5 Enhanced Monitoring

**Integration Points:**
- Prometheus metrics export
- Grafana dashboards
- Alertmanager for sophisticated alerting
- Log aggregation (Loki)

**Trade-off:** Adds complexity, may not be needed for home use

**Priority:** Low (Healthchecks.io + ntfy sufficient for most users)

---

## 12. Implementation Roadmap

### 12.1 Phase 1: MVP (Weeks 1-4)

**Goal:** Working SyrvisCore SPK with basic functionality

**Deliverables:**

1. **Build System** (Week 1)
   - `build-tools/select-docker-versions` (interactive version selector)
   - `build-tools/build-spk` (SPK builder)
   - `build/config.yaml` schema
   - GitHub Actions workflow
   - Test build locally and via CI/CD

2. **Core Package** (Week 2)
   - Python package structure (`src/syrviscore/`)
   - `pyproject.toml` configuration
   - SPK lifecycle scripts (postinst, preupgrade, etc.)
   - Manifest generation
   - Config validation

3. **CLI Basics** (Week 3)
   - `syrvis core status`
   - `syrvis core logs`
   - `syrvis version current`
   - `syrvis config show`
   - `syrvis manifest show`

4. **Testing & Documentation** (Week 4)
   - Install SPK on test Synology
   - Test upgrade flow
   - Test rollback
   - Write installation guide
   - Create example `syrviscore-config.yaml`

**Success Criteria:**
- SPK installs successfully
- Core containers start and are healthy
- CLI shows accurate status
- Upgrade works without data loss

### 12.2 Phase 2: Version Management (Weeks 5-6)

**Deliverables:**

1. **Version Commands**
   - `syrvis version available` (GitHub API integration)
   - `syrvis version download`
   - `syrvis version cleanup`
   - Version cache management

2. **Rollback Implementation**
   - `syrvis version rollback`
   - Backup/restore logic
   - Manifest tracking of previous version

3. **Testing**
   - Test upgrade v1.0.0 → v1.1.0
   - Test rollback v1.1.0 → v1.0.0
   - Verify cache cleanup

**Success Criteria:**
- Can check for updates
- Can download and cache SPKs
- Can rollback successfully

### 12.3 Phase 3: Stack Management (Weeks 7-8)

**Deliverables:**

1. **Stack Commands**
   - `syrvis stacks list`
   - `syrvis stacks sync`
   - Portainer API integration

2. **Example Stack**
   - Create `homeassistant-stack` repo
   - Document stack creation process
   - Test deployment via Portainer

3. **Documentation**
   - Stack developer guide
   - Migration guide from existing setups

**Success Criteria:**
- Can list deployed stacks
- Can sync stack definitions
- Home Assistant stack deploys successfully

### 12.4 Phase 4: Polish & Community (Weeks 9-10)

**Deliverables:**

1. **Documentation**
   - Complete wiki (installation, upgrade, troubleshooting)
   - DR runbook
   - CONTRIBUTING.md
   - Architecture diagrams

2. **Community Prep**
   - GitHub repo cleanup
   - Issue templates
   - README with badges
   - License (MIT recommended)

3. **Optional Enhancements**
   - Web UI (if time permits)
   - Additional CLI commands
   - More example stacks

**Success Criteria:**
- Documentation complete enough for external users
- GitHub repo ready for public release
- 1-2 test users successfully install and use

### 12.5 Post-MVP Backlog

**Nice to Have:**
- Auto-update notifications
- SyrvisStack catalog
- Web UI dashboard
- Enhanced monitoring
- Synology DSM widgets
- More pre-built stacks

**Community Contributions:**
- Stack submissions
- Translation
- Bug fixes
- Feature requests

---

## Appendices

### A. File Extension Convention

**Preference:** `.yaml` over `.yml`

**Rationale:**
- YAML official extension is `.yaml` (per YAML spec)
- More explicit and recognizable
- Consistent with modern tooling

**Exceptions:**
- GitHub Actions uses `.yml` by convention (follow their standard)
- Docker Compose examples often use `.yml` (but `.yaml` also works)

**Decision:** Use `.yaml` for all SyrvisCore files except where external conventions dictate otherwise.

### B. Glossary

- **Core:** SyrvisCore infrastructure components (Traefik, Portainer, Cloudflared)
- **Stack:** SyrvisStack application containers (Home Assistant, Wiki, etc.)
- **SPK:** Synology Package file format
- **Manifest:** `.syrviscore-manifest.json` - installation metadata
- **Build Config:** `build/config.yaml` - pinned version definitions
- **User Config:** `syrviscore-config.yaml` - user-facing configuration
- **venv:** Python virtual environment (isolated dependency installation)

### C. References

**External Documentation:**
- Synology DSM: https://www.synology.com/en-us/dsm
- Docker: https://docs.docker.com/
- Traefik: https://doc.traefik.io/traefik/
- Portainer: https://docs.portainer.io/
- Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/

**Python Packaging:**
- pyproject.toml: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- Click: https://click.palletsprojects.com/
- Tox: https://tox.wiki/

### D. License

**Recommended:** MIT License

**Rationale:**
- Permissive, community-friendly
- Allows commercial and personal use
- Simple and well-understood
- Compatible with most dependencies

---

## End of Design Document

**Status:** Ready for Implementation

**Next Steps:**
1. Review and approve design
2. Create implementation issues/tasks per phase
3. Begin Phase 1: Build System

**Questions or Feedback:** Open GitHub issue or discussion

**Version:** 1.0.0-draft  
**Last Updated:** 2024-11-29
