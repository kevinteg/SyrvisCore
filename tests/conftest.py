"""Shared test helpers.

``stamp_install_root`` exists because of the 2026-08-16 cold-boot incident.
``paths.get_syrvis_home()`` used to accept ANY existing directory named by
``SYRVIS_HOME``; it now requires a real install root (a self-identifying
``.syrviscore-manifest.json``), exactly like the two auto-detection strategies
beside it and like the sibling manager package. Production always has one — the
wrapper only ever exports the home it installed — so a test pointing
``SYRVIS_HOME`` at a bare tmpdir was asserting against a state that cannot
legitimately occur, and is precisely the state the hardening rejects.

Tests that WANT the rejection (``tests/test_paths.py``) deliberately do not use
this.
"""

import json
from pathlib import Path

import pytest

MANIFEST_FILENAME = ".syrviscore-manifest.json"


def stamp_install_root(home, version: str = "0.0.1") -> Path:
    """Make ``home`` a directory ``paths.get_syrvis_home()`` will accept.

    Writes the minimum self-identifying manifest: ``install_path`` equal to the
    directory itself (that self-reference is what stops a stray copy under a
    location root from masquerading as the install root) plus the version
    bookkeeping a bare marker would not have. Idempotent.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    manifest = home / MANIFEST_FILENAME
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "active_version": version,
                    "install_path": str(home),
                    "setup_complete": True,
                    "versions": {version: {"status": "active"}},
                },
                indent=2,
            )
        )
    return home


@pytest.fixture
def install_root():
    """Factory fixture wrapping :func:`stamp_install_root`."""
    return stamp_install_root
