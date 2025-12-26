"""
DSM 7.0 Simulation Environment for Python Tests.

This module provides a Python interface to the DSM simulation environment,
allowing tests to run in a simulated Synology DSM context.

Usage:
    from tests.dsm_sim import DsmSimulator

    # Create and initialize simulator
    sim = DsmSimulator()
    sim.setup()

    # Run code within simulation context
    with sim.activated():
        # Environment variables are set, paths point to simulation
        import subprocess
        subprocess.run(['syrvisctl', '--version'])

    # Reset simulation to clean state
    sim.reset()

    # Or use as pytest fixture:
    @pytest.fixture
    def dsm_sim():
        sim = DsmSimulator()
        sim.setup()
        yield sim
        sim.reset()
"""

import os
import json
import shutil
import subprocess
from pathlib import Path
from contextlib import contextmanager
from typing import Dict, Any, Optional, List


class DsmSimulator:
    """DSM 7.0 simulation environment for testing SyrvisCore."""

    def __init__(self, base_path: Optional[Path] = None):
        """
        Initialize simulator.

        Args:
            base_path: Base directory for simulation (default: tests/dsm-sim)
        """
        if base_path is None:
            # Find tests directory relative to this file
            tests_dir = Path(__file__).parent
            base_path = tests_dir / "dsm-sim"

        self.base_path = base_path.resolve()
        self.root = self.base_path / "root"
        self.state_dir = self.base_path / "state"
        self.bin_dir = self.base_path / "bin"
        self.logs_dir = self.base_path / "logs"

        # Key paths within simulation
        self.synopkg_pkgdest = self.root / "var/packages/syrviscore/target"
        self.syrvis_home = self.root / "volume1/docker/syrviscore"
        self.usr_local_bin = self.root / "usr/local/bin"
        self.docker_sock = self.root / "var/run/docker.sock"
        self.tmp_dir = self.root / "tmp"

        # Original environment (saved when activated)
        self._orig_env: Dict[str, Optional[str]] = {}
        self._is_active = False

    @property
    def is_active(self) -> bool:
        """Check if simulation is currently active."""
        return self._is_active or os.environ.get("DSM_SIM_ACTIVE") == "1"

    def setup(self) -> None:
        """Initialize simulation directory structure."""
        # Create directories
        self.synopkg_pkgdest.mkdir(parents=True, exist_ok=True)
        self.syrvis_home.mkdir(parents=True, exist_ok=True)
        self.usr_local_bin.mkdir(parents=True, exist_ok=True)
        self.docker_sock.parent.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        # Create rc.d directory
        (self.root / "usr/local/etc/rc.d").mkdir(parents=True, exist_ok=True)

        # Initialize state files
        self._write_state("docker-status.txt", "running")
        self._write_state("installed-packages.json", "[]")
        self._write_state("docker-group-members.txt", "")

        # Create mock timezone file
        tz_file = self.root / "etc/TZ"
        tz_file.parent.mkdir(parents=True, exist_ok=True)
        tz_file.write_text("UTC\n")

        # Link or create mock docker socket
        real_socket = Path("/var/run/docker.sock")
        if self.docker_sock.is_symlink():
            self.docker_sock.unlink()
        elif self.docker_sock.exists():
            self.docker_sock.unlink()

        if real_socket.exists() and real_socket.is_socket():
            self.docker_sock.symlink_to(real_socket)
        else:
            self.docker_sock.touch()

        # Make mock commands executable
        for cmd in self.bin_dir.glob("*"):
            if cmd.is_file():
                cmd.chmod(0o755)

    def reset(self) -> None:
        """Reset simulation to clean state."""
        # Clear installation directories
        for path in [self.synopkg_pkgdest, self.syrvis_home]:
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True)

        # Clear usr/local/bin
        if self.usr_local_bin.exists():
            shutil.rmtree(self.usr_local_bin)
        self.usr_local_bin.mkdir(parents=True)

        # Clear rc.d
        rc_d = self.root / "usr/local/etc/rc.d"
        if rc_d.exists():
            shutil.rmtree(rc_d)
        rc_d.mkdir(parents=True)

        # Clear tmp
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True)

        # Reset state files
        self._write_state("docker-status.txt", "running")
        self._write_state("installed-packages.json", "[]")
        self._write_state("docker-group-members.txt", "")

        # Clear logs
        for log_file in self.logs_dir.glob("*.log"):
            log_file.unlink()

    def clean(self) -> None:
        """Remove simulation entirely."""
        if self.root.exists():
            shutil.rmtree(self.root)
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        if self.logs_dir.exists():
            shutil.rmtree(self.logs_dir)

    def _write_state(self, filename: str, content: str) -> None:
        """Write a state file."""
        state_file = self.state_dir / filename
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(content)

    def _read_state(self, filename: str) -> str:
        """Read a state file."""
        return (self.state_dir / filename).read_text()

    def get_env(self) -> Dict[str, str]:
        """Get environment variables for simulation."""
        return {
            "DSM_SIM_ACTIVE": "1",
            "DSM_SIM_ROOT": str(self.root),
            "DSM_SIM_STATE": str(self.state_dir),
            "DSM_SIM_LOGS": str(self.logs_dir),
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "SYNOPKG_PKGDEST": str(self.synopkg_pkgdest),
            "SYRVIS_HOME": str(self.syrvis_home),
            "PACKAGE_NAME": "syrviscore",
            # Wizard variables
            "pkgwizard_volume": str(self.root / "volume1"),
            "pkgwizard_network_interface": "en0",
            "pkgwizard_network_subnet": "192.168.1.0/24",
            "pkgwizard_gateway_ip": "192.168.1.1",
            "pkgwizard_traefik_ip": "192.168.1.100",
            "pkgwizard_domain": "test.local",
            "pkgwizard_acme_email": "test@test.local",
            "pkgwizard_cloudflare_token": "",
        }

    @contextmanager
    def activated(self):
        """Context manager to activate simulation environment."""
        if self._is_active:
            yield self
            return

        sim_env = self.get_env()

        # Save original values
        for key in sim_env:
            self._orig_env[key] = os.environ.get(key)

        # Set simulation environment
        os.environ.update(sim_env)
        self._is_active = True

        try:
            yield self
        finally:
            # Restore original environment
            for key, orig_value in self._orig_env.items():
                if orig_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = orig_value
            self._orig_env.clear()
            self._is_active = False

    # === State Management Methods ===

    def set_docker_status(self, running: bool) -> None:
        """Set mock Docker daemon status."""
        self._write_state("docker-status.txt", "running" if running else "stopped")

    def get_docker_status(self) -> bool:
        """Get mock Docker daemon status."""
        return self._read_state("docker-status.txt").strip() == "running"

    def add_group_member(self, username: str, group: str = "docker") -> None:
        """Add user to mock docker group."""
        if group != "docker":
            return
        members = self._read_state("docker-group-members.txt").strip()
        member_list = [m for m in members.split("\n") if m]
        if username not in member_list:
            member_list.append(username)
            self._write_state("docker-group-members.txt", "\n".join(member_list))

    def remove_group_member(self, username: str, group: str = "docker") -> None:
        """Remove user from mock docker group."""
        if group != "docker":
            return
        members = self._read_state("docker-group-members.txt").strip()
        member_list = [m for m in members.split("\n") if m and m != username]
        self._write_state("docker-group-members.txt", "\n".join(member_list))

    def get_group_members(self, group: str = "docker") -> List[str]:
        """Get members of mock docker group."""
        if group != "docker":
            return []
        members = self._read_state("docker-group-members.txt").strip()
        return [m for m in members.split("\n") if m]

    def is_user_in_group(self, username: str, group: str = "docker") -> bool:
        """Check if user is in mock docker group."""
        return username in self.get_group_members(group)

    # === Script Execution Methods ===

    def run_script(
        self,
        script_path: Path,
        check: bool = False,
        **kwargs
    ) -> subprocess.CompletedProcess:
        """
        Run a shell script within simulation environment.

        Args:
            script_path: Path to script to run
            check: If True, raise exception on non-zero exit
            **kwargs: Additional arguments to subprocess.run
        """
        env = {**os.environ, **self.get_env()}
        return subprocess.run(
            [str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            check=check,
            **kwargs
        )

    def run_command(
        self,
        command: List[str],
        check: bool = False,
        **kwargs
    ) -> subprocess.CompletedProcess:
        """
        Run a command within simulation environment.

        Args:
            command: Command and arguments as list
            check: If True, raise exception on non-zero exit
            **kwargs: Additional arguments to subprocess.run
        """
        env = {**os.environ, **self.get_env()}
        return subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            check=check,
            **kwargs
        )

    # === Installation Methods ===

    def install_wheel(self, wheel_path: Path, to_manager: bool = True) -> Path:
        """
        Install a wheel file to the simulation's venv.

        Args:
            wheel_path: Path to the wheel file
            to_manager: If True, install to manager venv; if False, to service venv

        Returns:
            Path to the created venv
        """
        if to_manager:
            venv_path = self.synopkg_pkgdest / "venv"
        else:
            # For service, would need version number
            venv_path = self.syrvis_home / "current/cli/venv"

        # Create venv if needed
        if not venv_path.exists():
            subprocess.run(
                ["python3", "-m", "venv", str(venv_path)],
                check=True,
                capture_output=True
            )

        # Install wheel
        pip_path = venv_path / "bin/pip"
        subprocess.run(
            [str(pip_path), "install", "--no-cache-dir", str(wheel_path)],
            check=True,
            capture_output=True
        )

        return venv_path

    def extract_spk(self, spk_path: Path) -> Path:
        """
        Extract an SPK file to the simulation.

        Args:
            spk_path: Path to the SPK file

        Returns:
            Path to extracted contents
        """
        extract_dir = self.tmp_dir / "spk-extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        # Extract outer tar
        subprocess.run(
            ["tar", "-xf", str(spk_path), "-C", str(extract_dir)],
            check=True,
            capture_output=True
        )

        # Extract package.tgz to SYNOPKG_PKGDEST
        package_tgz = extract_dir / "package.tgz"
        if package_tgz.exists():
            subprocess.run(
                ["tar", "-xzf", str(package_tgz), "-C", str(self.synopkg_pkgdest)],
                check=True,
                capture_output=True
            )

        return extract_dir


# Pytest fixtures
def pytest_dsm_sim():
    """
    Pytest fixture for DSM simulation.

    Usage:
        def test_something(dsm_sim):
            with dsm_sim.activated():
                # run test
    """
    sim = DsmSimulator()
    sim.setup()
    yield sim
    sim.reset()
