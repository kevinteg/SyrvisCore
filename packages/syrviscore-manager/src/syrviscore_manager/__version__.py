"""SyrvisCore Manager version information."""

# 0.3.5 = the 2026-08-16 incident work: `syrvisctl doctor` (diagnoses from the
# rootfs with no resolvable home) and the wrapper's renamed-install discovery
# fallback. The manager is SPK-installed, so this needs an SPK reinstall to reach
# a NAS — unlike the service package, which `syrvisctl install` updates.
__version__ = "0.3.5"
__author__ = "Kevin Tegtmeier"
__description__ = "Version management for SyrvisCore on Synology NAS"
