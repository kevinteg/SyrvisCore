#!/bin/bash
# build-manager.sh - Build the SyrvisCore Manager package
# This builds the manager wheel that gets installed via SPK

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MANAGER_DIR="$PROJECT_ROOT/packages/syrviscore-manager"

log_info "SyrvisCore Manager Builder"
log_info "Manager directory: $MANAGER_DIR"

# Check manager package exists
if [ ! -d "$MANAGER_DIR" ]; then
    log_error "Manager package not found at: $MANAGER_DIR"
    exit 1
fi

if [ ! -f "$MANAGER_DIR/pyproject.toml" ]; then
    log_error "pyproject.toml not found in manager package"
    exit 1
fi

# Read version
VERSION=$(grep '^__version__' "$MANAGER_DIR/src/syrviscore_manager/__version__.py" | cut -d'"' -f2)
log_info "Building version: $VERSION"

# Create dist directory
mkdir -p "$PROJECT_ROOT/dist"

# Clean previous builds
log_info "Cleaning previous builds"
rm -rf "$MANAGER_DIR/dist" "$MANAGER_DIR/build" "$MANAGER_DIR/*.egg-info"
rm -f "$PROJECT_ROOT/dist"/syrviscore_manager-*.whl

# Build the wheel
log_info "Building manager wheel"
cd "$MANAGER_DIR"

python -m build --wheel --outdir "$PROJECT_ROOT/dist"

# Find the wheel
WHEEL_FILE=$(ls "$PROJECT_ROOT/dist"/syrviscore_manager-*.whl 2>/dev/null | head -1)

if [ -z "$WHEEL_FILE" ]; then
    log_error "Failed to build wheel"
    exit 1
fi

WHEEL_NAME=$(basename "$WHEEL_FILE")
WHEEL_SIZE=$(du -h "$WHEEL_FILE" | cut -f1)

log_success "=========================================="
log_success "Manager wheel built successfully!"
log_success "=========================================="
log_info "Package: $WHEEL_NAME"
log_info "Version: $VERSION"
log_info "Size: $WHEEL_SIZE"
log_info "Location: $PROJECT_ROOT/dist/$WHEEL_NAME"
log_info ""
log_info "To build SPK: ./build-tools/build-spk.sh"

exit 0
