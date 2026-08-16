"""SyrvisCore Manager version information."""

# 0.3.5 = the 2026-08-16 incident work: `syrvisctl doctor` (diagnoses from the
# rootfs with no resolvable home) and the wrapper's renamed-install discovery
# fallback. 0.3.6 = doctor learns the configured apps-root segment (read from the
# rootfs boot-env cache) so a renamed `syrviscore-apps_1` classifies as RENAMED,
# plus the matching MIN_BOOT_HOOK_CONTRACT bump to 3. The manager is
# SPK-installed, so this needs an SPK reinstall to reach a NAS — unlike the
# service package, which `syrvisctl install` updates.
__version__ = "0.3.6"
__author__ = "Kevin Tegtmeier"
__description__ = "Version management for SyrvisCore on Synology NAS"
