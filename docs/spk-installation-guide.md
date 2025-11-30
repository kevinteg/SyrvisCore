# SyrvisCore SPK Installation Guide

This guide walks you through installing, using, and troubleshooting the SyrvisCore SPK package on your Synology NAS.

## Prerequisites

### System Requirements
- **Synology DSM**: Version 7.0 or later
- **Docker Package**: Must be installed and running
- **Architecture**: Any (noarch package)
- **Volume**: At least 1GB free space for installation and Docker images

### Pre-Installation Checklist
1. ✅ Docker is installed from Package Center
2. ✅ Docker service is running
3. ✅ You have admin access to DSM
4. ✅ You know your network configuration (IP addresses, subnet)
5. ✅ You have a domain name configured (for SSL certificates)

## Installation

### Method 1: Package Center (Recommended for Production)

1. **Download the SPK file**
   - Get the latest release from: https://github.com/kevinteg/SyrvisCore/releases
   - Or build it yourself (see Building section below)

2. **Open Package Center**
   - Log into DSM
   - Open Package Center

3. **Manual Install**
   - Click "Manual Install" button
   - Browse and select the `.spk` file
   - Click "Next"

4. **Installation Wizard**

   **Step 1: Basic Configuration**
   - Select the volume where SyrvisCore will be installed
   - Typically `/volume1` or `/volume5`

   **Step 2: Network Configuration**
   - Network Interface: `ovs_eth0` (default for Synology)
   - Network Subnet: Your local subnet e.g., `192.168.8.0/24`
   - Gateway IP: Your router's IP e.g., `192.168.8.1`
   - Traefik IP: A free IP in your subnet e.g., `192.168.8.4`

   **Step 3: Domain and SSL**
   - Domain Name: Your domain e.g., `example.com`
   - ACME Email: Your email for Let's Encrypt

   **Step 4: Optional - Cloudflare Tunnel**
   - Enter Cloudflare tunnel token if using
   - Leave blank to skip

5. **Complete Installation**
   - Click "Apply"
   - Installation will take 1-2 minutes
   - Package will appear in Package Center

### Method 2: SSH Installation (Advanced)

```bash
# Upload SPK to your NAS (via SFTP or similar)
# Then install via SSH:
sudo synopkg install /path/to/syrviscore-0.1.0-dev-noarch.spk

# Check status
sudo synopkg status syrviscore
```

## Post-Installation Setup

### 1. Verify Installation

```bash
# SSH into your Synology
ssh admin@192.168.8.3

# Check CLI is available
which syrvis
# Expected: /usr/local/bin/syrvis

# Check version
syrvis --version
# Expected: syrviscore, version 0.1.0-dev
```

### 2. Verify Directory Structure

```bash
# Navigate to installation (adjust volume as needed)
cd /volume5/docker/syrviscore

# List contents
ls -la
# Expected:
# - cli/           (Python venv)
# - data/          (Persistent data)
# - .env           (Configuration)
# - .env.template  (Reference)
# - .syrviscore-manifest.json
# - README.md
```

### 3. Generate Docker Compose

```bash
cd /volume5/docker/syrviscore

# Generate docker-compose.yaml
syrvis compose generate

# Verify file was created
ls -l docker-compose.yaml
```

### 4. Start Services

```bash
# Start all services
syrvis start

# Check status
syrvis status

# Expected output:
# ✓ traefik     running
# ✓ portainer   running
# ✓ cloudflared running  (if configured)
```

### 5. Access Services

- **Traefik Dashboard**: https://traefik.yourdomain.com
- **Portainer**: https://portainer.yourdomain.com

Initial setup:
1. Traefik dashboard may require basic auth (configure in Traefik)
2. Portainer will ask you to create admin account on first access

## Verifying Network Configuration

### Check macvlan Network

```bash
# List Docker networks
docker network ls | grep syrviscore

# Inspect macvlan network
docker network inspect syrviscore-network

# Should show:
# - Driver: macvlan
# - Subnet: Your configured subnet
# - Gateway: Your configured gateway
# - Parent interface: ovs_eth0
```

### Check Traefik IP

```bash
# Check Traefik container
docker inspect traefik | grep IPAddress

# Should show your configured Traefik IP (e.g., 192.168.8.4)
```

### Test from Another Device

```bash
# From another computer on your network:
ping 192.168.8.4

# Try accessing Traefik dashboard:
curl -k https://192.168.8.4
# Should get HTML response
```

## Common Commands

### Service Management

```bash
# Start all services
syrvis start

# Stop all services
syrvis stop

# Restart all services
syrvis restart

# Check status
syrvis status
```

### View Logs

```bash
# View all logs
syrvis logs

# View specific service logs
syrvis logs traefik
syrvis logs portainer
syrvis logs cloudflared

# Follow logs in real-time
syrvis logs -f traefik
```

### Configuration

```bash
# Regenerate docker-compose.yaml
syrvis compose generate

# View current configuration
cat /volume5/docker/syrviscore/.env

# Edit configuration (requires sudo)
sudo nano /volume5/docker/syrviscore/.env

# After editing, regenerate and restart:
syrvis compose generate
syrvis restart
```

## Troubleshooting

### Installation Issues

**Issue**: "Docker is not installed or not running"
```bash
# Check Docker status
synopkg status Docker

# Start Docker if needed
sudo synopkg start Docker
```

**Issue**: "Cannot find installation directory"
```bash
# Check which volumes exist
ls -la /volume*

# Installation should be at:
# /volume{X}/docker/syrviscore/
```

### Network Issues

**Issue**: "Cannot ping Traefik IP"

1. Check IP is in correct subnet
2. Check no IP conflict
3. Verify network interface exists:
   ```bash
   ip link show ovs_eth0
   ```

**Issue**: "Port 80/443 already in use"

This shouldn't happen with macvlan, but if it does:
```bash
# Check what's using port 80
sudo netstat -tulpn | grep :80

# SyrvisCore uses macvlan to avoid port conflicts
```

### Service Issues

**Issue**: "Services won't start"

```bash
# Check Docker is running
docker ps

# Check syrviscore containers
docker ps -a | grep syrviscore

# View container logs
docker logs traefik
docker logs portainer

# Check for errors in installation log
cat /tmp/syrviscore_install.log
```

**Issue**: "SSL certificates not working"

1. Check domain DNS points to Traefik IP
2. Verify ports 80/443 accessible from internet
3. Check acme.json permissions:
   ```bash
   ls -l /volume5/docker/syrviscore/data/traefik/acme.json
   # Should be: -rw------- 1 syrvis-bot users
   ```

### Permission Issues

**Issue**: "Permission denied errors"

```bash
# Check syrvis-bot user exists
id syrvis-bot

# Check group memberships
groups syrvis-bot
# Should include: docker, administrators

# Check Docker socket permissions
ls -l /var/run/docker.sock
# Should be: srw-rw---- 1 root docker

# Reset permissions (run as root)
sudo /usr/local/etc/rc.d/S99syrviscore-docker.sh start
```

## Upgrading

### Via Package Center

1. Download new SPK version
2. Package Center → Manual Install
3. Select new SPK file
4. Confirm upgrade
5. Your data and configuration are preserved

### Via SSH

```bash
sudo synopkg install /path/to/new-version.spk
```

### Post-Upgrade

```bash
# Check new version
syrvis --version

# Regenerate docker-compose.yaml if needed
cd /volume5/docker/syrviscore
syrvis compose generate

# Restart services
syrvis restart
```

## Uninstalling

### Via Package Center

1. Package Center → Installed
2. Find SyrvisCore
3. Click Uninstall
4. **Important**: By default, your data is preserved

### Complete Removal

If you want to remove all data:

```bash
# After uninstalling via Package Center:
sudo rm -rf /volume5/docker/syrviscore
```

### Reinstalling

If you uninstalled and kept data:
1. Install SPK normally
2. Installation will detect existing data
3. CLI will be reinstalled, data will be preserved

## Logs and Debugging

### Installation Logs

```bash
# Installation log
cat /tmp/syrviscore_install.log

# Startup script log
cat /tmp/syrviscore_startup.log

# Uninstallation log
cat /tmp/syrviscore_uninstall.log
```

### Service Logs

```bash
# Via syrvis CLI
syrvis logs traefik
syrvis logs portainer

# Direct Docker logs
docker logs traefik
docker logs portainer
```

### System Status

```bash
# Check installation manifest
cat /volume5/docker/syrviscore/.syrviscore-manifest.json

# Check Docker networks
docker network ls

# Check running containers
docker ps
```

## Getting Help

### Documentation
- Main docs: https://github.com/kevinteg/SyrvisCore
- Design doc: `docs/design-doc.md`
- This guide: `docs/spk-installation-guide.md`

### Support Channels
- GitHub Issues: https://github.com/kevinteg/SyrvisCore/issues
- GitHub Discussions: https://github.com/kevinteg/SyrvisCore/discussions

### Before Asking for Help

Please include:
1. DSM version: `cat /etc/VERSION`
2. SyrvisCore version: `syrvis --version`
3. Docker version: `docker --version`
4. Installation log: `/tmp/syrviscore_install.log`
5. Service logs: `syrvis logs`
6. Network configuration: `docker network inspect syrviscore-network`

## Advanced Topics

### Custom Configuration

Edit `.env` file for advanced configuration:
```bash
sudo nano /volume5/docker/syrviscore/.env
```

Common customizations:
- `TRAEFIK_LOG_LEVEL`: Change logging verbosity
- `TRAEFIK_API_DASHBOARD`: Enable/disable dashboard
- `PORTAINER_BIND_PORT`: Change Portainer port

After changes:
```bash
syrvis compose generate
syrvis restart
```

### Backup and Restore

**Backup**:
```bash
cd /volume5/docker
tar -czf syrviscore-backup-$(date +%Y%m%d).tar.gz \
    syrviscore/data/ \
    syrviscore/.env \
    syrviscore/.syrviscore-manifest.json
```

**Restore**:
```bash
# On new system, install SyrvisCore first
# Then restore data:
cd /volume5/docker/syrviscore
sudo tar -xzf /path/to/backup.tar.gz --strip-components=1
sudo chown -R syrvis-bot:users data/
```

### Multiple Installations

Not recommended, but possible on different volumes:
- Install on `/volume1` with one configuration
- Install on `/volume2` with different configuration
- Each has its own Traefik IP

## Security Considerations

1. **acme.json**: Contains SSL certificates
   - Must be 600 permissions
   - Never commit to git
   - Backup securely

2. **.env file**: Contains sensitive configuration
   - Never commit to git
   - Restrict access: `chmod 600 .env`

3. **syrvis-bot user**: Created automatically
   - Member of docker and administrators groups
   - Runs all containers
   - Do not modify manually

4. **Docker socket**: Shared with containers
   - Permissions reset on each boot
   - Managed by startup script

## Best Practices

1. **Use domain names**: Don't rely on IP addresses
2. **Enable HTTPS**: Always use SSL for production
3. **Regular backups**: Backup data directory weekly
4. **Monitor logs**: Check logs regularly for issues
5. **Keep updated**: Update to latest version for security fixes
6. **Network isolation**: Use macvlan to avoid port conflicts
7. **Strong passwords**: Use strong passwords for Portainer

## FAQ

**Q: Can I run SyrvisCore on multiple Synology devices?**
A: Yes, each device is independent.

**Q: Can I change the Traefik IP after installation?**
A: Edit `.env`, regenerate compose, restart services.

**Q: Do I need to open ports on my firewall?**
A: Yes, forward 80/443 to Traefik IP for SSL to work.

**Q: Can I use SyrvisCore with DSM's built-in reverse proxy?**
A: No, SyrvisCore provides its own reverse proxy (Traefik).

**Q: What if I already have Traefik installed?**
A: Uninstall other Traefik first or use different IPs.

**Q: Can I add my own Docker containers?**
A: Yes, through Portainer or by editing docker-compose.yaml.

**Q: Where are SSL certificates stored?**
A: `data/traefik/acme.json` - backup this file!
