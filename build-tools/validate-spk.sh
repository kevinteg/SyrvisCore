#!/bin/bash
# validate_spk.sh - Comprehensive SPK validation for DSM 7.1
# Helps diagnose errors like 263 (invalid format) and 313 (file attributes)

set -e

SPK_FILE="$1"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

error() { echo -e "${RED}✗${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${BLUE}ℹ${NC} $*"; }

if [ -z "$SPK_FILE" ]; then
    echo "Usage: $0 <spk_file>"
    echo ""
    echo "Examples:"
    echo "  $0 mypackage.spk"
    echo "  $0 dist/syrviscore-0.1.0-noarch.spk"
    exit 1
fi

if [ ! -f "$SPK_FILE" ]; then
    error "File not found: $SPK_FILE"
    exit 1
fi

echo "================================================================="
echo "         SPK VALIDATION REPORT - DSM 7.1 Compatibility"
echo "================================================================="
echo ""
info "File: $SPK_FILE"
echo ""

# Check file type
FILE_TYPE=$(file -b "$SPK_FILE")
info "File type: $FILE_TYPE"

if [[ ! "$FILE_TYPE" =~ "tar" ]] && [[ ! "$FILE_TYPE" =~ "Synology" ]]; then
    error "Not a valid TAR archive (should be uncompressed tar)"
    error "SPK files must be POSIX tar archives without gzip/bzip2 compression"
    exit 1
else
    success "File is a TAR archive"
fi

# Check if it's compressed (it shouldn't be!)
if [[ "$FILE_TYPE" =~ "gzip" ]] || [[ "$FILE_TYPE" =~ "compressed" ]]; then
    error "SPK is compressed! DSM 7 requires UNCOMPRESSED tar archives"
    error "Rebuild with: tar -cf (no -z or -j flags)"
    exit 1
else
    success "Archive is uncompressed (correct)"
fi

echo ""
echo "=== EXTRACTING SPK ==="

# Extract to temp directory
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

if ! tar -xf "$SPK_FILE" -C "$TMPDIR" 2>&1; then
    error "Failed to extract SPK - archive may be corrupted"
    exit 1
fi

success "SPK extracted to: $TMPDIR"
echo ""

# Check structure
echo "=== STRUCTURE VALIDATION ==="

ISSUES=0

# Required files
if [ -f "$TMPDIR/INFO" ]; then
    success "INFO file present"
else
    error "INFO file MISSING (REQUIRED)"
    ((ISSUES++))
fi

if [ -f "$TMPDIR/package.tgz" ]; then
    success "package.tgz present"
    
    # Verify package.tgz is actually compressed
    PKG_TYPE=$(file -b "$TMPDIR/package.tgz")
    if [[ "$PKG_TYPE" =~ "gzip" ]]; then
        success "package.tgz is gzip compressed (correct)"
    else
        warn "package.tgz is not gzipped: $PKG_TYPE"
    fi
else
    error "package.tgz MISSING (REQUIRED)"
    ((ISSUES++))
fi

# Icons
if [ -f "$TMPDIR/PACKAGE_ICON.PNG" ]; then
    success "PACKAGE_ICON.PNG present"
    
    # Check icon size (DSM 7 requires 64x64)
    if command -v identify >/dev/null 2>&1; then
        ICON_SIZE=$(identify -format "%wx%h" "$TMPDIR/PACKAGE_ICON.PNG" 2>/dev/null)
        if [ "$ICON_SIZE" = "64x64" ]; then
            success "PACKAGE_ICON.PNG is 64x64 (DSM 7 standard)"
        else
            warn "PACKAGE_ICON.PNG is $ICON_SIZE (DSM 7 recommends 64x64)"
        fi
    fi
else
    warn "PACKAGE_ICON.PNG missing (recommended)"
fi

# conf directory
if [ -d "$TMPDIR/conf" ]; then
    success "conf/ directory present"
else
    error "conf/ directory MISSING (REQUIRED for DSM 7)"
    ((ISSUES++))
fi

if [ -f "$TMPDIR/conf/privilege" ]; then
    success "conf/privilege present"
else
    error "conf/privilege MISSING (REQUIRED for DSM 7)"
    ((ISSUES++))
fi

# scripts - CRITICAL: Must be a DIRECTORY, not a tar file!
if [ -d "$TMPDIR/scripts" ]; then
    success "scripts/ is a DIRECTORY (correct)"
    
    # List scripts
    echo ""
    info "Scripts found:"
    ls -lh "$TMPDIR/scripts/" | tail -n +2 | while read line; do
        echo "  $line"
    done
    
    # Check script permissions
    echo ""
    info "Checking script permissions:"
    NON_EXEC=0
    for script in "$TMPDIR/scripts/"*; do
        if [ -f "$script" ]; then
            SCRIPT_NAME=$(basename "$script")
            if [ -x "$script" ]; then
                success "$SCRIPT_NAME is executable"
            else
                error "$SCRIPT_NAME is NOT executable (will cause installation failure)"
                ((ISSUES++))
                ((NON_EXEC++))
            fi
        fi
    done
    
    if [ $NON_EXEC -gt 0 ]; then
        error "Found $NON_EXEC non-executable scripts"
        info "All scripts must have mode 755 (rwxr-xr-x)"
    fi
    
elif [ -f "$TMPDIR/scripts" ]; then
    error "scripts is a FILE, not a DIRECTORY!"
    error "This is a CRITICAL error that causes error 313"
    error "Scripts must be included as a directory: scripts/"
    error "NOT as a tar archive file: scripts"
    ((ISSUES++))
    
    # Try to extract it to show what's inside
    info "Attempting to extract scripts archive..."
    mkdir -p "$TMPDIR/scripts_extracted"
    if tar -xf "$TMPDIR/scripts" -C "$TMPDIR/scripts_extracted" 2>/dev/null; then
        warn "Scripts archive contains:"
        ls -la "$TMPDIR/scripts_extracted/"
    fi
else
    warn "scripts/ directory missing (required for startable packages)"
fi

echo ""
echo "=== INFO FILE VALIDATION ==="

if [ -f "$TMPDIR/INFO" ]; then
    # Check mandatory fields for DSM 7
    REQUIRED_FIELDS=("package" "version" "os_min_ver" "description" "arch" "maintainer")
    
    for field in "${REQUIRED_FIELDS[@]}"; do
        if grep -q "^${field}=" "$TMPDIR/INFO"; then
            VALUE=$(grep "^${field}=" "$TMPDIR/INFO" | cut -d'=' -f2- | tr -d '"')
            success "$field=\"$VALUE\""
        else
            error "$field MISSING (REQUIRED)"
            ((ISSUES++))
        fi
    done
    
    # Check os_min_ver for DSM 7 compatibility
    OS_MIN=$(grep "^os_min_ver=" "$TMPDIR/INFO" 2>/dev/null | cut -d'"' -f2)
    if [[ "$OS_MIN" =~ ^7\. ]]; then
        success "os_min_ver=$OS_MIN (DSM 7 compatible)"
    elif [[ "$OS_MIN" =~ ^[0-9] ]]; then
        error "os_min_ver=$OS_MIN (NOT compatible with DSM 7)"
        error "DSM 7 requires os_min_ver >= 7.0-40000"
        ((ISSUES++))
    else
        error "os_min_ver not set or invalid"
        ((ISSUES++))
    fi
    
    echo ""
    info "Complete INFO file contents:"
    echo "----------------------------------------"
    cat "$TMPDIR/INFO"
    echo "----------------------------------------"
fi

echo ""
echo "=== conf/privilege VALIDATION ==="

if [ -f "$TMPDIR/conf/privilege" ]; then
    echo ""
    info "conf/privilege contents:"
    echo "----------------------------------------"
    cat "$TMPDIR/conf/privilege"
    echo "----------------------------------------"
    echo ""
    
    # Validate JSON
    if command -v jq >/dev/null 2>&1; then
        if jq empty "$TMPDIR/conf/privilege" 2>/dev/null; then
            success "Valid JSON format"
        else
            error "Invalid JSON format in conf/privilege"
            ((ISSUES++))
        fi
    fi
    
    # Check for run-as
    if grep -q '"run-as"' "$TMPDIR/conf/privilege"; then
        RUN_AS=$(grep -o '"run-as"[[:space:]]*:[[:space:]]*"[^"]*"' "$TMPDIR/conf/privilege" | cut -d'"' -f4)
        if [ "$RUN_AS" = "root" ]; then
            error "run-as: root is FORBIDDEN in DSM 7"
            error "Change to: \"run-as\": \"package\""
            ((ISSUES++))
        else
            success "run-as: $RUN_AS (valid)"
        fi
    else
        error "Missing \"run-as\" declaration"
        error "Required for DSM 7: {\"defaults\": {\"run-as\": \"package\"}}"
        ((ISSUES++))
    fi
fi

echo ""
echo "=== PERMISSIONS & OWNERSHIP CHECK ==="

# Check ownership of files in the SPK
info "Checking file ownership and permissions in SPK..."
echo ""

tar -tvf "$SPK_FILE" | head -20 | while read -r line; do
    # Parse tar output: permissions uid/gid size date time name
    PERMS=$(echo "$line" | awk '{print $1}')
    OWNER=$(echo "$line" | awk '{print $2}')
    SIZE=$(echo "$line" | awk '{print $3}')
    NAME=$(echo "$line" | awk '{print $NF}')
    
    echo "  $PERMS $OWNER $NAME"
    
    # Check for root ownership (potential issue)
    if echo "$OWNER" | grep -q "root/root" || echo "$OWNER" | grep -q "0/0"; then
        warn "  └─ File owned by root - may cause error 313"
    fi
    
    # Check if scripts are executable
    if [[ "$NAME" =~ scripts/.* ]] && [[ ! "$PERMS" =~ x ]]; then
        warn "  └─ Script not executable - will cause installation failure"
    fi
done

echo ""
echo "=== PACKAGE.TGZ CONTENTS ==="

if [ -f "$TMPDIR/package.tgz" ]; then
    info "Listing package.tgz contents (first 20 files):"
    echo ""
    tar -tzf "$TMPDIR/package.tgz" 2>/dev/null | head -20 | while read -r file; do
        echo "  $file"
    done
    
    FILE_COUNT=$(tar -tzf "$TMPDIR/package.tgz" 2>/dev/null | wc -l)
    info "Total files in package.tgz: $FILE_COUNT"
fi

echo ""
echo "================================================================="
echo "                    VALIDATION SUMMARY"
echo "================================================================="
echo ""

if [ $ISSUES -eq 0 ]; then
    success "All validations PASSED!"
    echo ""
    info "This SPK should install correctly on DSM 7.1+"
    echo ""
    info "Next steps:"
    echo "  1. Test installation:"
    echo "     sudo synopkg install $SPK_FILE"
    echo ""
    echo "  2. Monitor logs during installation:"
    echo "     tail -f /var/log/synopkg.log"
    echo ""
else
    error "Found $ISSUES issue(s) that may prevent installation"
    echo ""
    info "Common error codes and their causes:"
    echo ""
    echo "  Error 263 (invalid format):"
    echo "    - SPK is compressed (use tar -cf, not tar -czf)"
    echo "    - Missing INFO file"
    echo "    - Missing conf/privilege"
    echo "    - os_min_ver < 7.0-40000"
    echo ""
    echo "  Error 313 (file attributes):"
    echo "    - scripts/ is a tar file instead of directory"
    echo "    - Scripts not executable (need chmod 755)"
    echo "    - Files owned by root or wrong UID"
    echo "    - Incorrect permissions on conf/privilege"
    echo ""
    info "Fix the issues above and rebuild the SPK"
fi

echo ""
echo "=== ADDITIONAL DEBUGGING COMMANDS ==="
echo ""
echo "Extract SPK manually:"
echo "  mkdir /tmp/spk-debug && tar -xvf $SPK_FILE -C /tmp/spk-debug"
echo ""
echo "Check SPK installation logs:"
echo "  tail -f /var/log/synopkg.log"
echo "  tail -f /var/log/messages"
echo ""
echo "Query package info before installing:"
echo "  synopkg query $SPK_FILE"
echo ""
echo "Install with verbose output:"
echo "  sudo synopkg install $SPK_FILE"
echo ""

exit $ISSUES