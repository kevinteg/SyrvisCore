"""SyrvisCore CLI - Main entry point."""

import click
from dotenv import load_dotenv

from syrviscore.__version__ import __version__
from syrviscore.compose import generate_compose_from_config
from syrviscore.docker_manager import DockerConnectionError, DockerError, DockerManager
from syrviscore.paths import SyrvisHomeError, get_syrvis_home, get_active_version
from syrviscore.traefik_config import (
    generate_traefik_dynamic_config,
    generate_traefik_static_config,
)
from syrviscore.setup import setup
from syrviscore.doctor import doctor
from syrviscore.update import update


@click.group()
@click.version_option(version=__version__, prog_name="syrvis")
def cli():
    """SyrvisCore - Self-hosted infrastructure platform for Synology NAS."""
    pass


# Register command groups
cli.add_command(setup)
cli.add_command(doctor)
cli.add_command(update)


# =============================================================================
# Top-level convenience commands
# =============================================================================

@cli.command()
def status():
    """Show status of all services (alias for 'core status')."""
    try:
        manager = DockerManager()
        statuses = manager.get_container_status()

        if not statuses:
            click.echo("No services found")
            click.echo("Run 'syrvis setup' to complete installation")
            return

        click.echo()
        click.echo("SyrvisCore Status")
        click.echo("=" * 60)

        # Show version info
        active = get_active_version()
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

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
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
    help="Path to build configuration file",
    type=click.Path(exists=True),
)
@click.option(
    "--output",
    "-o",
    default="docker-compose.yaml",
    help="Path for output docker-compose file",
    type=click.Path(),
)
def generate(config, output):
    """Generate docker-compose.yaml from build configuration."""
    try:
        load_dotenv()

        click.echo(f"Reading build config from: {config}")
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

        click.echo()
        click.echo("Run 'syrvis start' to start services.")

    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"Failed to generate compose file: {e}", err=True)
        raise click.Abort()


# =============================================================================
# Core command group (kept for backwards compatibility)
# =============================================================================

@cli.group()
def core():
    """Manage core services (Traefik, Portainer, Cloudflared)."""
    pass


@core.command('start')
def core_start():
    """Start core services."""
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


@core.command('stop')
def core_stop():
    """Stop core services."""
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


@core.command('restart')
def core_restart():
    """Restart core services."""
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


@core.command('status')
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


@core.command('logs')
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
        from . import paths as p

        syrvis_home = p.get_syrvis_home()
        env_path = p.get_env_path()

        click.echo()
        click.echo("SyrvisCore Configuration")
        click.echo("=" * 60)
        click.echo()
        click.echo(f"Install path:  {syrvis_home}")
        click.echo(f"Active version: {p.get_active_version() or 'unknown'}")
        click.echo()

        if env_path.exists():
            click.echo(f"Configuration ({env_path}):")
            click.echo("-" * 60)
            for line in env_path.read_text().splitlines():
                if line and not line.startswith('#'):
                    # Mask sensitive values
                    if 'TOKEN' in line or 'SECRET' in line or 'PASSWORD' in line:
                        key = line.split('=')[0]
                        click.echo(f"  {key}=****")
                    else:
                        click.echo(f"  {line}")
        else:
            click.echo("No .env file found")
            click.echo("Run 'syrvis setup' to create configuration")

    except SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
