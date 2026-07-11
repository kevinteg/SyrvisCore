"""Framework-agnostic Docker helpers shared by logs + management.

Resolves containers by name but only ones SyrvisCore is allowed to touch: the
core stack, the dashboard itself, or anything in a ``syrvis*`` compose project.
Raises plain exceptions (no FastAPI coupling) so the library stays reusable; the
API layer maps them to HTTP status codes.
"""

import re

from syrviscore.docker_manager import DockerManager

SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
CORE_SERVICES = tuple(DockerManager.CORE_SERVICES)  # traefik, portainer, cloudflared
DASHBOARD_CONTAINER = "syrviscore-dashboard"
_MANAGED_PROJECT_PREFIX = "syrvis"


class DockerUnavailable(Exception):
    """The Docker daemon could not be reached."""


class ContainerNotFound(Exception):
    """No container with that name exists."""


class NotManaged(Exception):
    """The container exists but is not a SyrvisCore-managed container."""


class InvalidName(Exception):
    """The requested name failed the safe-name check."""


def client():
    import docker

    try:
        c = docker.from_env()
        c.ping()
        return c
    except Exception as exc:  # noqa: BLE001
        raise DockerUnavailable(str(exc))


def is_managed(name: str, project_label: str) -> bool:
    return (
        name in CORE_SERVICES
        or name == DASHBOARD_CONTAINER
        or (project_label or "").startswith(_MANAGED_PROJECT_PREFIX)
    )


def get_managed_container(name: str):
    """Return the docker container for ``name`` if SyrvisCore may manage it."""
    if not SAFE_NAME.match(name or ""):
        raise InvalidName(name)

    import docker

    c = client()
    try:
        container = c.containers.get(name)
    except docker.errors.NotFound:
        raise ContainerNotFound(name)
    except DockerUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DockerUnavailable(str(exc))

    if not is_managed(name, container.labels.get("com.docker.compose.project", "")):
        raise NotManaged(name)
    return container
