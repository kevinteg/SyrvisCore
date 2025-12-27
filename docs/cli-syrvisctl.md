# syrvisctl CLI Reference

`syrvisctl` is the manager CLI for SyrvisCore. It handles version management, installation, and updates for the service package.

## Installation

The `syrvisctl` command is installed automatically when you install the SyrvisCore SPK package on your Synology NAS.

**Location:** `/usr/local/bin/syrvisctl` (symlink to venv)

## Commands

### syrvisctl install

Download and install a service package version from GitHub.

```bash
syrvisctl install [VERSION]
```

**Arguments:**
- `VERSION` - (Optional) Specific version to install. If omitted, installs latest.

**Examples:**
```bash
# Install latest version
syrvisctl install

# Install specific version
syrvisctl install 0.2.0
```

**What it does:**
1. Downloads service wheel from GitHub releases
2. Creates version-specific virtual environment at `versions/{version}/cli/venv/`
3. Installs the `syrvis` CLI
4. Updates `current` symlink to new version

---

### syrvisctl uninstall

Remove an installed service version.

```bash
syrvisctl uninstall VERSION
```

**Arguments:**
- `VERSION` - Version to uninstall

**Example:**
```bash
syrvisctl uninstall 0.1.0
```

**Notes:**
- Cannot uninstall the currently active version
- Removes the entire `versions/{version}/` directory

---

### syrvisctl list

List all installed service versions.

```bash
syrvisctl list
```

**Output:**
```
Installed versions:
  0.1.0     installed 2024-12-20
* 0.2.0     installed 2024-12-25 (active)
```

The asterisk (*) indicates the currently active version.

---

### syrvisctl activate

Switch to a different installed version.

```bash
syrvisctl activate VERSION
```

**Arguments:**
- `VERSION` - Version to activate (must be already installed)

**Example:**
```bash
syrvisctl activate 0.1.0
```

**What it does:**
1. Stops running services
2. Updates `current` symlink to point to new version
3. Restarts services with new version

---

### syrvisctl rollback

Roll back to the previous version.

```bash
syrvisctl rollback
```

**Example:**
```bash
syrvisctl rollback
```

**What it does:**
1. Identifies previous version from manifest history
2. Stops current services
3. Switches `current` symlink to previous version
4. Restarts services

**Notes:**
- Requires at least one previous version to be installed
- Equivalent to `syrvisctl activate {previous_version}`

---

### syrvisctl check

Check for available updates on GitHub.

```bash
syrvisctl check
```

**Output:**
```
Current version: 0.2.0
Latest available: 0.3.0

Changes in 0.3.0:
  - Updated Traefik to v3.3.0
  - Bug fixes

Run 'syrvisctl install 0.3.0' to update.
```

---

### syrvisctl info

Display installation information.

```bash
syrvisctl info
```

**Output:**
```
SyrvisCore Installation Info
============================
Manager version:  0.1.0
Service version:  0.2.0 (active)
Install path:     /volume1/syrviscore
Setup complete:   yes

Installed versions: 2
  0.1.0 (available)
  0.2.0 (active)
```

---

### syrvisctl cleanup

Remove old versions to free disk space.

```bash
syrvisctl cleanup [--keep N]
```

**Options:**
- `--keep N` - Number of versions to keep (default: 2)

**Examples:**
```bash
# Keep default (2 most recent versions)
syrvisctl cleanup

# Keep only current version
syrvisctl cleanup --keep 1

# Keep 3 versions
syrvisctl cleanup --keep 3
```

**Notes:**
- Always keeps the currently active version
- Shows what will be removed and asks for confirmation

---

### syrvisctl migrate

Migrate from a legacy (pre-v3) installation.

```bash
syrvisctl migrate
```

**What it does:**
1. Detects legacy installation structure
2. Moves CLI to versioned layout
3. Creates proper manifest
4. Sets up symlinks

**When to use:**
- Upgrading from v2 or earlier installation
- Converting single-package installation to split-package

---

### syrvisctl --version

Display version information.

```bash
syrvisctl --version
```

**Output:**
```
syrvisctl, version 0.1.0
```

---

### syrvisctl --help

Display help information.

```bash
syrvisctl --help
syrvisctl install --help
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SYNOPKG_PKGDEST` | SPK installation directory (set by DSM) |
| `SYRVIS_HOME` | Service data directory |
| `GITHUB_TOKEN` | (Optional) GitHub token for API rate limits |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Invalid arguments |
| 3 | Version not found |
| 4 | Network error |
| 5 | Permission denied |

## Files

| File | Description |
|------|-------------|
| `/var/packages/syrviscore/target/venv/` | Manager virtual environment |
| `$SYRVIS_HOME/versions/` | Installed service versions |
| `$SYRVIS_HOME/current` | Symlink to active version |
| `$SYRVIS_HOME/.syrviscore-manifest.json` | Installation manifest |

## See Also

- [syrvis CLI Reference](cli-syrvis.md) - Service CLI documentation
- [SPK Installation Guide](spk-installation-guide.md) - Installation instructions
- [Design Document](design-doc.md) - Architecture overview
