"""
`syrvis verify` — read-only health + drift report.

This is the first-class health command from the v2 design: it reports whether
the installation is internally consistent (validators) AND whether the running
containers match what the compose file declares (drift). It never mutates
system state — remediation (`verify --fix`) is a separate, privileged concern
handled elsewhere. `--json` makes it the MCP layer's native health probe.

Tiers:
- ``verify --smoke``: fast, non-destructive subset (install + config +
  core-stack drift). The post-install gate for the tarball loop.
- ``verify``: the full report (adds DNS/cert/network validators).
"""

import json as jsonlib
from typing import Dict, List, Optional, Tuple

import click

from . import drift, paths, privilege, remediation, validators
from .docker_manager import DockerConnectionError, DockerError, DockerManager


def _core_compose_expected() -> Dict[str, str]:
    """Desired core-stack services from the generated docker-compose.yaml."""
    compose_path = paths.get_docker_compose_path()
    return drift.expected_services_from_compose(compose_path)


def _core_actual_from_docker() -> Dict[str, Dict[str, str]]:
    """Actual core container state from docker (read-only inspection)."""
    mgr = DockerManager()
    status = mgr.get_container_status()
    # get_container_status returns {service: {name,status,uptime,image}}
    return {
        svc: {"status": info.get("status", ""), "image": info.get("image", "")}
        for svc, info in status.items()
    }


def gather_static_config_drift() -> Optional[drift.DriftItem]:
    """Detect a traefik.yml newer than the running Traefik process.

    Traefik reads its STATIC config only at process start, so a regenerated
    ``traefik.yml`` that postdates the container's StartedAt is silently NOT in
    effect (the /ping-404 class of failure). Best-effort and read-only: any
    error (docker unreachable, file missing, container absent) yields None —
    the container-level drift kinds already cover those states.
    """
    try:
        static_path = paths.get_traefik_data_dir() / "traefik.yml"
        if not static_path.exists():
            return None

        import docker

        container = docker.from_env().containers.get("traefik")
        if container.status != "running":
            return None  # a stopped/missing traefik is reported as STOPPED/MISSING
        started_at = (container.attrs.get("State") or {}).get("StartedAt") or ""
        stale = drift.static_config_is_stale(static_path.stat().st_mtime, started_at)
        if not stale:
            return None
        from datetime import datetime, timezone

        mtime_iso = datetime.fromtimestamp(static_path.stat().st_mtime, timezone.utc).isoformat()
        return drift.DriftItem(
            service="traefik",
            kind=drift.DriftKind.STALE_STATIC,
            expected=mtime_iso,
            actual=started_at,
        )
    except Exception:  # noqa: BLE001 - best-effort; never break verify
        return None


def gather_core_drift(actual: Optional[Dict[str, Dict[str, str]]] = None) -> drift.DriftReport:
    """Build the core-stack drift report.

    Args:
        actual: injectable actual-state map (for tests / when docker state was
            already gathered). When None, it is read from docker.
    """
    expected = _core_compose_expected()
    if actual is None:
        actual = _core_actual_from_docker()
    report = drift.detect_drift("core", expected, actual)

    # Stale-static-config is a process-level drift the container diff can't see.
    stale = gather_static_config_drift()
    if stale is not None:
        report.items.append(stale)
    return report


def gather_l2_drift() -> Optional[drift.DriftReport]:
    """Drift report for the Layer 2 set: every installed service should be
    running its declared image.

    Returns None when no services are installed or docker is unreachable (core
    drift already reports docker-down); an unloadable manifest surfaces as a
    MISSING item rather than being silently skipped.
    """
    try:
        from .service_manager import ServiceManager
        from .service_schema import load_service_definition

        manager = ServiceManager()
        services_dir = manager.services_dir
        if not services_dir.exists():
            return None

        expected: Dict[str, str] = {}
        container_names: Dict[str, str] = {}
        for service_dir in sorted(services_dir.iterdir()):
            manifest = service_dir / "syrvis-service.yaml"
            if not service_dir.is_dir() or not manifest.exists():
                continue
            try:
                svc = load_service_definition(manifest)
                expected[svc.name] = svc.image
                container_names[svc.name] = svc.container_name or svc.name
            except Exception:  # noqa: BLE001 - broken manifest -> shows as MISSING
                expected[service_dir.name] = "unloadable-manifest"
                container_names[service_dir.name] = service_dir.name
        if not expected:
            return None

        import docker

        client = docker.from_env()
        actual: Dict[str, Dict[str, str]] = {}
        for name, cname in container_names.items():
            try:
                container = client.containers.get(cname)
                actual[name] = {
                    "status": container.status,
                    "image": (container.attrs.get("Config") or {}).get("Image") or "Unknown",
                }
            except Exception:  # noqa: BLE001 - absent container -> MISSING drift
                pass
        # L2 containers outside the declared set are not this scope's concern.
        return drift.detect_drift("layer2", expected, actual, flag_unexpected=False)
    except Exception:  # noqa: BLE001 - docker/library unavailable: skip the tier
        return None


def run_validation_reports(smoke: bool) -> List[validators.ValidationReport]:
    """Run the read-only validators appropriate to the tier."""
    reports = [
        validators.validate_installation(),
        validators.validate_configuration(),
    ]
    if not smoke:
        # The full tier adds the slower / network-touching validators.
        try:
            reports.extend(
                r
                for r in validators.validate_all()
                if r.category not in {rep.category for rep in reports}
            )
        except Exception:
            # validate_all touches DNS/certs; never let it crash verify
            pass
    return reports


def build_report(
    smoke: bool, actual: Optional[Dict[str, Dict[str, str]]] = None
) -> Dict[str, object]:
    """Assemble the full verify result as a plain dict (JSON-ready)."""
    result: Dict[str, object] = {"smoke": smoke, "healthy": True, "checks": [], "drift": None}

    checks_out = []
    for report in run_validation_reports(smoke):
        for check in report.checks:
            checks_out.append(
                {
                    "category": report.category,
                    "name": check.name,
                    "passed": check.passed,
                    "message": check.message,
                    "fixable": check.fixable,
                }
            )
            if not check.passed:
                result["healthy"] = False
    result["checks"] = checks_out

    # Drift — degrade gracefully if docker/compose are unavailable
    try:
        report = gather_core_drift(actual)
        result["drift"] = report.to_dict()
        if not report.in_sync:
            result["healthy"] = False
    except DockerConnectionError as e:
        result["drift"] = {"scope": "core", "error": "docker unreachable: {}".format(e)}
        result["healthy"] = False
    except FileNotFoundError as e:
        result["drift"] = {"scope": "core", "error": str(e)}
    except Exception as e:  # pragma: no cover - defensive
        result["drift"] = {"scope": "core", "error": str(e)}

    # Layer 2 drift — the declared service set, not just the core stack.
    l2_report = gather_l2_drift()
    if l2_report is not None:
        result["l2_drift"] = l2_report.to_dict()
        if not l2_report.in_sync:
            result["healthy"] = False
    else:
        result["l2_drift"] = None

    return result


def _reconcile_core_drift() -> Tuple[bool, str]:
    """Bring the core stack back to the compose-declared state.

    Pull declared images (best-effort — an image mismatch needs the new image
    present) then ``up -d``, which recreates any missing/stopped/wrong-image
    container to match compose. Requires root (docker socket + macvlan shim).
    """
    mgr = DockerManager()
    try:
        mgr.pull_core_images()
    except DockerError:
        # pull is best-effort; up -d will still recreate from local images
        pass
    mgr.start_core_services()
    return True, "core services reconciled to compose (pull + up -d)"


def remediate(smoke: bool) -> List[Dict[str, object]]:
    """Apply sanctioned remediations for a fixable-unhealthy install.

    Two kinds of remediation, all privileged (the caller must have elevated):
    1. Validator fixable issues → the shared remediation.apply_fix dispatch
       (docker group, socket perms, boot hook, symlink, manifest perms).
    2. Core drift → reconcile containers to the compose-declared state.

    Returns a structured list of the actions taken (for text/JSON reporting).
    """
    actions: List[Dict[str, object]] = []
    install_dir = remediation.resolve_install_dir()

    for report in run_validation_reports(smoke):
        for check in report.fixable_issues:
            ok, msg = remediation.apply_fix(check.fix_action, install_dir)
            actions.append(
                {
                    "target": check.name,
                    "action": check.fix_action,
                    "ok": ok,
                    "message": msg,
                }
            )

    try:
        report = gather_core_drift()
        stale_static = [i for i in report.items if i.kind is drift.DriftKind.STALE_STATIC]
        other_failures = [f for f in report.failures if f.kind is not drift.DriftKind.STALE_STATIC]
        if other_failures:
            ok, msg = _reconcile_core_drift()
            actions.append(
                {"target": "core-drift", "action": "compose_up", "ok": ok, "message": msg}
            )
        if stale_static:
            # `up -d` won't restart a container for a bind-mounted file change,
            # so the stale-static remediation is a targeted Traefik restart.
            from .docker_manager import restart_traefik_if_running

            ok = restart_traefik_if_running()
            actions.append(
                {
                    "target": "traefik-static-config",
                    "action": "restart_traefik",
                    "ok": ok,
                    "message": (
                        "restarted traefik to load the regenerated static config"
                        if ok
                        else "could not restart traefik (not running or docker unreachable)"
                    ),
                }
            )
    except DockerConnectionError as e:
        actions.append(
            {
                "target": "core-drift",
                "action": "compose_up",
                "ok": False,
                "message": "docker unreachable: {}".format(e),
            }
        )
    except FileNotFoundError:
        # No compose file yet — nothing to reconcile
        pass
    except Exception as e:  # pragma: no cover - defensive
        actions.append(
            {"target": "core-drift", "action": "compose_up", "ok": False, "message": str(e)}
        )

    # Layer 2 drift: (re)start each failing declared service — its per-service
    # `compose up -d` recreates on spec change, so this also heals image drift.
    try:
        l2_report = gather_l2_drift()
        if l2_report is not None and not l2_report.in_sync:
            from .service_manager import ServiceManager

            sm = ServiceManager()
            for item in l2_report.failures:
                ok, msg = sm.start(item.service)
                actions.append(
                    {
                        "target": "l2:{}".format(item.service),
                        "action": "service_start",
                        "ok": ok,
                        "message": msg,
                    }
                )
    except Exception as e:  # noqa: BLE001 - defensive
        actions.append(
            {"target": "l2-drift", "action": "service_start", "ok": False, "message": str(e)}
        )

    return actions


def _render_text(result: Dict[str, object]) -> None:
    tier = "smoke" if result["smoke"] else "full"
    click.echo()
    click.echo("SyrvisCore verify ({} check)".format(tier))
    click.echo("=" * 40)

    for check in result["checks"]:
        mark = "✓" if check["passed"] else "✗"
        click.echo(
            "  {} [{}] {}: {}".format(mark, check["category"], check["name"], check["message"])
        )

    click.echo()
    drift_data = result["drift"]
    if drift_data is None:
        click.echo("  Drift: not evaluated")
    elif "error" in drift_data:
        click.echo("  Drift: could not evaluate ({})".format(drift_data["error"]))
    elif drift_data["in_sync"]:
        click.echo("  Drift: in sync (running containers match compose)")
    else:
        click.echo("  Drift: OUT OF SYNC")
        for item in drift_data["items"]:
            if item["failure"]:
                click.echo(
                    "    ✗ {} [{}] expected={} actual={}".format(
                        item["service"], item["kind"], item["expected"], item["actual"]
                    )
                )

    l2_data = result.get("l2_drift")
    if l2_data and "error" not in l2_data:
        if l2_data["in_sync"]:
            click.echo("  L2 services: in sync")
        else:
            click.echo("  L2 services: OUT OF SYNC")
            for item in l2_data["items"]:
                if item["failure"]:
                    click.echo(
                        "    ✗ {} [{}] expected={} actual={}".format(
                            item["service"], item["kind"], item["expected"], item["actual"]
                        )
                    )

    remediation_actions = result.get("remediation")
    if remediation_actions:
        click.echo()
        click.echo("Remediation applied:")
        for action in remediation_actions:
            mark = "✓" if action["ok"] else "✗"
            click.echo("  {} {}: {}".format(mark, action["target"], action["message"]))

    click.echo()
    click.echo("Result: {}".format("HEALTHY" if result["healthy"] else "UNHEALTHY"))


@click.command()
@click.option("--smoke", is_flag=True, help="Fast non-destructive subset (post-install gate)")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP health probe)")
@click.option(
    "--fix",
    is_flag=True,
    help="Apply sanctioned remediations (privileged: docker perms, boot hook, "
    "reconcile drift) then re-report",
)
def verify(smoke, as_json, fix):
    """Report installation health and desired-vs-actual container drift.

    Without --fix this is read-only: it reports problems and exits non-zero
    when unhealthy, but never changes system state. With --fix it self-elevates
    and applies sanctioned remediations, then re-reports the resulting state.
    """
    remediation_actions = None
    if fix:
        privilege.ensure_elevated("verify --fix applies privileged remediations.")
        # Only act when there is something to fix, to avoid needless restarts.
        pre = build_report(smoke=smoke)
        if not pre["healthy"]:
            remediation_actions = remediate(smoke=smoke)

    result = build_report(smoke=smoke)
    if remediation_actions is not None:
        result["remediation"] = remediation_actions

    if as_json:
        click.echo(jsonlib.dumps(result, indent=2))
    else:
        _render_text(result)

    if not result["healthy"]:
        raise SystemExit(1)
