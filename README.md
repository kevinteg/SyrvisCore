# SyrvisCore

**Self-hosted infrastructure platform for Synology NAS**

SyrvisCore provides a complete reverse proxy and container management platform for Synology NAS devices, featuring:

- **Traefik** - Reverse proxy with SSL termination
- **Portainer** - Container management UI
- **Cloudflared** - Cloudflare Tunnel for external access (optional)

## Status

🚧 **In Development** - MVP Phase

Current version: `0.1.0-dev`

## Quick Start

Coming soon - package not yet ready for installation.

## Development

### Local Setup
```bash
# Clone repository
git clone git@github.com:kevinteg/SyrvisCore.git
cd SyrvisCore

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install in development mode
pip install -e .

# Test CLI
syrvis hello
```

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Project Structure
SyrvisCore/
├── src/syrviscore/     # Main Python package
├── build-tools/        # SPK building utilities
├── tests/              # Test suite
└── docs/               # Documentation
## Roadmap

- [ ] Phase 1: MVP - Basic CLI and build system
- [ ] Phase 2: Version management
- [ ] Phase 3: Stack management
- [ ] Phase 4: Community release

See [Design Document](docs/design-doc.md) for full details.
