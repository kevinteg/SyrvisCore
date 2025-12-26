#!/bin/bash
# test_sim_workflow.sh - Full SyrvisCore workflow test in DSM simulation
#
# This script tests the complete installation and setup workflow:
# 1. Extract and install the SPK (manager package)
# 2. Run syrvisctl to install service package
# 3. Verify directory structures are created correctly

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SIM_DIR="$SCRIPT_DIR/dsm-sim"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_step() { echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${BLUE}Step $1: $2${NC}"; echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

echo ""
echo "============================================================"
echo "  SyrvisCore DSM Simulation Workflow Test"
echo "============================================================"
echo ""
echo "  Project Root: $PROJECT_ROOT"
echo "  Simulation:   $SIM_DIR"
echo ""

# ============================================================
# Step 1: Initialize simulation
# ============================================================
log_step "1/8" "Initialize simulation environment"

if [ ! -f "$SIM_DIR/setup-sim.sh" ]; then
    log_fail "setup-sim.sh not found. Run from project root."
fi

chmod +x "$SIM_DIR/setup-sim.sh"
chmod +x "$SIM_DIR/bin/"* 2>/dev/null || true
"$SIM_DIR/setup-sim.sh"
log_pass "Simulation initialized"

# ============================================================
# Step 2: Activate simulation environment
# ============================================================
log_step "2/8" "Activate simulation environment"

# Source activate.sh to set environment
source "$SIM_DIR/activate.sh"

# Verify environment variables are set
if [ -z "$DSM_SIM_ACTIVE" ]; then
    log_fail "DSM_SIM_ACTIVE not set"
fi

if [ -z "$SYNOPKG_PKGDEST" ]; then
    log_fail "SYNOPKG_PKGDEST not set"
fi

log_pass "Environment activated"
echo "  SYNOPKG_PKGDEST: $SYNOPKG_PKGDEST"
echo "  SYRVIS_HOME:     $SYRVIS_HOME"

# ============================================================
# Step 3: Verify mock commands work
# ============================================================
log_step "3/8" "Verify mock commands"

# Test synopkg
if ! synopkg status Docker >/dev/null 2>&1; then
    log_fail "synopkg mock not working"
fi
log_pass "synopkg mock working"

# Test synogroup
if ! synogroup --get docker >/dev/null 2>&1; then
    log_fail "synogroup mock not working"
fi
log_pass "synogroup mock working"

# ============================================================
# Step 4: Build SPK if needed
# ============================================================
log_step "4/8" "Locate or build SPK package"

# Get version from manager package
VERSION_FILE="$PROJECT_ROOT/packages/syrviscore-manager/src/syrviscore_manager/__version__.py"
if [ -f "$VERSION_FILE" ]; then
    VERSION=$(grep '^__version__' "$VERSION_FILE" | cut -d'"' -f2)
else
    VERSION="0.0.1"
fi

SPK_FILE="$PROJECT_ROOT/dist/syrviscore-${VERSION}-noarch.spk"

if [ ! -f "$SPK_FILE" ]; then
    log_warn "SPK not found: $SPK_FILE"
    log_info "Building SPK..."

    # Deactivate temporarily to avoid PATH issues during build
    source "$SIM_DIR/deactivate.sh"

    cd "$PROJECT_ROOT"
    if ! make build-spk; then
        log_fail "SPK build failed"
    fi

    # Re-activate simulation
    source "$SIM_DIR/activate.sh"
fi

if [ ! -f "$SPK_FILE" ]; then
    log_fail "SPK file still not found after build: $SPK_FILE"
fi

log_pass "Found SPK: $(basename "$SPK_FILE")"

# ============================================================
# Step 5: Extract SPK
# ============================================================
log_step "5/8" "Extract SPK package"

EXTRACT_DIR="$DSM_SIM_ROOT/tmp/spk-extract"
mkdir -p "$EXTRACT_DIR"

# Extract outer tar
tar -xf "$SPK_FILE" -C "$EXTRACT_DIR"

# Extract package.tgz to SYNOPKG_PKGDEST
if [ -f "$EXTRACT_DIR/package.tgz" ]; then
    tar -xzf "$EXTRACT_DIR/package.tgz" -C "$SYNOPKG_PKGDEST"
else
    log_fail "package.tgz not found in SPK"
fi

log_pass "SPK extracted to $SYNOPKG_PKGDEST"
echo "  Contents:"
ls -la "$SYNOPKG_PKGDEST" | head -10

# ============================================================
# Step 6: Run postinst script
# ============================================================
log_step "6/8" "Run postinst script (install manager)"

POSTINST="$EXTRACT_DIR/scripts/postinst"

if [ ! -f "$POSTINST" ]; then
    log_fail "postinst script not found"
fi

chmod +x "$POSTINST"

# Run postinst
if ! "$POSTINST"; then
    log_fail "postinst script failed"
fi

log_pass "postinst completed"

# ============================================================
# Step 7: Verify syrvisctl installation
# ============================================================
log_step "7/8" "Verify syrvisctl installation"

SYRVISCTL="$SYNOPKG_PKGDEST/venv/bin/syrvisctl"

if [ ! -x "$SYRVISCTL" ]; then
    log_fail "syrvisctl not found at $SYRVISCTL"
fi

# Test syrvisctl
SYRVISCTL_VERSION=$("$SYRVISCTL" --version 2>&1)
log_pass "syrvisctl installed: $SYRVISCTL_VERSION"

# Test syrvisctl info
log_info "Running 'syrvisctl info'..."
"$SYRVISCTL" info || true

# Test syrvisctl list
log_info "Running 'syrvisctl list'..."
"$SYRVISCTL" list || true

# ============================================================
# Step 8: Test syrvisctl install (optional - requires network)
# ============================================================
log_step "8/8" "Test syrvisctl install (service package)"

# Check if we have network access to GitHub
if curl -s --connect-timeout 3 https://api.github.com >/dev/null 2>&1; then
    log_info "Network available - testing syrvisctl install..."

    # Check for updates (doesn't require actual release)
    "$SYRVISCTL" check || log_warn "No releases found on GitHub (expected for new project)"

    # Try to install (will fail if no release exists, which is fine)
    log_info "Attempting syrvisctl install..."
    if "$SYRVISCTL" install 2>&1; then
        log_pass "Service package installed"

        # Verify SYRVIS_HOME was created
        if [ -d "$SYRVIS_HOME" ]; then
            log_pass "SYRVIS_HOME created: $SYRVIS_HOME"
            echo "  Contents:"
            ls -la "$SYRVIS_HOME" | head -10
        fi
    else
        log_warn "syrvisctl install failed (no release available?)"
    fi
else
    log_warn "No network access - skipping syrvisctl install test"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "============================================================"
echo -e "${GREEN}  DSM Simulation Workflow Test COMPLETED${NC}"
echo "============================================================"
echo ""
echo "  Simulation state preserved at: $SIM_DIR"
echo ""
echo "  Installed components:"
echo "    - Manager venv: $SYNOPKG_PKGDEST/venv/"
echo "    - syrvisctl:    $SYRVISCTL"
echo ""
echo "  To continue testing interactively:"
echo "    source $SIM_DIR/activate.sh"
echo "    $SYRVISCTL --help"
echo ""
echo "  To reset simulation:"
echo "    source $SIM_DIR/deactivate.sh"
echo "    make sim-reset"
echo ""

# Don't deactivate - leave simulation active for interactive use
log_info "Simulation remains active. Run 'source tests/dsm-sim/deactivate.sh' when done."
