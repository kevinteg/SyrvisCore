#!/bin/bash
# activate.sh - Enter DSM 7.0 simulation environment
#
# Usage: source tests/dsm-sim/activate.sh
#
# This script sets up environment variables to simulate a Synology DSM 7.0
# environment for testing SyrvisCore on macOS.

# Get script directory (works when sourced)
if [ -n "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    # Fallback for zsh
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

SIM_ROOT="$SCRIPT_DIR/root"

# Check if simulation is already active
if [ -n "$DSM_SIM_ACTIVE" ]; then
    echo "DSM simulation is already active"
    return 0 2>/dev/null || exit 0
fi

# Check if simulation has been set up
if [ ! -d "$SIM_ROOT" ]; then
    echo "Error: Simulation not initialized. Run setup-sim.sh first."
    return 1 2>/dev/null || exit 1
fi

# Save original environment values
export _DSM_ORIG_PATH="$PATH"
export _DSM_ORIG_SYNOPKG_PKGDEST="${SYNOPKG_PKGDEST:-}"
export _DSM_ORIG_SYRVIS_HOME="${SYRVIS_HOME:-}"
export _DSM_ORIG_PS1="${PS1:-}"

# Set simulation environment
export DSM_SIM_ACTIVE=1
export DSM_SIM_ROOT="$SIM_ROOT"
export DSM_SIM_STATE="$SCRIPT_DIR/state"
export DSM_SIM_LOGS="$SCRIPT_DIR/logs"

# Override PATH to use mock commands first
export PATH="$SCRIPT_DIR/bin:$PATH"

# Set DSM-specific environment variables
export SYNOPKG_PKGDEST="$SIM_ROOT/var/packages/syrviscore/target"
export SYRVIS_HOME="$SIM_ROOT/volume1/docker/syrviscore"
export PACKAGE_NAME="syrviscore"

# Wizard variables (default test values, can be overridden)
export pkgwizard_volume="$SIM_ROOT/volume1"
export pkgwizard_network_interface="en0"
export pkgwizard_network_subnet="192.168.1.0/24"
export pkgwizard_gateway_ip="192.168.1.1"
export pkgwizard_traefik_ip="192.168.1.100"
export pkgwizard_domain="test.local"
export pkgwizard_acme_email="test@test.local"
export pkgwizard_cloudflare_token=""

# Update prompt to show simulation is active
export PS1="(dsm-sim) ${PS1}"

echo ""
echo "============================================"
echo "  DSM 7.0 Simulation ACTIVATED"
echo "============================================"
echo ""
echo "  DSM_SIM_ROOT:    $DSM_SIM_ROOT"
echo "  SYNOPKG_PKGDEST: $SYNOPKG_PKGDEST"
echo "  SYRVIS_HOME:     $SYRVIS_HOME"
echo ""
echo "  To deactivate:"
echo "    source $(dirname "${BASH_SOURCE[0]}")/deactivate.sh"
echo ""
