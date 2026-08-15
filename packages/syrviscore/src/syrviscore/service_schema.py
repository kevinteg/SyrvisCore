"""
Service definition schema for Layer 2 services.

Each service is defined by a syrvis-service.yaml file in its git repository.
This module provides the dataclasses for parsing and validating these definitions.

SECURITY: this schema is the trust boundary for third-party repositories.
A syrvis-service.yaml is attacker-controlled input that ends up as filesystem
paths (services/<name>, data/<name>, compose/<name>.yaml, a Traefik-watched
config file) and as a docker-compose file that root starts. Every field is
therefore strictly validated here — names are constrained to a safe charset,
host mounts are restricted to the service's own data directory, and unknown
keys are rejected outright.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

import yaml

from syrviscore.errors import SyrvisError

from . import exposure as exposure_mod

# Safe identifier: what we allow as a service/container/network name.
# Used directly as a path component and a compose project name.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Single DNS label for Traefik subdomains
SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Domain override: dot-separated DNS labels (≥2 labels), each matching SUBDOMAIN_RE.
# Used to allow a service to route on a zone other than the instance DOMAIN.
# Example: "example.com", "photos.example.com"
DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,}[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# design/26: the ONLY shape an app location may take — a DSM volume ROOT.
# No subpaths, no trailing slash, no '..' (the charset admits none of them).
# Parse-time validation is regex-ONLY (a laptop validating a declaration has no
# /volumeN mounts); the is-it-really-a-mounted-volume check happens at INSTALL
# time (paths.is_mounted_volume), alongside the authorship gate.
LOCATION_RE = re.compile(r"^/volume\d+$")

# Names owned by the core stack — a third-party service may not impersonate
# or replace them.
# "deployments"/"state" are platform-owned subtrees of data/ — a service with
# one of these names would overlay them (its data dir is data/<name>), and
# `service remove --purge` would then rmtree the platform's own records.
# Reserved so the collision can never exist.
RESERVED_NAMES = frozenset(
    {
        "traefik",
        "portainer",
        "cloudflared",
        "proxy",
        "syrvis-macvlan",
        "deployments",
        "state",
    }
)

ALLOWED_RESTART = frozenset({"no", "always", "on-failure", "unless-stopped"})

# The complete set of keys a syrvis-service.yaml may contain. Anything else
# is rejected — this is what stops a manifest from smuggling compose options
# we never audited (privileged, cap_add, devices, network_mode, ...).
ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "name",
        "version",
        "image",
        "description",
        "author",
        "homepage",
        "container_name",
        "traefik",
        "environment",
        "command",
        "env_file",
        "volumes",
        "ports",
        "networks",
        "depends_on",
        "config_templates",
        "restart",
        "healthcheck",
        "resources",
        # Orchestration keys: meaningful ONLY in config/services.d declarations
        # (`enabled` gates whether reconcile runs it; `critical` affects health
        # severity). They are accepted anywhere for round-tripping but have NO
        # effect in a git/catalog manifest: installs ignore them, materialized
        # manifests strip them, and the dual-written declaration preserves the
        # OPERATOR's existing orchestration — so an untrusted repo can never
        # declare itself critical or toggle its own enablement.
        "enabled",
        "critical",
        # The privileged tier selector (design/22). "" (default) | "infra".
        # `tier: infra` unlocks a fixed read-only host-mount allowlist for
        # operator-authored first-party exporters; the AUTHORSHIP gate (only a
        # services.d/deploy declaration may set it — never a git/image/catalog
        # service) is enforced at install time in _install_from_definition.
        "tier",
        # The app's declared home volume (design/26). "" (default) = the legacy
        # layout under SYRVIS_HOME (data/<name>); "/volume<N>" = the v2 app-home
        # layout <location>/syrviscore/apps/<name>/{data,config,secrets,logs}.
        # Validated ^/volume\d+$ at parse; mount-checked + AUTHORSHIP-gated at
        # install time (only a services.d/deploy declaration may set it — a
        # git/image/catalog manifest can never redirect its home onto a host path).
        "location",
        # Declared one-shot tasks ({name: {command: [...]}}) the operator can
        # run inside the service's own container via `syrvis service task` —
        # e.g. a DB bootstrap. Same trust class + audit rules as `command`.
        "tasks",
        # Lifecycle container hooks ({event: task_name}): run a DECLARED task
        # at a transition. Only pre-stop / post-start (the two events where the
        # container is verifiably running); the referenced task must exist in
        # `tasks:`, so this selects WHICH audited argv runs WHEN — never code.
        "hooks",
        # Graceful-shutdown knobs ({stop_timeout, priority}): how long the
        # container gets to exit cleanly (SIGTERM grace; DBs set 120+) and its
        # stop band during instance shutdown (lower stops first; DBs high so
        # they stop after their consumers).
        "shutdown",
        # Optional per-service metric panels ({panels: [{title, expr, ...}]})
        # consumed by the dashboard generator (`syrvis dashboard generate`) to
        # add domain-specific rows/panels for this service. Pure PRESENTATION
        # metadata: the exprs only ever land in a Grafana dashboard JSON a human
        # provisions — SyrvisCore never executes them and they never reach the
        # shell/compose — so this block grants NO authority (unlike command/
        # tasks/volumes) and, uniquely, permits '$' (the ${container} token).
        "dashboard",
    }
)

# tasks sub-schema: each task is exactly one audited exec-form argv.
ALLOWED_TASK_KEYS = frozenset({"command"})
MAX_TASKS = 16

# hooks sub-schema: event -> declared task name. Container hooks are docker
# exec into a RUNNING container, so only these two events qualify.
CONTAINER_HOOK_EVENTS = frozenset({"pre-stop", "post-start"})
MAX_HOOKS = 8

# shutdown sub-schema: graceful-stop knobs for the instance shutdown path.
ALLOWED_SHUTDOWN_KEYS = frozenset({"stop_timeout", "priority"})
SHUTDOWN_STOP_TIMEOUT_RANGE = (5, 300)
SHUTDOWN_PRIORITY_RANGE = (0, 100)

# dashboard sub-schema: optional presentation-only metric panels for the
# generated Grafana dashboard (see dashboard.py). NOT a trust boundary — the
# strings only ever render into a dashboard JSON, never executed — so the checks
# here are for well-formedness (bounded counts/lengths, no control chars), not
# safety. '$' IS allowed (the ${container} substitution token).
ALLOWED_DASHBOARD_KEYS = frozenset({"panels", "about", "links"})
ALLOWED_PANEL_KEYS = frozenset({"title", "expr", "kind", "unit"})
ALLOWED_PANEL_KINDS = frozenset({"stat", "timeseries", "table", "gauge"})
ALLOWED_LINK_KEYS = frozenset({"title", "url"})
MAX_DASHBOARD_PANELS = 12
MAX_PANEL_TITLE = 120
MAX_PANEL_EXPR = 512
MAX_ABOUT = 1000
MAX_LINKS = 8
MAX_LINK_TITLE = 80
MAX_LINK_URL = 300
UNIT_RE = re.compile(r"^[A-Za-z0-9/%_.-]{0,32}$")
_LINK_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

ALLOWED_TIERS = frozenset({"", "infra"})

# design/22: the ONLY absolute host mounts an `infra`-tier service may declare,
# each READ-ONLY. Everything else stays refused even for infra. node_exporter
# needs /proc,/sys,/ ; docker-socket-proxy needs the docker socket (and NOTHING
# else mounts it — see design/22 §3).
INFRA_HOST_MOUNTS = frozenset({"/proc", "/sys", "/", "/var/run/docker.sock"})

# healthcheck sub-schema (audited subset of compose's healthcheck)
ALLOWED_HEALTHCHECK_KEYS = frozenset({"test", "interval", "timeout", "retries", "start_period"})
DURATION_RE = re.compile(r"^\d+(s|m|h)$")

# resources sub-schema (service-level compose limits)
ALLOWED_RESOURCE_KEYS = frozenset({"cpus", "memory"})
CPUS_RE = re.compile(r"^\d+(\.\d+)?$")
MEMORY_RE = re.compile(r"^\d+(b|k|m|g)$", re.IGNORECASE)


class ServiceValidationError(SyrvisError, ValueError):
    """A syrvis-service.yaml failed validation (unsafe or malformed).

    Also a ValueError so existing ``except ValueError`` call sites keep catching it.
    """

    code = "service_invalid"


def validate_service_name(name: str, what: str = "name") -> str:
    """Validate a service/container/network identifier.

    The value is used as a filesystem path component and compose project
    name, so the charset is deliberately narrow.
    """
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ServiceValidationError(
            "Invalid {} {!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}".format(what, name)
        )
    if name in RESERVED_NAMES:
        raise ServiceValidationError(
            "Invalid {} {!r}: reserved for the SyrvisCore core stack".format(what, name)
        )
    return name


def _validate_relative_subpath(value: str, what: str) -> str:
    """Validate a path that must stay inside its designated directory."""
    if not isinstance(value, str) or not value:
        raise ServiceValidationError("{} must be a non-empty string".format(what))
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise ServiceValidationError(
            "Invalid {} {!r}: absolute paths and '..' are not allowed".format(what, value)
        )
    return value


def _validate_image(image: str) -> str:
    """Validate an image reference: pinned tag or digest, never :latest."""
    if not isinstance(image, str) or not image or any(c.isspace() for c in image):
        raise ServiceValidationError("Invalid image reference {!r}".format(image))
    ref = image
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
        if not re.match(r"^sha256:[a-f0-9]{64}$", digest):
            raise ServiceValidationError("Invalid image digest in {!r}".format(image))
        return image
    # Tag is everything after the last ':' unless that segment contains '/'
    # (which would make it a registry port, not a tag)
    tag = None
    if ":" in ref:
        candidate = ref.rsplit(":", 1)[1]
        if "/" not in candidate:
            tag = candidate
    if not tag:
        raise ServiceValidationError(
            "Image {!r} has no tag: pin a specific version (house rule: no floating images)".format(
                image
            )
        )
    if tag == "latest":
        raise ServiceValidationError(
            "Image {!r} uses :latest — pin a specific version tag".format(image)
        )
    return image


def _validate_healthcheck(data: Any) -> Dict[str, Any]:
    """Validate an audited subset of compose's healthcheck.

    Allowed: test (a list whose first element is CMD or CMD-SHELL), interval,
    timeout, start_period (``\\d+(s|m|h)``), retries (1-10). Anything else is
    rejected — same allowlist philosophy as the top-level keys.
    """
    if not isinstance(data, dict):
        raise ServiceValidationError("healthcheck must be a mapping")
    unknown = set(data.keys()) - ALLOWED_HEALTHCHECK_KEYS
    if unknown:
        raise ServiceValidationError(
            "healthcheck: unknown keys {} (allowed: {})".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_HEALTHCHECK_KEYS))
            )
        )

    test = data.get("test")
    if (
        not isinstance(test, list)
        or not test
        or test[0] not in ("CMD", "CMD-SHELL")
        or not all(isinstance(part, str) for part in test)
    ):
        raise ServiceValidationError(
            "healthcheck.test must be a list starting with CMD or CMD-SHELL"
        )

    for key in ("interval", "timeout", "start_period"):
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not DURATION_RE.fullmatch(value):
                raise ServiceValidationError(
                    "healthcheck.{} must match <number>(s|m|h), got {!r}".format(key, value)
                )

    if "retries" in data:
        retries = data["retries"]
        if not isinstance(retries, int) or isinstance(retries, bool) or not 1 <= retries <= 10:
            raise ServiceValidationError("healthcheck.retries must be an integer 1-10")

    return dict(data)


def _validate_resources(data: Any) -> Dict[str, str]:
    """Validate resource limits: cpus (decimal) and/or memory (<n>(b|k|m|g))."""
    if not isinstance(data, dict):
        raise ServiceValidationError("resources must be a mapping")
    unknown = set(data.keys()) - ALLOWED_RESOURCE_KEYS
    if unknown:
        raise ServiceValidationError(
            "resources: unknown keys {} (allowed: cpus, memory)".format(", ".join(sorted(unknown)))
        )
    out: Dict[str, str] = {}
    if "cpus" in data:
        cpus = str(data["cpus"])
        if not CPUS_RE.fullmatch(cpus):
            raise ServiceValidationError("resources.cpus must be a decimal, e.g. '1.5'")
        out["cpus"] = cpus
    if "memory" in data:
        memory = str(data["memory"])
        if not MEMORY_RE.fullmatch(memory):
            raise ServiceValidationError("resources.memory must be <number>(b|k|m|g), e.g. '512m'")
        out["memory"] = memory
    if not out:
        raise ServiceValidationError("resources must declare cpus and/or memory")
    return out


def _validate_command(data: Any) -> List[str]:
    """Validate a container command override (the compose ``command:``, i.e. argv).

    SECURITY / trust boundary. ``command:`` sets the container's CMD — the argv
    handed to the image's ENTRYPOINT. It runs INSIDE the container under the same
    confinement every Layer 2 service gets (``no-new-privileges:true``, no added
    capabilities, no host mounts, bridge-only networking), so it grants no
    authority the image did not already have: the image's own ENTRYPOINT+CMD
    already execute arbitrary code on ``up``, and ``command:`` merely
    parameterizes the CMD of an image the manifest already fully controls. It is
    therefore in the same benign class as ``environment:`` — NOT with the refused
    keys (``privileged``/``cap_add``/``devices``/``network_mode``/docker.sock),
    which grant authority over the HOST.

    To keep it auditable we constrain it harder than compose would:

      - a LIST of strings only (exec form). The bare-string shell form is
        refused, so there is no shell word-splitting or metacharacter surprise —
        each argument is explicit.
      - every element a non-empty string.
      - no ``$`` — the argv is literal and pinned, never subject to compose-time
        ``${VAR}`` interpolation (the same rule the volume validator enforces).
    """
    if not isinstance(data, list) or not data:
        raise ServiceValidationError(
            "command must be a non-empty list of argv strings (exec form); the "
            "bare-string shell form is not accepted"
        )
    for arg in data:
        if not isinstance(arg, str) or not arg:
            raise ServiceValidationError("command entries must be non-empty strings")
        # ASCII '$' (0x24) only — that is the ONLY character docker-compose treats
        # as an interpolation trigger, so banning it is what makes the argv literal.
        # Fullwidth/lookalike dollars (U+FF04 '＄', U+FE69 '﹩') are deliberately NOT
        # matched: compose never interpolates them, so they are inert literals.
        if "$" in arg:
            raise ServiceValidationError(
                "Invalid command entry {!r}: environment expansion ('$') is not "
                "permitted; pin literal arguments".format(arg)
            )
    return list(data)


def _validate_tasks(data: Any) -> Dict[str, List[str]]:
    """Validate declared one-shot tasks (``tasks: {name: {command: [...]}}``).

    A task is a pre-declared, audited argv the operator can execute inside the
    service's OWN running container (``syrvis service task``) — e.g. a DB
    bootstrap — instead of a raw break-glass ``docker exec``. Trust class:
    identical to ``command:`` (see :func:`_validate_command`) — it runs under
    the container's existing confinement and grants no authority the image did
    not already have, so it gets the same audit rules: exec form only, every
    argument literal, no ``$``.
    """
    if not isinstance(data, dict) or not data:
        raise ServiceValidationError(
            "tasks must be a non-empty mapping of name -> {command: [...]}"
        )
    if len(data) > MAX_TASKS:
        raise ServiceValidationError("too many tasks ({}; max {})".format(len(data), MAX_TASKS))
    out: Dict[str, List[str]] = {}
    for task_name, spec in data.items():
        if not isinstance(task_name, str) or not NAME_RE.fullmatch(task_name):
            raise ServiceValidationError(
                "Invalid task name {!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}".format(task_name)
            )
        if not isinstance(spec, dict):
            raise ServiceValidationError("task {!r} must be a mapping".format(task_name))
        unknown = set(spec.keys()) - ALLOWED_TASK_KEYS
        if unknown:
            raise ServiceValidationError(
                "task {!r} has unknown keys {} (allowed: command)".format(
                    task_name, ", ".join(sorted(unknown))
                )
            )
        if "command" not in spec:
            raise ServiceValidationError("task {!r} is missing 'command'".format(task_name))
        try:
            out[task_name] = _validate_command(spec["command"])
        except ServiceValidationError as e:
            raise ServiceValidationError("task {!r}: {}".format(task_name, e))
    return out


def _validate_hooks(data: Any, task_names: frozenset) -> Dict[str, str]:
    """Validate ``hooks: {event: task_name}`` (container lifecycle hooks).

    Selects WHICH declared task runs at WHICH transition — it can never supply
    code (the argv lives in the audited ``tasks:`` map). Only ``pre-stop`` and
    ``post-start`` are accepted: a container hook is a docker exec, so the
    container must be verifiably running.
    """
    if not isinstance(data, dict) or not data:
        raise ServiceValidationError("hooks must be a non-empty mapping of event -> task name")
    if len(data) > MAX_HOOKS:
        raise ServiceValidationError("too many hooks ({}; max {})".format(len(data), MAX_HOOKS))
    out: Dict[str, str] = {}
    for event, task_name in data.items():
        if event not in CONTAINER_HOOK_EVENTS:
            raise ServiceValidationError(
                "hook event {!r} is not supported for container hooks "
                "(allowed: {}; other transitions take host hooks in hooks.d/)".format(
                    event, ", ".join(sorted(CONTAINER_HOOK_EVENTS))
                )
            )
        if not isinstance(task_name, str) or not NAME_RE.fullmatch(task_name):
            raise ServiceValidationError("hook {!r} has an invalid task name".format(event))
        if task_name not in task_names:
            raise ServiceValidationError(
                "hook {!r} references undeclared task {!r} (declared: {})".format(
                    event, task_name, ", ".join(sorted(task_names)) or "(none)"
                )
            )
        out[event] = task_name
    return out


def _validate_shutdown(data: Any) -> Dict[str, int]:
    """Validate ``shutdown: {stop_timeout, priority}`` (graceful-stop knobs)."""
    if not isinstance(data, dict) or not data:
        raise ServiceValidationError(
            "shutdown must be a non-empty mapping (stop_timeout and/or priority)"
        )
    unknown = set(data.keys()) - ALLOWED_SHUTDOWN_KEYS
    if unknown:
        raise ServiceValidationError(
            "shutdown has unknown keys {} (allowed: {})".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_SHUTDOWN_KEYS))
            )
        )
    out: Dict[str, int] = {}
    ranges = {
        "stop_timeout": SHUTDOWN_STOP_TIMEOUT_RANGE,
        "priority": SHUTDOWN_PRIORITY_RANGE,
    }
    for key, value in data.items():
        low, high = ranges[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ServiceValidationError(
                "shutdown.{} must be an integer {}..{}".format(key, low, high)
            )
        out[key] = value
    return out


def _validate_location(value: Any) -> str:
    """Validate an app ``location:`` — a DSM volume root, nothing else.

    Regex-ONLY on purpose: parsing must work off-box (laptop-side declaration
    validation) and in reconcile planning, where /volumeN may not exist. The
    mount-exists check (paths.is_mounted_volume) belongs to install time.
    The charset rules out every traversal shape (/etc, /volume6/../volume1,
    subpaths, trailing slash, whitespace) by construction.
    """
    if not isinstance(value, str) or not LOCATION_RE.fullmatch(value):
        raise ServiceValidationError(
            "Invalid location {!r}: must be a DSM volume root matching "
            "^/volume<N>$ (e.g. /volume6)".format(value)
        )
    return value


def _no_control_chars(value: str) -> bool:
    """True if the string has no ASCII control characters (incl. newlines/tabs)."""
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _validate_dashboard(data: Any) -> Dict[str, Any]:
    """Validate the optional ``dashboard:`` block (metric panels for the row).

    Presentation-only metadata for ``dashboard.py`` — the panel exprs render
    into a Grafana dashboard JSON and are NEVER executed by SyrvisCore or the
    shell, so this validates well-formedness (structure, bounded counts and
    lengths, no control characters), not safety. Unlike ``command``/``volumes``,
    ``$`` is permitted: authors write ``${container}`` and PromQL selectors.

    Shape::

        dashboard:
          panels:
            - {title: "Queue depth", expr: 'immich_job_queue{...}', kind: timeseries, unit: short}
    """
    if not isinstance(data, dict):
        raise ServiceValidationError("dashboard must be a mapping")
    unknown = set(data.keys()) - ALLOWED_DASHBOARD_KEYS
    if unknown:
        raise ServiceValidationError(
            "dashboard: unknown keys {} (allowed: {})".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_DASHBOARD_KEYS))
            )
        )
    panels = data.get("panels", [])
    if not isinstance(panels, list):
        raise ServiceValidationError("dashboard.panels must be a list")
    if len(panels) > MAX_DASHBOARD_PANELS:
        raise ServiceValidationError(
            "dashboard.panels: too many panels ({}; max {})".format(
                len(panels), MAX_DASHBOARD_PANELS
            )
        )
    out_panels: List[Dict[str, Any]] = []
    for i, panel in enumerate(panels):
        if not isinstance(panel, dict):
            raise ServiceValidationError("dashboard.panels[{}] must be a mapping".format(i))
        unknown = set(panel.keys()) - ALLOWED_PANEL_KEYS
        if unknown:
            raise ServiceValidationError(
                "dashboard.panels[{}]: unknown keys {} (allowed: {})".format(
                    i, ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_PANEL_KEYS))
                )
            )
        title = panel.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_PANEL_TITLE:
            raise ServiceValidationError(
                "dashboard.panels[{}].title must be a non-empty string (<= {} chars)".format(
                    i, MAX_PANEL_TITLE
                )
            )
        expr = panel.get("expr")
        if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_PANEL_EXPR:
            raise ServiceValidationError(
                "dashboard.panels[{}].expr must be a non-empty string (<= {} chars)".format(
                    i, MAX_PANEL_EXPR
                )
            )
        if not _no_control_chars(title) or not _no_control_chars(expr):
            raise ServiceValidationError(
                "dashboard.panels[{}]: title/expr must not contain control characters".format(i)
            )
        kind = panel.get("kind", "timeseries")
        if kind not in ALLOWED_PANEL_KINDS:
            raise ServiceValidationError(
                "dashboard.panels[{}].kind {!r}: allowed: {}".format(
                    i, kind, ", ".join(sorted(ALLOWED_PANEL_KINDS))
                )
            )
        unit = panel.get("unit", "")
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
            raise ServiceValidationError(
                "dashboard.panels[{}].unit {!r}: must match {}".format(i, unit, UNIT_RE.pattern)
            )
        out: Dict[str, Any] = {"title": title, "expr": expr, "kind": kind}
        if unit:
            out["unit"] = unit
        out_panels.append(out)

    result: Dict[str, Any] = {"panels": out_panels}

    # about: a markdown blurb for the per-service dashboard's header text panel.
    # Newlines/tabs are fine (it's markdown); other control chars are not.
    about = data.get("about")
    if about is not None:
        if not isinstance(about, str) or len(about) > MAX_ABOUT:
            raise ServiceValidationError(
                "dashboard.about must be a string (<= {} chars)".format(MAX_ABOUT)
            )
        if any(ord(ch) < 0x20 and ch not in "\n\t" or ord(ch) == 0x7F for ch in about):
            raise ServiceValidationError("dashboard.about must not contain control characters")
        result["about"] = about

    # links: helpful links for the dashboard header ([{title, url}]). url must be
    # http(s) — these render as markdown links a human clicks.
    links = data.get("links")
    if links is not None:
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise ServiceValidationError(
                "dashboard.links must be a list of <= {} {{title, url}} entries".format(MAX_LINKS)
            )
        out_links: List[Dict[str, str]] = []
        for i, link in enumerate(links):
            if not isinstance(link, dict):
                raise ServiceValidationError("dashboard.links[{}] must be a mapping".format(i))
            unknown = set(link.keys()) - ALLOWED_LINK_KEYS
            if unknown:
                raise ServiceValidationError(
                    "dashboard.links[{}]: unknown keys {} (allowed: title, url)".format(
                        i, ", ".join(sorted(unknown))
                    )
                )
            ltitle = link.get("title")
            lurl = link.get("url")
            if not isinstance(ltitle, str) or not ltitle.strip() or len(ltitle) > MAX_LINK_TITLE:
                raise ServiceValidationError(
                    "dashboard.links[{}].title must be a non-empty string (<= {} chars)".format(
                        i, MAX_LINK_TITLE
                    )
                )
            if (
                not isinstance(lurl, str)
                or len(lurl) > MAX_LINK_URL
                or not _LINK_URL_RE.match(lurl)
                or not _no_control_chars(lurl)
            ):
                raise ServiceValidationError(
                    "dashboard.links[{}].url must be an http(s) URL (<= {} chars)".format(
                        i, MAX_LINK_URL
                    )
                )
            out_links.append({"title": ltitle, "url": lurl})
        result["links"] = out_links

    return result


FILEPLANE_PREFIX = "fileplane="
_SHARE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _normalize_fileplane_entry(entry: Dict[str, Any]) -> str:
    """Normalize the YAML mapping form of a fileplane volume to its canonical string.

    Declaration form (services.d YAML):

        volumes:
          - fileplane: {share: gaming, subpath: Emulation/ROMS, mount: /roms, mode: ro}

    Canonical internal form (round-trips through manifests unchanged):

        fileplane=gaming/Emulation/ROMS:/roms:ro

    The share is referenced BY ID — resolution against $SYRVIS_HOME/shares.d
    happens at render time (shares_registry), so a declaration validates
    anywhere but only binds on a host that has declared the share.
    """
    spec = entry.get("fileplane")
    if not isinstance(spec, dict):
        raise ServiceValidationError(
            "Volume {!r}: 'fileplane' must be a mapping with keys "
            "share/mount[/subpath/mode]".format(entry)
        )
    unknown = set(spec) - {"share", "subpath", "mount", "mode"}
    if unknown:
        raise ServiceValidationError(
            "Volume fileplane: unknown key(s) {} — allowed: share, subpath, "
            "mount, mode".format(sorted(unknown))
        )
    share = spec.get("share")
    if not isinstance(share, str) or not _SHARE_ID_RE.match(share or ""):
        raise ServiceValidationError(
            "Volume fileplane: 'share' must be a lowercase share id "
            "([a-z0-9-]), got {!r}".format(share)
        )
    subpath = spec.get("subpath", "")
    if subpath:
        _validate_relative_subpath(subpath, "fileplane subpath")
        if ":" in subpath:
            raise ServiceValidationError("Volume fileplane: ':' is not permitted in subpath")
    mount = spec.get("mount")
    mode = spec.get("mode", "ro")
    source = share + ("/" + subpath if subpath else "")
    return "{}{}:{}:{}".format(FILEPLANE_PREFIX, source, mount, mode)


def _validate_fileplane_volume(vol: str) -> str:
    """Validate the canonical ``fileplane=<share>[/<subpath>]:<mount>:<mode>`` form.

    File-plane binds reference operator-declared NAS shares (shares.d) by id —
    the sanctioned exception to the absolute-host-path refusal, precisely
    because no path appears here: the engine resolves the id at render time
    and refuses undeclared shares. Mode defaults ro; rw is additionally gated
    at render time by the share's writers: sanction (shares_registry).
    """
    body = vol[len(FILEPLANE_PREFIX) :]
    parts = body.split(":")
    if len(parts) not in (2, 3):
        raise ServiceValidationError(
            "Volume {!r}: expected 'fileplane=<share>[/<subpath>]:"
            "/container/path[:mode]'".format(vol)
        )
    source, container = parts[0], parts[1]
    mode = parts[2] if len(parts) == 3 else "ro"
    if mode not in ("ro", "rw"):
        raise ServiceValidationError("Volume {!r}: mode must be ro or rw".format(vol))
    share, _, subpath = source.partition("/")
    if not _SHARE_ID_RE.match(share):
        raise ServiceValidationError(
            "Volume {!r}: share id must be lowercase [a-z0-9-]".format(vol)
        )
    if subpath:
        _validate_relative_subpath(subpath, "fileplane subpath")
    if not PurePosixPath(container).is_absolute() or ".." in PurePosixPath(container).parts:
        raise ServiceValidationError(
            "Volume {!r}: container path must be absolute (no '..')".format(vol)
        )
    return "{}{}:{}:{}".format(FILEPLANE_PREFIX, source, container, mode)


def _validate_ports(data: Any) -> List[str]:
    """Validate ``ports:`` — direct host port publishes, ``"H:C[/proto]"``.

    This is the raw-protocol escape hatch for services whose data plane is not
    HTTP (sync protocols, game servers): Traefik cannot route them, so they
    publish on the host. Declared intent, same as exposure — a published port
    is reachable by every LAN client, and the declaration is the audit trail.
    """
    if not isinstance(data, list):
        raise ServiceValidationError("ports must be a list of 'host:container[/proto]' strings")
    out: List[str] = []
    for entry in data:
        if not isinstance(entry, str) or not entry:
            raise ServiceValidationError("ports entries must be non-empty strings")
        spec, _, proto = entry.partition("/")
        proto = proto or "tcp"
        if proto not in ("tcp", "udp"):
            raise ServiceValidationError("Port {!r}: protocol must be tcp or udp".format(entry))
        host_s, sep, container_s = spec.partition(":")
        if not sep:
            raise ServiceValidationError(
                "Port {!r}: expected 'host:container[/proto]'".format(entry)
            )
        try:
            host_p, container_p = int(host_s), int(container_s)
        except ValueError:
            raise ServiceValidationError("Port {!r}: ports must be integers".format(entry))
        for p in (host_p, container_p):
            if not 1 <= p <= 65535:
                raise ServiceValidationError("Port {!r}: out of range 1-65535".format(entry))
        out.append("{}:{}/{}".format(host_p, container_p, proto))
    return out


def _validate_volume(vol: str, tier: str = "") -> str:
    """Validate a volume entry against the mount policy.

    Allowed (ALL tiers):
      - named volumes: ``myvolume:/container/path[:mode]``
      - relative host paths (resolved under data/<service>/):
        ``subdir:/container/path[:mode]``
      - file-plane share references (resolved via shares.d at render time):
        ``fileplane=<share>[/<subpath>]:/container/path[:mode]``
    Refused (all tiers): '..' traversal, '$' expansions, modes other than ro/rw.

    The **infra tier** (design/22) additionally permits an ENUMERATED, READ-ONLY
    set of absolute host mounts — ``INFRA_HOST_MOUNTS`` (/proc, /sys, /, and the
    docker socket) — for operator-authored first-party exporters. Any OTHER
    absolute host path (or docker.sock at a non-canonical path) stays refused even
    for infra, and a non-infra service keeps the full refusal. The gate that only
    an operator declaration may SET ``tier: infra`` lives at install time.
    """
    if not isinstance(vol, str) or not vol:
        raise ServiceValidationError("Volume entries must be non-empty strings")
    if "$" in vol:
        raise ServiceValidationError(
            "Volume {!r}: environment expansion is not permitted".format(vol)
        )
    if vol.startswith(FILEPLANE_PREFIX):
        return _validate_fileplane_volume(vol)

    parts = vol.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ServiceValidationError(
            "Volume {!r}: expected 'source:/container/path[:mode]'".format(vol)
        )

    host, container = parts[0], parts[1]
    mode = parts[2] if len(parts) == 3 else "rw"

    if mode not in ("ro", "rw"):
        raise ServiceValidationError("Volume {!r}: mode must be ro or rw".format(vol))

    if not PurePosixPath(container).is_absolute() or ".." in PurePosixPath(container).parts:
        raise ServiceValidationError(
            "Volume {!r}: container path must be absolute (no '..')".format(vol)
        )

    # infra tier: an ENUMERATED, READ-ONLY absolute host mount is permitted. This
    # is the ONLY path by which an absolute host path or the docker socket passes.
    if tier == "infra" and host in INFRA_HOST_MOUNTS:
        if mode != "ro":
            raise ServiceValidationError(
                "Volume {!r}: an infra host mount must be read-only (:ro)".format(vol)
            )
        return vol

    # Everything else: the strict refusals (every tier).
    if "docker.sock" in vol:
        raise ServiceValidationError(
            "Volume {!r}: mounting the Docker socket is not permitted".format(vol)
        )
    if host.startswith("/") or host.startswith("~"):
        raise ServiceValidationError(
            "Volume {!r}: absolute host paths are not permitted; use a path "
            "relative to the service data directory or a named volume".format(vol)
        )
    if ".." in PurePosixPath(host).parts:
        raise ServiceValidationError("Volume {!r}: '..' is not permitted".format(vol))

    return vol


@dataclass
class TraefikConfig:
    """Traefik routing configuration for a service."""

    enabled: bool = True
    subdomain: str = ""
    # Optional per-service domain override.  When set, the effective hostname is
    # ``<subdomain>.<domain>`` instead of ``<subdomain>.<instance-DOMAIN>``.
    # Empty string (the default) means "use the instance domain" — every existing
    # service that omits this field is byte-for-byte unchanged.
    domain: str = ""
    port: int = 80
    middlewares: List[str] = field(default_factory=list)
    # How the routed service is reached from outside: "internal" (LAN-only) or
    # "tunnel" (Cloudflare Tunnel + Access). Declared intent only — SyrvisCore
    # routes both the same; it drives the `syrvis stack hostnames` report.
    exposure: str = exposure_mod.DEFAULT

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TraefikConfig":
        """Create TraefikConfig from dictionary."""
        if not data:
            return cls(enabled=False)
        return cls(
            enabled=data.get("enabled", True),
            subdomain=data.get("subdomain", ""),
            domain=str(data.get("domain") or "").strip().lower(),
            port=data.get("port", 80),
            middlewares=data.get("middlewares", []),
            exposure=str(data.get("exposure") or exposure_mod.DEFAULT).strip().lower(),
        )


@dataclass
class ConfigTemplate:
    """Template file to copy during service installation."""

    source: str
    dest: str

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "ConfigTemplate":
        """Create ConfigTemplate from dictionary."""
        return cls(
            source=data.get("source", ""),
            dest=data.get("dest", ""),
        )


@dataclass
class ServiceDefinition:
    """Complete service definition from syrvis-service.yaml."""

    name: str
    version: str
    image: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    container_name: str = ""
    traefik: TraefikConfig = field(default_factory=TraefikConfig)
    environment: List[str] = field(default_factory=list)
    # Container command override (argv / exec form). Audited by _validate_command:
    # a non-empty list of literal strings, no shell, no '$'. Empty == use the
    # image's default CMD. Needed by argv-driven images (e.g. VictoriaMetrics'
    # vmagent/vmalert) that have no env-var-only configuration path.
    command: List[str] = field(default_factory=list)
    # Declared one-shot tasks: {task_name: argv}. Audited like `command`
    # (_validate_tasks); executed inside the RUNNING container by
    # `syrvis service task <name> --task <task>` (e.g. a DB bootstrap).
    tasks: Dict[str, List[str]] = field(default_factory=dict)
    # Container lifecycle hooks: {event: task_name} — run a DECLARED task at
    # pre-stop / post-start (the DB-quiesce path). Validated by _validate_hooks.
    hooks: Dict[str, str] = field(default_factory=dict)
    # Graceful-shutdown knobs: {stop_timeout, priority} — SIGTERM grace and
    # instance-shutdown stop band. None == defaults (30s, band 50).
    shutdown: Optional[Dict[str, int]] = None
    # Privileged tier selector (design/22): "" (default) | "infra". `infra` unlocks
    # the enumerated read-only host-mount allowlist; only an operator-authored
    # declaration may set it (install-time authorship gate).
    tier: str = ""
    # App home volume (design/26): "" (default) = legacy layout under SYRVIS_HOME;
    # "/volume<N>" = the v2 app home <location>/syrviscore/apps/<name>/. Same
    # trust anchor as `tier` — only an operator-authored declaration may set it
    # (install-time authorship gate + mount check in ServiceManager).
    location: str = ""
    # A data-dir-relative env file (installed 0600) — the recommended home for
    # secrets, keeping them out of this manifest.
    env_file: str = ""
    volumes: List[str] = field(default_factory=list)
    ports: List[str] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    config_templates: List[ConfigTemplate] = field(default_factory=list)
    restart: str = "unless-stopped"
    healthcheck: Optional[Dict[str, Any]] = None
    resources: Optional[Dict[str, str]] = None
    # Optional presentation-only metric panels for the generated Grafana
    # dashboard (dashboard.py). None == the service gets only the standard
    # container panels. {"panels": [{title, expr, kind, unit?}]} adds
    # domain-specific panels to this service's row. Never executed by Core.
    dashboard: Optional[Dict[str, Any]] = None
    # Orchestration (services.d): declared-but-off, and health severity.
    enabled: bool = True
    critical: bool = False
    # Source information (set after loading)
    source_path: Optional[Path] = None
    source_url: Optional[str] = None

    def __post_init__(self):
        """Set defaults after initialization."""
        if not self.container_name:
            self.container_name = self.name
        if not self.networks:
            self.networks = ["proxy"]
        elif "proxy" not in self.networks:
            self.networks.append("proxy")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServiceDefinition":
        """Create ServiceDefinition from dictionary (strictly validated)."""
        if not isinstance(data, dict):
            raise ServiceValidationError("Service definition must be a mapping")

        unknown = set(data.keys()) - ALLOWED_TOP_LEVEL_KEYS
        if unknown:
            raise ServiceValidationError(
                "Unknown keys in service definition: {} — only audited keys are "
                "accepted".format(", ".join(sorted(unknown)))
            )

        # Validate required fields
        required = ["name", "version", "image"]
        missing = [f for f in required if f not in data]
        if missing:
            raise ServiceValidationError(f"Missing required fields: {', '.join(missing)}")

        name = validate_service_name(data["name"], "service name")
        container_name = data.get("container_name", name)
        validate_service_name(container_name, "container_name")
        image = _validate_image(data["image"])

        restart = data.get("restart", "unless-stopped")
        if restart not in ALLOWED_RESTART:
            raise ServiceValidationError(
                "Invalid restart policy {!r}: allowed: {}".format(
                    restart, ", ".join(sorted(ALLOWED_RESTART))
                )
            )

        environment = data.get("environment", [])
        if not isinstance(environment, list):
            raise ServiceValidationError("environment must be a list of KEY=VALUE strings")
        for entry in environment:
            if not isinstance(entry, str) or "=" not in entry:
                raise ServiceValidationError(
                    "Invalid environment entry {!r}: expected KEY=VALUE".format(entry)
                )
            key = entry.split("=", 1)[0]
            if not ENV_KEY_RE.fullmatch(key):
                raise ServiceValidationError("Invalid environment variable name {!r}".format(key))

        command: List[str] = []
        if data.get("command") is not None:
            command = _validate_command(data["command"])

        tasks: Dict[str, List[str]] = {}
        if data.get("tasks") is not None:
            tasks = _validate_tasks(data["tasks"])

        hooks: Dict[str, str] = {}
        if data.get("hooks") is not None:
            hooks = _validate_hooks(data["hooks"], frozenset(tasks))

        shutdown = None
        if data.get("shutdown") is not None:
            shutdown = _validate_shutdown(data["shutdown"])

        tier = data.get("tier", "")
        if tier not in ALLOWED_TIERS:
            raise ServiceValidationError(
                "Invalid tier {!r}: allowed: '' (default) or 'infra'".format(tier)
            )

        location = ""
        if data.get("location"):
            location = _validate_location(data["location"])

        raw_volumes = data.get("volumes", [])
        if not isinstance(raw_volumes, list):
            raise ServiceValidationError("volumes must be a list")
        volumes = []
        for vol in raw_volumes:
            if isinstance(vol, dict) and "fileplane" in vol:
                vol = _normalize_fileplane_entry(vol)
            volumes.append(_validate_volume(vol, tier))

        ports = _validate_ports(data.get("ports", [])) if data.get("ports") else []

        networks = data.get("networks", [])
        if not isinstance(networks, list):
            raise ServiceValidationError("networks must be a list")
        for net in networks:
            if net == "proxy":
                continue
            validate_service_name(net, "network name")

        depends_on = data.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ServiceValidationError("depends_on must be a list")
        if depends_on:
            # Each Layer-2 service runs as its OWN single-service compose project
            # (-p syrvis-<name>), so compose `depends_on` — which only orders
            # services WITHIN one project — can never reference another Syrvis
            # service. Reject it clearly instead of writing a silent no-op that
            # fails at docker-run time. (Sidecars need a real multi-container
            # manifest, which the schema does not yet support.)
            raise ServiceValidationError(
                "depends_on is not supported: each service is its own compose "
                "project, so it cannot depend on another Syrvis service. Remove "
                "the depends_on block (multi-container manifests are not yet supported)."
            )
        for dep in depends_on:
            validate_service_name(dep, "depends_on entry")

        templates = []
        for t in data.get("config_templates", []):
            template = ConfigTemplate.from_dict(t)
            _validate_relative_subpath(template.source, "config template source")
            _validate_relative_subpath(template.dest, "config template dest")
            templates.append(template)

        env_file = data.get("env_file", "")
        if env_file:
            _validate_relative_subpath(env_file, "env_file")

        healthcheck = None
        if data.get("healthcheck") is not None:
            healthcheck = _validate_healthcheck(data["healthcheck"])

        for flag in ("enabled", "critical"):
            if flag in data and not isinstance(data[flag], bool):
                raise ServiceValidationError("{} must be a boolean".format(flag))

        resources = None
        if data.get("resources") is not None:
            resources = _validate_resources(data["resources"])

        dashboard = None
        if data.get("dashboard") is not None:
            dashboard = _validate_dashboard(data["dashboard"])

        traefik = TraefikConfig.from_dict(data.get("traefik"))
        if traefik.enabled:
            if not SUBDOMAIN_RE.fullmatch(traefik.subdomain or ""):
                raise ServiceValidationError(
                    "Invalid traefik subdomain {!r}: must be a single DNS label".format(
                        traefik.subdomain
                    )
                )
            if traefik.domain and not DOMAIN_RE.fullmatch(traefik.domain):
                raise ServiceValidationError(
                    "Invalid traefik domain {!r}: must be a dot-separated domain with "
                    "at least 2 labels (e.g. 'example.com'), each label matching "
                    "[a-z0-9][a-z0-9-]{{0,61}}[a-z0-9]".format(traefik.domain)
                )
            if not isinstance(traefik.port, int) or not 1 <= traefik.port <= 65535:
                raise ServiceValidationError(
                    "Invalid traefik port {!r}: must be 1-65535".format(traefik.port)
                )
            if not exposure_mod.is_valid(traefik.exposure):
                raise ServiceValidationError(
                    "Invalid traefik exposure {!r}: must be one of {}".format(
                        traefik.exposure, ", ".join(exposure_mod.EXPOSURES)
                    )
                )

        return cls(
            name=name,
            version=str(data["version"]),
            image=image,
            description=data.get("description", ""),
            author=data.get("author", ""),
            homepage=data.get("homepage", ""),
            container_name=container_name,
            traefik=traefik,
            environment=environment,
            command=command,
            tasks=tasks,
            hooks=hooks,
            shutdown=shutdown,
            tier=tier,
            location=location,
            env_file=env_file,
            volumes=volumes,
            ports=ports,
            networks=networks,
            depends_on=depends_on,
            config_templates=templates,
            restart=restart,
            healthcheck=healthcheck,
            resources=resources,
            dashboard=dashboard,
            enabled=data.get("enabled", True),
            critical=data.get("critical", False),
        )

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "ServiceDefinition":
        """Load service definition from YAML file."""
        if not yaml_path.exists():
            raise FileNotFoundError(f"Service definition not found: {yaml_path}")

        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty service definition: {yaml_path}")

        service = cls.from_dict(data)
        service.source_path = yaml_path.parent
        return service

    def to_dict(self) -> Dict[str, Any]:
        """Convert service definition to dictionary."""
        result = {
            "name": self.name,
            "version": self.version,
            "image": self.image,
            "container_name": self.container_name,
            "restart": self.restart,
        }

        if self.description:
            result["description"] = self.description
        if self.author:
            result["author"] = self.author
        if self.homepage:
            result["homepage"] = self.homepage

        if self.traefik.enabled:
            result["traefik"] = {
                "enabled": self.traefik.enabled,
                "subdomain": self.traefik.subdomain,
                "port": self.traefik.port,
                "exposure": self.traefik.exposure,
            }
            if self.traefik.domain:
                result["traefik"]["domain"] = self.traefik.domain
            if self.traefik.middlewares:
                result["traefik"]["middlewares"] = self.traefik.middlewares

        if self.environment:
            result["environment"] = self.environment
        if self.command:
            result["command"] = self.command
        if self.tasks:
            result["tasks"] = {name: {"command": argv} for name, argv in self.tasks.items()}
        if self.hooks:
            result["hooks"] = dict(self.hooks)
        if self.shutdown:
            result["shutdown"] = dict(self.shutdown)
        if self.tier:
            result["tier"] = self.tier
        if self.location:
            result["location"] = self.location
        if self.env_file:
            result["env_file"] = self.env_file
        if self.volumes:
            result["volumes"] = self.volumes
        if self.ports:
            result["ports"] = list(self.ports)
        if self.networks:
            result["networks"] = self.networks
        if self.depends_on:
            result["depends_on"] = self.depends_on
        if self.config_templates:
            result["config_templates"] = [
                {"source": t.source, "dest": t.dest} for t in self.config_templates
            ]
        if self.healthcheck:
            result["healthcheck"] = self.healthcheck
        if self.resources:
            result["resources"] = self.resources
        if self.dashboard:
            result["dashboard"] = self.dashboard
        if not self.enabled:
            result["enabled"] = False
        if self.critical:
            result["critical"] = True

        return result


ORCHESTRATION_KEYS = ("enabled", "critical")


def dump_definition(
    service: "ServiceDefinition", path: Path, include_orchestration: bool = True
) -> Path:
    """Serialize a definition to ``path`` with the shared secrets policy.

    The ONE writer behind installed manifests (orchestration stripped — they
    describe the container, and older service versions must keep parsing them)
    and services.d declarations (orchestration kept — that is where it lives).
    Files carrying inline env entries are written 0600.
    """
    data = service.to_dict()
    if not include_orchestration:
        for key in ORCHESTRATION_KEYS:
            data.pop(key, None)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    path.chmod(0o600 if service.environment else 0o644)
    return path


def load_service_definition(path: Path) -> ServiceDefinition:
    """Load a service definition from a directory or YAML file.

    Args:
        path: Path to service directory or syrvis-service.yaml file

    Returns:
        Parsed ServiceDefinition

    Raises:
        FileNotFoundError: If service definition not found
        ValueError: If service definition is invalid
    """
    if path.is_dir():
        yaml_path = path / "syrvis-service.yaml"
    else:
        yaml_path = path

    return ServiceDefinition.from_yaml(yaml_path)
