"""
Argument validation — the injection boundary (guardrails G2–G6).

Every user-supplied argument is validated HERE, before it is ever placed into an
ssh argv. Validation is allowlist-first: a value must match a narrow, known-safe
pattern, and separately must contain no shell/ssh metacharacter. Only then does
remote.py quote and send it. Nothing reaches SSH unvalidated.
"""

import re
from typing import Optional

from ._cli_regexes import EXPOSURES, NAME_RE, RESERVED_NAMES, SUBDOMAIN_RE, validate_version_str
from .errors import ValidationError

# A registry-qualified, pinned image reference: host[:port]/path...[:tag][@digest].
# Requires at least one '/' (so it is registry-qualified — the image-first path
# only accepts registry images, never a bare Docker Hub short name). This is a
# shape guard; validate_image additionally enforces the registry allowlist and
# that the reference is pinned (tag != latest, or a digest).
_IMAGE_RE = re.compile(
    r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(:[0-9]+)?"  # host[:port]
    r"(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+"  # /path (one or more segments)
    r"(:[A-Za-z0-9._-]+)?"  # optional :tag
    r"(@sha256:[a-f0-9]{64})?$"  # optional @digest
)

# G6 — global deny list applied to every string argument regardless of type.
# These are the characters that could break out of an ssh remote-command,
# a sudo argv re-parse, or the forced-command shim's word splitting.
_FORBIDDEN_CHARS = set("\x00\r\n;`$|&<>()!*?{}[]\\'\" \t")
_MAX_LEN = 256

# Stricter-than-CLI git URL policy (G4). The CLI's _is_git_url is permissive
# (accepts file://, http://, bare *.git); the MCP allows only these three
# explicit, safe prefixes and nothing that git could interpret as a
# transport-helper or local path.
# Path chars include '.', so a trailing '.git' is just part of the path — no
# special-casing needed. Host chars are letters/digits/dot/hyphen only.
_GIT_HTTPS = re.compile(r"^https://[A-Za-z0-9.\-]+(:\d+)?/[A-Za-z0-9._/\-]+$")
_GIT_SSH_SCP = re.compile(r"^git@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$")
_GIT_SSH_URL = re.compile(r"^ssh://git@[A-Za-z0-9.\-]+(:\d+)?/[A-Za-z0-9._/\-]+$")


def _reject_metachars(value: str, what: str) -> None:
    if not isinstance(value, str):
        raise ValidationError(f"{what} must be a string")
    if len(value) > _MAX_LEN:
        raise ValidationError(f"{what} is too long (>{_MAX_LEN} chars)")
    bad = _FORBIDDEN_CHARS.intersection(value)
    if bad:
        raise ValidationError(
            f"{what} contains a forbidden character",
            operator_hint="arguments may not contain shell metacharacters or whitespace",
        )
    if value.startswith("-"):
        # A leading '-' would be parsed as a flag on the far side; the '--'
        # separator in build_remote defends against this too, but reject early.
        raise ValidationError(f"{what} may not start with '-'")


def validate_version(version: str) -> str:
    """G2 — MAJOR.MINOR.PATCH (one optional leading 'v'), no metachars."""
    _reject_metachars(version, "version")
    try:
        return validate_version_str(version)
    except ValueError as e:
        raise ValidationError(str(e))


def validate_name(name: str) -> str:
    """G3 — service/container name; charset-limited and not a reserved core name."""
    _reject_metachars(name, "name")
    if not NAME_RE.match(name):
        raise ValidationError(f"invalid name {name!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}")
    if name in RESERVED_NAMES:
        raise ValidationError(f"{name!r} is a reserved SyrvisCore core name")
    return name


def validate_git_url(url: str, allowed_hosts: Optional[list] = None) -> str:
    """G4 — only https / scp-style git@ / ssh:// git URLs; MANDATORY host allowlist.

    ``service_add`` is the one tool that clones and runs *new* attacker-supplied
    code on the NAS, so the host allowlist fails CLOSED: an empty/unset list
    means "disabled", never "allow any host". Configure
    safety.git_url_allowed_hosts to enable it.
    """
    _reject_metachars(url, "git_url")
    lowered = url.lower()
    for bad_prefix in ("file://", "http://", "ext::", "fd::", "-"):
        if lowered.startswith(bad_prefix):
            raise ValidationError(
                f"git URL protocol not allowed: {url!r}",
                operator_hint="use https://, git@host:path, or ssh://git@host/path",
            )
    host = None
    if _GIT_HTTPS.match(url):
        host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    elif _GIT_SSH_SCP.match(url):
        host = url.split("@", 1)[1].split(":", 1)[0]
    elif _GIT_SSH_URL.match(url):
        host = url.split("@", 1)[1].split("/", 1)[0].split(":", 1)[0]
    else:
        raise ValidationError(
            f"invalid git URL {url!r}",
            operator_hint="use https://, git@host:path, or ssh://git@host/path",
        )
    if not allowed_hosts:
        raise ValidationError(
            "service_add is disabled: no safety.git_url_allowed_hosts configured",
            operator_hint="set safety.git_url_allowed_hosts (e.g. ['github.com']) to enable service_add",
        )
    if host not in allowed_hosts:
        raise ValidationError(
            f"git host {host!r} is not in the allowed list {allowed_hosts}",
        )
    return url


def validate_subdomain(subdomain: str) -> str:
    """A single DNS label (mirrors service_schema.SUBDOMAIN_RE); no metachars."""
    _reject_metachars(subdomain, "subdomain")
    if not SUBDOMAIN_RE.match(subdomain):
        raise ValidationError(
            f"invalid subdomain {subdomain!r}: must be a single DNS label",
        )
    return subdomain


def validate_exposure(exposure: str) -> str:
    """'internal' (LAN-only) or 'tunnel' (remote via Cloudflare)."""
    _reject_metachars(exposure, "exposure")
    if exposure not in EXPOSURES:
        raise ValidationError(
            f"invalid exposure {exposure!r}: must be one of {sorted(EXPOSURES)}",
        )
    return exposure


def validate_port(port: int) -> int:
    """A container port Traefik forwards to (1-65535)."""
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValidationError("port must be an integer")
    if not 1 <= port <= 65535:
        raise ValidationError("port must be between 1 and 65535")
    return port


def validate_image(image: str, allowed_registries: Optional[list] = None) -> str:
    """G4-style — a pinned, registry-qualified image whose registry is allowlisted.

    ``service_run`` pulls and RUNS a container image, so like ``service_add`` it
    fails CLOSED: an empty/unset ``safety.image_allowed_registries`` means
    "disabled", never "allow any registry". The image must be pinned (a specific
    tag, never ``:latest``, or a digest) so a run is reproducible.
    """
    _reject_metachars(image, "image")
    if not _IMAGE_RE.match(image):
        raise ValidationError(
            f"invalid image reference {image!r}",
            operator_hint="use a registry-qualified pinned image, e.g. ghcr.io/owner/name:1.2.3",
        )
    # Pinned: a digest, or a tag that is not 'latest'.
    if "@sha256:" not in image:
        ref = image.split("@", 1)[0]
        last = ref.rsplit("/", 1)[-1]  # only a ':' in the final path segment is a tag
        tag = last.rsplit(":", 1)[1] if ":" in last else None
        if not tag:
            raise ValidationError(
                f"image {image!r} is not pinned: add a version tag or @sha256 digest",
            )
        if tag == "latest":
            raise ValidationError(f"image {image!r} uses :latest — pin a specific version tag")
    registry = image.split("/", 1)[0]
    if not allowed_registries:
        raise ValidationError(
            "service_run is disabled: no safety.image_allowed_registries configured",
            operator_hint="set safety.image_allowed_registries (e.g. ['ghcr.io']) to enable service_run",
        )
    if registry not in allowed_registries:
        raise ValidationError(
            f"image registry {registry!r} is not in the allowed list {allowed_registries}",
        )
    return image


_PRUNE_POLICIES = frozenset({"stop", "remove", "purge"})


def validate_prune_policy(policy: str) -> str:
    """Exactly 'stop' | 'remove' | 'purge' — reconcile's undeclared-service policy."""
    _reject_metachars(policy, "prune policy")
    if policy not in _PRUNE_POLICIES:
        raise ValidationError(
            f"invalid prune policy {policy!r}: must be one of {sorted(_PRUNE_POLICIES)}",
        )
    return policy


def validate_bool_flag(value: str) -> str:
    """Exactly 'true' or 'false' — the lowercase rendering of a Python bool.

    The MCP tools take real booleans and render them lowercase before the value
    reaches this slot validator; anything else (including 'True'/'1') is rejected.
    """
    _reject_metachars(value, "boolean flag")
    if value not in ("true", "false"):
        raise ValidationError(
            f"invalid boolean flag {value!r}: must be 'true' or 'false'",
        )
    return value


_CRON_FIELD_CHARS = frozenset("0123456789*/,-")


def validate_cron_spec(spec: str) -> str:
    """A cron spec: exactly 5 whitespace-separated fields, each drawn from
    ``[0-9*/,-]`` only.

    Defense in depth for scheduled jobs. The cron spec MUST NOT travel over the
    MCP seam as an argv token — its ``*``/``,``/``?`` would be rejected by the
    forced-command shim's char-allowlist anyway. The schedule lives only in
    ``jobs.d/<name>.yaml`` (validated server-side in ``jobs_d.validate_cron_spec``
    at reconcile time); this validator exists so that IF a cron value is ever
    handled MCP-side it fails closed to the same rule, never reaching SSH.
    """
    if not isinstance(spec, str):
        raise ValidationError("cron spec must be a string")
    fields = spec.split()
    if len(fields) != 5:
        raise ValidationError(
            f"cron spec must have exactly 5 fields (got {len(fields)})"
        )
    for field in fields:
        if not field or set(field) - _CRON_FIELD_CHARS:
            raise ValidationError(
                f"cron field {field!r} contains disallowed characters (allowed: [0-9*/,-])"
            )
    return " ".join(fields)


def validate_tail(tail: int) -> int:
    """G5 — log tail line count bounds."""
    if not isinstance(tail, int) or isinstance(tail, bool):
        raise ValidationError("tail must be an integer")
    if not 1 <= tail <= 10000:
        raise ValidationError("tail must be between 1 and 10000")
    return tail


def validate_keep(keep: int) -> int:
    """G5 — number of versions/backups to keep."""
    if not isinstance(keep, int) or isinstance(keep, bool):
        raise ValidationError("keep must be an integer")
    if not 0 <= keep <= 50:
        raise ValidationError("keep must be between 0 and 50")
    return keep


def assert_safe_token(value: str, what: str = "argument") -> str:
    """Belt-and-braces check used by remote.py on every token before it is sent."""
    _reject_metachars(value, what)
    return value
