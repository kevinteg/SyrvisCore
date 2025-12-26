# SyrvisCore Build Tools

This directory contains utilities for building SyrvisCore SPK packages.

## Quick Start

**For most users, use the Makefile instead of calling these tools directly:**

```bash
# See all available commands
make help

# Development workflow
make dev-install      # Install dependencies
make test             # Run tests
make lint             # Check code quality
make build-spk        # Build complete SPK package

# Full build pipeline
make all              # lint + test + build-spk
```

**Continue reading if you need to call build tools directly.**

## Tools

### `select-docker-versions`

Interactive tool for discovering and selecting Docker image versions.

**Purpose:** Create a versioned build configuration (`build/config.yaml`) with pinned Docker image tags for reproducible builds.

**Usage:**

```bash
# Interactive mode (recommended)
./build-tools/select-docker-versions

# Non-interactive mode (use latest stable versions)
./build-tools/select-docker-versions --non-interactive

# Custom output path
./build-tools/select-docker-versions --output my-build-config.yaml
```

**Features:**
- Fetches latest stable versions from Docker Hub
- Filters out development/beta tags
- Shows version metadata (update date, size)
- Allows manual tag entry for specific versions
- Optional components can be skipped
- Generates YAML build configuration

**Requirements:**

Dependencies are automatically installed when you set up the development environment:

```bash
pip install -e ".[dev]"
```

This installs all required packages including `requests`, `pyyaml`, `click`, and `docker`.

**Output Example:**

Creates `build/config.yaml` with Docker image versions only:

```yaml
metadata:
  syrviscore_version: 0.0.1
  created_at: '2024-11-29T12:00:00Z'
  created_by: select-docker-versions

docker_images:
  traefik:
    image: traefik
    tag: v3.0.0
    full_image: traefik:v3.0.0
  portainer:
    image: portainer/portainer-ce
    tag: 2.19.4
    full_image: portainer/portainer-ce:2.19.4
  cloudflared:
    image: cloudflare/cloudflared
    tag: 2024.10.0
    full_image: cloudflare/cloudflared:2024.10.0
```

**Note:** Python dependencies are managed in `pyproject.toml`, not in `build/config.yaml`. The build config only contains Docker image versions for reproducible builds.

---

### `build-python-package.sh`

Builds standard Python wheel package using Python's official packaging tools.

**Usage:**

```bash
./build-tools/build-python-package.sh
```

**Or via Makefile:**

```bash
make build-wheel
```

**Output:** Creates wheel file in `dist/syrviscore-{version}-py3-none-any.whl`

---

### `build-spk.sh`

Tool to build complete SyrvisCore SPK package from wheel and configuration.

**Usage:**

```bash
# Build wheel first
./build-tools/build-python-package.sh

# Then build SPK
./build-tools/build-spk.sh
```

**Or via Makefile (recommended):**

```bash
make build-spk  # Automatically builds wheel first
```

**Output:** Creates SPK file in `dist/syrviscore-{version}-noarch.spk`

---

### `validate-spk.sh`

Comprehensive SPK validation tool for DSM 7.1+ compatibility.

**Usage:**

```bash
./build-tools/validate-spk.sh dist/syrviscore-0.0.1-noarch.spk
```

**Or via Makefile:**

```bash
make validate
```

**Checks:**
- File format (must be uncompressed tar)
- Required files (INFO, package.tgz, scripts/, conf/)
- Script permissions (must be executable)
- INFO file fields (DSM 7 compatibility)
- conf/privilege JSON validation
- Ownership and permissions


---

## Workflow

### Option 1: Using Makefile (Recommended)

```bash
# Complete workflow
make all              # Runs lint + test + build-spk

# Validate package
make validate

# Install to Synology (requires SSH)
make install SSH_HOST=192.168.0.100
```

### Option 2: Manual Steps

#### 1. Select Docker Versions
```bash
./build-tools/select-docker-versions
# Or: make select-docker-versions
```

This creates `build/config.yaml` with pinned versions.

#### 2. Review Configuration
```bash
cat build/config.yaml
```

Verify the selected versions are correct.

#### 3. Build Python Wheel
```bash
./build-tools/build-python-package.sh
# Or: make build-wheel
```

#### 4. Build SPK Package
```bash
./build-tools/build-spk.sh
# Or: make build-spk
```

This generates the installable `.spk` file in `dist/`.

#### 5. Validate SPK
```bash
./build-tools/validate-spk.sh dist/syrviscore-{version}-noarch.spk
# Or: make validate
```

#### 6. Install to Synology
```bash
# Via Makefile (requires SSH access)
make install SSH_HOST=192.168.0.100

# Or manually upload to Package Center
# Or via SSH manually:
scp dist/syrviscore-*.spk admin@192.168.0.100:/tmp/
ssh admin@192.168.0.100 "sudo synopkg install /tmp/syrviscore-*.spk"
```

---

## Build Configuration Schema

The `build/config.yaml` file contains **Docker image versions only**:

```yaml
metadata:
  syrviscore_version: string      # SyrvisCore version (e.g., "1.0.0")
  created_at: datetime            # ISO 8601 timestamp
  created_by: string              # Tool or user that created config

docker_images:
  <component_name>:               # e.g., traefik, portainer, cloudflared
    image: string                 # Docker Hub repository
    tag: string                   # Specific version tag
    full_image: string            # Combined image:tag
```

**Important:** Python dependencies are managed in `pyproject.toml`, not in this file.

---

## Development

### Adding a New Component

To add a new Docker component to the selector:

1. Edit `select-docker-versions`
2. Add to `self.components` dict:

```python
"mycomponent": {
    "repository": "dockerhub/repo-name",
    "description": "My Component Description",
    "required": False,  # or True
}
```

3. Add to interactive loop if needed

### Testing

```bash
# Test version fetching
python3 -c "
from select-docker-versions import DockerHubClient
client = DockerHubClient()
tags = client.get_tags('traefik', limit=5)
print([t['name'] for t in tags])
"

# Test config generation
./build-tools/select-docker-versions --non-interactive --output /tmp/test.yaml
cat /tmp/test.yaml
```

---

## Troubleshooting

### Error: "ModuleNotFoundError" or import errors

Ensure you've installed the package in development mode:

```bash
# From the project root
pip install -e ".[dev]"
```

This installs all required dependencies including `requests`, `pyyaml`, `click`, and development tools.

### Error: "No versions found"

- Check internet connection
- Docker Hub may be rate-limiting (wait a few minutes)
- Repository name might be incorrect

### Build config not created

- Ensure `build/` directory exists: `mkdir -p build`
- Check write permissions
- Verify output path is valid

---

## Future Enhancements

- [ ] Version comparison (highlight major/minor/patch updates)
- [ ] Changelog integration (show what changed between versions)
- [ ] Vulnerability scanning integration
- [ ] Multi-architecture support detection
- [ ] Automated update checking (CI/CD integration)

---

## License

Part of SyrvisCore - MIT License
