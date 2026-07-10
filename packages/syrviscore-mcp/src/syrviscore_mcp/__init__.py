"""SyrvisCore MCP server — operator-side tools for managing a Synology NAS.

Runs on the operator's Mac and executes syrvis/syrvisctl on the NAS over SSH.
Never elevates itself; never builds a shell string; never runs arbitrary
commands on the NAS. See docs/mcp-design.md.
"""

__version__ = "0.1.0"
