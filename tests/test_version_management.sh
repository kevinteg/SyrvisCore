#!/bin/bash
# Test version management: install, activate, rollback
#
# This test verifies that multiple service versions can be installed,
# switched between, and rolled back in the DSM simulation environment.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SIM_ROOT="$SCRIPT_DIR/dsm-sim/root"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# ============================================================
# Setup
# ============================================================

echo
echo "============================================================"
echo "  Version Management Test"
echo "============================================================"
echo

# Reset simulation
info "Resetting simulation environment..."
"$SCRIPT_DIR/dsm-sim/reset-sim.sh" > /dev/null 2>&1

# Activate simulation
export DSM_SIM_ACTIVE=1
export DSM_SIM_ROOT="$SIM_ROOT"
export SYNOPKG_PKGDEST="$SIM_ROOT/var/packages/syrviscore/target"
export SYRVIS_HOME="$SIM_ROOT/volume1/docker/syrviscore"
export PATH="$SCRIPT_DIR/dsm-sim/bin:$SIM_ROOT/usr/local/bin:$PATH"

# Get syrvisctl path
SYRVISCTL="$SYNOPKG_PKGDEST/venv/bin/syrvisctl"

# Check if manager is installed
if [ ! -f "$SYRVISCTL" ]; then
    info "Installing manager (postinst)..."

    # Build and extract SPK
    "$PROJECT_ROOT/build-tools/build-spk.sh" > /dev/null 2>&1

    SPK_FILE=$(ls "$PROJECT_ROOT/dist"/*.spk 2>/dev/null | head -1)
    if [ -z "$SPK_FILE" ]; then
        fail "No SPK file found"
    fi

    EXTRACT_DIR="$SIM_ROOT/tmp/spk-extract"
    mkdir -p "$EXTRACT_DIR"
    tar -xf "$SPK_FILE" -C "$EXTRACT_DIR"
    tar -xzf "$EXTRACT_DIR/package.tgz" -C "$SYNOPKG_PKGDEST"

    # Run postinst
    "$EXTRACT_DIR/scripts/postinst" > /dev/null 2>&1

    if [ ! -f "$SYRVISCTL" ]; then
        fail "Manager installation failed"
    fi
    pass "Manager installed"
fi

# ============================================================
# Test 1: Install v0.0.1
# ============================================================

echo
info "Test 1: Installing v0.0.1..."
$SYRVISCTL install 0.0.1 2>&1 | tail -5

# Verify installation
if [ -d "$SYRVIS_HOME/versions/0.0.1" ]; then
    pass "v0.0.1 installed"
else
    fail "v0.0.1 not found in versions directory"
fi

# Check active version
ACTIVE=$($SYRVISCTL info 2>&1 | grep "Active version:" | awk '{print $3}')
if [ "$ACTIVE" = "0.0.1" ]; then
    pass "v0.0.1 is active"
else
    fail "Expected active version 0.0.1, got: $ACTIVE"
fi

# ============================================================
# Test 2: Install v0.0.2
# ============================================================

echo
info "Test 2: Installing v0.0.2..."
$SYRVISCTL install 0.0.2 2>&1 | tail -5

# Verify installation
if [ -d "$SYRVIS_HOME/versions/0.0.2" ]; then
    pass "v0.0.2 installed"
else
    fail "v0.0.2 not found in versions directory"
fi

# Check active version (should now be 0.0.2)
ACTIVE=$($SYRVISCTL info 2>&1 | grep "Active version:" | awk '{print $3}')
if [ "$ACTIVE" = "0.0.2" ]; then
    pass "v0.0.2 is now active"
else
    fail "Expected active version 0.0.2, got: $ACTIVE"
fi

# ============================================================
# Test 3: List versions
# ============================================================

echo
info "Test 3: Listing versions..."
$SYRVISCTL list 2>&1

# Count versions
VERSION_COUNT=$($SYRVISCTL list 2>&1 | grep -E "^\s+0\." | wc -l | tr -d ' ')
if [ "$VERSION_COUNT" -eq 2 ]; then
    pass "Both versions listed (count: $VERSION_COUNT)"
else
    fail "Expected 2 versions, got: $VERSION_COUNT"
fi

# ============================================================
# Test 4: Activate v0.0.1
# ============================================================

echo
info "Test 4: Activating v0.0.1..."
$SYRVISCTL activate 0.0.1 2>&1

# Verify
ACTIVE=$($SYRVISCTL info 2>&1 | grep "Active version:" | awk '{print $3}')
if [ "$ACTIVE" = "0.0.1" ]; then
    pass "Switched to v0.0.1"
else
    fail "Expected active version 0.0.1, got: $ACTIVE"
fi

# Verify 'current' symlink
CURRENT_TARGET=$(readlink "$SYRVIS_HOME/current")
if [ "$CURRENT_TARGET" = "versions/0.0.1" ]; then
    pass "Symlink points to v0.0.1"
else
    fail "Expected symlink to versions/0.0.1, got: $CURRENT_TARGET"
fi

# ============================================================
# Test 5: Activate v0.0.2
# ============================================================

echo
info "Test 5: Activating v0.0.2..."
$SYRVISCTL activate 0.0.2 2>&1

ACTIVE=$($SYRVISCTL info 2>&1 | grep "Active version:" | awk '{print $3}')
if [ "$ACTIVE" = "0.0.2" ]; then
    pass "Switched to v0.0.2"
else
    fail "Expected active version 0.0.2, got: $ACTIVE"
fi

# ============================================================
# Test 6: Rollback to v0.0.1
# ============================================================

echo
info "Test 6: Rolling back..."
echo "y" | $SYRVISCTL rollback 2>&1

ACTIVE=$($SYRVISCTL info 2>&1 | grep "Active version:" | awk '{print $3}')
if [ "$ACTIVE" = "0.0.1" ]; then
    pass "Rolled back to v0.0.1"
else
    fail "Expected rollback to 0.0.1, got: $ACTIVE"
fi

# ============================================================
# Test 7: Show info
# ============================================================

echo
info "Test 7: Show installation info..."
$SYRVISCTL info 2>&1

# Check update history
HISTORY=$($SYRVISCTL info 2>&1 | grep -A10 "Recent updates:" | grep -c '\->' 2>/dev/null) || HISTORY=0
if [ "$HISTORY" -ge 2 ]; then
    pass "Update history recorded ($HISTORY entries)"
else
    warn "Update history has $HISTORY entries (expected >= 2)"
fi

# ============================================================
# Summary
# ============================================================

echo
echo "============================================================"
echo -e "${GREEN}  Version Management Test PASSED${NC}"
echo "============================================================"
echo
echo "Tested operations:"
echo "  - Install multiple versions (0.0.1, 0.0.2)"
echo "  - List installed versions"
echo "  - Activate specific version"
echo "  - Rollback to previous version"
echo "  - Track update history"
echo
echo "Simulation preserved at: $SIM_ROOT"
echo
