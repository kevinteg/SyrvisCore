"""Privileged operations for setup and doctor commands."""

import os
import subprocess
import grp
import pwd
from pathlib import Path
from typing import Tuple, Optional


class PrivilegedOpsError(Exception):
    """Error during privileged operation."""
    pass


def is_simulation_mode() -> bool:
    """Check if running in DSM simulation mode."""
    return os.environ.get("DSM_SIM_ACTIVE") == "1"


def get_sim_root() -> Optional[Path]:
    """Get simulation root directory if in simulation mode."""
    if is_simulation_mode():
        sim_root = os.environ.get("DSM_SIM_ROOT")
        if sim_root:
            return Path(sim_root)
    return None


def verify_root() -> None:
    """Ensure running as root (skipped in simulation mode)."""
    if is_simulation_mode():
        return  # Skip root check in simulation
    if os.getuid() != 0:
        raise PrivilegedOpsError(
            "This operation requires root privileges.\n"
            "Run with: sudo syrvis <command>"
        )


def get_target_user() -> str:
    """Get the user who invoked sudo (or current user in simulation mode)."""
    if is_simulation_mode():
        # In simulation mode, just use current user
        return os.environ.get('USER', 'testuser')

    user = os.environ.get('SUDO_USER') or os.environ.get('USER')
    if user == 'root' or not user:
        raise PrivilegedOpsError(
            "Cannot determine target user.\n"
            "Don't run as root directly. Use sudo from your user account:\n"
            "  sudo syrvis setup --interactive"
        )
    return user


def verify_docker_installed() -> Tuple[bool, str]:
    """Check if Docker package is installed on Synology."""
    try:
        result = subprocess.run(
            ['synopkg', 'status', 'Docker'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and 'running' in result.stdout.lower():
            return True, "Docker is installed and running"
        elif result.returncode == 0:
            return False, "Docker is installed but not running"
        else:
            return False, "Docker package not installed"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, "Unable to check Docker status (synopkg command failed)"


def verify_docker_socket_exists() -> Tuple[bool, str]:
    """Check if Docker socket exists."""
    sim_root = get_sim_root()
    if sim_root:
        socket_path = sim_root / 'var/run/docker.sock'
    else:
        socket_path = Path('/var/run/docker.sock')

    if socket_path.exists():
        return True, f"Docker socket exists: {socket_path}"
    return False, f"Docker socket not found at {socket_path}"


def get_docker_group_info() -> Tuple[bool, Optional[int]]:
    """Check if docker group exists and return its GID."""
    try:
        docker_group = grp.getgrnam('docker')
        return True, docker_group.gr_gid
    except KeyError:
        return False, None


def ensure_docker_group() -> Tuple[bool, str]:
    """Create docker group if it doesn't exist."""
    exists, gid = get_docker_group_info()
    if exists:
        return True, f"Docker group already exists (GID: {gid})"
    
    try:
        result = subprocess.run(
            ['synogroup', '--add', 'docker'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            exists, gid = get_docker_group_info()
            return True, f"Docker group created (GID: {gid})"
        else:
            return False, f"Failed to create docker group: {result.stderr}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"Error creating docker group: {e}"


def is_user_in_group(username: str, groupname: str) -> bool:
    """Check if user is in specified group."""
    try:
        user_info = pwd.getpwnam(username)
        user_groups = [g.gr_name for g in grp.getgrall() if username in g.gr_mem]
        # Also check primary group
        primary_group = grp.getgrgid(user_info.pw_gid).gr_name
        user_groups.append(primary_group)
        return groupname in user_groups
    except KeyError:
        return False


def ensure_user_in_docker_group(username: str) -> Tuple[bool, str]:
    """Add user to docker group."""
    if is_user_in_group(username, 'docker'):
        return True, f"User '{username}' already in docker group"
    
    try:
        result = subprocess.run(
            ['synogroup', '--member', 'docker', username],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return True, f"User '{username}' added to docker group (logout required)"
        else:
            return False, f"Failed to add user to docker group: {result.stderr}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"Error adding user to docker group: {e}"


def get_docker_socket_permissions() -> Tuple[str, str, str]:
    """Get Docker socket owner, group, and permissions."""
    sim_root = get_sim_root()
    if sim_root:
        socket_path = sim_root / 'var/run/docker.sock'
    else:
        socket_path = Path('/var/run/docker.sock')

    if not socket_path.exists():
        return "missing", "missing", "000"

    stat_info = socket_path.stat()
    try:
        owner = pwd.getpwuid(stat_info.st_uid).pw_name
    except KeyError:
        owner = str(stat_info.st_uid)
    try:
        group = grp.getgrgid(stat_info.st_gid).gr_name
    except KeyError:
        group = str(stat_info.st_gid)
    perms = oct(stat_info.st_mode)[-3:]
    return owner, group, perms


def ensure_docker_socket_permissions() -> Tuple[bool, str]:
    """Set Docker socket to root:docker 660."""
    sim_root = get_sim_root()
    if sim_root:
        socket_path = sim_root / 'var/run/docker.sock'
        # In simulation mode, just check if it exists
        if socket_path.exists():
            return True, "Docker socket permissions OK (simulation mode)"
        return False, "Docker socket not found (simulation mode)"
    else:
        socket_path = Path('/var/run/docker.sock')

    if not socket_path.exists():
        return False, "Docker socket not found"

    owner, group, perms = get_docker_socket_permissions()

    # Check if already correct
    if group == 'docker' and perms == '660':
        return True, f"Docker socket permissions already correct ({owner}:{group} {perms})"

    try:
        # Get docker group GID
        _, gid = get_docker_group_info()
        if gid is None:
            return False, "Docker group not found"

        # Change group ownership
        os.chown(str(socket_path), -1, gid)

        # Set permissions
        os.chmod(str(socket_path), 0o660)

        owner, group, perms = get_docker_socket_permissions()
        return True, f"Docker socket permissions updated ({owner}:{group} {perms})"
    except (OSError, PermissionError) as e:
        return False, f"Failed to set socket permissions: {e}"


def ensure_global_symlink(install_dir: Path) -> Tuple[bool, str]:
    """Create /usr/local/bin/syrvis symlink."""
    sim_root = get_sim_root()
    if sim_root:
        symlink_path = sim_root / 'usr/local/bin/syrvis'
    else:
        symlink_path = Path('/usr/local/bin/syrvis')

    # Use absolute path for target
    target = (install_dir / 'bin' / 'syrvis').resolve()

    if not target.exists():
        return False, f"Target script not found: {target}"

    # Check if symlink exists and is correct
    if symlink_path.exists():
        if symlink_path.is_symlink():
            # Use os.readlink() for Python 3.8 compatibility
            current_target = os.readlink(str(symlink_path))
            if str(current_target) == str(target):
                return True, f"Global symlink already correct: {symlink_path} → {target}"
            else:
                # Remove incorrect symlink
                symlink_path.unlink()
        else:
            return False, f"File exists but is not a symlink: {symlink_path}"

    try:
        # Create parent directory if needed
        symlink_path.parent.mkdir(parents=True, exist_ok=True)

        # Create symlink
        symlink_path.symlink_to(target)
        return True, f"Global symlink created: {symlink_path} → {target}"
    except (OSError, PermissionError) as e:
        return False, f"Failed to create symlink: {e}"


def ensure_startup_script(install_dir: Path, username: str) -> Tuple[bool, str]:
    """Install boot-time startup script using Task Scheduler."""
    # For now, we'll just create the script file
    # User can import it into Task Scheduler manually
    # A future enhancement could use DSM API to automate this
    
    startup_script_path = install_dir / 'bin' / 'syrvis-startup.sh'
    
    script_content = f"""#!/bin/bash
# SyrvisCore startup script
# This script should be run at boot via Task Scheduler

# Set up Docker permissions
DOCKER_GROUP_GID=$(getent group docker | cut -d: -f3)
if [ -n "$DOCKER_GROUP_GID" ]; then
    chown root:docker /var/run/docker.sock
    chmod 660 /var/run/docker.sock
fi

# Ensure user is in docker group
/usr/syno/sbin/synogroup --member docker {username} 2>/dev/null || true

exit 0
"""
    
    try:
        startup_script_path.write_text(script_content)
        startup_script_path.chmod(0o755)
        return True, f"Startup script created: {startup_script_path}"
    except (OSError, PermissionError) as e:
        return False, f"Failed to create startup script: {e}"


def verify_docker_accessible(username: Optional[str] = None) -> Tuple[bool, str]:
    """Test if Docker daemon is accessible."""
    try:
        # Try as root first
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return True, "Docker daemon accessible"
        
        # If we have a username, try as that user
        if username:
            result = subprocess.run(
                ['su', '-', username, '-c', 'docker info'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, f"Docker daemon accessible for user '{username}'"
            else:
                return False, f"Docker not accessible for user '{username}' (may need logout)"
        
        return False, "Docker daemon not accessible"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"Cannot test Docker access: {e}"
