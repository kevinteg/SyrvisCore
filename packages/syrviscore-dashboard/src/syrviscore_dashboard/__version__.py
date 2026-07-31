"""SyrvisCore Dashboard version information."""

# INDEPENDENT of the service version (owner decision 2026-07-31): the dashboard
# advances only when the dashboard package itself changes, NOT on every service
# release. The invariant is running == PINNED — this equals the compose image pin
# tag (syrviscore.compose DEFAULT_DOCKER_IMAGES["dashboard"]). Bump it by hand on
# a real dashboard change and re-pin the image; a service-only release leaves it
# alone. (Was force-synced to the service version until 2026-07-31, which drifted
# the package version ahead of the correctly-pinned 0.5.1 image.)
__version__ = "0.5.1"
