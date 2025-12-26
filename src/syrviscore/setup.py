"""
Setup command for SyrvisCore - handles interactive configuration and privileged operations.

This command is the primary entry point for completing SyrvisCore installation.
It handles:
- Self-elevation to root when needed
- Interactive configuration prompts
- Privileged system setup (docker group, socket permissions, etc.)
- Configuration file generation
- Service startup
"""

import click
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from . import privileged_ops
from . import paths
from .__version__ import __version__


# Get the system operations provider once at module load
def _get_ops() -> privileged_ops.SystemOperations:
    """Get the system operations provider."""
    return privileged_ops.get_system_operations()


def self_elevate() -> None:
    """Re-execute this command with sudo."""
    click.echo("\nSome operations require root privileges.")

    # Build the command to re-execute
    python_path = sys.executable
    script_args = sys.argv

    # Use sudo to re-execute
    os.execvp("sudo", ["sudo", python_path] + script_args)


def get_default_network_settings() -> dict:
    """Attempt to detect network settings."""
    import subprocess

    defaults = {
        "interface": "eth0",
        "subnet": "192.168.1.0/24",
        "gateway": "192.168.1.1",
        "traefik_ip": "192.168.1.100",
    }

    # Try to detect interface
    try:
        # On Synology, ovs_eth0 is common for Open vSwitch
        if Path("/sys/class/net/ovs_eth0").exists():
            defaults["interface"] = "ovs_eth0"
        elif Path("/sys/class/net/eth0").exists():
            defaults["interface"] = "eth0"

        # Try to get gateway from route
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and "via" in result.stdout:
            parts = result.stdout.split()
            if "via" in parts:
                idx = parts.index("via")
                if idx + 1 < len(parts):
                    gateway = parts[idx + 1]
                    defaults["gateway"] = gateway
                    # Derive subnet from gateway
                    prefix = ".".join(gateway.split(".")[:3])
                    defaults["subnet"] = f"{prefix}.0/24"
                    # Suggest traefik IP in same subnet
                    defaults["traefik_ip"] = f"{prefix}.100"
    except Exception:
        pass

    return defaults


def get_timezone() -> str:
    """Get system timezone from /etc/TZ or default to UTC."""
    try:
        return Path('/etc/TZ').read_text().strip()
    except Exception:
        return 'UTC'


def prompt_configuration(defaults: dict) -> dict:
    """Prompt user for configuration values."""
    click.echo()
    click.echo("Configuration")
    click.echo("-" * 60)
    click.echo()

    config = {}

    # Domain and email
    config["domain"] = click.prompt(
        "  Domain name",
        default="example.com"
    )
    config["email"] = click.prompt(
        "  Email for Let's Encrypt",
        default=f"admin@{config['domain']}"
    )

    click.echo()
    click.echo("  Network Configuration (for macvlan):")

    config["interface"] = click.prompt(
        "    Network interface",
        default=defaults["interface"]
    )
    config["subnet"] = click.prompt(
        "    Network subnet (CIDR)",
        default=defaults["subnet"]
    )
    config["gateway"] = click.prompt(
        "    Gateway IP",
        default=defaults["gateway"]
    )
    config["traefik_ip"] = click.prompt(
        "    Traefik IP (dedicated macvlan IP)",
        default=defaults["traefik_ip"]
    )

    click.echo()
    enable_cloudflare = click.confirm(
        "  Enable Cloudflare Tunnel?",
        default=False
    )
    if enable_cloudflare:
        config["cloudflare_token"] = click.prompt(
            "    Cloudflare Tunnel token",
            default="",
            hide_input=True
        )
    else:
        config["cloudflare_token"] = ""

    return config


def display_configuration(config: dict) -> None:
    """Display configuration summary."""
    click.echo()
    click.echo("Configuration Summary")
    click.echo("-" * 60)
    click.echo(f"  Domain:       {config['domain']}")
    click.echo(f"  Email:        {config['email']}")
    click.echo(f"  Interface:    {config['interface']}")
    click.echo(f"  Subnet:       {config['subnet']}")
    click.echo(f"  Gateway:      {config['gateway']}")
    click.echo(f"  Traefik IP:   {config['traefik_ip']}")
    click.echo(f"  Cloudflare:   {'configured' if config.get('cloudflare_token') else 'not configured'}")
    click.echo()


def perform_privileged_setup(username: str, install_dir: Path) -> bool:
    """Perform privileged system setup operations."""
    click.echo()
    click.echo("System Setup (privileged operations)")
    click.echo("-" * 60)

    success = True

    # Step 1: Verify Docker
    click.echo("[1/6] Checking Docker installation...")
    ok, msg = privileged_ops.verify_docker_installed()
    if ok:
        click.echo(f"      {msg}")
    else:
        click.echo(f"      {msg}", err=True)
        return False

    # Step 2: Docker group
    click.echo("[2/6] Ensuring docker group exists...")
    ok, msg = privileged_ops.ensure_docker_group()
    if ok:
        click.echo(f"      {msg}")
    else:
        click.echo(f"      {msg}", err=True)
        return False

    # Step 3: Add user to docker group
    click.echo(f"[3/6] Adding {username} to docker group...")
    ok, msg = privileged_ops.ensure_user_in_docker_group(username)
    if ok:
        click.echo(f"      {msg}")
    else:
        click.echo(f"      {msg}", err=True)
        return False

    # Step 4: Docker socket permissions
    click.echo("[4/6] Setting Docker socket permissions...")
    ok, msg = privileged_ops.ensure_docker_socket_permissions()
    if ok:
        click.echo(f"      {msg}")
    else:
        click.echo(f"      {msg}", err=True)
        # Non-fatal, continue

    # Step 5: Global symlink
    click.echo("[5/6] Creating global command symlink...")
    ok, msg = privileged_ops.ensure_global_symlink(install_dir)
    if ok:
        click.echo(f"      {msg}")
    else:
        click.echo(f"      Warning: {msg}")
        # Non-fatal, continue

    # Step 6: Startup script
    click.echo("[6/6] Creating startup script...")
    ok, msg = privileged_ops.ensure_startup_script(install_dir, username)
    if ok:
        click.echo(f"      {msg}")
    else:
        click.echo(f"      Warning: {msg}")
        # Non-fatal, continue

    return success


def generate_env_file(config: dict, install_dir: Path, username: str) -> Path:
    """Generate .env configuration file."""
    env_path = paths.get_env_path()

    # Ensure config directory exists
    env_path.parent.mkdir(parents=True, exist_ok=True)

    tz = get_timezone()

    env_content = f"""# SyrvisCore Configuration
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Version: {__version__}

# Installation
SYRVIS_HOME={install_dir}
SYRVIS_DATA_DIR={install_dir}/data

# Network (macvlan)
NETWORK_INTERFACE={config['interface']}
NETWORK_SUBNET={config['subnet']}
NETWORK_GATEWAY={config['gateway']}
TRAEFIK_IP={config['traefik_ip']}

# Domain & SSL
DOMAIN={config['domain']}
ACME_EMAIL={config['email']}

# Cloudflare Tunnel (optional)
CLOUDFLARE_TUNNEL_TOKEN={config.get('cloudflare_token', '')}

# Traefik
TRAEFIK_LOG_LEVEL=INFO
TRAEFIK_API_DASHBOARD=true

# Portainer
PORTAINER_BIND_PORT=9443

# System
TZ={tz}
"""

    env_path.write_text(env_content)
    env_path.chmod(0o644)

    # Change ownership to target user if running as root
    if os.getuid() == 0:
        try:
            import pwd
            user_info = pwd.getpwnam(username)
            os.chown(env_path, user_info.pw_uid, user_info.pw_gid)
        except Exception:
            pass

    return env_path


def generate_docker_compose(install_dir: Path) -> bool:
    """Generate docker-compose.yaml from configuration."""
    try:
        from dotenv import load_dotenv
        from .compose import generate_compose_from_config

        # Load the .env file we just created (override=True to ensure values are used)
        load_dotenv(paths.get_env_path(), override=True)

        config_yaml = paths.get_version_config_yaml()
        output_path = paths.get_docker_compose_path()

        if not config_yaml.exists():
            click.echo(f"      Warning: {config_yaml} not found, skipping compose generation")
            return False

        generate_compose_from_config(
            config_path=str(config_yaml),
            output_path=str(output_path)
        )
        return True
    except Exception as e:
        click.echo(f"      Warning: Failed to generate docker-compose.yaml: {e}")
        return False


def ensure_data_directories() -> None:
    """Ensure all data directories exist with proper permissions."""
    data_dir = paths.get_data_dir()

    # Create all required data directories with 755 permissions
    directories = [
        data_dir,
        data_dir / "traefik",
        data_dir / "traefik" / "config",
        data_dir / "traefik" / "logs",
        data_dir / "portainer",
        data_dir / "cloudflared",
    ]

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)


def generate_traefik_config() -> bool:
    """Generate Traefik configuration files."""
    try:
        from dotenv import load_dotenv
        from .traefik_config import generate_traefik_static_config, generate_traefik_dynamic_config

        load_dotenv(paths.get_env_path(), override=True)

        # Static config goes in data directory (mounted by container)
        traefik_data = paths.get_traefik_data_dir()
        traefik_data.mkdir(parents=True, exist_ok=True)
        traefik_data.chmod(0o755)

        static_path = traefik_data / "traefik.yml"
        static_path.write_text(generate_traefik_static_config())
        static_path.chmod(0o644)

        # Dynamic config
        config_dir = traefik_data / "config"
        config_dir.mkdir(exist_ok=True)
        config_dir.chmod(0o755)
        dynamic_path = config_dir / "dynamic.yml"
        dynamic_path.write_text(generate_traefik_dynamic_config())
        dynamic_path.chmod(0o644)

        # Logs directory
        logs_dir = traefik_data / "logs"
        logs_dir.mkdir(exist_ok=True)
        logs_dir.chmod(0o755)

        # Create empty acme.json with proper permissions
        acme_path = traefik_data / "acme.json"
        if not acme_path.exists():
            acme_path.touch()
            acme_path.chmod(0o600)

        return True
    except Exception as e:
        click.echo(f"      Warning: Failed to generate Traefik config: {e}")
        return False


def start_services() -> bool:
    """Start Docker services."""
    try:
        from .docker_manager import DockerManager

        manager = DockerManager()
        manager.start_core_services()
        return True
    except Exception as e:
        click.echo(f"      Warning: Failed to start services: {e}")
        return False


def get_service_status() -> dict:
    """Get status of running services."""
    try:
        from .docker_manager import DockerManager
        manager = DockerManager()
        return manager.get_container_status()
    except Exception:
        return {}


@click.command()
@click.option('--non-interactive', is_flag=True, help='Use defaults without prompting')
@click.option('--skip-start', is_flag=True, help='Skip starting services after setup')
@click.option('--domain', help='Domain name (e.g., example.com)')
@click.option('--email', help='Email for Let\'s Encrypt certificates')
@click.option('--traefik-ip', help='Traefik IP address (e.g., 192.168.1.100)')
def setup(non_interactive, skip_start, domain, email, traefik_ip):
    """
    Complete SyrvisCore setup.

    This command handles all setup tasks including:
    - Privilege elevation (automatically runs with sudo if needed)
    - Interactive configuration prompts
    - Docker group and socket setup
    - Configuration file generation
    - Service startup
    """

    # Get the system operations provider
    ops = _get_ops()

    click.echo()
    click.echo("=" * 60)
    click.echo("SyrvisCore Setup")
    click.echo("=" * 60)
    click.echo(f"Version: {__version__}")
    click.echo(f"Mode: {ops.mode_name}")

    # Step 1: Check prerequisites
    click.echo()
    click.echo("[1/7] Checking prerequisites...")

    # Check Python version
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    click.echo(f"      Python {py_version}")

    # Check if we need to elevate
    needs_elevation = ops.needs_privilege_elevation()
    if needs_elevation:
        click.echo("      Privileges: user (elevation needed)")
    else:
        click.echo("      Privileges: root" if not ops.is_simulation else "      Privileges: user (simulation)")

    # Step 2: Handle privilege elevation
    click.echo()
    click.echo("[2/7] Privilege check...")

    if needs_elevation:
        if non_interactive:
            click.echo("      Error: Cannot elevate in non-interactive mode", err=True)
            click.echo("      Run with: sudo syrvis setup --non-interactive", err=True)
            sys.exit(1)

        if click.confirm("      Run with sudo to continue?", default=True):
            self_elevate()
            # If we get here, exec failed
            click.echo("      Error: Failed to elevate privileges", err=True)
            sys.exit(1)
        else:
            click.echo("      Setup cancelled.")
            sys.exit(0)

    # Get target user
    try:
        username = ops.get_target_user()
        click.echo(f"      Target user: {username}")
    except privileged_ops.PrivilegedOpsError as e:
        click.echo(f"      Error: {e}", err=True)
        sys.exit(1)

    # Step 3: Find or detect installation
    click.echo()
    click.echo("[3/7] Detecting installation...")

    try:
        install_dir = paths.get_syrvis_home()
        click.echo(f"      Found: {install_dir}")
    except paths.SyrvisHomeError:
        # No existing installation - use default
        install_dir = Path("/volume1/docker/syrviscore")
        click.echo(f"      New installation: {install_dir}")

    # Check if already setup
    try:
        manifest = paths.get_manifest()
        if manifest.get('setup_complete', False):
            click.echo("      Status: Setup already completed")
            if not click.confirm("      Re-run setup?", default=False):
                click.echo("      Setup cancelled.")
                sys.exit(0)
    except FileNotFoundError:
        click.echo("      Status: Fresh installation")

    # Step 4: Interactive configuration
    click.echo()
    click.echo("[4/7] Configuration...")

    network_defaults = get_default_network_settings()

    if non_interactive:
        # Use provided options or defaults
        config = {
            "domain": domain or "example.com",
            "email": email or f"admin@{domain or 'example.com'}",
            "interface": network_defaults["interface"],
            "subnet": network_defaults["subnet"],
            "gateway": network_defaults["gateway"],
            "traefik_ip": traefik_ip or network_defaults["traefik_ip"],
            "cloudflare_token": "",
        }
        display_configuration(config)
    else:
        config = prompt_configuration(network_defaults)
        display_configuration(config)

        if not click.confirm("      Proceed with this configuration?", default=True):
            click.echo("      Setup cancelled.")
            sys.exit(0)

    # Step 5: Perform privileged setup
    click.echo()
    click.echo("[5/7] System setup...")

    if not perform_privileged_setup(username, install_dir):
        click.echo()
        click.echo("Setup failed. Please check the errors above.", err=True)
        sys.exit(1)

    # Step 6: Generate configuration files
    click.echo()
    click.echo("[6/7] Generating configuration files...")

    # Ensure data directories exist with proper permissions
    ensure_data_directories()

    env_path = generate_env_file(config, install_dir, username)
    click.echo(f"      Created: {env_path}")

    if generate_traefik_config():
        click.echo(f"      Created: {paths.get_traefik_data_dir()}/traefik.yml")
        click.echo(f"      Created: {paths.get_traefik_data_dir()}/config/dynamic.yml")

    if generate_docker_compose(install_dir):
        click.echo(f"      Created: {paths.get_docker_compose_path()}")

    # Update manifest
    try:
        paths.update_manifest({
            'setup_complete': True,
            'setup_completed_at': datetime.now().isoformat(),
            'setup_user': username,
            'config': {
                'domain': config['domain'],
                'traefik_ip': config['traefik_ip'],
            },
            'privileged_setup': {
                'docker_group_created': True,
                'user_added_to_docker': username,
                'global_symlink': '/usr/local/bin/syrvis',
                'startup_script': f'{install_dir}/bin/syrvis-startup.sh',
            }
        })
        click.echo("      Updated: manifest")
    except Exception as e:
        click.echo(f"      Warning: Failed to update manifest: {e}")

    # Step 7: Start services
    click.echo()
    click.echo("[7/7] Starting services...")

    if skip_start:
        click.echo("      Skipped (--skip-start)")
    else:
        if start_services():
            click.echo("      Services started")

            # Show status
            statuses = get_service_status()
            if statuses:
                click.echo()
                for name, info in statuses.items():
                    status_icon = "+" if info.get("status") == "running" else "-"
                    click.echo(f"      [{status_icon}] {name}: {info.get('status', 'unknown')}")
        else:
            click.echo("      Warning: Services may need manual start")
            click.echo("      Run: syrvis core start")

    # Success!
    click.echo()
    click.echo("=" * 60)
    click.echo("Setup Complete!")
    click.echo("=" * 60)
    click.echo()
    click.echo("Access your services:")
    click.echo(f"  Traefik:   https://traefik.{config['domain']}")
    click.echo(f"  Portainer: https://portainer.{config['domain']}")
    click.echo()
    click.echo("Useful commands:")
    click.echo("  syrvis status      - Check service status")
    click.echo("  syrvis logs        - View service logs")
    click.echo("  syrvis doctor      - Diagnose issues")
    click.echo()

    # Remind about logout if group was changed
    click.echo("Note: If this is your first setup, logout and login")
    click.echo(f"      for docker group membership to take effect.")
    click.echo()
