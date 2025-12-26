"""Doctor command for SyrvisCore - diagnose and fix installation issues."""

import click
import sys
import os
from pathlib import Path

from . import privileged_ops
from . import paths


@click.command()
@click.option('--fix', is_flag=True, help='Attempt to fix issues (requires root)')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed information')
def doctor(fix, verbose):
    """Verify SyrvisCore installation and diagnose issues."""
    
    is_root = os.getuid() == 0
    
    click.echo("=" * 70)
    click.echo("SyrvisCore Installation Diagnostics")
    click.echo("=" * 70)
    click.echo()
    
    if fix and not is_root:
        click.echo("Error: --fix requires root privileges", err=True)
        click.echo("Run with: sudo syrvis doctor --fix")
        sys.exit(1)
    
    issues = []
    fixable_issues = []
    
    # Check 1: SYRVIS_HOME detection
    click.echo("Installation")
    click.echo("-" * 70)
    
    try:
        install_dir = paths.get_syrvis_home()
        click.echo(f"  ✓ SYRVIS_HOME: {install_dir}")
    except paths.SyrvisHomeError as e:
        click.echo(f"  ✗ SYRVIS_HOME: Not found")
        issues.append("Cannot locate installation directory")
        install_dir = None
    
    if install_dir:
        # Check 2: Manifest exists
        try:
            manifest = paths.get_manifest()
            click.echo(f"  ✓ Manifest exists")
            
            # Check 3: Setup completed
            if manifest.get('setup_complete', False):
                setup_date = manifest.get('setup_date', 'unknown')
                click.echo(f"  ✓ Setup completed ({setup_date})")
            else:
                click.echo(f"  ✗ Setup not completed")
                issues.append("Setup not completed - run: sudo syrvis setup --interactive")
                
            if verbose:
                click.echo(f"     Version: {manifest.get('version', 'unknown')}")
                click.echo(f"     Python: {manifest.get('python_version', 'unknown')}")
        except FileNotFoundError:
            click.echo(f"  ✗ Manifest missing")
            issues.append("Manifest file not found")
        except Exception as e:
            click.echo(f"  ✗ Manifest error: {e}")
            issues.append(f"Manifest error: {e}")
        
        # Check 4: Python venv
        venv_path = install_dir / "cli" / "venv"
        if venv_path.exists():
            click.echo(f"  ✓ Python venv exists")
        else:
            click.echo(f"  ✗ Python venv missing")
            issues.append("Python virtual environment not found")
    
    click.echo()
    
    # Check 5-9: Docker Access
    click.echo("Docker Access")
    click.echo("-" * 70)
    
    # Check 5: Docker group exists
    docker_group_exists, docker_gid = privileged_ops.get_docker_group_info()
    if docker_group_exists:
        click.echo(f"  ✓ Docker group exists (GID: {docker_gid})")
    else:
        click.echo(f"  ✗ Docker group missing")
        issues.append("Docker group does not exist")
        if fix:
            fixable_issues.append("docker_group")
    
    # Check 6: Current user in docker group
    username = os.environ.get('USER', os.environ.get('SUDO_USER', 'unknown'))
    if docker_group_exists and username != 'unknown':
        in_group = privileged_ops.is_user_in_group(username, 'docker')
        if in_group:
            click.echo(f"  ✓ User '{username}' in docker group")
        else:
            click.echo(f"  ✗ User '{username}' not in docker group")
            issues.append(f"User '{username}' needs to be added to docker group")
            if fix:
                fixable_issues.append(f"user_group:{username}")
    
    # Check 7: Docker socket exists
    socket_exists, socket_msg = privileged_ops.verify_docker_socket_exists()
    if socket_exists:
        click.echo(f"  ✓ Docker socket exists")
    else:
        click.echo(f"  ✗ Docker socket missing")
        issues.append("Docker socket not found - is Docker installed?")
    
    # Check 8: Docker socket permissions
    if socket_exists:
        owner, group, perms = privileged_ops.get_docker_socket_permissions()
        if group == 'docker' and perms == '660':
            click.echo(f"  ✓ Socket permissions correct ({owner}:{group} {perms})")
        else:
            click.echo(f"  ✗ Socket permissions incorrect ({owner}:{group} {perms})")
            click.echo(f"     Expected: root:docker 660")
            issues.append("Docker socket permissions need fixing")
            if fix:
                fixable_issues.append("socket_perms")
    
    # Check 9: Docker daemon accessible
    docker_ok, docker_msg = privileged_ops.verify_docker_accessible(username if not is_root else None)
    if docker_ok:
        click.echo(f"  ✓ Docker daemon accessible")
    else:
        click.echo(f"  ✗ Cannot connect to Docker daemon")
        if "logout" in docker_msg.lower():
            issues.append("Docker access requires logout/login")
        else:
            issues.append("Cannot connect to Docker daemon")
    
    click.echo()
    
    # Check 10-11: Configuration
    if install_dir:
        click.echo("Configuration")
        click.echo("-" * 70)
        
        # Check 10: .env file exists
        env_path = paths.get_env_path()
        if env_path.exists():
            click.echo(f"  ✓ .env file exists")
            
            # Check 11: .env has required values
            try:
                env_content = env_path.read_text()
                required_vars = ['DOMAIN', 'ACME_EMAIL', 'NETWORK_SUBNET', 'NETWORK_GATEWAY', 'TRAEFIK_IP']
                missing_vars = []
                
                for var in required_vars:
                    if f"{var}=" not in env_content or f"{var}=" in env_content and not env_content.split(f"{var}=")[1].split('\n')[0].strip():
                        missing_vars.append(var)
                
                if missing_vars:
                    click.echo(f"  ✗ Missing configuration values: {', '.join(missing_vars)}")
                    issues.append(f"Edit {env_path} to set: {', '.join(missing_vars)}")
                else:
                    click.echo(f"  ✓ All required configuration present")
                    
            except Exception as e:
                click.echo(f"  ⚠ Could not validate .env: {e}")
        else:
            click.echo(f"  ✗ .env file missing")
            issues.append("Configuration file missing - run: sudo syrvis setup --interactive")
        
        click.echo()
    
    # Check 12-13: System Integration
    if install_dir:
        click.echo("System Integration")
        click.echo("-" * 70)
        
        # Check 12: Global symlink
        symlink_path = Path('/usr/local/bin/syrvis')
        if symlink_path.exists() and symlink_path.is_symlink():
            # Use os.readlink() for Python 3.8 compatibility (Path.readlink() added in 3.9)
            target = os.readlink(str(symlink_path))
            click.echo(f"  ✓ Global command: {symlink_path} → {target}")
        else:
            click.echo(f"  ✗ Global command missing")
            issues.append("Global syrvis command not available")
            if fix:
                fixable_issues.append("symlink")
        
        # Check 13: Startup script
        startup_script = install_dir / "bin" / "syrvis-startup.sh"
        if startup_script.exists():
            click.echo(f"  ✓ Startup script exists: {startup_script}")
            click.echo(f"     → Add to Task Scheduler for boot persistence")
        else:
            click.echo(f"  ✗ Startup script missing")
            issues.append("Startup script missing")
            if fix:
                fixable_issues.append(f"startup:{username}")
        
        click.echo()
    
    # Summary
    click.echo("=" * 70)
    if not issues:
        click.echo("✓ All checks passed!")
        click.echo()
        click.echo("Your SyrvisCore installation is healthy.")
    else:
        click.echo(f"✗ Issues Found: {len(issues)}")
        click.echo()
        for i, issue in enumerate(issues, 1):
            click.echo(f"  {i}. {issue}")
        
        click.echo()
        
        if fixable_issues and not fix:
            click.echo(f"Fixable with --fix: {len(fixable_issues)}")
            click.echo()
            click.echo("Run with --fix to attempt automatic repairs:")
            click.echo("  sudo syrvis doctor --fix")
        elif fix and fixable_issues:
            click.echo()
            click.echo("Attempting Fixes")
            click.echo("-" * 70)
            
            fixed_count = 0
            
            for item in fixable_issues:
                if item == "docker_group":
                    click.echo("Creating docker group...")
                    success, msg = privileged_ops.ensure_docker_group()
                    click.echo(f"  {'✓' if success else '✗'} {msg}")
                    if success:
                        fixed_count += 1
                
                elif item.startswith("user_group:"):
                    user = item.split(':', 1)[1]
                    click.echo(f"Adding {user} to docker group...")
                    success, msg = privileged_ops.ensure_user_in_docker_group(user)
                    click.echo(f"  {'✓' if success else '✗'} {msg}")
                    if success:
                        fixed_count += 1
                
                elif item == "socket_perms":
                    click.echo("Fixing Docker socket permissions...")
                    success, msg = privileged_ops.ensure_docker_socket_permissions()
                    click.echo(f"  {'✓' if success else '✗'} {msg}")
                    if success:
                        fixed_count += 1
                
                elif item == "symlink":
                    click.echo("Creating global symlink...")
                    success, msg = privileged_ops.ensure_global_symlink(install_dir)
                    click.echo(f"  {'✓' if success else '✗'} {msg}")
                    if success:
                        fixed_count += 1
                
                elif item.startswith("startup:"):
                    user = item.split(':', 1)[1]
                    click.echo("Creating startup script...")
                    success, msg = privileged_ops.ensure_startup_script(install_dir, user)
                    click.echo(f"  {'✓' if success else '✗'} {msg}")
                    if success:
                        fixed_count += 1
            
            click.echo()
            click.echo(f"Fixed: {fixed_count}/{len(fixable_issues)} issues")
            
            if fixed_count > 0:
                click.echo()
                click.echo("Re-run doctor to verify fixes:")
                click.echo("  syrvis doctor")
    
    click.echo("=" * 70)
    click.echo()
    
    sys.exit(0 if not issues else 1)
