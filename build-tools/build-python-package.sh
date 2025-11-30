#!/bin/bash
# build-python-package.sh - Build standard Python distribution packages
# Creates wheel and sdist using Python's official packaging tools

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

log_info "Building SyrvisCore Python Package"
log_info "Project root: $PROJECT_ROOT"

# Change to project root
cd "$PROJECT_ROOT"

# Get version from pyproject.toml or environment variable
if [ -n "$SYRVISCORE_VERSION" ]; then
    VERSION="$SYRVISCORE_VERSION"
    log_info "Using version from environment: $VERSION"
    
    # Update version in pyproject.toml
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/^version = .*/version = \"$VERSION\"/" pyproject.toml
    else
        sed -i "s/^version = .*/version = \"$VERSION\"/" pyproject.toml
    fi
    log_info "Updated pyproject.toml version to $VERSION"
else
    VERSION=$(grep '^version = ' pyproject.toml | cut -d'"' -f2)
    log_info "Using version from pyproject.toml: $VERSION"
fi

# Check if build package is installed
if ! python3 -c "import build" 2>/dev/null; then
    log_warn "build package not found, installing..."
    pip install build
fi

# Clean previous builds
log_info "Cleaning previous build artifacts"
rm -rf dist/ build/ *.egg-info src/*.egg-info

# Build using standard Python tooling
log_info "Building wheel and source distribution..."
python3 -m build

# Verify outputs exist
if [ ! -d "dist" ] || [ -z "$(ls -A dist/)" ]; then
    log_error "Build failed: dist/ directory is empty"
    exit 1
fi

# List built files
log_success "Build complete! Generated files:"
ls -lh dist/

# Verify wheel exists
WHEEL_FILE=$(ls dist/*.whl 2>/dev/null | head -1)
if [ -z "$WHEEL_FILE" ]; then
    log_error "No wheel file found in dist/"
    exit 1
fi

WHEEL_SIZE=$(du -h "$WHEEL_FILE" | cut -f1)
log_success "Wheel: $(basename "$WHEEL_FILE") ($WHEEL_SIZE)"

# Verify sdist exists
SDIST_FILE=$(ls dist/*.tar.gz 2>/dev/null | head -1)
if [ -n "$SDIST_FILE" ]; then
    SDIST_SIZE=$(du -h "$SDIST_FILE" | cut -f1)
    log_success "Source dist: $(basename "$SDIST_FILE") ($SDIST_SIZE)"
fi

log_success "=========================================="
log_success "Python package built successfully!"
log_success "=========================================="
log_info "Wheel file: $WHEEL_FILE"
log_info "Version: $VERSION"
log_info ""
log_info "To test locally:"
log_info "  pip install $WHEEL_FILE"
log_info "  syrvis --version"
log_info ""
log_info "To build SPK:"
log_info "  ./build-tools/build-spk.sh"
log_info ""

exit 0
