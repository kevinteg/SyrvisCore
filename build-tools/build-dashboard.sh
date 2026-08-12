#!/usr/bin/env sh
# Build (and optionally push) the SyrvisCore dashboard container image.
#
# Usage:
#   ./build-tools/build-dashboard.sh                # build ghcr.io/kevinteg/syrviscore-dashboard:<ver>
#   WITH_L2_TOOLS=true ./build-tools/build-dashboard.sh   # include docker-cli + compose + git
#   PUSH=1 ./build-tools/build-dashboard.sh          # also push to the registry
#   IMAGE=my/registry ./build-tools/build-dashboard.sh    # override the image name
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

# The image is tagged at the DASHBOARD package's own version — the compose pin
# (compose.py DEFAULT_DOCKER_IMAGES) references the same string, so the two
# agree by construction. The dashboard versions independently of the service:
# it may ship a patch (e.g. 0.5.7) while the core sits at 0.5.6, and tagging at
# the core version here would silently overwrite or collide with a published tag.
VERSION=$(grep '^__version__' \
  "$REPO_ROOT/packages/syrviscore-dashboard/src/syrviscore_dashboard/__version__.py" \
  | cut -d'"' -f2)

IMAGE="${IMAGE:-ghcr.io/kevinteg/syrviscore-dashboard}"
WITH_L2_TOOLS="${WITH_L2_TOOLS:-false}"
TAG="$IMAGE:$VERSION"

echo "[build-dashboard] building $TAG (WITH_L2_TOOLS=$WITH_L2_TOOLS)"
docker build \
  -f "$REPO_ROOT/packages/syrviscore-dashboard/Dockerfile" \
  --build-arg "WITH_L2_TOOLS=$WITH_L2_TOOLS" \
  -t "$TAG" \
  -t "$IMAGE:latest" \
  "$REPO_ROOT"

if [ "${PUSH:-0}" = "1" ]; then
  echo "[build-dashboard] pushing $TAG and $IMAGE:latest"
  docker push "$TAG"
  docker push "$IMAGE:latest"
fi

echo "[build-dashboard] done: $TAG"
