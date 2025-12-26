#!/bin/bash
# build-service.sh - Build the SyrvisCore service package
# This builds the service wheel that gets installed via syrvisctl

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
SERVICE_DIR="$PROJECT_ROOT/packages/syrviscore"

log_info "SyrvisCore Service Builder"
log_info "Service directory: $SERVICE_DIR"

# Check service package exists
if [ ! -d "$SERVICE_DIR" ]; then
    log_error "Service package not found at: $SERVICE_DIR"
    exit 1
fi

if [ ! -f "$SERVICE_DIR/pyproject.toml" ]; then
    log_error "pyproject.toml not found in service package"
    exit 1
fi

# Read version
VERSION=$(grep '^__version__' "$SERVICE_DIR/src/syrviscore/__version__.py" | cut -d'"' -f2)
log_info "Building version: $VERSION"

# Create dist directory
mkdir -p "$PROJECT_ROOT/dist"

# Clean previous builds
log_info "Cleaning previous builds"
rm -rf "$SERVICE_DIR/dist" "$SERVICE_DIR/build" "$SERVICE_DIR/*.egg-info"
rm -f "$PROJECT_ROOT/dist"/syrviscore-[0-9]*.whl

# Build the wheel
log_info "Building service wheel"
cd "$SERVICE_DIR"

python -m build --wheel --outdir "$PROJECT_ROOT/dist"

# Find the wheel
WHEEL_FILE=$(ls "$PROJECT_ROOT/dist"/syrviscore-[0-9]*.whl 2>/dev/null | head -1)

if [ -z "$WHEEL_FILE" ]; then
    log_error "Failed to build wheel"
    exit 1
fi

WHEEL_NAME=$(basename "$WHEEL_FILE")
WHEEL_SIZE=$(du -h "$WHEEL_FILE" | cut -f1)

log_success "=========================================="
log_success "Service wheel built successfully!"
log_success "=========================================="
log_info "Package: $WHEEL_NAME"
log_info "Version: $VERSION"
log_info "Size: $WHEEL_SIZE"
log_info "Location: $PROJECT_ROOT/dist/$WHEEL_NAME"
log_info ""
log_info "To create a GitHub release:"
log_info "  ./build-tools/release-service.sh"

exit 0
