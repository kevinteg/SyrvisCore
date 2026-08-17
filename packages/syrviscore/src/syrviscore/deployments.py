"""Deployment records: per-workload revision history + rollback targets.

Positions the managed stacks as a deployment system: every mutation that
changes what a workload RUNS (image, env, volumes, routing) appends one
immutable record under ``$SYRVIS_HOME/data/deployments/<workload>/<NNNN>.json``.
``syrvis history`` reads them (redacted); ``syrvis service rollback`` re-deploys
a prior revision's manifest through the full trust boundary.

Recording discipline (the part that must be right):

- **One record per logical action.** All materializations funnel through
  ``ServiceManager._install_from_definition``; a reconcile REPLACE (remove with
  ``keep_declaration=True`` + reinstall) records ONE deploy, never
  delete+create. ``deploy_bundle`` suppresses the inner install record and
  records once at its own end (after secrets land and the container starts).
- **Best-effort writes.** A history-write failure must NEVER fail a deployment
  (same discipline as the services.d dual-write): every write API catches all
  exceptions and returns ``None``.
- **Secrets never in the display path.** Records store env NAMES at top level;
  the full manifest snapshot (needed for rollback fidelity) keeps inline
  ``KEY=VALUE`` entries, so a record file carrying inline env is written
  0640 + services.d shared group (mirroring installed manifests) — readable by
  the operator, never world-readable. ``load_history`` masks every inline env
  value; only rollback reads the raw record. ``env_file`` contents (the real
  secret home) are never stored — only the declared file NAME.

Rollback is GitOps-ephemeral by design: it rewrites the services.d declaration
CONTENT to the old image, so the next IaC apply/reconcile that still declares
the newer image will redeploy it (exactly like ``kubectl rollout undo`` before
the repo is reverted). Both movements appear in history.

Kept import-light and Python 3.8-clean (runs on the DSM 3.8 CLI).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import SyrvisError

SCHEMA = "syrvis-deployment/v1"
DEPLOYMENTS_DIRNAME = "deployments"

# Instance-level workload ids. The '@' prefix can never collide with a service
# name (NAME_RE requires a leading [a-z0-9]).
CORE_WORKLOAD = "@core"
INSTANCE_WORKLOADS = frozenset({CORE_WORKLOAD})

# Auto-trim cap: only the newest KEEP record files are retained per workload
# (revision numbers keep climbing past it — a trimmed revision is simply no
# longer a valid rollback target).
DEFAULT_KEEP = 50

_REDACTED = "****"

# Valid record actions; rollback targets must be a *successful* one of these.
ROLLBACKABLE_ACTIONS = frozenset({"deploy", "rollback"})


class DeploymentError(SyrvisError):
    """A deployment-history read/rollback-target failure."""

    code = "deployment_invalid"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def deployments_dir(home: Path) -> Path:
    return Path(home) / "data" / DEPLOYMENTS_DIRNAME


def workload_dir(home: Path, workload: str) -> Path:
    """The record directory for one workload (validated, containment-checked)."""
    if workload not in INSTANCE_WORKLOADS:
        from .service_schema import validate_service_name

        validate_service_name(workload, "workload name")
    base = deployments_dir(home)
    resolved = Path(os.path.realpath(str(base / workload)))
    base_real = os.path.realpath(str(base))
    if str(resolved) != base_real and not str(resolved).startswith(base_real + os.sep):
        raise DeploymentError("workload {!r} escapes the deployments directory".format(workload))
    return base / workload


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _image_digest(image: str) -> Optional[str]:
    """The @sha256:... component IFF present in the ref (no docker call)."""
    if "@" in image:
        return image.split("@", 1)[1]
    return None


def _volume_entries(service) -> List[Dict[str, str]]:
    """Normalize declared volumes as {source, target, mode, kind} (as declared)."""
    from .service_schema import INFRA_HOST_MOUNTS

    entries = []
    for vol in service.volumes or []:
        parts = vol.split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"
        # The schema only admits two shapes: an enumerated read-only infra host
        # mount, or a path relative to the service's own data directory.
        is_infra_mount = service.tier == "infra" and source in INFRA_HOST_MOUNTS
        kind = "infra-host" if is_infra_mount else "service-data"
        entries.append({"source": source, "target": target, "mode": mode, "kind": kind})
    return entries


def _hostname(service) -> Optional[str]:
    """Best-effort effective hostname for a routed service."""
    if not (service.traefik.enabled and service.traefik.subdomain):
        return None
    domain = service.traefik.domain
    if not domain:
        try:
            from .traefik_config import get_domain_from_env

            domain = get_domain_from_env()
        except Exception:  # noqa: BLE001 - best-effort display field
            domain = None
    if not domain:
        return service.traefik.subdomain
    return "{}.{}".format(service.traefik.subdomain, domain)


def _config_checksums(home: Path, service, configs=None) -> Dict[str, str]:
    """sha256 of materialized config files (never their contents).

    Covers config_templates dests present in data/<name>/ plus any deploy-bundle
    configs. Best-effort: unreadable files are skipped.

    READ BACK by :func:`last_materialized_digests` (0.5.16, design/60 G1): this
    map was computed on every deploy since the record schema shipped and never
    consulted, so ``deploy_bundle`` had no way to tell a byte-identical redeploy
    from a real change and force-recreated the container either way.
    """
    import hashlib

    sums: Dict[str, str] = {}
    location = getattr(service, "location", "") or ""
    if location:
        # v2 app home (design/26): rendered configs live under home/config. The
        # apps-root segment is instance config (SYRVIS_APPS_ROOT_NAME), resolved
        # against the SAME home the record is being written under.
        from . import paths as paths_mod

        data_dir = (
            paths_mod.resolve_volume_root(location)
            / paths_mod.get_apps_root_name(home)
            / "apps"
            / service.name
            / "config"
        )
    else:
        data_dir = Path(home) / "data" / service.name
    dests = [t.dest for t in (service.config_templates or [])]
    for cfg in configs or []:
        dest = getattr(cfg, "dest", None)
        if dest:
            dests.append(dest)
    for dest in dests:
        path = data_dir / dest
        try:
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                sums[dest] = "sha256:{}".format(digest)
        except OSError:
            continue
    return sums


def content_digest(content) -> str:
    """``sha256:<hex>`` of ``content`` — the digest the record stores.

    One helper so the WRITE side (digesting what a deploy materialized) and the
    COMPARE side (digesting what the incoming bundle would materialize) can
    never encode the same bytes differently. ``surrogateescape`` mirrors
    ``ServiceManager._place_config``/``write_secret``, which is how the bytes
    actually reach disk.
    """
    import hashlib

    if isinstance(content, str):
        content = content.encode("utf-8", errors="surrogateescape")
    return "sha256:{}".format(hashlib.sha256(content).hexdigest())


def last_materialized_digests(home: Path, workload: str) -> Optional[Dict[str, Any]]:
    """What the NEWEST record says this workload's configs + env_file held.

    The read half of design/60 G1 ("a no-op apply of an unmodified stack
    restarts zero containers"), consumed by ``deploy_bundle``.

    NEWEST, not newest-*successful*, and that is the safe choice: a failed
    update records the digests of what it managed to materialize before it
    failed, so comparing against it makes a retry of the same bundle look
    UNCHANGED — which is correct, because a failed update also tears the
    container down and the next ``up -d`` therefore creates it fresh from the
    files on disk. Comparing against an older successful record instead would
    describe bytes that are no longer there.

    Returns ``None`` when nothing is recorded (→ the caller must assume
    "changed"; an unknown prior state may never be read as a no-op).
    """
    try:
        record = latest_revision(Path(home), workload)
    except Exception:  # noqa: BLE001 - an unreadable history never blocks a deploy
        return None
    if not isinstance(record, dict):
        return None
    sums = record.get("config_checksums")
    return {
        "revision": record.get("revision"),
        "action": record.get("action"),
        "outcome": record.get("outcome"),
        "config_checksums": sums if isinstance(sums, dict) else {},
        # Absent on records written before 0.5.16 and on non-deploy actions —
        # None means "unknown", which the comparison treats as changed.
        "secrets_checksum": record.get("secrets_checksum"),
    }


# ---------------------------------------------------------------------------
# Write API (best-effort: returns the record dict, or None on ANY failure)
# ---------------------------------------------------------------------------


def record_service_deploy(
    home: Path,
    service,
    action: str,
    trigger: str,
    outcome: str,
    previous_image: Optional[str] = None,
    rollback_of: Optional[int] = None,
    detail: str = "",
    configs=None,
    secrets_checksum: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Append one record for a Layer 2 service action. Never raises.

    ``secrets_checksum`` (0.5.16) is the ``sha256:`` of the env_file BODY this
    action materialized — never the body, never a per-key digest. It exists so
    the next deploy can tell "the same secrets" from "rotated secrets" and skip
    the container force-recreate in the first case (design/60 G1). A record
    carrying one is written 0640 like an inline-env record, so the digest is no
    more readable than the env NAMES already beside it.
    """
    try:
        env_names = [e.split("=", 1)[0] for e in (service.environment or [])]
        routed = bool(service.traefik.enabled and service.traefik.subdomain)
        payload = {
            "schema": SCHEMA,
            "workload": service.name,
            "tier": "service",
            "action": action,
            "trigger": trigger,
            "outcome": outcome,
            "detail": detail,
            "image": service.image,
            "image_digest": _image_digest(service.image),
            "previous_image": previous_image,
            "version": service.version,
            "exposure": service.traefik.exposure if routed else None,
            "hostname": _hostname(service),
            "ports": [service.traefik.port] if routed else [],
            "env_names": env_names,
            "env_file": service.env_file or None,
            "volumes": _volume_entries(service),
            "config_checksums": _config_checksums(home, service, configs),
            "secrets_checksum": secrets_checksum,
            "source_url": service.source_url,
            "tier_selector": service.tier,
            # design/26: where the app's home lives ("" = legacy layout).
            # Additive v1-schema field; readers are .get()-safe.
            "location": getattr(service, "location", "") or "",
            "rollback_of": rollback_of,
            "manifest": service.to_dict(),
        }
        return _write_record(Path(home), service.name, payload)
    except Exception:  # noqa: BLE001 - recording must never fail a deployment
        return None


def record_service_remove(
    home: Path,
    name: str,
    trigger: str,
    previous_image: Optional[str] = None,
    detail: str = "",
) -> Optional[Dict[str, Any]]:
    """Append a removal record (no manifest — the service is gone). Never raises."""
    try:
        payload = {
            "schema": SCHEMA,
            "workload": name,
            "tier": "service",
            "action": "remove",
            "trigger": trigger,
            "outcome": "success",
            "detail": detail,
            "image": None,
            "image_digest": None,
            "previous_image": previous_image,
            "version": None,
            "exposure": None,
            "hostname": None,
            "ports": [],
            "env_names": [],
            "env_file": None,
            "volumes": [],
            "config_checksums": {},
            "secrets_checksum": None,
            "source_url": None,
            "tier_selector": "",
            "location": None,
            "rollback_of": None,
            "manifest": None,
        }
        return _write_record(Path(home), name, payload)
    except Exception:  # noqa: BLE001
        return None


def record_core_apply(
    home: Path,
    pins: Dict[str, str],
    core_enabled: List[str],
    trigger: str,
    outcome: str = "success",
    previous_pins: Optional[Dict[str, str]] = None,
    detail: str = "",
) -> Optional[Dict[str, Any]]:
    """Append an instance-level '@core' record for a stack apply. Never raises."""
    try:
        payload = {
            "schema": SCHEMA,
            "workload": CORE_WORKLOAD,
            "tier": "core",
            "action": "stack-apply",
            "trigger": trigger,
            "outcome": outcome,
            "detail": detail,
            "pins": dict(pins),
            "previous_pins": dict(previous_pins) if previous_pins else None,
            "core_enabled": sorted(core_enabled),
            "rollback_of": None,
            "manifest": None,
        }
        return _write_record(Path(home), CORE_WORKLOAD, payload)
    except Exception:  # noqa: BLE001
        return None


def _write_record(home: Path, workload: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Claim the next revision number atomically and persist the record.

    ``os.link(tmp, NNNN.json)`` is atomic and fails if the number was taken, so
    concurrent writers converge on monotonic numbering without a lock.
    """
    directory = workload_dir(home, workload)
    directory.mkdir(parents=True, exist_ok=True)

    payload = dict(payload)
    payload["timestamp"] = _utc_now()

    revision = next_revision(directory)
    tmp = directory / ".{}.{}.tmp".format(revision, os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        for _ in range(1000):
            payload["revision"] = revision
            body = json.dumps(payload, indent=2, default=str)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
            dest = directory / "{:04d}.json".format(revision)
            try:
                os.link(str(tmp), str(dest))
                break
            except FileExistsError:
                revision += 1
        else:
            raise DeploymentError("could not claim a revision number for {!r}".format(workload))
    finally:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass

    _apply_record_perms(home, dest, payload)
    _trim(directory)
    return payload


def _apply_record_perms(home: Path, path: Path, payload: Dict[str, Any]) -> None:
    """0644 default; 0640 + services.d shared group when inline env is present.

    Mirrors ``_write_manifest``: history stays a NON-sudo operator read while
    inline env values never become world-readable. Best-effort.

    A ``secrets_checksum`` counts as env material for this purpose (0.5.16): a
    digest is not a value, but a digest of a SHORT low-entropy secret is a
    confirmation oracle, so it is held to the same 0640 as the env names.
    """
    try:
        if payload.get("env_names") or payload.get("secrets_checksum"):
            shared_gid = (Path(home) / "config" / "services.d").stat().st_gid
            os.chown(str(path), -1, shared_gid)
            path.chmod(0o640)
        else:
            path.chmod(0o644)
    except OSError:
        pass


def _record_files(directory: Path) -> List[Path]:
    """Record files sorted by revision number (ascending).

    Matches any all-digit stem — ``{:04d}`` is a MINIMUM width, so revision
    10000 writes ``10000.json`` and a fixed 4-char glob would lose it.
    """
    files = []
    for path in directory.glob("*.json"):
        if path.stem.isdigit() and path.is_file():
            files.append(path)
    return sorted(files, key=lambda p: int(p.stem))


def _trim(directory: Path, keep: Optional[int] = None) -> None:
    """Retain only the newest ``keep`` record files (best-effort)."""
    try:
        keep = DEFAULT_KEEP if keep is None else keep
        records = _record_files(directory)
        for path in records[:-keep] if len(records) > keep else []:
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def next_revision(directory: Path) -> int:
    """max(existing revision numbers) + 1, or 1 for a fresh workload."""
    highest = 0
    if directory.exists():
        records = _record_files(directory)
        if records:
            highest = int(records[-1].stem)
    return highest + 1


def _load_record_file(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("record must be a mapping")
    return data


def load_history(
    home: Path,
    workload: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """All records, newest-first per workload, REDACTED for display.

    Per-file corruption is isolated into ``invalid`` (mirroring
    services_d.load_declarations) — one bad record never hides the rest.
    """
    base = deployments_dir(Path(home))
    workloads: Dict[str, List[Dict[str, Any]]] = {}
    invalid: List[Dict[str, str]] = []

    if workload is not None:
        directories = [workload_dir(Path(home), workload)]
    elif base.exists():
        directories = sorted(p for p in base.iterdir() if p.is_dir())
    else:
        directories = []

    for directory in directories:
        if not directory.exists():
            continue
        records: List[Dict[str, Any]] = []
        for path in reversed(_record_files(directory)):
            try:
                records.append(_redact_for_output(_load_record_file(path)))
            except Exception as exc:  # noqa: BLE001 - isolation: report, keep loading
                invalid.append(
                    {"file": "{}/{}".format(directory.name, path.name), "error": str(exc)}
                )
        if limit is not None:
            records = records[:limit]
        if records:
            workloads[directory.name] = records

    return {"workloads": workloads, "invalid": invalid}


def load_revision(home: Path, workload: str, revision: int) -> Dict[str, Any]:
    """One RAW (unredacted) record — the rollback source of truth."""
    path = workload_dir(Path(home), workload) / "{:04d}.json".format(int(revision))
    if not path.exists():
        raise DeploymentError(
            "revision {} not found for {!r} (see 'syrvis history {}')".format(
                revision, workload, workload
            )
        )
    try:
        return _load_record_file(path)
    except Exception as exc:  # noqa: BLE001 - typed error at the boundary
        raise DeploymentError(
            "revision {} of {!r} is unreadable: {}".format(revision, workload, exc)
        )


def latest_revision(home: Path, workload: str) -> Optional[Dict[str, Any]]:
    """The newest readable record for a workload, or None."""
    directory = workload_dir(Path(home), workload)
    if not directory.exists():
        return None
    for path in reversed(_record_files(directory)):
        try:
            return _load_record_file(path)
        except Exception:  # noqa: BLE001 - skip corrupt, try older
            continue
    return None


def resolve_rollback_target(home: Path, workload: str, to: Optional[int] = None) -> Dict[str, Any]:
    """The record a rollback should restore (raises DeploymentError otherwise).

    A valid target is a *successful* deploy/rollback revision with a manifest.
    Without ``--to``, the newest such revision older than the current one wins.
    """
    current = latest_revision(home, workload)
    if current is None:
        raise DeploymentError("no deployment history for {!r}".format(workload))

    def usable(record: Dict[str, Any]) -> bool:
        return (
            record.get("action") in ROLLBACKABLE_ACTIONS
            and record.get("outcome") == "success"
            and isinstance(record.get("manifest"), dict)
        )

    if to is not None:
        target = load_revision(home, workload, to)
        if not usable(target):
            raise DeploymentError(
                "revision {} of {!r} is not a rollback target "
                "(need a successful deploy/rollback record)".format(to, workload)
            )
        return target

    directory = workload_dir(Path(home), workload)
    for path in reversed(_record_files(directory)):
        try:
            record = _load_record_file(path)
        except Exception:  # noqa: BLE001 - skip corrupt candidates
            continue
        if record.get("revision", 0) < current.get("revision", 0) and usable(record):
            return record
    raise DeploymentError(
        "no earlier successful revision to roll back to for {!r}".format(workload)
    )


def _redact_for_output(record: Dict[str, Any]) -> Dict[str, Any]:
    """Mask every inline env VALUE in the manifest snapshot (display path)."""
    out = dict(record)
    manifest = record.get("manifest")
    if isinstance(manifest, dict) and manifest.get("environment"):
        masked = dict(manifest)
        masked["environment"] = [
            "{}={}".format(e.split("=", 1)[0], _REDACTED) for e in manifest["environment"]
        ]
        out["manifest"] = masked
    return out
