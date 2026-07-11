"""Catalog of privileged operations the dashboard cannot run from a container.

Host-root work (docker group, macvlan shim, boot hook, version management) is
never executed here. Instead the API hands the operator the exact ``ssh <target>
'…'`` command to run — the same "print the exact command when there's no TTY"
philosophy the CLI's elevation path uses.
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SshAction:
    id: str
    title: str
    description: str
    command: str  # the remote command (without the ssh wrapper)
    why_privileged: str


CATALOG: List[SshAction] = [
    SshAction(
        id="setup",
        title="Run privileged setup",
        description="Interactive setup: docker group, socket perms, macvlan shim, boot hook.",
        command="sudo syrvis setup",
        why_privileged="creates the docker group + host macvlan shim + rc.d boot hook (host root)",
    ),
    SshAction(
        id="verify-fix",
        title="Verify & auto-fix",
        description="Run the health/drift report and apply sanctioned remediations.",
        command="sudo syrvis verify --fix",
        why_privileged="reconciles drift (compose up + shim) and fixes perms — self-elevates",
    ),
    SshAction(
        id="core-reconcile",
        title="Reconcile the core stack",
        description="Recreate core containers to match compose (pull + up -d) and the shim.",
        command="sudo syrvis start",
        why_privileged="compose up + macvlan shim creation need host root",
    ),
    SshAction(
        id="restart-core",
        title="Restart the whole core stack",
        description="docker-compose restart of the full core stack (regenerates traefik files).",
        command="syrvis restart",
        why_privileged="uses the compose binary + rewrites the traefik config dir",
    ),
    SshAction(
        id="install-version",
        title="Install a service version",
        description="Download + install a service version from GitHub (manager op).",
        command="sudo syrvisctl install",
        why_privileged="runs in the SPK manager venv, outside this container",
    ),
    SshAction(
        id="rollback",
        title="Roll back the service version",
        description="Switch the active version back (full restore).",
        command="sudo syrvisctl rollback",
        why_privileged="manager op in the SPK venv; destructive",
    ),
]

_BY_ID = {a.id: a for a in CATALOG}


def render(settings, action: SshAction) -> str:
    # ssh_target_effective resolves the placeholder alias to the concrete NAS_IP
    # when known, so the hint works without assuming the operator's ssh config.
    return "ssh {} '{}'".format(settings.ssh_target_effective, action.command)


def catalog(settings) -> List[dict]:
    return [
        {
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "ssh_command": render(settings, a),
            "why_privileged": a.why_privileged,
        }
        for a in CATALOG
    ]


def get(action_id: str) -> Optional[SshAction]:
    return _BY_ID.get(action_id)
