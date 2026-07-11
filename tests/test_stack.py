"""Tests for the declarative core-stack (config/stack.yaml)."""

import pytest

from syrviscore import stack


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    return h


def test_default_stack_is_opt_in():
    s = stack.default_stack()
    assert s.is_enabled("traefik") and s.is_enabled("portainer")  # primordial
    assert not s.is_enabled("dashboard")
    assert not s.is_enabled("cloudflared")
    assert not s.is_enabled("cloudflare_ddns")
    assert s.setting("dashboard", "subdomain") == "dash"


def test_primordial_cannot_be_disabled(home):
    with pytest.raises(stack.StackError):
        stack.set_enabled("portainer", False)


def test_unknown_service_rejected(home):
    with pytest.raises(stack.StackError):
        stack.set_enabled("nope", True)


def test_enable_persists_with_settings(home):
    stack.set_enabled("dashboard", True, {"subdomain": "panel"})
    s = stack.load_stack()
    assert s.is_enabled("dashboard")
    assert s.setting("dashboard", "subdomain") == "panel"
    assert stack.get_stack_path().exists()


def test_disable_persists(home):
    stack.set_enabled("cloudflared", True)
    assert stack.load_stack().is_enabled("cloudflared")
    stack.set_enabled("cloudflared", False)
    assert not stack.load_stack().is_enabled("cloudflared")


def test_infer_when_no_file_preserves_cloudflared(home, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    s = stack.load_stack()  # no stack.yaml -> infer
    assert s.is_enabled("cloudflared")  # pre-stack behavior preserved
    assert not s.is_enabled("dashboard")  # new -> opt-in
    assert not s.is_enabled("cloudflare_ddns")


def test_infer_enables_ddns_from_token(home, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "x")
    assert stack.load_stack().is_enabled("cloudflare_ddns")


def test_yaml_roundtrip(home):
    stack.set_enabled("dashboard", True)
    text = stack.get_stack_path().read_text()
    assert "dashboard" in text and "enabled: true" in text
    assert stack.load_stack().is_enabled("dashboard")


def test_from_dict_forces_primordial_on():
    s = stack.from_dict({"services": {"traefik": {"enabled": False}}})
    assert s.is_enabled("traefik")


def test_enabled_services_order(home):
    stack.set_enabled("dashboard", True)
    stack.set_enabled("cloudflared", True)
    # returned in canonical ALL_SERVICES order
    assert stack.load_stack().enabled_services() == [
        "traefik",
        "portainer",
        "cloudflared",
        "dashboard",
    ]
