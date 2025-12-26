#!/bin/bash
# build-manager.sh - Build the SyrvisCore Manager package
# This builds the manager wheel AND downloads all dependencies for offline install
# The SPK will bundle all wheels so no network access is needed at install time

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
DEPS_DIR="$PROJECT_ROOT/dist/manager-deps"

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
rm -rf "$DEPS_DIR"

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

log_success "Manager wheel built: $WHEEL_NAME ($WHEEL_SIZE)"

# Download all dependencies as wheels for offline installation
log_info "Downloading dependencies for offline installation..."
mkdir -p "$DEPS_DIR"

# Download wheels for the manager package and all its dependencies
# Using --platform linux_x86_64 to get Linux-compatible wheels for Synology
# Also get manylinux wheels which are compatible with most Linux systems
pip download \
    --dest "$DEPS_DIR" \
    --only-binary=:all: \
    --python-version 3.8 \
    --platform manylinux2014_x86_64 \
    --platform linux_x86_64 \
    --platform any \
    "$WHEEL_FILE" 2>/dev/null || {
    log_info "Some platform-specific wheels not available, downloading universal wheels..."
    pip download \
        --dest "$DEPS_DIR" \
        "$WHEEL_FILE"
}

# Count downloaded wheels
WHEEL_COUNT=$(ls -1 "$DEPS_DIR"/*.whl 2>/dev/null | wc -l | tr -d ' ')

log_info "Downloaded $WHEEL_COUNT wheel(s) to $DEPS_DIR"
log_info "Dependencies:"
ls -1 "$DEPS_DIR"/*.whl | while read f; do
    echo "  - $(basename "$f")"
done

# Copy main wheel to deps directory for unified installation
cp "$WHEEL_FILE" "$DEPS_DIR/"

log_success "=========================================="
log_success "Manager build completed!"
log_success "=========================================="
log_info "Package: $WHEEL_NAME"
log_info "Version: $VERSION"
log_info "Size: $WHEEL_SIZE"
log_info "Dependencies: $WHEEL_COUNT wheel(s)"
log_info ""
log_info "Wheel location: $PROJECT_ROOT/dist/$WHEEL_NAME"
log_info "Deps location:  $DEPS_DIR/"
log_info ""
log_info "To build SPK: ./build-tools/build-spk.sh"

exit 0
