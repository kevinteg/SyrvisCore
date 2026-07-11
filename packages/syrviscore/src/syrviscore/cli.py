"""SyrvisCore CLI - Main entry point."""

import functools
import json as jsonlib
import os
import sys

import click
from dotenv import load_dotenv

from syrviscore.__version__ import __version__
from syrviscore._format import format_row, status_glyph
from syrviscore.compose import generate_compose_from_config
from syrviscore.docker_manager import (
    DockerConnectionError,  # noqa: F401 - re-exported; tests/adapters import from here
    DockerError,  # noqa: F401 - re-exported; tests/adapters import from here
    DockerManager,
    restart_traefik_if_running,
    write_traefik_config_files,
)
from syrviscore.errors import SyrvisError
from syrviscore.paths import SyrvisHomeError, get_syrvis_home, get_active_version, get_env_path
from syrviscore.setup import setup
from syrviscore.doctor import doctor
from syrviscore.update import update
from syrviscore.verify import verify
from syrviscore import privilege


# =============================================================================
# Error handling at the CLI boundary
# =============================================================================


def handle_errors(f):
    """Render errors cleanly at the CLI boundary (mirror of syrvisctl's).

    Apply as the innermost decorator on a command instead of per-command
    try/except blocks:

        @cli.command()
        @handle_errors
        def mycmd(...): ...

    Behavior:
    - SyrvisError -> one ``Error: {e}`` line on stderr, exit(e.exit_code).
    - Unexpected Exception -> ``Error: {e}`` on stderr, exit 1.
    - Click's own control flow (click.Abort, click.UsageError/ClickException)
      and SystemExit propagate untouched, so confirmation aborts, usage errors,
      and explicit exits keep their native rendering.

    Commands with a ``--json`` flag must still emit their ``{"error": ...}``
    envelope to STDOUT on failure (the MCP contract). Keep that as a small
    in-command handler that calls :func:`json_error` in json mode and
    re-raises otherwise — see ``status`` / ``service list`` / ``stack list`` /
    ``config show`` for the pattern.
    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (click.Abort, click.ClickException):
            raise  # click renders these itself ("Aborted!", usage message)
        except SyrvisError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(e.exit_code)
        except Exception as e:  # noqa: BLE001 - last-resort CLI boundary
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    return wrapper


def json_error(e, indent=None):
    """Emit the --json error envelope to stdout and exit 1 (MCP contract)."""
    click.echo(jsonlib.dumps({"error": str(e)}, indent=indent))
    raise SystemExit(1)


@click.group()
@click.version_option(version=__version__, prog_name="syrvis")
def cli():
    """SyrvisCore - Self-hosted infrastructure platform for Synology NAS."""
    pass


# Register command groups
cli.add_command(setup)
cli.add_command(doctor)
cli.add_command(update)
cli.add_command(verify)


# =============================================================================
# Service command group (Layer 2 services)
# =============================================================================


@cli.group()
def service():
    """Manage Layer 2 services (user-installable containers)."""
    pass


@service.command("add")
@click.argument("source")
@click.option("--no-start", is_flag=True, help="Don't start the service after adding")
@click.option("--subdomain", default=None, help="Override the routed subdomain (servicename)")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default=None,
    help="internal = LAN-only; tunnel = remote via Cloudflare. "
    "Default: inherit the manifest's exposure (unlike 'service run', which defaults to internal).",
)
@handle_errors
def service_add(source, no_start, subdomain, exposure):
    """Add a service from a git URL.

    SOURCE can be a git repository URL containing a syrvis-service.yaml file.
    --subdomain / --exposure override the manifest's routing at enable time.

    Examples:
        syrvis service add https://github.com/user/syrvis-gollum.git
        syrvis service add https://github.com/user/svc.git --subdomain wiki --exposure tunnel
    """
    privilege.ensure_elevated("Adding services requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.add(
        source, start=not no_start, subdomain=subdomain, exposure=exposure
    )
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


@service.command("run")
@click.argument("name")
@click.option(
    "--image",
    default=None,
    help="Pinned image reference (e.g. a GHCR tag). Omit to resolve NAME from "
    "the service catalog ('syrvis service catalog' lists templates).",
)
@click.option("--subdomain", default=None, help="Subdomain to route at (defaults to NAME)")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default=None,
    help="internal = LAN-only; tunnel = remote via Cloudflare. Default: internal "
    "for --image runs; the template's exposure for catalog runs.",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Container port Traefik forwards to (default: 80, or the template's port)",
)
@click.option("--env", "env_vars", multiple=True, help="KEY=VALUE runtime env (repeatable)")
@click.option(
    "--volume",
    "volumes",
    multiple=True,
    help="Volume mount (repeatable): named volume or a path relative to the "
    "service's data dir, e.g. 'data:/app/data:rw'. Only with --image.",
)
@click.option(
    "--env-file",
    "env_file",
    default=None,
    help="A data-dir-relative env file for secrets (created 0600 if absent). " "Only with --image.",
)
@click.option("--description", default="", help="Human description")
@click.option("--no-start", is_flag=True, help="Create but don't start the service")
@handle_errors
def service_run(
    name, image, subdomain, exposure, port, env_vars, volumes, env_file, description, no_start
):
    """Run a Layer 2 service from a published image or a catalog template.

    With --image: the image-first path — hand SyrvisCore an image + how to
    route it, and it synthesizes a validated manifest and runs it. This is what
    home-tech drives over MCP for image-only services.

    Without --image: NAME is resolved from the service catalog (bundled
    templates + $SYRVIS_HOME/catalog/), and any --subdomain/--exposure/--port/
    --env override the template.

    Examples:
        syrvis service run gollum
        syrvis service run cyberquill --image ghcr.io/acme/cyberquill:1.4.0 \\
            --exposure tunnel --port 8080
    """
    privilege.ensure_elevated("Running services requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    if image is None:
        if volumes or env_file:
            raise SyrvisError(
                "--volume/--env-file apply to --image runs; a catalog template "
                "declares its own volumes (override them in the template instead)"
            )
        success, message = manager.add_from_catalog(
            name,
            subdomain=subdomain,
            exposure=exposure,
            port=port,
            environment=list(env_vars),
            start=not no_start,
        )
    else:
        success, message = manager.add_image(
            name,
            image,
            subdomain=subdomain,
            exposure=exposure or "internal",
            port=port if port is not None else 80,
            environment=list(env_vars),
            volumes=list(volumes),
            env_file=env_file,
            description=description,
            start=not no_start,
        )
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


@service.command("catalog")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def service_catalog(as_json):
    """List the service catalog (bundled + site-local templates)."""
    from syrviscore.catalog import list_templates

    entries = list_templates()
    if as_json:
        click.echo(jsonlib.dumps({"templates": entries}, indent=2))
        return
    if not entries:
        click.echo("No catalog templates found.")
        return
    click.echo()
    click.echo(format_row([("NAME", 16), ("IMAGE", 40), ("EXPOSURE", 10), ("SOURCE", 0)]))
    click.echo("-" * 76)
    for entry in entries:
        if "error" in entry:
            click.echo(format_row([(entry["name"], 16), ("INVALID: " + entry["error"], 0)]))
            continue
        click.echo(
            format_row(
                [
                    (entry["name"], 16),
                    (entry["image"], 40),
                    (entry.get("exposure") or "-", 10),
                    (entry["source"], 0),
                ]
            )
        )
    click.echo()
    click.echo("Install one with: syrvis service run <name>")


@service.command("adopt")
@click.argument("name", required=False)
@click.option("--all", "adopt_all", is_flag=True, help="Adopt every installed service")
@handle_errors
def service_adopt(name, adopt_all):
    """Generate a services.d declaration from an existing install.

    The migration path to declarative loading: an installed service becomes a
    file in config/services.d/ that `syrvis reconcile` (and home-tech's IaC)
    owns from then on. The install itself is not touched.
    """
    from syrviscore import services_d
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    if adopt_all:
        rows = manager.list()
        if not rows:
            click.echo("No installed services to adopt.")
            return
        for row in rows:
            try:
                path = services_d.adopt(manager, row["name"])
                click.echo("Adopted '{}' -> {}".format(row["name"], path))
            except Exception as e:  # noqa: BLE001 - per-row isolation
                click.echo("Error adopting '{}': {}".format(row["name"], e), err=True)
        return
    if not name:
        raise SyrvisError("Provide a service NAME or --all")
    path = services_d.adopt(manager, name)
    click.echo("Adopted '{}' -> {}".format(name, path))
    click.echo("It is now managed declaratively; edit the file and run 'syrvis reconcile'.")


@service.command("remove")
@click.argument("name")
@click.option("--purge", is_flag=True, help="Also remove service data")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def service_remove(name, purge, yes):
    """Remove an installed service.

    NAME is the name of the service to remove.
    """
    privilege.ensure_elevated("Removing services requires elevated privileges.")

    if not yes:
        msg = f"This will stop and remove the service '{name}'."
        if purge:
            msg += " All service data will also be deleted."
        click.echo(msg)
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted")
            return

    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.remove(name, purge=purge)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


@service.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def service_list(as_json):
    """List all installed services."""
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        services = manager.list()

        if as_json:
            click.echo(jsonlib.dumps({"services": services}, indent=2, default=str))
            return
    except Exception as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if not services:
        click.echo("No services installed")
        click.echo()
        click.echo("Add a service with: syrvis service add <git-url>")
        return

    widths = (20, 10, 12, 0)
    click.echo()
    click.echo(format_row(list(zip(("NAME", "VERSION", "STATUS", "URL"), widths))))
    click.echo("-" * 70)

    for svc in services:
        glyph = status_glyph(svc["status"])
        cells = (f"{glyph} {svc['name']}", svc["version"], svc["status"], svc["url"])
        click.echo(format_row(list(zip(cells, widths))))

    click.echo()


@service.command("start")
@click.argument("name")
@handle_errors
def service_start(name):
    """Start a service."""
    privilege.ensure_elevated("Starting services requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.start(name)
    if success:
        click.echo(f"Service '{name}' started")
    else:
        raise SyrvisError(message)


@service.command("stop")
@click.argument("name")
@handle_errors
def service_stop(name):
    """Stop a service."""
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.stop(name)
    if success:
        click.echo(f"Service '{name}' stopped")
    else:
        raise SyrvisError(message)


@service.command("update")
@click.argument("name")
@handle_errors
def service_update(name):
    """Update a service from its git repository."""
    privilege.ensure_elevated("Updating services requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.update(name)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


# =============================================================================
# Top-level convenience commands
# =============================================================================


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def status(as_json):
    """Show status of all services (alias for 'core status')."""
    try:
        manager = DockerManager()
        statuses = manager.get_container_status()
        active = get_active_version()

        if as_json:
            click.echo(
                jsonlib.dumps({"version": active, "services": statuses}, indent=2, default=str)
            )
            return
    except Exception as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if not statuses:
        click.echo("No services found")
        click.echo("Run 'syrvis setup' to complete installation")
        return

    click.echo()
    click.echo("SyrvisCore Status")
    click.echo("=" * 60)

    # Show version info
    if active:
        click.echo(f"Version: {active}")
    click.echo()

    widths = (15, 12, 0)
    click.echo(format_row(list(zip(("Service", "Status", "Uptime"), widths))))
    click.echo("-" * 50)

    for service_name, info in statuses.items():
        glyph = status_glyph(info["status"])
        cells = (f"{glyph} {service_name}", info["status"], info["uptime"])
        click.echo(format_row(list(zip(cells, widths))))

    click.echo()


@cli.command()
@click.argument("service", required=False)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--tail", "-n", default=100, help="Number of lines to show")
@handle_errors
def logs(service, follow, tail):
    """View service logs (alias for 'core logs')."""
    # Unknown-service ValueError from get_container_logs carries the
    # available-services list; the boundary renders it as "Error: ...".
    manager = DockerManager()

    if follow:
        if service:
            click.echo(f"Following logs for {service}... (Ctrl+C to stop)")
        else:
            click.echo("Following logs for all services... (Ctrl+C to stop)")
        manager.get_container_logs(service=service, follow=True, tail=tail)
    else:
        log_output = manager.get_container_logs(service=service, follow=False, tail=tail)
        click.echo(log_output)


@cli.command()
@handle_errors
def start():
    """Start all services (alias for 'core start')."""
    privilege.ensure_elevated("Starting services requires elevated privileges.")
    click.echo("Starting services...")
    manager = DockerManager()
    warnings = manager.start_core_services()
    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)
    click.echo("Services started")
    click.echo("Run 'syrvis status' to verify")


@cli.command()
@handle_errors
def stop():
    """Stop all services (alias for 'core stop')."""
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    click.echo("Stopping services...")
    manager = DockerManager()
    manager.stop_core_services()
    click.echo("Services stopped")


@cli.command()
@handle_errors
def restart():
    """Restart all services (alias for 'core restart')."""
    privilege.ensure_elevated("Restarting services requires elevated privileges.")
    click.echo("Restarting services...")
    manager = DockerManager()
    manager.restart_core_services()
    click.echo("Services restarted")
    click.echo("Run 'syrvis status' to verify")


@cli.command()
@click.option("--volumes", "-v", is_flag=True, help="Also remove named volumes")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def clean(volumes, yes):
    """Remove all SyrvisCore containers and networks.

    Useful for cleaning up before reinstall or when containers/networks
    are in a bad state. This stops and removes:
    - traefik, portainer, cloudflared containers
    - proxy and syrvis-macvlan networks
    """
    privilege.ensure_elevated("Cleaning up containers requires elevated privileges.")

    if not yes:
        msg = "This will remove all SyrvisCore containers and networks."
        if volumes:
            msg += " Named volumes will also be removed."
        click.echo(msg)
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted")
            return

    click.echo("Cleaning up containers and networks...")
    manager = DockerManager()
    results = manager.clean_core_services(remove_volumes=volumes)

    click.echo()
    click.echo("Cleanup Results:")

    # Show containers
    if results.get("containers_stopped"):
        click.echo(f"  Containers stopped: {', '.join(results['containers_stopped'])}")
    else:
        click.echo("  Containers stopped: (none)")

    # Show networks
    if results.get("networks_cleaned"):
        click.echo(f"  Networks removed:   {', '.join(results['networks_cleaned'])}")
    else:
        click.echo("  Networks removed:   (none)")

    # Show volumes if requested
    if volumes:
        if results.get("volumes_cleaned"):
            click.echo(f"  Volumes removed:    {', '.join(results['volumes_cleaned'])}")
        else:
            click.echo("  Volumes removed:    (none)")

    if results["errors"]:
        click.echo()
        click.echo("Warnings:", err=True)
        for error in results["errors"]:
            click.echo(f"  - {error}", err=True)

    click.echo()
    click.echo("Cleanup complete")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def reset(yes):
    """Clean everything and start services fresh.

    This is the nuclear option - removes all containers and networks,
    then starts services from scratch. Useful when:
    - Reinstalling after an update
    - Containers/networks are in a broken state
    - Port conflicts or network issues

    Unlike 'setup', this does NOT reconfigure anything - it just restarts
    the existing configuration. Use 'setup' if you need to change settings.
    """
    privilege.ensure_elevated("Resetting services requires elevated privileges.")

    if not yes:
        click.echo()
        click.echo("RESET: Restart services from scratch (keeps existing configuration)")
        click.echo("-" * 60)
        click.echo("This will:")
        click.echo(f"  1. Stop and remove containers: {', '.join(DockerManager.CORE_SERVICES)}")
        click.echo("  2. Remove Docker networks (proxy, syrvis-macvlan)")
        click.echo("  3. Recreate macvlan shim for host-to-container communication")
        click.echo("  4. Start all services fresh")
        click.echo()
        click.echo("Your configuration (.env) and certificates (acme.json) are preserved.")
        click.echo()
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted")
            return

    click.echo()
    # reset_core_services() cleans AND starts in one synchronous call, so the
    # start has already happened by the time this returns — report what it did
    # rather than staging fake "[1/2]/[2/2]" steps around a single library call.
    click.echo("Resetting services (removing containers/networks, then starting fresh)...")
    manager = DockerManager()
    results = manager.reset_core_services()

    # Show what was stopped/removed
    if results.get("containers_stopped"):
        click.echo(f"  Stopped: {', '.join(results['containers_stopped'])}")
    if results.get("networks_cleaned"):
        click.echo(f"  Removed networks: {', '.join(results['networks_cleaned'])}")

    if results["errors"]:
        click.echo("  Warnings:", err=True)
        for error in results["errors"]:
            click.echo(f"    - {error}", err=True)

    if results.get("started"):
        click.echo(f"  Started: {', '.join(DockerManager.CORE_SERVICES)}")

    click.echo()
    click.echo("Reset complete. Run 'syrvis status' to verify.")


# =============================================================================
# Hello / Test command
# =============================================================================


@cli.command()
def hello():
    """Test command to verify installation."""
    click.echo("Hello from SyrvisCore!")
    click.echo(f"Version: {__version__}")
    click.echo("CLI is working correctly")


# =============================================================================
# Compose command group
# =============================================================================


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show the plan without applying anything")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable plan/results")
@click.option(
    "--prune",
    type=click.Choice(["stop", "remove", "purge"]),
    default=None,
    help="Policy for installed services with NO declaration (default: report as "
    "unmanaged, touch nothing). remove drops config (data kept); purge drops data.",
)
@click.option("--strict", is_flag=True, help="Any invalid file or failed action exits non-zero")
@click.option(
    "--boot",
    is_flag=True,
    help="Boot mode: best-effort (always exits 0), never prunes, never prompts",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation of destructive prune actions")
@handle_errors
def reconcile(dry_run, as_json, prune, strict, boot, yes):
    """Converge to the declared services in config/services.d/.

    Loads every declaration with per-file failure isolation (a broken file
    marks only that service invalid), then converges each service
    independently (one failure never blocks the rest). Installed services with
    no declaration are reported as unmanaged and never touched unless --prune.

    Exit code: non-zero for any INVALID declaration file (corrupted intent
    must never pass silently) or a CRITICAL service's failure; any failure
    with --strict. --boot is always best-effort and exits 0.
    """
    from syrviscore import services_d
    from syrviscore.service_manager import ServiceManager

    if boot:
        prune = None  # boot never destroys anything
    if not dry_run:
        privilege.ensure_elevated("Reconciling services requires elevated privileges.")

    manager = ServiceManager()
    try:
        declarations, invalid = services_d.load_declarations(manager.syrvis_home)
        plan = services_d.build_reconcile_plan(manager, declarations, invalid, prune=prune)
    except SyrvisError as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if dry_run:
        if as_json:
            click.echo(jsonlib.dumps({"plan": plan, "applied": False}, indent=2))
        else:
            _render_reconcile_plan(plan)
            click.echo("(dry run — nothing applied)")
        return

    destructive = [a for a in plan["actions"] if a["destructive"]]
    if destructive and not yes and not boot:
        if as_json:
            # Never corrupt the --json contract with prompts/human rendering:
            # a machine caller must pass -y explicitly for destructive prunes.
            json_error(
                SyrvisError(
                    "destructive prune action(s) require -y in --json mode: {}".format(
                        ", ".join("{} {}".format(a["kind"], a["name"]) for a in destructive)
                    )
                ),
                indent=2,
            )
        _render_reconcile_plan(plan)
        click.confirm(
            "Apply {} destructive prune action(s) ({})?".format(
                len(destructive),
                ", ".join("{} {}".format(a["kind"], a["name"]) for a in destructive),
            ),
            abort=True,
        )

    results = services_d.apply_reconcile_plan(manager, declarations, plan)
    ok, reason = services_d.verdict(plan, results, strict=strict)

    if as_json:
        click.echo(
            jsonlib.dumps(
                {"plan": plan, "applied": True, "results": results, "ok": ok, "reason": reason},
                indent=2,
            )
        )
        if not ok and not boot:
            raise SystemExit(1)
        return

    _render_reconcile_plan(plan)
    if results:
        click.echo()
        for r in results:
            mark = "[+]" if r["ok"] else "[-]"
            crit = " (critical)" if r.get("critical") and not r["ok"] else ""
            click.echo("  {} {} {}{}: {}".format(mark, r["kind"], r["name"], crit, r["message"]))
    click.echo()
    if ok:
        click.echo("Reconcile complete.")
    else:
        click.echo("Reconcile finished UNHEALTHY: {}".format(reason))
        if not boot:
            raise SystemExit(1)


def _render_reconcile_plan(plan):
    click.echo()
    summary = plan["summary"]
    click.echo(
        "Declared: {}  in sync: {}  disabled: {}  unmanaged: {}  invalid: {}".format(
            summary["declared"],
            len(plan["in_sync"]),
            len(plan["disabled"]),
            len(plan["unmanaged"]),
            summary["invalid"],
        )
    )
    for row in plan["invalid"]:
        click.echo("  [!] invalid declaration {}: {}".format(row["file"], row["error"]))
    for name in plan["unmanaged"]:
        click.echo("  [?] unmanaged (installed, no declaration): {}".format(name))
    if not plan["actions"]:
        click.echo("  Nothing to do.")
        return
    click.echo("  Actions:")
    for action in plan["actions"]:
        marker = "!" if action["destructive"] else "-"
        crit = " (critical)" if action.get("critical") else ""
        click.echo("    {} {} {}{}".format(marker, action["kind"], action["name"], crit))


@cli.group()
def compose():
    """Manage docker-compose configuration."""
    pass


@compose.command()
@click.option(
    "--config",
    "-c",
    default=None,
    help="Explicit build-config file. Default: the active version's bundled "
    "config.yaml if present, else the built-in pinned image versions.",
    type=click.Path(),
)
@click.option(
    "--output",
    "-o",
    default="docker-compose.yaml",
    help="Path for output docker-compose file",
    type=click.Path(),
)
@handle_errors
def generate(config, output):
    """Generate docker-compose.yaml and Traefik configuration files."""
    from pathlib import Path

    # Load .env from SYRVIS_HOME/config/.env
    env_path = get_env_path()
    if env_path.exists():
        load_dotenv(env_path, override=True)
    else:
        click.echo(f"Warning: No .env file found at {env_path}", err=True)
        click.echo("Run 'syrvis setup' to configure first.", err=True)
        raise click.Abort()

    if config and Path(config).exists():
        click.echo(f"Reading build config from: {config}")
    else:
        click.echo(
            "Using the active version's bundled config.yaml if present, "
            "else built-in pinned image versions"
        )
    compose = generate_compose_from_config(config_path=config, output_path=output)

    click.echo(f"Generated docker-compose.yaml at: {output}")
    click.echo()
    click.echo("Services configured:")
    for service_name in compose["services"].keys():
        service = compose["services"][service_name]
        click.echo(f"  {service_name:<15} {service['image']}")

    # Show Traefik's dedicated IP
    traefik_networks = compose["services"]["traefik"]["networks"]
    if isinstance(traefik_networks, dict) and "syrvis-macvlan" in traefik_networks:
        traefik_ip = traefik_networks["syrvis-macvlan"]["ipv4_address"]
        click.echo()
        click.echo("Network Configuration:")
        click.echo(f"  Traefik IP: {traefik_ip}")
        click.echo(
            f"  Interface:  {compose['networks']['syrvis-macvlan']['driver_opts']['parent']}"
        )

    # Also regenerate Traefik configuration files (single writer)
    click.echo()
    click.echo("Regenerating Traefik configuration...")
    syrvis_home = get_syrvis_home()
    static_changed = write_traefik_config_files(syrvis_home)
    traefik_data = syrvis_home / "data" / "traefik"
    click.echo(f"  Generated: {traefik_data / 'traefik.yml'}")
    click.echo(f"  Generated: {traefik_data / 'config' / 'dynamic.yml'}")

    # A STATIC config change (traefik.yml) only takes effect on a Traefik
    # restart — `up -d` won't pick up a bind-mounted file edit. Restart the
    # running Traefik now so the change (e.g. `ping: {}`) applies immediately.
    if static_changed and restart_traefik_if_running():
        click.echo("  Static config changed — restarted Traefik to apply it.")

    click.echo()
    click.echo("Run 'syrvis start' to start services.")


# =============================================================================
# Stack command group (declarative core-tier services)
# =============================================================================


def _regenerate_compose():
    """Regenerate docker-compose.yaml + Traefik configs from the declared stack.

    Returns (ok, message). Best-effort: needs .env (network config) present.
    """
    try:
        from syrviscore import paths as p

        env_path = get_env_path()
        if env_path.exists():
            load_dotenv(env_path, override=True)

        # None lets the generator resolve: active version's bundled config.yaml
        # if present, else the built-in pinned image versions.
        versioned = None
        try:
            versioned = p.get_config_path()
        except Exception:
            versioned = None
        config_path = str(versioned) if versioned and versioned.exists() else None

        out = str(p.get_docker_compose_path())
        compose = generate_compose_from_config(config_path=config_path, output_path=out)

        # Keep Traefik static/dynamic config in sync too (single writer). A static
        # change only applies on a Traefik restart, so restart it if it's running.
        static_changed = write_traefik_config_files(p.get_syrvis_home())
        restarted = static_changed and restart_traefik_if_running()

        # Reconcile disabled optional core services: `up -d` never removes a
        # container that dropped out of the compose file, so stop/remove them
        # here (exact-name matches of known optional services only).
        from syrviscore.docker_manager import remove_disabled_core_containers

        removed = remove_disabled_core_containers()

        names = ", ".join(sorted(compose["services"].keys()))
        msg = "Regenerated {} ({} services: {})".format(out, len(compose["services"]), names)
        if restarted:
            msg += " — restarted Traefik to apply static config change"
        if removed:
            msg += " — stopped disabled: {}".format(", ".join(removed))
        return True, msg
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _post_stack_change(do_apply):
    if do_apply:
        ok, msg = _regenerate_compose()
        click.echo(msg if ok else "(compose not regenerated: {})".format(msg))
        if ok:
            click.echo("Run 'syrvis start' to bring the stack up.")
    else:
        click.echo("Run 'syrvis stack apply' to regenerate compose, then 'syrvis start'.")


@cli.group()
def stack():
    """Declare which core-tier containers this instance runs (config/stack.yaml)."""
    pass


@stack.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def stack_list(as_json):
    """Show declared core services and whether they're running."""
    from syrviscore import stack as stack_mod

    # Load .env so the "token not set" hint reflects the configured tokens, not
    # just whatever happens to be in the invoking shell's environment.
    try:
        env_path = get_env_path()
        if env_path.exists():
            load_dotenv(env_path, override=True)
    except Exception:
        pass

    try:
        st = stack_mod.load_stack()
    except stack_mod.StackError as e:
        # Honor the read-command --json contract on the error path too: a machine
        # consumer must get an {"error": ...} envelope, never click's 'Aborted!'.
        if as_json:
            json_error(e)
        raise

    running = {}
    try:
        from syrviscore.docker_manager import DockerManager

        status = DockerManager().get_container_status()
        running = {info["name"]: info["status"] for info in status.values()}
    except Exception:
        running = {}

    rows = []
    for name in stack_mod.ALL_SERVICES:
        svc = st.services.get(name)
        enabled = bool(svc and svc.enabled)
        cname = stack_mod.CONTAINER_NAME[name]
        token_env = stack_mod.TOKEN_FOR.get(name)
        note = ""
        if enabled and token_env and not os.getenv(token_env):
            note = "enabled but {} not set".format(token_env)
        rows.append(
            {
                "service": name,
                "primordial": name in stack_mod.PRIMORDIAL,
                "enabled": enabled,
                "container": cname,
                "running": running.get(cname, "not running"),
                "settings": (svc.settings if svc else {}),
                "note": note,
            }
        )

    if as_json:
        click.echo(jsonlib.dumps({"services": rows}, indent=2))
        return

    click.echo()
    click.echo("SyrvisCore stack (config/stack.yaml)")
    click.echo("=" * 52)
    widths = (20, 9, 0)
    for r in rows:
        glyph = status_glyph(r["enabled"])
        tag = " [primordial]" if r["primordial"] else ""
        state = "enabled" if r["enabled"] else "disabled"
        cells = ("{} {}".format(glyph, r["service"]), state, "{}{}".format(r["running"], tag))
        click.echo("  " + format_row(list(zip(cells, widths))))
        if r["note"]:
            click.echo("      ! {}".format(r["note"]))
    click.echo()


@stack.command("enable")
@click.argument("name")
@click.option("--subdomain", default=None, help="(dashboard) subdomain to route at")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default=None,
    help="internal = LAN-only; tunnel = remote via Cloudflare",
)
@click.option("--apply", "do_apply", is_flag=True, help="Regenerate compose immediately")
@handle_errors
def stack_enable(name, subdomain, exposure, do_apply):
    """Declare a core service enabled."""
    from syrviscore import stack as stack_mod

    settings = {}
    if subdomain:
        settings["subdomain"] = subdomain
    if exposure:
        settings["exposure"] = exposure
    stack_mod.set_enabled(name, True, settings or None)
    click.echo("Enabled '{}' in the stack.".format(name))
    _post_stack_change(do_apply)


@stack.command("disable")
@click.argument("name")
@click.option("--apply", "do_apply", is_flag=True, help="Regenerate compose immediately")
@handle_errors
def stack_disable(name, do_apply):
    """Declare a core service disabled (primordial services cannot be disabled)."""
    from syrviscore import stack as stack_mod

    stack_mod.set_enabled(name, False)
    click.echo("Disabled '{}' in the stack.".format(name))
    _post_stack_change(do_apply)


@stack.command("apply")
@click.option(
    "--from",
    "desired_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Converge the WHOLE instance (core stack + complete L2 set) to a "
    "desired-state YAML: add/replace/remove services to match it.",
)
@click.option("--dry-run", is_flag=True, help="Show the plan without applying anything")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable plan/results")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation of destructive actions")
@handle_errors
def stack_apply(desired_file, dry_run, as_json, yes):
    """Regenerate compose from the declared stack, or converge to a desired file.

    Without --from: regenerates docker-compose.yaml + Traefik config from the
    on-NAS config/stack.yaml (existing behavior).

    With --from FILE: whole-set convergence — diff the desired document against
    the instance and add/replace/stop/remove core + Layer 2 services to match.
    Destructive actions (remove/purge of undeclared services) require -y or an
    interactive confirmation. --dry-run prints the plan and changes nothing.
    """
    if desired_file is None:
        if dry_run:
            raise SyrvisError("--dry-run requires --from (there is no plan to preview)")
        ok, msg = _regenerate_compose()
        if not ok:
            raise SyrvisError(msg)
        click.echo(msg)
        click.echo("Run 'syrvis start' to bring the stack up (or 'syrvis restart').")
        return

    from pathlib import Path as _Path

    from syrviscore import converge as converge_mod

    try:
        desired = converge_mod.load_desired(_Path(desired_file))
        plan = converge_mod.build_plan(desired)
    except SyrvisError as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if dry_run:
        if as_json:
            click.echo(jsonlib.dumps({"plan": plan, "applied": False}, indent=2))
        else:
            _render_plan(plan)
            click.echo("(dry run — nothing applied)")
        return

    if plan["summary"]["destructive"] and not yes:
        _render_plan(plan)
        destructive = [a for a in plan["actions"] if a["destructive"]]
        click.confirm(
            "Apply {} destructive action(s) ({})?".format(
                len(destructive),
                ", ".join("{} {}".format(a["kind"], a["name"]) for a in destructive),
            ),
            abort=True,
        )

    privilege.ensure_elevated("Converging services requires elevated privileges.")
    results = converge_mod.apply_plan(plan)

    # Stack enablement changed -> regenerate compose (and restart Traefik on a
    # static change) so the converged declaration is materialized.
    stack_changed = any(r["kind"].startswith("stack_") and r["ok"] for r in results)
    regen_msg = None
    if stack_changed:
        ok, regen_msg = _regenerate_compose()
        if not ok:
            regen_msg = "(compose not regenerated: {})".format(regen_msg)

    if as_json:
        click.echo(
            jsonlib.dumps(
                {"plan": plan, "applied": True, "results": results, "regen": regen_msg},
                indent=2,
            )
        )
        if any(not r["ok"] for r in results):
            raise SystemExit(1)
        return

    click.echo()
    for r in results:
        mark = "[+]" if r["ok"] else "[-]"
        click.echo("  {} {} {}: {}".format(mark, r["kind"], r["name"], r["message"]))
    if regen_msg:
        click.echo("  {}".format(regen_msg))
    failed = [r for r in results if not r["ok"]]
    click.echo()
    if failed:
        click.echo("Converge finished with {} failure(s).".format(len(failed)))
        raise SystemExit(1)
    if stack_changed:
        click.echo("Converged. Run 'syrvis start' to bring newly-enabled core services up.")
    else:
        click.echo("Converged.")


def _render_plan(plan):
    click.echo()
    if not plan["actions"]:
        click.echo("In sync — no actions needed.")
        return
    click.echo(
        "Plan ({} action(s), {} destructive):".format(
            plan["summary"]["total"], plan["summary"]["destructive"]
        )
    )
    for action in plan["actions"]:
        marker = "!" if action["destructive"] else "-"
        target = action.get("name") or action.get("service")
        detail = ""
        if action["kind"] == "service_replace":
            detail = " ({})".format(
                ", ".join(
                    "{}: {} -> {}".format(k, v["from"], v["to"])
                    for k, v in action.get("changes", {}).items()
                )
            )
        elif action["kind"] == "service_add":
            detail = " ({} at {}, {})".format(
                action["image"], action["subdomain"], action["exposure"]
            )
        click.echo("  {} {} {}{}".format(marker, action["kind"], target, detail))


@stack.command("hostnames")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default=None,
    help="Only show hosts with this exposure",
)
@handle_errors
def stack_hostnames(as_json, exposure):
    """Report the external DNS / tunnel state this instance needs.

    Every hostname SyrvisCore routes, its exposure, and the record a deployment
    must create: a LAN DNS A record for 'internal', a Cloudflare Tunnel route +
    Access policy for 'tunnel'. This is the seam home-tech reconciles against.
    """
    from syrviscore import hostnames as hostnames_mod

    # Load .env so DOMAIN / TRAEFIK_IP reflect the configured instance.
    try:
        env_path = get_env_path()
        if env_path.exists():
            load_dotenv(env_path, override=True)
    except Exception:
        pass

    report = hostnames_mod.build_report()
    entries = report.get("entries", [])
    if exposure:
        entries = [e for e in entries if e["exposure"] == exposure]

    if as_json:
        out = dict(report)
        out["entries"] = entries
        click.echo(jsonlib.dumps(out, indent=2))
        return

    if report.get("error"):
        click.echo("Could not read config: {}".format(report["error"]), err=True)
        raise click.Abort()

    domain = report.get("domain") or "(domain unset)"
    traefik_ip = report.get("traefik_ip") or "(TRAEFIK_IP unset)"
    click.echo()
    click.echo("Required external state for {}".format(domain))
    click.echo("=" * 60)
    if not entries:
        click.echo("  (no routed hostnames)")
        click.echo()
        return

    internal = [e for e in entries if e["exposure"] == "internal"]
    tunnel = [e for e in entries if e["exposure"] == "tunnel"]

    if internal:
        click.echo("\n  LOCAL (add a LAN DNS A record -> {}):".format(traefik_ip))
        for e in internal:
            state = "" if e["enabled"] else "  [disabled]"
            click.echo("    {:<28} A   {}{}".format(e["hostname"], traefik_ip, state))
    if tunnel:
        click.echo("\n  REMOTE (Cloudflare Tunnel public hostname + Access policy):")
        for e in tunnel:
            state = "" if e["enabled"] else "  [disabled]"
            click.echo("    {:<28} tunnel + Access{}".format(e["hostname"], state))
    click.echo()


# =============================================================================
# Core command group (kept for backwards compatibility)
# =============================================================================


@cli.group()
def core():
    """Manage core services (Traefik, Portainer, Cloudflared)."""
    pass


@core.command("start")
@handle_errors
def core_start():
    """Start core services."""
    privilege.ensure_elevated("Starting services requires elevated privileges.")
    click.echo("Starting core services...")
    manager = DockerManager()
    warnings = manager.start_core_services()
    for warning in warnings:
        click.echo(f"Warning: {warning}", err=True)
    click.echo("Start initiated for core services")
    click.echo("Run 'syrvis status' to verify")


@core.command("stop")
@handle_errors
def core_stop():
    """Stop core services."""
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    click.echo("Stopping core services...")
    manager = DockerManager()
    manager.stop_core_services()
    click.echo("Stop initiated for core services")


@core.command("restart")
@handle_errors
def core_restart():
    """Restart core services."""
    privilege.ensure_elevated("Restarting services requires elevated privileges.")
    click.echo("Restarting core services...")
    manager = DockerManager()
    manager.restart_core_services()
    click.echo("Restart initiated for core services")
    click.echo("Run 'syrvis status' to verify")


@core.command("status")
@handle_errors
def core_status():
    """Show status of core services."""
    manager = DockerManager()
    statuses = manager.get_container_status()

    if not statuses:
        click.echo("No core services found")
        click.echo("Run 'syrvis start' to start services")
        return

    click.echo()
    click.echo("Core Services Status:")
    click.echo()
    widths = (15, 12, 20, 0)
    click.echo(format_row(list(zip(("Service", "Status", "Uptime", "Image"), widths))))
    click.echo("-" * 80)

    for service_name, info in statuses.items():
        glyph = status_glyph(info["status"])
        cells = (f"{glyph} {service_name}", info["status"], info["uptime"], info["image"])
        click.echo(format_row(list(zip(cells, widths))))


@core.command("logs")
@click.argument("service", required=False)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--tail", "-n", default=100, help="Number of lines to show from end")
@handle_errors
def core_logs(service, follow, tail):
    """View logs from core services."""
    # Unknown-service ValueError from get_container_logs carries the
    # available-services list; the boundary renders it as "Error: ...".
    manager = DockerManager()

    if follow:
        if service:
            click.echo(f"Following logs for {service}... (Ctrl+C to stop)")
        else:
            click.echo("Following logs for all services... (Ctrl+C to stop)")
        manager.get_container_logs(service=service, follow=True, tail=tail)
    else:
        log_output = manager.get_container_logs(service=service, follow=False, tail=tail)
        click.echo(log_output)


# =============================================================================
# Config command group
# =============================================================================


@cli.group()
def config():
    """Manage configuration files."""
    pass


@config.command()
@handle_errors
def generate_traefik():
    """Generate Traefik configuration files."""
    load_dotenv()

    domain = os.getenv("DOMAIN")
    if not domain:
        click.echo("Warning: DOMAIN environment variable not set", err=True)
        click.echo("  Using default: example.com", err=True)
        click.echo("  Set DOMAIN in .env file for production use", err=True)
        click.echo()

    syrvis_home = get_syrvis_home()
    traefik_data = syrvis_home / "data" / "traefik"

    static_changed = write_traefik_config_files(syrvis_home)
    click.echo(f"Generated static config: {traefik_data / 'traefik.yml'}")
    click.echo(f"Generated dynamic config: {traefik_data / 'config' / 'dynamic.yml'}")

    # Static config only applies on a Traefik restart (up -d ignores a
    # bind-mounted file edit); restart the running container if it changed.
    if static_changed and restart_traefik_if_running():
        click.echo("Restarted Traefik to apply the static config change.")

    click.echo()
    click.echo("Configuration files created successfully!")


@config.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def show(as_json):
    """Show current configuration."""
    from .config_reader import read_config

    try:
        cfg = read_config()

        if as_json:
            # read_config() redacts secrets by default, so the JSON view is safe
            # for the MCP/dashboard adapter contract.
            click.echo(jsonlib.dumps(cfg.to_dict(), indent=2))
            return
    except SyrvisHomeError as e:
        if as_json:
            json_error(e)
        raise

    click.echo()
    click.echo("SyrvisCore Configuration")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"Install path:  {cfg.install_path or 'unknown'}")
    click.echo(f"Active version: {cfg.active_version or 'unknown'}")
    click.echo()

    if cfg.values:
        click.echo(f"Configuration ({cfg.env_path}):")
        click.echo("-" * 60)
        for key, value in cfg.values.items():
            click.echo(f"  {key}={value}")
    else:
        click.echo("No .env file found")
        click.echo("Run 'syrvis setup' to create configuration")


if __name__ == "__main__":
    cli()
