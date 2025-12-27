# SyrvisCore Development Guide

This guide covers setting up a local development environment for SyrvisCore, including the DSM simulation environment for testing without a Synology NAS.

## Prerequisites

- **macOS or Linux** (Windows with WSL2 works)
- **Python 3.8.12** (matches Synology DSM)
- **pyenv** and **pyenv-virtualenv** for Python version management
- **Docker Desktop** (optional, for testing Docker operations)

## Quick Start

```bash
# Clone repository
git clone git@github.com:kevinteg/SyrvisCore.git
cd SyrvisCore

# Install Python 3.8.12 via pyenv
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

## Project Structure

```
SyrvisCore/
├── packages/
│   ├── syrviscore-manager/           # Manager package
│   │   ├── pyproject.toml
│   │   └── src/syrviscore_manager/
│   │       ├── cli.py                # syrvisctl entry point
│   │       ├── version_manager.py    # Install/activate/rollback
│   │       ├── downloader.py         # GitHub release downloads
│   │       └── manifest.py           # Manifest management
│   │
│   └── syrviscore/                   # Service package
│       ├── pyproject.toml
│       └── src/syrviscore/
│           ├── cli.py                # syrvis entry point
│           ├── setup.py              # Interactive setup
│           ├── docker_manager.py     # Container management
│           ├── privileged_ops.py     # Privileged operations
│           └── paths.py              # Path utilities
│
├── spk/                              # SPK packaging
│   ├── INFO                          # Package metadata
│   ├── scripts/                      # Lifecycle scripts
│   │   ├── preinst, postinst
│   │   ├── preuninst, postuninst
│   │   └── start-stop-status
│   └── package/                      # Package contents template
│
├── build-tools/                      # Build scripts
│   ├── build-manager.sh              # Build manager wheel + deps
│   ├── build-service.sh              # Build service wheel
│   └── build-spk.sh                  # Build SPK package
│
├── tests/                            # Test suite
│   ├── dsm-sim/                      # DSM simulation
│   │   ├── bin/                      # Mock commands
│   │   ├── setup-sim.sh
│   │   ├── activate.sh
│   │   ├── deactivate.sh
│   │   └── reset-sim.sh
│   ├── dsm_sim.py                    # Python simulation helper
│   └── test_*.py                     # Test files
│
├── build/config.yaml                 # Docker image versions
├── Makefile                          # Build automation
└── CLAUDE.md                         # AI assistant context
```

## Development Workflow

### Code Style

```bash
# Format code
make format

# Check formatting
make format-check

# Lint code
make lint

# Run all checks
make check
```

### Running Tests

```bash
# Run all tests
make test

# Run with coverage
make test-cov

# Run specific test
pytest tests/test_cli.py -v
```

### Building Packages

```bash
# Build manager wheel (includes dependency download)
./build-tools/build-manager.sh

# Build service wheel
./build-tools/build-service.sh

# Build complete SPK
./build-tools/build-spk.sh
# OR
make build-spk
```

## DSM Simulation Environment

The DSM simulation provides a mock Synology environment on macOS/Linux for testing without a physical NAS.

### Simulation Architecture

```
tests/dsm-sim/
├── bin/                      # Mock Synology commands
│   ├── synopkg               # Package manager mock
│   └── synogroup             # Group manager mock
├── root/                     # Simulated filesystem
│   ├── var/packages/syrviscore/target/
│   ├── volume1/syrviscore/
│   └── usr/local/bin/
├── state/                    # Mock state files
│   ├── docker-status.txt
│   ├── installed-packages.json
│   └── docker-group-members.txt
├── logs/                     # Test logs
├── activate.sh               # Enter simulation
├── deactivate.sh             # Exit simulation
├── setup-sim.sh              # Initialize
└── reset-sim.sh              # Reset to clean
```

### Using the Simulation

```bash
# Initialize simulation
make sim-setup

# Activate simulation (sets environment variables)
source tests/dsm-sim/activate.sh

# Your shell now has:
# - PATH includes mock commands
# - SYNOPKG_PKGDEST points to simulation
# - SYRVIS_HOME points to simulation
# - DSM_SIM_ACTIVE=1

# Test mock commands
synopkg status Docker      # Returns "running"
synogroup --get docker     # Shows mock group

# Run workflow test
make test-sim

# Reset simulation
make sim-reset

# Deactivate
source tests/dsm-sim/deactivate.sh
```

### Simulation Features

**Mock Commands:**
- `synopkg` - Package status, install, uninstall
- `synogroup` - Group membership management

**State Management:**
- Docker status (running/stopped)
- Installed packages list
- Docker group members

**Real Docker:**
- Symlinks to actual Docker socket if available
- Full Docker operations work on macOS with Docker Desktop

### Python Simulation API

```python
from tests.dsm_sim import DsmSimulator

# Create simulator
sim = DsmSimulator()
sim.setup()

# Use as context manager
with sim.activated():
    # Environment variables are set
    # Paths point to simulation
    import subprocess
    subprocess.run(['syrvisctl', '--version'])

# Or use methods directly
sim.set_docker_status(True)  # Mock Docker as running
sim.add_group_member('admin', 'docker')

# Run script in simulation
result = sim.run_script(Path('spk/scripts/postinst'))
print(result.stdout)

# Reset to clean state
sim.reset()
```

### Integration Testing

```bash
# Run full workflow test
./tests/test_sim_workflow.sh

# This tests:
# 1. Simulation initialization
# 2. SPK extraction
# 3. postinst script execution
# 4. syrvisctl installation
# 5. Directory structure verification
```

## SPK Script Development

### Script Requirements

- Written in **POSIX shell (sh)**, not bash
- Must be executable (`chmod +x`)
- Must handle both fresh install and upgrade scenarios

### Testing SPK Scripts

```bash
# Build SPK
make build-spk

# Activate simulation
source tests/dsm-sim/activate.sh

# Extract and test manually
cd tests/dsm-sim/root/tmp
tar -xf ../../../../dist/syrviscore-*.spk
tar -xzf package.tgz -C ../var/packages/syrviscore/target/

# Run postinst
./scripts/postinst

# Verify
ls -la ../var/packages/syrviscore/target/
```

## Dependency Management

### Manager Package Dependencies

Edit `packages/syrviscore-manager/pyproject.toml`:

```toml
[project]
dependencies = [
    "click==8.1.7",
    "requests==2.31.0",
]
```

Dependencies are pinned for reproducibility and bundled in SPK.

### Service Package Dependencies

Edit `packages/syrviscore/pyproject.toml`:

```toml
[project]
dependencies = [
    "click==8.1.7",
    "pyyaml==6.0.2",
    "requests==2.31.0",
    "docker==7.1.0",
    "python-dotenv==1.0.1",
]
```

### Adding Dependencies

1. Add to appropriate `pyproject.toml` with pinned version
2. Rebuild wheel: `./build-tools/build-manager.sh`
3. Test with simulation
4. Update documentation if needed

## Debugging

### Logging

Set log level via environment:

```bash
export SYRVIS_LOG_LEVEL=DEBUG
syrvis status
```

### Common Issues

**Import errors:**
```bash
# Ensure packages installed in dev mode
pip install -e "packages/syrviscore-manager[dev]"
pip install -e "packages/syrviscore[dev]"
```

**Simulation not active:**
```bash
# Check environment
echo $DSM_SIM_ACTIVE  # Should be "1"

# Re-activate
source tests/dsm-sim/activate.sh
```

**SPK build fails:**
```bash
# Check wheel exists
ls dist/*.whl

# Rebuild wheel first
./build-tools/build-manager.sh
./build-tools/build-spk.sh
```

## Makefile Targets

```bash
make help              # Show all targets

# Development
make dev-install       # Install packages in dev mode

# Code quality
make lint              # Run linter
make format            # Format code
make check             # lint + test

# Testing
make test              # Run tests
make test-cov          # With coverage

# Building
make build-spk         # Build SPK package
make validate          # Validate SPK

# Simulation
make sim-setup         # Initialize simulation
make sim-reset         # Reset simulation
make sim-clean         # Remove simulation
make test-sim          # Run simulation workflow test
```

## Contributing

1. Create feature branch
2. Make changes
3. Run `make check` (lint + test)
4. Test with simulation if SPK-related
5. Submit PR

### Code Standards

- Python 3.8+ compatibility
- Black formatting (line length 100)
- Ruff linting
- Pytest for testing
- Google-style docstrings for public functions

## See Also

- [Design Document](design-doc.md) - Architecture overview
- [Build Tools](../build-tools/README.md) - Build system details
- [CLI Reference - syrvisctl](cli-syrvisctl.md)
- [CLI Reference - syrvis](cli-syrvis.md)
