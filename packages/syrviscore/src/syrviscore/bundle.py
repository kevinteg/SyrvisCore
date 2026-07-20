"""
Deployment bundle schema for Layer 2 services (design/21).

A ``syrvis-bundle`` is the RESOLVED, self-contained input to ``syrvis deploy``:
a service manifest (a full :class:`ServiceDefinition`) + the non-secret config
files to place + the secret values to materialize into the declared env_file.
It is the one artifact ``deploy`` parses, validates, and applies.

SyrvisCore OWNS this schema and *understands* bundles; a deployment repo
(home-tech) owns the INSTANCES — it resolves its discoverable reference
declarations (config sources, sops keys) into a resolved bundle and streams it
over the operator seam. SyrvisCore never learns a service name, a sops file, or
the repo layout (design/15).

SECURITY: like ``syrvis-service.yaml``, a bundle is attacker-controlled input
that root turns into filesystem writes, so every field is strictly validated —
the service manifest through the full :class:`ServiceDefinition` trust boundary,
config dests confined under ``data/<name>/`` (and refused when they collide with
the declared env_file), secret env keys charset-checked. Config/secret VALUES
are inert data to their consumer (the config-render invariant, design/20) and
secret values are never echoed or logged.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List

from syrviscore.errors import SyrvisError

from .service_schema import ENV_KEY_RE, ServiceDefinition, ServiceValidationError

# The bundle wire/on-disk format version. Bumped only on a breaking schema
# change; ``deploy`` refuses an unknown apiVersion rather than guessing.
BUNDLE_API_VERSION = "syrvis-bundle/v1"

ALLOWED_BUNDLE_KEYS = frozenset({"apiVersion", "service", "configs", "secrets"})
ALLOWED_CONFIG_KEYS = frozenset({"dest", "content"})

# A config value is capped like a secret — enough for any real config file,
# rejects an OOM/DoS stream. Matches ServiceManager._SECRET_MAX_BYTES.
BUNDLE_MAX_BYTES = 65536


class BundleValidationError(SyrvisError, ValueError):
    """A syrvis-bundle failed validation (unsafe or malformed).

    Also a ValueError so existing ``except ValueError`` call sites keep catching it.
    """

    code = "bundle_invalid"


@dataclass(frozen=True)
class BundleConfig:
    """A single non-secret config file to place in the service's data dir.

    ``dest`` is relative to ``data/<service>/`` (validated: relative, no ``..``);
    ``content`` is the literal file body (written 0644 — container-readable over a
    :ro mount). Non-secret by contract: SECRETS travel in ``DeployBundle.secrets``
    and land in the 0600 env_file, never here.
    """

    dest: str
    content: str


@dataclass
class DeployBundle:
    """A resolved, self-contained deployment bundle (design/21 §3.5)."""

    service: ServiceDefinition
    configs: List[BundleConfig] = field(default_factory=list)
    # Resolved secret VALUES keyed by env var; materialized into the service's
    # declared env_file (0600) at apply. Never logged. Empty unless the manifest
    # declares an env_file to receive them.
    secrets: Dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.service.name

    @classmethod
    def from_dict(cls, data: Any) -> "DeployBundle":
        """Parse + strictly validate a bundle document.

        The ``service`` block runs the FULL ServiceDefinition trust boundary.
        Structural containment of config dests (realpath vs the live data dir) is
        re-checked at apply by the ServiceManager — this layer catches absolute
        paths, ``..``, env_file collisions, dup dests, and bad env keys early.
        """
        if not isinstance(data, dict):
            raise BundleValidationError("bundle must be a mapping")

        unknown = set(data.keys()) - ALLOWED_BUNDLE_KEYS
        if unknown:
            raise BundleValidationError(
                "unknown bundle keys {} (allowed: {})".format(
                    ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_BUNDLE_KEYS))
                )
            )

        api = data.get("apiVersion", BUNDLE_API_VERSION)
        if api != BUNDLE_API_VERSION:
            raise BundleValidationError(
                "unsupported bundle apiVersion {!r} (expected {!r})".format(api, BUNDLE_API_VERSION)
            )

        if "service" not in data:
            raise BundleValidationError("bundle is missing the required 'service' manifest")
        # The full Layer 2 trust boundary: name charset, pinned image, contained
        # volumes, audited command, unknown keys rejected, etc. Re-raise as a
        # BundleValidationError so a caller catches ONE bundle error type (the
        # ServiceDefinition detail is preserved in the message).
        try:
            service = ServiceDefinition.from_dict(data["service"])
        except ServiceValidationError as e:
            raise BundleValidationError("invalid service manifest in bundle: {}".format(e)) from e

        configs = cls._parse_configs(data.get("configs", []), service)
        secrets = cls._parse_secrets(data.get("secrets", {}), service)

        return cls(service=service, configs=configs, secrets=secrets)

    @staticmethod
    def _parse_configs(raw: Any, service: ServiceDefinition) -> List[BundleConfig]:
        if not isinstance(raw, list):
            raise BundleValidationError("'configs' must be a list of {dest, content}")

        # The declared env_file (if any) is off-limits to configs — it holds
        # secrets and is written 0600 from the secrets section, never 0644 here.
        env_file_norm = _norm(service.env_file) if service.env_file else None

        out: List[BundleConfig] = []
        seen_dests = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise BundleValidationError("each config must be a mapping {dest, content}")
            extra = set(entry.keys()) - ALLOWED_CONFIG_KEYS
            if extra:
                raise BundleValidationError(
                    "config has unknown keys {} (allowed: dest, content)".format(
                        ", ".join(sorted(extra))
                    )
                )
            dest = entry.get("dest")
            content = entry.get("content")
            _validate_dest(dest)
            if not isinstance(content, str):
                raise BundleValidationError("config {!r}: content must be a string".format(dest))
            content_bytes = content.encode("utf-8", errors="surrogateescape")
            if len(content_bytes) > BUNDLE_MAX_BYTES:
                raise BundleValidationError(
                    "config {!r}: content too large ({} bytes; max {})".format(
                        dest, len(content_bytes), BUNDLE_MAX_BYTES
                    )
                )
            norm = _norm(dest)
            if norm in seen_dests:
                raise BundleValidationError("duplicate config dest {!r}".format(dest))
            seen_dests.add(norm)
            if env_file_norm is not None and norm == env_file_norm:
                raise BundleValidationError(
                    "config dest {!r} is the declared env_file — secrets go in the "
                    "'secrets' section (written 0600), not 'configs' (0644)".format(dest)
                )
            out.append(BundleConfig(dest=dest, content=content))
        return out

    @staticmethod
    def _parse_secrets(raw: Any, service: ServiceDefinition) -> Dict[str, str]:
        if not isinstance(raw, dict):
            raise BundleValidationError("'secrets' must be a mapping of ENV=value")
        out: Dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not ENV_KEY_RE.match(key):
                raise BundleValidationError("invalid secret env name {!r}".format(key))
            if not isinstance(value, str):
                # Do NOT include the value in the error (it is a secret).
                raise BundleValidationError("secret {!r}: value must be a string".format(key))
            out[key] = value
        if out and not service.env_file:
            raise BundleValidationError(
                "bundle has secrets but the service declares no env_file to receive "
                "them (set env_file in the manifest)"
            )
        _guard_total_size(out)
        return out


def _validate_dest(dest: Any) -> None:
    """A config dest: a non-empty relative subpath, no absolute, no '..'."""
    if not isinstance(dest, str) or not dest:
        raise BundleValidationError("config dest must be a non-empty string")
    p = PurePosixPath(dest)
    if p.is_absolute() or ".." in p.parts:
        raise BundleValidationError(
            "invalid config dest {!r}: absolute paths and '..' are not allowed".format(dest)
        )


def _norm(rel: str) -> str:
    """Normalize a relative subpath for equality/dup checks (no filesystem access)."""
    return PurePosixPath(rel).as_posix()


def _guard_total_size(secrets: Dict[str, str]) -> None:
    total = sum(len(k.encode()) + len(v.encode("utf-8", errors="surrogateescape")) for k, v in secrets.items())
    if total > BUNDLE_MAX_BYTES:
        raise BundleValidationError(
            "secrets section too large ({} bytes; max {})".format(total, BUNDLE_MAX_BYTES)
        )
