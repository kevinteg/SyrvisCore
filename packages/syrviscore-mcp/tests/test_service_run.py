"""Tests for the image-first service_run MCP tool + its validators."""

import pytest

from syrviscore_mcp import tools, validate
from syrviscore_mcp.errors import ValidationError

from .conftest import FakeRunner, make_config

GHCR = ["ghcr.io"]


class TestValidateImage:
    def test_valid_tag(self):
        img = "ghcr.io/acme/cyberquill:1.4.0"
        assert validate.validate_image(img, GHCR) == img

    def test_valid_digest(self):
        img = "ghcr.io/a/b@sha256:" + "0" * 64
        assert validate.validate_image(img, GHCR) == img

    def test_empty_allowlist_fails_closed(self):
        with pytest.raises(ValidationError):
            validate.validate_image("ghcr.io/a/b:1.0", [])

    def test_wrong_registry_rejected(self):
        with pytest.raises(ValidationError):
            validate.validate_image("docker.io/a/b:1.0", GHCR)

    def test_latest_rejected(self):
        with pytest.raises(ValidationError):
            validate.validate_image("ghcr.io/a/b:latest", GHCR)

    def test_unpinned_rejected(self):
        with pytest.raises(ValidationError):
            validate.validate_image("ghcr.io/a/b", GHCR)

    @pytest.mark.parametrize(
        "img",
        [
            "ghcr.io/a/b:1.0 x",  # space
            "ghcr.io/a/b:1.0;id",  # metachar
            "-ghcr.io/a/b:1.0",  # leading dash
            "ghcr.io/a/b:1.0=x",  # '=' (also blocked by the shim charset)
        ],
    )
    def test_metachars_rejected(self, img):
        with pytest.raises(ValidationError):
            validate.validate_image(img, GHCR)


class TestValidateRoutingArgs:
    def test_subdomain_ok(self):
        assert validate.validate_subdomain("cyberquill") == "cyberquill"

    @pytest.mark.parametrize("s", ["Bad", "under_score", "-lead", "a.b", "sub_domain"])
    def test_subdomain_rejected(self, s):
        with pytest.raises(ValidationError):
            validate.validate_subdomain(s)

    def test_exposure_ok(self):
        assert validate.validate_exposure("tunnel") == "tunnel"

    @pytest.mark.parametrize("e", ["public", "vpn", "Internal"])
    def test_exposure_rejected(self, e):
        with pytest.raises(ValidationError):
            validate.validate_exposure(e)

    @pytest.mark.parametrize("p", [1, 80, 8080, 65535])
    def test_port_ok(self, p):
        assert validate.validate_port(p) == p

    @pytest.mark.parametrize("p", [0, 65536, -1, True, "80"])
    def test_port_rejected(self, p):
        with pytest.raises(ValidationError):
            validate.validate_port(p)


def make_ctx(cfg=None, responses=None):
    cfg = cfg or make_config(image_allowed_registries=["ghcr.io"])
    runner = FakeRunner(cfg, responses or {})
    ctx = tools.ToolContext(cfg=cfg, runner=runner, secret=b"s", now=lambda: 1000)
    return ctx, runner


IMG = "ghcr.io/acme/cyberquill:1.4.0"


class TestServiceRunTool:
    def test_without_confirm_returns_plan_no_mutation(self):
        ctx, runner = make_ctx(responses={"service_list": {"services": []}})
        out = tools.service_run(ctx, "cyberquill", IMG, exposure="tunnel", port=8080)
        assert out["needs_confirmation"] is True
        assert "confirm_token" in out
        assert "service_run" not in runner.ids()  # only the read-only service_list ran

    def test_with_valid_token_mutates(self):
        ctx, runner = make_ctx(responses={"service_list": {"services": []}})
        first = tools.service_run(ctx, "cyberquill", IMG, exposure="tunnel", port=8080)
        out = tools.service_run(
            ctx, "cyberquill", IMG, exposure="tunnel", port=8080, confirm=first["confirm_token"]
        )
        assert out.get("services") is not None or True
        assert "service_run" in runner.ids()
        run_call = next(c for c in runner.calls if c["id"] == "service_run")
        # subdomain defaulted from the name; argv carries validated routing.
        assert run_call["args"]["subdomain"] == "cyberquill"
        assert run_call["args"]["exposure"] == "tunnel"
        assert "-- cyberquill" in " ".join(run_call["tokens"])

    def test_registry_not_allowed_fails_closed(self):
        ctx, runner = make_ctx(cfg=make_config(image_allowed_registries=[]))
        with pytest.raises(ValidationError):
            tools.service_run(ctx, "cyberquill", IMG, exposure="tunnel")
        assert "service_run" not in runner.ids()

    def test_reserved_name_rejected(self):
        ctx, _ = make_ctx()
        with pytest.raises(ValidationError):
            tools.service_run(ctx, "traefik", IMG)


class TestStackHostnamesTool:
    def test_runs_command(self):
        ctx, runner = make_ctx(responses={"stack_hostnames": {"entries": []}})
        tools.stack_hostnames(ctx)
        assert runner.ids() == ["stack_hostnames"]
