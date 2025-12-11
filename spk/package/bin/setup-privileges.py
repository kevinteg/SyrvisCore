#!/usr/bin/env python3
"""
SyrvisCore Privileged Setup Script
Configures Docker permissions and system integration

This script performs operations that require root privileges:
- Creates docker group
- Adds user to docker group
- Sets Docker socket permissions
- Creates startup script for boot persistence
- Creates global CLI symlink
- Updates installation manifest

Safe to run multiple times (idempotent).
"""

import os
import sys
import json
import grp
import pwd
import subprocess
from pathlib import Path
from typing import Tuple, Optional


class SetupError(Exception):
    """Setup operation failed"""
    pass


class PrivilegedSetup:
    """Manages privileged system configuration for SyrvisCore"""
    
    def __init__(self, install_dir: Path, target_user: Optional[str] = None):
        self.install_dir = install_dir
        self.target_user = target_user or self._detect_user()
        self.manifest_path = install_dir / '.syrviscore-manifest.json'
        
        # Configuration
        self.docker_group = 'docker'
        self.docker_socket = Path('/var/run/docker.sock')
        self.startup_script = Path('/usr/local/etc/rc.d/S99syrviscore.sh')
        self.syrvis_symlink = Path('/usr/local/bin/syrvis')
        
    def _detect_user(self) -> str:
        """Detect the user who should own Docker access"""
        # If run with sudo, use the original user
        if sudo_user := os.environ.get('SUDO_USER'):
            return sudo_user
        # Otherwise use current user
        return os.environ.get('USER', 'admin')
    
    def verify_root(self) -> None:
        """Ensure running as root"""
        if os.getuid() != 0:
            raise SetupError(
                "This script must be run with root privileges.\n"
                "Usage: sudo python3 setup-privileges.py"
            )
    
    def verify_user(self) -> None:
        """Ensure target user exists and is not root"""
        if self.target_user == 'root':
            raise SetupError(
                "Target user should not be root.\n"
                "Run with 'sudo' as a regular user, not 'sudo su'."
            )
        try:
            pwd.getpwnam(self.target_user)
        except KeyError:
            raise SetupError(f"User '{self.target_user}' does not exist")
    
    def ensure_docker_group(self) -> Tuple[bool, str]:
        """Create docker group if it doesn't exist"""
        try:
            grp.getgrnam(self.docker_group)
            return False, "Docker group already exists"
        except KeyError:
            try:
                subprocess.run(
                    ['synogroup', '--add', self.docker_group],
                    check=True,
                    capture_output=True,
                    text=True
                )
                return True, "Docker group created"
            except subprocess.CalledProcessError as e:
                raise SetupError(f"Failed to create docker group: {e.stderr}")
    
    def ensure_user_in_group(self) -> Tuple[bool, str]:
        """Add user to docker group if not already member"""
        try:
            docker_group = grp.getgrnam(self.docker_group)
            if self.target_user in docker_group.gr_mem:
                return False, f"{self.target_user} already in docker group"
            
            subprocess.run(
                ['synogroup', '--member', self.docker_group, self.target_user],
                check=True,
                capture_output=True,
                text=True
            )
            return True, f"{self.target_user} added to docker group"
        except KeyError:
            raise SetupError("Docker group not found")
        except subprocess.CalledProcessError as e:
            raise SetupError(f"Failed to add user to group: {e.stderr}")
    
    def ensure_socket_permissions(self) -> Tuple[bool, str]:
        """Set Docker socket to root:docker 660"""
        if not self.docker_socket.exists():
            return False, "Docker socket not found (Docker not installed?)"
        
        if not self.docker_socket.is_socket():
            raise SetupError(f"{self.docker_socket} exists but is not a socket")
        
        stat_info = self.docker_socket.stat()
        docker_gid = grp.getgrnam(self.docker_group).gr_gid
        
        needs_update = (
            stat_info.st_uid != 0 or
            stat_info.st_gid != docker_gid or
            (stat_info.st_mode & 0o777) != 0o660
        )
        
        if needs_update:
            os.chown(self.docker_socket, 0, docker_gid)
            os.chmod(self.docker_socket, 0o660)
            return True, "Socket permissions set (root:docker 660)"
        
        return False, "Socket permissions already correct"
    
    def ensure_startup_script(self) -> Tuple[bool, str]:
        """Install boot-time startup script"""
        script_content = f'''#!/bin/sh
# SyrvisCore Startup Script
# Managed by SyrvisCore - DO NOT EDIT MANUALLY
# Ensures Docker group and socket permissions persist across reboots

DOCKER_GROUP="{self.docker_group}"
DOCKER_SOCKET="{self.docker_socket}"

case "$1" in
    start)
        # Recreate docker group if needed
        synogroup --get "$DOCKER_GROUP" >/dev/null 2>&1 || synogroup --add "$DOCKER_GROUP"
        
        # Reset socket permissions
        if [ -S "$DOCKER_SOCKET" ]; then
            chown root:"$DOCKER_GROUP" "$DOCKER_SOCKET"
            chmod 660 "$DOCKER_SOCKET"
        fi
        ;;
    stop|status)
        # No action needed
        ;;
    *)
        echo "Usage: $0 {{start|stop|status}}"
        exit 1
        ;;
esac

exit 0
'''
        
        if self.startup_script.exists():
            existing = self.startup_script.read_text()
            if existing == script_content:
                return False, "Startup script already installed"
        
        self.startup_script.write_text(script_content)
        self.startup_script.chmod(0o755)
        return True, "Startup script installed"
    
    def ensure_symlink(self) -> Tuple[bool, str]:
        """Create global syrvis command symlink"""
        target = self.install_dir / 'bin' / 'syrvis'
        
        if not target.exists():
            return False, f"CLI wrapper not found at {target}"
        
        if self.syrvis_symlink.is_symlink():
            if self.syrvis_symlink.resolve() == target.resolve():
                return False, "Global symlink already correct"
            self.syrvis_symlink.unlink()
        elif self.syrvis_symlink.exists():
            raise SetupError(f"{self.syrvis_symlink} exists but is not a symlink")
        
        self.syrvis_symlink.symlink_to(target)
        return True, "Global symlink created (/usr/local/bin/syrvis)"
    
    def update_manifest(self) -> Tuple[bool, str]:
        """Mark setup as complete in manifest"""
        if not self.manifest_path.exists():
            raise SetupError(f"Manifest not found: {self.manifest_path}")
        
        manifest = json.loads(self.manifest_path.read_text())
        
        if manifest.get('setup_complete'):
            return False, "Manifest already marked complete"
        
        manifest['setup_complete'] = True
        manifest['setup_date'] = subprocess.run(
            ['date', '-u', '+%Y-%m-%dT%H:%M:%SZ'],
            capture_output=True,
            text=True
        ).stdout.strip()
        
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        return True, "Manifest updated (setup_complete: true)"
    
    def run(self) -> int:
        """Execute all setup steps"""
        print("=" * 70)
        print("  SyrvisCore Privileged Setup")
        print("=" * 70)
        print(f"Installation: {self.install_dir}")
        print(f"Target User:  {self.target_user}")
        print(f"Running as:   {pwd.getpwuid(os.getuid()).pw_name} (UID {os.getuid()})")
        print()
        
        try:
            # Pre-flight checks
            self.verify_root()
            self.verify_user()
            
            # Setup steps
            steps = [
                ("Creating docker group", self.ensure_docker_group),
                ("Adding user to docker group", self.ensure_user_in_group),
                ("Setting Docker socket permissions", self.ensure_socket_permissions),
                ("Installing startup script", self.ensure_startup_script),
                ("Creating global CLI symlink", self.ensure_symlink),
                ("Updating manifest", self.update_manifest),
            ]
            
            for i, (desc, func) in enumerate(steps, 1):
                print(f"[{i}/{len(steps)}] {desc}...", end=" ", flush=True)
                try:
                    changed, message = func()
                    if changed:
                        print(f"✓ {message}")
                    else:
                        print(f"✓ {message}")
                except SetupError as e:
                    print(f"✗ {e}")
                    return 1
            
            print()
            print("=" * 70)
            print("  Setup Complete!")
            print("=" * 70)
            print()
            print("IMPORTANT: Group membership requires logout/login")
            print()
            print("Options:")
            print("  1. Log out and log back in (recommended)")
            print("  2. Run: newgrp docker (current session only)")
            print()
            print("Verify Docker access:")
            print("  docker ps")
            print()
            print("Run SyrvisCore diagnostics:")
            print(f"  {self.install_dir}/bin/syrvis doctor")
            print()
            return 0
            
        except SetupError as e:
            print()
            print(f"ERROR: {e}")
            return 1
        except Exception as e:
            print()
            print(f"UNEXPECTED ERROR: {e}")
            import traceback
            traceback.print_exc()
            return 1


def main():
    """Main entry point"""
    # Detect install directory from script location
    script_path = Path(__file__).resolve()
    install_dir = script_path.parent.parent
    
    setup = PrivilegedSetup(install_dir)
    return setup.run()


if __name__ == '__main__':
    sys.exit(main())
