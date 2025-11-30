"""
Path management for SyrvisCore.

Handles SYRVIS_HOME environment variable and provides helpers for common paths.
"""

import os
from pathlib import Path


class SyrvisHomeError(Exception):
    """Raised when SYRVIS_HOME is not set or invalid."""

    pass


def get_syrvis_home() -> Path:
    """
    Get the SYRVIS_HOME directory from environment variable.

    Returns:
        Path object for SYRVIS_HOME directory

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or doesn't exist
    """
    syrvis_home = os.environ.get("SYRVIS_HOME")

    if not syrvis_home:
        raise SyrvisHomeError(
            "SYRVIS_HOME environment variable not set. "
            "Please set it to your SyrvisCore installation directory."
        )

    syrvis_path = Path(syrvis_home)

    if not syrvis_path.exists():
        raise SyrvisHomeError(
            f"SYRVIS_HOME directory does not exist: {syrvis_path}\n"
            "Please ensure SYRVIS_HOME points to a valid directory."
        )

    if not syrvis_path.is_dir():
        raise SyrvisHomeError(
            f"SYRVIS_HOME is not a directory: {syrvis_path}\n"
            "SYRVIS_HOME must point to a directory."
        )

    return syrvis_path


def get_docker_compose_path() -> Path:
    """
    Get path to docker-compose.yaml file.

    Returns:
        Path to docker-compose.yaml in SYRVIS_HOME

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
    """
    return get_syrvis_home() / "docker-compose.yaml"


def get_config_path() -> Path:
    """
    Get path to build/config.yaml file.

    Returns:
        Path to build/config.yaml in SYRVIS_HOME

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
    """
    return get_syrvis_home() / "build" / "config.yaml"


def get_core_path() -> Path:
    """
    Get path to core data directory.

    Returns:
        Path to data directory in SYRVIS_HOME

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
    """
    return get_syrvis_home() / "data"


def validate_docker_compose_exists() -> None:
    """
    Validate that docker-compose.yaml exists in SYRVIS_HOME.

    Raises:
        SyrvisHomeError: If SYRVIS_HOME not set or invalid
        FileNotFoundError: If docker-compose.yaml doesn't exist
    """
    compose_path = get_docker_compose_path()

    if not compose_path.exists():
        raise FileNotFoundError(
            f"docker-compose.yaml not found in SYRVIS_HOME ({get_syrvis_home()})\n"
            "Run 'syrvis generate-compose' to create it."
        )


def set_syrvis_home(path: str) -> None:
    """
    Set SYRVIS_HOME environment variable (for testing).

    Args:
        path: Path to set as SYRVIS_HOME
    """
    os.environ["SYRVIS_HOME"] = path


def unset_syrvis_home() -> None:
    """
    Unset SYRVIS_HOME environment variable (for testing).
    """
    if "SYRVIS_HOME" in os.environ:
        del os.environ["SYRVIS_HOME"]
