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

Current version: `0.1.0-dev`

## Requirements

- Synology NAS running DSM 7.0+
- Docker package installed on Synology
- Python 3.8+ (for development)

## Installation

### For Synology (Production)

Coming soon - SPK package not yet ready for installation.

When available, you'll be able to add the SynoCommunity package source and install via Package Center.

### For Development

```bash
# Clone repository
git clone git@github.com:kevinteg/SyrvisCore.git
cd SyrvisCore

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode with all dependencies
pip install -e ".[dev]"

# Verify installation
syrvis --version
syrvis hello
```

## Development Workflow

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=syrviscore

# Run specific test file
pytest tests/test_cli.py -v

# Use tox for multi-version testing
tox
```

### Code Quality

```bash
# Format code with Black
black src/ tests/ build-tools/

# Check formatting
black --check src/ tests/ build-tools/

# Lint with Ruff
ruff check src/ tests/ build-tools/

# Auto-fix linting issues
ruff check --fix src/ tests/ build-tools/

# Run all quality checks
tox -e lint
```

### Build Tools

```bash
# Select Docker image versions interactively
./build-tools/select-docker-versions

# Or use non-interactive mode for CI/CD
./build-tools/select-docker-versions --non-interactive

# Build SPK (coming soon)
./build-tools/build-spk
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
3. **Set up development environment** (`pip install -e ".[dev]"`)
4. **Make your changes**
5. **Run tests and linters** (`pytest && black --check . && ruff check .`)
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
