"""Tests for the per-service Traefik domain override.

Covers:
  (a) service with traefik.domain produces Host(`subdomain.domain`) router rule
  (b) hostnames report emits the custom hostname with the right record type
  (c) NO domain => unchanged <subdomain>.<instance-domain> (regression guard)
  (d) invalid domain is rejected with ServiceValidationError
  (e) same subdomain on two different domains is allowed (no false conflict)
"""

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_BASE_MANIFEST: Dict[str, Any] = {
    "name": "photos",
    "version": "1.0.0",
    "image": "ghcr.io/example/photos:1.0",
    "traefik": {
        "enabled": True,
        "subdomain": "photos",
        "port": 2283,
        "exposure": "tunnel",
    },
}

_INSTANCE_DOMAIN = "konsume.org"


def _manifest(**traefik_overrides) -> Dict[str, Any]:
    """Return a copy of the base manifest with traefik overrides applied."""
    import copy

    m = copy.deepcopy(_BASE_MANIFEST)
    m["traefik"].update(traefik_overrides)
    return m


# ---------------------------------------------------------------------------
# (a) Traefik router rule uses the per-service domain
# ---------------------------------------------------------------------------


class TestTraefikRouterRule:
    def test_domain_override_produces_correct_host_rule(self):
        """Host() rule uses the per-service domain, not the instance domain."""
        from syrviscore.service_schema import ServiceDefinition
        from syrviscore.traefik_config import ServiceTraefikConfig

        svc = ServiceDefinition.from_dict(_manifest(domain="tegtmeier.me"))
        cfg_gen = ServiceTraefikConfig.__new__(ServiceTraefikConfig)  # skip __init__

        config = cfg_gen.generate_config(svc, _INSTANCE_DOMAIN)

        assert config, "expected non-empty config"
        routers = config["http"]["routers"]
        # Both the http-redirect router and the https router must use the overridden domain.
        assert routers["photos-http"]["rule"] == "Host(`photos.tegtmeier.me`)"
        assert routers["photos"]["rule"] == "Host(`photos.tegtmeier.me`)"

    def test_no_domain_override_uses_instance_domain(self):
        """When domain is absent the router rule uses the instance domain (regression)."""
        from syrviscore.service_schema import ServiceDefinition
        from syrviscore.traefik_config import ServiceTraefikConfig

        svc = ServiceDefinition.from_dict(_manifest())  # no domain key
        cfg_gen = ServiceTraefikConfig.__new__(ServiceTraefikConfig)

        config = cfg_gen.generate_config(svc, _INSTANCE_DOMAIN)

        routers = config["http"]["routers"]
        assert routers["photos-http"]["rule"] == f"Host(`photos.{_INSTANCE_DOMAIN}`)"
        assert routers["photos"]["rule"] == f"Host(`photos.{_INSTANCE_DOMAIN}`)"

    def test_empty_string_domain_uses_instance_domain(self):
        """Explicit empty string for domain is equivalent to omitting it."""
        from syrviscore.service_schema import ServiceDefinition
        from syrviscore.traefik_config import ServiceTraefikConfig

        svc = ServiceDefinition.from_dict(_manifest(domain=""))
        cfg_gen = ServiceTraefikConfig.__new__(ServiceTraefikConfig)

        config = cfg_gen.generate_config(svc, _INSTANCE_DOMAIN)

        routers = config["http"]["routers"]
        assert routers["photos"]["rule"] == f"Host(`photos.{_INSTANCE_DOMAIN}`)"

    def test_domain_override_roundtrips_through_yaml(self, tmp_path):
        """domain survives a to_dict/from_dict round-trip (manifest serialization)."""
        from syrviscore.service_schema import ServiceDefinition

        svc = ServiceDefinition.from_dict(_manifest(domain="tegtmeier.me"))
        serialized = svc.to_dict()

        assert serialized["traefik"]["domain"] == "tegtmeier.me"

        restored = ServiceDefinition.from_dict(serialized)
        assert restored.traefik.domain == "tegtmeier.me"

    def test_no_domain_not_in_serialized_dict(self):
        """When domain is empty it is omitted from to_dict() output (no noise)."""
        from syrviscore.service_schema import ServiceDefinition

        svc = ServiceDefinition.from_dict(_manifest())
        serialized = svc.to_dict()

        assert "domain" not in serialized.get("traefik", {})


# ---------------------------------------------------------------------------
# (b) hostnames report emits the correct custom hostname + record type
# ---------------------------------------------------------------------------


def _fake_config(domain=_INSTANCE_DOMAIN, traefik_ip="10.0.0.1"):
    cfg = MagicMock()
    cfg.domain = domain
    cfg.traefik_ip = traefik_ip
    cfg.values = {"TRAEFIK_IP": traefik_ip}
    cfg.enabled_components = {}
    return cfg


# ---------------------------------------------------------------------------
# Helper: call the Layer-2 section of build_report in isolation
# ---------------------------------------------------------------------------


def _build_report_l2_only(svc_info: Dict[str, Any], cfg) -> Dict[str, Any]:
    """Exercise only the hostnames Layer-2 logic with a single fake service.

    Suppresses primordial UIs, Synology services, and the stack module so only
    the Layer-2 entry produced by the fake service appears in the report.
    Compatible with Python 3.8 (nested with statements, no parenthesized form).

    ServiceManager is imported lazily inside build_report, so we patch it at
    the service_manager module level rather than on the hostnames module.
    """
    import sys
    from syrviscore import hostnames as hm

    # Silence stack loading by making load_stack raise.
    fake_stack_mod = MagicMock()
    fake_stack_mod.load_stack.side_effect = Exception("disabled in test")
    orig_stack = sys.modules.get("syrviscore.stack")
    sys.modules["syrviscore.stack"] = fake_stack_mod

    original_primordial = hm._PRIMORDIAL_UIS
    hm._PRIMORDIAL_UIS = ()
    try:
        with patch.object(hm, "read_config", return_value=cfg):
            # ServiceManager is imported inside build_report via a local import,
            # so patch the class at its source module.
            with patch(
                "syrviscore.service_manager.ServiceManager",
                return_value=MagicMock(list=MagicMock(return_value=[svc_info])),
            ):
                report = hm.build_report()
    finally:
        hm._PRIMORDIAL_UIS = original_primordial
        if orig_stack is None:
            sys.modules.pop("syrviscore.stack", None)
        else:
            sys.modules["syrviscore.stack"] = orig_stack

    return report


class TestHostnamesReport:
    def test_custom_domain_hostname_in_report(self):
        """Layer 2 service with domain override appears with the custom hostname."""
        svc_info = {
            "name": "photos",
            "subdomain": "photos",
            "domain": "tegtmeier.me",
            "exposure": "tunnel",
            "status": "running",
        }
        report = _build_report_l2_only(svc_info, _fake_config())

        entry = next((e for e in report["entries"] if e["service"] == "photos"), None)
        assert entry is not None, "photos entry missing from report"
        assert entry["hostname"] == "photos.tegtmeier.me"
        assert entry["record"]["type"] == "CNAME"  # tunnel exposure => CNAME
        assert entry["record"]["name"] == "photos.tegtmeier.me"

    def test_no_domain_override_uses_instance_domain_in_report(self):
        """Layer 2 service without domain override uses the instance domain."""
        svc_info = {
            "name": "photos",
            "subdomain": "photos",
            "domain": "",
            "exposure": "internal",
            "status": "running",
        }
        report = _build_report_l2_only(svc_info, _fake_config())

        entry = next((e for e in report["entries"] if e["service"] == "photos"), None)
        assert entry is not None
        assert entry["hostname"] == "photos.{}".format(_INSTANCE_DOMAIN)
        assert entry["record"]["type"] == "A"  # internal exposure => A record

    def test_tunnel_exposure_with_custom_domain_emits_cname(self):
        """tunnel + custom domain => CNAME record."""
        svc_info = {
            "name": "myapp",
            "subdomain": "app",
            "domain": "tegtmeier.me",
            "exposure": "tunnel",
            "status": "running",
        }
        report = _build_report_l2_only(svc_info, _fake_config())

        entry = next((e for e in report["entries"] if e["service"] == "myapp"), None)
        assert entry is not None
        assert entry["hostname"] == "app.tegtmeier.me"
        assert entry["record"]["type"] == "CNAME"

    def test_internal_exposure_with_custom_domain_emits_a_record(self):
        """internal + custom domain => A record pointing at traefik_ip."""
        svc_info = {
            "name": "myapp",
            "subdomain": "app",
            "domain": "tegtmeier.me",
            "exposure": "internal",
            "status": "running",
        }
        report = _build_report_l2_only(svc_info, _fake_config(traefik_ip="192.168.1.50"))

        entry = next((e for e in report["entries"] if e["service"] == "myapp"), None)
        assert entry is not None
        assert entry["record"]["type"] == "A"
        assert entry["record"]["target"] == "192.168.1.50"


# ---------------------------------------------------------------------------
# (c) Regression guard — default behavior unchanged
# ---------------------------------------------------------------------------


class TestDefaultDomainRegression:
    """No per-service domain => every existing code path is byte-for-byte the same."""

    def test_schema_default_domain_is_empty_string(self):
        from syrviscore.service_schema import TraefikConfig

        tc = TraefikConfig()
        assert tc.domain == ""

    def test_from_dict_without_domain_key(self):
        from syrviscore.service_schema import TraefikConfig

        tc = TraefikConfig.from_dict({"enabled": True, "subdomain": "foo", "port": 80})
        assert tc.domain == ""

    def test_service_definition_no_domain_parses_cleanly(self):
        from syrviscore.service_schema import ServiceDefinition

        svc = ServiceDefinition.from_dict(_manifest())
        assert svc.traefik.domain == ""

    def test_generate_config_no_domain_uses_instance_domain(self):
        from syrviscore.service_schema import ServiceDefinition
        from syrviscore.traefik_config import ServiceTraefikConfig

        svc = ServiceDefinition.from_dict(_manifest())
        cfg_gen = ServiceTraefikConfig.__new__(ServiceTraefikConfig)
        config = cfg_gen.generate_config(svc, "example.com")

        rule = config["http"]["routers"]["photos"]["rule"]
        assert rule == "Host(`photos.example.com`)"


# ---------------------------------------------------------------------------
# (d) Invalid domain rejected
# ---------------------------------------------------------------------------


class TestInvalidDomainRejected:
    def _expect_invalid(self, bad_domain: str):
        from syrviscore.service_schema import ServiceDefinition, ServiceValidationError

        with pytest.raises(ServiceValidationError, match="domain"):
            ServiceDefinition.from_dict(_manifest(domain=bad_domain))

    def test_rejects_single_label(self):
        """A bare label (no dot) is not a valid domain."""
        self._expect_invalid("localhost")

    def test_rejects_trailing_dot(self):
        """Trailing dots are not allowed (FQDN syntax rejected at schema level)."""
        self._expect_invalid("tegtmeier.me.")

    def test_rejects_uppercase(self):
        """Domain values are normalized to lowercase; uppercase in the raw YAML
        is fine (from_dict lowercases it) — but a value that doesn't match after
        normalization is not possible since normalization happens first.
        This test verifies uppercase is accepted after lowercasing."""
        from syrviscore.service_schema import ServiceDefinition

        # from_dict lowercases the domain; "Tegtmeier.ME" → "tegtmeier.me"
        svc = ServiceDefinition.from_dict(_manifest(domain="Tegtmeier.ME"))
        assert svc.traefik.domain == "tegtmeier.me"

    def test_rejects_label_with_underscore(self):
        """Underscores are not valid in DNS labels per SUBDOMAIN_RE."""
        self._expect_invalid("my_domain.me")

    def test_rejects_empty_label(self):
        """Double-dot (empty label) is invalid."""
        self._expect_invalid("foo..me")

    def test_rejects_label_starting_with_hyphen(self):
        """Labels cannot start with a hyphen."""
        self._expect_invalid("-bad.me")

    def test_valid_two_label_domain_accepted(self):
        from syrviscore.service_schema import ServiceDefinition

        svc = ServiceDefinition.from_dict(_manifest(domain="tegtmeier.me"))
        assert svc.traefik.domain == "tegtmeier.me"

    def test_valid_three_label_domain_accepted(self):
        from syrviscore.service_schema import ServiceDefinition

        svc = ServiceDefinition.from_dict(_manifest(domain="sub.tegtmeier.me"))
        assert svc.traefik.domain == "sub.tegtmeier.me"


# ---------------------------------------------------------------------------
# (e) Same subdomain on two different domains is NOT a conflict
# ---------------------------------------------------------------------------


class TestSubdomainCollisionPerDomain:
    def _make_syrvis_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "syrviscore"
        (home / "config" / "services.d").mkdir(parents=True)
        (home / "data").mkdir(parents=True)
        (home / "services").mkdir(parents=True)
        (home / "compose").mkdir(parents=True)
        (home / "data" / "traefik" / "config" / "dynamic").mkdir(parents=True)
        return home

    def _installed_service_info(self, name, subdomain, domain=""):
        return {
            "name": name,
            "subdomain": subdomain,
            "domain": domain,
            "exposure": "internal",
            "status": "running",
        }

    def test_same_subdomain_different_domains_no_conflict(self, tmp_path):
        """photos.konsume.org and photos.tegtmeier.me are distinct: no collision."""
        from syrviscore.service_manager import ServiceManager

        home = self._make_syrvis_home(tmp_path)
        mgr = ServiceManager(syrvis_home=home)

        # Patch list() to return one installed service on konsume.org.
        existing = [self._installed_service_info("photos-konsume", "photos", domain="")]

        with patch.object(mgr, "list", return_value=existing):
            # Should NOT find a conflict when the new service uses tegtmeier.me.
            owner = mgr._subdomain_in_use("photos", domain="tegtmeier.me")

        assert owner is None, (
            "Expected no conflict but _subdomain_in_use returned {!r}".format(owner)
        )

    def test_same_subdomain_same_domain_is_a_conflict(self, tmp_path):
        """photos.tegtmeier.me added twice IS a conflict."""
        from syrviscore.service_manager import ServiceManager

        home = self._make_syrvis_home(tmp_path)
        mgr = ServiceManager(syrvis_home=home)

        existing = [self._installed_service_info("photos", "photos", domain="tegtmeier.me")]

        with patch.object(mgr, "list", return_value=existing):
            owner = mgr._subdomain_in_use("photos", domain="tegtmeier.me")

        assert owner == "photos"

    def test_same_subdomain_same_implicit_domain_is_conflict(self, tmp_path):
        """Two services at photos.<instance-domain> (domain='') conflict."""
        from syrviscore.service_manager import ServiceManager

        home = self._make_syrvis_home(tmp_path)
        mgr = ServiceManager(syrvis_home=home)

        existing = [self._installed_service_info("photos-old", "photos", domain="")]

        with patch.object(mgr, "list", return_value=existing):
            owner = mgr._subdomain_in_use("photos", domain="")

        assert owner == "photos-old"

    def test_exclude_self_no_false_conflict(self, tmp_path):
        """When updating a service, its own subdomain+domain must not block itself."""
        from syrviscore.service_manager import ServiceManager

        home = self._make_syrvis_home(tmp_path)
        mgr = ServiceManager(syrvis_home=home)

        existing = [self._installed_service_info("photos", "photos", domain="tegtmeier.me")]

        with patch.object(mgr, "list", return_value=existing):
            owner = mgr._subdomain_in_use("photos", domain="tegtmeier.me", exclude="photos")

        assert owner is None


# ---------------------------------------------------------------------------
# TraefikConfig.from_dict domain normalization
# ---------------------------------------------------------------------------


class TestTraefikConfigFromDict:
    def test_domain_stripped_and_lowercased(self):
        from syrviscore.service_schema import TraefikConfig

        tc = TraefikConfig.from_dict(
            {"enabled": True, "subdomain": "foo", "port": 80, "domain": "  Example.COM  "}
        )
        assert tc.domain == "example.com"

    def test_none_domain_becomes_empty_string(self):
        from syrviscore.service_schema import TraefikConfig

        tc = TraefikConfig.from_dict(
            {"enabled": True, "subdomain": "foo", "port": 80, "domain": None}
        )
        assert tc.domain == ""

    def test_missing_domain_becomes_empty_string(self):
        from syrviscore.service_schema import TraefikConfig

        tc = TraefikConfig.from_dict({"enabled": True, "subdomain": "foo", "port": 80})
        assert tc.domain == ""
