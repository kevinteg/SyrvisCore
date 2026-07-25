"""
Instance bundle schema for the core tier — the input to ``syrvis apply``.

A ``syrvis-instance`` bundle is the RESOLVED, self-contained declaration of an
instance's core configuration: the runtime ``.env`` content, the core-tier
``stack.yaml`` enablement, and the complete ``services.d/`` declaration set.
It is the core-tier sibling of ``syrvis-bundle`` (bundle.py, design/21): the
deployment repo renders its instance intent (deployment.yaml + sops secrets)
into one JSON document and streams it over the operator seam —

    <instance.json> | sudo syrvis apply

— so nothing outside SyrvisCore ever writes a file under ``$SYRVIS_HOME/config``.
Applying the bundle only WRITES configuration; converging runtime state to it
stays a separate, already-seamed step (``syrvis stack apply`` / ``reconcile``).

SECURITY: like a deploy bundle, this is attacker-controlled input that root
turns into filesystem writes, so every section is strictly validated:

- ``env`` keys are charset-checked, values single-line; a bundle-supplied
  ``SYRVIS_HOME`` must equal the real home (a wrong one would corrupt compose
  interpolation). Changing an EXISTING secret value additionally requires
  ``--allow-secret-change`` so token rotation stays a deliberate act.
- ``stack`` is validated against the known core-tier services; disabling a
  primordial service is rejected, unknown services are rejected.
- every ``declarations`` entry runs the full :class:`ServiceDefinition` trust
  boundary (strict mode — never tolerant). The section is a REPLACE SET:
  declarations absent from the bundle are removed from ``services.d/``
  (matching the "the directory IS the declared set" contract; the containers
  themselves are only ever removed by ``reconcile --prune``).

Secret VALUES are never echoed in errors or reports — reports name keys only.
"""

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from syrviscore.errors import SyrvisError

from . import stack as stack_mod
from .config_reader import is_secret_key
from .service_schema import ServiceDefinition, ServiceValidationError
from .services_d import declaration_path, get_declarations_dir
from .validators import parse_env_file

# The wire format version. Bumped only on a breaking schema change; ``apply``
# refuses an unknown apiVersion rather than guessing.
INSTANCE_API_VERSION = "syrvis-instance/v1"

ALLOWED_INSTANCE_KEYS = frozenset({"apiVersion", "env", "stack", "declarations"})
ALLOWED_STACK_KEYS = frozenset({"version", "services"})

# Matches the KEY charset setup.py generates and validators.parse_env_file reads.
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Per-section caps (the CLI additionally caps the whole stream at 1 MiB).
ENV_MAX_BYTES = 65536
DECLARATION_MAX_BYTES = 65536


class InstanceBundleError(SyrvisError, ValueError):
    """A syrvis-instance bundle failed validation (unsafe or malformed).

    Also a ValueError so existing ``except ValueError`` call sites keep catching it.
    """

    code = "instance_bundle_invalid"


@dataclass
class InstanceBundle:
    """A resolved, self-contained core-tier configuration bundle."""

    env: Optional[Dict[str, str]] = None
    stack: Optional[Dict[str, Any]] = None  # validated raw mapping (stack.from_dict shape)
    declarations: Dict[str, ServiceDefinition] = field(default_factory=dict)
    has_declarations: bool = False  # distinguishes {} (remove all) from absent

    @classmethod
    def from_dict(cls, data: Any) -> "InstanceBundle":
        if not isinstance(data, dict):
            raise InstanceBundleError("instance bundle must be a mapping")

        unknown = set(data.keys()) - ALLOWED_INSTANCE_KEYS
        if unknown:
            raise InstanceBundleError(
                "unknown bundle keys {} (allowed: {})".format(
                    ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_INSTANCE_KEYS))
                )
            )

        api = data.get("apiVersion")
        if api != INSTANCE_API_VERSION:
            raise InstanceBundleError(
                "unsupported bundle apiVersion {!r} (expected {!r})".format(
                    api, INSTANCE_API_VERSION
                )
            )

        if not any(k in data for k in ("env", "stack", "declarations")):
            raise InstanceBundleError(
                "bundle declares nothing (provide at least one of env, stack, declarations)"
            )

        env = cls._parse_env(data["env"]) if "env" in data else None
        stack = cls._parse_stack(data["stack"]) if "stack" in data else None
        declarations = {}
        has_declarations = "declarations" in data
        if has_declarations:
            declarations = cls._parse_declarations(data["declarations"])

        return cls(
            env=env,
            stack=stack,
            declarations=declarations,
            has_declarations=has_declarations,
        )

    @staticmethod
    def _parse_env(raw: Any) -> Dict[str, str]:
        if not isinstance(raw, dict) or not raw:
            raise InstanceBundleError("'env' must be a non-empty mapping of KEY: value")
        out: Dict[str, str] = {}
        total = 0
        for key, value in raw.items():
            if not isinstance(key, str) or not ENV_NAME_RE.fullmatch(key):
                raise InstanceBundleError("invalid env key {!r}".format(key))
            if not isinstance(value, str):
                # Do NOT include the value in the error (it may be a secret).
                raise InstanceBundleError("env {!r}: value must be a string".format(key))
            # .env is line-oriented; an embedded newline would inject extra
            # attacker-chosen KEY=value lines. Never echo the value.
            if "\n" in value or "\r" in value:
                raise InstanceBundleError("env {!r}: value must not contain a newline".format(key))
            # Strip surrounding whitespace to match how the value is read back:
            # both validators.parse_env_file and python-dotenv strip, so an
            # un-stripped value could never compare equal to what apply wrote,
            # making every re-apply report the key "changed" (and, for a secret,
            # refusing re-apply without --allow-secret-change). Strip once here.
            value = value.strip()
            total += len(key.encode()) + len(value.encode("utf-8", errors="surrogateescape"))
            out[key] = value
        if total > ENV_MAX_BYTES:
            raise InstanceBundleError(
                "env section too large ({} bytes; max {})".format(total, ENV_MAX_BYTES)
            )
        if not out.get("DOMAIN"):
            raise InstanceBundleError("env must declare a non-empty DOMAIN")
        return out

    @staticmethod
    def _parse_stack(raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise InstanceBundleError("'stack' must be a mapping")
        unknown = set(raw.keys()) - ALLOWED_STACK_KEYS
        if unknown:
            raise InstanceBundleError(
                "unknown stack keys {} (allowed: {})".format(
                    ", ".join(sorted(unknown)), ", ".join(sorted(ALLOWED_STACK_KEYS))
                )
            )
        version = raw.get("version", stack_mod.STACK_SCHEMA_VERSION)
        if version != stack_mod.STACK_SCHEMA_VERSION:
            raise InstanceBundleError(
                "unsupported stack version {!r} (expected {})".format(
                    version, stack_mod.STACK_SCHEMA_VERSION
                )
            )
        services = raw.get("services")
        if not isinstance(services, dict) or not services:
            raise InstanceBundleError("'stack.services' must be a non-empty mapping")
        for name, entry in services.items():
            if name not in stack_mod.ALL_SERVICES:
                raise InstanceBundleError(
                    "unknown core service {!r} (known: {})".format(
                        name, ", ".join(stack_mod.ALL_SERVICES)
                    )
                )
            if not isinstance(entry, dict):
                raise InstanceBundleError("stack service {!r} must be a mapping".format(name))
            enabled = entry.get("enabled", name in stack_mod.PRIMORDIAL)
            if not isinstance(enabled, bool):
                raise InstanceBundleError(
                    "stack service {!r}: 'enabled' must be a boolean".format(name)
                )
            if name in stack_mod.PRIMORDIAL and not enabled:
                raise InstanceBundleError("'{}' is primordial and cannot be disabled".format(name))
            for key, value in entry.items():
                if key == "enabled":
                    continue
                if not isinstance(key, str) or not isinstance(value, (str, int, bool)):
                    raise InstanceBundleError(
                        "stack service {!r}: setting {!r} must be a scalar".format(name, key)
                    )
        return {"version": stack_mod.STACK_SCHEMA_VERSION, "services": services}

    @staticmethod
    def _parse_declarations(raw: Any) -> Dict[str, ServiceDefinition]:
        if not isinstance(raw, dict):
            raise InstanceBundleError("'declarations' must be a mapping of name -> manifest")
        out: Dict[str, ServiceDefinition] = {}
        for name, manifest in raw.items():
            if not isinstance(manifest, dict):
                raise InstanceBundleError("declaration {!r} must be a mapping".format(name))
            body = yaml.safe_dump(manifest)
            if len(body.encode("utf-8", errors="surrogateescape")) > DECLARATION_MAX_BYTES:
                raise InstanceBundleError(
                    "declaration {!r} too large (max {} bytes)".format(name, DECLARATION_MAX_BYTES)
                )
            # The full Layer 2 trust boundary — STRICT mode, never tolerant: an
            # unaudited key must be rejected here exactly as on the deploy path.
            try:
                service = ServiceDefinition.from_dict(manifest)
            except ServiceValidationError as e:
                raise InstanceBundleError("invalid declaration {!r}: {}".format(name, e)) from e
            if service.name != name:
                raise InstanceBundleError(
                    "declaration {!r} declares name {!r} — they must match".format(
                        name, service.name
                    )
                )
            out[name] = service
        return out


# =============================================================================
# Apply
# =============================================================================


def apply_instance_bundle(
    bundle: InstanceBundle,
    home: Path,
    allow_secret_change: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply (or plan) an instance bundle against ``$SYRVIS_HOME``.

    Returns a report dict naming what changed — env/declaration KEY NAMES only,
    never values. Raises :class:`InstanceBundleError` on a guard violation
    (secret change without ``allow_secret_change``, SYRVIS_HOME mismatch).
    """
    report: Dict[str, Any] = {"dry_run": dry_run, "env": None, "stack": None, "declarations": None}

    env_plan = (
        _plan_env(bundle, home, allow_secret_change, dry_run) if bundle.env is not None else None
    )
    stack_plan = _plan_stack(bundle, home) if bundle.stack is not None else None
    decl_plan = _plan_declarations(bundle, home) if bundle.has_declarations else None

    if env_plan:
        report["env"] = env_plan["report"]
    if stack_plan:
        report["stack"] = stack_plan["report"]
    if decl_plan:
        report["declarations"] = decl_plan["report"]

    if dry_run:
        return report

    # Writes are ordered config-first (env, stack) then the declaration set;
    # each individual write is atomic (temp + rename in the target directory),
    # and re-applying the same bundle is idempotent, so a failure mid-way is
    # recovered by re-running apply.
    if env_plan and env_plan["report"]["action"] != "unchanged":
        _write_env(env_plan, home)
    if stack_plan and stack_plan["report"]["action"] != "unchanged":
        _atomic_write(stack_plan["path"], stack_plan["content"], 0o644)
    if decl_plan:
        _write_declarations(decl_plan)

    return report


# --- env ---------------------------------------------------------------------


def _env_path(home: Path) -> Path:
    return home / "config" / ".env"


def _render_env(env: Dict[str, str]) -> str:
    lines = [
        "# SyrvisCore configuration — applied from a syrvis-instance bundle",
        "# (syrvis apply). Hand-edits are overwritten by the next apply.",
    ]
    lines += ["{}={}".format(k, v) for k, v in env.items()]
    return "\n".join(lines) + "\n"


def _plan_env(
    bundle: InstanceBundle, home: Path, allow_secret_change: bool, dry_run: bool = False
) -> Dict[str, Any]:
    env = dict(bundle.env or {})
    declared_home = env.get("SYRVIS_HOME")
    if declared_home is not None and declared_home != str(home):
        raise InstanceBundleError(
            "bundle env SYRVIS_HOME {!r} does not match this install ({})".format(
                declared_home, home
            )
        )
    env.setdefault("SYRVIS_HOME", str(home))

    path = _env_path(home)
    existing = parse_env_file(path)

    added = sorted(k for k in env if k not in existing)
    removed = sorted(k for k in existing if k not in env)
    changed = sorted(k for k in env if k in existing and existing[k] != env[k])

    # Token rotation is a deliberate act: changing or dropping an existing,
    # non-empty secret value needs the explicit flag. Setting a secret that was
    # empty/absent is a normal first configuration and passes. On a dry-run the
    # guard is NOT enforced — a plan writes nothing and reports key names only,
    # so a deployment can safely PREVIEW a rotation over the seam (there is no
    # `apply --dry-run --allow-secret-change` argv shape); the mutating apply
    # still requires the flag.
    secret_changes = [k for k in changed if is_secret_key(k) and existing.get(k)]
    secret_changes += [k for k in removed if is_secret_key(k) and existing.get(k)]
    if secret_changes and not allow_secret_change and not dry_run:
        raise InstanceBundleError(
            "refusing to change existing secret value(s) {} without "
            "--allow-secret-change".format(", ".join(sorted(secret_changes)))
        )

    content = _render_env(env)
    if path.exists() and not added and not removed and not changed:
        action = "unchanged"
    elif path.exists():
        action = "update"
    else:
        action = "create"
    return {
        "path": path,
        "content": content,
        "report": {"action": action, "added": added, "changed": changed, "removed": removed},
    }


def _write_env(plan: Dict[str, Any], home: Path) -> None:
    path = plan["path"]
    # Preserve the existing owner (setup chowns .env to the operating user so
    # unprivileged reads keep working); a fresh file stays with the caller (root).
    owner = None
    if path.exists():
        st = path.stat()
        owner = (st.st_uid, st.st_gid)
    _atomic_write(path, plan["content"], 0o600, owner=owner)


# --- stack -------------------------------------------------------------------


def _plan_stack(bundle: InstanceBundle, home: Path) -> Dict[str, Any]:
    stack = stack_mod.from_dict(bundle.stack)
    path = home / "config" / "stack.yaml"
    content = stack.to_yaml()

    previous = stack_mod.Stack(services={})
    if path.exists():
        try:
            previous = stack_mod.from_dict(yaml.safe_load(path.read_text()) or {})
        except Exception:  # noqa: BLE001 - unreadable prior stack: treat as empty
            pass
    enabled_changes = {
        name: stack.is_enabled(name)
        for name in stack_mod.ALL_SERVICES
        if stack.is_enabled(name) != previous.is_enabled(name)
    }
    if path.exists() and path.read_text() == content:
        action = "unchanged"
    elif path.exists():
        action = "update"
    else:
        action = "create"
    return {
        "path": path,
        "content": content,
        "report": {"action": action, "enabled_changes": enabled_changes},
    }


# --- declarations ------------------------------------------------------------


def _plan_declarations(bundle: InstanceBundle, home: Path) -> Dict[str, Any]:
    directory = get_declarations_dir(home)
    existing = {p.stem: p for p in sorted(directory.glob("*.yaml"))} if directory.exists() else {}

    writes: List[Dict[str, Any]] = []
    unchanged: List[str] = []
    for name, service in bundle.declarations.items():
        data = service.to_dict()
        content = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
        path = declaration_path(home, name)
        if path.exists() and path.read_text() == content:
            unchanged.append(name)
            continue
        # Same perms policy as dump_definition: inline env entries -> 0600.
        writes.append(
            {
                "path": path,
                "content": content,
                "mode": 0o600 if service.environment else 0o644,
                "name": name,
            }
        )
    removals = [existing[n] for n in sorted(existing) if n not in bundle.declarations]

    return {
        "directory": directory,
        "writes": writes,
        "removals": removals,
        "report": {
            "written": sorted(w["name"] for w in writes),
            "unchanged": sorted(unchanged),
            "removed": sorted(p.stem for p in removals),
        },
    }


def _write_declarations(plan: Dict[str, Any]) -> None:
    directory = plan["directory"]
    directory.mkdir(parents=True, exist_ok=True)
    for write in plan["writes"]:
        _atomic_write(write["path"], write["content"], write["mode"])
    for path in plan["removals"]:
        # The report already lists this name under `removed`; if the unlink
        # fails (e.g. a stray directory shadows the file, or perms), the removal
        # did NOT happen — surface it instead of reporting a false convergence.
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # already gone — the desired end state
        except OSError as e:
            raise InstanceBundleError("could not remove declaration {!r}: {}".format(path.stem, e))


# =============================================================================
# Export — the read companion to apply
# =============================================================================


def export_instance(home: Path, reveal_secrets: bool = False) -> Dict[str, Any]:
    """Read the live instance and emit it as a ``syrvis-instance/v1`` bundle.

    The inverse of :func:`apply_instance_bundle`: snapshot ``.env`` +
    ``stack.yaml`` + the complete ``services.d/`` set for GitOps snapshotting,
    diffing a desired bundle against reality, or disaster-recovery inspection.

    Secret VALUES are **redacted** by default (``****``) — the export is safe to
    print, commit, and diff. ``reveal_secrets=True`` includes real values (for a
    re-appliable backup); callers must treat that output as sensitive. The
    structure round-trips through ``apply`` (an exported redacted bundle applies
    cleanly except that redacted secrets are refused as a secret *change*).
    """
    home = Path(home)

    env: Dict[str, str] = {}
    env_path = _env_path(home)
    if env_path.exists():
        raw_env = parse_env_file(env_path)
        # Any var the operator forwards as a DNS-01 provider credential
        # (TRAEFIK_ACME_DNS_ENV) is a secret even if its name lacks a marker.
        extra_secret = {
            n.strip() for n in raw_env.get("TRAEFIK_ACME_DNS_ENV", "").split(",") if n.strip()
        }
        for key, value in raw_env.items():
            if not reveal_secrets and value and (is_secret_key(key) or key in extra_secret):
                env[key] = _REDACTED
            else:
                env[key] = value

    stack_doc = None
    stack_path = home / "config" / "stack.yaml"
    if stack_path.exists():
        try:
            loaded = stack_mod.from_dict(yaml.safe_load(stack_path.read_text()) or {})
            stack_doc = loaded.to_dict()
        except Exception:  # noqa: BLE001 - unreadable stack: omit the section
            stack_doc = None

    declarations: Dict[str, Any] = {}
    directory = get_declarations_dir(home)
    if directory.exists():
        for path in sorted(directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
            except Exception as e:  # noqa: BLE001
                # Fail loud: a snapshot that silently omits a declaration would,
                # on a later restore (declarations are a replace set), DELETE
                # that service's intent — worse than an error.
                raise InstanceBundleError(
                    "cannot export declaration {!r}: {} (fix or remove it first)".format(
                        path.stem, e
                    )
                )
            if isinstance(data, dict):
                declarations[path.stem] = _redact_declaration(data, reveal_secrets)

    bundle: Dict[str, Any] = {"apiVersion": INSTANCE_API_VERSION}
    if env:
        bundle["env"] = env
    if stack_doc:
        bundle["stack"] = stack_doc
    bundle["declarations"] = declarations
    return bundle


_REDACTED = "****"


def _redact_declaration(data: Dict[str, Any], reveal_secrets: bool) -> Dict[str, Any]:
    """Redact secret VALUES in a declaration's inline ``environment`` list.

    A declaration may carry ``environment: ["KEY=VALUE"]`` where KEY is a secret
    (the codebase writes such manifests 0600). export's default output is meant
    to be safe to commit/diff and it transits the operator seam, so mask those
    values exactly as the top-level env section is masked.
    """
    if reveal_secrets or "environment" not in data:
        return data
    env_list = data.get("environment")
    if not isinstance(env_list, list):
        return data
    redacted = []
    for entry in env_list:
        if isinstance(entry, str) and "=" in entry:
            key, _, value = entry.partition("=")
            if value and is_secret_key(key):
                redacted.append("{}={}".format(key, _REDACTED))
                continue
        redacted.append(entry)
    out = dict(data)
    out["environment"] = redacted
    return out


# --- shared ------------------------------------------------------------------


def _atomic_write(path: Path, content: str, mode: int, owner=None) -> None:
    """Write ``content`` atomically (temp + rename in the target directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".{}.".format(path.name))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        if owner is not None:
            try:
                os.chown(tmp_name, owner[0], owner[1])
            except OSError:
                pass  # non-root callers (tests) cannot chown; perms still apply
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
