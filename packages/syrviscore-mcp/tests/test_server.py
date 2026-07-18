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
    "reconcile_plan",
    "schedule_list",
    # privileged, non-destructive
    "start",
    "stop",
    "restart",
    "verify_fix",
    "stack_apply",
    "reconcile",
    "service_start",
    "service_stop",
    "service_update",
    "service_add",
    "service_run",
    "service_declare",
    "service_adopt",
    "install",
    # privileged + destructive
    "activate",
    "rollback",
    "uninstall",
    "cleanup",
    "service_remove",
    "reconcile_prune",
    "schedule_apply",
    "schedule_sync",
}

DESTRUCTIVE = {
    "activate",
    "rollback",
    "uninstall",
    "cleanup",
    "service_remove",
    "reconcile_prune",
    "schedule_apply",
    "schedule_sync",
}
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
    "reconcile_plan",
    "schedule_list",
}
IDEMPOTENT = {"reconcile", "service_declare", "service_adopt"}


def _tools():
    async def go():
        return await server.mcp.list_tools()

    tools = asyncio.run(go())
    return {t.name: t for t in tools}


def test_all_tools_registered():
    names = set(_tools().keys())
    assert names == EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == 34


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


def test_services_d_idempotent_tools_have_idempotent_hint():
    tools = _tools()
    for name in IDEMPOTENT:
        ann = getattr(tools[name], "annotations", None)
        assert ann is not None and getattr(ann, "idempotentHint", None), name
        assert not getattr(ann, "destructiveHint", None), name
