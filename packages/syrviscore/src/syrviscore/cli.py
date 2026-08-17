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


def read_json_stdin(what="bundle", max_bytes=1024 * 1024):
    """Read + parse a JSON document from STDIN, byte-capped (the seam-verb pattern).

    The single reader behind ``apply`` and ``deploy``: secrets arrive on stdin
    only (never argv/ps/logs), so the cap guards against a runaway/hostile
    stream. The cap is measured in BYTES (encode, not len(str)) so a multibyte
    payload can't slip several times past a byte limit. Raises SyrvisError on an
    over-cap, empty, or non-JSON stream; per-field caps downstream are tighter.
    """
    raw = click.get_text_stream("stdin").read(max_bytes + 1)
    if len(raw.encode("utf-8", errors="surrogateescape")) > max_bytes:
        raise SyrvisError(f"{what} too large (max {max_bytes} bytes)")
    if not raw.strip():
        raise SyrvisError(f"no {what} on stdin")
    try:
        return jsonlib.loads(raw)
    except ValueError as e:
        raise SyrvisError(f"{what} is not valid JSON: {e}")


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


@service.command("declare")
@click.argument("name")
@click.option("--image", required=True, help="Pinned image reference (never :latest)")
@click.option("--subdomain", default=None, help="Subdomain to route at (defaults to NAME)")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default="internal",
    help="internal = LAN-only (default); tunnel = remote via Cloudflare",
)
@click.option("--port", type=int, default=80, help="Container port Traefik forwards to")
@click.option(
    "--enabled",
    type=click.BOOL,
    default=True,
    help="true (default) = reconcile runs it; false = declared-but-off",
)
@click.option(
    "--critical",
    type=click.BOOL,
    default=False,
    help="true = a failure of this service makes reconcile/verify unhealthy",
)
@click.option("--env", "env_vars", multiple=True, help="KEY=VALUE runtime env (repeatable)")
@click.option("--description", default="", help="Human description")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def service_declare(
    name, image, subdomain, exposure, port, enabled, critical, env_vars, description, as_json
):
    """Author (or update) a services.d declaration WITHOUT applying it.

    Writes config/services.d/NAME.yaml through the full schema trust boundary.
    Nothing is installed or started — run 'syrvis reconcile' (or wait for the
    next boot/IaC reconcile) to converge to the declared intent.
    """
    privilege.ensure_elevated("Writing declarations requires elevated privileges.")
    from syrviscore import services_d
    from syrviscore.paths import get_syrvis_home as _home

    try:
        service_def = services_d.build_declaration(
            name,
            image,
            subdomain=subdomain,
            exposure=exposure,
            port=port,
            environment=list(env_vars),
            description=description,
            enabled=enabled,
            critical=critical,
        )
        path = services_d.write_declaration(_home(), service_def)
    except SyrvisError as e:
        if as_json:
            json_error(e)
        raise

    if as_json:
        click.echo(jsonlib.dumps({"ok": True, "name": name, "path": str(path), "applied": False}))
        return
    click.echo("Declared '{}' -> {}".format(name, path))
    click.echo("Apply it with: sudo syrvis reconcile")


@service.command("adopt")
@click.argument("name", required=False)
@click.option("--all", "adopt_all", is_flag=True, help="Adopt every installed service")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def service_adopt(name, adopt_all, as_json):
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
        adopted, errors = [], []
        for row in rows:
            try:
                path = services_d.adopt(manager, row["name"])
                adopted.append({"name": row["name"], "path": str(path)})
                if not as_json:
                    click.echo("Adopted '{}' -> {}".format(row["name"], path))
            except Exception as e:  # noqa: BLE001 - per-row isolation
                errors.append({"name": row["name"], "error": str(e)})
                if not as_json:
                    click.echo("Error adopting '{}': {}".format(row["name"], e), err=True)
        if as_json:
            click.echo(jsonlib.dumps({"ok": not errors, "adopted": adopted, "errors": errors}))
            if errors:
                raise SystemExit(1)
        elif not rows:
            click.echo("No installed services to adopt.")
        return
    if not name:
        raise SyrvisError("Provide a service NAME or --all")
    try:
        path = services_d.adopt(manager, name)
    except SyrvisError as e:
        if as_json:
            json_error(e)
        raise
    if as_json:
        click.echo(jsonlib.dumps({"ok": True, "adopted": [{"name": name, "path": str(path)}]}))
        return
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

    widths = (20, 10, 12, 10, 0)
    click.echo()
    click.echo(format_row(list(zip(("NAME", "VERSION", "STATUS", "INTENT", "URL"), widths))))
    click.echo("-" * 78)

    for svc in services:
        glyph = status_glyph(svc["status"])
        # "shed" beside "stopped" is the whole point: a not-running service that
        # is SUPPOSED to be down must not read like one that fell over.
        cells = (
            f"{glyph} {svc['name']}",
            svc["version"],
            svc["status"],
            svc.get("intent") or "-",
            svc["url"],
        )
        click.echo(format_row(list(zip(cells, widths))))

    shed = [s for s in services if s.get("intent") == "shed"]
    if shed:
        click.echo()
        click.echo("Shed ({}), deliberately down:".format(len(shed)))
        for svc in shed:
            click.echo(
                "  - {}: {}{}".format(
                    svc["name"],
                    svc.get("shed_reason") or "unspecified",
                    " (until {})".format(svc["shed_until"]) if svc.get("shed_until") else "",
                )
            )
        click.echo("  Lift with: sudo syrvis service unshed -- <name>")

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
    """Stop a service (EPHEMERAL intent: writes `enabled: false`).

    The right verb for a short, local stop. It writes `enabled: false` into the
    service's declaration, which reconcile honors — but that file is exactly
    what the next GitOps `syrvis apply` overwrites from the repo, so the stop
    lasts only until the next apply.

    For a stop that must OUTLIVE an apply — a load-shed, a vendor outage, a
    deliberate multi-day degradation — use `syrvis service shed --reason R`,
    which records the decision outside the declaration set.
    """
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.stop(name)
    if success:
        click.echo(f"Service '{name}' stopped")
    else:
        raise SyrvisError(message)


@service.command("shed")
@click.argument("name")
@click.option(
    "--reason",
    required=True,
    help="Short token recording WHY (e.g. md6-resync). Becomes a metric label.",
)
@click.option("--until", default=None, help="YYYY-MM-DD or YYYY-MM-DDThh:mm:ssZ (review date)")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def service_shed(name, reason, until, as_json):
    """Declare a service DELIBERATELY DOWN, durably, and stop it.

    `service stop` is the EPHEMERAL verb: it flips `enabled: false` in the
    service's declaration — the very file the next GitOps `syrvis apply`
    overwrites from the repo. That is fine for a five-minute stop and wrong for
    a five-day load-shed: it is how fourteen deliberately-stopped services got
    resurrected mid-array-rebuild by a runbook whose own text said they would
    stay down (incident 2026-08-16).

    `service shed` is the DURABLE verb. It records the decision — with a reason,
    a timestamp and an optional review date — in data/state/intent.json, which
    lives outside the declaration set and therefore survives apply, deploy,
    reconcile, resume and boot. While a service is shed:

      * reconcile/resume never start it and never call it drift;
      * an incoming bundle can never re-enable it (it is pinned enabled: false);
      * `service start` / `service recreate` refuse, naming the shed;
      * `deploy` still lands new bits — it just does not start them;
      * `service list --json` and `status --json` report it as `shed`, with the
        reason, instead of as one more unhealthy container.

    Lift it with `service unshed`. Idempotent; re-shedding keeps the original
    `since` and updates the reason/until.

        sudo syrvis service shed --reason md6-resync --until 2026-08-24 -- onyx-api
    """
    privilege.ensure_elevated("Shedding a service requires elevated privileges.")
    from syrviscore import intent as intent_mod
    from syrviscore import services_d
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    home = manager.syrvis_home
    # Gate on a KNOWN service (declared or installed), like secret/config set:
    # a typo must not manufacture a durable intent row for a service that will
    # never exist — nothing would ever clear it.
    declared, _invalid = services_d.load_declarations(home, tolerant=True)
    installed = (manager.services_dir / name).is_dir()
    if name not in declared and not installed:
        raise SyrvisError(
            "no such service {!r} (not declared in config/services.d and not "
            "installed) — shed records intent about a real workload".format(name)
        )

    row = intent_mod.shed(home, name, reason, until=until, by="cli")
    # Stop WITHOUT touching the declaration: the shed row is the intent now, so
    # lifting it must restore the service exactly as the declaration describes.
    stop_ok, stop_msg = True, "already stopped"
    container = declared[name].container_name or name if name in declared else name
    if manager._get_service_status(container) not in ("stopped", "exited", "unknown"):
        stop_ok, stop_msg = manager.stop(name, set_intent=False)

    if as_json:
        click.echo(
            jsonlib.dumps(
                {"shed": row, "stopped": stop_ok, "detail": stop_msg, "ok": stop_ok}, indent=2
            )
        )
        if not stop_ok:
            raise SystemExit(1)
        return
    click.echo(
        "Shed '{}' (reason: {}{}). Intent recorded — it survives apply/reconcile.".format(
            name, row["reason"], ", until {}".format(row["until"]) if row["until"] else ""
        )
    )
    click.echo("  stop: {}".format(stop_msg))
    if not stop_ok:
        # The INTENT is what had to be durable and it is written; a failed stop
        # is a separate, retryable problem (and reconcile will stop it anyway).
        raise SyrvisError("intent recorded, but the container could not be stopped: " + stop_msg)


@service.command("unshed")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def service_unshed(name, as_json):
    """Lift a service's shed — it may run again (nothing is started here).

    Deliberately does NOT start the service: the declaration underneath was
    never touched, so `syrvis reconcile` (or `service start`) is the one
    bring-up path, with the ordering, hooks and health handling that path
    already has.

        sudo syrvis service unshed -- onyx-api
    """
    privilege.ensure_elevated("Lifting a shed requires elevated privileges.")
    from syrviscore import intent as intent_mod
    from syrviscore.paths import get_syrvis_home

    home = get_syrvis_home()
    row = intent_mod.unshed(home, name)
    if as_json:
        click.echo(jsonlib.dumps({"unshed": row, "changed": row is not None}, indent=2))
        return
    if row is None:
        click.echo("Service '{}' was not shed — nothing to lift.".format(name))
        return
    click.echo(
        "Lifted the shed on '{}' (was: {}, since {}).".format(
            name, row.get("reason", "?"), row.get("since", "?")
        )
    )
    click.echo("Nothing was started — run 'sudo syrvis reconcile' to bring it back.")


@service.command("recreate")
@click.argument("name")
@handle_errors
def service_recreate(name):
    """Replace a service's container without changing declared intent.

    The verb to reach for when the CONTENT behind the compose spec changed but
    the spec did not — above all a rewritten env_file, whose values Docker bakes
    into the container at CREATE time (a `restart` re-runs the same container
    with the same baked env and re-reads nothing).

    Unlike `service stop` + `service start`, this writes NO `enabled:` flag, so a
    failure cannot leave the service declared off and held down by reconcile.

        sudo syrvis service recreate -- onyx-opensearch
    """
    privilege.ensure_elevated("Recreating a service container requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.recreate(name)
    if success:
        click.echo(message)
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


@service.command("set-image")
@click.argument("name")
@click.option("--image", required=True, help="New pinned image reference (must not be :latest)")
@handle_errors
def service_set_image(name, image):
    """Re-pin an image-first L2 service to a new image and redeploy (declarative update).

    The apply path for a container-image update from `syrvis updates`: swaps the
    manifest's image (re-validated: must be a pinned, audited ref), regenerates
    config, dual-writes the declaration, then pulls + restarts. Git-based
    services update via `syrvis service update` instead.

        sudo syrvis service set-image --image traefik:v3.7.0 -- traefik
    """
    privilege.ensure_elevated("Re-pinning a service image requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.set_image(name, image)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


@service.command("rollback")
@click.argument("name")
@click.option(
    "--to",
    "to_revision",
    type=int,
    default=None,
    help="Deployment revision to restore (see 'syrvis history NAME'); "
    "defaults to the previous successful revision",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def service_rollback(name, to_revision, yes):
    """Roll back a service to a prior deployment revision.

    Restores the target revision's manifest (image, env, volumes, routing)
    through the full trust boundary and redeploys — data and secrets are left
    in place. Records a NEW revision, Helm-style. Note: the next IaC apply or
    reconcile that still declares the newer image will redeploy it; revert the
    deployment repo too to make a rollback durable.

        sudo syrvis service rollback --to 3 -- cyberquill
    """
    privilege.ensure_elevated("Rolling back a service requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    if not yes:
        target = "revision {}".format(to_revision) if to_revision else "the previous revision"
        click.echo(f"This will redeploy '{name}' at {target}.")
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted")
            return

    manager = ServiceManager()
    success, message = manager.rollback(name, to_revision)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


@service.command("task")
@click.argument("name")
@click.option(
    "--task",
    "task_name",
    required=True,
    help="Declared task to run (a name from the manifest's tasks: block)",
)
@handle_errors
def service_task(name, task_name):
    """Run a DECLARED one-shot task inside a service's running container.

    Tasks are pre-declared, schema-audited argvs in the service manifest
    (tasks: {name: {command: [...]}}) — the encapsulated alternative to a raw
    `docker exec` for things like a DB bootstrap. The argv comes from the
    installed manifest only; this command picks a task NAME, never code:

        sudo syrvis service task --task init-legal-db -- immich-db
    """
    privilege.ensure_elevated("Running a service task requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    manager = ServiceManager()
    success, message = manager.run_task(name, task_name)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


# =============================================================================
# Scheduled jobs command group (OPTIONAL; dormant with empty config/jobs.d)
# =============================================================================


@cli.group()
def schedule():
    """Manage OPTIONAL scheduled jobs (config/jobs.d → managed /etc/crontab block).

    A job declaration carries {schedule, enabled} only — never a command or a
    source. The command is DERIVED as jobs/<name> (a root-owned, vetted script),
    and the script source is the SINGLE root-configured repo in config/jobs.source
    (the operator cannot set it). With no jobs.d entries the feature is dormant and
    invisible. `apply` is a LOCAL reconcile (no fetch); `sync` clones the source.
    """
    pass


@schedule.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def schedule_list(as_json):
    """List declared jobs and the current managed crontab block (read-only)."""
    from syrviscore import jobs_d, schedule as schedule_lib

    try:
        home = get_syrvis_home()
        declarations, invalid = jobs_d.load_job_declarations(home)
        plan = schedule_lib.compute_plan(home)
        jobs_out = [
            {
                "name": name,
                "schedule": job.schedule,
                "enabled": job.enabled,
                "command": job.derived_command(jobs_dir=home / "jobs"),
            }
            for name, job in sorted(declarations.items())
        ]
        result = {
            "jobs": jobs_out,
            "invalid": invalid,
            "source": plan.get("source"),
            "managed_block": sorted(plan["desired"].values()),
            "plan": plan,
        }
    except Exception as e:  # noqa: BLE001 - CLI boundary
        if as_json:
            json_error(e, indent=2)
        raise

    if as_json:
        click.echo(jsonlib.dumps(result, indent=2, default=str))
        return

    if not jobs_out:
        click.echo("No scheduled jobs declared (config/jobs.d is empty)")
        return
    click.echo()
    click.echo("Declared jobs:")
    for job in jobs_out:
        state = "enabled" if job["enabled"] else "disabled"
        click.echo("  {} [{}] {} -> {}".format(job["name"], state, job["schedule"], job["command"]))
    for row in invalid:
        click.echo("  ! invalid {}: {}".format(row["file"], row["error"]))
    click.echo()


@schedule.command("dsm-tasks")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def schedule_dsm_tasks(as_json):
    """Census of DSM's OWN Task Scheduler entries (read-only; never modified).

    `schedule list` reports only SyrvisCore's delimited /etc/crontab block, so a
    DSM task pointing anywhere else has always been invisible over the seam —
    the gap design/20 was written about. This enumerates them via
    `synoschedtask --get` so a deployment can compare "what DSM runs" against
    "what the repo believes". SyrvisCore never creates, edits or deletes a DSM
    task; the tool is root-only, so run this with sudo (the seam row does).
    """
    from syrviscore import schedule as schedule_lib

    result = schedule_lib.dsm_task_census()
    if as_json:
        click.echo(jsonlib.dumps(result, indent=2, default=str))
        return
    click.echo()
    if not result["ok"]:
        click.echo("DSM Task Scheduler census UNAVAILABLE: {}".format(result["error"]))
        click.echo()
        return
    click.echo("DSM Task Scheduler: {} task(s) [{}]".format(result["count"], result["tool"]))
    for task in result["tasks"]:
        click.echo(
            "  {:<4} {:<28} {:<8} {}".format(
                task.get("id", "?"),
                (task.get("name") or "(unnamed)")[:28],
                task.get("state", "?"),
                task.get("command", ""),
            )
        )
    click.echo()


@schedule.command("apply")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def schedule_apply(as_json):
    """LOCAL reconcile of the managed /etc/crontab block from config/jobs.d (privileged).

    No fetch — the scripts are already on disk from the last `schedule sync`. This
    is the self-heal path (boot hook / verify --fix) after DSM regenerates
    /etc/crontab. Rewrites ONLY SyrvisCore's delimited block — never DSM's own lines
    or the header. A declared+enabled job whose jobs/<name> script is missing is
    skipped (never scheduled with a missing script).
    """
    from syrviscore import schedule as schedule_lib

    privilege.ensure_elevated("Applying scheduled jobs requires elevated privileges.")
    try:
        home = get_syrvis_home()
        result = schedule_lib.apply_schedule(home)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        if as_json:
            json_error(e, indent=2)
        raise

    if as_json:
        click.echo(jsonlib.dumps(result, indent=2, default=str))
        if not result["ok"]:
            raise SystemExit(1)
        return

    click.echo()
    click.echo("Scheduled: {}".format(", ".join(result["scheduled"]) or "(none)"))
    for row in result.get("skipped", []):
        click.echo("  ~ skipped {}: {}".format(row["name"], row["reason"]))
    for row in result["invalid"]:
        click.echo("  ! invalid {}: {}".format(row["file"], row["error"]))
    click.echo()
    if not result["ok"]:
        raise SystemExit(1)


@schedule.command("sync")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def schedule_sync(as_json):
    """Clone the ROOT-configured source (config/jobs.source), install its jobs, reconcile.

    Clones the SINGLE root-configured repo, copies its jobs.d/*.yaml into
    config/jobs.d/ (each re-validated; the source is authoritative — locally removed
    declarations are dropped), materializes each enabled job's root-owned jobs/<name>
    script (root:root 0755), then LOCAL-reconciles the managed /etc/crontab block.
    With no config/jobs.source configured this is a dormant no-op.
    """
    from syrviscore import schedule as schedule_lib

    privilege.ensure_elevated("Syncing scheduled jobs requires elevated privileges.")
    try:
        home = get_syrvis_home()
        result = schedule_lib.sync_from_source(home)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        if as_json:
            json_error(e, indent=2)
        raise

    if as_json:
        click.echo(jsonlib.dumps(result, indent=2, default=str))
        if not result["ok"]:
            raise SystemExit(1)
        return

    click.echo()
    if not result.get("applied"):
        click.echo(result.get("message", "No config/jobs.source configured — dormant"))
        click.echo()
        return
    click.echo("Source: {}".format(result.get("source")))
    for m in result.get("synced", []):
        mark = "[+]" if m["ok"] else "[-]"
        click.echo("  {} {}: {}".format(mark, m["name"], m["message"]))
    reconcile = result.get("reconcile") or {}
    click.echo("Scheduled: {}".format(", ".join(reconcile.get("scheduled", [])) or "(none)"))
    for row in reconcile.get("skipped", []):
        click.echo("  ~ skipped {}: {}".format(row["name"], row["reason"]))
    click.echo()
    if not result["ok"]:
        raise SystemExit(1)


# =============================================================================
# Profile command group (platform-curated service sets)
# =============================================================================


@cli.group()
def profile():
    """Platform-curated service profiles (e.g. the monitoring stack)."""
    pass


@profile.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def profile_list(as_json):
    """List available profiles and their member services."""
    from syrviscore import profiles

    entries = profiles.list_profiles()
    if as_json:
        click.echo(jsonlib.dumps({"profiles": entries}, indent=2))
        return
    for entry in entries:
        click.echo("{} — {}".format(entry["name"], entry["description"]))
        click.echo("  services: {}".format(", ".join(entry["services"])))


@profile.command("enable")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option("--dry-run", is_flag=True, help="Report what would be declared; write nothing")
@handle_errors
def profile_enable(name, as_json, dry_run):
    """Declare a profile's services + seed default configs (then reconcile).

    Writes a services.d declaration for each member (platform-pinned catalog
    images; infra-tier members get their host mounts because the declaration
    is operator-authored) and seeds generic default configs — never
    overwriting anything that already exists. Converge with `syrvis reconcile`.
    """
    from syrviscore import profiles
    from syrviscore.paths import get_syrvis_home

    if not dry_run:
        privilege.ensure_elevated("Declaring profile services requires elevated privileges.")
    try:
        report = profiles.enable_profile(name, get_syrvis_home(), dry_run=dry_run)
    except SyrvisError as e:
        if as_json:
            json_error(e, indent=2)
        raise
    if as_json:
        click.echo(jsonlib.dumps(report, indent=2))
        return
    click.echo(
        "declared: {} (kept existing: {})".format(
            ", ".join(report["declared"]) or "(none)",
            ", ".join(report["existing_declarations_kept"]) or "(none)",
        )
    )
    click.echo(
        "configs seeded: {} (kept existing: {})".format(
            ", ".join(report["configs_written"]) or "(none)",
            ", ".join(report["configs_kept"]) or "(none)",
        )
    )
    click.echo("(dry run — nothing written)" if dry_run else "Run 'syrvis reconcile' to converge.")


# =============================================================================
# Secret command group (Layer 2 service secrets — operator seam)
# =============================================================================


@cli.group()
def secret():
    """Manage Layer 2 service secrets (env_file contents, written 0600 as root)."""
    pass


@secret.command("set")
@click.argument("name")
@handle_errors
def secret_set(name):
    """Write a service's env_file secret from STDIN (root-only, atomic, 0600).

    Reads the secret from STDIN and writes it atomically to the path derived
    from the service's services.d declaration env_file field.  The data dir
    must already exist (deploy the service first with `syrvis reconcile`).

    The secret NEVER appears in argv — it is read exclusively from stdin so
    it does not appear in ps/audit logs or shell history.

    Example (from home-tech apply-immich-secrets):
        echo "POSTGRES_PASSWORD=secret" | sudo syrvis secret set -- immich-db
    """
    privilege.ensure_elevated("Writing service secrets requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    # Read the secret ONLY from stdin (never argv / env — keeps it out of ps,
    # audit logs, and shell history).
    content = click.get_text_stream("stdin").read()

    if not content:
        raise SyrvisError("secret content must not be empty (nothing on stdin)")

    _MAX = 65536  # 64 KiB; matches ServiceManager._SECRET_MAX_BYTES
    if len(content.encode("utf-8", errors="surrogateescape")) > _MAX:
        raise SyrvisError(f"secret content too large (max {_MAX} bytes)")

    manager = ServiceManager()
    success, message = manager.write_secret(name, content)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


# =============================================================================
# Top-level convenience commands
# =============================================================================


@cli.command("deploy")
@click.argument("name")
@click.option(
    "--force",
    is_flag=True,
    help="Deploy even while a RAID array is rebuilding (the override is journaled)",
)
@handle_errors
def deploy(name, force):
    """Apply a resolved deployment bundle (JSON on STDIN) to service NAME (root-only).

    The encapsulated services-plane apply (design/21): one syrvis-bundle — the
    service manifest + non-secret configs + secret values — becomes a running,
    up-to-date service atomically (install OR update; configs 0644; env_file
    0600; start LAST; rollback on failure). NAME is the authoritative target; a
    bundle whose service.name differs is rejected.

    The bundle arrives on STDIN only — secrets never touch argv/ps/logs. A
    deployment repo assembles it from discoverable files (design/21; home-tech's
    scripts/deploy-stack) and streams it over the operator seam:

        <bundle.json> | sudo syrvis deploy -- snmp-exporter

    While a RAID array is rebuilding, deploying is REFUSED (a deploy pulls
    images and recreates containers against the spindles the rebuild is using).
    Override with --force; the override is recorded in logs/overrides.log.
    """
    privilege.ensure_elevated("Deploying a service requires elevated privileges.")
    from syrviscore import guards
    from syrviscore.bundle import BundleValidationError, DeployBundle
    from syrviscore.service_manager import ServiceManager

    # Secrets arrive on stdin only (never argv/ps); byte-capped shared reader.
    doc = read_json_stdin("bundle")
    try:
        bundle = DeployBundle.from_dict(doc)
    except BundleValidationError as e:
        raise SyrvisError(str(e))
    # The argv NAME is the shim-gated, authoritative target; refuse a bundle that
    # claims a different service.name (never deploy something else than named).
    if bundle.service.name != name:
        raise SyrvisError(
            f"bundle service name {bundle.service.name!r} does not match deploy target {name!r}"
        )

    manager = ServiceManager()
    guards.guard_bulk_degraded("deploy", force=force, home=manager.syrvis_home, services=[name])
    success, message = manager.deploy_bundle(bundle)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


@cli.command("apply")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (operator seam)")
@click.option("--dry-run", is_flag=True, help="Validate and report the plan; write nothing")
@click.option(
    "--allow-secret-change",
    is_flag=True,
    help="Permit changing an existing secret value in .env (deliberate rotation)",
)
@click.option(
    "--allow-enable-change",
    is_flag=True,
    help="Permit re-enabling declared-off services (deliberate resurrection)",
)
@handle_errors
def apply_cmd(as_json, dry_run, allow_secret_change, allow_enable_change):
    """Apply a core-tier instance bundle (JSON on STDIN) to this install (root-only).

    The core-tier sibling of `syrvis deploy`: one syrvis-instance bundle — the
    runtime .env content, the stack.yaml enablement, and the complete
    services.d/ declaration set — is validated and written atomically, so a
    deployment repo never writes files under $SYRVIS_HOME/config itself.
    Applying only WRITES configuration; converge afterwards with
    `syrvis stack apply` / `syrvis reconcile` (already seam verbs).

    The bundle arrives on STDIN only — tokens/secrets never touch argv/ps/logs.
    Reports name env keys, never values. Changing an EXISTING secret value
    requires --allow-secret-change (deliberate rotation):

        <instance.json> | sudo syrvis apply --json

    Two intent guards apply to the declaration set. A SHED service is always
    written `enabled: false`, whatever the bundle says (its shed is the
    operator's decision and the bundle cannot express it). Any OTHER service
    the bundle would flip from declared-off to declared-on is REFUSED by name
    unless --allow-enable-change — the guard that would have stopped fourteen
    load-shed services from being resurrected mid-rebuild (incident
    2026-08-16). Overrides are recorded in logs/overrides.log.
    """
    privilege.ensure_elevated("Applying an instance bundle requires elevated privileges.")
    from syrviscore.instance_bundle import InstanceBundle, apply_instance_bundle

    try:
        # Secrets arrive on stdin only; InstanceBundleError IS a SyrvisError, so
        # it flows to the envelope below without a code-flattening rewrap.
        doc = read_json_stdin("bundle")
        bundle = InstanceBundle.from_dict(doc)
        report = apply_instance_bundle(
            bundle,
            get_syrvis_home(),
            allow_secret_change=allow_secret_change,
            dry_run=dry_run,
            allow_enable_change=allow_enable_change,
        )
    except SyrvisError as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if as_json:
        click.echo(jsonlib.dumps(report, indent=2))
        return
    for section in ("env", "stack", "declarations"):
        entry = report[section]
        if entry is None:
            click.echo(f"{section}: not in bundle")
        elif section == "declarations":
            click.echo(
                "declarations: wrote {}, removed {}, unchanged {}".format(
                    len(entry["written"]), len(entry["removed"]), len(entry["unchanged"])
                )
            )
            if entry.get("shed_pinned"):
                click.echo(
                    "  shed (pinned enabled: false, bundle overridden): {}".format(
                        ", ".join(entry["shed_pinned"])
                    )
                )
            if entry.get("enable_changes"):
                click.echo(
                    "  re-enabled declared-off service(s): {}".format(
                        ", ".join(entry["enable_changes"])
                    )
                )
        elif section == "env":
            click.echo(
                "env: {} (+{} ~{} -{})".format(
                    entry["action"],
                    len(entry["added"]),
                    len(entry["changed"]),
                    len(entry["removed"]),
                )
            )
        else:
            click.echo(f"stack: {entry['action']}")
    if dry_run:
        click.echo("(dry run — nothing written)")


@cli.command("export")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON (default is YAML)")
@click.option(
    "--reveal-secrets",
    is_flag=True,
    help="Include real secret values (elevated; output is sensitive). Default: redacted.",
)
@handle_errors
def export_cmd(as_json, reveal_secrets):
    """Export the live instance as a syrvis-instance/v1 bundle (read companion to apply).

    Snapshots .env + stack.yaml + the services.d declaration set — for GitOps
    snapshotting, diffing a desired bundle against reality, or DR inspection.
    Secret VALUES are REDACTED by default (safe to print/commit/diff);
    --reveal-secrets includes real values for a re-appliable backup (requires
    elevation, and the output is sensitive).

        syrvis export                 # redacted YAML to stdout
        sudo syrvis export --reveal-secrets --json > instance.json
    """
    from syrviscore.instance_bundle import export_instance

    if reveal_secrets:
        privilege.ensure_elevated("Revealing secret values requires elevated privileges.")
    bundle = export_instance(get_syrvis_home(), reveal_secrets=reveal_secrets)
    if as_json:
        click.echo(jsonlib.dumps(bundle, indent=2))
    else:
        import yaml as yamllib

        click.echo(yamllib.safe_dump(bundle, default_flow_style=False, sort_keys=False), nl=False)


@cli.command("updates")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@click.option("--refresh", is_flag=True, help="Bypass the cache and re-query registries")
@handle_errors
def updates(as_json, refresh):
    """Report available container-image updates (report-only; never pulls).

    Checks every pinned image SyrvisCore runs — the core tier and installed
    Layer 2 services — against its registry and reports newer compatible tags.
    Applying an update stays a deliberate, declarative act: `syrvis service
    set-image <name> <ref>` for an L2 service; a new SyrvisCore release (which
    ships new core pins) for the core tier. Results are cached ~6h.
    """
    from syrviscore import image_updates

    try:
        home = get_syrvis_home()
    except SyrvisHomeError:
        home = None
    report = image_updates.check_updates(home=home, refresh=refresh)

    if as_json:
        click.echo(jsonlib.dumps(report, indent=2, default=str))
        return

    imgs = report.get("images", [])
    if not imgs:
        click.echo("No images to check (nothing installed yet).")
        return
    n = report.get("update_count", 0)
    click.echo(
        "{} image(s) checked, {} update(s) available{}:".format(
            report.get("count", len(imgs)),
            n,
            " (cached)" if report.get("cached") else "",
        )
    )
    click.echo()
    for img in imgs:
        name = img.get("name", "?")
        if img.get("error"):
            click.echo("  {:<22} {}  — {}".format(name, img.get("image", ""), img["error"]))
        elif img.get("update_available"):
            click.echo(
                "  {:<22} {} → {}  (newer: {})".format(
                    name,
                    img.get("current"),
                    img.get("latest"),
                    ", ".join(img.get("newer", [])),
                )
            )
        else:
            click.echo("  {:<22} {}  up to date".format(name, img.get("current")))


@cli.command("history")
@click.argument("workload", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@click.option("--limit", type=int, default=None, help="Newest N revisions per workload")
@click.option(
    "--revision",
    "revision",
    type=int,
    default=None,
    help="Show one revision in full (requires WORKLOAD)",
)
@handle_errors
def history(workload, as_json, limit, revision):
    """Show deployment history: revisions with image, env names, volumes, outcome.

    Every deploy/remove/rollback of a managed workload appends a revision under
    data/deployments/. Inline env VALUES are always redacted here — records
    show which variables a deployment exposed, never their contents. Roll a
    service back to a listed revision with 'syrvis service rollback'.
    """
    from syrviscore import deployments

    try:
        home = get_syrvis_home()
        if revision is not None:
            if not workload:
                raise SyrvisError("--revision requires a WORKLOAD argument")
            record = deployments._redact_for_output(
                deployments.load_revision(home, workload, revision)
            )
            report = {"workloads": {workload: [record]}, "invalid": []}
        else:
            report = deployments.load_history(home, workload=workload, limit=limit)

        if as_json:
            click.echo(jsonlib.dumps(report, indent=2, default=str))
            return
    except Exception as e:
        if as_json:
            json_error(e, indent=2)
        raise

    workloads = report.get("workloads", {})
    if not workloads:
        target = f" for '{workload}'" if workload else ""
        click.echo(f"No deployment history{target} yet.")
        click.echo("Records appear as services are deployed, updated, or removed.")
        return

    for name, records in workloads.items():
        click.echo()
        click.echo(f"{name}:")
        widths = (5, 18, 10, 11, 8, 0)
        click.echo(
            format_row(list(zip(("REV", "WHEN", "ACTION", "TRIGGER", "OUTCOME", "IMAGE"), widths)))
        )
        click.echo("-" * 78)
        for rec in records:
            if rec.get("tier") == "core":
                changed = []
                pins, prev = rec.get("pins") or {}, rec.get("previous_pins") or {}
                for svc, pin in sorted(pins.items()):
                    if prev.get(svc) != pin:
                        changed.append(svc)
                image = "pins: {}".format(", ".join(changed) or "(unchanged set)")
            else:
                image = rec.get("image") or "-"
                if rec.get("previous_image") and rec.get("previous_image") != rec.get("image"):
                    image = "{} ← {}".format(image, rec["previous_image"])
                if rec.get("rollback_of"):
                    image += " (rollback of {})".format(rec["rollback_of"])
            cells = (
                str(rec.get("revision", "?")),
                (rec.get("timestamp") or "")[:16].replace("T", " "),
                rec.get("action", "?"),
                rec.get("trigger", "?"),
                rec.get("outcome", "?"),
                image,
            )
            click.echo(format_row(list(zip(cells, widths))))

    invalid = report.get("invalid") or []
    if invalid:
        click.echo()
        for row in invalid:
            click.echo("  (unreadable record {}: {})".format(row["file"], row["error"]))
    click.echo()


@cli.command("images")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@click.option(
    "--refresh",
    is_flag=True,
    help="Do the network pass (manifest base, newer tags, last-pushed); else cached",
)
@handle_errors
def images(as_json, refresh):
    """Report image provenance + freshness for every pinned image.

    Per image: publisher class + expected base (from the committed
    image_trust.yaml), whether it is digest-pinned, and — with --refresh — a
    newer tag, the manifest base, and the last-pushed date. Reputability is a
    curated git assertion the check validates against (drift is reported);
    the heavy/network fields are cached ~6h. Complements `syrvis updates`.
    """
    from syrviscore import image_provenance

    try:
        home = get_syrvis_home()
    except SyrvisHomeError:
        home = None
    report = image_provenance.build_report(home=home, refresh=refresh)

    if as_json:
        click.echo(jsonlib.dumps(report, indent=2, default=str))
        return

    imgs = report.get("images", [])
    if not imgs:
        click.echo("No images to check (nothing installed yet).")
        return
    click.echo(
        "{} image(s): {} trusted, {} need attention{}".format(
            report.get("count", len(imgs)),
            report.get("trusted", 0),
            report.get("attention", 0),
            "" if report.get("heavy_fresh") else "   (freshness cached — --refresh to update)",
        )
    )
    click.echo()
    for img in imgs:
        mark = "OK" if img.get("trust") == "ok" else "!!"
        name = img.get("name") or img.get("repository", "?")
        pub = img.get("publisher_class", "?")
        base = (
            img.get("base_from_manifest")
            or img.get("base_from_label")
            or img.get("expected_base")
            or "?"
        )
        pin = "digest" if img.get("digest_pinned") else "TAG"
        upd = "  ^{}".format(img.get("latest")) if img.get("update_available") else ""
        click.echo("  {} {:<22} {:<13} base:{:<16} {}{}".format(mark, name, pub, base, pin, upd))
        if img.get("trust") != "ok":
            for note in img.get("notes", []):
                click.echo("        - {}".format(note))
    click.echo()


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
@handle_errors
def status(as_json):
    """Show status of all services (alias for 'core status')."""
    try:
        manager = DockerManager()
        statuses = manager.get_container_status()
        active = get_active_version()

        # Cheap, no-network image-trust summary (best-effort).
        try:
            from syrviscore import image_provenance

            _home = get_syrvis_home()
        except Exception:  # noqa: BLE001
            _home = None
        try:
            trust_summary = image_provenance.status_summary(_home)
        except Exception:  # noqa: BLE001
            trust_summary = {"count": 0, "trusted": 0, "attention": 0}

        # Intentionally-halted vs everything-crashed must be distinguishable.
        try:
            from syrviscore import lifecycle

            runstate = lifecycle.read_runstate() or {"state": "active"}
        except Exception:  # noqa: BLE001
            runstate = {"state": "active"}

        # Declared intent: the same distinction one level down. `halted` says
        # the INSTANCE is deliberately down; `shed` says these SERVICES are.
        try:
            from syrviscore import intent as intent_mod

            intent_block = intent_mod.summary(_home)
        except Exception:  # noqa: BLE001 - no home / unreadable intent
            intent_block = {
                "device": "in-service",
                "drained": False,
                "shed": [],
                "shed_count": 0,
                "shed_reasons": {},
                "shed_expired": [],
            }

        if as_json:
            click.echo(
                jsonlib.dumps(
                    {
                        "version": active,
                        "runstate": runstate,
                        "intent": intent_block,
                        "services": statuses,
                        "images": trust_summary,
                    },
                    indent=2,
                    default=str,
                )
            )
            return
    except Exception as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if runstate.get("state") == "halted":
        click.echo()
        click.echo(
            "INSTANCE HALTED (reason: {}, since {}) — services are intentionally "
            "stopped. Run 'syrvis resume'.".format(
                runstate.get("reason", "?"), runstate.get("at", "?")
            )
        )

    if intent_block.get("drained"):
        click.echo()
        click.echo("DEVICE INTENT: drained — workloads are deliberately not running.")
    if intent_block.get("shed_count"):
        click.echo()
        reasons = ", ".join(
            "{} x{}".format(r, n) for r, n in sorted(intent_block["shed_reasons"].items())
        )
        click.echo(
            "SHED: {} service(s) deliberately down ({}). 'syrvis service list' names them.".format(
                intent_block["shed_count"], reasons
            )
        )
        if intent_block.get("shed_expired"):
            click.echo(
                "  [!] past their review date: {} — unshed or extend.".format(
                    ", ".join(intent_block["shed_expired"])
                )
            )

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

    if trust_summary.get("count"):
        click.echo()
        attention = trust_summary.get("attention", 0)
        note = "" if not attention else "  ('syrvis images' for detail)"
        click.echo(
            "Images: {} trusted · {} need attention{}".format(
                trust_summary.get("trusted", 0), attention, note
            )
        )

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
@click.option(
    "--graceful",
    is_flag=True,
    help="Graceful full-instance restart: ordered stop of every managed "
    "workload (hooks + stop grace), then ordered bring-up. Without it: "
    "core-only force-recreate (existing behavior).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report (with --graceful)")
@handle_errors
def restart(graceful, as_json):
    """Restart all services (alias for 'core restart')."""
    privilege.ensure_elevated("Restarting services requires elevated privileges.")
    if graceful:
        from syrviscore import lifecycle

        try:
            home = get_syrvis_home()
            down = lifecycle.shutdown_instance(home, reason="maintenance", by="restart")
            up = lifecycle.resume_instance(home, by="restart")
        except Exception as e:
            if as_json:
                json_error(e, indent=2)
            raise
        report = {
            "action": "restart",
            "shutdown": down,
            "resume": up,
            "ok": down.get("ok", False) and up.get("ok", False),
        }
        if as_json:
            click.echo(jsonlib.dumps(report, indent=2, default=str))
        else:
            click.echo(
                "Gracefully restarted the instance ({})".format(
                    "ok" if report["ok"] else "degraded — see 'syrvis status'"
                )
            )
        if not report["ok"]:
            raise SystemExit(2)
        return
    click.echo("Restarting services...")
    manager = DockerManager()
    manager.restart_core_services()
    click.echo("Services restarted")
    click.echo("Run 'syrvis status' to verify")


@cli.command()
@click.option(
    "--reason",
    type=click.Choice(["ups", "reboot", "maintenance"]),
    default="maintenance",
    help="Why the instance is halting. 'ups' (power returned) and 'reboot' (a "
    "DSM shutdown/reboot) both resume automatically on the next boot; "
    "'maintenance' stays down until 'syrvis resume'.",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=int,
    default=None,
    help="Wall-clock budget in seconds (default 180); stragglers are force-stopped",
)
@click.option(
    "--vm-deadline",
    type=int,
    default=None,
    help="Max seconds to wait for VMs to power off gracefully (default 90)",
)
@click.option(
    "--hold/--resume-on-boot",
    "hold",
    default=None,
    help="Override the boot policy the reason implies",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report")
@handle_errors
def shutdown(reason, timeout_s, vm_deadline, hold, as_json):
    """Gracefully stop every managed workload and halt the instance.

    The UPS-on-battery verb: fires the instance pre-shutdown hook, issues VM
    ACPI shutdown (guests drain in parallel), stops Layer 2 services in
    priority order (pre-stop hooks quiesce databases, then a per-service stop
    grace), waits for VMs (force-off stragglers), and stops the core stack
    with Traefik last. Declared intent is untouched; 'syrvis resume' (or the
    next boot, for --reason ups) brings everything back.
    """
    from syrviscore import lifecycle

    privilege.ensure_elevated("Shutting down the instance requires elevated privileges.")
    kwargs = {}
    if timeout_s is not None:
        kwargs["budget_s"] = timeout_s
    if vm_deadline is not None:
        kwargs["vm_deadline_s"] = vm_deadline
    if hold is not None:
        kwargs["resume_on_boot"] = not hold
    try:
        report = lifecycle.shutdown_instance(get_syrvis_home(), reason=reason, **kwargs)
    except Exception as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if as_json:
        click.echo(jsonlib.dumps(report, indent=2, default=str))
    else:
        for phase in report["phases"]:
            mark = "[+]" if phase["ok"] else "[-]"
            click.echo("  {} {} ({} item(s))".format(mark, phase["phase"], len(phase["items"])))
        click.echo()
        click.echo(
            "Instance halted (reason: {}; {}). Resume with 'syrvis resume'.".format(
                report["reason"],
                "auto-resumes on next boot" if report["resume_on_boot"] else "stays down on boot",
            )
        )
    if report.get("exit"):
        raise SystemExit(report["exit"])


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report")
@handle_errors
def resume(as_json):
    """Bring a halted instance back: core stack, VMs, then Layer 2 services.

    Clears the halted state and starts everything that was running before the
    shutdown (declared intent was never touched, so the one reconcile engine
    restores it). A no-op when the instance is active.
    """
    from syrviscore import lifecycle

    privilege.ensure_elevated("Resuming the instance requires elevated privileges.")
    try:
        report = lifecycle.resume_instance(get_syrvis_home())
    except Exception as e:
        if as_json:
            json_error(e, indent=2)
        raise

    if as_json:
        click.echo(jsonlib.dumps(report, indent=2, default=str))
    elif not report.get("changed"):
        click.echo("Instance is active — nothing to resume.")
    else:
        for phase in report["phases"]:
            mark = "[+]" if phase["ok"] else "[-]"
            click.echo("  {} {} ({} item(s))".format(mark, phase["phase"], len(phase["items"])))
        click.echo()
        click.echo("Instance resumed ({}).".format("ok" if report.get("ok") else "degraded"))
    if report.get("exit"):
        raise SystemExit(report["exit"])


@cli.command("_regen-boot-hooks", hidden=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report")
@handle_errors
def regen_boot_hooks(as_json):
    """Regenerate the managed boot artifacts from THIS (active) version.

    Internal seam between the manager and the service: ``syrvisctl activate``
    (the real deploy path) invokes ``<newly-activated>/bin/syrvis
    _regen-boot-hooks`` after switching the ``current`` symlink, so the boot
    hook (``syrvis-startup.sh`` + the rc.d ``S99syrviscore.sh``) is always
    re-laid from the just-activated code and can never drift behind an
    upgrade/rollback. The manager cannot import an arbitrary activated version,
    so this runs in the version's own venv (mirroring the seam generator).

    Best-effort by construction: renders both artifacts, reports per-artifact
    outcome, and never raises for a render failure (a bad boot-hook regen must
    not fail the activation that already succeeded).
    """
    from syrviscore import privileged_ops

    privilege.ensure_elevated("Regenerating boot hooks requires elevated privileges.")

    install_dir = get_syrvis_home()
    ops = privileged_ops.get_system_operations()
    results = {}
    try:
        username = ops.get_target_user()
    except Exception as e:  # noqa: BLE001 - fall back rather than abort
        username = None
        results["target_user_error"] = str(e)

    if username:
        try:
            ok, msg = ops.ensure_startup_script(install_dir, username)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, "render failed: {}".format(e)
        results["startup_script"] = {"ok": ok, "message": msg}

        try:
            ok, msg = ops.ensure_boot_script(install_dir)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, "render failed: {}".format(e)
        results["boot_script"] = {"ok": ok, "message": msg}

    if as_json:
        click.echo(jsonlib.dumps(results, indent=2, default=str))
    else:
        for name, r in results.items():
            if isinstance(r, dict) and "ok" in r:
                mark = "[+]" if r["ok"] else "[-]"
                click.echo("  {} {}: {}".format(mark, name, r["message"]))
            else:
                click.echo("  [-] {}: {}".format(name, r))


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
@click.option(
    "--force",
    is_flag=True,
    help="Converge even while a RAID array is rebuilding (the override is journaled)",
)
@handle_errors
def reconcile(dry_run, as_json, prune, strict, boot, yes, force):
    """Converge to the declared services in config/services.d/.

    Loads every declaration with per-file failure isolation (a broken file
    marks only that service invalid), then converges each service
    independently (one failure never blocks the rest). Installed services with
    no declaration are reported as unmanaged and never touched unless --prune.

    SHED services (data/state/intent.json) are planned exactly as if they were
    declared `enabled: false`, whatever their declaration says — that overlay
    is how a deliberate load-shed survives a GitOps apply. They are reported
    separately from `disabled`, and a shed service found RUNNING is stopped.

    While a RAID array is rebuilding, a mutating reconcile is REFUSED (it pulls
    images and recreates containers against the rebuilding spindles). Override
    with --force; the override is recorded in logs/overrides.log. --dry-run and
    --boot are never blocked: planning is free, and boot recovery must proceed.

    Exit code: non-zero for any INVALID declaration file (corrupted intent
    must never pass silently) or a CRITICAL service's failure; any failure
    with --strict. --boot is always best-effort and exits 0.
    """
    from syrviscore import guards, lifecycle, services_d
    from syrviscore.service_manager import ServiceManager

    if boot:
        prune = None  # boot never destroys anything
        # The instance-halted matrix: a UPS halt auto-resumes when power (and
        # therefore this boot) returns; a maintenance halt survives reboots
        # until an explicit `syrvis resume`.
        state = lifecycle.read_runstate()
        if state is not None:
            if not state.get("resume_on_boot"):
                if as_json:
                    click.echo(
                        jsonlib.dumps(
                            {"halted": True, "resumed": False, "reason": state.get("reason")},
                            indent=2,
                        )
                    )
                else:
                    click.echo(
                        "Instance is halted ({}) — run 'syrvis resume' to bring it up.".format(
                            state.get("reason", "maintenance")
                        )
                    )
                return
            privilege.ensure_elevated("Resuming the instance requires elevated privileges.")
            report = lifecycle.resume_instance(get_syrvis_home(), boot=True, by="boot")
            if as_json:
                click.echo(jsonlib.dumps(report, indent=2, default=str))
            else:
                click.echo(
                    "Resumed after {} halt ({}).".format(
                        state.get("reason", "?"), "ok" if report.get("ok") else "degraded"
                    )
                )
            return
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

    # Only a plan with WORK to do is worth refusing: a no-op converge against a
    # rebuilding array costs nothing and refusing it would just train operators
    # to pass --force reflexively. --boot is exempt entirely (recovery first).
    if not boot and plan["actions"]:
        try:
            guards.guard_bulk_degraded(
                "reconcile",
                force=force,
                home=manager.syrvis_home,
                services=[a["name"] for a in plan["actions"]],
            )
        except SyrvisError as e:
            if as_json:
                json_error(e, indent=2)
            raise

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

    try:
        results = services_d.apply_reconcile_plan(manager, declarations, plan)
    except lifecycle.InstanceHaltedError as e:
        if as_json:
            json_error(e, indent=2)
        raise
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
        "Declared: {}  in sync: {}  disabled: {}  shed: {}  terminal: {}  "
        "blocked: {}  unmanaged: {}  invalid: {}".format(
            summary["declared"],
            len(plan["in_sync"]),
            len(plan["disabled"]),
            len(plan.get("shed") or []),
            len(plan.get("terminal") or []),
            len(plan.get("blocked") or []),
            len(plan["unmanaged"]),
            summary["invalid"],
        )
    )
    for name in plan.get("shed") or []:
        click.echo("  [~] shed (declared, deliberately down): {}".format(name))
    for row in plan.get("terminal") or []:
        click.echo("  [.] terminal ({}): {} — {}".format(row["status"], row["name"], row["reason"]))
    for row in plan.get("blocked") or []:
        click.echo(
            "  [>] {}: {} — {} withheld until the dependency is back".format(
                row["reason"], row["name"], row["withheld"]
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
        # Show the image the service becomes; on a replace, the from->to transition.
        img = action.get("image")
        if action["kind"] == "replace" and action.get("from_image") and action["from_image"] != img:
            detail = "  {} -> {}".format(action["from_image"], img)
        elif img:
            detail = "  {}".format(img)
        else:
            detail = ""
        click.echo("    {} {} {}{}{}".format(marker, action["kind"], action["name"], crit, detail))


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
    config_changed = write_traefik_config_files(syrvis_home)
    traefik_data = syrvis_home / "data" / "traefik"
    click.echo(f"  Generated: {traefik_data / 'traefik.yml'}")
    click.echo(f"  Generated: {traefik_data / 'config' / 'dynamic.yml'}")

    # A static change only applies on a Traefik restart, and the file-provider
    # watch is unreliable on Synology bind mounts, so a dynamic change gets the
    # same treatment. Restart the running Traefik so the change applies now.
    if config_changed and restart_traefik_if_running():
        click.echo("  Config changed — restarted Traefik to apply it.")

    click.echo()
    click.echo("Run 'syrvis start' to start services.")


# =============================================================================
# Stack command group (declarative core-tier services)
# =============================================================================


def _regenerate_compose(trigger="cli"):
    """Regenerate docker-compose.yaml + Traefik configs from the declared stack.

    Returns (ok, message). Best-effort: needs .env (network config) present.
    Records an instance-level '@core' deployment revision when the pin set or
    enabled set actually changed (a no-op regen writes nothing).
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

        # Keep Traefik static/dynamic config in sync too (single writer). A
        # static change only applies on a restart, and the file-provider watch
        # is unreliable on Synology bind mounts — restart on any real change.
        config_changed = write_traefik_config_files(p.get_syrvis_home())
        restarted = config_changed and restart_traefik_if_running()

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

        # A regenerated compose may carry new core image pins (e.g. after a
        # service upgrade); drop the updates cache so the report reflects them.
        try:
            from syrviscore import image_updates

            image_updates.invalidate_cache()
        except Exception:  # noqa: BLE001
            pass

        # Deployment history for the core tier: one '@core' revision per real
        # change of the pin/enabled set (best-effort, like the cache drop).
        try:
            from syrviscore import deployments

            pins = {n: svc.get("image") for n, svc in compose["services"].items()}
            enabled = sorted(compose["services"])
            latest = deployments.latest_revision(p.get_syrvis_home(), deployments.CORE_WORKLOAD)
            previous_pins = (latest or {}).get("pins")
            if latest is None or previous_pins != pins or latest.get("core_enabled") != enabled:
                deployments.record_core_apply(
                    p.get_syrvis_home(),
                    pins=pins,
                    core_enabled=enabled,
                    trigger=trigger,
                    previous_pins=previous_pins,
                )
        except Exception:  # noqa: BLE001 - recording must never fail the apply
            pass
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
        ok, regen_msg = _regenerate_compose(trigger="converge")
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
    declarations = plan.get("declarations") or {}
    for action in plan["actions"]:
        marker = "!" if action["destructive"] else "-"
        target = action.get("name") or action.get("service")
        detail = ""
        declared = declarations.get(target) or {}
        if action["kind"] in ("declare", "declare_update", "add", "replace") and declared:
            traefik = declared.get("traefik") or {}
            detail = " ({} at {}, {})".format(
                declared.get("image"),
                traefik.get("subdomain"),
                traefik.get("exposure"),
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


@cli.group()
def dashboard():
    """Generate a Grafana dashboard for the SyrvisCore layers (metrics platform)."""
    pass


@dashboard.command("generate")
@click.option("--json", "as_json", is_flag=True, help="Emit the Grafana dashboard JSON to stdout")
@click.option(
    "--all",
    "emit_all",
    is_flag=True,
    help="Emit the overview PLUS one deep-dive dashboard per service",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Write to a file (overview) or, with --all, a DIRECTORY of <uid>.json files",
)
@click.option(
    "--datasource", default="victoriametrics", help="Grafana datasource UID the panels query"
)
@click.option("--title", default="SyrvisCore — service metrics", help="Overview dashboard title")
@click.option(
    "--uid", default="syrvis-overview", help="Overview dashboard UID (stable across regenerations)"
)
@handle_errors
def dashboard_generate(as_json, emit_all, output, datasource, title, uid):
    """Emit an auto-generated Grafana dashboard for this instance's services.

    Default: a top-level overview (an aggregate row scoped to the SyrvisCore
    containers, then one row per service keyed by container_name). With --all:
    the overview PLUS a per-service DEEP-DIVE dashboard each — a header (what the
    service is + links, from its manifest description/homepage + `dashboard:`
    about/links), the standard measurements, and the service's own declared
    metric panels. A deployment repo drops these into Grafana provisioning
    (design/16). Deterministic: projects the DECLARED set, no Docker daemon.
    """
    from pathlib import Path

    from syrviscore import dashboard as dashboard_mod

    # Load .env so any config-derived enablement reflects the configured instance.
    try:
        env_path = get_env_path()
        if env_path.exists():
            load_dotenv(env_path, override=True)
    except Exception:
        pass

    collector = dashboard_mod.Collector(datasource_uid=datasource)

    if emit_all:
        models = dashboard_mod.generate_all(collector=collector)
        if output:
            out_dir = Path(output)
            out_dir.mkdir(parents=True, exist_ok=True)
            for m in models:
                (out_dir / "{}.json".format(m["uid"])).write_text(dashboard_mod.to_json(m) + "\n")
            click.echo("Wrote {} dashboard(s) to {}/".format(len(models), out_dir))
            return
        if as_json:
            click.echo(jsonlib.dumps({"dashboards": models}, indent=2, default=str))
            return
        click.echo("{} dashboard(s):".format(len(models)))
        for m in models:
            click.echo("  - {:<26} {} panels".format(m["uid"], len(m["panels"])))
        click.echo("\nRe-run with --json (stdout) or -o <dir> to emit the Grafana JSON files.")
        return

    model = dashboard_mod.generate(collector=collector, title=title, uid=uid)
    text = dashboard_mod.to_json(model)
    if output:
        Path(output).write_text(text + "\n")
        click.echo("Wrote {} ({} panels) to {}".format(model["uid"], len(model["panels"]), output))
        return
    if as_json:
        click.echo(text)
        return
    rows = [p for p in model["panels"] if p["type"] == "row"]
    click.echo("Dashboard '{}' (uid {})".format(model["title"], model["uid"]))
    click.echo("  {} panels across {} rows".format(len(model["panels"]), len(rows)))
    for r in rows:
        click.echo("    - {}".format(r["title"]))
    click.echo("\nRe-run with --all (per-service), --json (stdout), or -o <path>.")


@cli.group()
def vm():
    """Manage VMs as SyrvisCore workloads (config/vms.d/*.yaml; Synology VMM).

    A VM is just another declared workload. SyrvisCore owns its lifecycle
    (start/stop/status/adopt) via DSM's synowebapi — it never creates or deletes
    a VM (import it once in VMM, then `vm adopt`). VMM ops need root, so run these
    over the operator seam or with sudo. See docs/vms-workload-design.md.
    """
    pass


def _vm_home():
    try:
        return get_syrvis_home()
    except SyrvisHomeError:
        return None


@vm.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def vm_list(as_json):
    """List declared VMs (vms.d) with their live power state."""
    from syrviscore import vms as vms_mod

    rows = vms_mod.VmManager(home=_vm_home()).list()
    if as_json:
        click.echo(jsonlib.dumps({"vms": rows}, indent=2, default=str))
        return
    if not rows:
        click.echo("No VMs declared (config/vms.d/).")
        return
    click.echo("Declared VMs:")
    for r in rows:
        tags = ["stop {}s".format(r.get("stop_timeout", 90))]
        if r["critical"]:
            tags.append("critical")
        if not r["enabled"]:
            tags.append("disabled")
        click.echo(
            "  {:<20} {:<10} {}  [{}]".format(
                r["name"], r["power"], r["guest_name"], ", ".join(tags)
            )
        )
        if r.get("description"):
            click.echo("      {}".format(r["description"]))


@vm.command("status")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@handle_errors
def vm_status(name, as_json):
    """Show one declared VM's live power state."""
    import dataclasses

    from syrviscore import vms as vms_mod

    st = vms_mod.VmManager(home=_vm_home()).status(name)
    if as_json:
        click.echo(jsonlib.dumps(dataclasses.asdict(st), indent=2, default=str))
        return
    click.echo("{} ({}): {}".format(st.name, st.guest_name, st.power))
    if st.error:
        click.echo("  {}".format(st.error))


@vm.command("start")
@click.argument("name")
@handle_errors
def vm_start(name):
    """Power on a declared VM."""
    from syrviscore import vms as vms_mod

    click.echo(vms_mod.VmManager(home=_vm_home()).start(name))


@vm.command("stop")
@click.argument("name")
@click.option("--hard", is_flag=True, help="Force power-off instead of a graceful ACPI shutdown")
@handle_errors
def vm_stop(name, hard):
    """Stop a declared VM (graceful ACPI shutdown by default)."""
    from syrviscore import vms as vms_mod

    click.echo(vms_mod.VmManager(home=_vm_home()).stop(name, hard=hard))


@vm.command("restart")
@click.argument("name")
@handle_errors
def vm_restart(name):
    """Restart a declared VM (shutdown then power on)."""
    from syrviscore import vms as vms_mod

    click.echo(vms_mod.VmManager(home=_vm_home()).restart(name))


@vm.command("adopt")
@click.argument("guest_name")
@click.option(
    "--name", "decl_name", default=None, help="Declaration name (default: a slug of the guest)"
)
@handle_errors
def vm_adopt(guest_name, decl_name):
    """Write a vms.d declaration from an EXISTING VMM guest (never creates a VM)."""
    from syrviscore import vms as vms_mod

    path = vms_mod.VmManager(home=_vm_home()).adopt(guest_name, name=decl_name)
    click.echo("Wrote {} — review it, set `critical`/`autostart`, then `vm list`.".format(path))


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


@config.command("set")
@click.argument("name")
@handle_errors
def config_set(name):
    """Write a declared job's conf file from STDIN (root-only, atomic, 0600).

    The scheduled-jobs analog of `secret set`. Reads the conf body from STDIN
    and writes it atomically to config/<name>.conf. The name MUST be a job
    declared in config/jobs.d/ (e.g. login-alert, immich-db-backup); undeclared
    names are rejected.

    The conf body NEVER appears in argv — it is read exclusively from stdin so
    it does not appear in ps/audit logs or shell history.

    Example (from home-tech render-job-confs):
        echo "NTFY_URL=https://ntfy.example/topic" | sudo syrvis config set -- login-alert
    """
    privilege.ensure_elevated("Writing job config requires elevated privileges.")
    from syrviscore.service_manager import ServiceManager

    # Read the conf body ONLY from stdin (never argv / env — keeps it out of ps,
    # audit logs, and shell history).
    content = click.get_text_stream("stdin").read()

    if not content:
        raise SyrvisError("config content must not be empty (nothing on stdin)")

    _MAX = 65536  # 64 KiB; matches ServiceManager._SECRET_MAX_BYTES
    if len(content.encode("utf-8", errors="surrogateescape")) > _MAX:
        raise SyrvisError(f"config content too large (max {_MAX} bytes)")

    manager = ServiceManager()
    success, message = manager.write_config(name, content)
    if success:
        click.echo(message)
    else:
        raise SyrvisError(message)


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

    config_changed = write_traefik_config_files(syrvis_home)
    click.echo(f"Generated static config: {traefik_data / 'traefik.yml'}")
    click.echo(f"Generated dynamic config: {traefik_data / 'config' / 'dynamic.yml'}")

    # Static config only applies on a Traefik restart, and the dynamic-file
    # watch is unreliable on Synology bind mounts; restart on any real change.
    if config_changed and restart_traefik_if_running():
        click.echo("Restarted Traefik to apply the config change.")

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
