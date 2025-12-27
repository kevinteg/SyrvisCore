"""
Validation library for SyrvisCore.

Provides reusable validation functions for:
- Installation checks (directories, manifest, venv)
- Docker access (group, socket, daemon)
- Configuration (.env file, required variables)
- Network (macvlan shim, routes, DNS, certificates)
- Endpoints (HTTP health, backend services)

Used by:
- syrvis doctor: Comprehensive diagnostics
- syrvis setup: Pre-flight validation
- syrvis start: Service readiness checks
"""

import os
import socket
import ssl
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from . import paths
from . import privileged_ops


# =============================================================================
# Result Types
# =============================================================================

@dataclass
class CheckResult:
    """Result of a validation check."""
    name: str
    passed: bool
    message: str
    details: Optional[str] = None
    fixable: bool = False
    fix_action: Optional[str] = None


@dataclass
class ValidationReport:
    """Collection of check results."""
    category: str
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """All checks passed."""
        return all(c.passed for c in self.checks)

    @property
    def issues(self) -> List[CheckResult]:
        """Checks that failed."""
        return [c for c in self.checks if not c.passed]

    @property
    def fixable_issues(self) -> List[CheckResult]:
        """Failed checks that can be auto-fixed."""
        return [c for c in self.checks if not c.passed and c.fixable]


# =============================================================================
# DNS Validation
# =============================================================================

def dns_lookup(hostname: str, resolver: str = None) -> Tuple[bool, str]:
    """
    Perform DNS lookup for a hostname.

    Args:
        hostname: The hostname to look up
        resolver: Optional DNS resolver IP (e.g., "8.8.8.8")

    Returns:
        Tuple of (success, ip_or_error_message)
    """
    try:
        if resolver:
            # Use nslookup for specific resolver
            result = subprocess.run(
                ["nslookup", hostname, resolver],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                # Parse nslookup output for Address line
                for line in result.stdout.split('\n'):
                    if 'Address:' in line and resolver not in line:
                        ip = line.split('Address:')[1].strip().split('#')[0].strip()
                        if ip and not ip.startswith('127.'):
                            return True, ip
                return False, "NXDOMAIN"
            return False, "Lookup failed"
        else:
            # Use system resolver
            ip = socket.gethostbyname(hostname)
            return True, ip
    except socket.gaierror:
        return False, "NXDOMAIN"
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def validate_dns(domain: str, expected_ip: str = None) -> Dict:
    """
    Validate DNS for a domain against local and public resolvers.

    For split-horizon DNS setups (common with Traefik + Cloudflare):
    - Local DNS should resolve to TRAEFIK_IP (internal access)
    - Public DNS may resolve to external IP (via Cloudflare tunnel)
    - This is CORRECT, not an error!

    Returns dict with 'local', 'public' results and status flags.
    """
    local_ok, local_ip = dns_lookup(domain)
    public_ok, public_ip = dns_lookup(domain, "8.8.8.8")

    result = {
        "domain": domain,
        "local": {"ok": local_ok, "ip": local_ip},
        "public": {"ok": public_ok, "ip": public_ip},
        "consistent": local_ok and public_ok and local_ip == public_ip,
    }

    if expected_ip:
        result["expected_ip"] = expected_ip
        # For split-horizon: local should match expected_ip (Traefik)
        # Public can be different (Cloudflare tunnel) - that's OK
        result["local_correct"] = local_ok and local_ip == expected_ip
        # Split-horizon is valid: local matches expected AND public resolves (to anything)
        result["split_horizon_ok"] = result["local_correct"] and public_ok
        # Overall correctness: either consistent match OR valid split-horizon
        result["correct"] = (result["consistent"] and local_ip == expected_ip) or result["split_horizon_ok"]

    return result


# =============================================================================
# Certificate Validation
# =============================================================================

def check_certificate(hostname: str, port: int = 443) -> Dict:
    """
    Check SSL certificate for a hostname.

    Returns dict with certificate details and validation status.
    """
    result = {
        "hostname": hostname,
        "port": port,
        "valid": False,
        "issuer": None,
        "subject": None,
        "expires": None,
        "days_remaining": None,
        "is_letsencrypt": False,
        "is_self_signed": False,
        "is_traefik_default": False,
        "error": None,
    }

    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # We want to see any cert, even invalid

        with socket.create_connection((hostname, port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert(binary_form=True)

                # Parse certificate using openssl
                proc = subprocess.run(
                    ["openssl", "x509", "-inform", "DER", "-noout",
                     "-issuer", "-subject", "-dates"],
                    input=cert,
                    capture_output=True,
                    timeout=10
                )

                if proc.returncode == 0:
                    output = proc.stdout.decode()

                    for line in output.split('\n'):
                        if line.startswith('issuer='):
                            result["issuer"] = line.split('=', 1)[1].strip()
                        elif line.startswith('subject='):
                            result["subject"] = line.split('=', 1)[1].strip()
                        elif line.startswith('notAfter='):
                            date_str = line.split('=', 1)[1].strip()
                            try:
                                # Parse date like "Mar 26 17:28:22 2026 GMT"
                                expires = datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
                                result["expires"] = expires.isoformat()
                                result["days_remaining"] = (expires - datetime.utcnow()).days
                            except:
                                result["expires"] = date_str

                    # Determine certificate type
                    issuer = result.get("issuer", "")
                    subject = result.get("subject", "")

                    if "Let's Encrypt" in issuer or "R3" in issuer or "R10" in issuer or "R11" in issuer:
                        result["is_letsencrypt"] = True
                        result["valid"] = True
                    elif "TRAEFIK DEFAULT CERT" in issuer or "TRAEFIK DEFAULT CERT" in subject:
                        result["is_traefik_default"] = True
                    elif issuer == subject:
                        result["is_self_signed"] = True
                    elif "Synology" in issuer or "Synology" in subject:
                        result["is_self_signed"] = True
                    else:
                        # Some other valid CA
                        result["valid"] = True

    except socket.timeout:
        result["error"] = "Connection timeout"
    except ConnectionRefusedError:
        result["error"] = "Connection refused"
    except socket.gaierror:
        result["error"] = "DNS resolution failed"
    except Exception as e:
        result["error"] = str(e)

    return result


# =============================================================================
# Endpoint Health Checks
# =============================================================================

def check_http_endpoint(url: str, timeout: int = 10) -> Dict:
    """
    Check if an HTTP endpoint is reachable and responding.
    """
    result = {
        "url": url,
        "reachable": False,
        "status_code": None,
        "redirect": None,
        "error": None,
    }

    try:
        # Use curl for more reliable HTTP checking
        proc = subprocess.run(
            ["curl", "-sI", "-o", "/dev/null", "-w", "%{http_code}|%{redirect_url}",
             "-k", "--connect-timeout", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5
        )

        if proc.returncode == 0:
            parts = proc.stdout.strip().split('|')
            status = int(parts[0]) if parts[0].isdigit() else 0
            redirect = parts[1] if len(parts) > 1 and parts[1] else None

            result["status_code"] = status
            result["redirect"] = redirect
            result["reachable"] = status > 0 and status < 500

    except subprocess.TimeoutExpired:
        result["error"] = "Timeout"
    except Exception as e:
        result["error"] = str(e)

    return result


def check_tcp_port(host: str, port: int, timeout: int = 5) -> Dict:
    """
    Check if a TCP port is reachable.
    """
    result = {
        "host": host,
        "port": port,
        "reachable": False,
        "error": None,
    }

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            result["reachable"] = True
    except socket.timeout:
        result["error"] = "Connection timeout"
    except ConnectionRefusedError:
        result["error"] = "Connection refused"
    except Exception as e:
        result["error"] = str(e)

    return result


# =============================================================================
# Environment Parsing
# =============================================================================

def parse_env_file(env_path: Path) -> Dict[str, str]:
    """
    Parse a .env file into a dictionary.

    Args:
        env_path: Path to .env file

    Returns:
        Dictionary of environment variables
    """
    env_vars = {}

    if not env_path.exists():
        return env_vars

    try:
        for line in env_path.read_text().split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env_vars[key.strip()] = value.strip()
    except Exception:
        pass

    return env_vars


# =============================================================================
# Installation Validation
# =============================================================================

class InstallationValidator:
    """Validates SyrvisCore installation state."""

    def __init__(self):
        self._syrvis_home: Optional[Path] = None
        self._manifest: Optional[Dict] = None

    @property
    def syrvis_home(self) -> Optional[Path]:
        """Get SYRVIS_HOME path, caching result."""
        if self._syrvis_home is None:
            try:
                self._syrvis_home = paths.get_syrvis_home()
            except paths.SyrvisHomeError:
                pass
        return self._syrvis_home

    @property
    def manifest(self) -> Optional[Dict]:
        """Get manifest, caching result."""
        if self._manifest is None and self.syrvis_home:
            try:
                self._manifest = paths.get_manifest()
            except Exception:
                pass
        return self._manifest

    def check_syrvis_home(self) -> CheckResult:
        """Check if SYRVIS_HOME is found."""
        if self.syrvis_home:
            return CheckResult(
                name="SYRVIS_HOME",
                passed=True,
                message=str(self.syrvis_home)
            )
        return CheckResult(
            name="SYRVIS_HOME",
            passed=False,
            message="Not found",
            details="Run syrvisctl install to set up the service"
        )

    def check_manifest(self) -> CheckResult:
        """Check if manifest exists and is valid."""
        if not self.syrvis_home:
            return CheckResult(
                name="Manifest",
                passed=False,
                message="Cannot check - SYRVIS_HOME not found"
            )

        manifest_path = self.syrvis_home / ".syrviscore-manifest.json"

        if not manifest_path.exists():
            return CheckResult(
                name="Manifest",
                passed=False,
                message="Missing",
                details=f"Expected at {manifest_path}"
            )

        try:
            manifest = paths.get_manifest()
            return CheckResult(
                name="Manifest",
                passed=True,
                message="Valid",
                details=f"Schema v{manifest.get('schema_version', 'unknown')}"
            )
        except PermissionError:
            return CheckResult(
                name="Manifest",
                passed=False,
                message="Permission denied",
                details=f"Run: sudo chmod 644 {manifest_path}",
                fixable=True,
                fix_action="manifest_perms"
            )
        except Exception as e:
            return CheckResult(
                name="Manifest",
                passed=False,
                message=f"Error: {e}"
            )

    def check_setup_complete(self) -> CheckResult:
        """Check if setup has been completed."""
        if not self.manifest:
            return CheckResult(
                name="Setup",
                passed=False,
                message="Cannot check - manifest not available"
            )

        if self.manifest.get('setup_complete', False):
            setup_date = self.manifest.get('setup_date', 'unknown')
            return CheckResult(
                name="Setup",
                passed=True,
                message=f"Completed ({setup_date})"
            )

        return CheckResult(
            name="Setup",
            passed=False,
            message="Not completed",
            details="Run: syrvis setup"
        )

    def check_venv(self) -> CheckResult:
        """Check if Python venv exists."""
        if not self.syrvis_home:
            return CheckResult(
                name="Python venv",
                passed=False,
                message="Cannot check - SYRVIS_HOME not found"
            )

        # Check versioned structure: install_dir/current/cli/venv
        venv_paths = [
            self.syrvis_home / "current" / "cli" / "venv",
            self.syrvis_home / "cli" / "venv",  # Legacy
        ]

        # Also check versions directory
        versions_dir = self.syrvis_home / "versions"
        if versions_dir.exists():
            for version_dir in versions_dir.iterdir():
                venv_paths.append(version_dir / "cli" / "venv")

        for venv_path in venv_paths:
            if venv_path.exists():
                return CheckResult(
                    name="Python venv",
                    passed=True,
                    message="Exists",
                    details=str(venv_path)
                )

        return CheckResult(
            name="Python venv",
            passed=False,
            message="Not found"
        )

    def validate(self) -> ValidationReport:
        """Run all installation checks."""
        report = ValidationReport(category="Installation")
        report.checks.append(self.check_syrvis_home())

        if self.syrvis_home:
            report.checks.append(self.check_manifest())
            report.checks.append(self.check_setup_complete())
            report.checks.append(self.check_venv())

        return report


# =============================================================================
# Docker Validation
# =============================================================================

class DockerValidator:
    """Validates Docker access and configuration."""

    def __init__(self, username: str = None):
        self.username = username or os.environ.get(
            'USER', os.environ.get('SUDO_USER', 'unknown')
        )

    def check_docker_group(self) -> CheckResult:
        """Check if docker group exists."""
        exists, gid = privileged_ops.get_docker_group_info()

        if exists:
            return CheckResult(
                name="Docker group",
                passed=True,
                message=f"Exists (GID: {gid})"
            )

        return CheckResult(
            name="Docker group",
            passed=False,
            message="Missing",
            fixable=True,
            fix_action="docker_group"
        )

    def check_user_in_group(self) -> CheckResult:
        """Check if current user is in docker group."""
        exists, _ = privileged_ops.get_docker_group_info()

        if not exists:
            return CheckResult(
                name="User in docker group",
                passed=False,
                message="Docker group doesn't exist"
            )

        if self.username == 'unknown':
            return CheckResult(
                name="User in docker group",
                passed=False,
                message="Cannot determine username"
            )

        in_group = privileged_ops.is_user_in_group(self.username, 'docker')

        if in_group:
            return CheckResult(
                name="User in docker group",
                passed=True,
                message=f"'{self.username}' is member"
            )

        return CheckResult(
            name="User in docker group",
            passed=False,
            message=f"'{self.username}' not in group",
            fixable=True,
            fix_action=f"user_group:{self.username}"
        )

    def check_socket_exists(self) -> CheckResult:
        """Check if Docker socket exists."""
        exists, msg = privileged_ops.verify_docker_socket_exists()

        if exists:
            return CheckResult(
                name="Docker socket",
                passed=True,
                message="Exists"
            )

        return CheckResult(
            name="Docker socket",
            passed=False,
            message="Missing",
            details="Is Docker installed and running?"
        )

    def check_socket_permissions(self) -> CheckResult:
        """Check Docker socket permissions."""
        exists, _ = privileged_ops.verify_docker_socket_exists()

        if not exists:
            return CheckResult(
                name="Socket permissions",
                passed=False,
                message="Socket doesn't exist"
            )

        owner, group, perms = privileged_ops.get_docker_socket_permissions()

        if group == 'docker' and perms == '660':
            return CheckResult(
                name="Socket permissions",
                passed=True,
                message=f"{owner}:{group} {perms}"
            )

        return CheckResult(
            name="Socket permissions",
            passed=False,
            message=f"{owner}:{group} {perms}",
            details="Expected: root:docker 660",
            fixable=True,
            fix_action="socket_perms"
        )

    def check_daemon_accessible(self) -> CheckResult:
        """Check if Docker daemon is accessible."""
        is_root = os.getuid() == 0
        ok, msg = privileged_ops.verify_docker_accessible(
            self.username if not is_root else None
        )

        if ok:
            return CheckResult(
                name="Docker daemon",
                passed=True,
                message="Accessible"
            )

        details = None
        if "logout" in msg.lower():
            details = "Logout/login required for group changes"

        return CheckResult(
            name="Docker daemon",
            passed=False,
            message="Not accessible",
            details=details
        )

    def validate(self) -> ValidationReport:
        """Run all Docker checks."""
        report = ValidationReport(category="Docker Access")
        report.checks.append(self.check_docker_group())
        report.checks.append(self.check_user_in_group())
        report.checks.append(self.check_socket_exists())
        report.checks.append(self.check_socket_permissions())
        report.checks.append(self.check_daemon_accessible())
        return report


# =============================================================================
# Configuration Validation
# =============================================================================

class ConfigurationValidator:
    """Validates SyrvisCore configuration."""

    REQUIRED_VARS = [
        'DOMAIN',
        'ACME_EMAIL',
        'NETWORK_SUBNET',
        'NETWORK_GATEWAY',
        'TRAEFIK_IP',
    ]

    def __init__(self):
        self._env_path: Optional[Path] = None
        self._env_vars: Optional[Dict[str, str]] = None

    @property
    def env_path(self) -> Optional[Path]:
        """Get .env path."""
        if self._env_path is None:
            try:
                self._env_path = paths.get_env_path()
            except Exception:
                pass
        return self._env_path

    @property
    def env_vars(self) -> Dict[str, str]:
        """Get parsed environment variables."""
        if self._env_vars is None:
            if self.env_path and self.env_path.exists():
                self._env_vars = parse_env_file(self.env_path)
            else:
                self._env_vars = {}
        return self._env_vars

    def check_env_exists(self) -> CheckResult:
        """Check if .env file exists."""
        if self.env_path and self.env_path.exists():
            return CheckResult(
                name=".env file",
                passed=True,
                message="Exists"
            )

        return CheckResult(
            name=".env file",
            passed=False,
            message="Missing",
            details="Run: syrvis setup"
        )

    def check_required_vars(self) -> CheckResult:
        """Check if all required variables are set."""
        if not self.env_vars:
            return CheckResult(
                name="Required config",
                passed=False,
                message="Cannot check - .env not available"
            )

        missing = []
        for var in self.REQUIRED_VARS:
            if not self.env_vars.get(var):
                missing.append(var)

        if not missing:
            return CheckResult(
                name="Required config",
                passed=True,
                message="All values set"
            )

        return CheckResult(
            name="Required config",
            passed=False,
            message=f"Missing: {', '.join(missing)}",
            details=f"Edit {self.env_path}"
        )

    def get_value(self, key: str, default: str = "") -> str:
        """Get a configuration value."""
        return self.env_vars.get(key, default)

    def validate(self) -> ValidationReport:
        """Run all configuration checks."""
        report = ValidationReport(category="Configuration")
        report.checks.append(self.check_env_exists())

        if self.env_path and self.env_path.exists():
            report.checks.append(self.check_required_vars())

        return report


# =============================================================================
# Network Validation
# =============================================================================

class NetworkValidator:
    """Validates network configuration (macvlan, shim, routes)."""

    def __init__(self, config: ConfigurationValidator = None):
        self.config = config or ConfigurationValidator()

    @property
    def traefik_ip(self) -> str:
        """Get Traefik IP from config."""
        return self.config.get_value('TRAEFIK_IP', '')

    @property
    def shim_ip(self) -> str:
        """Get or calculate shim IP."""
        shim_ip = self.config.get_value('SHIM_IP', '')

        if not shim_ip and self.traefik_ip:
            # Calculate from traefik_ip + 1
            try:
                parts = self.traefik_ip.split('.')
                shim_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.{int(parts[3]) + 1}"
            except (IndexError, ValueError):
                pass

        return shim_ip

    def check_shim_interface(self) -> CheckResult:
        """Check if macvlan shim interface exists."""
        if not self.traefik_ip:
            return CheckResult(
                name="Shim interface",
                passed=False,
                message="Skipped - TRAEFIK_IP not configured"
            )

        try:
            result = subprocess.run(
                ["ip", "link", "show", "syrvis-shim"],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0:
                return CheckResult(
                    name="Shim interface",
                    passed=True,
                    message="syrvis-shim exists"
                )

            return CheckResult(
                name="Shim interface",
                passed=False,
                message="syrvis-shim missing",
                details="Run: syrvis start"
            )

        except FileNotFoundError:
            return CheckResult(
                name="Shim interface",
                passed=False,
                message="Skipped - 'ip' command not available"
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="Shim interface",
                passed=False,
                message="Check timed out"
            )

    def check_shim_ip(self) -> CheckResult:
        """Check if shim interface has correct IP."""
        if not self.shim_ip:
            return CheckResult(
                name="Shim IP",
                passed=False,
                message="Skipped - shim IP not configured"
            )

        try:
            result = subprocess.run(
                ["ip", "addr", "show", "syrvis-shim"],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0 and self.shim_ip in result.stdout:
                return CheckResult(
                    name="Shim IP",
                    passed=True,
                    message=self.shim_ip
                )

            return CheckResult(
                name="Shim IP",
                passed=False,
                message=f"Expected {self.shim_ip}",
                details="Run: syrvis start"
            )

        except FileNotFoundError:
            return CheckResult(
                name="Shim IP",
                passed=False,
                message="Skipped - 'ip' command not available"
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="Shim IP",
                passed=False,
                message="Check timed out"
            )

    def check_route(self) -> CheckResult:
        """Check if route to Traefik exists."""
        if not self.traefik_ip:
            return CheckResult(
                name="Route to Traefik",
                passed=False,
                message="Skipped - TRAEFIK_IP not configured"
            )

        try:
            result = subprocess.run(
                ["ip", "route", "show", f"{self.traefik_ip}/32"],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0 and "syrvis-shim" in result.stdout:
                return CheckResult(
                    name="Route to Traefik",
                    passed=True,
                    message=self.traefik_ip
                )

            return CheckResult(
                name="Route to Traefik",
                passed=False,
                message="Missing",
                details="Run: syrvis start"
            )

        except FileNotFoundError:
            return CheckResult(
                name="Route to Traefik",
                passed=False,
                message="Skipped - 'ip' command not available"
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="Route to Traefik",
                passed=False,
                message="Check timed out"
            )

    def validate(self) -> ValidationReport:
        """Run all network checks."""
        report = ValidationReport(category="Macvlan Configuration")

        if self.traefik_ip:
            report.checks.append(self.check_shim_interface())
            report.checks.append(self.check_shim_ip())
            report.checks.append(self.check_route())

        return report


# =============================================================================
# Endpoint Discovery
# =============================================================================

def get_configured_endpoints(config: ConfigurationValidator = None) -> List[Dict]:
    """
    Get list of configured endpoints from .env file.

    Returns list of endpoint dictionaries with:
    - name: Display name
    - subdomain: Subdomain part
    - domain: Full domain
    - expected_ip: Expected IP (Traefik IP)
    - backend_host: Backend service host
    - backend_port: Backend service port
    - expected_status: Expected HTTP status codes
    """
    config = config or ConfigurationValidator()
    endpoints = []

    domain = config.get_value('DOMAIN')
    traefik_ip = config.get_value('TRAEFIK_IP')
    nas_ip = config.get_value('NAS_IP')

    if not domain:
        return endpoints

    # Core services (always present)
    endpoints.append({
        "name": "Traefik Dashboard",
        "subdomain": "traefik",
        "domain": f"traefik.{domain}",
        "expected_ip": traefik_ip,
        "backend_host": None,
        "backend_port": None,
        # Traefik API returns 405 for HEAD requests - this is expected
        "expected_status": [200, 401, 405],
    })

    endpoints.append({
        "name": "Portainer",
        "subdomain": "portainer",
        "domain": f"portainer.{domain}",
        "expected_ip": traefik_ip,
        "backend_host": None,
        "backend_port": None,
        "expected_status": [200, 301, 302, 303, 307, 308],
    })

    # Synology services
    synology_services = {
        "SYNOLOGY_DSM_ENABLED": ("DSM Portal", "dsm", 5001, [200, 302]),
        "SYNOLOGY_PHOTOS_ENABLED": ("Photos", "photos", 5001, [200, 302]),
        "SYNOLOGY_DRIVE_ENABLED": ("Synology Drive", "drive", 6690, [200, 302, 400]),
        "SYNOLOGY_AUDIO_ENABLED": ("Audio Station", "audio", 5001, [200, 302]),
        "SYNOLOGY_VIDEO_ENABLED": ("Video Station", "video", 5001, [200, 302]),
    }

    for env_key, (name, subdomain, port, expected_status) in synology_services.items():
        if config.get_value(env_key, '').lower() in ('true', '1', 'yes'):
            endpoints.append({
                "name": name,
                "subdomain": subdomain,
                "domain": f"{subdomain}.{domain}",
                "expected_ip": traefik_ip,
                "backend_host": nas_ip,
                "backend_port": port,
                "expected_status": expected_status,
            })

    return endpoints


# =============================================================================
# System Integration Validation
# =============================================================================

class SystemValidator:
    """Validates system integration (symlinks, startup scripts)."""

    def __init__(self, install_dir: Path = None, username: str = None):
        self.install_dir = install_dir
        self.username = username or os.environ.get(
            'USER', os.environ.get('SUDO_USER', 'unknown')
        )

    def check_global_command(self) -> CheckResult:
        """Check if global syrvis command exists."""
        symlink_path = Path('/usr/local/bin/syrvis')

        if symlink_path.exists() and symlink_path.is_symlink():
            target = os.readlink(str(symlink_path))
            return CheckResult(
                name="Global command",
                passed=True,
                message=f"{symlink_path} → {target}"
            )

        return CheckResult(
            name="Global command",
            passed=False,
            message="syrvis not in PATH",
            fixable=True,
            fix_action="symlink"
        )

    def check_startup_script(self) -> CheckResult:
        """Check if startup script exists."""
        if not self.install_dir:
            return CheckResult(
                name="Startup script",
                passed=False,
                message="Cannot check - install dir unknown"
            )

        startup_script = self.install_dir / "bin" / "syrvis-startup.sh"

        if startup_script.exists():
            return CheckResult(
                name="Startup script",
                passed=True,
                message=str(startup_script)
            )

        return CheckResult(
            name="Startup script",
            passed=False,
            message="Missing",
            fixable=True,
            fix_action=f"startup:{self.username}"
        )

    def check_boot_script(self) -> CheckResult:
        """Check if boot script exists in /usr/local/etc/rc.d/."""
        boot_script = Path("/usr/local/etc/rc.d/S99syrviscore.sh")

        if boot_script.exists():
            return CheckResult(
                name="Boot script",
                passed=True,
                message=str(boot_script),
                details="Ensures macvlan shim is created on reboot"
            )

        return CheckResult(
            name="Boot script",
            passed=False,
            message="Missing - services won't auto-start after reboot",
            details="Run: sudo syrvis setup",
            fixable=True,
            fix_action="boot_script"
        )

    def validate(self) -> ValidationReport:
        """Run all system integration checks."""
        report = ValidationReport(category="System Integration")
        report.checks.append(self.check_global_command())

        if self.install_dir:
            report.checks.append(self.check_startup_script())
            report.checks.append(self.check_boot_script())

        return report


# =============================================================================
# Convenience Functions
# =============================================================================

def validate_installation() -> ValidationReport:
    """Quick validation of installation state."""
    return InstallationValidator().validate()


def validate_docker() -> ValidationReport:
    """Quick validation of Docker access."""
    return DockerValidator().validate()


def validate_configuration() -> ValidationReport:
    """Quick validation of configuration."""
    return ConfigurationValidator().validate()


def validate_all() -> List[ValidationReport]:
    """Run all validations and return list of reports."""
    install_validator = InstallationValidator()
    config_validator = ConfigurationValidator()

    reports = [
        install_validator.validate(),
        DockerValidator().validate(),
        config_validator.validate(),
    ]

    if install_validator.syrvis_home:
        reports.append(SystemValidator(install_validator.syrvis_home).validate())
        reports.append(NetworkValidator(config_validator).validate())

    return reports


def is_ready_to_start() -> Tuple[bool, List[str]]:
    """
    Check if the system is ready to start services.

    Returns:
        Tuple of (ready, list_of_issues)
    """
    issues = []

    # Check installation
    install = InstallationValidator()
    if not install.syrvis_home:
        issues.append("SYRVIS_HOME not found - run: syrvisctl install")
        return False, issues

    if not install.manifest:
        issues.append("Manifest missing - run: syrvisctl install")
        return False, issues

    # Check Docker
    docker = DockerValidator()
    if not docker.check_daemon_accessible().passed:
        issues.append("Docker not accessible")

    # Check configuration
    config = ConfigurationValidator()
    if not config.check_env_exists().passed:
        issues.append(".env missing - run: syrvis setup")
    elif not config.check_required_vars().passed:
        issues.append("Configuration incomplete - run: syrvis setup")

    return len(issues) == 0, issues
