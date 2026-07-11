"""Tests for the exposure vocabulary and its use in the L2 service schema."""

import pytest

from syrviscore import exposure
from syrviscore.service_schema import ServiceDefinition, ServiceValidationError


class TestExposureModule:
    def test_defaults_and_values(self):
        assert exposure.DEFAULT == "internal"
        assert exposure.EXPOSURES == ("internal", "tunnel")

    @pytest.mark.parametrize("value", [None, ""])
    def test_normalize_empty_is_default(self, value):
        assert exposure.normalize(value) == "internal"

    @pytest.mark.parametrize(
        "value,expected",
        [("tunnel", "tunnel"), ("Tunnel", "tunnel"), ("  INTERNAL ", "internal")],
    )
    def test_normalize_canonicalizes(self, value, expected):
        assert exposure.normalize(value) == expected

    @pytest.mark.parametrize("value", ["public", "vpn", "lan", "wan"])
    def test_normalize_rejects_unknown(self, value):
        with pytest.raises(ValueError):
            exposure.normalize(value)

    def test_is_valid(self):
        assert exposure.is_valid("tunnel")
        assert not exposure.is_valid("public")


class TestSchemaExposure:
    def _svc(self, **traefik):
        data = {
            "name": "svc",
            "version": "1.0.0",
            "image": "ghcr.io/acme/svc:1.0.0",
            "traefik": {"enabled": True, "subdomain": "svc", "port": 80, **traefik},
        }
        return ServiceDefinition.from_dict(data)

    def test_default_exposure_is_internal(self):
        svc = self._svc()
        assert svc.traefik.exposure == "internal"

    def test_tunnel_round_trips_through_to_dict(self):
        svc = self._svc(exposure="tunnel")
        assert svc.traefik.exposure == "tunnel"
        assert svc.to_dict()["traefik"]["exposure"] == "tunnel"

    def test_bad_exposure_rejected(self):
        with pytest.raises(ServiceValidationError):
            self._svc(exposure="public")
