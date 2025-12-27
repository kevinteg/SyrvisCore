# SyrvisCore Backup/Restore Design

## Overview

SyrvisCore provides automatic backup on upgrade and full point-in-time restore capabilities for disaster recovery and safe rollback.

## Design Principles

1. **Automatic safety nets** - Every upgrade creates a backup automatically
2. **Full restore** - Rollback restores code + config (point-in-time recovery)
3. **Version-tagged backups** - Each backup is named by the version it represents
4. **Disaster recovery** - Backups include wheel files for offline restoration

## Directory Structure

```
$SYRVIS_HOME/
├── backups/                           # Automatic backup location
│   ├── 0.1.10.tar.gz                  # Backup of 0.1.10 state
│   ├── 0.1.11.tar.gz                  # Backup of 0.1.11 state
│   └── 0.1.12.tar.gz                  # Backup of 0.1.12 state (current)
├── config/                            # Current configuration
├── data/                              # Current data
├── versions/                          # Installed versions
└── current -> versions/0.1.12/        # Active version
```

## Backup Archive Contents

Each backup `X.Y.Z.tar.gz` contains:

```
X.Y.Z.tar.gz
├── backup-metadata.json               # Backup info
├── manifest.json                      # Copy of .syrviscore-manifest.json
├── config/
│   └── .env                           # Configuration at this point
├── data/
│   ├── traefik/
│   │   ├── acme.json                  # SSL certificates
│   │   ├── traefik.yml                # Static config
│   │   └── config/dynamic.yml         # Dynamic config
│   ├── portainer/                     # Container management DB
│   └── cloudflared/                   # Tunnel credentials
└── wheel/
    └── syrviscore-X.Y.Z-py3-none-any.whl  # Service wheel file
```

**backup-metadata.json:**
```json
{
  "backup_version": 1,
  "created_at": "2025-12-26T15:30:00",
  "version": "0.1.12",
  "manager_version": "0.1.0",
  "reason": "pre-upgrade",
  "previous_version": null,
  "upgraded_to": "0.1.13",
  "syrvis_home": "/volume4/syrviscore"
}
```

## CLI Commands

### syrvisctl install (modified)

```bash
syrvisctl install [version]
```

**New behavior:** Before installing/upgrading, automatically creates a backup:

```
Installing SyrvisCore service...

  Current version: 0.1.12
  Target version:  0.1.13

[1/5] Creating backup of current state...
      Backup: /volume4/syrviscore/backups/0.1.12.tar.gz

[2/5] Downloading syrviscore-0.1.13...
...
```

### syrvisctl rollback (modified)

```bash
syrvisctl rollback [version]
```

**New behavior:** Full restore from backup (code + config):

```
SyrvisCore Rollback

  Current version: 0.1.13
  Available backups:
    0.1.12 (2025-12-26) - pre-upgrade
    0.1.11 (2025-12-25) - pre-upgrade
    0.1.10 (2025-12-24) - pre-upgrade

  Rollback to [0.1.12]:

Rolling back to 0.1.12...

[1/4] Stopping services...
[2/4] Restoring configuration...
[3/4] Activating version 0.1.12...
[4/4] Running doctor...

Rollback complete!

  Run 'syrvis start' to start services.
```

### syrvisctl backup (new)

```bash
syrvisctl backup [--output FILE]
```

Create a manual backup (for disaster recovery / external storage):

```
Creating SyrvisCore backup...

  Version: 0.1.12
  Output:  ~/syrviscore-backup-0.1.12.tar.gz

  Included:
    - Configuration (.env)
    - SSL certificates (acme.json)
    - Traefik config
    - Portainer data
    - Service wheel

Backup complete: ~/syrviscore-backup-0.1.12.tar.gz (2.3 MB)
```

### syrvisctl restore (new)

```bash
syrvisctl restore [backup-file] [--path INSTALL_PATH]
```

Restore from backup (for disaster recovery):

```bash
# Enumerate available backups in default location
syrvisctl restore
# Output:
# Available backups:
#   1. 0.1.12 (2025-12-26) - /volume4/syrviscore/backups/0.1.12.tar.gz
#   2. 0.1.11 (2025-12-25) - /volume4/syrviscore/backups/0.1.11.tar.gz
#
# Select backup [1]:

# Restore from specific file (disaster recovery)
syrvisctl restore ~/syrviscore-backup-0.1.12.tar.gz --path /volume4/syrviscore
```

### syrvisctl backup list (new)

```bash
syrvisctl backup list
```

List available backups:

```
Available backups:

  Version   Date        Size    Reason
  -------   ----        ----    ------
  0.1.12    2025-12-26  2.3 MB  pre-upgrade
  0.1.11    2025-12-25  2.1 MB  pre-upgrade
  0.1.10    2025-12-24  2.0 MB  pre-upgrade

  Location: /volume4/syrviscore/backups/
```

### syrvisctl backup cleanup (new)

```bash
syrvisctl backup cleanup [--keep N]
```

Remove old backups:

```bash
syrvisctl backup cleanup --keep 3
# Keeps most recent 3 backups, removes older ones
```

## Implementation Details

### Wheel Caching

During `syrvisctl install`, cache the wheel file:

```python
def download_and_install(version, ...):
    # Download wheel
    wheel_path = downloader.download_wheel(version, temp_dir)

    # Cache wheel in version directory
    wheel_cache = version_dir / "wheel"
    wheel_cache.mkdir(exist_ok=True)
    shutil.copy(wheel_path, wheel_cache / wheel_path.name)

    # Install to venv
    ...
```

### Pre-Upgrade Backup

```python
def create_pre_upgrade_backup(current_version: str, target_version: str) -> Path:
    """Create backup before upgrading."""
    backup_dir = paths.get_syrvis_home() / "backups"
    backup_dir.mkdir(exist_ok=True)

    backup_path = backup_dir / f"{current_version}.tar.gz"

    # If backup already exists, we're re-upgrading from same version
    # Keep the original backup
    if backup_path.exists():
        return backup_path

    create_backup(
        output_path=backup_path,
        metadata={
            "reason": "pre-upgrade",
            "upgraded_to": target_version,
        }
    )

    return backup_path
```

### Rollback Implementation

```python
def rollback_to_version(version: str) -> bool:
    """Full rollback: restore code + config from backup."""
    backup_path = paths.get_backup_path(version)

    if not backup_path.exists():
        raise ValueError(f"No backup found for version {version}")

    # 1. Stop services
    run_syrvis_stop()

    # 2. Extract and restore config/data
    restore_config_from_backup(backup_path)

    # 3. Ensure version is installed (from cached wheel if needed)
    ensure_version_installed(version, backup_path)

    # 4. Activate version
    paths.update_current_symlink(version)
    manifest.set_active_version(version)

    # 5. Run doctor
    run_syrvis_doctor()

    return True
```

## Disaster Recovery Flow

### Regular Operation (automatic)

```bash
# User upgrades - backup happens automatically
syrvisctl install 0.1.13
# Creates: $SYRVIS_HOME/backups/0.1.12.tar.gz

# Something goes wrong? Full rollback
syrvisctl rollback 0.1.12
# Restores code + config to pre-upgrade state
```

### Full Disaster Recovery

```bash
# 1. Fresh DSM install - Install SPK via Package Center
# 2. Source profile
source /var/packages/syrviscore/target/syrviscore.profile

# 3. Restore from external backup
syrvisctl restore /volume1/recovery/syrviscore-backup-0.1.12.tar.gz

# 4. Verify and start
syrvis doctor
syrvis start
```

### Hyper Backup Integration

For automated off-NAS backups:
1. Include `$SYRVIS_HOME/backups/` in Hyper Backup task
2. Or run `syrvisctl backup --output /volume1/backups/` in scheduled task

## Testing Strategy

### Simulation Test

```bash
# 1. Install version 0.1.11
syrvisctl install 0.1.11
syrvis setup --non-interactive

# 2. Upgrade to 0.1.12 (creates backup)
syrvisctl install 0.1.12

# 3. Verify backup exists
ls $SYRVIS_HOME/backups/
# 0.1.11.tar.gz

# 4. Modify config
echo "TEST=true" >> $SYRVIS_HOME/config/.env

# 5. Rollback
syrvisctl rollback 0.1.11

# 6. Verify config was restored (TEST=true should be gone)
grep TEST $SYRVIS_HOME/config/.env
# (no output - config was restored)
```

### Disaster Recovery Test

```bash
# 1. Create manual backup
syrvisctl backup --output ~/test-backup.tar.gz

# 2. Simulate disaster
rm -rf $SYRVIS_HOME

# 3. Restore
syrvisctl restore ~/test-backup.tar.gz

# 4. Verify
syrvis doctor
syrvis status
```

## File Inventory

### New Files
```
packages/syrviscore-manager/src/syrviscore_manager/
├── backup.py       # create_backup(), restore_from_backup()
└── cli.py          # Add backup, restore, rollback commands
```

### Modified Files
```
packages/syrviscore-manager/src/syrviscore_manager/
├── version_manager.py   # Add pre-upgrade backup, wheel caching
├── paths.py             # Add get_backups_dir(), get_backup_path()
└── downloader.py        # Ensure wheel file is accessible for caching
```
