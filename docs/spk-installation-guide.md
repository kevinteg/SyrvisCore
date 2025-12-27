# SyrvisCore SPK Installation Guide

This guide walks you through installing and setting up SyrvisCore on your Synology NAS.

## Prerequisites

### System Requirements

- **Synology DSM**: Version 7.0 or later
- **Docker Package**: Installed and running from Package Center
- **Architecture**: Any (noarch package)
- **Storage**: At least 1GB free space for installation and Docker images

### Pre-Installation Checklist

1. Docker is installed from Package Center
2. Docker service is running
3. You have admin access to DSM
4. You know your network configuration (IP addresses, subnet)
5. You have a domain name configured (for SSL certificates)

## Installation

### Step 1: Download SPK

Download the latest SPK from [GitHub Releases](https://github.com/kevinteg/SyrvisCore/releases):

```
syrviscore-{version}-noarch.spk
```

### Step 2: Install via Package Center

1. Log into DSM
2. Open **Package Center**
3. Click **Manual Install**
4. Browse and select the SPK file
5. Click **Next**

### Step 3: Installation Wizard

The wizard collects configuration for the installation:

**Volume Selection**
- Select the volume where SyrvisCore data will be stored
- Typically `/volume1` or another data volume

**Network Configuration**
- Network Interface: `ovs_eth0` (default for Synology)
- Network Subnet: Your local subnet (e.g., `192.168.1.0/24`)
- Gateway IP: Your router's IP (e.g., `192.168.1.1`)
- Traefik IP: A dedicated IP for Traefik (e.g., `192.168.1.100`)

**Domain and SSL**
- Domain Name: Your domain (e.g., `example.com`)
- ACME Email: Email for Let's Encrypt notifications

**Cloudflare Tunnel (Optional)**
- Enter Cloudflare tunnel token if using
- Leave blank to skip

### Step 4: Complete Installation

1. Click **Apply**
2. Installation takes 1-2 minutes
3. Package appears in Package Center as installed

At this point, only the **manager CLI** (`syrvisctl`) is installed.

## Post-Installation Setup

### Step 5: Install Service Package

SSH into your Synology and install the service package:

```bash
ssh admin@192.168.1.x

# Install service package from GitHub
syrvisctl install

# Check installation
syrvisctl info
syrvisctl list
```

### Step 6: Configure and Start Services

```bash
# Run interactive setup (handles Docker permissions, config generation)
syrvis setup

# Start services
syrvis start

# Verify services are running
syrvis status
```

### Step 7: Verify Installation

```bash
# Check directory structure
ls -la /volumeX/syrviscore/

# Expected:
# current -> versions/0.x.x/   (symlink to active version)
# versions/                     (service versions)
# config/                       (configuration files)
# data/                         (persistent data)
# bin/                          (wrapper scripts)
# .syrviscore-manifest.json

# Check services
syrvis status
# Expected:
# traefik:     running
# portainer:   running
# cloudflared: running (if configured)
```

## Accessing Services

Once running, access your services:

- **Traefik Dashboard**: https://traefik.yourdomain.com
- **Portainer**: https://portainer.yourdomain.com

**First-time setup:**
- Portainer will ask you to create an admin account on first access

## Managing Versions

### Check for Updates

```bash
syrvisctl check
```

### Install New Version

```bash
syrvisctl install 0.2.0
```

### Rollback

```bash
syrvisctl rollback
```

### List Installed Versions

```bash
syrvisctl list
```

## Service Management

### Start/Stop Services

```bash
syrvis start
syrvis stop
syrvis restart
```

### View Logs

```bash
# All logs
syrvis logs

# Specific service
syrvis logs traefik
syrvis logs portainer

# Follow logs
syrvis logs -f traefik
```

### Diagnose Issues

```bash
syrvis doctor

# Auto-fix common issues
syrvis doctor --fix
```

## Troubleshooting

For detailed troubleshooting, see [SPK Troubleshooting Guide](spk-troubleshooting.md).

### Log Files

Installation creates detailed logs:
```bash
# View main installation log
cat /tmp/syrviscore-install.log

# View pip installation log
cat /tmp/syrviscore-pip.log
```

### SPK Installation Issues

**"Docker is not installed or not running"**
```bash
# Check Docker status
synopkg status Docker

# Start Docker
sudo synopkg start Docker
```

**"Cannot find installation directory"**
```bash
# Check which volumes exist
ls -la /volume*
```

### Service Issues

**"Services won't start"**
```bash
# Check Docker
docker ps

# Check container logs
docker logs traefik
docker logs portainer

# Run diagnostics
syrvis doctor
```

**"Permission denied"**
```bash
# Re-run setup to fix permissions
syrvis setup

# Or manually check Docker group
groups $(whoami)
```

### Network Issues

**"Cannot reach Traefik IP"**
1. Verify IP is in correct subnet
2. Check for IP conflicts
3. Verify network interface:
   ```bash
   ip link show ovs_eth0
   ```

## Upgrading

### Via Package Center

1. Download new SPK version
2. Package Center → Manual Install
3. Select new SPK file
4. Confirm upgrade

The manager is updated. Then update the service:

```bash
syrvisctl install
```

### Via Command Line

```bash
# Check for updates
syrvisctl check

# Download and install update
syrvisctl install [version]
```

## Uninstalling

### Via Package Center

1. Package Center → Installed
2. Find SyrvisCore
3. Click Uninstall

**Note:** Data is preserved by default in `/volumeX/syrviscore/`

### Complete Removal

```bash
# After uninstalling SPK
sudo rm -rf /volumeX/syrviscore
```

## Backup and Restore

### Backup

```bash
cd /volumeX/syrviscore
tar -czf ~/syrviscore-backup-$(date +%Y%m%d).tar.gz \
    config/ \
    data/ \
    .syrviscore-manifest.json
```

### Restore

```bash
# Install SyrvisCore first, then restore data
cd /volumeX/syrviscore
sudo tar -xzf /path/to/backup.tar.gz
```

## Security Notes

1. **Configuration files** (`.env`) contain sensitive data - never commit to git
2. **ACME certificates** must have `0600` permissions
3. **Docker socket** access is managed by the `docker` group
4. Use Synology's encrypted shared folder for `/volume1/secrets/`

## Getting Help

- **Documentation**: [docs/](https://github.com/kevinteg/SyrvisCore/tree/main/docs)
- **Issues**: [GitHub Issues](https://github.com/kevinteg/SyrvisCore/issues)
- **CLI Help**: `syrvisctl --help` / `syrvis --help`

### Before Asking for Help

Include:
1. DSM version: `cat /etc/VERSION`
2. SyrvisCore version: `syrvisctl info`
3. Docker version: `docker --version`
4. Service status: `syrvis status`
5. Doctor output: `syrvis doctor`
