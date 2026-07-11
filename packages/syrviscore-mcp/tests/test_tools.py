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


class TestServiceAddSecurity:
    def _ctx_with_hosts(self, responses=None):
        cfg = make_config(git_url_allowed_hosts=["github.com"])
        runner = FakeRunner(cfg, responses or {})
        ctx = tools.ToolContext(cfg=cfg, runner=runner, secret=b"s", now=lambda: 1000)
        return ctx, runner

    def test_service_add_requires_confirmation(self):
        ctx, runner = self._ctx_with_hosts({"service_list": {"services": []}})
        out = tools.service_add(ctx, "https://github.com/u/r.git")
        assert out["needs_confirmation"] is True
        assert "service_add" not in runner.ids()  # no clone/run yet

    def test_service_add_with_token_runs(self):
        ctx, runner = self._ctx_with_hosts(
            {"service_list": {"services": []}, "service_add": {"ok": True}}
        )
        plan = tools.service_add(ctx, "https://github.com/u/r.git")
        tools.service_add(ctx, "https://github.com/u/r.git", confirm=plan["confirm_token"])
        assert "service_add" in runner.ids()

    def test_service_add_disallowed_host_refused(self):
        ctx, runner = self._ctx_with_hosts({"service_list": {"services": []}})
        from syrviscore_mcp.errors import ValidationError

        with pytest.raises(ValidationError):
            tools.service_add(ctx, "https://evil.example.com/u/r.git")

    def test_service_add_no_allowlist_fails_closed(self):
        # default config has empty git_url_allowed_hosts -> disabled
        ctx, runner = make_ctx({"service_list": {"services": []}})
        from syrviscore_mcp.errors import ValidationError

        with pytest.raises(ValidationError):
            tools.service_add(ctx, "https://github.com/u/r.git")


RECONCILE_PLAN = {
    "plan": {"actions": [{"kind": "install", "name": "gollum", "destructive": False}]},
    "applied": False,
}


class TestReconcile:
    def test_reconcile_plan_passthrough(self):
        ctx, runner = make_ctx({"reconcile_plan": RECONCILE_PLAN})
        out = tools.reconcile_plan(ctx)
        assert runner.ids() == ["reconcile_plan"]
        assert out["applied"] is False  # the CLI's JSON passes straight through

    def test_reconcile_passthrough(self):
        applied = {"plan": RECONCILE_PLAN["plan"], "applied": True, "results": [], "ok": True}
        ctx, runner = make_ctx({"reconcile": applied})
        out = tools.reconcile(ctx)
        assert runner.ids() == ["reconcile"]
        assert out["ok"] is True and out["applied"] is True


class TestReconcilePruneConfirmation:
    def test_without_confirm_returns_dry_run_plan_no_mutation(self):
        ctx, runner = make_ctx({"reconcile_plan": RECONCILE_PLAN})
        out = tools.reconcile_prune(ctx, "remove")
        assert out["needs_confirmation"] is True
        assert out["plan"]["prune"] == "remove"
        assert out["plan"]["plan"] == RECONCILE_PLAN["plan"]  # the dry-run preview
        assert "confirm_token" in out
        assert runner.ids() == ["reconcile_plan"]  # only the read-only plan ran

    def test_with_valid_token_executes(self):
        ctx, runner = make_ctx(
            {"reconcile_plan": RECONCILE_PLAN, "reconcile_prune": {"ok": True, "applied": True}}
        )
        first = tools.reconcile_prune(ctx, "stop")
        out = tools.reconcile_prune(ctx, "stop", confirm=first["confirm_token"])
        assert "reconcile_prune" in runner.ids()
        assert out["applied"] is True
        prune_call = next(c for c in runner.calls if c["id"] == "reconcile_prune")
        assert prune_call["tokens"][-2:] == ["--prune", "stop"]

    def test_token_for_other_policy_rejected(self):
        ctx, runner = make_ctx({"reconcile_plan": RECONCILE_PLAN})
        first = tools.reconcile_prune(ctx, "stop")
        with pytest.raises(ConfirmationError):
            tools.reconcile_prune(ctx, "purge", confirm=first["confirm_token"])
        assert "reconcile_prune" not in runner.ids()

    def test_token_dies_if_plan_changed_between_calls(self):
        # TOCTOU: the dry-run plan is bound into the state hash, so a change in
        # declarations/installs between plan and confirm voids the token.
        plans = iter([RECONCILE_PLAN, {"plan": {"actions": []}, "applied": False}])
        ctx, runner = make_ctx({"reconcile_plan": lambda args: next(plans)})
        first = tools.reconcile_prune(ctx, "remove")
        with pytest.raises(ConfirmationError):
            tools.reconcile_prune(ctx, "remove", confirm=first["confirm_token"])
        assert "reconcile_prune" not in runner.ids()

    def test_invalid_policy_rejected_before_any_ssh(self):
        from syrviscore_mcp.errors import ValidationError

        ctx, runner = make_ctx()
        for bad in ("everything", "stop;id", "-y", "Purge"):
            with pytest.raises(ValidationError):
                tools.reconcile_prune(ctx, bad)
        assert runner.ids() == []


DECLARE_IMG = "ghcr.io/acme/cyberquill:1.4.0"
DECLARED = {
    "ok": True,
    "name": "cyberquill",
    "path": "/x/services.d/cyberquill.yaml",
    "applied": False,
}


def make_declare_ctx(responses=None, registries=("ghcr.io",)):
    cfg = make_config(image_allowed_registries=list(registries))
    runner = FakeRunner(cfg, responses or {})
    ctx = tools.ToolContext(cfg=cfg, runner=runner, secret=b"s", now=lambda: 1000)
    return ctx, runner


class TestServiceDeclare:
    def test_happy_path_renders_bools_lowercase(self):
        ctx, runner = make_declare_ctx({"service_declare": DECLARED})
        out = tools.service_declare(
            ctx, "cyberquill", DECLARE_IMG, exposure="tunnel", port=8080, critical=True
        )
        assert out["applied"] is False  # authoring only; reconcile applies later
        assert runner.ids() == ["service_declare"]  # no token handshake, no follow-up
        call = runner.calls[0]
        assert call["args"]["enabled"] == "true" and call["args"]["critical"] == "true"
        assert call["args"]["subdomain"] == "cyberquill"  # defaulted from name
        toks = call["tokens"]
        assert toks[-2:] == ["--", "cyberquill"]
        assert toks[toks.index("--enabled") + 1] == "true"
        assert toks[toks.index("--critical") + 1] == "true"

    def test_image_allowlist_fails_closed(self):
        from syrviscore_mcp.errors import ValidationError

        ctx, runner = make_declare_ctx(registries=())
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG)
        assert runner.ids() == []

    def test_wrong_registry_rejected(self):
        from syrviscore_mcp.errors import ValidationError

        ctx, runner = make_declare_ctx()
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", "docker.io/acme/cyberquill:1.4.0")
        assert runner.ids() == []

    def test_injection_attempts_rejected(self):
        from syrviscore_mcp.errors import ValidationError

        ctx, runner = make_declare_ctx()
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "x;id", DECLARE_IMG)
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG + ";id")
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG, subdomain="Bad_Sub")
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG, exposure="public")
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG, port=0)
        assert runner.ids() == []

    def test_non_bool_flags_rejected(self):
        from syrviscore_mcp.errors import ValidationError

        ctx, runner = make_declare_ctx()
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG, enabled="yes")
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "cyberquill", DECLARE_IMG, critical=1)
        assert runner.ids() == []

    def test_reserved_name_rejected(self):
        from syrviscore_mcp.errors import ValidationError

        ctx, runner = make_declare_ctx()
        with pytest.raises(ValidationError):
            tools.service_declare(ctx, "traefik", DECLARE_IMG)
        assert runner.ids() == []


class TestServiceAdopt:
    def test_happy_path_checks_membership_first(self):
        adopted = {"ok": True, "adopted": [{"name": "gollum", "path": "/x/gollum.yaml"}]}
        ctx, runner = make_ctx({"service_list": MANAGED, "service_adopt": adopted})
        out = tools.service_adopt(ctx, "gollum")
        assert runner.ids() == ["service_list", "service_adopt"]
        assert out["adopted"][0]["name"] == "gollum"
        call = next(c for c in runner.calls if c["id"] == "service_adopt")
        assert call["tokens"][-2:] == ["--", "gollum"]

    def test_reserved_core_name_rejected(self):
        # core services can't be adopted — RESERVED_NAMES rejection is correct here
        ctx, runner = make_ctx({"service_list": MANAGED})
        with pytest.raises(McpError):
            tools.service_adopt(ctx, "traefik")
        assert "service_adopt" not in runner.ids()

    def test_unmanaged_service_refused(self):
        ctx, runner = make_ctx({"service_list": {"services": []}})
        with pytest.raises(SandboxError):
            tools.service_adopt(ctx, "gollum")
        assert "service_adopt" not in runner.ids()


class TestPerProcessKey:
    def test_token_from_one_context_fails_in_another(self):
        # F8: each ToolContext salts the secret, so a restart voids tokens
        ctx1, _ = make_ctx({"versions_list": VERSIONS})
        ctx2, _ = make_ctx({"versions_list": VERSIONS})
        assert ctx1.secret != ctx2.secret
        plan = tools.activate(ctx1, "0.1.0")
        token = plan["confirm_token"]
        from syrviscore_mcp.errors import ConfirmationError

        with pytest.raises(ConfirmationError):
            tools.activate(ctx2, "0.1.0", confirm=token)
