"""SyrvisCore Dashboard version information."""

# INDEPENDENT of the service version (owner decision 2026-07-31): the dashboard
# advances only when the dashboard package itself changes, NOT on every service
# release. The invariant is running == PINNED — this equals the compose image pin
# tag (syrviscore.compose DEFAULT_DOCKER_IMAGES["dashboard"]). Bump it by hand on
# a real dashboard change and re-pin the image; a service-only release leaves it
# alone. (Was force-synced to the service version until 2026-07-31, which drifted
# the package version ahead of the correctly-pinned 0.5.1 image — 0.5.2-0.5.6 are
# published ghcr tags from that era, so a real bump lands past them on the first
# free tag rather than colliding with an already-pushed image.)
#
# 0.5.8 = the /api/summary endpoint (syrvis-summary/v1). 0.5.7 is already a
# PUBLISHED ghcr tag (CI publishes on merge-to-main), so shipping new content
# needs the next free tag — re-pushing 0.5.7 would change what an existing pin
# resolves to.
__version__ = "0.5.8"
