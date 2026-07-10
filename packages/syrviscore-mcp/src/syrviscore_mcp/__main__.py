"""Entry point: `python -m syrviscore_mcp` runs the stdio MCP server."""


def main() -> None:
    from .server import mcp

    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
