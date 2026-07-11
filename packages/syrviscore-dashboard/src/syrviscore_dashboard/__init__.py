"""SyrvisCore Dashboard — a live web adapter over the ``syrviscore`` library.

A FastAPI service + React SPA that runs as a base-tier container and imports the
``syrviscore`` libraries in-process to observe and safely manage a SyrvisCore
instance. It is the third thin adapter over the deterministic core, alongside the
``syrvis`` CLI and the MCP server.
"""

from .__version__ import __version__

__all__ = ["__version__"]
