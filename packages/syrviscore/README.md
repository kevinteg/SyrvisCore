# SyrvisCore

Self-hosted infrastructure platform for Synology NAS.

## Overview

SyrvisCore provides a reverse proxy (Traefik), container management (Portainer), and secure remote access (Cloudflared) for your Synology NAS.

## Installation

SyrvisCore is installed and managed via `syrvisctl` (the SyrvisCore Manager):

```bash
# Install via manager
syrvisctl install

# Run setup
syrvis setup

# Start services
syrvis start
```

## Commands

```bash
# Service management
syrvis start          # Start all services
syrvis stop           # Stop all services
syrvis restart        # Restart all services
syrvis status         # Show service status
syrvis logs           # View logs
syrvis logs traefik   # View logs for specific service

# Configuration
syrvis setup          # Interactive setup
syrvis config show    # Show current configuration
syrvis doctor         # Check for issues

# Docker compose
syrvis compose generate  # Generate docker-compose.yaml
```

## Services

- **Traefik**: Reverse proxy with automatic HTTPS
- **Portainer**: Container management UI
- **Cloudflared**: Secure tunnel for remote access

## Configuration

Configuration is stored in `/volumeX/syrviscore/config/.env`.

Key settings:
- `DOMAIN`: Your domain name
- `ACME_EMAIL`: Email for Let's Encrypt
- `TRAEFIK_IP`: Dedicated IP for Traefik (macvlan)
- `NETWORK_INTERFACE`: Network interface (eth0, bond0, etc.)

## Directory Structure

```
/volumeX/syrviscore/
├── current -> versions/0.2.0      # Active version
├── versions/                      # Installed versions
│   └── 0.2.0/
│       ├── cli/venv/              # Python virtual environment
│       └── build/config.yaml      # Docker image versions
├── config/
│   ├── .env                       # Configuration
│   └── docker-compose.yaml        # Generated compose file
└── data/
    ├── traefik/                   # Traefik data & certs
    ├── portainer/                 # Portainer data
    └── cloudflared/               # Cloudflare tunnel config
```
