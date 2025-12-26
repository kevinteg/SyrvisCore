# SyrvisCore Manager

Version management CLI for SyrvisCore on Synology NAS.

## Overview

`syrvisctl` is the management utility installed via SPK package. It handles:

- Installing service versions from GitHub releases
- Managing multiple installed versions
- Activating/rolling back versions
- Checking for updates

## Installation

The manager is installed automatically when you install the SyrvisCore SPK package on your Synology NAS.

## Commands

```bash
# Install latest service version
syrvisctl install

# Install specific version
syrvisctl install 0.2.0

# List installed versions
syrvisctl list

# Check for updates
syrvisctl check

# Activate a specific version
syrvisctl activate 0.1.0

# Rollback to previous version
syrvisctl rollback

# Remove a version
syrvisctl uninstall 0.1.0

# Clean up old versions (keep 2)
syrvisctl cleanup --keep 2

# Show installation info
syrvisctl info
```

## After Installing a Service Version

After installing with `syrvisctl install`, run the service setup:

```bash
# Configure the service
syrvis setup

# Start the services
syrvis start
```

## Directory Structure

```
/var/packages/syrviscore/target/      # SPK install (manager)
├── venv/bin/syrvisctl                 # Manager CLI
└── syrviscore_manager-*.whl

/volumeX/docker/syrviscore/            # Service installation
├── current -> versions/0.2.0          # Active version symlink
├── versions/
│   ├── 0.1.0/cli/venv/bin/syrvis      # Previous version
│   └── 0.2.0/cli/venv/bin/syrvis      # Active version
├── config/                            # Shared config
├── data/                              # Persistent data
└── .syrviscore-manifest.json
```
