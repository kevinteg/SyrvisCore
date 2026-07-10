"""Tool orchestration: sandbox pre-checks, confirmation handshake, follow-up reads."""

import pytest

from syrviscore_mcp import tools
from syrviscore_mcp.errors import ConfirmationError, McpError, SandboxError

from .conftest import FakeRunner, make_config

MANAGED = {"services": [{"name": "gollum", "status": "running", "version": "1.0.0"}]}
VERSIONS = {"versions": ["0.2.0", "0.1.0"], "active": "0.2.0"}


def make_ctx(responses=None):
    cfg = make_config(token_ttl_s=300)
    runner = FakeRunner(cfg, responses or {})
    ctx = tools.ToolContext(cfg=cfg, runner=runner, secret=b"s", now=lambda: 1000)
    return ctx, runner


class TestReadOnly:
    def test_status(self):
        ctx, runner = make_ctx({"status": {"services": {}}})
        tools.status(ctx)
        assert runner.ids() == ["status"]

    def test_verify_smoke_uses_smoke_command(self):
        ctx, runner = make_ctx()
        tools.verify(ctx, smoke=True)
        assert runner.ids() == ["verify_smoke"]


class TestSandbox:
    def test_service_stop_checks_membership_first(self):
        ctx, runner = make_ctx({"service_list": MANAGED})
        tools.service_stop(ctx, "gollum")
        # service_list (membership) precedes service_stop; then a follow-up list
        assert runner.ids()[0] == "service_list"
        assert "service_stop" in runner.ids()

    def test_unmanaged_service_refused_no_mutation(self):
        ctx, runner = make_ctx({"service_list": {"services": []}})
        with pytest.raises(SandboxError):
            tools.service_stop(ctx, "gollum")
        assert "service_stop" not in runner.ids()

    def test_reserved_name_refused(self):
        # a reserved core name is refused (as a validation error, before SSH)
        ctx, runner = make_ctx({"service_list": MANAGED})
        with pytest.raises(McpError):
            tools.service_stop(ctx, "traefik")
        assert "service_stop" not in runner.ids()

    def test_logs_of_service_checks_membership(self):
        ctx, runner = make_ctx({"service_list": MANAGED})
        tools.logs(ctx, service="gollum", tail=10)
        assert runner.ids()[0] == "service_list"


class TestConfirmation:
    def test_activate_without_confirm_returns_plan_no_mutation(self):
        ctx, runner = make_ctx({"versions_list": VERSIONS})
        out = tools.activate(ctx, "0.1.0")
        assert out["needs_confirmation"] is True
        assert "confirm_token" in out
        assert "activate" not in runner.ids()  # no mutation issued

    def test_activate_with_valid_token_mutates(self):
        ctx, runner = make_ctx({"versions_list": VERSIONS, "activate": {"ok": True}})
        plan = tools.activate(ctx, "0.1.0")
        token = plan["confirm_token"]
        out = tools.activate(ctx, "0.1.0", confirm=token)
        assert "activate" in runner.ids()
        assert out.get("versions") == VERSIONS["versions"]  # follow-up read merged

    def test_token_for_other_version_rejected(self):
        ctx, runner = make_ctx({"versions_list": VERSIONS})
        plan = tools.activate(ctx, "0.1.0")
        token = plan["confirm_token"]
        with pytest.raises(ConfirmationError):
            tools.activate(ctx, "0.2.0", confirm=token)

    def test_service_remove_handshake(self):
        ctx, runner = make_ctx({"service_list": MANAGED, "service_remove": {"ok": True}})
        plan = tools.service_remove(ctx, "gollum")
        assert plan["needs_confirmation"] is True
        assert plan["plan"]["purge"] is False  # purge is never automatable
        tools.service_remove(ctx, "gollum", confirm=plan["confirm_token"])
        assert "service_remove" in runner.ids()


class TestFollowUpReads:
    def test_install_merges_versions(self):
        ctx, runner = make_ctx({"install": {"ok": True}, "versions_list": VERSIONS})
        out = tools.install(ctx, "0.2.0")
        assert out["active"] == "0.2.0"
        assert "install" in runner.ids()
