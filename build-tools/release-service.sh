#!/bin/bash
# release-service.sh - Create GitHub release for SyrvisCore service package
# This creates a release with the service wheel for installation via syrvisctl

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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
SERVICE_DIR="$PROJECT_ROOT/packages/syrviscore"

log_info "SyrvisCore Service Release"

# Check for gh CLI
if ! command -v gh &> /dev/null; then
    log_error "GitHub CLI (gh) is required but not installed"
    log_info "Install with: brew install gh"
    exit 1
fi

# Check if authenticated
if ! gh auth status &> /dev/null; then
    log_error "Not authenticated with GitHub CLI"
    log_info "Run: gh auth login"
    exit 1
fi

# Read version
VERSION=$(grep '^__version__' "$SERVICE_DIR/src/syrviscore/__version__.py" | cut -d'"' -f2)
TAG="v${VERSION}"
log_info "Version: $VERSION"
log_info "Tag: $TAG"

# Check if service wheel exists
WHEEL_FILE=$(ls "$PROJECT_ROOT/dist"/syrviscore-[0-9]*.whl 2>/dev/null | head -1)

if [ -z "$WHEEL_FILE" ]; then
    log_warn "Service wheel not found, building..."
    "$SCRIPT_DIR/build-service.sh"
    WHEEL_FILE=$(ls "$PROJECT_ROOT/dist"/syrviscore-[0-9]*.whl 2>/dev/null | head -1)
fi

if [ -z "$WHEEL_FILE" ]; then
    log_error "Failed to find or build service wheel"
    exit 1
fi

log_info "Wheel: $(basename "$WHEEL_FILE")"

# Check for config.yaml
CONFIG_FILE="$PROJECT_ROOT/build/config.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
    log_warn "build/config.yaml not found, release will not include it"
    CONFIG_FILE=""
fi

# Check for .env.template
ENV_TEMPLATE="$PROJECT_ROOT/.env.template"
if [ ! -f "$ENV_TEMPLATE" ]; then
    log_warn ".env.template not found, release will not include it"
    ENV_TEMPLATE=""
fi

# Check if tag already exists
if gh release view "$TAG" &> /dev/null; then
    log_warn "Release $TAG already exists"
    read -p "Delete and recreate? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Deleting existing release..."
        gh release delete "$TAG" --yes
        git push origin --delete "$TAG" 2>/dev/null || true
    else
        log_info "Aborted"
        exit 0
    fi
fi

# Create release
log_info "Creating GitHub release..."

# Build release notes
RELEASE_NOTES="## SyrvisCore Service v${VERSION}

### Installation

Install via syrvisctl:
\`\`\`bash
syrvisctl install ${VERSION}
\`\`\`

Or install latest:
\`\`\`bash
syrvisctl install
\`\`\`

### Included Files
- \`syrviscore-${VERSION}-py3-none-any.whl\` - Service package"

if [ -n "$CONFIG_FILE" ]; then
    RELEASE_NOTES+="
- \`config.yaml\` - Docker image versions"
fi

if [ -n "$ENV_TEMPLATE" ]; then
    RELEASE_NOTES+="
- \`.env.template\` - Configuration template"
fi

# Collect files to upload
RELEASE_FILES=("$WHEEL_FILE")
[ -n "$CONFIG_FILE" ] && RELEASE_FILES+=("$CONFIG_FILE")
[ -n "$ENV_TEMPLATE" ] && RELEASE_FILES+=("$ENV_TEMPLATE")

log_info "Creating release with ${#RELEASE_FILES[@]} files..."

gh release create "$TAG" \
    --title "SyrvisCore Service v${VERSION}" \
    --notes "$RELEASE_NOTES" \
    "${RELEASE_FILES[@]}"

log_success "=========================================="
log_success "GitHub release created successfully!"
log_success "=========================================="
log_info "Release: $TAG"
log_info "URL: https://github.com/kevinteg/SyrvisCore/releases/tag/$TAG"
log_info ""
log_info "Users can now install with:"
log_info "  syrvisctl install $VERSION"

exit 0
