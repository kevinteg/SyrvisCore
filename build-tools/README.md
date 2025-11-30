# SyrvisCore Build Tools

This directory contains utilities for building SyrvisCore SPK packages.

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
```bash
pip install requests pyyaml
```

**Output Example:**

Creates `build/config.yaml`:
```yaml
metadata:
  syrviscore_version: 0.1.0-dev
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
python_dependencies:
  click: '>=8.1.0'
  pyyaml: '>=6.0'
  requests: '>=2.31.0'
```

---

### `build-spk` *(Coming Soon)*

Tool to build SyrvisCore SPK package from build configuration.

**Planned Usage:**
```bash
./build-tools/build-spk --config build/config.yaml
```

Will generate: `dist/syrviscore-0.1.0-dev.spk`

---

## Workflow

### 1. Select Versions
```bash
./build-tools/select-docker-versions
```

This creates `build/config.yaml` with pinned versions.

### 2. Review Configuration
```bash
cat build/config.yaml
```

Verify the selected versions are correct.

### 3. Build SPK *(Coming Soon)*
```bash
./build-tools/build-spk
```

This generates the installable `.spk` file.

### 4. Test Installation
```bash
# Upload to Synology Package Center or install via command line
```

---

## Build Configuration Schema

The `build/config.yaml` file follows this schema:

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

python_dependencies:
  <package_name>: string          # Version specifier (e.g., ">=8.1.0")
```

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

### Error: "Required packages not installed"

```bash
pip install requests pyyaml
```

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
