"""
Argument validation — the injection boundary (guardrails G2–G6).

Every user-supplied argument is validated HERE, before it is ever placed into an
ssh argv. Validation is allowlist-first: a value must match a narrow, known-safe
pattern, and separately must contain no shell/ssh metacharacter. Only then does
remote.py quote and send it. Nothing reaches SSH unvalidated.
"""

import re
from typing import Optional

from ._cli_regexes import NAME_RE, RESERVED_NAMES, validate_version_str
from .errors import ValidationError

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
