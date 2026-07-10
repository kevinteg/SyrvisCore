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
from typing import Dict, List, Optional

import click

from . import drift, paths, validators
from .docker_manager import DockerConnectionError, DockerManager


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


def gather_core_drift(actual: Optional[Dict[str, Dict[str, str]]] = None) -> drift.DriftReport:
    """Build the core-stack drift report.

    Args:
        actual: injectable actual-state map (for tests / when docker state was
            already gathered). When None, it is read from docker.
    """
    expected = _core_compose_expected()
    if actual is None:
        actual = _core_actual_from_docker()
    return drift.detect_drift("core", expected, actual)


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

    return result


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

    click.echo()
    click.echo("Result: {}".format("HEALTHY" if result["healthy"] else "UNHEALTHY"))


@click.command()
@click.option("--smoke", is_flag=True, help="Fast non-destructive subset (post-install gate)")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP health probe)")
def verify(smoke, as_json):
    """Report installation health and desired-vs-actual container drift.

    Read-only: reports problems and exits non-zero when unhealthy, but never
    changes system state. Use 'syrvis doctor --fix' for remediation.
    """
    result = build_report(smoke=smoke)

    if as_json:
        click.echo(jsonlib.dumps(result, indent=2))
    else:
        _render_text(result)

    if not result["healthy"]:
        raise SystemExit(1)
