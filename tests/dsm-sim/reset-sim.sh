#!/bin/bash
# reset-sim.sh - Reset DSM simulation to clean state
#
# This removes all installed packages and resets state files
# while preserving the simulation infrastructure.

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
echo "  Resetting DSM Simulation"
echo "============================================"
echo ""

# Check if simulation exists
if [ ! -d "$SIM_ROOT" ]; then
    echo "Error: Simulation not initialized. Run setup-sim.sh first."
    exit 1
fi

log_info "Clearing SPK installation directory..."
rm -rf "$SIM_ROOT/var/packages/syrviscore/target/"*

log_info "Clearing SYRVIS_HOME directory..."
rm -rf "$SIM_ROOT/volume1/docker/syrviscore/"*

log_info "Clearing global symlinks..."
rm -rf "$SIM_ROOT/usr/local/bin/"*

log_info "Clearing startup scripts..."
rm -rf "$SIM_ROOT/usr/local/etc/rc.d/"*

log_info "Clearing temp directory..."
rm -rf "$SIM_ROOT/tmp/"*

log_info "Resetting state files..."
echo "running" > "$STATE_DIR/docker-status.txt"
echo "[]" > "$STATE_DIR/installed-packages.json"
> "$STATE_DIR/docker-group-members.txt"

log_info "Clearing logs..."
rm -f "$SCRIPT_DIR/logs/"*.log

echo ""
log_success "DSM simulation reset complete!"
echo ""
echo "  The simulation is now in a clean state."
echo "  Run 'source activate.sh' to re-enter the simulation."
echo ""
