"""Doctor command for SyrvisCore - diagnose and fix installation issues."""

import click
import sys
import os
from pathlib import Path
from typing import List

from . import privileged_ops
from . import paths
from .validators import (
    CheckResult,
    ValidationReport,
    InstallationValidator,
    DockerValidator,
    ConfigurationValidator,
    NetworkValidator,
    SystemValidator,
    get_configured_endpoints,
    validate_dns,
    check_certificate,
    check_http_endpoint,
    check_tcp_port,
)


# =============================================================================
# Output Formatting
# =============================================================================

def print_section(title: str):
    """Print a section header."""
    click.echo(title)
    click.echo("-" * 70)


def print_check(result: CheckResult, verbose: bool = False):
    """Print a single check result."""
    icon = "✓" if result.passed else "✗"
    if not result.passed and result.fixable:
        icon = "⚠"

    click.echo(f"  {icon} {result.name}: {result.message}")

    if result.details and (verbose or not result.passed):
        click.echo(f"     {result.details}")


def print_report(report: ValidationReport, verbose: bool = False):
    """Print a validation report."""
    print_section(report.category)

    for check in report.checks:
        print_check(check, verbose)

    click.echo()


# =============================================================================
# DNS & Certificate Checks (uses validators module)
# =============================================================================

def run_dns_checks(endpoints: List[dict], verbose: bool = False) -> List[str]:
    """Run DNS validation for all endpoints."""
    print_section("DNS Resolution")
    issues = []

    for endpoint in endpoints:
        domain = endpoint["domain"]
        expected_ip = endpoint.get("expected_ip", "")

        dns_result = validate_dns(domain, expected_ip)
        local = dns_result["local"]
        public = dns_result["public"]

        if local["ok"] and public["ok"]:
            # Check for valid split-horizon DNS
            if dns_result.get("split_horizon_ok"):
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
                    issues.append(f"{domain}: points to {local['ip']}, expected {expected_ip}")
                else:
                    click.echo(f"  ✓ {domain} → {local['ip']}")
            elif expected_ip and local["ip"] != expected_ip:
                click.echo(f"  ⚠ {domain}: Local DNS incorrect")
                click.echo(f"     Local: {local['ip']} (expected {expected_ip}) | Public: {public['ip']}")
                issues.append(f"{domain}: local ({local['ip']}) should be {expected_ip}")
            else:
                click.echo(f"  ✓ {domain}")
                if verbose:
                    click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
        elif public["ok"]:
            click.echo(f"  ⚠ {domain}: Local NXDOMAIN, Public: {public['ip']}")
            issues.append(f"{domain}: not in local DNS")
        elif local["ok"]:
            click.echo(f"  ✗ {domain}: Public NXDOMAIN (Let's Encrypt will fail!)")
            click.echo(f"     Local: {local['ip']} | Public: {public['ip']}")
            issues.append(f"{domain}: not in public DNS - Let's Encrypt will fail")
        else:
            click.echo(f"  ✗ {domain}: NXDOMAIN")
            issues.append(f"{domain}: no DNS record")

    click.echo()
    return issues


def run_certificate_checks(endpoints: List[dict], verbose: bool = False) -> List[str]:
    """Run SSL certificate validation for all endpoints."""
    print_section("SSL Certificates")
    issues = []

    for endpoint in endpoints:
        domain = endpoint["domain"]
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
    return issues


def run_backend_checks(endpoints: List[dict]) -> List[str]:
    """Run backend service health checks."""
    backends = [e for e in endpoints if e.get("backend_host") and e.get("backend_port")]

    if not backends:
        return []

    print_section("Backend Services")
    issues = []

    for endpoint in backends:
        name = endpoint["name"]
        host = endpoint["backend_host"]
        port = endpoint["backend_port"]

        result = check_tcp_port(host, port)

        if result["reachable"]:
            click.echo(f"  ✓ {name}: {host}:{port} reachable")
        else:
            error = result.get("error", "unreachable")
            click.echo(f"  ✗ {name}: {host}:{port} - {error}")
            issues.append(f"Backend: {name} ({host}:{port}) - {error}")

    click.echo()
    return issues


def run_file_sharing_checks(nas_ip: str) -> None:
    """Check file sharing services (SMB/AFP/NFS)."""
    if not nas_ip:
        return

    print_section("File Sharing (direct to NAS)")
    click.echo("  Note: SMB/AFP/NFS connect directly to NAS, not through Traefik")
    click.echo()

    services = [
        ("SMB (Windows/Mac)", 445, "smb://"),
        ("NetBIOS (legacy SMB)", 139, None),
        ("AFP (Mac)", 548, "afp://"),
        ("NFS (Linux)", 2049, "nfs://"),
    ]

    for name, port, url_scheme in services:
        result = check_tcp_port(nas_ip, port)
        if result["reachable"]:
            if url_scheme:
                click.echo(f"  ✓ {name}: {nas_ip}:{port}")
                click.echo(f"     Connect: {url_scheme}{nas_ip}")
            else:
                click.echo(f"  ✓ {name}: {nas_ip}:{port}")
        else:
            click.echo(f"  - {name}: {nas_ip}:{port} (not enabled)")

    click.echo()


def run_endpoint_health_checks(endpoints: List[dict]) -> None:
    """Run HTTP endpoint health checks."""
    print_section("Endpoint Health")

    for endpoint in endpoints:
        domain = endpoint["domain"]
        expected_status = endpoint.get("expected_status", [200, 301, 302, 303, 307, 308])

        result = check_http_endpoint(f"https://{domain}")

        if result["reachable"]:
            status = result["status_code"]
            if status in expected_status or status in (200, 301, 302, 303, 307, 308):
                click.echo(f"  ✓ {domain}: HTTP {status}")
            else:
                click.echo(f"  ⚠ {domain}: HTTP {status}")
        else:
            error = result.get("error", "unreachable")
            click.echo(f"  ✗ {domain}: {error}")

    click.echo()


# =============================================================================
# Fix Actions
# =============================================================================

def apply_fixes(fixable_issues: List[CheckResult], install_dir: Path = None) -> int:
    """Apply automatic fixes for fixable issues."""
    print_section("Attempting Fixes")
    fixed_count = 0

    for check in fixable_issues:
        action = check.fix_action

        if action == "docker_group":
            click.echo("Creating docker group...")
            success, msg = privileged_ops.ensure_docker_group()
            click.echo(f"  {'✓' if success else '✗'} {msg}")
            if success:
                fixed_count += 1

        elif action and action.startswith("user_group:"):
            user = action.split(':', 1)[1]
            click.echo(f"Adding {user} to docker group...")
            success, msg = privileged_ops.ensure_user_in_docker_group(user)
            click.echo(f"  {'✓' if success else '✗'} {msg}")
            if success:
                fixed_count += 1

        elif action == "socket_perms":
            click.echo("Fixing Docker socket permissions...")
            success, msg = privileged_ops.ensure_docker_socket_permissions()
            click.echo(f"  {'✓' if success else '✗'} {msg}")
            if success:
                fixed_count += 1

        elif action == "symlink" and install_dir:
            click.echo("Creating global symlink...")
            success, msg = privileged_ops.ensure_global_symlink(install_dir)
            click.echo(f"  {'✓' if success else '✗'} {msg}")
            if success:
                fixed_count += 1

        elif action and action.startswith("startup:") and install_dir:
            user = action.split(':', 1)[1]
            click.echo("Creating startup script...")
            success, msg = privileged_ops.ensure_startup_script(install_dir, user)
            click.echo(f"  {'✓' if success else '✗'} {msg}")
            if success:
                fixed_count += 1

    return fixed_count


# =============================================================================
# Main Doctor Command
# =============================================================================

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

    all_issues: List[str] = []
    fixable_checks: List[CheckResult] = []

    # Initialize validators
    install_validator = InstallationValidator()
    config_validator = ConfigurationValidator()

    # Skip installation checks if --network flag is set
    if not network:
        # Installation checks
        install_report = install_validator.validate()
        print_report(install_report, verbose)
        all_issues.extend([c.message for c in install_report.issues])
        fixable_checks.extend(install_report.fixable_issues)

        # Docker checks
        docker_report = DockerValidator().validate()
        print_report(docker_report, verbose)
        all_issues.extend([c.message for c in docker_report.issues])
        fixable_checks.extend(docker_report.fixable_issues)

        # Configuration checks
        if install_validator.syrvis_home:
            config_report = config_validator.validate()
            print_report(config_report, verbose)
            all_issues.extend([c.message for c in config_report.issues])
            fixable_checks.extend(config_report.fixable_issues)

            # System integration checks
            system_report = SystemValidator(install_validator.syrvis_home).validate()
            print_report(system_report, verbose)
            all_issues.extend([c.message for c in system_report.issues])
            fixable_checks.extend(system_report.fixable_issues)

    # Macvlan checks
    if config_validator.get_value('TRAEFIK_IP'):
        network_report = NetworkValidator(config_validator).validate()
        print_report(network_report, verbose)
        all_issues.extend([c.message for c in network_report.issues])

    # Endpoint checks
    endpoints = get_configured_endpoints(config_validator)

    if endpoints:
        # DNS resolution
        dns_issues = run_dns_checks(endpoints, verbose)
        all_issues.extend([f"DNS: {i}" for i in dns_issues])

        # SSL certificates
        cert_issues = run_certificate_checks(endpoints, verbose)
        all_issues.extend(cert_issues)

        # Backend services
        backend_issues = run_backend_checks(endpoints)
        all_issues.extend(backend_issues)

        # File sharing
        nas_ip = config_validator.get_value('NAS_IP')
        run_file_sharing_checks(nas_ip)

        # HTTP endpoints
        run_endpoint_health_checks(endpoints)

    # Summary
    click.echo("=" * 70)
    if not all_issues:
        click.echo("✓ All checks passed!")
        click.echo()
        click.echo("Your SyrvisCore installation is healthy.")
    else:
        click.echo(f"✗ Issues Found: {len(all_issues)}")
        click.echo()
        for i, issue in enumerate(all_issues, 1):
            click.echo(f"  {i}. {issue}")

        click.echo()

        if fixable_checks and not fix:
            click.echo(f"Fixable with --fix: {len(fixable_checks)}")
            click.echo()
            click.echo("Run with --fix to attempt automatic repairs:")
            click.echo("  sudo syrvis doctor --fix")
        elif fix and fixable_checks:
            click.echo()
            fixed_count = apply_fixes(fixable_checks, install_validator.syrvis_home)
            click.echo()
            click.echo(f"Fixed: {fixed_count}/{len(fixable_checks)} issues")

            if fixed_count > 0:
                click.echo()
                click.echo("Re-run doctor to verify fixes:")
                click.echo("  syrvis doctor")

    click.echo("=" * 70)
    click.echo()

    sys.exit(0 if not all_issues else 1)
