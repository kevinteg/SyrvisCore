# SyrvisCore

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Self-hosted infrastructure platform for Synology NAS**

SyrvisCore provides a complete reverse proxy and container management platform for Synology NAS devices, featuring:

- **Traefik** - Reverse proxy with SSL termination
- **Portainer** - Container management UI  
- **Cloudflared** - Cloudflare Tunnel for external access (optional)

## Status

🚧 **In Development** - MVP Phase

Current version: `0.0.1`

## Requirements

- Synology NAS running DSM 7.0+
- Docker package installed on Synology
- Python 3.8+ (for development)

## Installation

### For Synology (Production)

Download the latest SPK package from [Releases](https://github.com/kevinteg/SyrvisCore/releases) and install via Package Center.

**Quick Install:**
1. Download `syrviscore-{version}-noarch.spk`
2. Open Package Center on your Synology
3. Click "Manual Install"
4. Select the SPK file and follow the wizard

See [Installation Guide](docs/spk-installation-guide.md) for detailed instructions.

### For Development

```bash
# Clone repository
git clone git@github.com:kevinteg/SyrvisCore.git
cd SyrvisCore

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode with all dependencies
make dev-install

# Verify installation
syrvis --version
syrvis hello
```

## Development Workflow

### Quick Start with Makefile

The project includes a comprehensive Makefile for common development tasks:

```bash
# See all available commands
make help

# Development setup
make dev-install      # Install package with dev dependencies

# Code quality
make lint             # Run ruff linter
make format           # Format code with black
make format-check     # Check formatting without changes

# Testing
make test             # Run all tests
make test-cov         # Run tests with coverage report

# Building
make clean            # Remove build artifacts
make build-wheel      # Build Python wheel
make build-spk        # Build complete SPK package
make validate         # Validate SPK structure

# Complete workflow
make all              # lint + test + build-spk
```

### Running Tests

```bash
# Using Makefile (recommended)
make test             # Run all tests
make test-cov         # Run with coverage

# Using pytest directly
pytest
pytest --cov=syrviscore
pytest tests/test_cli.py -v

# Multi-version testing
tox
```

### Code Quality

```bash
# Using Makefile (recommended)
make lint             # Lint with ruff
make format           # Format with black
make format-check     # Check formatting
make check            # Run lint + test

# Using tools directly
black src/ tests/
ruff check src/ tests/
```

### Building Packages

**Using Makefile (Recommended):**
```bash
# Build complete SPK package (includes wheel)
make build-spk

# Build just the Python wheel
make build-wheel

# Clean + Lint + Test + Build
make all

# Validate SPK package
make validate
```

**Using build tools directly:**
```bash
# Build Python wheel
./build-tools/build-python-package.sh

# Build SPK package
./build-tools/build-spk.sh

# Validate SPK
./build-tools/validate-spk.sh dist/syrviscore-{version}-noarch.spk
```

**Build output:**
- `dist/syrviscore-{version}-py3-none-any.whl` - Python wheel
- `dist/syrviscore-{version}.tar.gz` - Source distribution
- `dist/syrviscore-{version}-noarch.spk` - Synology package

The build process uses:
- **Standard Python packaging** - `python -m build` creates wheel
- **pip installation** - SPK installer uses `pip install wheel`
- **No custom packaging** - Follows PEP 517/518 standards

### Deployment to Synology

```bash
# Install SPK via SSH (requires SSH access)
make install SSH_HOST=192.168.0.100

# With custom SSH user
make install SSH_HOST=192.168.0.100 SSH_USER=admin

# Uninstall from Synology
make uninstall SSH_HOST=192.168.0.100
```

See [build-tools/README.md](build-tools/README.md) for detailed documentation.

## Project Structure

```
SyrvisCore/
├── src/syrviscore/     # Main Python package
│   ├── __init__.py
│   ├── __version__.py  # Version info
│   └── cli.py          # CLI commands
├── build-tools/        # SPK building utilities
│   └── select-docker-versions.py
├── build/              # Build configuration (versioned)
│   └── config.yaml     # Docker image versions
├── tests/              # Test suite
├── docs/               # Documentation
├── .github/            # GitHub Actions workflows
├── .vscode/            # VS Code configuration
├── pyproject.toml      # Package configuration & dependencies
├── tox.ini             # Multi-environment testing config
└── README.md           # This file
```

## Contributing

Contributions are welcome! This project follows modern Python packaging best practices:

1. **Fork the repository**
2. **Create a feature branch** (`git checkout -b feature/amazing-feature`)
3. **Set up development environment** (`make dev-install`)
4. **Make your changes**
5. **Run tests and linters** (`make check`)
6. **Commit your changes** (`git commit -m 'Add amazing feature'`)
7. **Push to the branch** (`git push origin feature/amazing-feature`)
8. **Open a Pull Request**

### Code Standards

- Python 3.8+ compatibility
- Type hints encouraged
- Black formatting (line length 100)
- Ruff linting
- Pytest for testing
- Docstrings for public functions (Google style)

## Roadmap

- [x] Phase 1a: Project structure and build tools
- [ ] Phase 1b: Basic CLI commands (status, logs, config)
- [ ] Phase 1c: SPK package building
- [ ] Phase 2: Installation and upgrade system
- [ ] Phase 3: Stack management
- [ ] Phase 4: Web UI
- [ ] Phase 5: Community release

See [Design Document](docs/design-doc.md) for full details.

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Links

- **Documentation:** [docs/](docs/)
- **Issues:** [GitHub Issues](https://github.com/kevinteg/SyrvisCore/issues)
- **Design Doc:** [docs/design-doc.md](docs/design-doc.md)
- **Build Tools:** [build-tools/README.md](build-tools/README.md)
