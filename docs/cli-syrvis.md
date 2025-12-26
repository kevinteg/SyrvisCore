# syrvis CLI Reference

`syrvis` is the service CLI for SyrvisCore. It manages Docker services (Traefik, Portainer, Cloudflared), configuration, and diagnostics.

## Installation

The `syrvis` command is installed when you run `syrvisctl install`. Each version has its own virtual environment.

**Location:** `$SYRVIS_HOME/bin/syrvis` (wrapper script)

## Commands

### syrvis setup

Run interactive setup to configure SyrvisCore.

```bash
syrvis setup
```

**What it does:**
1. Checks prerequisites (Python, Docker)
2. Prompts for sudo if privileged operations needed
3. Creates/updates docker group membership
4. Sets Docker socket permissions
5. Generates configuration files
6. Creates startup scripts

**Options:**
```bash
syrvis setup --non-interactive  # Use defaults, no prompts
```

**Notes:**
- Safe to re-run to reconfigure
- Self-elevates with sudo when needed

---

### syrvis status

Display status of all services.

```bash
syrvis status
```

**Output:**
```
SyrvisCore Status
=================
traefik:     running  (192.168.1.100)
portainer:   running
cloudflared: disabled

Uptime: 5 days, 3 hours
```

---

### syrvis start

Start all services.

```bash
syrvis start [SERVICE]
```

**Arguments:**
- `SERVICE` - (Optional) Specific service to start

**Examples:**
```bash
# Start all services
syrvis start

# Start specific service
syrvis start traefik
```

---

### syrvis stop

Stop all services.

```bash
syrvis stop [SERVICE]
```

**Arguments:**
- `SERVICE` - (Optional) Specific service to stop

**Examples:**
```bash
# Stop all services
syrvis stop

# Stop specific service
syrvis stop cloudflared
```

---

### syrvis restart

Restart all services.

```bash
syrvis restart [SERVICE]
```

**Arguments:**
- `SERVICE` - (Optional) Specific service to restart

**Examples:**
```bash
# Restart all
syrvis restart

# Restart specific service
syrvis restart traefik
```

---

### syrvis logs

View service logs.

```bash
syrvis logs [SERVICE] [OPTIONS]
```

**Arguments:**
- `SERVICE` - (Optional) Service name (traefik, portainer, cloudflared)

**Options:**
- `-f, --follow` - Follow log output
- `-n, --tail N` - Show last N lines (default: 100)
- `--since TIME` - Show logs since timestamp

**Examples:**
```bash
# All logs
syrvis logs

# Specific service
syrvis logs traefik

# Follow logs
syrvis logs -f traefik

# Last 50 lines
syrvis logs -n 50 portainer

# Logs since time
syrvis logs --since "2024-12-25T10:00:00"
```

---

### syrvis doctor

Diagnose and optionally fix common issues.

```bash
syrvis doctor [OPTIONS]
```

**Options:**
- `--fix` - Attempt to automatically fix issues

**Output:**
```
SyrvisCore Diagnostics
======================

[PASS] Python version: 3.8.12
[PASS] Docker installed
[PASS] Docker running
[WARN] User not in docker group
[PASS] Docker socket exists
[FAIL] Docker socket not accessible

Issues found: 2
Run 'syrvis doctor --fix' to attempt automatic fixes.
```

**Examples:**
```bash
# Diagnose only
syrvis doctor

# Diagnose and fix
syrvis doctor --fix
```

---

### syrvis config show

Display current configuration.

```bash
syrvis config show
```

**Output:**
```
SyrvisCore Configuration
========================
Domain:      example.com
ACME Email:  admin@example.com
Traefik IP:  192.168.1.100
Subnet:      192.168.1.0/24
Gateway:     192.168.1.1
Cloudflare:  disabled
```

---

### syrvis compose generate

Generate or regenerate docker-compose.yaml.

```bash
syrvis compose generate
```

**What it does:**
1. Reads `.env` configuration
2. Reads Docker image versions from manifest
3. Generates `docker-compose.yaml`

**Notes:**
- Safe to re-run after configuration changes
- Backup is created before overwriting

---

### syrvis --version

Display version information.

```bash
syrvis --version
```

**Output:**
```
syrvis, version 0.2.0
```

---

### syrvis --help

Display help information.

```bash
syrvis --help
syrvis status --help
```

## Services

The following services are managed by `syrvis`:

| Service | Description | Required |
|---------|-------------|----------|
| `traefik` | Reverse proxy with SSL | Yes |
| `portainer` | Container management UI | Yes |
| `cloudflared` | Cloudflare Tunnel | No |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SYRVIS_HOME` | Service data directory |
| `DOCKER_HOST` | (Optional) Docker socket location |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Invalid arguments |
| 3 | Service not found |
| 4 | Docker error |
| 5 | Permission denied |
| 6 | Configuration error |

## Files

| File | Description |
|------|-------------|
| `$SYRVIS_HOME/config/.env` | Configuration file |
| `$SYRVIS_HOME/config/docker-compose.yaml` | Generated compose file |
| `$SYRVIS_HOME/config/traefik/` | Traefik configuration |
| `$SYRVIS_HOME/data/` | Persistent service data |
| `$SYRVIS_HOME/bin/syrvis` | Wrapper script |

## Configuration

Configuration is stored in `$SYRVIS_HOME/config/.env`:

```bash
# Domain configuration
DOMAIN=example.com
ACME_EMAIL=admin@example.com

# Network configuration
NETWORK_INTERFACE=ovs_eth0
NETWORK_SUBNET=192.168.1.0/24
GATEWAY_IP=192.168.1.1
TRAEFIK_IP=192.168.1.100

# Optional: Cloudflare
CLOUDFLARE_TUNNEL_TOKEN=
```

## See Also

- [syrvisctl CLI Reference](cli-syrvisctl.md) - Manager CLI documentation
- [SPK Installation Guide](spk-installation-guide.md) - Installation instructions
- [Design Document](design-doc.md) - Architecture overview
