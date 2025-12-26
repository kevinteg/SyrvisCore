# SyrvisCore

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Self-hosted infrastructure platform for Synology NAS**

SyrvisCore provides a complete reverse proxy and container management platform for Synology NAS devices:

- **Traefik** - Reverse proxy with SSL termination
- **Portainer** - Container management UI
- **Cloudflared** - Cloudflare Tunnel for external access (optional)

## Architecture

SyrvisCore uses a split-package architecture:

| Component | CLI | Purpose |
|-----------|-----|---------|
| Manager (`syrviscore-manager`) | `syrvisctl` | Version management, installs from SPK |
| Service (`syrviscore`) | `syrvis` | Docker services, installed via syrvisctl |

## Requirements

- **Synology NAS**: DSM 7.0 or later
- **Docker**: Installed from Package Center
- **Python 3.8+**: For development

## Installation

### For Synology (Production)

1. Download the latest SPK from [Releases](https://github.com/kevinteg/SyrvisCore/releases)
2. Install via Package Center → Manual Install
3. Follow the installation wizard
4. Run the service installer:
   ```bash
   syrvisctl install
   ```
5. Complete setup:
   ```bash
   syrvis setup
   syrvis start
   ```

See [SPK Installation Guide](docs/spk-installation-guide.md) for detailed instructions.

### For Development

```bash
# Clone repository
git clone git@github.com:kevinteg/SyrvisCore.git
cd SyrvisCore

# Install Python 3.8.12 via pyenv (matches Synology NAS)
pyenv install 3.8.12
pyenv virtualenv 3.8.12 syrviscore
pyenv activate syrviscore

# Install both packages in development mode
pip install -e "packages/syrviscore-manager[dev]"
pip install -e "packages/syrviscore[dev]"

# Verify installation
syrvisctl --version
syrvis --version
```

## CLI Commands

### syrvisctl (Manager)

```bash
syrvisctl install [version]   # Install service from GitHub
syrvisctl list                # List installed versions
syrvisctl activate <version>  # Switch active version
syrvisctl rollback            # Rollback to previous version
syrvisctl check               # Check for updates
syrvisctl info                # Show installation info
syrvisctl cleanup [--keep N]  # Remove old versions
```

### syrvis (Service)

```bash
syrvis setup                  # Interactive configuration
syrvis status                 # Show service status
syrvis start                  # Start all services
syrvis stop                   # Stop all services
syrvis restart                # Restart services
syrvis logs [service] [-f]    # View logs
syrvis doctor [--fix]         # Diagnose issues
```

## Project Structure

```
SyrvisCore/
├── packages/
│   ├── syrviscore-manager/    # Manager package (SPK)
│   │   ├── pyproject.toml
│   │   └── src/syrviscore_manager/
│   └── syrviscore/            # Service package
│       ├── pyproject.toml
│       └── src/syrviscore/
├── spk/                       # SPK packaging
├── build-tools/               # Build scripts
├── tests/                     # Test suite
│   └── dsm-sim/               # DSM simulation environment
├── docs/                      # Documentation
└── build/config.yaml          # Docker image versions
```

## Development

### Building Packages

```bash
# Build manager wheel (includes dependency download)
./build-tools/build-manager.sh

# Build service wheel
./build-tools/build-service.sh

# Build complete SPK (bundles all dependencies)
./build-tools/build-spk.sh
```

### Testing with DSM Simulation

The project includes a DSM simulation environment for local testing:

```bash
# Initialize simulation
make sim-setup

# Run full workflow test
make test-sim

# Activate simulation for interactive testing
source tests/dsm-sim/activate.sh

# Reset simulation
make sim-reset
```

See [Development Guide](docs/dev-guide.md) for details.

### Running Tests

```bash
# Run all tests
make test

# Run with coverage
make test-cov
```

### Code Quality

```bash
make lint        # Run ruff linter
make format      # Format with black
make check       # lint + test
```

## Documentation

- [Design Document](docs/design-doc.md) - Architecture overview
- [SPK Installation Guide](docs/spk-installation-guide.md) - User installation guide
- [CLI Reference - syrvisctl](docs/cli-syrvisctl.md) - Manager CLI docs
- [CLI Reference - syrvis](docs/cli-syrvis.md) - Service CLI docs
- [Development Guide](docs/dev-guide.md) - Local development setup
- [Build Tools](build-tools/README.md) - Build system documentation

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Set up development environment (see above)
4. Make your changes
5. Run tests and linters (`make check`)
6. Commit (`git commit -m 'Add amazing feature'`)
7. Push (`git push origin feature/amazing-feature`)
8. Open a Pull Request

### Code Standards

- Python 3.8+ compatibility
- Black formatting (line length 100)
- Ruff linting
- Pytest for testing
- Google-style docstrings

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Links

- **Issues**: [GitHub Issues](https://github.com/kevinteg/SyrvisCore/issues)
- **Discussions**: [GitHub Discussions](https://github.com/kevinteg/SyrvisCore/discussions)
