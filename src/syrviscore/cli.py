"""SyrvisCore CLI - Main entry point."""

import click
from syrviscore.__version__ import __version__
from syrviscore.compose import generate_compose_from_config
from syrviscore.docker_manager import DockerConnectionError, DockerManager
from syrviscore.paths import SyrvisHomeError


@click.group()
@click.version_option(version=__version__, prog_name="syrvis")
def cli():
    """SyrvisCore - Self-hosted infrastructure platform for Synology NAS."""
    pass


@cli.command()
def hello():
    """Hello World - Test command to verify installation."""
    click.echo("🎉 Hello from SyrvisCore!")
    click.echo(f"Version: {__version__}")
    click.echo("✓ CLI is working correctly")


@cli.command()
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
def generate_compose(config, output):
    """Generate docker-compose.yaml from build configuration."""
    try:
        click.echo(f"📦 Reading build config from: {config}")
        compose = generate_compose_from_config(config_path=config, output_path=output)

        click.echo(f"✅ Generated docker-compose.yaml at: {output}")
        click.echo("\n📊 Services configured:")
        for service_name in compose["services"].keys():
            service = compose["services"][service_name]
            click.echo(f"  • {service_name:<15} {service['image']}")

        click.echo("\n⚠️  Note: Traefik runs on ports 8080/8443 (not 80/443)")
        click.echo("   Configure DSM Application Portal to forward 80/443 -> 8080/8443")
        click.echo("\n✨ Done! Run 'docker-compose up -d' to start services.")
    except FileNotFoundError as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Failed to generate compose file: {e}", err=True)
        raise click.Abort()


@cli.group()
def core():
    """Manage core services (Traefik, Portainer, Cloudflared)."""
    pass


@core.command()
def start():
    """Start core services."""
    try:
        click.echo("🚀 Starting core services...")
        manager = DockerManager()
        manager.start_core_services()
        click.echo("✅ Core services started successfully")
        click.echo("Run 'syrvis core status' to check service status")
    except SyrvisHomeError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except FileNotFoundError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Failed to start services: {e}", err=True)
        raise click.Abort()


@core.command()
def stop():
    """Stop core services."""
    try:
        click.echo("🛑 Stopping core services...")
        manager = DockerManager()
        manager.stop_core_services()
        click.echo("✅ Core services stopped successfully")
    except SyrvisHomeError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except FileNotFoundError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Failed to stop services: {e}", err=True)
        raise click.Abort()


@core.command()
def restart():
    """Restart core services."""
    try:
        click.echo("🔄 Restarting core services...")
        manager = DockerManager()
        manager.restart_core_services()
        click.echo("✅ Core services restarted successfully")
    except SyrvisHomeError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except FileNotFoundError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Failed to restart services: {e}", err=True)
        raise click.Abort()


@core.command()
def status():
    """Show status of core services."""
    try:
        manager = DockerManager()
        statuses = manager.get_container_status()

        if not statuses:
            click.echo("⚠️  No core services found")
            click.echo("Run 'syrvis core start' to start services")
            return

        click.echo("📊 Core Services Status:\n")
        click.echo(f"{'Service':<15} {'Status':<12} {'Uptime':<20} {'Image'}")
        click.echo("-" * 80)

        for service_name, info in statuses.items():
            status_icon = "✅" if info["status"] == "running" else "⚠️"
            click.echo(
                f"{status_icon} {service_name:<13} {info['status']:<12} "
                f"{info['uptime']:<20} {info['image']}"
            )

    except SyrvisHomeError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Failed to get status: {e}", err=True)
        raise click.Abort()


@core.command()
@click.argument("service", required=False)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--tail", "-n", default=100, help="Number of lines to show from end")
def logs(service, follow, tail):
    """View logs from core services."""
    try:
        manager = DockerManager()

        if follow:
            if service:
                click.echo(f"📜 Following logs for {service}... (Ctrl+C to stop)")
            else:
                click.echo("📜 Following logs for all services... (Ctrl+C to stop)")
            manager.get_container_logs(service=service, follow=True, tail=tail)
        else:
            log_output = manager.get_container_logs(service=service, follow=False, tail=tail)
            click.echo(log_output)

    except SyrvisHomeError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except DockerConnectionError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except ValueError as e:
        click.echo(f"❌ {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"❌ Failed to get logs: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
