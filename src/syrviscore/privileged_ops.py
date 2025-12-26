"""
Privileged operations for setup and doctor commands.

Uses a provider pattern to abstract system operations, allowing different
implementations for real DSM environments vs simulation/testing.
"""

import os
import subprocess
import grp
import pwd
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Tuple, Optional


class PrivilegedOpsError(Exception):
    """Error during privileged operation."""
    pass


# =============================================================================
# System Operations Interface
# =============================================================================

class SystemOperations(ABC):
    """
    Abstract interface for system operations.

    Implementations provide environment-specific behavior for privileged
    operations like Docker management, group membership, etc.
    """

    @property
    @abstractmethod
    def mode_name(self) -> str:
        """Human-readable name for this operations mode (for logging/display)."""
        pass

    @property
    @abstractmethod
    def is_simulation(self) -> bool:
        """Whether this is a simulation/test environment."""
        pass

    @abstractmethod
    def get_target_user(self) -> str:
        """Get the user who should own installed files."""
        pass

    @abstractmethod
    def needs_privilege_elevation(self) -> bool:
        """Check if we need to elevate to root."""
        pass

    @abstractmethod
    def verify_docker_installed(self) -> Tuple[bool, str]:
        """Check if Docker is installed and running."""
        pass

    @abstractmethod
    def verify_docker_socket_exists(self) -> Tuple[bool, str]:
        """Check if Docker socket exists."""
        pass

    @abstractmethod
    def ensure_docker_group(self) -> Tuple[bool, str]:
        """Create docker group if it doesn't exist."""
        pass

    @abstractmethod
    def ensure_user_in_docker_group(self, username: str) -> Tuple[bool, str]:
        """Add user to docker group."""
        pass

    @abstractmethod
    def ensure_docker_socket_permissions(self) -> Tuple[bool, str]:
        """Set Docker socket permissions."""
        pass

    @abstractmethod
    def ensure_global_symlink(self, install_dir: Path) -> Tuple[bool, str]:
        """Create global command symlink."""
        pass

    @abstractmethod
    def ensure_startup_script(self, install_dir: Path, username: str) -> Tuple[bool, str]:
        """Create startup script."""
        pass

    @abstractmethod
    def verify_docker_accessible(self, username: Optional[str] = None) -> Tuple[bool, str]:
        """Test if Docker daemon is accessible."""
        pass

    @abstractmethod
    def ensure_macvlan_shim(self, interface: str, traefik_ip: str, shim_ip: str) -> Tuple[bool, str]:
        """
        Create macvlan shim interface to allow host-to-container communication.

        Macvlan containers cannot communicate with their host by default.
        This creates a shim interface on the host to enable communication.

        Args:
            interface: Parent network interface (e.g., ovs_eth0)
            traefik_ip: IP address of the Traefik container
            shim_ip: IP address to assign to the shim interface

        Returns:
            Tuple of (success, message)
        """
        pass


# =============================================================================
# DSM Operations (Production)
# =============================================================================

class DsmOperations(SystemOperations):
    """
    Real DSM operations for Synology NAS environment.

    Uses synopkg, synogroup, and other Synology-specific commands.
    """

    @property
    def mode_name(self) -> str:
        return "DSM"

    @property
    def is_simulation(self) -> bool:
        return False

    def get_target_user(self) -> str:
        """Get the user who invoked sudo."""
        user = os.environ.get('SUDO_USER') or os.environ.get('USER')
        if user == 'root' or not user:
            raise PrivilegedOpsError(
                "Cannot determine target user.\n"
                "Don't run as root directly. Use sudo from your user account:\n"
                "  sudo syrvis setup"
            )
        return user

    def needs_privilege_elevation(self) -> bool:
        """Check if we need to elevate to root."""
        return os.getuid() != 0

    def verify_docker_installed(self) -> Tuple[bool, str]:
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

    def verify_docker_socket_exists(self) -> Tuple[bool, str]:
        """Check if Docker socket exists."""
        socket_path = Path('/var/run/docker.sock')
        if socket_path.exists():
            return True, f"Docker socket exists: {socket_path}"
        return False, "Docker socket not found at /var/run/docker.sock"

    def _get_docker_group_info(self) -> Tuple[bool, Optional[int]]:
        """Check if docker group exists and return its GID."""
        try:
            docker_group = grp.getgrnam('docker')
            return True, docker_group.gr_gid
        except KeyError:
            return False, None

    def ensure_docker_group(self) -> Tuple[bool, str]:
        """Create docker group if it doesn't exist."""
        exists, gid = self._get_docker_group_info()
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
                exists, gid = self._get_docker_group_info()
                return True, f"Docker group created (GID: {gid})"
            else:
                return False, f"Failed to create docker group: {result.stderr}"
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, f"Error creating docker group: {e}"

    def _is_user_in_group(self, username: str, groupname: str) -> bool:
        """Check if user is in specified group."""
        try:
            user_info = pwd.getpwnam(username)
            user_groups = [g.gr_name for g in grp.getgrall() if username in g.gr_mem]
            primary_group = grp.getgrgid(user_info.pw_gid).gr_name
            user_groups.append(primary_group)
            return groupname in user_groups
        except KeyError:
            return False

    def ensure_user_in_docker_group(self, username: str) -> Tuple[bool, str]:
        """Add user to docker group."""
        if self._is_user_in_group(username, 'docker'):
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

    def _get_docker_socket_permissions(self) -> Tuple[str, str, str]:
        """Get Docker socket owner, group, and permissions."""
        socket_path = Path('/var/run/docker.sock')
        if not socket_path.exists():
            return "missing", "missing", "000"

        stat_info = socket_path.stat()
        owner = pwd.getpwuid(stat_info.st_uid).pw_name
        group = grp.getgrgid(stat_info.st_gid).gr_name
        perms = oct(stat_info.st_mode)[-3:]
        return owner, group, perms

    def ensure_docker_socket_permissions(self) -> Tuple[bool, str]:
        """Set Docker socket to root:docker 660."""
        socket_path = Path('/var/run/docker.sock')
        if not socket_path.exists():
            return False, "Docker socket not found"

        owner, group, perms = self._get_docker_socket_permissions()

        if group == 'docker' and perms == '660':
            return True, f"Docker socket permissions already correct ({owner}:{group} {perms})"

        try:
            _, gid = self._get_docker_group_info()
            if gid is None:
                return False, "Docker group not found"

            os.chown(str(socket_path), -1, gid)
            os.chmod(str(socket_path), 0o660)

            owner, group, perms = self._get_docker_socket_permissions()
            return True, f"Docker socket permissions updated ({owner}:{group} {perms})"
        except (OSError, PermissionError) as e:
            return False, f"Failed to set socket permissions: {e}"

    def ensure_global_symlink(self, install_dir: Path) -> Tuple[bool, str]:
        """Create /usr/local/bin/syrvis symlink."""
        symlink_path = Path('/usr/local/bin/syrvis')
        target = (install_dir / 'bin' / 'syrvis').resolve()

        if not target.exists():
            return False, f"Target script not found: {target}"

        # Check if symlink exists (including broken symlinks)
        # exists() returns False for broken symlinks, but is_symlink() returns True
        if symlink_path.exists() or symlink_path.is_symlink():
            if symlink_path.is_symlink():
                current_target = os.readlink(str(symlink_path))
                if str(current_target) == str(target):
                    return True, f"Global symlink already correct: {symlink_path} -> {target}"
                else:
                    symlink_path.unlink()
            else:
                return False, f"File exists but is not a symlink: {symlink_path}"

        try:
            symlink_path.parent.mkdir(parents=True, exist_ok=True)
            symlink_path.symlink_to(target)
            return True, f"Global symlink created: {symlink_path} -> {target}"
        except (OSError, PermissionError) as e:
            return False, f"Failed to create symlink: {e}"

    def ensure_startup_script(self, install_dir: Path, username: str) -> Tuple[bool, str]:
        """Create startup script for Task Scheduler."""
        startup_script_path = install_dir / 'bin' / 'syrvis-startup.sh'
        env_path = install_dir / 'config' / '.env'

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

# Load environment variables
if [ -f "{env_path}" ]; then
    export $(grep -v '^#' "{env_path}" | xargs)
fi

# Create macvlan shim for host-to-container communication
# This is required because macvlan containers cannot talk to their host
if [ -n "$NETWORK_INTERFACE" ] && [ -n "$TRAEFIK_IP" ]; then
    SHIM_NAME="syrvis-shim"

    # Calculate shim IP (traefik_ip + 1)
    SHIM_IP=$(echo "$TRAEFIK_IP" | awk -F. '{{print $1"."$2"."$3"."$4+1}}')

    # Check if shim already exists
    if ! ip link show "$SHIM_NAME" >/dev/null 2>&1; then
        # Create macvlan shim interface
        ip link add "$SHIM_NAME" link "$NETWORK_INTERFACE" type macvlan mode bridge
        ip addr add "$SHIM_IP/32" dev "$SHIM_NAME"
        ip link set "$SHIM_NAME" up
        ip route add "$TRAEFIK_IP/32" dev "$SHIM_NAME"
        echo "Created macvlan shim: $SHIM_NAME ($SHIM_IP) -> $TRAEFIK_IP"
    fi
fi

exit 0
"""

        try:
            startup_script_path.parent.mkdir(parents=True, exist_ok=True)
            startup_script_path.write_text(script_content)
            startup_script_path.chmod(0o755)
            return True, f"Startup script created: {startup_script_path}"
        except (OSError, PermissionError) as e:
            return False, f"Failed to create startup script: {e}"

    def verify_docker_accessible(self, username: Optional[str] = None) -> Tuple[bool, str]:
        """Test if Docker daemon is accessible."""
        try:
            result = subprocess.run(
                ['docker', 'info'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, "Docker daemon accessible"

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

    def ensure_macvlan_shim(self, interface: str, traefik_ip: str, shim_ip: str) -> Tuple[bool, str]:
        """
        Create macvlan shim interface to allow host-to-container communication.

        This is required because macvlan containers cannot communicate with
        their host directly. The shim interface bridges this gap.
        """
        shim_name = "syrvis-shim"

        try:
            # Check if shim interface already exists
            result = subprocess.run(
                ['ip', 'link', 'show', shim_name],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                # Interface exists, check if route to traefik_ip exists
                route_result = subprocess.run(
                    ['ip', 'route', 'show', f'{traefik_ip}/32'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if traefik_ip in route_result.stdout:
                    return True, f"Macvlan shim already configured ({shim_name})"

                # Add route if missing
                subprocess.run(
                    ['ip', 'route', 'add', f'{traefik_ip}/32', 'dev', shim_name],
                    capture_output=True,
                    timeout=5
                )
                return True, f"Macvlan shim route added for {traefik_ip}"

            # Create the shim interface
            # Step 1: Create macvlan interface
            result = subprocess.run(
                ['ip', 'link', 'add', shim_name, 'link', interface, 'type', 'macvlan', 'mode', 'bridge'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return False, f"Failed to create shim interface: {result.stderr}"

            # Step 2: Assign IP address to shim
            result = subprocess.run(
                ['ip', 'addr', 'add', f'{shim_ip}/32', 'dev', shim_name],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                # Cleanup on failure
                subprocess.run(['ip', 'link', 'del', shim_name], capture_output=True, timeout=5)
                return False, f"Failed to assign IP to shim: {result.stderr}"

            # Step 3: Bring interface up
            result = subprocess.run(
                ['ip', 'link', 'set', shim_name, 'up'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                subprocess.run(['ip', 'link', 'del', shim_name], capture_output=True, timeout=5)
                return False, f"Failed to bring up shim interface: {result.stderr}"

            # Step 4: Add route to Traefik IP
            result = subprocess.run(
                ['ip', 'route', 'add', f'{traefik_ip}/32', 'dev', shim_name],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                # Route might already exist, not a fatal error
                pass

            return True, f"Macvlan shim created: {shim_name} ({shim_ip}) -> {traefik_ip}"

        except subprocess.TimeoutExpired:
            return False, "Timeout while configuring macvlan shim"
        except Exception as e:
            return False, f"Error configuring macvlan shim: {e}"


# =============================================================================
# Simulation Operations (Testing)
# =============================================================================

class SimulationOperations(SystemOperations):
    """
    Simulation operations for testing on non-DSM systems (e.g., macOS).

    Skips privileged operations and uses simulation root paths.
    """

    def __init__(self, sim_root: Path):
        self._sim_root = sim_root

    @property
    def sim_root(self) -> Path:
        return self._sim_root

    @property
    def mode_name(self) -> str:
        return "Simulation"

    @property
    def is_simulation(self) -> bool:
        return True

    def get_target_user(self) -> str:
        """In simulation, use current user."""
        return os.environ.get('USER', 'simuser')

    def needs_privilege_elevation(self) -> bool:
        """Simulation never needs elevation."""
        return False

    def verify_docker_installed(self) -> Tuple[bool, str]:
        """Check if Docker is available on host."""
        try:
            result = subprocess.run(
                ['docker', '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, "Docker available (host)"
            return True, "Docker check skipped"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "Docker check skipped"

    def verify_docker_socket_exists(self) -> Tuple[bool, str]:
        """Check Docker socket on host."""
        if Path('/var/run/docker.sock').exists():
            return True, "Docker socket exists (host)"
        return True, "Docker socket check skipped"

    def ensure_docker_group(self) -> Tuple[bool, str]:
        """Skip docker group creation in simulation."""
        try:
            docker_group = grp.getgrnam('docker')
            return True, f"Docker group exists (GID: {docker_group.gr_gid})"
        except KeyError:
            return True, "Docker group check skipped"

    def ensure_user_in_docker_group(self, username: str) -> Tuple[bool, str]:
        """Skip group membership in simulation."""
        return True, f"User '{username}' group check skipped"

    def ensure_docker_socket_permissions(self) -> Tuple[bool, str]:
        """Skip socket permissions in simulation."""
        return True, "Socket permissions skipped"

    def ensure_global_symlink(self, install_dir: Path) -> Tuple[bool, str]:
        """Create symlink in simulation root."""
        symlink_path = self._sim_root / "usr" / "local" / "bin" / "syrvis"
        target = (install_dir / 'bin' / 'syrvis').resolve()

        if not target.exists():
            return False, f"Target script not found: {target}"

        # Check if symlink exists (including broken symlinks)
        if symlink_path.exists() or symlink_path.is_symlink():
            if symlink_path.is_symlink():
                current_target = os.readlink(str(symlink_path))
                if str(current_target) == str(target):
                    return True, f"Global symlink already correct: {symlink_path} -> {target}"
                else:
                    symlink_path.unlink()
            else:
                return False, f"File exists but is not a symlink: {symlink_path}"

        try:
            symlink_path.parent.mkdir(parents=True, exist_ok=True)
            symlink_path.symlink_to(target)
            return True, f"Global symlink created: {symlink_path} -> {target}"
        except (OSError, PermissionError) as e:
            return False, f"Failed to create symlink: {e}"

    def ensure_startup_script(self, install_dir: Path, username: str) -> Tuple[bool, str]:
        """Create startup script (same as DSM, just for testing)."""
        startup_script_path = install_dir / 'bin' / 'syrvis-startup.sh'

        script_content = f"""#!/bin/bash
# SyrvisCore startup script (simulation)
# This script would run at boot on real DSM

echo "Startup script executed for user: {username}"
exit 0
"""

        try:
            startup_script_path.parent.mkdir(parents=True, exist_ok=True)
            startup_script_path.write_text(script_content)
            startup_script_path.chmod(0o755)
            return True, f"Startup script created: {startup_script_path}"
        except (OSError, PermissionError) as e:
            return False, f"Failed to create startup script: {e}"

    def verify_docker_accessible(self, username: Optional[str] = None) -> Tuple[bool, str]:
        """Check Docker on host."""
        try:
            result = subprocess.run(
                ['docker', 'info'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return True, "Docker daemon accessible (host)"
            return True, "Docker access check skipped"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "Docker access check skipped"

    def ensure_macvlan_shim(self, interface: str, traefik_ip: str, shim_ip: str) -> Tuple[bool, str]:
        """Skip macvlan shim in simulation (not needed on macOS/Linux desktop)."""
        return True, "Macvlan shim skipped (simulation mode)"


# =============================================================================
# Provider Factory
# =============================================================================

# Module-level instance (lazily initialized)
_operations_instance: Optional[SystemOperations] = None


def get_system_operations() -> SystemOperations:
    """
    Get the appropriate SystemOperations implementation.

    Returns SimulationOperations if DSM_SIM_ACTIVE=1, otherwise DsmOperations.
    The instance is cached for the lifetime of the process.

    Returns:
        SystemOperations implementation appropriate for the environment
    """
    global _operations_instance

    if _operations_instance is None:
        if os.environ.get("DSM_SIM_ACTIVE") == "1":
            sim_root = os.environ.get("DSM_SIM_ROOT", "")
            if not sim_root:
                raise PrivilegedOpsError(
                    "DSM_SIM_ACTIVE=1 but DSM_SIM_ROOT not set"
                )
            _operations_instance = SimulationOperations(Path(sim_root))
        else:
            _operations_instance = DsmOperations()

    return _operations_instance


def reset_operations_instance() -> None:
    """Reset the cached operations instance (for testing)."""
    global _operations_instance
    _operations_instance = None


# =============================================================================
# Convenience Functions (backward compatibility)
# =============================================================================

def get_target_user() -> str:
    """Get the target user for installation."""
    return get_system_operations().get_target_user()


def verify_docker_installed() -> Tuple[bool, str]:
    """Check if Docker is installed."""
    return get_system_operations().verify_docker_installed()


def verify_docker_socket_exists() -> Tuple[bool, str]:
    """Check if Docker socket exists."""
    return get_system_operations().verify_docker_socket_exists()


def ensure_docker_group() -> Tuple[bool, str]:
    """Ensure docker group exists."""
    return get_system_operations().ensure_docker_group()


def ensure_user_in_docker_group(username: str) -> Tuple[bool, str]:
    """Ensure user is in docker group."""
    return get_system_operations().ensure_user_in_docker_group(username)


def ensure_docker_socket_permissions() -> Tuple[bool, str]:
    """Ensure docker socket has correct permissions."""
    return get_system_operations().ensure_docker_socket_permissions()


def ensure_global_symlink(install_dir: Path) -> Tuple[bool, str]:
    """Create global symlink."""
    return get_system_operations().ensure_global_symlink(install_dir)


def ensure_startup_script(install_dir: Path, username: str) -> Tuple[bool, str]:
    """Create startup script."""
    return get_system_operations().ensure_startup_script(install_dir, username)


def verify_docker_accessible(username: Optional[str] = None) -> Tuple[bool, str]:
    """Verify Docker is accessible."""
    return get_system_operations().verify_docker_accessible(username)


def ensure_macvlan_shim(interface: str, traefik_ip: str, shim_ip: str) -> Tuple[bool, str]:
    """Create macvlan shim for host-to-container communication."""
    return get_system_operations().ensure_macvlan_shim(interface, traefik_ip, shim_ip)


# =============================================================================
# Read-only diagnostic functions (don't need SystemOperations)
# =============================================================================

def get_docker_group_info() -> Tuple[bool, Optional[int]]:
    """Check if docker group exists and get its GID.

    Returns:
        Tuple of (exists, gid) - gid is None if group doesn't exist
    """
    import grp
    try:
        group_info = grp.getgrnam('docker')
        return True, group_info.gr_gid
    except KeyError:
        return False, None


def is_user_in_group(username: str, group: str) -> bool:
    """Check if a user is a member of a group.

    Args:
        username: The username to check
        group: The group name to check membership in

    Returns:
        True if user is in the group, False otherwise
    """
    import grp
    import pwd
    try:
        group_info = grp.getgrnam(group)
        # Check if user is in the group's member list
        if username in group_info.gr_mem:
            return True
        # Also check if this is the user's primary group
        try:
            user_info = pwd.getpwnam(username)
            if user_info.pw_gid == group_info.gr_gid:
                return True
        except KeyError:
            pass
        return False
    except KeyError:
        return False


def get_docker_socket_permissions() -> Tuple[str, str, str]:
    """Get Docker socket ownership and permissions.

    Returns:
        Tuple of (owner, group, permissions) as strings
        e.g., ("root", "docker", "660")
    """
    import os
    import stat
    import pwd
    import grp

    socket_path = "/var/run/docker.sock"

    try:
        st = os.stat(socket_path)

        # Get owner name
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)

        # Get group name
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group = str(st.st_gid)

        # Get permissions as octal string (e.g., "660")
        perms = oct(stat.S_IMODE(st.st_mode))[2:]

        return owner, group, perms
    except FileNotFoundError:
        return "unknown", "unknown", "000"
    except Exception:
        return "unknown", "unknown", "000"
