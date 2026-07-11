"""SyrvisCore CLI - Main entry point."""

import os

import click
from dotenv import load_dotenv

from syrviscore.__version__ import __version__
from syrviscore.compose import generate_compose_from_config
from syrviscore.docker_manager import DockerConnectionError, DockerError, DockerManager
from syrviscore.paths import SyrvisHomeError, get_syrvis_home, get_active_version, get_env_path
from syrviscore.traefik_config import (
    generate_traefik_dynamic_config,
    generate_traefik_static_config,
)
from syrviscore.setup import setup
from syrviscore.doctor import doctor
from syrviscore.update import update
from syrviscore.verify import verify
from syrviscore import privilege


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
    help="internal = LAN-only; tunnel = remote via Cloudflare",
)
def service_add(source, no_start, subdomain, exposure):
    """Add a service from a git URL.

    SOURCE can be a git repository URL containing a syrvis-service.yaml file.
    --subdomain / --exposure override the manifest's routing at enable time.

    Examples:
        syrvis service add https://github.com/user/syrvis-gollum.git
        syrvis service add https://github.com/user/svc.git --subdomain wiki --exposure tunnel
    """
    privilege.ensure_elevated("Adding services requires elevated privileges.")
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        success, message = manager.add(
            source, start=not no_start, subdomain=subdomain, exposure=exposure
        )

        if success:
            click.echo(message)
        else:
            click.echo(f"Error: {message}", err=True)
            raise click.Abort()

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to add service: {e}", err=True)
        raise click.Abort()


@service.command("run")
@click.argument("name")
@click.option("--image", required=True, help="Pinned image reference (e.g. a GHCR tag)")
@click.option("--subdomain", default=None, help="Subdomain to route at (defaults to NAME)")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default="internal",
    help="internal = LAN-only; tunnel = remote via Cloudflare",
)
@click.option("--port", type=int, default=80, help="Container port Traefik forwards to")
@click.option("--env", "env_vars", multiple=True, help="KEY=VALUE runtime env (repeatable)")
@click.option("--description", default="", help="Human description")
@click.option("--no-start", is_flag=True, help="Create but don't start the service")
def service_run(name, image, subdomain, exposure, port, env_vars, description, no_start):
    """Run a Layer 2 service straight from a published image (no git repo).

    The image-first path: hand SyrvisCore an image + how to route it, and it
    synthesizes a validated manifest and runs it. This is what home-tech drives
    over MCP for image-only services.

    Examples:
        syrvis service run cyberquill --image ghcr.io/acme/cyberquill:1.4.0 \\
            --exposure tunnel --port 8080
    """
    privilege.ensure_elevated("Running services requires elevated privileges.")
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        success, message = manager.add_image(
            name,
            image,
            subdomain=subdomain,
            exposure=exposure,
            port=port,
            environment=list(env_vars),
            description=description,
            start=not no_start,
        )
        if success:
            click.echo(message)
        else:
            click.echo(f"Error: {message}", err=True)
            raise click.Abort()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to run service: {e}", err=True)
        raise click.Abort()


@service.command("remove")
@click.argument("name")
@click.option("--purge", is_flag=True, help="Also remove service data")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
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

    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        success, message = manager.remove(name, purge=purge)

        if success:
            click.echo(message)
        else:
            click.echo(f"Error: {message}", err=True)
            raise click.Abort()

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to remove service: {e}", err=True)
        raise click.Abort()


@service.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
def service_list(as_json):
    """List all installed services."""
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        services = manager.list()

        if as_json:
            import json as jsonlib

            click.echo(jsonlib.dumps({"services": services}, indent=2, default=str))
            return

        if not services:
            click.echo("No services installed")
            click.echo()
            click.echo("Add a service with: syrvis service add <git-url>")
            return

        click.echo()
        click.echo(f"{'NAME':<20} {'VERSION':<10} {'STATUS':<12} {'URL'}")
        click.echo("-" * 70)

        for svc in services:
            status_icon = "[+]" if svc["status"] == "running" else "[-]"
            click.echo(
                f"{status_icon} {svc['name']:<17} {svc['version']:<10} "
                f"{svc['status']:<12} {svc['url']}"
            )

        click.echo()

    except Exception as e:
        if as_json:
            import json as jsonlib

            click.echo(jsonlib.dumps({"error": str(e)}, indent=2))
            raise SystemExit(1)
        click.echo(f"Failed to list services: {e}", err=True)
        raise click.Abort()


@service.command("start")
@click.argument("name")
def service_start(name):
    """Start a service."""
    privilege.ensure_elevated("Starting services requires elevated privileges.")
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        success, message = manager.start(name)

        if success:
            click.echo(f"Service '{name}' started")
        else:
            click.echo(f"Error: {message}", err=True)
            raise click.Abort()

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to start service: {e}", err=True)
        raise click.Abort()


@service.command("stop")
@click.argument("name")
def service_stop(name):
    """Stop a service."""
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        success, message = manager.stop(name)

        if success:
            click.echo(f"Service '{name}' stopped")
        else:
            click.echo(f"Error: {message}", err=True)
            raise click.Abort()

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to stop service: {e}", err=True)
        raise click.Abort()


@service.command("update")
@click.argument("name")
def service_update(name):
    """Update a service from its git repository."""
    privilege.ensure_elevated("Updating services requires elevated privileges.")
    try:
        from syrviscore.service_manager import ServiceManager

        manager = ServiceManager()
        success, message = manager.update(name)

        if success:
            click.echo(message)
        else:
            click.echo(f"Error: {message}", err=True)
            raise click.Abort()

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to update service: {e}", err=True)
        raise click.Abort()


# =============================================================================
# Top-level convenience commands
# =============================================================================


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (MCP)")
def status(as_json):
    """Show status of all services (alias for 'core status')."""
    try:
        manager = DockerManager()
        statuses = manager.get_container_status()
        active = get_active_version()

        if as_json:
            import json as jsonlib

            click.echo(
                jsonlib.dumps({"version": active, "services": statuses}, indent=2, default=str)
            )
            return

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

        click.echo(f"{'Service':<15} {'Status':<12} {'Uptime':<20}")
        click.echo("-" * 50)

        for service_name, info in statuses.items():
            status_icon = "[+]" if info["status"] == "running" else "[-]"
            click.echo(
                f"{status_icon} {service_name:<12} {info['status']:<12} {info['uptime']:<20}"
            )

        click.echo()

    except (SyrvisHomeError, DockerConnectionError) as e:
        if as_json:
            import json as jsonlib

            click.echo(jsonlib.dumps({"error": str(e)}, indent=2))
            raise SystemExit(1)
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        if as_json:
            import json as jsonlib

            click.echo(jsonlib.dumps({"error": str(e)}, indent=2))
            raise SystemExit(1)
        click.echo(f"Failed to get status: {e}", err=True)
        raise click.Abort()


@cli.command()
@click.argument("service", required=False)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--tail", "-n", default=100, help="Number of lines to show")
def logs(service, follow, tail):
    """View service logs (alias for 'core logs')."""
    try:
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

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to get logs: {e}", err=True)
        raise click.Abort()


@cli.command()
def start():
    """Start all services (alias for 'core start')."""
    privilege.ensure_elevated("Starting services requires elevated privileges.")
    try:
        click.echo("Starting services...")
        manager = DockerManager()
        manager.start_core_services()
        click.echo("Services started")
        click.echo("Run 'syrvis status' to verify")
    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to start services: {e}", err=True)
        raise click.Abort()


@cli.command()
def stop():
    """Stop all services (alias for 'core stop')."""
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    try:
        click.echo("Stopping services...")
        manager = DockerManager()
        manager.stop_core_services()
        click.echo("Services stopped")
    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to stop services: {e}", err=True)
        raise click.Abort()


@cli.command()
def restart():
    """Restart all services (alias for 'core restart')."""
    privilege.ensure_elevated("Restarting services requires elevated privileges.")
    try:
        click.echo("Restarting services...")
        manager = DockerManager()
        manager.restart_core_services()
        click.echo("Services restarted")
        click.echo("Run 'syrvis status' to verify")
    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to restart services: {e}", err=True)
        raise click.Abort()


@cli.command()
@click.option("--volumes", "-v", is_flag=True, help="Also remove named volumes")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
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

    try:
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

    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to clean: {e}", err=True)
        raise click.Abort()


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
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

    try:
        click.echo()
        click.echo("Resetting services...")
        click.echo()
        click.echo("[1/2] Cleaning up...")
        manager = DockerManager()
        results = manager.reset_core_services()

        # Show what was stopped/removed
        if results.get("containers_stopped"):
            click.echo(f"      Stopped: {', '.join(results['containers_stopped'])}")
        if results.get("networks_cleaned"):
            click.echo(f"      Removed networks: {', '.join(results['networks_cleaned'])}")

        if results["errors"]:
            click.echo("      Warnings:", err=True)
            for error in results["errors"]:
                click.echo(f"        - {error}", err=True)

        click.echo()
        click.echo("[2/2] Starting services...")
        # Show what's being started
        click.echo(f"      Starting: {', '.join(DockerManager.CORE_SERVICES)}")

        click.echo()
        click.echo("Reset complete. Run 'syrvis status' to verify.")

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to reset: {e}", err=True)
        raise click.Abort()


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


@cli.group()
def compose():
    """Manage docker-compose configuration."""
    pass


@compose.command()
@click.option(
    "--config",
    "-c",
    default="build/config.yaml",
    help="Path to build configuration file (uses defaults if missing)",
    type=click.Path(),
)
@click.option(
    "--output",
    "-o",
    default="docker-compose.yaml",
    help="Path for output docker-compose file",
    type=click.Path(),
)
def generate(config, output):
    """Generate docker-compose.yaml and Traefik configuration files."""
    from pathlib import Path

    try:
        # Load .env from SYRVIS_HOME/config/.env
        env_path = get_env_path()
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            click.echo(f"Warning: No .env file found at {env_path}", err=True)
            click.echo("Run 'syrvis setup' to configure first.", err=True)
            raise click.Abort()

        if Path(config).exists():
            click.echo(f"Reading build config from: {config}")
        else:
            click.echo("Using default Docker image versions")
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

        # Also regenerate Traefik configuration files
        click.echo()
        click.echo("Regenerating Traefik configuration...")
        syrvis_home = get_syrvis_home()
        traefik_data = syrvis_home / "data" / "traefik"
        traefik_data.mkdir(parents=True, exist_ok=True)
        config_dir = traefik_data / "config"
        config_dir.mkdir(exist_ok=True)

        static_config_path = traefik_data / "traefik.yml"
        static_config_path.write_text(generate_traefik_static_config())
        static_config_path.chmod(0o644)
        click.echo(f"  Generated: {static_config_path}")

        dynamic_config_path = config_dir / "dynamic.yml"
        dynamic_config_path.write_text(generate_traefik_dynamic_config())
        dynamic_config_path.chmod(0o644)
        click.echo(f"  Generated: {dynamic_config_path}")

        click.echo()
        click.echo("Run 'syrvis start' to start services.")

    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to generate compose file: {e}", err=True)
        raise click.Abort()


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

        versioned = None
        try:
            versioned = p.get_config_path()
        except Exception:
            versioned = None
        config_path = str(versioned) if versioned and versioned.exists() else "build/config.yaml"

        out = str(p.get_docker_compose_path())
        compose = generate_compose_from_config(config_path=config_path, output_path=out)

        # Keep Traefik static/dynamic config in sync too.
        traefik_data = p.get_syrvis_home() / "data" / "traefik"
        (traefik_data / "config").mkdir(parents=True, exist_ok=True)
        (traefik_data / "traefik.yml").write_text(generate_traefik_static_config())
        (traefik_data / "config" / "dynamic.yml").write_text(generate_traefik_dynamic_config())

        names = ", ".join(sorted(compose["services"].keys()))
        return True, "Regenerated {} ({} services: {})".format(out, len(compose["services"]), names)
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
def stack_list(as_json):
    """Show declared core services and whether they're running."""
    import json as jsonlib

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
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()

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
    for r in rows:
        mark = "●" if r["enabled"] else "○"
        tag = " [primordial]" if r["primordial"] else ""
        state = "enabled" if r["enabled"] else "disabled"
        click.echo("  {} {:<16} {:<9} {}{}".format(mark, r["service"], state, r["running"], tag))
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
def stack_enable(name, subdomain, exposure, do_apply):
    """Declare a core service enabled."""
    from syrviscore import stack as stack_mod

    settings = {}
    if subdomain:
        settings["subdomain"] = subdomain
    if exposure:
        settings["exposure"] = exposure
    try:
        stack_mod.set_enabled(name, True, settings or None)
    except stack_mod.StackError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    click.echo("Enabled '{}' in the stack.".format(name))
    _post_stack_change(do_apply)


@stack.command("disable")
@click.argument("name")
@click.option("--apply", "do_apply", is_flag=True, help="Regenerate compose immediately")
def stack_disable(name, do_apply):
    """Declare a core service disabled (primordial services cannot be disabled)."""
    from syrviscore import stack as stack_mod

    try:
        stack_mod.set_enabled(name, False)
    except stack_mod.StackError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    click.echo("Disabled '{}' in the stack.".format(name))
    _post_stack_change(do_apply)


@stack.command("apply")
def stack_apply():
    """Regenerate docker-compose.yaml from the declared stack."""
    ok, msg = _regenerate_compose()
    if not ok:
        click.echo(f"Error: {msg}", err=True)
        raise click.Abort()
    click.echo(msg)
    click.echo("Run 'syrvis start' to bring the stack up (or 'syrvis restart').")


@stack.command("hostnames")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option(
    "--exposure",
    type=click.Choice(["internal", "tunnel"]),
    default=None,
    help="Only show hosts with this exposure",
)
def stack_hostnames(as_json, exposure):
    """Report the external DNS / tunnel state this instance needs.

    Every hostname SyrvisCore routes, its exposure, and the record a deployment
    must create: a LAN DNS A record for 'internal', a Cloudflare Tunnel route +
    Access policy for 'tunnel'. This is the seam home-tech reconciles against.
    """
    import json as jsonlib

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
def core_start():
    """Start core services."""
    privilege.ensure_elevated("Starting services requires elevated privileges.")
    try:
        click.echo("Starting core services...")
        manager = DockerManager()
        manager.start_core_services()
        click.echo("Start initiated for core services")
        click.echo("Run 'syrvis status' to verify")
    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to start services: {e}", err=True)
        raise click.Abort()


@core.command("stop")
def core_stop():
    """Stop core services."""
    privilege.ensure_elevated("Stopping services requires elevated privileges.")
    try:
        click.echo("Stopping core services...")
        manager = DockerManager()
        manager.stop_core_services()
        click.echo("Stop initiated for core services")
    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to stop services: {e}", err=True)
        raise click.Abort()


@core.command("restart")
def core_restart():
    """Restart core services."""
    privilege.ensure_elevated("Restarting services requires elevated privileges.")
    try:
        click.echo("Restarting core services...")
        manager = DockerManager()
        manager.restart_core_services()
        click.echo("Restart initiated for core services")
        click.echo("Run 'syrvis status' to verify")
    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to restart services: {e}", err=True)
        raise click.Abort()


@core.command("status")
def core_status():
    """Show status of core services."""
    try:
        manager = DockerManager()
        statuses = manager.get_container_status()

        if not statuses:
            click.echo("No core services found")
            click.echo("Run 'syrvis start' to start services")
            return

        click.echo()
        click.echo("Core Services Status:")
        click.echo()
        click.echo(f"{'Service':<15} {'Status':<12} {'Uptime':<20} {'Image'}")
        click.echo("-" * 80)

        for service_name, info in statuses.items():
            status_icon = "[+]" if info["status"] == "running" else "[-]"
            click.echo(
                f"{status_icon} {service_name:<12} {info['status']:<12} "
                f"{info['uptime']:<20} {info['image']}"
            )

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to get status: {e}", err=True)
        raise click.Abort()


@core.command("logs")
@click.argument("service", required=False)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--tail", "-n", default=100, help="Number of lines to show from end")
def core_logs(service, follow, tail):
    """View logs from core services."""
    try:
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

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to get logs: {e}", err=True)
        raise click.Abort()


# =============================================================================
# Config command group
# =============================================================================


@cli.group()
def config():
    """Manage configuration files."""
    pass


@config.command()
def generate_traefik():
    """Generate Traefik configuration files."""
    try:
        load_dotenv()

        import os

        domain = os.getenv("DOMAIN")
        if not domain:
            click.echo("Warning: DOMAIN environment variable not set", err=True)
            click.echo("  Using default: example.com", err=True)
            click.echo("  Set DOMAIN in .env file for production use", err=True)
            click.echo()

        syrvis_home = get_syrvis_home()
        traefik_data = syrvis_home / "data" / "traefik"

        traefik_data.mkdir(parents=True, exist_ok=True)
        config_dir = traefik_data / "config"
        config_dir.mkdir(exist_ok=True)

        static_config_path = traefik_data / "traefik.yml"
        static_config = generate_traefik_static_config()
        static_config_path.write_text(static_config)
        static_config_path.chmod(0o644)
        click.echo(f"Generated static config: {static_config_path}")

        dynamic_config_path = config_dir / "dynamic.yml"
        dynamic_config = generate_traefik_dynamic_config()
        dynamic_config_path.write_text(dynamic_config)
        dynamic_config_path.chmod(0o644)
        click.echo(f"Generated dynamic config: {dynamic_config_path}")

        click.echo()
        click.echo("Configuration files created successfully!")

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to generate config: {e}", err=True)
        raise click.Abort()


@config.command()
def show():
    """Show current configuration."""
    try:
        from .config_reader import read_config

        cfg = read_config()

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

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
