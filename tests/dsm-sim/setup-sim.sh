#!/bin/bash
# setup-sim.sh - Initialize DSM 7.0 simulation environment
#
# This script creates the simulated Synology DSM directory structure
# for testing SyrvisCore on macOS.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM_ROOT="$SCRIPT_DIR/root"
STATE_DIR="$SCRIPT_DIR/state"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }

echo ""
echo "============================================"
echo "  DSM 7.0 Simulation Setup"
echo "============================================"
echo ""

log_info "Creating simulation directory structure..."

# Create simulated filesystem directories
mkdir -p "$SIM_ROOT/var/run"
mkdir -p "$SIM_ROOT/var/packages/syrviscore/target"
mkdir -p "$SIM_ROOT/volume1/docker"
mkdir -p "$SIM_ROOT/usr/local/bin"
mkdir -p "$SIM_ROOT/usr/local/etc/rc.d"
mkdir -p "$SIM_ROOT/etc"
mkdir -p "$SIM_ROOT/tmp"

# Create state and logs directories
mkdir -p "$STATE_DIR"
mkdir -p "$SCRIPT_DIR/logs"

log_info "Initializing state files..."

# Initialize state files
echo "running" > "$STATE_DIR/docker-status.txt"
echo "[]" > "$STATE_DIR/installed-packages.json"
touch "$STATE_DIR/docker-group-members.txt"

# Create timezone file
echo "UTC" > "$SIM_ROOT/etc/TZ"

log_info "Setting up Docker socket..."

# Link to real Docker socket if available (for real Docker operations)
REAL_DOCKER_SOCK="/var/run/docker.sock"
SIM_DOCKER_SOCK="$SIM_ROOT/var/run/docker.sock"

if [ -S "$REAL_DOCKER_SOCK" ]; then
    # Remove existing link/file
    rm -f "$SIM_DOCKER_SOCK"
    # Create symlink to real Docker socket
    ln -s "$REAL_DOCKER_SOCK" "$SIM_DOCKER_SOCK"
    log_success "Linked to real Docker socket"
else
    # Create placeholder file
    touch "$SIM_DOCKER_SOCK"
    log_info "Docker socket placeholder created (Docker not running)"
fi

log_info "Making mock commands executable..."

# Make mock commands executable
chmod +x "$SCRIPT_DIR/bin/"* 2>/dev/null || true

echo ""
log_success "DSM simulation environment initialized!"
echo ""
echo "  Simulation root: $SIM_ROOT"
echo "  State directory: $STATE_DIR"
echo ""
echo "  To activate the simulation:"
echo "    source $SCRIPT_DIR/activate.sh"
echo ""
