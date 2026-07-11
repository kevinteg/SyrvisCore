"""Config / versions / info endpoints (read-only, redacted)."""

from fastapi import APIRouter

from ..__version__ import __version__

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config() -> dict:
    """Redacted runtime config + which optional components are enabled."""
    try:
        from syrviscore.config_reader import read_config

        return read_config().to_dict()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "values": {}, "enabled_components": {}}


@router.get("/versions")
def get_versions() -> dict:
    """Installed service versions + the active one (from the manifest)."""
    try:
        from syrviscore import paths

        manifest = paths.get_manifest()
        return {
            "active_version": manifest.get("active_version"),
            "versions": manifest.get("versions", {}),
            "update_history": manifest.get("update_history", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "active_version": None, "versions": {}}


@router.get("/info")
def get_info() -> dict:
    """Install summary for the dashboard header."""
    from syrviscore import paths

    info = {"dashboard_version": __version__}
    try:
        info["install_path"] = str(paths.get_syrvis_home())
    except Exception:  # noqa: BLE001
        info["install_path"] = None
    try:
        info["active_version"] = paths.get_active_version()
    except Exception:  # noqa: BLE001
        info["active_version"] = None
    try:
        info["setup_complete"] = paths.verify_setup_complete()
    except Exception:  # noqa: BLE001
        info["setup_complete"] = False
    return info
