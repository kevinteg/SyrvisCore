#!/bin/bash
# test_spk_installation.sh - Test SPK installation simulation
# This script validates the SPK package before deploying to actual Synology

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[✓ PASS]${NC} $*"
    TESTS_PASSED=$((TESTS_PASSED + 1))
}

log_fail() {
    echo -e "${RED}[✗ FAIL]${NC} $*"
    TESTS_FAILED=$((TESTS_FAILED + 1))
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

# Test assertion functions
assert_file_exists() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if [ -f "$1" ]; then
        log_success "File exists: $1"
        return 0
    else
        log_fail "File missing: $1"
        return 1
    fi
}

assert_dir_exists() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if [ -d "$1" ]; then
        log_success "Directory exists: $1"
        return 0
    else
        log_fail "Directory missing: $1"
        return 1
    fi
}

assert_executable() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if [ -x "$1" ]; then
        log_success "File is executable: $1"
        return 0
    else
        log_fail "File is not executable: $1"
        return 1
    fi
}

assert_contains() {
    TESTS_RUN=$((TESTS_RUN + 1))
    local file="$1"
    local pattern="$2"
    local description="$3"
    
    if grep -q "$pattern" "$file"; then
        log_success "$description"
        return 0
    else
        log_fail "$description (pattern not found: $pattern)"
        return 1
    fi
}

assert_command_exists() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if command -v "$1" > /dev/null 2>&1; then
        log_success "Command exists: $1"
        return 0
    else
        log_fail "Command not found: $1"
        return 1
    fi
}

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo ""
echo "============================================================"
echo "  SyrvisCore SPK Installation Test Suite"
echo "============================================================"
echo ""

log_info "Project root: $PROJECT_ROOT"
cd "$PROJECT_ROOT"

# Read version
if [ ! -f "src/syrviscore/__version__.py" ]; then
    log_fail "Version file not found: src/syrviscore/__version__.py"
    exit 1
fi

VERSION=$(grep '^__version__' src/syrviscore/__version__.py | cut -d'"' -f2)
log_info "Testing version: $VERSION"

# Define paths
DIST_DIR="$PROJECT_ROOT/dist"
SPK_FILE="$DIST_DIR/syrviscore-${VERSION}-noarch.spk"
TEST_DIR="$PROJECT_ROOT/tests/spk-test-tmp"
TEST_INSTALL_DIR="$TEST_DIR/volume1/docker/syrviscore"
TEST_PKG_DIR="$TEST_DIR/var/packages/syrviscore"

# Wizard variables (simulating user input)
# Use TEST_DIR/volume1 to keep installation within test directory
export pkgwizard_volume="$TEST_DIR/volume1"
export pkgwizard_network_interface="ovs_eth0"
export pkgwizard_network_subnet="192.168.0.0/24"
export pkgwizard_gateway_ip="192.168.0.1"
export pkgwizard_traefik_ip="192.168.0.4"
export pkgwizard_domain="test.example.com"
export pkgwizard_acme_email="test@example.com"
export pkgwizard_cloudflare_token="test-token-12345"

# ============================================================
# TEST 1: SPK File Exists
# ============================================================
echo ""
log_info "TEST 1: Checking SPK package exists"
echo "------------------------------------------------------------"

if [ ! -f "$SPK_FILE" ]; then
    log_fail "SPK file not found: $SPK_FILE"
    log_warn "Please run: make build-spk"
    exit 1
fi

assert_file_exists "$SPK_FILE"

SPK_SIZE=$(du -h "$SPK_FILE" | cut -f1)
log_info "SPK size: $SPK_SIZE"

# ============================================================
# TEST 2: Extract SPK Structure
# ============================================================
echo ""
log_info "TEST 2: Extracting SPK structure"
echo "------------------------------------------------------------"

# Clean previous test
rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR/spk-contents"
mkdir -p "$TEST_PKG_DIR/target"

# Extract SPK
log_info "Extracting SPK to: $TEST_DIR/spk-contents"
tar -xf "$SPK_FILE" -C "$TEST_DIR/spk-contents"

# Verify SPK structure
assert_file_exists "$TEST_DIR/spk-contents/INFO"
assert_file_exists "$TEST_DIR/spk-contents/package.tgz"
assert_dir_exists "$TEST_DIR/spk-contents/scripts"
assert_dir_exists "$TEST_DIR/spk-contents/WIZARD_UIFILES"
assert_dir_exists "$TEST_DIR/spk-contents/conf"
assert_file_exists "$TEST_DIR/spk-contents/PACKAGE_ICON.PNG"
assert_file_exists "$TEST_DIR/spk-contents/PACKAGE_ICON_256.PNG"

# Extract package.tgz
log_info "Extracting package.tgz"
mkdir -p "$TEST_PKG_DIR/target/package"
tar -xzf "$TEST_DIR/spk-contents/package.tgz" -C "$TEST_PKG_DIR/target/package"

# Verify package contents
assert_file_exists "$TEST_PKG_DIR/target/package/.env.template"

# Find the wheel file
WHEEL_FILE=$(find "$TEST_PKG_DIR/target/package" -name "*.whl" | head -1)
if [ -n "$WHEEL_FILE" ]; then
    log_success "Found wheel file: $(basename "$WHEEL_FILE")"
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "No wheel file found in package"
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# Copy build config if it exists
if [ -d "$TEST_PKG_DIR/target/package/build" ]; then
    log_success "Found build config directory"
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
fi

# ============================================================
# TEST 3: Simulate Synology Environment
# ============================================================
echo ""
log_info "TEST 3: Setting up simulated Synology environment"
echo "------------------------------------------------------------"

# Create simulated directories
mkdir -p "$TEST_INSTALL_DIR"
mkdir -p "$(dirname "$TEST_PKG_DIR")"

# Copy INFO to package directory for version reading
cp "$TEST_DIR/spk-contents/INFO" "$TEST_PKG_DIR/"

log_info "Simulated environment:"
log_info "  SYNOPKG_PKGDEST: $TEST_PKG_DIR/target"
log_info "  INSTALL_DIR: $TEST_INSTALL_DIR"
log_info "  pkgwizard_volume (will create): $pkgwizard_volume"
log_info ""
log_info "Wizard variables:"
log_info "  pkgwizard_volume: $pkgwizard_volume"
log_info "  pkgwizard_network_interface: $pkgwizard_network_interface"
log_info "  pkgwizard_network_subnet: $pkgwizard_network_subnet"
log_info "  pkgwizard_gateway_ip: $pkgwizard_gateway_ip"
log_info "  pkgwizard_traefik_ip: $pkgwizard_traefik_ip"
log_info "  pkgwizard_domain: $pkgwizard_domain"
log_info "  pkgwizard_acme_email: $pkgwizard_acme_email"
log_info "  pkgwizard_cloudflare_token: $pkgwizard_cloudflare_token"

# ============================================================
# TEST 4: Run postinst Script
# ============================================================
echo ""
log_info "TEST 4: Running postinst script"
echo "------------------------------------------------------------"

# Check if Python 3 is available
assert_command_exists python3

# Make postinst executable
chmod +x "$TEST_DIR/spk-contents/scripts/postinst"
assert_executable "$TEST_DIR/spk-contents/scripts/postinst"

# Set up environment variables for postinst
export SYNOPKG_PKGDEST="$TEST_PKG_DIR/target"
export PACKAGE_NAME="syrviscore"

# Run postinst script
log_info "Executing postinst script..."
if "$TEST_DIR/spk-contents/scripts/postinst"; then
    log_success "postinst script completed successfully"
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "postinst script failed"
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    exit 1
fi

# ============================================================
# TEST 5: Verify Directory Structure
# ============================================================
echo ""
log_info "TEST 5: Verifying directory structure created"
echo "------------------------------------------------------------"

assert_dir_exists "$TEST_INSTALL_DIR"
assert_dir_exists "$TEST_INSTALL_DIR/data"
assert_dir_exists "$TEST_INSTALL_DIR/data/traefik"
assert_dir_exists "$TEST_INSTALL_DIR/data/traefik/config"
assert_dir_exists "$TEST_INSTALL_DIR/data/traefik/logs"
assert_dir_exists "$TEST_INSTALL_DIR/data/portainer"
assert_dir_exists "$TEST_INSTALL_DIR/data/cloudflared"
assert_dir_exists "$TEST_INSTALL_DIR/cli"
assert_dir_exists "$TEST_INSTALL_DIR/cli/venv"
assert_dir_exists "$TEST_INSTALL_DIR/bin"
assert_dir_exists "$TEST_INSTALL_DIR/versions"

# Check for build directory if config exists
if [ -d "$TEST_INSTALL_DIR/build" ]; then
    assert_dir_exists "$TEST_INSTALL_DIR/build"
fi

# ============================================================
# TEST 6: Verify Python Virtual Environment and CLI
# ============================================================
echo ""
log_info "TEST 6: Verifying Python venv and CLI installation"
echo "------------------------------------------------------------"

assert_file_exists "$TEST_INSTALL_DIR/cli/venv/bin/python3"
assert_file_exists "$TEST_INSTALL_DIR/cli/venv/bin/pip"
assert_file_exists "$TEST_INSTALL_DIR/cli/venv/bin/syrvis"
assert_executable "$TEST_INSTALL_DIR/cli/venv/bin/syrvis"

# Test that syrvis CLI is installed
log_info "Testing syrvis CLI..."
TESTS_RUN=$((TESTS_RUN + 1))
if "$TEST_INSTALL_DIR/cli/venv/bin/syrvis" --version > /dev/null 2>&1; then
    CLI_VERSION=$("$TEST_INSTALL_DIR/cli/venv/bin/syrvis" --version 2>&1 || echo "unknown")
    log_success "CLI installed and working: $CLI_VERSION"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "CLI not working properly"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# ============================================================
# TEST 7: Verify .env File Generated with Wizard Values
# ============================================================
echo ""
log_info "TEST 7: Verifying .env file generated with wizard values"
echo "------------------------------------------------------------"

ENV_FILE="$TEST_INSTALL_DIR/.env"
assert_file_exists "$ENV_FILE"

# Check file permissions (should be 600)
TESTS_RUN=$((TESTS_RUN + 1))
PERMS=$(stat -f "%OLp" "$ENV_FILE" 2>/dev/null || stat -c "%a" "$ENV_FILE" 2>/dev/null)
if [ "$PERMS" = "600" ]; then
    log_success ".env file has correct permissions: 600"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail ".env file has incorrect permissions: $PERMS (expected 600)"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# Verify .env contents
assert_contains "$ENV_FILE" "SYRVISCORE_INSTALL_DIR=$TEST_INSTALL_DIR" ".env contains INSTALL_DIR"
assert_contains "$ENV_FILE" "SYRVISCORE_DATA_DIR=$TEST_INSTALL_DIR/data" ".env contains DATA_DIR"
assert_contains "$ENV_FILE" "NETWORK_INTERFACE=$pkgwizard_network_interface" ".env contains NETWORK_INTERFACE"
assert_contains "$ENV_FILE" "NETWORK_SUBNET=$pkgwizard_network_subnet" ".env contains NETWORK_SUBNET"
assert_contains "$ENV_FILE" "GATEWAY_IP=$pkgwizard_gateway_ip" ".env contains GATEWAY_IP"
assert_contains "$ENV_FILE" "TRAEFIK_IP=$pkgwizard_traefik_ip" ".env contains TRAEFIK_IP"
assert_contains "$ENV_FILE" "DOMAIN=$pkgwizard_domain" ".env contains DOMAIN"
assert_contains "$ENV_FILE" "ACME_EMAIL=$pkgwizard_acme_email" ".env contains ACME_EMAIL"
assert_contains "$ENV_FILE" "CLOUDFLARE_TUNNEL_TOKEN=$pkgwizard_cloudflare_token" ".env contains CLOUDFLARE_TUNNEL_TOKEN"

# ============================================================
# TEST 8: Verify Manifest Created with setup_complete: false
# ============================================================
echo ""
log_info "TEST 8: Verifying manifest file"
echo "------------------------------------------------------------"

MANIFEST_FILE="$TEST_INSTALL_DIR/.syrviscore-manifest.json"
assert_file_exists "$MANIFEST_FILE"

# Check file permissions (should be 644)
TESTS_RUN=$((TESTS_RUN + 1))
PERMS=$(stat -f "%OLp" "$MANIFEST_FILE" 2>/dev/null || stat -c "%a" "$MANIFEST_FILE" 2>/dev/null)
if [ "$PERMS" = "644" ]; then
    log_success "Manifest file has correct permissions: 644"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "Manifest file has incorrect permissions: $PERMS (expected 644)"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# Verify manifest contents
assert_contains "$MANIFEST_FILE" '"version": "'"$VERSION"'"' "Manifest contains version"
assert_contains "$MANIFEST_FILE" '"install_dir": "'"$TEST_INSTALL_DIR"'"' "Manifest contains install_dir"
assert_contains "$MANIFEST_FILE" '"setup_complete": false' "Manifest has setup_complete: false"
assert_contains "$MANIFEST_FILE" '"domain": "'"$pkgwizard_domain"'"' "Manifest contains domain"

# Verify it's valid JSON
TESTS_RUN=$((TESTS_RUN + 1))
if python3 -m json.tool "$MANIFEST_FILE" > /dev/null 2>&1; then
    log_success "Manifest is valid JSON"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "Manifest is not valid JSON"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# ============================================================
# TEST 9: Verify setup-privileges.py is Executable
# ============================================================
echo ""
log_info "TEST 9: Verifying setup-privileges.py"
echo "------------------------------------------------------------"

SETUP_SCRIPT="$TEST_INSTALL_DIR/bin/setup-privileges.py"
assert_file_exists "$SETUP_SCRIPT"
assert_executable "$SETUP_SCRIPT"

# Verify it's a Python script
TESTS_RUN=$((TESTS_RUN + 1))
if head -n 1 "$SETUP_SCRIPT" | grep -q "^#!.*python"; then
    log_success "setup-privileges.py has Python shebang"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "setup-privileges.py missing Python shebang"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# ============================================================
# TEST 10: Verify bin/syrvis Wrapper Exists
# ============================================================
echo ""
log_info "TEST 10: Verifying bin/syrvis wrapper"
echo "------------------------------------------------------------"

WRAPPER_SCRIPT="$TEST_INSTALL_DIR/bin/syrvis"
assert_file_exists "$WRAPPER_SCRIPT"
assert_executable "$WRAPPER_SCRIPT"

# Verify wrapper contents
assert_contains "$WRAPPER_SCRIPT" "#!/bin/sh" "Wrapper has shell shebang"
assert_contains "$WRAPPER_SCRIPT" "SYRVIS_HOME" "Wrapper sets SYRVIS_HOME"
assert_contains "$WRAPPER_SCRIPT" "cli/venv/bin/syrvis" "Wrapper calls venv syrvis"

# Test wrapper execution
log_info "Testing wrapper script..."
TESTS_RUN=$((TESTS_RUN + 1))
if "$WRAPPER_SCRIPT" --version > /dev/null 2>&1; then
    WRAPPER_VERSION=$("$WRAPPER_SCRIPT" --version 2>&1 || echo "unknown")
    log_success "Wrapper script works: $WRAPPER_VERSION"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "Wrapper script not working"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# ============================================================
# TEST 11: Verify Additional Files
# ============================================================
echo ""
log_info "TEST 11: Verifying additional files"
echo "------------------------------------------------------------"

# acme.json should exist with strict permissions
ACME_FILE="$TEST_INSTALL_DIR/data/traefik/acme.json"
assert_file_exists "$ACME_FILE"

TESTS_RUN=$((TESTS_RUN + 1))
PERMS=$(stat -f "%OLp" "$ACME_FILE" 2>/dev/null || stat -c "%a" "$ACME_FILE" 2>/dev/null)
if [ "$PERMS" = "600" ]; then
    log_success "acme.json has correct permissions: 600"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "acme.json has incorrect permissions: $PERMS (expected 600)"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# .env.template should be copied
assert_file_exists "$TEST_INSTALL_DIR/.env.template"

# Task template should exist
TASK_TEMPLATE="$TEST_INSTALL_DIR/bin/syrviscore-task-template.json"
assert_file_exists "$TASK_TEMPLATE"

# Verify task template is valid JSON
TESTS_RUN=$((TESTS_RUN + 1))
if python3 -m json.tool "$TASK_TEMPLATE" > /dev/null 2>&1; then
    log_success "Task template is valid JSON"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "Task template is not valid JSON"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# Verify task template has correct path (not ${INSTALL_DIR})
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "${TEST_INSTALL_DIR}" "$TASK_TEMPLATE" && ! grep -q '${INSTALL_DIR}' "$TASK_TEMPLATE"; then
    log_success "Task template has correct install path"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    log_fail "Task template has incorrect install path"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi

# ============================================================
# TEST 12: Verify Script Files
# ============================================================
echo ""
log_info "TEST 12: Verifying SPK script files"
echo "------------------------------------------------------------"

# All scripts should be executable
for script in postinst preinst postuninst preuninst postupgrade preupgrade start-stop-status; do
    SCRIPT_FILE="$TEST_DIR/spk-contents/scripts/$script"
    if [ -f "$SCRIPT_FILE" ]; then
        assert_executable "$SCRIPT_FILE"
    fi
done

# ============================================================
# FINAL SUMMARY
# ============================================================
echo ""
echo "============================================================"
echo "  Test Summary"
echo "============================================================"
echo ""
log_info "Total tests run: $TESTS_RUN"
log_success "Tests passed: $TESTS_PASSED"

if [ $TESTS_FAILED -gt 0 ]; then
    log_fail "Tests failed: $TESTS_FAILED"
    echo ""
    echo "❌ Some tests failed. Please review the output above."
    echo ""
else
    echo ""
    echo "✅ All tests passed!"
    echo ""
fi

# Show test installation location for manual inspection
echo "------------------------------------------------------------"
log_info "Test installation preserved at: $TEST_DIR"
log_info "To inspect manually:"
log_info "  ls -laR $TEST_INSTALL_DIR"
log_info "  cat $TEST_INSTALL_DIR/.env"
log_info "  cat $TEST_INSTALL_DIR/.syrviscore-manifest.json"
log_info ""
log_info "To clean up test files:"
log_info "  rm -rf $TEST_DIR"
echo ""

# Exit with appropriate code
if [ $TESTS_FAILED -gt 0 ]; then
    exit 1
else
    exit 0
fi
