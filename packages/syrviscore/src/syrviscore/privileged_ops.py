"""
Privileged operations for setup and doctor commands.

Uses a provider pattern to abstract system operations, allowing different
implementations for real DSM environments vs simulation/testing.
"""

import os
import subprocess
import tempfile
import grp
import pwd
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Tuple, Optional

from syrviscore.errors import SyrvisError


class PrivilegedOpsError(SyrvisError):
    """Error during privileged operation."""

    code = "privileged_op_failed"


# Name of the host-side macvlan shim interface (see ``ensure_macvlan_shim``).
SHIM_NAME = "syrvis-shim"

# Where DSM keeps its per-interface network config. Module-level (not inlined)
# so tests can point it at a temp directory — the real path exists only on DSM.
NETWORK_SCRIPTS_DIR = Path("/etc/sysconfig/network-scripts")


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
    def ensure_boot_script(self, install_dir: Path) -> Tuple[bool, str]:
        """
        Create boot script in /usr/local/etc/rc.d/ to run startup script on boot.

        This is the critical hook that ensures the macvlan shim and Docker
        permissions are set up after every reboot.
        """
        pass

    @abstractmethod
    def ensure_macvlan_shim(
        self, interface: str, traefik_ip: str, shim_ip: str
    ) -> Tuple[bool, str]:
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
# Boot-hook rendering (single source of truth)
# =============================================================================


def render_startup_script(install_dir: Path, username: str) -> str:
    """Render the DSM boot hook (``syrvis-startup.sh``) content.

    Single source of truth for the boot hook: both ``DsmOperations.
    ensure_startup_script`` (which writes it) and the content-aware validator
    ``SystemValidator.check_startup_script`` (which compares the deployed file
    against the expected text) call this, so a deployed hook that has drifted
    behind the code is detected instead of silently rotting. Pure — computes a
    string, no I/O.
    """
    env_path = install_dir / "config" / ".env"
    # Same key set the CLI writes — rendered from the one source of truth with
    # shell variables in place of the concrete name/IP (see step 3 below).
    ifcfg_body = render_shim_ifcfg("$SHIM_NAME", "$SHIM_IP").rstrip("\n")

    return f"""#!/bin/bash
# SyrvisCore startup script (runs at boot via the rc.d hook / Task Scheduler).
# Auto-generated by syrvis setup — do not edit manually.
#
# ORDER MATTERS (boot-resume race, fixed 2026-07-30): the seam self-heal and the
# macvlan shim need no Docker and run FIRST, so the seam recovers even if the
# daemon is slow. The Docker socket perms and the reconcile run ONLY AFTER Docker
# is confirmed up, because on a cold/power-cycle boot ContainerManager can start
# minutes after this hook: perms applied before the socket exists silently miss,
# and a reconcile against a not-ready daemon silently no-ops (the estate then
# never auto-resumes).

# 1. Self-heal the operator seam shells. DSM regenerates /etc/passwd on EVERY
#    boot, resetting the seam accounts to /sbin/nologin. Idempotent + guarded;
#    needs no Docker, so it runs first.
for SEAM_USER in syrvis-operator syrvis-reader; do
    if grep -q "^$SEAM_USER:" /etc/passwd 2>/dev/null; then
        sed -i "s#^\\($SEAM_USER:.*\\):/sbin/nologin\\$#\\1:/bin/sh#" /etc/passwd
    fi
done

# 2. Load environment (source, don't word-split — values may contain spaces or
#    '#', and the Cloudflare token must not leak through xargs). Needed by the
#    macvlan shim, the reconcile, and the failure ntfy below.
if [ -f "{env_path}" ]; then
    set -a
    . "{env_path}"
    set +a
fi

# 3. Create the macvlan shim (host-to-container path; needs no Docker).
if [ -n "$NETWORK_INTERFACE" ] && [ -n "$TRAEFIK_IP" ]; then
    SHIM_NAME="{SHIM_NAME}"
    # Honor a configured SHIM_IP from .env; only compute (traefik_ip + 1) as a
    # fallback so the shim IP matches what `syrvis start` uses (no boot drift).
    SHIM_IP="${{SHIM_IP:-$(echo "$TRAEFIK_IP" | awk -F. '{{print $1"."$2"."$3"."$4+1}}')}}"
    if ! ip link show "$SHIM_NAME" >/dev/null 2>&1; then
        ip link add "$SHIM_NAME" link "$NETWORK_INTERFACE" type macvlan mode bridge
        ip addr add "$SHIM_IP/32" dev "$SHIM_NAME"
        ip link set "$SHIM_NAME" up
        ip route add "$TRAEFIK_IP/32" dev "$SHIM_NAME"
        echo "Created macvlan shim: $SHIM_NAME ($SHIM_IP) -> $TRAEFIK_IP"
    fi

    # Keep DSM's health poller quiet (incident 2026-08-10). DSM auto-stubs
    # ifcfg-<iface> for an interface it did not create with only
    # BOOTPROTO=static, then logs a read failure every ~60s
    # (SystemHealth.cpp:87 ... file_get_key_value.c:80). Write the full key set.
    # Deliberately OUTSIDE the create guard: an already-present shim whose file
    # DSM re-stubbed gets healed too. Check-then-write, so no churn per boot.
    # Boot writes it early; `syrvis start` re-asserts the same content via
    # privileged_ops.render_shim_ifcfg (one source of truth for the keys).
    if [ -d "{NETWORK_SCRIPTS_DIR}" ]; then
        IFCFG_PATH="{NETWORK_SCRIPTS_DIR}/ifcfg-$SHIM_NAME"
        IFCFG_WANT="{ifcfg_body}"
        if [ "$(cat "$IFCFG_PATH" 2>/dev/null)" != "$IFCFG_WANT" ]; then
            printf '%s\\n' "$IFCFG_WANT" > "$IFCFG_PATH" && chmod 644 "$IFCFG_PATH"
        fi
    fi
fi

# 4. Wait for Docker to be READY — robustly. Poll `docker info` until it answers,
#    up to ~600s (the old 120s cap let a slow ContainerManager slip past into a
#    silent no-op). Log progress every 30s so a slow boot is visible.
DOCKER_BIN=$(command -v docker || echo /usr/local/bin/docker)
DOCKER_WAIT=0
DOCKER_MAX=600
until "$DOCKER_BIN" info >/dev/null 2>&1; do
    if [ "$DOCKER_WAIT" -ge "$DOCKER_MAX" ]; then
        echo "syrvis-startup: Docker still not ready after ${{DOCKER_MAX}}s — proceeding"
        break
    fi
    [ $(( DOCKER_WAIT % 30 )) -eq 0 ] && echo "syrvis-startup: waiting for Docker... (${{DOCKER_WAIT}}s)"
    DOCKER_WAIT=$(( DOCKER_WAIT + 5 ))
    sleep 5
done

# 5. Fix Docker socket perms AFTER the daemon is up, so the chown lands on a real
#    socket and the seam user can reach Docker (running before it exists misses).
#    Add BOTH the setup user and syrvis-operator: DSM regenerates /etc/group on
#    boot like /etc/passwd, dropping the seam operator from the docker group, which
#    silently breaks the read-only seam checks (nas.monitoring / nas.runstate /
#    MCP status) until it is re-added.
DOCKER_GROUP_GID=$(getent group docker | cut -d: -f3)
if [ -n "$DOCKER_GROUP_GID" ] && [ -S /var/run/docker.sock ]; then
    chown root:docker /var/run/docker.sock
    chmod 660 /var/run/docker.sock
fi
/usr/syno/sbin/synogroup --member docker {username} syrvis-operator 2>/dev/null || true

# 6. Reconcile declared services / auto-resume (--boot honors resume_on_boot).
#    RETRY with visible logging — never silently swallow failure the way the old
#    `|| true` did, which is exactly how a slow-Docker boot left the estate down.
RECONCILE_OK=0
for attempt in 1 2 3; do
    if "{install_dir}/bin/syrvis" reconcile --boot; then
        RECONCILE_OK=1
        echo "syrvis-startup: reconcile --boot succeeded (attempt $attempt)"
        break
    fi
    echo "syrvis-startup: reconcile --boot failed (attempt $attempt) — retrying in 15s"
    sleep 15
done

# 7. Re-apply the managed /etc/crontab block (best-effort; no Docker needed).
"{install_dir}/bin/syrvis" schedule apply || true

# 8. Notify-on-failure: if the estate never reconciled, direct-POST ntfy so a
#    dead boot-resume pages instead of hiding. Best-effort + guarded (inert unless
#    NTFY_URL is present in the env).
if [ "$RECONCILE_OK" -ne 1 ]; then
    echo "syrvis-startup: BOOT RESUME FAILED after retries — estate likely down"
    if [ -n "$NTFY_URL" ]; then
        curl -fsS -m 15 \
            -H "Title: SyrvisCore boot-resume FAILED" \
            -H "Priority: urgent" \
            -H "Tags: rotating_light" \
            -d "RS1221+ booted but the estate did not auto-resume (reconcile --boot failed after retries). Run 'syrvis resume'." \
            "$NTFY_URL" >/dev/null 2>&1 || true
    fi
fi

exit 0
"""


def render_boot_script(install_dir: Path) -> str:
    """Render the rc.d S99 boot script (``S99syrviscore.sh``) content.

    Single source of truth for the rc.d hook: both ``DsmOperations.
    ensure_boot_script`` (which writes it) and the content-aware validator
    ``SystemValidator.check_boot_script`` (which compares the deployed file
    against this) call it, so a deployed S99 that drifted behind the code — e.g.
    an older render missing the design/28 ``stop)`` graceful-flush case — is
    detected instead of silently rotting. Pure — computes a string, no I/O.
    """
    startup_script = install_dir / "bin" / "syrvis-startup.sh"

    return f"""#!/bin/sh
# SyrvisCore boot script
# Auto-generated by syrvis setup - do not edit manually

case "$1" in
    start)
        if [ -x "{startup_script}" ]; then
            "{startup_script}"
        fi
        ;;
    stop)
        # home-tech design/28 Option A: on a DSM-initiated shutdown/reboot
        # (including UPS Safe Mode), gracefully flush the estate BEFORE teardown
        # so workloads halt in order (databases quiesced, stores stopped last).
        #
        # --reason reboot writes resume_on_boot=true, so the S99 start case
        # auto-resumes the estate on the next boot (unlike 'maintenance', which
        # would hold the estate down across a plain reboot/power event).
        #
        # SAFETY: this MUST NOT be able to hang DSM's own shutdown. The call is
        # hard-bounded by `timeout 150s`; on timeout/failure/absence we fall
        # through unconditionally to the shim delete + `exit 0`. The 150s bound
        # is the guard, but DSM's rc.d-stop timeout is UNVERIFIED here — a real
        # DSM-reboot test must confirm the whole stop case completes in time
        # before this graceful flush is trusted.
        if [ -x "{install_dir}/bin/syrvis" ]; then
            timeout 150s "{install_dir}/bin/syrvis" shutdown --reason reboot --json >/dev/null 2>&1 || true
        fi
        # Cleanup macvlan shim on shutdown (optional; always runs)
        ip link del {SHIM_NAME} 2>/dev/null || true
        ;;
    *)
        echo "Usage: $0 {{start|stop}}"
        exit 1
        ;;
esac

exit 0
"""


def render_shim_ifcfg(shim_name: str, shim_ip: str) -> str:
    """Render the DSM ``ifcfg-<shim>`` content for the macvlan shim. Pure.

    WHY THIS FILE EXISTS (incident 2026-08-10): DSM's health poller reads
    ``/etc/sysconfig/network-scripts/ifcfg-<iface>`` for every interface it sees.
    When it notices an interface it did not create — our macvlan shim — it
    auto-stubs that file with a single ``BOOTPROTO=static`` line, then fails to
    read the keys it actually wants and logs, every ~60 seconds:

        SystemHealth.cpp:87 Failed to get interface: [syrvis-shim] information
        [0x2000 file_get_key_value.c:80]

    Writing the full key set ourselves at shim-ensure time keeps the poller
    quiet. ``ONBOOT=no`` is load-bearing: the shim is created by SyrvisCore (CLI
    at start, rc.d hook at boot), so DSM must never try to bring it up itself
    from this file — the file is documentation for the poller, not a directive.
    ``NETMASK=255.255.255.255`` matches the /32 the shim is actually assigned.

    Single source of truth for the key set: the Python writer
    (``DsmOperations._ensure_shim_ifcfg``) and the boot hook's shell equivalent
    (``render_startup_script``) both render from here, so the two paths cannot
    disagree.
    """
    return (
        f"DEVICE={shim_name}\n"
        "BOOTPROTO=static\n"
        "ONBOOT=no\n"
        f"IPADDR={shim_ip}\n"
        "NETMASK=255.255.255.255\n"
    )


def _write_script_if_changed(
    path: Path, content: str, mode: int
) -> Tuple[bool, str]:
    """Write ``content`` to ``path`` (mode ``mode``) only when it differs.

    Content-aware: reads the current on-disk file (if any) and compares it to the
    freshly-rendered ``content``. Rewrites ONLY on a mismatch (or when absent) —
    the missing-only installers never noticed content drift, which is how a stale
    boot hook rotted on the NAS. Writes atomically (temp file + rename) so a crash
    mid-write can't leave a truncated boot script.

    Returns ``(changed, state)`` where ``state`` is one of ``"created"``,
    ``"updated"`` (content drift), or ``"unchanged"``.
    """
    existed = path.exists()
    if existed:
        try:
            if path.read_text() == content:
                # Already current; still (re)assert the mode cheaply, then no-op.
                path.chmod(mode)
                return False, "unchanged"
        except OSError:
            # Unreadable → fall through and rewrite from the rendered content.
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return True, ("updated" if existed else "created")


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
        user = os.environ.get("SUDO_USER") or os.environ.get("USER")
        if user == "root" or not user:
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
                ["synopkg", "status", "Docker"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "running" in result.stdout.lower():
                return True, "Docker is installed and running"
            elif result.returncode == 0:
                return False, "Docker is installed but not running"
            else:
                return False, "Docker package not installed"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False, "Unable to check Docker status (synopkg command failed)"

    def verify_docker_socket_exists(self) -> Tuple[bool, str]:
        """Check if Docker socket exists."""
        socket_path = Path("/var/run/docker.sock")
        if socket_path.exists():
            return True, f"Docker socket exists: {socket_path}"
        return False, "Docker socket not found at /var/run/docker.sock"

    def _get_docker_group_info(self) -> Tuple[bool, Optional[int]]:
        """Check if docker group exists and return its GID."""
        try:
            docker_group = grp.getgrnam("docker")
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
                ["synogroup", "--add", "docker"], capture_output=True, text=True, timeout=10
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
        if self._is_user_in_group(username, "docker"):
            return True, f"User '{username}' already in docker group"

        try:
            result = subprocess.run(
                ["synogroup", "--member", "docker", username],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, f"User '{username}' added to docker group (logout required)"
            else:
                return False, f"Failed to add user to docker group: {result.stderr}"
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, f"Error adding user to docker group: {e}"

    def _get_docker_socket_permissions(self) -> Tuple[str, str, str]:
        """Get Docker socket owner, group, and permissions."""
        socket_path = Path("/var/run/docker.sock")
        if not socket_path.exists():
            return "missing", "missing", "000"

        stat_info = socket_path.stat()
        owner = pwd.getpwuid(stat_info.st_uid).pw_name
        group = grp.getgrgid(stat_info.st_gid).gr_name
        perms = oct(stat_info.st_mode)[-3:]
        return owner, group, perms

    def ensure_docker_socket_permissions(self) -> Tuple[bool, str]:
        """Set Docker socket to root:docker 660."""
        socket_path = Path("/var/run/docker.sock")
        if not socket_path.exists():
            return False, "Docker socket not found"

        owner, group, perms = self._get_docker_socket_permissions()

        if group == "docker" and perms == "660":
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
        symlink_path = Path("/usr/local/bin/syrvis")
        target = (install_dir / "bin" / "syrvis").resolve()

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
        """Create/update the startup script, rewriting on content drift."""
        startup_script_path = install_dir / "bin" / "syrvis-startup.sh"

        # Rendered by the module-level renderer so the writer and the
        # content-aware validator share one source of truth (no silent drift).
        script_content = render_startup_script(install_dir, username)

        try:
            changed, state = _write_script_if_changed(startup_script_path, script_content, 0o755)
        except (OSError, PermissionError) as e:
            return False, f"Failed to create startup script: {e}"

        if state == "created":
            return True, f"Startup script created: {startup_script_path}"
        if state == "updated":
            return True, f"Startup script updated (content drift): {startup_script_path}"
        return True, f"Startup script already current: {startup_script_path}"

    def ensure_boot_script(self, install_dir: Path) -> Tuple[bool, str]:
        """Create/update the rc.d S99 boot script, rewriting on content drift.

        The old presence-only guard (rewrite only when absent, else short-circuit
        on a startup-path substring match) never noticed CONTENT drift — that is
        exactly why the design/28 ``stop)`` graceful-flush case never reached a
        NAS whose S99 predated it. Rendered via the shared ``render_boot_script``
        so writer and validator agree on the expected text.
        """
        boot_script_path = Path("/usr/local/etc/rc.d") / "S99syrviscore.sh"
        script_content = render_boot_script(install_dir)

        try:
            changed, state = _write_script_if_changed(boot_script_path, script_content, 0o755)
        except (OSError, PermissionError) as e:
            return False, f"Failed to create boot script: {e}"

        if state == "created":
            return True, f"Boot script created: {boot_script_path}"
        if state == "updated":
            return True, f"Boot script updated (content drift): {boot_script_path}"
        return True, f"Boot script already current: {boot_script_path}"

    def verify_docker_accessible(self, username: Optional[str] = None) -> Tuple[bool, str]:
        """Test if Docker daemon is accessible."""
        try:
            result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return True, "Docker daemon accessible"

            if username:
                result = subprocess.run(
                    ["su", "-", username, "-c", "docker info"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return True, f"Docker daemon accessible for user '{username}'"
                else:
                    return False, f"Docker not accessible for user '{username}' (may need logout)"

            return False, "Docker daemon not accessible"
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, f"Cannot test Docker access: {e}"

    def _ensure_shim_ifcfg(self, shim_name: str, shim_ip: str) -> str:
        """Reconcile ``ifcfg-<shim>`` so DSM's health poller can read the shim.

        See :func:`render_shim_ifcfg` for what DSM does without this file (the
        2026-08-10 ``SystemHealth.cpp:87`` log flood). Runs on EVERY ensure pass,
        not just at creation: DSM re-stubs the file whenever it re-notices the
        interface (and a DSM update can revert it), so a one-shot write at
        creation would silently rot.

        Two deliberate choices for the awkward cases:

        * **Directory absent** (a DSM that doesn't keep ifcfg files): skip, don't
          create it. Nothing polls a tree DSM doesn't maintain, and inventing
          system network-config directories we don't own is the riskier move.
        * **File carries EXTRA keys**: replaced wholesale, not merged. This file
          is declared state rendered in full from code — same philosophy as the
          boot hook (render → compare → rewrite on any drift). Keys that must
          persist belong in :func:`render_shim_ifcfg`, not in a hand edit.

        Never fails the shim: the interface, route, and reachability all work
        without this file — only DSM's logging is at stake. Returns a suffix to
        append to the caller's message (``""`` when the file was already
        correct, so an idempotent pass stays quiet).
        """
        if not NETWORK_SCRIPTS_DIR.is_dir():
            return f" (ifcfg skipped: no {NETWORK_SCRIPTS_DIR})"

        path = NETWORK_SCRIPTS_DIR / f"ifcfg-{shim_name}"
        content = render_shim_ifcfg(shim_name, shim_ip)

        try:
            # Reuses the atomic content-aware writer: compares first, so a
            # correct file is never rewritten (no churn, no inode change).
            _, state = _write_script_if_changed(path, content, 0o644)
        except (OSError, PermissionError) as e:
            return f" (ifcfg not written: {e})"

        return "" if state == "unchanged" else f" (ifcfg {state})"

    def ensure_macvlan_shim(
        self, interface: str, traefik_ip: str, shim_ip: str
    ) -> Tuple[bool, str]:
        """
        Create macvlan shim interface to allow host-to-container communication.

        This is required because macvlan containers cannot communicate with
        their host directly. The shim interface bridges this gap.

        Every successful path also reconciles the shim's DSM ifcfg file (see
        :meth:`_ensure_shim_ifcfg`) — that write is what keeps DSM's SystemHealth
        poller from logging a read failure every ~60s.
        """
        shim_name = SHIM_NAME

        try:
            # Check if shim interface already exists
            result = subprocess.run(
                ["ip", "link", "show", shim_name], capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0:
                # Interface exists. Reconcile its assigned address and host route
                # against the desired values. If TRAEFIK_IP (and thus SHIM_IP)
                # changed since the shim was created, the stale address/route would
                # linger and break host->Traefik reachability until reboot, so
                # tear the shim down and rebuild it cleanly. When the current state
                # already matches, do nothing extra (stay idempotent).
                addr_result = subprocess.run(
                    ["ip", "addr", "show", shim_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                addr_matches = f"inet {shim_ip}/32" in addr_result.stdout

                route_result = subprocess.run(
                    ["ip", "route", "show", f"{traefik_ip}/32"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                route_matches = f"dev {shim_name}" in route_result.stdout

                if addr_matches and route_matches:
                    ifcfg = self._ensure_shim_ifcfg(shim_name, shim_ip)
                    return True, f"Macvlan shim already configured ({shim_name}){ifcfg}"

                if not addr_matches:
                    # The shim's IP drifted (TRAEFIK_IP/SHIM_IP changed). Tear it
                    # down so it can be recreated with the correct address; the
                    # stale /32 route it carried disappears with the interface.
                    subprocess.run(["ip", "link", "del", shim_name], capture_output=True, timeout=5)
                    # Fall through to the create path below to rebuild cleanly.
                else:
                    # Address is correct but the route to Traefik is missing or on
                    # the wrong device. Reconcile the route in place.
                    subprocess.run(
                        ["ip", "route", "del", f"{traefik_ip}/32"],
                        capture_output=True,
                        timeout=5,
                    )
                    subprocess.run(
                        ["ip", "route", "add", f"{traefik_ip}/32", "dev", shim_name],
                        capture_output=True,
                        timeout=5,
                    )
                    ifcfg = self._ensure_shim_ifcfg(shim_name, shim_ip)
                    return True, f"Macvlan shim route reconciled for {traefik_ip}{ifcfg}"

            # Create the shim interface
            # Step 1: Create macvlan interface
            result = subprocess.run(
                [
                    "ip",
                    "link",
                    "add",
                    shim_name,
                    "link",
                    interface,
                    "type",
                    "macvlan",
                    "mode",
                    "bridge",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False, f"Failed to create shim interface: {result.stderr}"

            # Step 2: Assign IP address to shim
            result = subprocess.run(
                ["ip", "addr", "add", f"{shim_ip}/32", "dev", shim_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                # Cleanup on failure
                subprocess.run(["ip", "link", "del", shim_name], capture_output=True, timeout=5)
                return False, f"Failed to assign IP to shim: {result.stderr}"

            # Step 3: Bring interface up
            result = subprocess.run(
                ["ip", "link", "set", shim_name, "up"], capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                subprocess.run(["ip", "link", "del", shim_name], capture_output=True, timeout=5)
                return False, f"Failed to bring up shim interface: {result.stderr}"

            # Step 4: Add route to Traefik IP
            result = subprocess.run(
                ["ip", "route", "add", f"{traefik_ip}/32", "dev", shim_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                # Route might already exist, not a fatal error
                pass

            ifcfg = self._ensure_shim_ifcfg(shim_name, shim_ip)
            return True, f"Macvlan shim created: {shim_name} ({shim_ip}) -> {traefik_ip}{ifcfg}"

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
        return os.environ.get("USER", "simuser")

    def needs_privilege_elevation(self) -> bool:
        """Simulation never needs elevation."""
        return False

    def verify_docker_installed(self) -> Tuple[bool, str]:
        """Check if Docker is available on host."""
        try:
            result = subprocess.run(
                ["docker", "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return True, "Docker available (host)"
            return True, "Docker check skipped"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "Docker check skipped"

    def verify_docker_socket_exists(self) -> Tuple[bool, str]:
        """Check Docker socket on host."""
        if Path("/var/run/docker.sock").exists():
            return True, "Docker socket exists (host)"
        return True, "Docker socket check skipped"

    def ensure_docker_group(self) -> Tuple[bool, str]:
        """Skip docker group creation in simulation."""
        try:
            docker_group = grp.getgrnam("docker")
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
        target = (install_dir / "bin" / "syrvis").resolve()

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
        startup_script_path = install_dir / "bin" / "syrvis-startup.sh"

        script_content = f"""#!/bin/bash
# SyrvisCore startup script (simulation)
# This script would run at boot on real DSM

echo "Startup script executed for user: {username}"

# Reconcile declared Layer 2 services (config/services.d) -- best-effort:
# a failing declaration or service must never block boot or the core stack.
"{install_dir}/bin/syrvis" reconcile --boot || true

exit 0
"""

        try:
            startup_script_path.parent.mkdir(parents=True, exist_ok=True)
            startup_script_path.write_text(script_content)
            startup_script_path.chmod(0o755)
            return True, f"Startup script created: {startup_script_path}"
        except (OSError, PermissionError) as e:
            return False, f"Failed to create startup script: {e}"

    def ensure_boot_script(self, install_dir: Path) -> Tuple[bool, str]:
        """Create boot script in simulation rc.d directory."""
        rc_d_path = self._sim_root / "usr" / "local" / "etc" / "rc.d"
        boot_script_path = rc_d_path / "S99syrviscore.sh"
        startup_script = install_dir / "bin" / "syrvis-startup.sh"

        script_content = f"""#!/bin/sh
# SyrvisCore boot script (simulation)
# This script would run at boot on real DSM

case "$1" in
    start)
        echo "Would run: {startup_script}"
        ;;
    stop)
        echo "Would cleanup macvlan shim"
        ;;
esac
exit 0
"""

        try:
            rc_d_path.mkdir(parents=True, exist_ok=True)
            boot_script_path.write_text(script_content)
            boot_script_path.chmod(0o755)
            return True, f"Boot script created (sim): {boot_script_path}"
        except (OSError, PermissionError) as e:
            return False, f"Failed to create boot script: {e}"

    def verify_docker_accessible(self, username: Optional[str] = None) -> Tuple[bool, str]:
        """Check Docker on host."""
        try:
            result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return True, "Docker daemon accessible (host)"
            return True, "Docker access check skipped"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "Docker access check skipped"

    def ensure_macvlan_shim(
        self, interface: str, traefik_ip: str, shim_ip: str
    ) -> Tuple[bool, str]:
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
                raise PrivilegedOpsError("DSM_SIM_ACTIVE=1 but DSM_SIM_ROOT not set")
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


def ensure_boot_script(install_dir: Path) -> Tuple[bool, str]:
    """Create boot script to run startup script on reboot."""
    return get_system_operations().ensure_boot_script(install_dir)


def ensure_manifest_permissions(install_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Ensure the installation manifest is world-readable (0644).

    The manifest carries no secrets; a restrictive mode only breaks the
    unprivileged CLI's ability to read installation state, so 0644 is correct.
    """
    from . import paths as _paths

    if install_dir is not None:
        manifest_path = Path(install_dir) / ".syrviscore-manifest.json"
    else:
        try:
            manifest_path = _paths.get_manifest_path()
        except Exception as e:
            return False, f"Could not locate manifest: {e}"

    if not manifest_path.exists():
        return False, f"Manifest not found: {manifest_path}"

    try:
        manifest_path.chmod(0o644)
        return True, f"Manifest permissions set to 0644: {manifest_path}"
    except (OSError, PermissionError) as e:
        return False, f"Failed to set manifest permissions: {e}"


def ensure_config_tree_readable(install_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Make the config tree + service manifests + core compose readable by the
    operator (the ``docker`` group), so the unprivileged operator can run
    ``service list``/``verify`` without EPERM.

    A root reconcile/setup writes these ``root:root`` and locks the operator out.
    This chgrps them to the docker group and adds group-read (dirs g+rx, files
    g+r) WITHOUT touching ``config/.env`` — that stays 0600 because it carries
    secrets. Best-effort per path; idempotent; self-heals a root re-write.
    """
    from . import paths as _paths

    root = Path(install_dir) if install_dir is not None else None
    if root is None:
        try:
            root = _paths.get_syrvis_home()
        except Exception as e:
            return False, f"Could not locate SYRVIS_HOME: {e}"

    exists, gid = get_docker_group_info()
    if not exists or gid is None:
        return False, "docker group does not exist (fix docker_group first)"

    env_path = (root / "config" / ".env").resolve()
    fixed: list = []
    failed: list = []

    def _fix(p: Path, is_dir: bool) -> None:
        try:
            if p.resolve() == env_path:  # never touch the secret env file
                return
            os.chown(str(p), -1, gid)
            mode = p.stat().st_mode
            p.chmod(mode | (0o050 if is_dir else 0o040))
            fixed.append(p.name)
        except OSError as e:
            failed.append(f"{p.name}: {e}")

    for d in (
        root / "config",
        root / "config" / "services.d",
        root / "config" / "jobs.d",  # operator-writable job declarations (same treatment)
        root / "services",
    ):
        if d.is_dir():
            _fix(d, True)
    compose = root / "config" / "docker-compose.yaml"
    if compose.is_file():
        _fix(compose, False)
    services_dir = root / "services"
    if services_dir.is_dir():
        for manifest in services_dir.glob("*/syrvis-service.yaml"):
            _fix(manifest, False)
    # Declaration + job files a root writer (profile enable, service declare,
    # reconcile) may have written 0600 when they carry inline env — the
    # unprivileged operator's reconcile-plan and the dashboard must still read
    # them. (These are intent/config, never secrets: secret values live in the
    # env_file under data/, which this never touches.)
    for decl in (root / "config" / "services.d").glob("*.yaml"):
        _fix(decl, False)
    for job in (root / "config" / "jobs.d").glob("*.yaml"):
        _fix(job, False)

    if failed:
        return False, "config tree: fixed {}, failed: {}".format(len(fixed), "; ".join(failed[:4]))
    return True, "config tree readable by docker group (gid {}): {} path(s)".format(gid, len(fixed))


def ensure_schedule_block(install_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Re-apply the managed /etc/crontab block from config/jobs.d (self-heal).

    DSM regenerates /etc/crontab from its own DB on UI task edits, which can drop
    SyrvisCore's delimited block. This re-runs the scheduled-jobs reconcile so the
    block is restored (and job scripts re-materialized) — the remediation behind
    ``verify --fix``'s ``check_schedule_block``. Requires root (writes /etc/crontab
    + jobs/ root:root). With an empty jobs.d the result is an empty block (no-op).
    """
    from . import paths as _paths
    from . import schedule as _schedule

    root = Path(install_dir) if install_dir is not None else None
    if root is None:
        try:
            root = _paths.get_syrvis_home()
        except Exception as e:  # noqa: BLE001
            return False, "Could not locate SYRVIS_HOME: {}".format(e)
    try:
        result = _schedule.apply_schedule(root)
    except Exception as e:  # noqa: BLE001 - surface as a fix failure, never crash verify
        return False, "schedule apply failed: {}".format(e)
    scheduled = result.get("scheduled") or []
    if result.get("ok"):
        return True, "managed crontab block re-applied ({} job(s) scheduled)".format(len(scheduled))
    return False, "managed crontab block re-applied with errors: {}".format(
        result.get("invalid") or result.get("skipped")
    )


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
        group_info = grp.getgrnam("docker")
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
