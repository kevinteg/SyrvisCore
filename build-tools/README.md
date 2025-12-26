# SyrvisCore Build Tools

Build utilities for SyrvisCore's split-package architecture.

## Quick Start

```bash
# See all available commands
make help

# Build complete SPK (recommended)
make build-spk

# Or run scripts directly
./build-tools/build-manager.sh
./build-tools/build-spk.sh
```

## Architecture

SyrvisCore uses a split-package architecture:

| Package | Build Script | Output |
|---------|--------------|--------|
| Manager (`syrviscore-manager`) | `build-manager.sh` | Wheel + dependencies |
| Service (`syrviscore`) | `build-service.sh` | Wheel (downloaded at runtime) |
| SPK | `build-spk.sh` | Complete installable package |

## Build Scripts

### build-manager.sh

Builds the manager package wheel and downloads all dependencies for offline installation.

```bash
./build-tools/build-manager.sh
```

**What it does:**
1. Builds wheel from `packages/syrviscore-manager/`
2. Downloads all dependencies as wheels for target platform
3. Outputs to `dist/`:
   - `syrviscore_manager-{version}-py3-none-any.whl`
   - `dist/manager-deps/` (dependency wheels)

**Dependency bundling:**
```bash
pip download \
    --dest dist/manager-deps/ \
    --only-binary=:all: \
    --python-version 3.8 \
    --platform manylinux2014_x86_64 \
    --platform linux_x86_64 \
    --platform any \
    syrviscore_manager-*.whl
```

This ensures the SPK can install without network access on Synology.

---

### build-service.sh

Builds the service package wheel for GitHub release.

```bash
./build-tools/build-service.sh
```

**What it does:**
1. Builds wheel from `packages/syrviscore/`
2. Outputs to `dist/syrviscore-{version}-py3-none-any.whl`

**Note:** Service wheel is NOT bundled in SPK. It's downloaded at runtime by `syrvisctl install`.

---

### build-spk.sh

Builds the complete SPK package.

```bash
./build-tools/build-spk.sh
```

**What it does:**
1. Builds manager wheel (calls `build-manager.sh`)
2. Creates SPK directory structure
3. Bundles manager wheel + all dependencies
4. Packages scripts, INFO, icons
5. Creates SPK archive

**Output:**
- `dist/syrviscore-{version}-noarch.spk`

**SPK Contents:**
```
syrviscore-{version}-noarch.spk
├── INFO                    # Package metadata
├── package.tgz             # Compressed package contents
│   ├── wheels/             # Manager + all dependencies
│   │   ├── syrviscore_manager-*.whl
│   │   ├── click-*.whl
│   │   ├── requests-*.whl
│   │   └── ...
│   └── bin/                # Helper scripts
├── scripts/                # Lifecycle scripts
│   ├── preinst
│   ├── postinst
│   ├── preuninst
│   ├── postuninst
│   ├── preupgrade
│   ├── postupgrade
│   └── start-stop-status
├── conf/
│   ├── privilege
│   └── resource
├── WIZARD_UIFILES/         # Installation wizard
└── PACKAGE_ICON*.PNG       # Package icons
```

---

### select-docker-versions

Interactive tool for selecting Docker image versions.

```bash
./build-tools/select-docker-versions
```

**Features:**
- Queries Docker Hub for available versions
- Shows release dates
- Updates `build/config.yaml`

**Output (`build/config.yaml`):**
```yaml
metadata:
  syrviscore_version: 0.1.0
  created_at: '2024-12-25T12:00:00Z'

docker_images:
  traefik:
    image: traefik
    tag: v3.2.0
    full_image: traefik:v3.2.0
  portainer:
    image: portainer/portainer-ce
    tag: 2.21.4
    full_image: portainer/portainer-ce:2.21.4
  cloudflared:
    image: cloudflare/cloudflared
    tag: 2024.11.0
    full_image: cloudflare/cloudflared:2024.11.0
```

---

### validate-spk.sh

Validates SPK package structure for DSM 7.x compatibility.

```bash
./build-tools/validate-spk.sh dist/syrviscore-*.spk
```

**Checks:**
- File format (uncompressed tar)
- Required files present
- Script permissions
- INFO file fields
- privilege JSON validity

## Makefile Integration

```bash
# Development
make dev-install      # Install packages in dev mode
make lint             # Run ruff linter
make format           # Format with black
make test             # Run tests

# Building
make build-spk        # Build complete SPK (recommended)

# The build-spk target:
# 1. Calls build-manager.sh (builds wheel + downloads deps)
# 2. Calls build-spk.sh (creates SPK with bundled deps)

# Validation
make validate         # Validate SPK structure
```

## Dependency Bundling

The SPK bundles all Python dependencies for offline installation:

**Manager dependencies (bundled in SPK):**
- click
- requests
- urllib3
- certifi
- charset-normalizer
- idna

**How it works:**

1. `build-manager.sh` downloads wheels:
   ```bash
   pip download --only-binary=:all: \
       --python-version 3.8 \
       --platform manylinux2014_x86_64 \
       ...
   ```

2. `build-spk.sh` bundles wheels into `package/wheels/`

3. `postinst` installs from bundled wheels:
   ```sh
   pip install --no-index --find-links "$WHEELS_DIR" "$WHEEL"
   ```

**Result:** No network access required during SPK installation.

## Development Workflow

### Full Build

```bash
# Clean + build everything
make clean
make build-spk
make validate
```

### Testing Changes

```bash
# Rebuild just manager
./build-tools/build-manager.sh

# Rebuild SPK
./build-tools/build-spk.sh

# Test with simulation
source tests/dsm-sim/activate.sh
# ... test postinst, syrvisctl, etc.
```

### Release Process

1. Update versions in `pyproject.toml` files
2. Update `build/config.yaml` with Docker versions
3. Build and test:
   ```bash
   make build-spk
   make test-sim
   ```
4. Create GitHub release with:
   - `syrviscore-{version}-noarch.spk` (manager)
   - `syrviscore-{version}-py3-none-any.whl` (service)

## Troubleshooting

### "Wheel not found"

```bash
# Rebuild wheel first
./build-tools/build-manager.sh

# Then SPK
./build-tools/build-spk.sh
```

### "Dependency download failed"

```bash
# Check network
pip download --help

# Try with different platform
pip download --platform any ...
```

### "SPK validation failed"

```bash
# Check SPK contents
tar -tvf dist/syrviscore-*.spk

# Check scripts are executable
tar -xf dist/syrviscore-*.spk scripts/
ls -la scripts/
```

## See Also

- [Development Guide](../docs/dev-guide.md) - Full development setup
- [Design Document](../docs/design-doc.md) - Architecture overview
