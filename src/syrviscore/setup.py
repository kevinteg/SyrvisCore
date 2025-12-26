"""Setup command for SyrvisCore - handles privileged operations and configuration."""

import click
import sys
from pathlib import Path
from datetime import datetime

from . import privileged_ops
from . import paths


@click.command()
@click.option('--interactive', '-i', is_flag=True, help='Interactive configuration mode')
@click.option('--domain', help='Domain name (e.g., example.com)')
@click.option('--email', help='Email for Let\'s Encrypt certificates')
@click.option('--subnet', help='Network subnet in CIDR notation (e.g., 192.168.1.0/24)')
@click.option('--gateway', help='Gateway IP address (e.g., 192.168.1.1)')
@click.option('--traefik-ip', help='Traefik IP address (e.g., 192.168.1.4)')
@click.option('--interface', default='ovs_eth0', help='Network interface (default: ovs_eth0)')
@click.option('--cloudflare-token', default='', help='Cloudflare tunnel token (optional)')
def setup(interactive, domain, email, subnet, gateway, traefik_ip, interface, cloudflare_token):
    """Complete SyrvisCore setup with privileged operations and configuration."""
    
    click.echo("=" * 70)
    click.echo("SyrvisCore Setup")
    click.echo("=" * 70)
    click.echo()
    
    # Verify running as root
    try:
        privileged_ops.verify_root()
    except privileged_ops.PrivilegedOpsError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    
    # Get target user
    try:
        username = privileged_ops.get_target_user()
        click.echo(f"Target user: {username}")
    except privileged_ops.PrivilegedOpsError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    
    # Find installation directory
    try:
        install_dir = paths.get_syrvis_home()
        click.echo(f"Installation: {install_dir}")
    except paths.SyrvisHomeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    
    # Check if already setup
    try:
        manifest = paths.get_manifest()
        if manifest.get('setup_complete', False):
            if not click.confirm("\nSetup already completed. Re-run setup?"):
                click.echo("Setup cancelled.")
                sys.exit(0)
    except FileNotFoundError:
        click.echo("Warning: Manifest file not found", err=True)
    
    click.echo()
    
    # Interactive mode: prompt for values
    if interactive:
        click.echo("Interactive Configuration")
        click.echo("-" * 70)
        domain = click.prompt("Domain name", default=domain or "example.com")
        email = click.prompt("Email for Let's Encrypt", default=email or f"admin@{domain}")
        interface = click.prompt("Network interface", default=interface)
        subnet = click.prompt("Network subnet (CIDR)", default=subnet or "192.168.0.0/24")
        gateway = click.prompt("Gateway IP", default=gateway or "192.168.0.1")
        traefik_ip = click.prompt("Traefik IP", default=traefik_ip or "192.168.0.4")
        cloudflare_token = click.prompt("Cloudflare token (optional)", default=cloudflare_token or "", show_default=False)
        click.echo()
    
    # Validate required parameters
    if not all([domain, email, subnet, gateway, traefik_ip]):
        click.echo("Error: Missing required parameters", err=True)
        click.echo("\nProvide all parameters via command line or use --interactive", err=True)
        sys.exit(1)
    
    # Display configuration
    click.echo("Configuration Summary")
    click.echo("-" * 70)
    click.echo(f"Domain:         {domain}")
    click.echo(f"Email:          {email}")
    click.echo(f"Interface:      {interface}")
    click.echo(f"Subnet:         {subnet}")
    click.echo(f"Gateway:        {gateway}")
    click.echo(f"Traefik IP:     {traefik_ip}")
    click.echo(f"Cloudflare:     {'(configured)' if cloudflare_token else '(not configured)'}")
    click.echo()
    
    if not click.confirm("Proceed with setup?"):
        click.echo("Setup cancelled.")
        sys.exit(0)
    
    click.echo()
    click.echo("Performing Setup")
    click.echo("-" * 70)
    
    # Step 1: Verify Docker is installed
    click.echo("[1/7] Checking Docker installation...")
    success, msg = privileged_ops.verify_docker_installed()
    if success:
        click.echo(f"  ✓ {msg}")
    else:
        click.echo(f"  ✗ {msg}", err=True)
        sys.exit(1)
    
    # Step 2: Ensure docker group exists
    click.echo("[2/7] Ensuring docker group exists...")
    success, msg = privileged_ops.ensure_docker_group()
    if success:
        click.echo(f"  ✓ {msg}")
    else:
        click.echo(f"  ✗ {msg}", err=True)
        sys.exit(1)
    
    # Step 3: Add user to docker group
    click.echo(f"[3/7] Adding {username} to docker group...")
    success, msg = privileged_ops.ensure_user_in_docker_group(username)
    if success:
        click.echo(f"  ✓ {msg}")
        if "logout required" in msg.lower():
            click.echo("  ⚠ You must logout and login for group changes to take effect")
    else:
        click.echo(f"  ✗ {msg}", err=True)
        sys.exit(1)
    
    # Step 4: Set Docker socket permissions
    click.echo("[4/7] Setting Docker socket permissions...")
    success, msg = privileged_ops.ensure_docker_socket_permissions()
    if success:
        click.echo(f"  ✓ {msg}")
    else:
        click.echo(f"  ✗ {msg}", err=True)
        sys.exit(1)
    
    # Step 5: Create global symlink
    click.echo("[5/7] Creating global command symlink...")
    success, msg = privileged_ops.ensure_global_symlink(install_dir)
    if success:
        click.echo(f"  ✓ {msg}")
    else:
        click.echo(f"  ⚠ {msg}")
    
    # Step 6: Create startup script
    click.echo("[6/7] Creating startup script...")
    success, msg = privileged_ops.ensure_startup_script(install_dir, username)
    if success:
        click.echo(f"  ✓ {msg}")
        click.echo("  → Add to Task Scheduler for boot persistence")
    else:
        click.echo(f"  ⚠ {msg}")
    
    # Step 7: Generate .env file
    click.echo("[7/7] Generating configuration file...")
    try:
        env_path = paths.get_env_path()
        tz = _get_timezone()
        
        env_content = f"""# SyrvisCore Configuration
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

# Installation
SYRVIS_HOME={install_dir}
SYRVIS_DATA_DIR={install_dir}/data

# Network (macvlan)
NETWORK_INTERFACE={interface}
NETWORK_SUBNET={subnet}
GATEWAY_IP={gateway}
TRAEFIK_IP={traefik_ip}

# Domain & SSL
DOMAIN={domain}
ACME_EMAIL={email}

# Cloudflare Tunnel (optional)
CLOUDFLARE_TUNNEL_TOKEN={cloudflare_token}

# Traefik
TRAEFIK_LOG_LEVEL=INFO
TRAEFIK_API_DASHBOARD=true

# Portainer
PORTAINER_BIND_PORT=9443

# System
TZ={tz}
"""
        env_path.write_text(env_content)
        # Set permissions to 644 (owner RW, group R, others R) so user can read it
        env_path.chmod(0o644)
        # Change ownership to target user (run as root, but file should be owned by user)
        import pwd
        try:
            user_info = pwd.getpwnam(username)
            import os
            os.chown(env_path, user_info.pw_uid, user_info.pw_gid)
        except Exception as e:
            click.echo(f"  ⚠ Could not change ownership: {e}")
        click.echo(f"  ✓ Configuration saved: {env_path}")
    except Exception as e:
        click.echo(f"  ✗ Failed to create .env file: {e}", err=True)
        sys.exit(1)
    
    # Update manifest
    try:
        paths.update_manifest({
            'setup_complete': True,
            'setup_date': datetime.now().isoformat(),
            'setup_user': username,
            'domain': domain,
        })
        click.echo("  ✓ Manifest updated")
    except Exception as e:
        click.echo(f"  ⚠ Failed to update manifest: {e}")
    
    click.echo()
    click.echo("=" * 70)
    click.echo("Setup Complete!")
    click.echo("=" * 70)
    click.echo()
    click.echo("Next Steps:")
    click.echo(f"  1. Logout and login as {username} (for group changes)")
    click.echo(f"  2. Verify installation: syrvis doctor")
    click.echo(f"  3. Generate compose file: syrvis compose generate")
    click.echo(f"  4. Start services: syrvis start")
    click.echo()
    click.echo("For boot persistence, add startup script to Task Scheduler:")
    click.echo(f"  Script: {install_dir}/bin/syrvis-startup.sh")
    click.echo(f"  User: root")
    click.echo(f"  Schedule: Run on boot")
    click.echo()


def _get_timezone() -> str:
    """Get system timezone from /etc/TZ or default to UTC."""
    try:
        return Path('/etc/TZ').read_text().strip()
    except:
        return 'UTC'
