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

    Returns dict with 'local', 'public' results and 'consistent' flag.
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
        result["correct"] = (local_ok and local_ip == expected_ip) or (public_ok and public_ip == expected_ip)

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
        })

        endpoints.append({
            "name": "Portainer",
            "subdomain": "portainer",
            "domain": f"portainer.{domain}",
            "expected_ip": traefik_ip,
            "backend_host": None,
            "backend_port": None,
        })

        # Synology services
        synology_services = {
            "SYNOLOGY_DSM_ENABLED": ("DSM Portal", "dsm", 5001),
            "SYNOLOGY_DSFILE_ENABLED": ("DS File", "files", 5006),
            "SYNOLOGY_PHOTOS_ENABLED": ("Synology Photos", "photos", 5001),
            "SYNOLOGY_DRIVE_ENABLED": ("Synology Drive", "drive", 6690),
            "SYNOLOGY_AUDIO_ENABLED": ("Audio Station", "audio", 5001),
            "SYNOLOGY_VIDEO_ENABLED": ("Video Station", "video", 5001),
        }

        for env_key, (name, subdomain, port) in synology_services.items():
            if env_vars.get(env_key, '').lower() in ('true', '1', 'yes'):
                endpoints.append({
                    "name": name,
                    "subdomain": subdomain,
                    "domain": f"{subdomain}.{domain}",
                    "expected_ip": traefik_ip,
                    "backend_host": nas_ip,
                    "backend_port": port,
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
                if dns_result.get("consistent"):
                    if expected_ip and local["ip"] == expected_ip:
                        click.echo(f"  ✓ {domain}")
                        if verbose:
                            click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
                    elif expected_ip:
                        click.echo(f"  ⚠ {domain} → {local['ip']} (expected {expected_ip})")
                        dns_issues.append(f"{domain}: points to {local['ip']}, expected {expected_ip}")
                    else:
                        click.echo(f"  ✓ {domain} → {local['ip']}")
                else:
                    click.echo(f"  ⚠ {domain}: DNS inconsistent")
                    click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
                    dns_issues.append(f"{domain}: local ({local['ip']}) != public ({public['ip']})")
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

        # HTTP endpoint checks
        click.echo("Endpoint Health")
        click.echo("-" * 70)

        for endpoint in endpoints:
            domain = endpoint["domain"]
            name = endpoint["name"]

            http_result = check_http_endpoint(f"https://{domain}")

            if http_result["reachable"]:
                status = http_result["status_code"]
                if status in (200, 301, 302, 303, 307, 308):
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
