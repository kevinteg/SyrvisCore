#!/bin/bash
# build-tarball.sh - Build the SyrvisCore devkit tarball (the dev-loop artifact)
#
# The devkit bundles everything needed to stand up a complete dev install on
# the NAS without Package Center or GitHub:
#   bootstrap.sh                       - the installer (see build-tools/bootstrap.sh)
#   wheels/                            - manager wheel + pinned dependency wheels
#   syrviscore-<version>-*.whl         - service wheel
#   config.yaml                        - Docker image versions (if build/config.yaml exists)
#   SHA256SUMS                         - integrity manifest (verified by bootstrap.sh)
#
# Dev loop: make tarball && make nas-dev SSH_HOST=<nas>

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$PROJECT_ROOT/dist"
STAGE_DIR="$PROJECT_ROOT/build-tarball-tmp/devkit"

SERVICE_VERSION=$(grep '^__version__' \
    "$PROJECT_ROOT/packages/syrviscore/src/syrviscore/__version__.py" | cut -d'"' -f2)
TARBALL_NAME="syrviscore-devkit-${SERVICE_VERSION}.tar.gz"

log_info "Building devkit for service version ${SERVICE_VERSION}"

# === Build the wheels ===
log_info "Building manager wheel + bundled dependencies..."
"$SCRIPT_DIR/build-manager.sh" >/dev/null

log_info "Building service wheel..."
"$SCRIPT_DIR/build-service.sh" >/dev/null

SERVICE_WHEEL=$(ls "$DIST_DIR"/syrviscore-[0-9]*.whl 2>/dev/null | head -1)
DEPS_DIR="$DIST_DIR/manager-deps"
[ -n "$SERVICE_WHEEL" ] || { log_error "Service wheel not found in $DIST_DIR"; exit 1; }
[ -d "$DEPS_DIR" ] || { log_error "Manager deps not found at $DEPS_DIR"; exit 1; }

# === Stage the devkit ===
log_info "Staging devkit contents..."
rm -rf "$PROJECT_ROOT/build-tarball-tmp"
mkdir -p "$STAGE_DIR/wheels"

cp "$SCRIPT_DIR/bootstrap.sh" "$STAGE_DIR/bootstrap.sh"
chmod 755 "$STAGE_DIR/bootstrap.sh"
cp "$DEPS_DIR"/*.whl "$STAGE_DIR/wheels/"
cp "$SERVICE_WHEEL" "$STAGE_DIR/"

if [ -f "$PROJECT_ROOT/build/config.yaml" ]; then
    cp "$PROJECT_ROOT/build/config.yaml" "$STAGE_DIR/config.yaml"
    log_info "  Included build/config.yaml"
else
    log_info "  No build/config.yaml (service will use built-in defaults)"
fi

# === Integrity manifest ===
log_info "Generating SHA256SUMS..."
cd "$STAGE_DIR"
FILES=$(find . -type f ! -name SHA256SUMS ! -name bootstrap.log | sed 's|^\./||' | LC_ALL=C sort)
if command -v sha256sum >/dev/null 2>&1; then
    echo "$FILES" | xargs sha256sum > SHA256SUMS
else
    echo "$FILES" | xargs shasum -a 256 > SHA256SUMS
fi

# === Create the tarball (deterministic when GNU tar is available) ===
log_info "Creating ${TARBALL_NAME}..."
mkdir -p "$DIST_DIR"
cd "$PROJECT_ROOT/build-tarball-tmp"

EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$PROJECT_ROOT" log -1 --format=%ct 2>/dev/null || echo 0)}"
TAR_BIN="tar"
command -v gtar >/dev/null 2>&1 && TAR_BIN="gtar"

if "$TAR_BIN" --version 2>/dev/null | grep -q "GNU tar"; then
    "$TAR_BIN" --sort=name --owner=0 --group=0 --numeric-owner \
        --mtime="@${EPOCH}" \
        -czf "$DIST_DIR/$TARBALL_NAME" devkit
else
    # bsdtar (macOS): not bit-reproducible; SHA256SUMS inside still covers
    # content integrity. COPYFILE_DISABLE prevents AppleDouble ._* leakage.
    COPYFILE_DISABLE=1 "$TAR_BIN" -czf "$DIST_DIR/$TARBALL_NAME" devkit
    log_info "  (bsdtar: archive is not bit-reproducible; install gnu-tar for that)"
fi

cd "$PROJECT_ROOT"
rm -rf "$PROJECT_ROOT/build-tarball-tmp"

SIZE=$(du -h "$DIST_DIR/$TARBALL_NAME" | cut -f1)
if command -v sha256sum >/dev/null 2>&1; then
    DIGEST=$(sha256sum "$DIST_DIR/$TARBALL_NAME" | cut -d' ' -f1)
else
    DIGEST=$(shasum -a 256 "$DIST_DIR/$TARBALL_NAME" | cut -d' ' -f1)
fi

log_success "=========================================="
log_success "Devkit built: $TARBALL_NAME ($SIZE)"
log_success "=========================================="
log_info "Location: $DIST_DIR/$TARBALL_NAME"
log_info "SHA-256:  $DIGEST"
log_info ""
log_info "Deploy to the NAS:"
log_info "  make nas-dev SSH_HOST=<nas-ip>"
log_info "Or manually:"
log_info "  scp $DIST_DIR/$TARBALL_NAME <user>@<nas>:/tmp/"
log_info "  ssh <user>@<nas> 'mkdir -p ~/syrviscore-devkit && tar xzf /tmp/$TARBALL_NAME --strip-components=1 -C ~/syrviscore-devkit && cd ~/syrviscore-devkit && ./bootstrap.sh'"

exit 0
