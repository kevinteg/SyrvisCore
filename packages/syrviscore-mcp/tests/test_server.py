"""The FastMCP server registers the expected tool set with correct hints."""

import asyncio

import pytest

pytest.importorskip("fastmcp")

from syrviscore_mcp import server  # noqa: E402

EXPECTED_TOOLS = {
    # read-only
    "status",
    "verify",
    "service_list",
    "stack_hostnames",
    "logs",
    "versions_list",
    "check_updates",
    "info",
    "backup_list",
    "cleanup_preview",
    # privileged, non-destructive
    "start",
    "stop",
    "restart",
    "verify_fix",
    "service_start",
    "service_stop",
    "service_update",
    "service_add",
    "service_run",
    "install",
    # privileged + destructive
    "activate",
    "rollback",
    "uninstall",
    "cleanup",
    "service_remove",
}

DESTRUCTIVE = {"activate", "rollback", "uninstall", "cleanup", "service_remove"}
READ_ONLY = {
    "status",
    "verify",
    "service_list",
    "stack_hostnames",
    "logs",
    "versions_list",
    "check_updates",
    "info",
    "backup_list",
    "cleanup_preview",
}


def _tools():
    async def go():
        return await server.mcp.list_tools()

    tools = asyncio.run(go())
    return {t.name: t for t in tools}


def test_all_tools_registered():
    names = set(_tools().keys())
    assert names == EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == 25


def test_destructive_tools_have_destructive_hint():
    tools = _tools()
    for name in DESTRUCTIVE:
        ann = getattr(tools[name], "annotations", None)
        assert ann is not None and getattr(ann, "destructiveHint", None), name


def test_read_only_tools_have_readonly_hint():
    tools = _tools()
    for name in READ_ONLY:
        ann = getattr(tools[name], "annotations", None)
        assert ann is not None and getattr(ann, "readOnlyHint", None), name
