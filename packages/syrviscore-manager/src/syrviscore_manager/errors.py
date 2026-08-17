"""
Typed error taxonomy for SyrvisCore Manager.

Every failure mode raises a SyrvisError subclass with a stable machine-readable
``code``. The CLI (and later the MCP server) catches SyrvisError at the boundary
and renders it for humans or as JSON; library code never prints or exits.
"""


class SyrvisError(Exception):
    """Base class for all manager errors."""

    code = "error"
    exit_code = 1

    def to_dict(self):
        return {"error": self.code, "message": str(self)}


class HomeNotFoundError(SyrvisError):
    """No SyrvisCore installation could be located."""

    code = "home_not_found"


class AmbiguousHomeError(SyrvisError):
    """Multiple SyrvisCore installations found and none selected explicitly."""

    code = "ambiguous_home"


class InvalidVersionError(SyrvisError):
    """Version string is not a valid MAJOR.MINOR.PATCH version."""

    code = "invalid_version"


class VersionNotFoundError(SyrvisError):
    """Requested version is not installed."""

    code = "version_not_found"


class ActiveVersionError(SyrvisError):
    """Operation refused because it targets the active version."""

    code = "active_version"


class ReleaseNotFoundError(SyrvisError):
    """No matching release found on GitHub."""

    code = "release_not_found"


class NetworkError(SyrvisError):
    """Network or GitHub API failure (includes HTTP status when known)."""

    code = "network"


class IntegrityError(SyrvisError):
    """Downloaded artifact failed checksum verification (or none was available)."""

    code = "integrity"


class InstallError(SyrvisError):
    """Venv creation or wheel installation failed."""

    code = "install"


class ActivationError(SyrvisError):
    """Switching the current symlink / wrapper failed."""

    code = "activation"


class BackupError(SyrvisError):
    """Backup creation failed."""

    code = "backup"


class RestoreError(SyrvisError):
    """Restore from backup failed or the archive is unsafe/invalid."""

    code = "restore"


class LockError(SyrvisError):
    """Could not acquire the installation lock (another operation in progress)."""

    code = "lock"


class CompatibilityError(SyrvisError):
    """The service version declares a newer minimum manager than is installed."""

    code = "incompatible_manager"


class CollisionError(SyrvisError):
    """A DSM collision-renamed platform root is present (install refuses).

    ``<volume>/<name>_1`` means DSM renamed one of our volume roots at boot
    because a shared folder shares its name. Installing over that state writes a
    NEW empty tree beside the real one and reads as data loss (incident
    2026-08-16); the fix is a ``mv``, never an install.
    """

    code = "volume_root_collision"
