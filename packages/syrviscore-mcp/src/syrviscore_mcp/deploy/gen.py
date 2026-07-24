#!/usr/bin/env python3
"""Back-compat launcher for the relocated seam generator.

The registry and generators live in the platform now (``syrviscore.seam``), so
the NAS, the MCP, and deployment tooling all derive the seam from one source.
This module keeps the historical entry points working:

    python -m syrviscore_mcp.deploy.gen sudoers|shim|provision|check ...
    python3 .../src/syrviscore_mcp/deploy/gen.py ...   (source tree, no install)
"""

import sys
from pathlib import Path

# Source-tree fallback: parents[4] is the monorepo's packages/ directory, so the
# generator stays runnable with no venv at all (the README documents this).
try:
    from syrviscore.seam.gen import (  # noqa: F401
        DEFAULT,
        DeployConfig,
        SUDOERS_INSTALL_PATH,
        command_patterns,
        main,
        render_provision,
        render_shim,
        render_sudoers,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "syrviscore" / "src"))
    from syrviscore.seam.gen import (  # noqa: F401
        DEFAULT,
        DeployConfig,
        SUDOERS_INSTALL_PATH,
        command_patterns,
        main,
        render_provision,
        render_shim,
        render_sudoers,
    )

if __name__ == "__main__":
    sys.exit(main(sys.argv))
