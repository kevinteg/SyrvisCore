"""Doctor command for SyrvisCore - diagnose and fix installation issues."""

import click
import sys
import os
import socket
import subprocess
import ssl
from pathlib import Path
from datetime import datetime
from typing import Tuple, Optional, List, Dict

from . import privileged_ops
from . import paths


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


def validate_dns_for_domain(domain: str, expected_ip: str = None) -> Dict:
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


def check_backend_service(host: str, port: int, protocol: str = "https") -> Dict:
    """
    Check if a backend service is reachable.
    """
    result = {
        "host": host,
        "port": port,
        "protocol": protocol,
        "reachable": False,
        "error": None,
    }

    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            result["reachable"] = True
    except socket.timeout:
        result["error"] = "Connection timeout"
    except ConnectionRefusedError:
        result["error"] = "Connection refused"
    except Exception as e:
        result["error"] = str(e)

    return result


# =============================================================================
# Endpoint Discovery
# =============================================================================

def get_configured_endpoints() -> List[Dict]:
    """
    Get list of configured endpoints from .env file.
    """
    endpoints = []

    try:
        env_path = paths.get_env_path()
        if not env_path.exists():
            return endpoints

        env_content = env_path.read_text()
        env_vars = {}

        for line in env_content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env_vars[key.strip()] = value.strip()

        domain = env_vars.get('DOMAIN', '')
        traefik_ip = env_vars.get('TRAEFIK_IP', '')
        nas_ip = env_vars.get('NAS_IP', '')

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

        # Synology services: (name, subdomain, port, expected_status_codes)
        synology_services = {
            "SYNOLOGY_DSM_ENABLED": ("DSM Portal", "dsm", 5001, [200, 302]),
            "SYNOLOGY_PHOTOS_ENABLED": ("Synology Photos", "photos", 5001, [200, 302]),
            "SYNOLOGY_PHOTOSTATION_ENABLED": ("Photo Station", "photostation", 5001, [200, 302]),
            "SYNOLOGY_DRIVE_ENABLED": ("Synology Drive", "drive", 6690, [200, 302, 400]),
            "SYNOLOGY_AUDIO_ENABLED": ("Audio Station", "audio", 5001, [200, 302]),
            "SYNOLOGY_VIDEO_ENABLED": ("Video Station", "video", 5001, [200, 302]),
        }

        for env_key, (name, subdomain, port, expected_status) in synology_services.items():
            if env_vars.get(env_key, '').lower() in ('true', '1', 'yes'):
                endpoints.append({
                    "name": name,
                    "subdomain": subdomain,
                    "domain": f"{subdomain}.{domain}",
                    "expected_ip": traefik_ip,
                    "backend_host": nas_ip,
                    "backend_port": port,
                    "expected_status": expected_status,
                })

    except Exception:
        pass

    return endpoints


@click.command()
@click.option('--fix', is_flag=True, help='Attempt to fix issues (requires root)')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed information')
@click.option('--network', '-n', is_flag=True, help='Run network checks only (DNS, certs, endpoints)')
def doctor(fix, verbose, network):
    """Verify SyrvisCore installation and diagnose issues."""

    is_root = os.getuid() == 0

    click.echo("=" * 70)
    if network:
        click.echo("SyrvisCore Network Diagnostics")
    else:
        click.echo("SyrvisCore Installation Diagnostics")
    click.echo("=" * 70)
    click.echo()

    if fix and not is_root:
        click.echo("Error: --fix requires root privileges", err=True)
        click.echo("Run with: sudo syrvis doctor --fix")
        sys.exit(1)
    
    issues = []
    fixable_issues = []
    install_dir = None

    # Always try to find install_dir for endpoint discovery
    try:
        install_dir = paths.get_syrvis_home()
    except paths.SyrvisHomeError:
        pass

    # Skip installation checks if --network flag is set
    if not network:
        # Check 1: SYRVIS_HOME detection
        click.echo("Installation")
        click.echo("-" * 70)

        if install_dir:
            click.echo(f"  ✓ SYRVIS_HOME: {install_dir}")
        else:
            click.echo(f"  ✗ SYRVIS_HOME: Not found")
            issues.append("Cannot locate installation directory")
    
    if install_dir and not network:
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
        except PermissionError:
            click.echo(f"  ⚠ Manifest not readable (permission denied)")
            click.echo(f"     Fix with: sudo chmod 644 {install_dir}/.syrviscore-manifest.json")
            # Don't add to issues - it's a minor problem
        except Exception as e:
            click.echo(f"  ✗ Manifest error: {e}")
            issues.append(f"Manifest error: {e}")

        # Check 4: Python venv (check versioned structure)
        venv_found = False
        venv_path = None

        # Try versioned structure first: install_dir/current/cli/venv
        current_venv = install_dir / "current" / "cli" / "venv"
        if current_venv.exists():
            venv_found = True
            venv_path = current_venv
        else:
            # Fallback: check versions directory
            versions_dir = install_dir / "versions"
            if versions_dir.exists():
                for version_dir in versions_dir.iterdir():
                    candidate = version_dir / "cli" / "venv"
                    if candidate.exists():
                        venv_found = True
                        venv_path = candidate
                        break

            # Legacy fallback: direct cli/venv
            if not venv_found:
                legacy_venv = install_dir / "cli" / "venv"
                if legacy_venv.exists():
                    venv_found = True
                    venv_path = legacy_venv

        if venv_found:
            click.echo(f"  ✓ Python venv exists")
            if verbose and venv_path:
                click.echo(f"     Path: {venv_path}")
        else:
            click.echo(f"  ⚠ Python venv missing (optional)")
            # Don't add to issues - venv is optional if syrvis command works

        click.echo()

    # Check 5-9: Docker Access (skip if --network)
    if not network:
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

    # ==========================================================================
    # Macvlan Configuration
    # ==========================================================================
    # Check macvlan shim interface (required for host-to-container communication)
    try:
        env_path = paths.get_env_path()
        if env_path.exists():
            env_content = env_path.read_text()
            env_vars = {}
            for line in env_content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()

            traefik_ip = env_vars.get('TRAEFIK_IP', '')
            shim_ip = env_vars.get('SHIM_IP', '')

            # Calculate shim IP for backwards compatibility
            if traefik_ip and not shim_ip:
                try:
                    parts = traefik_ip.split('.')
                    shim_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.{int(parts[3]) + 1}"
                except (IndexError, ValueError):
                    pass

            if traefik_ip:
                click.echo("Macvlan Configuration")
                click.echo("-" * 70)

                # Check if shim interface exists
                shim_exists = False
                shim_has_ip = False
                route_exists = False

                try:
                    # Check interface exists
                    result = subprocess.run(
                        ["ip", "link", "show", "syrvis-shim"],
                        capture_output=True, text=True, timeout=5
                    )
                    shim_exists = result.returncode == 0

                    if shim_exists:
                        # Check interface has correct IP
                        result = subprocess.run(
                            ["ip", "addr", "show", "syrvis-shim"],
                            capture_output=True, text=True, timeout=5
                        )
                        if result.returncode == 0 and shim_ip in result.stdout:
                            shim_has_ip = True

                        # Check route to traefik IP exists
                        result = subprocess.run(
                            ["ip", "route", "show", f"{traefik_ip}/32"],
                            capture_output=True, text=True, timeout=5
                        )
                        if result.returncode == 0 and "syrvis-shim" in result.stdout:
                            route_exists = True

                except subprocess.TimeoutExpired:
                    pass
                except FileNotFoundError:
                    # ip command not available (e.g., macOS)
                    click.echo("  - Skipped: 'ip' command not available")

                if shim_exists:
                    click.echo(f"  ✓ Shim interface exists (syrvis-shim)")
                    if shim_has_ip:
                        click.echo(f"  ✓ Shim IP configured ({shim_ip})")
                    else:
                        click.echo(f"  ✗ Shim IP missing (expected {shim_ip})")
                        issues.append(f"Shim interface missing IP - run: syrvis core start")
                    if route_exists:
                        click.echo(f"  ✓ Route to Traefik configured ({traefik_ip})")
                    else:
                        click.echo(f"  ✗ Route to Traefik missing")
                        issues.append(f"Route to Traefik missing - run: syrvis core start")
                else:
                    click.echo(f"  ✗ Shim interface missing (syrvis-shim)")
                    click.echo(f"     The shim enables host-to-container communication with macvlan.")
                    click.echo(f"     Run: syrvis core start")
                    issues.append("Macvlan shim missing - run: syrvis core start")

                click.echo()
    except Exception as e:
        if verbose:
            click.echo(f"  ⚠ Shim check failed: {e}")

    # ==========================================================================
    # Network & Endpoints Validation
    # ==========================================================================
    endpoints = get_configured_endpoints()

    if endpoints:
        click.echo("DNS Resolution")
        click.echo("-" * 70)

        dns_issues = []
        for endpoint in endpoints:
            domain = endpoint["domain"]
            expected_ip = endpoint.get("expected_ip", "")

            dns_result = validate_dns_for_domain(domain, expected_ip)
            local = dns_result["local"]
            public = dns_result["public"]

            if local["ok"] and public["ok"]:
                # Check for valid split-horizon DNS (local → Traefik, public → external)
                if dns_result.get("split_horizon_ok"):
                    # Split-horizon is correct: local points to Traefik, public resolves (Cloudflare)
                    click.echo(f"  ✓ {domain}")
                    if verbose or not dns_result.get("consistent"):
                        click.echo(f"     Local: {local['ip']} | Public: {public['ip']} (split-horizon OK)")
                elif dns_result.get("consistent"):
                    if expected_ip and local["ip"] == expected_ip:
                        click.echo(f"  ✓ {domain}")
                        if verbose:
                            click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
                    elif expected_ip:
                        click.echo(f"  ⚠ {domain} → {local['ip']} (expected {expected_ip})")
                        dns_issues.append(f"{domain}: points to {local['ip']}, expected {expected_ip}")
                    else:
                        click.echo(f"  ✓ {domain} → {local['ip']}")
                elif expected_ip and local["ip"] != expected_ip:
                    # Local doesn't point to Traefik - this is wrong
                    click.echo(f"  ⚠ {domain}: Local DNS incorrect")
                    click.echo(f"     Local: {local['ip']} (expected {expected_ip}) | Public: {public['ip']}")
                    dns_issues.append(f"{domain}: local ({local['ip']}) should be {expected_ip}")
                else:
                    # No expected IP, just note the inconsistency
                    click.echo(f"  ✓ {domain}")
                    if verbose:
                        click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
            elif public["ok"]:
                click.echo(f"  ⚠ {domain}: Local NXDOMAIN, Public: {public['ip']}")
                dns_issues.append(f"{domain}: not in local DNS")
            elif local["ok"]:
                click.echo(f"  ✗ {domain}: Public NXDOMAIN (Let's Encrypt will fail!)")
                click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
                dns_issues.append(f"{domain}: not in public DNS - Let's Encrypt will fail")
                issues.append(f"DNS: {domain} has no public record")
            else:
                click.echo(f"  ✗ {domain}: NXDOMAIN")
                dns_issues.append(f"{domain}: no DNS record")
                issues.append(f"DNS: {domain} has no record")

        click.echo()

        # Certificate validation
        click.echo("SSL Certificates")
        click.echo("-" * 70)

        for endpoint in endpoints:
            domain = endpoint["domain"]
            name = endpoint["name"]

            cert_result = check_certificate(domain)

            if cert_result.get("error"):
                click.echo(f"  ✗ {domain}: {cert_result['error']}")
                issues.append(f"Cert: {domain} - {cert_result['error']}")
            elif cert_result.get("is_letsencrypt"):
                days = cert_result.get("days_remaining", "?")
                click.echo(f"  ✓ {domain}: Let's Encrypt (expires in {days} days)")
            elif cert_result.get("is_traefik_default"):
                click.echo(f"  ✗ {domain}: Traefik default cert (no Let's Encrypt)")
                issues.append(f"Cert: {domain} using Traefik default - check DNS & port 80")
            elif cert_result.get("is_self_signed"):
                click.echo(f"  ⚠ {domain}: Self-signed certificate")
                if verbose:
                    click.echo(f"     Issuer: {cert_result.get('issuer', 'unknown')}")
            else:
                issuer = cert_result.get("issuer", "unknown")
                click.echo(f"  ? {domain}: {issuer}")

        click.echo()

        # Backend health checks (for Synology services)
        backends_to_check = [e for e in endpoints if e.get("backend_host") and e.get("backend_port")]

        if backends_to_check:
            click.echo("Backend Services")
            click.echo("-" * 70)

            for endpoint in backends_to_check:
                name = endpoint["name"]
                host = endpoint["backend_host"]
                port = endpoint["backend_port"]

                backend_result = check_backend_service(host, port)

                if backend_result["reachable"]:
                    click.echo(f"  ✓ {name}: {host}:{port} reachable")
                else:
                    error = backend_result.get("error", "unreachable")
                    click.echo(f"  ✗ {name}: {host}:{port} - {error}")
                    issues.append(f"Backend: {name} ({host}:{port}) - {error}")

            click.echo()

        # File Sharing Services (SMB/AFP/NFS - direct to NAS, not through Traefik)
        # Get NAS IP from env
        nas_ip_for_shares = None
        try:
            env_path = paths.get_env_path()
            if env_path.exists():
                for line in env_path.read_text().split('\n'):
                    if line.startswith('NAS_IP='):
                        nas_ip_for_shares = line.split('=', 1)[1].strip()
                        break
        except Exception:
            pass

        if nas_ip_for_shares:
            click.echo("File Sharing (direct to NAS)")
            click.echo("-" * 70)
            click.echo("  Note: SMB/AFP/NFS connect directly to NAS, not through Traefik")
            click.echo()

            # File sharing protocols and their ports
            file_share_services = [
                ("SMB (Windows/Mac)", 445, "smb://"),
                ("NetBIOS (legacy SMB)", 139, None),
                ("AFP (Mac)", 548, "afp://"),
                ("NFS (Linux)", 2049, "nfs://"),
            ]

            for name, port, url_scheme in file_share_services:
                result = check_backend_service(nas_ip_for_shares, port)
                if result["reachable"]:
                    if url_scheme:
                        click.echo(f"  ✓ {name}: {nas_ip_for_shares}:{port}")
                        click.echo(f"     Connect: {url_scheme}{nas_ip_for_shares}")
                    else:
                        click.echo(f"  ✓ {name}: {nas_ip_for_shares}:{port}")
                else:
                    click.echo(f"  - {name}: {nas_ip_for_shares}:{port} (not enabled)")

            click.echo()

        # HTTP endpoint checks
        click.echo("Endpoint Health")
        click.echo("-" * 70)

        for endpoint in endpoints:
            domain = endpoint["domain"]
            name = endpoint["name"]
            expected_status = endpoint.get("expected_status", [200, 301, 302, 303, 307, 308])

            http_result = check_http_endpoint(f"https://{domain}")

            if http_result["reachable"]:
                status = http_result["status_code"]
                if status in expected_status:
                    click.echo(f"  ✓ {domain}: HTTP {status}")
                elif status in (200, 301, 302, 303, 307, 308):
                    # Still reachable but unexpected status for this service
                    click.echo(f"  ✓ {domain}: HTTP {status}")
                else:
                    click.echo(f"  ⚠ {domain}: HTTP {status}")
            else:
                error = http_result.get("error", "unreachable")
                click.echo(f"  ✗ {domain}: {error}")

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
