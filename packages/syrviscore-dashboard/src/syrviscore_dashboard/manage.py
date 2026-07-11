"""Container-safe mutations.

What a socket-holding container can safely do:
- **Core lifecycle** via the Docker SDK on individual containers (start/stop/restart
  of the ``CORE_SERVICES`` allowlist). We deliberately do NOT call
  ``DockerManager.start_core_services()`` — that shells the compose v1 binary and
  tries to (re)create the host macvlan shim, which needs host root. Recreating a
  missing container is therefore surfaced as an SSH action, not attempted here.
- **Layer 2 lifecycle** via ``ServiceManager`` (which shells ``docker compose`` +
  ``git``), gated behind ``ENABLE_L2_MUTATIONS`` and the ``WITH_L2_TOOLS`` image.
"""

from typing import Tuple

from . import docker_util

CORE_ACTIONS = ("start", "stop", "restart")
LAYER2_ACTIONS = ("start", "stop", "restart", "update")


class ManagementDisabled(Exception):
    """A mutation was attempted while its scope is disabled by config."""


def core_lifecycle(service: str, action: str) -> Tuple[bool, str]:
    """Start/stop/restart a single core container via the Docker SDK."""
    if service not in docker_util.CORE_SERVICES:
        raise docker_util.NotManaged(service)
    if action not in CORE_ACTIONS:
        raise ValueError("unknown action: {}".format(action))

    container = docker_util.get_managed_container(service)
    if action == "start":
        container.start()
    elif action == "stop":
        container.stop(timeout=10)
    else:  # restart
        container.restart(timeout=10)
    return True, "{} {}".format(service, "restarted" if action == "restart" else action + "ed")


def _service_manager():
    from syrviscore.service_manager import ServiceManager

    return ServiceManager()


def layer2_action(name: str, action: str, settings) -> Tuple[bool, str]:
    if not settings.enable_l2_mutations:
        raise ManagementDisabled("Layer 2 mutations are disabled (ENABLE_L2_MUTATIONS=false)")
    if action not in LAYER2_ACTIONS:
        raise ValueError("unknown action: {}".format(action))

    mgr = _service_manager()
    if action == "start":
        return mgr.start(name)
    if action == "stop":
        return mgr.stop(name)
    if action == "update":
        return mgr.update(name)
    # restart = stop then start; report the failing step
    ok, msg = mgr.stop(name)
    if not ok:
        return ok, msg
    return mgr.start(name)


def layer2_add(source: str, start: bool, settings) -> Tuple[bool, str]:
    if not settings.enable_l2_mutations:
        raise ManagementDisabled("Layer 2 mutations are disabled (ENABLE_L2_MUTATIONS=false)")
    return _service_manager().add(source, start=start)


def layer2_remove(name: str, purge: bool, settings) -> Tuple[bool, str]:
    if not settings.enable_l2_mutations:
        raise ManagementDisabled("Layer 2 mutations are disabled (ENABLE_L2_MUTATIONS=false)")
    return _service_manager().remove(name, purge=purge)
