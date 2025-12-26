#!/bin/bash
# build-spk.sh - Build SyrvisCore SPK package
# This script packages the SPK for Synology DSM
#
# The SPK contains only the MANAGER package (syrviscore-manager).
# The service package (syrviscore) is installed separately via syrvisctl.

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
MANAGER_DIR="$PROJECT_ROOT/packages/syrviscore-manager"

log_info "SyrvisCore SPK Builder"
log_info "Project root: $PROJECT_ROOT"

# Change to project root
cd "$PROJECT_ROOT"

# Read version from manager package
VERSION_FILE="$MANAGER_DIR/src/syrviscore_manager/__version__.py"
if [ ! -f "$VERSION_FILE" ]; then
    log_error "Version file not found: $VERSION_FILE"
    exit 1
fi

VERSION=$(grep '^__version__' "$VERSION_FILE" | cut -d'"' -f2)
log_info "Building version: $VERSION"

# Define paths
SPK_DIR="$PROJECT_ROOT/spk"
BUILD_DIR="$PROJECT_ROOT/build-spk-tmp"
DIST_DIR="$PROJECT_ROOT/dist"
PACKAGE_NAME="syrviscore-${VERSION}-noarch.spk"

# Clean previous build
log_info "Cleaning previous build artifacts"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
mkdir -p "$DIST_DIR"

# Validation: Check all required files exist
log_info "Validating SPK structure"

REQUIRED_FILES=(
    "spk/INFO"
    "spk/conf/privilege"
    "spk/scripts/preinst"
    "spk/scripts/postinst"
    "spk/scripts/preuninst"
    "spk/scripts/postuninst"
    "spk/scripts/preupgrade"
    "spk/scripts/postupgrade"
    "spk/icons/PACKAGE_ICON.PNG"
    "spk/icons/PACKAGE_ICON_256.PNG"
)

VALIDATION_FAILED=0
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        log_error "Required file missing: $file"
        VALIDATION_FAILED=1
    fi
done

if [ $VALIDATION_FAILED -eq 1 ]; then
    log_error "SPK structure validation failed"
    exit 1
fi

log_success "All required files present"

# Update version in INFO file
log_info "Updating version in INFO file"
sed -i.bak "s/^version=.*/version=\"${VERSION}\"/" spk/INFO
rm -f spk/INFO.bak

# Find the MANAGER wheel file (not service wheel)
log_info "Looking for manager wheel file"
WHEEL_FILE=$(ls "$PROJECT_ROOT/dist"/syrviscore_manager-*.whl 2>/dev/null | head -1)

if [ -z "$WHEEL_FILE" ]; then
    log_warn "Manager wheel not found, building..."
    "$SCRIPT_DIR/build-manager.sh"
    WHEEL_FILE=$(ls "$PROJECT_ROOT/dist"/syrviscore_manager-*.whl 2>/dev/null | head -1)
fi

if [ -z "$WHEEL_FILE" ]; then
    log_error "Failed to find or build manager wheel"
    log_error "Run ./build-tools/build-manager.sh first"
    exit 1
fi

WHEEL_NAME=$(basename "$WHEEL_FILE")
log_success "Found manager wheel: $WHEEL_NAME"

# Copy wheel and templates to build directory
log_info "Preparing package contents"
mkdir -p "$BUILD_DIR/package"

# Copy the wheel file
log_info "Copying Python wheel"
cp "$WHEEL_FILE" "$BUILD_DIR/package/"

# Note: .env.template and build/config.yaml are bundled with the SERVICE package,
# not the manager. The manager SPK is minimal - just the version management CLI.

# Verify package contents
log_info "Verifying package contents"
if [ ! -f "$BUILD_DIR/package/$WHEEL_NAME" ]; then
    log_error "Manager wheel file not found in package"
    exit 1
fi

log_success "Package contents verified (manager wheel only)"

# Create package.tgz
log_info "Creating package.tgz"
cd "$BUILD_DIR/package"
tar -czf ../package.tgz .
cd "$PROJECT_ROOT"

if [ ! -f "$BUILD_DIR/package.tgz" ]; then
    log_error "Failed to create package.tgz"
    exit 1
fi

PACKAGE_SIZE=$(du -h "$BUILD_DIR/package.tgz" | cut -f1)
log_success "Created package.tgz ($PACKAGE_SIZE)"

# Copy SPK metadata files to build directory
log_info "Copying SPK metadata"
cp spk/INFO "$BUILD_DIR/"

# Copy icons
log_info "Copying icons"
cp spk/icons/PACKAGE_ICON.PNG "$BUILD_DIR/"
cp spk/icons/PACKAGE_ICON_256.PNG "$BUILD_DIR/"

# Copy conf directory
log_info "Copying conf directory"
cp -r spk/conf "$BUILD_DIR/"

# CRITICAL FIX: Copy scripts as a DIRECTORY, not a tar archive
log_info "Copying scripts directory"
cp -r spk/scripts "$BUILD_DIR/"

# Verify scripts were copied
if [ ! -d "$BUILD_DIR/scripts" ]; then
    log_error "Failed to copy scripts directory"
    exit 1
fi

log_success "Scripts directory copied successfully"

# List build directory contents for verification
log_info "Build directory contents:"
ls -lh "$BUILD_DIR" | tail -n +2 | awk '{print "  " $9 " (" $5 ")"}'

# CRITICAL: Set proper ownership and permissions BEFORE creating SPK
# This prevents error 313 "failed to revise file attributes"
log_info "Setting ownership and permissions"
cd "$BUILD_DIR"

# Get current user's UID and GID
CURRENT_UID=$(id -u)
CURRENT_GID=$(id -g)

# Reset all ownership to current user (avoid root ownership issues)
log_info "Resetting ownership to ${CURRENT_UID}:${CURRENT_GID}"
find . -exec chown ${CURRENT_UID}:${CURRENT_GID} {} \; 2>/dev/null || true

# Set standard permissions on directories
log_info "Setting directory permissions (755)"
find . -type d -exec chmod 755 {} \;

# Set standard permissions on regular files
log_info "Setting file permissions (644)"
find . -type f -exec chmod 644 {} \;

# Make all script files executable (CRITICAL)
log_info "Making scripts executable (755)"
find scripts -type f -exec chmod 755 {} \;

# Set explicit permissions on SPK root components
chmod 644 INFO
chmod 644 package.tgz
chmod 644 PACKAGE_ICON.PNG
chmod 644 PACKAGE_ICON_256.PNG
chmod 755 conf
chmod 755 scripts
chmod 644 conf/privilege

# Verify script permissions
log_info "Verifying script permissions:"
ls -la scripts/ | grep -E '\.(sh|py)$|^d|preinst|postinst|preupgrade|postupgrade|preuninst|postuninst' || ls -la scripts/

log_success "Ownership and permissions set correctly"

# Create final SPK package
# CRITICAL: Outer SPK archive MUST be uncompressed tar (no -z flag)
# Using gzip causes Synology error 263 "invalid file format"
log_info "Creating final SPK package (uncompressed tar)"

# SPK is an UNCOMPRESSED tar archive containing:
# - INFO (file)
# - package.tgz (gzipped tar)
# - scripts/ (directory with executable scripts)
# - conf/ (directory)
# - PACKAGE_ICON*.PNG (files)

tar -cf "$DIST_DIR/$PACKAGE_NAME" \
    INFO \
    package.tgz \
    scripts \
    conf \
    PACKAGE_ICON.PNG \
    PACKAGE_ICON_256.PNG

cd "$PROJECT_ROOT"

if [ ! -f "$DIST_DIR/$PACKAGE_NAME" ]; then
    log_error "Failed to create SPK package"
    exit 1
fi

SPK_SIZE=$(du -h "$DIST_DIR/$PACKAGE_NAME" | cut -f1)
log_success "Created SPK package: $PACKAGE_NAME ($SPK_SIZE)"

# Verify SPK contents
log_info "Verifying SPK contents"
SPK_CONTENTS=$(tar -tf "$DIST_DIR/$PACKAGE_NAME" | sort)

log_info "SPK contents:"
echo "$SPK_CONTENTS" | head -20

# Check for critical files
EXPECTED_FILES=(
    "INFO"
    "PACKAGE_ICON.PNG"
    "PACKAGE_ICON_256.PNG"
    "package.tgz"
)

VERIFY_FAILED=0
for expected in "${EXPECTED_FILES[@]}"; do
    if ! echo "$SPK_CONTENTS" | grep -q "^${expected}$"; then
        log_error "Missing expected file in SPK: $expected"
        VERIFY_FAILED=1
    fi
done

# Check for directories
for dir in "scripts/" "conf/"; do
    if ! echo "$SPK_CONTENTS" | grep -q "^${dir}"; then
        log_error "Missing expected directory in SPK: $dir"
        VERIFY_FAILED=1
    fi
done

if [ $VERIFY_FAILED -eq 1 ]; then
    log_error "SPK verification failed"
    exit 1
fi

log_success "SPK contents verified"

# Verify SPK format
log_info "Verifying SPK package format"
if [ -f "$DIST_DIR/$PACKAGE_NAME" ] && [ -s "$DIST_DIR/$PACKAGE_NAME" ]; then
    # Check file type
    FILE_TYPE=$(file "$DIST_DIR/$PACKAGE_NAME")
    log_info "File type: $FILE_TYPE"
    
    # Test that tar can read it
    if tar -tf "$DIST_DIR/$PACKAGE_NAME" > /dev/null 2>&1; then
        log_success "SPK package verified (valid uncompressed tar format)"
    else
        log_error "SPK package is not a valid tar archive"
        exit 1
    fi
else
    log_error "SPK package file missing or empty"
    exit 1
fi

# Clean up build directory
log_info "Cleaning up temporary files"
rm -rf "$BUILD_DIR"

# Final summary
log_success "=========================================="
log_success "SPK package built successfully!"
log_success "=========================================="
log_info "Package: $DIST_DIR/$PACKAGE_NAME"
log_info "Version: $VERSION"
log_info "Size: $SPK_SIZE"
log_info ""
log_info "This SPK contains the SyrvisCore Manager (syrvisctl)."
log_info "After installation, users run 'syrvisctl install' to install the service."
log_info ""
log_info "Next steps:"
log_info "  1. Install SPK on Synology DSM"
log_info "  2. Run: syrvisctl install"
log_info "  3. Run: syrvis setup"
log_info ""
log_info "To inspect the package:"
log_info "  tar -tf $DIST_DIR/$PACKAGE_NAME"
log_info ""

exit 0
